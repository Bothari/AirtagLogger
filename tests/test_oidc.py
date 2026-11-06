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
        "oidc_enabled": True, "oidc_provider_url": ISSUER, "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "shh", "public_url": "https://airtag.example.com",
    })
    assert r.status_code == 200, r.text
    return p


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


def test_new_identity_becomes_a_plain_user(provider, client):
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, provider, "sub-bob", email="bob@example.com", email_verified=True)
    assert "url=/'" in page
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["role"], me["email"], me["sso_linked"]) == ("bob", "user", "bob@example.com", True)
    assert client.get("/api/devices").json() == []


def test_unverified_email_is_not_trusted(provider, client):
    client.post("/api/auth/logout", headers=H)
    sso_login(client, provider, "sub-x", email="max@example.com", email_verified="false",
              preferred_username="maxish")
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["email"]) == ("maxish", "")


def test_sso_never_merges_by_username(provider, client):
    """An IdP user called 'max' must not become the local admin 'max'."""
    client.post("/api/auth/logout", headers=H)
    sso_login(client, provider, "sub-impostor", preferred_username="max")
    me = client.get("/api/auth/me").json()
    assert me["role"] == "user" and me["username"] == "max-2"


def test_link_then_login(provider, client):
    page = sso_login(client, provider, "sub-max", link=True)
    assert "sso_linked=1" in page
    client.post("/api/auth/logout", headers=H)
    sso_login(client, provider, "sub-max")
    me = client.get("/api/auth/me").json()
    assert (me["username"], me["role"]) == ("max", "admin")
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_cannot_link_identity_owned_by_someone_else(provider, client):
    create_user(client, "alice")
    alice = login_as("alice", "alice-temp-pw")
    sso_login(alice, provider, "sub-alice", link=True)
    page = sso_login(client, provider, "sub-alice", link=True)
    assert "sso_error=already_linked" in page


def test_nonce_mismatch_rejected(provider, client):
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, provider, "sub-n", tamper_nonce=True)
    assert "sso_error=failed" in page
    assert client.get("/api/auth/me").status_code == 401


def test_state_mismatch_rejected(provider, client):
    client.get("/api/auth/oidc/start", follow_redirects=False)
    r = client.get("/api/auth/oidc/callback?code=abc&state=forged")
    assert "sso_error=failed" in r.text


def test_auto_provision_off(provider, client):
    client.put("/api/admin/sso", headers=H, json={"oidc_auto_provision": False})
    client.post("/api/auth/logout", headers=H)
    page = sso_login(client, provider, "sub-new")
    assert "sso_error=not_provisioned" in page


def test_unlink_requires_password(provider, client):
    client.post("/api/auth/logout", headers=H)
    sso_login(client, provider, "sub-sso-only")
    assert client.post("/api/auth/unlink-sso", headers=H).status_code == 400
    # SSO-only users can set a first password without a current one
    r = client.post("/api/auth/change-password", headers=H, json={"new_password": "first-password"})
    assert r.status_code == 200
    assert client.post("/api/auth/unlink-sso", headers=H).status_code == 200


def test_discovery_issuer_pinned(admin, monkeypatch):
    def evil(request):
        return httpx.Response(200, json={"issuer": "https://evil.example.com"})
    real = httpx.AsyncClient
    monkeypatch.setattr(routes_auth.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(evil), **kw))
    routes_auth._discovery_cache.clear()
    r = admin.put("/api/admin/sso", headers=H, json={"oidc_enabled": True, "oidc_provider_url": ISSUER})
    assert r.status_code == 400
