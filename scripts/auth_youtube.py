#!/usr/bin/env python3
"""
One-time OAuth2 authorisation flow for YouTube uploads.

Run this script once on a machine with a browser (can be your laptop, not
the Pi).  It will open a browser window, ask you to log in with Google, and
save credentials to the path specified in config.yaml → youtube.credentials_cache.

Pre-requisites:
  1. Google Cloud project with YouTube Data API v3 enabled.
  2. OAuth 2.0 client credentials (Desktop type) downloaded as client_secrets.json.
  See: https://developers.google.com/youtube/v3/quickstart/python

Usage:
  python3 scripts/auth_youtube.py [--config /path/to/config.yaml]
"""

import argparse
import sys
from pathlib import Path

import yaml
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)

    yt = cfg.get("youtube", {})
    secrets = yt.get("client_secrets_file", "client_secrets.json")
    creds_cache = yt.get("credentials_cache", "youtube_creds.json")

    if not Path(secrets).exists():
        print(f"ERROR: client_secrets.json not found at {secrets}")
        print("Download it from Google Cloud Console → APIs & Services → Credentials")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES)
    creds = flow.run_local_server(port=0)

    Path(creds_cache).write_text(creds.to_json())
    print(f"Credentials saved to {creds_cache}")
    print("Copy that file to the Pi if you ran this on a different machine.")


if __name__ == "__main__":
    main()
