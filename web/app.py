"""
Prusa Camera Manager — web UI backend.

Runs as a separate process from the main snapshot/recording service.
Reads/writes the shared config.yaml and proxies RTSP streams as MJPEG.
"""

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Prusa Camera Manager")

CONFIG_PATH = Path(os.environ.get("CONFIG", "/etc/prusa-cameras/config.yaml"))


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"cameras": [], "recording": {"output_dir": "/var/lib/prusa-cameras/recordings"}}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _find_cam(cfg: dict, name: str) -> Optional[dict]:
    return next((c for c in cfg.get("cameras", []) if c["name"] == name), None)


# ── Pydantic models ────────────────────────────────────────────────────────────

class CameraBody(BaseModel):
    name: str
    rtsp_url: str
    token: str
    fingerprint: str = ""
    snapshot_interval: int = 10


class PrusaLinkBody(BaseModel):
    host: str
    api_key: str
    poll_interval: int = 15


class YouTubeBody(BaseModel):
    enabled: bool = False
    client_secrets_file: str = ""
    credentials_cache: str = ""
    privacy: str = "unlisted"
    playlist_id: str = ""


class RecordingBody(BaseModel):
    output_dir: str
    retention_days: int = 7


# ── Camera CRUD ────────────────────────────────────────────────────────────────

@app.get("/api/cameras")
def list_cameras():
    return load_config().get("cameras", [])


@app.post("/api/cameras", status_code=201)
def add_camera(body: CameraBody):
    cfg = load_config()
    cameras = cfg.setdefault("cameras", [])
    if any(c["name"] == body.name for c in cameras):
        raise HTTPException(409, f"Camera '{body.name}' already exists")
    data = body.model_dump()
    if not data["fingerprint"]:
        data["fingerprint"] = str(uuid.uuid4())
    cameras.append(data)
    save_config(cfg)
    return data


@app.put("/api/cameras/{name}")
def update_camera(name: str, body: CameraBody):
    cfg = load_config()
    cameras = cfg.get("cameras", [])
    for i, cam in enumerate(cameras):
        if cam["name"] == name:
            data = body.model_dump()
            if not data["fingerprint"]:
                data["fingerprint"] = cam.get("fingerprint", str(uuid.uuid4()))
            cameras[i] = data
            save_config(cfg)
            return data
    raise HTTPException(404, f"Camera '{name}' not found")


@app.delete("/api/cameras/{name}", status_code=204)
def delete_camera(name: str):
    cfg = load_config()
    before = len(cfg.get("cameras", []))
    cfg["cameras"] = [c for c in cfg.get("cameras", []) if c["name"] != name]
    if len(cfg["cameras"]) == before:
        raise HTTPException(404)
    save_config(cfg)


# ── Settings endpoints ─────────────────────────────────────────────────────────

@app.get("/api/prusalink")
def get_prusalink():
    return load_config().get("prusalink", {})


@app.put("/api/prusalink")
def update_prusalink(body: PrusaLinkBody):
    cfg = load_config()
    cfg["prusalink"] = body.model_dump()
    save_config(cfg)
    return cfg["prusalink"]


@app.get("/api/youtube")
def get_youtube():
    return load_config().get("youtube", {})


@app.put("/api/youtube")
def update_youtube(body: YouTubeBody):
    cfg = load_config()
    cfg["youtube"] = body.model_dump()
    save_config(cfg)
    return cfg["youtube"]


@app.get("/api/recording-config")
def get_recording_config():
    return load_config().get("recording", {})


@app.put("/api/recording-config")
def update_recording_config(body: RecordingBody):
    cfg = load_config()
    cfg["recording"] = body.model_dump()
    save_config(cfg)
    return cfg["recording"]


# ── Service status / control ───────────────────────────────────────────────────

@app.get("/api/service/status")
def service_status():
    r = subprocess.run(
        ["systemctl", "is-active", "prusa-cameras"],
        capture_output=True, text=True,
    )
    state = r.stdout.strip()
    return {"active": state == "active", "state": state}


@app.post("/api/service/restart")
def restart_service():
    r = subprocess.run(
        ["sudo", "systemctl", "restart", "prusa-cameras"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise HTTPException(500, r.stderr.strip() or "systemctl restart failed")
    return {"ok": True}


# ── Stream proxy ───────────────────────────────────────────────────────────────

@app.get("/api/stream/{camera_name}/snapshot")
async def get_snapshot(camera_name: str):
    cfg = load_config()
    cam = _find_cam(cfg, camera_name)
    if not cam:
        raise HTTPException(404, "Camera not found")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "quiet",
        "-rtsp_transport", "tcp",
        "-i", cam["rtsp_url"],
        "-vframes", "1", "-q:v", "5", "-f", "image2", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        jpeg, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "Stream timeout")
    if not jpeg:
        raise HTTPException(503, "Stream unavailable")
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/stream/{camera_name}/mjpeg")
async def stream_mjpeg(camera_name: str):
    cfg = load_config()
    cam = _find_cam(cfg, camera_name)
    if not cam:
        raise HTTPException(404, "Camera not found")

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "quiet",
            "-rtsp_transport", "tcp",
            "-i", cam["rtsp_url"],
            "-vf", "fps=5",
            "-q:v", "10",
            "-f", "mpjpeg", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                # 10s timeout detects stalled streams
                chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=10)
                if not chunk:
                    break
                yield chunk
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()

    # ffmpeg mpjpeg muxer uses "--ffserver" as the boundary delimiter,
    # so the boundary param (without the "--" prefix) is "ffserver"
    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace;boundary=ffserver",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


# ── Recordings ─────────────────────────────────────────────────────────────────

@app.get("/api/recordings")
def list_recordings():
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))
    if not rec_dir.exists():
        return []
    return [
        {"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
        for f in sorted(rec_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)
    ]


@app.delete("/api/recordings/{filename}", status_code=204)
def delete_recording(filename: str):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(400, "Invalid filename")
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))
    path = rec_dir / filename
    if not path.exists():
        raise HTTPException(404)
    path.unlink()


# ── Live logs via WebSocket ───────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-fu", "prusa-cameras", "--no-pager", "--output=cat",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            # 60s timeout keeps the connection alive even during quiet periods
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            if not line:
                break
            await ws.send_text(line.decode(errors="replace").rstrip())
    except (WebSocketDisconnect, asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            proc.kill()


# ── Static files — must be mounted last ───────────────────────────────────────

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
