# prusa-connect-cameras

Turns RTSP streams (via [mediamtx](https://github.com/bluenviron/mediamtx)) into Prusa Connect cameras, and records prints to YouTube.

**What it does**

- Pushes JPEG snapshots from each RTSP stream to Prusa Connect so cameras appear in the dashboard
- Watches the local PrusaLink API for print start/finish/error events
- Starts an FFmpeg recording on each camera when a print begins
- Stops the recording and uploads the MP4 to YouTube when the print ends

## Prerequisites

- Raspberry Pi 5 running mediamtx with streams available (see below)
- Python 3.11+
- ffmpeg (`sudo apt install ffmpeg`)
- A Prusa printer running PrusaLink (MK4, XL, MINI, MK3.9)

## Quick start

```sh
# Clone/copy the repo to the Pi
cd /home/pi
git clone <repo> prusa-connect-cameras
cd prusa-connect-cameras

# Install both services and dependencies
bash scripts/setup.sh

# Start the web UI
sudo systemctl enable --now prusa-cameras-web
```

Then open **http://\<pi-ip\>:8080** in a browser and configure everything from the UI.

Find your Pi's IP with `hostname -I` (the setup script prints it at the end).

---

## Configuration

### Via the web UI (recommended)

The web UI at `http://<pi-ip>:8080` covers all configuration:

| Tab | What you can do |
|-----|-----------------|
| **Streams** | Live MJPEG view of every configured camera |
| **Settings → Cameras** | Add, edit, or delete cameras; includes a snapshot preview to verify the RTSP URL |
| **Settings → PrusaLink** | Printer host + API key; controls when recording starts and stops |
| **Settings → YouTube** | Enable/disable uploads, set privacy, playlist ID |
| **Settings → Recordings** | Output directory and retention policy |
| **Recordings** | Browse, inspect, and delete completed recordings |
| **Logs** | Live tail of the `prusa-cameras` service journal |

After saving any settings, click **↺ Restart** in the top-right to apply them.

### Via config file (manual)

The config file lives at `/etc/prusa-cameras/config.yaml` and can be edited directly:

```sh
sudo nano /etc/prusa-cameras/config.yaml
sudo systemctl restart prusa-cameras
```

#### 1. Camera tokens

For each camera, you need a token from Prusa Connect:

1. Log in to [connect.prusa3d.com](https://connect.prusa3d.com)
2. Select your printer → **Camera** tab → **Add new other camera**
3. Copy the generated token into the config (or the web UI's Settings → Cameras form)
4. The fingerprint is auto-generated if left blank; or run `python3 scripts/gen_fingerprint.py` to create one manually

#### 2. PrusaLink API key

Find the API key on the printer itself: **Settings → Network → PrusaLink API key**

Set `prusalink.host` to the printer's local IP (e.g. `http://192.168.1.100`).

#### 3. YouTube uploads (optional)

1. Create a Google Cloud project and enable **YouTube Data API v3**
2. Create **OAuth 2.0 credentials** (Desktop application type)
3. Download the JSON file and copy it to the Pi:
   ```sh
   sudo cp client_secrets.json /etc/prusa-cameras/
   ```
4. Run the one-time OAuth flow — this can be done on your laptop if the Pi has no display, then copy the credentials file across:
   ```sh
   python3 scripts/auth_youtube.py --config /etc/prusa-cameras/config.yaml
   # Copy the generated youtube_creds.json to the path set in config.yaml
   ```
5. Enable uploads in the web UI (Settings → YouTube) or set `youtube.enabled: true` in config

## Typical streaming setup

```sh
sudo systemctl stop webcamd
cd mediamtx && ./mediamtx

# Pi camera (libcamera)
libcamera-vid -v 0 -t 0 --codec h264 --inline --libav-format h264 \
  --autofocus-mode auto --gain 1.0 -o - | \
ffmpeg -fflags +genpts+igndts -use_wallclock_as_timestamps 1 \
  -f h264 -i - -vf "transpose=2" -c:v libx264 -preset ultrafast -tune zerolatency \
  -f rtsp rtsp://localhost:8554/mystream

# USB camera
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x720 -i /dev/video8 \
  -vf "transpose=2,format=yuv420p" \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -bsf:v h264_mp4toannexb -pix_fmt yuv420p \
  -f rtsp rtsp://localhost:8554/usbcam
```

## Services

Two systemd services are installed:

| Service | Purpose | Default port |
|---------|---------|-------------|
| `prusa-cameras` | Snapshot sender, print monitor, recorder, YouTube uploader | — |
| `prusa-cameras-web` | Web UI and stream proxy | 8080 |

```sh
# Enable and start both
sudo systemctl enable --now prusa-cameras prusa-cameras-web

# Status
sudo systemctl status prusa-cameras prusa-cameras-web
```

## Project layout

```
src/
  main.py             — camera service entry point
  camera.py           — RTSP → Prusa Connect snapshot loop
  printer_monitor.py  — PrusaLink state poller
  recorder.py         — FFmpeg recording manager
  youtube_uploader.py — YouTube Data API v3 uploader
web/
  app.py              — FastAPI web UI backend
  static/
    index.html        — single-page UI
    style.css         — dark dashboard theme
    app.js            — frontend logic
scripts/
  setup.sh            — installs to /opt/prusa-cameras
  gen_fingerprint.py  — generate a camera fingerprint UUID
  auth_youtube.py     — one-time YouTube OAuth2 flow
systemd/
  prusa-cameras.service
  prusa-cameras-web.service
config.yaml           — template (installed to /etc/prusa-cameras/)
```

## Logs

```sh
# Camera service
sudo journalctl -fu prusa-cameras

# Web UI
sudo journalctl -fu prusa-cameras-web
```

The Logs tab in the web UI shows the camera service journal live.

Set `LOG_LEVEL=DEBUG` in `/etc/systemd/system/prusa-cameras.service` for verbose output, then `sudo systemctl daemon-reload && sudo systemctl restart prusa-cameras`.
