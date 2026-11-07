# AirTag Logger

A self-hosted web app that records where your AirTags have been. It polls
Apple's Find My network on a schedule, keeps every location in SQLite, and
shows them on a map with history trails — no Mac needed at runtime.

Built on [FindMy.py](https://github.com/malmeloo/FindMy.py).

## Features

- **Map** of every tag's latest position, with history trails, marker
  clustering and a timeline scrubber to replay where things were.
- **Polling** every 15 minutes by default (adjustable in the UI), or on demand.
- **Places** — circular or drawn (polygon) geofences.
- **Alerts** on entering or leaving a place, sent through
  [Pushover](https://pushover.net), with per-alert active hours and priority.
- **Multiple users** with admin/user roles; tags can be shared between users.
- **Single sign-on** via any OpenID Connect provider (e.g. Authentik). When SSO
  is on, it is the only way to sign in.
- **Export** history as GPX or CSV.
- Mobile-friendly, dark theme. Vanilla JS + Leaflet — no build step.

## How it works

Apple doesn't offer an API for this. Two things are needed:

1. **The AirTags' private keys.** These live in a Mac's keychain and are
   exported once, as JSON files. Any Mac running macOS 14 or earlier works; if
   you don't have one, a macOS VM does (see below). You only need to redo this
   when you add a tag.
2. **An Apple ID** to query the Find My network with. You sign in through the
   web UI (2FA is supported) and the session is saved. Passkeys aren't
   supported by FindMy.py, so the account must allow password sign-in. It
   doesn't have to be the account that owns the tags.

Location reports on the Find My network are end-to-end encrypted; the keys stay
on your server and are used there to decrypt the reports the Apple ID fetches.

## Quick start

```yaml
# docker-compose.yml
services:
  airtag-logger:
    build: .
    container_name: airtag-logger
    ports:
      - "8743:8080"
    volumes:
      - ./data:/data
    environment:
      - TZ=America/Los_Angeles
    restart: unless-stopped
```

```bash
docker compose up -d
docker logs airtag-logger 2>&1 | grep 'setup token'
```

Open `http://localhost:8743`. The first visit asks for that **one-time setup
token** (it is only printed to the log, so nobody else on your network can
claim the instance) and has you create the admin account. Then:

1. Connect the Apple ID when prompted, and complete 2FA. Every user's map is
   fed from this one account.
2. **Devices → AirTags**: upload the key JSON files exported from the Mac.
3. Locations appear on the map after the first poll.

`run.sh` does the same thing with plain `docker run`.

## Exporting AirTag keys

On a Mac (macOS 14 or earlier), signed into the iCloud account that owns the
tags, with Find My open and the tags visible:

```bash
pip3 install findmy
python3 -m findmy decrypt --out-dir ~/Desktop/devices/
```

You'll be prompted for the login keychain password. Each tag becomes one JSON
file.

macOS 15 needs extra tooling; macOS 26 currently can't export keys at all.

### No Mac? Use a VM

`start-macos-vm.sh` runs macOS Monterey in Docker via
[Docker-OSX](https://github.com/sickcodes/Docker-OSX) (requires KVM), reachable
over VNC at `localhost:5999` and SSH on port `50922`.

iCloud ties a sign-in to the Mac's serial number, so pin the VM's identity once
you've signed in, or every reboot will sign you out: copy
`macos-vm/identity.env.example` to `macos-vm/identity.env` and fill it in. The
disk image and `identity.env` are gitignored.

`tools/test_findmy.py` is a standalone check that exported keys work: it signs
in from the terminal and prints each tag's latest location.

[docs/PLAN.md](docs/PLAN.md) has the original research notes on key extraction
and which macOS versions work.

## Configuration

| Variable | Default | |
|---|---|---|
| `DATA_DIR` | `/data` | Database, Apple session, key files |
| `TZ` | `UTC` | Time zone used when displaying times |
| `POLL_INTERVAL_MINUTES` | `15` | Initial poll interval (changeable in the UI) |
| `LOG_LEVEL` | `INFO` | |
| `DISABLE_POLLING` | — | Set to `1` to turn the poller off (for development) |

Everything else — SSO, Pushover, users, places — is set in the UI and stored in
the database.

### Data directory

```
/data/
  airtag.db       SQLite database (locations, users, settings)
  account.json    Saved Apple ID session
  ani_libs.bin    Anisette libraries, downloaded on first run
  devices/        AirTag key files
```

`account.json` and the key files are as sensitive as the Apple ID itself:
anyone holding them can track your tags. Back the directory up, but keep it
private.

### Single sign-on

In **Settings → General**, enter your OIDC provider's URL, client ID and
secret. The redirect URI is `https://<your-host>/api/auth/oidc/callback`. The
admin enabling SSO must link their own account to it first, so you can't lock
yourself out by mistake.

If SSO breaks and nobody can sign in:

```bash
docker exec airtag-logger python -m app.recover disable-sso
```

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The app is FastAPI (`app/`) serving a static front end (`static/`). Schema
migrations run automatically at startup.

## License

MIT — see [LICENSE](LICENSE).
