#!/usr/bin/env bash
# Установить рабочий стол NAS как системную службу (автозапуск после перезагрузки,
# от root — чтобы работали SMART, управление контейнерами и питание).
# Запуск:  sudo ./nas-web-install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужен root. Запустите: sudo $0"
  exit 1
fi

# остановить фоновый экземпляр, если запускался вручную
pkill -f "nas-web.py" 2>/dev/null || true
sleep 1

install -m644 "$HERE/nas-web.service" /etc/systemd/system/nas-web.service
systemctl daemon-reload
systemctl enable --now nas-web.service

sleep 1
systemctl --no-pager --lines=3 status nas-web.service || true
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Готово. Рабочий стол:  http://$(hostname).local   (http://${IP})   — порт 80"
