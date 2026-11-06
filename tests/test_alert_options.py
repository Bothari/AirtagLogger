import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from app import alerts
from app.alerts import evaluate_rules, in_window
from conftest import H, add_device, add_location, db

LA = "America/Los_Angeles"
HOME = (34.0, -118.0)


def utc(h, m=0):
    # 2026-11-01 is PST (UTC-8) after the DST change: local = utc - 8h
    return datetime(2026, 11, 2, h, m, tzinfo=timezone.utc)


def test_in_window_daytime_and_overnight():
    # 09:00-17:00 local; 17:00 UTC = 09:00 PST
    assert in_window(utc(17), "09:00", "17:00", LA)
    assert not in_window(utc(16, 59), "09:00", "17:00", LA)
    assert not in_window(utc(1), "09:00", "17:00", LA)            # 17:00 PST: end is exclusive
    # 22:00-07:00 overnight
    assert in_window(utc(7), "22:00", "07:00", LA)                # 23:00 PST
    assert in_window(utc(14, 30), "22:00", "07:00", LA)           # 06:30 PST
    assert not in_window(utc(15), "22:00", "07:00", LA)           # 07:00 PST
    assert not in_window(utc(20), "22:00", "07:00", LA)           # 12:00 PST


@pytest.fixture
def pushover(admin, monkeypatch):
    """Configure Pushover for the admin and capture what would be sent."""
    sent = []
    real = httpx.AsyncClient

    def handler(request):
        sent.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"status": 1})

    monkeypatch.setattr(alerts.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    admin.put("/api/admin/notifications", headers=H, json={"pushover_app_token": "a" * 30})
    me = admin.get("/api/auth/me").json()
    admin.patch(f"/api/admin/users/{me['id']}", headers=H, json={"pushover_user_key": "u" * 30})
    add_device("d1", "Keys")
    return sent


def stale_rule(client, **extra):
    r = client.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "stale", "stale_hours": 1, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def go_stale(now):
    """A fix at 00:00 UTC, evaluated fresh first, then stale at `now`."""
    add_location("d1", *HOME, "2026-11-02T00:00:00Z")
    asyncio.run(evaluate_rules(datetime(2026, 11, 2, 0, 10, tzinfo=timezone.utc)))
    asyncio.run(evaluate_rules(now))


def events():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT message, delivered, muted FROM alert_events ORDER BY id")]


@pytest.mark.parametrize("priority", [-1, 0, 1])
def test_priority_is_sent(pushover, admin, priority):
    stale_rule(admin, priority=priority)
    go_stale(utc(5))
    assert len(pushover) == 1
    assert pushover[0]["priority"] == str(priority)
    assert "retry" not in pushover[0]


def test_emergency_repeats_until_acknowledged(pushover, admin):
    stale_rule(admin, priority=2)
    go_stale(utc(5))
    assert (pushover[0]["priority"], pushover[0]["retry"], pushover[0]["expire"]) == ("2", "60", "3600")


def test_outside_hours_is_muted_not_lost(pushover, admin):
    # only alert 09:00-17:00 local; going stale at 21:00 PST (05:00 UTC) is muted
    stale_rule(admin, window_mode="inside", window_start="09:00", window_end="17:00", window_tz=LA)
    go_stale(utc(5))
    assert pushover == []
    assert [(e["delivered"], e["muted"]) for e in events()] == [(0, 1)]
    # state was still tracked: it does not fire again when the window opens
    asyncio.run(evaluate_rules(utc(18)))
    assert pushover == [] and len(events()) == 1


def test_outside_mode_mutes_during_the_window(pushover, admin):
    # quiet overnight: "only outside 22:00-07:00"
    stale_rule(admin, window_mode="outside", window_start="22:00", window_end="07:00", window_tz=LA)
    go_stale(utc(7))          # 23:00 PST: muted
    assert pushover == [] and events()[0]["muted"] == 1


def test_outside_mode_sends_during_the_day(pushover, admin):
    stale_rule(admin, window_mode="outside", window_start="22:00", window_end="07:00", window_tz=LA)
    go_stale(utc(20))         # 12:00 PST: sent
    assert len(pushover) == 1 and events()[0]["muted"] == 0


def test_rule_validation(pushover, admin):
    bad = [
        {"priority": 3}, {"priority": -2},
        {"window_mode": "during", "window_start": "09:00", "window_end": "17:00", "window_tz": LA},
        {"window_mode": "inside", "window_start": "9am", "window_end": "17:00", "window_tz": LA},
        {"window_mode": "inside", "window_start": "09:00", "window_end": "09:00", "window_tz": LA},
        {"window_mode": "inside", "window_start": "09:00", "window_end": "17:00", "window_tz": "Mars/Olympus"},
    ]
    for extra in bad:
        r = admin.post("/api/alerts", headers=H, json={"device_id": "d1", "kind": "stale", "stale_hours": 1, **extra})
        assert r.status_code == 400, extra
    # no window mode: stray window fields are dropped
    r = stale_rule(admin, window_start="09:00")
    assert r["window_mode"] is None and r["window_start"] is None


def test_editing_hours_keeps_state_but_retargeting_resets(pushover, admin):
    rule = stale_rule(admin)
    go_stale(utc(5))
    with db() as conn:
        assert conn.execute("SELECT last_state FROM alert_rules").fetchone()[0] == "stale"
    body = {"device_id": "d1", "kind": "stale", "stale_hours": 1, "priority": 1,
            "window_mode": "inside", "window_start": "08:00", "window_end": "20:00", "window_tz": LA}
    r = admin.put(f"/api/alerts/{rule['id']}", headers=H, json=body)
    assert (r.json()["priority"], r.json()["window_start"], r.json()["last_state"]) == (1, "08:00", "stale")
    r = admin.put(f"/api/alerts/{rule['id']}", headers=H, json={**body, "stale_hours": 2})
    assert r.json()["last_state"] is None
