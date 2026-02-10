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

from app.database import get_db

router = APIRouter()


# ── Auth endpoints ────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str

class TwoFactorRequest(BaseModel):
    method_id: int
    code: str

class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/auth/status")
async def auth_status():
    from app.poller import is_authenticated, get_account_name
    return {
        "authenticated": is_authenticated(),
        "account": get_account_name(),
    }


@router.post("/auth/login")
async def auth_login(req: LoginRequest):
    from app.poller import login
    return await login(req.email, req.password)


@router.post("/auth/2fa")
async def auth_2fa(req: TwoFactorRequest):
    from app.poller import submit_2fa
    return await submit_2fa(req.method_id, req.code)


# ── Device endpoints ──────────────────────────────────────

@router.get("/devices")
async def list_devices(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("""
        SELECT d.*, (
            SELECT MAX(l.fetched_at) FROM locations l WHERE l.device_id = d.id
        ) AS last_fetched
        FROM devices d
        ORDER BY d.enabled DESC, d.name COLLATE NOCASE
    """)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.post("/devices")
async def add_device(
    name: str = Form(...),
    color: str = Form("#3388ff"),
    key_file: UploadFile = File(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    import os
    from app.database import DATA_DIR

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
    return {"id": device_id, "name": name, "original_name": original_name, "key_file": filename, "color": color, "enabled": 1}


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"ok": True}


@router.patch("/devices/{device_id}")
async def update_device(device_id: str, update: DeviceUpdate, db: aiosqlite.Connection = Depends(get_db)):
    sets = []
    params = []
    if update.name is not None:
        sets.append("name = ?")
        params.append(update.name)
    if update.color is not None:
        sets.append("color = ?")
        params.append(update.color)
    if update.enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if update.enabled else 0)
    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")
    params.append(device_id)
    cursor = await db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", params)
    await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Device not found")
    row = await db.execute("SELECT * FROM devices WHERE id = ?", (device_id,))
    device = await row.fetchone()
    return dict(device)


@router.get("/locations/latest")
async def latest_locations(db: aiosqlite.Connection = Depends(get_db)):
    cursor = await db.execute("""
        SELECT l.*, d.name as device_name, d.color as device_color
        FROM locations l
        INNER JOIN devices d ON l.device_id = d.id
        WHERE l.timestamp = (
            SELECT MAX(l2.timestamp) FROM locations l2 WHERE l2.device_id = l.device_id
        )
    """)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.get("/locations/{device_id}")
async def device_locations(
    device_id: str,
    limit: int = 100,
    offset: int = 0,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    query = "SELECT * FROM locations WHERE device_id = ?"
    params: list = [device_id]

    if from_ts:
        query += " AND timestamp >= ?"
        params.append(from_ts)
    if to_ts:
        query += " AND timestamp <= ?"
        params.append(to_ts)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.post("/poll")
async def trigger_poll():
    from app.poller import poll_once
    result = await poll_once()
    return result


@router.get("/status")
async def status(db: aiosqlite.Connection = Depends(get_db)):
    device_count = await db.execute("SELECT COUNT(*) FROM devices")
    device_count = (await device_count.fetchone())[0]

    location_count = await db.execute("SELECT COUNT(*) FROM locations")
    location_count = (await location_count.fetchone())[0]

    last_fetch = await db.execute(
        "SELECT MAX(fetched_at) FROM locations"
    )
    last_fetch = (await last_fetch.fetchone())[0]

    return {
        "devices": device_count,
        "locations": location_count,
        "last_fetch": last_fetch,
    }


@router.get("/settings")
async def get_settings(db: aiosqlite.Connection = Depends(get_db)):
    from app.poller import get_poll_interval
    cursor = await db.execute("SELECT key, value FROM settings")
    rows = await cursor.fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    # Default poll interval from env/poller if not in DB
    if "poll_interval" not in settings:
        settings["poll_interval"] = str(get_poll_interval())
    return settings


class SettingsUpdate(BaseModel):
    poll_interval: Optional[int] = None


@router.put("/settings")
async def update_settings(update: SettingsUpdate, db: aiosqlite.Connection = Depends(get_db)):
    if update.poll_interval is not None:
        val = max(1, min(1440, update.poll_interval))
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('poll_interval', ?)",
            (str(val),),
        )
        await db.commit()
        from app.poller import set_poll_interval
        set_poll_interval(val)
        return {"poll_interval": val}
    return {}


# ── Export endpoint ──────────────────────────────────────

@router.get("/export")
async def export_data(
    devices: str = Query(..., description="Comma-separated device IDs"),
    format: str = Query("gpx", description="Export format: gpx or csv"),
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    db: aiosqlite.Connection = Depends(get_db),
):
    device_ids = [d.strip() for d in devices.split(",") if d.strip()]
    if not device_ids:
        raise HTTPException(status_code=400, detail="No devices specified")
    if format not in ("gpx", "csv"):
        raise HTTPException(status_code=400, detail="Format must be gpx or csv")

    # Build device name lookup
    placeholders = ",".join("?" * len(device_ids))
    cursor = await db.execute(
        f"SELECT id, name, color FROM devices WHERE id IN ({placeholders})", device_ids
    )
    device_map = {row["id"]: dict(row) for row in await cursor.fetchall()}
    if not device_map:
        raise HTTPException(status_code=404, detail="No matching devices found")

    # Fetch locations
    query = f"SELECT * FROM locations WHERE device_id IN ({placeholders})"
    params: list = list(device_ids)
    if from_ts:
        query += " AND timestamp >= ?"
        params.append(from_ts)
    if to_ts:
        query += " AND timestamp <= ?"
        params.append(to_ts)
    query += " ORDER BY device_id, timestamp ASC"

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["device_name", "device_id", "latitude", "longitude", "timestamp", "fetched_at"])
        for row in rows:
            name = device_map.get(row["device_id"], {}).get("name", "Unknown")
            writer.writerow([name, row["device_id"], row["latitude"], row["longitude"], row["timestamp"], row["fetched_at"]])
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
