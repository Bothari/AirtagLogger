"""
Background polling service that fetches AirTag locations via FindMy.py
and stores them in the database.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiosqlite
from findmy import (
    AsyncAppleAccount,
    FindMyAccessory,
    LocalAnisetteProvider,
    LoginState,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

from app.database import DATA_DIR, DB_PATH

log = logging.getLogger(__name__)

DEVICES_DIR = os.path.join(DATA_DIR, "devices")
ACCOUNT_PATH = os.path.join(DATA_DIR, "account.json")
ANISETTE_LIBS_PATH = os.path.join(DATA_DIR, "ani_libs.bin")

# Polling state
_poll_task: asyncio.Task | None = None
_poll_interval: int = int(os.environ.get("POLL_INTERVAL_MINUTES", "15"))
_account: AsyncAppleAccount | None = None
_running = False

# Auth state (held between login and 2FA submit)
_pending_account: AsyncAppleAccount | None = None
_pending_2fa_methods: list | None = None


async def get_account() -> AsyncAppleAccount | None:
    """Load the saved Apple account session. Returns None if no session exists."""
    global _account
    if _account is not None:
        return _account

    try:
        _account = AsyncAppleAccount.from_json(
            ACCOUNT_PATH,
            anisette_libs_path=ANISETTE_LIBS_PATH,
        )
        log.info("Restored Apple account session for %s", _account.account_name)
        return _account
    except FileNotFoundError:
        log.warning("No Apple account session found at %s", ACCOUNT_PATH)
        log.warning("Run test_findmy.py interactively first to set up authentication")
        return None


def is_authenticated() -> bool:
    """Check if an Apple account session exists."""
    return os.path.exists(ACCOUNT_PATH)


def get_account_name() -> str | None:
    """Get the account email from the active session, if any."""
    if _account is not None:
        return _account.account_name
    return None


async def login(email: str, password: str) -> dict:
    """Start Apple ID login. Returns status and 2FA methods if needed."""
    global _pending_account, _pending_2fa_methods

    anisette = LocalAnisetteProvider(libs_path=ANISETTE_LIBS_PATH)
    account = AsyncAppleAccount(anisette)

    try:
        state = await account.login(email, password)
    except Exception as e:
        log.error("Login failed: %s", e)
        return {"status": "error", "message": str(e)}

    if state == LoginState.LOGGED_IN:
        account.to_json(ACCOUNT_PATH)
        set_account(account)
        return {"status": "ok"}

    if state == LoginState.REQUIRE_2FA:
        methods = await account.get_2fa_methods()
        _pending_account = account
        _pending_2fa_methods = methods

        method_list = []
        for i, m in enumerate(methods):
            if isinstance(m, TrustedDeviceSecondFactorMethod):
                method_list.append({"id": i, "type": "trusted_device"})
            elif isinstance(m, SmsSecondFactorMethod):
                method_list.append({"id": i, "type": "sms", "phone": m.phone_number})

        return {"status": "2fa", "methods": method_list}

    return {"status": "error", "message": f"Unexpected login state: {state}"}


async def submit_2fa(method_id: int, code: str) -> dict:
    """Submit a 2FA code to complete login."""
    global _pending_account, _pending_2fa_methods

    if _pending_account is None or _pending_2fa_methods is None:
        return {"status": "error", "message": "No pending login. Start login first."}

    if method_id < 0 or method_id >= len(_pending_2fa_methods):
        return {"status": "error", "message": "Invalid method ID."}

    method = _pending_2fa_methods[method_id]

    try:
        await method.request()
        await method.submit(code)
    except Exception as e:
        log.error("2FA submission failed: %s", e)
        return {"status": "error", "message": str(e)}

    _pending_account.to_json(ACCOUNT_PATH)
    set_account(_pending_account)

    _pending_account = None
    _pending_2fa_methods = None

    return {"status": "ok"}


def get_poll_interval() -> int:
    return _poll_interval


def set_poll_interval(minutes: int):
    global _poll_interval
    _poll_interval = minutes
    log.info("Poll interval changed to %d minutes", minutes)


def set_account(account: AsyncAppleAccount):
    """Set the active account and ensure polling is running."""
    global _account
    _account = account
    log.info("Account set: %s", account.account_name)
    start_polling()


def load_accessory(key_file_path: str) -> FindMyAccessory | None:
    """Load a single FindMy accessory from a key file."""
    try:
        return FindMyAccessory.from_json(Path(key_file_path))
    except Exception as e:
        log.error("Failed to load accessory from %s: %s", key_file_path, e)
        return None


async def poll_once() -> dict:
    """
    Fetch locations for all registered devices and store them in the database.
    Returns a summary dict with counts.
    """
    account = await get_account()
    if account is None:
        return {"error": "No Apple account session. Run initial auth first.", "fetched": 0}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, key_file FROM devices WHERE enabled = 1")
        devices = await cursor.fetchall()

    if not devices:
        log.info("No devices registered, skipping poll")
        return {"fetched": 0, "devices": 0}

    total_fetched = 0
    errors = 0

    for device in devices:
        device_id = device["id"]
        key_file = device["key_file"]
        key_path = os.path.join(DEVICES_DIR, key_file)

        if not os.path.exists(key_path):
            log.warning("Key file missing for device %s: %s", device_id, key_path)
            errors += 1
            continue

        accessory = load_accessory(key_path)
        if accessory is None:
            errors += 1
            continue

        try:
            location = await account.fetch_location(accessory)
        except Exception as e:
            log.error("Failed to fetch location for device %s: %s", device_id, e)
            errors += 1
            continue

        if location is None:
            log.info("No location returned for device %s", device_id)
            continue

        # Store in database
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await db.execute(
                    """INSERT INTO locations (device_id, latitude, longitude, timestamp)
                       VALUES (?, ?, ?, ?)""",
                    (device_id, location.latitude, location.longitude, str(location.timestamp)),
                )
                await db.commit()
                total_fetched += 1
                log.info(
                    "Stored location for %s: %.5f, %.5f @ %s",
                    device_id, location.latitude, location.longitude, location.timestamp,
                )
            except Exception as e:
                log.error("Failed to store location for device %s: %s", device_id, e)
                errors += 1

        # Save updated accessory state (key rotation)
        try:
            accessory.to_json(Path(key_path))
        except Exception as e:
            log.warning("Failed to save accessory state for %s: %s", device_id, e)

    # Save updated account state
    try:
        account.to_json(ACCOUNT_PATH)
    except Exception as e:
        log.warning("Failed to save account state: %s", e)

    summary = {"fetched": total_fetched, "devices": len(devices), "errors": errors}
    log.info("Poll complete: %s", summary)
    return summary


async def _poll_loop():
    """Background loop that polls at the configured interval."""
    global _running
    _running = True
    log.info("Polling started (interval: %d minutes)", _poll_interval)

    while _running:
        try:
            await poll_once()
        except Exception as e:
            log.exception("Poll failed: %s", e)

        await asyncio.sleep(_poll_interval * 60)


def start_polling():
    """Start the background polling task."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        log.warning("Polling already running")
        return
    _poll_task = asyncio.create_task(_poll_loop())
    log.info("Background polling task created")


async def stop_polling():
    """Stop the background polling task."""
    global _poll_task, _running, _account
    _running = False
    if _poll_task is not None:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None
    _account = None
    log.info("Polling stopped")
