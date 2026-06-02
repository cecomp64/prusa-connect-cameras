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

import requests
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
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
    webrtc_url: str = ""
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
    category_id: str = "28"
    keywords: list[str] = ["3d printing", "prusa", "timelapse"]


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


@app.get("/api/printer/status")
def get_printer_status():
    cfg = load_config()
    pl  = cfg.get("prusalink", {})

    if not pl.get("host") or not pl.get("api_key"):
        return {"configured": False}

    host    = pl["host"].rstrip("/")
    api_key = pl["api_key"]

    try:
        resp = requests.get(
            f"{host}/api/v1/status",
            headers={"X-Api-Key": api_key},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc), "printer": None, "job": None}

    printer = data.get("printer") or {}
    job     = data.get("job") or None

    return {
        "configured": True,
        "reachable":  True,
        "error":      None,
        "printer": {
            "state":          printer.get("state", "UNKNOWN"),
            "temp_nozzle":    printer.get("temp_nozzle"),
            "target_nozzle":  printer.get("target_nozzle"),
            "temp_bed":       printer.get("temp_bed"),
            "target_bed":     printer.get("target_bed"),
            "axis_z":         printer.get("axis_z"),
            "flow":           printer.get("flow"),
            "speed":          printer.get("speed"),
            "fan_hotend":     printer.get("fan_hotend"),
            "fan_print":      printer.get("fan_print"),
        },
        "job": {
            "progress":        job.get("progress"),
            "time_remaining":  job.get("time_remaining"),
            "time_printing":   job.get("time_printing"),
            "display_name":    job.get("display_name"),
        } if job else None,
    }


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


# ── YouTube OAuth flow ────────────────────────────────────────────────────────
# Uses the same copy-paste redirect approach as OctoStreamControl:
# 1. Generate auth URL with redirect_uri=http://localhost:8181 (nothing listens there)
# 2. User opens the URL, authorizes with Google
# 3. Browser is redirected to localhost:8181/?code=...&state=... which fails to load
# 4. User copies that URL from the address bar and pastes it back here
# 5. We parse the code+state and exchange for credentials
#
# This avoids private-IP redirect restrictions and SSH tunnel requirements.
# Requires "Desktop app" OAuth client type in Google Cloud Console.

_YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# Google's loopback redirect — nothing needs to listen here
_LOOPBACK_REDIRECT = "http://localhost:8181"


@app.get("/api/youtube/auth/status")
def youtube_auth_status():
    import pickle
    cfg = load_config()
    creds_file = cfg.get("youtube", {}).get("credentials_cache", "")
    if not creds_file or not Path(creds_file).exists():
        return {"authorized": False}
    try:
        with open(creds_file, "rb") as f:
            creds = pickle.load(f)
        if creds.valid:
            return {"authorized": True}
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with open(creds_file, "wb") as f:
                pickle.dump(creds, f)
            return {"authorized": True}
    except Exception:
        pass
    return {"authorized": False}


@app.post("/api/youtube/auth/start")
def youtube_auth_start():
    """Generate and return the Google authorization URL."""
    import base64, hashlib, json as _json, secrets as _secrets, tempfile as _tmp
    from urllib.parse import urlencode

    cfg = load_config()
    secrets_file = cfg.get("youtube", {}).get("client_secrets_file", "")
    if not secrets_file or not Path(secrets_file).exists():
        raise HTTPException(
            400,
            f"client_secrets.json not found at '{secrets_file}'. "
            "Set the path in Settings → YouTube and save first.",
        )

    with open(secrets_file) as f:
        client_json = _json.load(f)
    client = client_json.get("web") or client_json.get("installed")
    if not client:
        raise HTTPException(400, "Unrecognised client_secrets.json format")

    # Generate PKCE pair ourselves — bypassing google_auth_oauthlib entirely so
    # we know the exact verifier that corresponds to the challenge in the URL.
    code_verifier  = _secrets.token_urlsafe(96)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(32)

    auth_url = client["auth_uri"] + "?" + urlencode({
        "client_id":             client["client_id"],
        "redirect_uri":          _LOOPBACK_REDIRECT,
        "response_type":         "code",
        "scope":                 " ".join(_YOUTUBE_SCOPES),
        "access_type":           "offline",
        "prompt":                "consent",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    })

    state_file = Path(_tmp.gettempdir()) / f"prusa_yt_flow_{state}.json"
    state_file.write_text(_json.dumps({
        "state":         state,
        "secrets_file":  secrets_file,
        "redirect_uri":  _LOOPBACK_REDIRECT,
        "code_verifier": code_verifier,
    }))

    return {"auth_url": auth_url, "state": state}


class CompleteAuthBody(BaseModel):
    redirect_url: str


@app.post("/api/youtube/auth/complete")
def youtube_auth_complete(body: CompleteAuthBody):
    """Exchange the code in the pasted redirect URL for credentials."""
    import json as _json, os as _os, tempfile as _tmp
    from urllib.parse import urlparse, parse_qs

    _os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    # Parse code + state out of the pasted URL
    try:
        params = parse_qs(urlparse(body.redirect_url).query)
        code  = params.get("code",  [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
    except Exception as exc:
        raise HTTPException(400, f"Could not parse URL: {exc}")

    if error:
        raise HTTPException(400, f"Authorization denied: {error}")
    if not code or not state:
        raise HTTPException(400, "URL is missing code or state — did you copy the full address bar URL?")

    # Load the persisted flow state
    state_file = Path(_tmp.gettempdir()) / f"prusa_yt_flow_{state}.json"
    if not state_file.exists():
        raise HTTPException(400, "Authorization session not found or expired — please start over.")

    flow_data = _json.loads(state_file.read_text())
    state_file.unlink(missing_ok=True)

    # Read client config directly from the secrets file
    with open(flow_data["secrets_file"]) as f:
        client_json = _json.load(f)
    client = client_json.get("installed") or client_json.get("web")
    if not client:
        raise HTTPException(400, "Unrecognised client_secrets.json format")

    # Make the token exchange directly so code_verifier is guaranteed in the POST
    # body — google_auth_oauthlib silently drops it when the flow is reconstructed.
    import requests as _req
    resp = _req.post(
        client["token_uri"],
        data={
            "code":          code,
            "client_id":     client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri":  _LOOPBACK_REDIRECT,
            "grant_type":    "authorization_code",
            "code_verifier": flow_data["code_verifier"],
        },
    )
    # Always parse the body so we can surface Google's actual error message
    try:
        token_data = resp.json()
    except Exception:
        raise HTTPException(400, f"Google returned HTTP {resp.status_code}: {resp.text[:300]}")

    if not resp.ok or "error" in token_data:
        err  = token_data.get("error", f"HTTP {resp.status_code}")
        desc = token_data.get("error_description", "")
        msg  = f"{err}: {desc}"
        import logging; logging.getLogger("youtube_auth").error("Token exchange failed — %s | full response: %s", msg, token_data)
        raise HTTPException(400, msg)

    # Build a Credentials object from the raw token response
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri=client["token_uri"],
        client_id=client["client_id"],
        client_secret=client["client_secret"],
        scopes=_YOUTUBE_SCOPES,
    )

    cfg = load_config()
    creds_file = cfg.get("youtube", {}).get(
        "credentials_cache", "/var/lib/prusa-cameras/youtube_creds.json"
    )
    try:
        import pickle
        dest = Path(creds_file)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            pickle.dump(creds, f)
    except Exception as exc:
        raise HTTPException(500, f"Failed to save credentials: {exc}")

    return {"ok": True}


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
