# AirTag Logger - Project Plan

## Overview

A self-hosted Docker application that periodically logs Apple AirTag locations and displays them on an interactive map via a local web interface.

---

## Key Research Findings

### How AirTag Location Retrieval Works

The [FindMy.py](https://github.com/malmeloo/FindMy.py) library enables cross-platform querying of Apple's FindMy network **without requiring a Mac at runtime**. The workflow:

1. **AirTag keys must be exported from a Mac once** - The AirTag's cryptographic keys are stored in macOS's keychain. You run `python3 -m findmy decrypt --out-dir devices/` on a Mac to extract them into JSON files. This is a one-time setup step.
2. **An Apple ID is required** - The account must have been previously paired with a device. It doesn't need to be the same account that owns the AirTags, but it must be a valid Apple account. 2FA is required on first login, after which the session is persisted.
3. **Anisette provider** - Needed for authentication. The library includes a built-in local provider (recommended) that downloads ~2MB of Apple libraries on first run.
4. **Location fetching** - Once authenticated and with keys, calling `account.fetch_location(accessory)` returns a `LocationReport` with lat/lon/timestamp.

### Critical Limitations

- **macOS 26 (Tahoe)**: Currently **unsupported** for key extraction. The BeaconStoreKey is protected even with SIP disabled.
- **macOS 15 (Sequoia)**: May require an additional tool (`beaconstorekey-extractor`) + plist-to-json conversion.
- **macOS 14 and earlier**: The built-in `findmy decrypt` utility works directly.
- **No Mac at all**: Cannot extract keys for official AirTags. Only custom DIY tags (OpenHaystack) work without a Mac. **However, a macOS VM works — see below.**
- **Passkeys**: FindMy.py does not support passkey authentication. Must be disabled on the Apple account.

### AirTag Key Extraction via macOS VM (No Physical Mac Required)

Since no physical Mac is available, we will extract AirTag keys using a **macOS Monterey VM** running on Linux. This is a proven approach — at least one user on GitHub runs 5 Monterey VMs on a Windows PC to manage 160 AirTags.

#### Why Monterey

| macOS Version | VM Compatibility | Key Extraction |
|---|---|---|
| **Monterey (12)** | Best — works in VirtualBox, QEMU/KVM | `python3 -m findmy decrypt` works directly |
| **Sonoma (14)** | Works but more finicky | `python3 -m findmy decrypt` works directly |
| **Sequoia (15)** | Possible but harder | Needs `beaconstorekey-extractor` + SIP disable |
| **Tahoe (26)** | Possible | **Key extraction broken**, no known workaround |

Monterey is the sweet spot: best VM support and simplest key extraction.

#### VM Options

- **[OSX-KVM](https://github.com/kholia/OSX-KVM)** — macOS on QEMU/KVM with OpenCore. Well-documented, actively maintained.
- **[Docker-OSX](https://github.com/sickcodes/Docker-OSX)** — macOS in a Docker container (uses KVM under the hood). Easiest to set up.

#### Key Extraction Steps (Phase -1 of this project)

1. **Check KVM support**: `lscpu | grep Virtualization` — need VT-x or AMD-V enabled in BIOS
2. **Set up the VM** using Docker-OSX (simplest) or OSX-KVM:
   ```bash
   # Docker-OSX example (Monterey, with GUI via VNC/noVNC)
   docker run -it \
     --device /dev/kvm \
     -p 50922:10022 \
     -p 5999:5999 \
     -v /tmp/.X11-unix:/tmp/.X11-unix \
     -e "DISPLAY=${DISPLAY:-:0.0}" \
     -e GENERATE_UNIQUE=true \
     sickcodes/docker-osx:monterey
   ```
   - `GENERATE_UNIQUE=true` generates unique serial/board-id/UUID, critical for iCloud sign-in
3. **Inside the VM**:
   - Complete macOS setup
   - Sign into iCloud (System Preferences > Apple ID)
   - Open the FindMy app — wait for your AirTags to sync (may take a few minutes)
   - Open Terminal
   - Install Python: `xcode-select --install` or download from python.org
   - Install FindMy.py: `pip3 install findmy` (if deps like bleak/pyobjc fail to compile, use `pip3 install <pkg> --only-binary=:all:`)
   - Extract keys: `python3 -m findmy decrypt --out-dir ~/Desktop/devices/`
   - This pops up a keychain password prompt — enter your macOS login password
4. **Copy JSON files out of the VM**:
   - SCP via the forwarded SSH port: `scp -P 50922 user@localhost:~/Desktop/devices/*.json ./airtag-data/devices/`
   - Or use a shared folder / USB passthrough
5. **Sign out of iCloud**, shut down the VM. You won't need it again unless you add new AirTags.

#### Risks & Gotchas

- **iCloud sign-in can be flaky in VMs** — proper OpenCore serial spoofing is essential. `GENERATE_UNIQUE=true` in Docker-OSX handles this. If sign-in fails, try generating a new unique set.
- **Apple may flag the account** — rare, but possible. Use a secondary Apple ID if concerned. The account used for key extraction doesn't need to be the same one used for location fetching.
- **KVM must be available** — needs hardware virtualization support in your CPU/BIOS. Most modern CPUs have this.
- **First boot is slow** — macOS installer takes a while in a VM. Budget 30-60 minutes for the full setup.

---

## Architecture

### Components

```
┌─────────────────────────────────────────────────┐
│                 Docker Container                 │
│                                                  │
│  ┌──────────────┐   ┌────────────────────────┐  │
│  │   Frontend    │   │   Backend (FastAPI)     │  │
│  │   (Leaflet    │   │                        │  │
│  │    + Vanilla  │   │  - REST API            │  │
│  │    JS/HTML)   │   │  - Location poller     │  │
│  │              ◄├───┤►  - Auth management     │  │
│  └──────────────┘   │  - Device management    │  │
│                     └───────────┬────────────┘  │
│                                 │                │
│                     ┌───────────▼────────────┐  │
│                     │   SQLite Database       │  │
│                     │   /data/airtag.db       │  │
│                     └────────────────────────┘  │
│                                                  │
│  Volume mount: /data (DB, account.json,          │
│                       device keys, anisette libs)│
└─────────────────────────────────────────────────┘
```

### Why These Choices

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Database | **SQLite** | Zero config, single file, perfect for a single-user self-hosted app. No need for Postgres/MySQL overhead. Lives in the `/data` volume. |
| Backend | **FastAPI** | Async-native (matches FindMy.py's async API), lightweight, built-in OpenAPI docs, easy to serve static files. |
| Frontend | **Vanilla JS + Leaflet.js** | No build step, no node_modules. Leaflet is the standard for interactive maps. Served as static files by FastAPI. Uses OpenStreetMap tiles (free, no API key). |
| Docker | **Single container** | No need for docker-compose. SQLite is embedded, no external DB. Everything runs in one process with a background scheduler. |
| Config | **Environment variables + `/data` volume** | Minimal config: just mount a volume and optionally set env vars. Sensible defaults for everything. |

---

## Data Structures

### SQLite Schema

```sql
-- Devices (AirTags) registered in the system
CREATE TABLE devices (
    id          TEXT PRIMARY KEY,  -- UUID
    name        TEXT NOT NULL,     -- User-friendly name (e.g. "Car Keys")
    key_file    TEXT NOT NULL,     -- Filename of the device JSON in /data/devices/
    color       TEXT DEFAULT '#3388ff',  -- Map marker color
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Location history log
CREATE TABLE locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    timestamp   TIMESTAMP NOT NULL,  -- When Apple recorded the location
    fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- When we fetched it
    raw_report  TEXT,  -- Full JSON of the LocationReport (optional, for debugging)
    UNIQUE(device_id, timestamp)  -- Prevent duplicate entries
);

CREATE INDEX idx_locations_device_time ON locations(device_id, timestamp DESC);
```

### API Endpoints

```
GET  /                        -- Serve the map UI
GET  /api/devices             -- List all tracked devices
POST /api/devices             -- Add a new device (upload key JSON)
DELETE /api/devices/:id       -- Remove a device
GET  /api/locations/:device_id?from=&to=&limit=  -- Get location history
GET  /api/locations/latest    -- Get latest location for all devices
POST /api/poll                -- Manually trigger a location fetch
GET  /api/status              -- System status (last poll time, account status, etc.)
```

---

## Configuration

### Environment Variables (all optional, sensible defaults)

| Variable | Default | Description |
|----------|---------|-------------|
| `POLL_INTERVAL_MINUTES` | `15` | How often to fetch locations |
| `APPLE_EMAIL` | _(none)_ | Apple ID email (can also be set via web UI on first run) |
| `APPLE_PASSWORD` | _(none)_ | Apple ID password (can also be set via web UI on first run) |
| `DATA_DIR` | `/data` | Where to store DB, keys, account state |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `TZ` | `UTC` | Timezone for display |

### Volume Mount

The only required setup is mounting a persistent volume at `/data`:
```
docker run -v ./airtag-data:/data -p 8080:8080 airtag-logger
```

The `/data` directory contains:
```
/data/
  airtag.db              -- SQLite database
  account.json           -- Persisted Apple account session
  ani_libs.bin           -- Anisette libraries (auto-downloaded)
  devices/               -- AirTag key JSON files
    car-keys.json
    backpack.json
```

---

## Project Phases & Tasks

### Phase -1: AirTag Key Extraction via macOS VM — DONE
> This is a manual, one-time step. No code to write — just follow the procedure.

- [x] **-1.1** Verify KVM support: VT-x confirmed, `/dev/kvm` exists, user added to kvm group.
- [x] **-1.2** Set up macOS Monterey VM using Docker-OSX or OSX-KVM (see steps above)
- [x] **-1.3** Sign into iCloud in the VM, open FindMy, confirm AirTags appear
- [x] **-1.4** Install FindMy.py in the VM: `pip3 install findmy`
- [x] **-1.5** Extract keys: `python3 -m findmy decrypt --out-dir ~/Desktop/devices/`
- [x] **-1.6** Copy the JSON files to the host machine at `./data/devices/`
- [x] **-1.7** Sign out of iCloud, shut down VM

### Phase 0: Project Setup — DONE
- [x] **0.1** Initialize project structure (directories, .gitignore, git repo)
- [x] **0.2** Create Python package structure with requirements.txt
- [x] **0.3** Create basic Dockerfile and `run.sh` (builds + runs on port 8743)
- [x] **0.4** Set up SQLite database with schema (devices + locations tables)
- [x] **0.5** Create async database access layer (aiosqlite)
- [x] **0.6** Set up FastAPI app with static file serving
- [x] **0.7** Implement device CRUD endpoints
- [x] **0.8** Implement location query endpoints (with time range filtering)
- [x] **0.9** Implement status endpoint
- [x] **0.10** Create HTML page with Leaflet.js map, sidebar, device list
- [x] **0.11** Show latest device locations as markers on the map
- [x] **0.12** Add location history trail (polyline on map for selected device)

### Phase 1: AirTag Location Retrieval (CRITICAL - Prove Feasibility) — DONE
> Proved that extracted keys work for fetching locations on Linux. Golf Clubs AirTag returned lat/lon successfully.

- [x] **1.1** Write a standalone test script that authenticates with Apple and fetches a single AirTag location using FindMy.py
- [x] **1.2** Handle first-time login with 2FA interactively (via terminal for initial setup)
- [x] **1.3** Verify session persistence (login once, fetch locations on subsequent runs without 2FA)
- [x] **1.4** Test with multiple AirTags in a single fetch call
- [x] **1.5** Run `test_findmy.py` with real extracted keys to verify end-to-end location fetching works

### Phase 2: Backend Integration — DONE
> Polling service fetches locations every 15min (configurable via POLL_INTERVAL_MINUTES env var).
> Tested end-to-end: Golf Clubs AirTag location stored and served via API.

- [x] **2.1** Build the location polling service (asyncio background task in `app/poller.py`)
- [x] **2.2** Wire up FindMy.py integration into the polling service
- [x] **2.3** Implement manual poll trigger endpoint (`POST /api/poll`)

### Phase 3: UI/UX Redesign — DONE
> Two-page SPA: Map view with config sidebar/collapsible panel, Settings page with tabs. Dark theme, fully responsive.

- [x] **3.1** Design overall layout and visual style (dark theme, CSS variables, typography, spacing)
- [x] **3.2** Redesign config panel: device checkboxes with color dots, last-seen time, collapsible on mobile
- [x] **3.3** Add device management UI (add/remove devices, upload key files via drag-and-drop, color picker swatches)
- [x] **3.4** Add bottom nav bar for Map/Settings page switching
- [x] **3.5** Improve map UX: colored markers per device, history trail polylines, popups with coordinates + timestamp
- [x] **3.6** Add time range selector for history trails (1h, 24h, 7d, 30d, All)
- [x] **3.7** Add empty states ("No devices yet" with add button) and delete confirmation
- [x] **3.8** Mobile-responsive layout (collapsible config panel, touch-friendly bottom nav)
- [x] **3.9** Dark theme (dark CARTO map tiles, dark UI throughout)

### Phase 4: Docker & Deployment
- [ ] **4.1** Production Dockerfile (non-root user, health check)
- [ ] **4.2** Handle first-run setup flow (Apple account login via web UI or env vars)
- [ ] **4.3** Write README with setup instructions

### Phase 5: Polish & Quality of Life
- [ ] **5.1** Add proper error handling and retry logic for FindMy.py calls
- [ ] **5.2** Handle Apple session expiry gracefully (re-auth notification)
- [ ] **5.3** Add data retention settings (auto-delete locations older than X days)
- [ ] **6.4** Add export functionality (CSV/GPX export of location history)

---

## First-Run User Experience (Goal)

1. User extracts AirTag keys (one-time):
   - **If they have a Mac**: `python3 -m findmy decrypt --out-dir devices/`
   - **If no Mac**: Set up a macOS Monterey VM (Docker-OSX), sign into iCloud, extract keys (see Phase -1)
2. User copies the JSON key files to `./airtag-data/devices/`
3. User runs: `docker run -v ./airtag-data:/data -p 8080:8080 airtag-logger`
4. User opens `http://localhost:8080` — sees a setup wizard
5. Setup wizard asks for Apple ID email + password, handles 2FA
6. User names their devices, picks colors
7. Done — locations start appearing on the map

---

## Open Questions / Risks

1. **2FA in Docker**: The initial Apple login requires 2FA. We need either:
   - A web-based 2FA flow in the setup wizard (preferred)
   - Or: user logs in interactively via `docker exec` first (fallback)
2. **Session expiry**: How often does the Apple account session expire? Need to handle re-auth gracefully.
3. **Rate limiting**: Does Apple rate-limit FindMy queries? Need to test and potentially add backoff logic.
4. **Multiple AirTags**: Does `fetch_location([tag1, tag2, ...])` work efficiently in a single call? (The docs suggest yes.)
5. **Key rotation**: AirTags rotate their keys. The library handles this via state persistence (`airtag.to_json()`), but we need to make sure we save state after every fetch.
