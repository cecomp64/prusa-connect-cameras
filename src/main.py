"""Entry point — wires up cameras, printer monitor, recorder, and uploader."""

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import yaml

from camera import Camera
from database import Database
from printer_monitor import PrinterMonitor, PrinterState
from recorder import Recorder
from youtube_uploader import YouTubeUploader

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

_shutdown = threading.Event()


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def _purge_loop(db: Database, stop_event: threading.Event) -> None:
    """Purge old telemetry once per day."""
    stop_event.wait(86400)
    while not stop_event.is_set():
        db.purge_old_telemetry()
        stop_event.wait(86400)


def main() -> None:
    cfg_path = os.environ.get("CONFIG", "config.yaml")
    cfg = load_config(cfg_path)

    cameras = [Camera(c) for c in cfg["cameras"]]
    recorder = Recorder(cfg)

    yt_cfg = cfg.get("youtube", {})
    uploader = YouTubeUploader(yt_cfg) if yt_cfg.get("enabled") else None

    db_path = Path(cfg.get("db_path", "/var/lib/prusa-cameras/prusa.db"))
    retention = cfg.get("telemetry_retention_days", 180)
    db = Database(db_path, telemetry_retention_days=retention)
    db.purge_old_telemetry()

    threading.Thread(target=_purge_loop, args=(db, _shutdown), daemon=True,
                     name="telemetry-purge").start()

    def on_print_start(state: PrinterState, job_id: str) -> None:
        logger.info("Print started (%s) — job %s", state.value, job_id)
        recorder.start_all(label="print")

    def on_print_end(state: PrinterState, job_id: str) -> None:
        logger.info("Print ended (%s) — stopping recordings", state.value)
        files = recorder.stop_all()
        db.end_print_job(job_id, state.value, files)

        if uploader and files:
            timestamp = time.strftime("%Y-%m-%d %H:%M")
            for path in files:
                title = f"3D Print — {timestamp} ({state.value})"
                try:
                    url = uploader.upload(path, title=title)
                    logger.info("YouTube: %s", url)
                except Exception as exc:
                    logger.error("YouTube upload failed for %s: %s", path, exc)

    monitor: PrinterMonitor | None = None
    pl_cfg = cfg.get("prusalink", {})
    if pl_cfg.get("api_key") and pl_cfg.get("host"):
        monitor = PrinterMonitor(
            pl_cfg, db=db,
            on_print_start=on_print_start,
            on_print_end=on_print_end,
        )
    else:
        logger.warning("PrusaLink not configured — recording will not start/stop automatically")

    def _shutdown_handler(sig, frame) -> None:
        logger.info("Signal %d received — shutting down", sig)
        recorder.stop_all()
        for cam in cameras:
            cam.stop()
        if monitor:
            monitor.stop()
        db.close()
        _shutdown.set()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    for cam in cameras:
        cam.start()
    if monitor:
        monitor.start()

    logger.info("Service running — %d camera(s) active", len(cameras))
    _shutdown.wait()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
