"""Uploads completed recordings to YouTube via the Data API v3."""

import json
import logging
import pickle
import subprocess
import time
from pathlib import Path

import httplib2
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)


def _probe_file(path: Path) -> dict | None:
    """Return ffprobe JSON summary or None if ffprobe is unavailable/fails."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.debug("ffprobe unavailable or failed: %s", exc)
    return None


class YouTubeUploader:
    def __init__(self, cfg: dict):
        self._secrets = cfg["client_secrets_file"]
        self._creds_cache = cfg.get("credentials_cache", "youtube_creds.json")
        self._privacy = cfg.get("privacy", "unlisted")
        self._playlist_id = cfg.get("playlist_id", "")
        self._category_id = str(cfg.get("category_id", "28"))
        self._keywords = cfg.get("keywords", ["3d printing", "prusa", "timelapse"])
        self._uploaded_log = Path(cfg.get("uploaded_log", "/var/lib/prusa-cameras/uploaded.json"))
        self._uploaded: dict[str, dict] = self._load_uploaded_log()
        self._svc = None  # lazy-initialised on first upload

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def upload(self, video_path: str, title: str, description: str = "", on_progress=None) -> str:
        """Upload *video_path* and return the YouTube watch URL."""
        path = Path(video_path)
        key = str(path.resolve())
        if key in self._uploaded:
            existing_url = self._uploaded[key]["url"]
            logger.info("Already uploaded %s → %s (skipping)", path.name, existing_url)
            return existing_url

        if not self._svc:
            self._svc = self._build_service()

        # --- pre-upload diagnostics ---
        file_size = path.stat().st_size if path.exists() else 0
        logger.info(
            "YouTube upload starting: %s  size=%d bytes (%.1f MB)",
            path, file_size, file_size / 1_048_576,
        )

        probe = _probe_file(path)
        if probe:
            fmt = probe.get("format", {})
            duration = float(fmt.get("duration", 0))
            bit_rate = int(fmt.get("bit_rate", 0))
            fmt_name = fmt.get("format_name", "unknown")
            logger.info(
                "ffprobe: format=%s  duration=%.1fs  bitrate=%d kbps",
                fmt_name, duration, bit_rate // 1000,
            )
            for stream in probe.get("streams", []):
                codec_type = stream.get("codec_type", "?")
                codec_name = stream.get("codec_name", "?")
                if codec_type == "video":
                    logger.info(
                        "ffprobe video stream: codec=%s  %sx%s  fps=%s",
                        codec_name,
                        stream.get("width", "?"),
                        stream.get("height", "?"),
                        stream.get("r_frame_rate", "?"),
                    )
                elif codec_type == "audio":
                    logger.info(
                        "ffprobe audio stream: codec=%s  channels=%s  sample_rate=%s",
                        codec_name,
                        stream.get("channels", "?"),
                        stream.get("sample_rate", "?"),
                    )
            if duration < 1.0:
                logger.warning(
                    "Video duration is %.1fs — YouTube may reject very short files", duration
                )
        else:
            logger.warning("ffprobe not available; cannot validate file before upload")

        body = {
            "snippet": {
                "title": title[:100],  # YouTube title limit
                "description": description or "Recorded by prusa-connect-cameras",
                "tags": self._keywords,
                "categoryId": self._category_id,
            },
            "status": {
                "privacyStatus": self._privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        mime = "video/mp4" if str(path).lower().endswith(".mp4") else "video/*"
        logger.info("Uploading as MIME type: %s", mime)
        media = MediaFileUpload(
            str(path), mimetype=mime, chunksize=10 * 1024 * 1024, resumable=True
        )
        req = self._svc.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media
        )

        response = None
        chunk_count = 0
        while response is None:
            status, response = req.next_chunk()
            chunk_count += 1
            if status:
                pct = int(status.progress() * 100)
                uploaded = int(status.progress() * file_size)
                logger.info(
                    "YouTube upload chunk %d: %d%%  (%d / %d bytes)",
                    chunk_count, pct, uploaded, file_size,
                )
                if on_progress:
                    on_progress(pct)

        logger.info("YouTube API response: %s", json.dumps(response, indent=2))
        video_id = response["id"]
        url = f"https://youtu.be/{video_id}"
        logger.info("Uploaded → %s  (processing status: %s)", url, response.get("status", {}).get("uploadStatus", "unknown"))

        if self._playlist_id:
            self._add_to_playlist(video_id)

        self._uploaded[key] = {
            "url": url,
            "title": title,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save_uploaded_log()
        return url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_uploaded_log(self) -> dict:
        if self._uploaded_log.exists():
            try:
                return json.loads(self._uploaded_log.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read uploaded log %s: %s", self._uploaded_log, exc)
        return {}

    def _save_uploaded_log(self) -> None:
        try:
            self._uploaded_log.parent.mkdir(parents=True, exist_ok=True)
            self._uploaded_log.write_text(json.dumps(self._uploaded, indent=2))
        except OSError as exc:
            logger.warning("Failed to save uploaded log: %s", exc)

    def _build_service(self):
        creds = self._load_creds()
        # 120-second per-request timeout prevents hangs on slow/dropped connections.
        authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
        return build("youtube", "v3", http=authorized_http, cache_discovery=False)

    def _load_creds(self):
        cache = Path(self._creds_cache)
        if not cache.exists():
            raise RuntimeError(
                f"YouTube credentials not found at {cache}. "
                "Authorize via Settings → YouTube in the web UI."
            )

        with open(cache, "rb") as f:
            creds = pickle.load(f)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(cache, "wb") as f:
                    pickle.dump(creds, f)
            else:
                raise RuntimeError(
                    "YouTube credentials are invalid. "
                    "Re-authorize via Settings → YouTube in the web UI."
                )

        return creds

    def _add_to_playlist(self, video_id: str) -> None:
        try:
            self._svc.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": self._playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            logger.info("Added to playlist %s", self._playlist_id)
        except Exception as exc:
            logger.warning("Playlist insert failed: %s", exc)
