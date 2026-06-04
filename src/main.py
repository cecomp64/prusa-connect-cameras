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
from print_logger import PrintLogger
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


def main() -> None:
    cfg_path = os.environ.get("CONFIG", "config.yaml")
    cfg = load_config(cfg_path)

    cameras = [Camera(c) for c in cfg["cameras"]]
    recorder = Recorder(cfg)

    yt_cfg = cfg.get("youtube", {})
    uploader = YouTubeUploader(yt_cfg) if yt_cfg.get("enabled") else None

    events_path = Path(cfg.get("stats", {}).get("events_file",
                       "/var/lib/prusa-cameras/print_events.json"))
    print_logger = PrintLogger(events_path)

    def on_print_start(state: PrinterState, display_name: str | None = None) -> None:
        logger.info("Print started (%s) — %s", state.value, display_name or "unknown file")
        print_logger.on_print_start(display_name)
        recorder.start_all(label="print")

    def on_print_end(state: PrinterState) -> None:
        logger.info("Print ended (%s) — stopping recordings", state.value)
        files = recorder.stop_all()
        print_logger.on_print_end(state.value, [str(f) for f in files])

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
        monitor = PrinterMonitor(pl_cfg, on_print_start=on_print_start, on_print_end=on_print_end)
    else:
        logger.warning("PrusaLink not configured — recording will not start/stop automatically")

    def _shutdown_handler(sig, frame) -> None:
        logger.info("Signal %d received — shutting down", sig)
        recorder.stop_all()
        for cam in cameras:
            cam.stop()
        if monitor:
            monitor.stop()
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
