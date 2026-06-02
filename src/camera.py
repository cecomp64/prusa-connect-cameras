"""Grabs JPEG snapshots from an RTSP stream and pushes them to Prusa Connect."""

import logging
import subprocess
import threading

import requests

logger = logging.getLogger(__name__)

SNAPSHOT_URL = "https://webcam.connect.prusa3d.com/c/snapshot"
# Prusa Connect rejects snapshots larger than this
MAX_JPEG_BYTES = 8 * 1024 * 1024

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


class Camera:
    def __init__(self, cfg: dict):
        self.name: str = cfg["name"]
        self.rtsp_url: str = cfg["rtsp_url"]
        self.token: str = cfg["token"]
        self.fingerprint: str = cfg["fingerprint"]
        self.interval: int = cfg.get("snapshot_interval", 10)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream_thread: threading.Thread | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._latest_frame: bytes | None = None
        self._frame_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._stream_thread = threading.Thread(
            target=self._maintain_stream, name=f"cam-stream-{self.name}", daemon=True
        )
        self._stream_thread.start()

        self._thread = threading.Thread(
            target=self._loop, name=f"cam-{self.name}", daemon=True
        )
        self._thread.start()
        logger.info("[%s] Snapshot thread started (every %ds)", self.name, self.interval)

    def stop(self) -> None:
        self._stop.set()
        proc = self._ffmpeg_proc
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._thread:
            self._thread.join(timeout=20)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maintain_stream(self) -> None:
        """Keep a persistent ffmpeg RTSP connection alive, restart on failure."""
        cmd = [
            "ffmpeg",
            "-loglevel", "quiet",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-vf", "fps=1",     # one frame/sec is plenty; reduces decode overhead
            "-q:v", "5",        # JPEG quality 1-31 (lower = better)
            "-f", "mjpeg",
            "pipe:1",
        ]

        while not self._stop.is_set():
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                self._ffmpeg_proc = proc
                logger.info("[%s] Persistent RTSP stream connected", self.name)
                self._read_mjpeg(proc)
            except Exception as exc:
                logger.warning("[%s] stream error: %s", self.name, exc)
            finally:
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()

            if not self._stop.is_set():
                logger.info("[%s] RTSP stream lost, reconnecting in 5s…", self.name)
                self._stop.wait(5)

    def _read_mjpeg(self, proc: subprocess.Popen) -> None:
        """Parse JPEG frames from the MJPEG stdout pipe and cache the latest."""
        buf = b""
        while not self._stop.is_set():
            chunk = proc.stdout.read(65536)
            if not chunk:
                return  # process ended
            buf += chunk

            # Extract every complete JPEG frame from the buffer
            while True:
                start = buf.find(_JPEG_SOI)
                if start == -1:
                    buf = b""
                    break
                end = buf.find(_JPEG_EOI, start + 2)
                if end == -1:
                    buf = buf[start:]   # keep partial frame
                    break
                frame = buf[start:end + 2]
                buf = buf[end + 2:]
                if len(frame) <= MAX_JPEG_BYTES:
                    with self._frame_lock:
                        self._latest_frame = frame

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._frame_lock:
                    jpeg = self._latest_frame
                if jpeg is None:
                    logger.debug("[%s] waiting for first frame…", self.name)
                else:
                    self._post_snapshot(jpeg)
                    logger.debug("[%s] snapshot sent (%d bytes)", self.name, len(jpeg))
            except Exception as exc:
                logger.warning("[%s] snapshot error: %s", self.name, exc)
            self._stop.wait(self.interval)

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
