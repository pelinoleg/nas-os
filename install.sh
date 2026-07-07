#!/usr/bin/env bash
# NAS-OS — установка с чистой системы одной командой:
#   curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | sudo bash
#
# Ставит ГЛОБАЛЬНУЮ базу (пакеты, docker, каталоги, кэш превью+таймер) и веб-службу.
# Диски/пул/шары/стеки/тюнинг настраиваются потом в веб-мастере (у всех железо разное).
set -euo pipefail

REPO="${NASOS_REPO:-https://github.com/pelinoleg/nas-os.git}"
BRANCH="${NASOS_BRANCH:-main}"
DEST="${NASOS_DEST:-/opt/nas-os}"
PORT="${NAS_WEB_PORT:-80}"

say(){ printf '\033[36m▸ %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Нужен root. Запусти:  curl -fsSL .../install.sh | sudo bash"

# кто вызвал sudo — для прав, домашней папки конфига и терминала (роняем root→этот юзер)
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] || TARGET_USER="$(logname 2>/dev/null || id -un 1000 2>/dev/null || echo root)"
say "Пользователь NAS: $TARGET_USER    каталог: $DEST"

say "Базовые пакеты для установки (git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends git ca-certificates >/dev/null

if [ -d "$DEST/.git" ]; then
  say "Обновляю $DEST из git…"
  git -C "$DEST" fetch --depth 1 origin "$BRANCH" && git -C "$DEST" reset --hard "origin/$BRANCH"
elif [ -f "$DEST/nas-web.py" ]; then
  say "Использую существующие файлы в $DEST (без git)"
else
  say "Клонирую $REPO → $DEST…"
  rm -rf "$DEST"
  git clone --depth 1 -b "$BRANCH" "$REPO" "$DEST"
fi
chmod +x "$DEST/nas-wizard.sh" 2>/dev/null || true

# --- глобальный системный этап визарда (пакеты/docker/каталоги/кэш превью+таймер) ---
say "Системная подготовка (пакеты, docker, ffmpeg, кэш превью…) — это может занять время…"
SUDO_USER="$TARGET_USER" bash "$DEST/nas-wizard.sh" api system

# --- systemd-служба веб-стола (root: SMART/docker/питание/PTY), пути и юзер — актуальные ---
say "Установка службы nas-web (порт $PORT)…"
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
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/nas-web.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now nas-web.service

sleep 1
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="$(hostname 2>/dev/null || echo nas)"
echo
if systemctl is-active --quiet nas-web.service; then
  printf '\033[32m✔ NAS-OS готов.\033[0m  Рабочий стол:  http://%s.local' "$HOST"
  [ -n "$IP" ] && printf '   (http://%s)' "$IP"
  [ "$PORT" != "80" ] && printf '   порт %s' "$PORT"
  echo; echo "  Дальше — открой в браузере и пройди мастер (Диски → Шары → Приложения)."
else
  die "Служба не запустилась. Смотри: journalctl -u nas-web -n 50"
fi
