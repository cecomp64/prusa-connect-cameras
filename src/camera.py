"""Grabs JPEG snapshots from an RTSP stream and pushes them to Prusa Connect."""

import logging
import subprocess
import threading

import requests

logger = logging.getLogger(__name__)

SNAPSHOT_URL = "https://webcam.connect.prusa3d.com/c/snapshot"
# Prusa Connect rejects snapshots larger than this
MAX_JPEG_BYTES = 8 * 1024 * 1024


class Camera:
    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.rtsp_url: str = cfg["rtsp_url"]
        self.token: str = cfg["token"]
        self.fingerprint: str = cfg["fingerprint"]
        self.interval: int = cfg.get("snapshot_interval", 10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"cam-{self.name}", daemon=True
        )
        self._thread.start()
        logger.info("[%s] Snapshot thread started (every %ds)", self.name, self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                jpeg = self._grab_frame()
                self._post_snapshot(jpeg)
                logger.debug("[%s] snapshot sent (%d bytes)", self.name, len(jpeg))
            except Exception as exc:
                logger.warning("[%s] snapshot error: %s", self.name, exc)
            self._stop.wait(self.interval)

    def _grab_frame(self) -> bytes:
        """Pull a single JPEG frame from the RTSP stream via ffmpeg."""
        cmd = [
            "ffmpeg",
            "-loglevel", "quiet",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vframes", "1",
            "-q:v", "5",          # JPEG quality 1-31 (lower = better)
            "-f", "image2",
            "-",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        if len(result.stdout) > MAX_JPEG_BYTES:
            raise ValueError(f"frame too large: {len(result.stdout)} bytes")
        return result.stdout

    def _post_snapshot(self, jpeg: bytes) -> None:
        resp = requests.put(
            SNAPSHOT_URL,
            headers={
                "token": self.token,
                "fingerprint": self.fingerprint,
                "Content-Type": "image/jpg",
            },
            data=jpeg,
            timeout=15,
        )
        resp.raise_for_status()
