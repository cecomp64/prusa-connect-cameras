"""Persists a JSON log of print events for the analytics tab."""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class PrintLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._current: dict | None = None

    def on_print_start(self, display_name: str | None) -> None:
        with self._lock:
            self._current = {
                "id":           str(uuid.uuid4()),
                "start_time":   datetime.now().isoformat(timespec="seconds"),
                "display_name": display_name,
                "end_time":     None,
                "duration_seconds": None,
                "end_state":    None,
                "recordings":   [],
            }
        logger.debug("Print event started: %s", display_name)

    def on_print_end(self, end_state: str, recording_paths: list[str]) -> None:
        with self._lock:
            if self._current is None:
                logger.warning("on_print_end called with no active print")
                return
            now = datetime.now()
            start = datetime.fromisoformat(self._current["start_time"])
            self._current.update({
                "end_time":         now.isoformat(timespec="seconds"),
                "duration_seconds": int((now - start).total_seconds()),
                "end_state":        end_state,
                "recordings":       recording_paths,
            })
            self._append(dict(self._current))
            self._current = None

    def _append(self, event: dict) -> None:
        events = self._load()
        events.append(event)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(events, indent=2))
            logger.info("Print event logged: %s (%s)", event.get("display_name"), event.get("end_state"))
        except Exception as exc:
            logger.error("Failed to write print event log: %s", exc)

    def _load(self) -> list:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return []
