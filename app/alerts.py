"""
Geofences and alerts.

Each user owns their own geofences and alert rules, on devices they can see.
After every poll, evaluate_rules() compares each rule's device against its
geofence (or staleness threshold) and notifies via Pushover on a change of state.

Geofences are circles or polygons (drawn areas, which may be concave).

AirTag fixes are noisy, so geofence state uses hysteresis: a device is 'out'
only once it is clearly beyond the boundary, and keeps its previous state in
the band just outside. A fix too imprecise to tell inside from outside leaves
the state unchanged: for circles, accuracy worse than the radius; for polygons,
accuracy worse than the fix's distance to the boundary.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import geo
from app.auth import require_admin, require_device, require_user
from app.database import connect, get_db, get_setting, set_setting
from app.geo import haversine_m

log = logging.getLogger(__name__)
router = APIRouter()

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
MIN_RADIUS_M = 25
MAX_RADIUS_M = 50_000
MIN_AREA_M2 = 500
POLYGON_MARGIN_M = 25.0


def hysteresis_margin(radius_m: float) -> float:
    return max(25.0, 0.15 * radius_m)


def geofence_state(distance_m: float, radius_m: float, previous: str | None) -> str | None:
    if distance_m <= radius_m:
        return "in"
    if distance_m > radius_m + hysteresis_margin(radius_m):
        return "out"
    return previous  # in the dead band: no change


def polygon_state(inside: bool, boundary_m: float, accuracy: float | None, previous: str | None) -> str | None:
    if accuracy is not None and accuracy >= boundary_m:
        return previous  # the fix's error reaches the boundary: could be either side
    if inside:
        return "in"
    if boundary_m > POLYGON_MARGIN_M:
        return "out"
    return previous  # just outside: no change


def _parse_ts(ts: str) -> datetime:
    """Stored timestamps are UTC, with or without a 'Z'."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Pushover ────────────────────────────────────────────

# Pushover priorities offered per alert. -2 (no notification at all) is left out.
PRIORITIES = {-1: "low", 0: "normal", 1: "high", 2: "emergency"}
# Emergency alerts repeat until acknowledged: every EMERGENCY_RETRY seconds,
# giving up after EMERGENCY_EXPIRE. Pushover's limits are retry >= 30, expire <= 10800.
EMERGENCY_RETRY = 60
EMERGENCY_EXPIRE = 3600


async def send_pushover(app_token: str, user_key: str, title: str, message: str,
                        url: str | None = None, priority: int = 0) -> tuple[bool, str]:
    data = {"token": app_token, "user": user_key, "title": title, "message": message, "priority": priority}
    if priority == 2:
        data["retry"] = EMERGENCY_RETRY
        data["expire"] = EMERGENCY_EXPIRE
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


# ── Alert hours ─────────────────────────────────────────

WINDOW_MODES = ("inside", "outside")


def _parse_hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Times must be HH:MM")


def in_window(now: datetime, start: str, end: str, tz: str) -> bool:
    """Is `now` within [start, end) local time? A window whose end is before its
    start runs overnight (22:00-07:00)."""
    local = now.astimezone(ZoneInfo(tz)).time().replace(tzinfo=None)
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    if s <= e:
        return s <= local < e
    return local >= s or local < e


def window_allows(rule, now: datetime) -> bool:
    """Whether a rule may notify right now, given its optional alert hours."""
    if not rule["window_mode"]:
        return True
    inside = in_window(now, rule["window_start"], rule["window_end"], rule["window_tz"])
    return inside if rule["window_mode"] == "inside" else not inside


async def _notify(db, rule, user_row, title: str, message: str, url: str | None, now: datetime):
    """Send (or, outside the rule's alert hours, just record) an alert. The rule's
    state is updated by the caller either way, so a change during muted hours is
    not reported later."""
    import json
    muted = not window_allows(rule, now)
    delivered = False
    if not muted:
        app_token = await get_setting(db, "pushover_app_token", "")
        user_key = json.loads(user_row["prefs"] or "{}").get("pushover_user_key", "")
        if app_token and user_key:
            delivered, info = await send_pushover(app_token, user_key, title, message, url,
                                                  priority=rule["priority"] or 0)
            if not delivered:
                log.warning("Alert for %s not delivered: %s", user_row["username"], info)
    await db.execute(
        "INSERT INTO alert_events (rule_id, user_id, message, delivered, muted) VALUES (?, ?, ?, ?, ?)",
        (rule["id"], user_row["id"], f"{title}: {message}", 1 if delivered else 0, 1 if muted else 0),
    )
    log.info("Alert for %s: %s: %s (delivered=%s, muted=%s)", user_row["username"], title, message, delivered, muted)


# ── Evaluation (called by the poller) ───────────────────

RULES_SQL = """
    SELECT r.*, g.name AS geofence_name, g.latitude AS g_lat, g.longitude AS g_lon, g.radius_m,
           g.shape, g.polygon, d.name AS device_name
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
                          f"No new location for {age_h:.0f}h (threshold {rule['stale_hours']:g}h).", maps_url, now)
        elif rule["last_state"] == "stale":
            await _notify(db, rule, user, f"{rule['device_name']} is reporting again",
                          "A fresh location came in.", maps_url, now)
        else:
            return  # first observation, and it's fresh: just the baseline
        await db.execute("UPDATE alert_rules SET last_fired_at = CURRENT_TIMESTAMP WHERE id = ?", (rule["id"],))
        return

    if rule["g_lat"] is None:
        return
    previous = rule["last_state"]
    if rule["shape"] == "polygon":
        poly = json.loads(rule["polygon"])
        inside = geo.point_in_polygon(loc["latitude"], loc["longitude"], poly)
        boundary = geo.distance_to_boundary_m(loc["latitude"], loc["longitude"], poly)
        state = polygon_state(inside, boundary, loc["accuracy"], previous)
        detail = f"{boundary:.0f} m {'inside' if inside else 'outside'} the boundary."
    else:
        if loc["accuracy"] is not None and loc["accuracy"] > rule["radius_m"]:
            return  # too imprecise to judge
        distance = haversine_m(loc["latitude"], loc["longitude"], rule["g_lat"], rule["g_lon"])
        state = geofence_state(distance, rule["radius_m"], previous)
        detail = f"{distance:.0f} m from the centre (radius {rule['radius_m']:.0f} m)."
    if state is None or state == previous:
        return
    await db.execute("UPDATE alert_rules SET last_state = ? WHERE id = ?", (state, rule["id"]))
    if previous is None:
        return  # first observation just establishes the baseline
    entered = state == "in"
    if rule["kind"] == "both" or (rule["kind"] == "enter") == entered:
        verb = "arrived at" if entered else "left"
        await _notify(db, rule, user, f"{rule['device_name']} {verb} {rule['geofence_name']}", detail, maps_url, now)
        await db.execute("UPDATE alert_rules SET last_fired_at = CURRENT_TIMESTAMP WHERE id = ?", (rule["id"],))


# ── Geofence CRUD ───────────────────────────────────────

class GeofenceBody(BaseModel):
    name: str
    shape: str = "circle"
    # circle
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    radius_m: Optional[float] = None
    # polygon: [[lat, lon], ...], not closed
    polygon: Optional[list[list[float]]] = None


def _valid_coord(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180


def _check_geofence(body: GeofenceBody) -> tuple:
    """Validate and return the row values (name, shape, lat, lon, radius_m, polygon_json).
    For polygons, lat/lon/radius describe a bounding circle used for display."""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    if body.shape == "circle":
        if body.latitude is None or body.longitude is None or body.radius_m is None:
            raise HTTPException(400, "A circle needs a centre and radius")
        if not _valid_coord(body.latitude, body.longitude):
            raise HTTPException(400, "Invalid coordinates")
        if not (MIN_RADIUS_M <= body.radius_m <= MAX_RADIUS_M):
            raise HTTPException(400, f"Radius must be {MIN_RADIUS_M}-{MAX_RADIUS_M} m")
        return name, "circle", body.latitude, body.longitude, body.radius_m, None
    if body.shape == "polygon":
        poly = body.polygon or []
        if len(poly) > 1 and poly[0] == poly[-1]:
            poly = poly[:-1]  # accept an explicitly closed ring
        if not (3 <= len(poly) <= geo.MAX_VERTICES):
            raise HTTPException(400, f"An area needs 3-{geo.MAX_VERTICES} points")
        if any(len(p) != 2 or not _valid_coord(*p) for p in poly):
            raise HTTPException(400, "Invalid coordinates")
        if not geo.is_simple(poly):
            raise HTTPException(400, "The outline crosses itself. Move a point so the edges don't cross.")
        if geo.area_m2(poly) < MIN_AREA_M2:
            raise HTTPException(400, f"The area is too small (minimum {MIN_AREA_M2} m²)")
        lat, lon, radius = geo.bounding_circle(poly)
        if radius > MAX_RADIUS_M:
            raise HTTPException(400, f"The area is too large (max {MAX_RADIUS_M // 1000} km across from its centre)")
        return name, "polygon", lat, lon, radius, json.dumps([[round(a, 7), round(b, 7)] for a, b in poly])
    raise HTTPException(400, "shape must be circle or polygon")


def _geofence_out(row) -> dict:
    out = dict(row)
    out["polygon"] = json.loads(row["polygon"]) if row["polygon"] else None
    return out


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
    return [_geofence_out(r) for r in rows]


@router.post("/geofences")
async def create_geofence(body: GeofenceBody, user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    values = _check_geofence(body)
    gid = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO geofences (id, user_id, name, shape, latitude, longitude, radius_m, polygon)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (gid, user["id"], *values),
    )
    await db.commit()
    return _geofence_out(await _own_geofence(db, user, gid))


@router.put("/geofences/{geofence_id}")
async def update_geofence(geofence_id: str, body: GeofenceBody, user=Depends(require_user),
                          db: aiosqlite.Connection = Depends(get_db)):
    await _own_geofence(db, user, geofence_id)
    values = _check_geofence(body)
    await db.execute(
        "UPDATE geofences SET name = ?, shape = ?, latitude = ?, longitude = ?, radius_m = ?, polygon = ?"
        " WHERE id = ?",
        (*values, geofence_id),
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
    priority: int = 0
    # Optional alert hours: notify only inside (or only outside) start-end, local time in tz.
    window_mode: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    window_tz: Optional[str] = None


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
    if body.priority not in PRIORITIES:
        raise HTTPException(400, "priority must be -1 (low), 0 (normal), 1 (high) or 2 (emergency)")
    if body.window_mode:
        if body.window_mode not in WINDOW_MODES:
            raise HTTPException(400, "window_mode must be inside or outside")
        start, end = _parse_hhmm(body.window_start), _parse_hhmm(body.window_end)
        if start == end:
            raise HTTPException(400, "Start and end times must differ")
        try:
            ZoneInfo(body.window_tz or "")
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(400, f"Unknown time zone {body.window_tz!r}")
        body.window_start, body.window_end = start.strftime("%H:%M"), end.strftime("%H:%M")
    else:
        body.window_mode = body.window_start = body.window_end = body.window_tz = None


RULE_VIEW = """
    SELECT r.id, r.device_id, r.geofence_id, r.kind, r.stale_hours, r.enabled, r.last_state,
           r.last_fired_at, r.priority, r.window_mode, r.window_start, r.window_end, r.window_tz,
           g.name AS geofence_name
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
        """INSERT INTO alert_rules (id, user_id, device_id, geofence_id, kind, stale_hours, enabled,
                                   priority, window_mode, window_start, window_end, window_tz)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rid, user["id"], body.device_id, body.geofence_id, body.kind, body.stale_hours, 1 if body.enabled else 0,
         body.priority, body.window_mode, body.window_start, body.window_end, body.window_tz),
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
    # Changing what the rule watches resets its baseline; hours/priority don't.
    changed = (existing["device_id"], existing["geofence_id"], existing["kind"], existing["stale_hours"]) != \
              (body.device_id, body.geofence_id, body.kind, body.stale_hours)
    await db.execute(
        """UPDATE alert_rules SET device_id = ?, geofence_id = ?, kind = ?, stale_hours = ?, enabled = ?,
               priority = ?, window_mode = ?, window_start = ?, window_end = ?, window_tz = ?,
               last_state = CASE WHEN ? THEN NULL ELSE last_state END
           WHERE id = ?""",
        (body.device_id, body.geofence_id, body.kind, body.stale_hours, 1 if body.enabled else 0,
         body.priority, body.window_mode, body.window_start, body.window_end, body.window_tz,
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
        "SELECT id, rule_id, message, delivered, muted, created_at FROM alert_events"
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
