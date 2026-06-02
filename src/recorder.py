"""Manages per-camera FFmpeg recording sessions."""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATUS_FILE = Path("/tmp/prusa-cameras-status.json")


class Recorder:
    def __init__(self, cfg: dict):
        self._camera_cfgs: list[dict] = cfg["cameras"]
        self._output_dir = Path(cfg["recording"]["output_dir"])
        self._output_dir.mkdir(parents=True, exist_ok=True)
        # name → (proc, output_path)
        self._sessions: dict[str, tuple[subprocess.Popen, Path]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start_all(self, label: str = "print") -> None:
        for cam in self._camera_cfgs:
            self._start(cam, label)

    def stop_all(self) -> list[str]:
        """Stop every active recording and return paths of completed files."""
        with self._lock:
            names = list(self._sessions.keys())
        return [p for name in names if (p := self._stop(name))]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_status(self) -> None:
        with self._lock:
            sessions = [
                {"name": name, "pid": proc.pid, "path": str(out)}
                for name, (proc, out) in self._sessions.items()
            ]
        try:
            STATUS_FILE.write_text(json.dumps({"recording": sessions}))
        except OSError:
            pass

    def _start(self, cam: dict, label: str) -> None:
        name = cam["name"]
        with self._lock:
            if name in self._sessions:
                logger.warning("[%s] Already recording, skipping", name)
                return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe = name.replace(" ", "_").lower()
        out = self._output_dir / f"{safe}_{label}_{timestamp}.mp4"

        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", cam["rtsp_url"],
            # Copy the stream as-is — no transcoding overhead on the Pi
            "-c:v", "copy",
            # faststart moves the moov atom to the front so the file is
            # playable even if recording is interrupted
            "-movflags", "+faststart",
            str(out),
        ]

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        with self._lock:
            self._sessions[name] = (proc, out)
        logger.info("[%s] Recording started → %s", name, out)
        self._write_status()

    def _stop(self, name: str) -> str | None:
        with self._lock:
            entry = self._sessions.pop(name, None)
        if not entry:
            return None

        proc, out = entry
        # Ask ffmpeg to stop cleanly (flush + write moov atom)
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except OSError:
            pass

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        self._write_status()

        if out.exists() and out.stat().st_size > 0:
            logger.info("[%s] Recording saved → %s (%d MB)", name, out, out.stat().st_size // 1_048_576)
            return str(out)

        logger.warning("[%s] Recording file missing or empty: %s", name, out)
        return None
