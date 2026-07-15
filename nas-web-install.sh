#!/usr/bin/env bash
# Install the NAS desktop as a system service (autostart after reboot,
# runs as root — so SMART, container management and power all work).
# Run:  sudo ./nas-web-install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${NAS_WEB_PORT:-80}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Root required. Run: sudo $0"
  exit 1
fi

# whoever invoked sudo — for SUDO_USER (the terminal drops root -> this user) and permissions
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] || TARGET_USER="$(logname 2>/dev/null || id -un 1000 2>/dev/null || echo root)"

# stop the background instance if it was started manually
pkill -f "nas-web.py" 2>/dev/null || true
sleep 1

# The unit is generated from the actual location and user (not from a static
# file with a hardcoded path) — so it works from any directory (/opt/nas-os, ~, …).
cat >/etc/systemd/system/nas-web.service <<UNIT
[Unit]
Description=NAS OS web desktop & setup wizard
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=NAS_WEB_PORT=$PORT
Environment=SUDO_USER=$TARGET_USER
WorkingDirectory=$HERE
ExecStart=/usr/bin/python3 $HERE/nas-web.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now nas-web.service

sleep 1
systemctl --no-pager --lines=3 status nas-web.service || true
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Done. Desktop:  http://$(hostname).local   (http://${IP})   — port $PORT"
