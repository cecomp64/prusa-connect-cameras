"""
Prusa Camera Manager — web UI backend.

Runs as a separate process from the main snapshot/recording service.
Reads/writes the shared config.yaml and proxies RTSP streams as MJPEG.
"""

import asyncio
import json
import logging
import os
import pickle
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

import requests
import yaml
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

logger = logging.getLogger("prusa_web")
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from pydantic import BaseModel

app = FastAPI(title="Prusa Camera Manager")

CONFIG_PATH = Path(os.environ.get("CONFIG", "/etc/prusa-cameras/config.yaml"))

# In-memory upload state: filename → {status, pct, url, error}
_uploads: dict[str, dict] = {}


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


def _pl_config() -> tuple[str, str]:
    cfg = load_config()
    pl = cfg.get("prusalink", {})
    if not pl.get("host") or not pl.get("api_key"):
        raise HTTPException(503, "PrusaLink not configured")
    return pl["host"].rstrip("/"), pl["api_key"]


@app.post("/api/printer/control/pause")
def printer_pause():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "pause", "action": "pause"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/control/resume")
def printer_resume():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "pause", "action": "resume"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/control/stop")
def printer_stop():
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/job",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "cancel"},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


@app.post("/api/printer/upload")
def printer_upload(
    file: UploadFile = File(...),
    storage: str = Form("usb"),
    print_after_upload: str = Form("false"),
):
    from urllib.parse import quote as urlquote
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    fname = file.filename or ""
    if not fname.lower().endswith((".gcode", ".bgcode")):
        raise HTTPException(400, "Only .gcode and .bgcode files are supported")
    host, api_key = _pl_config()
    do_print = print_after_upload.lower() in ("true", "1", "yes")
    data = file.file.read()
    size_mb = len(data) / 1_048_576
    logger.info("Uploading %s to printer (%s, %.1f MB, print_after=%s)", fname, storage, size_mb, do_print)
    headers: dict[str, str] = {
        "X-Api-Key": api_key,
        "Content-Type": "application/octet-stream",
        "Overwrite": "?1",
    }
    if do_print:
        headers["Print-After-Upload"] = "?1"
    try:
        r = requests.put(
            f"{host}/api/v1/files/{storage}/{urlquote(fname)}",
            headers=headers,
            data=data,
            timeout=(15, 600),
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        logger.error("Printer upload failed for %s: HTTP %s — %s", fname, exc.response.status_code, exc.response.text[:200])
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        logger.error("Printer upload failed for %s: %s", fname, exc)
        raise HTTPException(503, str(exc))
    logger.info("Upload complete: %s (%.1f MB)", fname, size_mb)
    return {"ok": True, "filename": fname}


def _flatten_files(node, storage: str, prefix: str = "") -> list[dict]:
    """Recursively flatten a PrusaLink v1 file-tree node into a flat list of print files."""
    results: list[dict] = []
    if isinstance(node, list):
        for item in node:
            results.extend(_flatten_files(item, storage, prefix))
    elif isinstance(node, dict):
        name  = node.get("name", "")
        ftype = (node.get("type") or "").upper()
        path  = f"{prefix}/{name}".lstrip("/") if prefix else name

        is_print = ftype == "PRINT_FILE" or (
            ftype not in ("FOLDER",) and name.lower().endswith((".gcode", ".bgcode"))
        )
        if is_print and name:
            results.append({
                "name":         name,
                "display_name": node.get("display_name") or name,
                "size":         node.get("size") or node.get("bytes") or 0,
                "timestamp":    node.get("m_timestamp") or node.get("date") or 0,
                "storage":      storage,
                "path":         path,
            })
        for child in node.get("children") or []:
            results.extend(_flatten_files(child, storage, path if name and name != "/" else prefix))
    return results


@app.get("/api/printer/files/{storage}")
def list_printer_files(storage: str):
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    host, api_key = _pl_config()
    try:
        r = requests.get(
            f"{host}/api/v1/files/{storage}",
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    files = _flatten_files(data, storage)
    files.sort(key=lambda f: f["timestamp"], reverse=True)
    return files


@app.post("/api/printer/files/{storage}/{path:path}/print")
def print_file(storage: str, path: str):
    from urllib.parse import quote as urlquote
    if storage not in ("usb", "local"):
        raise HTTPException(400, "storage must be 'usb' or 'local'")
    host, api_key = _pl_config()
    try:
        r = requests.post(
            f"{host}/api/files/{storage}/{urlquote(path)}",
            headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
            json={"command": "select", "print": True},
            timeout=10,
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text[:200])
    except Exception as exc:
        raise HTTPException(503, str(exc))
    return {"ok": True}


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


# ── Recording status ───────────────────────────────────────────────────────────

_STATUS_FILE = Path("/tmp/prusa-cameras-status.json")


def _live_sessions() -> list[dict]:
    """Return only recording sessions whose ffmpeg process is still running."""
    try:
        data = json.loads(_STATUS_FILE.read_text())
    except Exception:
        return []
    live = []
    for s in data.get("recording", []):
        if not isinstance(s, dict):
            continue
        pid = s.get("pid")
        if pid is None:
            live.append(s)
            continue
        try:
            os.kill(pid, 0)
            live.append(s)
        except ProcessLookupError:
            logger.debug("Stale recording session for '%s' (pid %d gone)", s.get("name"), pid)
        except PermissionError:
            live.append(s)  # process exists but owned by a different user
    return live


@app.get("/api/recording-status")
def recording_status():
    return {"recording": _live_sessions()}


@app.post("/api/recording-status/start/{camera_name}")
def start_recording(camera_name: str):
    import time as _time

    cfg = load_config()
    cam = _find_cam(cfg, camera_name)
    if not cam:
        raise HTTPException(404, f"Camera '{camera_name}' not found")

    # Use PID-checked live sessions so a dead stale entry doesn't block restarts
    if any(s.get("name") == camera_name for s in _live_sessions()):
        raise HTTPException(409, f"Camera '{camera_name}' is already recording")

    rec_cfg = cfg.get("recording", {})
    output_dir = Path(rec_cfg.get("output_dir", "/var/lib/prusa-cameras/recordings"))
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    safe = camera_name.replace(" ", "_").lower()
    out = output_dir / f"{safe}_manual_{timestamp}.mp4"

    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", cam["rtsp_url"],
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "25",
        "-g", "30",
        "-bf", "0",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        "-movflags", "+frag_keyframe+empty_moov+faststart",
        str(out),
    ]

    import shutil
    ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
    cmd[0] = ffmpeg_bin
    logger.info("[%s] ffmpeg binary: %s", camera_name, ffmpeg_bin)
    logger.info("[%s] command: %s", camera_name, " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait briefly so we can catch an immediate failure (bad URL, codec error, etc.)
    _time.sleep(1.5)
    rc = proc.poll()
    if rc is not None:
        stderr = proc.stderr.read().decode(errors="replace").strip()
        logger.error("[%s] ffmpeg exited immediately (rc=%d): %s", camera_name, rc, stderr)
        raise HTTPException(500, f"Recording failed to start (rc={rc}): {stderr[-300:] or 'unknown error'}")

    # Process is alive — drain its stderr in the background so warnings reach the log
    def _drain_stderr(p: subprocess.Popen, name: str) -> None:
        for line in p.stderr:
            logger.warning("[%s] ffmpeg: %s", name, line.decode(errors="replace").rstrip())

    threading.Thread(target=_drain_stderr, args=(proc, camera_name), daemon=True).start()

    # Write only live sessions + the new one (avoids re-adding any stale entries)
    sessions = [s for s in _live_sessions() if s.get("name") != camera_name]
    sessions.append({"name": camera_name, "pid": proc.pid, "path": str(out)})
    try:
        _STATUS_FILE.write_text(json.dumps({"recording": sessions}))
    except OSError:
        pass

    logger.info("[%s] Manual recording started → %s (pid %d)", camera_name, out, proc.pid)
    return {"ok": True, "path": str(out)}


@app.post("/api/recording-status/stop/{camera_name}")
def stop_recording(camera_name: str):
    try:
        data = json.loads(_STATUS_FILE.read_text())
    except Exception:
        raise HTTPException(404, "No active recordings found")
    sessions = data.get("recording", [])
    session = next((s for s in sessions if isinstance(s, dict) and s.get("name") == camera_name), None)
    if not session:
        raise HTTPException(404, f"No active recording for '{camera_name}'")
    pid = session.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    data["recording"] = [s for s in sessions if not (isinstance(s, dict) and s.get("name") == camera_name)]
    try:
        _STATUS_FILE.write_text(json.dumps(data))
    except OSError:
        pass
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

    # Build map of filename → session for any active recordings
    live_by_name: dict[str, dict] = {
        Path(s["path"]).name: s
        for s in _live_sessions()
        if "path" in s
    }

    results = []
    if rec_dir.exists():
        for f in sorted(rec_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            session = live_by_name.pop(f.name, None)
            results.append({
                "name": f.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "live": session is not None,
                "camera_name": session["name"] if session else None,
            })

    # Sessions whose files haven't appeared on disk yet
    for fname, session in live_by_name.items():
        results.insert(0, {
            "name": fname,
            "size": 0,
            "mtime": 0,
            "live": True,
            "camera_name": session["name"],
        })

    return results


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


@app.post("/api/recordings/{filename}/upload")
def start_upload(filename: str):
    if "/" in filename or ".." in filename or not filename.endswith(".mp4"):
        raise HTTPException(400, "Invalid filename")
    if _uploads.get(filename, {}).get("status") == "uploading":
        raise HTTPException(409, "Upload already in progress")
    cfg = load_config()
    rec_dir = Path(cfg.get("recording", {}).get("output_dir", "/var/lib/prusa-cameras/recordings"))
    video_path = rec_dir / filename
    if not video_path.exists():
        raise HTTPException(404, "Recording not found")
    _uploads[filename] = {"status": "pending", "pct": 0, "url": None, "error": None}
    threading.Thread(target=_do_upload, args=(filename, str(video_path), cfg), daemon=True).start()
    return {"ok": True}


@app.get("/api/uploads/statuses")
def get_upload_statuses():
    return _uploads


def _probe_video(path: str) -> dict | None:
    """Run ffprobe and return parsed JSON, or None if unavailable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return json.loads(r.stdout)
        logger.warning("ffprobe exited %d: %s", r.returncode, r.stderr.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("ffprobe failed: %s", exc)
    return None


def _remux_for_upload(src: str) -> str | None:
    """
    Re-mux src into a temp file with a clean moov atom at the front.
    Returns path to remuxed file, or None on failure.

    This is a speculative fix for YouTube "Processing Abandoned": when ffmpeg
    is stopped via stdin 'q', the moov atom should be written, but if the
    original stream had issues the container may still be malformed.
    Re-muxing validates and rebuilds the container headers.
    """
    dst = src.replace(".mp4", "_remux.mp4")
    logger.info("Re-muxing %s → %s", src, dst)
    try:
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-v", "warning",
                "-i", src,
                "-c", "copy",
                "-movflags", "+faststart",
                dst,
            ],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            logger.error("Re-mux failed (exit %d):\nstdout: %s\nstderr: %s", r.returncode, r.stdout.strip(), r.stderr.strip())
            return None
        if r.stderr.strip():
            logger.warning("ffmpeg re-mux warnings: %s", r.stderr.strip())
        dst_size = Path(dst).stat().st_size if Path(dst).exists() else 0
        logger.info("Re-mux complete: %s  (%.1f MB)", dst, dst_size / 1_048_576)
        return dst
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("Re-mux exception: %s", exc)
        return None


def _do_upload(filename: str, video_path: str, cfg: dict) -> None:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = cfg.get("youtube", {})
    creds_file = yt.get("credentials_cache", "")
    remuxed_path = None
    try:
        # ── pre-upload diagnostics ──────────────────────────────────────
        src = Path(video_path)
        file_size = src.stat().st_size if src.exists() else 0
        logger.info(
            "YouTube upload requested: %s  exists=%s  size=%d bytes (%.1f MB)",
            video_path, src.exists(), file_size, file_size / 1_048_576,
        )

        probe = _probe_video(video_path)
        if probe:
            fmt = probe.get("format", {})
            duration = float(fmt.get("duration", 0))
            bit_rate = int(fmt.get("bit_rate", 0))
            logger.info(
                "ffprobe: format=%s  duration=%.1fs  bitrate=%d kbps  nb_streams=%s",
                fmt.get("format_name", "?"), duration, bit_rate // 1000,
                fmt.get("nb_streams", "?"),
            )
            for stream in probe.get("streams", []):
                ctype = stream.get("codec_type", "?")
                if ctype == "video":
                    logger.info(
                        "ffprobe video: codec=%s  %sx%s  fps=%s  profile=%s",
                        stream.get("codec_name", "?"),
                        stream.get("width", "?"), stream.get("height", "?"),
                        stream.get("r_frame_rate", "?"),
                        stream.get("profile", "?"),
                    )
                elif ctype == "audio":
                    logger.info(
                        "ffprobe audio: codec=%s  channels=%s  sample_rate=%s",
                        stream.get("codec_name", "?"),
                        stream.get("channels", "?"),
                        stream.get("sample_rate", "?"),
                    )
            if duration < 2.0:
                logger.warning("Duration %.1fs is very short — YouTube often rejects short files", duration)
        else:
            logger.warning("ffprobe unavailable — cannot validate file before upload")

        upload_path = video_path
        upload_size = file_size

        # ── credentials ────────────────────────────────────────────────
        if not creds_file or not Path(creds_file).exists():
            raise RuntimeError("YouTube credentials not found — authorize in Settings → YouTube")
        with open(creds_file, "rb") as f:
            creds = pickle.load(f)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                logger.info("Refreshing expired YouTube credentials")
                creds.refresh(GoogleRequest())
                with open(creds_file, "wb") as f:
                    pickle.dump(creds, f)
            else:
                raise RuntimeError("YouTube credentials expired — re-authorize in Settings → YouTube")

        svc = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "snippet": {
                "title": filename[:100],
                "description": "Recorded by prusa-connect-cameras",
                "tags": yt.get("keywords", []),
                "categoryId": str(yt.get("category_id", "28")),
            },
            "status": {
                "privacyStatus": yt.get("privacy", "unlisted"),
                "selfDeclaredMadeForKids": False,
            },
        }

        mime = "video/mp4" if upload_path.lower().endswith(".mp4") else "video/*"
        logger.info("Uploading %s as %s (%d bytes)", upload_path, mime, upload_size)
        media = MediaFileUpload(upload_path, mimetype=mime, chunksize=10 * 1024 * 1024, resumable=True)
        req = svc.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

        _uploads[filename] = {"status": "uploading", "pct": 0, "url": None, "error": None}
        response = None
        chunk_num = 0
        while response is None:
            status, response = req.next_chunk()
            chunk_num += 1
            if status:
                pct = int(status.progress() * 100)
                sent = int(status.progress() * upload_size)
                _uploads[filename]["pct"] = pct
                logger.info("Upload chunk %d: %d%%  (%d / %d bytes)", chunk_num, pct, sent, upload_size)

        logger.info("YouTube API response: %s", json.dumps(response, indent=2))
        upload_status = response.get("status", {}).get("uploadStatus", "unknown")
        if upload_status != "uploaded":
            logger.error("YouTube upload status is '%s' — expected 'uploaded'", upload_status)

        video_id = response["id"]
        url = f"https://youtu.be/{video_id}"
        logger.info("Upload complete → %s  uploadStatus=%s", url, upload_status)

        playlist_id = yt.get("playlist_id", "")
        if playlist_id:
            try:
                svc.playlistItems().insert(
                    part="snippet",
                    body={"snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }},
                ).execute()
                logger.info("Added to playlist %s", playlist_id)
            except Exception as exc:
                logger.warning("Playlist insert failed: %s", exc)

        _uploads[filename] = {"status": "done", "pct": 100, "url": url, "error": None}
    except Exception as exc:
        logger.error("YouTube upload failed for %s: %s", filename, exc, exc_info=True)
        _uploads[filename] = {"status": "error", "pct": 0, "url": None, "error": str(exc)}
    finally:
        if remuxed_path and Path(remuxed_path).exists():
            try:
                Path(remuxed_path).unlink()
                logger.info("Cleaned up re-muxed temp file: %s", remuxed_path)
            except OSError:
                pass


# ── Live logs via WebSocket ───────────────────────────────────────────────────

@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-fu", "prusa-cameras", "-u", "prusa-cameras-web", "--no-pager", "--output=cat",
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
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
