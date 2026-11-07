#!/usr/bin/env python3
"""
Phase 1: Test script for AirTag location fetching via FindMy.py.

Proves that extracted keys work on Linux for fetching locations.
Handles first-time login with 2FA, persists the session for future runs,
and supports fetching from multiple AirTags at once.

Usage:
    # First run (will prompt for Apple ID + 2FA):
    python3 tools/test_findmy.py

    # Subsequent runs (uses saved session):
    python3 tools/test_findmy.py

    # Fetch a specific device only:
    python3 tools/test_findmy.py --device car-keys.json

All state is stored in ./data/ (account session, anisette libs, device keys).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from findmy import (
    AppleAccount,
    FindMyAccessory,
    LocalAnisetteProvider,
    LoginState,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DEVICES_DIR = DATA_DIR / "devices"
ACCOUNT_PATH = DATA_DIR / "account.json"
ANISETTE_LIBS_PATH = DATA_DIR / "ani_libs.bin"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def login(account: AppleAccount) -> None:
    """Interactive login with 2FA support."""
    email = input("Apple ID email: ")
    password = input("Apple ID password: ")

    state = account.login(email, password)

    if state == LoginState.REQUIRE_2FA:
        methods = account.get_2fa_methods()

        print("\n2FA methods:")
        for i, method in enumerate(methods):
            if isinstance(method, TrustedDeviceSecondFactorMethod):
                print(f"  {i} - Trusted Device")
            elif isinstance(method, SmsSecondFactorMethod):
                print(f"  {i} - SMS ({method.phone_number})")

        idx = int(input("\nSelect method: "))
        method = methods[idx]
        method.request()

        code = input("Enter 2FA code: ")
        method.submit(code)

    elif state == LoginState.LOGGED_IN:
        print("Logged in (no 2FA required).")
    else:
        print(f"Unexpected login state: {state}", file=sys.stderr)
        sys.exit(1)


def get_account() -> AppleAccount:
    """Restore saved session or prompt for login."""
    try:
        acc = AppleAccount.from_json(
            str(ACCOUNT_PATH),
            anisette_libs_path=str(ANISETTE_LIBS_PATH),
        )
        log.info("Restored saved session for %s", acc.account_name)
    except FileNotFoundError:
        log.info("No saved session found. Logging in...")
        anisette = LocalAnisetteProvider(libs_path=str(ANISETTE_LIBS_PATH))
        acc = AppleAccount(anisette)
        login(acc)
        acc.to_json(str(ACCOUNT_PATH))
        log.info("Session saved to %s", ACCOUNT_PATH)

    return acc


def load_accessories(device_filter: str | None) -> list[tuple[Path, FindMyAccessory]]:
    """Load device key files from the devices directory."""
    if not DEVICES_DIR.exists():
        print(f"No devices directory at {DEVICES_DIR}", file=sys.stderr)
        print("Copy your AirTag key JSON files there first.", file=sys.stderr)
        sys.exit(1)

    key_files = list(DEVICES_DIR.glob("*.json"))
    if not key_files:
        print(f"No .json key files found in {DEVICES_DIR}", file=sys.stderr)
        sys.exit(1)

    if device_filter:
        key_files = [f for f in key_files if f.name == device_filter]
        if not key_files:
            print(f"Device file '{device_filter}' not found in {DEVICES_DIR}", file=sys.stderr)
            sys.exit(1)

    accessories = []
    for path in key_files:
        try:
            acc = FindMyAccessory.from_json(path)
            accessories.append((path, acc))
            log.info("Loaded device key: %s", path.name)
        except Exception as e:
            log.warning("Failed to load %s: %s", path.name, e)

    return accessories


def main() -> int:
    parser = argparse.ArgumentParser(description="Test AirTag location fetching")
    parser.add_argument("--device", help="Fetch only this device key file (e.g. car-keys.json)")
    args = parser.parse_args()

    # Load device keys
    accessories = load_accessories(args.device)
    if not accessories:
        print("No valid device keys loaded.", file=sys.stderr)
        return 1

    print(f"\nLoaded {len(accessories)} device(s)")

    # Authenticate
    account = get_account()
    print(f"Logged in as: {account.account_name} ({account.first_name} {account.last_name})\n")

    # Fetch locations
    print("=" * 60)
    for path, accessory in accessories:
        print(f"\nDevice: {path.stem}")
        try:
            location = account.fetch_location(accessory)
            if location:
                print(f"  Latitude:  {location.latitude}")
                print(f"  Longitude: {location.longitude}")
                print(f"  Timestamp: {location.timestamp}")
            else:
                print("  No location found (device may be out of range)")
        except Exception as e:
            log.error("  Failed to fetch: %s", e)

    print("\n" + "=" * 60)

    # Save updated state
    account.to_json(str(ACCOUNT_PATH))
    for path, accessory in accessories:
        accessory.to_json(path)
    log.info("Saved updated state")

    return 0


if __name__ == "__main__":
    sys.exit(main())
