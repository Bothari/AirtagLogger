import asyncio
from datetime import datetime, timezone

import pytest

from app import geo
from app.alerts import evaluate_rules, polygon_state
from conftest import H, add_device, add_location, db

M = 1 / 111_320.0  # ~1 m of latitude in degrees
LAT0, LON0 = 34.0, -118.0


def pt(east_m: float, north_m: float) -> list[float]:
    """A point offset in metres from (LAT0, LON0)."""
    import math
    return [LAT0 + north_m * M, LON0 + east_m * M / math.cos(math.radians(LAT0))]


# A 300 m x 300 m "U": the notch (x 100-200, y 100-300) is outside.
U = [pt(0, 0), pt(300, 0), pt(300, 300), pt(200, 300), pt(200, 100), pt(100, 100), pt(100, 300), pt(0, 300)]


def test_point_in_concave_polygon():
    assert geo.point_in_polygon(*pt(50, 250), U)        # left arm
    assert geo.point_in_polygon(*pt(250, 250), U)       # right arm
    assert geo.point_in_polygon(*pt(150, 50), U)        # base
    assert not geo.point_in_polygon(*pt(150, 200), U)   # in the notch
    assert not geo.point_in_polygon(*pt(-50, 150), U)   # outside entirely
    assert not geo.point_in_polygon(*pt(150, 400), U)   # above the notch


def test_distance_to_boundary():
    assert geo.distance_to_boundary_m(*pt(150, 200), U) == pytest.approx(50, abs=1)   # notch centre
    assert geo.distance_to_boundary_m(*pt(50, 250), U) == pytest.approx(50, abs=1)    # inside arm
    assert geo.distance_to_boundary_m(*pt(-40, 150), U) == pytest.approx(40, abs=1)   # outside


def test_area_and_simplicity():
    assert geo.area_m2(U) == pytest.approx(300 * 300 - 100 * 200, rel=0.01)
    assert geo.is_simple(U)
    bowtie = [pt(0, 0), pt(100, 100), pt(100, 0), pt(0, 100)]
    assert not geo.is_simple(bowtie)
    # a vertex touching a non-adjacent edge also counts as crossing
    touching = [pt(0, 0), pt(200, 0), pt(200, 200), pt(100, 0), pt(0, 200)]
    assert not geo.is_simple(touching)


def test_polygon_hysteresis():
    assert polygon_state(True, 40, None, None) == "in"
    assert polygon_state(True, 40, 60, "out") == "out"   # error reaches the boundary
    assert polygon_state(False, 10, None, "in") == "in"  # just outside: hold
    assert polygon_state(False, 10, None, None) is None
    assert polygon_state(False, 80, None, "in") == "out"
    assert polygon_state(False, 80, 100, "in") == "in"   # imprecise: hold


def polygon_body(points, name="School"):
    return {"name": name, "shape": "polygon", "polygon": points}


def test_polygon_api_roundtrip(admin):
    r = admin.post("/api/geofences", headers=H, json=polygon_body(U))
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["shape"] == "polygon" and len(g["polygon"]) == 8
    # bounding circle covers the shape, for display
    centre = pt(150, 150)
    assert g["latitude"] == pytest.approx(centre[0], abs=1e-5)
    assert 200 < g["radius_m"] < 230
    listed = admin.get("/api/geofences").json()
    assert listed[0]["polygon"] == g["polygon"]


def test_polygon_validation(admin):
    bad = [
        [pt(0, 0), pt(100, 0)],                                     # too few points
        [pt(0, 0), pt(100, 100), pt(100, 0), pt(0, 100)],           # self-crossing
        [pt(0, 0), pt(10, 0), pt(10, 10), pt(0, 10)],               # 100 m², too small
        [[95, 0], [96, 1], [97, 0]],                                # invalid latitude
    ]
    for poly in bad:
        assert admin.post("/api/geofences", headers=H, json=polygon_body(poly)).status_code == 400, poly
    assert admin.post("/api/geofences", headers=H, json={"name": "x", "shape": "hexagon"}).status_code == 400
    # a closed ring (first point repeated) is accepted
    closed = U + [U[0]]
    r = admin.post("/api/geofences", headers=H, json=polygon_body(closed))
    assert r.status_code == 200 and len(r.json()["polygon"]) == 8


def evaluate():
    asyncio.run(evaluate_rules(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)))


def events():
    with db() as conn:
        return [r["message"] for r in conn.execute("SELECT message FROM alert_events ORDER BY id")]


def test_polygon_alert_cycle(admin):
    add_device("d1", "Keys")
    g = admin.post("/api/geofences", headers=H, json=polygon_body(U)).json()
    r = admin.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "both", "geofence_id": g["id"]})
    assert r.status_code == 200

    add_location("d1", *pt(150, 200), "2026-11-01T10:00:00Z")   # in the notch: outside
    evaluate()
    assert events() == []                                       # baseline only

    add_location("d1", *pt(50, 250), "2026-11-01T10:15:00Z")    # into the left arm
    evaluate()
    assert events() == ["Keys arrived at School: 50 m inside the boundary."]

    add_location("d1", *pt(150, 190), "2026-11-01T10:30:00Z", accuracy=80)  # imprecise
    evaluate()
    assert len(events()) == 1

    add_location("d1", *pt(150, 200), "2026-11-01T10:45:00Z")   # back into the notch
    evaluate()
    assert events()[-1] == "Keys left School: 50 m outside the boundary."


def test_circle_can_become_polygon(admin):
    add_device("d1", "Keys")
    g = admin.post("/api/geofences", headers=H, json={
        "name": "Home", "shape": "circle", "latitude": LAT0, "longitude": LON0, "radius_m": 100}).json()
    admin.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "both", "geofence_id": g["id"]})
    add_location("d1", *pt(10, 10), "2026-11-01T10:00:00Z")
    evaluate()
    r = admin.put(f"/api/geofences/{g['id']}", headers=H, json=polygon_body(U, "Home"))
    assert r.status_code == 200 and r.json()["shape"] == "polygon"
    with db() as conn:
        assert conn.execute("SELECT last_state FROM alert_rules").fetchone()[0] is None  # re-baselined
