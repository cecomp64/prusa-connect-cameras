"""Uploads completed recordings to YouTube via the Data API v3."""

import logging
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)


class YouTubeUploader:
    def __init__(self, cfg: dict):
        self._secrets = cfg["client_secrets_file"]
        self._creds_cache = cfg.get("credentials_cache", "youtube_creds.json")
        self._privacy = cfg.get("privacy", "unlisted")
        self._playlist_id = cfg.get("playlist_id", "")
        self._category_id = str(cfg.get("category_id", "28"))
        self._keywords = cfg.get("keywords", ["3d printing", "prusa", "timelapse"])
        self._svc = None  # lazy-initialised on first upload

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def upload(self, video_path: str, title: str, description: str = "") -> str:
        """Upload *video_path* and return the YouTube watch URL."""
        if not self._svc:
            self._svc = self._build_service()

        path = Path(video_path)
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

        media = MediaFileUpload(str(path), chunksize=10 * 1024 * 1024, resumable=True)
        req = self._svc.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media
        )

        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info("YouTube upload %d%%", pct)

        video_id = response["id"]
        url = f"https://youtu.be/{video_id}"
        logger.info("Uploaded → %s", url)

        if self._playlist_id:
            self._add_to_playlist(video_id)

        return url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_service(self):
        creds = self._load_creds()
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

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
