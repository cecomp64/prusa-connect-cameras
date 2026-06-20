"""Entry point — wires up cameras, printer monitor, recorder, and uploader."""

import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

import psutil
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


def _upload_worker(q: queue.Queue, uploader: "YouTubeUploader", db: Database) -> None:
    while True:
        path, title, filename = q.get()
        logger.info("Upload worker: starting %s", filename)
        try:
            db.set_upload_state(filename, "uploading", pct=0)
            url = uploader.upload(
                path, title=title,
                on_progress=lambda pct, fn=filename: db.set_upload_pct(fn, pct),
            )
            db.set_upload_state(filename, "done", url=url, pct=100)
            logger.info("YouTube upload complete: %s → %s", filename, url)
        except Exception as exc:
            db.set_upload_state(filename, "error", error=str(exc))
            logger.error("YouTube upload failed for %s: %s", path, exc)
        finally:
            q.task_done()


def _system_metrics_loop(db: Database, stop_event: threading.Event) -> None:
    """Collect CPU and memory stats every 60 seconds."""
    while not stop_event.is_set():
        try:
            cpu_temp = None
            try:
                with open("/sys/class/thermal/thermal_zone0/temp") as f:
                    cpu_temp = round(int(f.read().strip()) / 1000, 1)
            except Exception:
                pass
            mem = psutil.virtual_memory()
            db.insert_system_metrics(
                cpu_temp=cpu_temp,
                cpu_usage=psutil.cpu_percent(interval=None),
                mem_used=round(mem.used / 1024 / 1024),
                mem_total=round(mem.total / 1024 / 1024),
            )
        except Exception as exc:
            logger.warning("System metrics collection error: %s", exc)
        stop_event.wait(60)


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
    threading.Thread(target=_system_metrics_loop, args=(db, _shutdown), daemon=True,
                     name="system-metrics").start()

    upload_queue: queue.Queue | None = None
    if uploader:
        upload_queue = queue.Queue()
        threading.Thread(
            target=_upload_worker,
            args=(upload_queue, uploader, db),
            daemon=True,
            name="yt-upload",
        ).start()
        for item in db.get_pending_uploads():
            file_path = item.get("file_path")
            if file_path and Path(file_path).exists():
                logger.info("Requeueing interrupted upload: %s", item["filename"])
                upload_queue.put((file_path, item.get("title") or item["filename"], item["filename"]))
            else:
                logger.warning("Interrupted upload file missing, marking error: %s", item["filename"])
                db.set_upload_state(item["filename"], "error", error="File missing after service restart")

    def on_print_start(state: PrinterState, job_id: str) -> None:
        logger.info("Print started (%s) — job %s", state.value, job_id)
        recorder.start_all(label="print")

    def on_print_end(state: PrinterState, job_id: str | None, job_name: str | None, printer_duration: int | None, paused_seconds: int = 0) -> None:
        logger.info("Print ended (%s) — stopping recordings", state.value)
        files = recorder.stop_all()
        if job_id:
            db.end_print_job(job_id, state.value, files, printer_duration, paused_seconds)

        if upload_queue is not None and files:
            timestamp = time.strftime("%Y-%m-%d %H:%M")
            for path in files:
                filename = Path(path).name
                title = f"{job_name} — {timestamp}" if job_name else f"3D Print — {timestamp} ({state.value})"
                db.set_upload_state(filename, "pending", pct=0, title=title, file_path=path)
                upload_queue.put((path, title, filename))
                logger.info("Queued for upload: %s", filename)

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
