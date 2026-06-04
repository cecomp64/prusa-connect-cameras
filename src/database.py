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

CREATE TABLE IF NOT EXISTS print_jobs (
    id               TEXT    PRIMARY KEY,
    display_name     TEXT,
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
    recording_id TEXT    REFERENCES recordings(id),
    video_id     TEXT,
    url          TEXT,
    status       TEXT    NOT NULL,
    error        TEXT,
    uploaded_ts  INTEGER NOT NULL
);
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
        logger.info("Database opened at %s", path)

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

    # ── Print jobs ────────────────────────────────────────────────────────────────

    def begin_print_job(self, job_id: str, display_name: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO print_jobs (id, display_name, start_ts) VALUES (?, ?, ?)",
                (job_id, display_name, int(time.time())),
            )
            self._conn.commit()
        logger.info("Print job started: %s (%s)", job_id, display_name)

    def end_print_job(self, job_id: str, end_state: str, recording_paths: list[str]) -> None:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT start_ts FROM print_jobs WHERE id = ?", (job_id,)
            ).fetchone()
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

    # ── Maintenance ───────────────────────────────────────────────────────────────

    def purge_old_telemetry(self) -> int:
        cutoff = int(time.time()) - self._retention_days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM printer_telemetry WHERE ts < ?", (cutoff,)
            )
            self._conn.commit()
        n = cur.rowcount
        if n:
            logger.info("Purged %d old telemetry rows (older than %d days)", n, self._retention_days)
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()
