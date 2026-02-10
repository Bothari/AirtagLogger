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
    raw_report  TEXT
);

CREATE INDEX IF NOT EXISTS idx_locations_device_time
    ON locations(device_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE devices ADD COLUMN original_name TEXT",
    "ALTER TABLE devices ADD COLUMN enabled INTEGER DEFAULT 1",
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
INSERT OR IGNORE INTO locations_new SELECT * FROM locations;
DROP TABLE locations;
ALTER TABLE locations_new RENAME TO locations;
CREATE INDEX IF NOT EXISTS idx_locations_device_time ON locations(device_id, timestamp DESC);
"""


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
        # Drop UNIQUE constraint on locations if it exists
        cursor = await db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='locations'")
        row = await cursor.fetchone()
        if row and "UNIQUE" in row[0]:
            await db.executescript(DROP_UNIQUE_MIGRATION)
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
