#!/usr/bin/env bash
# Установить рабочий стол NAS как системную службу (автозапуск после перезагрузки,
# от root — чтобы работали SMART, управление контейнерами и питание).
# Запуск:  sudo ./nas-web-install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${NAS_WEB_PORT:-80}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужен root. Запустите: sudo $0"
  exit 1
fi

# кто вызвал sudo — для SUDO_USER (терминал роняет root -> этот юзер) и прав
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] || TARGET_USER="$(logname 2>/dev/null || id -un 1000 2>/dev/null || echo root)"

# остановить фоновый экземпляр, если запускался вручную
pkill -f "nas-web.py" 2>/dev/null || true
sleep 1

# Юнит генерируем из фактического расположения и пользователя (а не из статического
# файла с захардкоженным путём) — чтобы работал из любого каталога (/opt/nas-os, ~, …).
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
echo "Готово. Рабочий стол:  http://$(hostname).local   (http://${IP})   — порт $PORT"
