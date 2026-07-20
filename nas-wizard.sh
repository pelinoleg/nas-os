#!/usr/bin/env bash
#
# nas-wizard.sh — NAS setup wizard for Raspberry Pi 5
#
# Implemented stages (per spec):
#   1.  System preparation (NAS stack + utilities + Pi packages, docker,
#       groups, fstrim, directories, hostname/tz)
#   2.  Disk handling (format -> fstab -> mount), data OR parity
#   2b. mergerfs — pool of >=2 data disks (auto when a 2nd disk is added)
#   3.  SnapRAID — snapraid.conf, sync with mass-delete protection,
#       systemd timers (sync daily / scrub weekly), notifications
#   4.  Docker — reads ./services/<service>/*.yml NEXT TO THE SCRIPT, checklist
#       "which to bring up", up/down, generates deploy.sh ("apply everything at once")
#   5.  Pi tuning — PCIe Gen3, USB max current, memory cgroup, sysctl, zram,
#       watchdog, EEPROM, Wi-Fi powersave, temp/throttle (opt-in checklist)
#   6.  Security — unattended-upgrades, journald cap, log2ram, ufw, fail2ban,
#       SSH key-only (safe: only when keys are present)
#   7.  Network shares — Samba / NFS to /mnt/storage + Avahi (mDNS)
#   8.  Backups/monitoring — smartd alerts, health timer (disk/temperature),
#       Notifications via Pushover. Plus api mode for the web UI (nas-web.py).
#
# Principles: idempotency, --dry-run, logging, confirmation of destructive
# operations, fstab backup, system disk protection, config versioning in git.
#
# Usage:
#   sudo ./nas-wizard.sh                   # interactive menu
#   sudo ./nas-wizard.sh --dry-run         # changes nothing, prints the action plan
#   sudo ./nas-wizard.sh --stage snapraid  # run only one stage
#     (stages: system | disk | mergerfs | snapraid | docker)
#
set -o pipefail

# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------
DRY_RUN=0
FORCE_STAGE=""
LOG="/var/log/nas-wizard.log"

# Directory of the script itself (docker services live next to the script, in ./services/)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" 2>/dev/null && pwd || echo "$PWD")"
SERVICES_SRC="$SCRIPT_DIR/services"

# --- Packages (we don't install whiptail — it's needed for this script to work at all) ---
# NAS stack
# docker-ce/compose-plugin are installed separately from the official Docker repo (see ensure_docker_repo) —
# they aren't in the Debian/RPi OS repos. Here only packages available in the stock repositories.
STACK_PACKAGES=(mergerfs snapraid smartmontools)
# General-purpose utilities — what a server/NAS almost always needs
UTIL_PACKAGES=(
  vnstat              # per-interface traffic counter («Traffic» widget in the panel)
  sshfs               # «Servers» in the panel file manager: SSH mounts in /mnt/remote
  dialog
  libheif-examples   # heif-convert: iPhone HEIC is sliced into tiles, ffmpeg takes only one
  eject              # soft media ejection after USB import (power-off kills the whole reader)
  iputils-arping     # nas-netguard: fallback gateway check when it stays silent on ICMP
  curl wget ca-certificates gnupg
  git rsync sshpass
  vim nano
  htop iotop
  tmux screen
  tree ncdu
  jq unzip zip p7zip-full
  lsof net-tools bind9-dnsutils iproute2 nmap
  bash-completion
  parted gdisk dosfstools e2fsprogs xfsprogs exfatprogs ntfs-3g btrfs-progs udisks2
  hdparm nvme-cli sysstat
  unattended-upgrades apt-listchanges
  ffmpeg poppler-utils
)
# Pi-specific
PI_PACKAGES=(libraspberrypi-bin raspi-config rpi-eeprom)

# Mount points / directories
STORAGE_MNT="/mnt/storage"
DOCKER_ROOT="/opt/docker"          # container configs: /opt/docker/<service>/

# The user we set things up for (not root)
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="$(id -un 1000 2>/dev/null || echo "root")"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -z "$TARGET_HOME" ] && TARGET_HOME="/home/$TARGET_USER"
NAS_CONFIG="$TARGET_HOME/nas-config"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
nas-wizard.sh — NAS setup on Raspberry Pi 5

  --dry-run           Print commands, change nothing
  --stage system      Stage 1: system preparation
  --stage disk        Stage 2: disk handling (format/fstab/mount)
  --stage mergerfs    Stage 2b: build/update the mergerfs pool
  --stage snapraid    Stage 3: SnapRAID (conf, sync, timers)
  --stage docker      Stage 4: Docker (find compose folders and bring up)
  --stage pi          Stage 5: Pi tuning (PCIe, USB power, watchdog)
  --stage security    Stage 6: Security (ufw, fail2ban, SSH, journald)
  --stage shares      Stage 7: Network shares (Samba/NFS/Avahi)
  --stage backup      Stage 8: Backups and monitoring (SMART, health)
  -h, --help          This help
EOF
}

# Headless API mode for the web UI (nas-web.py): `nas-wizard.sh api <action>`.
# Parameters arrive via NASW_* environment variables, dry-run via NASW_DRYRUN=1.
API_ACTION=""
if [ "${1:-}" = "api" ]; then
    API_ACTION="${2:-}"
    shift 2 2>/dev/null || shift $#
fi
[ "${NASW_DRYRUN:-0}" = "1" ] && DRY_RUN=1

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --stage)   FORCE_STAGE="$2"; shift ;;
        --stage=*) FORCE_STAGE="${1#*=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    # Write to the log (and to stderr in dry-run for visibility)
    local msg="$*"
    { printf '%s [%s] %s\n' "$(ts)" "$([ "$DRY_RUN" -eq 1 ] && echo DRY || echo RUN)" "$msg" >>"$LOG"; } 2>/dev/null
}

info()  { echo "  $*"; log "INFO: $*"; }
warn()  { echo "  ! $*" >&2; log "WARN: $*"; }
die()   { echo "  ERROR: $*" >&2; log "ERROR: $*"; exit 1; }

# run — wrapper for MUTATING commands (mkfs, mount, systemctl, apt, mkdir ...).
# Read-only commands (lsblk, blkid, df, findmnt) we call directly — they don't need dry-run.
run() {
    log "CMD: $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] %s\n' "$*"
        return 0
    fi
    "$@" >>"$LOG" 2>&1
    local rc=$?
    if [ $rc -ne 0 ]; then
        warn "command exited with code $rc: $*"
    fi
    return $rc
}

# remove_fstab_mount — remove from /etc/fstab the lines mounting to a given point
# (except comments); needed before adding a new line with a new UUID.
remove_fstab_mount() {
    local mp="$1"
    [ -n "$mp" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] remove old fstab lines for %s\n' "$mp"
        return 0
    fi
    [ -f /etc/fstab ] || return 0
    awk -v mp="$mp" '$1 ~ /^#/ || $2 != mp' /etc/fstab > /etc/fstab.nastmp \
        && cat /etc/fstab.nastmp > /etc/fstab && rm -f /etc/fstab.nastmp
}

# append_line — idempotently append a line to a file (for fstab etc.)
append_line() {
    local line="$1" file="$2"
    if [ -f "$file" ] && grep -qsF "$line" "$file"; then
        info "already present in $file, skipping"
        return 0
    fi
    log "APPEND -> $file : $line"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] echo %q >> %s\n' "$line" "$file"
        return 0
    fi
    # ensure a trailing newline at end of file, otherwise the new line sticks to the last one
    # (corrupts the previous fstab entry → possible boot failure)
    if [ -s "$file" ] && [ -n "$(tail -c1 "$file")" ]; then
        printf '\n' >>"$file"
    fi
    printf '%s\n' "$line" >>"$file"
}

# run_as — run a command as TARGET_USER (for git in their home directory)
run_as() {
    log "CMD(as $TARGET_USER): $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] sudo -u %s %s\n' "$TARGET_USER" "$*"
        return 0
    fi
    sudo -u "$TARGET_USER" "$@" >>"$LOG" 2>&1
}

# run_visible — like run(), but output is visible to the user (for long ops: snapraid sync)
run_visible() {
    log "CMD(visible): $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] %s\n' "$*"
        return 0
    fi
    "$@" 2>&1 | tee -a "$LOG"
    return "${PIPESTATUS[0]}"
}

# write_file — write a whole file (content from stdin). Respects dry-run, backs up the existing one.
write_file() {
    local path="$1" content
    content="$(cat)"
    log "WRITE -> $path ($(printf '%s' "$content" | wc -l) lines)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] write file %s (%s lines)\n' "$path" "$(printf '%s\n' "$content" | wc -l)"
        return 0
    fi
    if [ -f "$path" ]; then
        cp -a "$path" "${path}.bak.$(date '+%Y%m%d-%H%M%S')"
    fi
    printf '%s\n' "$content" > "$path"
}

# commit_config — take a snapshot of key configs into the git repository and commit
commit_config() {
    local msg="$1"
    [ -d "$NAS_CONFIG/.git" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] git commit configs: $msg"
        return 0
    fi
    [ -f /etc/fstab ]          && cp -a /etc/fstab          "$NAS_CONFIG/fstab.snapshot"
    [ -f /etc/snapraid.conf ]  && cp -a /etc/snapraid.conf  "$NAS_CONFIG/snapraid.conf"
    chown -R "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG" 2>/dev/null || true
    run_as git -C "$NAS_CONFIG" add -A
    run_as git -C "$NAS_CONFIG" -c user.email="nas@localhost" -c user.name="nas-wizard" commit -q -m "$msg" || true
}

# docker_compose_cmd — determine which compose is available ("docker compose" | "docker-compose")
docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        echo "docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo "docker-compose"
    else
        echo ""
    fi
}

# install_packages <label> pkg...  — idempotent; skips already installed and repo-unavailable ones
install_packages() {
    local label="$1"; shift
    local to_install=() pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 && continue
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            to_install+=("$pkg")
        else
            warn "$label: package not available in the repository, skipping: $pkg"
        fi
    done
    if [ "${#to_install[@]}" -eq 0 ]; then
        info "$label: everything already installed"
        return 0
    fi
    info "$label: installing (${#to_install[@]}): ${to_install[*]}"
    run apt-get install -y "${to_install[@]}"
}

# ---------------------------------------------------------------------------
# ensure_docker_repo — hook up the official Docker CE repository and install the engine.
# Why: docker-compose-plugin (v2, «docker compose») and docker-ce are NOT in the
# Debian/Raspberry Pi OS repositories — they live only on download.docker.com. Without this repo
# docker_compose_cmd is empty → Stage 4, Dockge, deploy.sh, nas-stacks.service — no-op on
# a clean machine. Idempotent: a repeat run only recreates what's missing.
# ---------------------------------------------------------------------------
ensure_docker_repo() {
    local keyring=/etc/apt/keyrings/docker.asc
    local list=/etc/apt/sources.list.d/docker.list
    local arch codename
    arch="$(dpkg --print-architecture)"
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"

    # curl + ca-certificates are needed to download the GPG key (usually already present on RPi OS)
    install_packages "Docker: dependencies" ca-certificates curl

    # Docker doesn't publish every Debian release right away. If there's no repository
    # for our codename yet — fall back to bookworm (compatible binary).
    if ! curl -fsS --max-time 10 -o /dev/null "https://download.docker.com/linux/debian/dists/${codename}/Release" 2>/dev/null; then
        warn "Docker: repository for '$codename' not available yet, using bookworm"
        codename="bookworm"
    fi

    run install -m 0755 -d /etc/apt/keyrings
    if [ ! -s "$keyring" ]; then
        run curl -fsSL https://download.docker.com/linux/debian/gpg -o "$keyring"
        run chmod a+r "$keyring"
    fi

    # rewrite the source only if it changed (e.g. the codename changed)
    local want="deb [arch=${arch} signed-by=${keyring}] https://download.docker.com/linux/debian ${codename} stable"
    if [ "$(cat "$list" 2>/dev/null)" != "$want" ]; then
        printf '%s\n' "$want" | write_file "$list"
        run apt-get update
    fi

    # remove conflicting distro packages (absent on a clean machine — no-op)
    local p present=()
    for p in docker.io docker-compose docker-doc podman-docker containerd runc; do
        dpkg -s "$p" >/dev/null 2>&1 && present+=("$p")
    done
    [ "${#present[@]}" -gt 0 ] && run apt-get remove -y "${present[@]}"

    install_packages "Docker CE" docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

# ---------------------------------------------------------------------------
# ensure_gh — GitHub CLI (gh) from the official repository cli.github.com.
# gh is NOT in the Debian/Raspberry Pi OS repositories — it needs its own source.
# Handy for pushing panel code to github.com/pelinoleg/nas-os straight
# from the box. Idempotent: if gh is already installed — exit right away.
# ---------------------------------------------------------------------------
ensure_gh() {
    command -v gh >/dev/null 2>&1 && { info "gh already installed ($(gh --version 2>/dev/null | head -1))"; return 0; }
    local keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
    local list=/etc/apt/sources.list.d/github-cli.list
    local arch; arch="$(dpkg --print-architecture)"
    install_packages "gh: dependencies" ca-certificates curl
    run install -m 0755 -d /etc/apt/keyrings
    if [ ! -s "$keyring" ]; then
        run curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o "$keyring"
        run chmod go+r "$keyring"
    fi
    local want="deb [arch=${arch} signed-by=${keyring}] https://cli.github.com/packages stable main"
    if [ "$(cat "$list" 2>/dev/null)" != "$want" ]; then
        printf '%s\n' "$want" | write_file "$list"
        run apt-get update
    fi
    install_packages "GitHub CLI" gh
}

# ---------------------------------------------------------------------------
# UI wrappers — dialog backend (richer, themeable) with a fallback to whiptail
# ---------------------------------------------------------------------------
UI_BIN="whiptail"     # the real value is set by ui_init()
UI_OPTS=()            # extra backend options (e.g. --colors for dialog)

# ui_init — pick the backend and apply the dark theme. Called from main().
ui_init() {
    if command -v dialog >/dev/null 2>&1; then
        UI_BIN="dialog"
        UI_OPTS=(--colors)
        # theme: dialogrc-nas file next to the script (if present)
        [ -f "$SCRIPT_DIR/dialogrc-nas" ] && export DIALOGRC="$SCRIPT_DIR/dialogrc-nas"
    elif command -v whiptail >/dev/null 2>&1; then
        UI_BIN="whiptail"
        UI_OPTS=()
        # dark theme for whiptail (newt)
        export NEWT_COLORS='
root=,black
window=,black
border=white,black
title=brightcyan,black
button=black,cyan
actbutton=black,brightcyan
listbox=white,black
actlistbox=black,cyan
checkbox=white,black
actcheckbox=black,cyan
entry=brightwhite,black
textbox=white,black
label=brightcyan,black'
    else
        die "neither dialog nor whiptail found (apt install whiptail)"
    fi
    log "UI backend: $UI_BIN"
}

ui_menu()      { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --menu "$2" 20 78 10 "${@:3}" 3>&1 1>&2 2>&3; }
ui_input()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --inputbox "$2" 12 78 "$3" 3>&1 1>&2 2>&3; }
ui_password()  { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --passwordbox "$2" 12 78 3>&1 1>&2 2>&3; }
ui_yesno()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --yesno "$2" 18 78; }   # 0 = Yes
ui_msg()       { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --msgbox "$2" 20 78; }
ui_checklist() { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --checklist "$2" 20 78 10 "${@:3}" 3>&1 1>&2 2>&3; }
# ui_gauge — reads percent (0..100) from stdin: <cmd> | ui_gauge "Title" "Text"
ui_gauge()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --gauge "$2" 8 78 0; }

# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------
require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "root privileges required. Run: sudo $0 $*"
    fi
}

ensure_log() {
    if [ "$DRY_RUN" -eq 0 ]; then
        touch "$LOG" 2>/dev/null || die "cannot write to $LOG"
        chmod 640 "$LOG" 2>/dev/null || true
    fi
    log "===== nas-wizard start (dry_run=$DRY_RUN, stage='${FORCE_STAGE:-menu}', user=$TARGET_USER) ====="
}

# ---------------------------------------------------------------------------
# Disk handling: identifying system/protected devices
# ---------------------------------------------------------------------------

# Parent disk for a mount point (for example / -> mmcblk0)
disk_of_mountpoint() {
    local mp="$1" src pk mm
    src="$(findmnt -no SOURCE "$mp" 2>/dev/null)" || return 1
    [ -z "$src" ] && return 1
    # findmnt may return a pseudo-source (/dev/root) — resolve to the real
    # device via MAJ:MIN, otherwise the system disk drops out of protection
    if [ ! -b "$src" ] || [ "$src" = "/dev/root" ]; then
        mm="$(findmnt -no MAJ:MIN "$mp" 2>/dev/null | head -1)"
        [ -n "$mm" ] && [ -e "/dev/block/$mm" ] || return 1
        src="$(realpath "/dev/block/$mm" 2>/dev/null)" || return 1
    fi
    pk="$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)"
    if [ -n "$pk" ]; then
        echo "/dev/$pk"
    else
        # src may already be a whole disk
        echo "$src"
    fi
}

# List of protected disks (system). Returns /dev/xxx lines
protected_disks() {
    local mp d src pk
    {
        for mp in / /boot /boot/firmware /home /var; do
            d="$(disk_of_mountpoint "$mp" 2>/dev/null)"
            [ -n "$d" ] && echo "$d"
        done
        # a disk with an active swap partition is also protected
        while read -r src _; do
            [ -b "$src" ] || continue
            case "$src" in /dev/zram*) continue ;; esac
            pk="$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)"
            if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
        done < <(tail -n +2 /proc/swaps 2>/dev/null)
    } | grep -v '^$' | sort -u
}

# Disk busy? (itself or any of its partitions mounted somewhere)
disk_in_use() {
    local dev="$1" mps
    mps="$(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -c . )"
    [ "$mps" -gt 0 ]
}

# Who holds the device mounted (process names) — for an honest "busy" message
# instead of a bare umount error. Never kills anything.
dev_holders() {
    local dev="$1" mp out=""
    while read -r mp; do
        [ -n "$mp" ] || continue
        out="$out $(fuser -m "$mp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' \
                    | while read -r p; do ps -o comm= -p "$p" 2>/dev/null; done | sort -u | tr '\n' ' ')"
    done < <(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep .)
    out="$(echo "$out" | tr -s ' ')"
    [ -n "${out// /}" ] && echo "$out" || echo "unknown"
}

# Unmount all mount points of the device and its partitions. No lazy umount: a lazy umount
# leaves open files, and mkfs comes next — that way data can be lost silently.
unmount_dev() {
    local dev="$1" mp rc=0
    while read -r mp; do
        [ -n "$mp" ] || continue
        if ! run umount "$mp"; then
            sleep 2                      # give it time to release (indexer/automounter)
            run umount "$mp" || rc=1
        fi
    done < <(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep .)
    return $rc
}

is_protected() {
    local dev="$1" p
    while read -r p; do
        [ "$dev" = "$p" ] && return 0
    done < <(protected_disks)
    return 1
}

# Build the list of candidate disks (not system, not mounted, size > 0)
# Prints: DEV<TAB>SIZE<TAB>MODEL
candidate_disks() {
    local dev size model type sizebytes
    while read -r dev type sizebytes; do
        [ "$type" = "disk" ] || continue
        # skip zram/loop
        case "$dev" in
            /dev/zram*|/dev/loop*) continue ;;
        esac
        [ "${sizebytes:-0}" -gt 0 ] 2>/dev/null || continue
        is_protected "$dev" && continue
        disk_in_use "$dev" && continue
        size="$(lsblk -dno SIZE "$dev" 2>/dev/null | tr -d ' ')"
        model="$(lsblk -dno MODEL "$dev" 2>/dev/null | sed 's/[[:space:]]*$//')"
        [ -z "$model" ] && model="(no model)"
        printf '%s\t%s\t%s\n' "$dev" "$size" "$model"
    done < <(lsblk -dpno NAME,TYPE,SIZE -b 2>/dev/null)
}

# Disk info for confirmation
disk_info_block() {
    local dev="$1"
    echo "Device     : $dev"
    echo "Model      : $(lsblk -dno MODEL "$dev" 2>/dev/null | sed 's/[[:space:]]*$//')"
    echo "Serial     : $(lsblk -dno SERIAL "$dev" 2>/dev/null)"
    echo "Size       : $(lsblk -dno SIZE "$dev" 2>/dev/null | tr -d ' ')"
    local mps
    mps="$(lsblk -nro NAME,MOUNTPOINT "$dev" 2>/dev/null | awk 'NF>1{print "  "$1" -> "$2}')"
    if [ -n "$mps" ]; then
        echo "Mounted:"
        echo "$mps"
    else
        echo "Mounted: no"
    fi
}

# Next free number for /mnt/diskN (in practice: dir/fstab)
next_disk_number() {
    local n=1
    while grep -qsE "[[:space:]]/mnt/disk${n}[[:space:]]" /etc/fstab || { [ -d "/mnt/disk${n}" ] && [ -n "$(ls -A "/mnt/disk${n}" 2>/dev/null)" ]; }; do
        n=$((n+1))
    done
    echo "$n"
}

next_parity_number() {
    local n=1
    while grep -qsE "[[:space:]]/mnt/parity${n}[[:space:]]" /etc/fstab || { [ -d "/mnt/parity${n}" ] && [ -n "$(ls -A "/mnt/parity${n}" 2>/dev/null)" ]; }; do
        n=$((n+1))
    done
    echo "$n"
}

# Size in bytes of the largest DATA disk (by mount /mnt/disk*)
largest_data_disk_bytes() {
    local max=0 mp src b
    for mp in /mnt/disk*; do
        [ -d "$mp" ] || continue
        src="$(findmnt -no SOURCE "$mp" 2>/dev/null)" || continue
        [ -z "$src" ] && continue
        b="$(lsblk -bdno SIZE "$src" 2>/dev/null | head -1)"
        [ "${b:-0}" -gt "$max" ] 2>/dev/null && max="$b"
    done
    echo "$max"
}

# Mounted data / parity disks (one path per line, natural sort)
mounted_data_disks() {
    local m
    for m in $(ls -d /mnt/disk* 2>/dev/null | sort -V); do
        findmnt -no TARGET "$m" >/dev/null 2>&1 && echo "$m"
    done
}
mounted_parity_disks() {
    local m
    for m in $(ls -d /mnt/parity* 2>/dev/null | sort -V); do
        findmnt -no TARGET "$m" >/dev/null 2>&1 && echo "$m"
    done
}

# ---------------------------------------------------------------------------
# fstab backup
# ---------------------------------------------------------------------------
backup_fstab() {
    local stamp bak
    stamp="$(date '+%Y%m%d-%H%M%S')"
    bak="/etc/fstab.bak.${stamp}"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] backup /etc/fstab -> $bak"
        return 0
    fi
    cp -a /etc/fstab "$bak" && info "fstab backup: $bak"
    # a copy of the snapshot into the config git repository as well
    if [ -d "$NAS_CONFIG" ]; then
        run_as cp -a /etc/fstab "$NAS_CONFIG/fstab.snapshot"
    fi
}

# ---------------------------------------------------------------------------
# STAGE 0: system preparation
# ---------------------------------------------------------------------------
stage_system() {
    echo
    echo "=== Stage 0: system preparation ==="
    log "--- stage_system start ---"

    # 0.1 apt update / full-upgrade (with consent)
    if ui_yesno "System update" "Run apt update && apt full-upgrade?\n\nMay take a while. Recommended on first setup."; then
        run apt-get update
        run apt-get full-upgrade -y
    else
        info "system update skipped"
    fi

    # 0.2 software install: NAS stack + utilities + Pi-specific (idempotent, unavailable ones skipped)
    run apt-get update
    install_packages "NAS stack"   "${STACK_PACKAGES[@]}"
    install_smartd_guard   # smartmontools is installed right here — immediately clear failed with no disks
    install_screen         # local touch screen: installed only if the panel is actually connected
    install_packages "utilities"    "${UTIL_PACKAGES[@]}"
    install_packages "Pi packages"  "${PI_PACKAGES[@]}"
    ensure_docker_repo   # docker-ce + compose-plugin from the official Docker repo
    ensure_gh            # GitHub CLI (for pushing panel code from the box)

    # 0.3 enable/start services (idempotent)
    local svc
    for svc in docker; do
        if systemctl is-enabled "$svc" >/dev/null 2>&1; then
            info "service already enabled: $svc"
        else
            run systemctl enable "$svc"
        fi
        if systemctl is-active "$svc" >/dev/null 2>&1; then
            info "service already running: $svc"
        else
            run systemctl start "$svc"
        fi
    done

    # 0.3b TRIM for SSD/NVMe (weekly) — reduces wear and keeps speed
    if systemctl list-unit-files fstrim.timer >/dev/null 2>&1; then
        if systemctl is-enabled fstrim.timer >/dev/null 2>&1; then
            info "fstrim.timer already enabled"
        else
            run systemctl enable --now fstrim.timer
        fi
    fi

    # 0.4 add the user to the docker group
    if id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        info "user $TARGET_USER already in the docker group"
    else
        run usermod -aG docker "$TARGET_USER"
        info "user $TARGET_USER added to the docker group (relogin required)"
    fi

    # 0.5 network interface check (detected dynamically)
    local iface
    iface="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
    if [ -z "$iface" ]; then
        warn "could not determine the default network interface"
    elif [ "$iface" = "end0" ]; then
        info "network interface: end0 (standard for Pi5/Bookworm)"
    else
        warn "default network interface: '$iface' (expected end0 on Pi5/Bookworm)."
        ui_msg "Network" "Primary interface: $iface\n\nOn Raspberry Pi 5 / Bookworm the wired one is usually named end0. If you use Wi-Fi ('$iface' looks like a wireless one) — note that a stable wired link is preferred for a NAS.\n\nThe interface name isn't hardcoded anywhere — just a warning."
    fi

    # 0.6 directory structure
    info "creating directory structure"
    run mkdir -p "$STORAGE_MNT" "$DOCKER_ROOT" "$SERVICES_SRC"
    # nas-config — in the user's home, a git repository
    if [ ! -d "$NAS_CONFIG" ]; then
        run mkdir -p "$NAS_CONFIG/scripts"
        run chown -R "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG"
    fi
    # git init + first commit
    if [ ! -d "$NAS_CONFIG/.git" ]; then
        run_as git -C "$NAS_CONFIG" init -q
        if [ "$DRY_RUN" -eq 0 ]; then
            if [ ! -f "$NAS_CONFIG/README.md" ]; then
                printf '# nas-config\n\nVersioned NAS configs (fstab snippets, snapraid.conf, docker-compose).\nGenerated by nas-wizard.sh.\n' \
                    | sudo -u "$TARGET_USER" tee "$NAS_CONFIG/README.md" >/dev/null
            fi
            run_as git -C "$NAS_CONFIG" add -A
            run_as git -C "$NAS_CONFIG" -c user.email="nas@localhost" -c user.name="nas-wizard" commit -q -m "init nas-config" || true
        else
            info "[DRY-RUN] git init + first commit in $NAS_CONFIG"
        fi
    else
        info "git repository already exists: $NAS_CONFIG"
    fi

    # 0.7 hostname / timezone
    local cur_host cur_tz
    cur_host="$(hostnamectl --static 2>/dev/null || hostname)"
    cur_tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
    if ui_yesno "Hostname" "Current hostname: $cur_host\n\nChange it?"; then
        local newhost
        newhost="$(ui_input "Hostname" "New host name:" "$cur_host")" && \
            [ -n "$newhost" ] && [ "$newhost" != "$cur_host" ] && run hostnamectl set-hostname "$newhost"
    fi
    # only nudge when the timezone is unset/UTC; let the user type any zone (no baked-in city)
    if [ -z "$cur_tz" ] || [ "$cur_tz" = "Etc/UTC" ] || [ "$cur_tz" = "UTC" ]; then
        local newtz
        newtz="$(ui_input "Timezone" "Timezone (e.g. Europe/Madrid, America/New_York):" "${cur_tz:-UTC}")" && \
            [ -n "$newtz" ] && [ "$newtz" != "$cur_tz" ] && run timedatectl set-timezone "$newtz"
    else
        info "timezone: $cur_tz"
    fi

    # summary
    stage_system_summary
    log "--- stage_system end ---"
}

stage_system_summary() {
    local msg
    msg="Stage 0 complete.

Check:
  systemctl status docker
  df -h
  ls -la $NAS_CONFIG

Remaining: attach disks (stage 2).

NOTE: docker group membership applies after $TARGET_USER relogs in."
    ui_msg "Summary: system preparation" "$msg"
    echo "$msg"
}

# ---------------------------------------------------------------------------
# STAGE 2: working with a single disk
# ---------------------------------------------------------------------------

# Triple confirmation + requirement to type the device name as text
confirm_destructive() {
    local dev="$1" purpose="$2" fs="$3" label="$4"
    local block typed
    block="$(disk_info_block "$dev")"

    if ! ui_yesno "FORMAT CONFIRMATION" \
"THE DISK WILL BE FORMATTED as $fs (label: $label), purpose: $purpose.

$block

ALL DATA ON THIS DISK WILL BE PERMANENTLY ERASED.

Continue?"; then
        info "formatting cancelled by user"
        return 1
    fi

    # Require typing the device name as text
    typed="$(ui_input "Final confirmation" \
"To confirm formatting, type the DEVICE NAME exactly like this:

$dev" "")" || { info "cancelled"; return 1; }

    if [ "$typed" != "$dev" ]; then
        ui_msg "Cancel" "Got '$typed', expected '$dev'. Formatting CANCELLED."
        info "device name did not match ('$typed' != '$dev') — formatting cancelled"
        return 1
    fi
    return 0
}

# Format disk + fstab + mount. Arguments: dev mountpoint fs label pass
# Is mkfs.<fs> present?
mkfs_available() { command -v "mkfs.$1" >/dev/null 2>&1; }

# Create FS on device with a label. Supported: ext4/xfs/btrfs/exfat/ntfs/vfat.
make_fs() {
    local dev="$1" fs="$2" label="$3"
    mkfs_available "$fs" || die "no mkfs.$fs — install the package (exfatprogs/ntfs-3g/btrfs-progs/xfsprogs)"
    # mkfs MUST fail hard: otherwise format_and_mount would take the old UUID (blkid),
    # mount the not-fully-formatted FS and add it to fstab/pool → data loss/mess.
    case "$fs" in
        ext4)  run mkfs.ext4  -F -L "$label" "$dev" || die "mkfs.ext4 failed on $dev" ;;
        xfs)   run mkfs.xfs   -f -L "$(printf '%s' "$label" | cut -c1-12)" "$dev" || die "mkfs.xfs failed on $dev" ;;
        btrfs) run mkfs.btrfs -f -L "$label" "$dev" || die "mkfs.btrfs failed on $dev" ;;
        exfat) run mkfs.exfat -L "$label" "$dev" || die "mkfs.exfat failed on $dev" ;;
        ntfs)  run mkfs.ntfs  -Q -L "$label" "$dev" || die "mkfs.ntfs failed on $dev" ;;
        vfat)  run mkfs.vfat  -n "$(printf '%s' "$label" | tr 'a-z' 'A-Z' | tr -cd 'A-Z0-9_-' | cut -c1-11)" "$dev" || die "mkfs.vfat failed on $dev" ;;
        *)     die "unknown FS: $fs" ;;
    esac
}

format_and_mount() {
    local dev="$1" mp="$2" fs="$3" label="$4" pass="$5"
    local uuid

    backup_fstab

    make_fs "$dev" "$fs" "$label"

    # UUID (in dry-run — a placeholder, since the disk was not formatted)
    if [ "$DRY_RUN" -eq 1 ]; then
        uuid="<UUID-appears-after-mkfs>"
    else
        uuid="$(blkid -s UUID -o value "$dev" 2>/dev/null)"
        [ -z "$uuid" ] && die "failed to get UUID of $dev after formatting"
    fi

    # mount point directory
    run mkdir -p "$mp"

    # remove stale fstab lines for this same mount point (after reformatting the UUID changes —
    # otherwise a dead line with the old UUID for the same /mnt/diskN would remain)
    remove_fstab_mount "$mp"

    # fstab by UUID (+ nofail: so a missing disk does not block the NAS boot)
    local fstab_line="UUID=$uuid  $mp  $fs  defaults,noatime,nofail,x-systemd.device-timeout=10  0  $pass"
    append_line "$fstab_line" /etc/fstab
    # snippet into the git repository
    if [ -d "$NAS_CONFIG" ]; then
        append_line "$fstab_line" "$NAS_CONFIG/fstab.snippets"
    fi

    # mount and verify
    run systemctl daemon-reload
    run mount -a
    if [ "$DRY_RUN" -eq 0 ]; then
        if findmnt -no TARGET "$mp" >/dev/null 2>&1; then
            info "mounted: $mp"
        else
            warn "mount point $mp is not mounted — check /etc/fstab and the mount -a output"
        fi
    fi
}

# Idempotency: is the disk already configured?
disk_already_configured() {
    # The disk is already configured if its UUID (or the UUID of any of its partitions) is in fstab.
    local dev="$1" uuid u
    uuid="$(blkid -s UUID -o value "$dev" 2>/dev/null)"
    [ -n "$uuid" ] && grep -qsF "UUID=$uuid" /etc/fstab && return 0
    while read -r u; do
        [ -n "$u" ] && grep -qsF "UUID=$u" /etc/fstab && return 0
    done < <(lsblk -nro UUID "$dev" 2>/dev/null)
    return 1
}

stage_disk() {
    echo
    echo "=== Stage 2: working with a disk ==="
    log "--- stage_disk start ---"

    # Collect candidates
    local rows dev size model
    rows="$(candidate_disks)"
    if [ -z "$rows" ]; then
        ui_msg "No free disks" "No free block devices found.

Candidates are excluded if the disk:
 - is a system disk (/, /boot, /home, /var),
 - is already mounted somewhere,
 - has zero size.

Check the disk connection and run the stage again."
        info "no candidate disks"
        return 0
    fi

    # Disk selection menu
    local menu_args=()
    while IFS=$'\t' read -r dev size model; do
        [ -z "$dev" ] && continue
        menu_args+=("$dev" "$size — $model")
    done <<< "$rows"

    dev="$(ui_menu "Disk selection" "Free disks (system and mounted disks excluded):" "${menu_args[@]}")" || {
        info "disk selection cancelled"; return 0;
    }
    [ -z "$dev" ] && { info "no disk selected"; return 0; }

    # Double safeguard: the disk is not protected and not busy
    if is_protected "$dev"; then die "$dev — system disk, operation forbidden"; fi
    if disk_in_use "$dev"; then die "$dev is currently mounted — unmount it first"; fi

    # Idempotency
    if disk_already_configured "$dev"; then
        ui_msg "Disk already configured" "$dev is already present in /etc/fstab by UUID. Skipping."
        info "$dev already configured, skipping"
        return 0
    fi

    # Data or parity?
    local role
    role="$(ui_menu "Disk role" "Disk $dev — what for?" \
        "data"   "DATA disk" \
        "parity" "PARITY disk (SnapRAID parity)")" || { info "cancelled"; return 0; }

    # FS selection
    local fs
    fs="$(ui_menu "File system" "FS for $dev:" \
        "ext4" "ext4 (default, recommended)" \
        "xfs"  "xfs")" || { info "cancelled"; return 0; }
    [ -z "$fs" ] && fs="ext4"

    if [ "$role" = "data" ]; then
        local n mp label
        n="$(next_disk_number)"
        # The path is FIXED as /mnt/diskN, not free input: all discovery
        # (mounted_data_disks, largest_data_disk_bytes, next_disk_number, snapraid
        # d$n names) finds data disks by the /mnt/disk* pattern. A custom mount point
        # would make the disk invisible to the pool and SnapRAID — a silent hole in data protection.
        mp="/mnt/disk${n}"
        label="disk${n}"

        confirm_destructive "$dev" "DATA ($mp)" "$fs" "$label" || return 0
        format_and_mount "$dev" "$mp" "$fs" "$label" 2

        # mergerfs: merge into a pool only with >= 2 data disks (per spec)
        local data_count
        data_count="$(mounted_data_disks | grep -c .)"
        if [ "$data_count" -lt 2 ]; then
            ui_msg "mergerfs" "You have $data_count data disk(s).

There is no point merging a mergerfs pool yet (>= 2 disks needed).
It will be configured automatically once you add a second data disk."
            info "$data_count data disk — mergerfs not configured (per spec)"
        else
            info "data disks: $data_count — configuring mergerfs"
            generate_mergerfs
        fi

    else  # parity
        # Check that parity size >= the largest data disk
        local pn mp label pbytes maxdata
        pbytes="$(lsblk -bdno SIZE "$dev" 2>/dev/null | head -1)"
        maxdata="$(largest_data_disk_bytes)"
        if [ "${maxdata:-0}" -gt 0 ] && [ "${pbytes:-0}" -lt "$maxdata" ]; then
            local phr mhr risk
            phr="$(numfmt --to=iec "$pbytes" 2>/dev/null || echo "$pbytes")"
            mhr="$(numfmt --to=iec "$maxdata" 2>/dev/null || echo "$maxdata")"
            warn "parity ($phr) is SMALLER than the largest data disk ($mhr)"
            if ! ui_yesno "RISK: small parity disk" \
"The parity disk ($phr) is SMALLER than the largest data disk ($mhr).

SnapRAID WILL NOT be able to protect the data fully: parity must be >= the largest data disk.

This is a dangerous configuration. Continue?"; then
                info "parity smaller than data — user declined"
                return 0
            fi
            risk="$(ui_input "Risk confirmation" "Type the phrase: I understand the risk" "")" || return 0
            if [ "$risk" != "I understand the risk" ]; then
                ui_msg "Cancel" "The phrase did not match. Operation cancelled."
                info "risk confirmation phrase did not match"
                return 0
            fi
        fi

        pn="$(next_parity_number)"
        mp="/mnt/parity${pn}"
        label="parity${pn}"
        confirm_destructive "$dev" "PARITY ($mp)" "$fs" "$label" || return 0
        format_and_mount "$dev" "$mp" "$fs" "$label" 2

        ui_msg "SnapRAID" "Parity disk mounted at $mp.

Now you can configure SnapRAID: pick \"Stage 3: SnapRAID\" from the menu (or run with --stage snapraid)."
    fi

    stage_disk_summary "$dev"
    log "--- stage_disk end ---"
}

stage_disk_summary() {
    local dev="$1" msg
    msg="Stage 2 (disk $dev) complete.

Current state:
$(findmnt -rno TARGET,SOURCE,FSTYPE,SIZE /mnt/disk* /mnt/parity* 2>/dev/null | sed 's/^/  /')

Check:
  lsblk -f
  df -h
  cat /etc/fstab

fstab backups: ls -la /etc/fstab.bak.*
Configs in git: $NAS_CONFIG"
    ui_msg "Summary: working with a disk" "$msg"
    echo "$msg"
}

# ---------------------------------------------------------------------------
# STAGE 2b: mergerfs (pool from >= 2 data disks)
# ---------------------------------------------------------------------------
# nofail — do not block boot in emergency mode if the pool did not mount.
# x-systemd.requires=<branch> is added to each branch dynamically in generate_mergerfs
# (disk paths are not static), so the pool mounts ONLY after its branches, not over empty /mnt/diskN.
MERGERFS_OPTS="defaults,nofail,allow_other,use_ino,category.create=mfs,minfreespace=20G,fsname=mergerfs"

remove_fstab_mergerfs() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] remove the fuse.mergerfs line from /etc/fstab"
        return 0
    fi
    sed -i '/fuse\.mergerfs/d' /etc/fstab
}

# mergerfs options for the COMMAND line (-o ...): without the fstab constructs defaults/nofail.
MERGERFS_SVC_OPTS="allow_other,use_ino,category.create=mfs,minfreespace=20G,fsname=mergerfs"
MERGERFS_UNIT="/etc/systemd/system/nas-mergerfs.service"
# Keep the mergerfs pool as a systemd SERVICE with Restart=always, NOT an fstab line. Reason:
# the FUSE process may crash ("Transport endpoint is not connected"), and an fstab mount is then
# dead until a manual umount+mount. The systemd service brings it back in seconds by itself (+ ExecStartPre
# clears the stuck mount point). The /mnt/disk* branches stay in fstab; the pool depends on local-fs.
generate_mergerfs() {
    local branches=() mp
    while read -r mp; do [ -n "$mp" ] && branches+=("$mp"); done < <(mounted_data_disks)
    local count="${#branches[@]}"
    if [ "$count" -lt 2 ]; then
        info "mounted data disks: $count — mergerfs not needed (>= 2 required)"
        # a disk left the pool — remove the service if it existed
        if [ -e "$MERGERFS_UNIT" ]; then
            run systemctl disable --now nas-mergerfs.service
            run rm -f "$MERGERFS_UNIT"
            run systemctl daemon-reload
        fi
        return 0
    fi

    local branchspec mergerfs_bin
    branchspec="$(IFS=:; printf '%s' "${branches[*]}")"
    mergerfs_bin="$(command -v mergerfs 2>/dev/null || echo /usr/bin/mergerfs)"

    # migration from the old scheme: remove the pool line from fstab (the service now runs the pool).
    # We do not touch the /mnt/disk* branches in fstab.
    if grep -qsE 'fuse\.mergerfs' /etc/fstab; then
        backup_fstab
        findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1 && run umount -l "$STORAGE_MNT"
        remove_fstab_mergerfs
    fi
    run mkdir -p "$STORAGE_MNT"

    # We escape \$(seq …) — this is a command run at service START, not at file generation
    write_file "$MERGERFS_UNIT" <<EOF
[Unit]
Description=NAS mergerfs pool (${STORAGE_MNT})
After=local-fs.target
Wants=local-fs.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStartPre=-/bin/umount -l ${STORAGE_MNT}
ExecStart=${mergerfs_bin} -f ${branchspec} ${STORAGE_MNT} -o ${MERGERFS_SVC_OPTS}
ExecStartPost=/bin/sh -c 'for i in \$(seq 1 50); do mountpoint -q ${STORAGE_MNT} && exit 0; sleep 0.1; done; exit 1'
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

    run systemctl daemon-reload
    run systemctl enable nas-mergerfs.service
    run systemctl restart nas-mergerfs.service
    if [ "$DRY_RUN" -eq 0 ]; then
        sleep 1
        if findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1; then
            info "mergerfs pool brought up by the nas-mergerfs service (Restart=always): $STORAGE_MNT ($count disks)"
        else
            warn "pool $STORAGE_MNT did not come up — check: systemctl status nas-mergerfs"
        fi
    fi
    commit_config "mergerfs: nas-mergerfs service (pool from $count disks)"
}

stage_mergerfs() {
    echo
    echo "=== Stage 2b: mergerfs ==="
    log "--- stage_mergerfs start ---"
    local count
    count="$(mounted_data_disks | grep -c .)"
    if [ "$count" -lt 2 ]; then
        ui_msg "mergerfs" "Mounted data disks: $count.

At least 2 data disks are needed to merge them into a mergerfs pool.
Add another data disk (stage 2) and come back here."
        info "mergerfs: not enough disks ($count)"
        return 0
    fi
    generate_mergerfs
    if [ "$DRY_RUN" -eq 0 ]; then
        ui_msg "Summary: mergerfs" "Pool $STORAGE_MNT assembled from $count disks.

Check:
$(df -h "$STORAGE_MNT" 2>/dev/null | sed 's/^/  /')"
    fi
    log "--- stage_mergerfs end ---"
}

# ---------------------------------------------------------------------------
# STAGE 3: SnapRAID
# ---------------------------------------------------------------------------
SNAPRAID_CONF="/etc/snapraid.conf"

parity_keyword() { case "$1" in 1) echo "parity" ;; *) echo "$1-parity" ;; esac; }

ensure_snapraid_conf() {
    local data_mounts=() parity_mounts=() m
    while read -r m; do [ -n "$m" ] && data_mounts+=("$m"); done < <(mounted_data_disks)
    while read -r m; do [ -n "$m" ] && parity_mounts+=("$m"); done < <(mounted_parity_disks)

    if [ "${#data_mounts[@]}" -eq 0 ]; then
        ui_msg "SnapRAID" "No mounted data disks (/mnt/disk*). Add a data disk first (stage 2)."
        return 1
    fi
    if [ "${#parity_mounts[@]}" -eq 0 ]; then
        ui_msg "SnapRAID" "No parity disk (/mnt/parity*). SnapRAID requires at least one parity disk. Add parity (stage 2)."
        return 1
    fi

    run mkdir -p /var/snapraid

    if [ ! -f "$SNAPRAID_CONF" ]; then
        # Fresh generation
        info "generating $SNAPRAID_CONF ($((${#data_mounts[@]})) data, $((${#parity_mounts[@]})) parity)"
        {
            echo "# snapraid.conf — generated by nas-wizard $(date '+%F %T')"
            echo "# Edit disks via the wizard; excludes can be added manually."
            echo
            local i=1 p kw
            for p in "${parity_mounts[@]}"; do
                kw="$(parity_keyword "$i")"
                echo "$kw $p/snapraid.$kw"
                i=$((i+1))
            done
            echo
            echo "content /var/snapraid/content"
            local d n
            for d in "${data_mounts[@]}"; do
                echo "content $d/.snapraid.content"
            done
            echo
            for d in "${data_mounts[@]}"; do
                n="${d##*/disk}"
                echo "data d$n $d"
            done
            echo
            echo "exclude *.tmp"
            echo "exclude *.unrecoverable"
            echo "exclude /tmp/"
            echo "exclude /lost+found/"
            echo "exclude .Trash-*/"
            echo "exclude .snapraid.content*"
        } | write_file "$SNAPRAID_CONF"
    else
        # Idempotent append of missing lines (we do NOT touch excludes)
        info "$SNAPRAID_CONF exists — appending missing disks"
        local i=1 p kw d n
        for p in "${parity_mounts[@]}"; do
            kw="$(parity_keyword "$i")"
            append_line "$kw $p/snapraid.$kw" "$SNAPRAID_CONF"
            i=$((i+1))
        done
        for d in "${data_mounts[@]}"; do
            append_line "content $d/.snapraid.content" "$SNAPRAID_CONF"
            n="${d##*/disk}"
            if ! grep -qsE "^data[[:space:]]+\S+[[:space:]]+$d\$" "$SNAPRAID_CONF"; then
                append_line "data d$n $d" "$SNAPRAID_CONF"
            else
                info "data disk already in config: $d"
            fi
        done
    fi
    commit_config "snapraid.conf: ${#data_mounts[@]} data / ${#parity_mounts[@]} parity"
    return 0
}

install_snapraid_wrapper() {
    write_file /usr/local/bin/nas-snapraid.sh <<'WRAP'
#!/usr/bin/env bash
# nas-wizard: snapraid sync/scrub wrapper with mass-deletion protection + status ping
set -uo pipefail
ACTION="${1:-sync}"
LOG=/var/log/snapraid.log
CONF=/etc/nas-wizard/notify.conf
DELETE_THRESHOLD=500
HEALTHCHECK_URL=""
[ -f "$CONF" ] && . "$CONF"
notify(){ [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$@" || true; }
# Healthchecks/ntfy/webhook ping: success — the base URL, error — /fail (Healthchecks convention)
ping_hc(){ [ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 12 --retry 2 "$HEALTHCHECK_URL$1" >/dev/null 2>&1 || true; }

{
    echo "===== $(date '+%F %T') snapraid $ACTION ====="
    # DATA PROTECTION: if any data disk from the config is not mounted, its files
    # look "deleted" → sync would record the deletions into parity. We abort.
    miss=""
    while read -r _ _ dpath; do
        [ -n "$dpath" ] || continue
        mountpoint -q "$dpath" || miss="$miss $dpath"
    done < <(grep -E '^data ' /etc/snapraid.conf 2>/dev/null)
    if [ -n "$miss" ]; then
        echo "ABORT: data disks not mounted:$miss — $ACTION SKIPPED (parity protection)."
        ping_hc "/fail"
        echo "NASRESULT $ACTION err rc=9" >>"$LOG"
        exit 9
    fi
    if [ "$ACTION" = "sync" ]; then
        # diff must run correctly (0=no changes, 2=changes present); otherwise — do NOT sync
        diff_out="$(snapraid diff 2>&1)"; diff_rc=$?
        printf '%s\n' "$diff_out"
        if [ "$diff_rc" != 0 ] && [ "$diff_rc" != 2 ]; then
            echo "ABORT: snapraid diff exited with code $diff_rc — sync SKIPPED (could not assess deletions)."
            ping_hc "/fail"
            exit 1
        fi
        removed=$(printf '%s\n' "$diff_out" | sed -n 's/^ *\([0-9][0-9]*\) removed$/\1/p')
        removed=${removed:-0}
        echo "diff: removed=$removed threshold=$DELETE_THRESHOLD"
        if [ "$removed" -gt "$DELETE_THRESHOLD" ]; then
            echo "ABORT: files removed $removed > threshold $DELETE_THRESHOLD — sync SKIPPED (data protection)."
            ping_hc "/fail"
            exit 1
        fi
        snapraid sync
    else
        snapraid scrub -p 12 -o 10
    fi
} >>"$LOG" 2>&1
rc=$?
# NASRESULT markers are read by nas-web (unified notifications with Pushover priorities)
if [ "$rc" -eq 0 ]; then echo "NASRESULT $ACTION ok $(date '+%F %T')" >>"$LOG"; ping_hc ""
else echo "NASRESULT $ACTION err rc=$rc" >>"$LOG"; ping_hc "/fail"; fi
exit "$rc"
WRAP
    run chmod +x /usr/local/bin/nas-snapraid.sh
    install_notify_helper
}

install_snapraid_timers() {
    write_file /etc/systemd/system/snapraid-sync.service <<'UNIT'
[Unit]
Description=SnapRAID sync (nas-wizard)
After=local-fs.target

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart=/usr/local/bin/nas-snapraid.sh sync
UNIT
    write_file /etc/systemd/system/snapraid-sync.timer <<'UNIT'
[Unit]
Description=Daily SnapRAID sync (nas-wizard)

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=900

[Install]
WantedBy=timers.target
UNIT
    write_file /etc/systemd/system/snapraid-scrub.service <<'UNIT'
[Unit]
Description=SnapRAID scrub (nas-wizard)
After=local-fs.target

[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
ExecStart=/usr/local/bin/nas-snapraid.sh scrub
UNIT
    write_file /etc/systemd/system/snapraid-scrub.timer <<'UNIT'
[Unit]
Description=Weekly SnapRAID scrub (nas-wizard)

[Timer]
OnCalendar=Sun *-*-* 05:00:00
Persistent=true
RandomizedDelaySec=1800

[Install]
WantedBy=timers.target
UNIT
    run systemctl daemon-reload
    run systemctl enable --now snapraid-sync.timer
    run systemctl enable --now snapraid-scrub.timer
}

setup_snapraid_notify() {
    if ui_yesno "Notifications" "Set up a snapraid sync status ping (Healthchecks.io / ntfy / webhook)?

On success the URL is pinged; for Healthchecks /fail is appended on error. If you already have Healthchecks/Uptime Kuma — you can point to their URL."; then
        local url
        url="$(ui_input "Ping URL" "URL to ping on SUCCESS:" "")" || return 0
        if [ -z "$url" ]; then info "URL is empty — notifications not configured"; return 0; fi
        notify_conf_set HEALTHCHECK_URL "$url"
        info "notifications configured: $url"
    else
        info "notifications not configured"
    fi
}

stage_snapraid() {
    echo
    echo "=== Stage 3: SnapRAID ==="
    log "--- stage_snapraid start ---"

    ensure_snapraid_conf || { log "--- stage_snapraid aborted ---"; return 0; }

    # Notifications (before installing the wrapper — so notify.conf already exists)
    setup_snapraid_notify

    # Wrapper + systemd timers
    install_snapraid_wrapper
    install_snapraid_timers

    # First sync (with consent)
    if ui_yesno "SnapRAID sync" "Run the first snapraid sync NOW?

WARNING: on large disks this may take a VERY long time. Progress will be visible in the terminal.
Skip it if you want to wait for the nightly auto-sync (the timer is already configured)."; then
        echo "--- snapraid sync (progress below) ---"
        run_visible snapraid sync
    else
        info "first sync skipped (will run by the timer at 03:00)"
    fi

    stage_snapraid_summary
    log "--- stage_snapraid end ---"
}

stage_snapraid_summary() {
    local status="(run: sudo snapraid status)"
    [ "$DRY_RUN" -eq 0 ] && status="$(snapraid status 2>/dev/null | tail -n 15 | sed 's/^/  /')"
    ui_msg "Summary: SnapRAID" "Config: $SNAPRAID_CONF
Wrapper: /usr/local/bin/nas-snapraid.sh
Timers: snapraid-sync.timer (daily 03:00), snapraid-scrub.timer (weekly Sun 05:00)
Log: /var/log/snapraid.log

Check:
  systemctl list-timers 'snapraid-*'
  sudo snapraid status
  sudo snapraid sync"
    echo "Stage 3 complete."
    echo "$status"
}

# ---------------------------------------------------------------------------
# STAGE 4: Docker (discovery-based: read folders with compose files)
# ---------------------------------------------------------------------------

# Prints: SERVICE<TAB>COMPOSE_FILE — one line per discovered service.
# Searches the services/ directory next to the script ($1 overrides the directory).
# shellcheck disable=SC2120  # $1 is optional, defaults to $SERVICES_SRC
discover_compose_services() {
    local base="${1:-$SERVICES_SRC}" d f
    for d in "$base"/*/; do
        [ -d "$d" ] || continue
        for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
            if [ -f "$d$f" ]; then
                printf '%s\t%s\n' "$(basename "$d")" "$d$f"
                break
            fi
        done
    done
}

# Is the service running (at least one running container)?
service_running() {
    local file="$1" ids running
    ids="$($DC -f "$file" ps -q 2>/dev/null)"
    [ -n "$ids" ] || return 1
    running="$(docker inspect -f '{{.State.Running}}' $ids 2>/dev/null | grep -c true)"
    [ "${running:-0}" -gt 0 ]
}

generate_deploy_script() {
    run_as mkdir -p "$NAS_CONFIG/scripts"
    # SERVICES_SRC is substituted at generation time so deploy.sh is self-contained
    write_file "$NAS_CONFIG/scripts/deploy.sh" <<DEPLOY
#!/usr/bin/env bash
# Auto-generated by nas-wizard. Idempotently brings up ALL compose services from services/ next to the script.
# "Apply the desired state": docker compose up -d for each discovered file.
set -uo pipefail
COMPOSE_DIR="${SERVICES_SRC}"
DC="docker compose"
if ! docker compose version >/dev/null 2>&1; then
    command -v docker-compose >/dev/null 2>&1 && DC="docker-compose"
fi
rc=0
for d in "\$COMPOSE_DIR"/*/; do
    [ -d "\$d" ] || continue
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
        if [ -f "\$d\$f" ]; then
            echo "==> \$(basename "\$d")"
            \$DC -f "\$d\$f" up -d || rc=1
            break
        fi
    done
done
exit "\$rc"
DEPLOY
    [ "$DRY_RUN" -eq 0 ] && chmod +x "$NAS_CONFIG/scripts/deploy.sh" 2>/dev/null
    run chown "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG/scripts/deploy.sh" 2>/dev/null || true
}

# Boot order: docker waits for the mergerfs pool, stacks come up after mounting.
# Protects against containers writing into an EMPTY /mnt/storage mount point if the pool is not mounted yet.
install_stacks_autostart() {
    # We DELIBERATELY do not put RequiresMountsFor on docker.service itself: that would make
    # starting the daemon (and thus ALL containers) depend on the pool — one
    # missing/renamed disk would take down the whole Docker. We wait for the pool only at
    # the nas-stacks.service level (stacks bring-up), not the daemon.
    # Clean up the drop-in if an old wizard run left it behind:
    if [ -f /etc/systemd/system/docker.service.d/wait-storage.conf ]; then
        run rm -f /etc/systemd/system/docker.service.d/wait-storage.conf
        rmdir /etc/systemd/system/docker.service.d 2>/dev/null || true
    fi
    local reqmount=""
    if findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1 || grep -qsE "[[:space:]]${STORAGE_MNT}[[:space:]]" /etc/fstab; then
        reqmount="RequiresMountsFor=$STORAGE_MNT"
    fi
    write_file /etc/systemd/system/nas-stacks.service <<EOF
[Unit]
Description=Bring up NAS docker stacks (nas-wizard)
After=docker.service network-online.target
Requires=docker.service
$reqmount

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/env bash $NAS_CONFIG/scripts/deploy.sh

[Install]
WantedBy=multi-user.target
EOF
    run systemctl daemon-reload
    run systemctl enable nas-stacks.service
}

stage_docker() {
    echo
    echo "=== Stage 4: Docker ==="
    log "--- stage_docker start ---"

    DC="$(docker_compose_cmd)"
    if [ -z "$DC" ]; then
        ui_msg "Docker" "Docker Compose not found. Run stage 0 first (system preparation), it installs docker-ce + docker-compose-plugin from the official Docker repo."
        info "docker compose unavailable"
        return 0
    fi

    run mkdir -p "$DOCKER_ROOT" "$SERVICES_SRC"

    # Always generate deploy.sh — so it exists from day one
    generate_deploy_script
    # autostart stacks after boot + wait for the pool
    install_stacks_autostart

    local rows
    rows="$(discover_compose_services)"
    if [ -z "$rows" ]; then
        ui_msg "Docker: no services" "No compose file found in $SERVICES_SRC.

How it works: docker services live NEXT TO THE SCRIPT, in the services/ folder.
Each service is its own subfolder with a docker-compose.yml. The script finds them and offers to bring them up.

Example:
  mkdir -p $SERVICES_SRC/immich
  \$EDITOR $SERVICES_SRC/immich/docker-compose.yml

Keep configs/data in $DOCKER_ROOT/<service>/ and $STORAGE_MNT/<service>/.
Then run this stage again.

Already created: $NAS_CONFIG/scripts/deploy.sh (brings everything up at once)."
        info "no compose services found in $SERVICES_SRC"
        return 0
    fi

    # Checklist: mark the already running ones
    local menu_args=() svc file state
    while IFS=$'\t' read -r svc file; do
        [ -z "$svc" ] && continue
        if service_running "$file"; then state="ON"; else state="OFF"; fi
        menu_args+=("$svc" "$file" "$state")
    done <<< "$rows"

    local raw
    raw="$(ui_checklist "Docker: which services to bring up" \
        "Check the services for 'up -d' (already running ones are marked). Unchecked running ones will be offered to stop." \
        "${menu_args[@]}")" || { info "service selection cancelled"; return 0; }

    # Parse the selected ones (whiptail returns them quoted)
    local selected
    selected="$(printf '%s' "$raw" | tr -d '"')"

    # Set of selected ones for a quick check
    local want=" $selected "

    # Bring up the selected ones, collect "running but not selected"
    local to_stop=() up_count=0
    while IFS=$'\t' read -r svc file; do
        [ -z "$svc" ] && continue
        if [[ "$want" == *" $svc "* ]]; then
            # warning about floating tags
            if grep -qsE 'image:.*:latest([[:space:]]|$)|image:[[:space:]]*[^:]+$' "$file"; then
                warn "$svc: image without a pinned tag (:latest or no tag) — pinned versions are recommended"
            fi
            info "up -d: $svc"
            run_visible $DC -f "$file" up -d
            up_count=$((up_count+1))
        else
            if service_running "$file"; then to_stop+=("$svc|$file"); fi
        fi
    done <<< "$rows"

    # Offer to stop services that were unchecked but are running
    if [ "${#to_stop[@]}" -gt 0 ]; then
        local names=""
        local item
        for item in "${to_stop[@]}"; do names+="  ${item%%|*}\n"; done
        if ui_yesno "Stop services?" "These services are running but NOT checked:\n\n$names\nStop them (docker compose down)?"; then
            for item in "${to_stop[@]}"; do
                svc="${item%%|*}"; file="${item##*|}"
                info "down: $svc"
                run_visible $DC -f "$file" down
            done
        else
            info "unchecked services left running"
        fi
    fi

    commit_config "docker: deploy.sh + up ($up_count services)"
    stage_docker_summary "$up_count"
    log "--- stage_docker end ---"
}

stage_docker_summary() {
    local n="$1"
    ui_msg "Summary: Docker" "Services started: $n
Compose folders: $SERVICES_SRC/<service>/ (next to the script)
deploy.sh:     $NAS_CONFIG/scripts/deploy.sh (apply everything at once)

Check:
  docker ps
  docker compose ls
  bash $NAS_CONFIG/scripts/deploy.sh

Recommendations: pinned image tags (not latest), restart: unless-stopped,
volumes under $STORAGE_MNT/<service>/."
    echo "Stage 4 complete. Services started: $n"
}

# ---------------------------------------------------------------------------
# Shared helpers for stages 5-8
# ---------------------------------------------------------------------------
enable_service() {
    local svc="$1"
    systemctl is-enabled "$svc" >/dev/null 2>&1 || run systemctl enable "$svc"
    systemctl is-active  "$svc" >/dev/null 2>&1 || run systemctl start "$svc"
}

backup_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] backup $f"; return 0; fi
    cp -a "$f" "${f}.bak.$(date '+%Y%m%d-%H%M%S')" && info "backup: $f"
}

boot_config_path() {
    if   [ -f /boot/firmware/config.txt ]; then echo /boot/firmware/config.txt
    elif [ -f /boot/config.txt ];          then echo /boot/config.txt
    else echo ""; fi
}

# LAN subnet like 192.168.1.0/24 (from the connected route)
detect_lan_cidr() {
    ip -o -f inet route show scope link 2>/dev/null \
        | awk '$1 ~ /\// && $1 !~ /^169\.254/ {print $1; exit}'
}

# checklist -> " tag1 tag2 " for checks like: case " $sel " in *" tag "*)
checklist_selected() { printf ' %s ' "$(printf '%s' "$1" | tr -d '"')"; }

# ---------------------------------------------------------------------------
# STAGE 5: Pi tuning (hardware). config.txt edits require a reboot.
# ---------------------------------------------------------------------------
pi_pcie3() {
    local cfg="$1"
    if [ -z "$cfg" ]; then warn "config.txt not found — PCIe Gen3 skipped"; return 0; fi
    backup_file "$cfg"
    append_line "dtparam=pciex1_gen=3" "$cfg"
    info "PCIe Gen3 for NVMe added to $cfg (takes effect after reboot)"
}
pi_wifi_powersave_off() {
    run mkdir -p /etc/NetworkManager/conf.d
    write_file /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
    systemctl is-active NetworkManager >/dev/null 2>&1 && run systemctl restart NetworkManager
    info "Wi-Fi power-save disabled"
}
pi_watchdog() {
    run mkdir -p /etc/systemd/system.conf.d
    write_file /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
    run systemctl daemon-reexec
    info "watchdog enabled (RuntimeWatchdogSec=15s)"
}
# USB max current — on Pi5, without this the total USB current is capped at 600mA => brownouts on USB-SSD
pi_usb_power() {
    local cfg="$1"
    if [ -z "$cfg" ]; then warn "config.txt not found — USB power skipped"; return 0; fi
    backup_file "$cfg"
    append_line "usb_max_current_enable=1" "$cfg"
    info "usb_max_current_enable=1 (power for USB disks; takes effect after reboot)"
}
# Memory cgroup for docker limits (editing cmdline.txt — a SINGLE-line file!)
pi_cgroup() {
    local cl=/boot/firmware/cmdline.txt
    [ -f "$cl" ] || cl=/boot/cmdline.txt
    [ -f "$cl" ] || { warn "cmdline.txt not found — cgroup skipped"; return 0; }
    if grep -qs 'cgroup_enable=memory' "$cl"; then info "memory cgroup already enabled"; return 0; fi
    backup_file "$cl"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] add cgroup_enable=memory cgroup_memory=1 to $cl"
        return 0
    fi
    sed -i 's/\bcgroup_disable=memory\b//g; s/[[:space:]]\+/ /g; s/[[:space:]]*$//' "$cl"
    sed -i '1 s|$| cgroup_enable=memory cgroup_memory=1|' "$cl"
    info "memory cgroup enabled in $cl (reboot needed; memory limits in docker-compose)"
}
pi_sysctl() {
    write_file /etc/sysctl.d/99-nas.conf <<'EOF'
# nas-wizard: tuning for NAS
vm.swappiness = 10
net.core.somaxconn = 512
net.ipv4.tcp_keepalive_time = 120
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3
EOF
    run sysctl --system
    info "sysctl tuning applied (swappiness=10, somaxconn=512, tcp keepalive)"
}
# Silence the legacy zramswap.service (zram-tools package). On modern
# Raspberry Pi OS zram-swap is brought up by systemd-zram-generator/rpi-swap, and
# zramswap.service FIGHTS it over /dev/zram0: "Device or resource busy",
# "zram0 is mounted; will not make swapspace" — endless failed spam in the journal.
zram_disable_zramtools() {
    if systemctl list-unit-files 2>/dev/null | grep -q '^zramswap\.service' \
       || [ -e /etc/systemd/system/zramswap.service ]; then
        run systemctl stop zramswap.service 2>/dev/null || true
        run systemctl disable zramswap.service 2>/dev/null || true
        # home-grown override unit in /etc (from older versions) — remove it
        [ -e /etc/systemd/system/zramswap.service ] && run rm -f /etc/systemd/system/zramswap.service
        run systemctl daemon-reload 2>/dev/null || true
        run systemctl mask zramswap.service 2>/dev/null || true
    fi
}
# Is there a native zram generator (modern Pi OS Bookworm+)?
zram_have_native() {
    [ -e /usr/lib/systemd/system/systemd-zram-setup@.service ] \
    || [ -f /etc/rpi/swap.conf ] \
    || [ -f /etc/systemd/zram-generator.conf ] \
    || [ -f /usr/lib/systemd/zram-generator.conf ]
}
pi_zram() {
    if zram_have_native; then
        # 1) silence the conflicting zramswap.service so it doesn't touch zram0
        zram_disable_zramtools
        # 2) configure native zram: ~50% RAM (cap 4 GiB), zstd (kernel default)
        if [ "$DRY_RUN" -eq 0 ]; then
            if [ -f /etc/rpi/swap.conf ]; then
                run mkdir -p /etc/rpi/swap.conf.d
                cat > /etc/rpi/swap.conf.d/60-nas-os.conf <<'EOF'
# NAS-OS: zram-swap enabled (~50% RAM, cap 4 GiB). See swap.conf(5).
[Main]
Mechanism=zram+file
[Zram]
RamMultiplier=0.5
MaxSizeMiB=4096
EOF
            else
                run mkdir -p /etc/systemd
                cat > /etc/systemd/zram-generator.conf <<'EOF'
# NAS-OS: zram-swap (zstd, ~50% RAM, cap 4 GiB). See zram-generator.conf(5).
[zram0]
zram-size = min(ram / 2, 4096)
compression-algorithm = zstd
EOF
            fi
            run systemctl daemon-reload 2>/dev/null || true
            # apply live (best-effort; fully from the next boot).
            # reset-failed — otherwise a quick .swap restart hits start-limit and
            # leaves the system with no swap at all.
            run systemctl reset-failed dev-zram0.swap systemd-zram-setup@zram0.service 2>/dev/null || true
            run systemctl restart dev-zram0.swap 2>/dev/null \
                || run systemctl start dev-zram0.swap 2>/dev/null || true
        fi
        info "zram-swap: zstd, ~50% RAM (native systemd-zram-generator)"
        return 0
    fi
    # Legacy system without a generator — install zram-tools
    install_packages "zram" zram-tools
    if [ -f /etc/default/zramswap ] && [ "$DRY_RUN" -eq 0 ]; then
        backup_file /etc/default/zramswap
        sed -i 's/^#\?ALGO=.*/ALGO=zstd/; s/^#\?PERCENT=.*/PERCENT=50/' /etc/default/zramswap
        grep -qs '^ALGO=' /etc/default/zramswap || echo "ALGO=zstd" >> /etc/default/zramswap
        grep -qs '^PERCENT=' /etc/default/zramswap || echo "PERCENT=50" >> /etc/default/zramswap
        run systemctl enable --now zramswap 2>/dev/null || run systemctl restart zramswap 2>/dev/null || true
    fi
    info "zram-swap: zstd, 50% RAM (zram-tools)"
}
# VID:PID of USB storage devices (for usb-storage.quirks) — one per line, unique
detect_usb_storage_ids() {
    local b p vid pid
    for b in /sys/block/sd*; do
        [ -e "$b" ] || continue
        p="$(readlink -f "$b/device" 2>/dev/null)" || continue
        while [ -n "$p" ] && [ "$p" != "/" ]; do
            if [ -f "$p/idVendor" ] && [ -f "$p/idProduct" ]; then
                vid="$(cat "$p/idVendor" 2>/dev/null)"; pid="$(cat "$p/idProduct" 2>/dev/null)"
                [ -n "$vid" ] && [ -n "$pid" ] && echo "${vid}:${pid}"
                break
            fi
            p="$(dirname "$p")"
        done
    done | sort -u
}
# Seed usb-storage.quirks in cmdline with the bridges attached right now, so they come up
# on usb-storage from the very first boot. Everything plugged in LATER is handled at runtime
# by the udev hook (nas-uas-off.sh) — see install_uas_off().
uas_seed_cmdline() {
    local cl=/boot/firmware/cmdline.txt
    [ -f "$cl" ] || cl=/boot/cmdline.txt
    [ -f "$cl" ] || { warn "cmdline.txt not found — UAS quirks skipped"; return 0; }
    local ids; ids="${NASW_QUIRKS:-$(detect_usb_storage_ids)}"
    [ -n "$ids" ] || { info "no USB disks right now — udev adds quirks on plug-in"; return 0; }
    local want="" id
    for id in $ids; do want="${want:+$want,}${id}:u"; done
    local line cur merged
    line="$(head -1 "$cl")"
    cur="$(printf '%s\n' "$line" | grep -o 'usb-storage\.quirks=[^ ]*' | head -1 | sed 's/usb-storage\.quirks=//')"
    merged="$(printf '%s,%s' "$cur" "$want" | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"
    if [ -n "$cur" ] && [ "$cur" = "$merged" ]; then info "usb-storage.quirks already configured ($cur)"; return 0; fi
    backup_file "$cl"
    if [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] usb-storage.quirks=$merged in $cl"; return 0; fi
    if [ -n "$cur" ]; then
        line="$(printf '%s\n' "$line" | sed "s#usb-storage\.quirks=[^ ]*#usb-storage.quirks=$merged#")"
    else
        line="$line usb-storage.quirks=$merged"
    fi
    printf '%s\n' "$line" > "$cl"
    info "UAS disabled for USB bridges: $merged (in $cl; reboot needed)"
}
# Disable UAS for every USB-SATA bridge — current and future. NOT optional, on purpose.
#
# Why UAS must go: its error recovery resets the WHOLE usb device. One command that hangs
# (a SMART ATA pass-through issued while rsync is writing is enough) aborts every in-flight
# write, the disk is offlined and ext4 flips to emergency read-only IN THE MIDDLE of a backup.
# Real case 2026-07-12: Ugreen RTL9210 + ST4000LM024. usb-storage (BOT) has no device-wide
# reset — a stuck command fails alone. BOT is slower in theory, but the ceiling here is the
# gigabit LAN and the HDD itself, so it costs nothing in practice.
#
# Why it can't be one global switch: uas is BUILT INTO the Pi kernel (not a module), so
# `blacklist uas` is a no-op. The only lever the kernel offers is usb-storage.quirks, and it
# takes explicit VID:PID — no wildcards. So the list has to exist; the trick is to never let
# it go stale. Hence the udev hook: a hand-maintained list is exactly what let the Ugreen
# bridge stay on UAS and eat the backup.
install_uas_off() {
    write_file /usr/local/bin/nas-uas-off.sh <<'UASOFF'
#!/bin/sh
# nas-wizard: disable UAS for one USB mass-storage bridge (from udev via systemd-run).
# Adds VID:PID to the live usb-storage quirks parameter AND to cmdline.txt, then
# re-enumerates the device so it re-probes and lands on usb-storage instead of uas.
# Runs once per bridge: on every later plug the quirk is already there and we exit early.
set -u
VID="${1:-}"; PID="${2:-}"; DEV="${3:-}"
[ -n "$VID" ] && [ -n "$PID" ] && [ -n "$DEV" ] || exit 0
LOG=/var/log/nas-automount.log
log(){ printf '%s nas-uas-off: %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null
       logger -t nas-uas-off -- "$*" 2>/dev/null || true; }

Q=/sys/module/usb_storage/parameters/quirks
[ -w "$Q" ] || { log "no $Q — skipping"; exit 0; }
cur="$(tr -d '\n' <"$Q" 2>/dev/null)"
# Already quirked (the normal path on every boot after the first) — nothing to do.
case ",$cur," in *",$VID:$PID:u,"*) exit 0 ;; esac
new="${cur:+$cur,}$VID:$PID:u"
printf '%s' "$new" >"$Q" 2>/dev/null || { log "cannot write $Q"; exit 0; }

CL=/boot/firmware/cmdline.txt; [ -f "$CL" ] || CL=/boot/cmdline.txt
if [ -f "$CL" ]; then
  line="$(head -1 "$CL")"
  case "$line" in
    *usb-storage.quirks=*) line="$(printf '%s' "$line" | sed "s#usb-storage\.quirks=[^ ]*#usb-storage.quirks=$new#")" ;;
    *)                     line="$line usb-storage.quirks=$new" ;;
  esac
  printf '%s\n' "$line" >"$CL" 2>/dev/null || log "cannot write $CL"
fi

# The bridge is bound to uas right now; only a re-enumeration makes the kernel re-probe it.
# We run at plug time, so nothing should be using the disk — but the automount hook fires on
# the same event, so drop any mount it just made rather than yank a mounted filesystem.
SYS="/sys/bus/usb/devices/$DEV"
for blk in "$SYS"/*/host*/target*/*/block/*; do
  [ -e "$blk" ] || continue
  b="$(basename "$blk")"
  for p in /dev/"$b" /dev/"$b"[0-9]*; do
    [ -b "$p" ] || continue
    t="$(findmnt -rn -S "$p" -o TARGET 2>/dev/null | head -1)"
    [ -n "$t" ] && { umount -l "$t" 2>/dev/null && log "unmounted $t before re-enumeration"; }
  done
done
sync
[ -w "$SYS/authorized" ] || { log "no $SYS/authorized — nothing to re-enumerate with"; exit 0; }
log "UAS disabled for $VID:$PID — re-enumerating $DEV (will fall back to usb-storage)"
echo 0 >"$SYS/authorized" 2>/dev/null
sleep 2
echo 1 >"$SYS/authorized" 2>/dev/null
UASOFF
    run chmod +x /usr/local/bin/nas-uas-off.sh
    # Separate rules file on purpose: turning automount OFF must not turn this protection off.
    write_file /etc/udev/rules.d/99-nas-uas-off.rules <<'RULES'
# nas-wizard: any USB bridge that CAN do UAS is switched to usb-storage (see install_uas_off).
# Match by ID_USB_INTERFACES: ":080662:" = mass-storage/UAS. A bridge without UAS lacks it,
# and the rule leaves it alone; ordinary flash drives (only ":080650:" = BOT) are untouched too.
# systemd-run --no-block is mandatory: the script re-enumerates the device and waits, while udev
# kills its children on timeout — udev must not be blocked.
ACTION=="add", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ENV{ID_USB_INTERFACES}=="*:080662:*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/nas-uas-off.sh $env{ID_VENDOR_ID} $env{ID_MODEL_ID} %k"
RULES
    run udevadm control --reload-rules
    uas_seed_cmdline
}

install_usb_timeout() {
    # SCSI command timeout for USB disks: 30 s (default) → 180 s. A safety-critical rule
    # that must exist on EVERY box independently of automount (it used to live inside the
    # automount rules file, so a default install — or toggling automount off — lost it).
    # A 2.5" SMR disk under sustained writes goes into an internal shuffle and may not
    # respond for a minute; at 30 s the kernel declares an error, error recovery doesn't
    # help, the disk goes OFFLINE and ext4 flips to emergency read-only MID backup
    # (real case 2026-07-12: Ugreen RTL9210 + ST4000LM024, the command hung 68 s while
    # SMART stayed clean). %p = sysfs path of the block device; device/timeout is the scsi
    # device behind it.
    write_file /etc/udev/rules.d/99-nas-usbtimeout.rules <<'RULES'
# nas-wizard: raise the SCSI command timeout for USB disks to 180 s (see install_usb_timeout).
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="sd*", ENV{ID_USB_DRIVER}=="?*", RUN+="/bin/sh -c 'echo 180 > /sys/%p/device/timeout'"
RULES
    run udevadm control --reload-rules
    run udevadm trigger --subsystem-match=block --action=change 2>/dev/null || true
}
# ---------------------------------------------------------------------------
# Local touch screen (DSI panel) — kiosk dashboard on the box itself
# ---------------------------------------------------------------------------
# No panel driver needed: on current PiOS it is brought up by the kernel
# vc4 via display_auto_detect=1 (Waveshare 4.3" DSI 800x480 reports DSI-1 connected,
# touch arrives as edt_ft5x06 on i2c). We install the RENDERER: cage (minimal
# Wayland compositor for a kiosk, no desktop) + chromium pointed
# at http://127.0.0.1/screen — nas-web serves this page BEFORE the auth gate, but only
# on loopback, so the screen never asks for a password and it can't be opened from the LAN.
screen_present() {
    local f
    for f in /sys/class/drm/card*-DSI-*/status; do
        [ -e "$f" ] && [ "$(cat "$f" 2>/dev/null)" = "connected" ] && return 0
    done
    # some panels are visible only via the backlight
    for f in /sys/class/backlight/*/brightness; do [ -e "$f" ] && return 0; done
    return 1
}
install_screen() {
    if ! screen_present; then
        info "screen not connected — not installing the kiosk"
        return 0
    fi
    install_packages "screen" cage chromium seatd
    # the kiosk session draws to DRM and reads touch directly
    run usermod -aG video,input,render "$TARGET_USER"

    # true backlight-off pokes the panel's ATTINY over /dev/i2c-* (PC_LED_EN bit:
    # PWM=0 alone leaves a glow) — the i2c-dev char device must exist after boot
    write_file /etc/modules-load.d/nas-screen.conf <<'EOF'
i2c-dev
EOF
    run modprobe i2c-dev || true

    # Chromium obeys ONLY managed policy: the --disable-features=Translate flags
    # don't remove the translate bar (verified on 150.x). The kiosk must show
    # NOTHING popping up — no translation, no password manager, no access prompts.
    run mkdir -p /etc/chromium/policies/managed
    write_file /etc/chromium/policies/managed/nas-kiosk.json <<'EOF'
{
  "TranslateEnabled": false,
  "PasswordManagerEnabled": false,
  "AutofillAddressEnabled": false,
  "AutofillCreditCardEnabled": false,
  "SpellcheckEnabled": false,
  "SafeBrowsingEnabled": false,
  "SyncDisabled": true,
  "BrowserSignin": 0,
  "MetricsReportingEnabled": false,
  "SearchSuggestEnabled": false,
  "BookmarkBarEnabled": false,
  "ShowHomeButton": false,
  "PromptForDownloadLocation": false,
  "DefaultNotificationsSetting": 2,
  "DefaultPopupsSetting": 2,
  "DefaultGeolocationSetting": 2
}
EOF

    # Backlight: sysfs is root-only, but night mode and the brightness slider in the
    # panel must be able to change it without root.
    write_file /etc/udev/rules.d/99-nas-backlight.rules <<'EOF'
# nas-wizard: panel backlight — the video group can change brightness without root.
SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chgrp video /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power", RUN+="/bin/chmod 0664 /sys/class/backlight/%k/brightness /sys/class/backlight/%k/bl_power"
EOF
    # A mouse cursor on the wall screen looks like a bug: the compositor draws a pointer
    # as soon as a USB dongle is plugged into the box (keyboards declare themselves as a mouse too).
    # CSS cursor:none and XCURSOR_SIZE don't remove it — we kill the pointer itself at the
    # libinput level. Keyboard and touch keep working.
    write_file /etc/udev/rules.d/99-nas-nopointer.rules <<'EOF'
# nas-wizard: the kiosk needs ONLY a finger. We give the compositor nothing else.
# Otherwise it draws a cursor: the pointer turns out to be the dongle's mouse, its "Consumer Control"
# (which has relative axes but no ID_INPUT_MOUSE label), and even the HDMI jacks on Pi 4
# (ID_INPUT_POINTINGSTICK). The rule affects only libinput (the kiosk session) —
# console and SSH are untouched.
SUBSYSTEM=="input", ENV{ID_INPUT}=="1", ENV{ID_INPUT_TOUCHSCREEN}!="1", ENV{LIBINPUT_IGNORE_DEVICE}="1"
EOF
    run udevadm control --reload-rules
    run udevadm trigger --subsystem-match=backlight --action=add
    run udevadm trigger --subsystem-match=input --action=add

    # Empty cursor: wlroots draws a pointer in the center even when there's no mouse.
    # Drop in a theme with a transparent 1x1 Xcursor (generated in place — no need for xcursorgen).
    run mkdir -p /usr/share/icons/nas-blank/cursors
    python3 - <<'PYCUR'
import struct, os
d = "/usr/share/icons/nas-blank/cursors"
size, w, h = 24, 1, 1
img = struct.pack("<IIIIIIIII", 36, 0xfffd0002, size, 1, w, h, 0, 0, 0) + struct.pack("<I", 0)
hdr = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
toc = struct.pack("<III", 0xfffd0002, size, 28)
open(os.path.join(d, "left_ptr"), "wb").write(hdr + toc + img)
for n in ("default", "pointer", "arrow", "text", "hand1", "hand2", "watch"):
    p = os.path.join(d, n)
    if not os.path.exists(p):
        os.symlink("left_ptr", p)
open("/usr/share/icons/nas-blank/index.theme", "w").write(
    "[Icon Theme]\nName=nas-blank\nComment=Empty cursor for the NAS kiosk\n")
PYCUR

    run mkdir -p /var/lib/nas-screen
    run chown "$TARGET_USER:$TARGET_USER" /var/lib/nas-screen
    # the kiosk loads the panel on whatever port nas-web actually binds (default 80)
    local SCR_PORT
    SCR_PORT="$(sed -n 's/^Environment=NAS_WEB_PORT=//p' /etc/systemd/system/nas-web.service 2>/dev/null | tail -n1)"
    SCR_PORT="${SCR_PORT:-80}"
    write_file /etc/systemd/system/nas-screen.service <<EOF
[Unit]
Description=NAS local screen — cage + chromium kiosk on the DSI panel
After=nas-web.service systemd-user-sessions.service
Wants=nas-web.service
# cage takes the VT — getty on tty1 would fight it for the screen
Conflicts=getty@tty1.service
After=getty@tty1.service

[Service]
Type=simple
User=$TARGET_USER
# PAMName=login gives the process a logind session on seat0 (libseat -> DRM master)
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
StandardInput=tty-fail
StandardOutput=journal
StandardError=journal
Environment=XDG_SESSION_TYPE=wayland
# mouse cursor: the compositor draws its pointer if a USB mouse is plugged into the box
# (a keyboard dongle counts as a mouse too). CSS cursor:none doesn't remove it — we kill it
# via cursor size, while the mouse itself keeps working.
Environment=XCURSOR_SIZE=1
Environment=XCURSOR_THEME=nas-blank
ExecStart=/usr/bin/cage -d -- /usr/bin/chromium \\
  --kiosk --ozone-platform=wayland --touch-events=enabled \\
  --user-data-dir=/var/lib/nas-screen/chromium \\
  --no-first-run --no-default-browser-check --noerrdialogs --disable-infobars \\
  --disable-session-crashed-bubble --hide-crash-restore-bubble --disable-pinch \\
  --overscroll-history-navigation=0 --password-store=basic \\
  --check-for-update-interval=31536000 --disable-component-update \\
  http://127.0.0.1:${SCR_PORT}/screen
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    run systemctl daemon-reload
    enable_service nas-screen
    info "screen enabled (cage+chromium -> http://127.0.0.1/screen, tty1)"
}
api_screen() {
    if [ "${NASW_ENABLE:-1}" = "0" ]; then
        run systemctl disable --now nas-screen
        run systemctl start getty@tty1   # return the console to the panel
        info "screen disabled"
        return 0
    fi
    install_screen
}

# Accurate time: chrony instead of systemd-timesyncd
pi_chrony() {
    install_packages "chrony" chrony
    if [ "$DRY_RUN" -eq 0 ]; then
        if systemctl list-unit-files systemd-timesyncd.service >/dev/null 2>&1; then
            systemctl is-active  systemd-timesyncd >/dev/null 2>&1 && run systemctl stop    systemd-timesyncd
            systemctl is-enabled systemd-timesyncd >/dev/null 2>&1 && run systemctl disable systemd-timesyncd
        fi
        if   systemctl list-unit-files chrony.service  >/dev/null 2>&1; then enable_service chrony
        elif systemctl list-unit-files chronyd.service >/dev/null 2>&1; then enable_service chronyd; fi
    fi
    info "chrony enabled, systemd-timesyncd disabled (accurate time sync)"
}
# Adaptive CPU governor by temperature/throttling (for a fanless case)
pi_governor() {
    write_file /usr/local/bin/nas-governor.sh <<'EOF'
#!/bin/bash
# nas-wizard: adaptive CPU governor by temperature/throttling
tz=/sys/class/thermal/thermal_zone0/temp
temp=0; [ -r "$tz" ] && temp=$(( $(cat "$tz") / 1000 ))
thr_hex="$(vcgencmd get_throttled 2>/dev/null | sed 's/.*=//')"
cur=$(( ${thr_hex:-0} & 0xf ))
gov=ondemand
if [ "$temp" -ge 80 ] || [ "$cur" -ne 0 ]; then gov=powersave; fi
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$g" ] && echo "$gov" > "$g" 2>/dev/null || true
done
EOF
    run chmod +x /usr/local/bin/nas-governor.sh
    write_file /etc/systemd/system/nas-governor.service <<'EOF'
[Unit]
Description=NAS adaptive CPU governor (temp/throttle)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/nas-governor.sh
EOF
    write_file /etc/systemd/system/nas-governor.timer <<'EOF'
[Unit]
Description=Periodic NAS adaptive CPU governor
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
EOF
    run systemctl daemon-reload
    enable_service nas-governor.timer
    info "adaptive CPU governor enabled (every 2 min: ≥80°C or throttle → powersave, else ondemand)"
}

stage_pi() {
    echo; echo "=== Stage 5: Pi tuning ==="
    log "--- stage_pi start ---"
    local cfg temp throttled
    cfg="$(boot_config_path)"
    temp="$(vcgencmd measure_temp 2>/dev/null | sed 's/temp=//')"
    throttled="$(vcgencmd get_throttled 2>/dev/null)"

    local raw
    raw="$(ui_checklist "Pi tuning (hardware)" \
        "Current temp: ${temp:-?}  throttle: ${throttled:-?}\nCheck the actions (config.txt/cmdline edits require a reboot):" \
        "usbpower" "USB max current — power for USB disks (Pi5)" ON \
        "trim"     "Enable fstrim.timer (TRIM for SSD/NVMe)" ON \
        "pcie3"    "PCIe Gen3 for NVMe — faster, but out of spec" OFF \
        "cgroup"   "Memory cgroup — memory limits for docker" OFF \
        "sysctl"   "Sysctl tuning (swappiness, somaxconn, tcp)" OFF \
        "zram"     "zram-swap (zstd, 50% RAM)" OFF \
        "chrony"   "chrony instead of timesyncd (accurate time)" OFF \
        "governor" "Adaptive CPU governor by temperature" OFF \
        "eeprom"   "Update EEPROM firmware (rpi-eeprom)" OFF \
        "wifips"   "Disable Wi-Fi power-save (stability)" OFF \
        "watchdog" "Watchdog: auto-reboot on hang" ON)" || { info "cancelled"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    local need_reboot=0
    case "$sel" in *" usbpower "*) pi_usb_power "$cfg"; need_reboot=1 ;; esac
    case "$sel" in *" trim "*)     enable_service fstrim.timer ;; esac
    case "$sel" in *" pcie3 "*)    pi_pcie3 "$cfg"; need_reboot=1 ;; esac
    case "$sel" in *" cgroup "*)   pi_cgroup; need_reboot=1 ;; esac
    case "$sel" in *" sysctl "*)   pi_sysctl ;; esac
    case "$sel" in *" zram "*)     pi_zram ;; esac
    case "$sel" in *" chrony "*)   pi_chrony ;; esac
    case "$sel" in *" governor "*) pi_governor ;; esac
    case "$sel" in *" eeprom "*)   run rpi-eeprom-update -a; need_reboot=1 ;; esac
    case "$sel" in *" wifips "*)   pi_wifi_powersave_off ;; esac
    case "$sel" in *" watchdog "*) pi_watchdog ;; esac

    commit_config "pi-tuning"
    local extra=""
    [ "$need_reboot" -eq 1 ] && extra="

WARNING: config.txt/EEPROM changes take effect after a REBOOT."
    ui_msg "Summary: Pi tuning" "Done.$extra

Check:
  vcgencmd measure_temp
  vcgencmd get_throttled   (0x0 = all good)
  sudo lspci -vv | grep -i speed   (after reboot for PCIe)"
    log "--- stage_pi end ---"
}

# ---------------------------------------------------------------------------
# STAGE 6: Security / basic settings
# ---------------------------------------------------------------------------
sec_unattended() {
    install_packages "security" unattended-upgrades apt-listchanges
    write_file /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    info "unattended-upgrades enabled (security only by default)"
}
sec_journald() {
    run mkdir -p /etc/systemd/journald.conf.d
    # Drop-ins are merged in filename order across /usr/lib and /etc, last wins.
    # Raspberry Pi OS ships 40-rpi-volatile-storage.conf (Storage=volatile) to spare
    # the SD card, so ours must sort after it — hence the 99- prefix, not 00-.
    run rm -f /etc/systemd/journald.conf.d/00-nas.conf
    write_file /etc/systemd/journald.conf.d/99-nas.conf <<'EOF'
[Journal]
# Persistent journal. Without it the log lives in /run and evaporates on power
# loss — exactly the case where the log is the only way to learn what happened.
Storage=persistent
SystemMaxUse=200M
SystemMaxFileSize=50M
EOF
    run mkdir -p /var/log/journal
    run systemd-tmpfiles --create --prefix /var/log/journal
    # log2ram keeps /var/log on tmpfs, so the journal would never reach the disk
    if systemctl is-enabled log2ram >/dev/null 2>&1; then
        warn "log2ram keeps /var/log in RAM — the journal won't survive a power-off"
        run systemctl disable --now log2ram
        info "log2ram disabled for a persistent journal"
    fi
    run systemctl restart systemd-journald
    info "journald: persistent journal, 200M limit"
}
sec_log2ram() {
    # log2ram spares an SD card from write wear. On an NVMe/SSD root it buys nothing
    # and costs every log written since the last sync whenever power is cut — which
    # is precisely when the logs matter. Refuse instead of silently losing them.
    local rootdev
    rootdev="$(findmnt -no SOURCE / 2>/dev/null | sed 's|^/dev/||')"
    case "$rootdev" in
        mmcblk*) ;;
        *) warn "root is not on an SD card (/dev/${rootdev:-?}) — log2ram is unneeded and costs you logs on an emergency power-off"
           return 0 ;;
    esac
    if dpkg -s log2ram >/dev/null 2>&1; then info "log2ram already installed"; return 0; fi
    info "adding the external azlux repository for log2ram"
    run mkdir -p /usr/share/keyrings
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] add azlux key+repo, apt install log2ram"
    else
        wget -qO- https://azlux.fr/repo.gpg 2>>"$LOG" | gpg --dearmor > /usr/share/keyrings/azlux.gpg 2>>"$LOG" || { warn "failed to fetch azlux key"; return 1; }
        echo "deb [signed-by=/usr/share/keyrings/azlux.gpg] http://packages.azlux.fr/debian/ stable main" > /etc/apt/sources.list.d/azlux.list
        run apt-get update
        run apt-get install -y log2ram
    fi
    info "log2ram installed (logs in RAM, flushed to disk on a timer)"
}
sec_ufw() {
    install_packages "firewall" ufw
    # FIRST allow SSH, then enable — so we don't lock ourselves out
    run ufw --force reset
    run ufw default deny incoming
    run ufw default allow outgoing
    if ufw app list 2>/dev/null | grep -q OpenSSH; then run ufw allow OpenSSH; else run ufw allow 22/tcp; fi
    # CRITICAL: the web panel's own port (nas-web) — otherwise enabling the firewall closes
    # access to the panel it was enabled from. Port from the unit, default 80.
    local WEBPORT="${NAS_WEB_PORT:-80}"
    run ufw allow "${WEBPORT}/tcp"
    run ufw allow 5353/udp    # mDNS (avahi) — otherwise <host>.local won't resolve
    # Open share ports if they are installed
    if dpkg -s samba >/dev/null 2>&1; then run ufw allow Samba 2>/dev/null || run ufw allow 445/tcp; fi
    if dpkg -s nfs-kernel-server >/dev/null 2>&1; then run ufw allow 2049/tcp; run ufw allow 111/tcp; fi
    run ufw --force enable
    info "ufw enabled (panel :${WEBPORT}, SSH, shares — if present)"
    warn "docker publishes ports bypassing ufw (iptables) — keep that in mind"
}
sec_fail2ban() {
    install_packages "fail2ban" fail2ban
    write_file /etc/fail2ban/jail.d/nas.conf <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
    enable_service fail2ban
    info "fail2ban enabled (jail sshd)"
}
sec_sshkeys() {
    local akeys="$TARGET_HOME/.ssh/authorized_keys"
    if [ ! -s "$akeys" ]; then
        ui_msg "SSH: unsafe" "User $TARGET_USER has NO SSH keys ($akeys is empty/missing).

Disabling password login would LOCK YOU OUT. Skipping — add a key first:
  ssh-copy-id $TARGET_USER@<pi>"
        warn "no SSH keys found — password login NOT disabled (lockout protection)"
        return 0
    fi
    run mkdir -p /etc/ssh/sshd_config.d
    write_file /etc/ssh/sshd_config.d/99-nas.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
    run systemctl restart ssh 2>/dev/null || run systemctl restart sshd
    info "SSH: password login disabled (keys present)"
}

stage_security() {
    echo; echo "=== Stage 6: Security ==="
    log "--- stage_security start ---"
    local raw
    raw="$(ui_checklist "Security / basic settings" "Check what to configure:" \
        "unattended" "Auto security updates (unattended-upgrades)" ON \
        "journald"   "journald 200M limit (less SD wear)" ON \
        "log2ram"    "log2ram: logs in RAM (external repository)" OFF \
        "ufw"        "ufw firewall (SSH, shares)" OFF \
        "fail2ban"   "fail2ban for SSH" OFF \
        "sshkeys"    "SSH: disable password (keys required!)" OFF)" || { info "cancelled"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" unattended "*) sec_unattended ;; esac
    case "$sel" in *" journald "*)   sec_journald ;; esac
    case "$sel" in *" log2ram "*)    sec_log2ram ;; esac
    case "$sel" in *" fail2ban "*)   sec_fail2ban ;; esac
    case "$sel" in *" ufw "*)        sec_ufw ;; esac   # ufw after shares/fail2ban, to open their ports
    case "$sel" in *" sshkeys "*)    sec_sshkeys ;; esac

    commit_config "security"
    ui_msg "Summary: Security" "Done.

Check:
  sudo ufw status verbose
  sudo fail2ban-client status sshd
  systemctl status unattended-upgrades
  journalctl --disk-usage"
    log "--- stage_security end ---"
}

# ---------------------------------------------------------------------------
# STAGE 7: Network shares (Samba / NFS / Avahi) to /mnt/storage
# ---------------------------------------------------------------------------
shares_samba() {
    # Samba + macOS globals + Finder-icon avahi + the panel-managed include, and
    # nothing else: shares and users are created from the panel (Settings →
    # Sharing), so a fresh box starts with a clean, empty share list.
    install_packages "samba" samba
    install_smb_shares
    enable_service smbd
    systemctl list-unit-files nmbd.service >/dev/null 2>&1 && enable_service nmbd
    info "Samba ready — create shares in the panel: Settings → Sharing (SMB)"
}
shares_nfs() {
    install_packages "nfs" nfs-kernel-server
    local cidr share="/mnt/storage"
    cidr="$(detect_lan_cidr)"; [ -z "$cidr" ] && cidr="192.168.0.0/24"
    cidr="$(ui_input "NFS" "Who may access (subnet):" "$cidr")" || return 0
    local line="$share $cidr(rw,sync,no_subtree_check,root_squash)"
    if ! grep -qsF "$share " /etc/exports; then
        backup_file /etc/exports
        append_line "$line" /etc/exports
        run exportfs -ra
    else
        info "export $share already present in /etc/exports"
    fi
    enable_service nfs-server
    info "NFS: $share -> $cidr"
}
shares_avahi() {
    install_packages "avahi" avahi-daemon
    enable_service avahi-daemon
    info "Avahi/mDNS enabled: $(hostname).local"
}

# macOS-friendly Samba globals + Finder-icon Avahi service + the panel's managed
# shares include. Shares themselves are created/edited from the panel (Settings →
# Sharing), which owns /etc/samba/nas-shares.conf; here we only lay the groundwork
# so a fresh box behaves nicely on macOS even before the tab is opened.
SMB_SHARES_INC="/etc/samba/nas-shares.conf"
SMB_SHARES_AVAHI="/etc/avahi/services/nas-shares.service"
install_smb_shares() {
    install_packages "samba" samba
    install_packages "avahi" avahi-daemon
    if [ ! -f "$SMB_SHARES_INC" ]; then
        write_file "$SMB_SHARES_INC" <<'EOF'
# NAS-OS shared folders — managed by the panel. Do not edit by hand.
[global]
   min protocol = SMB2
   vfs objects = catia fruit streams_xattr
   fruit:metadata = stream
   fruit:model = RackMac
   fruit:posix_rename = yes
   fruit:veto_appledouble = no
   fruit:nfs_aces = no
   fruit:wipe_intentionally_left_blank_rfork = yes
   fruit:delete_empty_adfiles = yes
   load printers = no
   printing = bsd
   disable spoolss = yes
   usershare max shares = 0
EOF
    fi
    # include at the END of smb.conf (never inside [global]; see tm_ensure_include)
    [ -f "$SMB_CONF" ] || write_file "$SMB_CONF" <<'EOF'
[global]
   workgroup = WORKGROUP
   server role = standalone server
EOF
    if [ "$(tail -n1 "$SMB_CONF" 2>/dev/null)" != "include = $SMB_SHARES_INC" ] \
       || [ "$(grep -cF "include = $SMB_SHARES_INC" "$SMB_CONF" 2>/dev/null)" != 1 ]; then
        if [ "$DRY_RUN" -eq 0 ]; then
            backup_file "$SMB_CONF"
            grep -vF "include = $SMB_SHARES_INC" "$SMB_CONF" > "$SMB_CONF.tmp"
            printf '\ninclude = %s\n' "$SMB_SHARES_INC" >> "$SMB_CONF.tmp"
            mv "$SMB_CONF.tmp" "$SMB_CONF"
        fi
    fi
    # hide Debian's default shares ([homes]/[printers]/[print$]) so the browse
    # list is clean — otherwise a Mac sees 'nobody' and printer shares (reversible ';')
    if [ "$DRY_RUN" -eq 0 ] && grep -qE '^\[(homes|printers|print\$)\]' "$SMB_CONF" 2>/dev/null; then
        awk '
            /^\[(homes|printers|print\$)\]/{sec=1; print ";" $0; next}
            /^\[/{sec=0}
            sec && $0!~/^;/ && $0!~/^[[:space:]]*$/{print ";" $0; next}
            {print}
        ' "$SMB_CONF" > "$SMB_CONF.tmp" && mv "$SMB_CONF.tmp" "$SMB_CONF"
    fi
    # Finder → Network icon: advertise SMB + a device model over mDNS
    write_file "$SMB_SHARES_AVAHI" <<'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_smb._tcp</type>
    <port>445</port>
  </service>
  <service>
    <type>_device-info._tcp</type>
    <port>0</port>
    <txt-record>model=RackMac</txt-record>
  </service>
</service-group>
EOF
    enable_service smbd
    systemctl list-unit-files nmbd.service >/dev/null 2>&1 && enable_service nmbd
    enable_service avahi-daemon
    [ "$DRY_RUN" -eq 0 ] && command -v testparm >/dev/null 2>&1 && testparm -s >/dev/null 2>>"$LOG" \
        && smbcontrol all reload-config >/dev/null 2>&1
    info "Samba shares groundwork ready (manage from the panel: Settings → Sharing)"
}

# --------------------------------------------------------------------------- #
#  Time Machine — this NAS as a macOS Time Machine target (Samba + vfs_fruit +
#  Avahi _adisk). COMPLETELY separate from the shared folders: its own include
#  file, its own avahi service, its own folder. Nothing in the shared Samba is touched.
# --------------------------------------------------------------------------- #
TM_CONF="/etc/nas-wizard/timemachine.conf"       # persisted params (in settings backup)
TM_INC="/etc/samba/nas-timemachine.conf"          # dedicated smb include (managed)
TM_AVAHI="/etc/avahi/services/nas-timemachine.service"
SMB_CONF="/etc/samba/smb.conf"

tm_write_share() {
    local path="$1" user="$2" quota="$3" qline=""
    [ "${quota:-0}" -gt 0 ] 2>/dev/null && qline="   fruit:time machine max size = ${quota}G"
    write_file "$TM_INC" <<EOF
# NAS-OS Time Machine — managed by the panel. Do not edit by hand.
[global]
   fruit:aapl = yes
   fruit:model = TimeCapsule8,119
   fruit:metadata = stream
   fruit:posix_rename = yes
   fruit:veto_appledouble = no
   fruit:nfs_aces = no
   fruit:wipe_intentionally_left_blank_rfork = yes
   fruit:delete_empty_adfiles = yes
   min protocol = SMB2

[TimeMachine]
   comment = Time Machine (NAS-OS)
   path = $path
   valid users = $user
   writable = yes
   browseable = yes
   create mask = 0600
   directory mask = 0700
   vfs objects = catia fruit streams_xattr
   fruit:time machine = yes
$qline
EOF
}

tm_write_avahi() {
    write_file "$TM_AVAHI" <<'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service>
    <type>_smb._tcp</type>
    <port>445</port>
  </service>
  <service>
    <type>_device-info._tcp</type>
    <port>0</port>
    <txt-record>model=TimeCapsule8,119</txt-record>
  </service>
  <service>
    <type>_adisk._tcp</type>
    <port>9</port>
    <txt-record>dk0=adVN=TimeMachine,adVF=0x82</txt-record>
    <txt-record>sys=waMa=0,adVF=0x100</txt-record>
  </service>
</service-group>
EOF
}

# add `include = TM_INC` under [global] in smb.conf, once
tm_ensure_include() {
    [ -f "$SMB_CONF" ] || write_file "$SMB_CONF" <<'EOF'
[global]
   workgroup = WORKGROUP
   server role = standalone server
EOF
    # The include MUST be top-level at the END of smb.conf, not inside [global]:
    # an include placed after [global] mis-attributes every global parameter that
    # follows it to the include's last share section (verified with testparm).
    # Normalise idempotently: if it's not already the sole, last line, strip every
    # occurrence (old buggy in-[global] placement included) and re-append at the end.
    if [ "$(tail -n1 "$SMB_CONF" 2>/dev/null)" = "include = $TM_INC" ] \
       && [ "$(grep -cF "include = $TM_INC" "$SMB_CONF" 2>/dev/null)" = 1 ]; then
        :
    elif [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] place Time Machine include at the end of smb.conf"
    else
        backup_file "$SMB_CONF"
        grep -vF "include = $TM_INC" "$SMB_CONF" > "$SMB_CONF.tmp"
        printf '\ninclude = %s\n' "$TM_INC" >> "$SMB_CONF.tmp"
        mv "$SMB_CONF.tmp" "$SMB_CONF"
        info "Time Machine include placed at the end of smb.conf"
    fi
}

tm_apply() {
    local path="${NASW_TM_PATH:-$STORAGE_MNT/TimeMachine}"
    local user="${NASW_TM_USER:-timemachine}"
    local quota="${NASW_TM_QUOTA:-0}"
    local pass="${NASW_TM_PASS:-}"
    case "$path" in /*) : ;; *) warn "path must be absolute"; return 2 ;; esac

    install_packages "samba" samba
    install_packages "avahi" avahi-daemon

    # Dedicated Time-Machine-only account: a system user with no login shell,
    # not the admin user. It only owns the TM folder + is the sole valid user of
    # the TM share, so it gives no access to anything else on the box.
    if ! id "$user" >/dev/null 2>&1; then
        run useradd --system --no-create-home --shell /usr/sbin/nologin "$user" \
            && info "created dedicated Time Machine user: $user" \
            || warn "failed to create user $user"
    fi

    run mkdir -p "$path"
    run chown "$user" "$path"          # owner only; group left as-is, 0700 keeps it private
    run chmod 0700 "$path"

    tm_ensure_include
    tm_write_share "$path" "$user" "$quota"
    tm_write_avahi

    # Samba password (required for the target; the Mac asks for name/password on connect)
    if [ -n "$pass" ] && [ "$DRY_RUN" -eq 0 ]; then
        printf '%s\n%s\n' "$pass" "$pass" | smbpasswd -a -s "$user" >>"$LOG" 2>&1 \
            && info "Samba password for $user set" || warn "failed to set Samba password"
    fi

    # firewall (if ufw is active)
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active; then
        run ufw allow Samba 2>/dev/null || run ufw allow 445/tcp
    fi

    run mkdir -p /etc/nas-wizard
    write_file "$TM_CONF" <<EOF
enabled=1
path=$path
user=$user
quota=$quota
EOF

    enable_service smbd
    systemctl list-unit-files nmbd.service >/dev/null 2>&1 && enable_service nmbd
    run systemctl restart smbd
    enable_service avahi-daemon
    run systemctl reload avahi-daemon 2>/dev/null || run systemctl restart avahi-daemon
    info "Time Machine ready: //$(hostname)/TimeMachine  (folder $path, user $user)"
}

tm_disable() {
    # stop announcing and remove the share; DATA and password are left untouched
    [ -f "$TM_AVAHI" ] && run rm -f "$TM_AVAHI"
    if [ -f "$SMB_CONF" ] && grep -qs "include = $TM_INC" "$SMB_CONF"; then
        backup_file "$SMB_CONF"
        [ "$DRY_RUN" -eq 0 ] && grep -v "include = $TM_INC" "$SMB_CONF" > "$SMB_CONF.tmp" && mv "$SMB_CONF.tmp" "$SMB_CONF"
    fi
    if [ -f "$TM_CONF" ] && [ "$DRY_RUN" -eq 0 ]; then
        sed -i 's/^enabled=.*/enabled=0/' "$TM_CONF"
    fi
    run systemctl restart smbd 2>/dev/null
    run systemctl reload avahi-daemon 2>/dev/null || true
    info "Time Machine disabled (data preserved)"
}

# reapply on a fresh system stage if it was configured (survives reinstall once
# the settings backup restored /etc/nas-wizard/timemachine.conf)
tm_reapply_if_configured() {
    [ -f "$TM_CONF" ] || return 0
    # shellcheck disable=SC1090
    . "$TM_CONF" 2>/dev/null || return 0
    [ "${enabled:-0}" = "1" ] || return 0
    NASW_TM_PATH="${path:-}" NASW_TM_USER="${user:-}" NASW_TM_QUOTA="${quota:-0}" tm_apply
}

stage_shares() {
    echo; echo "=== Stage 7: Network shares ==="
    log "--- stage_shares start ---"
    local raw
    raw="$(ui_checklist "Network shares" "Access to /mnt/storage over the network:" \
        "samba" "Samba (SMB) — Windows/Mac/phone" OFF \
        "nfs"   "NFS — Linux clients" OFF \
        "avahi" "Avahi/mDNS — visible as <host>.local" ON)" || { info "cancelled"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" samba "*) shares_samba ;; esac
    case "$sel" in *" nfs "*)   shares_nfs ;; esac
    case "$sel" in *" avahi "*) shares_avahi ;; esac

    ui_msg "Summary: Network shares" "Done.

Check:
  smbclient -L localhost -U <user>      (Samba)
  showmount -e localhost                 (NFS)
  avahi-browse -a                        (mDNS)

If ufw is enabled, the share ports are already open (on a repeated run of stage 6)."
    log "--- stage_shares end ---"
}

# ---------------------------------------------------------------------------
# STAGE 8: Backups and monitoring (SMART alerts, health)
# ---------------------------------------------------------------------------
# smartd on a box with no SMART disks: DEVICESCAN finds nothing, smartd exits
# with code 17 ("No devices to monitor"), systemd marks the unit failed, and the
# panel shows it in the list of failed services. Yet NOTHING is broken — there is
# simply nothing to monitor (Pi booted from an SD card, no NVMe slot at all, disks
# not plugged in yet). smartmontools is in STACK_PACKAGES, and Debian enables its
# unit on install, so this hits EVERY fresh box before the first disk — not just bk_smartd.
#
# Two layers, because "nothing to monitor" comes in two kinds:
#   1. no disks at all -> ConditionPathExistsGlob prevents starting entirely
#      ('|' = triggering condition: it is enough for one of the two to hold);
#   2. a disk exists but does not report SMART (flash drive) -> smartd still exits with 17.
#      SuccessExitStatus alone is not enough: with the default Type=notify, exiting BEFORE
#      READY=1 yields Result=protocol -> failed, whatever SuccessExitStatus says (verified).
#      smartd already runs with -n (no fork), and nothing lines up behind it,
#      so Type=simple is safe and makes 17 a clean exit.
# The udev rule brings smartd up as soon as a real disk appears. Only start and
# only if not running: a blind restart on every add would re-poll ALL disks and
# wake sleeping ones.
install_smartd_guard() {
    systemctl list-unit-files smartmontools.service >/dev/null 2>&1 || return 0
    run mkdir -p /etc/systemd/system/smartmontools.service.d
    write_file /etc/systemd/system/smartmontools.service.d/nas-nodisk.conf <<'EOF'
[Unit]
# nas-wizard: no SMART disk at all (Pi with just an SD card) — do not start at all.
ConditionPathExistsGlob=|/dev/sd*
ConditionPathExistsGlob=|/dev/nvme*n*

[Service]
# nas-wizard: a disk exists but does not report SMART (flash drive) — smartd exits with code 17.
# Type=notify turns an early exit into Result=protocol (failed) regardless of
# SuccessExitStatus, so we switch to simple: smartd already runs with -n (no fork).
Type=simple
SuccessExitStatus=17
EOF
    write_file /etc/udev/rules.d/99-nas-smartd.rules <<'RULES'
# nas-wizard: bring smartd up when the first real disk appears (before that the unit
# is skipped by ConditionPathExistsGlob). Only start and only if not running:
# a restart on every add would re-poll ALL disks and wake sleeping ones.
ACTION=="add", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", KERNEL=="sd*|nvme*", RUN+="/bin/sh -c 'systemctl is-active --quiet smartmontools.service || systemctl start --no-block smartmontools.service'"
RULES
    run udevadm control --reload-rules
    run systemctl daemon-reload
    # clear the red state left by a boot that happened before this guard existed
    systemctl is-failed smartmontools.service >/dev/null 2>&1 && run systemctl reset-failed smartmontools.service
    return 0
}
bk_smartd() {
    install_packages "smart" smartmontools
    write_file /usr/local/bin/nas-smart-alert.sh <<'ALERT'
#!/usr/bin/env bash
# nas-wizard: called by smartd on a disk problem
LOG=/var/log/nas-smart.log
echo "$(date '+%F %T') SMART ALERT: ${SMARTD_MESSAGE:-unknown} (${SMARTD_DEVICE:-?})" >> "$LOG"
# event/key match the panel monitor's SMART scan ("smart:/dev/sdX") so the
# same failing disk is reported once, not by both watchers
[ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "SMART: disk problem" "${SMARTD_DEVICE:-?}: ${SMARTD_MESSAGE:-error}" 1 smart "smart:${SMARTD_DEVICE:-?}" || true
ALERT
    run chmod +x /usr/local/bin/nas-smart-alert.sh
    install_notify_helper
    install_netguard
    install_motd
    if ! grep -qs 'nas-smart-alert' /etc/smartd.conf 2>/dev/null; then
        backup_file /etc/smartd.conf
        write_file /etc/smartd.conf <<'EOF'
# nas-wizard: monitor all disks, alert via nas-smart-alert.sh
DEVICESCAN -a -o on -S on -n standby,q -s (S/../.././02|L/../../6/03) -W 4,45,55 -m root -M exec /usr/local/bin/nas-smart-alert.sh
EOF
    fi
    install_smartd_guard   # no disks yet -> skip start instead of failing
    enable_service smartd
    info "smartd enabled (alerts -> /var/log/nas-smart.log + ping)"
}
bk_spacetemp() {
    write_file /usr/local/bin/nas-health-check.sh <<'HEALTH'
#!/usr/bin/env bash
# nas-wizard: alert on pool fill and Pi temperature
set -uo pipefail
LOG=/var/log/nas-health.log
DISK_PCT_MAX=90
TEMP_MAX=75
# The panel monitor already watches pool fill and temperature continuously
# (with UI-configurable thresholds) — this hourly check is only the fallback
# for when the panel is down, otherwise every alert arrived twice (RU + EN).
PORT="$(sed -n 's/^Environment=NAS_WEB_PORT=//p' /etc/systemd/system/nas-web.service 2>/dev/null | tail -n1)"
code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:${PORT:-80}/" 2>/dev/null)" || code=000
case "$code" in ''|000|5*) ;; *) exit 0 ;; esac
alert=0; msg=""
if mountpoint -q /mnt/storage; then
    pct=$(df --output=pcent /mnt/storage 2>/dev/null | tr -dc '0-9')
    if [ -n "$pct" ] && [ "$pct" -ge "$DISK_PCT_MAX" ]; then alert=1; msg="$msg disk=${pct}%"; fi
fi
if command -v vcgencmd >/dev/null 2>&1; then
    t=$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.' | cut -d. -f1)
    if [ -n "$t" ] && [ "$t" -ge "$TEMP_MAX" ]; then alert=1; msg="$msg temp=${t}C"; fi
fi
if [ "$alert" -eq 1 ]; then
    echo "$(date '+%F %T') HEALTH ALERT:$msg" >> "$LOG"
    [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "NAS: attention" "Threshold exceeded:$msg" 1 || true
fi
HEALTH
    run chmod +x /usr/local/bin/nas-health-check.sh
    write_file /etc/systemd/system/nas-health.service <<'EOF'
[Unit]
Description=NAS health check (nas-wizard)

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nas-health-check.sh
EOF
    write_file /etc/systemd/system/nas-health.timer <<'EOF'
[Unit]
Description=NAS health check hourly (nas-wizard)

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF
    run systemctl daemon-reload
    run systemctl enable --now nas-health.timer
    info "health timer enabled (disk>90% / temp>75C -> ping+log)"
}
stage_backup() {
    echo; echo "=== Stage 8: Backups and monitoring ==="
    log "--- stage_backup start ---"
    local raw
    raw="$(ui_checklist "Backups and monitoring" "What to configure:" \
        "smartd"    "SMART disk monitoring + alert" ON \
        "spacetemp" "Alert: disk fill and Pi temperature" ON)" || { info "cancelled"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" smartd "*)    bk_smartd ;; esac
    case "$sel" in *" spacetemp "*) bk_spacetemp ;; esac

    commit_config "backup/monitoring"
    ui_msg "Summary: Backups/monitoring" "Done.

Notifications use /etc/nas-wizard/notify.conf (Pushover).

Check:
  systemctl status smartd
  systemctl list-timers 'nas-*'
  cat /var/log/nas-smart.log /var/log/nas-health.log"
    log "--- stage_backup end ---"
}

# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------
main_menu() {
    while true; do
        local choice
        choice="$(ui_menu "NAS Wizard (Raspberry Pi 5)$([ "$DRY_RUN" -eq 1 ] && echo '  [DRY-RUN]')" \
            "Choose a stage. Log: $LOG" \
            "system"   "Stage 1: system preparation (packages, docker, directories)" \
            "disk"     "Stage 2: attach a disk (format -> fstab -> mount)" \
            "mergerfs" "Stage 2b: build/update the mergerfs pool (>=2 disks)" \
            "snapraid" "Stage 3: SnapRAID (conf, sync, timers, notifications)" \
            "docker"   "Stage 4: Docker (find compose folders and bring them up)" \
            "pi"       "Stage 5: Pi tuning (PCIe, USB power, watchdog, temp)" \
            "security" "Stage 6: Security (ufw, fail2ban, SSH, journald)" \
            "shares"   "Stage 7: Network shares (Samba/NFS/Avahi)" \
            "backup"   "Stage 8: Backups and monitoring (SMART, health, restic)" \
            "quit"     "Exit")" || break

        case "$choice" in
            system)   stage_system ;;
            disk)     stage_disk ;;
            mergerfs) stage_mergerfs ;;
            snapraid) stage_snapraid ;;
            docker)   stage_docker ;;
            pi)       stage_pi ;;
            security) stage_security ;;
            shares)   stage_shares ;;
            backup)   stage_backup ;;
            quit|"") break ;;
        esac
    done
    echo "Done. Full log: $LOG"
}

# ---------------------------------------------------------------------------
# Notifications (Pushover) — single helper, called from the wrappers
# ---------------------------------------------------------------------------
NOTIFY_CONF=/etc/nas-wizard/notify.conf
# Set a single KEY="VAL" in notify.conf without clobbering other keys: the file
# has three writers (Healthchecks URL from the wizard, Pushover from api notify and the web UI).
notify_conf_set() {
    local key="$1" val="${2//\"/}"
    log "NOTIFY-CONF: $key"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] notify.conf: %s="%s"\n' "$key" "$val"
        return 0
    fi
    mkdir -p /etc/nas-wizard
    { [ -f "$NOTIFY_CONF" ] && grep -v "^${key}=" "$NOTIFY_CONF"; true
      printf '%s="%s"\n' "$key" "$val"; } > "${NOTIFY_CONF}.tmp"
    mv "${NOTIFY_CONF}.tmp" "$NOTIFY_CONF"
    chmod 600 "$NOTIFY_CONF"
}

install_notify_helper() {
    write_file /usr/local/bin/nas-notify.sh <<'NOTIFY'
#!/usr/bin/env bash
# nas-wizard: alert helper.  nas-notify.sh "Title" "Message" [priority] [event] [key] [journal]
# Prefers the panel's local API (/api/agent/notify): it translates the text,
# writes the event journal and dedups against the panel's own monitor via the
# shared cooldown key, so one incident never yields two pushes (RU + EN).
# Direct Pushover stays as the fallback for when the panel itself is down.
CONF=/etc/nas-wizard/notify.conf
PORT="$(sed -n 's/^Environment=NAS_WEB_PORT=//p' /etc/systemd/system/nas-web.service 2>/dev/null | tail -n1)"
BODY="$(python3 - "${1:-NAS}" "${2:-}" "${3:-0}" "${4:-}" "${5:-}" "${6:-1}" <<'PY' 2>/dev/null
import json, sys
a = sys.argv
print(json.dumps({"title": a[1], "msg": a[2], "priority": a[3],
                  "event": a[4], "key": a[5], "journal": a[6]}))
PY
)"
if [ -n "$BODY" ]; then
  curl -fsS -m 8 --retry 2 --retry-connrefused -H 'Content-Type: application/json' \
    -d "$BODY" "http://127.0.0.1:${PORT:-80}/api/agent/notify" >/dev/null 2>&1 && exit 0
fi
# panel is down or rejected the call -> raw Pushover (untranslated), as before
PUSHOVER_USER=""; PUSHOVER_TOKEN=""
[ -f "$CONF" ] && . "$CONF"
[ -n "$PUSHOVER_USER" ] && [ -n "$PUSHOVER_TOKEN" ] || exit 0
curl -fsS -m 12 --retry 2 \
  --form-string "token=$PUSHOVER_TOKEN" --form-string "user=$PUSHOVER_USER" \
  --form-string "title=${1:-NAS}" --form-string "message=${2:-}" \
  --form-string "priority=${3:-0}" \
  https://api.pushover.net/1/messages.json >/dev/null 2>&1 || true
NOTIFY
    run chmod +x /usr/local/bin/nas-notify.sh
}
# ---------------------------------------------------------------------------
# Network guard: "one active link at a time" + notification on network change.
#
# Why. eth0 and wlan0 live in the same subnet 192.168.1.0/24. When both are up,
# Linux breaks predictably: ARP flux (both interfaces answer ARP for the other's
# address), NM drops the subnet's on-link route on a false ACD conflict, replies
# to neighbours go out via the gateway, the path becomes asymmetric — and the
# router, seeing only half of the TCP session, injects RST. Symptom: ping works,
# HTTP hangs. Cured by keeping exactly one interface active.
# ---------------------------------------------------------------------------
# comitup ships with the Pi image for headless Wi-Fi provisioning, but on a NAS that lives on
# ethernet it does more harm than good: while the wire is up netguard keeps the home Wi-Fi
# profile's autoconnect off, comitup reads "wlan0 has no connection" as "no network" and raises
# a comitup-* hotspot; netguard tears it down; comitup raises it again — dozens of times an hour.
# And when the wire really does blink, wlan0 comes back up INSIDE that hotspot instead of the
# home network (the 2026-07-11 lockout: box alive, LAN unreachable for six hours). The Wi-Fi
# fallback does not need comitup — the home profile lives in NM and netguard brings it up itself.
disable_comitup() {
    local units u c found=0
    # unit names differ across images (comitup / comitup-watch / comitup-web) — disable whichever exist
    units="$(systemctl list-unit-files --no-legend 2>/dev/null | awk '$1 ~ /^comitup/ {print $1}')"
    [ -n "$units" ] || return 0
    for u in $units; do
        run systemctl disable --now "$u" || true
        run systemctl mask "$u" || true
        # comitup dies by SIGKILL on stop -> unit lingers as "failed" and the
        # panel screams about a broken service that is masked and gone forever
        run systemctl reset-failed "$u" || true
        found=1
    done
    # without the daemon the hotspot profiles are dead anyway, but keep them out of the network list
    for c in $(nmcli -t -f NAME connection show 2>/dev/null | grep '^comitup-' || true); do
        run nmcli connection delete "$c" || true
    done
    [ "$found" -eq 1 ] && info "comitup disabled: netguard brings up the Wi-Fi fallback, the emergency hotspot no longer interferes"
    return 0
}

install_netguard() {
    write_file /usr/local/bin/nas-netguard.sh <<'GUARD'
#!/bin/bash
# nas-wizard: one active link at a time + network guard.
# Wired eth0 is primary, Wi-Fi wlan0 is the fallback. While eth0 is really working
# (has carrier, an address and the gateway answers) — Wi-Fi is off. As soon as the
# wire is gone or stalls (macb on Pi 5 sometimes has a TX stall) — Wi-Fi comes back.
set -u
# never hardcode the NIC name: it's eth0 on older Pi but end0 on Pi5/Bookworm, and
# varies by board/kernel. Detect at runtime — first physical non-wireless non-virtual
# iface = wired; first with a wireless/ dir = Wi-Fi. NAS_ETH/NAS_WIFI override.
_ng_eth() {
    for d in /sys/class/net/*; do
        n=${d##*/}
        [ -e "$d/wireless" ] && continue
        case "$n" in lo|docker*|veth*|br-*|virbr*|tap*|tun*|wg*|bond*) continue ;; esac
        { [ -e "$d/device" ] || [ -L "$d/device" ]; } && { echo "$n"; return; }
    done
}
_ng_wifi() { for d in /sys/class/net/*/wireless; do n=${d%/wireless}; echo "${n##*/}"; return; done; }
ETH="${NAS_ETH:-$(_ng_eth)}"; ETH="${ETH:-eth0}"
WIFI="${NAS_WIFI:-$(_ng_wifi)}"; WIFI="${WIFI:-wlan0}"
STATE="${NAS_NETGUARD_STATE:-/var/lib/nas-wizard/netguard.state}"
WSTATE="${NAS_NETGUARD_WIFI_STATE:-/var/lib/nas-wizard/netguard.wifi}"
AVLOG="${NAS_NETGUARD_AVAIL:-/var/lib/nas-wizard/avail.log}"
BEAT="${NAS_NETGUARD_BEAT:-/var/lib/nas-wizard/avail.beat}"
LOCK="${NAS_NETGUARD_LOCK:-/run/nas-netguard.lock}"
GWMISS="${NAS_NETGUARD_GWMISS:-/run/nas-netguard.gwmiss}"
GWMISS_MAX="${NAS_NETGUARD_GWMISS_MAX:-3}"

# args 4-5 (event, key) let the panel dedup this alert against its own
# monitor events (link_changed/ip_changed use the monitor's cooldown keys)
notify(){ [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$1" "$2" "${3:-0}" "${4:-}" "${5:-}" >/dev/null 2>&1 || true; }
logj(){ logger -t nas-netguard -- "$*" 2>/dev/null || true; }

# the timer and NM dispatcher can fire simultaneously
exec 9>"$LOCK" 2>/dev/null || exit 0
flock -n 9 || exit 0

has_nm(){ command -v nmcli >/dev/null 2>&1; }
dev_state(){ nmcli -t -f DEVICE,STATE device 2>/dev/null | awk -F: -v d="$1" '$1==d{print $2; exit}'; }
ip4(){ ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk '{print $4; exit}'; }

gw_miss_reset(){ rm -f "$GWMISS" 2>/dev/null || true; }

# Hard failure (no carrier / no address / no default route) = the wire is gone right now:
# fail over immediately. Soft failure (link is up, but the gateway went quiet) is NOT proof
# of a dead wire — under load the router drops the odd ICMP/ARP (the hourly MySpeed
# speedtest did exactly this every hour on the hour). Failing over on one miss put a second
# address of the SAME subnet on the box for ~15 s — precisely the split-route mess this
# guard exists to prevent. So: believe the gateway only after GWMISS_MAX misses in a row.
eth_healthy(){
  [ -e "/sys/class/net/$ETH" ] || { gw_miss_reset; return 1; }
  [ "$(cat "/sys/class/net/$ETH/carrier" 2>/dev/null || echo 0)" = "1" ] || { gw_miss_reset; return 1; }
  [ -n "$(ip4 "$ETH")" ] || { gw_miss_reset; return 1; }
  local g n
  g="$(ip -4 route show default dev "$ETH" 2>/dev/null | awk '{print $3; exit}')"
  [ -n "$g" ] || { gw_miss_reset; return 1; }
  # the gateway may not answer ICMP — then try reaching it at the link layer
  if ping -c1 -W2 -I "$ETH" "$g" >/dev/null 2>&1 || arping -c1 -w2 -I "$ETH" "$g" >/dev/null 2>&1; then
    gw_miss_reset; return 0
  fi
  n=$(( $(cat "$GWMISS" 2>/dev/null || echo 0) + 1 ))
  echo "$n" > "$GWMISS" 2>/dev/null || true
  if [ "$n" -ge "$GWMISS_MAX" ]; then
    logj "gateway $g silent for $n checks in a row — treating the wire as dead"
    return 1
  fi
  logj "gateway $g did not answer ($n/$GWMISS_MAX) — keeping the wire, waiting for the next check"
  return 0
}

# NM sometimes drops the subnet's on-link route (false ACD conflict with two
# interfaces in the same network). Without it, replies to neighbours go via the gateway.
fix_onlink(){
  local dev="$1" cidr net
  cidr="$(ip4 "$dev")"; [ -n "$cidr" ] || return 0
  net="$(python3 -c 'import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)' "$cidr" 2>/dev/null)"
  [ -n "$net" ] || return 0
  ip -4 route show "$net" dev "$dev" scope link 2>/dev/null | grep -q . && return 0
  ip -4 route add "$net" dev "$dev" proto kernel scope link src "${cidr%%/*}" 2>/dev/null \
    && logj "restored on-link route $net dev $dev"
}

# comitup (headless Wi-Fi provisioning) reacts to a lost home network by
# starting a comitup-* hotspot, then gives each reconnect attempt only ~20 s —
# not enough for a 5 GHz scan after a router reboot — so wlan0 can stay stuck
# in the hotspot forever (box alive, LAN unreachable). Rescue it ourselves:
# stop comitup, give NM one full attempt, bring comitup back either way.
wifi_rescue(){
  has_nm || return 0
  local act home name type now
  act="$(nmcli -g GENERAL.CONNECTION device show "$WIFI" 2>/dev/null)"
  case "$act" in comitup-*) ;; *) rm -f "$WSTATE" 2>/dev/null || true; return 0 ;; esac
  home=""
  while IFS=: read -r name type; do
    case "$name" in ''|comitup*) continue ;; esac
    [ "$type" = "802-11-wireless" ] || continue
    [ "$(nmcli -g 802-11-wireless.mode connection show "$name" 2>/dev/null)" = "ap" ] && continue
    home="$name"; break
  done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
  [ -n "$home" ] || return 0
  FAILS=0; LAST=0
  [ -r "$WSTATE" ] && . "$WSTATE"
  now="$(date +%s)"
  # after 5 straight failures retry every 5 min so the hotspot stays usable
  if [ "${FAILS:-0}" -ge 5 ] && [ $(( now - ${LAST:-0} )) -lt 300 ]; then return 0; fi
  logj "$WIFI stuck in hotspot $act — restoring '$home'"
  systemctl stop comitup >/dev/null 2>&1 || true
  if nmcli --wait 45 connection up "$home" >/dev/null 2>&1; then
    rm -f "$WSTATE" 2>/dev/null || true
    logj "Wi-Fi restored: $home"
    notify "NAS: Wi-Fi restored" "$WIFI returned to network '$home' from the comitup emergency hotspot"
  else
    mkdir -p "$(dirname "$WSTATE")" 2>/dev/null || true
    printf 'FAILS=%s\nLAST=%s\n' "$(( ${FAILS:-0} + 1 ))" "$now" > "$WSTATE"
    logj "failed to restore '$home' (attempt $(( ${FAILS:-0} + 1 )))"
  fi
  systemctl start --no-block comitup >/dev/null 2>&1 || true
}

# A single `nmcli device disconnect` is not enough: the home Wi-Fi profile has autoconnect=yes,
# and NM brings it up again a couple of minutes later — the guard drops it, NM raises it... (real
# case: 63 "disconnecting wlan0" in 12 hours). Every such spike = a second address in the SAME
# subnet for a few seconds — exactly the trouble that makes HTTP hang while ping works.
# So while the wire is alive we turn the home Wi-Fi's autoconnect off entirely, and restore
# it as soon as the wire drops (the guard runs every 30 s — the fallback is not lost).
wifi_autoconnect(){
  local want="$1" name type cur
  has_nm || return 0
  while IFS=: read -r name type; do
    case "$name" in ''|comitup*) continue ;; esac        # do not touch the comitup hotspot
    [ "$type" = "802-11-wireless" ] || continue
    [ "$(nmcli -g 802-11-wireless.mode connection show "$name" 2>/dev/null)" = "ap" ] && continue
    cur="$(nmcli -g connection.autoconnect connection show "$name" 2>/dev/null)"
    [ "$cur" = "$want" ] && continue
    nmcli connection modify "$name" connection.autoconnect "$want" >/dev/null 2>&1 \
      && logj "Wi-Fi autoconnect '$name' -> $want"
  done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
}

if eth_healthy; then
  ACTIVE="$ETH"
  wifi_autoconnect no
  if has_nm && [ "$(dev_state "$WIFI")" = "connected" ]; then
    logj "wire is working — disconnecting $WIFI"
    nmcli device disconnect "$WIFI" >/dev/null 2>&1 || true
  fi
else
  ACTIVE="$WIFI"
  wifi_autoconnect yes            # no wire — Wi-Fi is allowed again as the fallback path
  if has_nm && [ "$(dev_state "$WIFI")" != "connected" ]; then
    logj "no wire — bringing up $WIFI"
    nmcli device connect "$WIFI" >/dev/null 2>&1 || true
    sleep 3
  fi
fi
if [ "$ACTIVE" = "$WIFI" ]; then wifi_rescue; fi
fix_onlink "$ACTIVE"

# Availability journal for status-page bars (read by nas-web /api/glance).
# avail.log is an append-only run-length timeline: "<epoch> up|local|off".
#   up    = global IPv4 + default gateway answers (NAS reachable from LAN)
#   local = box alive but no usable network (e.g. stuck in comitup hotspot)
#   off   = gap between heartbeats (powered off / crashed), written on wake
avail_track(){
  local now state ip gw beat last boot
  now="$(date +%s)"
  state=local
  ip="$(ip4 "$ACTIVE")"
  if [ -n "$ip" ]; then
    gw="$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')"
    if [ -n "$gw" ] && { ping -c1 -W2 "$gw" >/dev/null 2>&1 \
        || arping -c1 -w2 -I "$ACTIVE" "$gw" >/dev/null 2>&1; }; then
      state=up
    fi
  fi
  mkdir -p "$(dirname "$AVLOG")" 2>/dev/null || true
  beat="$(cat "$BEAT" 2>/dev/null || true)"
  case "$beat" in *[!0-9]*|'') beat="" ;; esac
  # RTC-less Pi: right after boot the clock is STALE (restored from disk, hours
  # in the past) until NTP syncs. A run in that window must not touch the journal:
  # it would rewind beat into the past, erasing the real "last alive" mark, and
  # the first synced run would then backdate the outage by hours (real case
  # 2026-07-14: overnight shutdown start moved from ~22:00 back to 13:34 and
  # swallowed the whole previous day). Clock behind beat = clock is wrong, skip.
  if [ -n "$beat" ] && [ "$now" -lt $(( beat - 60 )) ]; then
    return 0
  fi
  # A beat older than the current boot means the box went down, even when the
  # whole reboot fits inside the gap threshold (real case 2026-07-11: hard
  # reset + ~2.5 min of downtime < 180 s threshold -> outage never recorded).
  boot="$(( now - $(cut -d. -f1 /proc/uptime) ))"
  if [ -n "$beat" ] && [ $(( beat + 30 )) -lt "$now" ] \
     && { [ $(( now - beat )) -gt 90 ] || [ "$beat" -lt "$boot" ]; }; then
    # The line is written with a BACKDATED timestamp, and the journal is read as
    # "interval until the next line" — so it must not land BEFORE the last record. With
    # a stale beat (restored /var/lib, cloned card) that is exactly what happened: an "off"
    # with a timestamp from the past swallowed three hours the journal called "up", and
    # the widget showed two overlapping outages. Clamp it to the end of the journal.
    off_at=$(( beat + 30 ))
    last_ts="$(tail -n1 "$AVLOG" 2>/dev/null | awk '{print $1}')"
    case "$last_ts" in ''|*[!0-9]*) last_ts=0 ;; esac
    [ "$off_at" -gt "$last_ts" ] || off_at=$(( last_ts + 1 ))
    [ "$off_at" -lt "$now" ] && printf '%s off\n' "$off_at" >> "$AVLOG"
  fi
  last="$(tail -n1 "$AVLOG" 2>/dev/null | awk '{print $2}')"
  [ "$last" = "$state" ] || printf '%s %s\n' "$now" "$state" >> "$AVLOG"
  printf '%s' "$now" > "$BEAT"
  # trim: transitions are rare, 20k lines is years of history
  if [ "$(wc -l < "$AVLOG" 2>/dev/null || echo 0)" -gt 20000 ]; then
    tail -n 10000 "$AVLOG" > "$AVLOG.tmp" 2>/dev/null && mv "$AVLOG.tmp" "$AVLOG"
  fi
}
avail_track

# nas-web can hang without exiting (worker threads stuck) — systemd's
# Restart=on-failure never fires because the process is still alive.
# Probe HTTP on localhost every run; any HTTP status (even 401/404) proves
# the server answers, only a timeout / connection failure / 5xx counts as
# down. 3 consecutive failures (~45 s at the 15 s timer) -> restart nas-web.
# No reboot escalation on purpose: if the panel code itself is broken,
# rebooting would not fix it and would kill SSH sessions mid-repair.
web_selfheal(){
  systemctl is-active --quiet nas-web 2>/dev/null || return 0
  local st=/run/nas-web.fail code n port
  port="$(sed -n 's/^Environment=NAS_WEB_PORT=//p' /etc/systemd/system/nas-web.service 2>/dev/null | tail -n1)"
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:${port:-80}/" 2>/dev/null)" || code=000
  case "$code" in
    ''|000|5*) ;;
    *) rm -f "$st" 2>/dev/null; return 0 ;;
  esac
  n="$(cat "$st" 2>/dev/null || echo 0)"
  case "$n" in *[!0-9]*|'') n=0 ;; esac
  n=$(( n + 1 ))
  if [ "$n" -ge 3 ]; then
    printf '0' > "$st"
    # ask python to dump all thread stacks to the journal first (faulthandler
    # on SIGUSR1) so we learn WHERE it hung, then restart
    local pid; pid="$(systemctl show -p MainPID --value nas-web 2>/dev/null)"
    case "$pid" in ''|0|*[!0-9]*) ;; *) kill -USR1 "$pid" 2>/dev/null || true; sleep 1 ;; esac
    systemctl --no-block try-restart nas-web 2>/dev/null || true
    logj "panel not answering on localhost (HTTP $code) — restarting nas-web"
    notify "Panel hung" "nas-web did not answer on localhost (HTTP $code) — restarted automatically" 1
  else
    printf '%s' "$n" > "$st"
  fi
}
web_selfheal

# interface/address change -> Pushover. The first run only records the state,
# so that install and reboot don't spam notifications.
CUR_IF="$ACTIVE"
CUR_IP="$(ip4 "$ACTIVE")"; CUR_IP="${CUR_IP%%/*}"
[ -n "$CUR_IP" ] || exit 0
OLD_IF=""; OLD_IP=""
[ -r "$STATE" ] && . "$STATE"
if [ "$OLD_IF" != "$CUR_IF" ] || [ "$OLD_IP" != "$CUR_IP" ]; then
  if [ -n "$OLD_IF" ] && [ "$OLD_IF" != "$CUR_IF" ]; then
    notify "NAS: network change" "Active interface: $OLD_IF → $CUR_IF" 0 link_changed
  fi
  if [ -n "$OLD_IP" ] && [ "$OLD_IP" != "$CUR_IP" ]; then
    notify "NAS: IP changed" "Was $OLD_IP → now $CUR_IP" 0 ip_changed
  fi
  mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
  printf 'OLD_IF=%s\nOLD_IP=%s\n' "$CUR_IF" "$CUR_IP" > "$STATE"
  logj "active link $CUR_IF ($CUR_IP)"
fi
GUARD
    run chmod +x /usr/local/bin/nas-netguard.sh
    run mkdir -p /var/lib/nas-wizard

    # instant reaction to a link change. Calling nmcli directly from here is not allowed:
    # NM waits for the dispatcher script to finish, and nmcli waits for NM's reply -> deadlock.
    run mkdir -p /etc/NetworkManager/dispatcher.d   # parent write_file doesn't create it
    write_file /etc/NetworkManager/dispatcher.d/50-nas-netguard <<'DISP'
#!/bin/bash
# nas-wizard: poke the network guard on a link state change (asynchronously!)
case "${2:-}" in up|down|carrier-up|carrier-down|dhcp4-change) ;; *) exit 0 ;; esac
# poke for any real NIC (eth0/end0/wlan0/…); ignore only virtual interfaces
case "${1:-}" in lo|docker*|veth*|br-*|virbr*|tap*|tun*|wg*) exit 0 ;; esac
systemctl start --no-block nas-netguard.service >/dev/null 2>&1 || true
exit 0
DISP
    run chmod 755 /etc/NetworkManager/dispatcher.d/50-nas-netguard
    # same trap: NM runs everything placed in dispatcher.d
    run rm -f /etc/NetworkManager/dispatcher.d/50-nas-netguard.bak.*

    write_file /etc/systemd/system/nas-netguard.service <<'UNIT'
[Unit]
Description=NAS: single active link + network guard
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nas-netguard.sh
UNIT

    write_file /etc/systemd/system/nas-netguard.timer <<'UNIT'
[Unit]
Description=NAS: periodic network check

[Timer]
OnBootSec=45s
OnUnitActiveSec=30s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

    # Safety for the window while both interfaces are up: each answers ARP only
    # for its own address and announces its own (otherwise ARP-flux poisons neighbours' cache).
    write_file /etc/sysctl.d/99-nas-arp.conf <<'SYSCTL'
# nas-wizard: two interfaces in one subnet — without this ARP-flux is possible
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
SYSCTL
    run sysctl -q --system

    run systemctl daemon-reload
    run systemctl enable --now nas-netguard.timer
    info "network guard enabled: wired primary, Wi-Fi backup, notifications on IP change"
    disable_comitup
    # warn honestly: if both links are up now, Wi-Fi will be disabled,
    # and a session opened over it (SSH/panel) will drop — reconnect via the wired address
    local _eth _wifi
    _eth="$(for d in /sys/class/net/*; do n=${d##*/}; [ -e "$d/wireless" ] && continue; case "$n" in lo|docker*|veth*|br-*|virbr*|tap*|tun*|wg*) continue;; esac; { [ -e "$d/device" ] || [ -L "$d/device" ]; } && { echo "$n"; break; }; done)"; _eth="${_eth:-eth0}"
    _wifi="$(for d in /sys/class/net/*/wireless; do n=${d%/wireless}; echo "${n##*/}"; break; done)"; _wifi="${_wifi:-wlan0}"
    if [ "$(cat /sys/class/net/$_eth/carrier 2>/dev/null || echo 0)" = "1" ] \
       && nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -q "^$_wifi:connected$"; then
        warn "cable connected — Wi-Fi will be disabled. If you connected over Wi-Fi, the session will drop; reconnect via <host>.local"
    fi
}

# ---------------------------------------------------------------------------
# SSH login greeting (MOTD).
# sshd here runs with PrintMotd=no — the text is drawn by pam_motd from /etc/update-motd.d/,
# so we put our block there instead of editing /etc/motd (which we blank out: the Debian
# legal wall of text just gets in the way on a NAS; write_file makes a backup).
# ---------------------------------------------------------------------------
install_motd() {
    # create the user text only if it doesn't exist yet — don't overwrite edits
    run mkdir -p /etc/nas-wizard
    if [ ! -f /etc/nas-wizard/motd.txt ]; then
        write_file /etc/nas-wizard/motd.txt <<'TXT'
NAS-OS - home NAS on Raspberry Pi 5

  Panel        {panel}
  Data pool    /mnt/storage          Stacks  ~/services
  Panel logs   journalctl -u nas-web -f

  Always power off with `sudo poweroff` - SnapRAID hates sudden power loss.
TXT
    fi
    [ -f /etc/nas-wizard/motd.conf ] || write_file /etc/nas-wizard/motd.conf <<'CONF'
# nas-wizard: what to show on SSH login
MOTD_LOGO=1
MOTD_TEXT=1
MOTD_INFO=1
# third-party greeting pieces (applied by nas-web at startup and on save):
MOTD_UNAME=1
MOTD_LASTLOG=1
CONF

    run mkdir -p /etc/update-motd.d
    write_file /etc/update-motd.d/20-nas-os <<'MOTD'
#!/bin/bash
# nas-wizard: SSH login greeting.
# NOTE: runs on EVERY login. Only cheap commands; nothing that
# wakes sleeping disks (no smartctl/hdparm) and nothing that touches the network.
CONF=/etc/nas-wizard/motd.conf
TXT=/etc/nas-wizard/motd.txt
MOTD_TEXT=1; MOTD_INFO=1
[ -r "$CONF" ] && . "$CONF"

# NO_COLOR=1 — for the preview in the web panel
if [ -n "${NO_COLOR:-}" ]; then
  B=""; D=""; G=""; Y=""; R=""
else
  B=$'\033[1;36m'; D=$'\033[2;37m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[0m'
fi
# labels in latin: printf measures bytes, Cyrillic would break column alignment
row(){ printf '  %s%-12s%s %s\n' "$D" "$1" "$R" "$2"; }

# ---- values. Computed once: used both by the custom text (via tokens) and the summary.
V_HOST="$(hostname)"
V_UPTIME="$(uptime -p 2>/dev/null | sed 's/^up //')"
V_LOAD="$(awk '{printf "%s %s %s", $1, $2, $3}' /proc/loadavg 2>/dev/null)"
V_TEMP=""; V_TEMPC=""
t=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
[ -n "$t" ] && { V_TEMPC=$((t/1000)); V_TEMP="${V_TEMPC}C"; }
V_MEM="$(free -h 2>/dev/null | awk '/^Mem:/{printf "%s of %s", $3, $2}')"
V_DATE="$(date '+%Y-%m-%d')"
V_TIME="$(date '+%H:%M')"

usage(){ df -h --output=used,size,pcent "$1" 2>/dev/null | tail -1 | awk '{printf "%s of %s (%s)", $1, $2, $3}'; }
V_SYSTEM="$(usage /)"
V_POOL=""
findmnt -n /mnt/storage >/dev/null 2>&1 && V_POOL="$(usage /mnt/storage)"

# route get only reads the routing table, doesn't touch the network. Parse by keys:
# a route may lack "via", but can have "uid 1000" at the tail.
net="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="dev")d=$(i+1);if($i=="src")s=$(i+1)}}END{print s" "d}')"
V_IP="${net%% *}"; V_IFACE="${net##* }"
[ -n "$V_IP" ] && V_PANEL="http://$V_IP/" || V_PANEL=""

need_containers=0
[ "${MOTD_INFO:-1}" = "1" ] && need_containers=1
[ "${MOTD_TEXT:-1}" = "1" ] && [ -r "$TXT" ] && grep -q '{containers}' "$TXT" 2>/dev/null && need_containers=1
V_CONT=""
if [ "$need_containers" = "1" ] && command -v docker >/dev/null 2>&1; then
  V_CONT="$(timeout 3 docker ps -q 2>/dev/null | grep -c .)"
fi

# ---- logo. Raspberry Pi brand colours: berry #C51A4A, leaves #75A928.
# 24-bit codes only when the terminal announced them: ssh forwards TERM but not
# COLORTERM, and Terminal.app cannot parse them. Otherwise nearest 256-palette.
if [ "${MOTD_LOGO:-1}" = "1" ]; then
  if [ -n "${NO_COLOR:-}" ]; then
    PIR=""; PIG=""; PID=""
  elif [ "${COLORTERM:-}" = "truecolor" ] || [ "${COLORTERM:-}" = "24bit" ]; then
    PIR=$'\033[1;38;2;197;26;74m'; PIG=$'\033[1;38;2;117;169;40m'; PID=$'\033[2;37m'
  else
    PIR=$'\033[1;38;5;161m'; PIG=$'\033[1;38;5;106m'; PID=$'\033[2;37m'
  fi
  printf '\n'
  printf '  %s╔╗╔╔═╗╔═╗%s    %s╔═╗╔═╗%s\n' "$PIR" "$R" "$PIG" "$R"
  printf '  %s║║║╠═╣╚═╗%s%s────%s%s║ ║╚═╗%s\n' "$PIR" "$R" "$PID" "$R" "$PIG" "$R"
  printf '  %s╝╚╝╩ ╩╚═╝%s    %s╚═╝╚═╝%s\n' "$PIR" "$R" "$PIG" "$R"
fi

# ---- custom text: substitute tokens. No eval — only substring replacement,
# so commands and variables inside the text are not executed.
if [ "${MOTD_TEXT:-1}" = "1" ] && [ -r "$TXT" ]; then
  txt="$(cat "$TXT")"
  txt="${txt//\{host\}/$V_HOST}"
  txt="${txt//\{uptime\}/$V_UPTIME}"
  txt="${txt//\{load\}/$V_LOAD}"
  txt="${txt//\{temp\}/$V_TEMP}"
  txt="${txt//\{memory\}/$V_MEM}"
  txt="${txt//\{system\}/$V_SYSTEM}"
  txt="${txt//\{pool\}/$V_POOL}"
  txt="${txt//\{ip\}/$V_IP}"
  txt="${txt//\{iface\}/$V_IFACE}"
  txt="${txt//\{panel\}/$V_PANEL}"
  txt="${txt//\{containers\}/$V_CONT}"
  txt="${txt//\{date\}/$V_DATE}"
  txt="${txt//\{time\}/$V_TIME}"
  printf '\n%s\n' "$txt"
fi

[ "${MOTD_INFO:-1}" = "1" ] || exit 0
printf '\n%s%s%s\n' "$B" "$V_HOST" "$R"
row "Uptime" "${V_UPTIME:-?}   ${D}load${R} ${V_LOAD:-?}"
if [ -n "$V_TEMPC" ]; then
  col="$G"; [ "$V_TEMPC" -ge 70 ] && col="$Y"
  row "Temp" "${col}${V_TEMP}${R}"
fi
[ -n "$V_MEM" ] && row "Memory" "$V_MEM"
[ -n "$V_SYSTEM" ] && row "System" "$V_SYSTEM"
[ -n "$V_POOL" ]   && row "Pool"   "$V_POOL"
if [ -n "$V_IP" ]; then
  row "Network" "$V_IP (${V_IFACE:-?})"
  row "Panel" "$V_PANEL"
fi
[ -n "$V_CONT" ] && row "Containers" "$V_CONT running"
printf '\n'
MOTD
    run chmod +x /etc/update-motd.d/20-nas-os
    # write_file makes a backup via `cp -a`, keeping the exec bit, and pam_motd
    # runs ALL files in the directory — the old copy printed the greeting a second time.
    run rm -f /etc/update-motd.d/20-nas-os.bak.*

    # the Debian legal wall of text on top of our block is just noise
    write_file /etc/motd </dev/null
    info "SSH greeting installed (/etc/nas-wizard/motd.txt — custom text)"
}

setup_snapraid_notify_noninteractive() { :; }   # notifications are configured separately (api notify)

# ---------------------------------------------------------------------------
# Non-interactive apply wrappers for the API (reuse proven functions)
# ---------------------------------------------------------------------------
stage_system_apply() {
    export DEBIAN_FRONTEND=noninteractive
    # first thing — update the whole system (as requested: apt update && full-upgrade)
    run apt-get update
    run apt-get full-upgrade -y
    install_packages "NAS stack"  "${STACK_PACKAGES[@]}"
    install_smartd_guard   # smartmontools is installed here too — immediately clear 'failed' when there are no disks
    install_screen         # local touchscreen: installed only if the panel is actually connected
    install_packages "utilities"   "${UTIL_PACKAGES[@]}"
    install_packages "Pi packages" "${PI_PACKAGES[@]}"
    ensure_docker_repo   # docker-ce + compose-plugin from the official Docker repo
    ensure_gh            # GitHub CLI (to push panel code from the box)
    local svc
    for svc in docker; do enable_service "$svc"; done
    systemctl list-unit-files fstrim.timer >/dev/null 2>&1 && enable_service fstrim.timer
    # Hardware watchdog: if the kernel hangs, the Pi auto-reboots instead of sitting
    # dead until someone hits the power button. Applied by default for reliability
    # (still listed in pi-tuning so it can be toggled). Harmless without a watchdog device.
    pi_watchdog
    # USB-SATA bridges always go through usb-storage, never UAS. Not a toggle: UAS error
    # recovery resets the whole device and takes a running backup down with it. See install_uas_off().
    install_uas_off
    install_usb_timeout      # 180s USB SCSI timeout — always on, independent of automount
    # Time Machine target: rebuild the SMB share + Avahi advert if it was configured
    # before (settings backup restores /etc/nas-wizard/timemachine.conf).
    tm_reapply_if_configured
    id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker || run usermod -aG docker "$TARGET_USER"
    run mkdir -p "$STORAGE_MNT" "$DOCKER_ROOT" "$SERVICES_SRC"
    if [ ! -d "$NAS_CONFIG" ]; then run mkdir -p "$NAS_CONFIG/scripts"; run chown -R "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG"; fi
    if [ ! -d "$NAS_CONFIG/.git" ]; then
        run_as git -C "$NAS_CONFIG" init -q
        run_as git -C "$NAS_CONFIG" add -A
        run_as git -C "$NAS_CONFIG" -c user.email="nas@localhost" -c user.name="nas-wizard" commit -q -m "init nas-config" || true
    fi
    [ -n "${NASW_TZ:-}" ]   && run timedatectl set-timezone "$NASW_TZ"
    [ -n "${NASW_HOST:-}" ] && run hostnamectl set-hostname "$NASW_HOST"
    # File previews: cache + nightly warm-up (ffmpeg/pdftoppm installed above in utilities)
    run mkdir -p /var/cache/nas-thumbs
    write_file /etc/systemd/system/nas-thumbs.service <<UNIT
[Unit]
Description=NAS thumbnail cache sweep
[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
CPUQuota=200%
ExecStart=/usr/bin/python3 $SCRIPT_DIR/nas-web.py thumbs-sweep $STORAGE_MNT $TARGET_HOME
UNIT
    write_file /etc/systemd/system/nas-thumbs.timer <<'UNIT'
[Unit]
Description=Nightly NAS thumbnail sweep
[Timer]
OnCalendar=*-*-* 00:20:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT
    run systemctl daemon-reload
    run systemctl enable --now nas-thumbs.timer
    echo "system prepared"
}
# Mount a removable medium into the automount base (explicit action: format/mount).
# Mounts directly, regardless of whether udev automount is enabled.
automount_now() {
    local dev="$1" want="${2:-}" fs label base target opts uid gid i=1
    base="/media/nas"
    [ -f /etc/nas-wizard/automount.conf ] && base="$(. /etc/nas-wizard/automount.conf 2>/dev/null; echo "${BASE:-/media/nas}")"
    if [ "$DRY_RUN" -eq 0 ]; then
        fs="$(blkid -s TYPE -o value "$dev" 2>/dev/null)"
        label="$(blkid -s LABEL -o value "$dev" 2>/dev/null)"
    fi
    label="${label:-$(basename "$dev")}"; label="${label//[^A-Za-z0-9._-]/_}"
    if [ -n "$want" ]; then
        # user specified their own mount point — create it (mkdir -p), don't pick _N
        case "$want" in /*) ;; *) echo "mount point must be an absolute path"; return 2 ;; esac
        case "$want" in *..*) echo "invalid path"; return 2 ;; esac
        target="$want"
        if findmnt -rn "$target" >/dev/null 2>&1; then echo "something is already mounted at $target"; return 2; fi
    else
        target="$base/$label"
        # don't overwrite someone else's directory with data
        while findmnt -rn "$target" >/dev/null 2>&1 || { [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; }; do
            target="$base/${label}_$i"; i=$((i+1)); done
    fi
    run mkdir -p "$target"
    uid="$(id -u "$TARGET_USER" 2>/dev/null || echo 1000)"; gid="$(id -g "$TARGET_USER" 2>/dev/null || echo 1000)"
    case "$fs" in
        vfat|exfat|ntfs) opts="rw,noatime,nofail,uid=$uid,gid=$gid,umask=002" ;;
        *)               opts="rw,noatime,nofail" ;;
    esac
    run mount -o "$opts" "$dev" "$target"
}

api_format_disk() {
    local dev="${NASW_DEV:-}" role="${NASW_ROLE:-data}" fs="${NASW_FS:-ext4}" n mp label parent
    [ -n "$dev" ] || { echo "no disk specified (NASW_DEV)"; return 2; }
    [ -b "$dev" ] || { echo "REJECTED: $dev is not a block device"; return 2; }
    is_protected "$dev" && { echo "REJECTED: $dev is a system disk"; return 2; }
    # protect the system disk's partitions too: check the parent device
    parent="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1)"
    [ -n "$parent" ] && is_protected "/dev/$parent" && { echo "REJECTED: $dev is a partition of the system disk"; return 2; }
    # Do NOT reformat an already-configured disk (it may have temporarily dropped from mount due to nofail).
    # IMPORTANT: this rejection is BEFORE unmounting, otherwise a pool disk would first drop from
    # /mnt/diskN, and the format would be cancelled anyway — leaving the pool broken.
    disk_already_configured "$dev" && { echo "REJECTED: $dev is already configured (its UUID is in /etc/fstab) — format cancelled to avoid data loss"; return 2; }
    # Mounted? We used to reject and ask to unmount by hand — an extra step: the disk
    # is going to be wiped anyway. System and configured disks are cut off above, so here
    # only removable/foreign ones remain — safe to unmount ourselves.
    if disk_in_use "$dev"; then
        echo "unmounting $dev before formatting…"
        unmount_dev "$dev" || {
            echo "REJECTED: $dev is busy, could not unmount. Held by: $(dev_holders "$dev")"
            return 2; }
    fi
    case "$role" in
        parity)
            n="$(next_parity_number)"; mp="/mnt/parity${n}"; label="${NASW_LABEL:-parity${n}}"
            format_and_mount "$dev" "$mp" "$fs" "$label" 2
            echo "done: $dev -> $mp" ;;
        removable|media|usb)
            label="${NASW_LABEL:-USB}"
            make_fs "$dev" "$fs" "$label"
            run partprobe "$dev" 2>/dev/null || true
            automount_now "$dev"
            echo "done: $dev formatted ($fs, label «$label») and mounted" ;;
        *)
            n="$(next_disk_number)"; mp="/mnt/disk${n}"; label="${NASW_LABEL:-disk${n}}"
            format_and_mount "$dev" "$mp" "$fs" "$label" 2
            [ "$(mounted_data_disks | grep -c .)" -ge 2 ] && generate_mergerfs
            echo "done: $dev -> $mp" ;;
    esac
}
# Mount an arbitrary device (flash drive/partition) into the automount base
api_mount_dev() {
    local dev="${NASW_DEV:-}"
    [ -n "$dev" ] || { echo "no disk specified (NASW_DEV)"; return 2; }
    is_protected "$dev" && { echo "REJECTED: $dev is a system disk"; return 2; }
    disk_in_use "$dev" && { echo "$dev is already mounted"; return 0; }
    [ -n "$(blkid -s TYPE -o value "$dev" 2>/dev/null)" ] || { echo "no filesystem on $dev"; return 2; }
    automount_now "$dev" "${NASW_TARGET:-}" || return $?
    echo "mounted $dev"
}
api_label_disk() {
    local dev="${NASW_DEV:-}" label="${NASW_LABEL:-}" fs mp rc
    [ -n "$dev" ] || { echo "no disk specified (NASW_DEV)"; return 2; }
    [ -n "$label" ] || { echo "no label specified (NASW_LABEL)"; return 2; }
    is_protected "$dev" && { echo "REJECTED: $dev is a system disk"; return 2; }
    fs="$(blkid -s TYPE -o value "$dev" 2>/dev/null)"
    [ -n "$fs" ] || { echo "no filesystem on $dev"; return 2; }
    mp="$(findmnt -no TARGET "$dev" 2>/dev/null | head -1)"
    case "$fs" in
        ext2|ext3|ext4) run e2label "$dev" "$label"; rc=$? ;;
        xfs)   [ -z "$mp" ] || { echo "xfs: unmount the partition first"; return 2; }
               command -v xfs_admin >/dev/null || { echo "no xfs_admin (install xfsprogs)"; return 2; }
               run xfs_admin -L "$label" "$dev"; rc=$? ;;
        vfat)  run fatlabel "$dev" "$(printf '%s' "$label" | tr 'a-z' 'A-Z' | cut -c1-11)"; rc=$? ;;
        exfat) run exfatlabel "$dev" "$label"; rc=$? ;;
        ntfs)  run ntfslabel "$dev" "$label"; rc=$? ;;
        btrfs) run btrfs filesystem label "${mp:-$dev}" "$label"; rc=$? ;;
        *)     echo "renaming not supported for filesystem $fs"; return 2 ;;
    esac
    [ "${rc:-1}" -eq 0 ] || { echo "failed to rename $dev ($fs) — see the log"; return 1; }
    run udevadm trigger --settle "$dev" 2>/dev/null || true
    echo "label $dev -> «$label» ($fs)"
}
# Install/update automount for removable media (udev + systemd-run + helper)
install_automount() {
    local user="${1:-$TARGET_USER}" base="${2:-/media/nas}"
    run mkdir -p /etc/nas-wizard "$base"
    write_file /etc/nas-wizard/automount.conf <<EOF
# nas-wizard: automount for removable media
ENABLED=1
BASE="$base"
AM_USER="$user"
OPTS_NATIVE="rw,noatime,nofail"
EOF
    write_file /usr/local/bin/nas-automount.sh <<'AM'
#!/usr/bin/env bash
# nas-wizard: auto mount/unmount of removable media (called from udev via systemd-run)
set -uo pipefail
CONF=/etc/nas-wizard/automount.conf
ENABLED=1; BASE=/media/nas; AM_USER=""; OPTS_NATIVE="rw,noatime,nofail"
[ -f "$CONF" ] && . "$CONF"
LOG=/var/log/nas-automount.log
log(){ printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null; }
poke(){ touch /run/nas-web-refresh 2>/dev/null; }   # wake the panel: disks changed
ACTION="${1:-}"; KDEV="${2:-}"
[ "$ENABLED" = "1" ] || { log "disabled — skip"; exit 0; }
[ -n "$KDEV" ] || exit 0
DEV="/dev/$KDEV"

clean_stale(){    # unmount everything under BASE whose device has disappeared
  findmnt -rn -o TARGET,SOURCE 2>/dev/null | while read -r t s; do
    case "$t" in "$BASE"/*)
      [ -b "$s" ] || { umount -l "$t" 2>>"$LOG" && rmdir "$t" 2>/dev/null; log "removed $t (device gone)"; } ;;
    esac
  done
}
do_add(){
  local fs uuid label name target opts uid gid i=1
  fs="$(blkid -s TYPE -o value "$DEV" 2>/dev/null)"; [ -n "$fs" ] || { log "no filesystem on $DEV"; exit 0; }
  uuid="$(blkid -s UUID -o value "$DEV" 2>/dev/null)"
  grep -qsF "UUID=$uuid" /etc/fstab && { log "$DEV in fstab — skip"; exit 0; }
  findmnt -rn -S "$DEV" >/dev/null 2>&1 && { log "$DEV already mounted"; exit 0; }
  label="$(blkid -s LABEL -o value "$DEV" 2>/dev/null)"
  name="${label:-$KDEV}"; name="${name//[^A-Za-z0-9._-]/_}"
  target="$BASE/$name"
  while findmnt -rn "$target" >/dev/null 2>&1 || { [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; }; do
    target="$BASE/${name}_$i"; i=$((i+1)); done
  mkdir -p "$target"
  uid="$(id -u "${AM_USER:-1000}" 2>/dev/null || echo 1000)"; gid="$(id -g "${AM_USER:-1000}" 2>/dev/null || echo 1000)"
  case "$fs" in
    # vfat: without iocharset the kernel falls back to ascii, and any non-ASCII name fails
    # with EINVAL — a backup to such a stick dies on the first Cyrillic folder (2026-07-12).
    # exfat/ntfs speak UTF-8 by themselves.
    vfat)       opts="rw,noatime,nofail,uid=$uid,gid=$gid,umask=002,iocharset=utf8" ;;
    exfat|ntfs) opts="rw,noatime,nofail,uid=$uid,gid=$gid,umask=002" ;;
    *)          opts="$OPTS_NATIVE" ;;
  esac
  if mount -o "$opts" "$DEV" "$target" 2>>"$LOG"; then log "mounted $DEV ($fs) -> $target"; poke
  else mount "$DEV" "$target" 2>>"$LOG" && { log "mounted(default) $DEV -> $target"; poke; } || { rmdir "$target" 2>/dev/null; log "ERROR mounting $DEV"; }
  fi
}
case "$ACTION" in
  add)    do_add ;;
  remove) clean_stale; poke ;;
  *)      exit 0 ;;
esac
AM
    run chmod +x /usr/local/bin/nas-automount.sh
    write_file /etc/udev/rules.d/99-nas-automount.rules <<'RULES'
# nas-wizard: automount for removable USB media.
# Match by ID_USB_DRIVER (usb-storage/uas), NOT by ID_BUS==usb: USB-SATA bridges
# (UAS) present the disk as ID_BUS=ata, and the old rule didn't fire for them.
ACTION=="add",    SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", ENV{ID_USB_DRIVER}=="?*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/nas-automount.sh add %k"
ACTION=="remove", SUBSYSTEM=="block", ENV{ID_USB_DRIVER}=="?*", RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/nas-automount.sh remove %k"

RULES
    run udevadm control --reload-rules
    run udevadm trigger --subsystem-match=block --action=add 2>/dev/null || true
}
api_automount() {
    local enable="${NASW_ENABLE:-1}" user="${NASW_USER:-$TARGET_USER}" base="${NASW_BASE:-/media/nas}"
    if [ "$enable" = "0" ]; then
        [ -f /etc/nas-wizard/automount.conf ] && run sed -i 's/^ENABLED=.*/ENABLED=0/' /etc/nas-wizard/automount.conf
        run rm -f /etc/udev/rules.d/99-nas-automount.rules
        run udevadm control --reload-rules
        echo "automount disabled"
        return 0
    fi
    install_automount "$user" "$base"
    echo "automount enabled (USB media -> $base)"
}
api_pi() {
    local cfg k; cfg="$(boot_config_path)"
    for k in ${NASW_KEYS:-}; do case "$k" in
        usbpower) pi_usb_power "$cfg" ;;   pcie3) pi_pcie3 "$cfg" ;;
        trim)     enable_service fstrim.timer ;; eeprom) run rpi-eeprom-update -a ;;
        cgroup)   pi_cgroup ;;  sysctl) pi_sysctl ;;  zram) pi_zram ;;
        chrony)   pi_chrony ;;  governor) pi_governor ;;
        wifips)   pi_wifi_powersave_off ;;  watchdog) pi_watchdog ;;
    esac; done
}
api_shares() {
    local k
    for k in ${NASW_KEYS:-}; do case "$k" in
        samba) shares_samba ;; nfs) shares_nfs ;; avahi) shares_avahi ;;
    esac; done
}

# ---------------------------------------------------------------------------
# Modules: comitup / Tailscale / static IP
# ---------------------------------------------------------------------------
mod_comitup() {
    if dpkg -s comitup >/dev/null 2>&1; then echo "comitup already installed"; return 0; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] add davesteele repository + apt install comitup"
        echo "comitup (dry-run)"; return 0
    fi
    warn "comitup manages the network — a brief connection drop over Wi-Fi is possible"
    run mkdir -p /usr/share/keyrings
    if curl -fsSL https://davesteele.github.io/key-366150CE.pub.txt 2>>"$LOG" | gpg --dearmor > /usr/share/keyrings/davesteele.gpg 2>>"$LOG"; then
        echo "deb [signed-by=/usr/share/keyrings/davesteele.gpg] https://davesteele.github.io/comitup/repo comitup main" > /etc/apt/sources.list.d/comitup.list
        run apt-get update
        run apt-get install -y comitup
        run systemctl enable comitup 2>/dev/null || true
        echo "comitup installed (Wi-Fi access point + captive portal)"
    else
        warn "failed to fetch the davesteele key — comitup skipped"
    fi
}
mod_tailscale() {
    if command -v tailscale >/dev/null 2>&1; then echo "tailscale already installed"
    elif [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] install tailscale (get.tailscale.com)"
    else curl -fsSL https://tailscale.com/install.sh 2>>"$LOG" | sh >>"$LOG" 2>&1 || warn "failed to install tailscale"; fi
    echo "Tailscale ready. Log in: sudo tailscale up"
}
mod_staticip() {
    local ip="${NASW_IP:-}" gw="${NASW_GW:-}" dns="${NASW_DNS:-1.1.1.1}" con
    [ -n "$ip" ] || { echo "no IP specified (NASW_IP)"; return 2; }
    con="$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)"
    [ -n "$con" ] || { echo "no active NetworkManager connection found"; return 2; }
    run nmcli connection modify "$con" ipv4.addresses "$ip" ${gw:+ipv4.gateway "$gw"} ipv4.dns "$dns" ipv4.method manual
    run nmcli connection up "$con"
    echo "static IP $ip assigned ($con)"
}
# ---------------------------------------------------------------------------
# API mode (headless, for nas-web.py). No whiptail; confirmations come from the browser.
# Parameters in NASW_* ; output is a human-readable log to stdout, return code 0/≠0.
# ---------------------------------------------------------------------------
api_compose_file() {           # $1=service -> prints the path of the compose file
    local svc="$1" f
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
        [ -f "$SERVICES_SRC/$svc/$f" ] && { echo "$SERVICES_SRC/$svc/$f"; return 0; }
    done
    return 1
}
api_docker() {                 # $1=up|down|restart|pull
    local act="$1" svc="${NASW_SERVICE:-}" file DC
    [ -n "$svc" ] || { echo "no service specified"; return 2; }
    file="$(api_compose_file "$svc")" || { echo "compose file not found: $svc"; return 2; }
    DC="$(docker_compose_cmd)"; [ -n "$DC" ] || { echo "docker compose unavailable"; return 2; }
    echo "== $act $svc =="
    case "$act" in
        up)      run_visible $DC -f "$file" up -d ;;
        down)    run_visible $DC -f "$file" down ;;
        restart) run_visible $DC -f "$file" restart ;;
        pull)    run_visible $DC -f "$file" pull ;;
        *)       echo "unknown action: $act"; return 2 ;;
    esac
}
# Install and start Dockge (stack manager). Stacks live in /opt/stacks.
api_dockge() {
    local dir="${NASW_STACKS_DIR:-/opt/stacks}" src DC
    src="$(api_compose_file dockge)" || { echo "Dockge compose not found"; return 2; }
    run mkdir -p "$dir/dockge" /opt/docker/dockge/data
    run cp -f "$src" "$dir/dockge/compose.yaml"
    info "Dockge → $dir/dockge/compose.yaml"
    DC="$(docker_compose_cmd)"; [ -n "$DC" ] || { echo "docker compose unavailable — run the «System» stage first"; return 2; }
    run_visible $DC -f "$dir/dockge/compose.yaml" up -d
    echo "Dockge started → http://<pi>:5001 (manages stacks in $dir)"
}
# Copy the selected bundled stacks (NASW_KEYS) into the Dockge directory. We don't start them — start from Dockge.
# run a set of functions by keys from NASW_KEYS (space-separated)
api_keys_run() {               # $1=prefix (pi|sec|...) ; calls <prefix>_<key>
    local prefix="$1" k
    for k in ${NASW_KEYS:-}; do
        if declare -F "${prefix}_${k}" >/dev/null; then "${prefix}_${k}"; fi
    done
}
api_notify() {                 # Pushover in /etc/nas-wizard/notify.conf
    notify_conf_set PUSHOVER_USER  "${NASW_PUSER:-}"
    notify_conf_set PUSHOVER_TOKEN "${NASW_PTOKEN:-}"
    install_notify_helper
    echo "Pushover configured"
}
api_state() {                  # brief state for the wizard (JSON)
    local host tz iface
    host="$(hostnamectl --static 2>/dev/null || hostname)"
    tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
    iface="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
    printf '{"host":"%s","tz":"%s","iface":"%s","docker":%s,"data_disks":%s,"parity_disks":%s,"pool":%s,"snapraid":%s,"samba":%s,"nfs":%s,"fail2ban":%s,"ufw":%s,"comitup":%s,"unattended":%s}\n' \
        "$host" "$tz" "$iface" \
        "$(command -v docker >/dev/null 2>&1 && echo true || echo false)" \
        "$(mounted_data_disks | grep -c . )" \
        "$(mounted_parity_disks | grep -c . )" \
        "$(findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1 && echo true || echo false)" \
        "$([ -f /etc/snapraid.conf ] && echo true || echo false)" \
        "$(systemctl is-active smbd >/dev/null 2>&1 && echo true || echo false)" \
        "$(systemctl is-active nfs-kernel-server >/dev/null 2>&1 && echo true || echo false)" \
        "$(systemctl is-active fail2ban >/dev/null 2>&1 && echo true || echo false)" \
        "$(ufw status 2>/dev/null | grep -q 'Status: active' && echo true || echo false)" \
        "$(systemctl is-active comitup >/dev/null 2>&1 && echo true || echo false)" \
        "$([ -f /etc/apt/apt.conf.d/20auto-upgrades ] && echo true || echo false)"
}

run_api() {
    local action="$1"
    # non-interactive UI stubs: confirmations are already done in the browser
    ui_msg(){ :; }; ui_yesno(){ return 0; }; ui_input(){ echo "${3:-}"; }
    ui_password(){ echo "${NASW_PASSWORD:-}"; }; ui_checklist(){ echo ""; }
    case "$action" in
        state)          api_state ;;
        docker-up)      api_docker up ;;
        docker-down)    api_docker down ;;
        docker-restart) api_docker restart ;;
        docker-pull)    api_docker pull ;;
        dockge)         api_dockge ;;
        system)         stage_system_apply ;;
        format-disk)    api_format_disk ;;
        label-disk)     api_label_disk ;;
        mount-dev)      api_mount_dev ;;
        automount)      api_automount ;;
        mergerfs)       generate_mergerfs ;;
        snapraid)       ensure_snapraid_conf && { setup_snapraid_notify_noninteractive; install_snapraid_wrapper; install_snapraid_timers; [ "${NASW_SYNC:-0}" = "1" ] && run_visible snapraid sync; } ;;
        snapraid-sync)  if [ -x /usr/local/bin/nas-snapraid.sh ]; then run_visible /usr/local/bin/nas-snapraid.sh "${NASW_KIND:-sync}"; else echo "SnapRAID not configured — run the Wizard first (SnapRAID stage)"; exit 2; fi ;;
        pi)             api_pi ;;
        security)       api_keys_run sec ;;
        shares)         api_shares ;;
        timemachine)    tm_apply ;;
        timemachine-off) tm_disable ;;
        backup)         api_keys_run bk ;;
        notify)         api_notify ;;
        netguard)       install_netguard ;;
        motd)           install_motd ;;
        comitup)        mod_comitup ;;
        tailscale)      mod_tailscale ;;
        staticip)       mod_staticip ;;
        screen)         api_screen ;;
        *)              echo "unknown api action: $action" >&2; return 2 ;;
    esac
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    require_root "$@"
    ensure_log
    if [ -n "$API_ACTION" ]; then
        run_api "$API_ACTION"
        exit $?
    fi
    ui_init

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "*** --dry-run MODE: no changes are applied, only the action plan ***"
    fi

    case "$FORCE_STAGE" in
        system)   stage_system ;;
        disk)     stage_disk ;;
        mergerfs) stage_mergerfs ;;
        snapraid) stage_snapraid ;;
        docker)   stage_docker ;;
        pi)       stage_pi ;;
        security) stage_security ;;
        shares)   stage_shares ;;
        backup)   stage_backup ;;
        "")       main_menu ;;
        *)        die "unknown stage: $FORCE_STAGE (system|disk|mergerfs|snapraid|docker|pi|security|shares|backup)" ;;
    esac
}

main "$@"
