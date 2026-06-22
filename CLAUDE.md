# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Runs two systemd services on a Raspberry Pi:
- **`prusa-cameras`** — pushes RTSP snapshots to Prusa Connect, monitors PrusaLink for print events, starts/stops FFmpeg recordings, and uploads completed recordings to YouTube.
- **`prusa-cameras-web`** — FastAPI web UI that reads the shared SQLite database and serves `http://<pi>:8080`.

## Development commands

```sh
# Install dependencies locally
pip install -r requirements.txt

# Run the main service (reads config.yaml in cwd by default)
CONFIG=config.yaml python src/main.py

# Run the web UI (default port 8080)
CONFIG=config.yaml uvicorn web.app:app --host 0.0.0.0 --port 8080 --reload

# Deploy to Pi (rsync + pip if requirements changed)
bash scripts/update.sh

# Full install from scratch (Pi only)
bash scripts/setup.sh
```

Log level is controlled by `LOG_LEVEL` env var (default `INFO`).

## Architecture

**Two separate processes share one SQLite database** (`/var/lib/prusa-cameras/prusa.db`):
- `src/main.py` is the sole writer — it owns all schema creation and migration.
- `web/app.py` opens the DB read-only (`?mode=ro`) for queries; uses a separate RW connection only for upload state and recording metadata that the main service doesn't manage.

**Inter-process state for active recordings** is communicated through `/tmp/prusa-cameras-status.json` — the web UI reads this file to show live sessions. `_live_sessions()` in `web/app.py` cross-checks PIDs with `os.kill(pid, 0)` to filter stale entries.

### Key modules

| File | Responsibility |
|------|----------------|
| `src/main.py` | Wires everything together; owns `on_print_start`/`on_print_end` callbacks |
| `src/camera.py` | Persistent FFmpeg MJPEG pipe per camera; pushes JPEG frames to Prusa Connect |
| `src/printer_monitor.py` | Polls PrusaLink `/api/v1/status`; fires state-transition callbacks |
| `src/recorder.py` | Starts/stops per-camera FFmpeg recording sessions |
| `src/youtube_uploader.py` | Uploads MP4s to YouTube Data API v3 via resumable upload |
| `src/database.py` | All SQLite access from the main service; owns schema and migrations |
| `web/app.py` | FastAPI routes: config CRUD, recording control, stream proxy, stats |

### Printer state machine

`PrinterMonitor` tracks `IDLE → PRINTING → FINISHED/STOPPED/ERROR`. Important invariants:
- `ATTENTION` is treated as **active** (mid-print filament change prompts — must not trigger `on_print_end`).
- Printers often skip `FINISHED` and jump straight to `IDLE`; the monitor maps `IDLE` end-of-print to `FINISHED` in the DB.
- State is persisted through service restarts via `db.get_open_print_job()` — the monitor restores its `_print_active` / `_current_job_id` state on startup.

### Recording files

Filename format: `{camera_safe_name}_{label}_{YYYYMMDD_HHMMSS}.mp4`  
Example: `bed_camera_print_20240315_143022.mp4`

FFmpeg recording flags:
- `-movflags +frag_keyframe+empty_moov+faststart` — makes files valid even after SIGKILL
- `-f lavfi -i anullsrc` silent audio track — YouTube rejects video-only MP4s with "Processing Abandoned"
- `-c:v libx264 -g 30 -bf 0` — clean H.264 profile; long keyframe intervals from camera streams cause YouTube issues

### YouTube OAuth

The OAuth flow **bypasses `google_auth_oauthlib` entirely** and builds the PKCE URL + token exchange manually (in `web/app.py` around line 1472). This was necessary because `google_auth_oauthlib` silently drops `code_verifier` when reconstructing a flow from state. Credentials are stored as a `pickle`-serialised `google.oauth2.credentials.Credentials` object.

The flow uses redirect URI `http://localhost:8181` (nothing listens there) — the user copies the failed redirect URL from their browser address bar and pastes it back into the web UI.

### DB schema evolution

`Database._migrate()` in `src/database.py` applies incremental `ALTER TABLE ADD COLUMN` / `CREATE INDEX` statements. Each statement is wrapped in a try/except that swallows `OperationalError` (column already exists). **Both** `src/database.py` and `web/app.py` have their own copy of the migration list — keep them in sync when adding columns.

### Installed paths (on Pi)

| Path | Purpose |
|------|---------|
| `/opt/prusa-cameras/` | Installed source (rsync'd from repo) |
| `/etc/prusa-cameras/config.yaml` | Live config (edited by web UI) |
| `/var/lib/prusa-cameras/prusa.db` | Shared SQLite database |
| `/var/lib/prusa-cameras/recordings/` | MP4 output directory |
| `/var/lib/prusa-cameras/youtube_creds.json` | Pickled OAuth credentials |
