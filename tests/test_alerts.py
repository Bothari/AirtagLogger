import asyncio
from datetime import datetime, timezone

import pytest

from app.alerts import evaluate_rules, geofence_state, haversine_m
from conftest import H, add_device, add_location, create_user, db, login_as

HOME = (34.0000, -118.0000)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def offset_north(meters: float) -> tuple[float, float]:
    return HOME[0] + meters / 111_320.0, HOME[1]


def evaluate(now=NOW):
    asyncio.run(evaluate_rules(now))


def events():
    with db() as conn:
        return [r["message"] for r in conn.execute("SELECT message FROM alert_events ORDER BY id")]


def test_haversine():
    # offset_north uses a flat 111.32 km/deg; haversine a spherical Earth
    assert abs(haversine_m(*HOME, *offset_north(1000)) - 1000) < 5


def test_hysteresis():
    assert geofence_state(90, 100, None) == "in"
    assert geofence_state(110, 100, "in") == "in"     # inside the dead band: keep state
    assert geofence_state(110, 100, "out") == "out"
    assert geofence_state(110, 100, None) is None
    assert geofence_state(130, 100, "in") == "out"


@pytest.fixture
def fence(admin):
    add_device("d1", "Keys")
    r = admin.post("/api/geofences", headers=H,
                   json={"name": "Home", "latitude": HOME[0], "longitude": HOME[1], "radius_m": 100})
    assert r.status_code == 200
    return admin, r.json()


def rule(client, fence_id, kind="both"):
    r = client.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": kind, "geofence_id": fence_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_enter_exit_cycle(fence):
    admin, g = fence
    rule(admin, g["id"])
    add_location("d1", *offset_north(500), "2026-11-01T10:00:00Z")
    evaluate()
    assert events() == []  # first fix only sets the baseline

    add_location("d1", *offset_north(20), "2026-11-01T10:15:00Z")
    evaluate()
    assert events() == ["Keys arrived at Home: 20 m from the centre (radius 100 m)."]

    add_location("d1", *offset_north(110), "2026-11-01T10:30:00Z")  # dead band
    evaluate()
    assert len(events()) == 1

    add_location("d1", *offset_north(400), "2026-11-01T10:45:00Z")
    evaluate()
    assert events()[-1].startswith("Keys left Home")

    evaluate()  # no new fix: no repeat
    assert len(events()) == 2


def test_enter_only_rule_ignores_exits(fence):
    admin, g = fence
    rule(admin, g["id"], kind="enter")
    add_location("d1", *offset_north(20), "2026-11-01T10:00:00Z")
    evaluate()
    add_location("d1", *offset_north(500), "2026-11-01T10:15:00Z")
    evaluate()
    assert events() == []
    add_location("d1", *offset_north(10), "2026-11-01T10:30:00Z")
    evaluate()
    assert len(events()) == 1


def test_imprecise_fix_is_ignored(fence):
    admin, g = fence
    rule(admin, g["id"])
    add_location("d1", *offset_north(20), "2026-11-01T10:00:00Z")
    evaluate()
    add_location("d1", *offset_north(500), "2026-11-01T10:15:00Z", accuracy=800)
    evaluate()
    assert events() == []


def test_moving_the_fence_resets_baseline(fence):
    admin, g = fence
    rule(admin, g["id"])
    add_location("d1", *offset_north(20), "2026-11-01T10:00:00Z")
    evaluate()
    far = offset_north(5000)
    admin.put(f"/api/geofences/{g['id']}", headers=H,
              json={"name": "Home", "latitude": far[0], "longitude": far[1], "radius_m": 100})
    evaluate()
    assert events() == []  # re-baselined as 'out', not reported as an exit


def test_stale(admin):
    add_device("d1", "Keys")
    r = admin.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "stale", "stale_hours": 6})
    assert r.status_code == 200
    add_location("d1", *HOME, "2026-11-01T10:00:00Z")
    evaluate()
    assert events() == []
    evaluate(datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc))
    assert events()[-1].startswith("Keys has gone quiet")
    evaluate(datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc))
    assert len(events()) == 1
    add_location("d1", *HOME, "2026-11-01T18:30:00Z")
    evaluate(datetime(2026, 11, 1, 18, 45, tzinfo=timezone.utc))
    assert events()[-1].startswith("Keys is reporting again")


def test_rules_are_private_and_scoped(fence):
    admin, g = fence
    create_user(admin)
    alice = login_as("alice", "alice-temp-pw")
    # can't use someone else's geofence, or a device not shared with her
    assert alice.post("/api/alerts", headers=H,
                      json={"device_id": "d1", "kind": "enter", "geofence_id": g["id"]}).status_code == 404
    assert alice.get("/api/geofences").json() == []
    assert alice.delete(f"/api/geofences/{g['id']}", headers=H).status_code == 404


def test_validation(fence):
    admin, g = fence
    bad = [
        {"device_id": "d1", "kind": "enter"},                       # no geofence
        {"device_id": "d1", "kind": "stale", "stale_hours": 0.1},   # too short
        {"device_id": "d1", "kind": "teleport"},
        {"device_id": "nope", "kind": "stale", "stale_hours": 6},
    ]
    for body in bad:
        assert admin.post("/api/alerts", headers=H, json=body).status_code in (400, 404), body
    r = admin.post("/api/geofences", headers=H, json={"name": "x", "latitude": 0, "longitude": 0, "radius_m": 5})
    assert r.status_code == 400


def test_notification_config(admin):
    assert admin.post("/api/alerts/test", headers=H).status_code == 400
    assert admin.put("/api/admin/notifications", headers=H, json={"pushover_app_token": "short"}).status_code == 400
    r = admin.put("/api/admin/notifications", headers=H, json={"pushover_app_token": "a" * 30})
    assert r.json() == {"pushover_app_token_set": True}
    assert "a" * 30 not in admin.get("/api/admin/notifications").text
