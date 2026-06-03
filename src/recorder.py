"""Manages per-camera FFmpeg recording sessions."""

import json
import logging
import signal
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
            # Transcode to a clean H.264 profile rather than stream-copying the
            # camera's raw bitstream. Cameras can produce H.264 with long
            # keyframe intervals, B-frames, data-partitioning, or non-standard
            # profiles that cause YouTube "Processing Abandoned". libx264 veryfast
            # is fast enough for a Pi recording a print at typical camera resolutions.
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-g", "60",   # keyframe every 60 frames (~2 s at 30 fps)
            "-bf", "0",   # no B-frames — simpler for downstream decoders
            # Add a silent audio track. YouTube frequently rejects video-only
            # files with "Processing Abandoned".
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-c:a", "aac", "-b:a", "64k",
            "-shortest",
            # frag_keyframe+empty_moov writes a minimal moov at the START of the
            # file and a self-contained fragment per keyframe. The file is valid
            # even if ffmpeg is SIGKILL'd mid-recording — no moov-at-end needed.
            "-movflags", "frag_keyframe+empty_moov",
            str(out),
        ]

        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
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
        # With frag_keyframe+empty_moov, a SIGKILL-terminated file is still
        # valid (all written fragments are self-contained). But we still try
        # a clean shutdown so the last partial GOP is flushed.
        logger.info("[%s] Sending SIGTERM to ffmpeg (pid %d)", name, proc.pid)
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass

        try:
            proc.wait(timeout=30)
            logger.info("[%s] ffmpeg exited (rc=%d)", name, proc.returncode)
        except subprocess.TimeoutExpired:
            logger.warning("[%s] ffmpeg still running after 30 s — sending SIGINT", name)
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=10)
                logger.info("[%s] ffmpeg exited after SIGINT (rc=%d)", name, proc.returncode)
            except (subprocess.TimeoutExpired, OSError):
                logger.warning("[%s] ffmpeg still running — sending SIGKILL", name)
                proc.kill()
                proc.wait()

        self._write_status()

        if out.exists() and out.stat().st_size > 0:
            size_mb = out.stat().st_size // 1_048_576
            logger.info("[%s] Recording saved → %s (%d MB)", name, out, size_mb)
            return str(out)

        logger.warning("[%s] Recording file missing or empty: %s", name, out)
        return None
