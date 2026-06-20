"""SQLite event database — written by the main service, read by the web API."""

import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MAT_AFTER_LAYER = re.compile(r'_(\d+[\.,]\d+mm)[_-]([A-Z][A-Z0-9+\-]*)_', re.IGNORECASE)
_MAT_KNOWN = re.compile(r'\b(PLA\+?|PETG|ASA|ABS|TPU|PC|PA|NYLON|FLEX|PVA|HIPS|PP|CPE|PCTG)\b', re.IGNORECASE)


def _parse_material(display_name: str | None) -> str:
    if not display_name:
        return "Unknown"
    name = re.sub(r'\.(bgcode|gcode)$', '', display_name, flags=re.IGNORECASE)
    m = _MAT_AFTER_LAYER.search(name)
    if m:
        return m.group(2).upper()
    m2 = _MAT_KNOWN.search(name)
    return m2.group(1).upper() if m2 else "Unknown"


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS printer_telemetry (
    id                 INTEGER PRIMARY KEY,
    ts                 INTEGER NOT NULL,
    state              TEXT    NOT NULL,
    temp_nozzle        REAL,
    target_nozzle      REAL,
    temp_bed           REAL,
    target_bed         REAL,
    axis_z             REAL,
    speed              INTEGER,
    flow               INTEGER,
    fan_hotend         INTEGER,
    fan_print          INTEGER,
    job_progress       REAL,
    job_time_remaining INTEGER,
    job_time_printing  INTEGER,
    job_display_name   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pt_ts ON printer_telemetry(ts DESC);

CREATE TABLE IF NOT EXISTS printer_status (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    ts                 INTEGER NOT NULL,
    state              TEXT    NOT NULL,
    temp_nozzle        REAL,
    target_nozzle      REAL,
    temp_bed           REAL,
    target_bed         REAL,
    axis_z             REAL,
    speed              INTEGER,
    flow               INTEGER,
    fan_hotend         INTEGER,
    fan_print          INTEGER,
    job_progress       REAL,
    job_time_remaining INTEGER,
    job_time_printing  INTEGER,
    job_display_name   TEXT
);

CREATE TABLE IF NOT EXISTS print_jobs (
    id               TEXT    PRIMARY KEY,
    display_name     TEXT,
    material         TEXT,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER,
    duration_seconds INTEGER,
    end_state        TEXT
);

CREATE TABLE IF NOT EXISTS recordings (
    id               TEXT    PRIMARY KEY,
    job_id           TEXT    REFERENCES print_jobs(id) ON DELETE SET NULL,
    camera_safe_name TEXT,
    file_path        TEXT    NOT NULL UNIQUE,
    file_size_bytes  INTEGER,
    start_ts         INTEGER,
    end_ts           INTEGER
);

CREATE TABLE IF NOT EXISTS youtube_uploads (
    id           INTEGER PRIMARY KEY,
    filename     TEXT    UNIQUE,
    recording_id TEXT    REFERENCES recordings(id),
    video_id     TEXT,
    url          TEXT,
    status       TEXT    NOT NULL,
    pct          INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    uploaded_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS system_metrics (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    cpu_temp  REAL,
    cpu_usage REAL,
    mem_used  INTEGER,
    mem_total INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sm_ts ON system_metrics(ts DESC);
"""

_TELEMETRY_COLUMNS = (
    "state", "temp_nozzle", "target_nozzle", "temp_bed", "target_bed",
    "axis_z", "speed", "flow", "fan_hotend", "fan_print",
    "job_progress", "job_time_remaining", "job_time_printing", "job_display_name",
)

_REC_PATTERN = re.compile(r"_(print|manual)_(\d{8}_\d{6})\.mp4$")


class Database:
    def __init__(self, path: Path, telemetry_retention_days: int = 180) -> None:
        self.path = path
        self._retention_days = telemetry_retention_days
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()
        logger.info("Database opened at %s", path)

    def _migrate(self) -> None:
        """Apply incremental schema changes that CREATE TABLE IF NOT EXISTS can't handle."""
        for stmt in [
            # UNIQUE cannot be inline in ALTER TABLE ADD COLUMN in SQLite — use a separate index
            "ALTER TABLE youtube_uploads ADD COLUMN filename TEXT",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_yt_filename ON youtube_uploads(filename)",
            "ALTER TABLE youtube_uploads ADD COLUMN pct INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE recordings ADD COLUMN file_deleted INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE youtube_uploads ADD COLUMN title TEXT",
            "ALTER TABLE youtube_uploads ADD COLUMN file_path TEXT",
            "ALTER TABLE print_jobs ADD COLUMN notes TEXT",
            "ALTER TABLE print_jobs ADD COLUMN material TEXT",
        ]:
            try:
                self._conn.execute(stmt)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column/index already exists

        # Backfill material for any print_jobs that have a display_name but no material yet
        rows = self._conn.execute(
            "SELECT id, display_name FROM print_jobs WHERE material IS NULL AND display_name IS NOT NULL"
        ).fetchall()
        if rows:
            self._conn.executemany(
                "UPDATE print_jobs SET material = ? WHERE id = ?",
                [(_parse_material(r["display_name"]), r["id"]) for r in rows],
            )
            self._conn.commit()
            logger.info("Backfilled material for %d print jobs", len(rows))

    # ── Telemetry ─────────────────────────────────────────────────────────────────

    def insert_telemetry(self, snapshot: dict) -> None:
        row = tuple(snapshot.get(c) for c in _TELEMETRY_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO printer_telemetry (ts, {', '.join(_TELEMETRY_COLUMNS)}) "
                f"VALUES (?, {', '.join('?' * len(_TELEMETRY_COLUMNS))})",
                (int(time.time()), *row),
            )
            self._conn.commit()

    def update_printer_status(self, snapshot: dict) -> None:
        row = tuple(snapshot.get(c) for c in _TELEMETRY_COLUMNS)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO printer_status (id, ts, {', '.join(_TELEMETRY_COLUMNS)}) "
                f"VALUES (1, ?, {', '.join('?' * len(_TELEMETRY_COLUMNS))})",
                (int(time.time()), *row),
            )
            self._conn.commit()

    # ── Startup state restoration ─────────────────────────────────────────────────

    def get_last_printer_state(self) -> str | None:
        """Return the most recently recorded printer state string, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM printer_status WHERE id = 1"
            ).fetchone()
            if not row:
                row = self._conn.execute(
                    "SELECT state FROM printer_telemetry ORDER BY ts DESC LIMIT 1"
                ).fetchone()
        return row["state"] if row else None

    def get_open_print_job(self) -> dict | None:
        """Return the most recent print job with no end_ts, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, display_name FROM print_jobs WHERE end_ts IS NULL "
                "ORDER BY start_ts DESC LIMIT 1"
            ).fetchone()
        return {"id": row["id"], "display_name": row["display_name"]} if row else None

    # ── Print jobs ────────────────────────────────────────────────────────────────

    def begin_print_job(self, job_id: str, display_name: str | None) -> None:
        material = _parse_material(display_name)
        with self._lock:
            self._conn.execute(
                "INSERT INTO print_jobs (id, display_name, material, start_ts) VALUES (?, ?, ?, ?)",
                (job_id, display_name, material, int(time.time())),
            )
            self._conn.commit()
        logger.info("Print job started: %s (%s)", job_id, display_name)

    def update_print_job_name(self, job_id: str, display_name: str) -> None:
        """Backfill or correct display_name once PrusaLink reports it."""
        with self._lock:
            self._conn.execute(
                "UPDATE print_jobs SET display_name = ?, material = ? WHERE id = ?",
                (display_name, _parse_material(display_name), job_id),
            )
            self._conn.commit()

    def end_print_job(
        self,
        job_id: str,
        end_state: str,
        recording_paths: list[str],
        printer_duration_seconds: int | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT start_ts FROM print_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if printer_duration_seconds is not None and printer_duration_seconds > 0:
                duration = printer_duration_seconds
            else:
                start_ts = row["start_ts"] if row else now
                duration = now - start_ts

            self._conn.execute(
                "UPDATE print_jobs SET end_ts=?, duration_seconds=?, end_state=? WHERE id=?",
                (now, duration, end_state, job_id),
            )

            for path_str in recording_paths:
                self._insert_recording(job_id, path_str, now)

            self._conn.commit()
        logger.info("Print job ended: %s (%s, %ds)", job_id, end_state, duration)

    def _insert_recording(self, job_id: str, path_str: str, end_ts: int) -> None:
        p = Path(path_str)
        m = _REC_PATTERN.search(p.name)
        camera_safe = None
        start_ts = None
        if m:
            ts_str = m.group(2)
            try:
                start_ts = int(datetime.strptime(ts_str, "%Y%m%d_%H%M%S").timestamp())
                # camera_safe_name is everything before _{label}_
                camera_safe = p.stem[: p.stem.index(f"_{m.group(1)}_")]
            except (ValueError, IndexError):
                pass

        size = p.stat().st_size if p.exists() else None

        try:
            self._conn.execute(
                "INSERT INTO recordings "
                "(id, job_id, camera_safe_name, file_path, file_size_bytes, start_ts, end_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), job_id, camera_safe, path_str, size, start_ts, end_ts),
            )
        except sqlite3.IntegrityError:
            pass  # UNIQUE violation: already recorded

    # ── YouTube uploads ───────────────────────────────────────────────────────────

    def set_upload_state(
        self,
        filename: str,
        status: str,
        url: str | None = None,
        error: str | None = None,
        pct: int = 0,
        title: str | None = None,
        file_path: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO youtube_uploads (filename, status, pct, url, error, uploaded_ts, title, file_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(filename) DO UPDATE SET
                     status=excluded.status, pct=excluded.pct, url=excluded.url,
                     error=excluded.error, uploaded_ts=excluded.uploaded_ts,
                     title=COALESCE(excluded.title, title),
                     file_path=COALESCE(excluded.file_path, file_path)""",
                (filename, status, pct, url, error, int(time.time()), title, file_path),
            )
            self._conn.commit()

    def set_upload_pct(self, filename: str, pct: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE youtube_uploads SET pct=? WHERE filename=?",
                (pct, filename),
            )
            self._conn.commit()

    def get_pending_uploads(self) -> list[dict]:
        """Return uploads stuck in pending/uploading state from a previous interrupted run."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT filename, file_path, title FROM youtube_uploads "
                "WHERE status IN ('pending', 'uploading')"
            ).fetchall()
        return [dict(row) for row in rows]

    # ── System metrics ────────────────────────────────────────────────────────────

    def insert_system_metrics(
        self,
        cpu_temp: float | None,
        cpu_usage: float,
        mem_used: int,
        mem_total: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO system_metrics (ts, cpu_temp, cpu_usage, mem_used, mem_total) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), cpu_temp, cpu_usage, mem_used, mem_total),
            )
            self._conn.commit()

    # ── Maintenance ───────────────────────────────────────────────────────────────

    def purge_old_telemetry(self) -> int:
        cutoff = int(time.time()) - self._retention_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM printer_telemetry WHERE ts < ?", (cutoff,)
            )
            self._conn.execute(
                "DELETE FROM system_metrics WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
        n = cur.rowcount
        if n:
            logger.info("Purged %d old telemetry rows (older than %d days)", n, self._retention_days)
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()
