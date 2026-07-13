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
import json, os, re, subprocess, time, shutil, socket, threading, pwd, mimetypes, glob, errno, heapq, sys, sqlite3, fnmatch
import html as _htmllib
import pty, select, struct, hashlib, base64, signal, fcntl, termios, secrets, hmac, shlex, stat
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
BODY_MAX    = 32 * 1024 * 1024   # потолок JSON-тела запроса (обои/base64 влезают; больше — через _upload_raw)
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

def _json_save(path, obj, indent=None):
    """Write JSON through a temp file + rename. A plain open("w") truncates the file
    first, so a power cut mid-write leaves valid-looking garbage — and every loader
    here answers a parse error with silent defaults, quietly wiping the user's
    settings. Rename is atomic, so the old file survives until the new one is whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

_BAD_CONFIGS = []      # basenames of corrupt config files, drained by monitor_tick

def _json_load_strict(path, default):
    """Missing file → default (normal). Corrupt file → keep it aside as .bad and
    queue a warning: silently falling back to defaults is how settings appear to
    'turn themselves off'. Reporting is deferred to monitor_tick because log_event
    reads monitor.json through this very function — logging here would recurse."""
    try:
        with open(path) as f:
            return json.load(f)
    except OSError:
        return default
    except ValueError:
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        name = os.path.basename(path)
        if name not in _BAD_CONFIGS:
            _BAD_CONFIGS.append(name)
        sys.stderr.write("nas-web: %s повреждён, сохранён как %s.bad\n" % (name, name))
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
    free  = s.f_bavail * s.f_frsize                  # доступно (как df Avail; БЕЗ ext4-резерва)
    used  = (s.f_blocks - s.f_bfree) * s.f_frsize    # реально занято данными (резерв ≠ «занято»)
    denom = used + free                              # база для % как в df (без учёта резерва)
    return {"path": path, "total": total, "used": used, "free": free,
            "pct": round(100 * used / denom, 1) if denom else 0}

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

# --------------------------------------------------------------------------- #
#  LAN quality — is the negotiated link speed the REAL ceiling?
#  A gigabit NIC says nothing about what sits between us and the rest of the LAN.
#  Real case (2026-07-12): the Pi was cabled into a GL.iNet box that bridged to the
#  home network over Wi-Fi. eth0 negotiated 1000 Mb/s, yet a backup pull from the
#  other NAS (gigabit too) never passed ~38 MB/s, LAN RTT was 5-30 ms and a second
#  parallel stream added nothing — the radio hop was the ceiling, not the disks.
#  Two tells, both free (no traffic needed):
#    * RTT to the gateway: a real switch answers well under a millisecond;
#    * proxy-ARP: a bridging repeater answers ARP for every remote host with its OWN
#      MAC, so several LAN addresses end up sharing the gateway's MAC.
# --------------------------------------------------------------------------- #
LAN_RTT_BAD = 2.5                              # ms — above this a wired LAN has a hop in it
_LAN_CACHE = {"t": 0.0, "d": None}

def _gateway_ip():
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[1] == "00000000" and f[2] != "00000000":
            g = int(f[2], 16)                  # little-endian hex, as the kernel prints it
            return "%d.%d.%d.%d" % (g & 0xFF, (g >> 8) & 0xFF, (g >> 16) & 0xFF, (g >> 24) & 0xFF)
    return ""

def _arp_relay(gw, iface):
    """How many OTHER hosts answer with the gateway's MAC (proxy-ARP = we sit behind
    a bridge/repeater, not a switch). 0 → clean L2."""
    mac, shared = "", 0
    rows = []
    for line in _read("/proc/net/arp").splitlines()[1:]:
        f = line.split()
        if len(f) < 6 or f[2] == "0x0" or f[5] != iface:   # 0x0 = incomplete entry
            continue
        rows.append((f[0], f[3].lower()))
        if f[0] == gw:
            mac = f[3].lower()
    if not mac or mac in ("00:00:00:00:00:00",):
        return 0
    for ip, m in rows:
        if m == mac and ip != gw:
            shared += 1
    return shared

def lan_quality(is_wifi, ttl=120):
    """Отклик до роутера + признак «мы за мостом/репитером». Кэш: пинг раз в 2 минуты."""
    now = time.time()
    if _LAN_CACHE["d"] is not None and now - _LAN_CACHE["t"] < ttl:
        return _LAN_CACHE["d"]
    gw, iface = _gateway_ip(), default_iface() or ""
    d = {"gw": gw, "rtt": None, "relay": 0, "slow": False}
    if gw:
        try:
            out = subprocess.run(["ping", "-qc", "4", "-i", "0.2", "-W", "1", gw],
                                 capture_output=True, text=True, timeout=8).stdout
            m = re.search(r"=\s*[\d.]+/([\d.]+)/", out)
            if m:
                d["rtt"] = round(float(m.group(1)), 1)
        except (OSError, subprocess.SubprocessError):
            pass
        d["relay"] = _arp_relay(gw, iface)
    # Wi-Fi is expected to answer in milliseconds — only a CABLE promises a clean path,
    # so the "your link speed is a lie" verdict is for wired links only.
    d["slow"] = bool(not is_wifi and d["rtt"] is not None and d["rtt"] > LAN_RTT_BAD)
    _LAN_CACHE.update(t=now, d=d)
    return d

def net_info():
    """Тип активного подключения (Wi-Fi/кабель), скорость линка (Мбит/с),
    для Wi-Fi — SSID/диапазон/сигнал. Чтобы наглядно видеть эффект кабеля."""
    iface = default_iface()
    info = {"iface": iface or "", "ip": lan_ip(), "type": "", "ssid": "",
            "band": "", "signal": None, "signal_pct": None, "link_mbit": None}
    if not iface:
        return info
    is_wifi = os.path.isdir("/sys/class/net/%s/wireless" % iface) or iface.startswith(("wl", "wlan"))
    if is_wifi:
        info["type"] = "wifi"
        try:
            out = subprocess.run(["iw", "dev", iface, "link"],
                                 capture_output=True, text=True, timeout=4).stdout
            m = re.search(r"SSID:\s*(.+)", out)
            if m:
                info["ssid"] = m.group(1).strip()
            m = re.search(r"freq:\s*(\d+)", out)
            if m:
                fr = int(m.group(1)); info["band"] = "5 ГГц" if fr >= 5000 else "2.4 ГГц"
            m = re.search(r"signal:\s*(-?\d+)", out)
            if m:
                sig = int(m.group(1)); info["signal"] = sig
                info["signal_pct"] = max(0, min(100, 2 * (sig + 100)))   # ~ -100..-50 dBm → 0..100
            m = re.search(r"tx bitrate:\s*([\d.]+)", out)
            if m:
                info["link_mbit"] = int(float(m.group(1)))
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        info["type"] = "eth"
        sp = _read("/sys/class/net/%s/speed" % iface, "").strip()
        try:
            if sp and int(sp) > 0:
                info["link_mbit"] = int(sp)
        except ValueError:
            pass
    info["lan"] = lan_quality(is_wifi)
    return info

def net_speedtest():
    """Мини-спидтест download+upload через Cloudflare. Крутится ~8-10 c ради точности."""
    out = {"ok": False}
    # --- DOWNLOAD: тянем до ~10 c; считаем по факту скачанного (даже если поток
    #     оборвался — Cloudflare может закрыть соединение после части объёма) ---
    n = 0.0; t0 = time.time()
    try:
        # OVH (Европа) — Cloudflare в Испании часто заблокирован (LaLiga)
        req = urllib.request.Request("https://proof.ovh.net/files/1Gb.dat",
                                     headers={"User-Agent": "nas-os"})
        with urllib.request.urlopen(req, timeout=40) as r:
            while time.time() - t0 < 10:
                chunk = r.read(262144)
                if not chunk:
                    break
                n += len(chunk)
    except Exception as e:
        if n < 1000000:
            out["log"] = ("нет интернета?" if "URLError" in str(type(e)) else str(e)[:100])
    dt = max(0.1, time.time() - t0)
    if n >= 1000000:
        out["ok"] = True
        out["down_MBs"] = round(n / dt / 1048576, 1)
        out["down_mbps"] = round(n * 8 / dt / 1e6, 1)
    # --- UPLOAD: шлём фиксированный объём, замеряем время ---
    try:
        total = 60 * 1024 * 1024   # 60 МБ
        data = b"\0" * total
        req = urllib.request.Request("http://speedtest.tele2.net/upload.php", data=data,
                                     headers={"User-Agent": "nas-os", "Content-Type": "application/octet-stream"})
        t0 = time.time()
        urllib.request.urlopen(req, timeout=40).read()
        dt = max(0.1, time.time() - t0)
        out["up_MBs"] = round(total / dt / 1048576, 1)
        out["up_mbps"] = round(total * 8 / dt / 1e6, 1)
        out["ok"] = True
    except Exception:
        pass
    return out

def uptime_s():
    try:
        return int(float(_read("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return 0

# get_throttled: bits 0-3 say "right now", bits 16-19 say "happened since boot".
# Undervoltage and frequency capping are different faults with different fixes, so
# they are reported apart. The flags live in firmware RAM and are wiped when power
# is cut, hence a sag must reach the event log (which survives a reboot) while the
# board is still up: after a brownout-triggered power-off nothing is left to read.
_UV_MASK  = (1 << 0) | (1 << 16)                        # under-voltage
_THR_MASK = sum(1 << b for b in (1, 2, 3, 17, 18, 19))  # freq cap / throttling / soft temp limit

# USB-PD negotiation result, mA: 5000 = official 27 W PSU, 3000 = 15 W (or a weak
# cable that dropped the PD talk) — under 3xNVMe + USB load the PMIC cuts power.
# Static per boot, so read once at import.
def _psu_max_current():
    try:
        with open("/proc/device-tree/chosen/power/max_current", "rb") as f:
            return int.from_bytes(f.read(4), "big") or None
    except OSError:
        return None
PSU_MA = _psu_max_current()

def throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        val = out.split("=")[-1]
        v = int(val, 16)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return {"raw": None, "ok": True, "undervolt": False, "throttle": False}
    return {"raw": val, "ok": v == 0,
            "undervolt": bool(v & _UV_MASK), "throttle": bool(v & _THR_MASK)}

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
COMITUP_CONF = "/etc/comitup.conf"

def comitup_state():
    """comitup — Wi-Fi точка доступа + captive-портал для первичной настройки без
    монитора. Статус берём из лога (D-Bus у comitup подвисает), настройки из conf.
    Не установлен → {installed:false}."""
    out = {"installed": bool(shutil.which("comitup")), "mode": None, "ssid": None,
           "ap_name": "comitup-<nnn>", "ap_password": ""}
    if not out["installed"]:
        return out
    # настройки: раскомментированные ap_name/ap_password
    for l in _read(COMITUP_CONF).splitlines():
        l = l.strip()
        if l.startswith("ap_name:"):
            out["ap_name"] = l.split(":", 1)[1].strip()
        elif l.startswith("ap_password:"):
            out["ap_password"] = l.split(":", 1)[1].strip()
    # режим/SSID из последних строк журнала comitup
    log = _read("/var/log/comitup.log")
    for l in reversed(log.splitlines()[-80:]):
        if "Setting state to" in l and out["mode"] is None:
            out["mode"] = l.rsplit("Setting state to", 1)[1].strip()
        if "Attempting connection to" in l and out["ssid"] is None:
            out["ssid"] = l.rsplit("Attempting connection to", 1)[1].strip()
        if out["mode"] and out["ssid"]:
            break
    return out

def comitup_save(ap_name, ap_password):
    """Записать имя/пароль точки доступа comitup. Пустое поле → строка убирается
    (возврат к дефолту). Пароль 8–63 символа (требование WPA) или пусто (открытая)."""
    if not shutil.which("comitup"):
        return {"ok": False, "log": "comitup не установлен"}
    ap_name = (ap_name or "").strip()
    ap_password = (ap_password or "").strip()
    if ap_password and not (8 <= len(ap_password) <= 63):
        return {"ok": False, "log": "пароль точки доступа: 8–63 символа (или пусто)"}
    try:
        lines = _read(COMITUP_CONF).splitlines()
    except Exception:
        lines = []
    # выкидываем прежние (в т.ч. закомментированные-активные) строки настроек
    keep = [l for l in lines if not re.match(r"^\s*(ap_name|ap_password)\s*:", l)]
    if ap_name:
        keep.append("ap_name: " + ap_name)
    if ap_password:
        keep.append("ap_password: " + ap_password)
    try:
        with open(COMITUP_CONF, "w") as f:
            f.write("\n".join(keep).rstrip("\n") + "\n")
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "log": "сохранено — применится после перезапуска comitup или ребута"}

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
    if not re.match(r"^/dev/[\w-]+$", dev or ""):   # как у соседей: не даём dev вида "-x…" стать флагом
        return {}
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
    # -n standby: НЕ будить спящий диск ради фонового опроса (список/виджет/health).
    # Если диск в standby, smartctl вернёт пусто → диск покажет здоровье/темп как «—».
    j = _smartctl_json(["-n", "standby", "-H", "-A"], dev, timeout=8)
    if not _smart_has_data(j):
        return None
    t = (j.get("temperature") or {}).get("current")
    return {"temp": t if t else None,      # USB-мосты отдают 0 — это «не знаю», а не ноль градусов
            "healthy": (j.get("smart_status") or {}).get("passed"),
            "hours": (j.get("power_on_time") or {}).get("hours")}

def fs_tools():
    """Файловые системы, для которых есть mkfs (что реально можно создать)."""
    return [fs for fs in ("ext4", "xfs", "btrfs", "exfat", "ntfs", "vfat")
            if shutil.which("mkfs." + fs)]

_SZ_MUL = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
def _size_bytes(s):
    """lsblk отдаёт размер строкой («238.8G»). Для выбора главного раздела нужен порядок."""
    m = re.match(r"^\s*([\d.,]+)\s*([BKMGTP])?", str(s or ""))
    if not m:
        return 0
    try:
        return int(float(m.group(1).replace(",", ".")) * _SZ_MUL.get(m.group(2) or "B", 1))
    except ValueError:
        return 0

def disks():
    res = []
    am_base = automount_state().get("base", "/media/nas")
    spin = _load_spindown()
    spd = _speedtest_load()
    scr = scrutiny_state().get("devices", {})   # {} если Scrutiny не установлен
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
        parts = []
        for ch in d.get("children", []) or []:
            cmp = ch.get("mountpoint")
            parts.append({
                "name": ch.get("name"), "path": ch.get("path"), "size": ch.get("size"),
                "fstype": ch.get("fstype"), "label": ch.get("label"),
                "mount": cmp, "mounted": bool(cmp),
                "parttypename": ch.get("parttypename"),
            })
        # Главный раздел — САМЫЙ БОЛЬШОЙ смонтированный, а не первый в списке.
        # У флешки с остатками загрузочного образа первым идёт EFI на 200 МБ, и
        # карточка показывала «197 МБ свободно» вместо честных 239 ГБ.
        mparts = [x for x in parts if x["mount"]]
        main = max(mparts, key=lambda x: _size_bytes(x["size"])) if mparts else None
        primary = (("/" if "/" in mounts else None)
                   or (main["mount"] if main else None)
                   or (mounts[0] if mounts else None))
        # ФС и метка — того же раздела, что и статистика (иначе заголовок от одного,
        # цифры от другого)
        fstype = (main.get("fstype") if main else None) or d.get("fstype")
        label = (main.get("label") if main else None) or d.get("label")
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
            "speedtest": spd.get((d.get("serial") or "").strip() or "\0") or spd.get(d.get("path")),
            "scrutiny": scr.get((d.get("serial") or "").strip()),   # None, если Scrutiny нет/нет данных
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

_C_ENV = dict(os.environ, LC_ALL="C", LANG="C")   # стабильный (английский) вывод утилит для парсинга

def _run(cmd, timeout=40, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or _C_ENV)
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

SPEEDTEST_FILE = os.path.join(NAS_CONFIG, "speedtest.json")
_speed_lock = threading.Lock()

def _speedtest_load():
    try:
        with open(SPEEDTEST_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def _speedtest_key(dev):
    """Ключ сохранённого замера: серийник (не меняется при переименовании sdX) или сам dev."""
    r = _run(["lsblk", "-dno", "SERIAL", dev], timeout=8)
    ser = (r.get("log") or "").strip()
    return ser if r.get("ok") and ser else dev

def _dd_mbps(log):
    m = re.search(r"([\d.,]+)\s*([kMG]?B)/s", log or "")
    if not m:
        return None
    val = float(m.group(1).replace(",", ".")); unit = m.group(2)
    return val * {"B": 1e-6, "kB": 1e-3, "MB": 1, "GB": 1e3}.get(unit, 1)

def disk_speedtest(dev):
    """Тест скорости: последовательное чтение с устройства (мимо кэша) +
    последовательная запись временного файла на смонтированный раздел этого диска
    (файл удаляется). Результат сохраняется и показывается в карточке диска."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "недопустимое устройство"}
    if not os.path.exists(dev):
        return {"ok": False, "log": "нет такого устройства"}
    r = _run(["dd", "if=" + dev, "of=/dev/null", "bs=4M", "count=256", "iflag=direct"], timeout=120)
    read_mbps = _dd_mbps(r.get("log", ""))
    if read_mbps is None:
        return {"ok": False, "log": "не удалось измерить: " + (r.get("log", "")[-120:])}
    # запись: на смонтированную rw-точку с запасом места; для системного диска
    # (нет «своих» точек кроме / и /boot) пишем во временный каталог /var/tmp
    write_mbps = None
    wnote = "запись: нет смонтированного раздела для теста"
    mps = _disk_mountpoints(dev)
    cands = [mp for mp in mps if mp not in _SYS_MPS] or (["/var/tmp"] if "/" in mps else [])
    for mp in cands:
        try:
            st = os.statvfs(mp)
            if st.f_bavail * st.f_frsize < (1 << 30):
                wnote = "запись: мало свободного места для теста"
                continue
        except OSError:
            continue
        tmp = os.path.join(mp, ".nas-speedtest.tmp")
        try:
            w = _run(["dd", "if=/dev/zero", "of=" + tmp, "bs=4M", "count=64",
                      "oflag=direct", "conv=fsync"], timeout=90)
            if not w["ok"]:      # ФС без O_DIRECT (exFAT/NTFS) — повторить через fsync
                w = _run(["dd", "if=/dev/zero", "of=" + tmp, "bs=4M", "count=64",
                          "conv=fsync"], timeout=90)
            write_mbps = _dd_mbps(w.get("log", ""))
            wnote = "" if write_mbps is not None else "запись: не удалось измерить"
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        break
    res = {"read": round(read_mbps, 1),
           "write": round(write_mbps, 1) if write_mbps is not None else None,
           "t": int(time.time())}
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        with _speed_lock:                 # два одновременных теста не должны терять записи
            saved = _speedtest_load()
            saved[_speedtest_key(dev)] = res
            tmp = SPEEDTEST_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(saved, f)
            os.replace(tmp, SPEEDTEST_FILE)
    except OSError:
        pass
    log = "чтение: %.0f МБ/с" % read_mbps
    log += (" · запись: %.0f МБ/с" % write_mbps) if write_mbps is not None else (" · " + wnote)
    return {"ok": True, "read_mbps": res["read"], "write_mbps": res["write"],
            "t": res["t"], "log": log}

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

_health_cache = {"t": 0, "data": None}
_health_lock = threading.Lock()

def health_report():
    """Сводка здоровья с кэшем 60 с (тяжёлая: smartctl/systemctl/disks)."""
    with _health_lock:
        if _health_cache["data"] is not None and time.time() - _health_cache["t"] < 60:
            return _health_cache["data"]
    data = _health_report_build()
    with _health_lock:
        _health_cache["t"] = time.time(); _health_cache["data"] = data
    return data

def _health_report_build():
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
        _json_save(SPINDOWN_FILE, cfg)
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
        pid = int(pid); sig = int(sig)
    except (ValueError, TypeError):
        return {"ok": False, "log": "неверный pid/сигнал"}
    # pid<=1 у os.kill означает «вся группа/все процессы» или init — категорически запрещаем
    if pid <= 1:
        return {"ok": False, "log": "недопустимый pid"}
    if pid == os.getpid():
        return {"ok": False, "log": "нельзя убить сам сервер"}
    try:
        os.kill(pid, sig)
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
        _json_save(CREATED_UNITS, lst)
    except OSError:
        pass

def _track_unit(name, add=True):
    lst = load_created_units()
    if add and name not in lst:
        lst.append(name); _save_created_units(lst)
    elif not add and name in lst:
        lst.remove(name); _save_created_units(lst)

def _units_by_trigger(kind):
    """Имена сервисов, за которыми стоит .timer или .socket — чтобы их «dead»
    показывать как «по расписанию»/«по запросу», а не как поломку."""
    names = set()
    r = _run(["systemctl", "list-units", f"--type={kind}", "--all", "--no-legend",
              "--plain", "--no-pager"], timeout=8)
    for line in (r.get("log") or "").splitlines():
        u = line.split(None, 1)[0] if line.split() else ""
        if u.endswith("." + kind):
            names.add(u.rsplit(".", 1)[0] + ".service")   # foo.timer → foo.service
    return names

def systemd_units(kind="service"):
    r = _run(["systemctl", "list-units", f"--type={kind}", "--all", "--no-legend",
              "--plain", "--no-pager"], timeout=10)
    timers = _units_by_trigger("timer") if kind == "service" else set()
    sockets = _units_by_trigger("socket") if kind == "service" else set()
    out = []
    for line in (r.get("log") or "").splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or not parts[0].endswith("." + kind):
            continue
        # чем «разбудят» неактивный юнит: таймер по расписанию или сокет по запросу
        trig = "timer" if parts[0] in timers else ("socket" if parts[0] in sockets else None)
        out.append({"unit": parts[0], "load": parts[1], "active": parts[2],
                    "sub": parts[3], "desc": parts[4] if len(parts) > 4 else "",
                    "trigger": trig})
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
    _json_save(SNIPPETS_FILE, d, indent=2)

# --------------------------------------------------------------------------- #
#  История операций (загрузки, копирования, USB-импорт).
#  Раньше жила только в localStorage браузера: у телефона была своя, пустая.
#  Клиент шлёт сюда завершённые операции; USB-импорт сервер записывает сам,
#  даже если панель не открыта ни в одном браузере.
# --------------------------------------------------------------------------- #
OPS_HIST_FILE = os.path.join(NAS_CONFIG, "ops-history.json")
OPS_HIST_KEEP = 300
OPS_HIST_TTL  = 30 * 86400
_ops_lock = threading.Lock()
_OPS_STATES = ("done", "err", "cancel")

def _ops_hist_read():
    try:
        with open(OPS_HIST_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []

def _ops_hist_write(items):
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        tmp = OPS_HIST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, OPS_HIST_FILE)
        _chown_user(OPS_HIST_FILE)
    except OSError:
        pass

def _ops_clean(items, now=None):
    now = now or time.time()
    items = [x for x in items if isinstance(x, dict) and (now - (x.get("ts") or 0)) < OPS_HIST_TTL]
    return items[-OPS_HIST_KEEP:]

def ops_hist_list():
    with _ops_lock:
        return _ops_clean(_ops_hist_read())

def ops_hist_add(e):
    """Одна завершённая операция. uid — ключ дедупа (перезапуск панели, два браузера)."""
    if not isinstance(e, dict):
        return {"ok": False, "log": "неверная запись"}
    uid = str(e.get("uid") or "")[:80]
    state = e.get("state")
    if not uid or state not in _OPS_STATES:
        return {"ok": False, "log": "неверная запись"}
    try:
        ts = int(e.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = int(time.time())
    item = {"uid": uid, "state": state, "ts": ts,
            "title": str(e.get("title") or "")[:120],
            "label": str(e.get("label") or "")[:400]}
    with _ops_lock:
        items = _ops_hist_read()
        if any(x.get("uid") == uid for x in items):
            return {"ok": True, "dup": True}
        items.append(item)
        _ops_hist_write(_ops_clean(items))
    return {"ok": True}

def ops_hist_clear():
    with _ops_lock:
        _ops_hist_write([])
    return {"ok": True}

# --------------------------------------------------------------------------- #
#  MySpeed — виджет с последним замером скорости интернета.
#  Ходим за данными сами: браузеру мешал бы CORS, а пароль (если включён)
#  передаётся заголовком и не должен светиться в клиенте.
#  Сервиса нет или он молчит → {"ok": false}, и виджет прячется целиком.
# --------------------------------------------------------------------------- #
_ms_cache = {"t": 0, "data": {"ok": False}}
_ms_lock = threading.Lock()
MYSPEED_TTL = 20

def _myspeed_get(base, path, password, timeout=3):
    req = urllib.request.Request(base.rstrip("/") + path, headers={"Accept": "application/json"})
    if password:
        req.add_header("Password", password)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _myspeed_ok(t):
    """A failed run is flagged with `error` and stored as -1/-1/-1, not as null."""
    if not isinstance(t, dict) or t.get("error"):
        return False
    d = t.get("download")
    return isinstance(d, (int, float)) and not isinstance(d, bool) and d >= 0

def myspeed_state():
    """Последний удачный замер + краткая статистика. Кэш, чтобы виджет не долбил сервис."""
    with _ms_lock:
        if time.time() - _ms_cache["t"] < MYSPEED_TTL:
            return _ms_cache["data"]
    m = load_maintenance()
    base = (m.get("myspeed_url") or "").strip()
    out = {"ok": False}
    if base.startswith(("http://", "https://")):
        pw = m.get("myspeed_password") or ""
        try:
            tests = _myspeed_get(base, "/api/speedtests", pw)
            rows = [x for x in tests if isinstance(x, dict)] if isinstance(tests, list) else []
            if rows:
                # MySpeed отдаёт новые первыми, но полагаться на порядок незачем
                newest = lambda seq: max(seq, key=lambda x: str(x.get("created") or ""),
                                         default=None)
                last = newest(rows)
                # Провалившийся замер записывается как -1/-1/-1 — показывать эти числа
                # нельзя. Цифры берём из последнего удачного, а про сбой говорим отдельно.
                good = newest([x for x in rows if _myspeed_ok(x)]) or {}
                out = {"ok": True, "url": base, "count": len(rows),
                       "download": good.get("download"), "upload": good.get("upload"),
                       "ping": good.get("ping"), "created": good.get("created"),
                       "failed": not _myspeed_ok(last), "failed_at": last.get("created"),
                       "error": last.get("error")}
                try:                       # статистика необязательна — без неё виджет живёт
                    st = _myspeed_get(base, "/api/speedtests/statistics", pw)
                    out["avg"] = {"download": (st.get("download") or {}).get("avg"),
                                  "upload": (st.get("upload") or {}).get("avg"),
                                  "ping": (st.get("ping") or {}).get("avg")}
                except Exception:
                    pass
        except Exception:
            out = {"ok": False}
    with _ms_lock:
        _ms_cache["t"] = time.time(); _ms_cache["data"] = out
    return out

# --------------------------------------------------------------------------- #
#  Автоопределение URL сервиса по имени контейнера и его ВНУТРЕННЕМУ порту.
#  Читаем фактический проброс из живого Docker, поэтому порт в compose можно
#  менять свободно — панель всегда возьмёт актуальный. Контейнера нет или он
#  остановлен → None, и вызывающий просто ничего не показывает (без ошибок).
# --------------------------------------------------------------------------- #
_svc_url_cache = {}
_svc_url_lock = threading.Lock()
SVC_URL_TTL = 30

def docker_service_url(name, internal_port):
    key = (name, internal_port)
    now = time.time()
    with _svc_url_lock:
        c = _svc_url_cache.get(key)
        if c and now - c[0] < SVC_URL_TTL:
            return c[1]
    url = None
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f",
             "{{.State.Running}}\x1f{{json .NetworkSettings.Ports}}", name],
            capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            running, _, ports_json = r.stdout.strip().partition("\x1f")
            if running == "true":
                ports = json.loads(ports_json or "{}") or {}
                for b in (ports.get("%d/tcp" % internal_port) or []):
                    if b.get("HostPort"):
                        url = "http://127.0.0.1:%s" % b["HostPort"]
                        break
    except (OSError, subprocess.SubprocessError, ValueError):
        url = None
    with _svc_url_lock:
        _svc_url_cache[key] = (now, url)
    return url

# --------------------------------------------------------------------------- #
#  Scrutiny — здоровье дисков (device_status), температура и наработка по данным
#  его collector'а. Ключ сопоставления с нашими дисками — серийник. Контейнера
#  нет → {ok:False}, и диски работают как раньше, на прямом SMART.
# --------------------------------------------------------------------------- #
_scrutiny_cache = {"t": 0, "data": {"ok": False}}
_scrutiny_lock = threading.Lock()
SCRUTINY_TTL = 60

def scrutiny_state():
    with _scrutiny_lock:
        if time.time() - _scrutiny_cache["t"] < SCRUTINY_TTL:
            return _scrutiny_cache["data"]
    out = {"ok": False}
    base = docker_service_url("scrutiny", 8080)
    if base:
        try:
            req = urllib.request.Request(base + "/api/summary",
                                         headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            summ = ((j.get("data") or {}).get("summary")) or {}
            by_serial = {}
            for uuid, x in summ.items():
                dev = x.get("device") or {}
                sm = x.get("smart") or {}
                serial = (dev.get("serial_number") or "").strip()
                if serial:
                    by_serial[serial] = {
                        "status": dev.get("device_status", 0),   # 0 = здоров
                        "temp": sm.get("temp"),
                        "power_on_hours": sm.get("power_on_hours"),
                        "uuid": dev.get("scrutiny_uuid") or uuid}
            out = {"ok": True, "url": base, "devices": by_serial}
        except Exception:
            out = {"ok": False}
    with _scrutiny_lock:
        _scrutiny_cache["t"] = time.time(); _scrutiny_cache["data"] = out
    return out

# Понятные имена ключевых SMART-атрибутов Scrutiny (NVMe + SATA) для крупной сводки.
_SCR_NAMES = {
    "percentage_used": ("Износ", "%"), "available_spare": ("Запас блоков", "%"),
    "media_errors": ("Ошибки носителя", ""), "num_err_log_entries": ("Записей в логе ошибок", ""),
    "critical_warning": ("Крит. предупреждений", ""), "unsafe_shutdowns": ("Небезопасных выключений", ""),
    "power_cycles": ("Циклов питания", ""), "power_cycle_count": ("Циклов питания", ""),
    "reallocated_sector_ct": ("Переназначено секторов", ""), "current_pending_sector": ("Ожидающих секторов", ""),
    "offline_uncorrectable": ("Неисправимых секторов", ""), "udma_crc_error_count": ("Ошибок кабеля (CRC)", ""),
    "data_units_written": ("Записано", "TB"), "temperature": ("Температура", "°C"),
}
_SCR_ORDER = ["percentage_used", "available_spare", "data_units_written", "media_errors",
              "reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable",
              "udma_crc_error_count", "unsafe_shutdowns", "power_cycles", "power_cycle_count"]

# Вердикт по каждому показателю (good/warn/bad/info) + человеческая подсказка.
# level(value) → уровень; чистые числа сами по себе не читаются, поэтому объясняем.
_SCR_META = {
    "percentage_used": ("Износ ресурса записи SSD. До ~80% спокойно, ближе к 100% — планируйте замену.",
                        lambda v: "good" if v < 70 else "warn" if v < 90 else "bad"),
    "available_spare": ("Запас резервных блоков SSD. 100% — идеально; падение к 10% — тревога.",
                        lambda v: "good" if v > 20 else "warn" if v > 10 else "bad"),
    "media_errors": ("Неисправимые ошибки носителя. Норма — 0.", lambda v: "good" if v == 0 else "bad"),
    "num_err_log_entries": ("Записей в логе ошибок контроллера. Норма — 0.", lambda v: "good" if v == 0 else "warn"),
    "critical_warning": ("Критические предупреждения NVMe. Норма — 0.", lambda v: "good" if v == 0 else "bad"),
    "reallocated_sector_ct": ("Переназначенные сбойные секторы. Норма — 0; рост — износ поверхности.",
                              lambda v: "good" if v == 0 else "warn"),
    "current_pending_sector": ("Секторы, ждущие переназначения. Норма — 0; ненулевое — плохой признак.",
                               lambda v: "good" if v == 0 else "bad"),
    "offline_uncorrectable": ("Неисправимые секторы. Норма — 0.", lambda v: "good" if v == 0 else "bad"),
    "udma_crc_error_count": ("Ошибки передачи по кабелю SATA — обычно виноват кабель/контакт, а не диск.",
                             lambda v: "good" if v == 0 else "warn"),
    "unsafe_shutdowns": ("Сколько раз диск обесточили без корректного отключения. Не поломка, но много — повод к ИБП.",
                         lambda v: "info"),
    "power_cycles": ("Число включений диска. Информационно.", lambda v: "info"),
    "power_cycle_count": ("Число включений диска. Информационно.", lambda v: "info"),
    "data_units_written": ("Всего записано на диск. Информационно (ресурс TBW зависит от модели).",
                           lambda v: "info"),
    "temperature": ("Текущая температура.", lambda v: "good" if v < 60 else "warn" if v < 70 else "bad"),
}

def _scr_verdict(key, value, status):
    """Уровень показателя: сначала мнение Scrutiny (флаг status), затем наши пороги."""
    hint, lvlfn = _SCR_META.get(key, ("", None))
    if status:                              # Scrutiny сам пометил атрибут проблемным
        return "bad", hint
    if lvlfn is not None and isinstance(value, (int, float)):
        try:
            return lvlfn(value), hint
        except Exception:
            pass
    return "info", hint

def scrutiny_device(serial):
    """Детальные атрибуты одного диска из Scrutiny: износ, запас, ошибки, история
    температуры, помеченные проблемные атрибуты. Нет Scrutiny/данных → {ok:False}."""
    dev = scrutiny_state().get("devices", {}).get((serial or "").strip())
    base = docker_service_url("scrutiny", 8080)
    if not dev or not base:
        return {"ok": False}
    try:
        req = urllib.request.Request(base + "/api/device/%s/details" % dev["uuid"],
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = (json.loads(r.read().decode("utf-8", "replace")).get("data")) or {}
        results = data.get("smart_results") or []
        if not results:
            return {"ok": False}
        latest = results[0]
        attrs = latest.get("attrs") or {}
        head, seen = [], set()
        for k in _SCR_ORDER:
            a = attrs.get(k)
            if a is None or k in seen:
                continue
            seen.add(k)
            raw = a.get("value")
            val = raw
            if k == "data_units_written" and isinstance(raw, (int, float)):
                val = round(raw * 512000 / 1e12, 2)          # NVMe: единицы по 512000 байт → TB
            level, hint = _scr_verdict(k, raw, a.get("status", 0))
            nm, unit = _SCR_NAMES.get(k, (k, ""))
            head.append({"name": nm, "value": val, "unit": unit, "status": a.get("status", 0),
                         "level": level, "hint": hint})
        flagged = [{"name": _SCR_NAMES.get(k, (k, ""))[0], "value": v.get("value")}
                   for k, v in attrs.items() if v.get("status")]
        hist = [s.get("temp") for s in reversed(results) if isinstance(s.get("temp"), (int, float))][-60:]
        return {"ok": True, "status": dev.get("status", 0), "temp": latest.get("temp"),
                "power_on_hours": latest.get("power_on_hours"),
                "power_cycles": latest.get("power_cycle_count"),
                "headline": head, "flagged": flagged, "temp_history": hist}
    except Exception:
        return {"ok": False}

# --------------------------------------------------------------------------- #
#  vnstat — счётчик трафика по основному интерфейсу (сегодня/месяц/всего).
#  Системный пакет; нет бинаря/данных → {ok:False}, и виджет прячется.
#  vnstat 2.x (json v2) отдаёт rx/tx в БАЙТАХ.
# --------------------------------------------------------------------------- #
_vnstat_cache = {"t": 0, "data": {"ok": False}}
_vnstat_lock = threading.Lock()
VNSTAT_TTL = 30

# Физические аплинки (eth0/wlan0/en*), но не docker-мосты (br-*), veth*, lo.
# Суммируем их: при переключении кабель↔Wi-Fi (netguard) трафик идёт то по eth0,
# то по wlan0 — сумма даёт цельную картину, а не «теряет» историю при смене линка.
_PHYS_IF = re.compile(r"^(eth|en|end|wlan|wl)\d")

def vnstat_state():
    with _vnstat_lock:
        if time.time() - _vnstat_cache["t"] < VNSTAT_TTL:
            return _vnstat_cache["data"]
    out = {"ok": False}
    if shutil.which("vnstat"):
        try:
            r = subprocess.run(["vnstat", "--json"], capture_output=True, text=True, timeout=6)
            if r.returncode == 0:
                lt = time.localtime()
                today, thismon = (lt.tm_year, lt.tm_mon, lt.tm_mday), (lt.tm_year, lt.tm_mon)
                acc = {"today": [0, 0], "month": [0, 0], "total": [0, 0]}
                ifaces = []
                for it in (json.loads(r.stdout).get("interfaces") or []):
                    nm = it.get("name", "")
                    if not _PHYS_IF.match(nm):
                        continue
                    ifaces.append(nm)
                    t = it.get("traffic") or {}
                    tot = t.get("total") or {}
                    acc["total"][0] += tot.get("rx", 0); acc["total"][1] += tot.get("tx", 0)
                    for e in (t.get("day") or []):
                        dd = e.get("date") or {}
                        if (dd.get("year"), dd.get("month"), dd.get("day")) == today:
                            acc["today"][0] += e.get("rx", 0); acc["today"][1] += e.get("tx", 0)
                    for e in (t.get("month") or []):
                        dd = e.get("date") or {}
                        if (dd.get("year"), dd.get("month")) == thismon:
                            acc["month"][0] += e.get("rx", 0); acc["month"][1] += e.get("tx", 0)
                if ifaces:
                    mk = lambda p: {"rx": p[0], "tx": p[1]}
                    out = {"ok": True, "ifaces": ifaces, "today": mk(acc["today"]),
                           "month": mk(acc["month"]), "total": mk(acc["total"])}
        except Exception:
            out = {"ok": False}
    with _vnstat_lock:
        _vnstat_cache["t"] = time.time(); _vnstat_cache["data"] = out
    return out

# --------------------------------------------------------------------------- #
#  What's Up Docker (WUD) — сколько контейнеров ждут обновления образа.
#  REST /api/containers. Нет контейнера → {ok:False}, показываем лишь бейдж-совет.
#  Опрос редкий: WUD сам ходит в реестры по cron (6ч), чаще дёргать незачем.
# --------------------------------------------------------------------------- #
_wud_cache = {"t": 0, "data": {"ok": False}}
_wud_lock = threading.Lock()
WUD_TTL = 45      # WUD пересчитывает «есть обновление» на docker-событие мгновенно;
                  # держим кэш недолго, чтобы плашка гасла быстро и после внешних апдейтов

def wud_state():
    with _wud_lock:
        if time.time() - _wud_cache["t"] < WUD_TTL:
            return _wud_cache["data"]
    out = {"ok": False}
    base = docker_service_url("wud", 3000)
    if base:
        try:
            req = urllib.request.Request(base + "/api/containers",
                                         headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=4) as r:
                arr = json.loads(r.read().decode("utf-8", "replace"))
            ups = []
            for c in (arr if isinstance(arr, list) else []):
                if c.get("updateAvailable"):
                    img = c.get("image") or {}
                    uk = c.get("updateKind") or {}
                    ups.append({"name": c.get("name"),
                                "current": uk.get("localValue") or (img.get("tag") or {}).get("value"),
                                "latest": uk.get("remoteValue") or (c.get("result") or {}).get("tag"),
                                "kind": uk.get("kind"), "diff": uk.get("semverDiff")})
            out = {"ok": True, "url": base, "count": len(ups), "updates": ups}
        except Exception:
            out = {"ok": False}
    with _wud_lock:
        _wud_cache["t"] = time.time(); _wud_cache["data"] = out
    return out

def wud_invalidate():
    """Сбросить кэш обновлений: после действия со стеком/контейнером (pull/up)
    состояние «есть обновление» меняется, а иначе плашка висела бы до TTL."""
    with _wud_lock:
        _wud_cache["t"] = 0

# --------------------------------------------------------------------------- #
#  Glance: compact status feed for external displays (ESP32 etc.)
#  GET /api/glance is reachable with a dedicated read-only token, so a dumb
#  microcontroller never holds a panel session. The server decides WHAT to
#  show: the settings tab picks/orders tiles, the device just renders them.
# --------------------------------------------------------------------------- #
GLANCE_FILE = os.path.join(NAS_CONFIG, "glance.json")
AVAIL_LOG = "/var/lib/nas-wizard/avail.log"   # written by nas-netguard.sh

# (id, label-ru, label-en) — every tile the server can build. Collectors may
# return None (service absent) — the tile silently disappears from the feed.
GLANCE_TILES = [
    ("pool",     "Пул",              "Pool"),
    ("backup",   "Бэкап (общий)",    "Backup (all)"),
    ("nbnext",   "Следующий бэкап",  "Next backup"),
    ("avail",    "Доступность · 24h", "Uptime 24h"),
    ("avail30",  "Доступность · 30d", "Uptime 30d"),
    ("cputemp",  "Температура CPU",  "CPU temp"),
    ("disktemp", "Температура дисков", "Disk temp"),
    ("cpu",      "CPU",              "CPU"),
    ("load",     "Нагрузка",         "Load"),
    ("ram",      "Память",           "RAM"),
    ("rootfs",   "Система",          "System SSD"),
    ("uptime",   "Аптайм",           "Booted"),
    ("net",      "Сеть",             "Network"),
    ("netspeed", "Скорость сети",    "Net speed"),
    ("inet",     "Интернет",         "Internet"),
    ("traffic",  "Трафик",           "Traffic"),
    ("speed",    "Спидтест",         "Speedtest"),
    ("docker",   "Docker",           "Docker"),
    ("wud",      "Образы",           "Images"),
    ("updates",  "Обновления",       "Updates"),
    ("snapraid", "SnapRAID",         "SnapRAID"),
    ("events",   "События",          "Events"),
]
GLANCE_DEF_TILES = ["pool", "backup", "avail", "cputemp", "net", "docker"]

def glance_catalog():
    """Static tiles + one per backup profile (named like its tab) + one per
    user check script in ~/nas-config/scripts/glance/."""
    cat = []
    for t in GLANCE_TILES:
        cat.append(t)
        if t[0] == "backup":
            for pr in _safe(nb_profiles, []) or []:
                nm = pr.get("name") or pr["id"]
                cat.append(("nb:" + pr["id"], "Бэкап · " + nm, "Backup · " + nm))
    for s in _glance_scripts():
        cat.append((s["id"], s["name"], s["name"]))
    return cat

# User check scripts: any executable in ~/nas-config/scripts/glance/ becomes a
# tile. First stdout line: "ok|warn|danger <short text>"; otherwise the exit
# code decides (0=ok, 1=warn, else danger). Refreshed in a background thread so
# a slow script can never stall the glance response.
GLANCE_SCRIPTS_DIR = os.path.join(NAS_CONFIG, "scripts", "glance")
_SC_CACHE = {"t": 0, "busy": False, "data": []}

def _sc_refresh():
    out = []
    try:
        names = sorted(os.listdir(GLANCE_SCRIPTS_DIR))
    except OSError:
        names = []
    for n in names:
        fp = os.path.join(GLANCE_SCRIPTS_DIR, n)
        if not (os.path.isfile(fp) and os.access(fp, os.X_OK)):
            continue
        st, txt = "danger", ""
        try:
            r = subprocess.run([fp], capture_output=True, text=True, timeout=10)
            lines = (r.stdout or "").strip().splitlines()
            line = lines[0].strip() if lines else ""
            w = line.split(None, 1)
            if w and w[0].lower() in ("ok", "warn", "danger"):
                st = w[0].lower()
                txt = w[1] if len(w) > 1 else ""
            else:
                st = {0: "ok", 1: "warn"}.get(r.returncode, "danger")
                txt = line
        except subprocess.TimeoutExpired:
            st, txt = "warn", "таймаут"
        except OSError as e:
            st, txt = "danger", str(e)
        out.append({"id": "sc:" + n, "name": os.path.splitext(n)[0],
                    "state": st, "text": txt[:60]})
    _SC_CACHE["data"] = out
    _SC_CACHE["t"] = time.time()
    _SC_CACHE["busy"] = False

def _glance_scripts():
    if time.time() - _SC_CACHE["t"] >= 60 and not _SC_CACHE["busy"]:
        _SC_CACHE["busy"] = True
        threading.Thread(target=_sc_refresh, daemon=True).start()
    return _SC_CACHE["data"]

def _nb_next_run(cfg):
    """Next scheduled run (epoch) for a backup profile, None if not scheduled."""
    s = (cfg or {}).get("schedule") or {}
    if not s.get("enabled") or not s.get("time"):
        return None
    try:
        hh, mm = map(int, str(s["time"]).split(":"))
    except ValueError:
        return None
    now = time.localtime()
    base = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for d in range(8):
        t = base + d * 86400
        if t <= time.time():
            continue
        if s.get("freq") == "weekly" and dows[time.localtime(t).tm_wday] != s.get("dow", "Sun"):
            continue
        return t
    return None

# tile id -> history.json field for the 24h sparkline ("net" = rx+tx)
GLANCE_SPARKS = {"cputemp": "temp", "cpu": "cpu", "ram": "mem",
                 "netspeed": "net", "pool": "pool"}

def _gl_spark(field, points=48):
    h = history_snapshot("24h").get("history") or []
    if len(h) < 4:
        return None
    if field == "net":
        raw = [(p.get("rx") or 0) + (p.get("tx") or 0) for p in h]
    else:
        raw = [p.get(field) or 0 for p in h]
    n = len(raw)
    out = []
    for i in range(points):
        a = i * n // points
        b = max(a + 1, (i + 1) * n // points)
        seg = raw[a:b]
        v = sum(seg) / len(seg)
        out.append(int(v) if v >= 100 else round(v, 1))
    return out

# tile style: positions use the 9-grid (matches TFT_eSPI datums); the unit can
# also be glued to the value: "val" = right after it, "valb" = under it
_GL_POS = {"tl", "tc", "tr", "cl", "c", "cr", "bl", "bc", "br", "hide", "val", "valb"}

# colors are #RRGGBB or #RRGGBBAA — the device blends the alpha against the
# tile background itself (TFT has no true transparency)
_GL_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")

def _gl_norm_style(st):
    if not isinstance(st, dict):
        return {}
    out = {}
    for k in ("lp", "vp", "up"):                      # label/value/unit position
        if st.get(k) in _GL_POS:
            out[k] = st[k]
    for k in ("ls", "vs", "us"):                      # label/value/unit font px
        try:
            v = int(st.get(k))
            if 6 <= v <= 120:
                out[k] = v
        except (TypeError, ValueError):
            pass
    for k in ("lc", "vc", "uc", "bc"):                # text/border colors
        v = st.get(k)
        if isinstance(v, str) and _GL_COLOR.match(v):
            out[k] = v
    bg = st.get("bg")                                 # absent=default card
    if bg == "none" or (isinstance(bg, str) and _GL_COLOR.match(bg)):
        out["bg"] = bg
    try:
        bw = int(st.get("bw"))                        # border width, 0=none
        if 0 <= bw <= 6:
            out["bw"] = bw
    except (TypeError, ValueError):
        pass
    return out

def _gl_norm_tiles(lst):
    """Normalize a tile list: id string (legacy) or
    {id, size s|m|l, x/y/w/h (free mode, device px), st (style)}."""
    out = []
    for t in lst or []:
        if isinstance(t, str):
            out.append({"id": t, "size": "m"})
            continue
        if not (isinstance(t, dict) and t.get("id")):
            continue
        d = {"id": str(t["id"]),
             "size": t.get("size") if t.get("size") in ("s", "m", "l") else "m"}
        for k in ("x", "y", "w", "h"):
            try:
                d[k] = max(0, min(4096, int(t[k])))
            except (KeyError, TypeError, ValueError):
                pass
        st = _gl_norm_style(t.get("st"))
        if st:
            d["st"] = st
        out.append(d)
    return out

# display size presets for the constructor canvas ("сколько поместится")
GLANCE_PRESETS = [
    ("320x170", "LilyGO T-Display-S3"),
    ("640x180", "LilyGO T-Display-S3 Long"),
    ("320x240", "TFT 2.4\" 320×240"),
    ("480x320", "TFT 3.5\" 480×320"),
    ("296x128", "e-ink 2.9\" 296×128"),
]

def _gl_norm_pages(pages):
    return [{"name": str(p.get("name") or "Экран")[:24], "tiles": _gl_norm_tiles(p.get("tiles"))}
            for p in (pages or []) if isinstance(p, dict)][:6]

def load_glance():
    d = _json_load_strict(GLANCE_FILE, {})
    screens = d.get("screens")
    if not isinstance(screens, list) or not screens:
        # migrate: legacy flat pages/tiles -> a single screen
        pages = d.get("pages")
        if not isinstance(pages, list) or not pages:
            flat = d.get("tiles") if isinstance(d.get("tiles"), list) else list(GLANCE_DEF_TILES)
            pages = [{"name": "Главная", "tiles": flat}]
        screens = [{"id": "main", "name": "Экран 1", "preset": "320x170", "pages": pages}]
    out = []
    for s in screens[:4]:
        if not isinstance(s, dict):
            continue
        sid = re.sub(r"[^a-z0-9]", "", str(s.get("id") or ""))[:12] or "s%d" % (len(out) + 1)
        preset = str(s.get("preset") or "320x170")
        if not re.match(r"^\d{2,4}x\d{2,4}$", preset):
            preset = "320x170"
        try:
            gap = max(0, min(24, int(s.get("gap"))))
        except (TypeError, ValueError):
            gap = 0
        out.append({"id": sid, "name": str(s.get("name") or "Экран")[:24],
                    "preset": preset, "gap": gap,
                    "mode": "free" if s.get("mode") == "free" else "flow",
                    "avail": s.get("avail") is not False,   # bottom 24h strip
                    "defst": _gl_norm_style(s.get("defst")),  # style for new tiles
                    "pages": _gl_norm_pages(s.get("pages"))})
    return {"enabled": bool(d.get("enabled")),
            "token": d.get("token") or "",
            "ping_interval": int(d.get("ping_interval") or 30),
            "screens": out}

def save_glance(d):
    cur = load_glance()
    if "enabled" in d:
        cur["enabled"] = bool(d["enabled"])
    if isinstance(d.get("screens"), list):
        ok_ids = {t[0] for t in glance_catalog()}
        screens, seen = [], set()
        for s in d["screens"][:4]:
            if not isinstance(s, dict):
                continue
            sid = re.sub(r"[^a-z0-9]", "", str(s.get("id") or ""))[:12]
            while not sid or sid in seen:
                sid = secrets.token_hex(3)
            seen.add(sid)
            preset = str(s.get("preset") or "320x170")
            if not re.match(r"^\d{2,4}x\d{2,4}$", preset):
                preset = "320x170"
            try:
                gap = max(0, min(24, int(s.get("gap"))))
            except (TypeError, ValueError):
                gap = 0
            pages = _gl_norm_pages(s.get("pages"))
            for p in pages:
                p["tiles"] = [t for t in p["tiles"] if t["id"] in ok_ids]
            screens.append({"id": sid, "name": str(s.get("name") or "Экран")[:24],
                            "preset": preset, "gap": gap,
                            "mode": "free" if s.get("mode") == "free" else "flow",
                            "avail": s.get("avail") is not False,
                            "defst": _gl_norm_style(s.get("defst")),
                            "pages": pages or [{"name": "Главная", "tiles": []}]})
        if screens:
            cur["screens"] = screens
    act = d.get("token_action")
    if act == "new":
        cur["token"] = secrets.token_hex(16)
    elif act == "revoke":
        cur["token"] = ""
    pi = d.get("ping_interval")
    if pi in (15, 30, 60, 120):
        cur["ping_interval"] = pi
        # availability probe period = netguard timer period; a systemd drop-in
        # overrides the base 30 s without touching the wizard-managed unit
        try:
            os.makedirs("/etc/systemd/system/nas-netguard.timer.d", exist_ok=True)
            with open("/etc/systemd/system/nas-netguard.timer.d/override.conf", "w") as f:
                f.write("[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=%ds\n" % pi)
            subprocess.run(["systemctl", "daemon-reload"], timeout=15)
            subprocess.run(["systemctl", "restart", "nas-netguard.timer"], timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    _json_save(GLANCE_FILE, cur, indent=2)
    with _GL_LOCK:
        _GL_CACHE["langs"].clear()
    return cur

def _avail_segments():
    segs = []
    try:
        with open(AVAIL_LOG) as f:
            for ln in f:
                p = ln.split()
                if len(p) == 2 and p[0].isdigit() and p[1] in ("up", "local", "off"):
                    segs.append((int(p[0]), p[1]))
    except OSError:
        pass
    return segs

def avail_bars(hours=24, slots=96):
    """RLE timeline -> per-slot worst state + uptime %. 2=up 1=local 0=off -1=no data."""
    segs = _avail_segments()
    now = int(time.time())
    start = now - hours * 3600
    rank = {"off": 0, "local": 1, "up": 2}
    bars = [-1] * slots
    slot_w = hours * 3600.0 / slots
    up_t = known_t = 0
    events = []              # exact non-up intervals for the widget tooltips
    for i, (t, s) in enumerate(segs):
        t2 = segs[i + 1][0] if i + 1 < len(segs) else now
        a, b = max(t, start), min(t2, now)
        if b <= a:
            continue
        known_t += b - a
        if s == "up":
            up_t += b - a
        else:
            events.append({"from": a, "to": b, "state": s})
        s0 = int((a - start) / slot_w)
        s1 = int((b - 1 - start) / slot_w)
        v = rank[s]
        for k in range(max(0, s0), min(slots - 1, s1) + 1):
            bars[k] = v if bars[k] < 0 else min(bars[k], v)
    pct = round(100.0 * up_t / known_t, 1) if known_t else None
    return {"bars": bars, "pct": pct, "hours": hours, "start": start, "now": now,
            "events": events[-40:]}   # cap: tooltips only need recent detail

_INET_CACHE = {"t": 0, "ok": False}
def _inet_ok():
    """Cheap cached internet check: TCP connect to a public resolver."""
    if time.time() - _INET_CACHE["t"] < 30:
        return _INET_CACHE["ok"]
    ok = False
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=1.5).close()
            ok = True
            break
        except OSError:
            pass
    _INET_CACHE["t"] = time.time(); _INET_CACHE["ok"] = ok
    return ok

def _gl_ago(sec, en):
    """Short age: '3д'/'3d', '5ч'/'5h', '12м'/'12m'."""
    sec = max(0, int(sec))
    if sec >= 172800:
        return "%dд" % (sec // 86400) if not en else "%dd" % (sec // 86400)
    if sec >= 5400:
        return "%dч" % round(sec / 3600) if not en else "%dh" % round(sec / 3600)
    return "%dм" % (sec // 60) if not en else "%dm" % (sec // 60)

def _gl_gb(n, en):
    """Free space as a short number + unit tuple."""
    n = float(n or 0) / 1024 ** 3
    if n >= 1000:
        return ("%.1f" % (n / 1024), "ТБ" if not en else "TB")
    return ("%d" % n, "ГБ" if not en else "GB")

def _gl_bytes(n, en):
    """fmt_bytes with latin units for lang=en (TFT default fonts have no cyrillic)."""
    s = fmt_bytes(n)
    if en:
        for a, b in (("КБ", "KB"), ("МБ", "MB"), ("ГБ", "GB"), ("ТБ", "TB"), ("ПБ", "PB"), ("Б", "B")):
            if s.endswith(a):
                return s[:-len(a)] + b
    return s

def _hwmon_disk_temps():
    """Disk temps from hwmon (nvme/drivetemp) — never wakes sleeping disks,
    unlike smartctl. Returns [(dev, °C)]."""
    out = []
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = _read(os.path.join(h, "name")).strip()
        if name not in ("nvme", "drivetemp"):
            continue
        try:
            t = int(_read(os.path.join(h, "temp1_input")).strip()) / 1000.0
        except ValueError:
            continue
        dev = ""
        try:
            for d in os.listdir(os.path.join(h, "device")):
                if re.match(r"^(nvme\d+n\d+|sd[a-z]+)$", d):
                    dev = d
                    break
        except OSError:
            pass
        out.append((dev or name, t))
    return out

def _nb_last_ok(pid):
    """Timestamp of the profile's last completed run (ok or warn), 0 if none."""
    for h in nb_history(pid):
        if h.get("result") in ("ok", "warn"):
            return h.get("ts", 0)
    return 0

def _gl_backup_tile(best, en):
    if not best:
        return {"value": "—", "unit": "", "state": "warn",
                "note": "ещё не было" if not en else "never ran", "raw": None}
    age = time.time() - best
    st = "danger" if age > 7 * 86400 else ("warn" if age > 2 * 86400 else "ok")
    return {"value": _gl_ago(age, en), "unit": "назад" if not en else "ago", "state": st,
            "raw": {"ts": int(best), "age_s": int(age)}}

def _glance_tile(tid, en):
    """Build one tile -> {value, unit, state, raw[, note]} or None to hide it.
    value/unit are display-ready strings; raw is the machine-readable source
    for anyone building their own UI on top of /api/glance."""
    if tid == "pool":
        di = disk_info(STORAGE) if os.path.ismount(STORAGE) else None
        if not di:
            return {"value": "—", "unit": "", "state": "danger",
                    "note": "пул не смонтирован" if not en else "pool not mounted", "raw": None}
        v, u = _gl_gb(di["free"], en)
        st = "danger" if di["pct"] >= 90 else ("warn" if di["pct"] >= 80 else "ok")
        return {"value": v, "unit": u + (" своб." if not en else " free"), "state": st,
                "raw": {"free": di["free"], "used": di["used"], "pct": di["pct"]}}
    if tid == "backup":
        return _gl_backup_tile(max((_nb_last_ok(pr["id"]) for pr in nb_profiles()), default=0), en)
    if tid.startswith("nb:"):
        return _gl_backup_tile(_nb_last_ok(tid[3:]), en)
    if tid == "nbnext":
        best, name = None, ""
        for pr in nb_profiles():
            t = _nb_next_run(pr)
            if t and (best is None or t < best):
                best, name = t, pr.get("name") or pr["id"]
        if best is None:
            return None
        return {"value": _gl_ago(best - time.time(), en),
                "unit": "до запуска" if not en else "until run", "state": "ok", "note": name,
                "raw": {"ts": int(best), "in_s": int(best - time.time()), "profile": name}}
    if tid.startswith("sc:"):
        s = next((x for x in _glance_scripts() if x["id"] == tid), None)
        if not s:
            return None
        fall = {"ok": "OK", "warn": "WARN", "danger": "FAIL"}[s["state"]]
        return {"value": s["text"] or fall, "unit": "", "state": s["state"],
                "raw": {"state": s["state"], "text": s["text"]}}
    if tid in ("avail", "avail30"):
        hours = 24 if tid == "avail" else 720
        av = avail_bars(hours, 96)
        if av["pct"] is None:
            return None
        st = "ok" if av["pct"] >= 99 else ("warn" if av["pct"] >= 95 else "danger")
        unit = ("% / 24ч" if not en else "% / 24h") if tid == "avail" else \
               ("% / 30д" if not en else "% / 30d")
        return {"value": "%.1f" % av["pct"], "unit": unit, "state": st,
                "raw": {"pct": av["pct"], "hours": hours}}
    if tid == "disktemp":
        temps = _hwmon_disk_temps()
        if not temps:
            return None
        dev, t = max(temps, key=lambda x: x[1])
        try:
            warn_at = int((load_monitor().get("events", {}).get("disktemp") or {}).get("threshold", 60))
        except (TypeError, ValueError):
            warn_at = 60
        st = "danger" if t >= warn_at + 10 else ("warn" if t >= warn_at else "ok")
        return {"value": "%d" % round(t), "unit": "°C", "state": st, "note": dev,
                "raw": {"c": round(t, 1), "dev": dev,
                        "all": [{"dev": d, "c": round(x, 1)} for d, x in temps]}}
    if tid == "cpu":
        pct = cpu_percent()
        st = "danger" if pct >= 95 else ("warn" if pct >= 80 else "ok")
        return {"value": "%d" % round(pct), "unit": "%", "state": st, "raw": {"pct": pct}}
    if tid == "netspeed":
        r = net_rate(default_iface()) or {}
        raw = {"rx": r.get("rx", 0), "tx": r.get("tx", 0)}
        if load_settings().get("netUnits") == "bits":  # panel-wide unit choice
            def _mb(x):
                x = (x or 0) * 8 / 1e6
                return "%d" % x if x >= 100 else "%.1f" % x
            return {"value": "↓%s ↑%s" % (_mb(raw["rx"]), _mb(raw["tx"])),
                    "unit": "Мбит/с" if not en else "Mbit/s", "state": "ok", "raw": raw}
        return {"value": "↓%s ↑%s" % (_gl_bytes(raw["rx"], en), _gl_bytes(raw["tx"], en)),
                "unit": "/с" if not en else "/s", "state": "ok", "raw": raw}
    if tid == "cputemp":
        t = temp_c()
        if t is None:
            return None
        thr = _safe(throttled) or {}
        st = "danger" if (t >= 75 or thr.get("throttle")) else ("warn" if t >= 65 else "ok")
        return {"value": "%d" % round(t), "unit": "°C", "state": st,
                "raw": {"c": round(t, 1), "throttle": bool(thr.get("throttle"))}}
    if tid == "load":
        la = os.getloadavg()[0]
        ncpu = os.cpu_count() or 4
        st = "danger" if la >= ncpu * 2 else ("warn" if la >= ncpu else "ok")
        return {"value": "%.1f" % la, "unit": "load", "state": st,
                "raw": {"load1": round(la, 2), "ncpu": ncpu}}
    if tid == "ram":
        mi = mem_info()
        pct = mi["pct"]
        st = "danger" if pct >= 92 else ("warn" if pct >= 80 else "ok")
        return {"value": "%d" % round(pct), "unit": "%", "state": st,
                "raw": {"pct": pct, "used": mi["used"], "total": mi["total"]}}
    if tid == "rootfs":
        di = disk_info("/")
        if not di:
            return None
        st = "danger" if di["pct"] >= 90 else ("warn" if di["pct"] >= 80 else "ok")
        v, u = _gl_gb(di["free"], en)
        return {"value": v, "unit": u + (" своб." if not en else " free"), "state": st,
                "raw": {"free": di["free"], "used": di["used"], "pct": di["pct"]}}
    if tid == "uptime":
        up = uptime_s()
        return {"value": _gl_ago(up, en), "unit": "", "state": "ok", "raw": {"s": int(up)}}
    if tid == "net":
        ip = lan_ip()
        good = bool(ip) and not ip.startswith("127.")
        return {"value": ip or "—", "unit": default_iface() or "",
                "state": "ok" if good else "danger",
                "raw": {"ip": ip, "iface": default_iface(), "ok": good}}
    if tid == "inet":
        ok = _inet_ok()
        return {"value": ("есть" if not en else "up") if ok else ("нет" if not en else "down"),
                "unit": "", "state": "ok" if ok else "danger", "raw": {"ok": ok}}
    if tid == "traffic":
        v = _safe(vnstat_state) or {}
        if not v.get("ok"):
            return None
        td = v.get("today") or {}
        return {"value": "↓%s ↑%s" % (_gl_bytes(td.get("rx"), en).replace(" ", ""),
                                       _gl_bytes(td.get("tx"), en).replace(" ", "")),
                "unit": "сегодня" if not en else "today", "state": "ok",
                "raw": {"rx": td.get("rx", 0), "tx": td.get("tx", 0)}}
    if tid == "speed":
        m = _safe(myspeed_state) or {}
        if not m.get("ok") or m.get("download") is None:
            return None
        return {"value": "↓%s ↑%s" % (m.get("download"), m.get("upload")),
                "unit": "Мбит" if not en else "Mbit",
                "state": "warn" if m.get("failed") else "ok",
                "raw": {"down": m.get("download"), "up": m.get("upload"),
                        "ping": m.get("ping"), "created": m.get("created")}}
    if tid == "docker":
        ps = _safe(_docker_ps) or []
        if not ps:
            return None
        run = [c for c in ps if str(c.get("State")) == "running"]
        bad = [c for c in run if "unhealthy" in str(c.get("Status", ""))]
        st = "warn" if bad else "ok"
        note = ", ".join(c.get("Names", "?") for c in bad[:3]) if bad else None
        out = {"value": "%d/%d" % (len(run), len(ps)), "unit": "", "state": st,
               "raw": {"running": len(run), "total": len(ps),
                       "unhealthy": [c.get("Names") for c in bad]}}
        if note:
            out["note"] = note
        return out
    if tid == "wud":
        w = _safe(wud_state) or {}
        if not w.get("ok"):
            return None
        n = w.get("count", 0)
        return {"value": str(n), "unit": "обнов." if not en else "upd",
                "state": "warn" if n else "ok", "raw": {"count": n}}
    if tid == "updates":
        n = _safe(_apt_upgradable, 0) or 0
        return {"value": str(n), "unit": "apt", "state": "warn" if n > 20 else "ok",
                "raw": {"count": n}}
    if tid == "snapraid":
        sr = _safe(snapraid_status) or {}
        if not sr.get("configured"):
            return None
        ls = sr.get("last_sync") or {}
        if not ls.get("date"):
            return {"value": "—", "unit": "sync", "state": "warn", "raw": None}
        try:
            age = time.time() - time.mktime(time.strptime(ls["date"], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            age = 0
        st = "danger" if (ls.get("result") == "err" or sr.get("blocked")) else \
             ("warn" if age > 8 * 86400 else "ok")
        return {"value": _gl_ago(age, en), "unit": "sync", "state": st,
                "raw": {"age_s": int(age), "result": ls.get("result"),
                        "blocked": bool(sr.get("blocked"))}}
    if tid == "events":
        try:
            with open(EVENTS_FILE) as f:
                ev = json.load(f)
            unseen = sum(1 for e in ev.get("items", []) if e.get("id", 0) > ev.get("seen", 0))
        except (OSError, ValueError):
            unseen = 0
        return {"value": str(unseen), "unit": "новых" if not en else "new", "state": "ok",
                "raw": {"unseen": unseen}}
    return None

# per-language cache: labels/values are localized, so each language keeps its
# own change-signature and seq stream (a device polls with one fixed lang)
_GL_CACHE = {"t": 0, "langs": {}}
_GL_LOCK = threading.Lock()

_GL_PAL_CACHE = {}
def glance_palette(lang="ru"):
    """Every catalog tile built with live data — for the constructor app's
    palette (session-only; devices never request this)."""
    en = (lang == "en")
    c = _GL_PAL_CACHE.get(lang)
    if c and time.time() - c["t"] < 5:
        return c["data"]
    out = []
    for tid, ru, en_l in glance_catalog():
        d = _safe(lambda: _glance_tile(tid, en))
        if d:
            out.append(dict(d, id=tid, label=(en_l if en else ru)))
    _GL_PAL_CACHE[lang] = {"t": time.time(), "data": out}
    return out

def glance_payload(lang="ru", screen=""):
    """Glance document for one screen profile; cached a few seconds,
    seq bumps only on change. Cache/seq stream is per (lang, screen)."""
    en = (lang == "en")
    cfg = load_glance()
    scr = next((s for s in cfg["screens"] if s["id"] == screen), cfg["screens"][0])
    key = lang + "|" + scr["id"]
    with _GL_LOCK:
        c = _GL_CACHE["langs"].get(key)
        if c and time.time() - c["t"] < 3:
            return c["payload"]
    labels = {t[0]: (t[2] if en else t[1]) for t in glance_catalog()}
    built, problems, seen_prob = {}, [], set()
    def build(tid):
        if tid in built:
            return built[tid]
        d = _safe(lambda: _glance_tile(tid, en)) if tid in labels else None
        if d:
            d = dict(d, id=tid, label=labels[tid])
            if tid in GLANCE_SPARKS:
                sp = _safe(lambda: _gl_spark(GLANCE_SPARKS[tid]))
                if sp:
                    d["spark"] = sp
        built[tid] = d
        return d
    pages = []
    for pg in scr["pages"]:
        tl = []
        for t in pg["tiles"]:
            d = build(t["id"])
            if not d:
                continue
            extra = {k: t[k] for k in ("x", "y", "w", "h", "st") if k in t}
            tl.append(dict(d, size=t["size"], **extra))
            if d["state"] != "ok" and d["id"] not in seen_prob:
                seen_prob.add(d["id"])
                problems.append("%s: %s %s" % (d["label"], d["value"], d.get("note") or d["unit"]))
        pages.append({"name": pg["name"], "tiles": tl})
    shown = [d for d in built.values() if d]
    status = "ok"
    if any(t["state"] == "danger" for t in shown):
        status = "danger"
    elif any(t["state"] == "warn" for t in shown):
        status = "warn"
    av = avail_bars(24, 96)
    payload = {"v": 2, "host": socket.gethostname(), "status": status,
               "screen": {"id": scr["id"], "name": scr["name"], "preset": scr["preset"],
                          "mode": scr["mode"], "gap": scr["gap"], "avail": scr["avail"]},
               "problems": problems[:4], "pages": pages,
               # legacy flat list = first page (older sketches keep working)
               "tiles": pages[0]["tiles"] if pages else [],
               "avail": {"bars": av["bars"], "pct24": av["pct"]},
               "ts": int(time.time())}
    sig = json.dumps([pages, problems, status, av["bars"]], sort_keys=True, ensure_ascii=False)
    with _GL_LOCK:
        c = _GL_CACHE["langs"].setdefault(key, {"t": 0, "sig": "", "seq": 0, "payload": None})
        if sig != c["sig"]:
            c["seq"] += 1
            c["sig"] = sig
        payload["seq"] = c["seq"]
        c["t"] = time.time()
        c["payload"] = payload
    return payload

# --------------------------------------------------------------------------- #
#  Notes: folders of plain .md files (default on the pool) with a tiny
#  frontmatter (title/tags). Readable over SMB, portable, zero lock-in;
#  images live next to the note in _assets/, deletes go to .trash/.
# --------------------------------------------------------------------------- #
NOTES_CONF = os.path.join(NAS_CONFIG, "notes.json")

def load_notes_conf():
    return _json_load_strict(NOTES_CONF, {})

def notes_root(create=True):
    root = (load_notes_conf().get("root") or "").strip()
    if not root:
        root = os.path.join(STORAGE, "notes") if os.path.ismount(STORAGE) \
            else os.path.join(HOME, "nas-notes")
    if create and not os.path.isdir(root):
        os.makedirs(root, exist_ok=True)
        _chown_user(root)
    return os.path.realpath(root)

def _notes_abs(rel):
    root = notes_root()
    rel = (rel or "").replace("\\", "/").strip("/")
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("путь вне папки заметок")
    return p

_NOTE_FM = re.compile(r"^---\n(.*?)\n---\n?", re.S)

def _note_parse(text):
    meta, body = {}, text
    m = _NOTE_FM.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    tags = [t.strip().lstrip("#") for t in (meta.get("tags") or "").split(",") if t.strip()]
    return meta.get("title") or "", tags, body

def _note_dump(title, tags, body, pinned=False):
    return "---\ntitle: %s\ntags: %s\nupdated: %s%s\n---\n%s" % (
        str(title).replace("\n", " "), ", ".join(tags),
        time.strftime("%Y-%m-%d %H:%M"),
        "\npinned: 1" if pinned else "", body)

def notes_tree():
    root = notes_root()
    dirs, notes = [], []
    stats = {"notes": 0, "dirs": 0, "size": 0, "assets": 0, "trash": 0, "history": 0}
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        top = rel.split("/")[0]
        # service trees are only counted for the stats, not listed
        if top in (".trash", ".history") or "/_assets" in "/" + rel:
            key = "trash" if top == ".trash" else ("history" if top == ".history" else "assets")
            for f in fn:
                try:
                    n = os.stat(os.path.join(dp, f)).st_size
                    stats[key] += n
                    stats["size"] += n
                except OSError:
                    pass
            continue
        # keep .trash/.history walkable so their size lands in the stats
        dn[:] = sorted(d for d in dn if not d.startswith(".") or d in (".trash", ".history"))
        if rel:
            dirs.append(rel)
            stats["dirs"] += 1
        for f in sorted(fn):
            fp = os.path.join(dp, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            stats["size"] += st.st_size
            if not f.lower().endswith(".md"):
                continue
            try:
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(2048)
            except OSError:
                continue
            title, tags, body = _note_parse(head)
            # list-card excerpt: strip HTML tags first, decode entities, then
            # drop markdown noise and urls — plain text only
            prev = re.sub(r"<[^>]*>", " ", body)
            prev = _htmllib.unescape(prev)
            prev = re.sub(r"\(https?://\S+\)|\(/api/\S+\)|https?://\S+|/api/\S+", " ", prev)
            prev = re.sub(r"[#*`>\[\]!|_-]+", " ", prev)
            prev = " ".join(prev.split())[:150]
            stats["notes"] += 1
            notes.append({"path": (rel + "/" if rel else "") + f, "folder": rel,
                          "title": title or f[:-3], "tags": tags, "prev": prev,
                          "pinned": _note_meta(head).get("pinned") == "1",
                          "mtime": int(st.st_mtime), "size": st.st_size})
    return {"root": root, "dirs": dirs, "notes": notes, "stats": stats}

def _note_meta(text):
    m = _NOTE_FM.match(text)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta

# --- history: .history/<note path>/<date_time>.md, one snapshot per 10 min ---
_NT_VER = re.compile(r"^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.md$")

def _nt_hist_dir(rel):
    return os.path.join(notes_root(), ".history", rel.strip("/"))

def _nt_snapshot(rel, force=False):
    p = _notes_abs(rel)
    if not os.path.isfile(p):
        return
    hd = _nt_hist_dir(rel)
    snaps = sorted(f for f in os.listdir(hd)) if os.path.isdir(hd) else []
    if snaps and not force:
        try:
            if time.time() - os.path.getmtime(os.path.join(hd, snaps[-1])) < 600:
                return
        except OSError:
            pass
    os.makedirs(hd, exist_ok=True)
    dst = os.path.join(hd, time.strftime("%Y-%m-%d_%H-%M-%S") + ".md")
    shutil.copy2(p, dst)
    _chown_user(dst)
    for old in sorted(os.listdir(hd))[:-60]:      # keep the last 60 versions
        try:
            os.remove(os.path.join(hd, old))
        except OSError:
            pass

def notes_history(rel):
    hd = _nt_hist_dir(rel)
    vs = sorted((f for f in os.listdir(hd) if _NT_VER.match(f)), reverse=True) \
        if os.path.isdir(hd) else []
    return {"versions": vs}

def notes_hist_get(rel, ver):
    if not _NT_VER.match(ver or ""):
        raise ValueError("плохая версия")
    fp = os.path.join(_nt_hist_dir(rel), ver)
    if not os.path.isfile(fp):
        raise ValueError("нет такой версии")
    with open(fp, encoding="utf-8", errors="replace") as f:
        title, tags, body = _note_parse(f.read())
    return {"title": title, "tags": tags, "md": body, "ver": ver}

def notes_restore(rel, ver):
    v = notes_hist_get(rel, ver)
    _nt_snapshot(rel, force=True)                 # keep what we are replacing
    cur = note_get(rel)
    return note_save(rel, v["title"] or cur["title"], v["tags"], v["md"])

# --- trash: .trash/<YYYY-MM-DD>/<name> ---
def notes_trash_list():
    tr = os.path.join(notes_root(), ".trash")
    out = []
    if os.path.isdir(tr):
        for day in sorted(os.listdir(tr), reverse=True):
            dd = os.path.join(tr, day)
            if not os.path.isdir(dd):
                continue
            for f in sorted(os.listdir(dd)):
                if f == ".origins.json":
                    continue
                fp = os.path.join(dd, f)
                out.append({"path": ".trash/" + day + "/" + f, "name": f, "day": day,
                            "isdir": os.path.isdir(fp),
                            "size": os.path.getsize(fp) if os.path.isfile(fp) else 0})
    return {"items": out}

def notes_trash_restore(rel):
    src = _notes_abs(rel)
    parts = rel.strip("/").split("/")
    if parts[0] != ".trash" or not os.path.exists(src):
        raise ValueError("это не корзина")
    name = parts[-1]
    # restore to the folder the item was deleted from (recorded in .origins.json),
    # falling back to the notes root for items trashed before origins existed
    og = os.path.join(os.path.dirname(src), ".origins.json")
    m = _json_load_strict(og, {})
    try:
        base = _notes_abs(m.get(name, ""))
    except ValueError:
        base = notes_root()
    os.makedirs(base, exist_ok=True)
    _chown_user(base)
    dst = os.path.join(base, name)
    i, stem = 1, os.path.splitext(name)
    while os.path.exists(dst):
        i += 1
        dst = os.path.join(base, "%s (%d)%s" % (stem[0], i, stem[1]))
    os.rename(src, dst)
    if name in m:
        del m[name]
        _json_save(og, m)
    return {"ok": True}

def notes_trash_clear():
    tr = os.path.join(notes_root(), ".trash")
    if os.path.isdir(tr):
        shutil.rmtree(tr)
    return {"ok": True}

def notes_gc():
    """Daily housekeeping for the notes tree: expire old trash days
    (maintenance.json:notes_trash_days, 0 = keep forever), drop version history
    of notes that no longer exist anywhere, and remove _assets files that no
    note text references."""
    root = notes_root(create=False)
    if not os.path.isdir(root):
        return
    now = time.time()
    days = int(load_maintenance().get("notes_trash_days", 0) or 0)
    tr = os.path.join(root, ".trash")
    if days > 0 and os.path.isdir(tr):
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - days * 86400))
        for day in os.listdir(tr):
            if re.match(r"^\d{4}-\d\d-\d\d$", day) and day < cutoff:
                shutil.rmtree(os.path.join(tr, day), ignore_errors=True)
    # orphaned history: the note is gone and the newest snapshot is older than
    # the trash horizon — by then the trash copy (the only way the note could
    # come back to this path) has expired too
    hist = os.path.join(root, ".history")
    keep_s = max(days, 30) * 86400
    for dp, dn, fn in os.walk(hist, topdown=False):
        rel = os.path.relpath(dp, hist).replace(os.sep, "/")
        if not rel.lower().endswith(".md") or os.path.isfile(os.path.join(root, rel)):
            continue
        try:
            newest = max(os.path.getmtime(os.path.join(dp, f)) for f in fn) if fn else 0
        except OSError:
            continue
        if now - newest > keep_s:
            shutil.rmtree(dp, ignore_errors=True)
    # unreferenced assets: file name (raw or percent-encoded, as note_upload
    # embeds it) is absent from every note text — including trashed notes and
    # history versions, so a restore keeps its images. Age guard: an image is
    # uploaded before the note referencing it is saved.
    corpus = []
    for dp, dn, fn in os.walk(root):
        if os.path.basename(dp) == "_assets":
            dn[:] = []
            continue
        for f in fn:
            if f.lower().endswith(".md"):
                try:
                    with open(os.path.join(dp, f), encoding="utf-8", errors="replace") as fh:
                        corpus.append(fh.read())
                except OSError:
                    pass
    corpus = "\n".join(corpus)
    for dp, dn, fn in os.walk(root):
        if os.path.basename(dp) != "_assets" or "/.trash/" in (dp + "/").replace(os.sep, "/"):
            continue
        for f in fn:
            fp = os.path.join(dp, f)
            try:
                if now - os.path.getmtime(fp) < 7 * 86400:
                    continue
            except OSError:
                continue
            if f not in corpus and quote(f) not in corpus:
                _safe(lambda: os.remove(fp))

def note_get(rel):
    p = _notes_abs(rel)
    if not os.path.isfile(p):
        raise ValueError("нет такой заметки")
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    title, tags, body = _note_parse(text)
    return {"path": rel.strip("/"), "title": title, "tags": tags, "md": body,
            "pinned": _note_meta(text).get("pinned") == "1",
            "mtime": int(os.stat(p).st_mtime)}

def _note_slug(name):
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", str(name or "").strip())
    return name[:80] or "Note"

def note_save(rel, title, tags, md, pinned=False, base_mtime=0, force=False, conflict_copy=False):
    p = _notes_abs(rel)
    if not p.lower().endswith(".md"):
        raise ValueError("не .md")
    out = {"ok": True}
    # optimistic lock: the file changed on disk after this client opened it
    # (another device or tab) — never clobber the other text silently
    try:
        base_mtime = int(base_mtime or 0)
    except (TypeError, ValueError):
        base_mtime = 0
    if base_mtime and not force and os.path.isfile(p) \
            and int(os.stat(p).st_mtime) > base_mtime:
        if not conflict_copy:
            return {"ok": False, "conflict": True, "mtime": int(os.stat(p).st_mtime)}
        # unload-flush can't ask the user — park this client's text in a sibling;
        # the file name is a disk artifact, so it follows the UI language
        word = "конфликт" if (load_settings().get("lang") or "en") == "ru" else "conflict"
        stem = p[:-3] + " (" + word + time.strftime(" %Y-%m-%d %H-%M") + ")"
        cp, i = stem + ".md", 1
        while os.path.exists(cp):
            i += 1
            cp = "%s %d.md" % (stem, i)
        out["conflict_copy"] = os.path.basename(cp)
        rel, p = None, cp
    if rel:
        _safe(lambda: _nt_snapshot(rel))            # history before overwriting
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tags = [_note_slug(t) for t in (tags or []) if str(t).strip()][:20]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_note_dump(title or "", tags, str(md or ""), pinned))
    os.replace(tmp, p)
    _chown_user(p)
    out["mtime"] = int(os.stat(p).st_mtime)
    return out

def note_new(folder, title):
    lang = load_settings().get("lang") or "en"
    base = _note_slug(title or ("Новая заметка" if lang == "ru" else "New note"))
    d = _notes_abs(folder)
    os.makedirs(d, exist_ok=True)
    _chown_user(d)
    name, i = base, 1
    while os.path.exists(os.path.join(d, name + ".md")):
        i += 1
        name = "%s %d" % (base, i)
    rel = ((folder.strip("/") + "/") if (folder or "").strip("/") else "") + name + ".md"
    # title = deduped file name ("Новая заметка 2"), so duplicates are tellable apart
    note_save(rel, name, [], "")
    return {"ok": True, "path": rel}

def note_mkdir(folder, name):
    d = os.path.join(_notes_abs(folder), _note_slug(name))
    os.makedirs(d, exist_ok=True)
    _chown_user(d)
    return {"ok": True}

def note_move(rel, to):
    src, dst = _notes_abs(rel), _notes_abs(to)
    if not os.path.exists(src):
        raise ValueError("нет источника")
    if os.path.exists(dst):
        raise ValueError("такое имя уже занято")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)
    hd = _nt_hist_dir(rel)                      # history follows the note
    if os.path.isdir(hd):
        nh = _nt_hist_dir(to)
        os.makedirs(os.path.dirname(nh), exist_ok=True)
        if not os.path.exists(nh):
            os.rename(hd, nh)
    return {"ok": True}

def note_delete(rel):
    p = _notes_abs(rel)
    if not os.path.exists(p):
        raise ValueError("нет такого пути")
    tr = os.path.join(notes_root(), ".trash", time.strftime("%Y-%m-%d"))
    os.makedirs(tr, exist_ok=True)
    name, i = os.path.basename(p), 1
    stem, ext = os.path.splitext(name)
    dst = os.path.join(tr, name)
    while os.path.exists(dst):
        i += 1
        dst = os.path.join(tr, "%s (%d)%s" % (stem, i, ext))
    os.rename(p, dst)
    # remember the source folder so restore can put the item back where it lived
    og = os.path.join(tr, ".origins.json")
    m = _json_load_strict(og, {})
    m[os.path.basename(dst)] = os.path.dirname(rel.strip("/"))
    _json_save(og, m)
    return {"ok": True}

def note_upload(folder, name, data_b64):
    raw = base64.b64decode((data_b64 or "").split(",")[-1])
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("файл больше 30 МБ")
    ad = os.path.join(_notes_abs(folder), "_assets")
    os.makedirs(ad, exist_ok=True)
    _chown_user(ad)
    stem, ext = os.path.splitext(_note_slug(name or "img"))
    fn, i = stem + ext, 1
    while os.path.exists(os.path.join(ad, fn)):
        i += 1
        fn = "%s-%d%s" % (stem, i, ext)
    fp = os.path.join(ad, fn)
    with open(fp, "wb") as f:
        f.write(raw)
    _chown_user(fp)
    rel = ((folder.strip("/") + "/") if (folder or "").strip("/") else "") + "_assets/" + fn
    return {"ok": True, "path": rel, "url": "/api/notes/file?path=" + quote(rel)}

def notes_search(q):
    q = (q or "").strip().lower()
    out = []
    if not q:
        return {"hits": out}
    for n in notes_tree()["notes"]:
        try:
            with open(_notes_abs(n["path"]), encoding="utf-8", errors="replace") as f:
                text = f.read(200000)
        except (OSError, ValueError):
            continue
        low = text.lower()
        if q in low or q in n["title"].lower() or any(q in t.lower() for t in n["tags"]):
            i = low.find(q)
            out.append(dict(n, snip=text[max(0, i - 40):i + 80].replace("\n", " ") if i >= 0 else ""))
        if len(out) >= 60:
            break
    return {"hits": out}

def notes_migrate(new_root):
    """Move the whole notes tree to another folder (Настройки → Заметки)."""
    old = notes_root()
    new_root = (new_root or "").strip()
    if not new_root.startswith("/"):
        raise ValueError("нужен абсолютный путь")
    new = os.path.realpath(new_root)
    if new == old:
        return {"ok": True, "log": "уже там", "root": new}
    if old.startswith(new + os.sep) or new.startswith(old + os.sep):
        raise ValueError("папки вложены друг в друга")
    os.makedirs(new, exist_ok=True)
    _chown_user(new)
    moved = 0
    for name in os.listdir(old):
        if os.path.exists(os.path.join(new, name)):
            raise ValueError("в новой папке уже есть «%s»" % name)
        shutil.move(os.path.join(old, name), os.path.join(new, name))
        moved += 1
    cur = load_notes_conf()
    cur["root"] = new
    _json_save(NOTES_CONF, cur, indent=2)
    return {"ok": True, "log": "перенесено объектов: %d" % moved, "root": new}

FAV_FILE = os.path.join(NAS_CONFIG, "fm-favorites.json")
def load_favs():
    try:
        with open(FAV_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
def save_favs(d):
    _json_save(FAV_FILE, d, indent=2)

SETTINGS_FILE = os.path.join(NAS_CONFIG, "desktop.json")
def load_settings():
    return _json_load_strict(SETTINGS_FILE, {})
def save_settings(d):
    # MERGE, не перезапись: частичное обновление (например {lang} из мастера)
    # не должно стирать остальные настройки
    cur = load_settings()
    if isinstance(d, dict):
        cur.update(d)
    _json_save(SETTINGS_FILE, cur, indent=2)   # fsync + rename: settings must survive a power cut

WINPOS_FILE = os.path.join(NAS_CONFIG, "winpos.json")
def load_winpos():
    return _json_load_strict(WINPOS_FILE, {})
def save_winpos(d):
    _json_save(WINPOS_FILE, d, indent=2)

def stats():
    iface = default_iface()
    return {
        "host": socket.gethostname(),
        "ip": lan_ip(),
        "cpu": cpu_percent(),
        "temp": temp_c(),
        "throttled": throttled(),
        "psu_ma": PSU_MA,
        "mem": mem_info(),
        # пула нет — значит нет и его статистики (раньше молча подставлялась
        # системная карта, и «заполнение пула» показывало ерунду)
        "disk_pool": disk_info(STORAGE) if os.path.ismount(STORAGE) else None,
        "disk_root": disk_info("/"),
        "net": net_rate(iface),
        "iface": iface,
        "uptime": uptime_s(),
        "load": list(os.getloadavg()),
        # is any backup running (dock icon indicator). nb_run_active also verifies
        # the transient unit is alive, so a stale "running" flag left by a power
        # loss can't spin the icon forever; systemctl is only called while the
        # state flag is set, so the idle path stays cheap.
        "nb_running": any(nb_run_active(p["id"]) for p in nb_profiles()),
        # USB-импорт идёт в фоне (udev), панель могла быть закрыта — показываем его
        # в общем центре операций, а не только на вкладке настроек USB
        "usb_import": _safe(lambda: usb_import_progress()["jobs"], []),
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
_MON_WEEKLY = time.time()   # время последнего еженедельного отчёта (не слать сразу после рестарта)
_MON_DISKSTAT = None  # предыдущий снимок /proc/diskstats (для slow_disk)
_MON_HOG = {}         # pid → сколько тиков подряд процесс жрёт ресурсы
_MON_USBIMP = time.time()   # старые записи журнала импорта не переигрываем
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
        "undervolt":   {"on": True,  "priority": 2},
        "cfg_corrupt": {"on": True,  "priority": 1},
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
        # --- история файлов (fswatch); priority 2 = Pushover emergency (ретраи
        #     каждую минуту до подтверждения) — по умолчанию не используем ---
        "fsw_corrupt": {"on": True,  "priority": 1},
        "fsw_guard":   {"on": True,  "priority": 1},
        "fsw_root":    {"on": True,  "priority": 1},
        "fsw_del":     {"on": True,  "priority": 0, "threshold": 50},
        "fsw_scan":    {"on": True,  "priority": -1},
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
        "slow_disk":   {"on": False, "priority": 0, "threshold": 250},
        "proc_hog":    {"on": False, "priority": 0, "threshold": 80},
        "inodes":      {"on": True,  "priority": 1, "threshold": 90},
        "boot":        {"on": False, "priority": -1},
        # --- USB авто-импорт (итог копирования SD/флешки; Pushover off — скрипт
        #     импорта умеет слать сам, чтобы не дублировать) ---
        "usb_import":  {"on": False, "priority": 0, "desk": True},
        # --- бэкап главного NAS на этот NAS (итог прогона) ---
        "nas_backup":  {"on": True, "priority": 0, "desk": True},
        # --- здоровье бэкапа (периодические проверки, раз в 30 мин) ---
        "nb_conn":     {"on": True,  "priority": 1, "desk": True},
        "nb_srcmiss":  {"on": False, "priority": 1, "desk": True},
        "nb_stale":    {"on": True,  "priority": 1, "threshold": 7,  "desk": True},
        "nb_size":     {"on": False, "priority": 0, "threshold": 40, "desk": True},
        "nb_dest":     {"on": True,  "priority": 1, "threshold": 95, "desk": True},
        "nb_guard":    {"on": True,  "priority": 2, "desk": True},   # сработала защита --max-delete
        "nb_verify":   {"on": True,  "priority": 1, "desk": True},   # сверка контрольных сумм нашла расхождения
        # --- надёжность: диск сам переподключился (авто-mount) ---
        "disk_remount":{"on": True, "priority": 0, "desk": True},
        # --- активная термозащита (предупреждение/действие) ---
        "thermal_guard":{"on": True, "priority": 1, "desk": True},
        # --- ежедневная/еженедельная сводка состояния ---
        "daily_summary":{"on": True, "priority": -1, "desk": False},
    }}

def _monitor_defaults_desk(d):
    """desk = показывать плашкой на рабочем столе; по умолчанию — всё важное (priority>=1)."""
    for k, v in d.get("events", {}).items():
        v.setdefault("desk", v.get("priority", 0) >= 1)
    return d

def load_monitor():
    d = _def_monitor()
    saved = _json_load_strict(MONITOR_FILE, {})
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k == "events" and isinstance(v, dict):
                for ek, ev in v.items():
                    if isinstance(ev, dict):
                        d["events"].setdefault(ek, {}).update(ev)
            else:
                d[k] = v
    return _monitor_defaults_desk(d)

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
                for bk in ("on", "desk"):
                    if bk in cur["events"][ek]:
                        cur["events"][ek][bk] = bool(cur["events"][ek][bk])
    _json_save(MONITOR_FILE, cur)
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

# --------------------------------------------------------------------------- #
#  Серверный перевод тем же словарём, что и клиент (web/i18n.js). Нужен для того,
#  что уходит МИМО браузера — прежде всего Pushover: там строки формируются на
#  сервере по-русски и клиентский nasTr к ним не применяется. Правило переноса
#  один в один: перевод по «целым словам» (границы — не-кириллица), длинные ключи
#  приоритетнее. Язык берём из настроек (desktop.json:lang), кэшируем словарь.
# --------------------------------------------------------------------------- #
_I18N = None
_I18N_RX = None
_i18n_lock = threading.Lock()

def _i18n_load():
    global _I18N, _I18N_RX
    if _I18N is not None:
        return
    d = {}
    try:
        src = _read(os.path.join(WEB_DIR, "i18n.js"))
        for m in re.finditer(r'"((?:\\.|[^"\\])*)"\s*:\s*"((?:\\.|[^"\\])*)"', src):
            try:
                k = json.loads('"' + m.group(1) + '"')
                v = json.loads('"' + m.group(2) + '"')
            except ValueError:
                continue
            if k and re.search(r"[А-Яа-яЁё]", k):      # ключ обязан быть русским (не шум из IIFE)
                d[k] = v
    except Exception:
        d = {}
    if d:
        alts = "|".join(re.escape(k) for k in sorted(d, key=len, reverse=True))
        try:
            _I18N_RX = re.compile(r"(?<![А-Яа-яЁё])(?:" + alts + r")(?![А-Яа-яЁё])")
        except re.error:
            _I18N_RX = None
    _I18N = d

def tr(text, lang=None):
    if not isinstance(text, str) or not text:
        return text
    if lang is None:
        lang = (load_settings().get("lang") or "en")
    if lang != "en" or not re.search(r"[А-Яа-яЁё]", text):
        return text
    with _i18n_lock:
        _i18n_load()
    if not _I18N_RX:
        return text
    return _I18N_RX.sub(lambda m: _I18N.get(m.group(0), m.group(0)), text)

def push_notify(title, msg, priority=0):
    title = tr(title); msg = tr(msg)      # Pushover идёт мимо клиента — переводим тут
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
    # No nas-notify.sh fallback here: it reads the SAME notify.conf, so it can't
    # help when keys are missing, and on a send timeout it re-sent the raw RU
    # text after the EN one already went through (duplicate pushes).
    return False

# --------------------------------------------------------------------------- #
#  Журнал событий (центр уведомлений): всё важное — события монитора, действия
#  пользователя, ошибки — пишется сюда и хранится ~месяц. Pushover/рабочий стол —
#  лишь способы доставки, журнал ведётся всегда.
# --------------------------------------------------------------------------- #
EVENTS_FILE = os.path.join(NAS_CONFIG, "events.json")
EVENTS_CAP  = 3000
EVENTS_DAYS = 31
_events = None
_events_lock = threading.Lock()
# Условие на том же замке: long-poll /api/events спит на нём, log_event будит —
# панель узнаёт о событии мгновенно, а не на следующем опросе.
_events_cond = threading.Condition(_events_lock)

def _events_load():
    global _events
    if _events is None:
        try:
            with open(EVENTS_FILE) as f:
                _events = json.load(f)
        except (OSError, ValueError):
            _events = None
        if not isinstance(_events, dict) or not isinstance(_events.get("items"), list):
            _events = {"seq": 0, "seen": 0, "items": []}
        _events.setdefault("seq", 0); _events.setdefault("seen", 0)
    return _events

def _events_save(ev):
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        tmp = EVENTS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(ev, f, ensure_ascii=False)
        os.replace(tmp, EVENTS_FILE)
    except OSError:
        pass

# категория события каталога → раздел журнала (для фильтров в окне уведомлений)
_EVENT_KIND = {}
for _k in ("disk_add", "disk_remove", "readonly", "fserror", "smart", "smart_wear",
           "disktemp", "slow_disk", "sd_degrade", "usb_import", "disk_remount"):
    _EVENT_KIND[_k] = "disk"
for _k in ("pool", "diskfull", "root_full", "inodes", "docker_space"):
    _EVENT_KIND[_k] = "space"
for _k in ("temp", "throttle", "undervolt", "mem", "swap", "load", "sustained_heat",
           "fan_stall", "proc_hog", "thermal_guard"):
    _EVENT_KIND[_k] = "power"
for _k in ("svcfail", "container", "container_loop", "cron_failed", "boot",
           "reboot_req", "updates", "sec_updates", "time_drift", "weekly",
           "daily_summary", "cfg_corrupt"):
    _EVENT_KIND[_k] = "svc"
for _k in ("panel_new", "panel_fail", "ssh_login"):
    _EVENT_KIND[_k] = "access"
for _k in ("ip_changed", "link_changed", "vpn_offline", "traffic"):
    _EVENT_KIND[_k] = "net"
for _k in ("snap_ok", "snap_err", "scrub_err", "delete_block", "backup", "mergerfs",
           "nas_backup", "nb_conn", "nb_srcmiss", "nb_stale", "nb_size", "nb_dest", "nb_guard", "nb_verify",
           "fsw_corrupt", "fsw_guard", "fsw_root", "fsw_del", "fsw_scan"):
    _EVENT_KIND[_k] = "protect"

def log_event(event, title, msg="", lvl=None, kind=None, desk=None):
    """Записать событие в журнал. lvl: info|ok|warn|crit. desk=None → из настроек
    события (показывать ли плашкой на рабочем столе)."""
    evc = load_monitor().get("events", {}).get(event) or {}
    if lvl is None:
        p = evc.get("priority", 0)
        lvl = "crit" if p >= 2 else "warn" if p >= 1 else "info"
    if lvl not in ("info", "ok", "warn", "crit"):
        lvl = "info"
    if desk is None:
        desk = bool(evc.get("desk", evc.get("priority", 0) >= 1))
    now = int(time.time())
    title = str(title or "")[:160]; msg = str(msg or "")[:500]
    with _events_lock:
        ev = _events_load()
        items = ev["items"]
        # дедуп: то же некритичное событие с тем же заголовком за 4 ч → счётчик ×N.
        # Склеиваем ТОЛЬКО непрочитанные записи: если пользователь уже прочитал
        # старую, повтор должен создать новую (иначе бейдж/плашка не оживут).
        # Критичные всегда добавляются заново — это и есть периодическое напоминание.
        if lvl != "crit":
            for it in reversed(items[-40:]):
                if it.get("event") == event and it.get("title") == title \
                        and it.get("id", 0) > ev.get("seen", 0) \
                        and now - it.get("t", 0) <= 4 * 3600:
                    it["n"] = it.get("n", 1) + 1
                    it["t2"] = now
                    if msg:
                        it["msg"] = msg
                    _events_save(ev)
                    return it["id"]
        ev["seq"] += 1
        items.append({"id": ev["seq"], "t": now, "event": event, "title": title,
                      "msg": msg, "lvl": lvl,
                      "kind": kind or _EVENT_KIND.get(event, "system"),
                      "desk": bool(desk)})
        cutoff = now - EVENTS_DAYS * 86400
        if len(items) > EVENTS_CAP or (items and items[0].get("t", 0) < cutoff):
            ev["items"] = [it for it in items if it.get("t", 0) >= cutoff][-EVENTS_CAP:]
        _events_save(ev)
        _events_cond.notify_all()      # разбудить long-poll ожидающих /api/events
        return ev["seq"]

def events_list(after=0, limit=400, wait=0):
    """wait>0 (сек) — long-poll: если новых событий нет, держим запрос до wait
    секунд, пока log_event не разбудит. Так панель реагирует мгновенно, а не
    ждёт следующего опроса. wait капим ниже таймаута сокета обработчика."""
    try:
        after = int(after or 0); limit = max(1, min(3000, int(limit or 400)))
        wait = max(0, min(25, int(wait or 0)))
    except (ValueError, TypeError):
        after, limit, wait = 0, 400, 0
    deadline = time.time() + wait
    def snapshot(ev):
        items = ev["items"]
        out = [it for it in items if it["id"] > after] if after else list(items)
        return {"events": out[-limit:], "seen": ev["seen"], "seq": ev["seq"],
                "unseen": sum(1 for it in items if it["id"] > ev["seen"])}
    with _events_cond:                          # тот же замок, что _events_lock
        while True:
            ev = _events_load()
            # отдаём сразу: есть новое, либо это обычный опрос (не long-poll)
            if ev["seq"] > after or not after or wait <= 0:
                return snapshot(ev)
            remain = deadline - time.time()
            if remain <= 0:
                return snapshot(ev)             # таймаут — пустой ответ с текущим seq
            _events_cond.wait(min(remain, 10))  # спим; log_event разбудит раньше

def events_seen(eid):
    try:
        eid = int(eid)
    except (ValueError, TypeError):
        return {"ok": False, "log": "плохой id"}
    with _events_lock:
        ev = _events_load()
        ev["seen"] = max(ev["seen"], min(eid, ev["seq"]))
        _events_save(ev)
        return {"ok": True, "seen": ev["seen"]}

def events_clear():
    with _events_lock:
        ev = _events_load()
        ev["items"] = []
        ev["seen"] = ev["seq"]
        _events_save(ev)
    return {"ok": True}

# --- журналирование действий пользователя (вызывается из do_POST) ------------
_SYSD_RU = {"start": "запуск", "stop": "остановка", "restart": "перезапуск",
            "enable": "автозапуск вкл", "disable": "автозапуск выкл", "reload": "reload"}

def _act_title(p, b):
    """Заголовок записи журнала для действия, или None если действие не журналируем."""
    g = lambda k: str(b.get(k) or "")
    if p == "/api/power":
        return {"reboot": "Перезагрузка по команде с панели",
                "poweroff": "Выключение по команде с панели"}.get(g("action"))
    if p == "/api/systemd":
        return "Служба %s: %s" % (g("unit"), _SYSD_RU.get(g("action"), g("action")))
    if p == "/api/stack/action":
        return "Стек %s: %s" % (g("name"), g("action"))
    if p == "/api/container/action":
        return "Контейнер %s: %s" % (g("id")[:12], g("action"))
    if p == "/api/docker/prune":
        return "Docker: очистка (%s)" % g("what")
    if p == "/api/disk/format":
        return None if b.get("dry") else "Форматирование %s → %s (%s)" % (g("dev"), g("fs") or "ext4", g("role") or "data")
    if p == "/api/disk/eject":
        return "Извлечён диск %s" % g("dev")
    if p == "/api/disk/mount":
        return ("Отмонтировано: %s" if b.get("unmount") else "Смонтировано: %s") % g("target")
    if p == "/api/disk/mount-dev":
        return "Подключение диска %s" % g("dev")
    if p == "/api/disk/label":
        return "Метка диска %s → %s" % (g("dev"), g("label"))
    if p == "/api/disk/spindown":
        m = b.get("minutes") or 0
        return "Сон диска %s: %s" % (g("dev"), ("%s мин" % m) if m else "выкл")
    if p == "/api/disk/smart-test":
        return "SMART-тест %s (%s)" % (g("dev"), g("kind") or "short")
    if p == "/api/fs/trash/empty":
        return "Корзина очищена"
    if p == "/api/usb-import/run":
        return "USB-импорт запущен вручную (%s)" % g("dev")
    if p == "/api/usb-import":
        return "Настройки USB-импорта изменены"
    if p == "/api/motd":
        return "SSH-приветствие изменено"
    return None

_ACT_KIND = {"/api/disk/": "disk", "/api/fs/": "files", "/api/power": "svc",
             "/api/systemd": "svc", "/api/stack": "svc", "/api/container": "svc",
             "/api/docker": "svc", "/api/usb-import": "disk"}

def log_action(p, body, result):
    title = _act_title(p, body if isinstance(body, dict) else {})
    if not title:
        return
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    kind = next((v for k, v in _ACT_KIND.items() if p.startswith(k)), "action")
    msg = "" if ok else str((result or {}).get("log") or "")[:300]
    log_event("action", title + ("" if ok else " — ошибка"), msg,
              "ok" if ok else "warn", kind=kind, desk=False)

def _phys_devs():
    try:
        return ["/dev/" + d for d in os.listdir("/dev") if re.match(r"^(sd[a-z]|nvme\d+n\d+)$", d)]
    except OSError:
        return []

def _smart_scan():
    """Один проход smartctl по всем физическим дискам → dict со здоровьем/износом/темп."""
    res = {}
    for dev in _phys_devs():
        j = _smartctl_json(["-n", "standby", "-H", "-A"], dev, timeout=15)   # не будить спящие диски
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

def _removable_devs():
    """Пути съёмных носителей (флешки, SD) вместе с разделами: разделы наследуют
    флаг rm родителя. USB-SATA мосты с постоянными дисками сюда НЕ попадают —
    у них rm=0, и тревога по ним обязана срабатывать."""
    out = set()
    def walk(d, rm):
        rm = rm or d.get("rm") in (True, "1", 1)
        if rm and d.get("path"):
            out.add(d["path"])
        for c in d.get("children") or []:
            walk(c, rm)
    for d in _lsblk():
        walk(d, False)
    return out

def _readonly_mounts():
    """Смонтированное ro, что похоже на сбой ФС. Съёмные носители исключаем:
    флешка/карта, поднятая автомонтом только на чтение (грязный ext4, vfat с
    ошибкой), — обычное дело, а не поломка NAS."""
    out = []
    rem = _safe(_removable_devs, set())
    amb = (automount_state().get("base") or "/media/nas").rstrip("/") + "/"
    for line in _read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        src, mp, fstype, opts = p[0], p[1], p[2], p[3]
        if fstype in ("iso9660", "squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "cgroup2"):
            continue
        if not (mp.startswith("/mnt/") or mp.startswith("/media/")):
            continue
        if mp.startswith(amb) or src in rem:      # съёмное — не наша забота
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
        # 0/143(SIGTERM)/137(SIGKILL) — штатная остановка (docker stop), не сбой
        m = re.search(r"Exited \((\d+)\)", status)
        if state == "exited" and m and int(m.group(1)) not in (0, 143, 137):
            bad.append("%s (упал, код %s)" % (name, m.group(1)))
        elif "unhealthy" in status.lower():
            bad.append("%s (unhealthy)" % name)
    return bad

# --------------------------------------------------------------------------- #
#  Единая отправка события уведомления (проверяет вкл./cooldown/приоритет).
#  Зовётся и из monitor_tick, и из хука входа в панель.
# --------------------------------------------------------------------------- #
def _safe(fn, default=None):
    """Вызвать детектор, проглотив исключение — один сбойный детектор не должен
    останавливать весь monitor_tick."""
    try:
        return fn()
    except Exception:
        return default

def mon_notify(dedup_key, title, msg, event=None):
    cfg = load_monitor()
    ev_name = event or dedup_key.split(":")[0]
    ev = cfg.get("events", {}).get(ev_name, {})
    now = time.time()
    if now - _MON_LAST.get(dedup_key, 0) < cfg.get("cooldown", 1800):
        return False
    _MON_LAST[dedup_key] = now
    try:
        log_event(ev_name, title, msg)      # журнал — всегда
    except Exception:
        pass
    if cfg.get("enabled") and ev.get("on"):
        return push_notify(title, msg, ev.get("priority", 0))
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
        _json_save(_KNOWN_IPS_FILE, sorted(s))
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

def _usb_import_events(since):
    """Новые записи журнала USB-импорта (/var/log/nas-usb-import.log) после метки since.
    → ([(lvl, title, msg, ts), ...], новая метка)."""
    log = _read("/var/log/nas-usb-import.log")
    out, latest = [], since
    for l in log.splitlines()[-30:] if log else []:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$", l)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        if ts <= since:
            continue
        latest = max(latest, ts)
        rest = m.group(2).strip()
        if rest.startswith("import OK ->"):
            out.append(("ok", "USB-импорт завершён", "Скопировано в " + rest.split("->", 1)[1].strip(), ts))
        elif rest.startswith("import FAIL"):
            out.append(("warn", "USB-импорт: ошибка", "Не удалось скопировать (" + rest[len("import FAIL"):].strip() + ") — подробности в /var/log/nas-usb-import.log", ts))
        elif rest.startswith("import ") and "->" in rest:
            out.append(("info", "USB-импорт начат", rest[len("import "):], ts))
    return out, latest

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

def apt_updates(refresh=False):
    """Список пакетов, доступных к обновлению: [{name, cur, new, security}]."""
    if refresh:
        _run(["apt-get", "update"], timeout=180)
    r = _run(["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"], timeout=60)
    pkgs = []
    for l in (r.get("log") or "").splitlines():
        # формат: Inst bash [5.2.15-2] (5.2.15-3 Debian:12/stable [arm64])
        m = re.match(r"^Inst (\S+) \[([^\]]*)\] \((\S+)\s+([^)]*)\)", l)
        if m:
            src = m.group(4)
            pkgs.append({"name": m.group(1), "cur": m.group(2), "new": m.group(3),
                         "security": "security" in src.lower()})
    pkgs.sort(key=lambda p: (not p["security"], p["name"]))
    return {"ok": True, "count": len(pkgs), "packages": pkgs}

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
    # chrony часто держит NTPSynchronized=no, будучи синхронным — сначала спросим сам chrony
    if shutil.which("chronyc"):
        r = _run(["chronyc", "-n", "tracking"], timeout=6)
        log = r.get("log") or ""
        if "Leap status" in log:
            return "Not synchronised" in log
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
    global _MON_BOOT_SENT, _MON_SMART_LAST, _MON_DEVS, _MON_IP, _MON_IFACE, _MON_HEAT, _MON_WEEKLY, _MON_USBIMP
    cfg = load_monitor()
    ev = cfg.get("events", {})
    # Детекция и ЖУРНАЛ работают всегда. Настройки события управляют только
    # доставкой: Pushover = cfg.enabled + ev.on (проверяется в fire),
    # плашка на рабочем столе = ev.desk (проверяется клиентом по журналу).
    on  = lambda k: True
    pri = lambda k: ev.get(k, {}).get("priority", 0)
    thr = lambda k, dv: ev.get(k, {}).get("threshold", dv)
    now = time.time()
    cd = cfg.get("cooldown", 1800)
    if len(_MON_LAST) > 400:            # не копить ключи вечно (ssh:/panel_new: по IP)
        for k in [k for k, v in list(_MON_LAST.items()) if now - v > max(cd * 2, 7200)]:
            _MON_LAST.pop(k, None)
    def fire(key, title, msg, priority=0, ev_name=None, lvl=None):
        if now - _MON_LAST.get(key, 0) < cd:
            return
        _MON_LAST[key] = now
        name = ev_name or key.split(":")[0]
        try:
            log_event(name, title, msg, lvl)
        except Exception:
            pass
        if cfg.get("enabled") and ev.get(name, {}).get("on"):
            push_notify(title, msg, priority)
    s = _safe(stats)
    if not s:                       # без базовых метрик тик пропускаем (следующий повторит)
        return
    host = s.get("host", "NAS")

    # --- запуск системы ---
    if not _MON_BOOT_SENT:
        try:
            log_event("boot", "Система запущена", "%s снова в сети" % host, "ok")
        except Exception:
            pass
        if cfg.get("enabled") and ev.get("boot", {}).get("on"):
            push_notify("NAS: система запущена", "%s снова в сети" % host, pri("boot"))
        _MON_BOOT_SENT = True

    # --- повреждённые файлы настроек (очередь набита загрузчиком) ---
    while _BAD_CONFIGS:
        bad = _BAD_CONFIGS.pop(0)
        fire("cfg_corrupt:%s" % bad, "NAS: файл настроек повреждён",
             "%s не читается — сохранён как %s.bad, применены значения по умолчанию. "
             "Проверьте настройки." % (bad, bad), pri("cfg_corrupt"),
             ev_name="cfg_corrupt", lvl="warn")

    # --- USB авто-импорт: итоги копирования из журнала импорта ---
    imp = _safe(lambda: _usb_import_events(_MON_USBIMP))
    if imp:
        evs, _MON_USBIMP = imp
        for lvl_, title_, msg_, ts_ in evs:
            fire("usbimp:%d" % int(ts_), title_, msg_, pri("usb_import"),
                 ev_name="usb_import", lvl=lvl_)

    # --- подключение / отключение дисков (по изменению набора томов) ---
    vols = _safe(_block_volumes)
    if vols is not None:            # при сбое не портим _MON_DEVS (иначе ложные add/remove)
        if _MON_DEVS is not None:
            added   = [vols[d] for d in vols if d not in _MON_DEVS]
            removed = [_MON_DEVS[d] for d in _MON_DEVS if d not in vols]
            if added and on("disk_add"):
                fire("disk_add", "NAS: диск подключён", "Появился: " + ", ".join(map(str, added)), pri("disk_add"))
            if removed and on("disk_remove"):
                fire("disk_remove", "NAS: диск отключён", "Пропал: " + ", ".join(map(str, removed)), pri("disk_remove"))
        _MON_DEVS = vols

    # --- авто-освежение анализатора места (сам троттлит до раза в 15 мин) ---
    _safe(lambda: _duscan_auto(load_maintenance().get("duscan_hours", 0)))
    # --- авто-синхронизация firewall: держать открытыми нужные порты (панель/SSH/
    #     Cockpit/шары + docker), чтобы новый контейнер/смена порта не остались за UFW ---
    _safe(ufw_autosync)

    # --- файловая система в режиме «только чтение» (риск данных) ---
    if on("readonly"):
        ro = _safe(_readonly_mounts, [])
        if ro:
            fire("readonly", "NAS: диск только для чтения",
                 "Смонтировано ro (сбой ФС?): " + ", ".join(ro), pri("readonly"))

    # --- ошибки ФС/ввода-вывода в журнале ядра ---
    if on("fserror"):
        errs = _safe(_kernel_fs_errors, [])
        if errs:
            fire("fserror", "NAS: ошибки диска в логе ядра", "\n".join(errs), pri("fserror"))

    # --- Pi: температура / троттлинг / память / swap / нагрузка ---
    t = s.get("temp")
    if on("temp") and t and t >= thr("temp", 75):
        fire("temp", "NAS: перегрев", "Температура %s°C (порог %s°C)" % (t, thr("temp", 75)), pri("temp"))
    tr = s.get("throttled") or {}
    # Просадка — это про блок питания, троттлинг — про охлаждение. Разные события:
    # флаг просадки гаснет только при снятии питания, так что сообщить надо сразу.
    if on("undervolt") and tr.get("undervolt"):
        fire("undervolt", "NAS: просадка питания",
             "Блоку питания не хватает тока (флаги %s) — плата может внезапно выключиться. "
             "Проверьте БП и кабель." % tr.get("raw", ""), pri("undervolt"), lvl="warn")
    if on("throttle") and tr.get("throttle"):
        fire("throttle", "NAS: троттлинг частоты",
             "CPU снизил частоту (флаги %s)" % tr.get("raw", ""), pri("throttle"))
    m = (s.get("mem") or {}).get("pct", 0)
    if on("mem") and m >= thr("mem", 92):
        fire("mem", "NAS: мало памяти", "RAM занята на %s%%" % m, pri("mem"))
    mem = _safe(mem_info, {})
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
        for mp, pct in _safe(_data_mounts_usage, []):
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
        bad = _safe(_bad_containers, [])
        if bad:
            fire("container", "NAS: проблема с контейнером", "; ".join(bad[:8]), pri("container"))

    # --- требуется перезагрузка (обновления ядра/libc) ---
    if on("reboot_req") and os.path.exists("/var/run/reboot-required"):
        fire("reboot_req", "NAS: нужна перезагрузка", "Обновления применятся после ребута", pri("reboot_req"))

    # --- SMART: единым проходом раз в N минут (настройка smart_scan_min) ---
    _scan_s = max(300, (_safe(load_maintenance, {}) or {}).get("smart_scan_min", 10) * 60)
    if (on("smart") or on("smart_wear") or on("disktemp")) and now - _MON_SMART_LAST >= _scan_s:
        _MON_SMART_LAST = now
        scan = _safe(_smart_scan, {})
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
                    fire("wear:" + dev, "NAS: износ диска", "%s — %s" % (dev, ", ".join(bad)), pri("smart_wear"), ev_name="smart_wear")
            if on("disktemp") and isinstance(d.get("temp"), int) and d["temp"] >= thr("disktemp", 60):
                fire("dtemp:" + dev, "NAS: диск перегрет", "%s — %s°C" % (dev, d["temp"]), pri("disktemp"), ev_name="disktemp")

    # --- контейнеры: restart-loop + распухший docker ---
    if shutil.which("docker"):
        if on("container_loop"):
            loops = _safe(_restart_loops, [])
            if loops:
                fire("cloop", "NAS: контейнер не поднимается", "Перезапускается по кругу: " + ", ".join(loops[:8]), pri("container_loop"), ev_name="container_loop")
        if on("docker_space") and _hourly("docker_space"):
            gb = _safe(_docker_reclaimable_gb, 0) or 0
            if gb >= thr("docker_space", 20):
                fire("dspace", "NAS: docker распух", "Можно освободить ~%s ГБ (prune)" % gb, pri("docker_space"), ev_name="docker_space")

    # --- SSH-вход ---
    if on("ssh_login"):
        for user, ip in _safe(_ssh_logins, []):
            fire("ssh:" + ip, "NAS: вход по SSH", "%s с %s" % (user, ip), pri("ssh_login"), ev_name="ssh_login")

    # --- сеть: смена IP / линка / VPN (через fire → cooldown, не спамим при мерцании) ---
    ip = s.get("ip")
    if _MON_IP is not None and ip and ip != _MON_IP and on("ip_changed"):
        fire("ip_changed", "NAS: сменился IP", "Было %s → стало %s" % (_MON_IP, ip), pri("ip_changed"))
    _MON_IP = ip or _MON_IP
    iface = s.get("iface")
    # только реальные переходы между непустыми интерфейсами (иначе мерцание null → спам)
    if _MON_IFACE is not None and iface and iface != _MON_IFACE and on("link_changed"):
        fire("link_changed", "NAS: смена сети", "Активный интерфейс: %s → %s" % (_MON_IFACE, iface), pri("link_changed"))
    _MON_IFACE = iface or _MON_IFACE
    if on("vpn_offline") and _safe(_tailscale_offline):
        fire("vpn", "NAS: VPN offline", "Tailscale не в сети — удалённый доступ недоступен", pri("vpn_offline"), ev_name="vpn_offline")

    # --- здоровье бэкапа главного NAS (связь/папки/давно/размер/место) ---
    _safe(lambda: nb_health_tick(fire, ev, pri, thr, now))
    # --- защита данных: SnapRAID + mergerfs + бэкап ---
    # берём последние sync/scrub с датой (snapraid_status) и включаем дату в ключ дедупа —
    # так каждое событие уведомляет ОДИН раз, а не каждый cooldown, пока строка в хвосте лога
    sn = _safe(snapraid_status, {}) or {}
    ls, lsc = sn.get("last_sync") or {}, sn.get("last_scrub") or {}
    if on("snap_ok") and ls.get("result") == "ok":
        fire("snapok:" + str(ls.get("date")), "NAS: SnapRAID sync ок", "Синхронизация чётности прошла (%s)" % (ls.get("date") or ""), pri("snap_ok"), ev_name="snap_ok", lvl="ok")
    if on("snap_err") and ls.get("result") == "err":
        fire("snaperr:" + str(ls.get("date")), "NAS: SnapRAID sync ошибка", "Синхронизация чётности не удалась (%s)" % (ls.get("date") or ""), pri("snap_err"), ev_name="snap_err")
    if on("scrub_err") and lsc.get("result") == "err":
        fire("scruberr:" + str(lsc.get("date")), "NAS: SnapRAID scrub ошибка", "Проверка нашла проблему (%s)" % (lsc.get("date") or ""), pri("scrub_err"), ev_name="scrub_err")
    if on("delete_block") and sn.get("blocked"):
        fire("delblk", "NAS: sync остановлен защитой", sn["blocked"], pri("delete_block"), ev_name="delete_block")
    if on("mergerfs"):
        miss = _safe(_mergerfs_missing, [])
        if miss:
            fire("mfs", "NAS: диск выпал из пула", "Не смонтированы: " + ", ".join(miss), pri("mergerfs"), ev_name="mergerfs")
    if on("backup"):
        blog = _read("/var/log/nas-backup.log")
        if blog:
            last = blog.splitlines()[-1]
            if re.search(r"\b(FAIL|ошибка|error)\b", last, re.I):
                fire("bkp", "NAS: бэкап не удался", last[-160:], pri("backup"), ev_name="backup", lvl="warn")
            elif re.search(r"\b(OK|успешно|done)\b", last, re.I):
                fire("bkp", "NAS: бэкап выполнен", last[-160:], pri("backup"), ev_name="backup", lvl="ok")

    # --- обслуживание ---
    root = _safe(lambda: disk_info("/")) or {}
    if on("root_full") and root.get("pct", 0) >= thr("root_full", 90):
        fire("rootfull", "NAS: мало места на системной карте", "Раздел / занят на %s%%" % root.get("pct"), pri("root_full"), ev_name="root_full")
    if on("sd_degrade"):
        sd = _safe(_sd_errors, [])
        if sd:
            fire("sderr", "NAS: сбои SD-карты", "\n".join(sd), pri("sd_degrade"), ev_name="sd_degrade")
    if on("sustained_heat"):
        hot = (t and t >= thr("temp", 75)) or not tr.get("ok", True)
        _MON_HEAT = _MON_HEAT + 1 if hot else 0
        if _MON_HEAT >= thr("sustained_heat", 10):
            fire("heat", "NAS: держится перегрев/троттлинг", "Уже %d мин подряд — проверьте охлаждение/питание" % _MON_HEAT, pri("sustained_heat"), ev_name="sustained_heat")
    if on("fan_stall"):
        rpm = _safe(_fan_rpm)
        if rpm == 0 and t and t >= thr("temp", 75):
            fire("fan", "NAS: вентилятор стоит", "0 об/мин при %s°C — проверьте кулер" % t, pri("fan_stall"), ev_name="fan_stall")
    if on("cron_failed"):
        cf = _safe(_cron_failures, [])
        if cf:
            fire("cron", "NAS: задача по расписанию упала", "Ошибка: " + ", ".join(map(str, cf)), pri("cron_failed"), ev_name="cron_failed")
    if on("time_drift") and _safe(_ntp_unsynced):
        fire("ntp", "NAS: время не синхронизировано", "Часы могут уплыть — проверьте chrony/timesyncd", pri("time_drift"), ev_name="time_drift")
    if on("updates") and _hourly("updates"):
        n = _safe(_apt_upgradable, 0) or 0
        if n > 0:
            fire("upd", "NAS: доступны обновления", "Можно обновить пакетов: %d" % n, pri("updates"), ev_name="updates")
    if on("sec_updates") and _hourly("sec_updates"):
        su = _safe(_sec_updates_recent, [])
        if su:
            fire("secupd", "NAS: накатились security-обновления", su[-1], pri("sec_updates"), ev_name="sec_updates")

    # --- поведенческие ---
    tx = (s.get("net") or {}).get("tx", 0)
    if on("traffic") and tx >= thr("traffic", 50) * 1024 * 1024:
        fire("traffic", "NAS: большой исходящий трафик", "Отдача %s/с — проверьте, что это ожидаемо" % fmt_bytes(tx), pri("traffic"))
    if on("slow_disk"):
        for dev, aw in (_safe(_diskstat_await, {}) or {}).items():
            if aw >= thr("slow_disk", 250):
                fire("slow:" + dev, "NAS: диск отвечает медленно", "%s — задержка %d мс/операцию" % (dev, round(aw)), pri("slow_disk"), ev_name="slow_disk")
    if on("proc_hog"):
        hog = _safe(lambda: _proc_hog(thr("proc_hog", 80)))
        if hog:
            fire("hog", "NAS: процесс грузит систему", hog, pri("proc_hog"), ev_name="proc_hog")
    if on("inodes"):
        ino = _safe(lambda: _inodes_full(thr("inodes", 90)), [])
        if ino:
            fire("inodes", "NAS: заканчиваются inode", "; ".join(ino), pri("inodes"))

    # --- еженедельный отчёт «жив» ---
    if now - _MON_WEEKLY >= 7 * 86400:
        _MON_WEEKLY = now
        wmsg = ("%s · аптайм %s · CPU %s%% · темп %s°C · пул %s%%"
                % (host, fmt_uptime(s.get("uptime", 0)), s.get("cpu"),
                   s.get("temp") or "—", (pool.get("pct") if pool else "—")))
        try:
            log_event("weekly", "Недельный отчёт", wmsg, "info")
        except Exception:
            pass
        if cfg.get("enabled") and ev.get("weekly", {}).get("on"):
            push_notify("NAS: недельный отчёт", wmsg, pri("weekly"))

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
HISTORY_LONG_FILE = os.path.join(NAS_CONFIG, "history-long.json")
HISTORY_LONG_CAP  = 4600     # шаг 10 мин → ~32 дня
_history = None
_history_long = None
_hist_dirty = 0
_hist_lock = threading.Lock()

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

def _load_history_long():
    global _history_long
    if _history_long is not None:
        return _history_long
    try:
        with open(HISTORY_LONG_FILE) as f:
            _history_long = json.load(f)[-HISTORY_LONG_CAP:]
    except (OSError, ValueError):
        _history_long = []
    return _history_long

# период → (какая серия, срез в секундах, шаг точки)
_HIST_RANGES = {"1h": ("fine", 3600, 60), "6h": ("fine", 6 * 3600, 60),
                "24h": ("fine", 25 * 3600, 60), "7d": ("long", 7 * 86400, 600),
                "30d": ("long", 31 * 86400, 600)}

def history_snapshot(rng="24h"):
    """Копия истории за период под локом — безопасно сериализовать в HTTP-потоке."""
    series, span, step = _HIST_RANGES.get(rng, _HIST_RANGES["24h"])
    cutoff = time.time() - span
    with _hist_lock:
        h = _load_history() if series == "fine" else _load_history_long()
        return {"history": [p for p in h if p.get("t", 0) >= cutoff], "step": step, "range": rng}

def history_sample():
    """Снять одну точку метрик и добавить в историю (зовётся раз в минуту)."""
    global _hist_dirty
    try:
        s = stats()
    except Exception:
        return
    pt = {"t": int(time.time()), "cpu": s.get("cpu"), "temp": s.get("temp"),
          "mem": (s.get("mem") or {}).get("pct"),
          "rx": (s.get("net") or {}).get("rx"), "tx": (s.get("net") or {}).get("tx"),
          "pool": (s.get("disk_pool") or {}).get("pct")}
    snap_long = None
    with _hist_lock:
        h = _load_history()
        h.append(pt)
        if len(h) > HISTORY_CAP:
            del h[:len(h) - HISTORY_CAP]
        _hist_dirty += 1
        write = _hist_dirty >= 5
        if write:
            _hist_dirty = 0
            snap = list(h)
        # длинная серия: раз в 10 минут — агрегат минутных точек (avg, для temp — max)
        hl = _load_history_long()
        last_t = hl[-1]["t"] if hl else 0
        if pt["t"] - last_t >= 600:
            win = [p for p in h if p.get("t", 0) > last_t][-15:]
            def avg(k):
                vs = [p[k] for p in win if isinstance(p.get(k), (int, float))]
                return round(sum(vs) / len(vs), 1) if vs else None
            def mx(k):
                vs = [p[k] for p in win if isinstance(p.get(k), (int, float))]
                return max(vs) if vs else None
            hl.append({"t": pt["t"], "cpu": avg("cpu"), "temp": mx("temp"),
                       "mem": avg("mem"), "rx": avg("rx"), "tx": avg("tx"),
                       "pool": pt.get("pool")})
            if len(hl) > HISTORY_LONG_CAP:
                del hl[:len(hl) - HISTORY_LONG_CAP]
            snap_long = list(hl)
    if write:                            # запись на диск вне лока (не держим мониторинг)
        try:
            os.makedirs(NAS_CONFIG, exist_ok=True)
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, HISTORY_FILE)
        except OSError:
            pass
    if snap_long is not None:
        try:
            os.makedirs(NAS_CONFIG, exist_ok=True)
            tmp = HISTORY_LONG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap_long, f)
            os.replace(tmp, HISTORY_LONG_FILE)
        except OSError:
            pass

# =========================================================================== #
#  Time Machine — этот NAS как приёмник macOS Time Machine (Samba + vfs_fruit +
#  Avahi). Полностью отдельная фича: свой include-конфиг, свой avahi-сервис,
#  своя папка. Движок применения — nas-wizard.sh api timemachine[-off].
# =========================================================================== #
TM_CONF   = "/etc/nas-wizard/timemachine.conf"          # persisted params
TM_INC    = "/etc/samba/nas-timemachine.conf"
TM_AVAHI  = "/etc/avahi/services/nas-timemachine.service"
TM_BUNDLE_EXT = (".sparsebundle", ".backupbundle")

def _tm_read_conf():
    """Прочитать /etc/nas-wizard/timemachine.conf (key=value) → dict."""
    d = {}
    try:
        with open(TM_CONF) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    d[k.strip()] = v.strip()
    except OSError:
        pass
    return d

def _pkg_installed(name):
    try:
        return subprocess.run(["dpkg", "-s", name], capture_output=True,
                              timeout=8).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def tm_status():
    """Состояние приёмника Time Machine: установка, активность, папка, лимит,
    место и список бэкапов Mac (sparsebundle)."""
    conf = _tm_read_conf()
    path = conf.get("path") or (STORAGE + "/TimeMachine")
    user = conf.get("user") or "timemachine"      # dedicated TM-only account (not the system user)
    try:
        quota = int(conf.get("quota") or 0)
    except ValueError:
        quota = 0
    enabled = conf.get("enabled") == "1"
    installed = _pkg_installed("samba")
    active = _safe(lambda: subprocess.run(
        ["systemctl", "is-active", "--quiet", "smbd"], timeout=5).returncode == 0, False)
    advertised = os.path.exists(TM_AVAHI)
    # место на разделе с папкой
    space = None
    try:
        st = os.statvfs(path if os.path.isdir(path) else os.path.dirname(path) or "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        space = {"total": total, "free": free, "used": total - free}
    except OSError:
        pass
    # бэкапы Mac: каждый *.sparsebundle/*.backupbundle — отдельная машина
    backups = []
    try:
        for name in sorted(os.listdir(path)):
            if not name.endswith(TM_BUNDLE_EXT):
                continue
            full = os.path.join(path, name)
            sz = _safe(lambda: _du_bytes(full), 0)
            mt = _safe(lambda: int(os.path.getmtime(full)), 0)
            backups.append({"name": name, "host": name.rsplit(".", 1)[0],
                            "size": sz, "mtime": mt})
    except OSError:
        pass
    return {"installed": installed, "active": active, "advertised": advertised,
            "enabled": enabled, "path": path, "user": user, "quota_gb": quota,
            "hostname": _safe(lambda: socket.gethostname(), ""),
            "space": space, "backups": backups}

# --------------------------------------------------------------------------- #
#  Автообслуживание: ежедневные фоновые задачи (авто-очистка корзины и т.п.)
# --------------------------------------------------------------------------- #
# =========================================================================== #
#  Бэкап главного NAS на этот NAS (rsync-демон или SSH), отдельное мини-приложение
# =========================================================================== #
NB_CONF   = "/etc/nas-os/nas-backup.json"                 # секреты → root 600
NB_QUEUE  = os.path.join(NAS_CONFIG, "nas-backup-queue.json")
NB_MAIN   = "main"                        # id первого (легаси) профиля
NB_MAX_PROFILES = 8
_NB_PID_RE = re.compile(r"^[a-z0-9]{1,12}$")

# Прогон бэкапа запускается ОТДЕЛЬНЫМ процессом в транзиентном systemd-юните
# (вне cgroup службы) → переживает перезапуск/обновление nas-web. Драйвер пишет
# вывод в файл-лог и статус в json; UI/сервер их читают и переподключаются.
# Всё состояние — ПОФАЙЛОВО НА ПРОФИЛЬ. У легаси-профиля имена без суффикса,
# чтобы миграция не потеряла историю и статус существующего бэкапа.
def _nb_f(pid, kind, ext):
    sfx = "" if pid == NB_MAIN else "-" + pid
    return os.path.join(NAS_CONFIG, "nas-backup-%s%s.%s" % (kind, sfx, ext))

def nb_status_file(pid):  return _nb_f(pid, "status",  "json")
def nb_run_log(pid):      return _nb_f(pid, "run",     "log")
def nb_run_state(pid):    return _nb_f(pid, "run",     "json")
def nb_run_cancel(pid):   return _nb_f(pid, "run",     "cancel")
def nb_history_file(pid): return _nb_f(pid, "history", "json")
def nb_health_file(pid):  return _nb_f(pid, "health",  "json")
def nb_unit(pid):         return "nas-backup-run" if pid == NB_MAIN else "nas-backup-run-" + pid

def _nb_run_state_read(pid):
    try:
        with open(nb_run_state(pid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def _nb_run_state_write(pid, d):
    try:
        _json_save(nb_run_state(pid), d)
    except OSError:
        pass

def _nb_unit_active(pid):
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", nb_unit(pid) + ".service"],
                              timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def nb_run_active(pid=NB_MAIN):
    """Идёт ли прогон: флаг в state И живой транзиентный юнит (чтобы не залипало после краха)."""
    st = _nb_run_state_read(pid)
    if not st.get("running"):
        return False
    if _nb_unit_active(pid):
        return True
    # Just spawning: state is written BEFORE systemd-run registers the unit — a poll
    # landing in that window must not mistake the starting run for an orphaned one
    # (it would flip running:=False and log a bogus "aborted" history entry).
    if time.time() - (st.get("started") or 0) < 15:
        return True
    # Orphaned run: state says "running" but the unit is gone (power loss / hard
    # reboot killed it mid-run). Close it out once — otherwise the dock spinner
    # sticks forever — and leave an "aborted" trace in run history.
    st["running"] = False
    st["result"] = st.get("result") or "aborted"
    _nb_run_state_write(pid, st)
    try:
        started = int(st.get("started") or 0)
        _nb_history_add(pid, {"ts": started or int(time.time()), "dur": 0,
                              "result": "aborted", "jobs": []})
    except Exception:
        pass
    return False

def nb_any_active():
    """Идёт ли прогон хоть какого-нибудь профиля (одновременно разрешён один)."""
    return any(nb_run_active(p["id"]) for p in nb_profiles())

# ---- очередь: параллельные прогоны запрещены, лишние ждут (см. _nb_queue_drain) ----
def _nb_queue_read():
    try:
        with open(NB_QUEUE) as f:
            q = json.load(f)
        return [x for x in q if isinstance(x, dict) and x.get("pid")] if isinstance(q, list) else []
    except (OSError, ValueError):
        return []

def _nb_queue_write(q):
    try:
        os.makedirs(NAS_CONFIG, exist_ok=True)
        tmp = NB_QUEUE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(q[:NB_MAX_PROFILES], f)
        os.replace(tmp, NB_QUEUE)
    except OSError:
        pass

def nb_queued(pid):
    return any(x["pid"] == pid for x in _nb_queue_read())

def _nb_queue_add(pid, dry, allow_delete=False):
    q = _nb_queue_read()
    if any(x["pid"] == pid for x in q):
        return False
    q.append({"pid": pid, "dry": bool(dry), "allow_delete": bool(allow_delete),
              "ts": int(time.time())})
    _nb_queue_write(q)
    return True

def _nb_queue_remove(pid):
    q = _nb_queue_read()
    q2 = [x for x in q if x["pid"] != pid]
    if len(q2) != len(q):
        _nb_queue_write(q2)
# разрешённые корни для локальных папок-приёмников (не системные каталоги)
_NB_DEST_OK = ("/mnt/", "/media/", "/srv/", "/home/")

def _nb_defaults():
    # direction: pull = grab from another NAS to here; push = send from this NAS
    # to an external disk (transport=local) or to another server (transport=ssh)
    return {"direction": "pull", "verify": False,
         "transport": "rsync", "host": "", "user": "", "password": "", "ssh_port": 22,
         "remote_sudo": False,
         "dest_mode": "single", "dest_base": "/mnt/storage/nas-backup",
         "jobs": [],
         "excludes": [".DS_Store", "._*", "Thumbs.db", "desktop.ini", "@eaDir/", "#recycle/",
                      "@Recycle/", ".@__thumb/", "$RECYCLE.BIN/", "System Volume Information/",
                      ".Trashes", ".Spotlight-V100", ".fseventsd", "*.tmp", "*.temp", "*.part",
                      "*.crdownload", "~$*",
                      "node_modules/", "__pycache__/", "*.pyc", ".venv/", "venv/", "vendor/",
                      ".git/", ".svn/", ".next/", ".nuxt/", "dist/", "build/", "target/",
                      ".gradle/", ".idea/", ".vscode/", ".pytest_cache/", ".mypy_cache/",
                      "*.egg-info/", ".cache/", "bower_components/", ".turbo/"],
         "delete_mode": "archive", "retention_days": 30, "retention_gb": 0,
         "deleted_dir": "_deleted/{date}", "max_delete_pct": 20, "bwlimit": 0,
         "schedule": {"enabled": False, "freq": "daily", "time": "03:00", "dow": "Sun"}}

def _nb_read_raw():
    try:
        with open(NB_CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def nb_profiles():
    """Все профили бэкапа. Всегда хотя бы один. Старый плоский конфиг (v1)
    читается как единственный профиль «Основной» — на диск ничего не пишем,
    запись делает _nb_migrate() один раз при старте."""
    raw = _nb_read_raw()
    if isinstance(raw, dict) and isinstance(raw.get("profiles"), list):
        items = raw["profiles"]
    elif isinstance(raw, dict) and raw:
        items = [dict(raw, id=NB_MAIN, name=raw.get("name") or "Default")]
    else:
        items = [{"id": NB_MAIN, "name": "Default"}]
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = str(it.get("id") or "")
        if not _NB_PID_RE.match(pid) or pid in seen:
            continue
        seen.add(pid)
        d = _nb_defaults()
        d.update(it)
        d["id"] = pid
        d["name"] = (str(it.get("name") or "").strip() or "Backup")[:40]
        out.append(d)
    if not out:
        d = _nb_defaults(); d.update({"id": NB_MAIN, "name": "Default"}); out = [d]
    return out

def nb_load(pid=None):
    """Профиль по id; без id — первый (совместимость со старым кодом и API)."""
    profs = nb_profiles()
    if pid:
        for p in profs:
            if p["id"] == pid:
                return p
    return profs[0]

def _nb_write_profiles(profs):
    try:
        os.makedirs(os.path.dirname(NB_CONF), exist_ok=True)
        tmp = NB_CONF + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 2, "profiles": profs}, f)
        os.chmod(tmp, 0o600); os.replace(tmp, NB_CONF)
    except OSError:
        pass

def _nb_migrate():
    """v1 (плоский конфиг) → v2 (список профилей). Один раз, с резервной копией."""
    raw = _nb_read_raw()
    if not isinstance(raw, dict) or isinstance(raw.get("profiles"), list):
        return False
    if not raw:                       # конфига ещё нет — писать нечего
        return False
    try:
        shutil.copy2(NB_CONF, NB_CONF + ".v1.bak")
    except OSError:
        pass
    _nb_write_profiles(nb_profiles())
    log_event("info", "Бэкап NAS: конфиг переведён в формат профилей", "", "ok",
              kind="backup", desk=False)
    return True

def _nb_valid_dest(p):
    p = os.path.normpath(str(p or ""))
    return p.startswith(_NB_DEST_OK) and ".." not in p

def _nb_push(cfg):
    return (cfg or {}).get("direction") == "push"

def _nb_push_ssh(cfg):
    return _nb_push(cfg) and (cfg or {}).get("transport") == "ssh"

def _nb_valid_push_dest(p):
    """push-ssh destination: an absolute remote path OR a module-style path (no
    leading /) — NAS boxes like UGREEN/Synology force rsync-over-SSH into daemon
    mode where paths are resolved from a «module»."""
    p = str(p or "").strip()
    return bool(p) and ".." not in p and not p.startswith("-")

def _nb_dest_for(cfg, src):
    """Where one source lands under the common destination folder. Same rule as destFor() in
    the UI: recreate the WHOLE source tree under the base (base/Cloud/Desktop, not base/Desktop)
    so trees can't collide; on push drop the familiar pool prefix, so it reads base/photos
    instead of base/mnt/storage/photos."""
    base = (cfg.get("dest_base") or "").rstrip("/")
    rel = str(src).lstrip("/")
    if _nb_push(cfg):
        rel = re.sub(r"^mnt/storage/", "", rel)
    return os.path.normpath(base + "/" + rel)

def nb_save(patch, pid=None):
    cur = nb_load(pid)
    if patch.get("direction") in ("pull", "push"):
        cur["direction"] = patch["direction"]
    if "verify" in patch:
        cur["verify"] = bool(patch["verify"])
    for k in ("transport", "host", "user", "password", "dest_base", "delete_mode", "dest_mode"):
        if k in patch and isinstance(patch[k], str):
            cur[k] = patch[k].strip()
    if isinstance(patch.get("deleted_dir"), str):
        v = re.sub(r"[^\w \-.{}/А-Яа-яЁё]", "", patch["deleted_dir"]).replace("..", "").strip("/")[:120]
        cur["deleted_dir"] = v or "_deleted/{date}"
    for k in ("ssh_port", "retention_days", "retention_gb", "bwlimit"):
        if k in patch:
            try: cur[k] = max(0, int(patch[k]))
            except (ValueError, TypeError): pass
    if "max_delete_pct" in patch:
        try: cur["max_delete_pct"] = max(0, min(100, int(patch["max_delete_pct"])))
        except (ValueError, TypeError): pass
    if "remote_sudo" in patch:
        cur["remote_sudo"] = bool(patch["remote_sudo"])
    if isinstance(patch.get("jobs"), list):
        jobs = []
        dst_ok = _nb_valid_push_dest if _nb_push_ssh(cur) else _nb_valid_dest
        for j in patch["jobs"][:200]:
            if not isinstance(j, dict): continue
            src = str(j.get("src", "")).strip().rstrip("/")   # ведущий / сохраняем (SSH abs-пути)
            dst = os.path.normpath(str(j.get("dest", "")).strip())
            if not src or not dst_ok(dst): continue
            job = {"src": src, "dest": dst, "enabled": bool(j.get("enabled", True))}
            # per-job исключения: анкорные rsync-паттерны относительно src (ведущий /).
            # так снятие галочки с вложенной папки исключает её, а родитель копирует
            # всё остальное — включая то, что появится в нём позже.
            if isinstance(j.get("excludes"), list):
                ex = []
                for x in j["excludes"]:
                    x = str(x).strip()
                    if not x or ".." in x: continue
                    if not x.startswith("/"): x = "/" + x
                    ex.append(x[:300])
                if ex:
                    job["excludes"] = ex[:200]
            jobs.append(job)
        cur["jobs"] = jobs
    if isinstance(patch.get("excludes"), list):
        cur["excludes"] = [str(x)[:150] for x in patch["excludes"] if str(x).strip()][:100]
    if isinstance(patch.get("schedule"), dict):
        s = patch["schedule"]
        cur["schedule"]["enabled"] = bool(s.get("enabled", cur["schedule"]["enabled"]))
        if s.get("freq") in ("daily", "weekly"): cur["schedule"]["freq"] = s["freq"]
        if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(s.get("time", ""))): cur["schedule"]["time"] = s["time"]
        if s.get("dow") in ("Mon","Tue","Wed","Thu","Fri","Sat","Sun"): cur["schedule"]["dow"] = s["dow"]
    if cur.get("direction") not in ("pull", "push"): cur["direction"] = "pull"
    tr_ok = ("local", "ssh") if _nb_push(cur) else ("rsync", "ssh")
    if cur["transport"] not in tr_ok: cur["transport"] = tr_ok[0]
    # re-validate jobs against the FINAL direction/transport: switching e.g. push-ssh →
    # local must not keep module-relative dests ("HDD6TB/…") that a local run would
    # happily create relative to cwd and fill the system disk with
    if cur["delete_mode"] not in ("archive", "mirror", "add"): cur["delete_mode"] = "archive"
    if cur["dest_mode"] not in ("single", "per"): cur["dest_mode"] = "single"
    # In single-folder mode a job's dest is a pure function of (dest_base, src), so derive it
    # rather than trust the copy stored when the source was added. Otherwise changing the common
    # destination folder moves the base but leaves every existing job writing to the OLD path:
    # the panel shows the new folder while rsync silently keeps filling the previous one
    # (real case 2026-07-12 — base said /media/nas/UNTITLED_2, jobs still went to
    # /mnt/storage/nas-backup-2). Per-job mode is the one where dests are edited by hand.
    per_mode = cur["dest_mode"] == "per" and not _nb_push_ssh(cur)   # same rule as the UI
    if not per_mode and (cur.get("dest_base") or "").strip():
        for j in cur.get("jobs") or []:
            j["dest"] = _nb_dest_for(cur, j["src"])
    final_ok = _nb_valid_push_dest if _nb_push_ssh(cur) else _nb_valid_dest
    cur["jobs"] = [j for j in (cur.get("jobs") or []) if final_ok(j.get("dest", ""))]
    cur["saved"] = int(time.time())   # какой профиль трогали последним — его и открывать
    profs = [cur if p["id"] == cur["id"] else p for p in nb_profiles()]
    _nb_write_profiles(profs)
    return cur

def nb_public(cfg=None):
    """Конфиг для UI без утечки пароля."""
    c = dict(cfg or nb_load())
    c["has_password"] = bool(c.get("password"))
    c["password"] = ""
    return c

def _nb_pid(pid):
    """Нормализовать id профиля: None/мусор/неизвестный -> первый профиль."""
    return nb_load(pid)["id"]

def _nb_qpid(q):
    """id профиля из query (?p=…); пусто -> первый профиль."""
    v = (q.get("p") or [""])[0]
    return v if _NB_PID_RE.match(v or "") else None

def _nb_bpid(b):
    """id профиля из тела POST."""
    v = str((b or {}).get("p") or "")
    return v if _NB_PID_RE.match(v) else None

def nb_profiles_public():
    """Короткая сводка по каждому профилю — для полосы вкладок."""
    out = []
    for p in nb_profiles():
        pid = p["id"]
        # push на локальный диск настроен без хоста — достаточно приёмника и задач
        conn = bool(p.get("host")) or (p.get("direction") == "push" and p.get("transport") == "local"
                                       and bool(p.get("dest_base")))
        st = _nb_run_state_read(pid)
        out.append({"id": pid, "name": p["name"], "direction": p.get("direction") or "pull",
                    "running": nb_run_active(pid), "queued": nb_queued(pid),
                    "jobs": len(p.get("jobs") or []),
                    "configured": bool(conn and p.get("jobs")),
                    # чем окно открывать: «последний, что трогали» = правка конфига или прогон
                    "saved": int(p.get("saved") or 0),
                    "last_run": int(st.get("started") or 0)})
    return out

def _nb_new_pid(existing):
    for _ in range(64):
        pid = secrets.token_hex(3)
        if pid not in existing and _NB_PID_RE.match(pid):
            return pid
    return None

def _nb_free_dest(base, taken):
    """Уникальная папка-приёмник: два профиля в одну базу писать не должны —
    их «защита от массового удаления» и архив _deleted перемешаются."""
    cand, n = base, 2
    while cand in taken and n < 50:
        cand = "%s-%d" % (base.rstrip("/"), n); n += 1
    return cand

def nb_profile_add(name="", clone_from="", direction=""):
    profs = nb_profiles()
    if len(profs) >= NB_MAX_PROFILES:
        return {"ok": False, "log": "больше %d профилей нельзя" % NB_MAX_PROFILES}
    pid = _nb_new_pid({p["id"] for p in profs})
    if not pid:
        return {"ok": False, "log": "не удалось выделить id"}
    # имя по умолчанию нейтральное к языку: панель по умолчанию английская,
    # а имя профиля — данные, через i18n они не проходят
    name = str(name or "").strip()[:40]
    if not name:
        used = {p["name"] for p in profs}
        n = 2
        while ("Backup %d" % n) in used and n < 99:
            n += 1
        name = "Backup %d" % n
    if clone_from:
        src = next((p for p in profs if p["id"] == clone_from), None)
        if not src:
            return {"ok": False, "log": "нет такого профиля"}
        # копируем подключение, исключения и политики; ИСТОЧНИКИ и расписание — нет:
        # пути на другом NAS другие, а два включённых расписания сразу — сюрприз
        new = dict(src)
        new["jobs"] = []
        new["schedule"] = dict(src.get("schedule") or {}, enabled=False)
    else:
        new = _nb_defaults()
        if direction == "push":
            # push: the user picks the destination (external disk/server) — no guessable default
            new["direction"] = "push"; new["transport"] = "local"; new["dest_base"] = ""
    new["id"] = pid
    new["name"] = name
    if not _nb_push(new):
        taken = {p.get("dest_base") for p in profs}
        new["dest_base"] = _nb_free_dest(new.get("dest_base") or "/mnt/storage/nas-backup", taken)
    profs.append(new)
    _nb_write_profiles(profs)
    log_event("action", "Бэкап NAS: создан профиль «%s»" % name, "", "ok", kind="backup", desk=False)
    return {"ok": True, "id": pid, "config": nb_public(new)}

def nb_profile_rename(pid, name):
    name = str(name or "").strip()[:40]
    if not name:
        return {"ok": False, "log": "пустое имя"}
    profs = nb_profiles()
    if not any(p["id"] == pid for p in profs):
        return {"ok": False, "log": "нет такого профиля"}
    for p in profs:
        if p["id"] == pid:
            p["name"] = name
    _nb_write_profiles(profs)
    return {"ok": True}

def nb_profile_delete(pid, confirm=""):
    """Защита от дурака: последний профиль не удалить; сначала убрать все источники;
    имя надо ввести вручную. Данные в приёмнике НЕ трогаем — исчезает только
    конфиг, история и логи."""
    profs = nb_profiles()
    if len(profs) <= 1:
        return {"ok": False, "log": "последний профиль удалить нельзя"}
    p = next((x for x in profs if x["id"] == pid), None)
    if not p:
        return {"ok": False, "log": "нет такого профиля"}
    if p.get("jobs"):
        return {"ok": False, "log": "сначала уберите все источники (%d осталось)" % len(p["jobs"])}
    if nb_run_active(pid):
        return {"ok": False, "log": "идёт прогон — сначала остановите"}
    if str(confirm or "").strip() != p["name"]:
        return {"ok": False, "log": "имя профиля не совпало"}
    _nb_queue_remove(pid)
    _nb_write_profiles([x for x in profs if x["id"] != pid])
    for f in (nb_status_file(pid), nb_run_log(pid), nb_run_state(pid),
              nb_run_cancel(pid), nb_history_file(pid), nb_health_file(pid)):
        try: os.remove(f)
        except OSError: pass
    log_event("action", "Бэкап NAS: удалён профиль «%s»" % p["name"],
              "скопированные данные в приёмнике остались нетронутыми", "ok", kind="backup", desk=False)
    return {"ok": True}

def _nb_remote_env(cfg):
    """(префикс удалённого пути, env, доп.аргументы rsync) для выбранного транспорта."""
    user, host = cfg.get("user", ""), cfg.get("host", "")
    if cfg.get("transport") == "local":          # push на диск этого NAS — без сети
        return ("", dict(_C_ENV), [])
    if cfg.get("transport") == "ssh":
        port = int(cfg.get("ssh_port", 22) or 22)
        pw = cfg.get("password", "")
        base = "ssh -p %d -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10" % port
        if pw and shutil.which("sshpass"):                # пароль по SSH через sshpass (env SSHPASS)
            return ("%s@%s:" % (user, host), dict(_C_ENV, SSHPASS=pw), ["-e", "sshpass -e " + base])
        return ("%s@%s:" % (user, host), _C_ENV, ["-e", base + " -o BatchMode=yes"])   # без пароля — по ключу
    env = dict(_C_ENV, RSYNC_PASSWORD=cfg.get("password", ""))
    return ("%s@%s::" % (user, host), env, [])            # rsync-демон — пароль через RSYNC_PASSWORD

def _nb_ssh_run(cfg, remote_cmd, timeout=30):
    """Run a command on the remote side of a push-ssh profile (mkdir/archive cleanup)."""
    port = int(cfg.get("ssh_port", 22) or 22)
    argv = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
    env = dict(_C_ENV)
    pw = cfg.get("password", "")
    if pw and shutil.which("sshpass"):
        env["SSHPASS"] = pw
        argv = ["sshpass", "-e"] + argv
    else:
        argv += ["-o", "BatchMode=yes"]
    tgt = "%s@%s" % (cfg.get("user", ""), cfg.get("host", ""))
    return _run(argv + [tgt, remote_cmd], timeout=timeout, env=env)

def _nb_err(raw):
    """Короткое человекочитаемое объяснение ошибки rsync/ssh (без простыни на весь экран)."""
    low = (raw or "").lower()
    if "sshpass" in low and ("not found" in low or "no such" in low):
        return "sshpass не установлен (нужен для пароля по SSH)"
    if "sudo:" in low or "not in the sudoers" in low:      # sudo на источнике (до общей проверки пароля)
        if "not allowed" in low or "not in the sudoers" in low or "may not run" in low:
            return "sudo на источнике не разрешён для rsync — добавьте правило NOPASSWD в sudoers"
        if "command not found" in low:
            return "на источнике не найден sudo"
        return "на источнике sudo требует пароль/TTY — нужен NOPASSWD sudo для rsync (см. подсказку у тумблера)"
    if "invalid path" in low:
        return ("приёмник принимает только пути от «модуля» (NAS с принудительным rsync-демоном) — "
                "укажите путь БЕЗ ведущего /, напр. HDD6TB/Downloads/backup; «Проверить» покажет доступные корни")
    if "permission denied" in low or "auth" in low or "password" in low:
        return "доступ отклонён — проверьте пользователя и пароль (или ключ)"
    if "connection refused" in low:
        return "соединение отклонено — служба/порт недоступны на источнике"
    if "timed out" in low or "timeout" in low:
        return "таймаут соединения"
    if "@error" in low:
        return "rsync-демон отклонил запрос (модуль или пароль)"
    if "host key" in low or "remote host identification" in low:
        return "проблема с host-key SSH источника"
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    return (lines[-1] if lines else "неизвестная ошибка")[:180]

def nb_test(cfg=None):
    """Connectivity test + module list (for rsync daemon). For push-local —
    checks that a destination folder is chosen and its disk is mounted."""
    cfg = cfg or nb_load()
    if cfg.get("transport") == "local":
        base = cfg.get("dest_base") or ""
        if not base:
            return {"ok": False, "log": "не выбрана папка-приёмник"}
        if _dest_disk_absent(base):
            return {"ok": False, "log": "диск приёмника не смонтирован (%s)" % base}
        return {"ok": True, "log": "приёмник на месте"}
    if not cfg.get("host") or not cfg.get("user"):
        return {"ok": False, "log": "не заданы адрес/пользователь"}
    if subprocess.run(["ping", "-c", "1", "-W", "3", "--", cfg["host"]],
                      capture_output=True, timeout=10).returncode != 0:
        return {"ok": False, "log": "%s не отвечает на ping" % cfg["host"]}
    remote, env, rsh = _nb_remote_env(cfg)
    if cfg.get("transport") == "ssh":
        if cfg.get("password") and not shutil.which("sshpass"):
            return {"ok": False, "log": "для пароля по SSH нужен sshpass (переустановите/обновите систему) — или используйте ключ"}
        extra = ["--rsync-path=sudo rsync"] if cfg.get("remote_sudo") else []   # проверяем и сам sudo-путь
        # push: list the root WITHOUT «/» — on a forced-rsync-daemon NAS
        # (UGREEN/Synology) «/» is an invalid path while an empty path lists modules
        r = _run(["rsync"] + rsh + extra + ["--list-only", remote if _nb_push(cfg) else remote + "/"],
                 timeout=25, env=env)
        ok_msg = "SSH-подключение работает" + (" · sudo на источнике ок" if cfg.get("remote_sudo") else "")
        if r["ok"] and _nb_push(cfg):
            roots = []
            for l in (r.get("log") or "").splitlines():
                m = re.match(r"^d\S*\s+[\d,]+\s+\S+\s+\S+\s+(.+)$", l)
                if m and m.group(1) not in (".", ""):
                    roots.append(m.group(1))
            if roots:
                ok_msg += " · корни: " + ", ".join(roots[:8])
        return {"ok": r["ok"], "log": ok_msg if r["ok"] else _nb_err(r["log"])}
    r = subprocess.run(["rsync", remote], capture_output=True, text=True, env=env, timeout=25)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "auth failed" in out or "@ERROR" in out:
        return {"ok": False, "log": _nb_err(out)}
    mods = [l.split("\t")[0].split()[0] for l in out.splitlines() if l.strip() and not l.startswith("@")]
    return {"ok": True, "modules": [m for m in mods if m], "log": "подключение работает"}

_NB_JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized",
            ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".apdisk"}
def _nb_is_junk(name):
    # мусорные служебные файлы ОС — не показываем в пикере (AppleDouble ._*, .DS_Store и пр.)
    return name in _NB_JUNK or name.startswith("._")

_NB_ROOT_SKIP = {"proc", "sys", "dev", "run", "tmp", "boot", "lost+found"}

def _nb_ls(cfg, spec, timeout=30):
    """--list-only of a remote path (spec is appended to the transport prefix as-is)."""
    remote, env, rsh = _nb_remote_env(cfg)
    try:
        r = subprocess.run(["rsync"] + rsh + ["--list-only", "--no-h", remote + spec],
                           capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "log": str(e)}
    if r.returncode != 0:
        return {"ok": False, "log": (r.stderr or r.stdout)[-300:]}
    entries = []
    for l in r.stdout.splitlines():
        m = re.match(r"^(.)\S*\s+[\d,]+\s+\S+\s+\S+\s+(.+)$", l)
        if not m: continue
        name = m.group(2)
        if name in (".", "") or _nb_is_junk(name): continue
        entries.append({"name": name, "dir": m.group(1) == "d"})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"ok": True, "entries": entries}

def _nb_remote_shell_fs(cfg):
    """True = remote rsync sees the real filesystem (plain shell mode).
    False = forced rsync daemon (UGREEN/Synology): paths only from «modules», and
    SSH shell commands live in a DIFFERENT path namespace. The probe is /etc/: it
    exists on any Linux, while a module named like that is exotic."""
    return bool(_nb_ls(cfg, "/etc/", timeout=15).get("ok"))

def nb_browse_dest(cfg, path):
    """push-ssh destination folder picker: walks the remote side. The root depends
    on the server mode: shell — «/», forced daemon — the module list."""
    if (cfg or {}).get("transport") != "ssh":
        return {"ok": False, "log": "приёмник не SSH"}
    path = str(path or "").strip().rstrip("/")
    if ".." in path:
        return {"ok": False, "log": "недопустимый путь"}
    if not path:
        if _nb_remote_shell_fs(cfg):
            r = _nb_ls(cfg, "/")
            return dict(r, path="", abs=True) if r.get("ok") else \
                {"ok": False, "log": _nb_err(r.get("log") or "")}
        r = _nb_ls(cfg, "")
        return dict(r, path="", abs=False) if r.get("ok") else \
            {"ok": False, "log": _nb_err(r.get("log") or "")}
    r = _nb_ls(cfg, path + "/")
    if not r.get("ok"):
        return {"ok": False, "log": _nb_err(r.get("log") or "")}
    return dict(r, path=path)

def nb_browse(cfg, path):
    """Source folder/file listing for the visual picker (path='' → root).
    push: the source is THIS NAS — walk the local FS (paths without a leading /)."""
    cfg = cfg or nb_load()
    path = str(path or "").strip().rstrip("/")
    if _nb_push(cfg):
        path = path.lstrip("/")
        if ".." in path:
            return {"ok": False, "log": "недопустимый путь"}
        base = "/" + path if path else "/"
        entries = []
        try:
            with os.scandir(base) as it:
                for e in it:
                    if _nb_is_junk(e.name):
                        continue
                    if not path and (e.name in _NB_ROOT_SKIP or e.name.startswith(".")):
                        continue   # at the root show only meaningful branches
                    try:
                        entries.append({"name": e.name, "dir": e.is_dir(follow_symlinks=False)})
                    except OSError:
                        pass
        except OSError as e:
            return {"ok": False, "log": str(e)}
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"ok": True, "path": path, "entries": entries}
    remote, env, rsh = _nb_remote_env(cfg)
    if cfg.get("transport") == "rsync" and not path:
        # корень rsync-демона = список модулей
        t = nb_test(cfg)
        if not t.get("ok"): return {"ok": False, "log": t.get("log", "")}
        return {"ok": True, "path": "", "entries": [{"name": m, "dir": True} for m in t.get("modules", [])]}
    src = remote + (path + "/" if path else "/")
    r = subprocess.run(["rsync"] + rsh + ["--list-only", "--no-h", src],
                       capture_output=True, text=True, env=env, timeout=30)
    if r.returncode != 0:
        return {"ok": False, "log": (r.stderr or r.stdout)[-160:]}
    entries = []
    for l in r.stdout.splitlines():
        # формат: "drwxr-xr-x  4096 2024/01/01 12:00:00 имя"
        m = re.match(r"^(.)\S*\s+[\d,]+\s+\S+\s+\S+\s+(.+)$", l)
        if not m: continue
        name = m.group(2)
        if name in (".", ""): continue
        if _nb_is_junk(name): continue        # не засорять пикер мусором (.DS_Store, ._*, Thumbs.db…)
        entries.append({"name": name, "dir": m.group(1) == "d"})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"ok": True, "path": path, "entries": entries}

def _nb_prune(cfg):
    """Ретеншен архива удалённых (_deleted/ДАТА): по дням И по суммарному размеру (ГБ)."""
    days = int(cfg.get("retention_days", 0) or 0)
    gb   = int(cfg.get("retention_gb", 0) or 0)
    snaps = []   # (mtime, path, size)
    dests = set(j["dest"] for j in cfg.get("jobs", [])) | {cfg.get("dest_base", "")}
    top = nb_deleted_top(cfg)
    for base in dests:
        d = os.path.join(base, top)
        if not os.path.isdir(d): continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isdir(p): continue
            try:
                sz = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs
                         if os.path.exists(os.path.join(r, f)))
                snaps.append([os.path.getmtime(p), p, sz])
            except OSError:
                pass
    removed = 0
    now = time.time()
    if days > 0:
        for mt, p, _ in list(snaps):
            if now - mt > days * 86400:
                try: shutil.rmtree(p); removed += 1; snaps.remove([mt, p, _])
                except OSError: pass
    if gb > 0:
        snaps.sort()  # старые сначала
        total = sum(s[2] for s in snaps)
        cap = gb * 1024**3
        for mt, p, sz in snaps:
            if total <= cap: break
            try: shutil.rmtree(p); total -= sz; removed += 1
            except OSError: pass
    return removed

# month names for {month-name} — ALWAYS English so folders never depend on locale
_NB_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]

def _nb_render_tpl(tpl, t=None):
    """Раскрыть токены шаблона папки удалённых ({date}/{year}/{month}/… как в USB-импорте)."""
    t = t or time.localtime()
    rep = {"{date}": time.strftime("%Y-%m-%d", t), "{time}": time.strftime("%H-%M-%S", t),
           "{datetime}": time.strftime("%Y-%m-%d_%H-%M-%S", t), "{year}": time.strftime("%Y", t),
           "{month}": time.strftime("%m", t), "{month-name}": _NB_MONTHS[t.tm_mon],
           "{day}": time.strftime("%d", t), "{hour}": time.strftime("%H", t),
           "{minute}": time.strftime("%M", t)}
    s = tpl or ""
    for k, v in rep.items():
        s = s.replace(k, v)
    s = re.sub(r"\{[^}]*\}", "", s)                              # выкинуть неизвестные токены
    s = re.sub(r"[^\w \-.А-Яа-яЁё/]", "", s).replace("..", "")
    s = re.sub(r"/+", "/", s).strip("/")
    return s

def nb_deleted_top(cfg):
    """Верхняя (статическая) папка архива удалённых — для exclude и ретеншена."""
    top = _nb_render_tpl((cfg.get("deleted_dir") or "_deleted/{date}").split("/")[0])
    return top or "_deleted"

def nb_deleted_rel(cfg, t=None):
    rel = _nb_render_tpl(cfg.get("deleted_dir") or "_deleted/{date}", t)
    return rel or ("_deleted/" + time.strftime("%Y-%m-%d", t or time.localtime()))

def nb_build_cmd(cfg, job, dry, prev_files=0, mkpath=False, allow_delete=False):
    """rsync-команда (+env) для одной задачи. prev_files — число файлов в прошлый
    прогон (для защиты --max-delete по проценту)."""
    remote, env, rsh = _nb_remote_env(cfg)
    dest = job["dest"].rstrip("/") + "/"
    owner = TARGET_USER
    limited = nb_dest_fs(cfg, job.get("dest")) in NB_FS_LIMITED
    if limited:
        # exFAT/NTFS/FAT: симлинков и спецфайлов там не бывает — не «-l»/«-D», и rsync
        # просто пропустит их (код 0), вместо того чтобы падать в 23 каждый прогон.
        # --modify-window=1: у FAT-подобных время файла округлено до 2 с, без этого
        # rsync считает файлы изменившимися и гоняет их заново КАЖДЫЙ раз.
        # --chown тоже не нужен: владельца такая ФС не хранит (он берётся из uid= в mount)
        args = ["rsync", "-rt", "--modify-window=1", "--info=progress2", "--stats",
                "--no-inc-recursive", "--no-owner", "--no-group", "--no-perms"] + rsh
    else:
        args = ["rsync", "-rltD", "--info=progress2", "--stats", "--no-inc-recursive",
                "--no-owner", "--no-group", "--no-perms"] + rsh
        if not _nb_push_ssh(cfg):   # local receiver — store files under the panel owner
            args.append("--chown=%s:%s" % (owner, owner))
    dm = cfg.get("delete_mode", "archive")
    if dm in ("archive", "mirror"):
        args.append("--delete")
        # защита: не удалять больше N% файлов (от числа в прошлый прогон) — иначе rsync
        # выходит с кодом 25 и НИЧЕГО не удаляет. Спасает от «источник стёрли/размонтировали».
        pct = int(cfg.get("max_delete_pct", 20) or 0)
        if pct > 0 and prev_files > 0 and not allow_delete:
            args.append("--max-delete=%d" % max(1, int(prev_files * pct / 100.0)))
    if dm == "archive":
        # template with tokens, default _deleted/{date}. A RELATIVE --backup-dir is
        # resolved by rsync against the destination dir — required for module-style
        # push-ssh dests (joining them would double-nest: dest/HDD6TB/…/dest/_deleted)
        snap = nb_deleted_rel(cfg) if (_nb_push_ssh(cfg) and not job["dest"].startswith("/")) \
            else os.path.join(dest, nb_deleted_rel(cfg))
        args += ["--backup", "--backup-dir=" + snap]
        args.append("--exclude=/" + nb_deleted_top(cfg)) # не бэкапить сам архив удалённых
    for ex in cfg.get("excludes", []):
        args.append("--exclude=" + ex)
    for ex in job.get("excludes", []):          # per-job: снятые в дереве вложенные папки
        args.append("--exclude=" + str(ex))
    bw = int(cfg.get("bwlimit", 0) or 0)
    if bw > 0:
        args.append("--bwlimit=%d" % bw)                 # КБ/с
    if cfg.get("transport") == "ssh" and cfg.get("remote_sudo"):
        args.append("--rsync-path=sudo rsync")   # читать файлы без доступа у пользователя (нужен NOPASSWD sudo на источнике)
    if mkpath:
        args.append("--mkpath")   # принудительный демон: папки в модуле создаёт сам rsync (≥3.2.3)
    if dry:
        args.append("--dry-run")
    args += _nb_src_dst(cfg, job, remote)
    return args, env

def _nb_src_dst(cfg, job, remote):
    """[src, dst] for rsync by direction: pull — remote source → local destination;
    push — local source → destination (local disk or the remote prefix)."""
    dest = job["dest"].rstrip("/") + "/"
    if _nb_push(cfg):
        return ["/" + job["src"].lstrip("/") + "/", remote + dest]
    return [remote + job["src"] + "/", dest]

def nb_verify_cmd(cfg, job):
    """Post-run verify command: rsync --checksum --dry-run re-reads files on both
    sides; every «>f…» line = a file whose content differs from the source."""
    remote, env, rsh = _nb_remote_env(cfg)
    args = ["rsync", "-rltDn", "--checksum", "--out-format=%i %n"] + rsh
    if cfg.get("delete_mode", "archive") == "archive":
        args.append("--exclude=/" + nb_deleted_top(cfg))
    for ex in cfg.get("excludes", []):
        args.append("--exclude=" + ex)
    for ex in job.get("excludes", []):
        args.append("--exclude=" + str(ex))
    if cfg.get("transport") == "ssh" and cfg.get("remote_sudo"):
        args.append("--rsync-path=sudo rsync")
    args += _nb_src_dst(cfg, job, remote)
    return args, env

def _mountpoint_of(p):
    """Ближайшая вверх точка монтирования для пути (существующего или нет)."""
    p = os.path.abspath(p)
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    return p

# Filesystems that cannot hold what a Linux backup normally carries: no symlinks,
# no sockets/fifos, no owner/perms, and они запрещают «:» и CR в именах. rsync
# упирается в это КАЖДЫЙ прогон и выходит с кодом 23 — поэтому такие приёмники
# обслуживаем иначе (см. nb_build_cmd), а не делаем вид, что это случайный сбой.
NB_FS_LIMITED = {"exfat", "vfat", "msdos", "fat", "fat32", "ntfs", "ntfs3", "fuseblk", "hfsplus"}
NB_FS_FAT     = {"vfat", "msdos", "fat", "fat32"}   # hard 4 GiB per-file cap — aborts the run
# «файл не лезет в эту ФС», а не «сбой»: запрещённое имя (22), симлинк/сокет (1),
# нет такой возможности у ФС (ENOSYS)
_NB_FS_ERR_RX = re.compile(r"failed: (Invalid argument \(22\)|Operation not permitted \(1\)|"
                           r"Function not implemented)")

def _fs_type(path):
    """Тип ФС для пути (по /proc/mounts, ближайшая точка монтирования). "" — не знаем."""
    mp = _mountpoint_of(path)
    best, best_len = "", -1
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (mnt == mp or mp.startswith(mnt.rstrip("/") + "/")) and len(mnt) > best_len:
                    best, best_len = parts[2], len(mnt)
    except OSError:
        return ""
    return best

def nb_dest_fs(cfg, dest=None):
    """Тип ФС приёмника — только когда он ЛОКАЛЕН (push по SSH мы не щупаем)."""
    if _nb_push_ssh(cfg):
        return ""
    d = dest or cfg.get("dest_base") or ""
    return _fs_type(d) if d.startswith("/") else ""

def _dest_disk_absent(dest):
    """dest под /mnt|/media|/srv подразумевает отдельный носитель. Если он НЕ
    смонтирован, точка проваливается в корень (mountpoint = '/') — писать туда
    нельзя: rsync молча зальёт системный диск. Пул /mnt/storage смонтирован →
    его mountpoint не '/', проверка проходит. Так безопасны и съёмные диски."""
    return bool(re.match(r"^/(mnt|media|srv)/", dest or "")) and _mountpoint_of(dest) == "/"

def nb_run(cfg, dry, writer, cancel=lambda: False, on_job=None, allow_delete=False):
    """Прогнать все включённые задачи. writer(line) — вывод; cancel() — прерывание.
    on_job(done, total) — after every finished job, so the UI can paint folder dots
    live instead of waiting for the whole run to end.
    allow_delete — ОДНОРАЗОВОЕ разрешение от пользователя: снять защиту --max-delete для
    этого прогона (он сам стёр много файлов на источнике и подтвердил это в панели).
    В конфиг НЕ пишется: следующий прогон снова под защитой."""
    cfg = cfg or nb_load()
    jobs = [j for j in cfg.get("jobs", []) if j.get("enabled", True)]
    if not jobs:
        writer("нет задач для бэкапа"); return {"ok": False, "jobs": []}
    t = nb_test(cfg)
    if not t.get("ok"):
        writer("ОШИБКА связи: " + t.get("log", "")); return {"ok": False, "unreachable": True, "jobs": []}
    pid = cfg.get("id") or NB_MAIN
    try:
        with open(nb_status_file(pid)) as f:
            prevf = {x.get("src"): x.get("files", 0) for x in json.load(f).get("jobs", [])}
    except (OSError, ValueError):
        prevf = {}
    t0 = time.time()
    push, push_ssh = _nb_push(cfg), _nb_push_ssh(cfg)
    shell_fs = None   # push-ssh: real FS (mkdir over SSH) vs forced daemon (--mkpath)
    if push_ssh:
        shell_fs = _nb_remote_shell_fs(cfg)
        if not shell_fs:
            writer("приёмник — rsync-демон с «модулями»: папки создаст сам rsync")
    if allow_delete:
        writer("РАЗРЕШЕНИЕ ПОЛЬЗОВАТЕЛЯ: защита от массового удаления снята на ЭТОТ прогон — "
               "лишнее в копии будет удалено (в режиме «архив» — перенесено в архив удалённых)")
    dest_fs = nb_dest_fs(cfg)
    if dest_fs in NB_FS_LIMITED:
        writer("приёмник в %s: эта файловая система не хранит симлинки, спецфайлы и права — "
               "они будут пропущены; имена с «:» и переносом строки она тоже не принимает"
               % dest_fs)
    # The FAT family caps a single file at 4 GiB. Unlike the notes above this is not a
    # "some files are skipped" nuisance: rsync dies with «File too large (27)» and the whole
    # job stops, so it has to be said loudly and up front (2026-07-12: a backup of game
    # repacks onto a FAT32 stick died on the first multi-gigabyte archive).
    if dest_fs in NB_FS_FAT:
        writer("ВНИМАНИЕ: %s не умеет файлы больше 4 ГиБ — на первом же таком файле прогон "
               "ОБОРВЁТСЯ с «File too large». Переформатируйте приёмник в ext4 (или exfat, "
               "если диск нужен и на Windows/Mac)" % dest_fs)
    results = []
    def emit(r):
        results.append(r)
        if on_job:
            try: on_job(list(results), len(jobs))
            except Exception: pass
    for j in jobs:
        if cancel():
            writer("— отменено —"); break
        writer("")
        writer("=== %s → %s ===" % (j["src"], j["dest"]))
        if push and not os.path.exists("/" + j["src"].lstrip("/")):
            writer("⚠ ПРОПУЩЕНО: источника нет на этом NAS (/%s) — папку удалили "
                   "или диск не смонтирован." % j["src"].lstrip("/"))
            emit({"src": j["src"], "ok": False, "src_missing": True}); continue
        if push_ssh:
            # plain server: mkdir over SSH (works with any remote rsync).
            # Forced daemon (UGREEN/Synology): the shell lives in a DIFFERENT path
            # namespace — mkdir can't reach it, rsync --mkpath creates module folders
            if shell_fs:
                mk = _nb_ssh_run(cfg, "mkdir -p " + shlex.quote(j["dest"]), timeout=25)
                if not mk["ok"]:
                    writer("не создать папку на приёмнике: %s" % (mk.get("log") or "").strip()[-160:])
                    emit({"src": j["src"], "ok": False}); continue
        else:
            # belt: a local destination must be an absolute allowed path — a relative
            # one (left over from a transport switch) would be created under cwd (/)
            if not _nb_valid_dest(j["dest"]):
                writer("⚠ ПРОПУЩЕНО: недопустимый локальный приёмник (%s) — выберите "
                       "папку в /mnt, /media, /srv или /home." % j["dest"])
                emit({"src": j["src"], "ok": False}); continue
            if _dest_disk_absent(j["dest"]):
                writer("⚠ ПРОПУЩЕНО: целевой диск не смонтирован (%s ведёт в системный "
                       "раздел). Бэкап пропущен, чтобы НЕ заполнить системный диск — "
                       "подключите диск назначения." % j["dest"])
                emit({"src": j["src"], "ok": False, "not_mounted": True}); continue
            try:
                os.makedirs(j["dest"], exist_ok=True)
            except OSError as e:
                writer("не создать папку: %s" % e); emit({"src": j["src"], "ok": False}); continue
        args, env = nb_build_cmd(cfg, j, dry, prev_files=prevf.get(j["src"], 0),
                                 mkpath=bool(push_ssh and not shell_fs),
                                 allow_delete=allow_delete)
        stat_lines = []
        try:
            p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            writer("не запустить rsync: %s" % e); emit({"src": j["src"], "ok": False}); continue
        # «Стоп» = флаг-файл, а rsync умеет молчать минутами (строит список файлов) —
        # проверки внутри цикла чтения там не случается вовсе. Сторож смотрит флаг сам,
        # поэтому остановка занимает ≤1 с, а не «сколько-нибудь»
        done_ev = threading.Event()
        def _watch_cancel(proc=p):
            while not done_ev.wait(0.5):
                if cancel():
                    try: proc.kill()
                    except OSError: pass
                    return
        threading.Thread(target=_watch_cancel, daemon=True).start()
        err_lines, errs, fs_bad, fs_files = [], 0, 0, []
        try:
            for line in iter(p.stdout.readline, ""):
                line = line.rstrip("\n")
                writer(line)
                if "Number of" in line or "Total transferred" in line or "Total file size" in line:
                    stat_lines.append(line)
                elif line.startswith("rsync:") or " failed: " in line:
                    # the first rsync complaint IS the answer to "so what went wrong?" —
                    # keep it for the panel instead of making the user dig through the log
                    errs += 1
                    if len(err_lines) < 3:
                        err_lines.append(line.strip())
                    if _NB_FS_ERR_RX.search(line):
                        # приёмник физически не может принять этот файл (имя с «:» или CR,
                        # симлинк, сокет) — это не поломка, это предел его файловой системы
                        fs_bad += 1
                        mm = re.search(r'"([^"]+)"', line)
                        if mm and len(fs_files) < 8:
                            fs_files.append(mm.group(1))
                if cancel():
                    p.kill(); break
            p.wait()
        finally:
            done_ev.set()
        try: p.stdout.close()
        except OSError: pass
        ok = p.returncode in (0, 24)     # 24 = vanished files — не ошибка
        stt = _nb_parse_stats(stat_lines)
        if not ok and push_ssh and not shell_fs and p.returncode == 1:
            # old receiver rsync (<3.2.3) rejects --mkpath as unknown option
            writer("подсказка: если выше «unknown option» — на приёмнике старый rsync "
                   "без --mkpath; создайте папки на нём вручную (файловым менеджером NAS)")
        if p.returncode == 25:           # сработала защита --max-delete
            pf = prevf.get(j["src"], 0)
            pctv = int(cfg.get("max_delete_pct", 20) or 0)
            res_limit = max(1, int(pf * pctv / 100.0)) if (pf and pctv) else 0
            stt.setdefault("guard_limit", res_limit)
            stt.setdefault("guard_pct", pctv)
            writer("⚠ ОСТАНОВЛЕНО ЗАЩИТОЙ: rsync попытался удалить слишком много файлов "
                   "(> %d%%). Ничего не удалено. Проверьте источник." % int(cfg.get("max_delete_pct", 20) or 0))
        sz = None
        if not dry and not push_ssh:
            try: sz = _du_bytes(j["dest"])
            except Exception: sz = None
        res = {"src": j["src"], "dest": j["dest"], "ok": ok, "code": p.returncode, "size": sz,
               "files": stt.get("files", prevf.get(j["src"], 0)), "xfer": stt.get("xfer", 0),
               "xfer_bytes": stt.get("xfer_bytes", 0), "deleted": stt.get("deleted", 0)}
        if p.returncode == 25:      # UI покажет, какой был порог, и предложит разрешить удаление
            res["guard_limit"] = stt.get("guard_limit", 0)
            res["guard_pct"] = stt.get("guard_pct", 0)
        if cancel():
            # мы сами убили rsync по «Стопу» — это не ошибка передачи (иначе в панели
            # висело бы «ошибка rsync, код -9», и поди догадайся, что это твой же стоп)
            res["stopped"] = True
        elif not ok:
            if err_lines:
                res["err"] = err_lines[0][:180]
                res["errn"] = errs
            if p.returncode == 23 and fs_bad and fs_bad == errs:
                # ВСЕ жалобы — про предел ФС приёмника. Остальное скопировалось; красная
                # точка тут врала бы каждый прогон, поэтому это отдельный, жёлтый исход
                res["fs_limit"] = fs_bad
                res["fs_files"] = fs_files
                res["fs"] = dest_fs
        if ok and not dry and cfg.get("verify") and not cancel():
            vb, vn, ve = _nb_verify_job(cfg, j, writer, cancel)
            res["verify_bad"], res["verify_new"], res["verify_err"] = vb, vn, ve
        emit(res)
        extra = " · %d файлов, передано %s" % (stt.get("xfer", 0), fmt_bytes(stt.get("xfer_bytes", 0))) if stt else ""
        writer("[%s] %s%s%s" % ("ок" if ok else ("остановлено" if p.returncode == 25 else "ошибка %d" % p.returncode),
                                j["src"], (" · " + fmt_bytes(sz)) if sz else "", extra))
    vbad = sum(int(r.get("verify_bad") or 0) for r in results)
    verr = any(r.get("verify_err") for r in results)
    stopped = cancel()          # остановлен пользователем — это не «бэкап с ошибками»
    allok = (all(r["ok"] for r in results) and len(results) == len(jobs)
             and not vbad and not verr and not stopped)
    if not dry:
        try: pruned = _nb_prune_remote(cfg, writer, shell_fs) if push_ssh else _nb_prune(cfg)
        except Exception: pruned = 0
        if pruned: writer("очищено старых снимков удалённых: %d" % pruned)
        _nb_write_status(pid, results)
        try: _nb_history_add(pid, {"ts": int(time.time()), "dur": int(time.time() - t0),
                              "result": "stopped" if stopped else ("ok" if allok else "warn"),
                              "jobs": results})
        except Exception: pass
    return {"ok": allok, "jobs": results, "verify_bad": vbad, "verify_err": verr,
            "stopped": stopped}

def _nb_verify_job(cfg, job, writer, cancel):
    """Post-run verify of one job: rsync -c -n re-reads both sides and compares
    checksums. Returns (mismatches, new-after-run, verify-error).
    «>f» without «+» = content differs; «>f+++» = the file appeared after the run."""
    writer("— сверка контрольных сумм (перечитывает все файлы — может быть долго)…")
    args, env = nb_verify_cmd(cfg, job)
    try:
        p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        writer("сверка не запустилась: %s" % e); return 0, 0, True
    # while everything matches rsync prints NOTHING, so readline() can block for the
    # whole scan — a side thread keeps the Cancel button responsive
    def _killer():
        while p.poll() is None:
            if cancel():
                p.kill(); return
            time.sleep(2)
    threading.Thread(target=_killer, daemon=True).start()
    bad, new = [], 0
    for line in iter(p.stdout.readline, ""):
        line = line.rstrip("\n")
        tok = line.split(" ", 1)
        if not tok[0].startswith((">f", "<f")):
            continue
        name = tok[1] if len(tok) > 1 else line
        if "+" in tok[0]:
            new += 1
        else:
            bad.append(name)
            if len(bad) <= 20:
                writer("  ≠ " + name)
    p.wait()
    try: p.stdout.close()
    except OSError: pass
    if cancel():
        writer("— сверка прервана —")   # user cancel is not a verification failure
        return len(bad), new, False
    if p.returncode not in (0, 24):
        writer("сверка завершилась с ошибкой (код %d)" % p.returncode)
        return len(bad), new, True
    if bad:
        writer("⚠ СВЕРКА: содержимое %d файла(ов) отличается от источника%s" %
               (len(bad), " (показаны первые 20)" if len(bad) > 20 else ""))
    else:
        writer("сверка ок — расхождений нет" +
               (" · %d новых файлов появилось после прогона" % new if new else ""))
    return len(bad), new, False

def _nb_prune_remote(cfg, writer, shell_fs=None):
    """Retention of the deleted-files archive on a push-ssh receiver — by age (days)
    only: sizing snapshots over SSH is too expensive, the GB cap does not apply here.
    shell_fs — probe result from the caller (nb_run), to avoid a second SSH round-trip."""
    days = int(cfg.get("retention_days", 0) or 0)
    if days <= 0:
        return 0
    top = nb_deleted_top(cfg)
    if shell_fs is None:
        shell_fs = _nb_remote_shell_fs(cfg)
    if not shell_fs:
        # forced rsync daemon: the SSH shell lives in a DIFFERENT path namespace,
        # find/rm can't reach inside the module — the archive is cleaned manually
        writer("модульный приёмник: автоочистка архива удалённых недоступна — чистите %s вручную" % top)
        return 0
    dests = set(j["dest"] for j in cfg.get("jobs", [])) | {cfg.get("dest_base", "")}
    removed = 0
    for base in dests:
        if not base:
            continue
        d = base.rstrip("/") + "/" + top
        find = "[ -d %s ] && find %s -mindepth 1 -maxdepth 1 -type d -mtime +%d" % (
            shlex.quote(d), shlex.quote(d), days)
        r = _nb_ssh_run(cfg, find, timeout=60)
        olds = [l.strip() for l in (r.get("log") or "").splitlines()
                if l.strip().startswith(d + "/")]
        for p in olds:
            rr = _nb_ssh_run(cfg, "rm -rf " + shlex.quote(p), timeout=300)
            if rr["ok"]:
                removed += 1
            else:
                writer("не удалить старый снимок %s: %s" % (p, (rr.get("log") or "").strip()[-120:]))
    return removed

def _nb_parse_stats(lines):
    """Вытащить числа из блока rsync --stats."""
    out = {}
    pats = {"files": r"Number of files:\s*([\d,]+)",
            "xfer": r"Number of regular files transferred:\s*([\d,]+)",
            "xfer_bytes": r"Total transferred file size:\s*([\d,]+)",
            "deleted": r"Number of deleted files:\s*([\d,]+)"}
    for l in lines:
        for k, pat in pats.items():
            m = re.search(pat, l)
            if m:
                try: out[k] = int(m.group(1).replace(",", ""))
                except ValueError: pass
    return out

def _nb_history_add(pid, entry):
    try:
        with open(nb_history_file(pid)) as f:
            hist = json.load(f)
        if not isinstance(hist, list): hist = []
    except (OSError, ValueError):
        hist = []
    hist.append(entry)
    hist = hist[-50:]                    # последние 50 прогонов
    try:
        _json_save(nb_history_file(pid), hist)
    except OSError:
        pass

def nb_history(pid=None):
    pid = _nb_pid(pid)
    try:
        with open(nb_history_file(pid)) as f:
            hist = json.load(f)
        return list(reversed(hist)) if isinstance(hist, list) else []
    except (OSError, ValueError):
        return []

def nb_history_clear(pid=None, ts=None):
    """ts=None → стереть всю историю профиля; иначе удалить одну запись по её ts."""
    pid = _nb_pid(pid)
    f = nb_history_file(pid)
    if ts is None:
        try: os.remove(f)
        except OSError: pass
        return {"ok": True, "history": []}
    try:
        with open(f) as fh:
            hist = json.load(fh)
        if not isinstance(hist, list): hist = []
    except (OSError, ValueError):
        hist = []
    hist = [e for e in hist if int(e.get("ts", 0)) != int(ts)]
    try:
        _json_save(f, hist)
    except OSError: pass
    return {"ok": True, "history": list(reversed(hist))}

def _nb_write_status(pid, results):
    st = {"ts": int(time.time()), "jobs": results}
    try:
        _json_save(nb_status_file(pid), st)
    except OSError:
        pass

def nb_status(pid=None):
    pid = _nb_pid(pid)
    try:
        with open(nb_status_file(pid)) as f: st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st["running"] = nb_run_active(pid)
    st["queued"] = nb_queued(pid)
    if st["queued"]:
        # caption data: who is running now and how many wait ahead of us. Cheap check
        # on purpose (state-file flag, no systemctl forks) — the UI polls this often
        st["queue_after"] = next((p["name"] for p in nb_profiles()
                                  if p["id"] != pid and _nb_run_state_read(p["id"]).get("running")), "")
        q = [x["pid"] for x in _nb_queue_read()]
        st["queue_ahead"] = q.index(pid) if pid in q else 0
    st["line"] = ""
    return st

def nb_dest_state(pid=None):
    """Реальное состояние приёмника СЕЙЧАС: существуют ли папки задач и не пусты ли.
    Быстро (isdir/listdir, без du). Ловит ручное удаление папок из приёмника —
    точки последнего прогона этого не видят."""
    cfg = nb_load(pid)
    base = cfg.get("dest_base") or ("" if _nb_push(cfg) else "/mnt/storage/nas-backup")
    if _nb_push(cfg) and not base:
        # fresh push profile: destination not chosen yet — not the same as "unmounted"
        return {"base": "", "unset": True, "base_mounted": True, "base_exists": False, "jobs": []}
    if _nb_push_ssh(cfg):
        # destination on another server — do not probe folders over SSH (too costly per UI tick)
        return {"base": base, "remote": True, "base_mounted": True, "base_exists": True,
                "jobs": [{"src": j.get("src", ""), "dest": j.get("dest", ""),
                          "enabled": j.get("enabled", True) is not False,
                          "exists": True, "empty": False} for j in cfg.get("jobs", [])]}
    arch = nb_deleted_top(cfg)
    jobs = []
    for j in cfg.get("jobs", []):
        dest = j.get("dest") or ""
        exists = bool(dest) and os.path.isdir(dest)
        empty = False
        if exists:
            try:
                empty = not [n for n in os.listdir(dest) if n != arch]
            except OSError:
                pass
        jobs.append({"src": j.get("src", ""), "dest": dest,
                     "enabled": j.get("enabled", True) is not False,
                     "exists": exists, "empty": empty})
    # "mounted" = the destination path does not fall through to the system root
    # (equivalent to ismount(/mnt/storage) for the pool; an honest check for USB disks)
    fs = _fs_type(base) if base.startswith("/") else ""
    return {"base": base, "base_mounted": bool(base) and not _dest_disk_absent(base),
            "base_exists": os.path.isdir(base), "jobs": jobs,
            "fs": fs, "fs_limited": fs in NB_FS_LIMITED, "fs_fat": fs in NB_FS_FAT}

def nb_log_tail(since, pid=None):
    """Хвост лога текущего/последнего прогона (из файла) — для переподключения UI."""
    pid = _nb_pid(pid)
    try:
        since = int(since)
    except (ValueError, TypeError):
        since = 0
    rs = _nb_run_state_read(pid)
    running = bool(rs.get("running")) and _nb_unit_active(pid)
    try:
        with open(nb_run_log(pid)) as f:
            lines = f.read().split("\n")
        if lines and lines[-1] == "":
            lines.pop()
    except OSError:
        lines = []
    cur_line = ""                            # последняя строка прогресса rsync (для полосы в UI)
    for l in reversed(lines[-8:]):
        if "%" in l and "/s" in l:
            cur_line = l.strip(); break
    base = 0
    if len(lines) > 2000:                    # ограничить объём ответа
        base = len(lines) - 2000; lines = lines[-2000:]
    end = base + len(lines)
    start = max(0, since - base)
    return {"running": running, "queued": nb_queued(pid),
            "started": rs.get("started", 0), "dry": rs.get("dry", False),
            "result": rs.get("result"), "cur": rs.get("cur", ""), "line": cur_line,
            "jobs": rs.get("jobs") or [], "total": rs.get("total") or 0,
            "stopping": bool(running and rs.get("stopping")),
            "seq": end, "base": base, "lines": lines[start:]}

# --------------------------------------------------------------------------- #
#  Здоровье бэкапа — периодические проверки (события nb_conn/nb_srcmiss/nb_stale/
#  nb_size/nb_dest). Гоняются не чаще раза в 30 мин и только если включена хоть
#  одна проверка И бэкап настроен (есть адрес и задачи) — иначе тишина.
# --------------------------------------------------------------------------- #
_nb_health_last = 0

def _nb_health_load(pid):
    try:
        with open(nb_health_file(pid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def _nb_health_save(pid, d):
    try:
        _json_save(nb_health_file(pid), d)
    except OSError:
        pass

def _du_bytes(path):
    r = subprocess.run(["du", "-sb", path], capture_output=True, text=True, timeout=120)
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return None

def nb_health_tick(fire, ev, pri, thr, now):
    """Периодические проверки бэкапа; fire()/ev/pri/thr — из monitor_tick.
    Гоняем по каждому профилю отдельно: свой кулдаун, свой файл здоровья."""
    global _nb_health_last
    HK = ("nb_conn", "nb_srcmiss", "nb_stale", "nb_size", "nb_dest")
    if not any(ev.get(k, {}).get("on") for k in HK):
        return
    if now - _nb_health_last < 1800:
        return
    _nb_health_last = now
    profs = nb_profiles()
    for cfg in profs:
        try:
            _nb_health_one(cfg, len(profs) > 1, fire, ev, pri, thr, now)
        except Exception:
            pass

def _nb_health_one(cfg, many, fire, ev, pri, thr, now):
    pid = cfg["id"]
    # с несколькими профилями «NAS-бэкап: источник недоступен» бесполезно —
    # дописываем имя, а ключ кулдауна делаем пер-профильным
    sfx = (" · " + cfg["name"]) if many else ""
    def fire_p(key, title, msg, priority, ev_name=None, lvl=None):
        fire(key + ":" + pid, title + sfx, msg, priority, ev_name=ev_name or key, lvl=lvl)
    jobs = [j for j in cfg.get("jobs", []) if j.get("enabled", True)]
    push, push_ssh = _nb_push(cfg), _nb_push_ssh(cfg)
    if not jobs or (not cfg.get("host") and not (push and cfg.get("transport") == "local")):
        return
    hs = _nb_health_load(pid)
    remote, env, rsh = _nb_remote_env(cfg)
    extra = ["--rsync-path=sudo rsync"] if (cfg.get("transport") == "ssh" and cfg.get("remote_sudo")) else []
    conn_ok = None
    # --- connectivity to the remote side (pull: source; push-ssh: destination) ---
    if ev.get("nb_conn", {}).get("on") and cfg.get("transport") != "local":
        t = nb_test(cfg)
        conn_ok = t.get("ok")
        if not conn_ok:
            fire_p("nb_conn", "Бэкап: приёмник недоступен" if push else "NAS-бэкап: источник недоступен",
                 "Не удаётся подключиться к %s: %s" % (cfg.get("host"), t.get("log", "")),
                 pri("nb_conn"), ev_name="nb_conn", lvl="warn")
    # --- source folders still present (push: locally; pull: only when reachable) ---
    if ev.get("nb_srcmiss", {}).get("on"):
        missing = []
        if push:
            missing = ["/" + j["src"].lstrip("/") for j in jobs[:50]
                       if not os.path.exists("/" + j["src"].lstrip("/"))]
        else:
            if conn_ok is None:
                conn_ok = nb_test(cfg).get("ok")
            if conn_ok:
                for j in jobs[:20]:
                    r = _run(["rsync"] + rsh + extra + ["--list-only", remote + j["src"] + "/"], timeout=20, env=env)
                    if not r["ok"] and re.search(r"no such file|not found|failed to", r["log"].lower()):
                        missing.append(j["src"])
        if missing:
            fire_p("nb_srcmiss", "NAS-бэкап: пропала исходная папка",
                 "Нет на источнике: " + ", ".join(missing), pri("nb_srcmiss"), ev_name="nb_srcmiss", lvl="warn")
    # --- давно не было прогона ---
    if ev.get("nb_stale", {}).get("on"):
        days = thr("nb_stale", 7)
        try:
            with open(nb_status_file(pid)) as f:
                ts = json.load(f).get("ts", 0)
        except (OSError, ValueError):
            ts = 0
        if days > 0 and not nb_run_active(pid):
            if not ts:
                fire_p("nb_stale", "NAS-бэкап: ещё не выполнялся",
                     "Бэкап настроен, но не было ни одного прогона", pri("nb_stale"), ev_name="nb_stale", lvl="warn")
            elif now - ts > days * 86400:
                fire_p("nb_stale", "NAS-бэкап: давно не обновлялся",
                     "Последний прогон %d дн назад (порог %d)" % (int((now - ts) / 86400), days),
                     pri("nb_stale"), ev_name="nb_stale", lvl="warn")
    if push_ssh:
        return   # do not monitor size/space of a destination on a foreign server over SSH
    # --- резкое изменение размера приёмника ---
    if ev.get("nb_size", {}).get("on") and not nb_run_active(pid):
        base_dir = cfg.get("dest_base") or "/mnt/storage/nas-backup"
        if os.path.isdir(base_dir):
            cur_sz = _du_bytes(base_dir)
            prev = hs.get("dest_size")
            if cur_sz is not None:
                if prev and prev > 0:
                    delta = abs(cur_sz - prev) * 100.0 / prev
                    lim = thr("nb_size", 40)
                    if delta >= lim:
                        arrow = "вырос" if cur_sz > prev else "уменьшился"
                        fire_p("nb_size", "NAS-бэкап: резко изменился размер",
                             "Приёмник %s на %.0f%% (%s → %s)" % (arrow, delta, fmt_bytes(prev), fmt_bytes(cur_sz)),
                             pri("nb_size"), ev_name="nb_size", lvl="warn")
                hs["dest_size"] = cur_sz
    # --- приёмник: место / смонтирован ли ---
    if ev.get("nb_dest", {}).get("on"):
        base_dir = cfg.get("dest_base") or "/mnt/storage/nas-backup"
        top = "/mnt/storage" if base_dir.startswith("/mnt/storage") else base_dir
        if top == "/mnt/storage" and not os.path.ismount("/mnt/storage"):
            fire_p("nb_dest", "NAS-бэкап: приёмник не смонтирован",
                 "Пул /mnt/storage не смонтирован — бэкап писать некуда", pri("nb_dest"), ev_name="nb_dest", lvl="warn")
        elif not os.path.isdir(base_dir):
            pass   # destination missing (USB disk unplugged) — nothing to say about space
        else:
            try:
                st = os.statvfs(base_dir)
                used = 100.0 * (st.f_blocks - st.f_bfree) / max(st.f_blocks, 1)
                lim = thr("nb_dest", 95)
                if used >= lim:
                    fire_p("nb_dest", "NAS-бэкап: мало места в приёмнике",
                         "%s заполнен на %.0f%% (порог %d%%)" % (base_dir, used, lim),
                         pri("nb_dest"), ev_name="nb_dest", lvl="warn")
            except OSError:
                pass
    _nb_health_save(pid, hs)

def _nb_start_unit(pid, dry, allow_delete=False):
    """Поднять транзиентный юнит с драйвером прогона. True — процесс стартовал."""
    try:
        if os.path.exists(nb_run_cancel(pid)):
            os.remove(nb_run_cancel(pid))
    except OSError:
        pass
    # начальный статус (драйвер перезапишет) — чтобы UI сразу увидел «идёт»
    _nb_run_state_write(pid, {"running": True, "started": int(time.time()), "dry": bool(dry),
                              "cur": "", "result": None})
    cmd = ["systemd-run", "--collect", "--quiet", "--unit", nb_unit(pid),
           "--setenv=SUDO_USER=" + TARGET_USER, "--setenv=HOME=" + HOME,   # тот же NAS_CONFIG, что у службы
           sys.executable, os.path.join(HERE, "nas-web.py"), "backup-run", pid] \
        + (["dry"] if dry else []) + (["allow-delete"] if allow_delete else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        _nb_run_state_write(pid, {"running": False, "result": "warn"})
        return False
    if r.returncode != 0:
        _nb_run_state_write(pid, {"running": False, "result": "warn"})
        return False
    return True

def nb_run_bg(pid=None, dry=False, allow_delete=False):
    """Запустить прогон профиля. Одновременно идёт РОВНО ОДИН прогон: пока занято,
    остальные ждут в очереди (два rsync на одном HDD только мешают друг другу)."""
    cfg = nb_load(pid); pid = cfg["id"]
    if nb_run_active(pid):
        return {"ok": False, "log": "уже выполняется"}
    if nb_any_active():
        _nb_queue_add(pid, dry, allow_delete)
        return {"ok": True, "queued": True, "log": "поставлено в очередь"}
    _nb_queue_remove(pid)
    if not _nb_start_unit(pid, dry, allow_delete):
        return {"ok": False, "log": "не удалось запустить"}
    return {"ok": True, "queued": False, "log": "запущено"}

def _nb_queue_drain():
    """Раз в минуту: если ничего не идёт — запустить первого из очереди."""
    q = _nb_queue_read()
    if not q or nb_any_active():
        return
    known = {p["id"] for p in nb_profiles()}
    while q:
        item = q.pop(0)
        if item["pid"] in known:
            _nb_queue_write(q)
            _nb_start_unit(item["pid"], item.get("dry", False), item.get("allow_delete", False))
            return
    _nb_queue_write(q)

def nb_run_cli(pid=None, dry=False, allow_delete=False):
    """Драйвер прогона (запускается в транзиентном юните). Пишет лог в файл и
    статус в json — независимо от того, жив ли основной процесс nas-web."""
    cfg = nb_load(pid); pid = cfg["id"]
    many = len(nb_profiles()) > 1
    sfx = (" · " + cfg["name"]) if many else ""
    started = int(time.time())
    _nb_run_state_write(pid, {"running": True, "started": started, "dry": bool(dry),
                              "cur": "", "result": None, "pid": os.getpid()})
    try:
        logf = open(nb_run_log(pid), "w", buffering=1)     # усечь и открыть на дозапись построчно
    except OSError:
        logf = None
    def w(l):
        if logf:
            try: logf.write(l + "\n")
            except OSError: pass
        if l.startswith("=== ") and " → " in l:
            st = _nb_run_state_read(pid); st["cur"] = l[4:].split(" → ")[0].strip()
            _nb_run_state_write(pid, st)
    def on_job(done, total):
        """Live per-folder result → run state: the panel paints the dots as folders
        finish, instead of staying grey until the whole run ends."""
        st = _nb_run_state_read(pid)
        st["jobs"] = [{k: r.get(k) for k in ("src", "ok", "code", "xfer", "xfer_bytes",
                                             "size", "verify_bad", "verify_err",
                                             "src_missing", "not_mounted", "stopped",
                                             "err", "errn", "fs_limit", "fs_files", "fs",
                                             "guard_limit", "guard_pct")}
                      for r in done]
        st["total"] = total
        _nb_run_state_write(pid, st)
    def cancel():
        return os.path.exists(nb_run_cancel(pid))
    push = _nb_push(cfg)
    title = ("Бэкап с этого NAS" if push else "Бэкап главного NAS") + sfx
    res = None
    try:
        r = nb_run(cfg, dry, w, cancel, on_job, allow_delete)
        res = ("ok" if r.get("ok") else "stopped" if r.get("stopped")
               else "unreachable" if r.get("unreachable") else "warn")
        if not dry and r.get("stopped"):
            try: log_event("nas_backup", title, "остановлен вручную", "info", kind="backup", desk=True)
            except Exception: pass
        if not dry and not r.get("stopped"):
            guarded = [j.get("src") for j in r.get("jobs", []) if j.get("code") == 25]
            if guarded:      # сработала защита от массового удаления — отдельное важное уведомление
                try: notify_event("nb_guard", "nb_guard:" + pid, "NAS-бэкап: остановлен защитой" + sfx,
                                  "Защита от массового удаления сработала: " + ", ".join(guarded) +
                                  ". Ничего не удалено — проверьте источник (не стёрли/размонтировали ли его).",
                                  "crit", cooldown=0)
                except Exception: pass
            vbad, verr = int(r.get("verify_bad") or 0), bool(r.get("verify_err"))
            if vbad or verr:      # checksum verify found mismatches / could not finish
                try: notify_event("nb_verify", "nb_verify:" + pid, "Бэкап: сверка нашла проблемы" + sfx,
                                  ("Содержимое %d файла(ов) в копии отличается от источника — "
                                   "подробности в журнале прогона." % vbad) if vbad
                                  else "Сверка не смогла завершиться — см. журнал прогона.",
                                  "warn", cooldown=0)
                except Exception: pass
            msg = ("все задачи выполнены" + (" · сверка ок" if cfg.get("verify") and not vbad and not verr else "")) if r.get("ok") \
                else (("приёмник недоступен — пропущено" if push else "главный NAS недоступен — пропущено") if r.get("unreachable")
                      else ("сверка: %d расхождений" % vbad if vbad else "часть задач с ошибками"))
            try: log_event("nas_backup", title, msg, "ok" if r.get("ok") else "warn", kind="backup", desk=True)
            except Exception: pass
    except Exception as e:
        res = "warn"; w("сбой: %s" % e)
        try: log_event("nas_backup", title, "сбой: %s" % e, "warn", kind="backup", desk=True)
        except Exception: pass
    finally:
        st = _nb_run_state_read(pid)
        st.update(running=False, result=(res or "warn"), cur="", done=int(time.time()))
        _nb_run_state_write(pid, st)
        try:
            if os.path.exists(nb_run_cancel(pid)): os.remove(nb_run_cancel(pid))
        except OSError: pass
        if logf:
            try: logf.close()
            except OSError: pass

def nb_schedule_due(cfg, nowt):
    """Пора ли запускать по расписанию (вызывается раз в минуту из monitor_loop)."""
    s = cfg.get("schedule", {})
    if not s.get("enabled"):
        return False
    if s.get("time") != time.strftime("%H:%M", time.localtime(nowt)):
        return False
    if s.get("freq") == "weekly":
        dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        if dows[time.localtime(nowt).tm_wday] != s.get("dow", "Sun"):
            return False
    return True

_nb_last_sched = ""

MAINT_FILE = os.path.join(NAS_CONFIG, "maintenance.json")
_maint_last = 0

def load_maintenance():
    # 0 = выключено (для *_days); все значения попадают в бэкап настроек вместе с файлом
    d = {"trash_days": 30, "pool_alias": "",
         "duscan_hours": 0,         # авто-освежение анализатора места: пересканить тома с кэшем старше N часов (0 = выкл)
         "thumb_cache_mb": 512,     # лимит кэша миниатюр ФМ, МБ (0 = без лимита)
         "import_stale_hours": 24,  # снести брошенные .incomplete-* старше N часов (0 = не трогать)
         "import_keep_days": 0,     # удалять импорты старше N дней (0 = хранить вечно)
         "import_warm_thumbs": True,  # прогреть превью сразу после импорта
         "myspeed_url": "http://127.0.0.1:5216",  # виджет MySpeed ("" = выключить)
         "myspeed_password": "",                  # если в MySpeed включён пароль
         "smart_scan_min": 10,      # фоновый опрос SMART-статуса, минут
         "smart_short_days": 7,     # короткий самотест дисков, раз в N дней (ночью)
         "smart_long_days": 30,     # длинный самотест, раз в N дней (ночью)
         "backup_days": 7,          # авто-бэкап настроек, раз в N дней
         "backup_keep": 10,         # сколько бэкапов хранить
         "settings_backup_dir": "",     # путь бэкапа настроек ("" = /mnt/storage/nas-settings-backup)
         "settings_backup_hide": True,  # скрывать эту папку в файловом менеджере (по умолчанию да)
         "snap_sync_time": "03:00",   # SnapRAID: ежедневный sync
         "snap_scrub_dow": "Sun",     # SnapRAID: день недели scrub
         "snap_scrub_time": "05:00",  # SnapRAID: время scrub
         "automount_recover": True,   # авто-перемонтирование отвалившегося диска
         "summary_enabled": False,    # сводка состояния (в Pushover/журнал)
         "summary_freq": "daily",     # daily | weekly
         "summary_time": "09:00",     # HH:MM
         "summary_dow": "Mon",        # для weekly
         "thermal_mode": "warn",      # off | warn | auto (активная термозащита)
         "thermal_hot": 80,           # порог «горячо», °C
         "thermal_crit": 85}          # порог «критично» (в auto — стоп контейнера)
    saved = _json_load_strict(MAINT_FILE, {})
    if isinstance(saved, dict):
        d.update(saved)
    return d

_ALIAS_RESERVED = ("/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/media", "/mnt",
                   "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var")

_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def _snap_sched_apply(cfg):
    """Расписание SnapRAID: drop-in override для таймеров визарда.
    Если таймеров ещё нет (SnapRAID не настроен) — тихо выходим; применится
    при старте службы после настройки."""
    if not os.path.isfile("/etc/systemd/system/snapraid-sync.timer"):
        return ""
    try:
        for unit, cal in (("snapraid-sync", "*-*-* %s:00" % cfg.get("snap_sync_time", "03:00")),
                          ("snapraid-scrub", "%s *-*-* %s:00" % (cfg.get("snap_scrub_dow", "Sun"),
                                                                 cfg.get("snap_scrub_time", "05:00")))):
            d = "/etc/systemd/system/%s.timer.d" % unit
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "override.conf"), "w") as f:
                f.write("[Timer]\nOnCalendar=\nOnCalendar=%s\n" % cal)
        _run(["systemctl", "daemon-reload"], timeout=20)
        for unit in ("snapraid-sync.timer", "snapraid-scrub.timer"):
            _run(["systemctl", "try-restart", unit], timeout=15)
        return ""
    except OSError as e:
        return str(e)

def _pool_alias_apply(alias, old=""):
    """Симлинк-псевдоним пула (напр. /volume2 → /mnt/storage). Возвращает ''/ошибку.
    Старый псевдоним убираем, только если это НАШ симлинк на пул."""
    if old and old != alias and os.path.islink(old):
        try:
            if os.readlink(old) == STORAGE:
                os.remove(old)
        except OSError:
            pass
    if not alias:
        return ""
    try:
        if os.path.islink(alias):
            if os.readlink(alias) == STORAGE:
                return ""
            os.remove(alias)
        elif os.path.exists(alias):
            return "путь %s уже существует и не является псевдонимом пула" % alias
        os.symlink(STORAGE, alias)
        return ""
    except OSError as e:
        return str(e)

def save_maintenance(d):
    cur = load_maintenance()
    err = ""
    # числовые настройки: ключ → (мин, макс)
    for k, (lo, hi) in {"trash_days": (0, 365), "notes_trash_days": (0, 365),
                        "smart_scan_min": (5, 120),
                        "duscan_hours": (0, 8760),
                        "thumb_cache_mb": (0, 100000),
                        "import_stale_hours": (0, 720), "import_keep_days": (0, 3650),
                        "smart_short_days": (0, 90), "smart_long_days": (0, 365),
                        "backup_days": (0, 30), "backup_keep": (2, 50)}.items():
        if k in d:
            try:
                cur[k] = max(lo, min(hi, int(d[k])))
            except (ValueError, TypeError):
                pass
    # расписание SnapRAID
    snap_changed = False
    for k in ("snap_sync_time", "snap_scrub_time"):
        if k in d and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(d[k] or "")):
            snap_changed |= cur.get(k) != d[k]; cur[k] = d[k]
    if "snap_scrub_dow" in d and d["snap_scrub_dow"] in _DOW:
        snap_changed |= cur.get("snap_scrub_dow") != d["snap_scrub_dow"]
        cur["snap_scrub_dow"] = d["snap_scrub_dow"]
    if snap_changed:
        err = _snap_sched_apply(cur) or err
        if not err:
            try:
                log_event("action", "Расписание SnapRAID изменено",
                          "sync %s · scrub %s %s" % (cur["snap_sync_time"], cur["snap_scrub_dow"], cur["snap_scrub_time"]),
                          "ok", kind="protect", desk=False)
            except Exception:
                pass
    if "pool_alias" in d:
        v = str(d["pool_alias"] or "").strip().rstrip("/")
        if v and (not re.match(r"^/[A-Za-z0-9._-]{1,32}$", v) or v.lower() in _ALIAS_RESERVED):
            err = "недопустимое имя: одно слово в корне, латиница/цифры (например /volume2)"
        else:
            err = _pool_alias_apply(v, cur.get("pool_alias", ""))
            if not err:
                if v != cur.get("pool_alias", ""):
                    try:
                        log_event("action", ("Псевдоним пула: %s → /mnt/storage" % v) if v
                                  else "Псевдоним пула отключён", "", "ok", kind="disk", desk=False)
                    except Exception:
                        pass
                cur["pool_alias"] = v
    # путь и скрытие папки бэкапа настроек
    if "settings_backup_dir" in d:
        v = str(d["settings_backup_dir"] or "").strip().rstrip("/")
        if v == "" or (v.startswith("/") and len(v) > 1 and ".." not in v and v not in _ALIAS_RESERVED):
            cur["settings_backup_dir"] = v
        else:
            err = err or "недопустимый путь: абсолютный, не системный корень"
    if "settings_backup_hide" in d:
        cur["settings_backup_hide"] = bool(d["settings_backup_hide"])
    # --- надёжность/отчёты/термозащита ---
    if "automount_recover" in d:
        cur["automount_recover"] = bool(d["automount_recover"])
    if "summary_enabled" in d:
        cur["summary_enabled"] = bool(d["summary_enabled"])
    if d.get("summary_freq") in ("daily", "weekly"):
        cur["summary_freq"] = d["summary_freq"]
    if "summary_time" in d and re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(d["summary_time"] or "")):
        cur["summary_time"] = d["summary_time"]
    if d.get("summary_dow") in _DOW:
        cur["summary_dow"] = d["summary_dow"]
    if d.get("thermal_mode") in ("off", "warn", "auto"):
        cur["thermal_mode"] = d["thermal_mode"]
    for k, (lo, hi) in {"thermal_hot": (60, 85), "thermal_crit": (65, 90)}.items():
        if k in d:
            try: cur[k] = max(lo, min(hi, int(d[k])))
            except (ValueError, TypeError): pass
    if cur.get("thermal_crit", 85) <= cur.get("thermal_hot", 80):
        cur["thermal_crit"] = cur.get("thermal_hot", 80) + 3
    try:
        _json_save(MAINT_FILE, cur)
    except OSError:
        pass
    out = dict(cur)
    if err:
        out["error"] = err
    return out

def _trash_autoclean(days):
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    items = _trash_load()
    keep, removed = [], 0
    for it in items:
        if it.get("deleted", 0) < cutoff:
            try:
                _trash_rm(it.get("store"))
            except OSError:
                pass
            removed += 1
        else:
            keep.append(it)
    if removed:
        _trash_save(keep)
    return removed

def maintenance_daily():
    """Раз в сутки: авто-очистка корзины + еженедельный авто-бэкап настроек."""
    global _maint_last
    now = time.time()
    if now - _maint_last < 86400:
        return
    _maint_last = now
    try:
        _trash_autoclean(load_maintenance().get("trash_days", 0))
    except Exception:
        pass
    try:
        thumbs_cache_gc(load_maintenance().get("thumb_cache_mb", 0))
    except Exception:
        pass
    try:
        m = load_maintenance()
        usb_import_gc(m.get("import_stale_hours", 24), m.get("import_keep_days", 0))
    except Exception:
        pass
    try:
        notes_gc()
    except Exception:
        pass
    try:
        _settings_backup_auto()
    except Exception:
        pass

# --------------------------------------------------------------------------- #
#  Бэкап ВСЕХ настроек: nas-config (без журналов/истории), конфиги визарда,
#  веб-пароль, samba, compose-файлы стеков; fstab/snapraid — справочно
#  (не восстанавливаются автоматически: железо может отличаться).
# --------------------------------------------------------------------------- #
import tarfile, io

BACKUP_KEEP = 10
_BK_NAME_RE = re.compile(r"^nas-settings-[\w.-]+\.tar\.gz$")
# archive-префикс → (источник, восстанавливать ли автоматически)
_BK_EXCLUDE = ("events.json", "history.json", "history-long.json", "sessions.json")

# Разделы выборочного восстановления: (ключ, название, секрет?, префиксы в архиве).
# Порядок значим дважды: он же порядок в диалоге, и раздел файла — ПЕРВЫЙ
# совпавший префикс (иначе "etc/nas-wizard/" из maint проглотил бы notify.conf).
# Секретные разделы в диалоге по умолчанию сняты: восстановление старого архива
# не должно молча подменить пароль входа тем, что был полгода назад.
_BK_SECTIONS = (
    ("desktop",   "Рабочий стол",              False, ("nas-config/desktop.json", "nas-config/winpos.json",
                                                       "nas-config/wallpaper.", "nas-config/fm-favorites.json",
                                                       "nas-config/icons/")),
    ("notify",    "Уведомления",               False, ("nas-config/monitor.json", "etc/nas-wizard/notify.conf")),
    ("maint",     "Обслуживание и расписания", False, ("nas-config/maintenance.json", "etc/nas-wizard/")),
    ("samba",     "Общие папки (Samba)",       False, ("etc/samba/", "var/lib/samba/")),
    ("stacks",    "Docker-стеки",              False, ("opt/stacks/",)),
    ("disks",     "Диски и пул",               False, ("nas-config/fstab.",)),
    ("webauth",   "Пароль панели",             True,  ("etc/nas-os/webauth.json",)),
    ("nasbackup", "Бэкап главного NAS",        True,  ("etc/nas-os/nas-backup.json", "nas-config/nas-backup-",
                                                       # store.json / remotes.json содержат SSH-пароли
                                                       "nas-config/store.json", "nas-config/remotes.json")),
    ("other",     "Прочее",                    False, ()),      # всё, что не подошло выше
)

def _bk_section(nm):
    for key, _title, _secret, prefixes in _BK_SECTIONS:
        for p in prefixes:
            if nm.startswith(p):
                return key
    return "other"

def _bk_restorable(m, nm):
    """Члены архива, которые вообще можно восстановить (reference/* — только справка).
    .git отвергаем и на восстановлении: старые архивы несут его внутри, а разливать
    чужую историю поверх рабочего репозитория nas-config нельзя."""
    parts = nm.split("/")
    return m.isreg() and nm != "manifest.json" and not nm.startswith("reference/") \
        and ".." not in parts and ".git" not in parts and not nm.startswith("/")

def settings_backup_inspect(path):
    """Какие разделы есть в архиве — для диалога с чекбоксами."""
    seen = {}
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                nm = m.name.lstrip("./")
                if not _bk_restorable(m, nm):
                    continue
                c = seen.setdefault(_bk_section(nm), [0, 0])
                c[0] += 1; c[1] += m.size
    except (OSError, tarfile.TarError) as e:
        return {"ok": False, "log": "плохой архив: %s" % e}
    return {"ok": True, "sections": [
        {"key": k, "title": t, "secret": s, "files": seen[k][0], "bytes": seen[k][1]}
        for k, t, s, _p in _BK_SECTIONS if k in seen]}

def settings_backup_path():
    """Куда складывать бэкап настроек: свой путь из maintenance или дефолт.
    Дефолт — на пуле (переживает переустановку), отдельная папка (не общая «backups»)."""
    custom = (load_maintenance().get("settings_backup_dir") or "").strip().rstrip("/")
    if custom and custom.startswith("/") and ".." not in custom:
        return custom
    return os.path.join(STORAGE, "nas-settings-backup") if os.path.ismount(STORAGE) \
        else "/var/backups/nas-os"

def settings_backup_dir():
    d = settings_backup_path()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d

def _bk_add_file(tar, src, arc):
    try:
        if os.path.isfile(src) and os.path.getsize(src) <= 8 * 1024 * 1024:
            tar.add(src, arcname=arc, recursive=False)
            return arc
    except OSError:
        pass
    return None

# Пересоздаваемое и нескончаемое: кэш анализатора места, логи прогонов бэкапа,
# огрызки после сбоя. Каталог .git репозитория nas-config — тем более: он весил
# больше всех настроек вместе взятых и разливался поверх чужой истории.
_BK_SKIP_DIRS = (".git",)
_BK_SKIP_FILE = re.compile(r"^duscan-.+\.json$|\.(log|tmp|bad)$")

def _bk_sources():
    """(src, arcname) всех файлов бэкапа. Каталоги обходим целиком —
    будущие настройки попадут в бэкап автоматически."""
    out = []
    for base, arcroot in ((NAS_CONFIG, "nas-config"), ("/etc/nas-wizard", "etc/nas-wizard")):
        if os.path.isdir(base):
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in _BK_SKIP_DIRS]
                for fn in files:
                    if fn in _BK_EXCLUDE or _BK_SKIP_FILE.search(fn):
                        continue
                    p = os.path.join(root, fn)
                    out.append((p, arcroot + "/" + os.path.relpath(p, base)))
    for p, arc in (("/etc/nas-os/webauth.json", "etc/nas-os/webauth.json"),
                   (NB_CONF, "etc/nas-os/nas-backup.json"),
                   ("/etc/samba/smb.conf", "etc/samba/smb.conf"),
                   ("/var/lib/samba/private/passdb.tdb", "var/lib/samba/private/passdb.tdb"),
                   ("/etc/fstab", "reference/etc/fstab"),
                   ("/etc/snapraid.conf", "reference/etc/snapraid.conf"),
                   ("/etc/exports", "reference/etc/exports")):
        out.append((p, arc))
    if os.path.isdir(STACKS_DIR):            # compose/env стеков (не данные томов)
        for root, _, files in os.walk(STACKS_DIR):
            for fn in files:
                if re.match(r"^(compose|docker-compose)\.ya?ml$|^\.env$|\.(yml|yaml|env|txt|md)$", fn):
                    p = os.path.join(root, fn)
                    out.append((p, "opt/stacks/" + os.path.relpath(p, STACKS_DIR)))
    return out

def settings_backup_make(auto=False):
    d = settings_backup_dir()
    try:
        os.chmod(d, 0o700)      # внутри архивов — пароль главного NAS и хеш пароля панели
    except OSError:
        pass
    name = "nas-settings-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(d, name)
    added = []
    try:
        with tarfile.open(path, "w:gz") as tar:
            for src, arc in _bk_sources():
                if _bk_add_file(tar, src, arc):
                    added.append(arc)
            mf = json.dumps({"version": 1, "ts": int(time.time()),
                             "host": socket.gethostname(), "files": added},
                            ensure_ascii=False).encode()
            ti = tarfile.TarInfo("manifest.json"); ti.size = len(mf); ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(mf))
        os.chmod(path, 0o600)
    except OSError as e:
        try:
            os.remove(path)
        except OSError:
            pass
        return {"ok": False, "log": str(e)}
    # ротация (сколько хранить — настройка backup_keep)
    try:
        keep = load_maintenance().get("backup_keep", BACKUP_KEEP)
        old = sorted(f for f in os.listdir(d) if _BK_NAME_RE.match(f))
        for f in old[:-keep]:
            os.remove(os.path.join(d, f))
    except OSError:
        pass
    try:
        log_event("action", "Бэкап настроек создан" + (" (по расписанию)" if auto else ""),
                  "%s · файлов: %d" % (path, len(added)), "ok", kind="action", desk=False)
    except Exception:
        pass
    return {"ok": True, "name": name, "files": len(added), "dir": d}

def settings_backup_list():
    d = settings_backup_dir()
    out = []
    try:
        for f in sorted(os.listdir(d), reverse=True):
            if _BK_NAME_RE.match(f):
                st = os.stat(os.path.join(d, f))
                out.append({"name": f, "size": st.st_size, "t": int(st.st_mtime)})
    except OSError:
        pass
    cfg = load_maintenance()
    return {"ok": True, "dir": d, "days": cfg.get("backup_days", 7),
            "keep": cfg.get("backup_keep", BACKUP_KEEP), "list": out}

def settings_backup_restore(path, sections=None):
    """Восстановить из архива. Проверяем каждого члена: только обычные файлы,
    без ../, только известные префиксы, разумный размер. reference/* не трогаем.
    sections=None — восстановить всё (совместимость со старым клиентом), иначе
    только перечисленные разделы из _BK_SECTIONS."""
    global _events, _history, _history_long
    sel = None if sections is None else {s for s in sections if isinstance(s, str)}
    if sel is not None and not sel:
        return {"ok": False, "log": "не выбрано ни одного раздела"}
    # префиксы архива → куда восстанавливать (STACKS_DIR определяется ниже по файлу)
    restore_map = [("etc/nas-wizard/", "/etc/nas-wizard"),
                   ("etc/nas-os/webauth.json", "/etc/nas-os/webauth.json"),
                   ("etc/nas-os/nas-backup.json", "/etc/nas-os/nas-backup.json"),
                   ("etc/samba/smb.conf", "/etc/samba/smb.conf"),
                   ("var/lib/samba/private/passdb.tdb", "/var/lib/samba/private/passdb.tdb"),
                   ("opt/stacks/", STACKS_DIR)]
    restored, skipped, deselected = [], [], 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                nm = m.name.lstrip("./")
                if not m.isreg() or ".." in nm.split("/") or nm.startswith("/"):
                    continue
                # снятый раздел — не ошибка, а осознанный выбор: не в skipped
                if sel is not None and _bk_restorable(m, nm) and _bk_section(nm) not in sel:
                    deselected += 1; continue
                if m.size > 16 * 1024 * 1024:
                    skipped.append(nm); continue
                dest = None
                if nm.startswith("nas-config/"):
                    dest = os.path.join(NAS_CONFIG, nm[len("nas-config/"):])
                else:
                    for pref, root in restore_map:
                        if nm == pref:
                            dest = root; break
                        if pref.endswith("/") and nm.startswith(pref):
                            dest = os.path.join(root, nm[len(pref):]); break
                if not dest:
                    if nm != "manifest.json":
                        skipped.append(nm)
                    continue
                src = tar.extractfile(m)
                if not src:
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
                # секреты: пароль панели, ключи Pushover, пароль главного NAS
                if any(x in dest for x in ("webauth", "notify.conf", "nas-backup.json")):
                    os.chmod(dest, 0o600)
                restored.append(nm)
    except (OSError, tarfile.TarError) as e:
        return {"ok": False, "log": "плохой архив: %s" % e}
    if not restored:
        return {"ok": False, "log": "в выбранных разделах нет файлов" if deselected
                else "в архиве нет файлов настроек NAS-OS"}
    # сбросить кэши в памяти, применить сон дисков
    with _events_lock:
        _events = None
    with _hist_lock:
        _history = None; _history_long = None
    try:
        apply_spindown_all()
    except Exception:
        pass
    titles = dict((k, t) for k, t, _s, _p in _BK_SECTIONS)
    what = ("разделы: " + ", ".join(titles[k] for k, _t, _s, _p in _BK_SECTIONS if k in sel)) \
        if sel is not None else "все разделы"
    try:
        log_event("action", "Настройки восстановлены из бэкапа",
                  "%s · файлов: %d%s" % (what, len(restored),
                                         (" · пропущено: %d" % len(skipped)) if skipped else ""),
                  "warn", kind="action", desk=False)
    except Exception:
        pass
    return {"ok": True, "restored": len(restored), "skipped": len(skipped),
            "log": "восстановлено файлов: %d — обновите страницу" % len(restored)}

def _settings_backup_auto():
    """Авто-бэкап раз в backup_days дней (0 = выключен); зовётся из maintenance_daily."""
    days = load_maintenance().get("backup_days", 7)
    if days <= 0:
        return
    d = settings_backup_dir()
    try:
        latest = max((os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)
                      if _BK_NAME_RE.match(f)), default=0)
    except OSError:
        latest = 0
    if time.time() - latest >= days * 86400:
        settings_backup_make(auto=True)

# --- периодические SMART-самотесты (короткий/длинный) ночью ------------------
SMARTTEST_FILE = os.path.join(NAS_CONFIG, "smart-selftest.json")

def _smart_selftest_tick():
    """Раз в N дней запускать самотест дисков (в 03–06 ночи, один вид за ночь;
    длинный приоритетнее). Сам тест идёт внутри диска и не мешает работе."""
    if not (3 <= time.localtime().tm_hour < 6):
        return
    cfg = load_maintenance()
    try:
        with open(SMARTTEST_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    now = time.time()
    for kind, key in (("long", "smart_long_days"), ("short", "smart_short_days")):
        days = cfg.get(key, 0)
        if days <= 0 or now - st.get(kind, 0) < days * 86400:
            continue
        devs = []
        for dev in _phys_devs():
            r = _run(["smartctl", "-t", kind, dev], timeout=30)
            if r["ok"] or "has begun" in (r.get("log") or ""):
                devs.append(os.path.basename(dev))
        st[kind] = now
        try:
            _json_save(SMARTTEST_FILE, st)
        except OSError:
            pass
        if devs:
            try:
                log_event("action", "SMART-самотест (%s) запущен" % ("длинный" if kind == "long" else "короткий"),
                          "Диски: " + ", ".join(devs), "info", kind="disk", desk=False)
            except Exception:
                pass
        break                      # один вид тестов за ночь

def _nb_sched_tick():
    """Запуск бэкапа главного NAS по расписанию (раз в минуту, без повтора в ту же минуту)."""
    global _nb_last_sched
    now = time.time(); slot = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    if slot != _nb_last_sched:
        _nb_last_sched = slot
        for cfg in nb_profiles():
            if nb_schedule_due(cfg, now):
                nb_run_bg(cfg["id"], dry=False)
    _nb_queue_drain()      # освободилось — берём следующего из очереди

def notify_event(name, key, title, msg, lvl=None, priority=None, cooldown=1800):
    """Доставка события вне monitor_tick: журнал всегда + Pushover, если включён и ev.on.
    key — ключ кулдауна (реюзает _MON_LAST, как fire())."""
    now = time.time()
    if cooldown and now - _MON_LAST.get(key, 0) < cooldown:
        return False
    _MON_LAST[key] = now
    cfg = load_monitor(); ev = cfg.get("events", {}).get(name, {})
    if priority is None:
        priority = ev.get("priority", 0)
    try:
        log_event(name, title, msg, lvl)
    except Exception:
        pass
    if cfg.get("enabled") and ev.get("on"):
        push_notify(title, msg, priority)
    return True

def agent_notify(b):
    """Alerts from local shell agents (nas-notify.sh: netguard/smartd/usb-import).
    Routing them through the panel translates the text (tr in push_notify),
    writes the event journal and — via the shared _MON_LAST cooldown keys —
    dedups against the panel's own monitor, so one incident no longer produces
    two pushes (raw RU from the agent + translated EN from the monitor)."""
    title = str(b.get("title") or "NAS")[:200]
    msg = str(b.get("msg") or "")[:2000]
    name = re.sub(r"[^a-z0-9_]", "", str(b.get("event") or ""))
    try:
        priority = max(-2, min(2, int(b.get("priority") or 0)))
    except (ValueError, TypeError):
        priority = 0
    key = (str(b.get("key") or "") or name or "agent:" + title)[:120]
    cfg = load_monitor()
    if name in cfg.get("events", {}):
        # monitor-catalog event: same key + cooldown as monitor_tick's fire(),
        # whichever watcher fires first wins and the other stays silent
        sent = notify_event(name, key, title, msg,
                            cooldown=cfg.get("cooldown", 1800))
        return {"ok": True, "sent": bool(sent)}
    # agent-only alert (wifi rescue, usb-import, …): push regardless of the
    # monitor toggle — these were always-on before; journal unless the agent
    # already has its own journal feed (usb-import log is parsed by the monitor)
    now = time.time()
    if now - _MON_LAST.get(key, 0) < 90:       # guard against tight agent loops
        return {"ok": True, "sent": False}
    _MON_LAST[key] = now
    if b.get("journal", 1) not in (0, "0", False):
        try:
            log_event(name or "agent", title, msg)
        except Exception:
            pass
    return {"ok": True, "sent": bool(push_notify(title, msg, priority))}

# ---- надёжность: авто-перемонтирование отвалившегося диска ----
_MOUNT_TRY = {}   # mp -> время последней попытки (не чаще раза в 5 мин)

def _fstab_targets():
    """Точки монтирования данных/пула из fstab (под /mnt), которые должны быть смонтированы."""
    out = []
    for line in _read("/etc/fstab").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = line.split()
        if len(f) < 4 or f[2] == "swap":
            continue
        mp = f[1]
        if (mp.startswith("/mnt/") or mp == STORAGE) and "noauto" not in f[3]:
            out.append(mp)
    return out

def _mounted(mp):
    try:
        return subprocess.run(["mountpoint", "-q", mp], timeout=8).returncode == 0
    except subprocess.SubprocessError:
        return False   # зависший/мёртвый mount не должен держать поток запроса вечно

def _stale_endpoint(mp):
    """Мёртвый FUSE-эндпоинт (mergerfs упал): stat() даёт ENOTCONN «Transport endpoint
    is not connected». mount на нём падает — точку сначала надо снять (umount -l)."""
    try:
        os.stat(mp)
        return False
    except OSError as e:
        return e.errno == errno.ENOTCONN
    except Exception:
        return False

def _automount_tick():
    if not load_maintenance().get("automount_recover", True):
        return
    now = time.time()
    for mp in _fstab_targets():
        stale = _stale_endpoint(mp)           # завис ли FUSE-эндпоинт (пул mergerfs)
        if _mounted(mp) and not stale:
            _MOUNT_TRY.pop(mp, None)
            continue
        # завис mergerfs — снимаем чаще (раз в минуту), обычный remount — не чаще 5 мин
        if now - _MOUNT_TRY.get(mp, 0) < (55 if stale else 300):
            continue
        _MOUNT_TRY[mp] = now
        if stale:
            # снять зависший эндпоинт, иначе mount выдаёт «Transport endpoint is not connected»
            _run(["umount", "-l", mp], timeout=20)
            _run(["fusermount", "-uz", mp], timeout=20)
        _run(["mount", mp], timeout=30)
        if _mounted(mp):
            notify_event("disk_remount", "remount:%s" % mp,
                         "NAS: пул переподключён" if stale else "NAS: диск переподключён",
                         "%s снова смонтирован автоматически" % mp, "ok", cooldown=120)
    # Пул mergerfs держит systemd-сервис с Restart=always — он сам поднимается за секунды.
    # Подстрахуем на случай, если сервис в failed: точка мертва/отсутствует → рестартим сервис.
    if os.path.exists("/etc/systemd/system/nas-mergerfs.service"):
        if (_stale_endpoint(STORAGE) or not _mounted(STORAGE)) and \
                now - _MOUNT_TRY.get(STORAGE, 0) >= 55:
            _MOUNT_TRY[STORAGE] = now
            _run(["systemctl", "restart", "nas-mergerfs.service"], timeout=40)
            if _mounted(STORAGE):
                notify_event("disk_remount", "remount:%s" % STORAGE, "NAS: пул переподключён",
                             "%s поднят сервисом nas-mergerfs" % STORAGE, "ok", cooldown=120)

def _pool_recovery():
    """Состояние сервиса пула mergerfs: активен ли и сколько раз systemd его
    автоматически перезапускал (= крашей FUSE восстановлено) с момента загрузки.
    None — если пул ещё не переведён на сервис (старая схема через fstab)."""
    if not os.path.exists("/etc/systemd/system/nas-mergerfs.service"):
        return None
    st = {"service": True, "active": _mounted(STORAGE), "restarts": 0}
    try:
        p = subprocess.run(["systemctl", "show", "nas-mergerfs.service",
                            "-p", "NRestarts", "-p", "ActiveState"],
                           capture_output=True, text=True, timeout=5)
        vals = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        st["restarts"] = int(vals.get("NRestarts", "0") or 0)
        st["active"] = vals.get("ActiveState") == "active" and _mounted(STORAGE)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return st

# ---- ежедневная/еженедельная сводка состояния ----
_LAST_SUMMARY = ""

def _build_summary():
    s = _safe(stats) or {}
    host = s.get("host", "NAS")
    lines = []
    up = s.get("uptime")
    if isinstance(up, (int, float)) and up > 0:
        d, rem = divmod(int(up), 86400); h, rem = divmod(rem, 3600); mi = rem // 60
        lines.append("Аптайм: " + (("%dд %dч" % (d, h)) if d else ("%dч %dм" % (h, mi)) if h else ("%dм" % mi)))
    elif up:
        lines.append("Аптайм: %s" % up)
    t = s.get("temp")
    if isinstance(t, (int, float)):
        lines.append("Температура: %d°C" % t)
    mem = (s.get("mem") or {}).get("pct")
    if isinstance(mem, (int, float)):
        lines.append("Память: %d%%" % mem)
    try:
        u = shutil.disk_usage(STORAGE)
        lines.append("Пул: занято %d%% (свободно %.0f ГБ)" % (
            round(100 * u.used / u.total), u.free / 1024**3))
    except OSError:
        pass
    try:
        h = health_report()
        bad = [c for c in (h.get("checks") or []) if c.get("lvl") in ("warn", "bad")]
        lines.append("Здоровье: %s" % ("всё в норме" if not bad
                     else ", ".join(c.get("name", "?") for c in bad[:4])))
    except Exception:
        pass
    try:
        for _p in nb_profiles():
            st = nb_status(_p["id"])
            if not st.get("ts"):
                continue
            okn = sum(1 for j in st.get("jobs", []) if j.get("ok"))
            _nm = (" «%s»" % _p["name"]) if len(nb_profiles()) > 1 else ""
            lines.append("Бэкап NAS%s: %d/%d ок" % (_nm, okn, len(st.get("jobs", []))))
    except Exception:
        pass
    return "NAS: сводка (%s)" % host, "\n".join(lines) or "нет данных"

def _summary_tick():
    global _LAST_SUMMARY
    m = load_maintenance()
    if not m.get("summary_enabled"):
        return
    lt = time.localtime()
    if time.strftime("%H:%M", lt) != m.get("summary_time", "09:00"):
        return
    if m.get("summary_freq") == "weekly" and _DOW[lt.tm_wday] != m.get("summary_dow", "Mon"):
        return
    slot = time.strftime("%Y-%m-%d %H:%M", lt)
    if slot == _LAST_SUMMARY:
        return
    _LAST_SUMMARY = slot
    title, body = _build_summary()
    notify_event("daily_summary", "summary:%s" % slot, title, body, "info", cooldown=0)

# ---- активная термозащита ----
_THERM = {"hot": 0, "cool": 0, "acted": {}}   # acted: name -> {"cpus": orig, "paused": bool}
# Что термозащита остановила/придушила — на диск. Иначе краш/ребут службы, пока
# контейнер на паузе, терял бы этот список: контейнер остался бы на паузе НАВСЕГДА,
# а панель бы «забыла», что сама его остановила. При старте осиротевшее снимаем.
THERM_FILE = os.path.join(NAS_CONFIG, "thermal-acted.json")

def _therm_save():
    try:
        _json_save(THERM_FILE, _THERM["acted"])
    except OSError:
        pass

def _therm_recover():
    """Разовое восстановление при старте: снять то, что термозащита оставила
    остановленным до краша/ребута. Если всё ещё горячо — тик снова среагирует."""
    acted = _json_load_strict(THERM_FILE, {})
    if isinstance(acted, dict) and acted:
        _THERM["acted"] = acted
        _therm_restore()   # разпаузит/вернёт cpus и очистит файл

def _hottest_container():
    """(имя, %CPU) самого нагружающего CPU контейнера, или (None, 0)."""
    r = _run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"], timeout=12)
    best, bestv = None, 0.0
    for l in (r.get("log") or "").splitlines():
        f = l.split("\t")
        if len(f) < 2:
            continue
        try:
            v = float(f[1].strip().rstrip("%"))
        except ValueError:
            continue
        if v > bestv and not re.search(r"(?i)dockge|postgres|mysql|mariadb|db", f[0]):
            best, bestv = f[0].strip(), v
    return best, bestv

def _therm_restore():
    for name, st in list(_THERM["acted"].items()):
        try:
            if st.get("paused"):
                _run(["docker", "unpause", name], timeout=15)
            _run(["docker", "update", "--cpus=%s" % (st.get("cpus") or 0), name], timeout=15)
        except Exception:
            pass
    if _THERM["acted"]:
        _THERM["acted"] = {}
        _therm_save()
        return True
    return False

def _thermal_tick():
    m = load_maintenance()
    mode = m.get("thermal_mode", "warn")
    if mode == "off":
        return
    t = _safe(temp_c)
    if not isinstance(t, (int, float)):
        return
    hot = int(m.get("thermal_hot", 80)); crit = int(m.get("thermal_crit", 85))
    if t >= hot:
        _THERM["hot"] += 1; _THERM["cool"] = 0
    elif t <= hot - 10:
        _THERM["cool"] += 1; _THERM["hot"] = 0
        if _THERM["cool"] >= 5 and _THERM["acted"]:
            if _therm_restore():
                notify_event("thermal_guard", "therm:restore", "NAS: температура в норме",
                             "остыло до %d°C — ограничения контейнеров сняты" % t, "ok", cooldown=300)
        return
    else:
        return
    if _THERM["hot"] < 3:      # реагируем только на устойчивый перегрев (~3 мин)
        return
    victim, cpu = _hottest_container()
    if mode == "warn":
        notify_event("thermal_guard", "therm:warn",
                     "NAS: перегрев %d°C" % t,
                     "температура держится ≥%d°C%s — проверьте охлаждение" % (
                         hot, (" (грузит: %s, %.0f%% CPU)" % (victim, cpu)) if victim else ""),
                     "warn", cooldown=1800)
        return
    # auto: душим/паузим самый жадный контейнер
    if not victim:
        notify_event("thermal_guard", "therm:auto", "NAS: перегрев %d°C" % t,
                     "нет контейнера-виновника — снизьте нагрузку вручную", "warn", cooldown=1800)
        return
    if t >= crit:
        try:
            _run(["docker", "pause", victim], timeout=15)
            _THERM["acted"].setdefault(victim, {"cpus": _container_cpus(victim)})["paused"] = True
            _therm_save()
        except Exception:
            pass
        notify_event("thermal_guard", "therm:auto",
                     "NAS: критический перегрев %d°C" % t,
                     "контейнер %s приостановлен до охлаждения" % victim, "crit", cooldown=600)
    else:
        if victim not in _THERM["acted"]:
            _THERM["acted"][victim] = {"cpus": _container_cpus(victim), "paused": False}
            _therm_save()
            _run(["docker", "update", "--cpus=0.5", victim], timeout=15)
            notify_event("thermal_guard", "therm:auto", "NAS: перегрев %d°C" % t,
                         "нагрузка %s ограничена (0.5 CPU) до охлаждения" % victim, "warn", cooldown=900)

def _container_cpus(name):
    r = _run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", name], timeout=10)
    try:
        return round(int((r.get("log") or "0").strip()) / 1e9, 2) or 0
    except (ValueError, TypeError):
        return 0

# Файл-сигнал: udev-хуки (монтирование/извлечение USB) трогают его, вотчер будит
# monitor_loop немедленно — вставка диска детектится за ~1-2с, а не на 60-сек тике.
POKE_FILE = "/run/nas-web-refresh"
_mon_wake = threading.Event()

def _poke_watcher():
    last = None
    while True:
        try:
            m = os.path.getmtime(POKE_FILE)
        except OSError:
            m = None
        if m != last:
            if last is not None:      # первый заход только запоминает — не будим на старте
                _mon_wake.set()
            last = m
        time.sleep(1.5)

def monitor_loop():
    _safe(_therm_recover)      # снять контейнеры, осиротевшие термозащитой до краша/ребута
    threading.Thread(target=_poke_watcher, daemon=True).start()
    while True:
        poked = _mon_wake.wait(60); _mon_wake.clear()
        # на poke (вставка/извлечение диска) гоняем только про изменения — быстро и
        # без лишнего: истории/расписаний/самотестов не трогаем, у них свой график.
        funcs = ((monitor_tick, _automount_tick, usb_ops_sync) if poked else
                 (history_sample, monitor_tick, maintenance_daily, _smart_selftest_tick,
                  _nb_sched_tick, _automount_tick, _summary_tick, _thermal_tick, usb_ops_sync,
                  _fsw_tick, _replica_tick, _remotes_tick,
                 _screen_tick))
        for fn in funcs:
            try:
                fn()
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
        _json_save(STACK_NOTES, d)
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
    cmap, wdmap = {}, {}
    for c in _docker_ps():
        proj = (c.get("Labels", "") or "")
        m = re.search(r"com\.docker\.compose\.project=([^,]+)", proj)
        key = m.group(1) if m else None
        wd = re.search(r"com\.docker\.compose\.project\.working_dir=([^,]+)", proj)
        if key and wd and key not in wdmap:
            wdmap[key] = wd.group(1)
        url = re.search(r"web-desktop\.url=([^,]+)", proj)
        ico = re.search(r"web-desktop\.icon=([^,]+)", proj)
        cmap.setdefault(key, []).append({
            "name": c.get("Names", ""), "state": c.get("State", ""),
            "status": c.get("Status", ""), "ports": c.get("Ports", ""),
            "image": c.get("Image", ""), "url": url.group(1) if url else "",
            "icon": ico.group(1) if ico else "",
            "health": _health_of(c.get("Status", ""))})
    notes = load_notes()
    custom = _safe(lambda: _store_load().get("custom") or {}, {})
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
        # stack icon: web-desktop.icon label → catalog meta.json → store.json custom card
        icon = (next((c["icon"] for c in conts if c["icon"]), "")
                or _safe(lambda: _store_meta(nm).get("icon") or "", "")
                or (custom.get(nm) or {}).get("icon", ""))
        out.append({"name": nm, "path": d, "has_compose": os.path.isfile(_compose_path(nm)),
                    "containers": conts, "running": running, "total": len(conts),
                    "url": url, "icon": icon, "note": notes.get(nm, "")})
    # orphans: compose projects whose containers are alive but the folder is gone
    # (deleted from /opt/stacks by hand) — docker keeps them running regardless.
    # A live project elsewhere on disk (working_dir exists) is not our business.
    seen = {s["name"] for s in out}
    for key, conts in sorted(cmap.items()):
        if not key or key in seen or not _STACK_RE.match(key):
            continue
        wd = wdmap.get(key, "")
        if wd and os.path.isdir(wd):
            continue
        running = sum(1 for c in conts if c["state"] == "running")
        icon = (next((c["icon"] for c in conts if c["icon"]), "")
                or _safe(lambda: _store_meta(key).get("icon") or "", "")
                or (custom.get(key) or {}).get("icon", ""))
        out.append({"name": key, "path": wd or os.path.join(STACKS_DIR, key),
                    "has_compose": False, "orphan": True,
                    "containers": conts, "running": running, "total": len(conts),
                    "url": next((c["url"] for c in conts if c["url"]), ""),
                    "icon": icon, "note": notes.get(key, "")})
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

def docker_overview():
    """Сводка для дашборда Docker: контейнеры, место (system df), версия."""
    if not shutil.which("docker"):
        return {"ok": False, "log": "docker не установлен"}
    out = {"ok": True}
    r = _run(["docker", "ps", "-a", "--format", "{{.State}}"], timeout=15)
    states = [l.strip() for l in (r.get("log") or "").splitlines() if l.strip()]
    out["containers"] = {"total": len(states), "running": states.count("running"),
                         "exited": sum(1 for s in states if s.startswith("exited")),
                         "restarting": states.count("restarting"),
                         "paused": states.count("paused")}
    r = _run(["docker", "system", "df", "--format",
              "{{.Type}}\t{{.TotalCount}}\t{{.Active}}\t{{.Size}}\t{{.Reclaimable}}"], timeout=25)
    df = {}
    ru = {"Images": "images", "Containers": "containers", "Local Volumes": "volumes", "Build Cache": "cache"}
    for l in (r.get("log") or "").splitlines():
        f = l.split("\t")
        if len(f) >= 5 and f[0] in ru:
            df[ru[f[0]]] = {"count": f[1], "active": f[2], "size": f[3], "reclaim": f[4]}
    out["df"] = df
    r = _run(["docker", "--version"], timeout=10)
    out["version"] = (r.get("log") or "").strip().replace("Docker version ", "").split(",")[0]
    _w = wud_state()      # обновления образов (если WUD установлен)
    out["wud"] = {"ok": _w.get("ok", False), "count": _w.get("count", 0),
                  "updates": _w.get("updates", []), "url": _w.get("url")}
    return out

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
    wud_invalidate()      # образы могли обновиться — плашка «есть обновление» пересчитается
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
    if not os.path.isdir(d):
        return _stack_zap(name)
    _dc(name, "down")
    try:
        shutil.rmtree(d)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True}

def _stack_zap(name):
    """Remove containers/networks of a compose project whose folder is gone:
    `compose down` needs the folder, so we go via the project label instead.
    Named volumes are left alone — they may hold user data."""
    flt = "label=com.docker.compose.project=" + name
    r = _run(["docker", "ps", "-aq", "--filter", flt], timeout=15)
    ids = (r.get("log") or "").split()
    if ids:
        r = _run(["docker", "rm", "-f", *ids], timeout=120)
        if not r["ok"]:
            return r
    n = _run(["docker", "network", "ls", "-q", "--filter", flt], timeout=15)
    nids = (n.get("log") or "").split()
    if nids:
        _run(["docker", "network", "rm", *nids], timeout=60)
    log_event("action", "Docker: убран осиротевший стек %s" % name,
              "контейнеров: %d" % len(ids), "ok", kind="svc", desk=False)
    return {"ok": True, "log": "контейнеров удалено: %d" % len(ids)}

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
    wud_invalidate()      # пересоздание/рестарт мог сменить образ — плашка пересчитается
    if action not in ("start", "stop", "restart", "rm"):
        return {"ok": False, "log": "недопустимое действие"}
    args = ["rm", "-f", cid] if action == "rm" else [action, cid]
    return _run(["docker", *args], timeout=60)

# --------------------------------------------------------------------------- #
#  Магазин приложений: каталог services/ (compose + meta.json) → /opt/stacks.
#  Установка = копия папки + .env из полей диалога + docker compose up (стрим).
#  store.json: карточки «своих» стеков (custom) и конфиги реплик (replica).
# --------------------------------------------------------------------------- #
SERVICES_DIR = os.path.join(HERE, "services")
STORE_FILE = os.path.join(NAS_CONFIG, "store.json")

def _store_load():
    return _json_load_strict(STORE_FILE, {})

def _store_save(d):
    _json_save(STORE_FILE, d, indent=2)

def _store_compose_src(sid):
    d = os.path.join(SERVICES_DIR, sid)
    for fn in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return None

def _store_meta(sid):
    return _json_load_strict(os.path.join(SERVICES_DIR, sid, "meta.json"), {})

def _store_subst(val):
    """Placeholders in meta defaults: {storage} {tz} {host} {rand}."""
    if not isinstance(val, str):
        return val
    if "{tz}" in val:
        val = val.replace("{tz}", _read("/etc/timezone") or "UTC")
    if "{rand}" in val:
        val = val.replace("{rand}", secrets.token_urlsafe(12))
    return val.replace("{storage}", STORAGE).replace("{host}", lan_ip())

def _replica_dir(sid):
    return os.path.join(NAS_CONFIG, "replica", sid)

def _replica_state(sid):
    """Last sync facts, read from files the sync script leaves behind."""
    rd = _replica_dir(sid)
    ver, ts = _read(os.path.join(rd, "version")), None
    dump = os.path.join(rd, "dump.sql.gz")
    if os.path.isfile(dump):
        ts = int(os.path.getmtime(dump))
    return {"version": ver, "synced": ts,
            "dump_mb": round(os.path.getsize(dump) / 1048576, 1) if ts else None}

def store_catalog():
    stacks = {s["name"]: s for s in docker_stacks().get("stacks", [])}
    st = _store_load()
    out = []
    try:
        ids = sorted(os.listdir(SERVICES_DIR))
    except OSError:
        ids = []
    for sid in ids:
        if not _store_compose_src(sid):
            continue
        m = _store_meta(sid)
        if m.get("hidden"):
            continue
        s = stacks.get(sid)
        rep = m.get("replica")
        item = {"id": sid, "name": m.get("name") or sid, "desc": m.get("desc") or "",
                "category": m.get("category") or "tools", "icon": m.get("icon") or "",
                "port": m.get("port"),
                "fields": [dict(f, default=_store_subst(f.get("default")))
                           for f in (m.get("fields") or []) if f.get("key")],
                "installed": bool(s), "running": bool(s and s["running"]),
                "total": (s or {}).get("total", 0)}
        if s and item["fields"]:
            # «Настроить…»: prefill the dialog with the live .env; secrets are
            # never sent back — the field just reports whether a value is set
            env = _stack_env(sid)
            for f in item["fields"]:
                cur = env.get(f["key"])
                if cur is None:
                    continue
                if f.get("secret"):
                    f["has_value"] = bool(cur)
                else:
                    f["default"] = cur
        if rep:
            cfg = (st.get("replica") or {}).get(sid) or {}
            item["replica"] = {"desc": rep.get("desc") or "",
                               "cfg": {k: v for k, v in cfg.items() if k != "pass"},
                               "has_pass": bool(cfg.get("pass")),
                               "dest_default": _stack_env(sid).get(rep.get("data_env") or "", ""),
                               "state": _replica_state(sid)}
        out.append(item)
    # свои стеки (не из каталога) — кандидаты на карточку/ярлык; published-порты
    # достаём из живых контейнеров, чтобы не гонять пользователя в редактор compose
    custom = st.get("custom") or {}
    def _host_ports(s):
        pts = set()
        for c in s.get("containers") or []:
            for mm in re.finditer(r":(\d+)->\d+/tcp", c.get("ports") or ""):
                pts.add(int(mm.group(1)))
        return sorted(pts)
    others = [{"id": n, "installed": True, "running": bool(s["running"]),
               "total": s["total"], "custom": custom.get(n), "ports": _host_ports(s)}
              for n, s in sorted(stacks.items()) if not _store_compose_src(n)]
    return {"ok": True, "apps": out, "own": others}

def _stack_env(name):
    """KEY=VALUE map from the installed stack's .env (empty if none)."""
    out = {}
    for line in _read(os.path.join(STACKS_DIR, name, ".env")).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out

def store_install(sid, values):
    src = _store_compose_src(sid) if _STACK_RE.match(sid or "") else None
    if not src:
        return {"ok": False, "log": "нет такого приложения"}
    m = _store_meta(sid)
    src_dir, dst = os.path.join(SERVICES_DIR, sid), os.path.join(STACKS_DIR, sid)
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src_dir):          # extra files (custom.css etc.) go along
        sp = os.path.join(src_dir, f)
        if f == "meta.json" or f.endswith(".example") or not os.path.isfile(sp):
            continue
        shutil.copy2(sp, os.path.join(dst, "compose.yaml" if sp == src else f))
    # .env: dialog values win, empty ones fall back to meta defaults; merge over
    # an existing .env so reinstall keeps manual edits it doesn't know about
    env = _stack_env(sid)
    for f in (m.get("fields") or []):
        k = f.get("key")
        if not k:
            continue
        v = (values or {}).get(k)
        if v in (None, ""):
            v = env.get(k) or _store_subst(f.get("default") or "")
        env[k] = str(v).replace("\n", " ")
        if f.get("type") == "path" and str(v).startswith("/"):
            try:
                os.makedirs(v, exist_ok=True)
                _chown_user(v)
            except OSError:
                pass
    with open(os.path.join(dst, ".env"), "w") as fh:   # env_file: .env must exist
        fh.write("\n".join("%s=%s" % kv for kv in env.items()) + "\n")
    log_event("action", "Магазин: установка %s" % (m.get("name") or sid), "", "ok",
              kind="svc", desk=False)
    return {"ok": True}

def store_icon_upload(stack, data_url):
    """Пользовательская иконка ярлыка: кладём в тот же кэш иконок под псевдо-URL
    custom://<stack>#<ts> (метка времени в ключе = браузерный кэш не отдаст старую
    картинку после замены)."""
    if not _STACK_RE.match(stack or ""):
        return {"ok": False, "log": "имя"}
    m = re.match(r"^data:image/(png|jpeg|svg\+xml|webp|gif|x-icon|vnd\.microsoft\.icon);base64,(.+)$",
                 data_url or "", re.S)
    if not m:
        return {"ok": False, "log": "нужна картинка: png / svg / jpeg / webp / gif / ico"}
    try:
        raw = base64.b64decode(m.group(2))
    except (ValueError, TypeError):
        return {"ok": False, "log": "битые данные"}
    if len(raw) > 1024 * 1024:
        return {"ok": False, "log": "иконка больше 1 МБ"}
    ext = {"png": ".png", "jpeg": ".jpg", "svg+xml": ".svg", "webp": ".webp",
           "gif": ".gif", "x-icon": ".ico", "vnd.microsoft.icon": ".ico"}[m.group(1)]
    url = "custom://%s#%d" % (stack, int(time.time()))
    os.makedirs(ICON_CACHE_DIR, exist_ok=True)
    with open(os.path.join(ICON_CACHE_DIR,
              hashlib.sha1(url.encode()).hexdigest() + ext), "wb") as f:
        f.write(raw)
    return {"ok": True, "icon": url}

def store_custom_save(stack, name, icon, port):
    if not _STACK_RE.match(stack or ""):
        return {"ok": False, "log": "имя"}
    st = _store_load()
    cust = st.setdefault("custom", {})
    if not (name or "").strip():
        cust.pop(stack, None)
    else:
        try:
            port = int(port) if port else None
        except (TypeError, ValueError):
            port = None
        cust[stack] = {"name": str(name).strip()[:40], "icon": str(icon or "").strip(),
                       "port": port}
    _store_save(st)
    return {"ok": True}

def _store_custom_forget(stacks):
    """Forget cards of stacks that no longer exist (called from discover_desktop_apps).
    Without this the desktop shortcut of a deleted stack lives on forever."""
    st = _store_load()
    cust = st.get("custom") or {}
    gone = [s for s in stacks if s in cust]
    if not gone:
        return
    for s in gone:
        cust.pop(s, None)
    st["custom"] = cust
    _store_save(st)
    # the stack name goes last: tr() substitutes literal Cyrillic fragments, so a
    # %s in the middle of the phrase would break the dictionary match
    log_event("user_action", "Ярлык убран со стола",
              "стек удалён, карточка ярлыка больше не нужна: %s" % ", ".join(gone),
              lvl="info", kind="docker", desk=False)

# ---- реплика приложения с другого NAS (рецепт в meta.json:replica) ----
def store_replica_save(sid, cfg):
    if not _store_meta(sid).get("replica"):
        return {"ok": False, "log": "у приложения нет рецепта реплики"}
    st = _store_load()
    cur = st.setdefault("replica", {}).setdefault(sid, {})
    for k in ("host", "user", "src_data", "dest_data"):
        if k in cfg:
            cur[k] = str(cfg.get(k) or "").strip()
    if "auto" in cfg:                       # "HH:MM" = ежедневный автосинк, "" = выкл
        a = str(cfg.get("auto") or "").strip()
        cur["auto"] = a if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", a) else ""
    if cfg.get("pass"):                     # пустое поле = пароль не трогаем
        cur["pass"] = str(cfg["pass"])
    if cfg.get("clear_pass"):
        cur.pop("pass", None)
    _store_save(st)
    return {"ok": True}

def _replica_ssh(cfg):
    """(ssh-команда, env) — sshpass при пароле, иначе ключевой вход."""
    tgt = "%s@%s" % (cfg.get("user") or "root", cfg["host"])
    opts = "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
    if cfg.get("pass"):
        if not shutil.which("sshpass"):
            raise ValueError("для входа по паролю нужен sshpass: sudo apt install sshpass")
        return "sshpass -e ssh %s %s" % (opts, tgt), {"SSHPASS": cfg["pass"]}
    return "ssh %s %s" % (opts, tgt), {}

def store_replica_script(sid, mode):
    """(bash-скрипт, env) для стрима: mode=sync (дамп+rsync) | restore (поднять реплику).
    ValueError с человекочитаемым текстом, если что-то не настроено."""
    rep = _store_meta(sid).get("replica") or {}
    if not rep:
        raise ValueError("у приложения нет рецепта реплики")
    cfg = (_store_load().get("replica") or {}).get(sid) or {}
    rd = _replica_dir(sid)
    dump = os.path.join(rd, "dump.sql.gz")
    if mode == "sync":
        if not cfg.get("host") or not cfg.get("src_data"):
            raise ValueError("реплика не настроена: адрес источника и путь медиатеки")
        dest = cfg.get("dest_data") or _stack_env(sid).get(rep.get("data_env") or "", "")
        if not dest:
            raise ValueError("не задана папка данных на этом NAS (установите приложение или укажите путь)")
        os.makedirs(rd, exist_ok=True)
        ssh, env = _replica_ssh(cfg)
        rsync_e = ssh.rsplit(" ", 1)[0]     # та же команда без host — для rsync -e
        q = shlex.quote
        script = """set -e
echo "== версия на источнике"
VER=$(%(ssh)s %(vcmd)s); echo "$VER"
printf '%%s' "$VER" > %(vfile)s
echo "== дамп базы на источнике (pg_dumpall | gzip)"
%(ssh)s %(dcmd)s > %(dump)s.part
mv %(dump)s.part %(dump)s
ls -lh %(dump)s
echo "== rsync медиатеки (первый раз может быть долго)"
mkdir -p %(dest)s
rsync -a --delete --info=progress2 -e %(re)s %(tgt)s:%(srcd)s/ %(dest)s/
echo "== синхронизация завершена: версия источника $VER"
""" % {"ssh": ssh, "vcmd": q(rep["version_cmd"]), "vfile": q(os.path.join(rd, "version")),
       "dcmd": q(rep["dump_cmd"]), "dump": q(dump), "re": q(rsync_e),
       "tgt": "%s@%s" % (cfg.get("user") or "root", cfg["host"]),
       "srcd": q(cfg["src_data"].rstrip("/")), "dest": q(dest.rstrip("/"))}
        return script, env
    # restore: поднять реплику той же версией, что источник в момент дампа
    comp = _compose_path(sid)
    if not os.path.isfile(comp):
        raise ValueError("приложение не установлено на этом NAS — сначала «Установить»")
    if not os.path.isfile(dump):
        raise ValueError("нет дампа — сначала «Синхронизировать»")
    ver = _read(os.path.join(rd, "version"))
    tag = ver.rsplit(":", 1)[-1] if ":" in ver else ""
    if not tag:
        raise ValueError("не удалось определить версию источника — повторите синхронизацию")
    q = shlex.quote
    script = """set -e
cd %(dir)s
echo "== реплика поднимается версией источника: %(tag)s"
if grep -q '^%(vkey)s=' .env 2>/dev/null; then
  sed -i 's|^%(vkey)s=.*|%(vkey)s=%(tag)s|' .env
else
  echo '%(vkey)s=%(tag)s' >> .env
fi
DC="docker compose -f %(comp)s -p %(sid)s"
echo "== останавливаем стек"
$DC down --remove-orphans
echo "== поднимаем базу"
$DC up -d %(dbsvc)s
echo "== ждём готовность Postgres"
docker exec %(dbc)s %(wait)s
echo "== восстанавливаем дамп (вывод psql скрыт)"
gunzip -c %(dump)s | docker exec -i %(dbc)s %(psql)s > /dev/null
echo "== тянем образы и запускаем всё"
$DC pull --quiet || true
$DC up -d
echo "== реплика обновлена: версия %(tag)s, дамп от $(date -r %(dump)s '+%%F %%T')"
""" % {"dir": q(os.path.join(STACKS_DIR, sid)), "comp": q(comp), "sid": q(sid),
       "tag": tag, "vkey": rep.get("version_env") or "VERSION",
       "dbsvc": rep.get("db_service") or "database",
       "dbc": rep.get("db_container") or (sid + "_db"),
       "wait": rep.get("wait_db_cmd") or "true", "psql": rep.get("psql_cmd") or "psql",
       "dump": q(dump)}
    return script, {}

# ---- автосинхронизация реплик (store.json: replica.<id>.auto = "HH:MM") ----
_REPLICA_RUN = set()          # sids syncing right now (manual runs don't set this)

def _replica_tick():
    """Kick the replica sync at the configured wall-clock minute, once a day.
    Runs in a background thread; the log lands next to the dump, the outcome
    goes to the event feed (failures also pop on the desktop)."""
    reps = _safe(lambda: _store_load().get("replica") or {}, {})
    hhmm, today = time.strftime("%H:%M"), time.strftime("%Y-%m-%d")
    for sid, cfg in (reps or {}).items():
        if (cfg or {}).get("auto") != hhmm or sid in _REPLICA_RUN:
            continue
        rd = _replica_dir(sid)
        stamp = os.path.join(rd, "auto-last")
        if _read(stamp) == today:
            continue
        os.makedirs(rd, exist_ok=True)
        with open(stamp, "w") as f:      # before the run: the loop may hit this minute twice
            f.write(today)
        try:
            script, env = store_replica_script(sid, "sync")
        except ValueError as e:
            log_event("replica_auto", "Реплика %s: автосинк не запущен" % sid, str(e),
                      "warn", kind="svc", desk=True)
            continue
        def run(sid=sid, script=script, env=env, rd=rd):
            try:
                r = subprocess.run(["bash", "-c", script], env=dict(_C_ENV, **env),
                                   capture_output=True, text=True, timeout=6 * 3600)
                out = (r.stdout or "") + (r.stderr or "")
                _safe(lambda: open(os.path.join(rd, "sync.log"), "w").write(out))
                ok = r.returncode == 0
                log_event("replica_auto",
                          "Реплика %s: %s" % (sid, "синхронизирована" if ok else "ошибка автосинка"),
                          "" if ok else "\n".join(out.splitlines()[-5:])[:400],
                          "ok" if ok else "warn", kind="svc", desk=not ok)
            except Exception as e:
                log_event("replica_auto", "Реплика %s: ошибка автосинка" % sid, str(e),
                          "warn", kind="svc", desk=True)
            finally:
                _REPLICA_RUN.discard(sid)
        _REPLICA_RUN.add(sid)
        threading.Thread(target=run, daemon=True).start()

# --------------------------------------------------------------------------- #
#  Внешние SSH-серверы в файловом менеджере: sshfs-маунты в /mnt/remote/<id>.
#  Смонтированный сервер — обычная папка, все операции ФМ работают как есть.
# --------------------------------------------------------------------------- #
REMOTES_FILE = os.path.join(NAS_CONFIG, "remotes.json")
REMOTE_MNT = "/mnt/remote"
_REMOTE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,30}$")

def _remotes_load():
    d = _json_load_strict(REMOTES_FILE, {})
    return d.get("remotes") or []

def _remotes_save(lst):
    _json_save(REMOTES_FILE, {"remotes": lst}, indent=2)

def _remote_mp(rid):
    return os.path.join(REMOTE_MNT, rid)

def _remote_unit(rid):
    return "nas-remote-%s.service" % rid

def _remote_listed(rid):
    mp = _remote_mp(rid)
    return any(l.split()[1:2] == [mp] for l in _read("/proc/mounts").splitlines())

def _remote_alive(rid):
    """A dead sshfs leaves its mountpoint in /proc/mounts, but every syscall on it fails
    with ENOTCONN — so "is it in /proc/mounts" is NOT the same as "does it work". statvfs
    on a broken FUSE mount is answered by the kernel right away (no round trip to the
    server), so this stays cheap."""
    try:
        os.statvfs(_remote_mp(rid))
        return True
    except OSError:
        return False

def _remote_mounted(rid):
    return _remote_listed(rid) and _remote_alive(rid)

def _remote_unstale(rid):
    """Tear down a mountpoint whose sshfs daemon is gone, so it can be mounted afresh.
    Without this the FM sees the stale entry, calls it mounted and never heals it."""
    if _remote_listed(rid) and not _remote_alive(rid):
        _run(["systemctl", "stop", _remote_unit(rid)], timeout=15)
        _run(["umount", "-l", _remote_mp(rid)], timeout=10)
        return True
    return False

def remotes_list():
    out = []
    for r in _remotes_load():
        out.append({"id": r["id"], "name": r.get("name") or r["id"],
                    "host": r.get("host", ""), "user": r.get("user", ""),
                    "port": r.get("port") or 22, "path": r.get("path") or "",
                    "has_pass": bool(r.get("pass")), "auto": bool(r.get("auto")),
                    "mounted": _remote_mounted(r["id"]), "mount": _remote_mp(r["id"])})
    return {"ok": True, "remotes": out, "sshfs": bool(shutil.which("sshfs"))}

def remotes_save(d):
    host = str(d.get("host") or "").strip()
    user = str(d.get("user") or "").strip() or "root"
    if not re.match(r"^[\w.\-]+$", host or "") or not re.match(r"^[\w.\-]+$", user):
        return {"ok": False, "log": "проверьте адрес и пользователя"}
    rid = str(d.get("id") or "").strip()
    lst = _remotes_load()
    cur = next((r for r in lst if r["id"] == rid), None)
    if cur is None:
        rid = re.sub(r"[^a-zA-Z0-9_-]", "-", (d.get("name") or host)).strip("-")[:24] or "srv"
        base, i = rid, 1
        while any(r["id"] == rid for r in lst):
            i += 1
            rid = "%s-%d" % (base, i)
        cur = {"id": rid}
        lst.append(cur)
    try:
        port = int(d.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    cur.update({"name": str(d.get("name") or "").strip()[:40] or host,
                "host": host, "user": user, "port": port,
                # пусто = домашняя папка (sshfs "host:"): у rsync.net и подобных
                # SFTP-хостингов корень / закрыт, доступен только home
                "path": str(d.get("path") or "").strip(),
                "auto": bool(d.get("auto"))})
    if d.get("pass"):
        cur["pass"] = str(d["pass"])
    if d.get("clear_pass"):
        cur.pop("pass", None)
    _remotes_save(lst)
    return {"ok": True, "id": rid}

def remotes_delete(rid):
    if not _REMOTE_ID.match(rid or ""):
        return {"ok": False, "log": "id"}
    remote_umount(rid)
    _remotes_save([r for r in _remotes_load() if r["id"] != rid])
    _safe(lambda: os.rmdir(_remote_mp(rid)))
    return {"ok": True}

def remote_mount(rid):
    r = next((x for x in _remotes_load() if x["id"] == rid), None)
    if not r:
        return {"ok": False, "log": "нет такого подключения"}
    if _remote_mounted(rid):
        return {"ok": True, "mount": _remote_mp(rid)}
    _remote_unstale(rid)          # dead daemon still in /proc/mounts → clear it, then remount
    if not shutil.which("sshfs"):
        return {"ok": False, "log": "sshfs не установлен: sudo apt install sshfs"}
    mp = _remote_mp(rid)
    os.makedirs(mp, exist_ok=True)
    # reconnect+ServerAlive: гуляющий Wi-Fi не оставляет «мёртвый» маунт, listing
    # падает с ошибкой за секунды, а не виснет; allow_other — папку видят и samba/oleg
    opts = ("reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,allow_other,"
            "default_permissions,StrictHostKeyChecking=accept-new,ConnectTimeout=10")
    if r.get("pass"):
        opts += ",password_stdin"
    src = "%s@%s:%s" % (r.get("user") or "root", r["host"], r.get("path") or "")
    # Own systemd unit per server, NOT a child of nas-web. A child would live in the
    # nas-web.service cgroup, and systemd kills that whole cgroup on restart — so every
    # `systemctl restart nas-web` silently killed the sshfs daemons and left stale
    # mountpoints behind (2026-07-12). As a unit it survives our restarts, and systemd
    # brings it back by itself if the daemon dies. sshfs runs with -f (foreground) so
    # systemd can actually supervise it instead of losing track after it daemonizes.
    unit = _remote_unit(rid)
    _run(["systemctl", "reset-failed", unit], timeout=10)
    inner = " ".join(shlex.quote(a) for a in
                     ["sshfs", "-f", src, mp, "-p", str(r.get("port") or 22), "-o", opts])
    cmd = ["systemd-run", "--unit", unit, "--collect",
           "--property=Restart=on-failure", "--property=RestartSec=5",
           "--property=ExecStopPost=/bin/umount -l %s" % mp]
    if r.get("pass"):
        # The password goes through the unit's environment, never argv — argv is readable
        # by anyone via /proc, the environment only by root. systemd re-applies it on every
        # restart, so a reconnect is fed the password as well. (StandardInputText would be
        # the obvious way, but systemd-run refuses it on a transient unit.)
        inner = 'printf %s "$NAS_SSHFS_PW" | ' + inner
        cmd += ["--setenv=NAS_SSHFS_PW=%s" % r["pass"]]
    cmd += ["/bin/sh", "-c", inner]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "сервер не ответил за 30 с"}
    except OSError as e:
        return {"ok": False, "log": str(e)}
    # systemd-run returns as soon as the unit starts; the mount appears a moment later.
    ok = False
    for _ in range(50):
        if _remote_mounted(rid):
            ok = True
            break
        time.sleep(0.2)
    if not ok:
        msg = (_run(["systemctl", "status", "--no-pager", "-n", "10", unit],
                    timeout=10).get("log") or p.stderr or p.stdout
               or "не смонтировалось").strip()[-300:]
        _run(["systemctl", "stop", unit], timeout=15)
        # sshfs = SFTP: если SSH пускает, а данные не идут — на сервере, скорее
        # всего, выключена служба SFTP (частый случай на Synology)
        if "Input/output error" in msg or "Connection reset" in msg:
            msg += " — похоже, на сервере выключен SFTP. Synology: Панель управления → Файловые службы → FTP → включить SFTP."
        return {"ok": False, "log": msg}
    log_event("action", "Подключён сервер: %s" % (r.get("name") or r["host"]), "", "ok",
              kind="files", desk=False)
    return {"ok": True, "mount": mp}

# авто-маунт: помеченные auto подключаются сами (после ребута, обрыва, недоступности);
# бэкофф 5 минут, чтобы не долбить выключенный сервер каждый тик
_REMOTE_TRY = {}

def _remotes_tick():
    for r in _remotes_load():
        rid = r["id"]
        if not r.get("auto") or _remote_mounted(rid):
            continue
        now = time.time()
        if now - _REMOTE_TRY.get(rid, 0) < 300:
            continue
        _REMOTE_TRY[rid] = now
        threading.Thread(target=lambda i=rid: _safe(lambda: remote_mount(i)),
                         daemon=True).start()

# резолв «настоящего» пути прямо на сервере: readlink -f + /proc/self/mountinfo
# раскрывают И симлинки, И bind-mount'ы (Ugreen: /Backup — bind на /volume2/Backup,
# через sftp это обычный каталог, симлинк-обходом не взять)
_REMOTE_REALPATH_SH = r'''p="$0"
if [ -e "$p" ]; then
  rp=$(readlink -f -- "$p" 2>/dev/null || echo "$p")
  awk -v p="$rp" '
  { dev[NR]=$3; root[NR]=$4; mp[NR]=$5 }
  END{
    bl=-1; bi=0
    for(i=1;i<=NR;i++){ m=mp[i]
      if(p==m || index(p, (m=="/")?"/":m"/")==1){ if(length(m)>bl){bl=length(m); bi=i} } }
    if(!bi){ print "REAL " p; exit }
    main=""
    for(i=1;i<=NR;i++) if(dev[i]==dev[bi] && root[i]=="/"){ main=mp[i]; break }
    r=root[bi]; if(r=="/") r=""
    if(main=="/") main=""
    out=main r substr(p, bl+1)
    print "REAL " ((out=="")?"/":out)
  }' /proc/self/mountinfo
else
  # путь есть только в chroot-витрине SFTP (Ugreen/Synology): ищем шару по имени
  # на томах и отдаём кандидатов с mtime — панель сверит с видом через sftp
  share="${p#/}"; share="${share%%/*}"
  rest="${p#/"$share"}"
  for c in /volume*/"$share"; do
    [ -e "$c$rest" ] && echo "CAND $c$rest $(stat -c %Y -- "$c$rest" 2>/dev/null || echo 0)"
  done
fi'''

def _remote_realpath_ssh(r, remote_path):
    """Спросить настоящий путь у самого сервера; None, если шелла/awk там нет."""
    cmd = ["ssh", "-p", str(r.get("port") or 22),
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8"]
    env = dict(_C_ENV)
    if r.get("pass"):
        if not shutil.which("sshpass"):
            return None
        cmd = ["sshpass", "-e"] + cmd
        env["SSHPASS"] = r["pass"]
    else:
        cmd += ["-o", "BatchMode=yes"]
    cmd += ["%s@%s" % (r.get("user") or "root", r["host"]),
            "sh -c %s %s" % (shlex.quote(_REMOTE_REALPATH_SH), shlex.quote(remote_path))]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=12, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    real, cands = None, []
    for line in (p.stdout or "").splitlines():
        if line.startswith("REAL /"):
            real = line[5:].strip()
        elif line.startswith("CAND /"):
            bits = line[5:].rsplit(" ", 1)
            try:
                c = (bits[0].strip(), int(bits[1]))
                if c not in cands:
                    cands.append(c)
            except (IndexError, ValueError):
                pass
    return real, cands

def remote_realpath(rid, local_path):
    """Настоящий путь В ПРОСТРАНСТВЕ СЕРВЕРА для файла внутри sshfs-маунта.
    Нельзя ни резолвить локально (realpath уйдёт за пределы маунта), ни тупо
    клеить базовый префикс: у NAS-ов (Ugreen/Synology) папки в корне SSH-вида —
    симлинки на /volumeN/…. Идём по компонентам: lstat/readlink через sshfs
    возвращают ЦЕЛИ ссылок в серверных путях — из них и собираем ответ."""
    r = next((x for x in _remotes_load() if x["id"] == rid), None)
    mp = _remote_mp(rid)
    lp0 = os.path.normpath(local_path or "")
    if not r or not (lp0 == mp or lp0.startswith(mp + os.sep)):
        return {"ok": False, "log": "путь вне маунта"}
    rp_cfg = (r.get("path") or "").strip()
    home_mode = rp_cfg == ""                 # маунт домашней папки: серверные пути относительные
    base = "" if rp_cfg in ("", "/") else rp_cfg.rstrip("/")
    def to_local(remote_abs):
        # серверный абсолютный путь → локальный через маунт (если достижим)
        if not base:
            return mp + remote_abs
        if remote_abs == base or remote_abs.startswith(base + "/"):
            return mp + remote_abs[len(base):]
        return None
    # идём только по хвосту ОТ базовой папки: компоненты самой базы через маунт
    # не видны (и резолвить их не нужно — пользователь задал базу буквально)
    parts = [p for p in lp0[len(mp):].split("/") if p]
    res, i, hops = base, 0, 0
    while i < len(parts):
        seg = parts[i]
        if seg == ".":
            i += 1; continue
        if seg == "..":
            res = res.rsplit("/", 1)[0]; i += 1; continue
        nxt = res + "/" + seg
        lp = to_local(nxt)
        if lp is None:      # цель ссылки за пределами базовой папки — дальше не заглянуть,
            break           # но сам серверный путь уже собран верно
        try:
            st = os.lstat(lp)
        except OSError:
            break
        if stat.S_ISLNK(st.st_mode) and hops < 40:
            hops += 1
            tgt = os.readlink(lp)
            tail = parts[i + 1:]
            if tgt.startswith("/"):
                res, i = "", 0
                parts = [p for p in tgt.split("/") if p] + tail
            else:                            # относительная ссылка — от текущего res
                parts = [p for p in tgt.split("/") if p] + tail
                i = 0
            continue
        res, i = nxt, i + 1
    if i < len(parts):
        res = res + "/" + "/".join(parts[i:])
    res = res or "/"
    # точнее знает сам сервер: bind-mounts и chroot-витрины SFTP через sshfs не
    # видны. Нет шелла на той стороне (rsync.net и т.п.) — остаёмся на sshfs-резолве
    ask = (res.lstrip("/") or ".") if home_mode else res
    got = _remote_realpath_ssh(r, ask)
    if got:
        real, cands = got
        if real:
            return {"ok": True, "path": real}
        if len(cands) == 1:
            return {"ok": True, "path": cands[0][0]}
        if len(cands) > 1:
            # шара с этим именем есть на нескольких томах — сверяем mtime
            # каталога через sftp с кандидатами
            try:
                mt = int(os.stat(lp0).st_mtime)
                hit = [c for c, m in cands if m == mt]
                if len(hit) == 1:
                    return {"ok": True, "path": hit[0]}
            except OSError:
                pass
    return {"ok": True, "path": res}

def remote_umount(rid):
    if not _REMOTE_ID.match(rid or ""):
        return {"ok": False, "log": "id"}
    # Stop the unit first — it owns the sshfs process and would otherwise restart it.
    # ExecStopPost unmounts; fusermount stays as the fallback for a mount left over by
    # an older build (or one whose daemon already died).
    _run(["systemctl", "stop", _remote_unit(rid)], timeout=20)
    if _remote_listed(rid):
        _run(["fusermount", "-uz", _remote_mp(rid)], timeout=15)   # lazy: не ждём зависший io
    ok = not _remote_listed(rid)
    return {"ok": ok, "log": "" if ok else "не размонтировалось"}

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
            "_proj": labels.get("com.docker.compose.project") or "",
        })
    # свои стеки, оформленные карточкой в магазине (store.json: custom) — ярлык
    # без правки чужого compose; стек с готовыми web-desktop-метками не дублируем
    cust = _safe(lambda: _store_load().get("custom") or {}, {})
    if cust:
        ps, stale = None, []
        for stack, c in sorted(cust.items()):
            hit = [a for a in apps if a.get("_proj") == stack]
            if hit:
                # ярлык, настроенный в панели, главнее web-desktop-меток compose:
                # метки часто приезжают с другого хоста с чужим URL
                for a in hit:
                    if c.get("name"):
                        a["name"] = c["name"]
                    if c.get("icon"):
                        a["icon"] = c["icon"]
                    if c.get("port"):
                        a["url"] = "http://%s:%d" % (lan_ip(), c["port"])
                continue
            if ps is None:
                ps = _docker_ps()
            conts = [x for x in ps
                     if ("com.docker.compose.project=%s" % stack) in (x.get("Labels") or "")]
            if not conts and not os.path.isdir(os.path.join(STACKS_DIR, stack)):
                # Стек снесли (ни контейнеров, ни папки в /opt/stacks) — карточка-призрак:
                # ярлык на столе живёт вечно, пока не почистить store.json. Чистим только
                # если docker вообще ответил (ps непустой), иначе мёртвый демон = пустой ps
                # и мы бы стёрли карточки живых стеков.
                if ps:
                    stale.append(stack)
                continue
            running = any(x.get("State") == "running" for x in conts)
            port = c.get("port")
            apps.append({"container": stack, "name": c.get("name") or stack,
                         "url": ("http://%s:%d" % (lan_ip(), port)) if port else "",
                         "icon": c.get("icon") or "", "running": running,
                         "status": "running" if running else "exited"})
        if stale:
            _store_custom_forget(stale)
    for a in apps:
        a.pop("_proj", None)
    apps.sort(key=lambda a: a["name"].lower())
    return apps

# --------------------------------------------------------------------------- #
#  Кэш иконок ярлыков (web-desktop.icon).  Браузер грузит иконку не из интернета,
#  а с NAS: сервер один раз качает картинку по URL и кладёт в ~/nas-config/icons/.
#  Ключ кэша = сам URL, поэтому смена метки (нового URL) = свежая загрузка,
#  а старый файл просто перестаёт использоваться.
# --------------------------------------------------------------------------- #
ICON_CACHE_DIR = os.path.join(NAS_CONFIG, "icons")
ICON_MAX_BYTES = 2 * 1024 * 1024          # 2 МБ на иконку — потолок
_icon_sem = threading.Semaphore(4)        # не долбить сеть десятками потоков
_ICON_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif", "image/bmp": ".bmp",
}
_ICON_EXTS = (".png", ".jpg", ".svg", ".ico", ".gif", ".webp", ".avif", ".bmp", "")

def _icon_cached_path(url):
    """Готовый файл кэша для URL (ищем <hash>.* среди известных расширений) или None."""
    h = hashlib.sha1(url.encode("utf-8", "surrogatepass")).hexdigest()
    base = os.path.join(ICON_CACHE_DIR, h)
    for ext in _ICON_EXTS:
        p = base + ext
        if os.path.isfile(p):
            return p
    return None

def fetch_icon(url):
    """Путь к локальной копии иконки по http(s)-URL (качает при отсутствии) или None.
    custom:// — загруженные пользователем иконки ярлыков: только кэш, не качаем."""
    if re.match(r"custom://", url or "", re.I):
        return _icon_cached_path(url)
    if not re.match(r"https?://", url or "", re.I):
        return None
    hit = _icon_cached_path(url)
    if hit:
        return hit
    with _icon_sem:
        hit = _icon_cached_path(url)          # мог скачать параллельный запрос
        if hit:
            return hit
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nas-web"})
            with urllib.request.urlopen(req, timeout=12) as r:
                ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = r.read(ICON_MAX_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not data or len(data) > ICON_MAX_BYTES:
            return None
        ext = _ICON_CT_EXT.get(ct)
        if not ext:                            # тип не пришёл — угадать по URL
            path = urlparse(url).path.lower()
            for e in (".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif", ".webp", ".avif", ".bmp"):
                if path.endswith(e):
                    ext = ".jpg" if e == ".jpeg" else e
                    break
        if not ext:
            ext = ".png"
        h = hashlib.sha1(url.encode("utf-8", "surrogatepass")).hexdigest()
        try:
            os.makedirs(ICON_CACHE_DIR, exist_ok=True)
            p = os.path.join(ICON_CACHE_DIR, h + ext)
            tmp = p + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            return p
        except OSError:
            return None

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

def _chown_user(path, stop=None):
    """Отдать созданное обычному пользователю: панель работает от root, иначе
    загруженные файлы и папки остаются root:root и правятся только через sudo.
    stop — каталог, выше которого не подниматься (владельца там не меняем)."""
    try:
        pw = pwd.getpwnam(TARGET_USER)
    except KeyError:
        return
    cur = path
    while True:
        try:
            os.chown(cur, pw.pw_uid, pw.pw_gid)
        except OSError:
            return
        if not stop or cur == stop:
            return
        nxt = os.path.dirname(cur)
        if nxt == cur or not nxt.startswith(stop):
            return
        if nxt == stop:
            return
        cur = nxt

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
# HEIC с айфона нарезан плитками 512×512 (напр. 8×6 для 4032×3024). ffmpeg читает
# его mov-демуксером и отдаёт ПЕРВУЮ ПЛИТКУ — миниатюра получалась куском угла.
# libheif (heif-convert) собирает картинку целиком. TIFF браузеры не рисуют вовсе.
_HEIF_EXT  = {"heic","heif"}
_VIEW_CONV = {"heic","heif","tif","tiff"}   # что нужно перегнать в JPEG для показа
# Крупные снимки (камера отдаёт 26 МП / 17 МБ) тоже уменьшаем: гнать оригинал в
# браузер бессмысленно — 40× трафика и тяжёлое декодирование на клиенте.
# gif/svg/ico не трогаем: анимация и вектор потеряются.
_VIEW_BIG_EXT = {"jpg","jpeg","png","webp","bmp","avif"}
VIEW_BIG_BYTES = 2 * 1024 * 1024
VIEW_PX    = 2560
_thumb_sem = threading.Semaphore(2)   # ограничить одновременный ffmpeg (миниатюры)
_view_sem  = threading.Semaphore(2)   # просмотр не должен ждать очередь миниатюр

def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def _heif_decode(src, out_jpg):
    """HEIC/HEIF → JPEG через libheif. True, если получилось.
    Промежуточный формат обязан быть JPEG, а не PNG: libheif декодирует
    12-мегапиксельный снимок за доли секунды, но zlib-сжатие PNG (~14 МБ)
    отнимало ещё ~4.7 с на каждое фото. JPEG q=92 отдаёт то же изображение
    (PSNR 45 dB против PNG-пути) в 12 раз быстрее.
    """
    if not shutil.which("heif-convert"):
        return False
    try:
        r = subprocess.run(["heif-convert", "-q", "92", src, out_jpg],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and os.path.isfile(out_jpg) and os.path.getsize(out_jpg) > 0

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
    # уникальный суффикс на КАЖДЫЙ вызов (pid одинаков во всех потоках, до 3 генераций разом)
    uniq = "%d.%s" % (os.getpid(), secrets.token_hex(4))
    tmp = tp + "." + uniq + ".tmp.jpg"
    ok = False
    with _thumb_sem:
        try:
            if kind == "img":
                # HEIC/HEIF сначала собираем через libheif — иначе ffmpeg возьмёт одну плитку
                if _ext(src) in _HEIF_EXT:
                    heif_tmp = tp + "." + uniq + ".heif.jpg"
                    if not _heif_decode(src, heif_tmp):
                        raise RuntimeError("heif-convert не смог")
                    ff_in = heif_tmp
                else:
                    ff_in = src
                # прозрачность PNG/WebP → подкладываем белый фон (иначе JPEG рисует мусор на месте альфы)
                cmd = ["ffmpeg","-y","-v","error","-i",ff_in,"-filter_complex",
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
                base = tp[:-4] + "." + uniq
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
            # подчистить все временные файлы этого вызова (в т.ч. варианты pdftoppm base-*.jpg)
            try:
                for leftover in glob.glob(tp + "." + uniq + "*") + glob.glob(tp[:-4] + "." + uniq + "*.jpg"):
                    try: os.remove(leftover)
                    except OSError: pass
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
    """Длительность + кодеки + список аудиодорожек и субтитров (для плеера/транскода)."""
    path = os.path.realpath(path)
    if not os.path.isfile(path) or not shutil.which("ffprobe"):
        return {"ok": False}
    dur, vc, ac = 0.0, "", ""
    audios, subs = [], []
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration:stream=codec_name,codec_type,channels:stream_tags=language,title",
                             "-of", "json", path], capture_output=True, text=True, timeout=12)
        j = json.loads(pr.stdout or "{}")
        dur = float(j.get("format", {}).get("duration") or 0)
        ai = si = 0
        for s in j.get("streams", []):
            ct = s.get("codec_type")
            tg = s.get("tags", {}) or {}
            lang = (tg.get("language") or "").strip()
            title = (tg.get("title") or "").strip()
            if ct == "video" and not vc:
                vc = s.get("codec_name", "")
            elif ct == "audio":
                if not ac:
                    ac = s.get("codec_name", "")
                audios.append({"i": ai, "codec": s.get("codec_name", ""), "lang": lang,
                               "title": title, "ch": s.get("channels", 0)})
                ai += 1
            elif ct == "subtitle":
                # только текстовые сабы можно отдать как WebVTT (картиночные pgs/dvdsub — нет)
                cn = (s.get("codec_name") or "").lower()
                subs.append({"i": si, "codec": cn, "lang": lang, "title": title,
                             "text": cn in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")})
                si += 1
    except Exception:
        pass
    return {"ok": True, "duration": dur, "vcodec": vc, "acodec": ac,
            "audios": audios, "subs": subs}

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

def thumbs_cache_stat():
    """(суммарный размер в байтах, число файлов) кэша миниатюр."""
    total = n = 0
    for root, _dirs, files in os.walk(THUMBS_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f)); n += 1
            except OSError:
                pass
    return total, n

def thumbs_cache_clear():
    """Полностью очистить кэш миниатюр. Возвращает число удалённых файлов."""
    n = 0
    for root, _dirs, files in os.walk(THUMBS_DIR):
        for f in files:
            try:
                os.remove(os.path.join(root, f)); n += 1
            except OSError:
                pass
    return n

def thumbs_cache_gc(limit_mb):
    """Держим кэш в пределах лимита: удаляем самые старые (по mtime), пока не влезем.
    limit_mb<=0 → без лимита (ничего не делаем). Возвращает число удалённых файлов."""
    try:
        limit_mb = int(limit_mb)
    except (ValueError, TypeError):
        return 0
    if limit_mb <= 0:
        return 0
    limit = limit_mb * 1024 * 1024
    files, total = [], 0
    for root, _dirs, fs in os.walk(THUMBS_DIR):
        for f in fs:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, fp)); total += st.st_size
    if total <= limit:
        return 0
    files.sort()          # старые первыми
    removed = 0
    for _mt, sz, fp in files:
        if total <= limit:
            break
        try:
            os.remove(fp); total -= sz; removed += 1
        except OSError:
            pass
    return removed

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

# Деревья, которые НИКОГДА не должны быть целью разрушительной операции файлового
# менеджера — даже для авторизованного админа: сам движок, корень ОС, системные
# каталоги. Острый край — пустой путь: os.path.realpath("") — это рабочий каталог
# процесса (/opt/nas-os), он проскакивал наивную проверку глубины и однажды унёс
# движок в корзину при пустом теле запроса. Чтение НЕ ограничиваем (это админ-панель).
_FS_PROTECTED = (HERE, "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
                 "/boot", "/proc", "/sys", "/dev", "/run", "/var")

def _fs_guard(path):
    """Нормализовать пользовательский путь для МУТИРУЮЩЕЙ операции.
    Возвращает (realpath, None) если можно, иначе (None, сообщение об ошибке)."""
    if not path or not str(path).strip():
        return None, "пустой путь"
    rp = os.path.realpath(path)
    if rp == "/" or rp.count("/") < 2:
        return None, "слишком опасный путь: " + rp
    for prot in _FS_PROTECTED:
        if rp == prot or rp.startswith(prot.rstrip("/") + os.sep):
            return None, "защищённый системный путь: " + rp
    return rp, None

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
    # защита от перезаписи движка/системных файлов через редактор ФМ (для этого
    # есть отдельные потоки; пустой путь тут — это realpath("")=/opt/nas-os)
    path, err = _fs_guard(path)
    if err:
        return {"ok": False, "log": err}
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
    d, err = _fs_guard(path)          # не качать в системные деревья/движок
    if err:
        return {"ok": False, "log": err}
    if not os.path.isdir(d):
        return {"ok": False, "log": "не каталог назначения"}
    fname = os.path.basename((name or "").strip()) or os.path.basename(unquote(_up(url).path)) or ""
    jid = hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    job = {"id": jid, "name": fname or "…", "total": 0, "got": 0,
           "done": False, "ok": False, "log": "", "path": "", "cancel": False}
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
                        if job["cancel"]:
                            raise IOError("отменено пользователем")
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

def fs_fetch_cancel(jid):
    with _FETCH_LOCK:
        job = _FETCH_JOBS.get(jid)
    if not job:
        return {"ok": False, "log": "задача не найдена"}
    job["cancel"] = True
    return {"ok": True}

def fs_mkdir(path, name):
    parent, err = _fs_guard(path)     # не даём создавать каталоги в системных деревьях/движке
    if err:
        return {"ok": False, "log": err}
    d = _child(parent, name)
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
    src, err = _fs_guard(src)
    if err:
        return {"ok": False, "log": err}
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
    path, err = _fs_guard(path)
    if err:
        return {"ok": False, "log": err}
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
    src, err = _fs_guard(src)          # источник уносится — защищаем от системных деревьев
    if err:
        return {"ok": False, "log": err}
    dst_dir = os.path.realpath(dst_dir)
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

# --------------------------------------------------------------------------- #
#  Анализатор места (DaisyDisk-подобный). Фоновый обход одного тома (в пределах
#  одной ФС, как `du -x`), строим плоскую карту каталогов с размерами, кэшируем
#  в ~/nas-config/duscan-<hash>.json. Фронт запрашивает по одному узлу (ленивый
#  drill-down) → маленькие ответы даже на терабайтных пулах.
# --------------------------------------------------------------------------- #
DUSCAN_TOPF = 12     # хранить топ-N крупнейших файлов на каталог
DUSCAN_MAXCH = 60    # максимум детей в узле (остальное → «прочее»)
DUSCAN_BIGN = 300    # глобальный топ-N крупнейших файлов тома
DUSCAN_DUPMIN = 1024 * 1024      # кандидаты в дубликаты — от 1 МиБ (мелочь не интересна)
DUSCAN_DUPCAP = 6000             # максимум файлов-кандидатов в кэше (защита от гигантских деревьев)
_duscan = {}         # root -> статус/прогресс скана
_duscache = {}       # root -> загруженное дерево {nodes, ts, size, files, dirs}
_duscan_lock = threading.Lock()

def _duscan_cache_path(root):
    h = hashlib.md5(root.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return os.path.join(NAS_CONFIG, "duscan-" + h + ".json")

def _duscan_build(root):
    dev = os.stat(root).st_dev
    own = {}; topf = {}; nfiles = {}; kids = {}; parent = {}; order = []
    tb = [0]
    bigheap = []          # min-heap (size, path) — глобальный топ крупнейших файлов
    dupmap = {}; dupn = [0]  # size -> [paths] для поиска дубликатов (>= DUPMIN)
    for dp, dns, fns in os.walk(root, topdown=True, onerror=lambda e: None, followlinks=False):
        keep = []
        for d in dns:                       # не выходим за пределы ФС и не идём по симлинкам
            fp = os.path.join(dp, d)
            try:
                if os.path.islink(fp):
                    continue
                if os.stat(fp).st_dev != dev:
                    continue
            except OSError:
                continue
            keep.append(d)
        dns[:] = keep
        ob = 0; nf = 0; fl = []
        for fn in fns:
            fp = os.path.join(dp, fn)
            try:
                if os.path.islink(fp):
                    continue
                sz = os.lstat(fp).st_size
            except OSError:
                continue
            ob += sz; nf += 1; fl.append((sz, fn))
            # глобальный топ крупнейших файлов
            if len(bigheap) < DUSCAN_BIGN:
                heapq.heappush(bigheap, (sz, fp))
            elif sz > bigheap[0][0]:
                heapq.heapreplace(bigheap, (sz, fp))
            # кандидаты в дубликаты: группируем по размеру (только крупные)
            if sz >= DUSCAN_DUPMIN and dupn[0] < DUSCAN_DUPCAP:
                dupmap.setdefault(sz, []).append(fp); dupn[0] += 1
        fl.sort(reverse=True)
        own[dp] = ob; nfiles[dp] = nf; topf[dp] = fl[:DUSCAN_TOPF]; kids[dp] = []; order.append(dp)
        for d in dns:
            parent[os.path.join(dp, d)] = dp
        tb[0] += ob
        with _duscan_lock:
            s = _duscan.get(root)
            if s:
                if s.get("cancel"):
                    raise RuntimeError("cancelled")
                s["scanned"] = len(order); s["bytes"] = tb[0]
    for d in order:
        p = parent.get(d)
        if p in kids:
            kids[p].append(d)
    total = {}
    for d in sorted(order, key=lambda x: x.count("/"), reverse=True):   # дети раньше родителей
        t = own.get(d, 0)
        for c in kids.get(d, []):
            t += total.get(c, 0)
        total[d] = t
    nodes = {}
    for d in order:
        ch = []
        for c in kids.get(d, []):
            ch.append({"n": os.path.basename(c) or c, "p": c, "s": total.get(c, 0), "d": 1})
        shown = 0
        for sz, fn in topf.get(d, []):
            ch.append({"n": fn, "p": os.path.join(d, fn), "s": sz}); shown += 1
        extra = nfiles.get(d, 0) - shown
        if extra > 0:
            esz = own.get(d, 0) - sum(s for s, _ in topf.get(d, []))
            if esz > 0:
                ch.append({"n": "… ещё %d" % extra, "s": esz, "o": 1})
        ch.sort(key=lambda x: x["s"], reverse=True)
        if len(ch) > DUSCAN_MAXCH:
            rest = ch[DUSCAN_MAXCH:]; ch = ch[:DUSCAN_MAXCH]
            ch.append({"n": "… прочее (%d)" % len(rest), "s": sum(x["s"] for x in rest), "o": 1})
        nodes[d] = {"s": total.get(d, 0), "ch": ch}
    bigfiles = [{"p": p, "s": s} for s, p in sorted(bigheap, reverse=True)]
    # кандидаты-дубликаты: только размеры, встретившиеся >1 раза; топ по потенциальной экономии
    dupcand = [{"s": sz, "paths": ps} for sz, ps in dupmap.items() if len(ps) > 1]
    dupcand.sort(key=lambda g: g["s"] * (len(g["paths"]) - 1), reverse=True)
    dupcand = dupcand[:500]
    return {"root": root, "ts": time.time(), "size": total.get(root, 0),
            "files": sum(nfiles.values()), "dirs": len(order), "nodes": nodes,
            "bigfiles": bigfiles, "dupcand": dupcand}

def _duscan_run(root):
    try:
        data = _duscan_build(root)
    except Exception as e:
        with _duscan_lock:
            if (_duscan.get(root) or {}).get("cancel"):
                _duscan[root] = {"status": "none", "root": root}
            else:
                _duscan[root] = {"status": "error", "root": root, "error": str(e)[:200]}
        return
    try:
        _json_save(_duscan_cache_path(root), data)   # atomic: a power cut mid-write mustn't leave a corrupt scan cache
    except OSError:
        pass
    with _duscan_lock:
        _duscache[root] = data
        _duscan[root] = {"status": "done", "root": root, "ts": data["ts"],
                         "size": data["size"], "files": data["files"], "dirs": data["dirs"]}

_duscan_auto_last = 0

def _duscan_auto(hours):
    """Периодически освежать УЖЕ сканированные тома (кэш старше N часов). 0 = выкл.
    Зовётся из monitor_tick (раз в минуту), но проверяет не чаще раза в ~15 мин;
    один скан за проход — du нагружает диск, пачкой гонять незачем."""
    global _duscan_auto_last
    try:
        hours = float(hours or 0)
    except (ValueError, TypeError):
        hours = 0
    if hours <= 0:
        return
    now = time.time()
    if now - _duscan_auto_last < 900:      # не чаще раза в 15 минут
        return
    _duscan_auto_last = now
    for f in sorted(glob.glob(os.path.join(NAS_CONFIG, "duscan-*.json"))):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        root, ts = d.get("root"), d.get("ts", 0)
        if not root or not os.path.isdir(root) or now - ts < hours * 3600:
            continue
        with _duscan_lock:
            if (_duscan.get(root) or {}).get("status") == "scanning":
                continue
        duscan_start(root)      # фоновый; освежит кэш и его ts
        break                   # по одному за проход — остальные освежатся в следующие проверки

def duscan_start(root):
    root = os.path.realpath(root or "/")
    if not os.path.isdir(root):
        return {"ok": False, "log": "не каталог: " + root}
    with _duscan_lock:
        s = _duscan.get(root)
        if s and s.get("status") == "scanning":
            return {"ok": True, "status": "scanning"}
        _duscan[root] = {"status": "scanning", "root": root, "scanned": 0, "bytes": 0, "started": time.time()}
    threading.Thread(target=_duscan_run, args=(root,), daemon=True).start()
    return {"ok": True, "status": "scanning"}

def duscan_cancel(root):
    root = os.path.realpath(root or "/")
    with _duscan_lock:
        s = _duscan.get(root)
        if s and s.get("status") == "scanning":
            s["cancel"] = True
    return {"ok": True}

def _duscan_load_cache(root):
    if root in _duscache:
        return _duscache[root]
    try:
        with open(_duscan_cache_path(root)) as f:
            data = json.load(f)
        _duscache[root] = data
        return data
    except (OSError, ValueError):
        return None

def duscan_status(root):
    root = os.path.realpath(root or "/")
    with _duscan_lock:
        s = dict(_duscan.get(root) or {})
    if s.get("status") == "scanning":
        return {"status": "scanning", "scanned": s.get("scanned", 0), "bytes": s.get("bytes", 0), "root": root}
    if s.get("status") == "error":
        return {"status": "error", "error": s.get("error"), "root": root}
    data = _duscan_load_cache(root)
    if data:
        return {"status": "done", "ts": data.get("ts"), "size": data.get("size"),
                "files": data.get("files"), "dirs": data.get("dirs"), "root": root}
    return {"status": "none", "root": root}

def duscan_node(root, path, depth=1):
    root = os.path.realpath(root or "/")
    path = os.path.realpath(path or root)
    data = _duscan_load_cache(root)
    if not data:
        return {"ok": False, "log": "нет данных — запустите скан"}
    nodes = data.get("nodes", {})
    # раньше падали, если пути нет в скане; теперь показываем живой листинг (новые папки после скана)
    if path not in nodes and not os.path.isdir(path):
        return {"ok": False, "log": "нет данных по этому пути (пере-сканируйте)"}
    try:
        depth = max(1, min(3, int(depth)))
    except (ValueError, TypeError):
        depth = 1
    # build СЛИВАЕТ реальные подпапки (os.listdir) с размерами из скана: новые папки
    # помечаются new (нет размера), удалённые не показываются, файлы берём из скана.
    def build(p, dep):
        nd = nodes.get(p)
        scan_dirs, scan_rest = {}, []
        if nd:
            for c in nd.get("ch", []):
                if c.get("d") and c.get("p"):
                    scan_dirs[c["p"]] = c
                else:
                    scan_rest.append(c)   # файлы + агрегаты «… ещё/прочее»
        try:
            live = sorted(n for n in os.listdir(p) if os.path.isdir(os.path.join(p, n)))
        except OSError:
            live = None
        out = []
        if live is not None:
            for name in live:
                fp = os.path.join(p, name)
                sc = scan_dirs.get(fp)
                it = {"n": name, "d": 1, "p": fp}
                if sc is not None:
                    it["s"] = sc.get("s", 0)
                    if dep > 1:
                        sub = build(fp, dep - 1)
                        if sub:
                            it["c"] = sub
                else:
                    it["s"] = 0; it["new"] = 1   # папки нет в скане — не считана
                out.append(it)
            for c in scan_rest:
                it = {"n": c["n"], "s": c.get("s", 0)}
                if c.get("o"):
                    it["o"] = 1
                else:
                    it["p"] = c.get("p")
                out.append(it)
        else:
            for c in (nd.get("ch", []) if nd else []):
                it = {"n": c["n"], "s": c.get("s", 0)}
                if c.get("d"):
                    it["d"] = 1; it["p"] = c["p"]
                    if dep > 1:
                        sub = build(c["p"], dep - 1)
                        if sub:
                            it["c"] = sub
                elif c.get("o"):
                    it["o"] = 1
                else:
                    it["p"] = c.get("p")
                out.append(it)
        return out
    return {"ok": True, "root": root, "path": path,
            "s": (nodes.get(path) or {}).get("s", 0),
            "ch": build(path, depth), "scanTs": data.get("ts")}

def duscan_bigfiles(root):
    """Глобальный топ крупнейших файлов тома (собран при скане)."""
    data = _duscan_load_cache(os.path.realpath(root or "/"))
    if not data:
        return {"ok": False, "log": "нет данных — запустите скан"}
    # фильтруем удалённые после скана
    bf = [f for f in data.get("bigfiles", []) if os.path.isfile(f["p"])]
    return {"ok": True, "root": root, "files": bf[:300], "scanTs": data.get("ts")}

def _file_hash_partial(p, sz):
    """Быстрый отпечаток: голова+хвост по 256 КБ + размер. При равном размере
    практически исключает ложные совпадения (для дедуп-инструмента достаточно)."""
    h = hashlib.md5()
    with open(p, "rb") as f:
        h.update(f.read(262144))
        if sz > 524288:
            f.seek(-262144, 2); h.update(f.read(262144))
    h.update(str(sz).encode())
    return h.hexdigest()

def duscan_dups(root):
    """Найти дубликаты среди кандидатов скана (равный размер → сверка отпечатка)."""
    data = _duscan_load_cache(os.path.realpath(root or "/"))
    if not data:
        return {"ok": False, "log": "нет данных — запустите скан"}
    cand = data.get("dupcand", [])
    groups = []; t0 = time.time(); truncated = False
    for g in cand:
        if time.time() - t0 > 25:
            truncated = True; break
        byh = {}
        for p in g["paths"]:
            try:
                if not os.path.isfile(p):
                    continue
                byh.setdefault(_file_hash_partial(p, g["s"]), []).append(p)
            except OSError:
                continue
        for paths in byh.values():
            if len(paths) > 1:
                groups.append({"s": g["s"], "n": len(paths),
                               "waste": g["s"] * (len(paths) - 1), "paths": sorted(paths)})
    groups.sort(key=lambda x: x["waste"], reverse=True)
    return {"ok": True, "root": root, "groups": groups[:300],
            "waste": sum(x["waste"] for x in groups), "truncated": truncated,
            "scanTs": data.get("ts")}

# --------------------------------------------------------------------------- #
#  File history & integrity ("История файлов"): incremental manifest of the
#  user's folders (SQLite: path/size/mtime/BLAKE2b) + event journal
#  (add / del / mod / move / corrupt) + rotating content re-verification to
#  catch bitrot without SnapRAID parity. A scan is metadata-cheap: content is
#  hashed only for new/changed files plus a time-boxed slice of the
#  oldest-verified ones ("verify"), so the full pool gets re-read gradually.
#  Guards: a missing/empty watched root (unmounted disk, detached mergerfs
#  branch) skips deletion recording; a mass-delete above guard_pct holds the
#  events until the user confirms in the UI.
# --------------------------------------------------------------------------- #
FSW_DB  = os.path.join(NAS_CONFIG, "fswatch.db")
FSW_CFG = os.path.join(NAS_CONFIG, "fswatch.json")
FSW_DEF_EXCLUDE = [".trash", ".recycle", "#recycle", "@eaDir", "lost+found",
                   ".Trash-*", "node_modules", ".git", "__pycache__",
                   ".DS_Store", "._*", "Thumbs.db", "desktop.ini",
                   "*.tmp", "*.part", "*.crdownload", "*.!qB", "~$*"]
_fsw = {"status": "idle"}      # scan progress, shared with the API
_fsw_lock = threading.Lock()

class _FswCancel(Exception):
    pass

def _fsw_human(n):
    n = float(n or 0)
    for u in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return ("%d %s" if u == "Б" else "%.1f %s") % (n, u)
        n /= 1024
    return "%.1f ТБ" % n

def fsw_load():
    d = {"enabled": True, "roots": [], "exclude": list(FSW_DEF_EXCLUDE),
         "exclude_re": [], "time": "02:30", "verify_days": 30,
         "verify_minutes": 20, "guard_pct": 25}
    try:
        with open(FSW_CFG) as f:
            u = json.load(f)
        for k in d:
            if k in u:
                d[k] = u[k]
    except (OSError, ValueError):
        pass
    if not d["roots"] and os.path.isdir("/mnt/storage"):
        d["roots"] = ["/mnt/storage"]
    return d

def fsw_save(patch):
    cur = fsw_load()
    if "roots" in patch:
        rs = []
        for p in (patch.get("roots") or []):
            p = os.path.realpath(str(p))
            if os.path.isdir(p) and p != "/" and p not in rs:
                rs.append(p)
        cur["roots"] = rs
    if "exclude" in patch:
        cur["exclude"] = [str(x).strip() for x in (patch.get("exclude") or [])
                          if str(x).strip()][:200]
    if "exclude_re" in patch:
        rs = []
        for r in (patch.get("exclude_re") or [])[:100]:
            r = str(r).strip()
            if not r:
                continue
            try:
                re.compile(r)
            except re.error as e:
                return {"ok": False, "log": "regex «%s»: %s" % (r, e)}
            rs.append(r)
        cur["exclude_re"] = rs
    if "time" in patch and re.match(r"^\d\d:\d\d$", str(patch.get("time") or "")):
        cur["time"] = patch["time"]
    for k, lo, hi in (("verify_days", 1, 365), ("verify_minutes", 1, 240),
                      ("guard_pct", 0, 100)):
        if k in patch:
            try:
                cur[k] = max(lo, min(hi, int(patch[k])))
            except (ValueError, TypeError):
                pass
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    _json_save(FSW_CFG, cur)
    return {"ok": True, "config": cur}

def _fsw_db():
    db = sqlite3.connect(FSW_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, size INT,
        mtime INT, hash TEXT, verified INT);
      CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INT, kind TEXT, path TEXT, dst TEXT, size INT, info TEXT);
      CREATE INDEX IF NOT EXISTS ev_kind ON events(kind, id);
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);""")
    return db

def _fsw_meta(db, k, v=None):
    if v is None:
        r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None
    db.execute("INSERT INTO meta(k,v) VALUES(?,?) "
               "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

def _fsw_matcher(patterns, regexes=()):
    """Split exclude patterns into plain names / basename globs / path globs
    (+ user regexes matched against the full path); returns
    (skip_entry(name, full), excluded_path(full)) — the latter also checks
    every path component, for filtering stale manifest rows."""
    rxs = []
    for r in regexes:
        try:
            rxs.append(re.compile(r))
        except re.error:
            pass
    names, globs, paths = set(), [], []
    for p in patterns:
        p = p.rstrip("/")
        if not p:
            continue
        if "/" in p:
            paths.append(p)
        elif any(ch in p for ch in "*?["):
            globs.append(p)
        else:
            names.add(p)
    def _name_hit(nm):
        return nm in names or any(fnmatch.fnmatch(nm, g) for g in globs)
    def _re_hit(full):
        return any(rx.search(full) for rx in rxs)
    def skip_entry(nm, full):
        if _name_hit(nm) or _re_hit(full):
            return True
        return any(full == pp or full.startswith(pp + "/") or
                   fnmatch.fnmatch(full, pp) for pp in paths)
    def excluded_path(full):
        if any(_name_hit(c) for c in full.split("/") if c) or _re_hit(full):
            return True
        return any(full == pp or full.startswith(pp + "/") or
                   fnmatch.fnmatch(full, pp) for pp in paths)
    return skip_entry, excluded_path

def _fsw_hash(path):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb", buffering=0) as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            with _fsw_lock:
                _fsw["bytes"] = _fsw.get("bytes", 0) + len(b)
                if _fsw.get("cancel"):
                    raise _FswCancel()
    return h.hexdigest()

def _fsw_fire(key, title, msg, lvl=None, priority=None):
    """log_event + Pushover honoring the notifications settings (scan runs in
    its own thread, outside monitor_tick's local fire())."""
    try:
        log_event(key, title, msg, lvl)
    except Exception:
        pass
    try:
        m = load_monitor()
        ev = (m.get("events") or {}).get(key) or {}
        if m.get("enabled") and ev.get("on"):
            push_notify(title, msg, ev.get("priority", 0) if priority is None else priority)
    except Exception:
        pass

def _fsw_run(deep=False, manual=False):
    cfg = fsw_load()
    now = int(time.time())
    t0 = time.time()
    db = _fsw_db()
    try:
        skip_entry, excluded_path = _fsw_matcher(cfg["exclude"], cfg.get("exclude_re") or ())
        man = {}
        for path, size, mtime, hsh, ver in db.execute("SELECT * FROM files"):
            man[path] = (size, mtime, hsh, ver)
        # the flag is set only after a COMPLETE scan: a restart mid-baseline
        # resumes as baseline (already-hashed files are skipped by mtime/size)
        # instead of flooding the journal with "add" events
        baseline = _fsw_meta(db, "baseline") != "1"
        def under(p, root):
            return p == root or p.startswith(root + "/")
        # unmounted disk / detached mergerfs branch: root missing or suddenly
        # empty while the manifest has files there -> scan it as "absent"
        # would record thousands of false deletions; skip that root instead
        ok_roots, bad_roots = [], []
        for r in cfg["roots"]:
            try:
                nonempty = any(True for _ in os.scandir(r))
            except OSError:
                nonempty = False
            had = any(under(p, r) for p in man)
            (ok_roots if (os.path.isdir(r) and (nonempty or not had)) else bad_roots).append(r)
        if bad_roots:
            _fsw_fire("fsw_root", "NAS: папка наблюдения недоступна",
                      "Не вижу содержимого: %s. Диск не смонтирован? Удаления не записаны."
                      % ", ".join(bad_roots), lvl="warn")
        added, mods, ev_rows, corrupt = [], [], [], []
        seen = set()
        pend_ops = 0
        for r in ok_roots:
            try:
                dev = os.stat(r).st_dev
            except OSError:
                continue
            stack = [r]
            while stack:
                d = stack.pop()
                try:
                    ents = list(os.scandir(d))
                except OSError:
                    continue
                for e in ents:
                    with _fsw_lock:
                        if _fsw.get("cancel"):
                            raise _FswCancel()
                    nm = e.name
                    if skip_entry(nm, e.path):
                        continue
                    try:
                        if e.is_symlink():
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if st.st_dev == dev:      # do not cross into other mounts
                            stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    p = e.path
                    seen.add(p)
                    with _fsw_lock:
                        _fsw["files"] = _fsw.get("files", 0) + 1
                        _fsw["cur"] = p
                    size, mt = st.st_size, int(st.st_mtime)
                    old = man.get(p)
                    if old is None:
                        try:
                            h = _fsw_hash(p)
                        except OSError:
                            continue
                        db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?)",
                                   (p, size, mt, h, now))
                        added.append((p, size, h))
                    elif old[0] != size or old[1] != mt:
                        try:
                            h = _fsw_hash(p)
                        except OSError:
                            continue
                        db.execute("UPDATE files SET size=?,mtime=?,hash=?,verified=? WHERE path=?",
                                   (size, mt, h, now, p))
                        mods.append((p, old[0], size))
                    pend_ops += 1
                    if pend_ops >= 500:
                        db.commit(); pend_ops = 0
        db.commit()
        # deletions: manifest rows not on disk. Rows outside the current watch
        # list or matching a (new) exclude pattern are forgotten silently.
        gone, dels, moves = [], [], []
        for p in man:
            if p in seen:
                continue
            if not any(under(p, r) for r in cfg["roots"]) or excluded_path(p):
                db.execute("DELETE FROM files WHERE path=?", (p,))
            elif any(under(p, r) for r in ok_roots):
                gone.append(p)
        accepted = _fsw_meta(db, "accept") == "1"
        guard = (not baseline and not accepted and cfg["guard_pct"] > 0
                 and len(gone) >= 20
                 and 100.0 * len(gone) / max(1, len(man)) >= cfg["guard_pct"])
        if guard:
            _fsw_meta(db, "pending", json.dumps(
                {"ts": now, "count": len(gone), "total": len(man), "sample": gone[:20]}))
            _fsw_fire("fsw_guard", "NAS: массовая пропажа файлов",
                      "Пропало %d из %d файлов под наблюдением. События не записаны — "
                      "подтвердите удаление в «Истории файлов» или проверьте диски."
                      % (len(gone), len(man)), lvl="crit")
        else:
            _fsw_meta(db, "pending", "")
            if accepted:
                _fsw_meta(db, "accept", "")
            byh = {}
            for (p, size, h) in added:
                byh.setdefault((h, size), []).append(p)
            for p in gone:
                size, mt, h, ver = man[p]
                tgt = byh.get((h, size))
                if h and tgt:                     # same content appeared elsewhere = move
                    moves.append((p, tgt.pop(0), size))
                    if not tgt:
                        byh.pop((h, size))
                else:
                    dels.append((p, size))
                db.execute("DELETE FROM files WHERE path=?", (p,))
        moved_dst = {dst for _, dst, _ in moves}
        if not baseline:
            ev_rows += [(now, "add", p, None, size, None)
                        for (p, size, h) in added if p not in moved_dst]
            ev_rows += [(now, "del", p, None, size, None) for (p, size) in dels]
            ev_rows += [(now, "move", src, dst, size, None) for (src, dst, size) in moves]
        ev_rows += [(now, "mod", p, None, ns, _fsw_human(o) + " → " + _fsw_human(ns))
                    for (p, o, ns) in mods]
        # verify phase: re-read the oldest-verified unchanged files within the
        # nightly time budget (deep scan = no budget) to catch silent bitrot
        with _fsw_lock:
            _fsw["status"] = "verify"
        vt0 = time.time()
        vfiles = vbytes = 0
        budget = None if deep else cfg["verify_minutes"] * 60
        cutoff = now + 1 if deep else now - cfg["verify_days"] * 86400
        rows = db.execute("SELECT path,size,mtime,hash FROM files WHERE verified<? "
                          "ORDER BY verified", (cutoff,)).fetchall()
        for p, size, mt, h in rows:
            if budget is not None and time.time() - vt0 > budget:
                break
            with _fsw_lock:
                if _fsw.get("cancel"):
                    raise _FswCancel()
                _fsw["cur"] = p
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size != size or int(st.st_mtime) != mt:
                continue                          # legit change; next scan logs it
            try:
                h2 = _fsw_hash(p)
                st2 = os.stat(p)
            except OSError:
                continue
            if st2.st_size != size or int(st2.st_mtime) != mt:
                continue                          # changed while hashing
            vfiles += 1; vbytes += size
            if h and h2 != h:
                corrupt.append(p)
                ev_rows.append((now, "corrupt", p, None, size,
                                "содержимое изменилось при прежних дате и размере"))
            db.execute("UPDATE files SET hash=?,verified=? WHERE path=?", (h2, now, p))
        if ev_rows:
            db.executemany("INSERT INTO events(ts,kind,path,dst,size,info) "
                           "VALUES(?,?,?,?,?,?)", ev_rows)
        db.execute("DELETE FROM events WHERE id <= "
                   "(SELECT COALESCE(MAX(id),0)-40000 FROM events)")
        n_files, n_size = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
        summary = {"ts": now, "dur": int(time.time() - t0),
                   "added": len([1 for (p, s, h) in added if p not in moved_dst]),
                   "removed": len(dels), "modified": len(mods), "moved": len(moves),
                   "corrupt": len(corrupt), "verified": vfiles, "vbytes": vbytes,
                   "files": n_files, "size": n_size, "baseline": baseline,
                   "guard": bool(guard), "deep": bool(deep)}
        _fsw_meta(db, "last", json.dumps(summary))
        _fsw_meta(db, "baseline", "1")
        db.commit()
        if corrupt:
            _fsw_fire("fsw_corrupt", "NAS: повреждены файлы (битрот)",
                      "%d файл(ов) изменились без изменения даты/размера: %s"
                      % (len(corrupt), ", ".join(os.path.basename(x) for x in corrupt[:3])),
                      lvl="crit")
        try:
            thr = int(((load_monitor().get("events") or {}).get("fsw_del") or {})
                      .get("threshold", 50))
        except Exception:
            thr = 50
        if dels and thr and len(dels) >= thr:
            _fsw_fire("fsw_del", "NAS: удалено %d файлов" % len(dels),
                      "С прошлого скана исчезло %d файлов (%s). Подробности — в «Истории файлов»."
                      % (len(dels), _fsw_human(sum(s for _, s in dels))), lvl="warn")
        if baseline:
            _fsw_fire("fsw_scan", "История файлов: индекс построен",
                      "Проиндексировано %d файлов (%s)." % (n_files, _fsw_human(n_size)), lvl="ok")
        elif manual:
            _fsw_fire("fsw_scan", "История файлов: скан завершён",
                      "+%d −%d ~%d →%d · повреждений: %d · проверено %s за %d сек" %
                      (summary["added"], summary["removed"], summary["modified"],
                       summary["moved"], summary["corrupt"], _fsw_human(vbytes),
                       summary["dur"]), lvl="ok")
        with _fsw_lock:
            _fsw.update({"status": "idle", "cancel": False})
    except _FswCancel:
        db.commit()
        with _fsw_lock:
            _fsw.update({"status": "idle", "cancel": False})
    except Exception as e:
        with _fsw_lock:
            _fsw.update({"status": "error", "error": str(e)[:200], "cancel": False})
    finally:
        db.close()

def fsw_start(deep=False, manual=True):
    with _fsw_lock:
        if _fsw.get("status") in ("scan", "verify"):
            return {"ok": False, "log": "скан уже идёт"}
        _fsw.clear()
        _fsw.update({"status": "scan", "started": int(time.time()),
                     "files": 0, "bytes": 0, "deep": bool(deep)})
    threading.Thread(target=_fsw_run, args=(bool(deep), bool(manual)),
                     daemon=True).start()
    return {"ok": True}

def fsw_cancel():
    with _fsw_lock:
        if _fsw.get("status") in ("scan", "verify"):
            _fsw["cancel"] = True
    return {"ok": True}

def fsw_accept():
    """User confirmed the mass deletion: record it on the next scan."""
    db = _fsw_db()
    try:
        _fsw_meta(db, "accept", "1")
        _fsw_meta(db, "pending", "")
        db.commit()
    finally:
        db.close()
    return fsw_start()

def fsw_status():
    cfg = fsw_load()
    n = sz = fresh = nev = ncor = 0
    oldest = None
    last = pend = None
    dbsz = 0
    for suf in ("", "-wal"):
        try:
            dbsz += os.path.getsize(FSW_DB + suf)
        except OSError:
            pass
    try:
        db = _fsw_db()
        try:
            n, sz = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
            nev = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            ncor = db.execute("SELECT COUNT(*) FROM events WHERE kind='corrupt'").fetchone()[0]
            last = json.loads(_fsw_meta(db, "last") or "null")
            p = _fsw_meta(db, "pending")
            pend = json.loads(p) if p else None
            oldest = db.execute("SELECT MIN(verified) FROM files").fetchone()[0]
            fresh = db.execute("SELECT COUNT(*) FROM files WHERE verified>=?",
                               (int(time.time()) - cfg["verify_days"] * 86400,)).fetchone()[0]
        finally:
            db.close()
    except Exception:
        pass
    with _fsw_lock:
        prog = dict(_fsw)
    return {"ok": True, "config": cfg, "files": n, "size": sz, "last": last,
            "pending": pend, "oldest_verify": oldest, "verified_fresh": fresh,
            "events": nev, "corrupt": ncor, "db_size": dbsz, "progress": prog}

def _fsw_day_range(day):
    """'YYYY-MM-DD' -> (t0, t1) local-time epoch bounds, or None."""
    try:
        t0 = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
        return t0, t0 + 86400
    except (ValueError, OverflowError):
        return None

def fsw_events(before=0, limit=100, kind="", q="", day="", ts=0, group=False, days=0):
    def _i(v):
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0
    before, ts, limit = _i(before), _i(ts), max(1, min(500, _i(limit) or 100))
    days = _i(days)
    # scope conditions (q/day/days) are shared by the feed, the per-kind
    # counters and the group view; kind/pagination narrow the feed only
    scope, sargs = [], []
    if q:
        scope.append("(path LIKE ? OR dst LIKE ?)")
        sargs += ["%" + q + "%"] * 2
    if day:
        dr = _fsw_day_range(day)
        if dr:
            scope.append("ts>=? AND ts<?"); sargs += list(dr)
    elif days:
        scope.append("ts>=?"); sargs.append(int(time.time()) - days * 86400)
    try:
        db = _fsw_db()
        try:
            counts = dict(db.execute(
                "SELECT kind, COUNT(*) FROM events" +
                (" WHERE " + " AND ".join(scope) if scope else "") +
                " GROUP BY kind", sargs).fetchall())
            conds, args = list(scope), list(sargs)
            if kind:
                conds.append("kind=?"); args.append(kind)
            if ts:
                conds.append("ts=?"); args.append(ts)
            if group:
                # one group per (scan ts, kind): up to 5 sample rows + totals;
                # pagination by ts (before = ts of the previous page's tail)
                if before:
                    conds.append("ts<?"); args.append(before)
                sql = ("SELECT id,ts,kind,path,dst,size,info,cnt,tot FROM ("
                       "SELECT id,ts,kind,path,dst,size,info,"
                       " ROW_NUMBER() OVER (PARTITION BY ts,kind ORDER BY id) rn,"
                       " COUNT(*) OVER (PARTITION BY ts,kind) cnt,"
                       " SUM(COALESCE(size,0)) OVER (PARTITION BY ts,kind) tot"
                       " FROM events" +
                       (" WHERE " + " AND ".join(conds) if conds else "") +
                       ") WHERE rn<=5 ORDER BY ts DESC, kind, id LIMIT 300")
                rows = db.execute(sql, args).fetchall()
                full = len(rows) == 300
                groups, order = {}, []
                for (i, t, k, p, d, s, inf, cnt, tot) in rows:
                    key = (t, k)
                    if key not in groups:
                        groups[key] = {"ts": t, "kind": k, "n": cnt, "size": tot,
                                       "items": []}
                        order.append(key)
                    groups[key]["items"].append(
                        {"id": i, "ts": t, "kind": k, "path": p, "dst": d,
                         "size": s, "info": inf})
                if full and order:
                    tail = order[-1][0]          # ts may be cut mid-batch
                    order = [k for k in order if k[0] != tail] or order
                nxt = order[-1][0] if (full and order) else None
                return {"ok": True, "groups": [groups[k] for k in order],
                        "counts": counts, "next": nxt}
            if before:
                conds.append("id<?"); args.append(before)
            sql = ("SELECT id,ts,kind,path,dst,size,info FROM events" +
                   (" WHERE " + " AND ".join(conds) if conds else "") +
                   " ORDER BY id DESC LIMIT ?")
            rows = db.execute(sql, args + [limit]).fetchall()
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "log": str(e)[:200]}
    return {"ok": True, "counts": counts, "events": [
        {"id": i, "ts": t, "kind": k, "path": p, "dst": d, "size": s, "info": inf}
        for (i, t, k, p, d, s, inf) in rows]}

def fsw_activity(days=60):
    """Events per local day per kind — feeds the mini activity chart."""
    try:
        days = max(7, min(365, int(days or 60)))
    except (ValueError, TypeError):
        days = 60
    out = {}
    rows = []
    try:
        db = _fsw_db()
        try:
            rows = db.execute(
                "SELECT date(ts,'unixepoch','localtime') d, kind, COUNT(*) "
                "FROM events WHERE ts>=? GROUP BY d, kind",
                (int(time.time()) - days * 86400,)).fetchall()
        finally:
            db.close()
    except Exception:
        pass
    for d, k, n in rows:
        out.setdefault(d, {})[k] = n
    # hot folders: parent dirs with the most events over the period
    hot = {}
    try:
        db = _fsw_db()
        try:
            for k, p in db.execute("SELECT kind, path FROM events WHERE ts>=?",
                                   (int(time.time()) - days * 86400,)):
                d = (p or "").rsplit("/", 1)[0] or "/"
                a = hot.setdefault(d, {"n": 0})
                a["n"] += 1
                a[k] = a.get(k, 0) + 1
        finally:
            db.close()
    except Exception:
        pass
    top = sorted(hot.items(), key=lambda kv: -kv[1]["n"])[:12]
    return {"ok": True, "days": out,
            "hot": [dict(v, dir=d) for d, v in top]}

def fsw_file(path):
    """Manifest row + full event history of one path (info dialog)."""
    path = path or ""
    out = {"ok": True, "path": path, "file": None, "events": []}
    try:
        db = _fsw_db()
        try:
            r = db.execute("SELECT size,mtime,hash,verified FROM files WHERE path=?",
                           (path,)).fetchone()
            if r:
                out["file"] = {"size": r[0], "mtime": r[1], "hash": r[2],
                               "verified": r[3]}
            out["events"] = [
                {"id": i, "ts": t, "kind": k, "path": p, "dst": d, "size": s, "info": inf}
                for (i, t, k, p, d, s, inf) in db.execute(
                    "SELECT id,ts,kind,path,dst,size,info FROM events "
                    "WHERE path=? OR dst=? ORDER BY id DESC LIMIT 100", (path, path))]
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "log": str(e)[:200]}
    return out

def fsw_export_csv(days=365, kind="", q=""):
    import csv, io
    try:
        days = max(1, min(3650, int(days or 365)))
    except (ValueError, TypeError):
        days = 365
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "time", "event", "path", "moved_to", "size", "info"])
    try:
        db = _fsw_db()
        try:
            conds = ["ts>=?"]
            args = [int(time.time()) - days * 86400]
            if kind:
                conds.append("kind=?"); args.append(kind)
            if q:
                conds.append("(path LIKE ? OR dst LIKE ?)")
                args += ["%" + q + "%"] * 2
            for ts, k, p, d, s, inf in db.execute(
                    "SELECT ts,kind,path,dst,size,info FROM events WHERE " +
                    " AND ".join(conds) + " ORDER BY id DESC LIMIT 100000", args):
                lt = time.localtime(ts)
                w.writerow([time.strftime("%Y-%m-%d", lt), time.strftime("%H:%M:%S", lt),
                            k, p, d or "", s if s is not None else "", inf or ""])
        finally:
            db.close()
    except Exception:
        pass
    return buf.getvalue()

def fsw_clear(mode):
    """mode='events' wipes the history journal; 'all' drops the whole index
    (files + events + meta) so the next scan starts a fresh baseline."""
    if _fsw.get("status") in ("scan", "verify"):
        return {"ok": False, "log": "скан уже идёт"}
    try:
        db = _fsw_db()
        try:
            db.execute("DELETE FROM events")
            if mode == "all":
                db.execute("DELETE FROM files")
                db.execute("DELETE FROM meta")
            db.commit()
            db.execute("VACUUM")
        finally:
            db.close()
    except Exception as e:
        return {"ok": False, "log": str(e)[:200]}
    return {"ok": True}

_fsw_auto_day = ""
def _fsw_tick():
    """Nightly auto-scan at the configured time (called from monitor_loop)."""
    global _fsw_auto_day
    cfg = fsw_load()
    if not cfg.get("enabled") or not cfg.get("roots"):
        return
    day = time.strftime("%Y-%m-%d")
    if _fsw_auto_day == day or time.strftime("%H:%M") < cfg.get("time", "02:30"):
        return
    try:
        db = _fsw_db()
        try:
            last = json.loads(_fsw_meta(db, "last") or "null")
        finally:
            db.close()
    except Exception:
        last = None
    _fsw_auto_day = day
    if last and time.strftime("%Y-%m-%d", time.localtime(last.get("ts", 0))) == day:
        return                                    # already scanned today (manual)
    fsw_start(manual=False)

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
    # Атомарно: не-атомарная запись при крахе/гонке рвала index.json → корзина
    # «пустела», а файлы в files/ оставались осиротевшими и молча копили гигабайты.
    _json_save(os.path.join(TRASH, "index.json"), items)

def _trash_orphans(known_ids):
    """Каталоги в files/, которых нет в индексе (индекс был повреждён/сброшен, а
    файлы остались). Возвращаем их как записи корзины — иначе место не вернуть."""
    out = []
    store_dir = os.path.join(TRASH, "files")
    try:
        entries = os.listdir(store_dir)
    except OSError:
        return out
    for nm in entries:
        tid = nm.split("__", 1)[0]
        if tid in known_ids:
            continue
        p = os.path.join(store_dir, nm)
        sz = 0
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                for root, _, files in os.walk(p):
                    for f in files:
                        try: sz += os.path.getsize(os.path.join(root, f))
                        except OSError: pass
            else:
                sz = os.path.getsize(p)
        except OSError:
            pass
        out.append({"id": tid, "orig": "", "name": nm.split("__", 1)[-1],
                    "deleted": int(os.path.getmtime(p)) if os.path.exists(p) else 0,
                    "isdir": os.path.isdir(p), "size": sz, "store": p, "orphan": True})
    return out

def _trash_rm(store):
    if store and os.path.lexists(store):
        if os.path.isdir(store) and not os.path.islink(store):
            shutil.rmtree(store)
        else:
            os.remove(store)

def fs_trash(path):
    path, err = _fs_guard(path)
    if err:
        return {"ok": False, "log": err}
    if not os.path.lexists(path):
        # список в панели мог отстать: объект уже удалён или перемещён другим окном
        return {"ok": False, "log": "уже удалён или перемещён"}
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
    indexed = _trash_load()
    for it in indexed:
        it = dict(it)
        it["exists"] = os.path.lexists(it.get("store", ""))
        items.append(it)
    # осиротевшие файлы (в files/, но не в индексе) — тоже показываем, иначе их
    # место не вернуть из UI; помечаем orphan, restore для них недоступен (orig="")
    for orp in _trash_orphans({i.get("id") for i in indexed}):
        orp["exists"] = True
        items.append(orp)
    items.sort(key=lambda x: x.get("deleted", 0), reverse=True)
    return {"ok": True, "items": items}

def fs_trash_restore(tid, dest_dir=""):
    """Восстановить из корзины. dest_dir пуст → в исходную папку (orig); задан →
    в выбранную. Осиротевшие (orig неизвестен) можно вернуть только выбором папки."""
    items = _trash_load()
    hit = next((i for i in items if i.get("id") == tid), None)
    orphan = False
    if not hit:                                  # не в индексе — ищем среди осиротевших
        hit = next((o for o in _trash_orphans({i.get("id") for i in items})
                    if o.get("id") == tid), None)
        orphan = bool(hit)
    if not hit:
        return {"ok": False, "log": "не найдено в корзине"}
    store = hit.get("store")
    if not store or not os.path.lexists(store):
        if not orphan:
            _trash_save([i for i in items if i.get("id") != tid])
        return {"ok": False, "log": "файл отсутствует в хранилище"}
    if dest_dir:
        d, err = _fs_guard(dest_dir)
        if err:
            return {"ok": False, "log": err}
        if not os.path.isdir(d):
            return {"ok": False, "log": "цель не каталог"}
        target = os.path.join(d, hit.get("name") or os.path.basename(store))
    elif hit.get("orig"):
        target = hit["orig"]
    else:
        return {"ok": False, "log": "исходный путь неизвестен — выберите папку"}
    dest = _uniq(target)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(store, dest)
    except (OSError, shutil.Error) as e:
        return {"ok": False, "log": str(e)}
    if not orphan:
        _trash_save([i for i in items if i.get("id") != tid])
    return {"ok": True, "path": dest}

def fs_trash_delete(tid):
    items = _trash_load()
    hit = next((i for i in items if i.get("id") == tid), None)
    if not hit:                                  # не в индексе — возможно, осиротевший
        hit = next((o for o in _trash_orphans({i.get("id") for i in items})
                    if o.get("id") == tid), None)
    if not hit:
        return {"ok": False, "log": "не найдено"}
    try:
        _trash_rm(hit.get("store"))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _trash_save([i for i in items if i.get("id") != tid])
    return {"ok": True}

def fs_trash_empty():
    # Чистим ВСЮ папку files/, а не только индекс: иначе осиротевшее (индекс был
    # повреждён/сброшен) осталось бы на диске и «пустая» корзина копила бы гигабайты.
    items = _trash_load()
    store_dir = os.path.join(TRASH, "files")
    removed = 0
    try:
        for nm in os.listdir(store_dir):
            try:
                _trash_rm(os.path.join(store_dir, nm)); removed += 1
            except OSError:
                pass
    except OSError:
        pass
    _trash_save([])
    return {"ok": True, "count": max(removed, len(items))}

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
_login_lock = threading.Lock()

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
    # смена пароля отзывает ВСЕ прежние сессии (в т.ч. возможную украденную куку);
    # вызывающий сразу получит новую сессию в обработчике
    with _sess_lock:
        _sessions.clear()
        _save_sessions()
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

def _zram_swap_active():
    """Есть ли активный zram-своп в /proc/swaps."""
    for l in _read("/proc/swaps").splitlines():
        if l.startswith("/dev/zram"):
            return True
    return False

def _zram_status():
    """Состояние zram-swap: штатный (systemd-zram-generator/rpi-swap) или
    legacy zram-tools (zramswap.service). На современном Pi OS zram поднимает
    генератор, а zramswap.service мы глушим — поэтому статус берём по факту."""
    for u in ("dev-zram0.swap", "systemd-zram-setup@zram0.service"):
        s = _svc(u)
        if s["installed"]:
            # generated/static-юниты: "enabled" по факту активного свопа
            s["enabled"] = s["active"] or _zram_swap_active()
            return s
    return _svc("zramswap")

def _zram_off():
    """Выключить zram-swap: и штатный генератор, и legacy zramswap."""
    # legacy
    if _svc("zramswap")["installed"]:
        _svc_toggle("zramswap", False)
    # штатный: убрать zram на следующей загрузке
    try:
        if os.path.isfile("/etc/rpi/swap.conf"):
            os.makedirs("/etc/rpi/swap.conf.d", exist_ok=True)
            with open("/etc/rpi/swap.conf.d/60-nas-os.conf", "w") as f:
                f.write("# NAS-OS: zram-swap выключен из веб-панели. См. swap.conf(5).\n"
                        "[Main]\nMechanism=swapfile\n")
        elif os.path.isfile("/etc/systemd/zram-generator.conf") or \
                os.path.isfile("/usr/lib/systemd/zram-generator.conf"):
            with open("/etc/systemd/zram-generator.conf", "w") as f:
                f.write("# NAS-OS: zram-swap выключен (нет секции [zram0]).\n")
    except OSError:
        pass
    _run(["systemctl", "daemon-reload"], timeout=20)
    # снять на живую (best-effort)
    _run(["swapoff", "/dev/zram0"], timeout=20)
    for u in ("dev-zram0.swap", "systemd-zram-setup@zram0.service"):
        _run(["systemctl", "stop", u], timeout=20)
    _run(["systemctl", "reset-failed", "dev-zram0.swap",
          "systemd-zram-setup@zram0.service"], timeout=20)
    return {"ok": True, "reboot": False}

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

def _ufw_managed_ports():
    """Порты, которые firewall обязан держать открытыми, с подписями «для чего».
    Системные (панель/SSH/Cockpit/шары) + все опубликованные docker-порты —
    последние подхватываются автоматически при смене порта или новом контейнере."""
    ports = {}
    def add(p, label):
        ports.setdefault(p, label)
    add("%d/tcp" % PORT, "Веб-панель NAS")
    add("22/tcp", "SSH")
    add("5353/udp", "Обнаружение (mDNS / .local)")   # иначе UFW режет avahi → pi5.local отваливается
    if os.path.exists("/lib/systemd/system/cockpit.socket") or shutil.which("cockpit-bridge"):
        add("9090/tcp", "Cockpit")
    if shutil.which("smbd") or os.path.exists("/etc/samba/smb.conf"):
        add("445/tcp", "Файлы (Samba)")
    if os.path.exists("/etc/exports") and os.path.getsize("/etc/exports") > 0:
        add("2049/tcp", "Файлы (NFS)")
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
                           capture_output=True, text=True, timeout=8)
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0]
            for m in re.finditer(r"(?:0\.0\.0\.0|\[::\]):(\d+)->\d+/(tcp|udp)", parts[1]):
                add("%s/%s" % (m.group(1), m.group(2)), "Docker · " + name)
    except (OSError, subprocess.SubprocessError):
        pass
    return ports

_ufw_sync_last = 0

def ufw_autosync():
    """Держать открытыми нужные порты, пока UFW активен: новый docker-контейнер или
    смена порта не должны оставаться за firewall. Троттл — раз в 2 минуты."""
    global _ufw_sync_last
    if not shutil.which("ufw"):
        return
    status = _sc("ufw", "status")
    if "Status: active" not in status:
        return
    now = time.time()
    if now - _ufw_sync_last < 120:
        return
    _ufw_sync_last = now
    have = set(re.findall(r"(?m)^(\S+)\s+ALLOW", status))
    ssh_ok = "OpenSSH" in have or "22/tcp" in have
    for port in _ufw_managed_ports():
        if port == "22/tcp" and ssh_ok:      # OpenSSH-правило уже покрывает SSH
            continue
        if port not in have:
            _run(["ufw", "allow", port], timeout=15)

def _ufw_state():
    out = _sc("ufw", "status")
    ports = sorted(set(m.group(1) for m in re.finditer(r"(?m)^(\S+)\s+ALLOW", out)))
    labels = dict(_ufw_managed_ports())
    labels.setdefault("OpenSSH", "SSH")      # app-правила ufw → человеческая подпись
    labels.setdefault("Samba", "Файлы (Samba)")
    rows = [{"port": p, "label": labels.get(p, ""), "auto": p in labels} for p in ports]
    return {"installed": bool(shutil.which("ufw")),
            "active": "Status: active" in out, "ports": ports, "rows": rows}

F2B_CONF = "/etc/fail2ban/jail.d/nas.conf"

def _fail2ban_state():
    out = dict(_svc("fail2ban"))
    conf = _read(F2B_CONF)
    def g(k, d):
        m = re.search(r"(?m)^\s*%s\s*=\s*(\S+)" % k, conf)
        return m.group(1) if m else d
    out["maxretry"] = g("maxretry", "5")
    out["bantime"] = g("bantime", "1h")
    banned = []
    if out.get("active"):
        m = re.search(r"Banned IP list:\s*(.*)", _sc("fail2ban-client", "status", "sshd"))
        if m:
            banned = [x for x in m.group(1).split() if x]
    out["banned"] = banned
    return out

def fail2ban_save(maxretry, bantime):
    if not shutil.which("fail2ban-client"):
        return {"ok": False, "log": "fail2ban не установлен"}
    try:
        mr = max(1, min(100, int(maxretry)))
    except (ValueError, TypeError):
        return {"ok": False, "log": "порог: число"}
    bt = str(bantime or "").strip()
    if bt != "-1" and not re.match(r"^\d+[smhdw]?$", bt):     # 600 / 30m / 1h / 1d / -1 (навсегда)
        return {"ok": False, "log": "время бана: напр. 30m, 1h, 1d или -1 (навсегда)"}
    try:
        with open(F2B_CONF, "w") as f:
            f.write("[sshd]\nenabled = true\nmaxretry = %d\nbantime = %s\n" % (mr, bt))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _run(["systemctl", "reload-or-restart", "fail2ban"], timeout=20)
    return {"ok": True, "log": "сохранено"}

def fail2ban_unban(ip):
    if not re.match(r"^[0-9A-Fa-f.:]+$", ip or ""):
        return {"ok": False, "log": "плохой IP"}
    r = _run(["fail2ban-client", "set", "sshd", "unbanip", ip], timeout=15)
    return {"ok": r["ok"], "log": (r.get("log") or "")[:200]}

def ufw_port(action, port):
    if not shutil.which("ufw"):
        return {"ok": False, "log": "ufw не установлен"}
    if not re.match(r"^\d{1,5}(/(tcp|udp))?$", port or ""):
        return {"ok": False, "log": "порт: напр. 8080 или 8080/tcp"}
    if action == "deny":
        # нельзя закрыть порт самой панели и SSH — иначе пользователь запрёт себя
        num = port.split("/")[0]
        if num in (str(PORT), "80", "22"):
            return {"ok": False, "log": "нельзя закрыть порт панели или SSH — потеряете доступ"}
    r = _run(["ufw", "allow", port] if action == "allow" else
             ["ufw", "delete", "allow", port], timeout=15)
    return {"ok": r["ok"], "log": (r.get("log") or "")[:200]}

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
            "fail2ban": _fail2ban_state(),
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
            "watchdog": os.path.isfile("/etc/systemd/system.conf.d/watchdog.conf"),
            "zram": _zram_status(),
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
            if not b:
                return _run(["ufw", "--force", "disable"])
            r = engine("security", {"keys": "ufw"})
            global _ufw_sync_last
            _ufw_sync_last = 0
            _safe(ufw_autosync)      # сразу открыть docker-порты, не ждать тик
            return r
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
        if key == "watchdog":
            return _watchdog(b)
        if key == "zram":
            return engine("pi", {"keys": "zram"}) if b else _zram_off()
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
# --------------------------------------------------------------------------- #
#  SSH-приветствие (MOTD). Скрипт /etc/update-motd.d/20-nas-os ставит визард;
#  здесь только правим пользовательский текст и два флага. Текст выводится
#  через `cat` — не исполняется, поэтому экранировать нечего.
# --------------------------------------------------------------------------- #
MOTD_CONF   = "/etc/nas-wizard/motd.conf"
# Приветствие складывается из нескольких источников, и наш скрипт — лишь один из них.
# pam_motd выполняет ВСЁ из update-motd.d и печатает файлы из /etc/motd.d,
# а «Last login» добавляет уже sshd. Дадим по тумблеру на каждый.
MOTD_UNAME_SH   = "/etc/update-motd.d/10-uname"
MOTD_COCKPIT_LN = "/etc/motd.d/cockpit"
MOTD_COCKPIT_TARGET = "../../run/cockpit/issue"
MOTD_SSHD_CONF  = "/etc/ssh/sshd_config.d/99-nas-motd.conf"
MOTD_TXT    = "/etc/nas-wizard/motd.txt"
MOTD_SCRIPT = "/etc/update-motd.d/20-nas-os"
MOTD_MAX    = 4000

_MOTD_FLAGS = {"MOTD_LOGO": "show_logo", "MOTD_TEXT": "show_text", "MOTD_INFO": "show_info",
               "MOTD_UNAME": "show_uname", "MOTD_COCKPIT": "show_cockpit",
               "MOTD_LASTLOG": "show_lastlog"}

def motd_load():
    cfg = {"text": _read(MOTD_TXT), "installed": os.path.isfile(MOTD_SCRIPT)}
    for v in _MOTD_FLAGS.values():
        cfg[v] = True
    for line in _read(MOTD_CONF).splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in _MOTD_FLAGS:
            cfg[_MOTD_FLAGS[k]] = v.strip().strip('"').strip("'") == "1"
    # состояние на диске важнее записанного: файлы мог поменять кто-то ещё
    cfg["has_uname"] = os.path.isfile(MOTD_UNAME_SH)
    cfg["has_cockpit"] = os.path.isdir(os.path.dirname(MOTD_COCKPIT_LN))
    return cfg

def _motd_extras_apply(cfg):
    """Погасить/вернуть чужие куски приветствия. Зовётся из motd_save и при старте,
    чтобы настройка пережила переустановку (motd.conf лежит в бэкапе настроек)."""
    # 1) строка ядра: снимаем бит исполнения, pam_motd её пропустит
    if os.path.isfile(MOTD_UNAME_SH):
        try:
            os.chmod(MOTD_UNAME_SH, 0o755 if cfg.get("show_uname", True) else 0o644)
        except OSError:
            pass
    # 2) баннер Cockpit: симлинк в /etc/motd.d
    try:
        d = os.path.dirname(MOTD_COCKPIT_LN)
        if cfg.get("show_cockpit", True):
            if os.path.isdir(d) and not os.path.lexists(MOTD_COCKPIT_LN):
                os.symlink(MOTD_COCKPIT_TARGET, MOTD_COCKPIT_LN)
        elif os.path.lexists(MOTD_COCKPIT_LN):
            os.remove(MOTD_COCKPIT_LN)
    except OSError:
        pass
    # 3) «Last login» печатает sshd, не pam_motd
    try:
        want = not cfg.get("show_lastlog", True)
        have = os.path.isfile(MOTD_SSHD_CONF)
        if want and not have:
            os.makedirs(os.path.dirname(MOTD_SSHD_CONF), exist_ok=True)
            with open(MOTD_SSHD_CONF, "w") as f:
                f.write("# nas-wizard: строку «Last login» отключили в панели\nPrintLastLog no\n")
        elif not want and have:
            os.remove(MOTD_SSHD_CONF)
        else:
            return
        if _run(["sshd", "-t"], timeout=8)["ok"]:          # не перезагружать битый конфиг
            _run(["systemctl", "reload", "ssh"], timeout=10)
        elif have and not want:
            pass
        else:                                              # конфиг не принят — откатываем
            try: os.remove(MOTD_SSHD_CONF)
            except OSError: pass
    except OSError:
        pass

def motd_preview():
    if not os.path.isfile(MOTD_SCRIPT):
        return ""
    env = dict(os.environ); env["NO_COLOR"] = "1"   # предпросмотр без ANSI-кодов
    r = _run(["/bin/bash", MOTD_SCRIPT], timeout=15, env=env)
    return r["log"]

def motd_save(b):
    if not os.path.isfile(MOTD_SCRIPT):
        return {"ok": False, "log": "приветствие не установлено: nas-wizard.sh api motd"}
    text = b.get("text", "")
    if not isinstance(text, str) or len(text) > MOTD_MAX:
        return {"ok": False, "log": "слишком длинный текст (макс. %d символов)" % MOTD_MAX}
    try:
        with open(MOTD_TXT, "w") as f:
            f.write(text if text.endswith("\n") or not text else text + "\n")
        with open(MOTD_CONF, "w") as f:
            f.write("# nas-wizard: что показывать при входе по SSH\n")
            for key, name in _MOTD_FLAGS.items():
                f.write("%s=%d\n" % (key, 1 if b.get(name, True) else 0))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _motd_extras_apply(motd_load())
    return {"ok": True, "log": "сохранено", "preview": motd_preview()}

USB_IMPORT_CONF = "/etc/nas-wizard/usb-import.conf"
USB_IMPORT_SH   = "/usr/local/bin/nas-usb-import.sh"
USB_IMPORT_RULE = "/etc/udev/rules.d/98-nas-usb-import.rules"
_USB_DEFAULT = {"enabled": False, "dest": "/mnt/storage/imports",
                "subdir": "{label}-{date}-{time}", "notify": False, "eject": False,
                "media_only": False, "restrict": False, "allow": []}
_USB_SH = r'''#!/bin/bash
# nas-wizard: авто-импорт содержимого вставленного USB в заданную папку.
# Копирование only-forward: с флешки ничего не удаляется. Стейджинг в .incomplete
# с переименованием на успехе — прерванный импорт виден и не путается с готовым.
CONF=/etc/nas-wizard/usb-import.conf
[ -r "$CONF" ] || exit 0
. "$CONF"
[ "${IMPORT_ENABLED:-0}" = "1" ] || [ "${IMPORT_FORCE:-0}" = "1" ] || exit 0
dev="$1"; [ -b "$dev" ] || exit 0
LOG=/var/log/nas-usb-import.log
log(){ echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }
# journal=0: the panel monitor already journals import results from LOG,
# a second journal entry from the notify route would duplicate the desk plate
notify(){ [ "${IMPORT_NOTIFY:-0}" = "1" ] && [ -x /usr/local/bin/nas-notify.sh ] && /usr/local/bin/nas-notify.sh "$1" "$2" 0 "" "" 0 2>/dev/null || true; }
# At boot udev replays ACTION=add for every medium that is already plugged in
# (coldplug). Importing those is a duplicate of the import done when the card was
# first inserted, so auto-import must fire on hot-plug only. USEC_INITIALIZED is
# the monotonic time (µs since kernel start) at which udev first saw the device:
# coldplug lands in the first seconds of uptime, a hand-inserted card lands far
# later. Manual "import now" (IMPORT_FORCE=1) bypasses the guard.
COLDPLUG_US=${IMPORT_COLDPLUG_US:-120000000}
if [ "${IMPORT_FORCE:-0}" != "1" ]; then
  seen_us="$(udevadm info -q property -n "$dev" 2>/dev/null | sed -n 's/^USEC_INITIALIZED=//p' | head -1)"
  # no property (odd bridge, udevadm unavailable) → fall back to plain uptime
  case "$seen_us" in ''|*[!0-9]*) seen_us="$(awk '{printf "%d", $1*1000000}' /proc/uptime)" ;; esac
  if [ "$seen_us" -lt "$COLDPLUG_US" ]; then
    log "skip $dev: носитель был вставлен до загрузки — автоимпорт только на «горячую» вставку"
    exit 0
  fi
fi
# udev дёргает нас отдельно на КАЖДЫЙ раздел диска. Регистрируем задание, чтобы
# извлечение в конце не вырвало устройство из-под ещё копирующегося соседа.
# Через pgrep это делать нельзя: подоболочка скрипта имеет ту же командную
# строку, но другой PID, — сам себя увидишь и будешь ждать вечно.
pk="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1)"
JOBD="/run/nas-usb-import.jobs/${pk:-none}"
mkdir -p "$JOBD" 2>/dev/null && : > "$JOBD/$$" 2>/dev/null
trap 'rm -f "$JOBD/$$" 2>/dev/null' EXIT
# сколько ЖИВЫХ соседей копируют этот же диск (мёртвые записи подчищаем)
siblings(){
  local n=0 f b
  for f in "$JOBD"/*; do
    b="${f##*/}"
    [ "$b" = "*" ] && continue
    [ "$b" = "$$" ] && continue
    if [ -d "/proc/$b" ]; then n=$((n+1)); else rm -f "$f" 2>/dev/null; fi
  done
  echo "$n"
}
# белый список: если ограничение включено — автоимпорт ТОЛЬКО с разрешённых устройств
# (по VID:PID). Ручной «импорт сейчас» (IMPORT_FORCE=1) список игнорирует.
# UAS-мосты не дают ID_VENDOR_ID — падаем на ID_USB_VENDOR_ID. Регистр нормализуем:
# в конфиге VID:PID хранится строчными.
if [ "${IMPORT_FORCE:-0}" != "1" ] && [ "${IMPORT_RESTRICT:-0}" = "1" ]; then
  props="$(udevadm info -q property -n "$dev" 2>/dev/null)"
  prop(){ printf '%s\n' "$props" | sed -n "s/^$1=//p" | head -1; }
  vid="$(prop ID_VENDOR_ID)"; [ -n "$vid" ] || vid="$(prop ID_USB_VENDOR_ID)"
  pid="$(prop ID_MODEL_ID)";  [ -n "$pid" ] || pid="$(prop ID_USB_MODEL_ID)"
  did="$(printf '%s:%s' "$vid" "$pid" | tr 'A-Z' 'a-z')"
  if [ -z "$vid" ] || [ -z "$pid" ]; then
    log "skip $dev: не удалось определить VID:PID"; notify "USB пропущен" "Не удалось определить устройство"; exit 0
  fi
  case " $(printf '%s' "${IMPORT_ALLOW}" | tr 'A-Z' 'a-z') " in
    *" $did "*) : ;;
    *) log "skip $dev ($did): не в списке разрешённых устройств"; notify "USB пропущен" "Устройство $did не в списке автоимпорта"; exit 0 ;;
  esac
fi
label="$(blkid -o value -s LABEL "$dev" 2>/dev/null)"; [ -n "$label" ] || label="usb-$(basename "$dev")"
label="$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '_')"
mp="$(findmnt -n -o TARGET --source "$dev" 2>/dev/null | head -1)"
selfmount=0
if [ -z "$mp" ]; then
  mp="$(mktemp -d /run/nas-usb-import.XXXXXX)"
  if mount -o ro "$dev" "$mp" 2>>"$LOG"; then
    selfmount=1
  else
    # Гонка с автомонтированием: оба скрипта висят на одном udev-событии ADD.
    # findmnt выше сказал «не смонтирован», но пока мы шли к mount, automount
    # успел занять носитель — тогда не падаем, а читаем из его точки.
    rmdir "$mp" 2>/dev/null
    mp="$(findmnt -n -o TARGET --source "$dev" 2>/dev/null | head -1)"
    [ -n "$mp" ] || { log "import FAIL $dev: не удалось смонтировать"; exit 1; }
    log "носитель уже смонтирован автомонтированием — читаю из $mp"
  fi
fi
cleanup(){ [ "$selfmount" = "1" ] && { umount "$mp" 2>>"$LOG"; rmdir "$mp" 2>/dev/null; }; }
base="${IMPORT_DEST:-/mnt/storage/imports}"
# защита от импорта самого себя (напр. флешка примонтирована внутри приёмника)
case "$(readlink -f "$base")/" in "$(readlink -f "$mp")"/*) log "self-import guard: $mp внутри $base"; cleanup; exit 0;; esac
# раскладка подпапок: шаблон с токенами {label}/{date}/{time}/{year}/{month}/
# {month-name}/{day}/{hour}/{minute}/{datetime}. Легаси-ключи мапим на шаблоны.
# ВНИМАНИЕ: фигурные скобки в значении по умолчанию ломают разбор ${VAR:-...} —
# первая же '}' закрывает подстановку, а хвост приклеивается как текст. Поэтому
# дефолт ставим отдельной строкой. Проверяем на «задана ли» (+set), а не на
# «непуста»: пустой шаблон — это осознанный режим «без подпапки».
if [ -z "${IMPORT_SUBDIR+set}" ]; then
  tpl='{label}-{date}-{time}'
else
  tpl="$IMPORT_SUBDIR"
fi
case "$tpl" in
  dated) tpl='{label}-{date}-{time}';;
  label) tpl='{label}';;
  flat)  tpl='';;
esac
sub="$tpl"
if [ -n "$sub" ]; then
  sub="${sub//\{label\}/$label}"
  sub="${sub//\{datetime\}/$(date '+%Y%m%d-%H%M%S')}"
  sub="${sub//\{date\}/$(date '+%Y-%m-%d')}"
  sub="${sub//\{time\}/$(date '+%H-%M-%S')}"
  sub="${sub//\{year\}/$(date '+%Y')}"
  sub="${sub//\{month-name\}/$(LC_ALL=C date '+%B')}"
  sub="${sub//\{month\}/$(date '+%m')}"
  sub="${sub//\{day\}/$(date '+%d')}"
  sub="${sub//\{hour\}/$(date '+%H')}"
  sub="${sub//\{minute\}/$(date '+%M')}"
  # безопасность пути: убрать .., ведущие слэши, схлопнуть повторы, обрезать пробелы у сегментов.
  # Фигурные скобки вычищаем: после подстановки их оставляет только опечатка в
  # токене ({lable}) — пусть будет «lable», а не мусор в имени папки.
  sub="$(printf '%s' "$sub" | tr -d '{}' | sed 's#\.\.##g; s#^/*##; s#/*$##; s#/\{2,\}#/#g')"
fi
# фильтр «только фото/видео» (регистронезависимо)
filter=(); if [ "${IMPORT_MEDIA_ONLY:-0}" = "1" ]; then
  filter=(--include='*/')
  for e in jpg jpeg png gif heic heif webp tif tiff bmp dng raw arw cr2 cr3 nef orf rw2 raf srw \
           mp4 mov avi mkv m4v mts m2ts 3gp mpg mpeg wmv webm; do
    u="$(printf '%s' "$e" | tr a-z A-Z)"; filter+=(--include="*.$e" --include="*.$u")
  done
  filter+=(--exclude='*')
fi
# проверка свободного места (нужно + 5% запас)
need="$(du -sb "$mp" 2>/dev/null | cut -f1)"; avail="$(df -PB1 "$base" 2>/dev/null | awk 'NR==2{print $4}')"
if [ -n "$need" ] && [ -n "$avail" ] && [ "$avail" -lt "$((need + need/20 + 10485760))" ]; then
  log "no space: need=$need avail=$avail"; notify "USB-импорт: мало места" "«$label» не поместится в $base"; cleanup; exit 1
fi
# Стейджинг — ОДНА папка верхнего уровня. Раньше шаблон подставлялся прямо в имя
# (.incomplete-$$-{year}/{month}/...), и при вложенном шаблоне mv падал: каталога
# назначения ещё нет, а «.incomplete-123-2026/07/...» — это уже три уровня.
if [ -n "$sub" ]; then
  dest="$base/$sub"
  stage="$base/.incomplete-$$-$label"
else
  dest="$base"
  stage="$dest"
fi
mkdir -p "$stage" 2>>"$LOG"
log "import $dev ($label) -> $dest"
notify "USB-импорт начат" "Копирую «$label» → $dest"

# --- прогресс для панели -----------------------------------------------------
# rsync --info=progress2 обновляет строку через \r; гоним её в файл в /run.
# pid пишем, чтобы панель отличила «идёт» от «процесс убили».
PROGD=/run/nas-usb-import.progress
PROG="$PROGD/${dev##*/}"
START="$(date +%s)"
mkdir -p "$PROGD" 2>/dev/null
prog(){                       # $1=статус  $2=строка rsync
  { printf 'pid=%s\ndev=%s\nlabel=%s\ndest=%s\ntotal=%s\nstarted=%s\nstatus=%s\n' \
      "$$" "$dev" "$label" "$dest" "${need:-0}" "$START" "$1"
    [ "$1" != "running" ] && printf 'finished=%s\n' "$(date +%s)"
    printf 'line=%s\n' "$2"
  } > "$PROG.tmp" 2>/dev/null && mv -f "$PROG.tmp" "$PROG" 2>/dev/null
  return 0
}
prog running ""
trap 'rm -f "$JOBD/$$" "$PROG.tmp" 2>/dev/null' EXIT

# LC_ALL=C — чтобы разделитель тысяч был запятой и парсер не гадал по локали.
# --no-inc-recursive: сканирует всё заранее, зато процент честный, а не «от
# увиденного до сих пор».
LC_ALL=C rsync -a --info=progress2 --no-inc-recursive "${filter[@]}" "$mp"/ "$stage"/ 2>>"$LOG" \
  | tr '\r' '\n' \
  | while IFS= read -r pl; do
      case "$pl" in *%*) prog running "$pl" ;; esac
    done
rc=${PIPESTATUS[0]}
own="$(getent passwd 1000 | cut -d: -f1)"; [ -n "$own" ] && chown -R "$own:$own" "$stage" 2>/dev/null
if [ "$rc" = 0 ] || [ "$rc" = 23 ] || [ "$rc" = 24 ]; then
  if [ "$stage" != "$dest" ]; then
    mkdir -p "$(dirname "$dest")" 2>>"$LOG"
    # не сливаться с уже существующей папкой: mv положил бы стейджинг ВНУТРЬ неё
    d="$dest"; n=2
    while [ -e "$d" ]; do d="$dest ($n)"; n=$((n+1)); done
    if mv "$stage" "$d" 2>>"$LOG"; then
      dest="$d"
    else
      log "import FAIL $dev: не перенести $stage -> $d (данные остались в стейджинге)"
      notify "USB-импорт: ошибка" "Не удалось разложить «$label» по папкам"
      prog fail "mv"; cleanup; exit 1
    fi
  fi
  log "import OK -> $dest"; notify "USB-импорт готов" "«$label» скопирован в $dest"
  # сохранить последнюю строку rsync: иначе в «готово» пропадут байты и итог
  prog done "$(sed -n 's/^line=//p' "$PROG" 2>/dev/null | tail -1)"
else
  log "import FAIL $dev rc=$rc (частичное в $stage)"; notify "USB-импорт: ошибка" "Не удалось скопировать «$label» (rc=$rc)"
  prog fail "rc=$rc"
fi
cleanup
if [ "${IMPORT_EJECT:-0}" = "1" ] && [ -n "$pk" ]; then
  # дождаться соседних разделов того же диска (потолок 2 часа)
  waited=0
  while [ "$(siblings)" -gt 0 ] && [ "$waited" -lt 7200 ]; do
    [ "$waited" = 0 ] && log "eject ждёт: копируются другие разделы /dev/$pk"
    sleep 1; waited=$((waited+1))
  done
  # автомонт мог поднять разделы в /media — пока они смонтированы, power-off
  # отказывает («drive in use») и остаётся только грубый eject с висящим монтом
  for part in $(lsblk -lnpo NAME "/dev/$pk" 2>/dev/null); do
    for mp in $(findmnt -rno TARGET -S "$part" 2>/dev/null); do
      umount "$mp" 2>>"$LOG" || udisksctl unmount -b "$part" >>"$LOG" 2>&1 || true
    done
  done
  sync
  # Сначала мягко извлекаем НОСИТЕЛЬ, и только если не вышло — обесточиваем УСТРОЙСТВО.
  # У картридера power-off убирает с шины сам ридер: вставлять карту потом некуда,
  # пока не передёрнешь кабель. eject же выбрасывает карту, ридер остаётся живым и
  # следующая вставка снова запускает импорт. Для флешки eject останавливает
  # устройство — данные сброшены, вынимать безопасно.
  if eject "/dev/$pk" >>"$LOG" 2>&1; then
    log "eject media /dev/$pk"
  elif udisksctl power-off -b "/dev/$pk" >>"$LOG" 2>&1; then
    log "power-off /dev/$pk"
  else
    log "eject /dev/$pk не удался"
  fi
  touch /run/nas-web-refresh 2>/dev/null   # диск исчез — разбудить панель сразу
fi
'''
# матчим по ID_USB_DRIVER (usb-storage/uas), а не ID_BUS==usb — иначе USB-SATA
# мосты (ID_BUS=ata) не срабатывают.
# --unit даёт читаемое имя вместо run-p564-i565.service (иначе «упала служба
# run-p…» ничего не говорит), --collect убирает упавший юнит сразу: без него он
# висит в failed и следующая вставка того же носителя не смогла бы стартовать.
_USB_RULE = ('ACTION=="add", SUBSYSTEM=="block", ENV{ID_USB_DRIVER}=="?*", '
            'ENV{ID_FS_USAGE}=="filesystem", '
            'RUN+="/usr/bin/systemd-run --no-block --collect '
            '--unit=nas-usb-import-%k /usr/local/bin/nas-usb-import.sh $devnode"\n')

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
        elif k == "IMPORT_MEDIA_ONLY": cfg["media_only"] = v == "1"
        elif k == "IMPORT_RESTRICT": cfg["restrict"] = v == "1"
        elif k == "IMPORT_ALLOW":  cfg["allow"] = [x.lower() for x in v.split() if x]
    cfg["installed"] = os.path.isfile(USB_IMPORT_RULE)
    cfg["rsync"] = bool(shutil.which("rsync"))
    return cfg

USB_PROG_DIR = "/run/nas-usb-import.progress"
# «  1,234,567  45%   12.34MB/s    0:00:12» — формат rsync --info=progress2 при LC_ALL=C
_RSYNC_PROG = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)")

def usb_import_progress():
    """Активные и недавно завершённые задания импорта. Файлы живут в /run,
    поэтому перезагрузка чистит их сама."""
    jobs = []
    now = time.time()
    try:
        names = sorted(os.listdir(USB_PROG_DIR))
    except OSError:
        return {"jobs": []}
    for n in names:
        if n.endswith(".tmp"):
            continue
        meta = {}
        for line in _read(os.path.join(USB_PROG_DIR, n)).splitlines():
            k, _, v = line.partition("=")
            meta[k] = v
        st = meta.get("status", "running")
        pid = meta.get("pid", "")
        # процесс убили (или ребут udev-задания) — иначе задание висело бы «идёт» вечно
        if st == "running" and pid and not os.path.isdir("/proc/" + pid):
            st = "aborted"
        fin = int(meta.get("finished") or 0)
        if st != "running" and fin and now - fin > 600:
            continue
        job = {"dev": meta.get("dev"), "label": meta.get("label"), "dest": meta.get("dest"),
               "status": st, "started": int(meta.get("started") or 0),
               "total": int(meta.get("total") or 0) or None,
               "percent": 100 if st == "done" else None,
               "bytes": None, "speed": None, "eta": None}
        m = _RSYNC_PROG.match(meta.get("line", ""))
        if m:
            job["bytes"] = int(m.group(1).replace(",", ""))
            if st != "done":
                job["percent"] = int(m.group(2))
            job["speed"], job["eta"] = m.group(3), m.group(4)
        jobs.append(job)
    return {"jobs": jobs}

def _view_path(src):
    h = hashlib.md5(src.encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(THUMBS_DIR, h[:2], h + ".view.jpg")

def view_needed(src):
    """Нужна ли перекодировка для показа: браузер не умеет формат ИЛИ файл огромный."""
    e = _ext(src)
    if e in _VIEW_CONV:
        return True
    if e in _VIEW_BIG_EXT:
        try:
            return os.path.getsize(src) > VIEW_BIG_BYTES
        except OSError:
            return False
    return False

def gen_view(src):
    """Крупный JPEG для просмотрщика: HEIC/HEIF и TIFF браузеры не рисуют,
    а 17-мегабайтные снимки с камеры незачем гнать целиком.
    Кэшируется рядом с миниатюрами, чистится тем же GC."""
    if not os.path.isfile(src) or not view_needed(src):
        return None
    vp = _view_path(src)
    if os.path.isfile(vp) and _thumb_fresh(src, vp):
        return vp
    if not shutil.which("ffmpeg"):
        return None
    try:
        os.makedirs(os.path.dirname(vp), exist_ok=True)
    except OSError:
        return None
    uniq = "%d.%s" % (os.getpid(), secrets.token_hex(4))
    tmp = vp + "." + uniq + ".tmp.jpg"
    heif_tmp = vp + "." + uniq + ".heif.jpg"
    ok = False
    with _view_sem:
        try:
            ff_in = src
            if _ext(src) in _HEIF_EXT:
                if not _heif_decode(src, heif_tmp):
                    raise RuntimeError("heif-convert не смог")
                ff_in = heif_tmp
            scale = ("scale='min(%d,iw)':'min(%d,ih)'"
                     ":force_original_aspect_ratio=decrease" % (VIEW_PX, VIEW_PX))
            r = subprocess.run(["ffmpeg","-y","-v","error","-i",ff_in,"-vf",scale,
                                "-frames:v","1","-q:v","3",tmp],
                               capture_output=True, timeout=90)
            if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, vp); ok = True
        except Exception:
            ok = False
        finally:
            for leftover in glob.glob(vp + "." + uniq + "*"):
                try: os.remove(leftover)
                except OSError: pass
    return vp if ok else None

_IMPORT_ROOTS_OK = ("/mnt/", "/media/", "/srv/", "/home/")

def usb_import_gc(stale_hours, keep_days):
    """Прибрать приёмник импорта: брошенные стейджинги .incomplete-* и, если
    попросили, старые импорты. Работаем ТОЛЬКО внутри папки назначения и только
    если она под разрешённым корнем — иначе рекурсивное удаление слишком опасно."""
    dest = os.path.realpath(usb_import_load().get("dest") or "")
    if not dest.startswith(_IMPORT_ROOTS_OK) or dest.count("/") < 2 or not os.path.isdir(dest):
        return 0
    now = time.time()
    removed = 0
    try:
        names = os.listdir(dest)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(dest, name)
        if not os.path.isdir(p) or os.path.islink(p):
            continue
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        stale = name.startswith(".incomplete-")
        # брошенный стейджинг: процесс импорта давно умер, папка мусорная
        if stale and stale_hours > 0 and age > stale_hours * 3600:
            try:
                shutil.rmtree(p); removed += 1
                log_event("info", "USB-импорт: убран брошенный стейджинг", name, "ok",
                          kind="disk", desk=False)
            except OSError:
                pass
        elif not stale and keep_days > 0 and age > keep_days * 86400:
            try:
                shutil.rmtree(p); removed += 1
            except OSError:
                pass
    return removed

def _thumbs_warm_bg(dest):
    """Прогреть превью импортированной папки в фоне. os.nice — чтобы карточка
    диска и панель не тормозили: миниатюра снимка 26 МП стоит ~0.8 с."""
    try:
        os.nice(15)
    except OSError:
        pass
    try:
        n = thumbs_sweep([dest])
        if n:
            log_event("info", "Превью подготовлены", "%s: %d шт." % (dest, n), "ok",
                      kind="files", desk=False)
    except Exception:
        pass

def usb_ops_sync():
    """Занести завершённые задания импорта в историю операций. Зовётся из
    monitor_loop, поэтому история пополняется и с закрытой панелью. Новый
    (не дублирующий) успешный импорт заодно запускает прогрев превью."""
    warm = load_maintenance().get("import_warm_thumbs", True)
    for j in usb_import_progress()["jobs"]:
        if j["status"] == "running":
            continue
        bits = [j.get("label") or j.get("dev") or ""]
        if j.get("bytes") is not None:
            bits.append(fmt_bytes(j["bytes"]) + (" из " + fmt_bytes(j["total"]) if j.get("total") else ""))
        if j.get("dest"):
            bits.append(j["dest"])
        r = ops_hist_add({"uid": "usb:%s:%s" % (j["dev"], j["started"]),
                          "state": "done" if j["status"] == "done" else "err",
                          "ts": j.get("finished") or j.get("started") or int(time.time()),
                          "title": "Импорт с USB", "label": " · ".join(x for x in bits if x)})
        # прогрев ровно один раз на импорт: dup означает, что мы его уже видели
        if warm and r.get("ok") and not r.get("dup") and j["status"] == "done" \
           and j.get("dest") and os.path.isdir(j["dest"]):
            threading.Thread(target=_thumbs_warm_bg, args=(j["dest"],), daemon=True).start()

def _usb_sh_sync():
    """Перезаписать хелпер и udev-правило, если они разошлись с кодом. Раньше и то
    и другое обновлялось только при сохранении настроек, поэтому после обновления
    панели на диске оставалась старая версия со старыми багами."""
    changed = []
    # _read() strips the trailing newline — compare stripped, or every service
    # start "updates" an identical helper and spams the event log
    if os.path.isfile(USB_IMPORT_SH) and _read(USB_IMPORT_SH) != _USB_SH.strip():
        with open(USB_IMPORT_SH, "w") as f:
            f.write(_USB_SH)
        os.chmod(USB_IMPORT_SH, 0o755)
        changed.append("хелпер")
    # правило трогаем только если оно уже стоит: его отсутствие = импорт выключен
    if os.path.isfile(USB_IMPORT_RULE) and _read(USB_IMPORT_RULE) != _USB_RULE.strip():
        with open(USB_IMPORT_RULE, "w") as f:
            f.write(_USB_RULE)
        _run(["udevadm", "control", "--reload"], timeout=15)
        changed.append("udev-правило")
    if changed:
        log_event("info", "USB-импорт: %s обновлён до текущей версии" % " + ".join(changed),
                  "", "ok", kind="disk", desk=False)

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
    # раскладка: легаси-ключ ИЛИ шаблон с токенами (разрешаем буквы/цифры/пробел/-_.{}/ )
    subdir = str(cfg.get("subdir", "{label}-{date}-{time}")).strip()
    if subdir not in ("dated", "label", "flat"):
        subdir = re.sub(r"[^\w \-.{}/А-Яа-яЁё]", "", subdir).replace("..", "").strip("/")[:120]
        if not subdir:
            subdir = "flat"
    try:
        os.makedirs("/etc/nas-wizard", exist_ok=True)
        with open(USB_IMPORT_CONF, "w") as f:
            f.write("IMPORT_ENABLED=%d\n" % (1 if cfg.get("enabled") else 0))
            # ЗНАЧЕНИЯ В КАВЫЧКАХ: конфиг сорсится шеллом, а шаблон и путь могут
            # содержать пробелы. Без кавычек bash видит «VAR=x cmd args» и переменная
            # в шелл не попадает вовсе — молча включался дефолтный шаблон.
            f.write('IMPORT_DEST="%s"\n' % dest)
            f.write('IMPORT_SUBDIR="%s"\n' % subdir)
            f.write("IMPORT_NOTIFY=%d\n" % (1 if cfg.get("notify") else 0))
            f.write("IMPORT_EJECT=%d\n" % (1 if cfg.get("eject") else 0))
            f.write("IMPORT_MEDIA_ONLY=%d\n" % (1 if cfg.get("media_only") else 0))
            # белый список устройств (VID:PID hex, через пробел) + флаг ограничения
            allow = [str(x).lower() for x in (cfg.get("allow") or [])
                     if re.match(r"^[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}$", str(x))]
            f.write("IMPORT_RESTRICT=%d\n" % (1 if cfg.get("restrict") else 0))
            f.write('IMPORT_ALLOW="%s"\n' % " ".join(sorted(set(allow))))
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

def _udev_props(dev):
    try:
        out = subprocess.run(["udevadm", "info", "-q", "property", "-n", dev],
                             capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    props = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k:
            props[k] = v.strip()
    return props

def usb_devices():
    """USB-накопители сейчас: VID:PID/модель/серийник — для белого списка автоимпорта."""
    cfg = usb_import_load()
    allow = set(cfg.get("allow", []))
    out = []
    for d in _lsblk():
        if d.get("type") != "disk":
            continue
        dev = d.get("path", "")
        if not dev:
            continue
        props = _udev_props(dev)
        # UAS-мосты отдают ID_BUS=ata (и lsblk TRAN может быть не "usb") — ловим по
        # ID_USB_DRIVER, он выставлен и для usb-storage, и для uas.
        if not props.get("ID_USB_DRIVER") and d.get("tran") != "usb":
            continue
        vid = props.get("ID_VENDOR_ID") or props.get("ID_USB_VENDOR_ID") or ""
        pid = props.get("ID_MODEL_ID") or props.get("ID_USB_MODEL_ID") or ""
        umodel = (props.get("ID_MODEL") or "").replace("_", " ")
        devid = ("%s:%s" % (vid, pid)).lower() if (vid and pid) else ""
        out.append({"dev": dev, "vid": vid, "pid": pid, "id": devid,
                    "serial": props.get("ID_SERIAL_SHORT", ""),
                    "model": (d.get("model") or umodel or "").strip(), "size": d.get("size"),
                    "label": d.get("label"), "removable": d.get("rm") in (True, "1", 1),
                    "allowed": bool(devid) and devid in allow})
    return {"devices": out, "restrict": cfg.get("restrict", False), "allow": sorted(allow)}

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
class _Server(ThreadingHTTPServer):
    daemon_threads = True          # рабочие потоки не держат остановку службы
    def handle_error(self, request, client_address):
        # браузер оборвал загрузку (закрыл вкладку, отменил картинку) — это норма,
        # а не сбой: полный трейсбек в journal только зашумляет. Всё прочее — как было.
        e = sys.exc_info()[1]
        if isinstance(e, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# --------------------------------------------------------------------------- #
#  Local touch screen — kiosk dashboard on the box itself (cage + chromium)
#
#  The panel is served at /screen and talks to /api/screen/*. Both are reachable
#  WITHOUT a session, but ONLY from loopback: the kiosk browser runs on this very
#  box, so asking for the panel password on a screen you can physically touch buys
#  nothing (and a token in the URL would not stop a local process either). From the
#  LAN the same paths still need a normal login, exactly like the rest of /api.
#  Same loopback-only trick as /api/agent/notify.
# --------------------------------------------------------------------------- #
SCREEN_FILE = os.path.join(NAS_CONFIG, "screen.json")
# tiles reused from Glance: same values, same colours, already translated
SCREEN_TILES = ("cpu", "load", "cputemp", "ram", "uptime", "pool", "rootfs",
                "netspeed", "docker", "updates", "disktemp", "snapraid", "inet")
_SCR = {"touch": time.time(), "last": None, "spd": None, "spd_run": False}


def load_screen():
    d = _json_load_strict(SCREEN_FILE, {})

    def _i(k, dflt, lo, hi):
        try:
            return max(lo, min(hi, int(d.get(k, dflt))))
        except (TypeError, ValueError):
            return dflt

    def _hhmm(k, dflt):
        v = str(d.get(k) or dflt)
        return v if re.match(r"^\d{1,2}:\d{2}$", v) else dflt

    return {"enabled": d.get("enabled") is not False,
            "bright": _i("bright", 200, 1, 255),
            "night": bool(d.get("night", True)),
            "night_from": _hhmm("night_from", "23:00"),
            "night_to": _hhmm("night_to", "07:00"),
            "night_bright": _i("night_bright", 0, 0, 255),
            "idle_min": _i("idle_min", 0, 0, 240),      # 0 = не гасить по простою
            "actions": d.get("actions") is not False,
            "lang": "ru" if d.get("lang") == "ru" else "en"}


def save_screen(d):
    cur = load_screen()
    if isinstance(d, dict):
        cur.update({k: v for k, v in d.items() if k in cur})
    _json_save(SCREEN_FILE, cur, indent=2)
    _safe(lambda: _screen_apply(force=True))
    return load_screen()


def _bl_dir():
    """First backlight device (the DSI panel), '' if the box has no screen."""
    try:
        for n in sorted(os.listdir("/sys/class/backlight")):
            return "/sys/class/backlight/" + n
    except OSError:
        return ""
    return ""


def screen_bright_set(v):
    d = _bl_dir()
    if not d:
        return False
    try:
        mx = int((_read(d + "/max_brightness") or "255").strip() or 255)
    except ValueError:
        mx = 255
    try:
        with open(d + "/brightness", "w") as f:
            f.write(str(max(0, min(mx, int(v)))))
        return True
    except (OSError, ValueError):
        return False


def _in_night(cfg, now=None):
    if not cfg["night"]:
        return False
    t = time.localtime(now or time.time())
    cur = t.tm_hour * 60 + t.tm_min

    def m(x):
        h, mm = x.split(":")
        return int(h) * 60 + int(mm)
    try:
        a, b = m(cfg["night_from"]), m(cfg["night_to"])
    except ValueError:
        return False
    # окно почти всегда через полночь (23:00 -> 07:00), поэтому не «a <= cur < b»
    return (a <= cur < b) if a < b else (cur >= a or cur < b)


def _screen_apply(force=False):
    """Brightness = f(night window, idle, alarm). Alarm wins over everything:
    a dead disk at 03:00 must light the screen up, that is the whole point."""
    cfg = load_screen()
    if not cfg["enabled"] or not _bl_dir():
        return
    now = time.time()
    hp = _safe(health_report, {}) or {}
    if hp.get("overall") == "bad":
        want = cfg["bright"]                       # тревога — жечь экран
    elif now - _SCR["touch"] < 60:
        want = cfg["bright"]                       # минуту после касания светим всегда
    elif _in_night(cfg):
        want = cfg["night_bright"]
    elif cfg["idle_min"] and now - _SCR["touch"] > cfg["idle_min"] * 60:
        want = 0
    else:
        want = cfg["bright"]
    if force or want != _SCR["last"]:
        if screen_bright_set(want):
            _SCR["last"] = want


def _screen_tick():
    _safe(lambda: _screen_apply())


def screen_payload(lang=""):
    # язык экрана задаётся в screen.json, а НЕ браузером киоска: i18n.js по умолчанию
    # ставит NAS_LANG=en, и без этого сервер слал бы английские подписи под русскую
    # разметку страницы — на экране получалась каша из двух языков
    cfg0 = load_screen()
    lang = lang or cfg0["lang"]
    en = (lang == "en")
    st = _safe(stats, {}) or {}
    # _glance_tile отдаёт value/unit/state/raw, но НЕ label — его glance_payload
    # подмешивает из каталога; здесь делаем то же самое
    labels = {t[0]: (t[2] if en else t[1]) for t in glance_catalog()}
    tiles = {}
    for tid in SCREEN_TILES:
        d = _safe(lambda t=tid: _glance_tile(t, en))
        if not d:
            continue
        d = dict(d, label=labels.get(tid, tid))
        if tid in GLANCE_SPARKS:
            sp = _safe(lambda t=tid: _gl_spark(GLANCE_SPARKS[t]))
            if sp:
                d["spark"] = sp
        tiles[tid] = d
    hp = _safe(health_report, {}) or {}
    problems = [{"name": c.get("name"), "value": c.get("value"),
                 "lvl": c.get("lvl"), "hint": c.get("hint") or ""}
                for c in (hp.get("checks") or []) if c.get("lvl") in ("bad", "warn")]
    ev = _safe(lambda: events_list(0, 8), {}) or {}
    events = list(reversed(ev.get("events") or []))[:5]
    bks = []
    for p in (_safe(nb_profiles_public, []) or []):
        h = _safe(lambda pid=p["id"]: nb_history(pid), []) or []   # newest first
        last = h[0] if h else {}
        bks.append({"id": p["id"], "name": p["name"],
                    "running": bool(p.get("running")), "queued": bool(p.get("queued")),
                    "configured": bool(p.get("configured")),
                    "last_ts": int(last.get("ts") or 0),
                    "last": last.get("result") or ""})
    av = _safe(lambda: avail_bars(24, 96), {}) or {}   # 2=up 1=local 0=off -1=нет данных
    # обои и их обработка — ТЕ ЖЕ, что на рабочем столе: экран читает desktop.json,
    # поэтому смена обоев/затемнения в браузере доезжает до панели сама (wpVer в URL)
    ds = _safe(load_settings, {}) or {}
    look = {"wpVer": ds.get("wpVer") or 0,
            "fxDim": ds.get("fxDim", 44), "fxBlur": ds.get("fxBlur", 27),
            "fxNoise": ds.get("fxNoise", 100),
            "theme": "light" if ds.get("theme") == "light" else "dark"}
    cfg = load_screen()
    host = st.get("host") or socket.gethostname()
    return {"host": host, "mdns": host + ".local", "ip": st.get("ip") or "",
            "iface": st.get("iface") or "", "net": st.get("net") or {"rx": 0, "tx": 0},
            "uptime": st.get("uptime") or 0, "cpu": st.get("cpu"),
            "temp": st.get("temp"), "load": st.get("load") or [],
            "mem": st.get("mem") or {}, "pool": st.get("disk_pool"),
            "root": st.get("disk_root"), "overall": hp.get("overall") or "ok",
            "tiles": tiles, "problems": problems, "events": events, "backups": bks,
            "avail": {"bars": av.get("bars") or [], "pct": av.get("pct")}, "look": look,
            "speed": _SCR["spd"], "speed_running": bool(_SCR["spd_run"]),
            "actions": cfg["actions"], "lang": cfg["lang"], "ts": int(time.time())}


def screen_action(b):
    """Actions from the local screen. 'touch' is not an action — it is the wake
    signal, so it works even when the action buttons are switched off."""
    a = str(b.get("a") or "")
    if a == "touch":
        _SCR["touch"] = time.time()
        _safe(lambda: _screen_apply(force=True))
        return {"ok": True}
    cfg = load_screen()
    if not cfg["actions"]:
        return {"ok": False, "log": "действия с экрана выключены"}
    if a == "backup":
        return nb_run_bg(_nb_bpid(b))
    if a == "speed":
        if _SCR["spd_run"]:
            return {"ok": True, "running": True}

        def go():
            _SCR["spd_run"] = True
            try:
                r = _safe(net_speedtest, {}) or {}
                r["ts"] = int(time.time())
                _SCR["spd"] = r
            finally:
                _SCR["spd_run"] = False
        # спидтест блокирует на 10-18 с — экран не должен висеть на fetch
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "running": True}
    if a in ("reboot", "poweroff"):
        log_event("screen_power", "С экрана: " + ("перезагрузка" if a == "reboot" else "выключение"),
                  "запрошено кнопкой на локальном экране", "warn", "system")
        return power(a)
    return {"ok": False, "log": "неизвестное действие"}


class H(BaseHTTPRequestHandler):
    server_version = "nas-web"
    # Таймаут сокета: без него зависшее/медленное соединение (slowloris) держит
    # поток вечно. Пул потоков не ограничен, так что вечные потоки = падение.
    timeout = 30
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
        # сервер слушает 0.0.0.0 напрямую (без доверенного прокси), поэтому берём
        # реальный адрес сокета, а НЕ подделываемый клиентом X-Forwarded-For
        try:
            return self.client_address[0]
        except (AttributeError, IndexError):
            return ""

    def _authed(self):
        return session_valid(self._cookie_token())

    def _local(self):
        """Запрос пришёл с самого бокса (киоск-браузер локального экрана)."""
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

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
            # антибрутфорс под локом: гейтим ДО проверки пароля, чтобы параллельные
            # запросы не проскочили пачкой (ThreadingHTTPServer)
            with _login_lock:
                now = time.time()
                if now - _login_fail["t"] > 60:           # окно попыток — минута
                    _login_fail["n"] = 0
                blocked = _login_fail["n"] >= 8
            if blocked:
                time.sleep(1.0)
                self._json({"ok": False, "log": "слишком много попыток, подождите минуту"}, 429)
            elif auth_configured() and auth_check_password(b.get("password", "")):
                with _login_lock:
                    _login_fail["n"] = 0
                ip = self._client_ip()
                if ip and ip not in _known_ips():         # вход с нового адреса
                    _remember_ip(ip)
                    threading.Thread(target=mon_notify, args=("panel_new:" + ip,
                        "NAS: вход в панель с нового адреса", "Успешный вход с %s" % ip, "panel_new"),
                        daemon=True).start()
                self._json({"ok": True}, cookie=self._session_cookie(session_new()))
            else:
                with _login_lock:
                    _login_fail["n"] += 1; _login_fail["t"] = time.time(); n = _login_fail["n"]
                if n >= load_monitor().get("events", {}).get("panel_fail", {}).get("threshold", 5):
                    threading.Thread(target=mon_notify, args=("panel_fail",
                        "NAS: подбор пароля к панели", "%d неудачных попыток входа (последняя с %s)"
                        % (n, self._client_ip() or "?"), "panel_fail"), daemon=True).start()
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
        # сторож: убить зависший движок (напр. блок на мёртвом mount), чтобы не течь потоком/сокетом
        deadline = threading.Timer(1800, lambda: p.poll() is None and p.kill())
        deadline.daemon = True; deadline.start()
        try:
            for line in iter(p.stdout.readline, ""):
                try:
                    self.wfile.write(line.encode()); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    p.kill(); break
            p.wait()
        finally:
            deadline.cancel()
            try: p.stdout.close()
            except OSError: pass
        try:
            self.wfile.write(("__EXIT__%d\n" % (p.returncode if p.returncode is not None else -1)).encode()); self.wfile.flush()
        except OSError:
            pass
        if not dry:
            try:
                ok = p.returncode == 0
                log_event("action", "Мастер: %s%s" % (action, "" if ok else " — ошибка"),
                          "", "ok" if ok else "warn", kind="action", desk=False)
            except Exception:
                pass

    def _stream_cmd(self, cmd, env=None, timeout=1800):
        """Стримить stdout произвольной команды построчно с маркером __EXIT__ (как _stream_engine)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            p = subprocess.Popen(cmd, env=env or _C_ENV, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self.wfile.write(("ошибка запуска: %s\n__EXIT__1\n" % e).encode()); return
        deadline = threading.Timer(timeout, lambda: p.poll() is None and p.kill())
        deadline.daemon = True; deadline.start()
        try:
            for line in iter(p.stdout.readline, ""):
                try:
                    self.wfile.write(line.encode()); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    p.kill(); break
            p.wait()
        finally:
            deadline.cancel()
            try: p.stdout.close()
            except OSError: pass
        try:
            self.wfile.write(("__EXIT__%d\n" % (p.returncode if p.returncode is not None else -1)).encode()); self.wfile.flush()
        except OSError:
            pass

    def _body(self):
        # Потолок ДО чтения: иначе Content-Length: 2000000000 (даже до авторизации,
        # на /api/auth/login) заставил бы прочитать 2 ГБ в один поток и убить службу
        # по OOM на Pi. Большие бинарные загрузки идут через потоковый _upload_raw.
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return {}
        if n <= 0 or n > BODY_MAX:
            return {}
        raw = self.rfile.read(n)
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
                 ".webp": "image/webp", ".woff2": "font/woff2"}.get(
                     os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # HTML — НЕ кэшировать вовсе (мобильные браузеры при no-cache без валидатора
        # всё равно показывали старую оболочку); JS/CSS ревалидировать.
        ext = os.path.splitext(full)[1]
        if ext == ".html":
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        elif ext in (".js", ".css"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        elif ext == ".woff2":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
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

    def _send_icon(self, url):
        fp = fetch_icon(url)
        if not fp:
            self.send_error(404); return
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404); return
        ctype = mimetypes.guess_type(fp)[0] or "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # URL-ключ стабилен → можно смело держать в кэше браузера надолго
        self.send_header("Cache-Control", "public, max-age=604800")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
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
        try:
            aidx = max(0, int((q.get("a") or ["0"])[0]))   # выбранная аудиодорожка (0:a:aidx)
        except ValueError:
            aidx = 0
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
        # маппинг: видео + выбранная аудиодорожка ('?' — не падать, если её нет)
        maps = ["-map", "0:v:0?", "-map", "0:a:%d?" % aidx]
        cmd += ["-i", src] + maps + vargs + ["-c:a", "aac", "-b:a", "128k", "-ac", "2",
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

    def _extract_sub(self):
        """Извлечь встроенную ТЕКСТОВУЮ дорожку субтитров (0:s:idx) как WebVTT."""
        q = parse_qs(urlparse(self.path).query)
        src = os.path.realpath((q.get("path") or [""])[0])
        try:
            idx = max(0, int((q.get("idx") or ["0"])[0]))
        except ValueError:
            idx = 0
        if not os.path.isfile(src) or not shutil.which("ffmpeg"):
            self.send_error(404); return
        try:
            pr = subprocess.run(["ffmpeg", "-v", "error", "-i", src,
                                 "-map", "0:s:%d" % idx, "-f", "webvtt", "pipe:1"],
                                capture_output=True, timeout=90)
            data = pr.stdout or b""
        except Exception:
            self.send_error(500); return
        if not data.strip():
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _upload_raw(self):
        """Потоковая бинарная загрузка (без base64 — не роняет вкладку на больших файлах).
        rel — путь подпапок внутри назначения (загрузка папки целиком)."""
        q = parse_qs(urlparse(self.path).query)
        d, err = _fs_guard((q.get("path") or [""])[0])   # не загружать в системные деревья/движок
        if err:
            return {"ok": False, "log": err}
        name = os.path.basename(((q.get("name") or [""])[0]).strip())
        if not os.path.isdir(d):
            return {"ok": False, "log": "не каталог назначения"}
        if not name:
            return {"ok": False, "log": "нет имени файла"}
        # подпапки при загрузке папки. Каждый сегмент проверяем отдельно: «..» и
        # абсолютные пути сюда не пролезут, symlink наружу тоже (сверяем realpath).
        rel = ((q.get("rel") or [""])[0]).strip().replace("\\", "/")
        if rel:
            segs = [x for x in rel.split("/") if x not in ("", ".")]
            if len(segs) > 32 or any(x == ".." or "/" in x or "\0" in x for x in segs):
                return {"ok": False, "log": "недопустимый путь"}
            sub = os.path.realpath(os.path.join(d, *segs))
            if sub != d and not sub.startswith(d + os.sep):
                return {"ok": False, "log": "путь вне каталога назначения"}
            try:
                os.makedirs(sub, exist_ok=True)
            except OSError as e:
                return {"ok": False, "log": str(e)}
            _chown_user(sub, stop=os.path.realpath((q.get("path") or ["/"])[0]))
            d = sub
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
            try:
                os.remove(dest)
            except OSError:
                pass
            return {"ok": False, "log": str(e)}
        if got < n:                      # обрыв/отмена — не оставлять обрезанный файл
            try:
                os.remove(dest)
            except OSError:
                pass
            return {"ok": False, "log": "загрузка прервана"}
        _chown_user(dest)
        if thumb_kind(name):
            threading.Thread(target=gen_thumb, args=(dest,), daemon=True).start()
        return {"ok": True, "path": dest, "size": got}

    def _send_zip(self, items, name):
        import zipfile, tempfile
        items = [os.path.realpath(i) for i in items if i]
        if not items:
            self.send_error(400); return
        name = re.sub(r'[\r\n"\\/]', "_", name or "").strip() or "archive.zip"
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
        sock.settimeout(None)      # WS-терминал живёт долго и простаивает между нажатиями —
                                   # общий timeout=30 (анти-slowloris) рвал бы его каждые 30с
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
                    # initgroups до setuid: иначе shell без вспомогательных групп (video,
                    # docker, gpio…) и падает vcgencmd/docker без sudo. setgid ПОСЛЕ.
                    try:
                        os.initgroups(TARGET_USER, u.pw_gid)
                    except OSError:
                        pass
                    os.setgid(u.pw_gid); os.setuid(u.pw_uid)
                    os.environ.update(HOME=u.pw_dir, USER=TARGET_USER, LOGNAME=TARGET_USER)
            except (KeyError, OSError):
                pass
            os.chdir(os.environ.get("HOME", "/"))
            os.execvp("bash", ["bash", "-l"])
            os._exit(1)
        try:
            while True:
                r, _, _ = select.select([master, sock], [], [], 30)
                if not r:
                    # простой — не рвём соединение, а шлём WS-ping: это keepalive
                    # (терминал не отваливается) и заодно детект мёртвого пира (send упадёт)
                    try:
                        _ws_send(sock, b"", 0x9)
                        continue
                    except OSError:
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
        if p == "/api/glance":
            # external displays authenticate with a read-only token instead of
            # a session cookie; the panel preview still works via the session
            cfg = load_glance()
            tok = (q.get("token") or [""])[0]
            tok_ok = bool(cfg["enabled"] and cfg["token"]
                          and hmac.compare_digest(tok, cfg["token"]))
            if not (tok_ok or self._authed()):
                self._json({"error": "auth"}, 401); return
            lang = (q.get("lang") or ["ru"])[0]
            pl = glance_payload(lang, (q.get("screen") or [""])[0])
            if (q.get("all") or [""])[0] and self._authed():
                # constructor palette: every possible tile with live values
                pl = dict(pl, palette=glance_palette(lang))
                self._json(pl); return
            try:
                seq = int((q.get("seq") or ["-1"])[0])
            except ValueError:
                seq = -1
            if seq == pl["seq"]:
                self.send_response(304); self.end_headers()
            else:
                self._json(pl)
            return
        if p in ("/screen", "/screen/"):
            self._static("/screen.html"); return
        if p == "/api/wallpaper/img" and self._local():
            wp = _wallpaper_path()          # киоск рисует те же обои, что рабочий стол
            if wp:
                self._sendraw(wp)
            else:
                self.send_error(404)
            return
        if p == "/api/screen/data":
            # локальный экран ходит без сессии; из локалки — только по паролю
            if not (self._local() or self._authed()):
                self._json({"error": "auth"}, 401); return
            self._json(screen_payload((q.get("lang") or [""])[0])); return
        if p.startswith("/api/") and not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        try:
            if p == "/api/stats":
                self._json(stats())
            elif p == "/api/screen/config":
                self._json({"config": load_screen(), "present": bool(_bl_dir()),
                            "unit": _run(["systemctl", "is-enabled", "nas-screen"],
                                         timeout=5).strip()})
            elif p == "/api/glance/config":
                self._json({"config": load_glance(),
                            "catalog": [{"id": t[0], "name": t[1]} for t in glance_catalog()],
                            "presets": [{"id": pi, "name": pn} for pi, pn in GLANCE_PRESETS]})
            elif p == "/api/avail":
                self._json(avail_bars(int((q.get("hours") or ["24"])[0]),
                                      int((q.get("slots") or ["96"])[0])))
            elif p == "/api/notes/tree":
                self._json(notes_tree())
            elif p == "/api/notes/get":
                self._json(note_get((q.get("path") or [""])[0]))
            elif p == "/api/notes/search":
                self._json(notes_search((q.get("q") or [""])[0]))
            elif p == "/api/notes/history":
                self._json(notes_history((q.get("path") or [""])[0]))
            elif p == "/api/notes/histget":
                self._json(notes_hist_get((q.get("path") or [""])[0],
                                          (q.get("v") or [""])[0]))
            elif p == "/api/notes/trash":
                self._json(notes_trash_list())
            elif p == "/api/notes/file":
                fp = _notes_abs((q.get("path") or [""])[0])
                if os.path.isfile(fp):
                    self._sendraw(fp)
                else:
                    self._json({"error": "нет файла"}, 404)
            elif p == "/api/net":
                self._json(net_info())
            elif p == "/api/net/speedtest":
                self._json(net_speedtest())
            elif p == "/api/history":
                self._json(history_snapshot((q.get("range") or ["24h"])[0]))
            elif p == "/api/events":
                self._json(events_list((q.get("after") or ["0"])[0],
                                       (q.get("limit") or ["400"])[0],
                                       (q.get("wait") or ["0"])[0]))
            elif p == "/api/settings-backup":
                self._json(settings_backup_list())
            elif p == "/api/settings-backup/inspect":
                nm = (q.get("name") or [""])[0]
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "нет такого бэкапа"}, 404)
                else:
                    self._json(settings_backup_inspect(fp))
            elif p == "/api/settings-backup/get":
                nm = (q.get("name") or [""])[0]
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "нет такого бэкапа"}, 404)
                else:
                    self._sendraw(fp, True)
            elif p == "/api/health":
                self._json(health_report())
            elif p == "/api/desktop":
                self._json({"apps": discover_desktop_apps(), "volumes": external_volumes()})
            elif p == "/api/icon":
                self._send_icon((q.get("u") or [""])[0])
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
            elif p == "/api/fs/view":
                vp = gen_view(os.path.realpath((q.get("path") or [""])[0]))
                if vp: self._sendraw(vp)
                else: self.send_error(404)
            elif p == "/api/fs/thumbcache":
                b, n = thumbs_cache_stat()
                self._json({"bytes": b, "count": n})
            elif p == "/api/fs/vmeta":
                self._json(video_meta((q.get("path") or [""])[0]))
            elif p == "/api/fs/transcode":
                self._transcode()
            elif p == "/api/fs/sub":
                self._extract_sub()
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
            elif p == "/api/fsw/status":
                self._json(fsw_status())
            elif p == "/api/fsw/activity":
                self._json(fsw_activity((q.get("days") or ["60"])[0]))
            elif p == "/api/fsw/file":
                self._json(fsw_file((q.get("path") or [""])[0]))
            elif p == "/api/fsw/export":
                data = fsw_export_csv((q.get("days") or ["365"])[0],
                                      (q.get("kind") or [""])[0],
                                      (q.get("q") or [""])[0]).encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="file-history.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif p == "/api/fsw/events":
                self._json(fsw_events((q.get("before") or ["0"])[0],
                                      (q.get("limit") or ["100"])[0],
                                      (q.get("kind") or [""])[0],
                                      (q.get("q") or [""])[0],
                                      (q.get("day") or [""])[0],
                                      (q.get("ts") or ["0"])[0],
                                      (q.get("group") or [""])[0] == "1",
                                      (q.get("days") or ["0"])[0]))
            elif p == "/api/fs/duscan/status":
                self._json(duscan_status((q.get("root") or ["/"])[0]))
            elif p == "/api/fs/duscan/node":
                self._json(duscan_node((q.get("root") or ["/"])[0],
                                       (q.get("path") or [""])[0], (q.get("depth") or ["1"])[0]))
            elif p == "/api/fs/duscan/bigfiles":
                self._json(duscan_bigfiles((q.get("root") or ["/"])[0]))
            elif p == "/api/fs/duscan/dups":
                self._json(duscan_dups((q.get("root") or ["/"])[0]))
            elif p == "/api/fs/grep":
                self._json(fs_grep((q.get("path") or ["/"])[0], (q.get("q") or [""])[0]))
            elif p == "/api/fs/trash":
                self._json(fs_trash_list())
            elif p == "/api/fs/zip":
                self._send_zip(q.get("item") or [], (q.get("name") or ["archive.zip"])[0])
            elif p == "/api/disks":
                _scr = scrutiny_state()
                self._json({"disks": disks(), "fs": fs_tools(), "snapraid": snapraid_status(),
                            "pool": _pool_recovery(),
                            "scrutiny": {"ok": _scr.get("ok", False), "url": _scr.get("url")}})
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
            elif p == "/api/store":
                self._json(store_catalog())
            elif p == "/api/remotes":
                self._json(remotes_list())
            elif p == "/api/stack":
                self._json(stack_read((q.get("name") or [""])[0]))
            elif p == "/api/stack/logs":
                self._json(stack_logs((q.get("name") or [""])[0], (q.get("tail") or ["200"])[0]))
            elif p == "/api/stack/validate":
                self._json(stack_validate((q.get("name") or [""])[0]))
            elif p == "/api/docker/overview":
                self._json(docker_overview())
            elif p == "/api/docker/stats":
                self._json(docker_stats())
            elif p == "/api/docker/images":
                self._json(docker_images())
            elif p == "/api/docker/volumes":
                self._json(docker_volumes())
            elif p == "/api/automount":
                self._json(automount_state())
            elif p == "/api/comitup":
                self._json(comitup_state())
            elif p == "/api/sysconf":
                self._json(sysconf())
            elif p == "/api/usb-import":
                self._json({"config": usb_import_load(), "drives": usb_removable()})
            elif p == "/api/usb-devices":
                self._json(usb_devices())
            elif p == "/api/motd":
                self._json({"config": motd_load(), "preview": motd_preview()})
            elif p == "/api/usb-import/progress":
                self._json(usb_import_progress())
            elif p == "/api/ops":
                self._json({"ops": ops_hist_list()})
            elif p == "/api/myspeed":
                self._json(myspeed_state())
            elif p == "/api/vnstat":
                self._json(vnstat_state())
            elif p == "/api/wud":
                self._json(wud_state())
            elif p == "/api/scrutiny/device":
                self._json(scrutiny_device((q.get("serial") or [""])[0]))
            elif p == "/api/wallpaper/img":
                wp = _wallpaper_path()
                if wp:
                    self._sendraw(wp)
                else:
                    self.send_error(404)
            elif p == "/api/settings":
                self._json({"settings": load_settings()})
            elif p == "/api/maintenance":
                self._json({"maintenance": load_maintenance(),
                            "settings_backup_path": settings_backup_path(),
                            "snapraid_configured": os.path.isfile("/etc/snapraid.conf")})
            elif p == "/api/updates":
                self._json(apt_updates(refresh=(q.get("refresh") or ["0"])[0] == "1"))
            elif p == "/api/backup/config":
                cfg = nb_load(_nb_qpid(q))
                self._json({"config": nb_public(cfg), "profile": cfg["id"],
                            "profiles": nb_profiles_public()})
            elif p == "/api/backup/status":
                self._json(nb_status(_nb_qpid(q)))
            elif p == "/api/backup/dest-state":
                self._json(nb_dest_state(_nb_qpid(q)))
            elif p == "/api/backup/log":
                self._json(nb_log_tail((q.get("since") or ["0"])[0], _nb_qpid(q)))
            elif p == "/api/backup/history":
                self._json({"history": nb_history(_nb_qpid(q))})
            elif p == "/api/timemachine":
                self._json(tm_status())
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
            elif p in ("/notes", "/notes/"):
                # standalone notes URL (for a tunnel/reverse proxy): the same
                # desktop shell, but the client hides everything except notes
                self._static("/desktop.html")
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
        if p == "/api/screen/act":
            # тач на самом боксе = физический доступ; пароль на этом экране не спрашиваем
            if not self._local():
                self._json({"error": "forbidden"}, 403); return
            self._json(screen_action(self._body())); return
        if p == "/api/agent/notify":
            # local shell agents (nas-notify.sh) — pre-auth, loopback only
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._json({"error": "forbidden"}, 403); return
            self._json(agent_notify(self._body())); return
        if not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        # журналирование действий: кэшируем тело и перехватываем ответ; в finally
        # обязательно снимаем инстанс-атрибуты (keep-alive переиспользует handler)
        _oj, _ob, _bc = self._json, self._body, {}
        def _body_cached():
            if "b" not in _bc:
                _bc["b"] = _ob()
            return _bc["b"]
        def _json_logged(data, code=200):
            _oj(data, code)
            try:
                log_action(p, _bc.get("b") or {}, data if isinstance(data, dict) else {})
            except Exception:
                pass
        self._body, self._json = _body_cached, _json_logged
        try:
            if p == "/api/creds":
                b = self._body(); save_creds(b.get("creds", []))
                self._json({"ok": True})
            elif p == "/api/settings":
                b = self._body(); save_settings(b.get("settings", {}))
                self._json({"ok": True})
            elif p == "/api/screen/config":
                self._json({"ok": True, "config": save_screen(self._body().get("config") or {})})
            elif p == "/api/glance/config":
                self._json({"ok": True, "config": save_glance(self._body())})
            elif p == "/api/notes/save":
                b = self._body()
                self._json(note_save(b.get("path", ""), b.get("title", ""),
                                     b.get("tags") or [], b.get("md", ""),
                                     bool(b.get("pinned")), b.get("base_mtime", 0),
                                     bool(b.get("force")), bool(b.get("conflict_copy"))))
            elif p == "/api/notes/restore":
                b = self._body()
                self._json(notes_restore(b.get("path", ""), b.get("v", "")))
            elif p == "/api/notes/trash/restore":
                self._json(notes_trash_restore(self._body().get("path", "")))
            elif p == "/api/notes/trash/clear":
                self._json(notes_trash_clear())
            elif p == "/api/notes/new":
                b = self._body()
                self._json(note_new(b.get("folder", ""), b.get("title", "")))
            elif p == "/api/notes/mkdir":
                b = self._body()
                self._json(note_mkdir(b.get("folder", ""), b.get("name", "")))
            elif p == "/api/notes/move":
                b = self._body()
                self._json(note_move(b.get("path", ""), b.get("to", "")))
            elif p == "/api/notes/delete":
                self._json(note_delete(self._body().get("path", "")))
            elif p == "/api/notes/upload":
                b = self._body()
                self._json(note_upload(b.get("folder", ""), b.get("name", ""), b.get("data", "")))
            elif p == "/api/notes/root":
                self._json(notes_migrate(self._body().get("root", "")))
            elif p == "/api/maintenance":
                b = self._body(); self._json({"maintenance": save_maintenance(b.get("maintenance", {}))})
            elif p == "/api/fs/thumbcache/clear":
                self._json({"ok": True, "removed": thumbs_cache_clear()})
            elif p == "/api/fsw/scan":
                self._json(fsw_start(bool(self._body().get("deep"))))
            elif p == "/api/fsw/cancel":
                self._json(fsw_cancel())
            elif p == "/api/fsw/accept":
                self._json(fsw_accept())
            elif p == "/api/fsw/clear":
                self._json(fsw_clear(self._body().get("mode") or "events"))
            elif p == "/api/fsw/config":
                self._json(fsw_save(self._body()))
            elif p == "/api/fs/duscan/start":
                self._json(duscan_start(self._body().get("root", "/")))
            elif p == "/api/fs/duscan/cancel":
                self._json(duscan_cancel(self._body().get("root", "/")))
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
            elif p == "/api/motd":
                self._json(motd_save(self._body()))
            elif p == "/api/ops":
                self._json(ops_hist_add(self._body()))
            elif p == "/api/ops/clear":
                self._body(); self._json(ops_hist_clear())
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
            elif p == "/api/events/seen":
                b = self._body(); self._json(events_seen(b.get("id")))
            elif p == "/api/events/clear":
                self._json(events_clear())
            elif p == "/api/events/log":
                b = self._body()
                lvl = b.get("lvl") if b.get("lvl") in ("info", "ok", "warn", "crit") else "info"
                kind = b.get("kind") if b.get("kind") in ("files", "action", "disk", "svc") else "files"
                eid = log_event("user_action", b.get("title") or "", b.get("msg") or "",
                                lvl, kind=kind, desk=False)
                self._json({"ok": True, "id": eid})
            elif p == "/api/fs/fetch/cancel":
                b = self._body(); self._json(fs_fetch_cancel(b.get("id", "")))
            elif p == "/api/settings-backup/make":
                self._json(settings_backup_make())
            elif p == "/api/settings-backup/restore":
                b = self._body(); nm = b.get("name", "")
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "нет такого бэкапа"}, 404)
                else:
                    secs = b.get("sections")
                    self._json(settings_backup_restore(
                        fp, secs if isinstance(secs, list) else None))
            elif p == "/api/settings-backup/delete":
                b = self._body(); nm = b.get("name", "")
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "нет такого бэкапа"}, 404)
                else:
                    os.remove(fp); self._json({"ok": True})
            elif p == "/api/settings-backup/upload":
                # восстановление из файла с компьютера: тело запроса = tar.gz
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n <= 0 or n > 64 * 1024 * 1024:
                    self._json({"ok": False, "log": "плохой размер архива"}, 400)
                else:
                    tmp = os.path.join(settings_backup_dir(), ".upload.tmp")
                    with open(tmp, "wb") as f:
                        remaining = n
                        while remaining > 0:
                            chunk = self.rfile.read(min(262144, remaining))
                            if not chunk:
                                break
                            f.write(chunk); remaining -= len(chunk)
                    # Не восстанавливаем сразу: кладём архив в список и отдаём имя,
                    # чтобы клиент показал тот же диалог выбора разделов. Иначе
                    # чужой файл молча перезаписал бы пароль панели.
                    info = settings_backup_inspect(tmp) if remaining == 0 \
                        else {"ok": False, "log": "обрыв загрузки"}
                    if not info.get("ok") or not info.get("sections"):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        self._json({"ok": False, "log": info.get("log")
                                    or "в архиве нет файлов настроек NAS-OS"}, 400)
                    else:
                        # дата в имени — чтобы ротация по backup_keep резала по возрасту
                        name = "nas-settings-%s-up.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
                        dst = os.path.join(settings_backup_dir(), name)
                        os.replace(tmp, dst); os.chmod(dst, 0o600)
                        self._json({"ok": True, "name": name, "sections": info["sections"],
                                    "log": "архив загружен"})
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
                elif any(mp in _SYS_MPS or mp == STORAGE or mp.startswith("/mnt/disk") or mp.startswith("/mnt/parity")
                         for mp in _disk_mountpoints(dev)):
                    # защита в вебе поверх движка: не форматировать системный/пуловый диск
                    self._json({"ok": False, "log": "это системный или пуловый диск — форматирование запрещено"}, 400)
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
            elif p == "/api/comitup/save":
                b = self._body()
                self._json(comitup_save(b.get("ap_name", ""), b.get("ap_password", "")))
            elif p == "/api/security/fail2ban":
                b = self._body()
                self._json(fail2ban_save(b.get("maxretry", 5), b.get("bantime", "1h")))
            elif p == "/api/security/unban":
                self._json(fail2ban_unban(self._body().get("ip", "")))
            elif p == "/api/security/ufw-port":
                b = self._body()
                self._json(ufw_port(b.get("action", ""), b.get("port", "")))
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
                b = self._body(); self._json(fs_trash_restore(b.get("id", ""), b.get("dest", "")))
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
            elif p == "/api/remotes/save":
                self._json(remotes_save(self._body()))
            elif p == "/api/remotes/delete":
                self._json(remotes_delete(self._body().get("id", "")))
            elif p == "/api/remotes/mount":
                self._json(remote_mount(self._body().get("id", "")))
            elif p == "/api/remotes/umount":
                self._json(remote_umount(self._body().get("id", "")))
            elif p == "/api/remotes/realpath":
                b = self._body()
                self._json(remote_realpath(b.get("id", ""), b.get("path", "")))
            elif p == "/api/store/install":
                b = self._body()
                self._json(store_install(b.get("id", ""), b.get("values") or {}))
            elif p == "/api/store/icon":
                b = self._body()
                self._json(store_icon_upload(b.get("stack", ""), b.get("data", "")))
            elif p == "/api/store/custom":
                b = self._body()
                self._json(store_custom_save(b.get("stack", ""), b.get("name", ""),
                                             b.get("icon", ""), b.get("port")))
            elif p == "/api/store/replica":
                b = self._body()
                self._json(store_replica_save(b.get("id", ""), b))
            elif p == "/api/store/replica/run":
                # синхронизация/восстановление реплики — долгий bash со стримом лога
                b = self._body()
                try:
                    script, env = store_replica_script(b.get("id", ""),
                                                       "restore" if b.get("mode") == "restore" else "sync")
                except ValueError as e:
                    self._json({"ok": False, "log": str(e)}); return
                log_event("action", "Реплика %s: %s" % (b.get("id", ""),
                          "восстановление" if b.get("mode") == "restore" else "синхронизация"),
                          "", "ok", kind="svc", desk=False)
                self._stream_cmd(["bash", "-c", script],
                                 env=dict(_C_ENV, **env), timeout=86400)
            elif p == "/api/stack/stream":
                # долгие compose-действия (up при установке, pull) — стримом
                b = self._body()
                name, action = b.get("name", ""), b.get("action", "")
                amap = {"up": ["up", "-d"], "pull": ["pull"], "update": ["pull"],
                        "rebuild": ["up", "-d", "--build"]}
                if not _STACK_RE.match(name) or not os.path.isfile(_compose_path(name)) \
                        or action not in amap:
                    self._json({"ok": False, "log": "недопустимый стек/действие"}); return
                wud_invalidate()
                args = ["docker", "compose", "-f", _compose_path(name), "-p", name] + amap[action]
                if action == "update":       # pull + перезапуск одной кнопкой
                    self._stream_cmd(["bash", "-c",
                        "%s && docker compose -f %s -p %s up -d" %
                        (" ".join(map(shlex.quote, args)),
                         shlex.quote(_compose_path(name)), shlex.quote(name))], timeout=3600)
                else:
                    self._stream_cmd(args, timeout=3600)
            elif p == "/api/updates/apply":
                # установка обновлений apt со стримом лога; DEBIAN_FRONTEND чтобы не задавало вопросов
                self._body()
                env = dict(_C_ENV, DEBIAN_FRONTEND="noninteractive")
                self._stream_cmd(["apt-get", "-y",
                                  "-o", "Dpkg::Options::=--force-confold",
                                  "-o", "Dpkg::Options::=--force-confdef", "upgrade"],
                                 env=env, timeout=3600)
                try:
                    log_event("action", "Обновление пакетов apt", "", "ok", kind="action", desk=True)
                except Exception:
                    pass
            elif p == "/api/backup/config":
                b = self._body()
                cfg = nb_save(b.get("config", {}), _nb_bpid(b))
                self._json({"config": nb_public(cfg), "profile": cfg["id"],
                            "profiles": nb_profiles_public()})
            elif p == "/api/backup/test":
                self._json(nb_test(nb_load(_nb_bpid(self._body()))))
            elif p == "/api/backup/browse":
                b = self._body()
                cfg = nb_load(_nb_bpid(b))
                self._json(nb_browse_dest(cfg, b.get("path", "")) if b.get("dest")
                           else nb_browse(cfg, b.get("path", "")))
            elif p == "/api/backup/run-bg":
                b = self._body()
                self._json(nb_run_bg(_nb_bpid(b), dry=bool(b.get("dry", False)),
                                     allow_delete=bool(b.get("allow_delete", False))))
            elif p == "/api/backup/cancel":
                b = self._body()
                pid = nb_load(_nb_bpid(b))["id"]
                _nb_queue_remove(pid)                        # если ещё не стартовал — просто выкинуть из очереди
                try:
                    open(nb_run_cancel(pid), "w").close()    # флаг для процесса-драйвера в юните
                except OSError:
                    pass
                # пометить остановку в состоянии: панель показывает «останавливаю…» даже
                # если страницу перезагрузили, пока rsync доумирает
                st = _nb_run_state_read(pid)
                if st.get("running"):
                    st["stopping"] = int(time.time())
                    _nb_run_state_write(pid, st)
                self._json({"ok": True})
            elif p == "/api/timemachine/apply":
                b = self._body()
                params = {}
                path = str(b.get("path", "")).strip()
                if path:
                    if not path.startswith("/") or ".." in path:
                        self._json({"ok": False, "log": "путь должен быть абсолютным без .."}); return
                    params["tm_path"] = path
                user = str(b.get("user", "")).strip()
                if user:
                    if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
                        self._json({"ok": False, "log": "недопустимое имя пользователя"}); return
                    params["tm_user"] = user
                if b.get("password"):
                    params["tm_pass"] = str(b.get("password"))
                try:
                    quota = int(b.get("quota_gb") or 0)
                except (TypeError, ValueError):
                    quota = 0
                params["tm_quota"] = str(max(0, quota))
                r = engine("timemachine", params)
                r["status"] = tm_status()
                self._json(r)
            elif p == "/api/timemachine/disable":
                r = engine("timemachine-off")
                r["status"] = tm_status()
                self._json(r)
            elif p == "/api/backup/history-del":
                b = self._body()
                if b.get("all"):
                    self._json(nb_history_clear(_nb_bpid(b)))
                else:
                    try: ts = int(b.get("ts"))
                    except (TypeError, ValueError): ts = None
                    self._json(nb_history_clear(_nb_bpid(b), ts) if ts is not None
                               else {"ok": False, "log": "нет ts"})
            elif p == "/api/backup/profile":
                b = self._body(); act = b.get("action", "")
                if act == "add":
                    self._json(nb_profile_add(b.get("name", ""), b.get("clone_from", ""),
                                              str(b.get("direction") or "")))
                elif act == "rename":
                    self._json(nb_profile_rename(_nb_bpid(b), b.get("name", "")))
                elif act == "delete":
                    self._json(nb_profile_delete(_nb_bpid(b), b.get("confirm", "")))
                else:
                    self._json({"ok": False, "log": "неизвестное действие"})
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
        finally:
            for attr in ("_json", "_body"):
                try:
                    delattr(self, attr)
                except AttributeError:
                    pass


def main():
    # SIGUSR1 -> dump all thread stacks to stderr (journal). nas-netguard sends
    # it right before restarting a hung panel, so the hang site gets logged.
    import faulthandler
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    os.makedirs(WEB_DIR, exist_ok=True)
    ensure_web_assets()
    try:
        apply_spindown_all()          # восстановить настройки сна дисков после старта/ребута
    except Exception:
        pass
    try:
        _pool_alias_apply(load_maintenance().get("pool_alias", ""))   # симлинк пула (напр. /volume2)
    except Exception:
        pass
    try:
        _snap_sched_apply(load_maintenance())    # расписание SnapRAID из настроек
    except Exception:
        pass
    try:
        _usb_sh_sync()      # хелпер импорта на диске мог протухнуть после git pull
    except Exception:
        pass
    try:
        _nb_migrate()       # старый плоский конфиг бэкапа -> список профилей
    except Exception:
        pass
    try:
        _motd_extras_apply(motd_load())   # чужие куски приветствия — по настройке
    except Exception:
        pass
    threading.Thread(target=monitor_loop, daemon=True).start()
    srv = _Server(("0.0.0.0", PORT), H)
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
    elif len(sys.argv) > 1 and sys.argv[1] == "backup-run":
        _args = sys.argv[2:]
        _pid = next((a for a in _args if a not in ("dry", "allow-delete")), NB_MAIN)
        nb_run_cli(_pid, dry=("dry" in _args), allow_delete=("allow-delete" in _args))
    else:
        main()
