import pytest

from conftest import H, add_device, add_location, create_user, db, login_as


@pytest.fixture
def setup(admin):
    add_device("d1", "Keys")
    add_device("d2", "Bike")
    add_location("d1", 34.0, -118.0, "2026-11-01T10:00:00Z")
    add_location("d2", 35.0, -119.0, "2026-11-01T10:00:00Z")
    user = create_user(admin)
    alice = login_as("alice", "alice-temp-pw")
    return admin, alice, user


def share(admin, device_id, user_id, access):
    r = admin.put(f"/api/admin/devices/{device_id}/shares/{user_id}", headers=H, json={"access": access})
    assert r.status_code == 200, r.text


def test_user_sees_nothing_until_shared(setup):
    admin, alice, user = setup
    assert alice.get("/api/devices").json() == []
    assert alice.get("/api/locations/latest").json() == []
    assert alice.get("/api/locations/d1").status_code == 404
    assert alice.get("/api/status").json()["devices"] == 0
    assert len(admin.get("/api/devices").json()) == 2


def test_view_share(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    devices = alice.get("/api/devices").json()
    assert [d["id"] for d in devices] == ["d1"]
    assert devices[0]["access"] == "view"
    assert "key_file" not in devices[0]
    assert len(alice.get("/api/locations/d1").json()) == 1
    assert alice.get("/api/locations/d2").status_code == 404
    assert [l["device_id"] for l in alice.get("/api/locations/latest").json()] == ["d1"]
    # view-only: cannot rename for everyone
    assert alice.patch("/api/devices/d1", headers=H, json={"name": "Mine"}).status_code == 403


def test_edit_share(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "edit")
    r = alice.patch("/api/devices/d1", headers=H, json={"name": "House keys"})
    assert r.status_code == 200
    assert {d["id"]: d["name"] for d in admin.get("/api/devices").json()}["d1"] == "House keys"
    # polling on/off stays admin-only
    assert alice.patch("/api/devices/d1", headers=H, json={"enabled": False}).status_code == 403


def test_admin_only_device_actions(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "edit")
    assert alice.delete("/api/devices/d1", headers=H).status_code == 403
    assert alice.post("/api/poll", headers=H).status_code == 403
    files = {"key_file": ("k.json", b'{"master_key": "x"}', "application/json")}
    assert alice.post("/api/devices", headers=H, data={"name": "x"}, files=files).status_code == 403
    assert alice.post("/api/apple/login", headers=H, json={"email": "a", "password": "b"}).status_code == 403
    assert alice.get("/api/apple/status").json()["account"] is None


def test_export_is_filtered(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    csv = alice.get("/api/export?devices=d1,d2&format=csv").text
    assert "d1" in csv and "d2" not in csv
    assert alice.get("/api/export?devices=d2&format=csv").status_code == 400


def test_one_name_and_colour_for_everyone(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "edit")
    alice.patch("/api/devices/d1", headers=H, json={"name": "House keys", "color": "#ff5c5c"})
    for client in (admin, alice):
        d = {x["id"]: x for x in client.get("/api/devices").json()}["d1"]
        assert (d["name"], d["color"]) == ("House keys", "#ff5c5c")
    # prefs only carries map visibility now
    assert alice.put("/api/devices/d1/prefs", headers=H, json={"name": "Mine"}).status_code == 422


def test_hiding_is_personal(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    assert alice.put("/api/devices/d1/prefs", headers=H, json={"hidden": True}).json()["hidden"] == 1
    assert {d["id"]: d for d in admin.get("/api/devices").json()}["d1"]["hidden"] == 0
    assert alice.put("/api/devices/d1/prefs", headers=H, json={"hidden": False}).json()["hidden"] == 0
    assert alice.put("/api/devices/d2/prefs", headers=H, json={"hidden": True}).status_code == 404


def test_unsharing_removes_alerts(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    r = alice.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "stale", "stale_hours": 6})
    assert r.status_code == 200
    share(admin, "d1", user["id"], None)
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_rules").fetchone()[0] == 0
    assert alice.get("/api/devices").json() == []


def test_user_centric_share_replace(setup):
    admin, alice, user = setup
    r = admin.put(f"/api/admin/users/{user['id']}/shares", headers=H,
                  json={"shares": [{"device_id": "d1", "can_edit": True}, {"device_id": "d2"}]})
    assert r.status_code == 200
    access = {d["id"]: d["access"] for d in alice.get("/api/devices").json()}
    assert access == {"d1": "edit", "d2": "view"}
    users = {u["username"]: u for u in admin.get("/api/admin/users").json()}
    assert len(users["alice"]["shares"]) == 2


def test_hidden_device_still_listed(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    alice.put("/api/devices/d1/prefs", headers=H, json={"hidden": True})
    assert alice.get("/api/devices").json()[0]["hidden"] == 1


def test_delete_device_with_history(setup):
    admin, alice, user = setup
    share(admin, "d1", user["id"], "view")
    assert admin.delete("/api/devices/d1", headers=H).status_code == 200
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM locations WHERE device_id='d1'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM device_shares").fetchone()[0] == 0


def test_admin_updates_return_the_device_they_changed(setup):
    """An admin sees every device, so the single-device lookup must still filter
    by id -- it once returned the first device instead (AND/OR precedence), and
    the UI then redrew the wrong card, e.g. the hide toggle snapping back off."""
    admin, alice, user = setup
    for did in ("d1", "d2"):
        r = admin.put(f"/api/devices/{did}/prefs", headers=H, json={"hidden": True})
        assert (r.json()["id"], r.json()["hidden"]) == (did, 1)
        r = admin.patch(f"/api/devices/{did}", headers=H, json={"color": "#34d399"})
        assert r.json()["id"] == did
