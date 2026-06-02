#!/usr/bin/env bash
# Installs the Prusa Connect camera service on a Raspberry Pi.
# Run as a regular user with sudo privileges.
set -euo pipefail

INSTALL_DIR="/opt/prusa-cameras"
CONFIG_DIR="/etc/prusa-cameras"
DATA_DIR="/var/lib/prusa-cameras"
SERVICE_USER="${SERVICE_USER:-pi}"

echo "==> Installing system dependencies"
sudo apt-get update -q
sudo apt-get install -y ffmpeg python3-pip python3-venv

echo "==> Creating directories"
sudo mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR/recordings"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/recordings"

echo "==> Copying files"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
sudo rsync -a --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$REPO_DIR/" "$INSTALL_DIR/"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Installing Python dependencies"
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "==> Setting up config"
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
    sudo cp "$INSTALL_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR/config.yaml"
    echo "    Default config installed at $CONFIG_DIR/config.yaml"
fi

echo "==> Configuring sudoers (allows web UI to restart the camera service)"
SUDOERS_LINE="$SERVICE_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart prusa-cameras"
SUDOERS_FILE="/etc/sudoers.d/prusa-cameras"
if ! sudo grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE" 2>/dev/null; then
    echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 0440 "$SUDOERS_FILE"
    echo "    Sudoers rule written to $SUDOERS_FILE"
fi

echo "==> Installing systemd services"
sudo cp "$INSTALL_DIR/systemd/prusa-cameras.service"     /etc/systemd/system/
sudo cp "$INSTALL_DIR/systemd/prusa-cameras-web.service" /etc/systemd/system/
sudo systemctl daemon-reload

PI_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "================================================================"
echo "  Installation complete!"
echo "================================================================"
echo ""
echo "QUICK START (via web UI):"
echo "  1. sudo systemctl enable --now prusa-cameras-web"
echo "  2. Open http://${PI_IP}:8080 in your browser"
echo "  3. Add cameras in the Settings tab"
echo "  4. Click 'Restart service' in the UI"
echo ""
echo "MANUAL CONFIG:"
echo "  sudo nano $CONFIG_DIR/config.yaml"
echo ""
echo "YOUTUBE SETUP (optional):"
echo "  sudo cp client_secrets.json $CONFIG_DIR/"
echo "  sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/python $INSTALL_DIR/scripts/auth_youtube.py"
echo ""
echo "ENABLE BOTH SERVICES:"
echo "  sudo systemctl enable --now prusa-cameras prusa-cameras-web"
echo ""
echo "LOGS:"
echo "  sudo journalctl -fu prusa-cameras"
echo "  sudo journalctl -fu prusa-cameras-web"
