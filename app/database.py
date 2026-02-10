import os

import aiosqlite

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
    UNIQUE(device_id, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_locations_device_time
    ON locations(device_id, timestamp DESC);
"""

MIGRATIONS = [
    "ALTER TABLE devices ADD COLUMN original_name TEXT",
    "ALTER TABLE devices ADD COLUMN enabled INTEGER DEFAULT 1",
]


async def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        # Run migrations for existing databases
        for migration in MIGRATIONS:
            try:
                await db.execute(migration)
            except Exception:
                pass  # Column already exists
        # Backfill original_name for devices that don't have one
        await db.execute("UPDATE devices SET original_name = name WHERE original_name IS NULL")
        await db.commit()


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
