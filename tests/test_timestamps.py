import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiosqlite

from app.database import DB_PATH, _fix_local_timestamps
from app.poller import utc_stamp
from conftest import add_device, db


def test_utc_stamp_converts_local_time():
    local = datetime(2026, 11, 6, 8, 56, 20, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert utc_stamp(local) == "2026-11-06T16:56:20Z"
    assert utc_stamp(datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))) == "2026-07-01T19:00:00Z"


def run_fix():
    async def go():
        async with aiosqlite.connect(DB_PATH) as conn:
            await _fix_local_timestamps(conn)
            await conn.commit()
    asyncio.run(go())


def test_migration_converts_wall_time_to_utc(client, monkeypatch):
    add_device("d1", "Keys")
    rows = [  # stored (wall time mislabelled "Z"), fetched_at (true UTC), expected
        ("2026-10-31T12:00:00Z", "2026-10-31 19:05:00", "2026-10-31T19:00:00Z"),  # PDT, UTC-7
        ("2026-11-03T08:00:00Z", "2026-11-03 16:02:00", "2026-11-03T16:00:00Z"),  # PST, UTC-8
        # 01:30 on Nov 1 happened twice; fetched_at tells which
        ("2026-11-01T01:30:00Z", "2026-11-01 08:40:00", "2026-11-01T08:30:00Z"),  # first (PDT)
        ("2026-11-01T01:30:00Z", "2026-11-01 09:40:00", "2026-11-01T09:30:00Z"),  # second (PST)
    ]
    with db() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'timestamps_utc'")
        for ts, fetched, _ in rows:
            conn.execute("INSERT INTO locations (device_id, latitude, longitude, timestamp, fetched_at)"
                         " VALUES ('d1', 0, 0, ?, ?)", (ts, fetched))
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    run_fix()
    with db() as conn:
        got = [r[0] for r in conn.execute("SELECT timestamp FROM locations ORDER BY id")]
        flag = conn.execute("SELECT value FROM settings WHERE key = 'timestamps_utc'").fetchone()[0]
    assert got == [expected for *_, expected in rows]
    assert flag.startswith("converted from America/Los_Angeles")
    # only ever once: a second run must not shift UTC data again
    run_fix()
    with db() as conn:
        assert [r[0] for r in conn.execute("SELECT timestamp FROM locations ORDER BY id")] == got


def test_fresh_install_is_marked_done(client):
    with db() as conn:
        assert conn.execute("SELECT 1 FROM settings WHERE key = 'timestamps_utc'").fetchone()
