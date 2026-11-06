from app import auth
from conftest import ADMIN_PW, H, create_user, db, login_as, make_client


def test_everything_requires_login(client):
    for path in ("/api/devices", "/api/locations/latest", "/api/settings", "/api/status",
                 "/api/apple/status", "/api/alerts", "/api/geofences"):
        assert client.get(path).status_code == 401, path


def test_setup_needs_the_logged_token(client):
    assert client.get("/api/auth/config").json()["setup_required"] is True
    r = client.post("/api/auth/setup", headers=H, json={"token": "wrong", "username": "max", "password": ADMIN_PW})
    assert r.status_code == 403
    r = client.post("/api/auth/setup", headers=H,
                    json={"token": auth._setup_token, "username": "max", "password": ADMIN_PW})
    assert r.status_code == 200
    assert client.get("/api/auth/me").json()["role"] == "admin"
    # one-shot
    assert client.get("/api/auth/config").json()["setup_required"] is False
    r = client.post("/api/auth/setup", headers=H, json={"token": "x", "username": "evil", "password": ADMIN_PW})
    assert r.status_code == 400


def test_setup_token_survives_restart_only_while_no_users(client):
    first = auth._setup_token
    with make_client():
        assert auth._setup_token and auth._setup_token != first  # regenerated, still pending
    client.post("/api/auth/setup", headers=H,
                json={"token": auth._setup_token, "username": "max", "password": ADMIN_PW})
    with make_client():
        assert auth._setup_token is None


def test_mutations_need_csrf_header(admin):
    r = admin.post("/api/admin/users", json={"username": "a", "password": "password123"})
    assert r.status_code == 403
    assert "X-Requested-With" in r.text


def test_login_logout(admin):
    admin.post("/api/auth/logout", headers=H)
    assert admin.get("/api/auth/me").status_code == 401
    assert admin.post("/api/auth/login", headers=H, json={"username": "max", "password": "nope-nope"}).status_code == 401
    r = admin.post("/api/auth/login", headers=H, json={"username": "MAX", "password": ADMIN_PW})
    assert r.status_code == 200  # usernames are case-insensitive
    assert admin.get("/api/auth/me").json()["username"] == "max"


def test_rate_limit(admin):
    for _ in range(10):
        admin.post("/api/auth/login", headers=H, json={"username": "max", "password": "wrong-password"})
    r = admin.post("/api/auth/login", headers=H, json={"username": "max", "password": ADMIN_PW})
    assert r.status_code == 429


def test_forced_password_change(admin):
    create_user(admin)
    c = login_as("alice", "alice-temp-pw", new_password=None)
    assert c.get("/api/auth/me").json()["force_password_change"] is True
    assert c.get("/api/devices").status_code == 403
    r = c.post("/api/auth/change-password", headers=H,
               json={"current_password": "alice-temp-pw", "new_password": "short"})
    assert r.status_code == 400
    r = c.post("/api/auth/change-password", headers=H,
               json={"current_password": "alice-temp-pw", "new_password": "a-good-password"})
    assert r.status_code == 200
    assert c.get("/api/devices").status_code == 200  # this session survives its own change


def test_reset_password_kills_existing_sessions(admin):
    user = create_user(admin)
    c = login_as("alice", "alice-temp-pw")
    assert c.get("/api/devices").status_code == 200
    admin.post(f"/api/admin/users/{user['id']}/reset-password", headers=H, json={"new_password": "brand-new-pw"})
    assert c.get("/api/devices").status_code == 401


def test_role_change_takes_effect_immediately(admin):
    user = create_user(admin)
    c = login_as("alice", "alice-temp-pw")
    assert c.get("/api/admin/users").status_code == 403
    admin.patch(f"/api/admin/users/{user['id']}", headers=H, json={"role": "admin"})
    # role change bumps session_version: log in again and the new role applies
    assert c.get("/api/auth/me").status_code == 401
    c = login_as("alice", "changed-password")
    assert c.get("/api/admin/users").status_code == 200


def test_deleted_user_is_logged_out(admin):
    user = create_user(admin)
    c = login_as("alice", "alice-temp-pw")
    admin.delete(f"/api/admin/users/{user['id']}", headers=H)
    assert c.get("/api/devices").status_code == 401


def test_last_admin_protected(admin):
    me = admin.get("/api/auth/me").json()
    assert admin.patch(f"/api/admin/users/{me['id']}", headers=H, json={"role": "user"}).status_code == 400
    assert admin.delete(f"/api/admin/users/{me['id']}", headers=H).status_code == 400


def test_users_cannot_manage_users(admin):
    create_user(admin)
    c = login_as("alice", "alice-temp-pw")
    assert c.post("/api/admin/users", headers=H, json={"username": "b", "password": "password123"}).status_code == 403
    assert c.get("/api/admin/sso").status_code == 403


def test_settings_never_leak_secrets(admin):
    body = admin.get("/api/settings").json()
    assert set(body) == {"poll_interval", "retention_days"}
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('oidc_client_secret', 's3cret')")
    assert "oidc_client_secret_set" in admin.get("/api/admin/sso").json()
    assert "s3cret" not in admin.get("/api/admin/sso").text


def test_duplicate_username(admin):
    create_user(admin, "alice")
    r = admin.post("/api/admin/users", headers=H, json={"username": "ALICE", "password": "password123"})
    assert r.status_code == 409


def test_prefs(admin):
    r = admin.patch("/api/auth/me/prefs", headers=H, json={"default_range_hours": 168})
    assert r.json() == {"default_range_hours": 168}
    assert admin.patch("/api/auth/me/prefs", headers=H, json={"is_admin": True}).status_code == 400
    # the Pushover key is admin-managed, not a self-service pref
    assert admin.patch("/api/auth/me/prefs", headers=H, json={"pushover_user_key": "u" * 30}).status_code == 400
    assert admin.get("/api/auth/me").json()["prefs"]["default_range_hours"] == 168


def test_admin_sets_pushover_key(admin):
    user = create_user(admin)
    alice = login_as("alice", "alice-temp-pw")
    assert alice.patch(f"/api/admin/users/{user['id']}", headers=H, json={"pushover_user_key": "u" * 30}).status_code == 403
    assert admin.patch(f"/api/admin/users/{user['id']}", headers=H, json={"pushover_user_key": "bad"}).status_code == 400
    r = admin.patch(f"/api/admin/users/{user['id']}", headers=H, json={"pushover_user_key": "u" * 30})
    assert r.json()["pushover_user_key"] == "u" * 30
    # it reaches the user's own profile (so the alert sender can use it)
    assert alice.get("/api/auth/me").json()["prefs"]["pushover_user_key"] == "u" * 30
    # and survives the user changing their own prefs
    alice.patch("/api/auth/me/prefs", headers=H, json={"default_range_hours": 1})
    r = admin.patch(f"/api/admin/users/{user['id']}", headers=H, json={"pushover_user_key": ""})
    assert r.json()["pushover_user_key"] == ""
