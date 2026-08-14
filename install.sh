#!/usr/bin/env bash
# NAS-OS — one-command install on a clean system:
#   curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | sudo bash
#
# Installs the GLOBAL base (packages, docker, directories, preview cache+timer) and the web service.
# Disks/pool/shares/stacks/tuning are configured later in the web wizard (everyone's hardware differs).
set -euo pipefail

REPO="${NASOS_REPO:-https://github.com/pelinoleg/nas-os.git}"
# NASOS_REF pins the install to a tag (or branch): a bare `curl | bash` takes whatever
# main is at that minute — including an edit pushed five minutes ago. A box being set
# up for real should install a version that is KNOWN to work:
#   curl -fsSL .../install.sh | sudo NASOS_REF=v2026.08.04 bash
# NASOS_BRANCH is the old name for the same knob and still works; REF wins when both set.
REF="${NASOS_REF:-${NASOS_BRANCH:-main}}"
DEST="${NASOS_DEST:-/opt/nas-os}"
PORT="${NAS_WEB_PORT:-80}"

say(){ printf '\033[36m▸ %s\033[0m\n' "$*"; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Root required. Run:  curl -fsSL .../install.sh | sudo bash"

# --- OS check: NAS-OS targets Debian on x86 ---------------------------------
# On Ubuntu the Docker CE repo falls back to Debian packages and nothing here is
# tested. Warn BEFORE anything is installed, while reinstalling the OS is cheap.
# Skip the question with NASOS_FORCE_OS=1 (e.g. unattended installs on a derivative).
OS_ID="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}")"
OS_NAME="$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-$OS_ID}")"
case "$OS_ID" in
  debian) ;;
  *)
    printf '\033[33;1m'
    echo "⚠  This system is: $OS_NAME"
    echo "   NAS-OS is built and tested for Debian on x86 only."
    echo "   On Ubuntu (or other distros) Docker gets Debian's packages and the"
    echo "   setup is untested — things may break now or after an OS upgrade."
    echo "   Best move: stop now and reinstall the box with Debian."
    printf '\033[0m\n'
    if [ "${NASOS_FORCE_OS:-0}" != "1" ]; then
      # stdin is the piped script itself (curl | bash) — ask on the terminal directly
      if [ -r /dev/tty ]; then
        printf 'Continue anyway on %s? [y/N] ' "$OS_ID"
        read -r ans </dev/tty || ans=""
        case "$ans" in y|Y|yes|YES) say "Continuing on $OS_ID (unsupported)…" ;;
                       *) die "Aborted — install Debian and re-run this script." ;;
        esac
      else
        say "No terminal to ask on — continuing in 15 s, press Ctrl+C to abort…"
        sleep 15
      fi
    fi
    ;;
esac

# whoever invoked sudo — used for permissions, the config home folder and the terminal (drop root→this user)
TARGET_USER="${SUDO_USER:-}"
[ -n "$TARGET_USER" ] || TARGET_USER="$(logname 2>/dev/null || id -un 1000 2>/dev/null || echo root)"
say "NAS user: $TARGET_USER    directory: $DEST"

say "Base packages for the install (git)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends git ca-certificates >/dev/null

if [ -d "$DEST/.git" ]; then
  say "Updating $DEST from git ($REF)…"
  # FETCH_HEAD instead of origin/$REF: a tag fetched shallowly has no remote-tracking
  # ref, so "origin/v2026.08.04" would not exist — FETCH_HEAD always does
  git -C "$DEST" fetch --depth 1 origin "$REF" && git -C "$DEST" reset --hard FETCH_HEAD
elif [ -f "$DEST/nas-web.py" ]; then
  say "Using existing files in $DEST (no git)"
else
  say "Cloning $REPO ($REF) → $DEST…"
  rm -rf "$DEST"
  # -b accepts both branches and tags
  git clone --depth 1 -b "$REF" "$REPO" "$DEST"
fi
chmod +x "$DEST/nas-wizard.sh" 2>/dev/null || true
chmod +x "$DEST/tools/update-ai-cli.sh" 2>/dev/null || true

# --- global system stage of the wizard (packages/docker/directories/preview cache+timer) ---
say "System setup (packages, docker, ffmpeg, preview cache…) — this may take a while…"
SUDO_USER="$TARGET_USER" bash "$DEST/nas-wizard.sh" api system

# --- systemd service for the web desktop (root: SMART/docker/power/PTY), paths and user are current ---
say "Installing the nas-web service (port $PORT)…"
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

# --- Claude Code + Codex CLI: install when absent, update daily ----------------
# Both CLIs keep credentials and binaries in the NAS user's home, so never run
# their installers as root. The randomized delay avoids every NAS downloading at
# exactly the same minute; Persistent catches up after the machine was powered off.
say "Enabling automatic Claude Code and Codex CLI updates for $TARGET_USER…"
install -m 0755 "$DEST/tools/update-ai-cli.sh" /usr/local/libexec/nas-ai-cli-update
install -m 0644 "$DEST/systemd/nas-ai-cli-update@.service" /etc/systemd/system/
install -m 0644 "$DEST/systemd/nas-ai-cli-update@.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "nas-ai-cli-update@$TARGET_USER.timer"

sleep 1
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="$(hostname 2>/dev/null || echo nas)"
echo
if systemctl is-active --quiet nas-web.service; then
  printf '\033[32m✔ NAS-OS is ready.\033[0m  Desktop:  http://%s.local' "$HOST"
  [ -n "$IP" ] && printf '   (http://%s)' "$IP"
  [ "$PORT" != "80" ] && printf '   port %s' "$PORT"
  echo; echo "  Next — open it in a browser and go through the wizard (Disks → Pool → Access → Security)."
else
  die "Service failed to start. Check: journalctl -u nas-web -n 50"
fi
