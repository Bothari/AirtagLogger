import os
import sqlite3
import tempfile

# Must be set before the app is imported: DATA_DIR/DB_PATH are read at import.
_DATA = tempfile.mkdtemp(prefix="airtag-test-")
os.environ["DATA_DIR"] = _DATA
os.environ["DISABLE_POLLING"] = "1"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.database import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402

H = {"X-Requested-With": "fetch"}
ADMIN_PW = "admin-password-1"


@pytest.fixture(autouse=True)
def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    auth._failures.clear()
    yield


def make_client() -> TestClient:
    # https base URL so Secure / __Host- cookies round-trip.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client():
    with make_client() as c:
        yield c


@pytest.fixture
def admin(client):
    """client logged in as the initial admin."""
    r = client.post("/api/auth/setup", headers=H,
                    json={"token": auth._setup_token, "username": "max", "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return client


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_device(device_id: str, name: str, color: str = "#4f8cff"):
    with db() as conn:
        conn.execute("INSERT INTO devices (id, name, original_name, key_file, color) VALUES (?, ?, ?, ?, ?)",
                     (device_id, name, name, f"{device_id}.json", color))


def add_location(device_id: str, lat: float, lon: float, ts: str, accuracy=None):
    with db() as conn:
        conn.execute("INSERT INTO locations (device_id, latitude, longitude, timestamp, accuracy) VALUES (?, ?, ?, ?, ?)",
                     (device_id, lat, lon, ts, accuracy))


def create_user(admin_client, username="alice", password="alice-temp-pw", role="user") -> dict:
    r = admin_client.post("/api/admin/users", headers=H,
                          json={"username": username, "password": password, "role": role})
    assert r.status_code == 200, r.text
    return r.json()


def login_as(username: str, password: str, new_password: str | None = "changed-password") -> TestClient:
    """A separate client logged in as `username`, past the forced password change."""
    c = make_client()
    c.__enter__()
    r = c.post("/api/auth/login", headers=H, json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    if r.json()["force_password_change"] and new_password:
        r = c.post("/api/auth/change-password", headers=H,
                   json={"current_password": password, "new_password": new_password})
        assert r.status_code == 200, r.text
    return c
