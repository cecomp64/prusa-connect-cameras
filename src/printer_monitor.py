"""Polls the local PrusaLink API, writes telemetry to DB, and fires callbacks on state transitions."""

import logging
import threading
import time
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Callable

import requests

if TYPE_CHECKING:
    from database import Database

logger = logging.getLogger(__name__)


class PrinterState(str, Enum):
    IDLE      = "IDLE"
    PRINTING  = "PRINTING"
    PAUSED    = "PAUSED"
    FINISHED  = "FINISHED"
    STOPPED   = "STOPPED"
    ERROR     = "ERROR"
    ATTENTION = "ATTENTION"
    UNKNOWN   = "UNKNOWN"


# States that mean "a print is in progress"
ACTIVE = {PrinterState.PRINTING, PrinterState.PAUSED}

# States that mean "a print has ended"
TERMINAL = {PrinterState.FINISHED, PrinterState.STOPPED, PrinterState.ERROR}


class PrinterMonitor:
    def __init__(
        self,
        cfg: dict,
        db: "Database",
        on_print_start: Callable[[PrinterState, str], None] | None = None,
        on_print_end: Callable[[PrinterState, str, str | None, int | None], None] | None = None,
    ):
        self._host: str = cfg["host"].rstrip("/")
        self._api_key: str = cfg["api_key"]
        self._interval: int = cfg.get("poll_interval", 10)
        self._db = db
        self.on_print_start = on_print_start
        self.on_print_end   = on_print_end

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state   = PrinterState.UNKNOWN
        self._print_active = False
        self._current_job_id: str | None = None
        self._current_job_name: str | None = None  # backfilled once PrusaLink reports it
        self._last_snapshot: dict | None = None  # last telemetry row inserted
        self._last_idle_write: float = 0.0       # timestamp of last non-active write
        self._last_job_time_printing: int | None = None  # printer's own active-time counter

    # ── Public interface ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="printer-monitor", daemon=True
        )
        self._thread.start()
        logger.info("Printer monitor started (polling %s every %ds)", self._host, self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    # ── Internal helpers ──────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self._fetch_snapshot()
                state    = PrinterState(snapshot["state"])
                self._db.update_printer_status(snapshot)

                if state in ACTIVE:
                    t = snapshot.get("job_time_printing")
                    if t is not None:
                        self._last_job_time_printing = t
                    # Backfill display_name as soon as PrusaLink reports it
                    name = snapshot.get("job_display_name")
                    if name and name != self._current_job_name and self._current_job_id:
                        self._current_job_name = name
                        self._db.update_print_job_name(self._current_job_id, name)
                    if self._snapshot_changed(snapshot):
                        self._db.insert_telemetry(snapshot)
                        self._last_snapshot = snapshot
                else:
                    # Write on state change OR as a heartbeat every 5 minutes so the
                    # web API always has a fresh row even when the printer is idle.
                    state_changed = snapshot["state"] != (self._last_snapshot or {}).get("state")
                    since_last = time.time() - self._last_idle_write
                    if state_changed or since_last >= 300:
                        self._db.insert_telemetry(snapshot)
                        self._last_snapshot = snapshot
                        self._last_idle_write = time.time()

                self._handle_transition(state, snapshot)

            except requests.exceptions.ConnectionError:
                logger.warning("PrusaLink unreachable at %s", self._host)
            except Exception as exc:
                logger.warning("Printer monitor error: %s", exc)

            self._stop.wait(self._interval)

    def _fetch_snapshot(self) -> dict:
        resp = requests.get(
            f"{self._host}/api/v1/status",
            headers={"X-Api-Key": self._api_key},
            timeout=10,
        )
        resp.raise_for_status()
        data    = resp.json()
        printer = data.get("printer") or {}
        job     = data.get("job") or {}

        raw = printer.get("state", "").upper()
        try:
            state = PrinterState(raw).value
        except ValueError:
            state = PrinterState.UNKNOWN.value

        # /api/v1/status never includes the filename. Fetch /api/v1/job once per
        # print job (when we don't have a name yet) to get file.display_name.
        display_name = self._current_job_name
        if job.get("id") and not display_name:
            try:
                job_resp = requests.get(
                    f"{self._host}/api/v1/job",
                    headers={"X-Api-Key": self._api_key},
                    timeout=10,
                )
                if job_resp.ok:
                    file_info = job_resp.json().get("file") or {}
                    display_name = file_info.get("display_name") or file_info.get("name")
            except Exception:
                pass

        return {
            "state":              state,
            "temp_nozzle":        printer.get("temp_nozzle"),
            "target_nozzle":      printer.get("target_nozzle"),
            "temp_bed":           printer.get("temp_bed"),
            "target_bed":         printer.get("target_bed"),
            "axis_z":             printer.get("axis_z"),
            "speed":              printer.get("speed"),
            "flow":               printer.get("flow"),
            "fan_hotend":         printer.get("fan_hotend"),
            "fan_print":          printer.get("fan_print"),
            "job_progress":       job.get("progress"),
            "job_time_remaining": job.get("time_remaining"),
            "job_time_printing":  job.get("time_printing"),
            "job_display_name":   display_name,
        }

    def _snapshot_changed(self, new: dict) -> bool:
        if self._last_snapshot is None:
            return True
        return any(
            new.get(k) != self._last_snapshot.get(k)
            for k in (
                "state", "temp_nozzle", "target_nozzle", "temp_bed", "target_bed",
                "axis_z", "speed", "flow", "fan_hotend", "fan_print",
                "job_progress", "job_time_remaining", "job_time_printing", "job_display_name",
            )
        )

    def _handle_transition(self, state: PrinterState, snapshot: dict) -> None:
        if state == self._last_state:
            return

        logger.info("Printer state: %s → %s", self._last_state.value, state.value)
        self._last_state = state

        if state in ACTIVE and not self._print_active:
            self._print_active = True
            job_id = str(uuid.uuid4())
            self._current_job_id = job_id
            name = snapshot.get("job_display_name")
            self._current_job_name = name
            self._db.begin_print_job(job_id, name)
            if self.on_print_start:
                self.on_print_start(state, job_id)

        elif self._print_active and state not in ACTIVE:
            # Fire on any exit from active — printers often skip FINISHED and go
            # straight to IDLE, so we can't gate this on TERMINAL states alone.
            self._print_active = False
            job_id = self._current_job_id
            job_name = self._current_job_name
            self._current_job_id = None
            self._current_job_name = None
            printer_duration = self._last_job_time_printing
            self._last_job_time_printing = None
            if self.on_print_end:
                self.on_print_end(state, job_id, job_name, printer_duration)
