#!/usr/bin/env python3
"""
nas-web.py — веб-бэкенд мастера настройки NAS и рабочего стола (Raspberry Pi 5).

Только стандартная библиотека Python 3 (без pip). Отдаёт статику из web/ и JSON API:
  GET  /api/stats                 — живые метрики Pi (CPU, temp, RAM, диск, сеть, uptime)
  GET  /api/desktop               — ярлыки рабочего стола из docker-лейблов web-desktop.*
  GET/POST /api/creds             — хранилище доступов (~/nas-config/credentials.json, 0600)
  GET  /api/setup/state           — состояние системы для мастера
  POST /api/setup/<action>        — выполнить шаг мастера (делегирует nas-wizard.sh api)

Системные изменения выполняет проверенный движок nas-wizard.sh (api-режим), поэтому
сервер нужно запускать от root (launcher nas-setup.sh это делает).
"""
import json, os, re, subprocess, time, shutil, socket, threading, pwd, mimetypes, glob
import pty, select, struct, hashlib, base64, signal, fcntl, termios, secrets, hmac
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlencode

HERE        = os.path.dirname(os.path.realpath(__file__))
WEB_DIR     = os.path.join(HERE, "web")
SERVICES    = os.path.join(HERE, "services")
ENGINE      = os.path.join(HERE, "nas-wizard.sh")
TARGET_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or "oleg"
HOME        = os.path.expanduser("~" + TARGET_USER)
NAS_CONFIG  = os.path.join(HOME, "nas-config")
CREDS_FILE  = os.path.join(NAS_CONFIG, "credentials.json")
TRASH       = os.path.join(HOME, ".nas-trash")
PORT        = int(os.environ.get("NAS_WEB_PORT", "8080"))
STORAGE     = "/mnt/storage"
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
CRON_URL    = os.environ.get("CRONMASTER_URL", "http://127.0.0.1:8123")  # опубликованный порт cronmaster

# --------------------------------------------------------------------------- #
#  Сбор метрик (read-only, root не нужен)
# --------------------------------------------------------------------------- #
def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default

def cpu_percent(sample=0.20):
    def snap():
        parts = _read("/proc/stat").splitlines()[0].split()[1:]
        vals = list(map(int, parts))
        idle = vals[3] + vals[4]
        return sum(vals), idle
    t1, i1 = snap(); time.sleep(sample); t2, i2 = snap()
    dt, di = t2 - t1, i2 - i1
    return round(100 * (dt - di) / dt, 1) if dt else 0.0

def temp_c():
    raw = _read("/sys/class/thermal/thermal_zone0/temp", "")
    try:
        return round(int(raw) / 1000, 1)
    except ValueError:
        return None

def mem_info():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.strip().split()[0])  # kB
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", 0)
    used = total - avail
    return {"total": total * 1024, "used": used * 1024,
            "pct": round(100 * used / total, 1) if total else 0,
            "swap_total": m.get("SwapTotal", 0) * 1024, "swap_free": m.get("SwapFree", 0) * 1024}

def disk_info(path):
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    total = s.f_blocks * s.f_frsize
    free  = s.f_bavail * s.f_frsize
    used  = total - free
    return {"path": path, "total": total, "used": used,
            "pct": round(100 * used / total, 1) if total else 0}

def default_iface():
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[1] == "00000000":
            return f[0]
    return None

_NET_CACHE = {}
def net_rate(iface):
    if not iface:
        return {"rx": 0, "tx": 0}
    def rd():
        for line in _read("/proc/net/dev").splitlines():
            if line.strip().startswith(iface + ":"):
                f = line.split(":")[1].split()
                return int(f[0]), int(f[8])
        return 0, 0
    now = time.time(); rx, tx = rd()
    prev = _NET_CACHE.get(iface)
    _NET_CACHE[iface] = (now, rx, tx)
    if not prev:
        return {"rx": 0, "tx": 0}
    dt = now - prev[0] or 1
    return {"rx": max(0, int((rx - prev[1]) / dt)), "tx": max(0, int((tx - prev[2]) / dt))}

def uptime_s():
    try:
        return int(float(_read("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return 0

def throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        val = out.split("=")[-1]
        return {"raw": val, "ok": val in ("0x0", "0x0\n", "")}
    except (OSError, subprocess.SubprocessError):
        return {"raw": None, "ok": True}

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 1)); ip = s.getsockname()[0]; s.close()
        return ip
    except OSError:
        return "127.0.0.1"

def _lsblk():
    try:
        out = subprocess.run(["lsblk", "-J", "-o",
              "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,MOUNTPOINT,FSTYPE,LABEL,TRAN,RM,ROTA,PARTTYPENAME"],
              capture_output=True, text=True, timeout=8).stdout
        return json.loads(out).get("blockdevices", [])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

AUTOMOUNT_CONF = "/etc/nas-wizard/automount.conf"
def automount_state():
    """Состояние автомонтирования: включено ли, база, пользователь."""
    conf = _read(AUTOMOUNT_CONF)
    st = {"enabled": False, "base": "/media/nas", "user": TARGET_USER,
          "installed": os.path.isfile("/etc/udev/rules.d/99-nas-automount.rules")}
    for line in conf.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if k == "ENABLED":
            st["enabled"] = v == "1"
        elif k == "BASE":
            st["base"] = v or st["base"]
        elif k == "AM_USER":
            st["user"] = v or st["user"]
    st["enabled"] = st["enabled"] and st["installed"]
    return st

def _smart_has_data(j):
    return bool(j.get("smart_status") or j.get("ata_smart_attributes")
                or j.get("nvme_smart_health_information_log")
                or (j.get("temperature") or {}).get("current") is not None)

def _smartctl_json(extra, dev, timeout=12):
    """smartctl -j с фолбэком типа устройства: голый → -d sat → -d scsi
    (USB-мосты без -d sat отдают только баннер версии)."""
    # для типа устройства подбираем варианты; NVMe работает напрямую
    variants = [[]] if dev.startswith("/dev/nvme") else [[], ["-d", "sat"], ["-d", "scsi"]]
    last = {}
    for dt in variants:
        try:
            p = subprocess.run(["smartctl", "-j"] + extra + dt + [dev],
                               capture_output=True, text=True, timeout=timeout)
            j = json.loads(p.stdout or "{}")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            continue
        last = j
        if _smart_has_data(j):
            return j
    return last

def smart_info(dev):
    j = _smartctl_json(["-H", "-A"], dev, timeout=8)
    if not _smart_has_data(j):
        return None
    return {"temp": (j.get("temperature") or {}).get("current"),
            "healthy": (j.get("smart_status") or {}).get("passed"),
            "hours": (j.get("power_on_time") or {}).get("hours")}

def fs_tools():
    """Файловые системы, для которых есть mkfs (что реально можно создать)."""
    return [fs for fs in ("ext4", "xfs", "btrfs", "exfat", "ntfs", "vfat")
            if shutil.which("mkfs." + fs)]

def disks():
    res = []
    am_base = automount_state().get("base", "/media/nas")
    spin = _load_spindown()
    for d in _lsblk():
        if d.get("type") != "disk":
            continue
        name = d.get("name", "")
        if name.startswith(("zram", "loop")):
            continue
        mounts = []
        def collect(node):
            mp = node.get("mountpoint")
            if mp:
                mounts.append(mp)
            for ch in node.get("children", []) or []:
                collect(ch)
        collect(d)
        role = "free"
        for mp in mounts:
            if mp in ("/", "/boot", "/boot/firmware", "/var"):
                role = "system"; break
        if role == "free":
            for mp in mounts:
                if mp.startswith("/mnt/disk"):
                    role = "data"; break
                if mp.startswith("/mnt/parity"):
                    role = "parity"; break
                if mp == STORAGE:
                    role = "pool"; break
                if mp == am_base or mp.startswith(am_base + "/") or mp.startswith("/media/"):
                    role = "removable"; break
        # для системного диска показываем корень «/», а не /boot/firmware (иначе «свободно»
        # берётся с крошечного boot-раздела); для остальных — точку в /mnt, затем /media
        primary = (("/" if "/" in mounts else None)
                   or next((mp for mp in mounts if mp.startswith("/mnt/")), None)
                   or next((mp for mp in mounts if mp.startswith("/media/")), None)
                   or (mounts[0] if mounts else None))
        # ФС и метка смонтированного/первого раздела
        fstype = d.get("fstype")
        label = d.get("label")
        parts = []
        for ch in d.get("children", []) or []:
            cmp = ch.get("mountpoint")
            parts.append({
                "name": ch.get("name"), "path": ch.get("path"), "size": ch.get("size"),
                "fstype": ch.get("fstype"), "label": ch.get("label"),
                "mount": cmp, "mounted": bool(cmp),
                "parttypename": ch.get("parttypename"),
            })
            if cmp:
                fstype = ch.get("fstype") or fstype
                label = ch.get("label") or label
        if label is None and parts:
            label = parts[0].get("label")
        fstab = _read("/etc/fstab")
        in_fstab = bool(primary and primary in fstab)
        size = d.get("size")
        # пустой слот картридера / нет вставленного носителя → lsblk отдаёт размер 0B
        no_media = (str(size).strip() in ("", "0", "0B", "None")) and not parts and not fstype
        res.append({
            "name": name, "path": d.get("path"), "size": size,
            "model": (d.get("model") or "").strip(), "serial": d.get("serial"),
            "tran": d.get("tran"), "role": role, "mounts": mounts, "mount": primary,
            "fstype": fstype, "label": label, "partitions": parts, "no_media": no_media,
            "removable": d.get("rm") in (True, "1", 1),
            "rotational": d.get("rota") in (True, "1", 1), "in_fstab": in_fstab,
            "mounted": bool(mounts),
            "usage": disk_info(primary) if primary else None,
            "smart": smart_info(d.get("path")),
            "spindown": spin.get(d.get("path")),
        })
    return res

def external_volumes():
    """Тома вне пула (USB-диски/флешки, свободные диски с ФС) — для ярлыков
    на рабочем столе и секции «Диски» в сайдбаре файлового менеджера."""
    vols = []
    for d in disks():
        if d.get("no_media") or d["role"] not in ("free", "removable"):
            continue
        parts = d["partitions"] or []
        if not parts and d.get("fstype"):      # ФС прямо на диске, без таблицы разделов
            parts = [{"name": d["name"], "path": d["path"], "size": d["size"],
                      "fstype": d["fstype"], "label": d["label"],
                      "mount": d["mount"], "mounted": d["mounted"]}]
        for p in parts:
            fs = p.get("fstype")
            if not fs or fs in ("swap", "linux_raid_member", "LVM2_member", "crypto_LUKS"):
                continue
            if p.get("parttypename") == "EFI System" or (p.get("label") or "").upper() == "EFI":
                continue
            vols.append({
                "dev": p["path"], "label": p.get("label") or (d.get("model") or "").strip() or p["name"],
                "size": p["size"], "fstype": fs, "mount": p.get("mount"),
                "mounted": bool(p.get("mount")), "disk": d["path"],
                "rotational": d.get("rotational"), "tran": d.get("tran"),
            })
    return vols

# --------------------------------------------------------------------------- #
#  SMART detail + disk actions
# --------------------------------------------------------------------------- #
def smart_detail(dev):
    j = _smartctl_json(["-a"], dev, timeout=12)
    if not _smart_has_data(j):
        return {"ok": False, "log": "SMART недоступен: диск/USB-мост не отдаёт данные (или нужен root)"}
    attrs = []
    ata = (j.get("ata_smart_attributes") or {}).get("table")
    if ata:
        for a in ata:
            attrs.append({"id": a.get("id"), "name": a.get("name"),
                          "value": a.get("value"), "worst": a.get("worst"),
                          "thresh": a.get("thresh"), "raw": (a.get("raw") or {}).get("string")})
    nv = j.get("nvme_smart_health_information_log")
    if nv:
        for k in ("temperature", "available_spare", "percentage_used", "power_cycles",
                  "power_on_hours", "unsafe_shutdowns", "media_errors", "data_units_written"):
            if k in nv:
                attrs.append({"id": None, "name": k, "value": None, "worst": None,
                              "thresh": None, "raw": str(nv[k])})
    st = j.get("smart_status") or {}
    return {
        "ok": True, "model": j.get("model_name"), "serial": j.get("serial_number"),
        "capacity": (j.get("user_capacity") or {}).get("bytes"),
        "healthy": st.get("passed"),
        "temp": (j.get("temperature") or {}).get("current"),
        "power_on_hours": (j.get("power_on_time") or {}).get("hours"),
        "power_cycles": j.get("power_cycle_count"),
        "rotation": j.get("rotation_rate"),
        "selftest": (j.get("ata_smart_self_test_log") or {}).get("standard", {}).get("table")
                    or (j.get("nvme_self_test_log") or {}).get("table"),
        "attrs": attrs,
    }

def _run(cmd, timeout=40):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode, "log": (p.stdout + p.stderr).strip()}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "code": -1, "log": str(e)}

def disk_mount(target, unmount=False):
    if not re.match(r"^/[\w/.+-]+$", target or ""):
        return {"ok": False, "log": "недопустимый путь"}
    r = _run(["umount" if unmount else "mount", target])
    if r["ok"] and not r["log"]:
        r["log"] = "размонтировано" if unmount else "смонтировано"
    return r

def _smart_dtype(dev):
    """Определить рабочий -d тип для устройства (для команд, не читающих JSON)."""
    if dev.startswith("/dev/nvme"):
        return []
    for dt in ([], ["-d", "sat"], ["-d", "scsi"]):
        try:
            p = subprocess.run(["smartctl", "-j", "-H"] + dt + [dev],
                               capture_output=True, text=True, timeout=8)
            if _smart_has_data(json.loads(p.stdout or "{}")):
                return dt
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            continue
    return []

def smart_test(dev, kind):
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "недопустимое устройство"}
    kind = kind if kind in ("short", "long") else "short"
    return _run(["smartctl", "-t", kind] + _smart_dtype(dev) + [dev], timeout=20)

def _disk_mountpoints(dev):
    out = _run(["lsblk", "-nrpo", "NAME,MOUNTPOINT", dev], timeout=8).get("log", "")
    mps = []
    for line in out.splitlines():
        p = line.split(None, 1)
        mp = (p[1].strip() if len(p) > 1 else "")
        if mp:
            mps.append(mp)
    return mps

_SYS_MPS = ("/", "/boot", "/boot/firmware", "/var", "/home", "/usr")

def disk_speedtest(dev):
    """Безопасный тест скорости последовательного ЧТЕНИЯ (мимо кэша, только чтение)."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "недопустимое устройство"}
    if not os.path.exists(dev):
        return {"ok": False, "log": "нет такого устройства"}
    r = _run(["dd", "if=" + dev, "of=/dev/null", "bs=4M", "count=256", "iflag=direct"], timeout=90)
    m = re.search(r"([\d.,]+)\s*([kMG]?B)/s", r.get("log", ""))
    if not m:
        return {"ok": False, "log": "не удалось измерить: " + (r.get("log", "")[-120:])}
    val = float(m.group(1).replace(",", ".")); unit = m.group(2)
    mbps = val * {"B": 1e-6, "kB": 1e-3, "MB": 1, "GB": 1e3}.get(unit, 1)
    return {"ok": True, "read_mbps": round(mbps, 1), "log": "последовательное чтение: %.0f МБ/с" % mbps}

def disk_eject(dev):
    """Безопасно извлечь съёмный диск: отмонтировать все разделы + снять питание."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "недопустимое устройство"}
    mps = _disk_mountpoints(dev)
    for mp in mps:
        if mp in _SYS_MPS or mp == STORAGE or mp.startswith("/mnt/disk") or mp.startswith("/mnt/parity"):
            return {"ok": False, "log": "это системный или пуловый диск — извлечение запрещено"}
    for mp in mps:
        r = _run(["umount", mp], timeout=20)
        if not r["ok"]:
            return {"ok": False, "log": "диск занят (%s): %s" % (mp, r["log"][-80:])}
    po = _run(["udisksctl", "power-off", "-b", dev], timeout=15)
    return {"ok": True, "log": "можно отключать" + (" (питание снято)" if po["ok"] else " (отмонтировано)")}

def health_report():
    """Одностраничная сводка здоровья: список проверок с уровнем ok/warn/bad."""
    checks = []
    def add(name, value, lvl, hint=""):
        checks.append({"name": name, "value": value, "lvl": lvl, "hint": hint})
    s = stats()
    t = s.get("temp")
    add("Температура CPU", ("%s °C" % t) if t is not None else "—",
        "bad" if t and t >= 75 else "warn" if t and t >= 65 else "ok")
    thr = s.get("throttled") or {}
    add("Питание и троттлинг", "просадка/троттлинг" if not thr.get("ok", True) else "в норме",
        "bad" if not thr.get("ok", True) else "ok",
        "Нехватка тока БП или перегрев" if not thr.get("ok", True) else "")
    m = (s.get("mem") or {}).get("pct", 0)
    add("Оперативная память", "%s%% занято" % m, "warn" if m >= 90 else "ok")
    root = disk_info("/") or {}
    rp = root.get("pct", 0)
    add("Системная карта /", "%s%% занято" % rp, "bad" if rp >= 95 else "warn" if rp >= 90 else "ok",
        "Свободно %s" % _fmt_b(root.get("total", 0) - root.get("used", 0)) if root else "")
    pool = s.get("disk_pool") or {}
    if pool.get("path") == "/mnt/storage":
        pp = pool.get("pct", 0)
        add("Хранилище (пул)", "%s%% занято" % pp, "bad" if pp >= 95 else "warn" if pp >= 90 else "ok",
            "Свободно %s" % _fmt_b(pool.get("total", 0) - pool.get("used", 0)))
    # диски: SMART здоровье и температура
    ds = disks()
    bad = [d["name"] for d in ds if (d.get("smart") or {}).get("healthy") is False]
    hot = [d["name"] for d in ds if isinstance((d.get("smart") or {}).get("temp"), int) and d["smart"]["temp"] >= 60]
    add("Здоровье дисков (SMART)",
        ("сбой: " + ", ".join(bad)) if bad else ("перегрев: " + ", ".join(hot)) if hot else "все исправны",
        "bad" if bad else "warn" if hot else "ok")
    # защита данных SnapRAID
    sn = snapraid_status()
    if sn.get("configured"):
        for kind, ru in (("sync", "синхронизация"), ("scrub", "проверка")):
            e = sn.get("last_" + kind)
            if e:
                add("SnapRAID · " + ru, "%s (%s)" % ("успешно" if e["result"] == "ok" else "ошибка", (e.get("date") or "")[:10]),
                    "ok" if e["result"] == "ok" else "bad")
        if sn.get("blocked"):
            add("SnapRAID · защита", "sync остановлен (массовое удаление)", "warn")
    # упавшие службы
    r = _run(["systemctl", "list-units", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=8)
    failed = [l.split()[0] for l in (r.get("log") or "").splitlines() if l.strip()]
    add("Службы systemd", (", ".join(failed[:5])) if failed else "все работают", "bad" if failed else "ok")
    # перезагрузка/обновления
    if os.path.exists("/var/run/reboot-required"):
        add("Обновления", "нужна перезагрузка", "warn", "Обновления ядра/libc применятся после ребута")
    order = {"bad": 2, "warn": 1, "ok": 0}
    overall = max((order[c["lvl"]] for c in checks), default=0)
    return {"checks": checks, "overall": ["ok", "warn", "bad"][overall], "ts": int(time.time())}

def _fmt_b(n):
    n = float(n or 0)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return "%.0f %s" % (n, u)
        n /= 1024
    return "%.1f ПБ" % n

SPINDOWN_FILE = os.path.join(NAS_CONFIG, "spindown.json")

def _load_spindown():
    try:
        with open(SPINDOWN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def _hdparm_s_value(minutes):
    if minutes <= 0:
        return 0
    if minutes <= 20:               # 1..240 → шаг 5 с
        return max(1, min(240, int(round(minutes * 60 / 5))))
    return min(251, 240 + int(round(minutes / 30.0)))   # 241.. → шаг 30 мин

def disk_spindown(dev, minutes):
    """Таймаут ухода диска в сон при простое (hdparm -S). minutes=0 — выключить."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "недопустимое устройство"}
    try:
        minutes = max(0, min(240, int(minutes)))
    except (ValueError, TypeError):
        return {"ok": False, "log": "неверное значение"}
    r = _run(["hdparm", "-S", str(_hdparm_s_value(minutes)), dev], timeout=20)
    cfg = _load_spindown()
    cfg[dev] = minutes
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        with open(SPINDOWN_FILE, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass
    if not r["ok"]:
        return {"ok": False, "log": "диск/USB-мост не поддерживает управление сном: " + r["log"][-80:]}
    return {"ok": True, "log": ("диск засыпает через %d мин простоя" % minutes) if minutes else "сон отключён (диск всегда активен)"}

def apply_spindown_all():
    for dev, minutes in _load_spindown().items():
        if os.path.exists(dev):
            try:
                _run(["hdparm", "-S", str(_hdparm_s_value(int(minutes))), dev], timeout=20)
            except Exception:
                pass

def snapraid_status():
    """Последние sync/scrub из /var/log/snapraid.log (для защиты данных)."""
    log = _read("/var/log/snapraid.log")
    st = {"configured": os.path.isfile("/etc/snapraid.conf")}
    if not log:
        return st
    lines = log.splitlines()[-400:]
    date = None
    for l in lines:
        m = re.match(r"=+ (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) snapraid (sync|scrub)", l)
        if m:
            date = m.group(1)
        r = re.search(r"NASRESULT (sync|scrub) (ok|err)", l)
        if r:
            st["last_" + r.group(1)] = {"result": r.group(2), "date": date}
        if "ABORT: удалено файлов" in l:
            st["blocked"] = l.strip()[-140:]
    return st

# --------------------------------------------------------------------------- #
#  Процессы и службы systemd
# --------------------------------------------------------------------------- #
def _pid_cputime(pid):
    st = _read(f"/proc/{pid}/stat")
    if not st:
        return None
    rp = st.rfind(")")
    f = st[rp + 2:].split()
    try:
        return int(f[11]) + int(f[12])          # utime + stime
    except (IndexError, ValueError):
        return None

def processes(sort="cpu", limit=45):
    def total_j():
        return sum(map(int, _read("/proc/stat").splitlines()[0].split()[1:]))
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    t1 = total_j(); s1 = {p: _pid_cputime(p) for p in pids}
    time.sleep(0.25)
    t2 = total_j(); dt = (t2 - t1) or 1
    ncpu = os.cpu_count() or 4
    page = os.sysconf("SC_PAGE_SIZE")
    total_mem = mem_info()["total"] or 1
    rows = []
    for p in pids:
        c2 = _pid_cputime(p); c1 = s1.get(p)
        if c2 is None or c1 is None:
            continue
        cpu = round(100 * ncpu * (c2 - c1) / dt, 1)
        try:
            rss = int(_read(f"/proc/{p}/statm").split()[1]) * page
        except (IndexError, ValueError):
            rss = 0
        name = _read(f"/proc/{p}/comm") or "?"
        cmd = _read(f"/proc/{p}/cmdline").replace("\x00", " ").strip() or name
        try:
            user = pwd.getpwuid(os.stat(f"/proc/{p}").st_uid).pw_name
        except (KeyError, OSError):
            user = "?"
        try:
            ppid = int(_read(f"/proc/{p}/stat").rpartition(")")[2].split()[1])
        except (IndexError, ValueError):
            ppid = 0
        rows.append({"pid": int(p), "ppid": ppid, "name": name, "cmd": cmd[:200], "user": user,
                     "cpu": cpu, "mem": round(100 * rss / total_mem, 1), "rss": rss})
    rows.sort(key=lambda r: r["mem" if sort == "mem" else "cpu"], reverse=True)
    return {"processes": rows[:limit], "ncpu": ncpu, "count": len(rows)}

def kill_process(pid, sig=15):
    try:
        os.kill(int(pid), int(sig))
        return {"ok": True, "log": f"сигнал {sig} -> {pid}"}
    except (ProcessLookupError, PermissionError, ValueError) as e:
        return {"ok": False, "log": str(e)}

CREATED_UNITS = os.path.join(NAS_CONFIG, "created-units.json")

def load_created_units():
    try:
        with open(CREATED_UNITS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []

def _save_created_units(lst):
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        with open(CREATED_UNITS, "w") as f:
            json.dump(lst, f)
    except OSError:
        pass

def _track_unit(name, add=True):
    lst = load_created_units()
    if add and name not in lst:
        lst.append(name); _save_created_units(lst)
    elif not add and name in lst:
        lst.remove(name); _save_created_units(lst)

def systemd_units(kind="service"):
    r = _run(["systemctl", "list-units", f"--type={kind}", "--all", "--no-legend",
              "--plain", "--no-pager"], timeout=10)
    out = []
    for line in (r.get("log") or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith("." + kind):
            continue
        out.append({"unit": parts[0], "load": parts[1], "active": parts[2],
                    "sub": parts[3], "desc": parts[4] if len(parts) > 4 else ""})
    return {"units": out, "created": load_created_units()}

def systemd_action(unit, action):
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return {"ok": False, "log": "недопустимое действие"}
    if not re.match(r"^[\w@.:-]+$", unit or ""):
        return {"ok": False, "log": "недопустимое имя юнита"}
    return _run(["systemctl", action, unit], timeout=30)

def systemd_journal(unit, lines=200):
    if not re.match(r"^[\w@.:-]+$", unit or ""):
        return {"ok": False, "log": "недопустимый юнит"}
    try:
        n = max(10, min(2000, int(lines)))
    except (ValueError, TypeError):
        n = 200
    r = _run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"], timeout=15)
    return {"ok": r.get("ok", True), "unit": unit, "log": r.get("log", "")}

def renice(pid, nice):
    try:
        n = int(nice); pid = int(pid)
    except (ValueError, TypeError):
        return {"ok": False, "log": "неверные аргументы"}
    if n < -20 or n > 19:
        return {"ok": False, "log": "приоритет вне диапазона -20..19"}
    r = _run(["renice", "-n", str(n), "-p", str(pid)], timeout=10)
    if r["ok"] and not r["log"]:
        r["log"] = f"nice={n} для PID {pid}"
    return r

UNIT_DIR = "/etc/systemd/system"
_UNIT_RE = re.compile(r"^[\w@.\-]+\.(service|timer|socket|mount|path|target)$")

def unit_read(name):
    if not _UNIT_RE.match(name or ""):
        return {"ok": False, "log": "имя вида name.service"}
    etc = os.path.join(UNIT_DIR, name)
    if os.path.isfile(etc):
        try:
            with open(etc) as f:
                return {"ok": True, "name": name, "path": etc, "editable": True, "base": False, "content": f.read()}
        except OSError as e:
            return {"ok": False, "log": str(e)}
    r = _run(["systemctl", "cat", name], timeout=10)
    return {"ok": True, "name": name, "path": "", "editable": True, "base": True,
            "content": r.get("log") or "# базовый юнит; сохранение создаст переопределение в " + UNIT_DIR}

def unit_write(name, content, create=False):
    if not _UNIT_RE.match(name or ""):
        return {"ok": False, "log": "имя вида name.service"}
    path = os.path.join(UNIT_DIR, name)
    if create and os.path.exists(path):
        return {"ok": False, "log": "юнит уже существует"}
    try:
        os.makedirs(UNIT_DIR, exist_ok=True)
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            f.write(content if content is not None else "")
    except OSError as e:
        return {"ok": False, "log": str(e)}
    r = _run(["systemctl", "daemon-reload"], timeout=20)
    if create:
        _track_unit(name, True)
    return {"ok": True, "name": name, "path": path, "log": r.get("log", "")}

def unit_delete(name):
    if not _UNIT_RE.match(name or ""):
        return {"ok": False, "log": "имя вида name.service"}
    path = os.path.realpath(os.path.join(UNIT_DIR, name))
    if not path.startswith(UNIT_DIR + os.sep):
        return {"ok": False, "log": "вне каталога юнитов"}
    if not os.path.isfile(path):
        return {"ok": False, "log": "это базовый системный юнит — его нельзя удалить здесь"}
    _run(["systemctl", "disable", "--now", name], timeout=30)
    try:
        os.remove(path)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _run(["systemctl", "daemon-reload"], timeout=20)
    _track_unit(name, False)
    return {"ok": True}

def power(action):
    if action not in ("reboot", "poweroff"):
        return {"ok": False, "log": "неизвестное действие"}
    try:
        subprocess.Popen(["systemctl", action])
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "log": str(e)}

SNIPPETS_FILE = os.path.join(NAS_CONFIG, "snippets.json")
def load_snippets():
    try:
        with open(SNIPPETS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
def save_snippets(d):
    os.makedirs(NAS_CONFIG, exist_ok=True)
    with open(SNIPPETS_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

FAV_FILE = os.path.join(NAS_CONFIG, "fm-favorites.json")
def load_favs():
    try:
        with open(FAV_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
def save_favs(d):
    os.makedirs(NAS_CONFIG, exist_ok=True)
    with open(FAV_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

SETTINGS_FILE = os.path.join(NAS_CONFIG, "desktop.json")
def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
def save_settings(d):
    os.makedirs(NAS_CONFIG, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

WINPOS_FILE = os.path.join(NAS_CONFIG, "winpos.json")
def load_winpos():
    try:
        with open(WINPOS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
def save_winpos(d):
    os.makedirs(NAS_CONFIG, exist_ok=True)
    with open(WINPOS_FILE, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def stats():
    iface = default_iface()
    return {
        "host": socket.gethostname(),
        "ip": lan_ip(),
        "cpu": cpu_percent(),
        "temp": temp_c(),
        "throttled": throttled(),
        "mem": mem_info(),
        "disk_pool": disk_info(STORAGE) or disk_info("/"),
        "disk_root": disk_info("/"),
        "net": net_rate(iface),
        "iface": iface,
        "uptime": uptime_s(),
        "load": list(os.getloadavg()),
        "ts": int(time.time()),
    }

# --------------------------------------------------------------------------- #
#  Монитор-уведомления (temp / throttle / пул → Pushover)
# --------------------------------------------------------------------------- #
MONITOR_FILE = os.path.join(NAS_CONFIG, "monitor.json")
NOTIFY_CONF = "/etc/nas-wizard/notify.conf"
_MON_LAST = {}
_MON_BOOT_SENT = False
_MON_SMART_LAST = 0
_MON_DEVS = None      # набор томов с ФС на прошлом тике (для disk_add/disk_remove)
_MON_IP = None        # последний известный локальный IP (для ip_changed)
_MON_IFACE = None     # активный интерфейс по умолчанию (для link_changed)
_MON_HEAT = 0         # счётчик подряд «горячих» тиков (для sustained_heat)
_MON_HOURLY = {}      # ключ → время последней «часовой» проверки (updates, docker_space, …)
_MON_WEEKLY = 0       # время последнего еженедельного отчёта
_MON_DISKSTAT = None  # предыдущий снимок /proc/diskstats (для slow_disk)
_MON_HOG = {}         # pid → сколько тиков подряд процесс жрёт ресурсы
_KNOWN_IPS_FILE = os.path.join(NAS_CONFIG, "known-ips.json")

# Каталог событий: on (по умолчанию), priority (Pushover: -2 тихо … 2 экстренно), threshold.
# priority-подсказка: 2 = риск потери данных (требует подтверждения), 1 = важно/срочно,
# 0 = обычное, -1 = к сведению (без звука), -2 = только бейдж.
def _def_monitor():
    return {"enabled": False, "cooldown": 1800, "events": {
        # --- диски: подключение/отключение/режим ---
        "disk_add":    {"on": True,  "priority": 0},
        "disk_remove": {"on": True,  "priority": 1},
        "readonly":    {"on": True,  "priority": 2},
        "fserror":     {"on": True,  "priority": 1},
        # --- здоровье дисков (SMART, раз в 10 мин) ---
        "smart":       {"on": True,  "priority": 2},
        "smart_wear":  {"on": True,  "priority": 1, "threshold": 1},
        "disktemp":    {"on": True,  "priority": 1, "threshold": 60},
        # --- место ---
        "pool":        {"on": True,  "priority": 0, "threshold": 90},
        "diskfull":    {"on": True,  "priority": 0, "threshold": 90},
        # --- Pi: питание/температура/ресурсы ---
        "temp":        {"on": True,  "priority": 1, "threshold": 75},
        "throttle":    {"on": True,  "priority": 1},
        "mem":         {"on": False, "priority": 0, "threshold": 92},
        "swap":        {"on": False, "priority": 0, "threshold": 60},
        "load":        {"on": False, "priority": 0, "threshold": 8},
        # --- службы и контейнеры ---
        "svcfail":     {"on": True,  "priority": 1},
        "container":   {"on": True,  "priority": 0},
        "container_loop": {"on": True, "priority": 1},
        "docker_space":{"on": False, "priority": 0, "threshold": 20},
        # --- доступ (вход в панель / SSH) ---
        "panel_new":   {"on": True,  "priority": 1},
        "panel_fail":  {"on": True,  "priority": 1, "threshold": 5},
        "ssh_login":   {"on": False, "priority": 0},
        # --- сеть ---
        "ip_changed":  {"on": True,  "priority": 0},
        "link_changed":{"on": True,  "priority": 0},
        "vpn_offline": {"on": True,  "priority": 1},
        # --- защита данных (SnapRAID / mergerfs / бэкап) ---
        "snap_ok":     {"on": False, "priority": -1},
        "snap_err":    {"on": True,  "priority": 1},
        "scrub_err":   {"on": True,  "priority": 2},
        "delete_block":{"on": True,  "priority": 1},
        "backup":      {"on": False, "priority": 0},
        "mergerfs":    {"on": True,  "priority": 1},
        # --- обслуживание ---
        "reboot_req":  {"on": True,  "priority": -1},
        "root_full":   {"on": True,  "priority": 1, "threshold": 90},
        "sd_degrade":  {"on": True,  "priority": 1},
        "sustained_heat": {"on": False, "priority": 1, "threshold": 10},
        "fan_stall":   {"on": True,  "priority": 1},
        "cron_failed": {"on": True,  "priority": 0},
        "time_drift":  {"on": True,  "priority": 0},
        "updates":     {"on": False, "priority": -1},
        "sec_updates": {"on": False, "priority": -1},
        "weekly":      {"on": False, "priority": -1},
        # --- поведенческие ---
        "traffic":     {"on": False, "priority": 0, "threshold": 50},
        "slow_disk":   {"on": False, "priority": 0, "threshold": 100},
        "proc_hog":    {"on": False, "priority": 0, "threshold": 80},
        "inodes":      {"on": True,  "priority": 1, "threshold": 90},
        "boot":        {"on": False, "priority": -1},
    }}

def load_monitor():
    d = _def_monitor()
    try:
        with open(MONITOR_FILE) as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "events" and isinstance(v, dict):
                for ek, ev in v.items():
                    if isinstance(ev, dict):
                        d["events"].setdefault(ek, {}).update(ev)
            else:
                d[k] = v
    except (OSError, ValueError):
        pass
    return d

def save_monitor(d):
    cur = load_monitor()
    if "enabled" in d:
        cur["enabled"] = bool(d["enabled"])
    if "cooldown" in d:
        try:
            cur["cooldown"] = max(60, int(d["cooldown"]))
        except (ValueError, TypeError):
            pass
    ev = d.get("events")
    if isinstance(ev, dict):
        for ek, evv in ev.items():
            if ek in cur["events"] and isinstance(evv, dict):
                cur["events"][ek].update(evv)
    os.makedirs(NAS_CONFIG, exist_ok=True)
    with open(MONITOR_FILE, "w") as f:
        json.dump(cur, f)
    return cur

def load_notify():
    user = token = ""
    try:
        for line in _read(NOTIFY_CONF).splitlines():
            line = line.strip()
            if line.startswith("PUSHOVER_USER"):
                user = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("PUSHOVER_TOKEN"):
                token = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return {"user": user, "token": token, "configured": bool(user and token)}

def save_notify(user, token):
    try:
        lines = _read(NOTIFY_CONF).splitlines()
    except OSError:
        lines = []
    def setkv(lines, key, val):
        out, found = [], False
        for l in lines:
            if l.strip().startswith(key + "="):
                out.append('%s="%s"' % (key, val)); found = True
            else:
                out.append(l)
        if not found:
            out.append('%s="%s"' % (key, val))
        return out
    lines = setkv(lines, "PUSHOVER_USER", user or "")
    lines = setkv(lines, "PUSHOVER_TOKEN", token or "")
    try:
        os.makedirs(os.path.dirname(NOTIFY_CONF), exist_ok=True)
        with open(NOTIFY_CONF, "w") as f:
            f.write("\n".join(l for l in lines if l is not None) + "\n")
        os.chmod(NOTIFY_CONF, 0o600)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def push_notify(title, msg, priority=0):
    try:
        priority = max(-2, min(2, int(priority)))
    except (ValueError, TypeError):
        priority = 0
    n = load_notify()
    if n["user"] and n["token"]:
        try:
            body = {"token": n["token"], "user": n["user"], "title": title,
                    "message": msg, "priority": priority}
            if priority == 2:            # экстренный: Pushover требует retry/expire + подтверждение
                body["retry"] = 60
                body["expire"] = 3600
            data = urlencode(body).encode()
            urllib.request.urlopen("https://api.pushover.net/1/messages.json", data=data, timeout=15)
            return True
        except OSError:
            pass
    if os.path.exists("/usr/local/bin/nas-notify.sh"):
        return _run(["/usr/local/bin/nas-notify.sh", title, msg, str(priority)], timeout=15)["ok"]
    return False

def _phys_devs():
    try:
        return ["/dev/" + d for d in os.listdir("/dev") if re.match(r"^(sd[a-z]|nvme\d+n\d+)$", d)]
    except OSError:
        return []

def _smart_scan():
    """Один проход smartctl по всем физическим дискам → dict со здоровьем/износом/темп."""
    res = {}
    for dev in _phys_devs():
        j = _smartctl_json(["-H", "-A"], dev, timeout=15)
        if not _smart_has_data(j):
            continue
        passed = (j.get("smart_status") or {}).get("passed")
        realloc = pending = temp = None
        for a in ((j.get("ata_smart_attributes") or {}).get("table") or []):
            nm, raw = a.get("name", ""), (a.get("raw") or {}).get("value")
            if nm == "Reallocated_Sector_Ct": realloc = raw
            elif nm == "Current_Pending_Sector": pending = raw
            elif nm in ("Temperature_Celsius", "Airflow_Temperature_Cel") and temp is None: temp = raw
        if temp is None:
            temp = (j.get("temperature") or {}).get("current")
        if isinstance(temp, int) and temp > 200:      # некоторые прошивки кладут в raw мусор
            temp = temp & 0xff
        res[dev] = {"passed": passed, "realloc": realloc, "pending": pending, "temp": temp}
    return res

def _block_volumes():
    """Устройства с файловой системой: путь → метка (для событий подключён/отключён)."""
    vols = {}
    def walk(node):
        fs = node.get("fstype")
        if fs and fs not in ("swap", "linux_raid_member", "LVM2_member"):
            vols[node.get("path")] = node.get("label") or node.get("name")
        for c in node.get("children", []) or []:
            walk(c)
    for d in _lsblk():
        if (d.get("name") or "").startswith(("zram", "loop")):
            continue
        walk(d)
    return vols

def _readonly_mounts():
    out = []
    for line in _read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        mp, fstype, opts = p[1], p[2], p[3]
        if fstype in ("iso9660", "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup2"):
            continue
        if not (mp.startswith("/mnt/") or mp.startswith("/media/")):
            continue
        if re.search(r"(^|,)ro(,|$)", opts):
            out.append(mp)
    return out

def _kernel_fs_errors():
    r = _run(["journalctl", "-k", "--since", "-90 seconds", "--no-pager", "-q"], timeout=8)
    pat = re.compile(r"EXT4-fs error|XFS.*(error|corruption)|Buffer I/O error|"
                     r"I/O error|remounting filesystem read-only|critical medium error", re.I)
    return [l.strip()[-140:] for l in (r.get("log") or "").splitlines() if pat.search(l)][:4]

def _data_mounts_usage():
    out = []
    seen = set()
    for mp in sorted(glob.glob("/mnt/disk*") + glob.glob("/mnt/parity*") + ["/mnt/storage"]):
        if mp in seen or not os.path.ismount(mp):
            continue
        seen.add(mp)
        di = disk_info(mp)
        if di:
            out.append((mp, di["pct"]))
    return out

def _bad_containers():
    r = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], timeout=12)
    bad = []
    for l in (r.get("log") or "").splitlines():
        f = l.split("\t")
        if len(f) < 3:
            continue
        name, state, status = f[0], f[1], f[2]
        if state == "exited" and "Exited (0)" not in status:
            bad.append("%s (упал)" % name)
        elif "unhealthy" in status.lower():
            bad.append("%s (unhealthy)" % name)
    return bad

# --------------------------------------------------------------------------- #
#  Единая отправка события уведомления (проверяет вкл./cooldown/приоритет).
#  Зовётся и из monitor_tick, и из хука входа в панель.
# --------------------------------------------------------------------------- #
def mon_notify(dedup_key, title, msg, event=None):
    cfg = load_monitor()
    if not cfg.get("enabled"):
        return False
    ev = cfg.get("events", {}).get(event or dedup_key.split(":")[0], {})
    if not ev.get("on"):
        return False
    now = time.time()
    if now - _MON_LAST.get(dedup_key, 0) < cfg.get("cooldown", 1800):
        return False
    if push_notify(title, msg, ev.get("priority", 0)):
        _MON_LAST[dedup_key] = now
        return True
    return False

def _known_ips():
    try:
        with open(_KNOWN_IPS_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()

def _remember_ip(ip):
    s = _known_ips(); s.add(ip)
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        with open(_KNOWN_IPS_FILE, "w") as f:
            json.dump(sorted(s), f)
    except OSError:
        pass

def _jrnl(since, *match):
    """Строки журнала за интервал, содержащие любой из паттернов (регистронезависимо)."""
    r = _run(["journalctl", "--since", since, "--no-pager", "-q"] + list(match), timeout=8)
    return (r.get("log") or "").splitlines()

def _ssh_logins():
    out = []
    for l in _jrnl("-90 seconds", "_COMM=sshd"):
        m = re.search(r"Accepted \S+ for (\S+) from ([\d.a-f:]+)", l)
        if m:
            out.append((m.group(1), m.group(2)))
    return out

def _tailscale_offline():
    if not shutil.which("tailscale"):
        return None
    r = _run(["tailscale", "status", "--json"], timeout=8)
    try:
        j = json.loads(r.get("log") or "{}")
        return (j.get("BackendState") not in ("Running", None))
    except ValueError:
        return None

def _snapraid_events():
    """Разобрать хвост /var/log/snapraid.log (маркеры NASRESULT от обёртки)."""
    log = _read("/var/log/snapraid.log")
    if not log:
        return None
    tail = log.splitlines()[-80:]
    ev = {}
    for l in tail:
        if "ABORT: удалено файлов" in l:
            ev["delete_blocked"] = l.strip()[-160:]
        mm = re.search(r"(\d+) errors", l)          # scrub: счётчик ошибок > 0
        if (mm and int(mm.group(1)) > 0) or "silent error" in l.lower():
            ev["scrub_err"] = l.strip()[-160:]
    for l in reversed(tail):                          # последний итог sync
        if "NASRESULT sync ok" in l:
            ev.setdefault("sync_ok", l.replace("NASRESULT ", "").strip()); break
        if "NASRESULT sync err" in l:
            ev.setdefault("sync_err", l.replace("NASRESULT ", "").strip()); break
    return ev or None

def _mergerfs_missing():
    """Диски данных из fstab, которые сейчас НЕ смонтированы (ветка выпала из пула)."""
    fstab = _read("/etc/fstab")
    want = set(re.findall(r"\s(/mnt/disk\d+)\s", fstab))
    return sorted(mp for mp in want if not os.path.ismount(mp))

def _restart_loops():
    r = _run(["docker", "ps", "--filter", "status=restarting", "--format", "{{.Names}}"], timeout=10)
    return [l.strip() for l in (r.get("log") or "").splitlines() if l.strip()]

def _docker_reclaimable_gb():
    r = _run(["docker", "system", "df", "--format", "{{.Reclaimable}}"], timeout=12)
    total = 0.0
    for l in (r.get("log") or "").splitlines():
        m = re.match(r"([\d.]+)\s*([KMGT]?)B", l.strip())
        if m:
            v = float(m.group(1)); u = m.group(2)
            total += v * {"": 1e-9, "K": 1e-6, "M": 1e-3, "G": 1, "T": 1e3}.get(u, 0)
    return round(total, 1)

def _apt_upgradable():
    r = _run(["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"], timeout=40)
    return sum(1 for l in (r.get("log") or "").splitlines() if l.startswith("Inst "))

def _sec_updates_recent():
    log = _read("/var/log/unattended-upgrades/unattended-upgrades.log")
    if not log:
        return []
    out = []
    for l in log.splitlines()[-40:]:
        if "Packages that will be upgraded:" in l or re.search(r"Installing .* to fix", l):
            out.append(l.strip()[-140:])
    return out[-1:]

def _sd_errors():
    return [l.strip()[-140:] for l in _jrnl("-90 seconds", "-k")
            if re.search(r"mmc\d+:.*(error|timeout)|mmcblk\d+:.*I/O", l, re.I)][:3]

def _fan_rpm():
    for p in glob.glob("/sys/class/hwmon/hwmon*/fan1_input"):
        v = _read(p)
        if v.isdigit():
            return int(v)
    return None

def _cron_failures():
    try:
        st = cron_stats()
    except Exception:
        return []
    return [j.get("id") or j.get("name") for j in (st.get("failed") or [])][:8] if isinstance(st, dict) else []

def _ntp_unsynced():
    r = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=6)
    return (r.get("log") or "").strip() == "no"

def _diskstat_await():
    """await (мс/операцию) по /proc/diskstats с прошлого снимка → dict dev→await."""
    global _MON_DISKSTAT
    cur = {}
    for l in _read("/proc/diskstats").splitlines():
        f = l.split()
        if len(f) < 14:
            continue
        dev = f[2]
        if not re.match(r"^(sd[a-z]|nvme\d+n\d+|mmcblk\d+)$", dev):
            continue
        # поля: reads(3) ... read_ticks(6), writes(7) ... write_ticks(10)
        ios = int(f[3]) + int(f[7]); ticks = int(f[6]) + int(f[10])
        cur[dev] = (ios, ticks)
    out = {}
    if _MON_DISKSTAT:
        for dev, (ios, ticks) in cur.items():
            p = _MON_DISKSTAT.get(dev)
            if p:
                dios, dticks = ios - p[0], ticks - p[1]
                if dios > 20:                       # только под заметной нагрузкой
                    out[dev] = dticks / dios
    _MON_DISKSTAT = cur
    return out

def _proc_hog(cpu_thr):
    """Процесс, устойчиво жрущий CPU (по нескольким тикам)."""
    global _MON_HOG
    try:
        procs = processes("cpu", 6).get("processes", [])
    except Exception:
        return None
    hot = {p["pid"]: p for p in procs if (p.get("cpu") or 0) >= cpu_thr}
    fired = None
    new = {}
    for pid, p in hot.items():
        n = _MON_HOG.get(pid, 0) + 1
        new[pid] = n
        if n == 3:                                  # ~3 тика подряд
            fired = "%s (pid %s) — %s%% CPU" % (p.get("name"), pid, round(p.get("cpu") or 0))
    _MON_HOG = new
    return fired

def _inodes_full(thr):
    out = []
    for mp in ["/"] + [m for m, _ in _data_mounts_usage()]:
        try:
            s = os.statvfs(mp)
        except OSError:
            continue
        if s.f_files:
            pct = round(100 * (s.f_files - s.f_favail) / s.f_files)
            if pct >= thr:
                out.append("%s — inode %s%%" % (mp, pct))
    return out

def monitor_tick():
    global _MON_BOOT_SENT, _MON_SMART_LAST, _MON_DEVS, _MON_IP, _MON_IFACE, _MON_HEAT, _MON_WEEKLY
    cfg = load_monitor()
    if not cfg.get("enabled"):
        return
    ev = cfg.get("events", {})
    on  = lambda k: ev.get(k, {}).get("on")
    pri = lambda k: ev.get(k, {}).get("priority", 0)
    thr = lambda k, dv: ev.get(k, {}).get("threshold", dv)
    now = time.time()
    cd = cfg.get("cooldown", 1800)
    def fire(key, title, msg, priority=0):
        if now - _MON_LAST.get(key, 0) >= cd:
            if push_notify(title, msg, priority):
                _MON_LAST[key] = now
    s = stats()
    host = s.get("host", "NAS")

    # --- запуск системы ---
    if not _MON_BOOT_SENT:
        if on("boot"):
            push_notify("NAS: система запущена", "%s снова в сети" % host, pri("boot"))
        _MON_BOOT_SENT = True

    # --- подключение / отключение дисков (по изменению набора томов) ---
    vols = _block_volumes()
    if _MON_DEVS is not None:
        added   = [vols[d] for d in vols if d not in _MON_DEVS]
        removed = [_MON_DEVS[d] for d in _MON_DEVS if d not in vols]
        if added and on("disk_add"):
            push_notify("NAS: диск подключён", "Появился: " + ", ".join(map(str, added)), pri("disk_add"))
        if removed and on("disk_remove"):
            push_notify("NAS: диск отключён", "Пропал: " + ", ".join(map(str, removed)), pri("disk_remove"))
    _MON_DEVS = vols

    # --- файловая система в режиме «только чтение» (риск данных) ---
    if on("readonly"):
        ro = _readonly_mounts()
        if ro:
            fire("readonly", "NAS: диск только для чтения",
                 "Смонтировано ro (сбой ФС?): " + ", ".join(ro), pri("readonly"))

    # --- ошибки ФС/ввода-вывода в журнале ядра ---
    if on("fserror"):
        errs = _kernel_fs_errors()
        if errs:
            fire("fserror", "NAS: ошибки диска в логе ядра", "\n".join(errs), pri("fserror"))

    # --- Pi: температура / троттлинг / память / swap / нагрузка ---
    t = s.get("temp")
    if on("temp") and t and t >= thr("temp", 75):
        fire("temp", "NAS: перегрев", "Температура %s°C (порог %s°C)" % (t, thr("temp", 75)), pri("temp"))
    tr = s.get("throttled") or {}
    if on("throttle") and not tr.get("ok", True):
        fire("throttle", "NAS: троттлинг", "Просадка питания/троттлинг: %s" % tr.get("raw", ""), pri("throttle"))
    m = (s.get("mem") or {}).get("pct", 0)
    if on("mem") and m >= thr("mem", 92):
        fire("mem", "NAS: мало памяти", "RAM занята на %s%%" % m, pri("mem"))
    mem = mem_info()
    if on("swap") and mem.get("swap_total"):
        swp = round(100 * (mem["swap_total"] - mem["swap_free"]) / mem["swap_total"])
        if swp >= thr("swap", 60):
            fire("swap", "NAS: активный swap", "Подкачка занята на %s%% — не хватает ОЗУ" % swp, pri("swap"))
    load1 = (s.get("load") or [0])[0]
    if on("load") and load1 >= thr("load", 8):
        fire("load", "NAS: высокая нагрузка", "Load average 1м = %.2f" % load1, pri("load"))

    # --- место: пул + отдельные диски ---
    pool = s.get("disk_pool") or {}
    if on("pool") and pool.get("pct", 0) >= thr("pool", 90):
        fire("pool", "NAS: хранилище заполнено", "%s занят на %s%%" % (pool.get("path", "пул"), pool.get("pct")), pri("pool"))
    if on("diskfull"):
        for mp, pct in _data_mounts_usage():
            if mp == "/mnt/storage":
                continue
            if pct >= thr("diskfull", 90):
                fire("diskfull:" + mp, "NAS: диск заполняется", "%s занят на %s%%" % (mp, pct), pri("diskfull"))

    # --- службы и контейнеры ---
    if on("svcfail"):
        r = _run(["systemctl", "list-units", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
        failed = [l.split()[0] for l in (r.get("log") or "").splitlines() if l.strip()]
        if failed:
            fire("svcfail", "NAS: сбой службы", "Упали: " + ", ".join(failed[:8]), pri("svcfail"))
    if on("container") and shutil.which("docker"):
        bad = _bad_containers()
        if bad:
            fire("container", "NAS: проблема с контейнером", "; ".join(bad[:8]), pri("container"))

    # --- требуется перезагрузка (обновления ядра/libc) ---
    if on("reboot_req") and os.path.exists("/var/run/reboot-required"):
        fire("reboot_req", "NAS: нужна перезагрузка", "Обновления применятся после ребута", pri("reboot_req"))

    # --- SMART: раз в 10 минут единым проходом (здоровье / износ / температура) ---
    if (on("smart") or on("smart_wear") or on("disktemp")) and now - _MON_SMART_LAST >= 600:
        _MON_SMART_LAST = now
        scan = _smart_scan()
        for dev, d in scan.items():
            if on("smart") and d.get("passed") is False:
                fire("smart:" + dev, "NAS: диск не прошёл SMART", "%s — SMART FAIL, замените диск" % dev, pri("smart"))
            if on("smart_wear"):
                bad = []
                if isinstance(d.get("realloc"), int) and d["realloc"] >= thr("smart_wear", 1):
                    bad.append("переназначено секторов: %d" % d["realloc"])
                if isinstance(d.get("pending"), int) and d["pending"] >= thr("smart_wear", 1):
                    bad.append("ожидают: %d" % d["pending"])
                if bad:
                    fire("wear:" + dev, "NAS: износ диска", "%s — %s" % (dev, ", ".join(bad)), pri("smart_wear"))
            if on("disktemp") and isinstance(d.get("temp"), int) and d["temp"] >= thr("disktemp", 60):
                fire("dtemp:" + dev, "NAS: диск перегрет", "%s — %s°C" % (dev, d["temp"]), pri("disktemp"))

    # --- контейнеры: restart-loop + распухший docker ---
    if shutil.which("docker"):
        if on("container_loop"):
            loops = _restart_loops()
            if loops:
                fire("cloop", "NAS: контейнер не поднимается", "Перезапускается по кругу: " + ", ".join(loops[:8]), pri("container_loop"))
        if on("docker_space") and _hourly("docker_space"):
            gb = _docker_reclaimable_gb()
            if gb >= thr("docker_space", 20):
                fire("dspace", "NAS: docker распух", "Можно освободить ~%s ГБ (prune)" % gb, pri("docker_space"))

    # --- SSH-вход ---
    if on("ssh_login"):
        for user, ip in _ssh_logins():
            fire("ssh:" + ip, "NAS: вход по SSH", "%s с %s" % (user, ip), pri("ssh_login"))

    # --- сеть: смена IP / линка / VPN ---
    ip = s.get("ip")
    if _MON_IP is not None and ip and ip != _MON_IP and on("ip_changed"):
        push_notify("NAS: сменился IP", "Было %s → стало %s" % (_MON_IP, ip), pri("ip_changed"))
    _MON_IP = ip or _MON_IP
    iface = s.get("iface")
    if _MON_IFACE is not None and iface != _MON_IFACE and on("link_changed"):
        push_notify("NAS: смена сети", "Активный интерфейс: %s → %s" % (_MON_IFACE or "нет", iface or "нет"), pri("link_changed"))
    _MON_IFACE = iface
    if on("vpn_offline") and _tailscale_offline():
        fire("vpn", "NAS: VPN offline", "Tailscale не в сети — удалённый доступ недоступен", pri("vpn_offline"))

    # --- защита данных: SnapRAID + mergerfs + бэкап ---
    sn = _snapraid_events() or {}
    if on("snap_ok") and sn.get("sync_ok"):
        fire("snapok", "NAS: SnapRAID sync ок", sn["sync_ok"], pri("snap_ok"))
    if on("snap_err") and sn.get("sync_err"):
        fire("snaperr", "NAS: SnapRAID sync ошибка", sn["sync_err"], pri("snap_err"))
    if on("scrub_err") and sn.get("scrub_err"):
        fire("scruberr", "NAS: SnapRAID нашёл повреждение", sn["scrub_err"], pri("scrub_err"))
    if on("delete_block") and sn.get("delete_blocked"):
        fire("delblk", "NAS: sync остановлен защитой", sn["delete_blocked"], pri("delete_block"))
    if on("mergerfs"):
        miss = _mergerfs_missing()
        if miss:
            fire("mfs", "NAS: диск выпал из пула", "Не смонтированы: " + ", ".join(miss), pri("mergerfs"))
    if on("backup"):
        blog = _read("/var/log/nas-backup.log")
        if blog:
            last = blog.splitlines()[-1]
            if re.search(r"\b(FAIL|ошибка|error)\b", last, re.I):
                fire("bkp", "NAS: бэкап не удался", last[-160:], pri("backup"))
            elif re.search(r"\b(OK|успешно|done)\b", last, re.I):
                fire("bkp", "NAS: бэкап выполнен", last[-160:], pri("backup"))

    # --- обслуживание ---
    root = disk_info("/") or {}
    if on("root_full") and root.get("pct", 0) >= thr("root_full", 90):
        fire("rootfull", "NAS: мало места на системной карте", "Раздел / занят на %s%%" % root.get("pct"), pri("root_full"))
    if on("sd_degrade"):
        sd = _sd_errors()
        if sd:
            fire("sderr", "NAS: сбои SD-карты", "\n".join(sd), pri("sd_degrade"))
    if on("sustained_heat"):
        hot = (t and t >= thr("temp", 75)) or not tr.get("ok", True)
        _MON_HEAT = _MON_HEAT + 1 if hot else 0
        if _MON_HEAT >= thr("sustained_heat", 10):
            fire("heat", "NAS: держится перегрев/троттлинг", "Уже %d мин подряд — проверьте охлаждение/питание" % _MON_HEAT, pri("sustained_heat"))
    if on("fan_stall"):
        rpm = _fan_rpm()
        if rpm == 0 and t and t >= thr("temp", 75):
            fire("fan", "NAS: вентилятор стоит", "0 об/мин при %s°C — проверьте кулер" % t, pri("fan_stall"))
    if on("cron_failed"):
        cf = _cron_failures()
        if cf:
            fire("cron", "NAS: задача по расписанию упала", "Ошибка: " + ", ".join(map(str, cf)), pri("cron_failed"))
    if on("time_drift") and _ntp_unsynced():
        fire("ntp", "NAS: время не синхронизировано", "Часы могут уплыть — проверьте chrony/timesyncd", pri("time_drift"))
    if on("updates") and _hourly("updates"):
        n = _apt_upgradable()
        if n > 0:
            fire("upd", "NAS: доступны обновления", "Можно обновить пакетов: %d" % n, pri("updates"))
    if on("sec_updates") and _hourly("sec_updates"):
        su = _sec_updates_recent()
        if su:
            fire("secupd", "NAS: накатились security-обновления", su[-1], pri("sec_updates"))

    # --- поведенческие ---
    tx = (s.get("net") or {}).get("tx", 0)
    if on("traffic") and tx >= thr("traffic", 50) * 1024 * 1024:
        fire("traffic", "NAS: большой исходящий трафик", "Отдача %s/с — проверьте, что это ожидаемо" % fmt_bytes(tx), pri("traffic"))
    if on("slow_disk"):
        for dev, aw in _diskstat_await().items():
            if aw >= thr("slow_disk", 100):
                fire("slow:" + dev, "NAS: диск отвечает медленно", "%s — задержка %d мс/операцию" % (dev, round(aw)), pri("slow_disk"))
    if on("proc_hog"):
        hog = _proc_hog(thr("proc_hog", 80))
        if hog:
            fire("hog", "NAS: процесс грузит систему", hog, pri("proc_hog"))
    if on("inodes"):
        ino = _inodes_full(thr("inodes", 90))
        if ino:
            fire("inodes", "NAS: заканчиваются inode", "; ".join(ino), pri("inodes"))

    # --- еженедельный отчёт «жив» ---
    if on("weekly") and now - _MON_WEEKLY >= 7 * 86400:
        _MON_WEEKLY = now
        push_notify("NAS: недельный отчёт",
                    "%s · аптайм %s · CPU %s%% · темп %s°C · пул %s%%"
                    % (host, fmt_uptime(s.get("uptime", 0)), s.get("cpu"),
                       s.get("temp") or "—", (pool.get("pct") if pool else "—")),
                    pri("weekly"))

def _hourly(key):
    """True не чаще раза в час (для тяжёлых проверок)."""
    now = time.time()
    if now - _MON_HOURLY.get(key, 0) >= 3600:
        _MON_HOURLY[key] = now
        return True
    return False

def fmt_bytes(n):
    n = float(n or 0)
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return "%.0f %s" % (n, u)
        n /= 1024
    return "%.0f ПБ" % n

def fmt_uptime(sec):
    sec = int(sec or 0); d, h = sec // 86400, (sec % 86400) // 3600
    return ("%dд %dч" % (d, h)) if d else ("%dч %dм" % (h, (sec % 3600) // 60))

# --------------------------------------------------------------------------- #
#  История метрик (лёгкий тайм-серия для графиков за сутки)
# --------------------------------------------------------------------------- #
HISTORY_FILE = os.path.join(NAS_CONFIG, "history.json")
HISTORY_CAP  = 1500          # ~25 часов при шаге 60 с
_history = None
_hist_dirty = 0

def _load_history():
    global _history
    if _history is not None:
        return _history
    try:
        with open(HISTORY_FILE) as f:
            _history = json.load(f)[-HISTORY_CAP:]
    except (OSError, ValueError):
        _history = []
    return _history

def history_sample():
    """Снять одну точку метрик и добавить в историю (зовётся раз в минуту)."""
    global _hist_dirty
    h = _load_history()
    try:
        s = stats()
    except Exception:
        return
    pt = {"t": int(time.time()), "cpu": s.get("cpu"), "temp": s.get("temp"),
          "mem": (s.get("mem") or {}).get("pct"),
          "rx": (s.get("net") or {}).get("rx"), "tx": (s.get("net") or {}).get("tx"),
          "pool": (s.get("disk_pool") or {}).get("pct")}
    h.append(pt)
    if len(h) > HISTORY_CAP:
        del h[:len(h) - HISTORY_CAP]
    _hist_dirty += 1
    if _hist_dirty >= 5:                 # писать на диск не чаще ~раз в 5 мин
        _hist_dirty = 0
        try:
            os.makedirs(NAS_CONFIG, exist_ok=True)
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(h, f)
            os.replace(tmp, HISTORY_FILE)
        except OSError:
            pass

def monitor_loop():
    while True:
        time.sleep(60)
        try:
            history_sample()
        except Exception:
            pass
        try:
            monitor_tick()
        except Exception:
            pass

# --------------------------------------------------------------------------- #
#  Docker-сервисы / стеки (GUI-менеджер)
# --------------------------------------------------------------------------- #
STACKS_DIR = "/opt/stacks"
_STACK_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

def _compose_path(name):
    d = os.path.join(STACKS_DIR, name)
    for fn in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return os.path.join(d, "compose.yaml")

def _dc(name, *args, timeout=180):
    return _run(["docker", "compose", "-f", _compose_path(name), "-p", name, *args], timeout=timeout)

STACK_NOTES = os.path.join(NAS_CONFIG, "stack-notes.json")

def load_notes():
    try:
        with open(STACK_NOTES) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save_stack_note(name, note):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "имя"}
    d = load_notes()
    if note:
        d[name] = note
    elif name in d:
        del d[name]
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        with open(STACK_NOTES, "w") as f:
            json.dump(d, f)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def _health_of(status):
    s = (status or "").lower()
    if "(healthy)" in s:
        return "healthy"
    if "(unhealthy)" in s:
        return "unhealthy"
    if "(health: starting)" in s:
        return "starting"
    m = re.search(r"exited \((\d+)\)", s)
    if m:
        return "exit:" + m.group(1)
    return ""

def docker_stacks():
    cmap = {}
    for c in _docker_ps():
        proj = (c.get("Labels", "") or "")
        m = re.search(r"com\.docker\.compose\.project=([^,]+)", proj)
        key = m.group(1) if m else None
        url = re.search(r"web-desktop\.url=([^,]+)", proj)
        cmap.setdefault(key, []).append({
            "name": c.get("Names", ""), "state": c.get("State", ""),
            "status": c.get("Status", ""), "ports": c.get("Ports", ""),
            "image": c.get("Image", ""), "url": url.group(1) if url else "",
            "health": _health_of(c.get("Status", ""))})
    notes = load_notes()
    out = []
    try:
        names = sorted(os.listdir(STACKS_DIR))
    except OSError:
        names = []
    for nm in names:
        d = os.path.join(STACKS_DIR, nm)
        if not os.path.isdir(d):
            continue
        conts = cmap.get(nm, [])
        running = sum(1 for c in conts if c["state"] == "running")
        url = next((c["url"] for c in conts if c["url"]), "")
        out.append({"name": nm, "path": d, "has_compose": os.path.isfile(_compose_path(nm)),
                    "containers": conts, "running": running, "total": len(conts),
                    "url": url, "note": notes.get(nm, "")})
    return {"ok": True, "stacks": out, "dir": STACKS_DIR}

def stack_validate(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "имя"}
    r = _dc(name, "config", "-q", timeout=30)
    return {"ok": r["ok"], "log": (r.get("log") or "").strip() or ("OK" if r["ok"] else "ошибка")}

def docker_stats():
    r = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=15)
    out = {}
    for l in (r.get("log") or "").splitlines():
        try:
            j = json.loads(l)
            out[j.get("Name", "")] = {"cpu": j.get("CPUPerc", ""), "mem": j.get("MemPerc", ""), "memusage": j.get("MemUsage", "")}
        except ValueError:
            pass
    return {"ok": True, "stats": out}

def docker_images():
    r = _run(["docker", "images", "--format", "{{json .}}"], timeout=15)
    out = []
    for l in (r.get("log") or "").splitlines():
        try:
            j = json.loads(l)
            out.append({"id": j.get("ID", ""), "repo": j.get("Repository", ""), "tag": j.get("Tag", ""),
                        "size": j.get("Size", ""), "created": j.get("CreatedSince", "")})
        except ValueError:
            pass
    return {"ok": True, "images": out}

def docker_volumes():
    r = _run(["docker", "volume", "ls", "--format", "{{json .}}"], timeout=15)
    out = []
    for l in (r.get("log") or "").splitlines():
        try:
            j = json.loads(l)
            out.append({"name": j.get("Name", ""), "driver": j.get("Driver", "")})
        except ValueError:
            pass
    return {"ok": True, "volumes": out}

def docker_prune(what):
    cmds = {"images": ["docker", "image", "prune", "-f"],
            "images-all": ["docker", "image", "prune", "-a", "-f"],
            "volumes": ["docker", "volume", "prune", "-f"],
            "builder": ["docker", "builder", "prune", "-f"],
            "system": ["docker", "system", "prune", "-f"]}
    if what not in cmds:
        return {"ok": False, "log": "неизвестно"}
    return _run(cmds[what], timeout=180)

def docker_image_rm(iid):
    if not re.match(r"^[\w:./@-]+$", iid or ""):
        return {"ok": False, "log": "id"}
    return _run(["docker", "image", "rm", iid], timeout=60)

def docker_volume_rm(name):
    if not re.match(r"^[\w.-]+$", name or ""):
        return {"ok": False, "log": "имя"}
    return _run(["docker", "volume", "rm", name], timeout=60)

def _read_file(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""

def stack_read(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "недопустимое имя"}
    cp = _compose_path(name)
    return {"ok": True, "name": name, "compose": _read_file(cp),
            "env": _read_file(os.path.join(STACKS_DIR, name, ".env")),
            "exists": os.path.isfile(cp)}

def stack_save(name, compose, env, create=False):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "имя: буквы/цифры/._-"}
    d = os.path.join(STACKS_DIR, name)
    cp = _compose_path(name)
    if create and os.path.isdir(d) and os.path.isfile(cp):
        return {"ok": False, "log": "стек уже существует"}
    try:
        os.makedirs(d, exist_ok=True)
        if os.path.isfile(cp):
            shutil.copy2(cp, cp + ".bak")
        with open(cp, "w") as f:
            f.write(compose if compose is not None else "")
        if env is not None:
            ep = os.path.join(d, ".env")
            if os.path.isfile(ep):
                shutil.copy2(ep, ep + ".bak")
            with open(ep, "w") as f:
                f.write(env)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "name": name, "path": cp}

def stack_action(name, action):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "недопустимое имя"}
    if action == "rebuild-nocache":
        r = _dc(name, "build", "--no-cache", timeout=900)
        if not r["ok"]:
            return r
        return _dc(name, "up", "-d", timeout=300)
    amap = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"],
            "stop": ["stop"], "start": ["start"], "pull": ["pull"], "build": ["build"],
            "rebuild": ["up", "-d", "--build"], "recreate": ["up", "-d", "--force-recreate"]}
    if action not in amap:
        return {"ok": False, "log": "недопустимое действие"}
    to = 900 if action in ("rebuild", "build", "pull") else 200
    return _dc(name, *amap[action], timeout=to)

def stack_delete(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "недопустимое имя"}
    d = os.path.realpath(os.path.join(STACKS_DIR, name))
    if not d.startswith(STACKS_DIR + os.sep):
        return {"ok": False, "log": "путь вне каталога стеков"}
    _dc(name, "down")
    try:
        shutil.rmtree(d)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def stack_logs(name, tail=200):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "недопустимое имя"}
    try:
        n = max(10, min(2000, int(tail)))
    except (ValueError, TypeError):
        n = 200
    r = _dc(name, "logs", "--tail", str(n), "--no-color", "--no-log-prefix", timeout=20)
    return {"ok": True, "name": name, "log": r.get("log", "")}

def container_action(cid, action):
    if not re.match(r"^[a-zA-Z0-9_.-]+$", cid or ""):
        return {"ok": False, "log": "недопустимый контейнер"}
    if action not in ("start", "stop", "restart", "rm"):
        return {"ok": False, "log": "недопустимое действие"}
    args = ["rm", "-f", cid] if action == "rm" else [action, cid]
    return _run(["docker", *args], timeout=60)

# --------------------------------------------------------------------------- #
#  Docker-сервисы
# --------------------------------------------------------------------------- #
def _docker_ps():
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=8).stdout
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

def discover_desktop_apps():
    """Ярлыки рабочего стола из docker-лейблов web-desktop.* на любых контейнерах.
    Метки: web-desktop.name / .url / .icon / .enable(=false → скрыть).
    Видит контейнеры независимо от того, кто их запустил (в т.ч. из Dockge)."""
    try:
        ids = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}}"],
                             capture_output=True, text=True, timeout=8).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    if not ids:
        return []
    US, RS = "\x1f", "\x1e"   # разделители, которых не бывает в значениях меток
    try:
        fmt = "{{.Name}}%s{{.State.Status}}%s{{json .Config.Labels}}%s" % (US, US, RS)
        raw = subprocess.run(["docker", "inspect", "-f", fmt] + ids,
                             capture_output=True, text=True, timeout=12).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    apps = []
    for rec in raw.split(RS):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        parts = rec.split(US)
        if len(parts) < 3:
            continue
        name, status = parts[0].lstrip("/"), parts[1]
        try:
            labels = json.loads(parts[2]) or {}
        except (json.JSONDecodeError, TypeError):
            labels = {}
        g = lambda k: labels.get("web-desktop." + k)
        if not (g("name") or g("url")):
            continue
        if (g("enable") or "true").strip().lower() in ("false", "0", "no", "off"):
            continue
        apps.append({
            "container": name,
            "name": g("name") or name,
            "url": g("url") or "",
            "icon": g("icon") or "",
            "running": status == "running",
            "status": status,
        })
    apps.sort(key=lambda a: a["name"].lower())
    return apps

# --------------------------------------------------------------------------- #
#  Cronmaster — прокси к его REST API (один origin, без CORS и ключей)
# --------------------------------------------------------------------------- #
def _cron(method, path, body=None, timeout=12):
    """Запрос к cronmaster. Возвращает {ok, status, data} или {ok:False, offline?, log}."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CRON_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "status": r.status, "data": json.loads(txt)}
            except json.JSONDecodeError:
                return {"ok": True, "status": r.status, "data": txt}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "log": e.read().decode("utf-8", "replace")[:400]}
    except (urllib.error.URLError, OSError):
        return {"ok": False, "offline": True, "log": "Cronmaster не запущен (установите его через Мастер → Dockge)"}

def cron_jobs():
    r = _cron("GET", "/api/cronjobs")
    if not r.get("ok"):
        return r
    d = r["data"]                              # cronmaster оборачивает: {success, data:[...]}
    jobs = d.get("data") if isinstance(d, dict) else d
    return {"ok": True, "jobs": jobs or []}

def cron_stats():
    r = _cron("GET", "/api/system-stats")      # плоский объект {uptime, memory, cpu, network}
    return {"ok": True, "stats": r["data"]} if r.get("ok") else r

def cron_run(jid):
    return _cron("GET", "/api/cronjobs/%s/execute?runInBackground=true" % jid)

def cron_update(jid, body):
    keep = {k: body[k] for k in ("schedule", "command", "comment", "logsEnabled") if k in body}
    return _cron("PATCH", "/api/cronjobs/%s" % jid, keep)

def cron_delete(jid):
    return _cron("DELETE", "/api/cronjobs/%s" % jid)

def cron_scripts():
    r = _cron("GET", "/api/scripts")
    if not r.get("ok"):
        return r
    d = r["data"]
    return {"ok": True, "scripts": (d.get("data") if isinstance(d, dict) else d) or []}

def cron_logs(run_id, offset=0, max_lines=500):
    try:
        offset = int(offset); max_lines = max(100, min(5000, int(max_lines)))
    except (TypeError, ValueError):
        offset, max_lines = 0, 500
    q = "runId=%s&offset=%d&maxLines=%d" % (quote(str(run_id)), offset, max_lines)
    r = _cron("GET", "/api/logs/stream?" + q)
    return {"ok": True, **r["data"]} if r.get("ok") and isinstance(r.get("data"), dict) else r

# --------------------------------------------------------------------------- #
#  Файловый менеджер (нативный, от root — вся ФС). LAN-админ-инструмент.
# --------------------------------------------------------------------------- #
FS_TEXT_MAX = 3 * 1024 * 1024   # больше — не грузим в редактор

def _fs_entry(full):
    st = os.lstat(full)
    isdir = os.path.isdir(full)   # следует по симлинку на каталог
    return {"name": os.path.basename(full) or full, "path": full,
            "type": "dir" if isdir else "file",
            "size": 0 if isdir else st.st_size, "mtime": int(st.st_mtime),
            "mode": oct(st.st_mode & 0o777)[2:], "link": os.path.islink(full)}

def _uniq(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while os.path.exists("%s (%d)%s" % (base, i, ext)):
        i += 1
    return "%s (%d)%s" % (base, i, ext)

# ---- thumbnails (кэш + генерация через ffmpeg/pdftoppm) ----
THUMBS_DIR = "/var/cache/nas-thumbs"
THUMB_PX   = 320
THUMB_MAX_SWEEP = 400     # не прогревать гигантские каталоги за раз
_THUMB_IMG = {"png","jpg","jpeg","gif","webp","bmp","ico","avif","tif","tiff","svg","heic","heif"}
_THUMB_VID = {"mp4","mkv","avi","mov","webm","m4v","ogv","wmv","flv","3gp","mpg","mpeg"}
_THUMB_AUD = {"mp3","flac","m4a","aac","ogg","opus","wma"}
_THUMB_PDF = {"pdf"}
_thumb_sem = threading.Semaphore(3)   # ограничить одновременный ffmpeg

def thumb_kind(name):
    e = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if e in _THUMB_IMG: return "img"
    if e in _THUMB_VID: return "vid"
    if e in _THUMB_AUD: return "aud"
    if e in _THUMB_PDF: return "pdf"
    return None

def _thumb_path(src):
    h = hashlib.md5(src.encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(THUMBS_DIR, h[:2], h + ".jpg")

def _thumb_fresh(src, tp):
    try:
        return os.path.getmtime(tp) >= os.path.getmtime(src)
    except OSError:
        return False

def gen_thumb(src):
    """Путь к готовому превью (генерит при отсутствии/устаревании) или None."""
    kind = thumb_kind(os.path.basename(src))
    if not kind or not os.path.isfile(src):
        return None
    tp = _thumb_path(src)
    if os.path.isfile(tp) and _thumb_fresh(src, tp):
        return tp
    if kind == "pdf":
        if not shutil.which("pdftoppm"): return None
    elif not shutil.which("ffmpeg"):
        return None
    try:
        os.makedirs(os.path.dirname(tp), exist_ok=True)
    except OSError:
        return None
    # вписать в коробку THUMB_PX×THUMB_PX (портретные/вертикальные не становятся огромными)
    scale = "scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease" % (THUMB_PX, THUMB_PX)
    tmp = tp + ".%d.tmp.jpg" % os.getpid()
    ok = False
    with _thumb_sem:
        try:
            if kind == "img":
                # прозрачность PNG/WebP → подкладываем белый фон (иначе JPEG рисует мусор на месте альфы)
                cmd = ["ffmpeg","-y","-v","error","-i",src,"-filter_complex",
                       "color=c=white:s=2x2[bg];[0:v]%s[fg];[bg][fg]scale2ref[bg2][fg2];[bg2][fg2]overlay=format=auto[o]" % scale,
                       "-map","[o]","-frames:v","1",tmp]
            elif kind == "vid":
                ss = 3.0   # запасной вариант, если длительность неизвестна
                try:
                    pr = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                                         "-of","default=nk=1:nw=1", src],
                                        capture_output=True, text=True, timeout=10)
                    dur = float((pr.stdout or "").strip() or 0)
                    if dur > 0:
                        ss = max(1.0, dur * 0.1)   # ~10% от длительности
                except Exception:
                    pass
                cmd = ["ffmpeg","-y","-v","error","-ss","%.2f" % ss,"-i",src,"-vf",scale,"-frames:v","1",tmp]
            elif kind == "aud":
                cmd = ["ffmpeg","-y","-v","error","-i",src,"-an","-vf",scale,"-frames:v","1",tmp]
            else:
                base = tp[:-4]
                cmd = ["pdftoppm","-jpeg","-f","1","-l","1","-scale-to",str(THUMB_PX),src,base]
            r = subprocess.run(cmd, capture_output=True, timeout=25)
            if kind == "pdf":
                for cand in (base+".jpg", base+"-1.jpg", base+"-01.jpg"):
                    if os.path.isfile(cand):
                        if cand != tp: os.replace(cand, tp)
                        ok = True; break
            elif r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, tp); ok = True
        except Exception:
            ok = False
        finally:
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except OSError:
                pass
    if ok:
        try: os.utime(tp, None)
        except OSError: pass
        return tp
    return None

def prewarm_thumbs(path):
    """Фоновая догенерация недостающих превью для каталога (при листинге)."""
    if not (shutil.which("ffmpeg") or shutil.which("pdftoppm")):
        return
    def work():
        try:
            names = os.listdir(path)
        except OSError:
            return
        n = 0
        for name in names:
            if n >= THUMB_MAX_SWEEP: break
            if not thumb_kind(name): continue
            full = os.path.join(path, name)
            if not os.path.isfile(full): continue
            tp = _thumb_path(full)
            if os.path.isfile(tp) and _thumb_fresh(full, tp): continue
            n += 1
            gen_thumb(full)
    threading.Thread(target=work, daemon=True).start()

def video_meta(path):
    """Длительность + кодеки (для плеера/транскода)."""
    path = os.path.realpath(path)
    if not os.path.isfile(path) or not shutil.which("ffprobe"):
        return {"ok": False}
    dur, vc, ac = 0.0, "", ""
    try:
        pr = subprocess.run(["ffprobe", "-v", "error",
                             "-show_entries", "format=duration:stream=codec_name,codec_type",
                             "-of", "json", path], capture_output=True, text=True, timeout=12)
        j = json.loads(pr.stdout or "{}")
        dur = float(j.get("format", {}).get("duration") or 0)
        for s in j.get("streams", []):
            if s.get("codec_type") == "video" and not vc:
                vc = s.get("codec_name", "")
            elif s.get("codec_type") == "audio" and not ac:
                ac = s.get("codec_name", "")
    except Exception:
        pass
    return {"ok": True, "duration": dur, "vcodec": vc, "acodec": ac}

def thumbs_sweep(dirs):
    """Рекурсивный прогрев кэша превью (для ночного таймера)."""
    n = 0
    for d in dirs:
        for root, _dirs, files in os.walk(os.path.realpath(d)):
            for name in files:
                if not thumb_kind(name):
                    continue
                full = os.path.join(root, name)
                tp = _thumb_path(full)
                if os.path.isfile(tp) and _thumb_fresh(full, tp):
                    continue
                if gen_thumb(full):
                    n += 1
    return n

def fs_list(path):
    path = os.path.realpath(path or "/")
    if not os.path.isdir(path):
        return {"ok": False, "log": "не каталог: " + path}
    entries = []
    try:
        names = os.listdir(path)
    except PermissionError:
        return {"ok": False, "log": "нет доступа к " + path}
    except OSError as e:
        return {"ok": False, "log": str(e)}
    for name in names:
        try:
            entries.append(_fs_entry(os.path.join(path, name)))
        except OSError:
            pass
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return {"ok": True, "path": path,
            "parent": (os.path.dirname(path) if path != "/" else None),
            "entries": entries}

def fs_read(path):
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        return {"ok": False, "log": "не файл"}
    size = os.path.getsize(path)
    if size > FS_TEXT_MAX:
        return {"ok": True, "path": path, "binary": True, "size": size}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"ok": False, "log": str(e)}
    try:
        return {"ok": True, "path": path, "content": raw.decode("utf-8"),
                "size": size, "binary": False}
    except UnicodeDecodeError:
        return {"ok": True, "path": path, "binary": True, "size": size}

def fs_write(path, content):
    path = os.path.realpath(path)
    if not os.path.isdir(os.path.dirname(path)):
        return {"ok": False, "log": "каталог не существует"}
    if os.path.isdir(path):
        return {"ok": False, "log": "это каталог"}
    try:
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        with open(path, "w") as f:
            f.write(content if content is not None else "")
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": path}

def _child(path, name):
    return os.path.join(os.path.realpath(path), os.path.basename((name or "").strip()))

# ---- загрузка файла по URL (сервер качает потоково в папку, с прогрессом) ----
_FETCH_JOBS = {}
_FETCH_LOCK = threading.Lock()

def fs_fetch_start(path, url, name=""):
    from urllib.parse import urlparse as _up, unquote
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return {"ok": False, "log": "нужен http(s) URL"}
    d = os.path.realpath(path or "/")
    if not os.path.isdir(d):
        return {"ok": False, "log": "не каталог назначения"}
    fname = os.path.basename((name or "").strip()) or os.path.basename(unquote(_up(url).path)) or ""
    jid = hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    job = {"id": jid, "name": fname or "…", "total": 0, "got": 0,
           "done": False, "ok": False, "log": "", "path": ""}
    with _FETCH_LOCK:
        # лёгкая уборка старых завершённых задач
        for k in [k for k, v in _FETCH_JOBS.items() if v.get("done")][:-20]:
            _FETCH_JOBS.pop(k, None)
        _FETCH_JOBS[jid] = job

    def work():
        dest = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nas-web"})
            with urllib.request.urlopen(req, timeout=30) as r:
                nm = fname
                if not nm:
                    cd = r.headers.get("Content-Disposition", "")
                    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)', cd)
                    nm = os.path.basename(unquote(m.group(1))) if m else "download"
                job["name"] = nm
                try:
                    job["total"] = int(r.headers.get("Content-Length") or 0)
                except ValueError:
                    job["total"] = 0
                dest = _uniq(os.path.join(d, os.path.basename(nm)))
                limit = 40 * 1024 * 1024 * 1024
                with open(dest, "wb") as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        job["got"] += len(chunk)
                        if job["got"] > limit:
                            raise IOError("файл больше 40 ГБ — прервано")
            job["ok"] = True
            job["path"] = dest
            if thumb_kind(os.path.basename(dest)):
                try:
                    gen_thumb(dest)
                except Exception:
                    pass
        except Exception as e:
            job["log"] = str(e)
            if dest and os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass
        finally:
            job["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "id": jid, "name": job["name"]}

def fs_fetch_status(jid):
    with _FETCH_LOCK:
        job = _FETCH_JOBS.get(jid)
    if not job:
        return {"ok": False, "log": "задача не найдена"}
    return {"ok": True, "job": dict(job)}

def fs_mkdir(path, name):
    d = _child(path, name)
    if not os.path.basename(d):
        return {"ok": False, "log": "пустое имя"}
    try:
        os.makedirs(d, exist_ok=False)
    except FileExistsError:
        return {"ok": False, "log": "уже существует"}
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": d}

def fs_rename(src, name):
    src = os.path.realpath(src)
    base = os.path.basename((name or "").strip())
    if not base:
        return {"ok": False, "log": "пустое имя"}
    dst = os.path.join(os.path.dirname(src), base)
    if os.path.exists(dst):
        return {"ok": False, "log": "уже существует"}
    try:
        os.rename(src, dst)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": dst}

def fs_delete(path):
    path = os.path.realpath(path)
    if path == "/" or path.count("/") < 2:   # защита от / и каталогов верхнего уровня (/etc, /usr…)
        return {"ok": False, "log": "слишком опасный путь: " + path}
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def fs_upload(path, name, data_b64):
    full = _child(path, name)
    if not os.path.basename(full):
        return {"ok": False, "log": "нет имени файла"}
    try:
        raw = base64.b64decode((data_b64 or "").split(",")[-1])
        with open(full, "wb") as f:
            f.write(raw)
    except (OSError, ValueError) as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": full, "size": len(raw)}

def fs_newfile(path, name):
    full = _child(path, name)
    if not os.path.basename(full):
        return {"ok": False, "log": "пустое имя"}
    if os.path.exists(full):
        return {"ok": False, "log": "уже существует"}
    try:
        open(full, "x").close()
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": full}

def _into_self(src, dst_dir):
    return dst_dir == src or dst_dir.startswith(src.rstrip("/") + os.sep)

def fs_copy(src, dst_dir):
    src = os.path.realpath(src); dst_dir = os.path.realpath(dst_dir)
    if not os.path.exists(src):
        return {"ok": False, "log": "нет источника"}
    if not os.path.isdir(dst_dir):
        return {"ok": False, "log": "цель не каталог"}
    if os.path.isdir(src) and _into_self(src, dst_dir):
        return {"ok": False, "log": "нельзя копировать в себя"}
    dst = _uniq(os.path.join(dst_dir, os.path.basename(src)))
    try:
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": dst}

def fs_move(src, dst_dir):
    src = os.path.realpath(src); dst_dir = os.path.realpath(dst_dir)
    if not os.path.exists(src):
        return {"ok": False, "log": "нет источника"}
    if not os.path.isdir(dst_dir):
        return {"ok": False, "log": "цель не каталог"}
    if _into_self(src, dst_dir) or os.path.dirname(src) == dst_dir:
        return {"ok": False, "log": "нельзя переместить сюда"}
    dst = _uniq(os.path.join(dst_dir, os.path.basename(src)))
    try:
        shutil.move(src, dst)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": dst}

def fs_search(path, query, limit=400):
    path = os.path.realpath(path or "/")
    q = (query or "").lower().strip()
    if not q or not os.path.isdir(path):
        return {"ok": True, "entries": [], "query": q}
    out = []
    for root, dirs, files in os.walk(path):
        for nm in dirs + files:
            if q in nm.lower():
                try:
                    out.append(_fs_entry(os.path.join(root, nm)))
                except OSError:
                    pass
                if len(out) >= limit:
                    return {"ok": True, "entries": out, "truncated": True, "query": q}
    return {"ok": True, "entries": out, "query": q}

def fs_chmod(path, mode, recursive=False):
    path = os.path.realpath(path)
    if path == "/" or path.count("/") < 2:
        return {"ok": False, "log": "слишком опасный путь: " + path}
    try:
        m = int(str(mode).strip(), 8)
    except ValueError:
        return {"ok": False, "log": "неверный режим (нужно восьмеричное, напр. 644)"}
    try:
        os.chmod(path, m)
        if recursive and os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                for nm in dirs + files:
                    try:
                        os.chmod(os.path.join(root, nm), m)
                    except OSError:
                        pass
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def _resolve_uid(owner):
    s = str(owner if owner is not None else "").strip()
    if not s:
        return -1
    return int(s) if s.isdigit() else pwd.getpwnam(s).pw_uid

def _resolve_gid(group):
    import grp
    s = str(group if group is not None else "").strip()
    if not s:
        return -1
    return int(s) if s.isdigit() else grp.getgrnam(s).gr_gid

def fs_chown(path, owner, group, recursive=False):
    path = os.path.realpath(path)
    if path == "/" or path.count("/") < 2:
        return {"ok": False, "log": "слишком опасный путь: " + path}
    try:
        uid = _resolve_uid(owner)
        gid = _resolve_gid(group)
    except (KeyError, ValueError):
        return {"ok": False, "log": "нет такого пользователя или группы"}
    if uid == -1 and gid == -1:
        return {"ok": False, "log": "не указан владелец или группа"}
    try:
        os.chown(path, uid, gid)
        if recursive and os.path.isdir(path) and not os.path.islink(path):
            for root, dirs, files in os.walk(path):
                for nm in dirs + files:
                    try:
                        os.chown(os.path.join(root, nm), uid, gid)
                    except OSError:
                        pass
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def fs_du(path):
    path = os.path.realpath(path)
    if not os.path.isdir(path):
        try:
            return {"ok": True, "size": os.path.getsize(path), "files": 1, "dirs": 0}
        except OSError as e:
            return {"ok": False, "log": str(e)}
    total = files = dirs = 0
    for root, ds, fls in os.walk(path):
        dirs += len(ds)
        for f in fls:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
                files += 1
            except OSError:
                pass
    return {"ok": True, "size": total, "files": files, "dirs": dirs}

def fs_grep(path, query, limit=200):
    path = os.path.realpath(path or "/")
    q = (query or "").strip()
    if not q or not os.path.isdir(path):
        return {"ok": True, "entries": [], "query": q}
    ql = q.lower()
    out = []
    SKIP = {".git", "node_modules", "__pycache__", ".cache", "vendor"}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for nm in files:
            fp = os.path.join(root, nm)
            try:
                if os.path.getsize(fp) > 2 * 1024 * 1024:
                    continue
                with open(fp, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if ql in line.lower():
                            e = _fs_entry(fp)
                            e["match"] = line.strip()[:200]
                            e["line"] = i
                            out.append(e)
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if len(out) >= limit:
                return {"ok": True, "entries": out, "truncated": True, "query": q}
    return {"ok": True, "entries": out, "query": q}

def fs_stat(path):
    path = os.path.realpath(path)
    try:
        st = os.lstat(path)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    try:
        import grp
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError, ImportError):
        group = str(st.st_gid)
    return {"ok": True, "path": path, "size": st.st_size, "mtime": int(st.st_mtime),
            "mode": oct(st.st_mode & 0o777)[2:], "owner": owner, "group": group,
            "type": "dir" if os.path.isdir(path) else "file", "link": os.path.islink(path)}

def _trash_load():
    try:
        with open(os.path.join(TRASH, "index.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []

def _trash_save(items):
    os.makedirs(TRASH, exist_ok=True)
    with open(os.path.join(TRASH, "index.json"), "w") as f:
        json.dump(items, f)

def _trash_rm(store):
    if store and os.path.lexists(store):
        if os.path.isdir(store) and not os.path.islink(store):
            shutil.rmtree(store)
        else:
            os.remove(store)

def fs_trash(path):
    path = os.path.realpath(path)
    if path == "/" or path.count("/") < 2:
        return {"ok": False, "log": "слишком опасный путь: " + path}
    if not os.path.lexists(path):
        return {"ok": False, "log": "нет такого пути"}
    if path == TRASH or path.startswith(TRASH + os.sep):
        return {"ok": False, "log": "уже в корзине"}
    store_dir = os.path.join(TRASH, "files")
    try:
        os.makedirs(store_dir, exist_ok=True)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    tid = hashlib.md5((path + str(time.time())).encode()).hexdigest()[:12]
    dest = os.path.join(store_dir, tid + "__" + os.path.basename(path))
    isdir = os.path.isdir(path) and not os.path.islink(path)
    size = 0
    try:
        size = 0 if isdir else os.path.getsize(path)
    except OSError:
        pass
    try:
        shutil.move(path, dest)
    except (OSError, shutil.Error) as e:
        return {"ok": False, "log": str(e)}
    items = _trash_load()
    items.append({"id": tid, "orig": path, "name": os.path.basename(path),
                  "deleted": int(time.time()), "isdir": isdir, "size": size, "store": dest})
    _trash_save(items)
    return {"ok": True, "id": tid}

def fs_trash_list():
    items = []
    for it in _trash_load():
        it = dict(it)
        it["exists"] = os.path.lexists(it.get("store", ""))
        items.append(it)
    items.sort(key=lambda x: x.get("deleted", 0), reverse=True)
    return {"ok": True, "items": items}

def fs_trash_restore(tid):
    items = _trash_load()
    hit = next((i for i in items if i.get("id") == tid), None)
    if not hit:
        return {"ok": False, "log": "не найдено в корзине"}
    store = hit.get("store")
    if not store or not os.path.lexists(store):
        _trash_save([i for i in items if i.get("id") != tid])
        return {"ok": False, "log": "файл отсутствует в хранилище"}
    dest = _uniq(hit["orig"])
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(store, dest)
    except (OSError, shutil.Error) as e:
        return {"ok": False, "log": str(e)}
    _trash_save([i for i in items if i.get("id") != tid])
    return {"ok": True, "path": dest}

def fs_trash_delete(tid):
    items = _trash_load()
    hit = next((i for i in items if i.get("id") == tid), None)
    if not hit:
        return {"ok": False, "log": "не найдено"}
    try:
        _trash_rm(hit.get("store"))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _trash_save([i for i in items if i.get("id") != tid])
    return {"ok": True}

def fs_trash_empty():
    items = _trash_load()
    for it in items:
        try:
            _trash_rm(it.get("store"))
        except OSError:
            pass
    _trash_save([])
    return {"ok": True, "count": len(items)}

def fs_archive(items, dest, name):
    import zipfile
    dest = os.path.realpath(dest or "/")
    if not os.path.isdir(dest):
        return {"ok": False, "log": "цель не каталог"}
    name = (name or "archive").strip() or "archive"
    if not name.endswith(".zip"):
        name += ".zip"
    out = _uniq(os.path.join(dest, os.path.basename(name)))
    items = [os.path.realpath(i) for i in (items or []) if i]
    if not items:
        return {"ok": False, "log": "нечего архивировать"}
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for it in items:
                if os.path.isdir(it):
                    base = os.path.dirname(it)
                    for root, _, files in os.walk(it):
                        for f in files:
                            fp = os.path.join(root, f)
                            z.write(fp, os.path.relpath(fp, base))
                elif os.path.isfile(it):
                    z.write(it, os.path.basename(it))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": out}

def fs_unzip(path, dest=None):
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        return {"ok": False, "log": "нет архива"}
    dest = os.path.realpath(dest) if dest else os.path.dirname(path)
    try:
        os.makedirs(dest, exist_ok=True)
        shutil.unpack_archive(path, dest)
    except (shutil.ReadError, OSError, ValueError) as e:
        return {"ok": False, "log": "не распаковать: " + str(e)}
    return {"ok": True, "path": dest}

HP_CATALOG = [
    ("Cockpit",      9090, True,  "cockpit",    "Панель управления сервером"),
    ("Dockge",       5001, False, "dockge",     "Менеджер docker-стеков"),
    ("Dozzle",       8083, False, "dozzle",     "Логи контейнеров"),
    ("Scrutiny",     8084, False, "scrutiny",   "SMART-здоровье дисков"),
    ("Syncthing",    8384, False, "syncthing",  "Синхронизация файлов"),
    ("NextExplorer", 3000, False, "mdi-folder", "Файловый менеджер"),
]
def write_homepage_config(host=None):
    host = host or (socket.gethostname() + ".local")
    cfgdir = "/opt/docker/homepage/config"
    try:
        os.makedirs(cfgdir, exist_ok=True)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    out = ["---", "- Сервисы NAS:"]
    for name, port, https, icon, desc in HP_CATALOG:
        url = f"{'https' if https else 'http'}://{host}:{port}"
        ic = icon if icon.startswith("mdi-") else icon + ".png"
        out += [f"    - {name}:", f"        href: {url}", f"        description: {desc}",
                f"        icon: {ic}", f"        siteMonitor: {url}"]
    with open(os.path.join(cfgdir, "services.yaml"), "w") as f:
        f.write("\n".join(out) + "\n")
    defaults = {
        "settings.yaml": "---\ntitle: NAS\ntheme: dark\ncolor: slate\nheaderStyle: clean\n",
        "widgets.yaml": "---\n- resources:\n    cpu: true\n    memory: true\n    disk: /mnt/storage\n- search:\n    provider: duckduckgo\n",
        "bookmarks.yaml": "---\n",
    }
    for fn, txt in defaults.items():
        pth = os.path.join(cfgdir, fn)
        if not os.path.isfile(pth):
            with open(pth, "w") as f:
                f.write(txt)
    return {"ok": True, "path": cfgdir, "services": len(HP_CATALOG)}

# --------------------------------------------------------------------------- #
#  Хранилище доступов
# --------------------------------------------------------------------------- #
def load_creds():
    try:
        with open(CREDS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

def save_creds(data):
    os.makedirs(NAS_CONFIG, exist_ok=True)
    tmp = CREDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CREDS_FILE)
    try:
        os.chmod(CREDS_FILE, 0o600)
    except OSError:
        pass

# --------------------------------------------------------------------------- #
#  Аутентификация веб-UI: пароль (PBKDF2 на диске) + сессии в памяти.
#  Файл создаётся лениво самим сервером — установщику ничего делать не нужно.
# --------------------------------------------------------------------------- #
AUTH_FILE   = "/etc/nas-os/webauth.json"
SESS_FILE   = "/etc/nas-os/sessions.json"   # сессии переживают перезапуск службы
SESSION_TTL = 30 * 86400
_sess_lock  = threading.Lock()
_login_fail = {"n": 0, "t": 0.0}        # антибрутфорс: пауза после серии неудач

def _load_sessions():
    try:
        with open(SESS_FILE) as f:
            d = json.load(f)
        now = time.time()
        return {t: e for t, e in d.items() if e > now}
    except (OSError, ValueError):
        return {}

def _save_sessions():
    try:
        os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
        tmp = SESS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_sessions, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESS_FILE)
    except OSError:
        pass

_sessions   = _load_sessions()          # token -> unix-время истечения

def _pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()

def auth_configured():
    return os.path.isfile(AUTH_FILE)

def auth_set_password(password):
    if len(password or "") < 4:
        return {"ok": False, "log": "пароль слишком короткий (минимум 4 символа)"}
    salt = secrets.token_bytes(16)
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"kdf": "pbkdf2-sha256-200k", "salt": salt.hex(),
                   "hash": _pw_hash(password, salt)}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
    return {"ok": True}

def auth_check_password(password):
    try:
        with open(AUTH_FILE) as f:
            d = json.load(f)
        return hmac.compare_digest(_pw_hash(password or "", bytes.fromhex(d["salt"])),
                                   d["hash"])
    except (OSError, ValueError, KeyError):
        return False

def session_new():
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with _sess_lock:
        for k in [k for k, e in _sessions.items() if e < now]:
            _sessions.pop(k, None)
        _sessions[tok] = now + SESSION_TTL
        _save_sessions()
    return tok

def session_valid(tok):
    if not tok:
        return False
    now = time.time()
    with _sess_lock:
        exp = _sessions.get(tok)
        if not exp or exp < now:
            if exp:
                _sessions.pop(tok, None); _save_sessions()
            return False
        # скользящее продление; на диск пишем только при заметном сдвиге (не чаще ~раз в сутки)
        if exp - now < SESSION_TTL - 86400:
            _sessions[tok] = now + SESSION_TTL
            _save_sessions()
        return True

def session_drop(tok):
    with _sess_lock:
        if _sessions.pop(tok, None) is not None:
            _save_sessions()

# --------------------------------------------------------------------------- #
#  Мост к движку nas-wizard.sh api
# --------------------------------------------------------------------------- #
ENGINE_ACTION_RE = re.compile(r"^[a-z0-9-]{1,40}$")

def _engine_env(params, dry):
    """Собрать NASW_* окружение, отфильтровав опасный ввод: имена параметров —
    только [A-Za-z0-9_], значения — без управляющих символов и разумной длины."""
    env = dict(os.environ)
    env["NASW_DRYRUN"] = "1" if dry else "0"
    for k, v in (params or {}).items():
        if not re.match(r"^[A-Za-z0-9_]{1,32}$", str(k)):
            raise ValueError("недопустимое имя параметра: %r" % k)
        v = str(v)
        if len(v) > 4096 or re.search(r"[\x00-\x1f]", v):
            raise ValueError("недопустимое значение параметра %s" % k)
        env["NASW_" + k.upper()] = v
    return env

def engine(action, params=None, dry=False):
    if not ENGINE_ACTION_RE.match(action or ""):
        return {"ok": False, "code": -1, "log": "недопустимое действие: %r" % action}
    try:
        env = _engine_env(params, dry)
    except ValueError as e:
        return {"ok": False, "code": -1, "log": str(e)}
    try:
        p = subprocess.run(["bash", ENGINE, "api", action], env=env,
                           capture_output=True, text=True, timeout=1800)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "log": (p.stdout + p.stderr)}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "code": -1, "log": str(e)}

# --------------------------------------------------------------------------- #
#  Настройки системы: чтение текущего состояния + запись (обе стороны).
#  Пакеты/скрипты ставит движок nas-wizard.sh (api pi|security|shares);
#  простые правки конфигов и переключение служб делаем здесь напрямую.
# --------------------------------------------------------------------------- #
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
SIZE_RE     = re.compile(r"^\d{1,6}[KMG]?$")
IP_RE       = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

def _sc(*args, timeout=15):
    """Короткий системный вызов -> stdout строкой ('' при ошибке)."""
    try:
        return subprocess.run(list(args), capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def _svc(units):
    """Состояние службы (первой существующей из списка альтернативных имён)."""
    if isinstance(units, str):
        units = [units]
    for u in units:
        out = _sc("systemctl", "show", u, "-p", "LoadState",
                  "-p", "UnitFileState", "-p", "ActiveState")
        kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        if kv.get("LoadState") == "loaded" or kv.get("UnitFileState"):
            return {"unit": u, "installed": kv.get("LoadState") == "loaded",
                    "enabled": kv.get("UnitFileState") in
                        ("enabled", "static", "enabled-runtime", "alias"),
                    "active": kv.get("ActiveState") == "active"}
    return {"unit": units[0], "installed": False, "enabled": False, "active": False}

def _svc_toggle(unit, on):
    return _run(["systemctl", "enable" if on else "disable", "--now", unit], timeout=40)

def _backup(path):
    try:
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
    except OSError:
        pass

def _boot_config():
    for p in ("/boot/firmware/config.txt", "/boot/config.txt"):
        if os.path.isfile(p):
            return p
    return ""

def _cmdline_path():
    for p in ("/boot/firmware/cmdline.txt", "/boot/cmdline.txt"):
        if os.path.isfile(p):
            return p
    return ""

def _cfg_has(path, prefix):
    for l in _read(path).splitlines():
        s = l.strip()
        if s and not s.startswith("#") and s.startswith(prefix):
            return True
    return False

def _cfg_set(path, prefix, line, on):
    """Идемпотентно добавить/убрать активную строку в config.txt (с бэкапом)."""
    if not path:
        return {"ok": False, "log": "config.txt не найден"}
    lines = _read(path).split("\n")
    kept = [l for l in lines
            if not (l.strip().startswith(prefix) and not l.strip().startswith("#"))]
    changed = len(kept) != len(lines)
    if on and not any(l.strip() == line for l in kept):
        while kept and kept[-1].strip() == "":
            kept.pop()
        kept.append(line)
        changed = True
    if changed:
        _backup(path)
        try:
            with open(path, "w") as f:
                f.write("\n".join(kept).rstrip("\n") + "\n")
        except OSError as e:
            return {"ok": False, "log": str(e)}
    return {"ok": True, "reboot": True,
            "log": "включено (применится после перезагрузки)" if on else "выключено"}

def _cmdline_set(add=(), remove_prefixes=()):
    """Правка cmdline.txt (одна строка, токены через пробел)."""
    path = _cmdline_path()
    if not path:
        return {"ok": False, "log": "cmdline.txt не найден"}
    line = (_read(path).split("\n") or [""])[0]
    toks = [t for t in line.split()
            if not any(t.startswith(p) for p in remove_prefixes)]
    for a in add:
        if a not in toks:
            toks.append(a)
    new = " ".join(toks)
    if new != line:
        _backup(path)
        try:
            with open(path, "w") as f:
                f.write(new + "\n")
        except OSError as e:
            return {"ok": False, "log": str(e)}
    return {"ok": True, "reboot": True,
            "log": "применится после перезагрузки"}

def _throttled_decode():
    m = re.search(r"0x[0-9a-fA-F]+", _sc("vcgencmd", "get_throttled"))
    v = int(m.group(0), 16) if m else 0
    bits = {0: "понижено напряжение", 1: "частота ограничена",
            2: "троттлинг", 3: "близко к тепловому пределу"}
    return {"raw": m.group(0) if m else "0x0",
            "now": [bits[b] for b in bits if v & (1 << b)],
            "ever": [bits[b] for b in bits if v & (1 << (b + 16))]}

def _usb_ids():
    ids = set()
    for b in glob.glob("/sys/block/sd*"):
        p = os.path.realpath(os.path.join(b, "device"))
        while p and p != "/":
            if os.path.isfile(os.path.join(p, "idVendor")) and \
               os.path.isfile(os.path.join(p, "idProduct")):
                ids.add(_read(os.path.join(p, "idVendor")) + ":" +
                        _read(os.path.join(p, "idProduct")))
                break
            p = os.path.dirname(p)
    return sorted(ids)

def _governor():
    base = "/sys/devices/system/cpu/cpu0/cpufreq/"
    return {"current": _read(base + "scaling_governor") or None,
            "available": _read(base + "scaling_available_governors").split(),
            "adaptive": _svc("nas-governor.timer")}

def _primary_iface():
    m = re.search(r"dev (\S+)", _sc("ip", "route", "get", "1.1.1.1"))
    return m.group(1) if m else ""

def _active_conn(ifc):
    for l in _sc("nmcli", "-t", "-f", "NAME,DEVICE",
                 "connection", "show", "--active").splitlines():
        parts = l.rsplit(":", 1)
        if len(parts) == 2 and parts[1] == ifc:
            return parts[0]
    return ""

def _net_state():
    ifc = _primary_iface()
    route = _sc("ip", "route", "get", "1.1.1.1")
    ipm = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)",
                    _sc("ip", "-o", "-4", "addr", "show", ifc))
    gm = re.search(r"via (\d+\.\d+\.\d+\.\d+)", route)
    conn = _active_conn(ifc)
    method = ""
    if conn:
        method = _sc("nmcli", "-t", "-f", "ipv4.method",
                     "connection", "show", conn).split(":", 1)[-1]
    dns = ",".join(x.split(":", 1)[-1]
                   for x in _sc("nmcli", "-t", "-f", "IP4.DNS",
                                "device", "show", ifc).splitlines() if x)
    is_wifi = ifc.startswith("wl")
    ps_off = None
    if is_wifi:
        ps_off = "off" in _sc("iw", "dev", ifc, "get", "power_save").lower()
    return {"iface": ifc, "ip": ipm.group(1) if ipm else "",
            "prefix": ipm.group(2) if ipm else "24",
            "gw": gm.group(1) if gm else "", "dns": dns, "conn": conn,
            "method": method or "auto", "wifi": is_wifi, "wifi_ps_off": ps_off,
            "avahi": _svc("avahi-daemon")}

def _ufw_state():
    out = _sc("ufw", "status")
    ports = [m.group(1) for m in re.finditer(r"^(\S+)\s+ALLOW", out, re.M)]
    return {"installed": bool(shutil.which("ufw")),
            "active": "Status: active" in out, "ports": sorted(set(ports))}

def _unattended_on():
    return 'Unattended-Upgrade "1"' in _read("/etc/apt/apt.conf.d/20auto-upgrades")

def _journald_max():
    for l in _read("/etc/systemd/journald.conf.d/00-nas.conf").splitlines():
        if l.strip().startswith("SystemMaxUse"):
            return l.split("=", 1)[-1].strip()
    return ""

def sysconf():
    """Полное текущее состояние всех настраиваемых параметров."""
    cfg, cl = _boot_config(), _cmdline_path()
    return {
        "system": {
            "hostname": _sc("hostname") or _read("/etc/hostname"),
            "timezone": _sc("timedatectl", "show", "-p", "Timezone", "--value")
                        or _read("/etc/timezone"),
            "time_synced": _sc("timedatectl", "show", "-p",
                               "NTPSynchronized", "--value") == "yes",
            "chrony": _svc(["chrony", "chronyd"]),
            "fstrim": _svc("fstrim.timer"),
            "unattended": _unattended_on(),
            "journald_max": _journald_max(),
        },
        "network": _net_state(),
        "security": {
            "ufw": _ufw_state(),
            "fail2ban": _svc("fail2ban"),
            "log2ram": _svc("log2ram"),
        },
        "pi": {
            "model": _read("/proc/device-tree/model").replace("\x00", "").strip(),
            "firmware": (_sc("vcgencmd", "version").splitlines() or [""])[0],
            "eeprom": (_sc("vcgencmd", "bootloader_version").splitlines() or [""])[0],
            "temp": temp_c(),
            "throttled": _throttled_decode(),
            "usbpower": _cfg_has(cfg, "usb_max_current_enable=1"),
            "pcie3": _cfg_has(cfg, "dtparam=pciex1_gen=3"),
            "cgroup": "cgroup_enable=memory" in _read(cl),
            "uasquirks": {"on": "usb-storage.quirks=" in _read(cl),
                          "detected": _usb_ids()},
            "watchdog": os.path.isfile("/etc/systemd/system.conf.d/watchdog.conf"),
            "zram": _svc("zramswap"),
            "governor": _governor(),
            "config_path": cfg, "cmdline_path": cl,
        },
    }

def _wifi_ps(off):
    ifc = _primary_iface() or "wlan0"
    conf = "/etc/NetworkManager/conf.d/wifi-powersave-off.conf"
    if off:
        os.makedirs("/etc/NetworkManager/conf.d", exist_ok=True)
        with open(conf, "w") as f:
            f.write("[connection]\nwifi.powersave = 2\n")
        _run(["iw", "dev", ifc, "set", "power_save", "off"])
        if _svc("wifi-powersave-off.service")["installed"]:
            _svc_toggle("wifi-powersave-off.service", True)
    else:
        try:
            os.remove(conf)
        except OSError:
            pass
        _run(["iw", "dev", ifc, "set", "power_save", "on"])
        if _svc("wifi-powersave-off.service")["installed"]:
            _svc_toggle("wifi-powersave-off.service", False)
    return {"ok": True, "log": "энергосбережение Wi-Fi " +
            ("отключено" if off else "включено")}

def _watchdog(on):
    p = "/etc/systemd/system.conf.d/watchdog.conf"
    if on:
        os.makedirs("/etc/systemd/system.conf.d", exist_ok=True)
        with open(p, "w") as f:
            f.write("[Manager]\nRuntimeWatchdogSec=15s\nRebootWatchdogSec=2min\n")
    else:
        try:
            os.remove(p)
        except OSError:
            pass
    _run(["systemctl", "daemon-reexec"], timeout=30)
    return {"ok": True, "log": "watchdog " + ("включён" if on else "выключен")}

def _set_governor(val):
    av = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors").split()
    if val not in av:
        return {"ok": False, "log": "governor недоступен: " + ", ".join(av)}
    n = 0
    for g in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        try:
            with open(g, "w") as f:
                f.write(val)
            n += 1
        except OSError:
            pass
    note = ""
    if _svc("nas-governor.timer")["active"]:
        _svc_toggle("nas-governor.timer", False)
        note = " (адаптивный governor отключён — иначе перезапишет)"
    return {"ok": n > 0, "log": f"governor={val} на {n} ядрах" + note}

def _net_apply(method, extra):
    ifc = _primary_iface()
    conn = _active_conn(ifc)
    if not conn:
        return {"ok": False, "log": "активное подключение не найдено"}
    if method == "auto":
        r = _run(["nmcli", "connection", "modify", conn, "ipv4.method", "auto",
                  "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""])
    else:
        ip, gw = extra.get("ip", ""), extra.get("gw", "")
        dns, prefix = extra.get("dns", ""), str(extra.get("prefix", "24"))
        if not IP_RE.match(ip):
            return {"ok": False, "log": "неверный IP-адрес"}
        args = ["nmcli", "connection", "modify", conn, "ipv4.method", "manual",
                "ipv4.addresses", f"{ip}/{prefix}"]
        args += ["ipv4.gateway", gw] if gw else []
        args += ["ipv4.dns", dns] if dns else []
        r = _run(args)
    if r["ok"]:
        _run(["nmcli", "connection", "up", conn], timeout=30)
        r["log"] = r.get("log") or "сетевые настройки применены"
    return r

def sysconf_set(key, val, extra=None):
    extra = extra or {}
    b = bool(val)
    try:
        if key == "hostname":
            if not HOSTNAME_RE.match(str(val or "")):
                return {"ok": False, "log": "недопустимое имя хоста"}
            r = _run(["hostnamectl", "set-hostname", val])
            r["log"] = r.get("log") or ("имя хоста: " + val)
            return r
        if key == "timezone":
            if not os.path.isfile("/usr/share/zoneinfo/" + str(val)):
                return {"ok": False, "log": "неизвестный часовой пояс"}
            r = _run(["timedatectl", "set-timezone", val])
            r["log"] = r.get("log") or ("часовой пояс: " + val)
            return r
        if key == "journald_max":
            if not SIZE_RE.match(str(val or "")):
                return {"ok": False, "log": "размер вида 200M / 1G"}
            os.makedirs("/etc/systemd/journald.conf.d", exist_ok=True)
            with open("/etc/systemd/journald.conf.d/00-nas.conf", "w") as f:
                f.write("[Journal]\nSystemMaxUse=%s\nSystemMaxFileSize=50M\n" % val)
            return _run(["systemctl", "restart", "systemd-journald"])
        if key == "chrony":
            if b:
                return engine("pi", {"keys": "chrony"})
            _svc_toggle("systemd-timesyncd", True)
            return _svc_toggle("chrony" if _svc("chrony")["installed"] else "chronyd", False)
        if key == "fstrim":
            return _svc_toggle("fstrim.timer", b)
        if key == "unattended":
            if b:
                return engine("security", {"keys": "unattended"})
            p = "/etc/apt/apt.conf.d/20auto-upgrades"
            if os.path.isfile(p):
                with open(p, "w") as f:
                    f.write('APT::Periodic::Update-Package-Lists "0";\n'
                            'APT::Periodic::Unattended-Upgrade "0";\n')
            return {"ok": True, "log": "автообновления выключены"}
        if key == "net_method":
            return _net_apply(val, extra)
        if key == "wifi_ps_off":
            return _wifi_ps(b)
        if key == "avahi":
            return engine("shares", {"keys": "avahi"}) if b \
                else _svc_toggle("avahi-daemon", False)
        if key == "ufw":
            return engine("security", {"keys": "ufw"}) if b \
                else _run(["ufw", "--force", "disable"])
        if key == "fail2ban":
            return engine("security", {"keys": "fail2ban"}) if b \
                else _svc_toggle("fail2ban", False)
        if key == "log2ram":
            return engine("security", {"keys": "log2ram"}) if b \
                else _svc_toggle("log2ram", False)
        if key == "usbpower":
            return _cfg_set(_boot_config(), "usb_max_current_enable",
                            "usb_max_current_enable=1", b)
        if key == "pcie3":
            return _cfg_set(_boot_config(), "dtparam=pciex1_gen",
                            "dtparam=pciex1_gen=3", b)
        if key == "cgroup":
            return _cmdline_set(add=["cgroup_enable=memory", "cgroup_memory=1"]) if b \
                else _cmdline_set(remove_prefixes=["cgroup_enable=memory", "cgroup_memory=1"])
        if key == "uasquirks":
            if b:
                r = engine("pi", {"keys": "uasquirks"})
                r["reboot"] = True
                return r
            return _cmdline_set(remove_prefixes=["usb-storage.quirks="])
        if key == "watchdog":
            return _watchdog(b)
        if key == "zram":
            return engine("pi", {"keys": "zram"}) if b else _svc_toggle("zramswap", False)
        if key == "governor_adaptive":
            return engine("pi", {"keys": "governor"}) if b \
                else _svc_toggle("nas-governor.timer", False)
        if key == "governor":
            return _set_governor(val)
        if key == "eeprom_update":
            r = _run(["rpi-eeprom-update", "-a"], timeout=120)
            r["reboot"] = True
            return r
        if key == "check_updates":
            _run(["apt-get", "update"], timeout=180)
            n = len(re.findall(r"^Inst ", _sc("apt-get", "-s", "upgrade"), re.M))
            return {"ok": True, "count": n, "log": f"{n} обновлений доступно"}
        if key == "restart_web":
            subprocess.Popen(["systemctl", "restart", "nas-web"])
            return {"ok": True, "log": "перезапуск службы…"}
        return {"ok": False, "log": "неизвестная настройка: " + str(key)}
    except Exception as e:
        return {"ok": False, "log": repr(e)}

# --------------------------------------------------------------------------- #
#  Обои рабочего стола: загрузка с ПК (base64) или скачивание по URL.
#  Активная картинка кэшируется локально (~/nas-config/wallpaper.<ext>) и
#  отдаётся с /api/wallpaper/img — стабильна между перезагрузками и клиентами.
# --------------------------------------------------------------------------- #
def _img_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ""

def _wallpaper_path():
    g = sorted(glob.glob(os.path.join(NAS_CONFIG, "wallpaper.*")))
    return g[0] if g else ""

def _wallpaper_save(data):
    ext = _img_ext(data)
    if not ext:
        return {"ok": False, "log": "не изображение (нужен jpg/png/webp/gif)"}
    if len(data) > 30 * 1024 * 1024:
        return {"ok": False, "log": "слишком большой файл (>30 МБ)"}
    os.makedirs(NAS_CONFIG, exist_ok=True)
    for old in glob.glob(os.path.join(NAS_CONFIG, "wallpaper.*")):
        try:
            os.remove(old)
        except OSError:
            pass
    try:
        with open(os.path.join(NAS_CONFIG, "wallpaper" + ext), "wb") as f:
            f.write(data)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "ext": ext}

def wallpaper_fetch(url):
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "log": "нужен http(s) URL"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nas-web"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(30 * 1024 * 1024 + 1)
    except Exception as e:
        return {"ok": False, "log": "не удалось загрузить: " + str(e)}
    return _wallpaper_save(data)

def wallpaper_upload(b64):
    try:
        data = base64.b64decode((b64 or "").split(",")[-1])
    except Exception:
        return {"ok": False, "log": "плохие данные изображения"}
    return _wallpaper_save(data)

# --------------------------------------------------------------------------- #
#  USB авто-импорт: при вставке флешки копировать её содержимое в заданную папку
#  (udev-хук → helper-скрипт → rsync; только копирование, с флешки ничего не удаляется)
# --------------------------------------------------------------------------- #
USB_IMPORT_CONF = "/etc/nas-wizard/usb-import.conf"
USB_IMPORT_SH   = "/usr/local/bin/nas-usb-import.sh"
USB_IMPORT_RULE = "/etc/udev/rules.d/98-nas-usb-import.rules"
_USB_DEFAULT = {"enabled": False, "dest": "/mnt/storage/imports",
                "subdir": "dated", "notify": False, "eject": False}
_USB_SH = r'''#!/bin/bash
# nas-wizard: авто-импорт содержимого вставленного USB в заданную папку.
CONF=/etc/nas-wizard/usb-import.conf
[ -r "$CONF" ] || exit 0
. "$CONF"
[ "${IMPORT_ENABLED:-0}" = "1" ] || [ "${IMPORT_FORCE:-0}" = "1" ] || exit 0
dev="$1"; [ -b "$dev" ] || exit 0
LOG=/var/log/nas-usb-import.log
log(){ echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }
notify(){ [ "${IMPORT_NOTIFY:-0}" = "1" ] && [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$1" "$2" 2>/dev/null || true; }
label="$(blkid -o value -s LABEL "$dev" 2>/dev/null)"; [ -n "$label" ] || label="usb-$(basename "$dev")"
label="$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '_')"
mp="$(findmnt -n -o TARGET --source "$dev" 2>/dev/null | head -1)"
selfmount=0
if [ -z "$mp" ]; then
  mp="$(mktemp -d /run/nas-usb-import.XXXXXX)"
  mount -o ro "$dev" "$mp" 2>>"$LOG" || { log "mount fail $dev"; rmdir "$mp" 2>/dev/null; exit 1; }
  selfmount=1
fi
case "${IMPORT_SUBDIR:-dated}" in
  dated) sub="${label}-$(date '+%Y%m%d-%H%M%S')";;
  label) sub="$label";;
  *)     sub="";;
esac
dest="${IMPORT_DEST:-/mnt/storage/imports}"; [ -n "$sub" ] && dest="$dest/$sub"
mkdir -p "$dest" 2>>"$LOG"
log "import $dev ($label) -> $dest"
notify "USB-импорт начат" "Копирую «$label» в $dest"
if rsync -a "$mp"/ "$dest"/ >>"$LOG" 2>&1; then
  log "import OK -> $dest"; notify "USB-импорт готов" "«$label» скопирован в $dest"
else
  log "import FAIL $dev"; notify "USB-импорт: ошибка" "Не удалось скопировать «$label»"
fi
[ "$selfmount" = "1" ] && { umount "$mp" 2>>"$LOG"; rmdir "$mp" 2>/dev/null; }
if [ "${IMPORT_EJECT:-0}" = "1" ]; then
  pk="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1)"
  [ -n "$pk" ] && { udisksctl power-off -b "/dev/$pk" >>"$LOG" 2>&1 || eject "/dev/$pk" >>"$LOG" 2>&1 || true; log "eject /dev/$pk"; }
fi
'''
_USB_RULE = ('ACTION=="add", SUBSYSTEM=="block", ENV{ID_BUS}=="usb", '
            'ENV{ID_FS_USAGE}=="filesystem", '
            'RUN+="/usr/bin/systemd-run --no-block /usr/local/bin/nas-usb-import.sh $devnode"\n')

def usb_import_load():
    cfg = dict(_USB_DEFAULT)
    for l in _read(USB_IMPORT_CONF).splitlines():
        if "=" not in l:
            continue
        k, v = l.split("=", 1); k = k.strip(); v = v.strip().strip('"')
        if k == "IMPORT_ENABLED":  cfg["enabled"] = v == "1"
        elif k == "IMPORT_DEST":   cfg["dest"] = v
        elif k == "IMPORT_SUBDIR": cfg["subdir"] = v
        elif k == "IMPORT_NOTIFY": cfg["notify"] = v == "1"
        elif k == "IMPORT_EJECT":  cfg["eject"] = v == "1"
    cfg["installed"] = os.path.isfile(USB_IMPORT_RULE)
    cfg["rsync"] = bool(shutil.which("rsync"))
    return cfg

def _usb_install(enabled):
    try:
        with open(USB_IMPORT_SH, "w") as f:      # helper всегда (нужен и для «импорт сейчас»)
            f.write(_USB_SH)
        os.chmod(USB_IMPORT_SH, 0o755)
        if enabled:                               # udev-хук только когда включено
            with open(USB_IMPORT_RULE, "w") as f:
                f.write(_USB_RULE)
        elif os.path.isfile(USB_IMPORT_RULE):
            os.remove(USB_IMPORT_RULE)
    except OSError as e:
        return str(e)
    _run(["udevadm", "control", "--reload"], timeout=15)
    return ""

def usb_import_save(cfg):
    dest = str(cfg.get("dest", "")).strip() or "/mnt/storage/imports"
    if dest == "/" or not re.match(r"^/[\w /.+-]{1,}$", dest):
        return {"ok": False, "log": "недопустимый путь назначения"}
    subdir = cfg.get("subdir", "dated")
    if subdir not in ("dated", "label", "flat"):
        subdir = "dated"
    try:
        os.makedirs("/etc/nas-wizard", exist_ok=True)
        with open(USB_IMPORT_CONF, "w") as f:
            f.write("IMPORT_ENABLED=%d\n" % (1 if cfg.get("enabled") else 0))
            f.write("IMPORT_DEST=%s\n" % dest)
            f.write("IMPORT_SUBDIR=%s\n" % subdir)
            f.write("IMPORT_NOTIFY=%d\n" % (1 if cfg.get("notify") else 0))
            f.write("IMPORT_EJECT=%d\n" % (1 if cfg.get("eject") else 0))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    err = _usb_install(bool(cfg.get("enabled")))
    if err:
        return {"ok": False, "log": err}
    if cfg.get("enabled") and not shutil.which("rsync"):
        return {"ok": True, "warn": "rsync не установлен — импорт не сработает (ставится на этапе «Система»)"}
    return {"ok": True, "log": "сохранено"}

def usb_removable():
    out = []
    try:
        j = json.loads(subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,LABEL,MOUNTPOINT,TRAN,HOTPLUG,TYPE,RM"],
            capture_output=True, text=True, timeout=8).stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return out
    SYSMOUNTS = ("/", "/boot", "/boot/firmware", "/var", "/usr", "/home", "[SWAP]")
    def walk(nodes, usb_ancestor=False):
        for n in nodes:
            usb = usb_ancestor or n.get("tran") == "usb"   # SD/NVMe (tran mmc/nvme) исключены
            mp = n.get("mountpoint")
            if usb and mp and mp not in SYSMOUNTS and n.get("type") in ("part", "disk"):
                out.append({"path": n.get("path"), "label": n.get("label") or n.get("name"),
                            "mount": mp})
            if n.get("children"):
                walk(n["children"], usb)
    walk(j.get("blockdevices", []))
    return out

def usb_import_run(dev):
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "неверное устройство"}
    if not os.path.isfile(USB_IMPORT_SH):
        return {"ok": False, "log": "сначала сохраните настройки импорта"}
    env = dict(os.environ); env["IMPORT_FORCE"] = "1"
    try:
        p = subprocess.run([USB_IMPORT_SH, dev], env=env, capture_output=True,
                           text=True, timeout=3600)
        return {"ok": p.returncode == 0,
                "log": (p.stdout + p.stderr).strip() or ("импорт запущен" if p.returncode == 0 else "ошибка")}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "log": str(e)}

# --------------------------------------------------------------------------- #
#  Нативный терминал: WebSocket <-> PTY (bash), без пароля, под текущим юзером
# --------------------------------------------------------------------------- #
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
def _ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()

def _ws_send(sock, data, opcode=0x2):
    hdr = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126); hdr += struct.pack(">H", n)
    else:
        hdr.append(127); hdr += struct.pack(">Q", n)
    try:
        sock.sendall(bytes(hdr) + data)
    except OSError:
        pass

def _ws_recv(sock):
    def rd(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                return None
            buf += c
        return buf
    h = rd(2)
    if not h:
        return None
    b2 = h[1]; opcode = h[0] & 0x0f; masked = b2 & 0x80; ln = b2 & 0x7f
    if ln == 126:
        ln = struct.unpack(">H", rd(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rd(8))[0]
    mask = rd(4) if masked else b"\x00\x00\x00\x00"
    pay = rd(ln) if ln else b""
    if pay is None:
        return None
    if masked:
        pay = bytes(pay[i] ^ mask[i % 4] for i in range(ln))
    return (opcode, pay)

def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass

XTERM_ASSETS = {
    "xterm.js": "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
    "xterm.css": "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
    "xterm-addon-fit.js": "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
    "xterm-addon-search.js": "https://cdn.jsdelivr.net/npm/xterm-addon-search@0.13.0/lib/xterm-addon-search.js",
    "xterm-addon-web-links.js": "https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.js",
}
# CodeMirror 5 (редактор в файловом менеджере) — моды самодостаточны, цепляются к глобалу CodeMirror
CM = "https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16"
CM_ASSETS = {
    "codemirror.js":    CM + "/codemirror.min.js",
    "codemirror.css":   CM + "/codemirror.min.css",
    "cm-yaml.js":       CM + "/mode/yaml/yaml.min.js",
    "cm-shell.js":      CM + "/mode/shell/shell.min.js",
    "cm-javascript.js": CM + "/mode/javascript/javascript.min.js",
    "cm-python.js":     CM + "/mode/python/python.min.js",
    "cm-xml.js":        CM + "/mode/xml/xml.min.js",
    "cm-css.js":        CM + "/mode/css/css.min.js",
    "cm-properties.js": CM + "/mode/properties/properties.min.js",
    "cm-dockerfile.js": CM + "/mode/dockerfile/dockerfile.min.js",
}
def ensure_web_assets():
    import urllib.request
    for fn, url in {**XTERM_ASSETS, **CM_ASSETS}.items():
        p = os.path.join(WEB_DIR, fn)
        if os.path.isfile(p) and os.path.getsize(p) > 500:
            continue
        try:
            urllib.request.urlretrieve(url, p)
            print(f"  загружен {fn}")
        except OSError as e:
            print(f"  не удалось загрузить {fn}: {e}")

# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #
class H(BaseHTTPRequestHandler):
    server_version = "nas-web"
    def log_message(self, *a):  # тихо
        pass

    def _json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    # ---- аутентификация ----
    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nasauth":
                return v
        return ""

    def _client_ip(self):
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        try:
            return self.client_address[0]
        except (AttributeError, IndexError):
            return ""

    def _authed(self):
        return session_valid(self._cookie_token())

    def _origin_ok(self):
        """Защита от CSRF и cross-site WebSocket: Origin (если прислан) должен
        совпадать с Host. Запросы без Origin (curl) пропускаем — сессия всё равно нужна."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "").strip()

    def _session_cookie(self, tok=None):
        if tok is None:
            return "nasauth=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        return "nasauth=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax" % (tok, SESSION_TTL)

    def _auth_endpoints(self, p):
        """Открытые ручки /api/auth/*. Возвращает True, если запрос обработан."""
        if p == "/api/auth/state":
            self._json({"configured": auth_configured(), "authed": self._authed()})
        elif p == "/api/auth/login":
            b = self._body()
            if _login_fail["n"] >= 5 and time.time() - _login_fail["t"] < 3.0:
                self._json({"ok": False, "log": "слишком много попыток, подождите"}, 429)
            elif auth_configured() and auth_check_password(b.get("password", "")):
                _login_fail["n"] = 0
                ip = self._client_ip()
                if ip and ip not in _known_ips():         # вход с нового адреса
                    _remember_ip(ip)
                    threading.Thread(target=mon_notify, args=("panel_new:" + ip,
                        "NAS: вход в панель с нового адреса", "Успешный вход с %s" % ip, "panel_new"),
                        daemon=True).start()
                self._json({"ok": True}, cookie=self._session_cookie(session_new()))
            else:
                _login_fail["n"] += 1
                _login_fail["t"] = time.time()
                if _login_fail["n"] >= load_monitor().get("events", {}).get("panel_fail", {}).get("threshold", 5):
                    threading.Thread(target=mon_notify, args=("panel_fail",
                        "NAS: подбор пароля к панели", "%d неудачных попыток входа (последняя с %s)"
                        % (_login_fail["n"], self._client_ip() or "?"), "panel_fail"), daemon=True).start()
                time.sleep(0.5)     # притормозить перебор
                self._json({"ok": False, "log": "неверный пароль"}, 403)
        elif p == "/api/auth/setup":
            # первичная установка пароля; со активной сессией — смена пароля
            if auth_configured() and not self._authed():
                self._json({"error": "auth"}, 401)
            else:
                r = auth_set_password(self._body().get("password", ""))
                if r.get("ok"):
                    self._json(r, cookie=self._session_cookie(session_new()))
                else:
                    self._json(r, 400)
        elif p == "/api/auth/logout":
            session_drop(self._cookie_token())
            self._json({"ok": True}, cookie=self._session_cookie(None))
        else:
            return False
        return True

    def _stream_engine(self, action, params, dry):
        """Запустить движок и стримить stdout построчно (для живого лога в мастере)."""
        if not ENGINE_ACTION_RE.match(action or ""):
            self._json({"error": "недопустимое действие"}, 400); return
        env = _engine_env(params, dry)   # ValueError уйдёт наружу -> 400
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            p = subprocess.Popen(["bash", ENGINE, "api", action], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
        except OSError as e:
            self.wfile.write(("ошибка запуска: %s\n__EXIT__1\n" % e).encode()); return
        for line in iter(p.stdout.readline, ""):
            try:
                self.wfile.write(line.encode()); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                p.kill(); break
        p.wait()
        try:
            self.wfile.write(("__EXIT__%d\n" % p.returncode).encode()); self.wfile.flush()
        except OSError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _static(self, path):
        if path == "/" or path == "":
            path = "/desktop.html"
        rel = path.lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        root = os.path.realpath(WEB_DIR)
        if (full != root and not full.startswith(root + os.sep)) or not os.path.isfile(full):
            self.send_error(404); return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "application/javascript", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon",
                 ".webmanifest": "application/manifest+json", ".json": "application/json",
                 ".webp": "image/webp"}.get(
                     os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # HTML/JS/CSS всегда ревалидировать — иначе браузер показывает старую версию после правок
        if os.path.splitext(full)[1] in (".html", ".js", ".css"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sendraw(self, path, download=False):
        if not os.path.isfile(path):
            self.send_error(404); return
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(500); return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        # HTTP Range → перемотка видео/аудио работает + стрим без загрузки файла в память
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1 == "" and g2:
                    start = max(0, size - int(g2))
                else:
                    start = int(g1) if g1 else 0
                    end = int(g2) if g2 else size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers(); return
                end = min(end, size - 1)
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if download:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % os.path.basename(path))
        self.send_header("Content-Length", str(length))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass   # браузер перемотал/закрыл — норма
        except OSError:
            pass

    def _send_thumb(self, src):
        src = os.path.realpath(src or "")
        tp = gen_thumb(src)
        if tp and os.path.isfile(tp):
            self._sendraw(tp)
        else:
            self.send_error(404)

    def _transcode(self):
        """Потоковый транскод в browser-friendly mp4 (для HEVC/экзотики). t = старт, сек."""
        q = parse_qs(urlparse(self.path).query)
        src = os.path.realpath((q.get("path") or [""])[0])
        try:
            t = max(0.0, float((q.get("t") or ["0"])[0]))
        except ValueError:
            t = 0.0
        if not os.path.isfile(src) or not shutil.which("ffmpeg"):
            self.send_error(404); return
        vcodec = ""
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1", src],
                                capture_output=True, text=True, timeout=10)
            vcodec = (pr.stdout or "").strip()
        except Exception:
            pass
        # h264 уже поддержан браузером — только ремукс (быстро, без нагрузки); иначе перекодируем
        if vcodec == "h264":
            vargs = ["-c:v", "copy"]
        else:
            vargs = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                     "-vf", "scale='min(1280,iw)':-2", "-pix_fmt", "yuv420p"]
        cmd = ["ffmpeg", "-v", "error"]
        if t > 0:
            cmd += ["-ss", "%.3f" % t]
        cmd += ["-i", src] + vargs + ["-c:a", "aac", "-b:a", "128k", "-ac", "2",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            self.send_error(500); return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass

    def _upload_raw(self):
        """Потоковая бинарная загрузка (без base64 — не роняет вкладку на больших файлах)."""
        q = parse_qs(urlparse(self.path).query)
        d = os.path.realpath((q.get("path") or ["/"])[0])
        name = os.path.basename(((q.get("name") or [""])[0]).strip())
        if not os.path.isdir(d):
            return {"ok": False, "log": "не каталог назначения"}
        if not name:
            return {"ok": False, "log": "нет имени файла"}
        dest = _uniq(os.path.join(d, name))
        n = int(self.headers.get("Content-Length", 0) or 0)
        got = 0
        try:
            with open(dest, "wb") as f:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(262144, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    got += len(chunk)
        except OSError as e:
            return {"ok": False, "log": str(e)}
        if thumb_kind(name):
            threading.Thread(target=gen_thumb, args=(dest,), daemon=True).start()
        return {"ok": True, "path": dest, "size": got}

    def _send_zip(self, items, name):
        import zipfile, tempfile
        items = [os.path.realpath(i) for i in items if i]
        if not items:
            self.send_error(400); return
        if not name.endswith(".zip"):
            name += ".zip"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for it in items:
                    if os.path.isdir(it):
                        base = os.path.dirname(it)
                        for root, _, files in os.walk(it):
                            for f in files:
                                fp = os.path.join(root, f)
                                try:
                                    z.write(fp, os.path.relpath(fp, base))
                                except OSError:
                                    pass
                    elif os.path.isfile(it):
                        z.write(it, os.path.basename(it))
            tmp.close()
            with open(tmp.name, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(500)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _ws_terminal(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400); return
        self.wfile.flush()
        self.connection.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
             "Connection: Upgrade\r\nSec-WebSocket-Accept: " + _ws_accept(key) + "\r\n\r\n").encode())
        sock = self.connection
        ex = (parse_qs(urlparse(self.path).query).get("exec") or [""])[0]
        ex = ex if re.match(r"^[a-zA-Z0-9_.-]+$", ex or "") else ""
        pid, master = pty.fork()
        if pid == 0:                       # ребёнок -> bash или docker exec
            os.environ["TERM"] = "xterm-256color"
            if ex:                         # exec в контейнер — остаёмся root (нужен доступ к docker.sock)
                os.execvp("docker", ["docker", "exec", "-it", ex, "sh", "-c",
                                     "command -v bash >/dev/null && exec bash || exec sh"])
                os._exit(1)
            try:
                if os.geteuid() == 0:      # если сервер root — уронить права до пользователя
                    u = pwd.getpwnam(TARGET_USER)
                    os.setgid(u.pw_gid); os.setuid(u.pw_uid)
                    os.environ.update(HOME=u.pw_dir, USER=TARGET_USER, LOGNAME=TARGET_USER)
            except (KeyError, OSError):
                pass
            os.chdir(os.environ.get("HOME", "/"))
            os.execvp("bash", ["bash", "-l"])
            os._exit(1)
        try:
            while True:
                r, _, _ = select.select([master, sock], [], [], 300)
                if not r:
                    break
                if master in r:
                    try:
                        data = os.read(master, 8192)
                    except OSError:
                        break
                    if not data:
                        break
                    _ws_send(sock, data, 0x2)
                if sock in r:
                    fr = _ws_recv(sock)
                    if fr is None:
                        break
                    op, pay = fr
                    if op == 0x8:
                        break
                    elif op == 0x9:
                        _ws_send(sock, pay, 0xA)
                    elif op in (0x1, 0x2):
                        if pay[:1] == b"\x01":          # управляющее: resize
                            try:
                                d = json.loads(pay[1:].decode())
                                _set_winsize(master, int(d["rows"]), int(d["cols"]))
                            except (ValueError, KeyError):
                                pass
                        else:
                            os.write(master, pay)
        finally:
            try: os.close(master)
            except OSError: pass
            try: os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            except OSError: pass

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        p = u.path
        if p == "/ws/term" and self.headers.get("Upgrade", "").lower() == "websocket":
            if not (self._origin_ok() and self._authed()):
                self.send_error(403); return
            try:
                self._ws_terminal()
            except (OSError, BrokenPipeError):
                pass
            return
        if p == "/api/auth/state":
            self._auth_endpoints(p); return
        if p.startswith("/api/") and not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        try:
            if p == "/api/stats":
                self._json(stats())
            elif p == "/api/history":
                self._json({"history": _load_history()})
            elif p == "/api/health":
                self._json(health_report())
            elif p == "/api/desktop":
                self._json({"apps": discover_desktop_apps(), "volumes": external_volumes()})
            elif p == "/api/cron/jobs":
                self._json(cron_jobs())
            elif p == "/api/cron/stats":
                self._json(cron_stats())
            elif p == "/api/cron/scripts":
                self._json(cron_scripts())
            elif p == "/api/cron/logs":
                self._json(cron_logs((q.get("runId") or [""])[0],
                                     (q.get("offset") or ["0"])[0], (q.get("maxLines") or ["500"])[0]))
            elif p == "/api/fs/list":
                r = fs_list((q.get("path") or ["/"])[0])
                if r.get("ok"):
                    try: prewarm_thumbs(r["path"])
                    except Exception: pass
                self._json(r)
            elif p == "/api/fs/read":
                self._json(fs_read((q.get("path") or [""])[0]))
            elif p == "/api/fs/raw":
                self._sendraw(os.path.realpath((q.get("path") or [""])[0]),
                              (q.get("dl") or ["0"])[0] == "1")
            elif p == "/api/fs/thumb":
                self._send_thumb((q.get("path") or [""])[0])
            elif p == "/api/fs/vmeta":
                self._json(video_meta((q.get("path") or [""])[0]))
            elif p == "/api/fs/transcode":
                self._transcode()
            elif p == "/api/fs/fetch/status":
                self._json(fs_fetch_status((q.get("id") or [""])[0]))
            elif p == "/api/fm/favorites":
                self._json({"favorites": load_favs()})
            elif p == "/api/fs/search":
                self._json(fs_search((q.get("path") or ["/"])[0], (q.get("q") or [""])[0]))
            elif p == "/api/fs/stat":
                self._json(fs_stat((q.get("path") or [""])[0]))
            elif p == "/api/fs/du":
                self._json(fs_du((q.get("path") or [""])[0]))
            elif p == "/api/fs/grep":
                self._json(fs_grep((q.get("path") or ["/"])[0], (q.get("q") or [""])[0]))
            elif p == "/api/fs/trash":
                self._json(fs_trash_list())
            elif p == "/api/fs/zip":
                self._send_zip(q.get("item") or [], (q.get("name") or ["archive.zip"])[0])
            elif p == "/api/disks":
                self._json({"disks": disks(), "fs": fs_tools(), "snapraid": snapraid_status()})
            elif p == "/api/disk/smart":
                self._json(smart_detail((q.get("dev") or [""])[0]))
            elif p == "/api/processes":
                self._json(processes((q.get("sort") or ["cpu"])[0]))
            elif p == "/api/systemd":
                self._json(systemd_units((q.get("type") or ["service"])[0]))
            elif p == "/api/systemd/journal":
                self._json(systemd_journal((q.get("unit") or [""])[0], (q.get("lines") or ["200"])[0]))
            elif p == "/api/monitor":
                self._json({"monitor": load_monitor(), "notify": load_notify()})
            elif p == "/api/unit":
                self._json(unit_read((q.get("name") or [""])[0]))
            elif p == "/api/stacks":
                self._json(docker_stacks())
            elif p == "/api/stack":
                self._json(stack_read((q.get("name") or [""])[0]))
            elif p == "/api/stack/logs":
                self._json(stack_logs((q.get("name") or [""])[0], (q.get("tail") or ["200"])[0]))
            elif p == "/api/stack/validate":
                self._json(stack_validate((q.get("name") or [""])[0]))
            elif p == "/api/docker/stats":
                self._json(docker_stats())
            elif p == "/api/docker/images":
                self._json(docker_images())
            elif p == "/api/docker/volumes":
                self._json(docker_volumes())
            elif p == "/api/automount":
                self._json(automount_state())
            elif p == "/api/sysconf":
                self._json(sysconf())
            elif p == "/api/usb-import":
                self._json({"config": usb_import_load(), "drives": usb_removable()})
            elif p == "/api/wallpaper/img":
                wp = _wallpaper_path()
                if wp:
                    self._sendraw(wp)
                else:
                    self.send_error(404)
            elif p == "/api/settings":
                self._json({"settings": load_settings()})
            elif p == "/api/winpos":
                self._json({"winpos": load_winpos()})
            elif p == "/api/snippets":
                self._json({"snippets": load_snippets()})
            elif p == "/api/creds":
                self._json({"creds": load_creds()})
            elif p == "/api/setup/state":
                self._json(engine("state"))
            elif p.startswith("/api/"):
                self._json({"error": "unknown endpoint"}, 404)
            else:
                self._static(p)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # не роняем сервер
            self._json({"error": repr(e)}, 500)

    # ---- POST ----
    def do_POST(self):
        p = urlparse(self.path).path
        if not self._origin_ok():
            self._json({"error": "origin mismatch"}, 403); return
        if p.startswith("/api/auth/"):
            if not self._auth_endpoints(p):
                self._json({"error": "unknown endpoint"}, 404)
            return
        if not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        try:
            if p == "/api/creds":
                b = self._body(); save_creds(b.get("creds", []))
                self._json({"ok": True})
            elif p == "/api/settings":
                b = self._body(); save_settings(b.get("settings", {}))
                self._json({"ok": True})
            elif p == "/api/winpos":
                b = self._body(); save_winpos(b.get("winpos", {}))
                self._json({"ok": True})
            elif p == "/api/sysconf":
                b = self._body()
                self._json(sysconf_set(b.get("key", ""), b.get("value"), b.get("extra")))
            elif p == "/api/usb-import":
                self._json(usb_import_save(self._body()))
            elif p == "/api/usb-import/run":
                self._json(usb_import_run(self._body().get("dev", "")))
            elif p == "/api/wallpaper/fetch":
                self._json(wallpaper_fetch(self._body().get("url", "")))
            elif p == "/api/wallpaper/upload":
                self._json(wallpaper_upload(self._body().get("data", "")))
            elif p == "/api/snippets":
                b = self._body(); save_snippets(b.get("snippets", []))
                self._json({"ok": True})
            elif p == "/api/power":
                self._json(power(self._body().get("action", "")))
            elif p == "/api/process/kill":
                b = self._body(); self._json(kill_process(b.get("pid"), b.get("signal", 15)))
            elif p == "/api/process/renice":
                b = self._body(); self._json(renice(b.get("pid"), b.get("nice", 0)))
            elif p == "/api/systemd":
                b = self._body(); self._json(systemd_action(b.get("unit", ""), b.get("action", "")))
            elif p == "/api/monitor":
                b = self._body()
                if b.get("test"):
                    ok = push_notify("NAS: тест", "Проверка уведомлений с панели управления")
                    self._json({"ok": ok, "log": "" if ok else "Pushover не настроен: заполните ключи"})
                else:
                    out = {}
                    if isinstance(b.get("notify"), dict):
                        save_notify(b["notify"].get("user", ""), b["notify"].get("token", ""))
                        out["notify"] = load_notify()
                    if isinstance(b.get("monitor"), dict):
                        out["monitor"] = save_monitor(b["monitor"])
                    self._json(out or {"ok": True})
            elif p == "/api/unit/save":
                b = self._body(); self._json(unit_write(b.get("name", ""), b.get("content", "")))
            elif p == "/api/unit/create":
                b = self._body(); self._json(unit_write(b.get("name", ""), b.get("content", ""), True))
            elif p == "/api/unit/delete":
                b = self._body(); self._json(unit_delete(b.get("name", "")))
            elif p == "/api/stack/save":
                b = self._body(); self._json(stack_save(b.get("name", ""), b.get("compose", ""), b.get("env"), b.get("create", False)))
            elif p == "/api/stack/create":
                b = self._body(); self._json(stack_save(b.get("name", ""), b.get("compose", ""), b.get("env"), True))
            elif p == "/api/stack/action":
                b = self._body(); self._json(stack_action(b.get("name", ""), b.get("action", "")))
            elif p == "/api/stack/note":
                b = self._body(); self._json(save_stack_note(b.get("name", ""), b.get("note", "")))
            elif p == "/api/docker/prune":
                b = self._body(); self._json(docker_prune(b.get("what", "")))
            elif p == "/api/docker/image/rm":
                b = self._body(); self._json(docker_image_rm(b.get("id", "")))
            elif p == "/api/docker/volume/rm":
                b = self._body(); self._json(docker_volume_rm(b.get("name", "")))
            elif p == "/api/stack/delete":
                b = self._body(); self._json(stack_delete(b.get("name", "")))
            elif p == "/api/container/action":
                b = self._body(); self._json(container_action(b.get("id", ""), b.get("action", "")))
            elif p == "/api/disk/mount":
                b = self._body(); self._json(disk_mount(b.get("target", ""), b.get("unmount", False)))
            elif p == "/api/disk/smart-test":
                b = self._body(); self._json(smart_test(b.get("dev", ""), b.get("kind", "short")))
            elif p == "/api/disk/speedtest":
                b = self._body(); self._json(disk_speedtest(b.get("dev", "")))
            elif p == "/api/disk/eject":
                b = self._body(); self._json(disk_eject(b.get("dev", "")))
            elif p == "/api/disk/spindown":
                b = self._body(); self._json(disk_spindown(b.get("dev", ""), b.get("minutes", 0)))
            elif p == "/api/disk/label":
                b = self._body(); dev = b.get("dev", ""); label = b.get("label", "")
                if not re.match(r"^/dev/[\w-]+$", dev or ""):
                    self._json({"ok": False, "log": "недопустимое устройство"}, 400)
                elif not re.match(r"^[A-Za-z0-9._-]{1,16}$", label or ""):
                    self._json({"ok": False, "log": "недопустимая метка (латиница/цифры/._-, до 16)"}, 400)
                else:
                    self._json(engine("label-disk", {"dev": dev, "label": label}))
            elif p == "/api/disk/format":
                b = self._body(); dev = b.get("dev", ""); label = b.get("label", "")
                if not re.match(r"^/dev/[\w-]+$", dev or ""):
                    self._json({"ok": False, "log": "недопустимое устройство"}, 400)
                elif b.get("role", "data") not in ("data", "parity", "usb"):
                    self._json({"ok": False, "log": "недопустимая роль"}, 400)
                elif b.get("fs", "ext4") not in ("ext4", "xfs", "btrfs", "exfat", "ntfs", "vfat"):
                    self._json({"ok": False, "log": "недопустимая ФС"}, 400)
                elif label and not re.match(r"^[A-Za-z0-9._-]{1,16}$", label):
                    self._json({"ok": False, "log": "недопустимая метка (латиница/цифры/._-, до 16)"}, 400)
                else:
                    self._json(engine("format-disk", {"dev": dev, "role": b.get("role", "data"),
                        "fs": b.get("fs", "ext4"), "label": label}, dry=b.get("dry", False)))
            elif p == "/api/disk/mount-dev":
                b = self._body(); dev = b.get("dev", ""); target = (b.get("target") or "").strip()
                if not re.match(r"^/dev/[\w-]+$", dev or ""):
                    self._json({"ok": False, "log": "недопустимое устройство"}, 400)
                elif target and (not re.match(r"^/[A-Za-z0-9._/ -]{1,120}$", target) or ".." in target
                                 or target in ("/", "/etc", "/usr", "/bin", "/boot", "/home", "/var", "/root")):
                    self._json({"ok": False, "log": "недопустимая точка монтирования"}, 400)
                else:
                    params = {"dev": dev}
                    if target:
                        params["target"] = target
                    self._json(engine("mount-dev", params))
            elif p == "/api/automount":
                b = self._body()
                user = b.get("user", TARGET_USER); base = b.get("base", "/media/nas")
                if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user or ""):
                    self._json({"ok": False, "log": "недопустимое имя пользователя"}, 400)
                elif not re.match(r"^/[A-Za-z0-9._/-]{1,120}$", base or "") or ".." in base:
                    self._json({"ok": False, "log": "недопустимый базовый каталог"}, 400)
                else:
                    self._json(engine("automount", {"enable": "1" if b.get("enable", True) else "0",
                        "user": user, "base": base}))
            elif p == "/api/homepage/config":
                self._json(write_homepage_config(self._body().get("host")))
            elif p == "/api/fs/write":
                b = self._body(); self._json(fs_write(b.get("path", ""), b.get("content", "")))
            elif p == "/api/fs/mkdir":
                b = self._body(); self._json(fs_mkdir(b.get("path", ""), b.get("name", "")))
            elif p == "/api/fs/rename":
                b = self._body(); self._json(fs_rename(b.get("path", ""), b.get("name", "")))
            elif p == "/api/fs/delete":
                b = self._body(); self._json(fs_delete(b.get("path", "")))
            elif p == "/api/fs/fetch":
                b = self._body(); self._json(fs_fetch_start(b.get("path", ""), b.get("url", ""), b.get("name", "")))
            elif p == "/api/fs/upload-raw":
                self._json(self._upload_raw())
            elif p == "/api/fs/upload":
                b = self._body(); ur = fs_upload(b.get("path", ""), b.get("name", ""), b.get("data", ""))
                if ur.get("ok") and thumb_kind(os.path.basename(ur.get("path",""))):
                    threading.Thread(target=gen_thumb, args=(ur["path"],), daemon=True).start()
                self._json(ur)
            elif p == "/api/fm/favorites":
                b = self._body(); save_favs(b.get("favorites", [])); self._json({"ok": True})
            elif p == "/api/fs/newfile":
                b = self._body(); self._json(fs_newfile(b.get("path", ""), b.get("name", "")))
            elif p == "/api/fs/copy":
                b = self._body(); self._json(fs_copy(b.get("src", ""), b.get("dest", "")))
            elif p == "/api/fs/move":
                b = self._body(); self._json(fs_move(b.get("src", ""), b.get("dest", "")))
            elif p == "/api/fs/chmod":
                b = self._body(); self._json(fs_chmod(b.get("path", ""), b.get("mode", ""), b.get("recursive", False)))
            elif p == "/api/fs/chown":
                b = self._body(); self._json(fs_chown(b.get("path", ""), b.get("owner", ""), b.get("group", ""), b.get("recursive", False)))
            elif p == "/api/fs/unzip":
                b = self._body(); self._json(fs_unzip(b.get("path", ""), b.get("dest", "")))
            elif p == "/api/fs/archive":
                b = self._body(); self._json(fs_archive(b.get("items", []), b.get("dest", ""), b.get("name", "")))
            elif p == "/api/fs/trash":
                b = self._body(); self._json(fs_trash(b.get("path", "")))
            elif p == "/api/fs/trash/restore":
                b = self._body(); self._json(fs_trash_restore(b.get("id", "")))
            elif p == "/api/fs/trash/delete":
                b = self._body(); self._json(fs_trash_delete(b.get("id", "")))
            elif p == "/api/fs/trash/empty":
                self._body(); self._json(fs_trash_empty())
            elif re.match(r"^/api/cron/job/[\w.\-:]+/(run|delete)$", p):
                _, _, _, _, jid, act = p.split("/")
                self._json(cron_run(jid) if act == "run" else cron_delete(jid))
            elif re.match(r"^/api/cron/job/[\w.\-:]+$", p):
                jid = p.rsplit("/", 1)[1]
                self._json(cron_update(jid, self._body()))
            elif p.startswith("/api/setup/"):
                action = p[len("/api/setup/"):]
                b = self._body()
                self._stream_engine(action, b.get("params", {}), b.get("dry", False))
            else:
                self._json({"error": "unknown endpoint"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": repr(e)}, 500)


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    ensure_web_assets()
    try:
        apply_spindown_all()          # восстановить настройки сна дисков после старта/ребута
    except Exception:
        pass
    threading.Thread(target=monitor_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    ip = lan_ip()
    print(f"nas-web запущен:  http://{ip}:{PORT}   (http://{socket.gethostname()}.local:{PORT})")
    print(f"  web/     : {WEB_DIR}")
    print(f"  services : {SERVICES}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "thumbs-sweep":
        print("thumbs-sweep: сгенерировано", thumbs_sweep(sys.argv[2:]), "превью")
    else:
        main()
