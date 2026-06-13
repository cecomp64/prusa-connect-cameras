"""
Prusa Camera Manager — web UI backend.

Runs as a separate process from the main snapshot/recording service.
Reads/writes the shared config.yaml and proxies RTSP streams as MJPEG.
"""

import asyncio
import json
import logging
import os
import pickle
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
import requests
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

logger = logging.getLogger("prusa_web")
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from pydantic import BaseModel

app = FastAPI(title="Prusa Camera Manager")


@app.on_event("startup")
def _on_startup() -> None:
    _migrate_db()
    _reset_stuck_uploads()


def _reset_stuck_uploads() -> None:
    """Reset uploads left in-progress from a previous run — those threads are gone."""
    try:
        with _open_db_rw() as conn:
            cur = conn.execute(
                "UPDATE youtube_uploads SET status='error', pct=0, "
                "error='Upload interrupted — click Retry to re-upload' "
                "WHERE status IN ('uploading', 'pending')"
            )
            conn.commit()
        if cur.rowcount:
            logger.info("Reset %d stuck upload(s) to error state", cur.rowcount)
    except Exception as exc:
        logger.warning("Could not reset stuck uploads: %s", exc)


CONFIG_PATH = Path(os.environ.get("CONFIG", "/etc/prusa-cameras/config.yaml"))



# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"cameras": [], "recording": {"output_dir": "/var/lib/prusa-cameras/recordings"}}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _find_cam(cfg: dict, name: str) -> Optional[dict]:
    return next((c for c in cfg.get("cameras", []) if c["name"] == name), None)


# ── Pydantic models ────────────────────────────────────────────────────────────

class CameraBody(BaseModel):
    name: str
    rtsp_url: str
    token: str
    fingerprint: str = ""
    webrtc_url: str = ""
    snapshot_interval: int = 10
    orientation: str = "landscape"


class PrusaLinkBody(BaseModel):
    host: str
    api_key: str
    poll_interval: int = 15


class YouTubeBody(BaseModel):
    enabled: bool = False
    client_secrets_file: str = ""
    credentials_cache: str = ""
    privacy: str = "unlisted"
    playlist_id: str = ""
    category_id: str = "28"
    keywords: list[str] = ["3d printing", "prusa", "timelapse"]


class RecordingBody(BaseModel):
    output_dir: str
    retention_days: int = 7


# ── Camera CRUD ────────────────────────────────────────────────────────────────

@app.get("/api/cameras")
def list_cameras():
    return load_config().get("cameras", [])


@app.post("/api/cameras", status_code=201)
def add_camera(body: CameraBody):
    cfg = load_config()
    cameras = cfg.setdefault("cameras", [])
    if any(c["name"] == body.name for c in cameras):
        raise HTTPException(409, f"Camera '{body.name}' already exists")
    data = body.model_dump()
    if not data["fingerprint"]:
        data["fingerprint"] = str(uuid.uuid4())
    cameras.append(data)
    save_config(cfg)
    return data


@app.put("/api/cameras/{name}")
def update_camera(name: str, body: CameraBody):
    cfg = load_config()
    cameras = cfg.get("cameras", [])
    for i, cam in enumerate(cameras):
        if cam["name"] == name:
            data = body.model_dump()
            if not data["fingerprint"]:
                data["fingerprint"] = cam.get("fingerprint", str(uuid.uuid4()))
            cameras[i] = data
            save_config(cfg)
            return data
    raise HTTPException(404, f"Camera '{name}' not found")


@app.delete("/api/cameras/{name}", status_code=204)
def delete_camera(name: str):
    cfg = load_config()
    before = len(cfg.get("cameras", []))
    cfg["cameras"] = [c for c in cfg.get("cameras", []) if c["name"] != name]
    if len(cfg["cameras"]) == before:
        raise HTTPException(404)
    save_config(cfg)


# ── Settings endpoints ─────────────────────────────────────────────────────────

@app.get("/api/prusalink")
def get_prusalink():
    return load_config().get("prusalink", {})


@app.put("/api/prusalink")
def update_prusalink(body: PrusaLinkBody):
    cfg = load_config()
    cfg["prusalink"] = body.model_dump()
    save_config(cfg)
    return cfg["prusalink"]


def _open_db() -> sqlite3.Connection:
    """Open a read-only connection to the shared SQLite database."""
    path = load_config().get("db_path", "/var/lib/prusa-cameras/prusa.db")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _open_db_rw() -> sqlite3.Connection:
    path = load_config().get("db_path", "/var/lib/prusa-cameras/prusa.db")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_db() -> None:
    """Run all incremental schema migrations. Safe to call on every startup."""
    # UNIQUE cannot be inline in ALTER TABLE ADD COLUMN in SQLite — use a separate index
    stmts = [
        "ALTER TABLE youtube_uploads ADD COLUMN filename TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_yt_filename ON youtube_uploads(filename)",
        "ALTER TABLE youtube_uploads ADD COLUMN pct INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE recordings ADD COLUMN file_deleted INTEGER NOT NULL DEFAULT 0",
        (
            "CREATE TABLE IF NOT EXISTS printer_files ("
            "  storage TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,"
            "  display_name TEXT, size INTEGER DEFAULT 0,"
            "  file_timestamp INTEGER DEFAULT 0, last_seen_ts INTEGER NOT NULL,"
            "  PRIMARY KEY (storage, path)"
            ")"
        ),
    ]
    try:
        with _open_db_rw() as conn:
            for stmt in stmts:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column/index already exists
            conn.commit()
    except Exception as exc:
        logger.warning("DB migration failed: %s", exc)


def _write_upload(filename: str, status: str, url: str | None = None, error: str | None = None, pct: int = 0) -> None:
    try:
        with _open_db_rw() as conn:
            conn.execute(
                """INSERT INTO youtube_uploads (filename, status, pct, url, error, uploaded_ts)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET
                     status=excluded.status, pct=excluded.pct, url=excluded.url,
                     error=excluded.error, uploaded_ts=excluded.uploaded_ts""",
                (filename, status, pct, url, error, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not persist upload state to DB: %s", exc)


def _write_upload_pct(filename: str, pct: int) -> None:
    try:
        with _open_db_rw() as conn:
            conn.execute("UPDATE youtube_uploads SET pct=? WHERE filename=?", (pct, filename))
            conn.commit()
    except Exception as exc:
        logger.warning("Could not update upload pct in DB: %s", exc)


@app.get("/api/printer/status")
def get_printer_status():
    cfg = load_config()
    pl  = cfg.get("prusalink", {})

    if not pl.get("host") or not pl.get("api_key"):
        return {"configured": False}

    try:
        with _open_db() as conn:
            row = conn.execute(
                "SELECT * FROM printer_status WHERE id = 1"
            ).fetchone()
            if row is None:
                # printer_status table exists but has no row yet — fall back to
                # the historical telemetry table (e.g. main service not restarted yet)
                row = conn.execute(
                    "SELECT * FROM printer_telemetry ORDER BY ts DESC LIMIT 1"
                ).fetchone()
    except Exception:
        # DB doesn't exist yet, or printer_status table not created yet
        try:
            with _open_db() as conn:
                row = conn.execute(
                    "SELECT * FROM printer_telemetry ORDER BY ts DESC LIMIT 1"
                ).fetchone()
        except Exception:
            return {"configured": True, "reachable": False, "error": "No data yet — is the main service running?", "printer": None, "job": None}

    if not row:
        return {"configured": True, "reachable": False, "error": "Waiting for first poll — printer may be idle", "printer": None, "job": None}

    age = int(time.time()) - row["ts"]
    reachable = age < 60  # stale after 60s (main service polls every 10s)

    has_job = row["job_progress"] is not None or row["job_time_printing"] is not None

    return {
        "configured": True,
        "reachable":  reachable,
        "error":      f"Data is {age}s old — main service may be down" if not reachable else None,
        "printer": {
            "state":         row["state"],
            "temp_nozzle":   row["temp_nozzle"],
            "target_nozzle": row["target_nozzle"],
            "temp_bed":      row["temp_bed"],
            "target_bed":    row["target_bed"],
            "axis_z":        row["axis_z"],
            "flow":          row["flow"],
            "speed":         row["speed"],
            "fan_hotend":    row["fan_hotend"],
            "fan_print":     row["fan_print"],
        },
        "job": {
            "progress":       row["job_progress"],
            "time_remaining": row["job_time_remaining"],
            "time_printing":  row["job_time_printing"],
            "display_name":   row["job_display_name"],
        } if has_job else None,
    }


@app.get("/api/system/status")
def get_system_status():
    cpu_temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        pass

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime_secs = int(time.time() - psutil.boot_time())

    # Raspberry Pi throttle / under-voltage detection via vcgencmd
    throttle: dict = {}
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2
        )
        val = int(result.stdout.strip().split("=")[1], 16)
        throttle = {
            "under_voltage":              bool(val & 0x00001),
            "freq_capped":                bool(val & 0x00002),
            "throttled":                  bool(val & 0x00004),
            "soft_temp_limit":            bool(val & 0x00008),
            "under_voltage_occurred":     bool(val & 0x10000),
            "freq_capped_occurred":       bool(val & 0x20000),
            "throttled_occurred":         bool(val & 0x40000),
            "soft_temp_limit_occurred":   bool(val & 0x80000),
        }
    except Exception:
        pass

    return {
        "cpu_temp":   cpu_temp,
        "cpu_usage":  psutil.cpu_percent(interval=0.1),
        "mem_used":   round(mem.used / 1024 / 1024),
        "mem_total":  round(mem.total / 1024 / 1024),
        "disk_free":  round(disk.free / 1024 / 1024 / 1024, 1),
        "disk_total": round(disk.total / 1024 / 1024 / 1024, 1),
        "uptime":     uptime_secs,
        **throttle,
    }


@app.get("/api/stats")
def get_stats():
    try:
        conn = _open_db()
    except Exception:
        conn = None

    # ── Summary ───────────────────────────────────────────────────────────────────
    if conn:
        with conn:
            summary = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(duration_seconds), 0) AS total_secs "
                "FROM print_jobs WHERE end_ts IS NOT NULL"
            ).fetchone()
            longest_row = conn.execute(
                "SELECT duration_seconds, display_name FROM print_jobs "
                "WHERE end_ts IS NOT NULL AND end_state != 'IDLE' ORDER BY duration_seconds DESC LIMIT 1"
            ).fetchone()
            month_rows = conn.execute(
                "SELECT strftime('%Y-%m', datetime(start_ts,'unixepoch')) AS month, "
                "COUNT(*) AS count, COALESCE(SUM(duration_seconds),0) AS total_secs "
                "FROM print_jobs WHERE end_ts IS NOT NULL "
                "GROUP BY month ORDER BY month"
            ).fetchall()
            wd_rows = conn.execute(
                "SELECT CAST(strftime('%w', datetime(start_ts,'unixepoch')) AS INTEGER) AS wd, "
                "COUNT(*) AS count, COALESCE(SUM(duration_seconds),0) AS total_secs "
                "FROM print_jobs WHERE end_ts IS NOT NULL GROUP BY wd"
            ).fetchall()
            dur_rows = conn.execute(
                "SELECT duration_seconds FROM print_jobs WHERE end_ts IS NOT NULL"
            ).fetchall()
            outcome_rows = conn.execute(
                "SELECT COALESCE(end_state,'UNKNOWN') AS state, COUNT(*) AS count "
                "FROM print_jobs WHERE end_ts IS NOT NULL GROUP BY state"
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT pj.id, "
                "COALESCE(pj.display_name, "
                "  (SELECT pt.job_display_name FROM printer_telemetry pt "
                "   WHERE pt.ts BETWEEN pj.start_ts AND pj.end_ts "
                "   AND pt.job_display_name IS NOT NULL LIMIT 1)"
                ") AS display_name, "
                "pj.start_ts, pj.end_ts, pj.duration_seconds, pj.end_state "
                "FROM print_jobs pj WHERE pj.end_ts IS NOT NULL "
                "ORDER BY pj.start_ts DESC LIMIT 15"
            ).fetchall()
    else:
        summary = {"cnt": 0, "total_secs": 0}
        longest_row = month_rows = wd_rows = dur_rows = recent_rows = outcome_rows = []

    total_prints = summary["cnt"] if conn else 0
    total_secs   = summary["total_secs"] if conn else 0
    total_hours  = round(total_secs / 3600, 1)
    avg_hours    = round(total_hours / total_prints, 1) if total_prints else 0.0

    longest = {
        "duration_seconds": longest_row["duration_seconds"],
        "display_name":     longest_row["display_name"],
    } if longest_row else None

    # ── By month — last 13 calendar months, fill gaps ─────────────────────────────
    now = datetime.now()
    months = []
    for i in range(12, -1, -1):
        year  = now.year  + (now.month - 1 - i) // 12
        month = (now.month - 1 - i) % 12 + 1
        months.append((f"{year:04d}-{month:02d}", datetime(year, month, 1)))

    month_map = {r["month"]: r for r in month_rows} if conn else {}
    by_month = []
    for key, dt in months:
        r = month_map.get(key)
        by_month.append({
            "month": key,
            "label": dt.strftime("%b '%y"),
            "count": r["count"] if r else 0,
            "hours": round((r["total_secs"] if r else 0) / 3600, 1),
        })

    # ── By weekday — SQLite %w: 0=Sun; remap to Mon=0 ────────────────────────────
    # Mon=0…Sun=6  →  SQLite wd: Mon=2,Tue=3,Wed=4,Thu=5,Fri=6,Sat=7(→0),Sun=1(→0)
    # Remap: Mon=2→0, Tue=3→1, Wed=4→2, Thu=5→3, Fri=6→4, Sat=0→5, Sun=1→6
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd_map = {r["wd"]: r for r in wd_rows} if conn else {}
    # SQLite %w: Sun=0, Mon=1, Tue=2, Wed=3, Thu=4, Fri=5, Sat=6
    sqlite_to_mon0 = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 0: 6}
    wd_accum = [{"count": 0, "secs": 0} for _ in range(7)]
    for sqlite_wd, r in wd_map.items():
        idx = sqlite_to_mon0.get(sqlite_wd, 0)
        wd_accum[idx]["count"] += r["count"]
        wd_accum[idx]["secs"]  += r["total_secs"]
    by_weekday = [
        {"day": day_names[i], "count": wd_accum[i]["count"],
         "hours": round(wd_accum[i]["secs"] / 3600, 1)}
        for i in range(7)
    ]

    # ── Duration distribution ─────────────────────────────────────────────────────
    buckets = [("<1h", 0, 3600), ("1–2h", 3600, 7200), ("2–4h", 7200, 14400),
               ("4–8h", 14400, 28800), (">8h", 28800, None)]
    bucket_counts = {label: 0 for label, *_ in buckets}
    for r in (dur_rows or []):
        d = r["duration_seconds"] or 0
        for label, lo, hi in buckets:
            if d >= lo and (hi is None or d < hi):
                bucket_counts[label] += 1
                break
    by_duration = [{"label": label, "count": bucket_counts[label]} for label, *_ in buckets]

    # ── By outcome ────────────────────────────────────────────────────────────────
    outcome_order = ["FINISHED", "STOPPED", "ERROR", "UNKNOWN"]
    outcome_map = {r["state"]: r["count"] for r in (outcome_rows or [])}
    by_outcome = [
        {"state": s, "count": outcome_map.get(s, 0)}
        for s in outcome_order
        if outcome_map.get(s, 0) > 0
    ]

    # ── Recent prints ─────────────────────────────────────────────────────────────
    recent_prints = [
        {
            "id":               r["id"],
            "display_name":     r["display_name"],
            "start_time":       datetime.fromtimestamp(r["start_ts"]).isoformat(timespec="seconds"),
            "end_time":         datetime.fromtimestamp(r["end_ts"]).isoformat(timespec="seconds") if r["end_ts"] else None,
            "duration_seconds": r["duration_seconds"],
            "end_state":        r["end_state"],
        }
        for r in (recent_rows or [])
    ]

    return {
        "total_prints":       total_prints,
        "total_hours":        total_hours,
        "avg_duration_hours": avg_hours,
        "longest_print":      longest,
        "by_month":           by_month,
        "by_weekday":         by_weekday,
        "by_duration":        by_duration,
        "by_outcome":         by_outcome,
        "recent_prints":      recent_prints,
    }


@app.get("/api/stats/system")
def get_system_stats():
    try:
        conn = _open_db()
    except Exception:
        conn = None

    # System metrics — last 24h bucketed into 5-minute averages
    metrics = []
    if conn:
        cutoff = int(time.time()) - 86400
        with conn:
            rows = conn.execute(
                "SELECT (ts / 300) * 300 AS bucket_ts, "
                "AVG(cpu_temp) AS cpu_temp, "
                "AVG(cpu_usage) AS cpu_usage, "
                "AVG(CASE WHEN mem_total > 0 "
                "    THEN CAST(mem_used AS REAL) / mem_total * 100 ELSE NULL END) AS mem_pct "
                "FROM system_metrics WHERE ts >= ? "
                "GROUP BY bucket_ts ORDER BY bucket_ts",
                (cutoff,),
            ).fetchall()
            for r in rows:
                metrics.append({
                    "ts":        r["bucket_ts"],
                    "cpu_temp":  round(r["cpu_temp"], 1) if r["cpu_temp"] is not None else None,
                    "cpu_usage": round(r["cpu_usage"], 1) if r["cpu_usage"] is not None else None,
                    "mem_pct":   round(r["mem_pct"], 1) if r["mem_pct"] is not None else None,
                })

    # Events — derive from print_jobs + youtube_uploads
    events: list[dict] = []
    if conn:
        with conn:
            job_rows = conn.execute(
                "SELECT COALESCE(pj.display_name, "
                "  (SELECT pt.job_display_name FROM printer_telemetry pt "
                "   WHERE pt.ts BETWEEN pj.start_ts "
                "     AND COALESCE(pj.end_ts, pj.start_ts + 86400) "
                "   AND pt.job_display_name IS NOT NULL LIMIT 1)"
                ") AS display_name, "
                "pj.start_ts, pj.end_ts, pj.end_state, pj.duration_seconds "
                "FROM print_jobs pj ORDER BY pj.start_ts DESC LIMIT 100"
            ).fetchall()
            for r in job_rows:
                name = r["display_name"] or "(unknown)"
                if r["start_ts"]:
                    events.append({"ts": r["start_ts"], "type": "print_start", "label": f"Print started: {name}"})
                if r["end_ts"]:
                    state = r["end_state"] or "?"
                    dur = ""
                    if r["duration_seconds"]:
                        h, m = divmod(r["duration_seconds"] // 60, 60)
                        dur = f" ({h}h {m:02d}m)" if h else f" ({m}m)"
                    events.append({
                        "ts": r["end_ts"], "type": "print_end", "state": state,
                        "label": f"Print {state.lower()}: {name}{dur}",
                    })
            try:
                upload_rows = conn.execute(
                    "SELECT filename, status, uploaded_ts FROM youtube_uploads "
                    "WHERE status IN ('done', 'error') ORDER BY uploaded_ts DESC LIMIT 30"
                ).fetchall()
                for r in upload_rows:
                    fname = r["filename"] or "(unknown)"
                    etype = "upload_done" if r["status"] == "done" else "upload_error"
                    label = f"Upload done: {fname}" if r["status"] == "done" else f"Upload failed: {fname}"
                    events.append({"ts": r["uploaded_ts"], "type": etype, "label": label})
            except Exception:
                pass
            try:
                rec_rows = conn.execute(
                    "SELECT camera_safe_name, start_ts, end_ts FROM recordings "
                    "WHERE start_ts IS NOT NULL OR end_ts IS NOT NULL "
                    "ORDER BY COALESCE(end_ts, start_ts) DESC LIMIT 100"
                ).fetchall()
                for r in rec_rows:
                    cam = (r["camera_safe_name"] or "camera").replace("_", " ")
                    if r["start_ts"]:
                        events.append({"ts": r["start_ts"], "type": "recording_start",
                                        "label": f"Recording started: {cam}"})
                    if r["end_ts"]:
                        dur = ""
                        if r["start_ts"] and r["end_ts"] > r["start_ts"]:
                            secs = r["end_ts"] - r["start_ts"]
                            h, m = divmod(secs // 60, 60)
                            dur = f" ({h}h {m:02d}m)" if h else f" ({m}m)"
                        events.append({"ts": r["end_ts"], "type": "recording_stop",
                                        "label": f"Recording stopped: {cam}{dur}"})
            except Exception:
                pass

    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:50]
    for e in events:
        try:
            e["time"] = datetime.fromtimestamp(e["ts"]).isoformat(timespec="seconds")
        except Exception:
            e["time"] = None

    return {"metrics": metrics, "events": events}


def _pl_config() -> tuple[str, str]:
    cfg = load_config()
    pl = cfg.get("prusalink", {})
    if not pl.get("host") or not pl.get("api_key"):
        raise HTTPException(503, "PrusaLink not configured")
    return pl["host"].rstrip("/"), pl["api_key"]


@app.get("/api/printer/thumbnail")
def get_printer_thumbnail():
    host, api_key = _pl_config()
    try:
        job_resp = requests.get(f"{host}/api/v1/job",
                                headers={"X-Api-Key": api_key}, timeout=10)
        job_resp.raise_for_status()
        refs = (job_resp.json().get("file") or {}).get("refs") or {}
        path = refs.get("thumbnail") or refs.get("icon")
        if not path:
            raise HTTPException(404, "No thumbnail available")
        img = requests.get(f"{host}{path}", headers={"X-Api-Key": api_key}, timeout=10)
        img.raise_for_status()
        return Response(content=img.content,
                        media_type=img.headers.get("content-type", "image/png"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, str(exc))


_ICON_CACHE_DIR = Path(os.environ.get("ICON_CACHE_DIR", "/var/lib/prusa-cameras/icon-cache"))
_icon_semaphore = threading.Semaphore(2)  # max 2 concurrent printer icon fetches


def _icon_cache_path(key: str) -> Path:
    import hashlib
    return _ICON_CACHE_DIR / hashlib.sha256(key.encode()).hexdigest()


# ── bgcode / gcode thumbnail extractor ───────────────────────────────────────

def _extract_thumbnail(raw: bytes) -> tuple[bytes, str] | None:
    """
    Extract the best available thumbnail from bgcode or plain-gcode bytes.
    Returns (image_bytes, mime_type) or None.

    bgcode embeds thumbnails as blocks (type 4).  Supported image formats:
      0 = PNG  (preferred — browser-native)
      1 = JPEG (browser-native)
      2 = QOI  (converted to PNG via Pillow if available, else skipped)

    Plain gcode embeds thumbnails as base64-encoded PNG in '; thumbnail' comments.
    """
    if not raw:
        return None
    if raw[:4] == b'GCDE':
        return _extract_bgcode(raw)
    return _extract_gcode_comments(raw)


def _extract_bgcode(raw: bytes) -> tuple[bytes, str] | None:
    import struct, zlib

    if len(raw) < 8:
        return None

    # File header: GCDE(4) + version(2) + checksum_type(2)
    checksum_type = struct.unpack_from('<H', raw, 6)[0]
    checksum_size = 4 if checksum_type in (1, 2) else 0

    THUMBNAIL_BLOCK = 4
    IMG_PNG, IMG_JPG, IMG_QOI = 0, 1, 2

    pos = 8
    candidates: list[tuple[int, bytes, int]] = []  # (pixels, data, fmt)

    while pos + 8 <= len(raw):
        try:
            block_type  = struct.unpack_from('<H', raw, pos)[0]
            compression = struct.unpack_from('<H', raw, pos + 2)[0]
            uncomp_size = struct.unpack_from('<I', raw, pos + 4)[0]
            pos += 8

            if compression != 0:
                if pos + 4 > len(raw):
                    break
                comp_size = struct.unpack_from('<I', raw, pos)[0]
                pos += 4
                block_raw = raw[pos:pos + comp_size]
                pos += comp_size
            else:
                if pos + uncomp_size > len(raw):
                    break
                block_raw = raw[pos:pos + uncomp_size]
                pos += uncomp_size

            pos += checksum_size

            if block_type == THUMBNAIL_BLOCK:
                if compression == 1:  # deflate
                    try:
                        block_raw = zlib.decompress(block_raw)
                    except zlib.error:
                        continue
                elif compression != 0:
                    continue  # heatshrink — skip

                if len(block_raw) < 7:
                    continue

                fmt    = struct.unpack_from('<H', block_raw, 0)[0]
                width  = struct.unpack_from('<H', block_raw, 2)[0]
                height = struct.unpack_from('<H', block_raw, 4)[0]
                img    = block_raw[6:]

                if img:
                    candidates.append((width * height, img, fmt))

        except struct.error:
            break

    if not candidates:
        return None

    # Prefer PNG, then JPEG, then QOI (largest by pixel count within each tier)
    for preferred_fmt in (IMG_PNG, IMG_JPG, IMG_QOI):
        tier = [(px, img, fmt) for px, img, fmt in candidates if fmt == preferred_fmt]
        if not tier:
            continue
        _, img, fmt = max(tier, key=lambda x: x[0])

        if fmt == IMG_PNG:
            return img, 'image/png'
        if fmt == IMG_JPG:
            return img, 'image/jpeg'
        if fmt == IMG_QOI:
            # Convert QOI → PNG using Pillow (10.0+ supports QOI)
            try:
                from PIL import Image
                import io
                pil_img = Image.open(io.BytesIO(img))
                buf = io.BytesIO()
                pil_img.save(buf, format='PNG')
                return buf.getvalue(), 'image/png'
            except Exception as exc:
                logger.debug("QOI→PNG conversion failed: %s", exc)
                continue

    return None


def _extract_gcode_comments(raw: bytes) -> tuple[bytes, str] | None:
    """Extract base64-encoded PNG thumbnail from PrusaSlicer gcode comment blocks."""
    import base64, re
    try:
        text = raw[:131072].decode('utf-8', errors='replace')
    except Exception:
        return None

    pattern = re.compile(
        r'; thumbnail begin \d+x\d+ \d+\r?\n(.*?); thumbnail end',
        re.DOTALL,
    )
    best: tuple[int, bytes] | None = None
    for m in pattern.finditer(text):
        b64 = ''.join(
            line.lstrip('; ').rstrip('\r\n')
            for line in m.group(1).splitlines()
        )
        try:
            data = base64.b64decode(b64)
            if best is None or len(data) > best[0]:
                best = (len(data), data)
        except Exception:
            continue

    return (best[1], 'image/png') if best else None


def _cache_and_serve_icon(cache_path: Path, content: bytes, mime: str) -> Response:
    meta_path = cache_path.with_suffix('.mime')
    try:
        _ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
        meta_path.write_text(mime)
    except OSError as exc:
        logger.warning("Could not write icon cache: %s", exc)
    return Response(
        content=content,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/api/printer/file-icon/{storage}/{path:path}")
def get_printer_file_icon(storage: str, path: str):
    """
    Return a thumbnail image for a file on the printer.

    Strategy:
      1. Disk cache (indefinite — icon is tied to file content).
      2. PrusaLink /thumb/ endpoint (fast, usually works).
      3. Download the first 512 KB of the file and parse the embedded
         bgcode thumbnail blocks ourselves (works even when PrusaLink's
         /thumb/ endpoint says 'File doesn't contain preview').
    """
    if storage not in ("usb", "local"):
        raise HTTPException(400, "Invalid storage")

    # Normalise: strip leading storage prefix if the JS included it (e.g. "usb/usb/file")
    if path.startswith(f"{storage}/"):
        path = path[len(storage) + 1:]

    cache_key  = f"{storage}/{path}"
    cache_path = _icon_cache_path(cache_key)
    meta_path  = cache_path.with_suffix(".mime")

    if cache_path.exists() and meta_path.exists():
        return Response(
            content=cache_path.read_bytes(),
            media_type=meta_path.read_text().strip(),
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    if not _icon_semaphore.acquire(timeout=30):
        raise HTTPException(503, "Too many icon requests in progress — try again shortly")

    try:
        host, api_key = _pl_config()
        auth_header = {"X-Api-Key": api_key}

        # ── 1. Try PrusaLink /thumb/ endpoints ───────────────────────────────────
        # /thumb/l/ (large) works where /thumb/s/ (small) may not.
        for thumb_size in ("l", "s"):
            try:
                r = requests.get(f"{host}/thumb/{thumb_size}/{storage}/{path}", headers=auth_header, timeout=10)
                if r.ok and len(r.content) > 64:
                    mime = r.headers.get("content-type", "image/png").split(";")[0].strip()
                    return _cache_and_serve_icon(cache_path, r.content, mime)
                logger.debug("PrusaLink /thumb/%s/ returned %d for %s/%s", thumb_size, r.status_code, storage, path)
            except Exception as exc:
                logger.debug("PrusaLink /thumb/%s/ failed: %s", thumb_size, exc)

        # ── 2. Download beginning of file and extract thumbnail ourselves ─────────
        download_candidates = [
            f"{host}/{storage}/{path}",               # WebDAV download path
            f"{host}/api/v1/files/{storage}/{path}",  # API v1 (may return JSON for some firmware)
        ]
        raw = None
        for download_url in download_candidates:
            for extra_headers in [{"Range": "bytes=0-524287"}, {}]:
                try:
                    r = requests.get(
                        download_url,
                        headers={**auth_header, "Accept": "application/octet-stream", **extra_headers},
                        timeout=20,
                        stream=True,
                    )
                    if r.status_code not in (200, 206):
                        logger.debug("Download %s → HTTP %d", download_url, r.status_code)
                        continue
                    chunks, total = [], 0
                    for chunk in r.iter_content(32768):
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= 524288:
                            break
                    raw = b"".join(chunks)
                    logger.debug("Downloaded %d bytes from %s", len(raw), download_url)
                    break  # got data
                except Exception as exc:
                    logger.debug("Download %s failed: %s", download_url, exc)
            if raw is not None:
                break
    finally:
        _icon_semaphore.release()

    if not raw:
        raise HTTPException(503, "Could not download file from printer for thumbnail extraction")

    result = _extract_thumbnail(raw)
    if not result:
        raise HTTPException(404, "No thumbnail found in file")

    img_data, mime = result
    logger.info("Extracted %s thumbnail (%d bytes) from %s/%s", mime, len(img_data), storage, path)
    return _cache_and_serve_icon(cache_path, img_data, mime)


@app.post("/api/printer/control/pause")
def printer_pause():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "pause", "action": "pause"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/control/resume")
def printer_resume():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "pause", "action": "resume"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/control/stop")
def printer_stop():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "cancel"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/upload")
def printer_upload(
    file: UploadFile = File(...),
    storage: str = Form("usb"),
    print_after_upload: str = Form("false"),
):
    from urllib.parse import quote as urlquote
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    fname = file.filename or ""
    if not fname.lower().endswith((".gcode", ".bgcode")):
        raise HTTPException(400, "Only .gcode and .bgcode files are supported")
    host, api_key = _pl_config()
    do_print = print_after_upload.lower() in ("true", "1", "yes")
    data = file.file.read()
    size_mb = len(data) / 1_048_576
    logger.info("Uploading %s to printer (%s, %.1f MB, print_after=%s)", fname, storage, size_mb, do_print)
    headers: dict[str, str] = {
        "X-Api-Key": api_key,
        "Content-Type": "application/octet-stream",
        "Overwrite": "?1",
    }
    if do_print:
        headers["Print-After-Upload"] = "?1"
    try:
        r = requests.put(
            f"{host}/api/v1/files/{storage}/{urlquote(fname)}",
            headers=headers,
            data=data,
            timeout=600,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        logger.error("Printer upload failed for %s: HTTP %s — %s", fname, exc.response.status_code, exc.response.text[:200])
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        logger.error("Printer upload failed for %s: %s", fname, exc)
        raise HTTPException(503, str(exc))
    logger.info("Upload complete: %s (%.1f MB)", fname, size_mb)
    return {"ok": True, "filename": fname}


def _flatten_files(node, storage: str, prefix: str = "") -> list[dict]:
    """Recursively flatten a PrusaLink v1 file-tree node into a flat list of print files."""
    results: list[dict] = []
    if isinstance(node, list):
        for item in node:
            results.extend(_flatten_files(item, storage, prefix))
    elif isinstance(node, dict):
        name  = node.get("name", "")
        ftype = (node.get("type") or "").upper()
        path  = f"{prefix}/{name}".lstrip("/") if prefix else name

        is_print = ftype == "PRINT_FILE" or (
            ftype not in ("FOLDER",) and name.lower().endswith((".gcode", ".bgcode"))
        )
        if is_print and name:
            refs = node.get("refs") or {}
            results.append({
                "name":         name,
                "display_name": node.get("display_name") or name,
                "size":         node.get("size") or node.get("bytes") or 0,
                "timestamp":    node.get("m_timestamp") or node.get("date") or 0,
                "storage":      storage,
                "path":         path,
                "icon_ref":     refs.get("icon") or refs.get("thumbnail") or f"/thumb/s/{storage}/{path}",
            })
        for child in node.get("children") or []:
            results.extend(_flatten_files(child, storage, path if name and name != "/" else prefix))
    return results


def _files_from_db(storage: str) -> list[dict]:
    """Return cached printer files from DB, enriched with print job stats and icon_cached flag."""
    with _open_db() as conn:
        rows = conn.execute(
            """SELECT pf.*,
               (SELECT COUNT(*) FROM print_jobs pj WHERE pj.display_name = pf.display_name) AS print_count,
               (SELECT MAX(start_ts) FROM print_jobs pj WHERE pj.display_name = pf.display_name) AS last_print_ts,
               (SELECT end_state FROM print_jobs pj WHERE pj.display_name = pf.display_name
                ORDER BY start_ts DESC LIMIT 1) AS last_print_state
               FROM printer_files pf WHERE pf.storage = ?
               ORDER BY pf.file_timestamp DESC""",
            (storage,),
        ).fetchall()
    result = []
    for r in rows:
        result.append({
            "name":             r["name"],
            "display_name":     r["display_name"] or r["name"],
            "size":             r["size"],
            "timestamp":        r["file_timestamp"],
            "storage":          r["storage"],
            "path":             r["path"],
            "print_count":      r["print_count"] or 0,
            "last_print_ts":    r["last_print_ts"],
            "last_print_state": r["last_print_state"],
        })
    return result


@app.get("/api/printer/files/{storage}")
def list_printer_files(storage: str, refresh: bool = False):
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")

    # Always serve from DB instantly unless explicitly refreshing or DB is empty
    if not refresh:
        try:
            cached = _files_from_db(storage)
            if cached:
                return cached
        except Exception:
            pass

    # Fetch from printer: either refresh=True or DB was empty (first run)
    host, api_key = _pl_config()
    try:
        r = requests.get(
            f"{host}/api/v1/files/{storage}",
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        fresh = _flatten_files(r.json(), storage)
    except requests.HTTPError as exc:
        try:
            return _files_from_db(storage)
        except Exception:
            pass
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        try:
            return _files_from_db(storage)
        except Exception:
            pass
        raise HTTPException(503, str(exc))

    now = int(time.time())
    try:
        with _open_db_rw() as conn:
            for f in fresh:
                conn.execute(
                    """INSERT INTO printer_files
                         (storage, path, name, display_name, size, file_timestamp, last_seen_ts)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(storage, path) DO UPDATE SET
                         name=excluded.name, display_name=excluded.display_name,
                         size=excluded.size, file_timestamp=excluded.file_timestamp,
                         last_seen_ts=excluded.last_seen_ts""",
                    (f["storage"], f["path"], f["name"], f["display_name"],
                     f["size"], f["timestamp"], now),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not cache printer files in DB: %s", exc)

    try:
        return _files_from_db(storage)
    except Exception:
        return fresh


@app.get("/api/printer/files-raw/{storage}")
def list_printer_files_raw(storage: str):
    """Debug: return the raw JSON from PrusaLink's files API, unmodified."""
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    host, api_key = _pl_config()
    try:
        r = requests.get(
            f"{host}/api/v1/files/{storage}",
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/printer/files/{storage}/{path:path}/print")
def print_file(storage: str, path: str):
    from urllib.parse import quote as urlquote
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/files/{storage}/{urlquote(path)}",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "select", "print": True},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.get("/api/youtube")
def get_youtube():
    return load_config().get("youtube", {})


@app.put("/api/youtube")
def update_youtube(body: YouTubeBody):
    cfg = load_config()
    cfg["youtube"] = body.model_dump()
    save_config(cfg)
    return cfg["youtube"]


@app.get("/api/recording-config")
def get_recording_config():
    return load_config().get("recording", {})


@app.put("/api/recording-config")
def update_recording_config(body: RecordingBody):
    cfg = load_config()
    cfg["recording"] = body.model_dump()
    save_config(cfg)
    return cfg["recording"]


# ── YouTube OAuth flow ────────────────────────────────────────────────────────
# Uses the same copy-paste redirect approach as OctoStreamControl:
# 1. Generate auth URL with redirect_uri=http://localhost:8181 (nothing listens there)
# 2. User opens the URL, authorizes with Google
# 3. Browser is redirected to localhost:8181/?code=...&state=... which fails to load
# 4. User copies that URL from the address bar and pastes it back here
# 5. We parse the code+state and exchange for credentials
#
# This avoids private-IP redirect restrictions and SSH tunnel requirements.
# Requires "Desktop app" OAuth client type in Google Cloud Console.

_YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# Google's loopback redirect — nothing needs to listen here
_LOOPBACK_REDIRECT = "http://localhost:8181"


@app.get("/api/youtube/auth/status")
def youtube_auth_status():
    import pickle
    cfg = load_config()
    creds_file = cfg.get("youtube", {}).get("credentials_cache", "")
    if not creds_file or not Path(creds_file).exists():
        return {"authorized": False}
    try:
        with open(creds_file, "rb") as f:
            creds = pickle.load(f)
        if creds.valid:
            return {"authorized": True}
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with open(creds_file, "wb") as f:
                pickle.dump(creds, f)
            return {"authorized": True}
    except Exception:
        pass
    return {"authorized": False}


@app.post("/api/youtube/auth/start")
def youtube_auth_start():
    """Generate and return the Google authorization URL."""
    import base64, hashlib, json as _json, secrets as _secrets, tempfile as _tmp
    from urllib.parse import urlencode

    cfg = load_config()
    secrets_file = cfg.get("youtube", {}).get("client_secrets_file", "")
    if not secrets_file or not Path(secrets_file).exists():
        raise HTTPException(
            400,
            f"client_secrets.json not found at '{secrets_file}'. "
            "Set the path in Settings → YouTube and save first.",
        )

    with open(secrets_file) as f:
        client_json = _json.load(f)
    client = client_json.get("web") or client_json.get("installed")
    if not client:
        raise HTTPException(400, "Unrecognised client_secrets.json format")

    # Generate PKCE pair ourselves — bypassing google_auth_oauthlib entirely so
    # we know the exact verifier that corresponds to the challenge in the URL.
    code_verifier  = _secrets.token_urlsafe(96)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(32)

    auth_url = client["auth_uri"] + "?" + urlencode({
        "client_id":             client["client_id"],
        "redirect_uri":          _LOOPBACK_REDIRECT,
        "response_type":         "code",
        "scope":                 " ".join(_YOUTUBE_SCOPES),
        "access_type":           "offline",
        "prompt":                "consent",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    })

    state_file = Path(_tmp.gettempdir()) / f"prusa_yt_flow_{state}.json"
    state_file.write_text(_json.dumps({
        "state":         state,
        "secrets_file":  secrets_file,
        "redirect_uri":  _LOOPBACK_REDIRECT,
        "code_verifier": code_verifier,
    }))

    return {"auth_url": auth_url, "state": state}


class CompleteAuthBody(BaseModel):
    redirect_url: str


@app.post("/api/youtube/auth/complete")
def youtube_auth_complete(body: CompleteAuthBody):
    """Exchange the code in the pasted redirect URL for credentials."""
    import json as _json, os as _os, tempfile as _tmp
    from urllib.parse import urlparse, parse_qs

    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    # Parse code + state out of the pasted URL
    try:
        params = parse_qs(urlparse(body.redirect_url).query)
        code  = params.get("code",  [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
    except Exception as exc:
        raise HTTPException(400, f"Could not parse URL: {exc}")

    if error:
        raise HTTPException(400, f"Authorization denied: {error}")
    if not code or not state:
        raise HTTPException(400, "URL is missing code or state — did you copy the full address bar URL?")

    # Load the persisted flow state
    state_file = Path(_tmp.gettempdir()) / f"prusa_yt_flow_{state}.json"
    if not state_file.exists():
        raise HTTPException(400, "Authorization session not found or expired — please start over.")

    flow_data = _json.loads(state_file.read_text())
    state_file.unlink(missing_ok=True)

    # Read client config directly from the secrets file
    with open(flow_data["secrets_file"]) as f:
        client_json = _json.load(f)
    client = client_json.get("installed") or client_json.get("web")
    if not client:
        raise HTTPException(400, "Unrecognised client_secrets.json format")

    # Make the token exchange directly so code_verifier is guaranteed in the POST
    # body — google_auth_oauthlib silently drops it when the flow is reconstructed.
    import requests as _req
    resp = _req.post(
        client["token_uri"],
        data={
            "code":          code,
            "client_id":     client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri":  _LOOPBACK_REDIRECT,
            "grant_type":    "authorization_code",
            "code_verifier": flow_data["code_verifier"],
        },
    )
    # Always parse the body so we can surface Google's actual error message
    try:
        token_data = resp.json()
    except Exception:
        raise HTTPException(400, f"Google returned HTTP {resp.status_code}: {resp.text[:300]}")

    if not resp.ok or "error" in token_data:
        err  = token_data.get("error", f"HTTP {resp.status_code}")
        desc = token_data.get("error_description", "")
        msg  = f"{err}: {desc}"
        import logging; logging.getLogger("youtube_auth").error("Token exchange failed — %s | full response: %s", msg, token_data)
        raise HTTPException(400, msg)

    # Build a Credentials object from the raw token response
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=client["token_uri"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=_YOUTUBE_SCOPES,
    )

    cfg = load_config()
    creds_file = cfg.get("youtube", {}).get(
        "credentials_cache", "/var/lib/prusa-cameras/youtube_creds.json"
    )
    try:
        import pickle
        dest = Path(creds_file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            pickle.dump(creds, f)
    except Exception as exc:
        raise HTTPException(500, f"Failed to save credentials: {exc}")

    return {"ok": True}


# ── Recording status ───────────────────────────────────────────────────────────

_STATUS_FILE = Path("/tmp/prusa-cameras-status.json")


def _live_sessions() -> list[dict]:
    """Return only recording sessions whose ffmpeg process is still running."""
    try:
        data = json.loads(_STATUS_FILE.read_text())
    except Exception:
        return []
    live = []
    for s in data.get("recording", []):
        if not isinstance(s, dict):
            continue
        pid = s.get("pid")
        if pid is None:
            live.append(s)
            continue
        try:
            os.kill(pid, 0)
            live.append(s)
        except ProcessLookupError:
            logger.debug("Stale recording session for '%s' (pid %d gone)", s.get("name"), pid)
        except PermissionError:
            live.append(s)  # process exists but owned by a different user
    return live


@app.get("/api/recording-status")
def recording_status():
    return {"recording": _live_sessions()}


@app.post("/api/recording-status/start/{camera_name}")
def start_recording(camera_name: str):
    import time as _time

    cfg = load_config()
    cam = _find_cam(cfg, camera_name)
    if not cam:
        raise HTTPException(404, f"Camera '{camera_name}' not found")

    # Use PID-checked live sessions so a dead stale entry doesn't block restarts
    if any(s.get("name") == camera_name for s in _live_sessions()):
        raise HTTPException(409, f"Camera '{camera_name}' is already recording")

    rec_cfg = cfg.get("recording", {})
    output_dir = Path(rec_cfg.get("output_dir", "/var/lib/prusa-cameras/recordings"))
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    safe = camera_name.replace(" ", "_").lower()
    out = output_dir / f"{safe}_manual_{timestamp}.mp4"

    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", cam["rtsp_url"],
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "25",
        "-g", "30",
        "-bf", "0",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        "-movflags", "+frag_keyframe+empty_moov+faststart",
        str(out),
    ]

    import shutil
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd[0] = ffmpeg_bin
    logger.info("[%s] ffmpeg binary: %s", camera_name, ffmpeg_bin)
    logger.info("[%s] command: %s", camera_name, " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait briefly so we can catch an immediate failure (bad URL, codec error, etc.)
    start_ts = int(_time.time())
    _time.sleep(1.5)
    rc = proc.poll()
    if rc is not None:
        stderr = proc.stderr.read().decode(errors="replace").strip()
        logger.error("[%s] ffmpeg exited immediately (rc=%d): %s", camera_name, rc, stderr)
        raise HTTPException(500, f"Recording failed to start (rc={rc}): {stderr[-300:] or 'unknown error'}")

    # Process is alive — drain stderr in background; update file size in DB once ffmpeg exits
    def _finalize(p: subprocess.Popen, name: str, path: str) -> None:
        for line in p.stderr:
            logger.warning("[%s] ffmpeg: %s", name, line.decode(errors="replace").rstrip())
        try:
            size = Path(path).stat().st_size if Path(path).exists() else None
            with _open_db_rw() as conn:
                conn.execute(
                    "UPDATE recordings SET file_size_bytes=? WHERE file_path=?",
                    (size, path),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[%s] Could not update recording file size: %s", name, exc)

    threading.Thread(target=_finalize, args=(proc, camera_name, str(out)), daemon=True).start()

    # Write only live sessions + the new one (avoids re-adding any stale entries)
    sessions = [s for s in _live_sessions() if s.get("name") != camera_name]
    sessions.append({"name": camera_name, "pid": proc.pid, "path": str(out)})
    try:
        _STATUS_FILE.write_text(json.dumps({"recording": sessions}))
    except OSError:
        pass

    job_id = None
    try:
        with _open_db() as conn:
            row = conn.execute(
                "SELECT id FROM print_jobs WHERE end_ts IS NULL ORDER BY start_ts DESC LIMIT 1"
            ).fetchone()
            if row:
                job_id = row["id"]
    except Exception:
        pass

    try:
        with _open_db_rw() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO recordings "
                "(id, job_id, camera_safe_name, file_path, start_ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), job_id, safe, str(out), start_ts),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[%s] Could not insert recording entry: %s", camera_name, exc)

    logger.info("[%s] Manual recording started → %s (pid %d)", camera_name, out, proc.pid)
    return {"ok": True, "path": str(out)}


@app.post("/api/recording-status/stop/{camera_name}")
def stop_recording(camera_name: str):
    try:
        data = json.loads(_STATUS_FILE.read_text())
    except Exception:
        raise HTTPException(404, "No active recordings found")
    sessions = data.get("recording", [])
    session = next((s for s in sessions if isinstance(s, dict) and s.get("name") == camera_name), None)
    if not session:
        raise HTTPException(404, f"No active recording for '{camera_name}'")
    pid = session.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    data["recording"] = [s for s in sessions if not (isinstance(s, dict) and s.get("name") == camera_name)]
    try:
        _STATUS_FILE.write_text(json.dumps(data))
    except OSError:
        pass
    out_path = session.get("path")
    if out_path:
        try:
            with _open_db_rw() as conn:
                conn.execute(
                    "UPDATE recordings SET end_ts=? WHERE file_path=?",
                    (int(time.time()), out_path),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("[%s] Could not update recording end_ts: %s", camera_name, exc)
    return {"ok": True}


# ── Service status / control ───────────────────────────────────────────────────

@app.get("/api/service/status")
def service_status():
    r = subprocess.run(
        ["systemctl", "is-active", "prusa-cameras"],
        capture_output=True, text=True,
    )
    state = r.stdout.strip()
    return {"active": state == "active", "state": state}


@app.post("/api/service/restart")
def restart_service():
    r = subprocess.run(
        ["sudo", "systemctl", "restart", "prusa-cameras"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise HTTPException(500, r.stderr.strip() or "systemctl restart failed")
    return {"ok": True}


# ── Stream proxy ───────────────────────────────────────────────────────────────

@app.get("/api/stream/{camera_name}/snapshot")
async def get_snapshot(camera_name: str):
    cfg = load_config()
    cam = _find_cam(cfg, camera_name)
    if not cam:
        raise HTTPException(404, "Camera not found")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "quiet",
        "-rtsp_transport", "tcp",
        "-i", cam["rtsp_url"],
        "-vframes", "1", "-q:v", "5", "-f", "image2", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        jpeg, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "Stream timeout")
    if not jpeg:
        raise HTTPException(503, "Stream unavailable")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


# ── Recordings ─────────────────────────────────────────────────────────────────

@app.get("/api/recordings")
def list_recordings():
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))

    # Build map of filename → session for any active recordings
    live_by_name: dict[str, dict] = {
        Path(s["path"]).name: s
        for s in _live_sessions()
        if "path" in s
    }

    # Look up print job names from DB (filename → display_name)
    print_name_by_file: dict[str, str] = {}
    try:
        with _open_db() as conn:
            rows = conn.execute(
                "SELECT r.file_path, "
                "COALESCE(pj.display_name, "
                "  (SELECT pt.job_display_name FROM printer_telemetry pt "
                "   WHERE pt.ts BETWEEN pj.start_ts "
                "     AND COALESCE(pj.end_ts, pj.start_ts + 86400) "
                "   AND pt.job_display_name IS NOT NULL LIMIT 1)"
                ") AS display_name "
                "FROM recordings r "
                "JOIN print_jobs pj ON pj.id = r.job_id"
            ).fetchall()
            for row in rows:
                fname = Path(row["file_path"]).name
                if row["display_name"]:
                    print_name_by_file[fname] = row["display_name"]
    except Exception:
        pass

    results = []
    if rec_dir.exists():
        for f in sorted(rec_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            session = live_by_name.pop(f.name, None)
            results.append({
                "name": f.name,
                "display_name": print_name_by_file.get(f.name),
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "live": session is not None,
                "deleted": False,
                "camera_name": session["name"] if session else None,
            })

    # Sessions whose files haven't appeared on disk yet
    for fname, session in live_by_name.items():
        results.insert(0, {
            "name": fname,
            "display_name": None,
            "size": 0,
            "mtime": 0,
            "live": True,
            "deleted": False,
            "camera_name": session["name"],
        })

    # Deleted recordings kept in DB
    try:
        with _open_db() as conn:
            deleted_rows = conn.execute(
                "SELECT r.file_path, r.file_size_bytes, r.end_ts, "
                "COALESCE(pj.display_name, "
                "  (SELECT pt.job_display_name FROM printer_telemetry pt "
                "   WHERE pt.ts BETWEEN pj.start_ts "
                "     AND COALESCE(pj.end_ts, pj.start_ts + 86400) "
                "   AND pt.job_display_name IS NOT NULL LIMIT 1)"
                ") AS display_name "
                "FROM recordings r "
                "LEFT JOIN print_jobs pj ON pj.id = r.job_id "
                "WHERE r.file_deleted = 1 "
                "ORDER BY r.end_ts DESC"
            ).fetchall()
            for row in deleted_rows:
                fname = Path(row["file_path"]).name
                results.append({
                    "name": fname,
                    "display_name": row["display_name"],
                    "size": row["file_size_bytes"] or 0,
                    "mtime": row["end_ts"] or 0,
                    "live": False,
                    "deleted": True,
                    "camera_name": None,
                })
    except Exception:
        pass

    return results


@app.delete("/api/recordings/{filename}", status_code=204)
def delete_recording(filename: str):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(400, "Invalid filename")
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))
    path = rec_dir / filename
    if not path.exists():
        raise HTTPException(404)
    size = path.stat().st_size
    path.unlink()
    try:
        with _open_db_rw() as conn:
            cur = conn.execute(
                "UPDATE recordings SET file_deleted=1 WHERE file_path=?",
                (str(path),),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT OR IGNORE INTO recordings "
                    "(id, file_path, file_size_bytes, file_deleted) VALUES (?, ?, ?, 1)",
                    (str(uuid.uuid4()), str(path), size),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Could not mark recording as deleted: %s", exc)


@app.post("/api/recordings/{filename}/upload")
def start_upload(filename: str):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(400, "Invalid filename")
    try:
        conn = _open_db()
        if conn:
            row = conn.execute(
                "SELECT status FROM youtube_uploads WHERE filename = ?", (filename,)
            ).fetchone()
            if row and row["status"] == "uploading":
                raise HTTPException(409, "Upload already in progress")
    except HTTPException:
        raise
    except Exception:
        pass
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))
    video_path = rec_dir / filename
    if not video_path.exists():
        raise HTTPException(404, "Recording not found")
    _write_upload(filename, "pending")
    threading.Thread(target=_do_upload, args=(filename, str(video_path), cfg), daemon=True).start()
    return {"ok": True}


@app.get("/api/uploads/statuses")
def get_upload_statuses():
    try:
        conn = _open_db()
        if not conn:
            return {}
        rows = conn.execute(
            "SELECT filename, status, pct, url, error FROM youtube_uploads WHERE filename IS NOT NULL"
        ).fetchall()
        return {
            row["filename"]: {
                "status": row["status"],
                "pct": row["pct"],
                "url": row["url"],
                "error": row["error"],
            }
            for row in rows
        }
    except Exception:
        return {}


def _probe_video(path: str) -> dict | None:
    """Run ffprobe and return parsed JSON, or None if unavailable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
        logger.warning("ffprobe exited %d: %s", r.returncode, r.stderr.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("ffprobe failed: %s", exc)
    return None


def _remux_for_upload(src: str) -> str | None:
    """
    Re-mux src into a temp file with a clean moov atom at the front.
    Returns path to remuxed file, or None on failure.

    This is a speculative fix for YouTube "Processing Abandoned": when ffmpeg
    is stopped via stdin 'q', the moov atom should be written, but if the
    original stream had issues the container may still be malformed.
    Re-muxing validates and rebuilds the container headers.
    """
    dst = src.replace(".mp4", "_remux.mp4")
    logger.info("Re-muxing %s → %s", src, dst)
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-v", "warning",
                "-i", src,
                "-c", "copy",
                "-movflags", "+faststart",
                dst,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            logger.error("Re-mux failed (exit %d):\nstdout: %s\nstderr: %s", r.returncode, r.stdout.strip(), r.stderr.strip())
            return None
        if r.stderr.strip():
            logger.warning("ffmpeg re-mux warnings: %s", r.stderr.strip())
        dst_size = Path(dst).stat().st_size if Path(dst).exists() else 0
        logger.info("Re-mux complete: %s  (%.1f MB)", dst, dst_size / 1_048_576)
        return dst
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Re-mux exception: %s", exc)
        return None


def _do_upload(filename: str, video_path: str, cfg: dict) -> None:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = cfg.get("youtube", {})
    creds_file = yt.get("credentials_cache", "")
    remuxed_path = None
    try:
        # ── pre-upload diagnostics ──────────────────────────────────────
        src = Path(video_path)
        file_size = src.stat().st_size if src.exists() else 0
        logger.info(
            "YouTube upload requested: %s  exists=%s  size=%d bytes (%.1f MB)",
            video_path, src.exists(), file_size, file_size / 1_048_576,
        )

        probe = _probe_video(video_path)
        if probe:
            fmt = probe.get("format", {})
            duration = float(fmt.get("duration", 0))
            bit_rate = int(fmt.get("bit_rate", 0))
            logger.info(
                "ffprobe: format=%s  duration=%.1fs  bitrate=%d kbps  nb_streams=%s",
                fmt.get("format_name", "?"), duration, bit_rate // 1000,
                fmt.get("nb_streams", "?"),
            )
            for stream in probe.get("streams", []):
                ctype = stream.get("codec_type", "?")
                if ctype == "video":
                    logger.info(
                        "ffprobe video: codec=%s  %sx%s  fps=%s  profile=%s",
                        stream.get("codec_name", "?"),
                        stream.get("width", "?"), stream.get("height", "?"),
                        stream.get("r_frame_rate", "?"),
                        stream.get("profile", "?"),
                    )
                elif ctype == "audio":
                    logger.info(
                        "ffprobe audio: codec=%s  channels=%s  sample_rate=%s",
                        stream.get("codec_name", "?"),
                        stream.get("channels", "?"),
                        stream.get("sample_rate", "?"),
                    )
            if duration < 2.0:
                logger.warning("Duration %.1fs is very short — YouTube often rejects short files", duration)
        else:
            logger.warning("ffprobe unavailable — cannot validate file before upload")

        upload_path = video_path
        upload_size = file_size

        # ── credentials ────────────────────────────────────────────────
        if not creds_file or not Path(creds_file).exists():
            raise RuntimeError("YouTube credentials not found — authorize in Settings → YouTube")
        with open(creds_file, "rb") as f:
            creds = pickle.load(f)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired YouTube credentials")
                creds.refresh(GoogleRequest())
                with open(creds_file, "wb") as f:
                    pickle.dump(creds, f)
            else:
                raise RuntimeError("YouTube credentials expired — re-authorize in Settings → YouTube")

        svc = build("youtube", "v3", credentials=creds, cache_discovery=False)

        print_name = None
        try:
            with _open_db() as conn:
                row = conn.execute(
                    "SELECT COALESCE(pj.display_name, "
                    "  (SELECT pt.job_display_name FROM printer_telemetry pt "
                    "   WHERE pt.ts BETWEEN pj.start_ts "
                    "     AND COALESCE(pj.end_ts, pj.start_ts + 86400) "
                    "   AND pt.job_display_name IS NOT NULL LIMIT 1)"
                    ") AS display_name "
                    "FROM recordings r "
                    "JOIN print_jobs pj ON pj.id = r.job_id "
                    "WHERE r.file_path LIKE ?",
                    (f"%{filename}",),
                ).fetchone()
                if row:
                    print_name = row["display_name"]
        except Exception:
            pass

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if print_name:
            title = f"{print_name} — {timestamp}"
        else:
            title = f"3D Print — {timestamp}"

        body = {
            "snippet": {
                "title": title[:100],
                "description": "Recorded by prusa-connect-cameras",
                "tags": yt.get("keywords", []),
                "categoryId": str(yt.get("category_id", "28")),
            },
            "status": {
                "privacyStatus": yt.get("privacy", "unlisted"),
                "selfDeclaredMadeForKids": False,
            },
        }

        mime = "video/mp4" if upload_path.lower().endswith(".mp4") else "video/*"
        logger.info("Uploading %s as %s (%d bytes)", upload_path, mime, upload_size)
        media = MediaFileUpload(upload_path, mimetype=mime, chunksize=10 * 1024 * 1024, resumable=True)
        req = svc.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

        _write_upload(filename, "uploading", pct=0)
        response = None
        chunk_num = 0
        while response is None:
            status, response = req.next_chunk()
            chunk_num += 1
            if status:
                pct = int(status.progress() * 100)
                sent = int(status.progress() * upload_size)
                _write_upload_pct(filename, pct)
                logger.info("Upload chunk %d: %d%%  (%d / %d bytes)", chunk_num, pct, sent, upload_size)

        logger.info("YouTube API response: %s", json.dumps(response, indent=2))
        upload_status = response.get("status", {}).get("uploadStatus", "unknown")
        if upload_status != "uploaded":
            logger.error("YouTube upload status is '%s' — expected 'uploaded'", upload_status)

        video_id = response["id"]
        url = f"https://youtu.be/{video_id}"
        logger.info("Upload complete → %s  uploadStatus=%s", url, upload_status)

        playlist_id = yt.get("playlist_id", "")
        if playlist_id:
            try:
                svc.playlistItems().insert(
                    part="snippet",
                    body={"snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }},
                ).execute()
                logger.info("Added to playlist %s", playlist_id)
            except Exception as exc:
                logger.warning("Playlist insert failed: %s", exc)

        _write_upload(filename, "done", url=url, pct=100)
    except Exception as exc:
        logger.error("YouTube upload failed for %s: %s", filename, exc, exc_info=True)
        _write_upload(filename, "error", error=str(exc))
    finally:
        if remuxed_path and Path(remuxed_path).exists():
            try:
                Path(remuxed_path).unlink()
                logger.info("Cleaned up re-muxed temp file: %s", remuxed_path)
            except OSError:
                pass


# ── Live logs via WebSocket ───────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-fu", "prusa-cameras", "-u", "prusa-cameras-web", "--no-pager", "--output=cat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            # 60s timeout keeps the connection alive even during quiet periods
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            if not line:
                break
            await ws.send_text(line.decode(errors="replace").rstrip())
    except (WebSocketDisconnect, asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            proc.kill()


# ── Static files — must be mounted last ───────────────────────────────────────

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
