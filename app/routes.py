import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import Response
from pydantic import BaseModel

import aiosqlite

from app.auth import require_admin, require_device, require_user, visible_device_ids
from app.database import get_db

router = APIRouter()

HEX_COLOR_LEN = 7


def _check_color(color: str) -> str:
    if len(color) != HEX_COLOR_LEN or not color.startswith("#"):
        raise HTTPException(status_code=400, detail="Color must be #rrggbb")
    try:
        int(color[1:], 16)
    except ValueError:
        raise HTTPException(status_code=400, detail="Color must be #rrggbb")
    return color


def _placeholders(items) -> str:
    return ",".join("?" * len(items))


# Device rows as a given user sees them. Name and colour are the same for
# everyone; `hidden` is this user's own map visibility. `access` is admin / edit / view.
DEVICE_SELECT = """
    SELECT d.id, d.name, d.color, d.original_name, d.enabled, d.created_at,
           COALESCE(p.hidden, 0) AS hidden,
           CASE WHEN :is_admin THEN 'admin'
                WHEN s.can_edit THEN 'edit'
                ELSE 'view' END AS access,
           (SELECT MAX(l.fetched_at) FROM locations l WHERE l.device_id = d.id) AS last_fetched
    FROM devices d
    LEFT JOIN user_device_prefs p ON p.device_id = d.id AND p.user_id = :uid
    LEFT JOIN device_shares s ON s.device_id = d.id AND s.user_id = :uid
    WHERE (:is_admin OR s.user_id IS NOT NULL)
"""


def _device_params(user) -> dict:
    return {"uid": user["id"], "is_admin": 1 if user["role"] == "admin" else 0}


async def _device_for(db, user, device_id: str) -> dict:
    row = await (await db.execute(DEVICE_SELECT + " AND d.id = :did",
                                  {**_device_params(user), "did": device_id})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Device not found")
    return dict(row)


# ── Apple account (admin only) ────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

class TwoFactorRequest(BaseModel):
    method_id: int
    code: str


@router.get("/apple/status")
async def apple_status(user=Depends(require_user)):
    from app.poller import is_authenticated, get_account_name
    return {
        "authenticated": is_authenticated(),
        # Only admins need to know which Apple ID backs the app.
        "account": get_account_name() if user["role"] == "admin" else None,
    }


@router.post("/apple/login")
async def apple_login(req: LoginRequest, admin=Depends(require_admin)):
    from app.poller import login
    return await login(req.email, req.password)


@router.post("/apple/2fa")
async def apple_2fa(req: TwoFactorRequest, admin=Depends(require_admin)):
    from app.poller import submit_2fa
    return await submit_2fa(req.method_id, req.code)


# ── Device endpoints ──────────────────────────────────────

@router.get("/devices")
async def list_devices(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute(
        DEVICE_SELECT + " ORDER BY d.enabled DESC, d.name COLLATE NOCASE",
        _device_params(user),
    )
    return [dict(row) for row in await cursor.fetchall()]


@router.post("/devices")
async def add_device(
    name: str = Form(...),
    color: str = Form("#3388ff"),
    key_file: UploadFile = File(...),
    admin=Depends(require_admin),
    db: aiosqlite.Connection = Depends(get_db),
):
    import os
    from app.database import DATA_DIR

    _check_color(color)
    device_id = str(uuid.uuid4())
    filename = f"{device_id}.json"
    devices_dir = os.path.join(DATA_DIR, "devices")
    os.makedirs(devices_dir, exist_ok=True)

    content = await key_file.read()
    # Validate it's valid JSON
    try:
        key_data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Key file must be valid JSON")
    if not isinstance(key_data, dict) or "master_key" not in key_data:
        raise HTTPException(status_code=400, detail="Not a FindMy key file (no master_key)")

    # Use name from key file as original_name if available
    original_name = key_data.get("name") or name

    filepath = os.path.join(devices_dir, filename)
    with open(filepath, "wb") as f:
        f.write(content)

    await db.execute(
        "INSERT INTO devices (id, name, original_name, key_file, color) VALUES (?, ?, ?, ?, ?)",
        (device_id, name, original_name, filename, color),
    )
    await db.commit()
    return await _device_for(db, admin, device_id)


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    # locations has no ON DELETE CASCADE (it predates foreign keys being enforced)
    await db.execute("DELETE FROM locations WHERE device_id = ?", (device_id,))
    cursor = await db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    enabled: Optional[bool] = None


@router.patch("/devices/{device_id}")
async def update_device(device_id: str, update: DeviceUpdate, user=Depends(require_user),
                        db: aiosqlite.Connection = Depends(get_db)):
    """Change the shared name/colour everyone sees. Needs edit access; enabling and
    disabling polling is admin-only."""
    access = await require_device(db, user, device_id, edit=True)
    sets = []
    params = []
    if update.name is not None:
        if not update.name.strip():
            raise HTTPException(status_code=400, detail="Name can't be empty")
        sets.append("name = ?")
        params.append(update.name.strip())
    if update.color is not None:
        sets.append("color = ?")
        params.append(_check_color(update.color))
    if update.enabled is not None:
        if access != "admin":
            raise HTTPException(status_code=403, detail="Only admins can enable or disable devices")
        sets.append("enabled = ?")
        params.append(1 if update.enabled else 0)
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(device_id)
    await db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()
    return await _device_for(db, user, device_id)


class DevicePrefs(BaseModel):
    hidden: bool


@router.put("/devices/{device_id}/prefs")
async def update_device_prefs(device_id: str, body: DevicePrefs, user=Depends(require_user),
                              db: aiosqlite.Connection = Depends(get_db)):
    """Hide a device from this user's own map. Affects nobody else."""
    await require_device(db, user, device_id)
    if body.hidden:
        await db.execute(
            "INSERT OR REPLACE INTO user_device_prefs (user_id, device_id, hidden) VALUES (?, ?, 1)",
            (user["id"], device_id),
        )
    else:
        await db.execute("DELETE FROM user_device_prefs WHERE user_id = ? AND device_id = ?", (user["id"], device_id))
    await db.commit()
    return await _device_for(db, user, device_id)


@router.get("/admin/devices/{device_id}/shares")
async def get_device_shares(device_id: str, admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    await require_device(db, admin, device_id)
    rows = await (await db.execute(
        """SELECT u.id AS user_id, u.username, u.role, s.can_edit
           FROM users u LEFT JOIN device_shares s ON s.user_id = u.id AND s.device_id = ?
           ORDER BY u.username COLLATE NOCASE""",
        (device_id,),
    )).fetchall()
    return [{
        "user_id": r["user_id"], "username": r["username"], "role": r["role"],
        # admins see everything regardless of shares
        "access": "admin" if r["role"] == "admin" else
                  ("edit" if r["can_edit"] else "view") if r["can_edit"] is not None else None,
    } for r in rows]


class DeviceShare(BaseModel):
    access: Optional[str] = None  # None / "view" / "edit"


@router.put("/admin/devices/{device_id}/shares/{user_id}")
async def set_device_share(device_id: str, user_id: str, body: DeviceShare,
                           admin=Depends(require_admin), db: aiosqlite.Connection = Depends(get_db)):
    await require_device(db, admin, device_id)
    if not await (await db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,))).fetchone():
        raise HTTPException(status_code=404, detail="User not found")
    if body.access is None:
        await db.execute("DELETE FROM device_shares WHERE device_id = ? AND user_id = ?", (device_id, user_id))
        # Their alerts on this device would keep leaking its location.
        await db.execute(
            "DELETE FROM alert_rules WHERE device_id = ? AND user_id = ? AND (SELECT role FROM users WHERE id = ?) != 'admin'",
            (device_id, user_id, user_id),
        )
    elif body.access in ("view", "edit"):
        await db.execute(
            "INSERT OR REPLACE INTO device_shares (device_id, user_id, can_edit) VALUES (?, ?, ?)",
            (device_id, user_id, 1 if body.access == "edit" else 0),
        )
    else:
        raise HTTPException(status_code=400, detail="access must be null, view or edit")
    await db.commit()
    return {"ok": True}


# ── Locations ─────────────────────────────────────────────

@router.get("/locations/latest")
async def latest_locations(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    ids = await visible_device_ids(db, user)
    if not ids:
        return []
    cursor = await db.execute(f"""
        SELECT l.*, d.name AS device_name, d.color AS device_color
        FROM locations l
        INNER JOIN devices d ON l.device_id = d.id
        WHERE l.device_id IN ({_placeholders(ids)})
          AND l.timestamp = (
            SELECT MAX(l2.timestamp) FROM locations l2 WHERE l2.device_id = l.device_id
        )
    """, ids)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.get("/locations/{device_id}")
async def device_locations(
    device_id: str,
    limit: int = 100,
    offset: int = 0,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    user=Depends(require_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    await require_device(db, user, device_id)
    query = "SELECT id, device_id, latitude, longitude, timestamp, fetched_at, accuracy FROM locations WHERE device_id = ?"
    params: list = [device_id]

    if from_ts:
        query += " AND datetime(timestamp) >= datetime(?)"
        params.append(from_ts)
    if to_ts:
        query += " AND datetime(timestamp) <= datetime(?)"
        params.append(to_ts)

    query += " ORDER BY timestamp DESC"
    if limit > 0:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.post("/poll")
async def trigger_poll(admin=Depends(require_admin)):
    from app.poller import poll_once
    result = await poll_once()
    return result


@router.get("/status")
async def status(user=Depends(require_user), db: aiosqlite.Connection = Depends(get_db)):
    ids = await visible_device_ids(db, user)
    if not ids:
        return {"devices": 0, "locations": 0, "last_fetch": None}
    ph = _placeholders(ids)
    row = await (await db.execute(
        f"SELECT COUNT(*) AS n, MAX(fetched_at) AS last FROM locations WHERE device_id IN ({ph})", ids
    )).fetchone()
    return {
        "devices": len(ids),
        "locations": row["n"],
        "last_fetch": row["last"],
    }


# ── Global settings ───────────────────────────────────────

@router.get("/settings")
async def get_settings(user=Depends(require_user)):
    from app.poller import get_poll_interval, get_retention_days
    # Only these two -- the settings table also holds the session secret and SSO
    # client secret, which must never be returned.
    return {
        "poll_interval": str(get_poll_interval()),
        "retention_days": str(get_retention_days()),
    }


class SettingsUpdate(BaseModel):
    poll_interval: Optional[int] = None
    retention_days: Optional[int] = None


@router.put("/settings")
async def update_settings(update: SettingsUpdate, admin=Depends(require_admin),
                          db: aiosqlite.Connection = Depends(get_db)):
    result = {}
    if update.poll_interval is not None:
        val = max(1, min(1440, update.poll_interval))
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('poll_interval', ?)",
            (str(val),),
        )
        from app.poller import set_poll_interval
        set_poll_interval(val)
        result["poll_interval"] = val
    if update.retention_days is not None:
        val = max(0, min(3650, update.retention_days))
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('retention_days', ?)",
            (str(val),),
        )
        from app.poller import set_retention_days
        set_retention_days(val)
        result["retention_days"] = val
    if result:
        await db.commit()
    return result


# ── Export endpoint ──────────────────────────────────────

@router.get("/export")
async def export_data(
    devices: str = Query(..., description="Comma-separated device IDs"),
    format: str = Query("gpx", description="Export format: gpx or csv"),
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    user=Depends(require_user),
    db: aiosqlite.Connection = Depends(get_db),
):
    if format not in ("gpx", "csv"):
        raise HTTPException(status_code=400, detail="Format must be gpx or csv")
    allowed = set(await visible_device_ids(db, user))
    device_ids = [d.strip() for d in devices.split(",") if d.strip() in allowed]
    if not device_ids:
        raise HTTPException(status_code=400, detail="No devices specified")

    # Build device name lookup
    placeholders = _placeholders(device_ids)
    cursor = await db.execute(
        f"SELECT id, name FROM devices WHERE id IN ({placeholders})", device_ids
    )
    device_map = {row["id"]: dict(row) for row in await cursor.fetchall()}

    # Fetch locations
    query = f"SELECT * FROM locations WHERE device_id IN ({placeholders})"
    params: list = list(device_ids)
    if from_ts:
        query += " AND datetime(timestamp) >= datetime(?)"
        params.append(from_ts)
    if to_ts:
        query += " AND datetime(timestamp) <= datetime(?)"
        params.append(to_ts)
    query += " ORDER BY device_id, timestamp ASC"

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["device_name", "device_id", "latitude", "longitude", "timestamp", "fetched_at", "accuracy_m"])
        for row in rows:
            name = device_map.get(row["device_id"], {}).get("name", "Unknown")
            writer.writerow([name, row["device_id"], row["latitude"], row["longitude"],
                             row["timestamp"], row["fetched_at"], row["accuracy"]])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="airtag_export_{now}.csv"'},
        )

    # GPX format
    gpx = Element("gpx", version="1.1", creator="AirTag Logger",
                   xmlns="http://www.topografix.com/GPX/1/1")
    metadata = SubElement(gpx, "metadata")
    SubElement(metadata, "name").text = "AirTag Logger Export"
    SubElement(metadata, "time").text = datetime.now(timezone.utc).isoformat()

    # Group locations by device
    by_device: dict[str, list] = {}
    for row in rows:
        by_device.setdefault(row["device_id"], []).append(row)

    for dev_id, locs in by_device.items():
        dev = device_map.get(dev_id, {})
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = dev.get("name", dev_id)
        trkseg = SubElement(trk, "trkseg")
        for loc in locs:
            trkpt = SubElement(trkseg, "trkpt", lat=str(loc["latitude"]), lon=str(loc["longitude"]))
            SubElement(trkpt, "time").text = str(loc["timestamp"])

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(gpx, encoding="unicode").encode("utf-8")
    return Response(
        content=xml_bytes,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="airtag_export_{now}.gpx"'},
    )
