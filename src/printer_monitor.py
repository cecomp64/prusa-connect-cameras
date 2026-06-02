"""Polls the local PrusaLink API and fires callbacks on print-state transitions."""

import logging
import threading
from enum import Enum
from typing import Callable

import requests

logger = logging.getLogger(__name__)


class PrinterState(str, Enum):
    IDLE = "IDLE"
    PRINTING = "PRINTING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    ATTENTION = "ATTENTION"
    UNKNOWN = "UNKNOWN"


# States that mean "a print is in progress"
ACTIVE = {PrinterState.PRINTING, PrinterState.PAUSED}

# States that mean "a print has ended"
TERMINAL = {PrinterState.FINISHED, PrinterState.STOPPED, PrinterState.ERROR}


class PrinterMonitor:
    def __init__(
        self,
        cfg: dict,
        on_print_start: Callable[[PrinterState], None] | None = None,
        on_print_end: Callable[[PrinterState], None] | None = None,
    ):
        self._host: str = cfg["host"].rstrip("/")
        self._api_key: str = cfg["api_key"]
        self._interval: int = cfg.get("poll_interval", 15)
        self.on_print_start = on_print_start
        self.on_print_end = on_print_end

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state = PrinterState.UNKNOWN
        self._print_active = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                state = self._fetch_state()
                self._handle_transition(state)
            except requests.exceptions.ConnectionError:
                logger.warning("PrusaLink unreachable at %s", self._host)
            except Exception as exc:
                logger.warning("Printer monitor error: %s", exc)
            self._stop.wait(self._interval)

    def _fetch_state(self) -> PrinterState:
        resp = requests.get(
            f"{self._host}/api/v1/status",
            headers={"X-Api-Key": self._api_key},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("printer", {}).get("state", "").upper()
        try:
            return PrinterState(raw)
        except ValueError:
            return PrinterState.UNKNOWN

    def _handle_transition(self, state: PrinterState) -> None:
        if state == self._last_state:
            return

        logger.info("Printer state: %s → %s", self._last_state.value, state.value)
        self._last_state = state

        if state in ACTIVE and not self._print_active:
            self._print_active = True
            if self.on_print_start:
                self.on_print_start(state)

        elif state in TERMINAL and self._print_active:
            self._print_active = False
            if self.on_print_end:
                self.on_print_end(state)
