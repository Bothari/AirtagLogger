"""OIDC flow against a mocked provider (discovery, token endpoint, JWKS)."""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app import routes_auth
from conftest import H, create_user, db, login_as

ISSUER = "https://sso.example.com/application/o/airtag"
CLIENT_ID = "airtag-client"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
_PUBLIC_JWK = {**jwk.construct(_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo), "RS256").to_dict(),
    "kid": "k1", "use": "sig"}


class Provider:
    def __init__(self):
        self.claims = {}
        self.nonce = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/jwks",
            })
        if url.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [{**_PUBLIC_JWK, "alg": "RS256"}]})
        if url.endswith("/token"):
            now = int(time.time())
            claims = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                      "nonce": self.nonce, **self.claims}
            id_token = jwt.encode(claims, _PRIVATE_PEM, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": id_token, "access_token": "at", "token_type": "Bearer"})
        return httpx.Response(404)


@pytest.fixture
def provider(admin, monkeypatch):
    p = Provider()
    real = httpx.AsyncClient
    monkeypatch.setattr(routes_auth.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(p.handler), **kw))
    routes_auth._discovery_cache.clear()
    r = admin.put("/api/admin/sso", headers=H, json={
        "oidc_provider_url": ISSUER, "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "shh", "public_url": "https://airtag.example.com",
    })
    assert r.status_code == 200, r.text
    return p


@pytest.fixture
def sso_on(provider, admin):
    """Provider configured, the admin linked as 'sub-max', and SSO switched on."""
    assert "sso_linked=1" in sso_login(admin, provider, "sub-max", link=True)
    r = admin.put("/api/admin/sso", headers=H, json={"oidc_enabled": True})
    assert r.status_code == 200, r.text
    return provider


def sso_login(client, provider, sub, link=False, tamper_nonce=False, **claims):
    r = client.get("/api/auth/oidc/start" + ("?link=1" if link else ""), follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["redirect_uri"] == ["https://airtag.example.com/api/auth/oidc/callback"]
    assert q["code_challenge_method"] == ["S256"]
    provider.nonce = "wrong" if tamper_nonce else q["nonce"][0]
    provider.claims = {"sub": sub, **claims}
    r = client.get(f"/api/auth/oidc/callback?code=abc&state={q['state'][0]}")
    assert r.status_code == 200
    return r.text  # the redirect page


def test_enabling_sso_requires_the_admin_to_be_linked(provider, admin):
    r = admin.put("/api/admin/sso", headers=H, json={"oidc_enabled": True})
    assert r.status_code == 400 and "Link SSO" in r.json()["detail"]
    # linking works while SSO is configured but still off
    assert admin.get("/api/auth/config").json()["oidc_configured"] is True
    assert "sso_linked=1" in sso_login(admin, provider, "sub-max", link=True)
    assert admin.put("/api/admin/sso", headers=H, json={"oidc_enabled": True}).status_code == 200
    assert admin.get("/api/auth/config").json()["local_login"] is False


def test_plain_sso_start_refused_while_off(provider, client):
    assert client.get("/api/auth/oidc/start", follow_redirects=False).status_code == 400


def test_sso_turns_password_features_off(sso_on, client):
    me_id = client.get("/api/auth/me").json()["id"]
    assert client.post("/api/auth/change-password", headers=H, json={"new_password": "whatever-123"}).status_code == 400
    assert client.post("/api/auth/unlink-sso", headers=H).status_code == 400
    assert client.post("/api/admin/users", headers=H,
                       json={"username": "x", "password": "password123"}).status_code == 400
    assert client.post(f"/api/admin/users/{me_id}/reset-password", headers=H,
                       json={"new_password": "password123"}).status_code == 400
    client.post("/api/auth/logout", headers=H)
    r = client.post("/api/auth/login", headers=H, json={"username": "max", "password": "admin-password-1"})
    assert r.status_code == 403  # even with the right password


def test_linked_admin_signs_in_with_sso(sso_on, client):
    client.post("/api/auth/logout", headers=H)
    sso_login(client, sso_on, "sub-max")
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["role"]) == ("max", "admin")
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_new_identity_becomes_a_plain_user(sso_on, client):
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, sso_on, "sub-bob", email="bob@example.com", email_verified=True)
    assert "url=/'" in page
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["role"], me["email"], me["sso_linked"]) == ("bob", "user", "bob@example.com", True)
    assert client.get("/api/devices").json() == []


def test_unverified_email_is_not_trusted(sso_on, client):
    client.post("/api/auth/logout", headers=H)
    sso_login(client, sso_on, "sub-x", email="max@example.com", email_verified="false",
              preferred_username="maxish")
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["email"]) == ("maxish", "")


def test_sso_never_merges_by_username(sso_on, client):
    """An IdP user called 'max' must not become the local admin 'max'."""
    client.post("/api/auth/logout", headers=H)
    sso_login(client, sso_on, "sub-impostor", preferred_username="max")
    me = client.get("/api/auth/me").json()
    assert me["role"] == "user" and me["username"] == "max-2"


def test_cannot_link_identity_owned_by_someone_else(provider, client):
    create_user(client, "alice")
    alice = login_as("alice", "alice-temp-pw")
    sso_login(alice, provider, "sub-alice", link=True)
    page = sso_login(client, provider, "sub-alice", link=True)
    assert "sso_error=already_linked" in page


def test_sso_clears_pending_password_change(provider, client):
    """A user still on a temporary password isn't stranded once passwords are off."""
    create_user(client, "alice")
    alice = login_as("alice", "alice-temp-pw", new_password=None)
    assert alice.get("/api/auth/me").json()["force_password_change"] is True
    sso_login(alice, provider, "sub-alice", link=True)
    sso_login(client, provider, "sub-max", link=True)
    client.put("/api/admin/sso", headers=H, json={"oidc_enabled": True})
    alice.post("/api/auth/logout", headers=H)
    sso_login(alice, provider, "sub-alice")
    assert alice.get("/api/devices").status_code == 200


def test_nonce_mismatch_rejected(sso_on, client):
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, sso_on, "sub-n", tamper_nonce=True)
    assert "sso_error=failed" in page
    assert client.get("/api/auth/me").status_code == 401


def test_state_mismatch_rejected(sso_on, client):
    client.get("/api/auth/oidc/start", follow_redirects=False)
    r = client.get("/api/auth/oidc/callback?code=abc&state=forged")
    assert "sso_error=failed" in r.text


def test_auto_provision_off(sso_on, client):
    client.put("/api/admin/sso", headers=H, json={"oidc_auto_provision": False})
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, sso_on, "sub-new")
    assert "sso_error=not_provisioned" in page


def test_recover_disables_sso(sso_on, client, capsys):
    from app import recover
    assert recover.main(["disable-sso"]) == 0
    assert "max" in capsys.readouterr().out
    client.post("/api/auth/logout", headers=H)
    r = client.post("/api/auth/login", headers=H, json={"username": "max", "password": "admin-password-1"})
    assert r.status_code == 200


def test_discovery_issuer_pinned(admin, monkeypatch):
    def evil(request):
        return httpx.Response(200, json={"issuer": "https://evil.example.com"})
    real = httpx.AsyncClient
    monkeypatch.setattr(routes_auth.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(evil), **kw))
    routes_auth._discovery_cache.clear()
    with db() as conn:  # get past the "admin must be linked" check to the issuer check
        conn.execute("UPDATE users SET oidc_iss = 'x', oidc_sub = 'y'")
    r = admin.put("/api/admin/sso", headers=H, json={"oidc_enabled": True, "oidc_provider_url": ISSUER})
    assert r.status_code == 400
