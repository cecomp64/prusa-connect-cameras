"""Uploads completed recordings to YouTube via the Data API v3."""

import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# Science & Technology category
YT_CATEGORY = "28"


class YouTubeUploader:
    def __init__(self, cfg: dict):
        self._secrets = cfg["client_secrets_file"]
        self._creds_cache = cfg.get("credentials_cache", "youtube_creds.json")
        self._privacy = cfg.get("privacy", "unlisted")
        self._playlist_id = cfg.get("playlist_id", "")
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
                "tags": ["3d printing", "prusa", "timelapse"],
                "categoryId": YT_CATEGORY,
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

    def _load_creds(self) -> Credentials:
        creds = None
        cache = Path(self._creds_cache)

        if cache.exists():
            creds = Credentials.from_authorized_user_file(str(cache), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self._secrets, SCOPES)
                creds = flow.run_local_server(port=0)

            cache.write_text(creds.to_json())

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
