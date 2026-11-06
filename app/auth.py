"""
App-level authentication: users, sessions, roles, and per-device access.

Sessions are HS256 JWTs in an httponly cookie carrying only the user id and the
user's session_version. The user row is re-read on every request, so role
changes, password resets and deletions take effect immediately rather than when
the token expires.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import aiosqlite
import bcrypt
from fastapi import Depends, HTTPException, Request, Response
from jose import JWTError, jwt

from app.database import get_db, get_setting, set_setting

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
SESSION_COOKIE = "session"
SESSION_DAYS = 30

_session_secret: str | None = None

# One-time token required to create the first admin. Printed to the log at
# startup while no users exist, so only someone with server access can claim
# admin on a fresh install.
_setup_token: str | None = None


# ── Secrets / bootstrap ─────────────────────────────────

async def init_auth(db: aiosqlite.Connection):
    """Load (or generate) the session secret; issue a setup token if there are no users."""
    global _session_secret, _setup_token
    secret = await get_setting(db, "session_secret")
    if not secret:
        secret = secrets.token_hex(32)
        await set_setting(db, "session_secret", secret)
        await db.commit()
    _session_secret = secret

    count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
    if count == 0:
        _setup_token = secrets.token_urlsafe(16)
        log.warning("=" * 60)
        log.warning("No users exist. Open the app and create the admin account")
        log.warning("using this one-time setup token:  %s", _setup_token)
        log.warning("=" * 60)
    else:
        _setup_token = None


def session_secret() -> str:
    assert _session_secret, "init_auth() not called"
    return _session_secret


def setup_pending() -> bool:
    return _setup_token is not None


def consume_setup_token(token: str) -> bool:
    global _setup_token
    if _setup_token is None or not secrets.compare_digest(token.strip(), _setup_token):
        return False
    _setup_token = None
    return True


# ── Passwords ───────────────────────────────────────────

MIN_PASSWORD = 8
MAX_PASSWORD_BYTES = 72  # bcrypt ignores (or rejects) anything beyond this


def validate_password(plain: str):
    if len(plain) < MIN_PASSWORD:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD} characters")
    if len(plain.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(400, f"Password must be at most {MAX_PASSWORD_BYTES} bytes")


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode()[:MAX_PASSWORD_BYTES], hashed.encode())
    except ValueError:
        return False


# Constant-time-ish failure path for unknown usernames, so response timing does
# not reveal which usernames exist.
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt()).decode()


def burn_password_check(plain: str):
    verify_password(plain, _DUMMY_HASH)


# ── Login rate limiting ─────────────────────────────────

_FAIL_WINDOW = 15 * 60
_FAIL_LIMIT = 10
_failures: dict[str, list[float]] = {}


def _prune(key: str) -> list[float]:
    cutoff = time.time() - _FAIL_WINDOW
    hits = [t for t in _failures.get(key, []) if t > cutoff]
    if hits:
        _failures[key] = hits
    else:
        _failures.pop(key, None)
    return hits


def check_rate_limit(*keys: str):
    for key in keys:
        if len(_prune(key)) >= _FAIL_LIMIT:
            raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")


def record_failure(*keys: str):
    now = time.time()
    for key in keys:
        _failures.setdefault(key, []).append(now)


def clear_failures(*keys: str):
    for key in keys:
        _failures.pop(key, None)


def client_ip(request: Request) -> str:
    # Behind Caddy the peer is the proxy; it sets X-Forwarded-For.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ── Sessions ────────────────────────────────────────────

def is_https(request: Request) -> bool:
    return (
        request.headers.get("x-forwarded-proto", "").lower() == "https"
        or request.url.scheme == "https"
    )


def make_session_token(user_id: str, session_version: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    return jwt.encode({"sub": user_id, "ver": session_version, "exp": exp},
                      session_secret(), algorithm=ALGORITHM)


def set_session_cookie(response: Response, request: Request, user_id: str, session_version: int):
    response.set_cookie(
        SESSION_COOKIE, make_session_token(user_id, session_version),
        httponly=True, samesite="lax", secure=is_https(request),
        max_age=SESSION_DAYS * 86400, path="/",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")


def public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role": row["role"],
        "force_password_change": bool(row["force_password_change"]),
        "has_password": bool(row["password_hash"]),
        "sso_linked": bool(row["oidc_sub"]),
        "prefs": json.loads(row["prefs"] or "{}"),
    }


async def user_from_request(request: Request, db: aiosqlite.Connection):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = jwt.decode(token, session_secret(), algorithms=[ALGORITHM])
    except JWTError:
        return None
    row = await (await db.execute("SELECT * FROM users WHERE id = ?", (payload.get("sub"),))).fetchone()
    if not row or row["session_version"] != payload.get("ver"):
        return None
    return row


async def require_session(request: Request, db: aiosqlite.Connection = Depends(get_db)):
    """Any logged-in user, including one who still has to change their password."""
    row = await user_from_request(request, db)
    if row is None:
        raise HTTPException(401, "Not logged in")
    return row


async def require_user(row=Depends(require_session)):
    """A logged-in user who is allowed to use the app."""
    if row["force_password_change"]:
        raise HTTPException(403, "Password change required")
    return row


async def require_admin(row=Depends(require_user)):
    if row["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return row


# ── Device access ───────────────────────────────────────

async def device_access(db: aiosqlite.Connection, user, device_id: str) -> str | None:
    """'admin', 'edit', 'view', or None if the user cannot see the device."""
    exists = await (await db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,))).fetchone()
    if not exists:
        return None
    if user["role"] == "admin":
        return "admin"
    row = await (await db.execute(
        "SELECT can_edit FROM device_shares WHERE device_id = ? AND user_id = ?",
        (device_id, user["id"]),
    )).fetchone()
    if not row:
        return None
    return "edit" if row["can_edit"] else "view"


async def require_device(db: aiosqlite.Connection, user, device_id: str, edit: bool = False) -> str:
    access = await device_access(db, user, device_id)
    # 404 rather than 403 so device ids a user has no access to are not confirmable.
    if access is None:
        raise HTTPException(404, "Device not found")
    if edit and access == "view":
        raise HTTPException(403, "You have view-only access to this device")
    return access


async def visible_device_ids(db: aiosqlite.Connection, user) -> list[str]:
    if user["role"] == "admin":
        rows = await (await db.execute("SELECT id FROM devices")).fetchall()
    else:
        rows = await (await db.execute(
            "SELECT device_id AS id FROM device_shares WHERE user_id = ?", (user["id"],)
        )).fetchall()
    return [r["id"] for r in rows]
