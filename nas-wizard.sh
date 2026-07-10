#!/usr/bin/env bash
#
# nas-wizard.sh — Wizard настройки NAS на Raspberry Pi 5
#
# Реализованы этапы (по ТЗ):
#   1.  Подготовка системы (NAS-стек + утилиты + Pi-пакеты, cockpit, docker,
#       группы, fstrim, каталоги, hostname/tz)
#   2.  Работа с диском (формат -> fstab -> mount), данные ИЛИ parity
#   2b. mergerfs — пул из >=2 дисков данных (авто при добавлении 2-го диска)
#   3.  SnapRAID — snapraid.conf, sync с защитой от массового удаления,
#       systemd-таймеры (sync ежедневно / scrub еженедельно), уведомления
#   4.  Docker — читает ./services/<service>/*.yml РЯДОМ СО СКРИПТОМ, чеклист
#       "какие поднять", up/down, генерирует deploy.sh ("применить всё разом")
#   5.  Pi-тюнинг — PCIe Gen3, USB max current, memory cgroup, sysctl, zram,
#       watchdog, EEPROM, Wi-Fi powersave, temp/throttle (opt-in чеклист)
#   6.  Безопасность — unattended-upgrades, journald cap, log2ram, ufw, fail2ban,
#       SSH key-only (безопасно: только при наличии ключей)
#   7.  Сетевые шары — Samba / NFS к /mnt/storage + Avahi (mDNS)
#   8.  Бэкапы/мониторинг — smartd-алерты, health-таймер (диск/температура),
#       Уведомления через Pushover. Плюс api-режим для веб-морды (nas-web.py).
#
# Принципы: идемпотентность, --dry-run, логирование, подтверждение разрушительных
# операций, бэкап fstab, защита системного диска, версионирование конфигов в git.
#
# Использование:
#   sudo ./nas-wizard.sh                   # интерактивное меню
#   sudo ./nas-wizard.sh --dry-run         # ничего не меняет, печатает план действий
#   sudo ./nas-wizard.sh --stage snapraid  # прогнать только один этап
#     (этапы: system | disk | mergerfs | snapraid | docker)
#
set -o pipefail

# ---------------------------------------------------------------------------
# Глобальные настройки
# ---------------------------------------------------------------------------
DRY_RUN=0
FORCE_STAGE=""
LOG="/var/log/nas-wizard.log"

# Каталог самого скрипта (docker-сервисы лежат рядом со скриптом, в ./services/)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" 2>/dev/null && pwd || echo "$PWD")"
SERVICES_SRC="$SCRIPT_DIR/services"

# --- Пакеты (whiptail не ставим — он нужен, чтобы этот скрипт вообще работал) ---
# NAS-стек
# docker-ce/compose-plugin ставятся отдельно из официального репо Docker (см. ensure_docker_repo) —
# в репах Debian/RPi OS их нет. Здесь только пакеты, доступные в штатных репозиториях.
STACK_PACKAGES=(cockpit cockpit-storaged cockpit-networkmanager mergerfs snapraid smartmontools)
# Утилиты общего назначения — то, что почти всегда нужно на сервере/NAS
UTIL_PACKAGES=(
  dialog
  libheif-examples   # heif-convert: HEIC с айфона нарезан плитками, ffmpeg берёт лишь одну
  eject              # мягкое извлечение носителя после USB-импорта (power-off гасит весь ридер)
  iputils-arping     # nas-netguard: запасная проверка шлюза, если он молчит на ICMP
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
# Pi-специфичное
PI_PACKAGES=(libraspberrypi-bin raspi-config rpi-eeprom)

# Точки монтирования / каталоги
STORAGE_MNT="/mnt/storage"
DOCKER_ROOT="/opt/docker"          # конфиги контейнеров: /opt/docker/<service>/

# Пользователь, от имени которого настраиваем (не root)
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
    TARGET_USER="$(id -un 1000 2>/dev/null || echo "oleg")"
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -z "$TARGET_HOME" ] && TARGET_HOME="/home/$TARGET_USER"
NAS_CONFIG="$TARGET_HOME/nas-config"

# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
nas-wizard.sh — настройка NAS на Raspberry Pi 5

  --dry-run           Печатать команды, ничего не менять
  --stage system      Этап 1: подготовка системы
  --stage disk        Этап 2: работа с диском (формат/fstab/mount)
  --stage mergerfs    Этап 2b: собрать/обновить пул mergerfs
  --stage snapraid    Этап 3: SnapRAID (conf, sync, таймеры)
  --stage docker      Этап 4: Docker (найти compose-папки и поднять)
  --stage pi          Этап 5: Pi-тюнинг (PCIe, USB-питание, watchdog)
  --stage security    Этап 6: Безопасность (ufw, fail2ban, SSH, journald)
  --stage shares      Этап 7: Сетевые шары (Samba/NFS/Avahi)
  --stage backup      Этап 8: Бэкапы и мониторинг (SMART, health)
  -h, --help          Эта справка
EOF
}

# Headless API-режим для веб-морды (nas-web.py): `nas-wizard.sh api <action>`.
# Параметры приходят через переменные окружения NASW_*, dry-run через NASW_DRYRUN=1.
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
        *) echo "Неизвестный аргумент: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
ts() { date '+%Y-%m-%d %H:%M:%S'; }

log() {
    # Пишем в лог (и на stderr в dry-run для наглядности)
    local msg="$*"
    { printf '%s [%s] %s\n' "$(ts)" "$([ "$DRY_RUN" -eq 1 ] && echo DRY || echo RUN)" "$msg" >>"$LOG"; } 2>/dev/null
}

info()  { echo "  $*"; log "INFO: $*"; }
warn()  { echo "  ! $*" >&2; log "WARN: $*"; }
die()   { echo "  ОШИБКА: $*" >&2; log "ERROR: $*"; exit 1; }

# run — обёртка для МУТИРУЮЩИХ команд (mkfs, mount, systemctl, apt, mkdir ...).
# Читающие команды (lsblk, blkid, df, findmnt) вызываем напрямую — им dry-run не нужен.
run() {
    log "CMD: $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] %s\n' "$*"
        return 0
    fi
    "$@" >>"$LOG" 2>&1
    local rc=$?
    if [ $rc -ne 0 ]; then
        warn "команда завершилась с кодом $rc: $*"
    fi
    return $rc
}

# remove_fstab_mount — удалить из /etc/fstab строки, монтирующие в заданную точку
# (кроме комментариев); нужно перед добавлением новой строки с новым UUID.
remove_fstab_mount() {
    local mp="$1"
    [ -n "$mp" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] удалить старые строки fstab для %s\n' "$mp"
        return 0
    fi
    [ -f /etc/fstab ] || return 0
    awk -v mp="$mp" '$1 ~ /^#/ || $2 != mp' /etc/fstab > /etc/fstab.nastmp \
        && cat /etc/fstab.nastmp > /etc/fstab && rm -f /etc/fstab.nastmp
}

# append_line — идемпотентно дописать строку в файл (для fstab и т.п.)
append_line() {
    local line="$1" file="$2"
    if [ -f "$file" ] && grep -qsF "$line" "$file"; then
        info "уже присутствует в $file, пропускаю"
        return 0
    fi
    log "APPEND -> $file : $line"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] echo %q >> %s\n' "$line" "$file"
        return 0
    fi
    # гарантировать перевод строки в конце файла, иначе новая строка слипнется с последней
    # (порча предыдущей записи fstab → возможен сбой загрузки)
    if [ -s "$file" ] && [ -n "$(tail -c1 "$file")" ]; then
        printf '\n' >>"$file"
    fi
    printf '%s\n' "$line" >>"$file"
}

# run_as — выполнить команду от имени TARGET_USER (для git в его домашке)
run_as() {
    log "CMD(as $TARGET_USER): $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] sudo -u %s %s\n' "$TARGET_USER" "$*"
        return 0
    fi
    sudo -u "$TARGET_USER" "$@" >>"$LOG" 2>&1
}

# run_visible — как run(), но вывод виден пользователю (для долгих ops: snapraid sync)
run_visible() {
    log "CMD(visible): $*"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] %s\n' "$*"
        return 0
    fi
    "$@" 2>&1 | tee -a "$LOG"
    return "${PIPESTATUS[0]}"
}

# write_file — записать файл целиком (контент из stdin). Уважает dry-run, бэкапит существующий.
write_file() {
    local path="$1" content
    content="$(cat)"
    log "WRITE -> $path ($(printf '%s' "$content" | wc -l) строк)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '  [DRY-RUN] записать файл %s (%s строк)\n' "$path" "$(printf '%s\n' "$content" | wc -l)"
        return 0
    fi
    if [ -f "$path" ]; then
        cp -a "$path" "${path}.bak.$(date '+%Y%m%d-%H%M%S')"
    fi
    printf '%s\n' "$content" > "$path"
}

# commit_config — снять снапшот ключевых конфигов в git-репозиторий и закоммитить
commit_config() {
    local msg="$1"
    [ -d "$NAS_CONFIG/.git" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] git commit конфигов: $msg"
        return 0
    fi
    [ -f /etc/fstab ]          && cp -a /etc/fstab          "$NAS_CONFIG/fstab.snapshot"
    [ -f /etc/snapraid.conf ]  && cp -a /etc/snapraid.conf  "$NAS_CONFIG/snapraid.conf"
    chown -R "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG" 2>/dev/null || true
    run_as git -C "$NAS_CONFIG" add -A
    run_as git -C "$NAS_CONFIG" -c user.email="nas@localhost" -c user.name="nas-wizard" commit -q -m "$msg" || true
}

# docker_compose_cmd — определить, какой compose доступен ("docker compose" | "docker-compose")
docker_compose_cmd() {
    if docker compose version >/dev/null 2>&1; then
        echo "docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo "docker-compose"
    else
        echo ""
    fi
}

# install_packages <label> pkg...  — идемпотентно; пропускает уже стоящие и недоступные в репо
install_packages() {
    local label="$1"; shift
    local to_install=() pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 && continue
        if apt-cache show "$pkg" >/dev/null 2>&1; then
            to_install+=("$pkg")
        else
            warn "$label: пакет недоступен в репозитории, пропускаю: $pkg"
        fi
    done
    if [ "${#to_install[@]}" -eq 0 ]; then
        info "$label: всё уже установлено"
        return 0
    fi
    info "$label: устанавливаю (${#to_install[@]}): ${to_install[*]}"
    run apt-get install -y "${to_install[@]}"
}

# ---------------------------------------------------------------------------
# ensure_docker_repo — подключить официальный репозиторий Docker CE и поставить движок.
# Зачем: docker-compose-plugin (v2, «docker compose») и docker-ce НЕ входят в репозитории
# Debian/Raspberry Pi OS — они живут только на download.docker.com. Без этого репо
# docker_compose_cmd пуст → Stage 4, Dockge, deploy.sh, nas-stacks.service — no-op на
# чистой машине. Идемпотентно: повторный запуск лишь досоздаёт недостающее.
# ---------------------------------------------------------------------------
ensure_docker_repo() {
    local keyring=/etc/apt/keyrings/docker.asc
    local list=/etc/apt/sources.list.d/docker.list
    local arch codename
    arch="$(dpkg --print-architecture)"
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"

    # curl + ca-certificates нужны для загрузки GPG-ключа (на RPi OS обычно уже есть)
    install_packages "Docker: зависимости" ca-certificates curl

    # Docker публикует не каждый релиз Debian сразу. Если для нашего codename
    # репозитория ещё нет — откатываемся на bookworm (совместимый бинарь).
    if ! curl -fsS --max-time 10 -o /dev/null "https://download.docker.com/linux/debian/dists/${codename}/Release" 2>/dev/null; then
        warn "Docker: репозиторий для '$codename' пока недоступен, использую bookworm"
        codename="bookworm"
    fi

    run install -m 0755 -d /etc/apt/keyrings
    if [ ! -s "$keyring" ]; then
        run curl -fsSL https://download.docker.com/linux/debian/gpg -o "$keyring"
        run chmod a+r "$keyring"
    fi

    # источник переписываем только если изменился (напр. сменился codename)
    local want="deb [arch=${arch} signed-by=${keyring}] https://download.docker.com/linux/debian ${codename} stable"
    if [ "$(cat "$list" 2>/dev/null)" != "$want" ]; then
        printf '%s\n' "$want" | write_file "$list"
        run apt-get update
    fi

    # убрать конфликтующие distro-пакеты (на чистой машине их нет — no-op)
    local p present=()
    for p in docker.io docker-compose docker-doc podman-docker containerd runc; do
        dpkg -s "$p" >/dev/null 2>&1 && present+=("$p")
    done
    [ "${#present[@]}" -gt 0 ] && run apt-get remove -y "${present[@]}"

    install_packages "Docker CE" docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

# ---------------------------------------------------------------------------
# ensure_gh — GitHub CLI (gh) из официального репозитория cli.github.com.
# gh НЕ входит в репозитории Debian/Raspberry Pi OS — нужен свой источник.
# Пригодится, чтобы пушить код панели в github.com/pelinoleg/nas-os прямо
# с бокса. Идемпотентно: если gh уже стоит — сразу выходим.
# ---------------------------------------------------------------------------
ensure_gh() {
    command -v gh >/dev/null 2>&1 && { info "gh уже установлен ($(gh --version 2>/dev/null | head -1))"; return 0; }
    local keyring=/etc/apt/keyrings/githubcli-archive-keyring.gpg
    local list=/etc/apt/sources.list.d/github-cli.list
    local arch; arch="$(dpkg --print-architecture)"
    install_packages "gh: зависимости" ca-certificates curl
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
# UI-обёртки — бэкенд dialog (богаче, темизируется) с откатом на whiptail
# ---------------------------------------------------------------------------
UI_BIN="whiptail"     # реальное значение выставляет ui_init()
UI_OPTS=()            # доп. опции бэкенда (напр. --colors для dialog)

# ui_init — выбрать бэкенд и применить тёмную тему. Вызывается из main().
ui_init() {
    if command -v dialog >/dev/null 2>&1; then
        UI_BIN="dialog"
        UI_OPTS=(--colors)
        # тема: файл dialogrc-nas рядом со скриптом (если есть)
        [ -f "$SCRIPT_DIR/dialogrc-nas" ] && export DIALOGRC="$SCRIPT_DIR/dialogrc-nas"
    elif command -v whiptail >/dev/null 2>&1; then
        UI_BIN="whiptail"
        UI_OPTS=()
        # тёмная тема для whiptail (newt)
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
        die "не найден ни dialog, ни whiptail (apt install whiptail)"
    fi
    log "UI backend: $UI_BIN"
}

ui_menu()      { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --menu "$2" 20 78 10 "${@:3}" 3>&1 1>&2 2>&3; }
ui_input()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --inputbox "$2" 12 78 "$3" 3>&1 1>&2 2>&3; }
ui_password()  { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --passwordbox "$2" 12 78 3>&1 1>&2 2>&3; }
ui_yesno()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --yesno "$2" 18 78; }   # 0 = Yes
ui_msg()       { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --msgbox "$2" 20 78; }
ui_checklist() { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --checklist "$2" 20 78 10 "${@:3}" 3>&1 1>&2 2>&3; }
# ui_gauge — читает проценты (0..100) из stdin: <cmd> | ui_gauge "Заголовок" "Текст"
ui_gauge()     { "$UI_BIN" "${UI_OPTS[@]}" --title "$1" --gauge "$2" 8 78 0; }

# ---------------------------------------------------------------------------
# Предпроверки
# ---------------------------------------------------------------------------
require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        die "нужны права root. Запустите: sudo $0 $*"
    fi
}

ensure_log() {
    if [ "$DRY_RUN" -eq 0 ]; then
        touch "$LOG" 2>/dev/null || die "не могу писать в $LOG"
        chmod 640 "$LOG" 2>/dev/null || true
    fi
    log "===== запуск nas-wizard (dry_run=$DRY_RUN, stage='${FORCE_STAGE:-menu}', user=$TARGET_USER) ====="
}

# ---------------------------------------------------------------------------
# Работа с дисками: определение системных/защищённых устройств
# ---------------------------------------------------------------------------

# Родительский диск для точки монтирования (например / -> mmcblk0)
disk_of_mountpoint() {
    local mp="$1" src pk mm
    src="$(findmnt -no SOURCE "$mp" 2>/dev/null)" || return 1
    [ -z "$src" ] && return 1
    # findmnt может отдать псевдо-источник (/dev/root) — привести к реальному
    # устройству через MAJ:MIN, иначе системный диск выпадет из-под защиты
    if [ ! -b "$src" ] || [ "$src" = "/dev/root" ]; then
        mm="$(findmnt -no MAJ:MIN "$mp" 2>/dev/null | head -1)"
        [ -n "$mm" ] && [ -e "/dev/block/$mm" ] || return 1
        src="$(realpath "/dev/block/$mm" 2>/dev/null)" || return 1
    fi
    pk="$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)"
    if [ -n "$pk" ]; then
        echo "/dev/$pk"
    else
        # src уже может быть цельным диском
        echo "$src"
    fi
}

# Список защищённых дисков (система). Возвращает строки /dev/xxx
protected_disks() {
    local mp d src pk
    {
        for mp in / /boot /boot/firmware /home /var; do
            d="$(disk_of_mountpoint "$mp" 2>/dev/null)"
            [ -n "$d" ] && echo "$d"
        done
        # диск с активным swap-разделом тоже под защитой
        while read -r src _; do
            [ -b "$src" ] || continue
            case "$src" in /dev/zram*) continue ;; esac
            pk="$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)"
            if [ -n "$pk" ]; then echo "/dev/$pk"; else echo "$src"; fi
        done < <(tail -n +2 /proc/swaps 2>/dev/null)
    } | grep -v '^$' | sort -u
}

# Диск занят? (сам или любой его раздел куда-то смонтирован)
disk_in_use() {
    local dev="$1" mps
    mps="$(lsblk -nro MOUNTPOINT "$dev" 2>/dev/null | grep -c . )"
    [ "$mps" -gt 0 ]
}

is_protected() {
    local dev="$1" p
    while read -r p; do
        [ "$dev" = "$p" ] && return 0
    done < <(protected_disks)
    return 1
}

# Собрать список дисков-кандидатов (не система, не смонтированы, размер > 0)
# Печатает: DEV<TAB>SIZE<TAB>MODEL
candidate_disks() {
    local dev size model type sizebytes
    while read -r dev type sizebytes; do
        [ "$type" = "disk" ] || continue
        # пропускаем zram/loop
        case "$dev" in
            /dev/zram*|/dev/loop*) continue ;;
        esac
        [ "${sizebytes:-0}" -gt 0 ] 2>/dev/null || continue
        is_protected "$dev" && continue
        disk_in_use "$dev" && continue
        size="$(lsblk -dno SIZE "$dev" 2>/dev/null | tr -d ' ')"
        model="$(lsblk -dno MODEL "$dev" 2>/dev/null | sed 's/[[:space:]]*$//')"
        [ -z "$model" ] && model="(нет модели)"
        printf '%s\t%s\t%s\n' "$dev" "$size" "$model"
    done < <(lsblk -dpno NAME,TYPE,SIZE -b 2>/dev/null)
}

# Информация о диске для подтверждения
disk_info_block() {
    local dev="$1"
    echo "Устройство : $dev"
    echo "Модель     : $(lsblk -dno MODEL "$dev" 2>/dev/null | sed 's/[[:space:]]*$//')"
    echo "Серийник   : $(lsblk -dno SERIAL "$dev" 2>/dev/null)"
    echo "Размер     : $(lsblk -dno SIZE "$dev" 2>/dev/null | tr -d ' ')"
    local mps
    mps="$(lsblk -nro NAME,MOUNTPOINT "$dev" 2>/dev/null | awk 'NF>1{print "  "$1" -> "$2}')"
    if [ -n "$mps" ]; then
        echo "Смонтировано:"
        echo "$mps"
    else
        echo "Смонтировано: нет"
    fi
}

# Следующий свободный номер для /mnt/diskN (по факту: dir/fstab)
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

# Размер в байтах самого большого диска ДАННЫХ (по mount /mnt/disk*)
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

# Смонтированные диски данных / чётности (по одному пути на строку, натуральная сортировка)
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
# Бэкап fstab
# ---------------------------------------------------------------------------
backup_fstab() {
    local stamp bak
    stamp="$(date '+%Y%m%d-%H%M%S')"
    bak="/etc/fstab.bak.${stamp}"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] бэкап /etc/fstab -> $bak"
        return 0
    fi
    cp -a /etc/fstab "$bak" && info "бэкап fstab: $bak"
    # копия снапшота и в git-репозиторий конфигов
    if [ -d "$NAS_CONFIG" ]; then
        run_as cp -a /etc/fstab "$NAS_CONFIG/fstab.snapshot"
    fi
}

# ---------------------------------------------------------------------------
# ЭТАП 0: подготовка системы
# ---------------------------------------------------------------------------
stage_system() {
    echo
    echo "=== Этап 0: подготовка системы ==="
    log "--- stage_system start ---"

    # 0.1 apt update / full-upgrade (по согласию)
    if ui_yesno "Обновление системы" "Выполнить apt update && apt full-upgrade?\n\nМожет занять время. Рекомендуется при первой настройке."; then
        run apt-get update
        run apt-get full-upgrade -y
    else
        info "обновление системы пропущено"
    fi

    # 0.2 установка софта: NAS-стек + утилиты + Pi-специфичное (идемпотентно, недоступные пропускаются)
    run apt-get update
    install_packages "NAS-стек"   "${STACK_PACKAGES[@]}"
    install_packages "утилиты"    "${UTIL_PACKAGES[@]}"
    install_packages "Pi-пакеты"  "${PI_PACKAGES[@]}"
    ensure_docker_repo   # docker-ce + compose-plugin из официального репо Docker
    ensure_gh            # GitHub CLI (для пуша кода панели с бокса)

    # 0.3 включить/запустить сервисы (идемпотентно)
    local svc
    for svc in cockpit.socket docker; do
        if systemctl is-enabled "$svc" >/dev/null 2>&1; then
            info "сервис уже включён: $svc"
        else
            run systemctl enable "$svc"
        fi
        if systemctl is-active "$svc" >/dev/null 2>&1; then
            info "сервис уже запущен: $svc"
        else
            run systemctl start "$svc"
        fi
    done

    # 0.3b TRIM для SSD/NVMe (еженедельно) — снижает износ и держит скорость
    if systemctl list-unit-files fstrim.timer >/dev/null 2>&1; then
        if systemctl is-enabled fstrim.timer >/dev/null 2>&1; then
            info "fstrim.timer уже включён"
        else
            run systemctl enable --now fstrim.timer
        fi
    fi

    # 0.4 добавить пользователя в группу docker
    if id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        info "пользователь $TARGET_USER уже в группе docker"
    else
        run usermod -aG docker "$TARGET_USER"
        info "пользователь $TARGET_USER добавлен в группу docker (нужен релогин)"
    fi

    # 0.5 проверка сетевого интерфейса (определяем динамически)
    local iface
    iface="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
    if [ -z "$iface" ]; then
        warn "не удалось определить сетевой интерфейс по умолчанию"
    elif [ "$iface" = "end0" ]; then
        info "сетевой интерфейс: end0 (штатно для Pi5/Bookworm)"
    else
        warn "сетевой интерфейс по умолчанию: '$iface' (ожидался end0 на Pi5/Bookworm)."
        ui_msg "Сеть" "Основной интерфейс: $iface\n\nНа Raspberry Pi 5 / Bookworm проводной обычно называется end0. Если вы используете Wi-Fi ('$iface' похоже на беспроводной) — учтите, что для NAS предпочтителен стабильный проводной линк.\n\nИмя интерфейса нигде не захардкожено — просто предупреждение."
    fi

    # 0.6 структура каталогов
    info "создаю структуру каталогов"
    run mkdir -p "$STORAGE_MNT" "$DOCKER_ROOT" "$SERVICES_SRC"
    # nas-config — в домашке пользователя, git-репозиторий
    if [ ! -d "$NAS_CONFIG" ]; then
        run mkdir -p "$NAS_CONFIG/scripts"
        run chown -R "$TARGET_USER:$TARGET_USER" "$NAS_CONFIG"
    fi
    # git init + первый коммит
    if [ ! -d "$NAS_CONFIG/.git" ]; then
        run_as git -C "$NAS_CONFIG" init -q
        if [ "$DRY_RUN" -eq 0 ]; then
            if [ ! -f "$NAS_CONFIG/README.md" ]; then
                printf '# nas-config\n\nВерсионируемые конфиги NAS (fstab-сниппеты, snapraid.conf, docker-compose).\nСгенерировано nas-wizard.sh.\n' \
                    | sudo -u "$TARGET_USER" tee "$NAS_CONFIG/README.md" >/dev/null
            fi
            run_as git -C "$NAS_CONFIG" add -A
            run_as git -C "$NAS_CONFIG" -c user.email="nas@localhost" -c user.name="nas-wizard" commit -q -m "init nas-config" || true
        else
            info "[DRY-RUN] git init + первый коммит в $NAS_CONFIG"
        fi
    else
        info "git-репозиторий уже существует: $NAS_CONFIG"
    fi

    # 0.7 hostname / timezone
    local cur_host cur_tz
    cur_host="$(hostnamectl --static 2>/dev/null || hostname)"
    cur_tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
    if ui_yesno "Hostname" "Текущий hostname: $cur_host\n\nИзменить?"; then
        local newhost
        newhost="$(ui_input "Hostname" "Новое имя хоста:" "$cur_host")" && \
            [ -n "$newhost" ] && [ "$newhost" != "$cur_host" ] && run hostnamectl set-hostname "$newhost"
    fi
    if [ "$cur_tz" != "Europe/Madrid" ]; then
        if ui_yesno "Timezone" "Текущая таймзона: ${cur_tz:-не задана}\n\nУстановить Europe/Madrid?"; then
            run timedatectl set-timezone "Europe/Madrid"
        fi
    else
        info "таймзона уже Europe/Madrid"
    fi

    # summary
    stage_system_summary
    log "--- stage_system end ---"
}

stage_system_summary() {
    local msg
    msg="Этап 0 завершён.

Проверка:
  systemctl status cockpit.socket docker
  df -h
  ls -la $NAS_CONFIG

Cockpit:  https://$(hostname -I 2>/dev/null | awk '{print $1}'):9090
Осталось: подключить диски (этап 2).

ВНИМАНИЕ: членство в группе docker применится после релогина $TARGET_USER."
    ui_msg "Итог: подготовка системы" "$msg"
    echo "$msg"
}

# ---------------------------------------------------------------------------
# ЭТАП 2: работа с одним диском
# ---------------------------------------------------------------------------

# Тройная проверка + требование ввести имя устройства текстом
confirm_destructive() {
    local dev="$1" purpose="$2" fs="$3" label="$4"
    local block typed
    block="$(disk_info_block "$dev")"

    if ! ui_yesno "ПОДТВЕРЖДЕНИЕ ФОРМАТИРОВАНИЯ" \
"БУДЕТ ОТФОРМАТИРОВАН диск как $fs (метка: $label), назначение: $purpose.

$block

ВСЕ ДАННЫЕ НА ЭТОМ ДИСКЕ БУДУТ БЕЗВОЗВРАТНО УДАЛЕНЫ.

Продолжить?"; then
        info "форматирование отменено пользователем"
        return 1
    fi

    # Требуем ввести имя устройства текстом
    typed="$(ui_input "Финальное подтверждение" \
"Чтобы подтвердить форматирование, введите ИМЯ УСТРОЙСТВА точно так:

$dev" "")" || { info "отменено"; return 1; }

    if [ "$typed" != "$dev" ]; then
        ui_msg "Отмена" "Введено '$typed', ожидалось '$dev'. Форматирование ОТМЕНЕНО."
        info "имя устройства не совпало ('$typed' != '$dev') — форматирование отменено"
        return 1
    fi
    return 0
}

# Формат диска + fstab + mount. Аргументы: dev mountpoint fs label pass
# mkfs.<fs> присутствует?
mkfs_available() { command -v "mkfs.$1" >/dev/null 2>&1; }

# Создать ФС на устройстве с меткой. Поддержка: ext4/xfs/btrfs/exfat/ntfs/vfat.
make_fs() {
    local dev="$1" fs="$2" label="$3"
    mkfs_available "$fs" || die "нет mkfs.$fs — установите пакет (exfatprogs/ntfs-3g/btrfs-progs/xfsprogs)"
    # mkfs ДОЛЖЕН падать жёстко: иначе format_and_mount возьмёт старый UUID (blkid),
    # смонтирует недоформатированную ФС и внесёт её в fstab/пул → потеря/каша данных.
    case "$fs" in
        ext4)  run mkfs.ext4  -F -L "$label" "$dev" || die "mkfs.ext4 не удался на $dev" ;;
        xfs)   run mkfs.xfs   -f -L "$(printf '%s' "$label" | cut -c1-12)" "$dev" || die "mkfs.xfs не удался на $dev" ;;
        btrfs) run mkfs.btrfs -f -L "$label" "$dev" || die "mkfs.btrfs не удался на $dev" ;;
        exfat) run mkfs.exfat -L "$label" "$dev" || die "mkfs.exfat не удался на $dev" ;;
        ntfs)  run mkfs.ntfs  -Q -L "$label" "$dev" || die "mkfs.ntfs не удался на $dev" ;;
        vfat)  run mkfs.vfat  -n "$(printf '%s' "$label" | tr 'a-z' 'A-Z' | tr -cd 'A-Z0-9_-' | cut -c1-11)" "$dev" || die "mkfs.vfat не удался на $dev" ;;
        *)     die "неизвестная ФС: $fs" ;;
    esac
}

format_and_mount() {
    local dev="$1" mp="$2" fs="$3" label="$4" pass="$5"
    local uuid

    backup_fstab

    make_fs "$dev" "$fs" "$label"

    # UUID (в dry-run — плейсхолдер, т.к. диск не форматировался)
    if [ "$DRY_RUN" -eq 1 ]; then
        uuid="<UUID-появится-после-mkfs>"
    else
        uuid="$(blkid -s UUID -o value "$dev" 2>/dev/null)"
        [ -z "$uuid" ] && die "не удалось получить UUID $dev после форматирования"
    fi

    # каталог точки монтирования
    run mkdir -p "$mp"

    # убрать устаревшие строки fstab для этой же точки (после переформатирования UUID меняется —
    # иначе останется мёртвая строка со старым UUID для того же /mnt/diskN)
    remove_fstab_mount "$mp"

    # fstab по UUID (+ nofail: чтобы отсутствие диска не блокировало загрузку NAS)
    local fstab_line="UUID=$uuid  $mp  $fs  defaults,noatime,nofail,x-systemd.device-timeout=10  0  $pass"
    append_line "$fstab_line" /etc/fstab
    # сниппет в git-репозиторий
    if [ -d "$NAS_CONFIG" ]; then
        append_line "$fstab_line" "$NAS_CONFIG/fstab.snippets"
    fi

    # монтируем и проверяем
    run systemctl daemon-reload
    run mount -a
    if [ "$DRY_RUN" -eq 0 ]; then
        if findmnt -no TARGET "$mp" >/dev/null 2>&1; then
            info "смонтировано: $mp"
        else
            warn "точка $mp не смонтирована — проверьте /etc/fstab и вывод mount -a"
        fi
    fi
}

# Идемпотентность: диск уже настроен?
disk_already_configured() {
    # Диск уже настроен, если его UUID (или UUID любого его раздела) есть в fstab.
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
    echo "=== Этап 2: работа с диском ==="
    log "--- stage_disk start ---"

    # Собираем кандидатов
    local rows dev size model
    rows="$(candidate_disks)"
    if [ -z "$rows" ]; then
        ui_msg "Нет свободных дисков" "Не найдено свободных блочных устройств.

Кандидаты исключаются, если диск:
 - является системным (/, /boot, /home, /var),
 - уже куда-то смонтирован,
 - имеет нулевой размер.

Проверьте подключение диска и запустите этап снова."
        info "нет дисков-кандидатов"
        return 0
    fi

    # Меню выбора диска
    local menu_args=()
    while IFS=$'\t' read -r dev size model; do
        [ -z "$dev" ] && continue
        menu_args+=("$dev" "$size — $model")
    done <<< "$rows"

    dev="$(ui_menu "Выбор диска" "Свободные диски (исключены системный и смонтированные):" "${menu_args[@]}")" || {
        info "выбор диска отменён"; return 0;
    }
    [ -z "$dev" ] && { info "диск не выбран"; return 0; }

    # Двойная страховка: диск не защищён и не занят
    if is_protected "$dev"; then die "$dev — системный диск, работа запрещена"; fi
    if disk_in_use "$dev"; then die "$dev сейчас смонтирован — сначала размонтируйте"; fi

    # Идемпотентность
    if disk_already_configured "$dev"; then
        ui_msg "Диск уже настроен" "$dev уже присутствует в /etc/fstab по UUID. Пропускаю."
        info "$dev уже настроен, пропуск"
        return 0
    fi

    # Данные или parity?
    local role
    role="$(ui_menu "Назначение диска" "Диск $dev — для чего?" \
        "data"   "Диск ДАННЫХ" \
        "parity" "Диск ЧЁТНОСТИ (SnapRAID parity)")" || { info "отменено"; return 0; }

    # Выбор ФС
    local fs
    fs="$(ui_menu "Файловая система" "ФС для $dev:" \
        "ext4" "ext4 (по умолчанию, рекомендуется)" \
        "xfs"  "xfs")" || { info "отменено"; return 0; }
    [ -z "$fs" ] && fs="ext4"

    if [ "$role" = "data" ]; then
        local n mp label
        n="$(next_disk_number)"
        # Путь ФИКСИРОВАН как /mnt/diskN, а не свободный ввод: вся дискавери
        # (mounted_data_disks, largest_data_disk_bytes, next_disk_number, имена
        # snapraid d$n) находит диски данных по шаблону /mnt/disk*. Кастомная точка
        # сделала бы диск невидимым для пула и SnapRAID — тихая дыра в защите данных.
        mp="/mnt/disk${n}"
        label="disk${n}"

        confirm_destructive "$dev" "ДАННЫЕ ($mp)" "$fs" "$label" || return 0
        format_and_mount "$dev" "$mp" "$fs" "$label" 2

        # mergerfs: пул объединяем только при >= 2 дисках данных (по ТЗ)
        local data_count
        data_count="$(mounted_data_disks | grep -c .)"
        if [ "$data_count" -lt 2 ]; then
            ui_msg "mergerfs" "У вас $data_count диск(а) данных.

mergerfs-пул объединять пока нет смысла (нужно >= 2 дисков).
Он будет настроен автоматически, когда вы добавите второй диск данных."
            info "$data_count диск данных — mergerfs не настраивается (по ТЗ)"
        else
            info "дисков данных: $data_count — настраиваю mergerfs"
            generate_mergerfs
        fi

    else  # parity
        # Проверка размера parity >= самого большого диска данных
        local pn mp label pbytes maxdata
        pbytes="$(lsblk -bdno SIZE "$dev" 2>/dev/null | head -1)"
        maxdata="$(largest_data_disk_bytes)"
        if [ "${maxdata:-0}" -gt 0 ] && [ "${pbytes:-0}" -lt "$maxdata" ]; then
            local phr mhr risk
            phr="$(numfmt --to=iec "$pbytes" 2>/dev/null || echo "$pbytes")"
            mhr="$(numfmt --to=iec "$maxdata" 2>/dev/null || echo "$maxdata")"
            warn "parity ($phr) МЕНЬШЕ самого большого диска данных ($mhr)"
            if ! ui_yesno "РИСК: маленький parity-диск" \
"Диск чётности ($phr) МЕНЬШЕ самого большого диска данных ($mhr).

SnapRAID НЕ СМОЖЕТ защитить данные полностью: parity обязан быть >= самого большого диска данных.

Это опасная конфигурация. Продолжить?"; then
                info "parity меньше данных — пользователь отказался"
                return 0
            fi
            risk="$(ui_input "Подтверждение риска" "Введите фразу: я понимаю риск" "")" || return 0
            if [ "$risk" != "я понимаю риск" ]; then
                ui_msg "Отмена" "Фраза не совпала. Операция отменена."
                info "фраза подтверждения риска не совпала"
                return 0
            fi
        fi

        pn="$(next_parity_number)"
        mp="/mnt/parity${pn}"
        label="parity${pn}"
        confirm_destructive "$dev" "PARITY ($mp)" "$fs" "$label" || return 0
        format_and_mount "$dev" "$mp" "$fs" "$label" 2

        ui_msg "SnapRAID" "Parity-диск смонтирован в $mp.

Теперь можно настроить SnapRAID: выберите в меню пункт \"Этап 3: SnapRAID\" (или запустите с --stage snapraid)."
    fi

    stage_disk_summary "$dev"
    log "--- stage_disk end ---"
}

stage_disk_summary() {
    local dev="$1" msg
    msg="Этап 2 (диск $dev) завершён.

Текущее состояние:
$(findmnt -rno TARGET,SOURCE,FSTYPE,SIZE /mnt/disk* /mnt/parity* 2>/dev/null | sed 's/^/  /')

Проверка:
  lsblk -f
  df -h
  cat /etc/fstab

Бэкапы fstab: ls -la /etc/fstab.bak.*
Конфиги в git: $NAS_CONFIG"
    ui_msg "Итог: работа с диском" "$msg"
    echo "$msg"
}

# ---------------------------------------------------------------------------
# ЭТАП 2b: mergerfs (пул из >= 2 дисков данных)
# ---------------------------------------------------------------------------
# nofail — не блокировать загрузку в emergency mode, если пул не смонтировался.
# x-systemd.requires=<ветка> добавляется на каждую ветку динамически в generate_mergerfs
# (пути дисков не статичны), чтобы пул монтировался ТОЛЬКО после своих веток, а не поверх пустых /mnt/diskN.
MERGERFS_OPTS="defaults,nofail,allow_other,use_ino,category.create=mfs,minfreespace=20G,fsname=mergerfs"

remove_fstab_mergerfs() {
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] удалить строку fuse.mergerfs из /etc/fstab"
        return 0
    fi
    sed -i '/fuse\.mergerfs/d' /etc/fstab
}

# Опции mergerfs для КОМАНДНОЙ строки (-o ...): без fstab-конструкций defaults/nofail.
MERGERFS_SVC_OPTS="allow_other,use_ino,category.create=mfs,minfreespace=20G,fsname=mergerfs"
MERGERFS_UNIT="/etc/systemd/system/nas-mergerfs.service"
# Пул mergerfs держим systemd-СЕРВИСОМ с Restart=always, а НЕ строкой fstab. Причина:
# FUSE-процесс может упасть («Transport endpoint is not connected»), и fstab-mount тогда
# мёртв до ручного umount+mount. Сервис же systemd поднимает за секунды сам (+ ExecStartPre
# снимает зависшую точку). Ветки /mnt/disk* по-прежнему в fstab; пул зависит от local-fs.
generate_mergerfs() {
    local branches=() mp
    while read -r mp; do [ -n "$mp" ] && branches+=("$mp"); done < <(mounted_data_disks)
    local count="${#branches[@]}"
    if [ "$count" -lt 2 ]; then
        info "смонтировано дисков данных: $count — mergerfs не требуется (нужно >= 2)"
        # диск вышел из пула — снять сервис, если был
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

    # миграция со старой схемы: убрать строку пула из fstab (теперь пулом рулит сервис).
    # Ветки /mnt/disk* в fstab не трогаем.
    if grep -qsE 'fuse\.mergerfs' /etc/fstab; then
        backup_fstab
        findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1 && run umount -l "$STORAGE_MNT"
        remove_fstab_mergerfs
    fi
    run mkdir -p "$STORAGE_MNT"

    # \$(seq …) экранируем — это команда времени ЗАПУСКА сервиса, а не генерации файла
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
            info "mergerfs-пул поднят сервисом nas-mergerfs (Restart=always): $STORAGE_MNT ($count дисков)"
        else
            warn "пул $STORAGE_MNT не поднялся — проверьте: systemctl status nas-mergerfs"
        fi
    fi
    commit_config "mergerfs: сервис nas-mergerfs (пул из $count дисков)"
}

stage_mergerfs() {
    echo
    echo "=== Этап 2b: mergerfs ==="
    log "--- stage_mergerfs start ---"
    local count
    count="$(mounted_data_disks | grep -c .)"
    if [ "$count" -lt 2 ]; then
        ui_msg "mergerfs" "Смонтировано дисков данных: $count.

Нужно минимум 2 диска данных, чтобы объединять их в пул mergerfs.
Добавьте ещё диск данных (этап 2) и вернитесь сюда."
        info "mergerfs: недостаточно дисков ($count)"
        return 0
    fi
    generate_mergerfs
    if [ "$DRY_RUN" -eq 0 ]; then
        ui_msg "Итог: mergerfs" "Пул $STORAGE_MNT собран из $count дисков.

Проверка:
$(df -h "$STORAGE_MNT" 2>/dev/null | sed 's/^/  /')"
    fi
    log "--- stage_mergerfs end ---"
}

# ---------------------------------------------------------------------------
# ЭТАП 3: SnapRAID
# ---------------------------------------------------------------------------
SNAPRAID_CONF="/etc/snapraid.conf"

parity_keyword() { case "$1" in 1) echo "parity" ;; *) echo "$1-parity" ;; esac; }

ensure_snapraid_conf() {
    local data_mounts=() parity_mounts=() m
    while read -r m; do [ -n "$m" ] && data_mounts+=("$m"); done < <(mounted_data_disks)
    while read -r m; do [ -n "$m" ] && parity_mounts+=("$m"); done < <(mounted_parity_disks)

    if [ "${#data_mounts[@]}" -eq 0 ]; then
        ui_msg "SnapRAID" "Нет смонтированных дисков данных (/mnt/disk*). Сначала добавьте диск данных (этап 2)."
        return 1
    fi
    if [ "${#parity_mounts[@]}" -eq 0 ]; then
        ui_msg "SnapRAID" "Нет parity-диска (/mnt/parity*). SnapRAID требует минимум один диск чётности. Добавьте parity (этап 2)."
        return 1
    fi

    run mkdir -p /var/snapraid

    if [ ! -f "$SNAPRAID_CONF" ]; then
        # Свежая генерация
        info "генерирую $SNAPRAID_CONF ($((${#data_mounts[@]})) данных, $((${#parity_mounts[@]})) parity)"
        {
            echo "# snapraid.conf — сгенерировано nas-wizard $(date '+%F %T')"
            echo "# Диски правьте через визард; excludes можно дописывать вручную."
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
        # Идемпотентная дозапись недостающих строк (excludes НЕ трогаем)
        info "$SNAPRAID_CONF существует — дописываю недостающие диски"
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
                info "data-диск уже в конфиге: $d"
            fi
        done
    fi
    commit_config "snapraid.conf: ${#data_mounts[@]} data / ${#parity_mounts[@]} parity"
    return 0
}

install_snapraid_wrapper() {
    write_file /usr/local/bin/nas-snapraid.sh <<'WRAP'
#!/usr/bin/env bash
# nas-wizard: обёртка snapraid sync/scrub с защитой от массового удаления + пинг статуса
set -uo pipefail
ACTION="${1:-sync}"
LOG=/var/log/snapraid.log
CONF=/etc/nas-wizard/notify.conf
DELETE_THRESHOLD=500
HEALTHCHECK_URL=""
[ -f "$CONF" ] && . "$CONF"
notify(){ [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$@" || true; }
# пинг Healthchecks/ntfy/webhook: успех — базовый URL, ошибка — /fail (конвенция Healthchecks)
ping_hc(){ [ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 12 --retry 2 "$HEALTHCHECK_URL$1" >/dev/null 2>&1 || true; }

{
    echo "===== $(date '+%F %T') snapraid $ACTION ====="
    # ЗАЩИТА ДАННЫХ: если хоть один диск данных из конфига не смонтирован, его файлы
    # выглядят как «удалённые» → sync записал бы удаления в чётность. Прерываем.
    miss=""
    while read -r _ _ dpath; do
        [ -n "$dpath" ] || continue
        mountpoint -q "$dpath" || miss="$miss $dpath"
    done < <(grep -E '^data ' /etc/snapraid.conf 2>/dev/null)
    if [ -n "$miss" ]; then
        echo "ABORT: диски данных не смонтированы:$miss — $ACTION ПРОПУЩЕН (защита чётности)."
        ping_hc "/fail"
        echo "NASRESULT $ACTION err rc=9" >>"$LOG"
        exit 9
    fi
    if [ "$ACTION" = "sync" ]; then
        # diff должен отработать корректно (0=нет изменений, 2=есть изменения); иначе — НЕ синкать
        diff_out="$(snapraid diff 2>&1)"; diff_rc=$?
        printf '%s\n' "$diff_out"
        if [ "$diff_rc" != 0 ] && [ "$diff_rc" != 2 ]; then
            echo "ABORT: snapraid diff завершился с кодом $diff_rc — sync ПРОПУЩЕН (не удалось оценить удаления)."
            ping_hc "/fail"
            exit 1
        fi
        removed=$(printf '%s\n' "$diff_out" | sed -n 's/^ *\([0-9][0-9]*\) removed$/\1/p')
        removed=${removed:-0}
        echo "diff: removed=$removed threshold=$DELETE_THRESHOLD"
        if [ "$removed" -gt "$DELETE_THRESHOLD" ]; then
            echo "ABORT: удалено файлов $removed > порога $DELETE_THRESHOLD — sync ПРОПУЩЕН (защита данных)."
            ping_hc "/fail"
            exit 1
        fi
        snapraid sync
    else
        snapraid scrub -p 12 -o 10
    fi
} >>"$LOG" 2>&1
rc=$?
# NASRESULT-маркеры читает nas-web (единые уведомления с приоритетами Pushover)
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
    if ui_yesno "Уведомления" "Настроить пинг статуса snapraid sync (Healthchecks.io / ntfy / webhook)?

При успехе дёргается URL; для Healthchecks при ошибке добавляется /fail. У вас уже есть Healthchecks/Uptime Kuma — можно указать их URL."; then
        local url
        url="$(ui_input "URL пинга" "URL, который дёргать при УСПЕХЕ:" "")" || return 0
        if [ -z "$url" ]; then info "URL пуст — уведомления не настроены"; return 0; fi
        notify_conf_set HEALTHCHECK_URL "$url"
        info "уведомления настроены: $url"
    else
        info "уведомления не настраиваются"
    fi
}

stage_snapraid() {
    echo
    echo "=== Этап 3: SnapRAID ==="
    log "--- stage_snapraid start ---"

    ensure_snapraid_conf || { log "--- stage_snapraid aborted ---"; return 0; }

    # Уведомления (до установки wrapper — чтобы notify.conf уже существовал)
    setup_snapraid_notify

    # Обёртка + systemd-таймеры
    install_snapraid_wrapper
    install_snapraid_timers

    # Первый sync (по согласию)
    if ui_yesno "SnapRAID sync" "Выполнить первый snapraid sync СЕЙЧАС?

ВНИМАНИЕ: на больших дисках может занять ОЧЕНЬ долго. Прогресс будет виден в терминале.
Пропустите, если хотите дождаться ночного авто-sync (таймер уже настроен)."; then
        echo "--- snapraid sync (прогресс ниже) ---"
        run_visible snapraid sync
    else
        info "первый sync пропущен (сработает по таймеру в 03:00)"
    fi

    stage_snapraid_summary
    log "--- stage_snapraid end ---"
}

stage_snapraid_summary() {
    local status="(запустите: sudo snapraid status)"
    [ "$DRY_RUN" -eq 0 ] && status="$(snapraid status 2>/dev/null | tail -n 15 | sed 's/^/  /')"
    ui_msg "Итог: SnapRAID" "Конфиг: $SNAPRAID_CONF
Обёртка: /usr/local/bin/nas-snapraid.sh
Таймеры: snapraid-sync.timer (ежедневно 03:00), snapraid-scrub.timer (еженедельно вс 05:00)
Лог: /var/log/snapraid.log

Проверка:
  systemctl list-timers 'snapraid-*'
  sudo snapraid status
  sudo snapraid sync"
    echo "Этап 3 завершён."
    echo "$status"
}

# ---------------------------------------------------------------------------
# ЭТАП 4: Docker (discovery-based: читаем папки с compose-файлами)
# ---------------------------------------------------------------------------

# Печатает: SERVICE<TAB>COMPOSE_FILE — по одной строке на найденный сервис.
# Ищет в каталоге services/ рядом со скриптом (аргумент $1 переопределяет каталог).
# shellcheck disable=SC2120  # $1 опционален, по умолчанию $SERVICES_SRC
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

# Запущен ли сервис (есть хотя бы один running-контейнер)?
service_running() {
    local file="$1" ids running
    ids="$($DC -f "$file" ps -q 2>/dev/null)"
    [ -n "$ids" ] || return 1
    running="$(docker inspect -f '{{.State.Running}}' $ids 2>/dev/null | grep -c true)"
    [ "${running:-0}" -gt 0 ]
}

generate_deploy_script() {
    run_as mkdir -p "$NAS_CONFIG/scripts"
    # SERVICES_SRC подставляется на этапе генерации, чтобы deploy.sh был самодостаточным
    write_file "$NAS_CONFIG/scripts/deploy.sh" <<DEPLOY
#!/usr/bin/env bash
# Автоген nas-wizard. Идемпотентно поднимает ВСЕ compose-сервисы из services/ рядом со скриптом.
# "Применить желаемое состояние": docker compose up -d по каждому найденному файлу.
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

# Порядок загрузки: docker ждёт mergerfs-пул, стеки поднимаются после монтирования.
# Защищает от записи контейнеров в ПУСТУЮ точку /mnt/storage, если пул ещё не смонтирован.
install_stacks_autostart() {
    # НАМЕРЕННО не вешаем RequiresMountsFor на сам docker.service: это делало бы
    # запуск демона (а значит и ВСЕХ контейнеров) зависимым от пула — один
    # пропавший/переименованный диск ронял бы весь Docker. Ждём пул только на
    # уровне nas-stacks.service (bring-up стеков), не демона.
    # Подчищаем drop-in, если его оставил старый запуск wizard'а:
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
    echo "=== Этап 4: Docker ==="
    log "--- stage_docker start ---"

    DC="$(docker_compose_cmd)"
    if [ -z "$DC" ]; then
        ui_msg "Docker" "Docker Compose не найден. Сначала прогоните этап 0 (подготовка системы), он ставит docker-ce + docker-compose-plugin из официального репо Docker."
        info "docker compose недоступен"
        return 0
    fi

    run mkdir -p "$DOCKER_ROOT" "$SERVICES_SRC"

    # deploy.sh генерируем всегда — чтобы существовал с первого дня
    generate_deploy_script
    # автозапуск стеков после загрузки + ожидание пула
    install_stacks_autostart

    local rows
    rows="$(discover_compose_services)"
    if [ -z "$rows" ]; then
        ui_msg "Docker: нет сервисов" "В $SERVICES_SRC не найдено ни одного compose-файла.

Модель работы: docker-сервисы лежат РЯДОМ СО СКРИПТОМ, в папке services/.
Каждый сервис — своя подпапка с docker-compose.yml. Скрипт их находит и предлагает поднять.

Пример:
  mkdir -p $SERVICES_SRC/immich
  \$EDITOR $SERVICES_SRC/immich/docker-compose.yml

Конфиги/данные держите в $DOCKER_ROOT/<service>/ и $STORAGE_MNT/<service>/.
Затем снова запустите этот этап.

Уже создан: $NAS_CONFIG/scripts/deploy.sh (поднимает всё разом)."
        info "compose-сервисы не найдены в $SERVICES_SRC"
        return 0
    fi

    # Чеклист: помечаем уже запущенные
    local menu_args=() svc file state
    while IFS=$'\t' read -r svc file; do
        [ -z "$svc" ] && continue
        if service_running "$file"; then state="ON"; else state="OFF"; fi
        menu_args+=("$svc" "$file" "$state")
    done <<< "$rows"

    local raw
    raw="$(ui_checklist "Docker: какие сервисы поднять" \
        "Отметьте сервисы для 'up -d' (уже запущенные помечены). Снятые с отметки запущенные будет предложено остановить." \
        "${menu_args[@]}")" || { info "выбор сервисов отменён"; return 0; }

    # Разбираем выбранные (whiptail возвращает в кавычках)
    local selected
    selected="$(printf '%s' "$raw" | tr -d '"')"

    # Множество выбранных для быстрой проверки
    local want=" $selected "

    # Поднимаем выбранные, собираем "запущенные, но не выбранные"
    local to_stop=() up_count=0
    while IFS=$'\t' read -r svc file; do
        [ -z "$svc" ] && continue
        if [[ "$want" == *" $svc "* ]]; then
            # предупреждение о плавающих тегах
            if grep -qsE 'image:.*:latest([[:space:]]|$)|image:[[:space:]]*[^:]+$' "$file"; then
                warn "$svc: образ без фиксированного тега (:latest или без тега) — по ТЗ рекомендуются фиксированные версии"
            fi
            info "up -d: $svc"
            run_visible $DC -f "$file" up -d
            up_count=$((up_count+1))
        else
            if service_running "$file"; then to_stop+=("$svc|$file"); fi
        fi
    done <<< "$rows"

    # Предложить остановить снятые с отметки, но запущенные
    if [ "${#to_stop[@]}" -gt 0 ]; then
        local names=""
        local item
        for item in "${to_stop[@]}"; do names+="  ${item%%|*}\n"; done
        if ui_yesno "Остановить сервисы?" "Эти сервисы запущены, но НЕ отмечены:\n\n$names\nОстановить их (docker compose down)?"; then
            for item in "${to_stop[@]}"; do
                svc="${item%%|*}"; file="${item##*|}"
                info "down: $svc"
                run_visible $DC -f "$file" down
            done
        else
            info "снятые сервисы оставлены запущенными"
        fi
    fi

    commit_config "docker: deploy.sh + up ($up_count сервисов)"
    stage_docker_summary "$up_count"
    log "--- stage_docker end ---"
}

stage_docker_summary() {
    local n="$1"
    ui_msg "Итог: Docker" "Поднято сервисов: $n
Compose-папки: $SERVICES_SRC/<service>/ (рядом со скриптом)
deploy.sh:     $NAS_CONFIG/scripts/deploy.sh (применить всё разом)

Проверка:
  docker ps
  docker compose ls
  bash $NAS_CONFIG/scripts/deploy.sh

Рекомендации (ТЗ): фиксированные теги образов (не latest), restart: unless-stopped,
volumes на $STORAGE_MNT/<service>/."
    echo "Этап 4 завершён. Поднято сервисов: $n"
}

# ---------------------------------------------------------------------------
# Общие помощники для этапов 5-8
# ---------------------------------------------------------------------------
enable_service() {
    local svc="$1"
    systemctl is-enabled "$svc" >/dev/null 2>&1 || run systemctl enable "$svc"
    systemctl is-active  "$svc" >/dev/null 2>&1 || run systemctl start "$svc"
}

backup_file() {
    local f="$1"
    [ -f "$f" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] бэкап $f"; return 0; fi
    cp -a "$f" "${f}.bak.$(date '+%Y%m%d-%H%M%S')" && info "бэкап: $f"
}

boot_config_path() {
    if   [ -f /boot/firmware/config.txt ]; then echo /boot/firmware/config.txt
    elif [ -f /boot/config.txt ];          then echo /boot/config.txt
    else echo ""; fi
}

# LAN-подсеть вида 192.168.1.0/24 (по connected-маршруту)
detect_lan_cidr() {
    ip -o -f inet route show scope link 2>/dev/null \
        | awk '$1 ~ /\// && $1 !~ /^169\.254/ {print $1; exit}'
}

# checklist -> " tag1 tag2 " для проверки вида: case " $sel " in *" tag "*)
checklist_selected() { printf ' %s ' "$(printf '%s' "$1" | tr -d '"')"; }

# ---------------------------------------------------------------------------
# ЭТАП 5: Pi-тюнинг (железо). config.txt-правки требуют перезагрузки.
# ---------------------------------------------------------------------------
pi_pcie3() {
    local cfg="$1"
    if [ -z "$cfg" ]; then warn "config.txt не найден — PCIe Gen3 пропущен"; return 0; fi
    backup_file "$cfg"
    append_line "dtparam=pciex1_gen=3" "$cfg"
    info "PCIe Gen3 для NVMe добавлен в $cfg (применится после reboot)"
}
pi_wifi_powersave_off() {
    run mkdir -p /etc/NetworkManager/conf.d
    write_file /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
    systemctl is-active NetworkManager >/dev/null 2>&1 && run systemctl restart NetworkManager
    info "Wi-Fi power-save отключён"
}
pi_watchdog() {
    run mkdir -p /etc/systemd/system.conf.d
    write_file /etc/systemd/system.conf.d/watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
EOF
    run systemctl daemon-reexec
    info "watchdog включён (RuntimeWatchdogSec=15s)"
}
# USB max current — на Pi5 без этого суммарный ток USB режется до 600mA => просадки на USB-SSD
pi_usb_power() {
    local cfg="$1"
    if [ -z "$cfg" ]; then warn "config.txt не найден — USB power пропущен"; return 0; fi
    backup_file "$cfg"
    append_line "usb_max_current_enable=1" "$cfg"
    info "usb_max_current_enable=1 (питание USB-дисков; применится после reboot)"
}
# Memory cgroup для docker-лимитов (правка cmdline.txt — файл в ОДНУ строку!)
pi_cgroup() {
    local cl=/boot/firmware/cmdline.txt
    [ -f "$cl" ] || cl=/boot/cmdline.txt
    [ -f "$cl" ] || { warn "cmdline.txt не найден — cgroup пропущен"; return 0; }
    if grep -qs 'cgroup_enable=memory' "$cl"; then info "memory cgroup уже включён"; return 0; fi
    backup_file "$cl"
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] добавить cgroup_enable=memory cgroup_memory=1 в $cl"
        return 0
    fi
    sed -i 's/\bcgroup_disable=memory\b//g; s/[[:space:]]\+/ /g; s/[[:space:]]*$//' "$cl"
    sed -i '1 s|$| cgroup_enable=memory cgroup_memory=1|' "$cl"
    info "memory cgroup включён в $cl (нужен reboot; лимиты памяти в docker-compose)"
}
pi_sysctl() {
    write_file /etc/sysctl.d/99-nas.conf <<'EOF'
# nas-wizard: тюнинг для NAS
vm.swappiness = 10
net.core.somaxconn = 512
net.ipv4.tcp_keepalive_time = 120
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3
EOF
    run sysctl --system
    info "sysctl-тюнинг применён (swappiness=10, somaxconn=512, tcp keepalive)"
}
# Глушим legacy-службу zramswap.service (пакет zram-tools). На современном
# Raspberry Pi OS zram-swap поднимает systemd-zram-generator/rpi-swap, и
# zramswap.service ВОЮЕТ с ним за /dev/zram0: "Device or resource busy",
# "zram0 is mounted; will not make swapspace" — вечный спам failed в журнале.
zram_disable_zramtools() {
    if systemctl list-unit-files 2>/dev/null | grep -q '^zramswap\.service' \
       || [ -e /etc/systemd/system/zramswap.service ]; then
        run systemctl stop zramswap.service 2>/dev/null || true
        run systemctl disable zramswap.service 2>/dev/null || true
        # самопальный оверрайд-юнит в /etc (от прежних версий) — убрать
        [ -e /etc/systemd/system/zramswap.service ] && run rm -f /etc/systemd/system/zramswap.service
        run systemctl daemon-reload 2>/dev/null || true
        run systemctl mask zramswap.service 2>/dev/null || true
    fi
}
# Есть ли штатный zram-генератор (современный Pi OS Bookworm+)?
zram_have_native() {
    [ -e /usr/lib/systemd/system/systemd-zram-setup@.service ] \
    || [ -f /etc/rpi/swap.conf ] \
    || [ -f /etc/systemd/zram-generator.conf ] \
    || [ -f /usr/lib/systemd/zram-generator.conf ]
}
pi_zram() {
    if zram_have_native; then
        # 1) гасим конфликтующий zramswap.service, чтобы он не лез в zram0
        zram_disable_zramtools
        # 2) настраиваем штатный zram: ~50% RAM (кап 4 GiB), zstd (kernel default)
        if [ "$DRY_RUN" -eq 0 ]; then
            if [ -f /etc/rpi/swap.conf ]; then
                run mkdir -p /etc/rpi/swap.conf.d
                cat > /etc/rpi/swap.conf.d/60-nas-os.conf <<'EOF'
# NAS-OS: zram-swap включён (~50% RAM, кап 4 GiB). См. swap.conf(5).
[Main]
Mechanism=zram+file
[Zram]
RamMultiplier=0.5
MaxSizeMiB=4096
EOF
            else
                run mkdir -p /etc/systemd
                cat > /etc/systemd/zram-generator.conf <<'EOF'
# NAS-OS: zram-swap (zstd, ~50% RAM, кап 4 GiB). См. zram-generator.conf(5).
[zram0]
zram-size = min(ram / 2, 4096)
compression-algorithm = zstd
EOF
            fi
            run systemctl daemon-reload 2>/dev/null || true
            # применить на живую (best-effort; полностью — со следующей загрузки).
            # reset-failed — иначе быстрый рестарт .swap ловит start-limit и
            # оставляет систему вообще без свопа.
            run systemctl reset-failed dev-zram0.swap systemd-zram-setup@zram0.service 2>/dev/null || true
            run systemctl restart dev-zram0.swap 2>/dev/null \
                || run systemctl start dev-zram0.swap 2>/dev/null || true
        fi
        info "zram-swap: zstd, ~50% RAM (штатный systemd-zram-generator)"
        return 0
    fi
    # Legacy-система без генератора — ставим zram-tools
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
# VID:PID USB-накопителей (для usb-storage.quirks) — по одному на строку, уникально
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
# Отключить UAS для USB-SATA-мостов (флаки-адаптеры сбрасываются под нагрузкой) через usb-storage.quirks в cmdline
pi_uas_quirks() {
    local cl=/boot/firmware/cmdline.txt
    [ -f "$cl" ] || cl=/boot/cmdline.txt
    [ -f "$cl" ] || { warn "cmdline.txt не найден — UAS-quirks пропущены"; return 0; }
    local ids; ids="${NASW_QUIRKS:-$(detect_usb_storage_ids)}"
    [ -n "$ids" ] || { warn "USB-накопители не найдены — UAS-quirks пропущены"; return 0; }
    local want="" id
    for id in $ids; do want="${want:+$want,}${id}:u"; done
    local line cur merged
    line="$(head -1 "$cl")"
    cur="$(printf '%s\n' "$line" | grep -o 'usb-storage\.quirks=[^ ]*' | head -1 | sed 's/usb-storage\.quirks=//')"
    merged="$(printf '%s,%s' "$cur" "$want" | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"
    if [ -n "$cur" ] && [ "$cur" = "$merged" ]; then info "usb-storage.quirks уже настроен ($cur)"; return 0; fi
    backup_file "$cl"
    if [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] usb-storage.quirks=$merged в $cl"; return 0; fi
    if [ -n "$cur" ]; then
        line="$(printf '%s\n' "$line" | sed "s#usb-storage\.quirks=[^ ]*#usb-storage.quirks=$merged#")"
    else
        line="$line usb-storage.quirks=$merged"
    fi
    printf '%s\n' "$line" > "$cl"
    info "UAS отключён для USB-мостов: $merged (в $cl; нужен reboot)"
}
# Точное время: chrony вместо systemd-timesyncd
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
    info "chrony включён, systemd-timesyncd отключён (точная синхронизация времени)"
}
# Адаптивный CPU-governor по температуре/троттлингу (для безвентиляторного корпуса)
pi_governor() {
    write_file /usr/local/bin/nas-governor.sh <<'EOF'
#!/bin/bash
# nas-wizard: адаптивный CPU governor по температуре/троттлингу
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
    info "адаптивный CPU governor включён (каждые 2 мин: ≥80°C или троттл → powersave, иначе ondemand)"
}

stage_pi() {
    echo; echo "=== Этап 5: Pi-тюнинг ==="
    log "--- stage_pi start ---"
    local cfg temp throttled
    cfg="$(boot_config_path)"
    temp="$(vcgencmd measure_temp 2>/dev/null | sed 's/temp=//')"
    throttled="$(vcgencmd get_throttled 2>/dev/null)"

    local raw
    raw="$(ui_checklist "Pi-тюнинг (железо)" \
        "Тек. темп: ${temp:-?}  throttle: ${throttled:-?}\nОтметьте действия (правки config.txt/cmdline требуют перезагрузки):" \
        "usbpower" "USB max current — питание USB-дисков (Pi5)" ON \
        "trim"     "Включить fstrim.timer (TRIM для SSD/NVMe)" ON \
        "pcie3"    "PCIe Gen3 для NVMe — быстрее, но вне спеки" OFF \
        "cgroup"   "Memory cgroup — лимиты памяти для docker" OFF \
        "sysctl"   "Sysctl-тюнинг (swappiness, somaxconn, tcp)" OFF \
        "zram"     "zram-swap (zstd, 50% RAM)" OFF \
        "uasquirks" "Отключить UAS для USB-дисков (флаки-мосты)" OFF \
        "chrony"   "chrony вместо timesyncd (точное время)" OFF \
        "governor" "Адаптивный CPU governor по температуре" OFF \
        "eeprom"   "Обновить прошивку EEPROM (rpi-eeprom)" OFF \
        "wifips"   "Отключить Wi-Fi power-save (стабильность)" OFF \
        "watchdog" "Watchdog: авто-ребут при зависании" OFF)" || { info "отменено"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    local need_reboot=0
    case "$sel" in *" usbpower "*) pi_usb_power "$cfg"; need_reboot=1 ;; esac
    case "$sel" in *" trim "*)     enable_service fstrim.timer ;; esac
    case "$sel" in *" pcie3 "*)    pi_pcie3 "$cfg"; need_reboot=1 ;; esac
    case "$sel" in *" cgroup "*)   pi_cgroup; need_reboot=1 ;; esac
    case "$sel" in *" sysctl "*)   pi_sysctl ;; esac
    case "$sel" in *" zram "*)     pi_zram ;; esac
    case "$sel" in *" uasquirks "*) pi_uas_quirks; need_reboot=1 ;; esac
    case "$sel" in *" chrony "*)   pi_chrony ;; esac
    case "$sel" in *" governor "*) pi_governor ;; esac
    case "$sel" in *" eeprom "*)   run rpi-eeprom-update -a; need_reboot=1 ;; esac
    case "$sel" in *" wifips "*)   pi_wifi_powersave_off ;; esac
    case "$sel" in *" watchdog "*) pi_watchdog ;; esac

    commit_config "pi-tuning"
    local extra=""
    [ "$need_reboot" -eq 1 ] && extra="

ВНИМАНИЕ: изменения config.txt/EEPROM применятся после ПЕРЕЗАГРУЗКИ."
    ui_msg "Итог: Pi-тюнинг" "Готово.$extra

Проверка:
  vcgencmd measure_temp
  vcgencmd get_throttled   (0x0 = всё ок)
  sudo lspci -vv | grep -i speed   (после reboot для PCIe)"
    log "--- stage_pi end ---"
}

# ---------------------------------------------------------------------------
# ЭТАП 6: Безопасность / базовые настройки
# ---------------------------------------------------------------------------
sec_unattended() {
    install_packages "security" unattended-upgrades apt-listchanges
    write_file /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
    info "unattended-upgrades включены (только security по умолчанию)"
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
        warn "log2ram держит /var/log в оперативке — журнал не переживёт выключение"
        run systemctl disable --now log2ram
        info "log2ram отключён ради постоянного журнала"
    fi
    run systemctl restart systemd-journald
    info "journald: постоянный журнал, лимит 200M"
}
sec_log2ram() {
    # log2ram spares an SD card from write wear. On an NVMe/SSD root it buys nothing
    # and costs every log written since the last sync whenever power is cut — which
    # is precisely when the logs matter. Refuse instead of silently losing them.
    local rootdev
    rootdev="$(findmnt -no SOURCE / 2>/dev/null | sed 's|^/dev/||')"
    case "$rootdev" in
        mmcblk*) ;;
        *) warn "корень не на SD-карте (/dev/${rootdev:-?}) — log2ram не нужен и лишает вас логов при аварийном выключении"
           return 0 ;;
    esac
    if dpkg -s log2ram >/dev/null 2>&1; then info "log2ram уже установлен"; return 0; fi
    info "подключаю внешний репозиторий azlux для log2ram"
    run mkdir -p /usr/share/keyrings
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] добавить ключ+репо azlux, apt install log2ram"
    else
        wget -qO- https://azlux.fr/repo.gpg 2>>"$LOG" | gpg --dearmor > /usr/share/keyrings/azlux.gpg 2>>"$LOG" || { warn "не удалось получить ключ azlux"; return 1; }
        echo "deb [signed-by=/usr/share/keyrings/azlux.gpg] http://packages.azlux.fr/debian/ stable main" > /etc/apt/sources.list.d/azlux.list
        run apt-get update
        run apt-get install -y log2ram
    fi
    info "log2ram установлен (логи в RAM, сброс на диск по таймеру)"
}
sec_ufw() {
    install_packages "firewall" ufw
    # СНАЧАЛА разрешаем SSH, потом включаем — чтобы не заблокировать себя
    run ufw --force reset
    run ufw default deny incoming
    run ufw default allow outgoing
    if ufw app list 2>/dev/null | grep -q OpenSSH; then run ufw allow OpenSSH; else run ufw allow 22/tcp; fi
    run ufw allow 9090/tcp    # Cockpit
    # Открыть порты шар, если они установлены
    if dpkg -s samba >/dev/null 2>&1; then run ufw allow Samba 2>/dev/null || run ufw allow 445/tcp; fi
    if dpkg -s nfs-kernel-server >/dev/null 2>&1; then run ufw allow 2049/tcp; run ufw allow 111/tcp; fi
    run ufw --force enable
    info "ufw включён (SSH, Cockpit 9090, шары — если есть)"
    warn "docker публикует порты в обход ufw (iptables) — учитывайте это"
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
    info "fail2ban включён (jail sshd)"
}
sec_sshkeys() {
    local akeys="$TARGET_HOME/.ssh/authorized_keys"
    if [ ! -s "$akeys" ]; then
        ui_msg "SSH: небезопасно" "У пользователя $TARGET_USER НЕТ SSH-ключей ($akeys пуст/отсутствует).

Отключение входа по паролю ЗАБЛОКИРУЕТ вам доступ. Пропускаю — сначала добавьте ключ:
  ssh-copy-id $TARGET_USER@<pi>"
        warn "SSH-ключи не найдены — вход по паролю НЕ отключён (защита от блокировки)"
        return 0
    fi
    run mkdir -p /etc/ssh/sshd_config.d
    write_file /etc/ssh/sshd_config.d/99-nas.conf <<'EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
    run systemctl restart ssh 2>/dev/null || run systemctl restart sshd
    info "SSH: вход по паролю отключён (ключи есть)"
}

stage_security() {
    echo; echo "=== Этап 6: Безопасность ==="
    log "--- stage_security start ---"
    local raw
    raw="$(ui_checklist "Безопасность / базовые настройки" "Отметьте, что настроить:" \
        "unattended" "Авто security-обновления (unattended-upgrades)" ON \
        "journald"   "Лимит journald 200M (меньше износ SD)" ON \
        "log2ram"    "log2ram: логи в RAM (внешний репозиторий)" OFF \
        "ufw"        "Firewall ufw (SSH, Cockpit, шары)" OFF \
        "fail2ban"   "fail2ban для SSH" OFF \
        "sshkeys"    "SSH: отключить пароль (нужны ключи!)" OFF)" || { info "отменено"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" unattended "*) sec_unattended ;; esac
    case "$sel" in *" journald "*)   sec_journald ;; esac
    case "$sel" in *" log2ram "*)    sec_log2ram ;; esac
    case "$sel" in *" fail2ban "*)   sec_fail2ban ;; esac
    case "$sel" in *" ufw "*)        sec_ufw ;; esac   # ufw после шар/fail2ban, чтобы открыть их порты
    case "$sel" in *" sshkeys "*)    sec_sshkeys ;; esac

    commit_config "security"
    ui_msg "Итог: Безопасность" "Готово.

Проверка:
  sudo ufw status verbose
  sudo fail2ban-client status sshd
  systemctl status unattended-upgrades
  journalctl --disk-usage"
    log "--- stage_security end ---"
}

# ---------------------------------------------------------------------------
# ЭТАП 7: Сетевые шары (Samba / NFS / Avahi) к /mnt/storage
# ---------------------------------------------------------------------------
shares_samba() {
    install_packages "samba" samba
    local user pass1 pass2 share="/mnt/storage"
    findmnt -no TARGET "$share" >/dev/null 2>&1 || warn "$share пока не смонтирован (пул mergerfs) — шара будет отдавать локальную папку"
    if ! grep -qs '^\[storage\]' /etc/samba/smb.conf; then
        backup_file /etc/samba/smb.conf
        user="$(ui_input "Samba" "Пользователь для доступа к шаре:" "$TARGET_USER")" || return 0
        [ -z "$user" ] && user="$TARGET_USER"
        {
            echo ""
            echo "[storage]"
            echo "   path = $share"
            echo "   browseable = yes"
            echo "   read only = no"
            echo "   valid users = $user"
            echo "   create mask = 0664"
            echo "   directory mask = 0775"
        } >> /etc/samba/smb.conf 2>/dev/null
        [ "$DRY_RUN" -eq 1 ] && info "[DRY-RUN] добавить [storage] в /etc/samba/smb.conf для $user"
        # пароль Samba
        if [ "$DRY_RUN" -eq 0 ]; then
            pass1="$(ui_password "Samba пароль" "Пароль Samba для $user:")" || pass1=""
            pass2="$(ui_password "Samba пароль" "Повторите пароль:")" || pass2=""
            if [ -n "$pass1" ] && [ "$pass1" = "$pass2" ]; then
                printf '%s\n%s\n' "$pass1" "$pass1" | smbpasswd -a -s "$user" >>"$LOG" 2>&1 && info "Samba-пароль установлен для $user"
            else
                warn "пароли не совпали/пусты — задайте вручную: sudo smbpasswd -a $user"
            fi
        fi
    else
        info "[storage] уже есть в smb.conf"
    fi
    enable_service smbd
    systemctl list-unit-files nmbd.service >/dev/null 2>&1 && enable_service nmbd
    info "Samba: //$(hostname)/storage"
}
shares_nfs() {
    install_packages "nfs" nfs-kernel-server
    local cidr share="/mnt/storage"
    cidr="$(detect_lan_cidr)"; [ -z "$cidr" ] && cidr="192.168.0.0/24"
    cidr="$(ui_input "NFS" "Кому разрешить доступ (подсеть):" "$cidr")" || return 0
    local line="$share $cidr(rw,sync,no_subtree_check,root_squash)"
    if ! grep -qsF "$share " /etc/exports; then
        backup_file /etc/exports
        append_line "$line" /etc/exports
        run exportfs -ra
    else
        info "экспорт $share уже есть в /etc/exports"
    fi
    enable_service nfs-server
    info "NFS: $share -> $cidr"
}
shares_avahi() {
    install_packages "avahi" avahi-daemon
    enable_service avahi-daemon
    info "Avahi/mDNS включён: $(hostname).local"
}

stage_shares() {
    echo; echo "=== Этап 7: Сетевые шары ==="
    log "--- stage_shares start ---"
    local raw
    raw="$(ui_checklist "Сетевые шары" "Доступ к /mnt/storage по сети:" \
        "samba" "Samba (SMB) — Windows/Mac/телефон" OFF \
        "nfs"   "NFS — Linux-клиенты" OFF \
        "avahi" "Avahi/mDNS — виден как <host>.local" ON)" || { info "отменено"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" samba "*) shares_samba ;; esac
    case "$sel" in *" nfs "*)   shares_nfs ;; esac
    case "$sel" in *" avahi "*) shares_avahi ;; esac

    ui_msg "Итог: Сетевые шары" "Готово.

Проверка:
  smbclient -L localhost -U <user>      (Samba)
  showmount -e localhost                 (NFS)
  avahi-browse -a                        (mDNS)

Если включён ufw — порты шар уже открыты (при повторном запуске этапа 6)."
    log "--- stage_shares end ---"
}

# ---------------------------------------------------------------------------
# ЭТАП 8: Бэкапы и мониторинг (SMART-алерты, health)
# ---------------------------------------------------------------------------
bk_smartd() {
    install_packages "smart" smartmontools
    write_file /usr/local/bin/nas-smart-alert.sh <<'ALERT'
#!/usr/bin/env bash
# nas-wizard: вызывается smartd при проблеме с диском
LOG=/var/log/nas-smart.log
echo "$(date '+%F %T') SMART ALERT: ${SMARTD_MESSAGE:-unknown} (${SMARTD_DEVICE:-?})" >> "$LOG"
[ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "SMART: проблема диска" "${SMARTD_DEVICE:-?}: ${SMARTD_MESSAGE:-error}" 1 || true
ALERT
    run chmod +x /usr/local/bin/nas-smart-alert.sh
    install_notify_helper
    install_netguard
    install_motd
    if ! grep -qs 'nas-smart-alert' /etc/smartd.conf 2>/dev/null; then
        backup_file /etc/smartd.conf
        write_file /etc/smartd.conf <<'EOF'
# nas-wizard: мониторить все диски, алерт через nas-smart-alert.sh
DEVICESCAN -a -o on -S on -n standby,q -s (S/../.././02|L/../../6/03) -W 4,45,55 -m root -M exec /usr/local/bin/nas-smart-alert.sh
EOF
    fi
    enable_service smartd
    info "smartd включён (алерты -> /var/log/nas-smart.log + ping)"
}
bk_spacetemp() {
    write_file /usr/local/bin/nas-health-check.sh <<'HEALTH'
#!/usr/bin/env bash
# nas-wizard: алерт по заполнению пула и температуре Pi
set -uo pipefail
LOG=/var/log/nas-health.log
DISK_PCT_MAX=90
TEMP_MAX=75
alert=0; msg=""
if mountpoint -q /mnt/storage; then
    pct=$(df --output=pcent /mnt/storage 2>/dev/null | tr -dc '0-9')
    if [ -n "$pct" ] && [ "$pct" -ge "$DISK_PCT_MAX" ]; then alert=1; msg="$msg диск=${pct}%"; fi
fi
if command -v vcgencmd >/dev/null 2>&1; then
    t=$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.' | cut -d. -f1)
    if [ -n "$t" ] && [ "$t" -ge "$TEMP_MAX" ]; then alert=1; msg="$msg темп=${t}C"; fi
fi
if [ "$alert" -eq 1 ]; then
    echo "$(date '+%F %T') HEALTH ALERT:$msg" >> "$LOG"
    [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "NAS: внимание" "Порог превышен:$msg" 1 || true
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
    info "health-таймер включён (диск>90% / темп>75C -> ping+лог)"
}
stage_backup() {
    echo; echo "=== Этап 8: Бэкапы и мониторинг ==="
    log "--- stage_backup start ---"
    local raw
    raw="$(ui_checklist "Бэкапы и мониторинг" "Что настроить:" \
        "smartd"    "SMART-мониторинг дисков + алерт" ON \
        "spacetemp" "Алерт: заполнение диска и температура Pi" ON)" || { info "отменено"; return 0; }

    local sel; sel="$(checklist_selected "$raw")"
    case "$sel" in *" smartd "*)    bk_smartd ;; esac
    case "$sel" in *" spacetemp "*) bk_spacetemp ;; esac

    commit_config "backup/monitoring"
    ui_msg "Итог: Бэкапы/мониторинг" "Готово.

Уведомления используют /etc/nas-wizard/notify.conf (Pushover).

Проверка:
  systemctl status smartd
  systemctl list-timers 'nas-*'
  cat /var/log/nas-smart.log /var/log/nas-health.log"
    log "--- stage_backup end ---"
}

# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------
main_menu() {
    while true; do
        local choice
        choice="$(ui_menu "NAS Wizard (Raspberry Pi 5)$([ "$DRY_RUN" -eq 1 ] && echo '  [DRY-RUN]')" \
            "Выберите этап. Лог: $LOG" \
            "system"   "Этап 1: подготовка системы (пакеты, cockpit, docker, каталоги)" \
            "disk"     "Этап 2: подключить диск (формат -> fstab -> mount)" \
            "mergerfs" "Этап 2b: собрать/обновить пул mergerfs (>=2 дисков)" \
            "snapraid" "Этап 3: SnapRAID (conf, sync, таймеры, уведомления)" \
            "docker"   "Этап 4: Docker (найти compose-папки и поднять)" \
            "pi"       "Этап 5: Pi-тюнинг (PCIe, USB-питание, watchdog, temp)" \
            "security" "Этап 6: Безопасность (ufw, fail2ban, SSH, journald)" \
            "shares"   "Этап 7: Сетевые шары (Samba/NFS/Avahi)" \
            "backup"   "Этап 8: Бэкапы и мониторинг (SMART, health, restic)" \
            "quit"     "Выход")" || break

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
    echo "Готово. Полный лог: $LOG"
}

# ---------------------------------------------------------------------------
# Уведомления (Pushover) — единый помощник, зовётся из обёрток
# ---------------------------------------------------------------------------
NOTIFY_CONF=/etc/nas-wizard/notify.conf
# Точечно выставить KEY="VAL" в notify.conf, не затирая чужие ключи: у файла
# три писателя (Healthchecks-URL из мастера, Pushover из api notify и веб-UI).
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
# nas-wizard: уведомление через Pushover.  nas-notify.sh "Заголовок" "Текст" [priority]
CONF=/etc/nas-wizard/notify.conf
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
# Сторож сети: «один активный линк за раз» + уведомление о смене сети.
#
# Зачем. eth0 и wlan0 живут в одной подсети 192.168.1.0/24. Когда оба подняты,
# Linux ломается предсказуемо: ARP-flux (оба интерфейса отвечают на ARP за чужой
# адрес), NM снимает on-link маршрут подсети по ложному ACD-конфликту, ответы
# соседям уходят через шлюз, путь становится асимметричным — и роутер, видя лишь
# половину TCP-сессии, впрыскивает RST. Симптом: ping идёт, а HTTP висит.
# Лечится тем, что активным держим ровно один интерфейс.
# ---------------------------------------------------------------------------
install_netguard() {
    write_file /usr/local/bin/nas-netguard.sh <<'GUARD'
#!/bin/bash
# nas-wizard: один активный линк за раз + сторож сети.
# Проводной eth0 — главный, Wi-Fi wlan0 — резерв. Пока eth0 реально рабочий
# (есть carrier, адрес и отзывается шлюз) — Wi-Fi отключён. Как только провод
# пропал или завис (у macb на Pi 5 бывает TX stall) — Wi-Fi возвращается.
set -u
ETH="${NAS_ETH:-eth0}"
WIFI="${NAS_WIFI:-wlan0}"
STATE="${NAS_NETGUARD_STATE:-/var/lib/nas-wizard/netguard.state}"
LOCK="${NAS_NETGUARD_LOCK:-/run/nas-netguard.lock}"

notify(){ [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$1" "$2" "${3:-0}" >/dev/null 2>&1 || true; }
logj(){ logger -t nas-netguard -- "$*" 2>/dev/null || true; }

# таймер и NM-dispatcher могут выстрелить одновременно
exec 9>"$LOCK" 2>/dev/null || exit 0
flock -n 9 || exit 0

has_nm(){ command -v nmcli >/dev/null 2>&1; }
dev_state(){ nmcli -t -f DEVICE,STATE device 2>/dev/null | awk -F: -v d="$1" '$1==d{print $2; exit}'; }
ip4(){ ip -4 -o addr show dev "$1" scope global 2>/dev/null | awk '{print $4; exit}'; }

eth_healthy(){
  [ -e "/sys/class/net/$ETH" ] || return 1
  [ "$(cat "/sys/class/net/$ETH/carrier" 2>/dev/null || echo 0)" = "1" ] || return 1
  [ -n "$(ip4 "$ETH")" ] || return 1
  local g
  g="$(ip -4 route show default dev "$ETH" 2>/dev/null | awk '{print $3; exit}')"
  [ -n "$g" ] || return 1
  # шлюз может не отвечать на ICMP — тогда пробуем достучаться на канальном уровне
  ping -c1 -W2 -I "$ETH" "$g" >/dev/null 2>&1 && return 0
  arping -c1 -w2 -I "$ETH" "$g" >/dev/null 2>&1
}

# NM иногда снимает on-link маршрут подсети (ложный ACD-конфликт при двух
# интерфейсах в одной сети). Без него ответы соседям уезжают через шлюз.
fix_onlink(){
  local dev="$1" cidr net
  cidr="$(ip4 "$dev")"; [ -n "$cidr" ] || return 0
  net="$(python3 -c 'import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)' "$cidr" 2>/dev/null)"
  [ -n "$net" ] || return 0
  ip -4 route show "$net" dev "$dev" scope link 2>/dev/null | grep -q . && return 0
  ip -4 route add "$net" dev "$dev" proto kernel scope link src "${cidr%%/*}" 2>/dev/null \
    && logj "восстановлен on-link маршрут $net dev $dev"
}

if eth_healthy; then
  ACTIVE="$ETH"
  if has_nm && [ "$(dev_state "$WIFI")" = "connected" ]; then
    logj "провод рабочий — отключаю $WIFI"
    nmcli device disconnect "$WIFI" >/dev/null 2>&1 || true
  fi
else
  ACTIVE="$WIFI"
  if has_nm && [ "$(dev_state "$WIFI")" != "connected" ]; then
    logj "провода нет — поднимаю $WIFI"
    nmcli device connect "$WIFI" >/dev/null 2>&1 || true
    sleep 3
  fi
fi
fix_onlink "$ACTIVE"

# смена интерфейса/адреса -> Pushover. Первый прогон только запоминает состояние,
# чтобы установка и перезагрузка не сыпали уведомлениями.
CUR_IF="$ACTIVE"
CUR_IP="$(ip4 "$ACTIVE")"; CUR_IP="${CUR_IP%%/*}"
[ -n "$CUR_IP" ] || exit 0
OLD_IF=""; OLD_IP=""
[ -r "$STATE" ] && . "$STATE"
if [ "$OLD_IF" != "$CUR_IF" ] || [ "$OLD_IP" != "$CUR_IP" ]; then
  if [ -n "$OLD_IF" ] && [ "$OLD_IF" != "$CUR_IF" ]; then
    notify "NAS: сеть переключилась" "Активный интерфейс: $OLD_IF → $CUR_IF"
  fi
  if [ -n "$OLD_IP" ] && [ "$OLD_IP" != "$CUR_IP" ]; then
    notify "NAS: изменился IP" "Было $OLD_IP → стало $CUR_IP"
  fi
  mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
  printf 'OLD_IF=%s\nOLD_IP=%s\n' "$CUR_IF" "$CUR_IP" > "$STATE"
  logj "активный линк $CUR_IF ($CUR_IP)"
fi
GUARD
    run chmod +x /usr/local/bin/nas-netguard.sh
    run mkdir -p /var/lib/nas-wizard

    # мгновенная реакция на смену линка. Звать nmcli прямо отсюда нельзя:
    # NM ждёт завершения dispatcher-скрипта, а nmcli ждёт ответа NM -> дедлок.
    run mkdir -p /etc/NetworkManager/dispatcher.d   # write_file родителя не создаёт
    write_file /etc/NetworkManager/dispatcher.d/50-nas-netguard <<'DISP'
#!/bin/bash
# nas-wizard: дёрнуть сторож сети при смене состояния линка (асинхронно!)
case "${2:-}" in up|down|carrier-up|carrier-down|dhcp4-change) ;; *) exit 0 ;; esac
case "${1:-}" in eth0|wlan0) ;; *) exit 0 ;; esac
systemctl start --no-block nas-netguard.service >/dev/null 2>&1 || true
exit 0
DISP
    run chmod 755 /etc/NetworkManager/dispatcher.d/50-nas-netguard
    # та же ловушка: NM выполняет всё, что лежит в dispatcher.d
    run rm -f /etc/NetworkManager/dispatcher.d/50-nas-netguard.bak.*

    write_file /etc/systemd/system/nas-netguard.service <<'UNIT'
[Unit]
Description=NAS: один активный линк + сторож сети
After=NetworkManager.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nas-netguard.sh
UNIT

    write_file /etc/systemd/system/nas-netguard.timer <<'UNIT'
[Unit]
Description=NAS: периодическая проверка сети

[Timer]
OnBootSec=45s
OnUnitActiveSec=30s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

    # Страховка на окно, пока оба интерфейса подняты: каждый отвечает ARP только
    # за свой адрес и представляется своим (иначе ARP-flux травит кэш соседей).
    write_file /etc/sysctl.d/99-nas-arp.conf <<'SYSCTL'
# nas-wizard: два интерфейса в одной подсети — без этого возможен ARP-flux
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
SYSCTL
    run sysctl -q --system

    run systemctl daemon-reload
    run systemctl enable --now nas-netguard.timer
    info "сторож сети включён: eth0 главный, wlan0 резерв, уведомления о смене IP"
    # честно предупреждаем: если сейчас подняты оба линка, Wi-Fi будет отключён,
    # и открытая по нему сессия (SSH/панель) оборвётся — надо зайти по адресу eth0
    if [ "$(cat /sys/class/net/eth0/carrier 2>/dev/null || echo 0)" = "1" ] \
       && nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -q '^wlan0:connected$'; then
        warn "кабель подключён — Wi-Fi будет отключён. Если вы зашли по Wi-Fi, сессия оборвётся; заходите заново по <хост>.local"
    fi
}

# ---------------------------------------------------------------------------
# Приветствие при входе по SSH (MOTD).
# sshd здесь с PrintMotd=no — текст рисует pam_motd из /etc/update-motd.d/,
# поэтому свой блок кладём туда, а не правим /etc/motd (его гасим: юридическая
# простыня Debian на NAS только мешает; бэкап делает write_file).
# ---------------------------------------------------------------------------
install_motd() {
    # пользовательский текст создаём только если его ещё нет — не затирать правки
    run mkdir -p /etc/nas-wizard
    if [ ! -f /etc/nas-wizard/motd.txt ]; then
        write_file /etc/nas-wizard/motd.txt <<'TXT'
NAS-OS - home NAS on Raspberry Pi 5

  Panel        http://pi5.local/
  Data pool    /mnt/storage          Stacks  ~/services
  Panel logs   journalctl -u nas-web -f

  Always power off with `sudo poweroff` - SnapRAID hates sudden power loss.
TXT
    fi
    [ -f /etc/nas-wizard/motd.conf ] || write_file /etc/nas-wizard/motd.conf <<'CONF'
# nas-wizard: что показывать при входе по SSH
MOTD_LOGO=1
MOTD_TEXT=1
MOTD_INFO=1
# чужие куски приветствия (применяет nas-web при старте и при сохранении):
MOTD_UNAME=1
MOTD_COCKPIT=1
MOTD_LASTLOG=1
CONF

    run mkdir -p /etc/update-motd.d
    write_file /etc/update-motd.d/20-nas-os <<'MOTD'
#!/bin/bash
# nas-wizard: приветствие при входе по SSH.
# ВНИМАНИЕ: выполняется на КАЖДОМ логине. Только дешёвые команды; ничего, что
# будит спящие диски (никаких smartctl/hdparm) и ничего, что лезет в сеть.
CONF=/etc/nas-wizard/motd.conf
TXT=/etc/nas-wizard/motd.txt
MOTD_TEXT=1; MOTD_INFO=1
[ -r "$CONF" ] && . "$CONF"

# NO_COLOR=1 — для предпросмотра в веб-панели
if [ -n "${NO_COLOR:-}" ]; then
  B=""; D=""; G=""; Y=""; R=""
else
  B=$'\033[1;36m'; D=$'\033[2;37m'; G=$'\033[1;32m'; Y=$'\033[1;33m'; R=$'\033[0m'
fi
# метки латиницей: printf меряет байты, кириллица ломала бы выравнивание колонок
row(){ printf '  %s%-12s%s %s\n' "$D" "$1" "$R" "$2"; }

# ---- значения. Считаем один раз: их используют и свой текст (через токены), и сводка.
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

# route get только читает таблицу маршрутов, в сеть не ходит. Разбираем по ключам:
# у маршрута может не быть "via", зато в хвосте бывает "uid 1000".
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

# ---- свой текст: подставляем токены. Никакого eval — только замена подстрок,
# поэтому команды и переменные внутри текста не выполняются.
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
    # write_file делает бэкап через `cp -a`, сохраняя бит исполнения, а pam_motd
    # запускает ВСЕ файлы каталога — старая копия печатала приветствие второй раз.
    run rm -f /etc/update-motd.d/20-nas-os.bak.*

    # юридическая простыня Debian поверх нашего блока — только шум
    write_file /etc/motd </dev/null
    info "SSH-приветствие установлено (/etc/nas-wizard/motd.txt — свой текст)"
}

setup_snapraid_notify_noninteractive() { :; }   # уведомления настраиваются отдельно (api notify)

# ---------------------------------------------------------------------------
# Неинтерактивные apply-обёртки для API (переиспользуют проверенные функции)
# ---------------------------------------------------------------------------
stage_system_apply() {
    export DEBIAN_FRONTEND=noninteractive
    # первым делом — обновляем систему целиком (по просьбе: apt update && full-upgrade)
    run apt-get update
    run apt-get full-upgrade -y
    install_packages "NAS-стек"  "${STACK_PACKAGES[@]}"
    install_packages "утилиты"   "${UTIL_PACKAGES[@]}"
    install_packages "Pi-пакеты" "${PI_PACKAGES[@]}"
    ensure_docker_repo   # docker-ce + compose-plugin из официального репо Docker
    ensure_gh            # GitHub CLI (для пуша кода панели с бокса)
    local svc
    for svc in cockpit.socket docker; do enable_service "$svc"; done
    systemctl list-unit-files fstrim.timer >/dev/null 2>&1 && enable_service fstrim.timer
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
    # Превью файлов: кэш + ночной прогрев (ffmpeg/pdftoppm ставятся выше в утилитах)
    run mkdir -p /var/cache/nas-thumbs
    write_file /etc/systemd/system/nas-thumbs.service <<UNIT
[Unit]
Description=NAS thumbnail cache sweep
[Service]
Type=oneshot
Nice=15
IOSchedulingClass=idle
ExecStart=/usr/bin/python3 $SCRIPT_DIR/nas-web.py thumbs-sweep $STORAGE_MNT /home/$TARGET_USER
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
    echo "система подготовлена"
}
# Смонтировать съёмный носитель в базу автомонтирования (явное действие: формат/монтирование).
# Монтирует напрямую, независимо от того, включён ли udev-автомаунт.
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
        # пользователь указал свою точку монтирования — создаём её (mkdir -p), не подбираем _N
        case "$want" in /*) ;; *) echo "точка монтирования должна быть абсолютным путём"; return 2 ;; esac
        case "$want" in *..*) echo "недопустимый путь"; return 2 ;; esac
        target="$want"
        if findmnt -rn "$target" >/dev/null 2>&1; then echo "в $target уже что-то смонтировано"; return 2; fi
    else
        target="$base/$label"
        # не затирать чужой каталог с данными
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
    [ -n "$dev" ] || { echo "не указан диск (NASW_DEV)"; return 2; }
    [ -b "$dev" ] || { echo "ОТКАЗ: $dev не блочное устройство"; return 2; }
    is_protected "$dev" && { echo "ОТКАЗ: $dev — системный диск"; return 2; }
    # защитить и разделы системного диска: проверить родительское устройство
    parent="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1)"
    [ -n "$parent" ] && is_protected "/dev/$parent" && { echo "ОТКАЗ: $dev — раздел системного диска"; return 2; }
    disk_in_use "$dev"  && { echo "ОТКАЗ: $dev смонтирован (сначала отмонтируйте)"; return 2; }
    # НЕ переформатировать уже настроенный диск (мог временно отвалиться от mount из-за nofail)
    disk_already_configured "$dev" && { echo "ОТКАЗ: $dev уже настроен (его UUID есть в /etc/fstab) — форматирование отменено во избежание потери данных"; return 2; }
    case "$role" in
        parity)
            n="$(next_parity_number)"; mp="/mnt/parity${n}"; label="${NASW_LABEL:-parity${n}}"
            format_and_mount "$dev" "$mp" "$fs" "$label" 2
            echo "готово: $dev -> $mp" ;;
        removable|media|usb)
            label="${NASW_LABEL:-USB}"
            make_fs "$dev" "$fs" "$label"
            run partprobe "$dev" 2>/dev/null || true
            automount_now "$dev"
            echo "готово: $dev отформатирован ($fs, метка «$label») и смонтирован" ;;
        *)
            n="$(next_disk_number)"; mp="/mnt/disk${n}"; label="${NASW_LABEL:-disk${n}}"
            format_and_mount "$dev" "$mp" "$fs" "$label" 2
            [ "$(mounted_data_disks | grep -c .)" -ge 2 ] && generate_mergerfs
            echo "готово: $dev -> $mp" ;;
    esac
}
# Смонтировать произвольное устройство (флешку/раздел) в базу автомонтирования
api_mount_dev() {
    local dev="${NASW_DEV:-}"
    [ -n "$dev" ] || { echo "не указан диск (NASW_DEV)"; return 2; }
    is_protected "$dev" && { echo "ОТКАЗ: $dev — системный диск"; return 2; }
    disk_in_use "$dev" && { echo "$dev уже смонтирован"; return 0; }
    [ -n "$(blkid -s TYPE -o value "$dev" 2>/dev/null)" ] || { echo "на $dev нет файловой системы"; return 2; }
    automount_now "$dev" "${NASW_TARGET:-}" || return $?
    echo "смонтирован $dev"
}
api_label_disk() {
    local dev="${NASW_DEV:-}" label="${NASW_LABEL:-}" fs mp rc
    [ -n "$dev" ] || { echo "не указан диск (NASW_DEV)"; return 2; }
    [ -n "$label" ] || { echo "не указана метка (NASW_LABEL)"; return 2; }
    is_protected "$dev" && { echo "ОТКАЗ: $dev — системный диск"; return 2; }
    fs="$(blkid -s TYPE -o value "$dev" 2>/dev/null)"
    [ -n "$fs" ] || { echo "на $dev нет файловой системы"; return 2; }
    mp="$(findmnt -no TARGET "$dev" 2>/dev/null | head -1)"
    case "$fs" in
        ext2|ext3|ext4) run e2label "$dev" "$label"; rc=$? ;;
        xfs)   [ -z "$mp" ] || { echo "xfs: сначала отмонтируйте раздел"; return 2; }
               command -v xfs_admin >/dev/null || { echo "нет xfs_admin (установите xfsprogs)"; return 2; }
               run xfs_admin -L "$label" "$dev"; rc=$? ;;
        vfat)  run fatlabel "$dev" "$(printf '%s' "$label" | tr 'a-z' 'A-Z' | cut -c1-11)"; rc=$? ;;
        exfat) run exfatlabel "$dev" "$label"; rc=$? ;;
        ntfs)  run ntfslabel "$dev" "$label"; rc=$? ;;
        btrfs) run btrfs filesystem label "${mp:-$dev}" "$label"; rc=$? ;;
        *)     echo "переименование не поддержано для ФС $fs"; return 2 ;;
    esac
    [ "${rc:-1}" -eq 0 ] || { echo "не удалось переименовать $dev ($fs) — см. лог"; return 1; }
    run udevadm trigger --settle "$dev" 2>/dev/null || true
    echo "метка $dev -> «$label» ($fs)"
}
# Установить/обновить автомонтирование съёмных носителей (udev + systemd-run + helper)
install_automount() {
    local user="${1:-$TARGET_USER}" base="${2:-/media/nas}"
    run mkdir -p /etc/nas-wizard "$base"
    write_file /etc/nas-wizard/automount.conf <<EOF
# nas-wizard: автомонтирование съёмных носителей
ENABLED=1
BASE="$base"
AM_USER="$user"
OPTS_NATIVE="rw,noatime,nofail"
EOF
    write_file /usr/local/bin/nas-automount.sh <<'AM'
#!/usr/bin/env bash
# nas-wizard: авто-монтирование/размонтирование съёмных носителей (вызывается из udev через systemd-run)
set -uo pipefail
CONF=/etc/nas-wizard/automount.conf
ENABLED=1; BASE=/media/nas; AM_USER=""; OPTS_NATIVE="rw,noatime,nofail"
[ -f "$CONF" ] && . "$CONF"
LOG=/var/log/nas-automount.log
log(){ printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null; }
ACTION="${1:-}"; KDEV="${2:-}"
[ "$ENABLED" = "1" ] || { log "выключено — пропуск"; exit 0; }
[ -n "$KDEV" ] || exit 0
DEV="/dev/$KDEV"

clean_stale(){    # снять все монтирования под BASE, чей девайс исчез
  findmnt -rn -o TARGET,SOURCE 2>/dev/null | while read -r t s; do
    case "$t" in "$BASE"/*)
      [ -b "$s" ] || { umount -l "$t" 2>>"$LOG" && rmdir "$t" 2>/dev/null; log "снято $t (девайс исчез)"; } ;;
    esac
  done
}
do_add(){
  local fs uuid label name target opts uid gid i=1
  fs="$(blkid -s TYPE -o value "$DEV" 2>/dev/null)"; [ -n "$fs" ] || { log "нет ФС на $DEV"; exit 0; }
  uuid="$(blkid -s UUID -o value "$DEV" 2>/dev/null)"
  grep -qsF "UUID=$uuid" /etc/fstab && { log "$DEV в fstab — пропуск"; exit 0; }
  findmnt -rn -S "$DEV" >/dev/null 2>&1 && { log "$DEV уже смонтирован"; exit 0; }
  label="$(blkid -s LABEL -o value "$DEV" 2>/dev/null)"
  name="${label:-$KDEV}"; name="${name//[^A-Za-z0-9._-]/_}"
  target="$BASE/$name"
  while findmnt -rn "$target" >/dev/null 2>&1 || { [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; }; do
    target="$BASE/${name}_$i"; i=$((i+1)); done
  mkdir -p "$target"
  uid="$(id -u "${AM_USER:-1000}" 2>/dev/null || echo 1000)"; gid="$(id -g "${AM_USER:-1000}" 2>/dev/null || echo 1000)"
  case "$fs" in
    vfat|exfat|ntfs) opts="rw,noatime,nofail,uid=$uid,gid=$gid,umask=002" ;;
    *)               opts="$OPTS_NATIVE" ;;
  esac
  if mount -o "$opts" "$DEV" "$target" 2>>"$LOG"; then log "смонтирован $DEV ($fs) -> $target"
  else mount "$DEV" "$target" 2>>"$LOG" && log "смонтирован(деф.) $DEV -> $target" || { rmdir "$target" 2>/dev/null; log "ОШИБКА монтирования $DEV"; }
  fi
}
case "$ACTION" in
  add)    do_add ;;
  remove) clean_stale ;;
  *)      exit 0 ;;
esac
AM
    run chmod +x /usr/local/bin/nas-automount.sh
    write_file /etc/udev/rules.d/99-nas-automount.rules <<'RULES'
# nas-wizard: автомонтирование съёмных USB-носителей.
# Матчим по ID_USB_DRIVER (usb-storage/uas), а НЕ по ID_BUS==usb: USB-SATA мосты
# (UAS) отдают диск как ID_BUS=ata, и старое правило для них не срабатывало.
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
        echo "автомонтирование выключено"
        return 0
    fi
    install_automount "$user" "$base"
    echo "автомонтирование включено (USB-носители -> $base)"
}
api_pi() {
    local cfg k; cfg="$(boot_config_path)"
    for k in ${NASW_KEYS:-}; do case "$k" in
        usbpower) pi_usb_power "$cfg" ;;   pcie3) pi_pcie3 "$cfg" ;;
        trim)     enable_service fstrim.timer ;; eeprom) run rpi-eeprom-update -a ;;
        cgroup)   pi_cgroup ;;  sysctl) pi_sysctl ;;  zram) pi_zram ;;
        uasquirks) pi_uas_quirks ;; chrony) pi_chrony ;; governor) pi_governor ;;
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
# Модули: comitup / Tailscale / статический IP / Cockpit-GUI
# ---------------------------------------------------------------------------
mod_comitup() {
    if dpkg -s comitup >/dev/null 2>&1; then echo "comitup уже установлен"; return 0; fi
    if [ "$DRY_RUN" -eq 1 ]; then
        info "[DRY-RUN] подключить репозиторий davesteele + apt install comitup"
        echo "comitup (dry-run)"; return 0
    fi
    warn "comitup управляет сетью — на Wi-Fi возможен кратковременный обрыв связи"
    run mkdir -p /usr/share/keyrings
    if curl -fsSL https://davesteele.github.io/key-366150CE.pub.txt 2>>"$LOG" | gpg --dearmor > /usr/share/keyrings/davesteele.gpg 2>>"$LOG"; then
        echo "deb [signed-by=/usr/share/keyrings/davesteele.gpg] https://davesteele.github.io/comitup/repo comitup main" > /etc/apt/sources.list.d/comitup.list
        run apt-get update
        run apt-get install -y comitup
        run systemctl enable comitup 2>/dev/null || true
        echo "comitup установлен (Wi-Fi точка доступа + captive-портал)"
    else
        warn "не удалось получить ключ davesteele — comitup пропущен"
    fi
}
mod_tailscale() {
    if command -v tailscale >/dev/null 2>&1; then echo "tailscale уже установлен"
    elif [ "$DRY_RUN" -eq 1 ]; then info "[DRY-RUN] установка tailscale (get.tailscale.com)"
    else curl -fsSL https://tailscale.com/install.sh 2>>"$LOG" | sh >>"$LOG" 2>&1 || warn "не удалось установить tailscale"; fi
    echo "Tailscale готов. Войдите: sudo tailscale up"
}
mod_staticip() {
    local ip="${NASW_IP:-}" gw="${NASW_GW:-}" dns="${NASW_DNS:-1.1.1.1}" con
    [ -n "$ip" ] || { echo "не указан IP (NASW_IP)"; return 2; }
    con="$(nmcli -t -f NAME connection show --active 2>/dev/null | head -1)"
    [ -n "$con" ] || { echo "активное подключение NetworkManager не найдено"; return 2; }
    run nmcli connection modify "$con" ipv4.addresses "$ip" ${gw:+ipv4.gateway "$gw"} ipv4.dns "$dns" ipv4.method manual
    run nmcli connection up "$con"
    echo "статический IP $ip назначен ($con)"
}
mod_cockpit_gui() {
    # cockpit-machines есть в Debian; file-sharing/navigator — из репозитория 45drives
    if ! dpkg -s cockpit-navigator >/dev/null 2>&1 && ! apt-cache show cockpit-navigator >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            info "[DRY-RUN] подключить репозиторий 45drives (repo.45drives.com/setup)"
        else
            curl -fsSL https://repo.45drives.com/setup 2>>"$LOG" | bash >>"$LOG" 2>&1 || warn "не удалось подключить репозиторий 45drives"
        fi
    fi
    install_packages "cockpit-gui" cockpit-machines cockpit-file-sharing cockpit-navigator
    echo "Cockpit-модули установлены (доступные в репозитории)"
}

# ---------------------------------------------------------------------------
# API-режим (headless, для nas-web.py). Без whiptail; подтверждения — из браузера.
# Параметры в NASW_* ; вывод — человекочитаемый лог в stdout, код возврата 0/≠0.
# ---------------------------------------------------------------------------
api_compose_file() {           # $1=service -> печатает путь compose-файла
    local svc="$1" f
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
        [ -f "$SERVICES_SRC/$svc/$f" ] && { echo "$SERVICES_SRC/$svc/$f"; return 0; }
    done
    return 1
}
api_docker() {                 # $1=up|down|restart|pull
    local act="$1" svc="${NASW_SERVICE:-}" file DC
    [ -n "$svc" ] || { echo "не указан сервис"; return 2; }
    file="$(api_compose_file "$svc")" || { echo "compose-файл не найден: $svc"; return 2; }
    DC="$(docker_compose_cmd)"; [ -n "$DC" ] || { echo "docker compose недоступен"; return 2; }
    echo "== $act $svc =="
    case "$act" in
        up)      run_visible $DC -f "$file" up -d ;;
        down)    run_visible $DC -f "$file" down ;;
        restart) run_visible $DC -f "$file" restart ;;
        pull)    run_visible $DC -f "$file" pull ;;
        *)       echo "неизвестное действие: $act"; return 2 ;;
    esac
}
# Установить и запустить Dockge (менеджер стеков). Стеки живут в /opt/stacks.
api_dockge() {
    local dir="${NASW_STACKS_DIR:-/opt/stacks}" src DC
    src="$(api_compose_file dockge)" || { echo "compose Dockge не найден"; return 2; }
    run mkdir -p "$dir/dockge" /opt/docker/dockge/data
    run cp -f "$src" "$dir/dockge/compose.yaml"
    info "Dockge → $dir/dockge/compose.yaml"
    DC="$(docker_compose_cmd)"; [ -n "$DC" ] || { echo "docker compose недоступен — сначала этап «Система»"; return 2; }
    run_visible $DC -f "$dir/dockge/compose.yaml" up -d
    echo "Dockge запущен → http://<pi>:5001 (управляет стеками в $dir)"
}
# Скопировать выбранные bundled-стеки (NASW_KEYS) в каталог Dockge. Не запускаем — старт в Dockge.
api_copy_stacks() {
    local dir="${NASW_STACKS_DIR:-/opt/stacks}" name src n=0
    run mkdir -p "$dir"
    for name in ${NASW_KEYS:-}; do
        [ "$name" = "dockge" ] && continue
        src="$(api_compose_file "$name")" || { warn "нет compose для $name — пропуск"; continue; }
        if [ -e "$dir/$name/compose.yaml" ]; then info "$name уже в Dockge — пропуск"; continue; fi
        run mkdir -p "$dir/$name"
        run cp -f "$src" "$dir/$name/compose.yaml"
        [ -f "$SERVICES_SRC/$name/.env" ] && run cp -f "$SERVICES_SRC/$name/.env" "$dir/$name/.env"
        info "стек добавлен: $name → $dir/$name/"
        n=$((n+1))
    done
    echo "Готово: добавлено стеков — $n (в $dir). Запускайте их в Dockge (http://<pi>:5001)."
}
# запустить набор функций по ключам из NASW_KEYS (через пробел)
api_keys_run() {               # $1=prefix (pi|sec|...) ; вызывает <prefix>_<key>
    local prefix="$1" k
    for k in ${NASW_KEYS:-}; do
        if declare -F "${prefix}_${k}" >/dev/null; then "${prefix}_${k}"; fi
    done
}
api_notify() {                 # Pushover в /etc/nas-wizard/notify.conf
    notify_conf_set PUSHOVER_USER  "${NASW_PUSER:-}"
    notify_conf_set PUSHOVER_TOKEN "${NASW_PTOKEN:-}"
    install_notify_helper
    echo "Pushover настроен"
}
api_state() {                  # краткое состояние для мастера (JSON)
    local host tz iface
    host="$(hostnamectl --static 2>/dev/null || hostname)"
    tz="$(timedatectl show -p Timezone --value 2>/dev/null)"
    iface="$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')"
    printf '{"host":"%s","tz":"%s","iface":"%s","docker":%s,"cockpit":%s,"data_disks":%s,"parity_disks":%s,"pool":%s,"snapraid":%s}\n' \
        "$host" "$tz" "$iface" \
        "$(command -v docker >/dev/null 2>&1 && echo true || echo false)" \
        "$(systemctl is-active cockpit.socket >/dev/null 2>&1 && echo true || echo false)" \
        "$(mounted_data_disks | grep -c . )" \
        "$(mounted_parity_disks | grep -c . )" \
        "$(findmnt -no TARGET "$STORAGE_MNT" >/dev/null 2>&1 && echo true || echo false)" \
        "$([ -f /etc/snapraid.conf ] && echo true || echo false)"
}

run_api() {
    local action="$1"
    # неинтерактивные заглушки UI: подтверждения уже сделаны в браузере
    ui_msg(){ :; }; ui_yesno(){ return 0; }; ui_input(){ echo "${3:-}"; }
    ui_password(){ echo "${NASW_PASSWORD:-}"; }; ui_checklist(){ echo ""; }
    case "$action" in
        state)          api_state ;;
        docker-up)      api_docker up ;;
        docker-down)    api_docker down ;;
        docker-restart) api_docker restart ;;
        docker-pull)    api_docker pull ;;
        dockge)         api_dockge ;;
        copy-stacks)    api_copy_stacks ;;
        system)         stage_system_apply ;;
        format-disk)    api_format_disk ;;
        label-disk)     api_label_disk ;;
        mount-dev)      api_mount_dev ;;
        automount)      api_automount ;;
        mergerfs)       generate_mergerfs ;;
        snapraid)       ensure_snapraid_conf && { setup_snapraid_notify_noninteractive; install_snapraid_wrapper; install_snapraid_timers; [ "${NASW_SYNC:-0}" = "1" ] && run_visible snapraid sync; } ;;
        snapraid-sync)  if [ -x /usr/local/bin/nas-snapraid.sh ]; then run_visible /usr/local/bin/nas-snapraid.sh "${NASW_KIND:-sync}"; else echo "SnapRAID не настроен — сначала пройдите Мастер (этап SnapRAID)"; exit 2; fi ;;
        pi)             api_pi ;;
        security)       api_keys_run sec ;;
        shares)         api_shares ;;
        backup)         api_keys_run bk ;;
        notify)         api_notify ;;
        netguard)       install_netguard ;;
        motd)           install_motd ;;
        comitup)        mod_comitup ;;
        tailscale)      mod_tailscale ;;
        staticip)       mod_staticip ;;
        cockpit-gui)    mod_cockpit_gui ;;
        *)              echo "неизвестное api-действие: $action" >&2; return 2 ;;
    esac
}

# ---------------------------------------------------------------------------
# Точка входа
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
        echo "*** РЕЖИМ --dry-run: изменения не выполняются, только план действий ***"
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
        *)        die "неизвестный этап: $FORCE_STAGE (system|disk|mergerfs|snapraid|docker|pi|security|shares|backup)" ;;
    esac
}

main "$@"
