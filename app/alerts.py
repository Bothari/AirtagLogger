"""
Geofences and alerts.

Each user owns their own geofences and alert rules, on devices they can see.
After every poll, evaluate_rules() compares each rule's device against its
geofence (or staleness threshold) and notifies via Pushover on a change of state.

AirTag fixes are noisy, so geofence state uses hysteresis: a device is 'in' at
distance <= radius, 'out' only beyond radius + margin, and keeps its previous
state in between. Fixes whose reported accuracy is worse than the radius are
ignored -- they cannot tell inside from outside.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_admin, require_device, require_user
from app.database import connect, get_db, get_setting, set_setting

log = logging.getLogger(__name__)
router = APIRouter()

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
MIN_RADIUS_M = 25
MAX_RADIUS_M = 50_000


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def hysteresis_margin(radius_m: float) -> float:
    return max(25.0, 0.15 * radius_m)


def geofence_state(distance_m: float, radius_m: float, previous: str | None) -> str | None:
    if distance_m <= radius_m:
        return "in"
    if distance_m > radius_m + hysteresis_margin(radius_m):
        return "out"
    return previous  # in the dead band: no change


def _parse_ts(ts: str) -> datetime:
    """Stored timestamps are UTC, with or without a 'Z'."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Pushover ────────────────────────────────────────────

async def send_pushover(app_token: str, user_key: str, title: str, message: str,
                        url: str | None = None) -> tuple[bool, str]:
    data = {"token": app_token, "user": user_key, "title": title, "message": message}
    if url:
        data["url"] = url
        data["url_title"] = "Open in Maps"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(PUSHOVER_URL, data=data)
        if resp.is_success:
            return True, "sent"
        return False, f"Pushover {resp.status_code}: {resp.text[:200]}"
    except httpx.HTTPError as e:
        return False, f"Pushover unreachable: {e}"


async def _notify(db, rule, user_row, title: str, message: str, url: str | None):
    import json
    app_token = await get_setting(db, "pushover_app_token", "")
    user_key = json.loads(user_row["prefs"] or "{}").get("pushover_user_key", "")
    delivered = False
    if app_token and user_key:
        delivered, info = await send_pushover(app_token, user_key, title, message, url)
        if not delivered:
            log.warning("Alert for %s not delivered: %s", user_row["username"], info)
    await db.execute(
        "INSERT INTO alert_events (rule_id, user_id, message, delivered) VALUES (?, ?, ?, ?)",
        (rule["id"], user_row["id"], f"{title}: {message}", 1 if delivered else 0),
    )
    log.info("Alert for %s: %s: %s (delivered=%s)", user_row["username"], title, message, delivered)


# ── Evaluation (called by the poller) ───────────────────

RULES_SQL = """
    SELECT r.*, g.name AS geofence_name, g.latitude AS g_lat, g.longitude AS g_lon, g.radius_m,
           d.name AS device_name
    FROM alert_rules r
    JOIN devices d ON d.id = r.device_id AND d.enabled = 1
    LEFT JOIN geofences g ON g.id = r.geofence_id
    WHERE r.enabled = 1
"""


async def evaluate_rules(now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    async with connect() as db:
        rules = await (await db.execute(RULES_SQL)).fetchall()
        if not rules:
            return
        users = {r["id"]: r for r in await (await db.execute("SELECT * FROM users")).fetchall()}
        latest: dict[str, aiosqlite.Row | None] = {}
        for rule in rules:
            dev = rule["device_id"]
            if dev not in latest:
                latest[dev] = await (await db.execute(
                    "SELECT latitude, longitude, timestamp, accuracy FROM locations"
                    " WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1", (dev,)
                )).fetchone()
            user = users.get(rule["user_id"])
            if user is not None:
                await _evaluate_rule(db, rule, user, latest[dev], now)
        await db.commit()


async def _evaluate_rule(db, rule, user, loc, now: datetime):
    if loc is None:
        return
    maps_url = f"https://maps.apple.com/?q={loc['latitude']:.6f},{loc['longitude']:.6f}"

    if rule["kind"] == "stale":
        age_h = (now - _parse_ts(loc["timestamp"])).total_seconds() / 3600
        state = "stale" if age_h > rule["stale_hours"] else "fresh"
        if state == rule["last_state"]:
            return
        await db.execute("UPDATE alert_rules SET last_state = ? WHERE id = ?", (state, rule["id"]))
        if state == "stale":
            await _notify(db, rule, user, f"{rule['device_name']} has gone quiet",
                          f"No new location for {age_h:.0f}h (threshold {rule['stale_hours']:g}h).", maps_url)
        elif rule["last_state"] == "stale":
            await _notify(db, rule, user, f"{rule['device_name']} is reporting again",
                          "A fresh location came in.", maps_url)
        await db.execute("UPDATE alert_rules SET last_fired_at = CURRENT_TIMESTAMP WHERE id = ?", (rule["id"],))
        return

    if rule["g_lat"] is None:
        return
    if loc["accuracy"] is not None and loc["accuracy"] > rule["radius_m"]:
        return  # too imprecise to judge
    distance = haversine_m(loc["latitude"], loc["longitude"], rule["g_lat"], rule["g_lon"])
    previous = rule["last_state"]
    state = geofence_state(distance, rule["radius_m"], previous)
    if state is None or state == previous:
        return
    await db.execute("UPDATE alert_rules SET last_state = ? WHERE id = ?", (state, rule["id"]))
    if previous is None:
        return  # first observation just establishes the baseline
    entered = state == "in"
    if rule["kind"] == "both" or (rule["kind"] == "enter") == entered:
        verb = "arrived at" if entered else "left"
        await _notify(db, rule, user, f"{rule['device_name']} {verb} {rule['geofence_name']}",
                      f"{distance:.0f} m from the centre (radius {rule['radius_m']:.0f} m).", maps_url)
        await db.execute("UPDATE alert_rules SET last_fired_at = CURRENT_TIMESTAMP WHERE id = ?", (rule["id"],))


# ── Geofence CRUD ───────────────────────────────────────

class GeofenceBody(BaseModel):
    name: str
    latitude: float
    longitude: float
    radius_m: float


def _check_geofence(body: GeofenceBody):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    if not (-90 <= body.latitude <= 90 and -180 <= body.longitude <= 180):
        raise HTTPException(400, "Invalid coordinates")
    if not (MIN_RADIUS_M <= body.radius_m <= MAX_RADIUS_M):
        raise HTTPException(400, f"Radius must be {MIN_RADIUS_M}-{MAX_RADIUS_M} m")


async def _own_geofence(db, user, geofence_id: str):
    row = await (await db.execute(
        "SELECT * FROM geofences WHERE id = ? AND user_id = ?", (geofence_id, user["id"])
    )).fetchone()
    if not row:
        raise HTTPException(404, "Geofence not found")
    return row


@router.get("/geofences")
async def list_geofences(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    rows = await (await db.execute(
        "SELECT * FROM geofences WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user["id"],)
    )).fetchall()
    return [dict(r) for r in rows]


@router.post("/geofences")
async def create_geofence(body: GeofenceBody, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    _check_geofence(body)
    gid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO geofences (id, user_id, name, latitude, longitude, radius_m) VALUES (?, ?, ?, ?, ?, ?)",
        (gid, user["id"], body.name.strip(), body.latitude, body.longitude, body.radius_m),
    )
    await db.commit()
    return dict(await _own_geofence(db, user, gid))


@router.put("/geofences/{geofence_id}")
async def update_geofence(geofence_id: str, body: GeofenceBody, user=Depends(require_user),
                          db: aiosqlite.Connection = Depends(get_db)):
    await _own_geofence(db, user, geofence_id)
    _check_geofence(body)
    await db.execute(
        "UPDATE geofences SET name = ?, latitude = ?, longitude = ?, radius_m = ? WHERE id = ?",
        (body.name.strip(), body.latitude, body.longitude, body.radius_m, geofence_id),
    )
    # Moving the fence invalidates every rule's notion of in/out.
    await db.execute("UPDATE alert_rules SET last_state = NULL WHERE geofence_id = ?", (geofence_id,))
    await db.commit()
    return dict(await _own_geofence(db, user, geofence_id))


@router.delete("/geofences/{geofence_id}")
async def delete_geofence(geofence_id: str, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    await _own_geofence(db, user, geofence_id)
    await db.execute("DELETE FROM geofences WHERE id = ?", (geofence_id,))
    await db.commit()
    return {"ok": True}


# ── Alert rule CRUD ─────────────────────────────────────

class RuleBody(BaseModel):
    device_id: str
    kind: str
    geofence_id: Optional[str] = None
    stale_hours: Optional[float] = None
    enabled: bool = True


async def _check_rule(db, user, body: RuleBody):
    await require_device(db, user, body.device_id)
    if body.kind in ("enter", "exit", "both"):
        if not body.geofence_id:
            raise HTTPException(400, "Pick a geofence")
        await _own_geofence(db, user, body.geofence_id)
        body.stale_hours = None
    elif body.kind == "stale":
        if not body.stale_hours or not (0.5 <= body.stale_hours <= 24 * 30):
            raise HTTPException(400, "Stale threshold must be between 0.5 hours and 30 days")
        body.geofence_id = None
    else:
        raise HTTPException(400, "kind must be enter, exit, both or stale")


RULE_VIEW = """
    SELECT r.id, r.device_id, r.geofence_id, r.kind, r.stale_hours, r.enabled, r.last_state,
           r.last_fired_at, g.name AS geofence_name
    FROM alert_rules r LEFT JOIN geofences g ON g.id = r.geofence_id
    WHERE r.user_id = ?
"""


@router.get("/alerts")
async def list_rules(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    rows = await (await db.execute(RULE_VIEW + " ORDER BY r.created_at", (user["id"],))).fetchall()
    return [dict(r) for r in rows]


@router.post("/alerts")
async def create_rule(body: RuleBody, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    await _check_rule(db, user, body)
    rid = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO alert_rules (id, user_id, device_id, geofence_id, kind, stale_hours, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rid, user["id"], body.device_id, body.geofence_id, body.kind, body.stale_hours, 1 if body.enabled else 0),
    )
    await db.commit()
    row = await (await db.execute(RULE_VIEW + " AND r.id = ?", (user["id"], rid))).fetchone()
    return dict(row)


@router.put("/alerts/{rule_id}")
async def update_rule(rule_id: str, body: RuleBody, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    existing = await (await db.execute(
        "SELECT * FROM alert_rules WHERE id = ? AND user_id = ?", (rule_id, user["id"])
    )).fetchone()
    if not existing:
        raise HTTPException(404, "Alert not found")
    await _check_rule(db, user, body)
    # Changing what the rule watches resets its baseline.
    changed = (existing["device_id"], existing["geofence_id"], existing["kind"], existing["stale_hours"]) != \
              (body.device_id, body.geofence_id, body.kind, body.stale_hours)
    await db.execute(
        """UPDATE alert_rules SET device_id = ?, geofence_id = ?, kind = ?, stale_hours = ?, enabled = ?,
               last_state = CASE WHEN ? THEN NULL ELSE last_state END
           WHERE id = ?""",
        (body.device_id, body.geofence_id, body.kind, body.stale_hours, 1 if body.enabled else 0,
         1 if changed else 0, rule_id),
    )
    await db.commit()
    row = await (await db.execute(RULE_VIEW + " AND r.id = ?", (user["id"], rule_id))).fetchone()
    return dict(row)


@router.delete("/alerts/{rule_id}")
async def delete_rule(rule_id: str, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("DELETE FROM alert_rules WHERE id = ? AND user_id = ?", (rule_id, user["id"]))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(404, "Alert not found")
    return {"ok": True}


@router.get("/alerts/events")
async def list_events(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    rows = await (await db.execute(
        "SELECT id, rule_id, message, delivered, created_at FROM alert_events"
        " WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50", (user["id"],)
    )).fetchall()
    return [dict(r) for r in rows]


@router.post("/alerts/test")
async def test_notification(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    import json
    app_token = await get_setting(db, "pushover_app_token", "")
    if not app_token:
        raise HTTPException(400, "An admin needs to set the Pushover app token first")
    user_key = json.loads(user["prefs"] or "{}").get("pushover_user_key", "")
    if not user_key:
        raise HTTPException(400, "Set your Pushover user key first")
    ok, info = await send_pushover(app_token, user_key, "AirTag Logger", "Test notification - alerts are working.")
    if not ok:
        raise HTTPException(502, info)
    return {"ok": True}


# ── Admin: notification config ──────────────────────────

@router.get("/admin/notifications")
async def get_notifications(admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    return {"pushover_app_token_set": bool(await get_setting(db, "pushover_app_token", ""))}


class NotificationsBody(BaseModel):
    pushover_app_token: str


@router.put("/admin/notifications")
async def put_notifications(body: NotificationsBody, admin=Depends(require_admin),
                            db: aiosqlite.Connection = Depends(get_db)):
    token = body.pushover_app_token.strip()
    if token and not re.fullmatch(r"[A-Za-z0-9]{30}", token):
        raise HTTPException(400, "Pushover app tokens are 30 letters and digits")
    await set_setting(db, "pushover_app_token", token)
    await db.commit()
    return {"pushover_app_token_set": bool(token)}
