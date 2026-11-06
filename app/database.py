import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "airtag.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    original_name   TEXT,
    key_file        TEXT NOT NULL,
    color           TEXT DEFAULT '#3388ff',
    enabled         INTEGER DEFAULT 1,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_report  TEXT,
    accuracy    REAL
);

CREATE INDEX IF NOT EXISTS idx_locations_device_time
    ON locations(device_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- App users. Local users have a password_hash; SSO users have (oidc_iss, oidc_sub).
-- A user can have both once they link SSO to a local account.
CREATE TABLE IF NOT EXISTS users (
    id                    TEXT PRIMARY KEY,
    username              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email                 TEXT NOT NULL DEFAULT '',
    role                  TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    password_hash         TEXT,
    force_password_change INTEGER NOT NULL DEFAULT 0,
    oidc_iss              TEXT,
    oidc_sub              TEXT,
    -- Bumped on password change/reset and role change; sessions carrying an older
    -- value are rejected, so those changes log the user out everywhere.
    session_version       INTEGER NOT NULL DEFAULT 1,
    prefs                 TEXT NOT NULL DEFAULT '{}',
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at         TIMESTAMP,
    UNIQUE (oidc_iss, oidc_sub)
);

-- Admin grants a user access to a device. Admins implicitly see every device.
CREATE TABLE IF NOT EXISTS device_shares (
    device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    can_edit   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, user_id)
);

-- Per-user map visibility. (name/color columns are unused: a device has a single
-- name and colour for everyone, kept on the devices row.)
CREATE TABLE IF NOT EXISTS user_device_prefs (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name       TEXT,
    color      TEXT,
    hidden     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, device_id)
);

CREATE TABLE IF NOT EXISTS geofences (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    latitude   REAL NOT NULL,
    longitude  REAL NOT NULL,
    radius_m   REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- 'circle' uses latitude/longitude/radius_m. 'polygon' tests against the
    -- [[lat, lon], ...] JSON in polygon; its lat/lon/radius_m are a bounding
    -- circle kept for display.
    shape      TEXT NOT NULL DEFAULT 'circle',
    polygon    TEXT
);

-- kind: 'enter' / 'exit' / 'both' (need geofence_id) or 'stale' (needs stale_hours).
-- last_state is the last known 'in'/'out' for geofence rules, or 'stale'/'fresh'
-- for stale rules; alerts fire only on a change of state.
CREATE TABLE IF NOT EXISTS alert_rules (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id     TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    geofence_id   TEXT REFERENCES geofences(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('enter', 'exit', 'both', 'stale')),
    stale_hours   REAL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_state    TEXT,
    last_fired_at TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Pushover priority: -1 low, 0 normal, 1 high, 2 emergency (repeats until acknowledged)
    priority      INTEGER NOT NULL DEFAULT 0,
    -- Optional alert hours: notify only 'inside' or only 'outside' window_start..window_end
    -- (HH:MM, local time in window_tz). A window ending before it starts runs overnight.
    window_mode   TEXT,
    window_start  TEXT,
    window_end    TEXT,
    window_tz     TEXT
);

CREATE TABLE IF NOT EXISTS alert_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    TEXT REFERENCES alert_rules(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message    TEXT NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    muted      INTEGER NOT NULL DEFAULT 0  -- happened outside the rule's alert hours: not sent
);
CREATE INDEX IF NOT EXISTS idx_alert_events_user ON alert_events(user_id, created_at DESC);
"""

MIGRATIONS = [
    "ALTER TABLE devices ADD COLUMN original_name TEXT",
    "ALTER TABLE devices ADD COLUMN enabled INTEGER DEFAULT 1",
    "ALTER TABLE locations ADD COLUMN accuracy REAL",
    "ALTER TABLE geofences ADD COLUMN shape TEXT NOT NULL DEFAULT 'circle'",
    "ALTER TABLE geofences ADD COLUMN polygon TEXT",
    "ALTER TABLE alert_rules ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE alert_rules ADD COLUMN window_mode TEXT",
    "ALTER TABLE alert_rules ADD COLUMN window_start TEXT",
    "ALTER TABLE alert_rules ADD COLUMN window_end TEXT",
    "ALTER TABLE alert_rules ADD COLUMN window_tz TEXT",
    "ALTER TABLE alert_events ADD COLUMN muted INTEGER NOT NULL DEFAULT 0",
]

DROP_UNIQUE_MIGRATION = """
CREATE TABLE IF NOT EXISTS locations_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    timestamp   TIMESTAMP NOT NULL,
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_report  TEXT
);
INSERT OR IGNORE INTO locations_new
    SELECT id, device_id, latitude, longitude, timestamp, fetched_at, raw_report FROM locations;
DROP TABLE locations;
ALTER TABLE locations_new RENAME TO locations;
CREATE INDEX IF NOT EXISTS idx_locations_device_time ON locations(device_id, timestamp DESC);
"""


async def _open() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # Needed for the ON DELETE CASCADEs on shares/prefs/rules; off by default in SQLite.
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # Drop UNIQUE constraint on locations if it exists. Runs before SCHEMA and
        # the accuracy migration because the rebuild uses a fixed column list.
        cursor = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='locations'")
        row = await cursor.fetchone()
        if row and "UNIQUE" in row[0]:
            await db.executescript(DROP_UNIQUE_MIGRATION)
        await db.executescript(SCHEMA)
        # Run migrations for existing databases
        for migration in MIGRATIONS:
            try:
                await db.execute(migration)
            except Exception:
                pass  # Column already exists
        # Backfill original_name for devices that don't have one
        await db.execute("UPDATE devices SET original_name = name WHERE original_name IS NULL")
        await _fix_local_timestamps(db)
        await db.commit()


async def _fix_local_timestamps(db):
    """One-time fix for location timestamps stored as local wall time with a "Z".

    The poller used to format FindMy.py's local-time timestamps with a literal
    "Z", so every stored time was the container's TZ (America/Los_Angeles here)
    mislabelled as UTC: 7-8 hours early. Convert them to real UTC. In the
    repeated hour when clocks go back, a wall time maps to two instants; pick
    the latest one that isn't after the row was fetched (fetched_at is true
    UTC, written by SQLite).
    """
    done = await (await db.execute("SELECT value FROM settings WHERE key = 'timestamps_utc'")).fetchone()
    if done:
        return
    try:
        tz = ZoneInfo(os.environ.get("TZ") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    rows = await (await db.execute("SELECT id, timestamp, fetched_at FROM locations")).fetchall()
    slack = timedelta(minutes=5)
    updates = []
    for row_id, ts, fetched_at in rows:
        try:
            wall = datetime.fromisoformat(ts.replace("Z", "").replace(" ", "T")).replace(tzinfo=None)
        except ValueError:
            continue
        candidates = sorted({wall.replace(tzinfo=tz, fold=f).astimezone(timezone.utc) for f in (0, 1)})
        chosen = candidates[0]
        if fetched_at and len(candidates) > 1:
            fetched = datetime.fromisoformat(fetched_at.replace(" ", "T")).replace(tzinfo=timezone.utc)
            plausible = [c for c in candidates if c <= fetched + slack]
            chosen = plausible[-1] if plausible else candidates[0]
        new = chosen.strftime("%Y-%m-%dT%H:%M:%SZ")
        if new != ts:
            updates.append((new, row_id))
    await db.executemany("UPDATE locations SET timestamp = ? WHERE id = ?", updates)
    await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('timestamps_utc', ?)",
                     (f"converted from {tz.key} on {datetime.now(timezone.utc):%Y-%m-%d}",))
    if updates:
        log.warning("Converted %d location timestamps from %s wall time to UTC", len(updates), tz.key)


@asynccontextmanager
async def connect():
    """DB connection for code outside request handlers (poller, alerts, startup)."""
    db = await _open()
    try:
        yield db
    finally:
        await db.close()


async def get_db():
    db = await _open()
    try:
        yield db
    finally:
        await db.close()


async def get_setting(db: aiosqlite.Connection, key: str, default: str | None = None) -> str | None:
    row = await (await db.execute("SELECT value FROM settings WHERE key = ?", (key,))).fetchone()
    return row["value"] if row else default


async def set_setting(db: aiosqlite.Connection, key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
