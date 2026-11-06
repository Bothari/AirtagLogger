"""
Login, first-run setup, profile, OIDC single sign-on, and admin user management.

The OIDC flow is ported from Athenaeum (app/routes/auth.py there), keeping its
hardening: discovery issuer pinned to the configured provider URL, PKCE, signed
state cookie, nonce/aud/azp/iss checks, and identity keyed only on (iss, sub) --
never merged onto an existing account by email or username. On top of that a
logged-in user can explicitly link an SSO identity to their own account.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel

from app import auth
from app.auth import require_admin, require_session, require_user
from app.database import get_db, get_setting, set_setting

log = logging.getLogger(__name__)
router = APIRouter()

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def _validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Username may only contain letters, digits and . _ @ -")
    return username


# ── OIDC settings ───────────────────────────────────────

OIDC_KEYS = ("oidc_enabled", "oidc_provider_url", "oidc_client_id", "oidc_client_secret",
             "oidc_scopes", "oidc_button_label", "oidc_auto_provision", "public_url")


def _configured(cfg: dict) -> bool:
    return bool(cfg["oidc_provider_url"] and cfg["oidc_client_id"])


async def _oidc_cfg(db) -> dict:
    cfg = {k: await get_setting(db, k, "") for k in OIDC_KEYS}
    cfg["oidc_enabled"] = cfg["oidc_enabled"] == "1"
    cfg["oidc_auto_provision"] = cfg["oidc_auto_provision"] != "0"  # default on
    cfg["oidc_scopes"] = cfg["oidc_scopes"] or "openid email profile"
    cfg["oidc_button_label"] = cfg["oidc_button_label"] or "Sign in with SSO"
    return cfg


# When SSO is on, it is the only way in: password sign-in, password changes and
# admin-set passwords are all refused. If the identity provider breaks, an admin
# with shell access runs `python -m app.recover disable-sso` in the container.
SSO_ONLY = "Password sign-in is turned off while single sign-on is enabled"


async def _sso_only(db) -> bool:
    return (await get_setting(db, "oidc_enabled", "")) == "1"


# ── Status / setup ──────────────────────────────────────

@router.get("/auth/config")
async def auth_config(db: aiosqlite.Connection = Depends(get_db)):
    """Unauthenticated: what the login page should offer."""
    cfg = await _oidc_cfg(db)
    return {
        "setup_required": auth.setup_pending(),
        "oidc_enabled": cfg["oidc_enabled"],
        "oidc_button_label": cfg["oidc_button_label"],
        "local_login": not cfg["oidc_enabled"],
        # lets a signed-in user link SSO before it is switched on
        "oidc_configured": _configured(cfg),
    }


class SetupBody(BaseModel):
    token: str
    username: str
    password: str


@router.post("/auth/setup")
async def setup(body: SetupBody, request: Request, response: Response,
                db: aiosqlite.Connection = Depends(get_db)):
    ip = auth.client_ip(request)
    auth.check_rate_limit(f"ip:{ip}")
    if not auth.setup_pending():
        raise HTTPException(400, "Setup already completed")
    username = _validate_username(body.username)
    auth.validate_password(body.password)
    if not auth.consume_setup_token(body.token):
        auth.record_failure(f"ip:{ip}")
        raise HTTPException(403, "Invalid setup token (see the container log)")
    user_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO users (id, username, role, password_hash, last_login_at) VALUES (?, ?, 'admin', ?, CURRENT_TIMESTAMP)",
        (user_id, username, auth.hash_password(body.password)),
    )
    await db.commit()
    log.info("Initial admin account %r created", username)
    auth.set_session_cookie(response, request, user_id, 1)
    return {"ok": True}


# ── Local login ─────────────────────────────────────────

class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response,
                db: aiosqlite.Connection = Depends(get_db)):
    if await _sso_only(db):
        raise HTTPException(403, SSO_ONLY)
    ip_key = f"ip:{auth.client_ip(request)}"
    user_key = f"user:{body.username.strip().lower()}"
    auth.check_rate_limit(ip_key, user_key)

    row = await (await db.execute(
        "SELECT * FROM users WHERE username = ?", (body.username.strip(),)
    )).fetchone()
    if row is None:
        auth.burn_password_check(body.password)
    if row is None or not auth.verify_password(body.password, row["password_hash"]):
        auth.record_failure(ip_key, user_key)
        raise HTTPException(401, "Invalid username or password")

    auth.clear_failures(user_key)
    await db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    await db.commit()
    auth.set_session_cookie(response, request, row["id"], row["session_version"])
    return auth.public_user(row)


@router.post("/auth/logout")
async def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
async def me(user=Depends(require_session)):
    return auth.public_user(user)


class ChangePasswordBody(BaseModel):
    current_password: Optional[str] = None
    new_password: str


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordBody, request: Request, response: Response,
                          user=Depends(require_session), db: aiosqlite.Connection = Depends(get_db)):
    if await _sso_only(db):
        raise HTTPException(400, SSO_ONLY)
    # SSO-only users setting a first password have no current one to prove.
    if user["password_hash"] and not auth.verify_password(body.current_password or "", user["password_hash"]):
        raise HTTPException(401, "Current password is incorrect")
    auth.validate_password(body.new_password)
    new_version = user["session_version"] + 1
    await db.execute(
        "UPDATE users SET password_hash = ?, force_password_change = 0, session_version = ? WHERE id = ?",
        (auth.hash_password(body.new_password), new_version, user["id"]),
    )
    await db.commit()
    # Other sessions are now invalid; keep this one alive.
    auth.set_session_cookie(response, request, user["id"], new_version)
    return {"ok": True}


# Preferences users may set on themselves. The Pushover key also lives in prefs
# but is admin-managed (see patch_user), so users can't redirect alerts.
ALLOWED_PREFS = {
    "default_range_hours": int,   # map time range on load (0 = all)
}


@router.patch("/auth/me/prefs")
async def update_prefs(body: dict, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    prefs = json.loads(user["prefs"] or "{}")
    for key, value in body.items():
        if key not in ALLOWED_PREFS:
            raise HTTPException(400, f"Unknown preference: {key}")
        if value is None or value == "":
            prefs.pop(key, None)
            continue
        try:
            prefs[key] = ALLOWED_PREFS[key](value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid value for {key}")
    await db.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps(prefs), user["id"]))
    await db.commit()
    return prefs


@router.post("/auth/unlink-sso")
async def unlink_sso(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    if await _sso_only(db):
        raise HTTPException(400, "SSO is the only way to sign in, so it can't be unlinked")
    if not user["password_hash"]:
        raise HTTPException(400, "Set a password first, or you will be locked out")
    await db.execute("UPDATE users SET oidc_iss = NULL, oidc_sub = NULL WHERE id = ?", (user["id"],))
    await db.commit()
    return {"ok": True}


# ── OIDC ────────────────────────────────────────────────

_OIDC_STATE_COOKIE = "__Host-oidc_state"
_discovery_cache: dict[str, tuple[float, dict]] = {}
_discovery_lock = asyncio.Lock()


async def _get_oidc_config(provider_url: str) -> dict:
    async with _discovery_lock:
        cached = _discovery_cache.get(provider_url)
        if cached and cached[0] > time.time():
            return cached[1]
    base = provider_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(base + "/.well-known/openid-configuration")
        resp.raise_for_status()
        config = resp.json()
    # Pin the discovery issuer to the configured URL; otherwise a spoofed discovery
    # document could name any issuer and we would verify tokens against it.
    if str(config.get("issuer", "")).rstrip("/") != base:
        raise HTTPException(400, f"OIDC discovery issuer mismatch: {config.get('issuer')!r} != {provider_url!r}")
    async with _discovery_lock:
        _discovery_cache[provider_url] = (time.time() + 3600, config)
    return config


_VALID_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:[0-9]+)?$")


def _clean_host(value: str | None) -> str:
    value = (value or "").strip()
    return value if value and _VALID_HOST_RE.match(value) else ""


def _redirect_uri(request: Request, cfg: dict) -> str:
    public_url = cfg["public_url"].rstrip("/")
    if public_url:
        return f"{public_url}/api/auth/oidc/callback"
    host = (_clean_host(request.headers.get("x-forwarded-host"))
            or _clean_host(request.headers.get("host")))
    if not host:
        raise HTTPException(400, "Cannot determine OIDC redirect URI; set the public URL in settings")
    return f"https://{host}/api/auth/oidc/callback"


def _done_page(target: str) -> HTMLResponse:
    """200 page that redirects client-side. Cookies set on a 200 survive reliably;
    on a 302 inside a cross-site redirect chain some browsers drop them."""
    html = f"<!DOCTYPE html><html><head><meta http-equiv='refresh' content='0;url={target}'></head><body></body></html>"
    resp = HTMLResponse(html, headers={"Cache-Control": "no-store"})
    resp.delete_cookie(_OIDC_STATE_COOKIE, path="/", secure=True, samesite="lax", httponly=True)
    return resp


class _OidcError(Exception):
    """Expected callback failure. `code` goes to the browser (fixed vocabulary);
    `detail` is only logged."""
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@router.get("/auth/oidc/start")
async def oidc_start(request: Request, link: int = 0, db: aiosqlite.Connection = Depends(get_db)):
    cfg = await _oidc_cfg(db)
    # Linking is allowed once the provider is configured, before SSO is switched
    # on: admins must link their own account first (see put_sso).
    if not cfg["oidc_enabled"] and not (link and _configured(cfg)):
        raise HTTPException(400, "SSO is not enabled")

    link_user_id = None
    if link:
        user = await auth.user_from_request(request, db)
        if user is None:
            raise HTTPException(401, "Log in before linking SSO")
        link_user_id = user["id"]

    config = await _get_oidc_config(cfg["oidc_provider_url"])
    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = _redirect_uri(request, cfg)

    signed_state = jwt.encode({
        "state": state, "nonce": nonce, "cv": verifier, "redirect_uri": redirect_uri,
        "link": link_user_id, "exp": int(time.time()) + 600,
    }, auth.session_secret(), algorithm=auth.ALGORITHM)

    params = {
        "response_type": "code",
        "client_id": cfg["oidc_client_id"],
        "redirect_uri": redirect_uri,
        "scope": cfg["oidc_scopes"],
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    resp = RedirectResponse(f"{config['authorization_endpoint']}?{urlencode(params)}", status_code=302)
    resp.set_cookie(_OIDC_STATE_COOKIE, signed_state, httponly=True, samesite="lax",
                    secure=True, max_age=600, path="/")
    return resp


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "",
                        db: aiosqlite.Connection = Depends(get_db)):
    raw = request.cookies.get(_OIDC_STATE_COOKIE)
    try:
        if not raw:
            raise _OidcError("expired", "No OIDC state cookie")
        return await _oidc_callback_inner(request, db, raw, code, state)
    except _OidcError as e:
        log.warning("OIDC callback rejected (%s): %s", e.code, e.detail)
        return _done_page(f"/?sso_error={e.code}")
    except Exception:
        log.exception("OIDC callback failed unexpectedly")
        return _done_page("/?sso_error=failed")


async def _oidc_callback_inner(request: Request, db, raw_state: str, code: str, state: str) -> Response:
    try:
        st = jwt.decode(raw_state, auth.session_secret(), algorithms=[auth.ALGORITHM])
    except JWTError:
        raise _OidcError("expired", "Invalid or expired state cookie")
    if not state or st.get("state") != state:
        raise _OidcError("failed", "State mismatch")

    cfg = await _oidc_cfg(db)
    if not cfg["oidc_enabled"] and not (st.get("link") and _configured(cfg)):
        raise _OidcError("disabled", "SSO disabled")
    client_id = cfg["oidc_client_id"]
    try:
        config = await _get_oidc_config(cfg["oidc_provider_url"])
    except HTTPException as e:
        raise _OidcError("failed", str(e.detail))

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(config["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": st["redirect_uri"],
            "client_id": client_id,
            "client_secret": cfg["oidc_client_secret"],
            "code_verifier": st["cv"],
        })
        if not token_resp.is_success:
            raise _OidcError("failed", f"Token exchange {token_resp.status_code}: {token_resp.text[:500]}")
        token_data = token_resp.json()
        jwks_resp = await client.get(config["jwks_uri"])
        jwks_resp.raise_for_status()
        jwks = jwks_resp.json()

    id_token = token_data.get("id_token")
    access_token = token_data.get("access_token")
    if not isinstance(id_token, str) or not id_token:
        raise _OidcError("failed", "No id_token in token response")

    try:
        claims = jwt.decode(
            id_token, jwks,
            # Asymmetric only: an HMAC alg against a JWKS is an algorithm-confusion vector.
            algorithms=["RS256", "ES256"],
            audience=client_id,
            issuer=config["issuer"],
            access_token=access_token if isinstance(access_token, str) else None,
            options={"require_aud": True, "require_exp": True, "require_iat": True,
                     "require_iss": True, "require_sub": True},
        )
    except JWTError as e:
        raise _OidcError("failed", f"ID token verification failed: {e}")

    azp = claims.get("azp")
    if azp is not None and azp != client_id:
        raise _OidcError("failed", "azp does not match client_id")
    aud = claims.get("aud")
    if isinstance(aud, (list, tuple)) and set(aud) - {client_id}:
        raise _OidcError("failed", "Untrusted extra audiences")
    if claims.get("nonce") != st.get("nonce"):
        raise _OidcError("failed", "Nonce mismatch")

    sub, iss = claims.get("sub"), claims.get("iss")
    if not isinstance(sub, str) or not sub or not isinstance(iss, str) or not iss:
        raise _OidcError("failed", "Missing sub/iss")
    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
    # Only boolean True counts; many IdPs send the string "false".
    email_verified = claims.get("email_verified") is True

    existing = await (await db.execute(
        "SELECT * FROM users WHERE oidc_iss = ? AND oidc_sub = ?", (iss, sub)
    )).fetchone()

    # Linking: attach this identity to the account that started the flow.
    if st.get("link"):
        current = await auth.user_from_request(request, db)
        if current is None or current["id"] != st["link"]:
            raise _OidcError("failed", "Link started by a different session")
        if existing and existing["id"] != current["id"]:
            raise _OidcError("already_linked", f"Identity already linked to user {existing['username']}")
        await db.execute("UPDATE users SET oidc_iss = ?, oidc_sub = ? WHERE id = ?", (iss, sub, current["id"]))
        if email_verified and email and not current["email"]:
            await db.execute("UPDATE users SET email = ? WHERE id = ?", (email, current["id"]))
        await db.commit()
        log.info("Linked SSO identity to user %r", current["username"])
        return _done_page("/?sso_linked=1")

    if existing:
        user = existing
    else:
        if not cfg["oidc_auto_provision"]:
            raise _OidcError("not_provisioned", f"Unknown SSO identity {sub} and auto-provisioning is off")
        preferred = claims.get("preferred_username")
        preferred = preferred.strip() if isinstance(preferred, str) else ""
        base = (email.split("@")[0] if email_verified and email else preferred or sub[:20])
        base = re.sub(r"[^A-Za-z0-9._@-]", "", base)[:32] or "user"
        user = await _provision(db, iss, sub, base, email if email_verified else "")

    # Signing in through SSO already proves identity; a pending "choose a new
    # password" would otherwise lock them out, since passwords are off with SSO.
    await db.execute("UPDATE users SET last_login_at = CURRENT_TIMESTAMP, force_password_change = 0 WHERE id = ?",
                     (user["id"],))
    await db.commit()
    resp = _done_page("/")
    auth.set_session_cookie(resp, request, user["id"], user["session_version"])
    return resp


async def _provision(db, iss: str, sub: str, base: str, email: str):
    """Create a 'user' for a new SSO identity, retrying username suffixes on collision."""
    for n in range(1, 51):
        candidate = base if n == 1 else f"{base[:24]}-{n}"
        user_id = str(uuid.uuid4())
        try:
            await db.execute(
                "INSERT INTO users (id, username, email, role, oidc_iss, oidc_sub) VALUES (?, ?, ?, 'user', ?, ?)",
                (user_id, candidate, email, iss, sub),
            )
            await db.commit()
            log.info("Provisioned SSO user %r", candidate)
            return await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
        except sqlite3.IntegrityError:
            await db.rollback()
            raced = await (await db.execute(
                "SELECT * FROM users WHERE oidc_iss = ? AND oidc_sub = ?", (iss, sub)
            )).fetchone()
            if raced:
                return raced
    raise _OidcError("failed", "Could not allocate a username")


# ── Admin: SSO configuration ────────────────────────────

@router.get("/admin/sso")
async def get_sso(admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    cfg = await _oidc_cfg(db)
    cfg["oidc_client_secret_set"] = bool(cfg.pop("oidc_client_secret"))
    cfg["redirect_uri_hint"] = (cfg["public_url"].rstrip("/") or "https://<your-host>") + "/api/auth/oidc/callback"
    return cfg


class SsoUpdate(BaseModel):
    oidc_enabled: Optional[bool] = None
    oidc_provider_url: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None  # write-only; omitted/empty keeps the current one
    oidc_scopes: Optional[str] = None
    oidc_button_label: Optional[str] = None
    oidc_auto_provision: Optional[bool] = None
    public_url: Optional[str] = None


@router.put("/admin/sso")
async def put_sso(body: SsoUpdate, admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if data.get("oidc_client_secret") == "":
        data.pop("oidc_client_secret")
    if data.get("public_url"):
        data["public_url"] = data["public_url"].strip().rstrip("/")
        if not data["public_url"].startswith("https://"):
            raise HTTPException(400, "Public URL must be https:// (SSO only works over HTTPS)")
    if data.get("oidc_enabled"):
        # Turning SSO on turns password sign-in off. An admin who hasn't linked
        # SSO would come back as a brand-new non-admin account.
        if not admin["oidc_sub"]:
            raise HTTPException(400, "Link SSO to your own account (Account tab) before turning SSO on")
        provider = data.get("oidc_provider_url", await get_setting(db, "oidc_provider_url", ""))
        try:
            await _get_oidc_config(provider)
        except Exception as e:
            raise HTTPException(400, f"Could not verify the provider: {e}")
    for key, value in data.items():
        if isinstance(value, bool):
            value = "1" if value else "0"
        await set_setting(db, key, value.strip() if isinstance(value, str) else value)
    await db.commit()
    _discovery_cache.clear()
    return await get_sso(admin, db)


# ── Admin: users ────────────────────────────────────────

async def _user_list_row(db, user_id: str) -> dict:
    row = await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    return _admin_view(row)


def _admin_view(row) -> dict:
    out = auth.public_user(row)
    out["pushover_user_key"] = out.pop("prefs").get("pushover_user_key", "")
    out["created_at"] = row["created_at"]
    out["last_login_at"] = row["last_login_at"]
    return out


@router.get("/admin/users")
async def list_users(admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    rows = await (await db.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE")).fetchall()
    shares = await (await db.execute("SELECT user_id, device_id, can_edit FROM device_shares")).fetchall()
    by_user: dict[str, list] = {}
    for s in shares:
        by_user.setdefault(s["user_id"], []).append({"device_id": s["device_id"], "can_edit": bool(s["can_edit"])})
    return [{**_admin_view(r), "shares": by_user.get(r["id"], [])} for r in rows]


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"
    email: str = ""


@router.post("/admin/users")
async def create_user(body: CreateUserBody, admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    if await _sso_only(db):
        raise HTTPException(400, "With SSO on, accounts are created when someone first signs in through SSO")
    username = _validate_username(body.username)
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be admin or user")
    auth.validate_password(body.password)
    user_id = str(uuid.uuid4())
    try:
        await db.execute(
            """INSERT INTO users (id, username, email, role, password_hash, force_password_change)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (user_id, username, body.email.strip(), body.role, auth.hash_password(body.password)),
        )
        await db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That username is taken")
    return await _user_list_row(db, user_id)


class PatchUserBody(BaseModel):
    role: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    pushover_user_key: Optional[str] = None  # "" clears it


async def _admin_count(db) -> int:
    return (await (await db.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")).fetchone())[0]


@router.patch("/admin/users/{user_id}")
async def patch_user(user_id: str, body: PatchUserBody, admin=Depends(require_admin),
                     db: aiosqlite.Connection = Depends(get_db)):
    target = await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(400, "Role must be admin or user")
        if target["role"] == "admin" and body.role == "user" and await _admin_count(db) <= 1:
            raise HTTPException(400, "Can't demote the last admin")
        if body.role != target["role"]:
            await db.execute("UPDATE users SET role = ?, session_version = session_version + 1 WHERE id = ?",
                             (body.role, user_id))
            if body.role == "user":
                # Demoted: drop alerts on devices that were only visible as admin.
                await db.execute(
                    "DELETE FROM alert_rules WHERE user_id = ? AND device_id NOT IN"
                    " (SELECT device_id FROM device_shares WHERE user_id = ?)",
                    (user_id, user_id),
                )
    if body.email is not None:
        await db.execute("UPDATE users SET email = ? WHERE id = ?", (body.email.strip(), user_id))
    if body.username is not None:
        try:
            await db.execute("UPDATE users SET username = ? WHERE id = ?", (_validate_username(body.username), user_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken")
    if body.pushover_user_key is not None:
        key = body.pushover_user_key.strip()
        if key and not re.fullmatch(r"[A-Za-z0-9]{30}", key):
            raise HTTPException(400, "Pushover user keys are 30 letters and digits")
        prefs = json.loads(target["prefs"] or "{}")
        if key:
            prefs["pushover_user_key"] = key
        else:
            prefs.pop("pushover_user_key", None)
        await db.execute("UPDATE users SET prefs = ? WHERE id = ?", (json.dumps(prefs), user_id))
    await db.commit()
    return await _user_list_row(db, user_id)


@router.delete("/admin/users/{user_id}")
async def delete_user(user_id: str, admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account")
    cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True}


class ResetPasswordBody(BaseModel):
    new_password: str


@router.post("/admin/users/{user_id}/reset-password")
async def reset_password(user_id: str, body: ResetPasswordBody, admin=Depends(require_admin),
                         db: aiosqlite.Connection = Depends(get_db)):
    if await _sso_only(db):
        raise HTTPException(400, SSO_ONLY)
    auth.validate_password(body.new_password)
    cursor = await db.execute(
        """UPDATE users SET password_hash = ?, force_password_change = 1,
               session_version = session_version + 1 WHERE id = ?""",
        (auth.hash_password(body.new_password), user_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True}


class SharesBody(BaseModel):
    shares: list[dict]  # [{device_id, can_edit}]


@router.put("/admin/users/{user_id}/shares")
async def set_user_shares(user_id: str, body: SharesBody, admin=Depends(require_admin),
                          db: aiosqlite.Connection = Depends(get_db)):
    """Replace the full set of devices shared with a user."""
    if not await (await db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,))).fetchone():
        raise HTTPException(404, "User not found")
    device_ids = {r["id"] for r in await (await db.execute("SELECT id FROM devices")).fetchall()}
    await db.execute("DELETE FROM device_shares WHERE user_id = ?", (user_id,))
    for s in body.shares:
        if s.get("device_id") not in device_ids:
            raise HTTPException(400, f"Unknown device {s.get('device_id')!r}")
        await db.execute("INSERT INTO device_shares (device_id, user_id, can_edit) VALUES (?, ?, ?)",
                         (s["device_id"], user_id, 1 if s.get("can_edit") else 0))
    # Alert rules on devices the user can no longer see would leak their location.
    await db.execute(
        "DELETE FROM alert_rules WHERE user_id = ? AND device_id NOT IN (SELECT device_id FROM device_shares WHERE user_id = ?)"
        " AND (SELECT role FROM users WHERE id = ?) != 'admin'",
        (user_id, user_id, user_id),
    )
    await db.commit()
    return {"ok": True}
