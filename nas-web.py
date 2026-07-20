#!/usr/bin/env python3
"""
nas-web.py — web backend of the NAS setup wizard and desktop (Raspberry Pi 5).

Python 3 standard library only (no pip). Serves static files from web/ and a JSON API:
  GET  /api/stats                 — live Pi metrics (CPU, temp, RAM, disk, network, uptime)
  GET  /api/desktop               — desktop shortcuts from docker labels web-desktop.*
  GET/POST /api/creds             — credential store (~/nas-config/credentials.json, 0600)
  GET  /api/setup/state           — system state for the wizard
  POST /api/setup/<action>        — run a wizard step (delegates to nas-wizard.sh api)

System changes are made by the vetted nas-wizard.sh engine (api mode), so the
server must run as root (the nas-setup.sh launcher does that).
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
def _uid1000_name():
    try:
        return pwd.getpwuid(1000).pw_name    # the box's primary user (install.sh provisions uid 1000)
    except KeyError:
        return "root"
TARGET_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or _uid1000_name()
HOME        = os.path.expanduser("~" + TARGET_USER)
NAS_CONFIG  = os.path.join(HOME, "nas-config")
CREDS_FILE  = os.path.join(NAS_CONFIG, "credentials.json")
TRASH       = os.path.join(HOME, ".nas-trash")
PORT        = int(os.environ.get("NAS_WEB_PORT", "8080"))
STORAGE     = "/mnt/storage"      # mergerfs pool (may not exist at all)
STORAGE_CONF = os.path.join(NAS_CONFIG, "storage.json")
BODY_MAX    = 32 * 1024 * 1024   # JSON request body cap (wallpaper/base64 fits; larger — via _upload_raw)
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
CRON_URL    = os.environ.get("CRONMASTER_URL", "http://127.0.0.1:8123")  # published cronmaster port

# --------------------------------------------------------------------------- #
#  Metrics collection (read-only, no root needed)
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
        sys.stderr.write("nas-web: %s is corrupt, saved as %s.bad\n" % (name, name))
        return default

# --------------------------------------------------------------------------- #
#  Primary storage
#
#  One concept instead of five different defaults. Previously every subsystem
#  (import, backup, notes, Time Machine, thumbnails) substituted "/mnt/storage/…"
#  itself — on a box WITHOUT a pool that path was an ordinary folder on the system
#  card: import silently dumped 8 GB onto it. Now there is a single source of truth:
#    pool mounted     → storage = pool (NVMe appeared — nothing to reconfigure);
#    otherwise        → the chosen removable volume (external SSD);
#    nothing          → None, and subsystems must honestly show that instead of
#                       writing "somewhere".
#  storage_conf() remembers the CHOICE (even when the disk is unplugged) — otherwise
#  after the SSD is removed the defaults would slide back onto the card.
# --------------------------------------------------------------------------- #
def storage_conf():
    d = _json_load_strict(STORAGE_CONF, {})
    return d if isinstance(d, dict) else {}

def storage_root():
    """Mounted storage root or None."""
    if os.path.ismount(STORAGE):
        return STORAGE
    r = str(storage_conf().get("root") or "").rstrip("/")
    if r and os.path.ismount(r):
        return r
    return None

def storage_base():
    """Where to READ defaults from, even if the medium is not attached right now:
    the path exists, it is just not mounted — subsystems will fail their mount
    check and say "disk not attached" instead of slipping in the system card."""
    if os.path.ismount(STORAGE):
        return STORAGE
    r = str(storage_conf().get("root") or "").rstrip("/")
    if r:
        return r
    return STORAGE if os.path.isdir(STORAGE) else None

def storage_state():
    """For the panel: what is chosen, whether it is mounted, and where defaults point."""
    cfg = storage_conf()
    root = storage_root()
    pool = os.path.ismount(STORAGE)
    base = storage_base()
    st = {"root": root, "base": base, "pool": pool,
          "chosen": str(cfg.get("root") or ""), "label": cfg.get("label") or "",
          "mounted": bool(root), "candidates": storage_candidates()}
    if root:
        try:
            u = shutil.disk_usage(root)
            st["size"], st["free"] = u.total, u.free
        except OSError:
            pass
    return st

def storage_candidates():
    """Volumes fit to be storage: the pool and everything mounted under
    /mnt|/media|/srv, except the system root. The system disk is deliberately kept
    out: "storage" on the same card the OS boots from is not storage."""
    out = []
    seen = set()
    for line in _read("/proc/mounts").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        dev, mp, fs = p[0], p[1].replace("\\040", " "), p[2]
        if mp in seen or fs in ("proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup2"):
            continue
        if not (mp == STORAGE or mp.startswith("/mnt/") or mp.startswith("/media/")
                or mp.startswith("/srv/")):
            continue
        seen.add(mp)
        item = {"path": mp, "fs": fs, "dev": dev, "pool": mp == STORAGE,
                "removable": mp.startswith("/media/")}
        try:
            u = shutil.disk_usage(mp)
            item["size"], item["free"] = u.total, u.free
        except OSError:
            pass
        out.append(item)
    out.sort(key=lambda x: (not x["pool"], x["path"]))
    return out

def storage_save(body):
    root = str((body or {}).get("root") or "").rstrip("/")
    if root:
        if not root.startswith(("/mnt/", "/media/", "/srv/")) or ".." in root:
            return {"ok": False, "log": "invalid storage path"}
        if not os.path.ismount(root):
            return {"ok": False, "log": "volume %s is not mounted" % root}
    cfg = storage_conf()
    cfg["root"] = root
    cfg["label"] = os.path.basename(root) if root else ""
    try:
        _json_save(STORAGE_CONF, cfg)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "log": "saved", "state": storage_state()}

def storage_sub(name):
    """Storage subfolder path for defaults. None — no storage at all."""
    b = storage_base()
    return os.path.join(b, name) if b else None

_CPU = {"t": 0.0, "tot": 0, "idle": 0, "v": 0.0}
_CPU_LOCK = threading.Lock()


def cpu_percent(sample=0.20):
    """CPU load since the PREVIOUS call, cached ~1 s and shared by everyone.

    Every caller used to take its own 200 ms snapshot at its own moment, so the
    wall screen, the panel and the glance tile each showed a different number
    for the same instant (and each burned 200 ms of a request). One rolling
    delta = one truth, and no sleeping in the request path."""
    def snap():
        parts = _read("/proc/stat").splitlines()[0].split()[1:]
        vals = list(map(int, parts))
        idle = vals[3] + vals[4]
        return sum(vals), idle
    with _CPU_LOCK:
        now = time.time()
        if _CPU["t"] and now - _CPU["t"] < 1.0:
            return _CPU["v"]
        tot, idle = snap()
        if not _CPU["t"]:                       # cold start: classic sample
            time.sleep(sample)
            tot2, idle2 = snap()
            dt, di = tot2 - tot, idle2 - idle
            _CPU.update(t=time.time(), tot=tot2, idle=idle2,
                        v=round(100 * (dt - di) / dt, 1) if dt else 0.0)
            return _CPU["v"]
        dt, di = tot - _CPU["tot"], idle - _CPU["idle"]
        _CPU.update(t=now, tot=tot, idle=idle,
                    v=round(100 * (dt - di) / dt, 1) if dt > 0 else _CPU["v"])
        return _CPU["v"]

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
    free  = s.f_bavail * s.f_frsize                  # available (like df Avail; WITHOUT ext4 reserve)
    used  = (s.f_blocks - s.f_bfree) * s.f_frsize    # actually used by data (reserve ≠ "used")
    denom = used + free                              # base for % as in df (ignoring the reserve)
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
    """RTT to the router + a sign of "we sit behind a bridge/repeater". Cache: ping once per 2 minutes."""
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
    """Type of the active connection (Wi-Fi/cable), link speed (Mbit/s),
    for Wi-Fi — SSID/band/signal. So the effect of the cable is plainly visible."""
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
                fr = int(m.group(1)); info["band"] = "5 GHz" if fr >= 5000 else "2.4 GHz"
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
    """Mini download+upload speedtest via Cloudflare. Runs ~8-10 s for accuracy."""
    out = {"ok": False}
    # --- DOWNLOAD: pull for up to ~10 s; count by what was actually downloaded (even if
    #     the stream broke — Cloudflare may close the connection after part of the volume) ---
    n = 0.0; t0 = time.time()
    try:
        # OVH (Europe) — Cloudflare in Spain is often blocked (LaLiga)
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
            out["log"] = ("no internet?" if "URLError" in str(type(e)) else str(e)[:100])
    dt = max(0.1, time.time() - t0)
    if n >= 1000000:
        out["ok"] = True
        out["down_MBs"] = round(n / dt / 1048576, 1)
        out["down_mbps"] = round(n * 8 / dt / 1e6, 1)
    # --- UPLOAD: send a fixed volume, measure the time ---
    try:
        total = 60 * 1024 * 1024   # 60 MB
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
        s.connect(("8.8.8.8", 1)); ip = s.getsockname()[0]; s.close()
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
    """Automount state: whether enabled, base, user."""
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
    """smartctl -j with a device-type fallback: bare → -d sat → -d scsi
    (USB bridges without -d sat return only the version banner)."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):   # like the neighbors: don't let a dev like "-x…" become a flag
        return {}
    # for the device type we try variants; NVMe works directly
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
    # -n standby: do NOT wake a sleeping disk for a background poll (list/widget/health).
    # If the disk is in standby, smartctl returns empty → the disk shows health/temp as "—".
    j = _smartctl_json(["-n", "standby", "-H", "-A"], dev, timeout=8)
    if not _smart_has_data(j):
        return None
    t = (j.get("temperature") or {}).get("current")
    return {"temp": t if t else None,      # USB bridges return 0 — that's "don't know", not zero degrees
            "healthy": (j.get("smart_status") or {}).get("passed"),
            "hours": (j.get("power_on_time") or {}).get("hours")}

def fs_tools():
    """Filesystems that have mkfs available (what can actually be created)."""
    return [fs for fs in ("ext4", "xfs", "btrfs", "exfat", "ntfs", "vfat")
            if shutil.which("mkfs." + fs)]

_SZ_MUL = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
def _size_bytes(s):
    """lsblk returns the size as a string ("238.8G"). Picking the main partition needs an ordering."""
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
    scr = scrutiny_state().get("devices", {})   # {} if Scrutiny is not installed
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
        # for the system disk show the root "/", not /boot/firmware (otherwise "free"
        # is taken from the tiny boot partition); for the rest — the point in /mnt, then /media
        parts = []
        for ch in d.get("children", []) or []:
            cmp = ch.get("mountpoint")
            parts.append({
                "name": ch.get("name"), "path": ch.get("path"), "size": ch.get("size"),
                "fstype": ch.get("fstype"), "label": ch.get("label"),
                "mount": cmp, "mounted": bool(cmp),
                "parttypename": ch.get("parttypename"),
            })
        # The main partition is the LARGEST mounted one, not the first in the list.
        # On a flash drive with leftovers of a boot image, a 200 MB EFI comes first, and
        # the card showed "197 MB free" instead of the honest 239 GB.
        mparts = [x for x in parts if x["mount"]]
        main = max(mparts, key=lambda x: _size_bytes(x["size"])) if mparts else None
        primary = (("/" if "/" in mounts else None)
                   or (main["mount"] if main else None)
                   or (mounts[0] if mounts else None))
        # FS and label from the same partition as the stats (otherwise the title is from one,
        # the numbers from another)
        fstype = (main.get("fstype") if main else None) or d.get("fstype")
        label = (main.get("label") if main else None) or d.get("label")
        if label is None and parts:
            label = parts[0].get("label")
        fstab = _read("/etc/fstab")
        # match the mountpoint as a whole fstab field, not a substring — else
        # /mnt/disk1 falsely matches an /mnt/disk10 line
        in_fstab = bool(primary and re.search(
            r"(?m)^\s*\S+\s+" + re.escape(primary) + r"\s", fstab))
        size = d.get("size")
        # empty card-reader slot / no inserted media → lsblk returns size 0B
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
            "spindown": spin.get((d.get("serial") or "").strip() or "\0") or spin.get(d.get("path")),
            "speedtest": spd.get((d.get("serial") or "").strip() or "\0") or spd.get(d.get("path")),
            "scrutiny": scr.get((d.get("serial") or "").strip()),   # None if no Scrutiny/no data
        })
    return res

def external_volumes():
    """Volumes outside the pool (USB disks/flash drives, free disks with a FS) — for
    desktop shortcuts and the "Disks" section in the file manager sidebar."""
    vols = []
    for d in disks():
        if d.get("no_media") or d["role"] not in ("free", "removable"):
            continue
        parts = d["partitions"] or []
        if not parts and d.get("fstype"):      # FS directly on the disk, without a partition table
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
        return {"ok": False, "log": "SMART unavailable: disk/USB bridge returns no data (or root is needed)"}
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

_C_ENV = dict(os.environ, LC_ALL="C", LANG="C")   # stable (English) utility output for parsing

def _run(cmd, timeout=40, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env or _C_ENV)
        return {"ok": p.returncode == 0, "code": p.returncode, "log": (p.stdout + p.stderr).strip()}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "code": -1, "log": str(e)}

def disk_mount(target, unmount=False):
    if not re.match(r"^/[\w/.+-]+$", target or ""):
        return {"ok": False, "log": "invalid path"}
    r = _run(["umount" if unmount else "mount", target])
    if r["ok"] and not r["log"]:
        r["log"] = "unmounted" if unmount else "mounted"
    return r

def _smart_dtype(dev):
    """Determine the working -d type for the device (for commands that don't read JSON)."""
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
        return {"ok": False, "log": "invalid device"}
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
    """Key of the saved measurement: serial (unchanged when sdX is renamed) or the dev itself."""
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
    """Speed test: sequential read from the device (bypassing the cache) +
    sequential write of a temp file to a mounted partition of this disk
    (the file is deleted). The result is saved and shown on the disk card."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "invalid device"}
    if not os.path.exists(dev):
        return {"ok": False, "log": "no such device"}
    r = _run(["dd", "if=" + dev, "of=/dev/null", "bs=4M", "count=256", "iflag=direct"], timeout=120)
    read_mbps = _dd_mbps(r.get("log", ""))
    if read_mbps is None:
        # a running backup keeps the disk busy — the read stalls and times out.
        # say so plainly (a cryptic dd error reads as "the test is broken"); the
        # previous measurement stays on the card because we don't overwrite it.
        if nb_any_active():
            return {"ok": False, "busy": True,
                    "log": "disk busy — a backup is running; measure again once it finishes"}
        return {"ok": False, "log": "measurement failed: " + (r.get("log", "")[-120:])}
    # write: to a mounted rw point with spare space; for the system disk
    # (no "own" points besides / and /boot) write to the temp dir /var/tmp
    write_mbps = None
    wnote = "write: no mounted partition to test"
    mps = _disk_mountpoints(dev)
    cands = [mp for mp in mps if mp not in _SYS_MPS] or (["/var/tmp"] if "/" in mps else [])
    for mp in cands:
        try:
            st = os.statvfs(mp)
            if st.f_bavail * st.f_frsize < (1 << 30):
                wnote = "write: not enough free space to test"
                continue
        except OSError:
            continue
        tmp = os.path.join(mp, ".nas-speedtest.tmp")
        try:
            w = _run(["dd", "if=/dev/zero", "of=" + tmp, "bs=4M", "count=64",
                      "oflag=direct", "conv=fsync"], timeout=90)
            if not w["ok"]:      # FS without O_DIRECT (exFAT/NTFS) — retry via fsync
                w = _run(["dd", "if=/dev/zero", "of=" + tmp, "bs=4M", "count=64",
                          "conv=fsync"], timeout=90)
            write_mbps = _dd_mbps(w.get("log", ""))
            wnote = "" if write_mbps is not None else "write: measurement failed"
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
        with _speed_lock:                 # two simultaneous tests must not lose entries
            saved = _speedtest_load()
            saved[_speedtest_key(dev)] = res
            tmp = SPEEDTEST_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(saved, f)
            os.replace(tmp, SPEEDTEST_FILE)
    except OSError:
        pass
    log = "read: %.0f MB/s" % read_mbps
    log += (" · write: %.0f MB/s" % write_mbps) if write_mbps is not None else (" · " + wnote)
    return {"ok": True, "read_mbps": res["read"], "write_mbps": res["write"],
            "t": res["t"], "log": log}

def disk_eject(dev):
    """Safely eject a removable disk: unmount all partitions + cut power."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "invalid device"}
    mps = _disk_mountpoints(dev)
    for mp in mps:
        if mp in _SYS_MPS or mp == STORAGE or mp.startswith("/mnt/disk") or mp.startswith("/mnt/parity"):
            return {"ok": False, "log": "this is a system or pool disk — ejection is not allowed"}
    for mp in mps:
        r = _run(["umount", mp], timeout=20)
        if not r["ok"]:
            return {"ok": False, "log": "disk busy (%s): %s" % (mp, r["log"][-80:])}
    po = _run(["udisksctl", "power-off", "-b", dev], timeout=15)
    return {"ok": True, "log": "safe to disconnect" + (" (power cut)" if po["ok"] else " (unmounted)")}

_health_cache = {"t": 0, "data": None}
_health_lock = threading.Lock()

def health_report():
    """Health summary cached for 60 s (heavy: smartctl/systemctl/disks)."""
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
    add("CPU temperature", ("%s °C" % t) if t is not None else "—",
        "bad" if t and t >= 75 else "warn" if t and t >= 65 else "ok")
    thr = s.get("throttled") or {}
    add("Power and throttling", "sag/throttling" if not thr.get("ok", True) else "normal",
        "bad" if not thr.get("ok", True) else "ok",
        "PSU current shortage or overheating" if not thr.get("ok", True) else "")
    m = (s.get("mem") or {}).get("pct", 0)
    add("RAM", "%s%% used" % m, "warn" if m >= 90 else "ok")
    root = disk_info("/") or {}
    rp = root.get("pct", 0)
    add("System card /", "%s%% used" % rp, "bad" if rp >= 95 else "warn" if rp >= 90 else "ok",
        "Free %s" % _fmt_b(root.get("total", 0) - root.get("used", 0)) if root else "")
    pool = s.get("disk_pool") or {}
    if pool.get("path") == "/mnt/storage":
        pp = pool.get("pct", 0)
        add("Storage (pool)", "%s%% used" % pp, "bad" if pp >= 95 else "warn" if pp >= 90 else "ok",
            "Free %s" % _fmt_b(pool.get("total", 0) - pool.get("used", 0)))
    # disks: SMART health and temperature
    ds = disks()
    bad = [d["name"] for d in ds if (d.get("smart") or {}).get("healthy") is False]
    hot = [d["name"] for d in ds if isinstance((d.get("smart") or {}).get("temp"), int) and d["smart"]["temp"] >= 60]
    add("Disk health (SMART)",
        ("failure: " + ", ".join(bad)) if bad else ("overheat: " + ", ".join(hot)) if hot else "all healthy",
        "bad" if bad else "warn" if hot else "ok")
    # a data disk gone read-only = emergency remount after an I/O error — data at risk.
    # Reuse _readonly_mounts(): it excludes removable media (a dirty flash drive brought
    # up ro by automount is routine, not a NAS failure) — the inline check here didn't.
    ro = _safe(_readonly_mounts, [])
    if ro:
        add("Filesystem read-only", ", ".join(ro), "bad",
            "A disk went read-only after an I/O error — unmount and run e2fsck")
    # critical disk temperature (≥65 screams; 60-64 is a quiet warn)
    dtc = [d["name"] for d in ds if isinstance((d.get("smart") or {}).get("temp"), int) and d["smart"]["temp"] >= 65]
    dth = [d["name"] for d in ds if isinstance((d.get("smart") or {}).get("temp"), int) and 60 <= d["smart"]["temp"] < 65]
    if dtc or dth:
        add("Disk temperature", ("critical: " + ", ".join(dtc)) if dtc else ("hot: " + ", ".join(dth)),
            "bad" if dtc else "warn", "A disk is running hot — check airflow/cooling")
    # a mounted USB disk that stopped responding (stale mount / device dropped mid-operation)
    usboff = []
    for d in ds:
        if d.get("tran") == "usb" and d.get("role") != "system":
            for mp in (d.get("mounts") or []):
                try:
                    os.statvfs(mp)
                except OSError:
                    usboff.append(d.get("name")); break
    if usboff:
        add("USB disk offline", ", ".join(usboff[:4]), "bad",
            "A mounted USB disk stopped responding — reconnect it, then e2fsck")
    # no internet (cheap cached probe)
    if not _safe(_inet_ok, True):
        add("Internet", "no connection", "warn", "Cable/router down or DNS unreachable")
    # backup problems: last run had errors, or the destination disk is unplugged
    try:
        bkerr, bkoff = [], []
        for pr in (nb_profiles() or []):
            pc = _safe(lambda x=pr: nb_load(x["id"]), {}) or {}
            pbase = pc.get("dest_base") or ""
            if pbase and _dest_disk_absent(pbase):
                bkoff.append(pr["name"])
            elif (nb_history(pr["id"]) or [{}])[0].get("result") == "warn":
                bkerr.append(pr["name"])
        if bkoff:
            add("Backup destination", "disk unplugged: " + ", ".join(bkoff[:4]), "warn",
                "The backup target disk is not connected")
        if bkerr:
            add("Backup errors", ", ".join(bkerr[:4]), "warn", "The last backup run reported errors")
    except Exception:
        pass
    # mass file deletion HELD by the integrity scanner awaiting confirmation.
    # Key on `pending` (the unconfirmed guard trip), NOT last.removed: an accepted
    # deletion or a routine cleanup under guard_pct must not keep the health page
    # yellow for up to interval_days — pending clears the moment the user confirms.
    pend = _safe(lambda: (fsw_status().get("pending") or {}), {}) or {}
    if pend.get("count", 0) >= 20:
        add("Mass file deletion", "{:,} files removed — confirm it".format(pend["count"]), "warn",
            "Many files disappeared from storage — confirm it was intentional in “File history”")
    # SnapRAID data protection
    sn = snapraid_status()
    if sn.get("configured"):
        for kind, ru in (("sync", "sync"), ("scrub", "scrub")):
            e = sn.get("last_" + kind)
            if e:
                add("SnapRAID · " + ru, "%s (%s)" % ("success" if e["result"] == "ok" else "error", (e.get("date") or "")[:10]),
                    "ok" if e["result"] == "ok" else "bad")
        if sn.get("blocked"):
            add("SnapRAID · protection", "sync halted (mass deletion)", "warn")
    # failed services
    r = _run(["systemctl", "list-units", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=8)
    failed = [l.split()[0] for l in (r.get("log") or "").splitlines() if l.strip()]
    add("systemd services", (", ".join(failed[:5])) if failed else "all running", "bad" if failed else "ok")
    # reboot/updates
    if os.path.exists("/var/run/reboot-required"):
        add("Updates", "reboot required", "warn", "Kernel/libc updates apply after a reboot")
    order = {"bad": 2, "warn": 1, "ok": 0}
    overall = max((order[c["lvl"]] for c in checks), default=0)
    return {"checks": checks, "overall": ["ok", "warn", "bad"][overall], "ts": int(time.time())}

def _fmt_b(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.0f %s" % (n, u)
        n /= 1024
    return "%.1f PB" % n

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
    if minutes <= 20:               # 1..240 → step 5 s
        return max(1, min(240, int(round(minutes * 60 / 5))))
    return min(251, 240 + int(round(minutes / 30.0)))   # 241.. → step 30 min

def disk_spindown(dev, minutes):
    """Idle timeout before the disk spins down (hdparm -S). minutes=0 — disable."""
    if not re.match(r"^/dev/[\w-]+$", dev or ""):
        return {"ok": False, "log": "invalid device"}
    try:
        minutes = max(0, min(240, int(minutes)))
    except (ValueError, TypeError):
        return {"ok": False, "log": "invalid value"}
    r = _run(["hdparm", "-S", str(_hdparm_s_value(minutes)), dev], timeout=20)
    cfg = _load_spindown()
    cfg.pop(dev, None)                      # drop any legacy dev-path entry for this disk
    cfg[_speedtest_key(dev)] = minutes      # key by serial: sdX isn't stable across re-plug
    try:
        _json_save(SPINDOWN_FILE, cfg)
    except OSError:
        pass
    if not r["ok"]:
        return {"ok": False, "log": "disk/USB bridge does not support sleep control: " + r["log"][-80:]}
    return {"ok": True, "log": ("disk sleeps after %d min idle" % minutes) if minutes else "sleep disabled (disk always active)"}

def apply_spindown_all():
    cfg = _load_spindown()
    if not cfg:
        return
    # entries are keyed by serial (new) or a /dev path (legacy). Resolve serials to the
    # CURRENT device, since sdX ordering isn't stable across reboots/re-plugs.
    ser2dev = {}
    for ln in (_run(["lsblk", "-dpno", "NAME,SERIAL"], timeout=8).get("log") or "").splitlines():
        parts = ln.split()
        if len(parts) >= 2:
            ser2dev[parts[-1]] = parts[0]
    for key, minutes in cfg.items():
        dev = key if key.startswith("/dev/") else ser2dev.get(key)
        if dev and os.path.exists(dev):
            try:
                _run(["hdparm", "-S", str(_hdparm_s_value(int(minutes))), dev], timeout=20)
            except Exception:
                pass

def snapraid_status():
    """Last sync/scrub from /var/log/snapraid.log (for data protection)."""
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
        if "ABORT: files deleted" in l:
            st["blocked"] = l.strip()[-140:]
    return st

# --------------------------------------------------------------------------- #
#  Processes and systemd services
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
        return {"ok": False, "log": "invalid pid/signal"}
    # pid<=1 in os.kill means "the whole group/all processes" or init — strictly forbidden
    if pid <= 1:
        return {"ok": False, "log": "invalid pid"}
    if pid == os.getpid():
        return {"ok": False, "log": "cannot kill the server itself"}
    try:
        os.kill(pid, sig)
        return {"ok": True, "log": f"signal {sig} -> {pid}"}
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
    """Names of services backed by a .timer or .socket — so their "dead" state can
    be shown as "scheduled"/"on demand" rather than as a failure."""
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
        # what "wakes" an inactive unit: a timer on schedule or a socket on demand
        trig = "timer" if parts[0] in timers else ("socket" if parts[0] in sockets else None)
        out.append({"unit": parts[0], "load": parts[1], "active": parts[2],
                    "sub": parts[3], "desc": parts[4] if len(parts) > 4 else "",
                    "trigger": trig})
    return {"units": out, "created": load_created_units()}

def systemd_action(unit, action):
    if action not in ("start", "stop", "restart", "enable", "disable"):
        return {"ok": False, "log": "invalid action"}
    if not re.match(r"^[\w@.:-]+$", unit or ""):
        return {"ok": False, "log": "invalid unit name"}
    return _run(["systemctl", action, unit], timeout=30)

def systemd_journal(unit, lines=200):
    if not re.match(r"^[\w@.:-]+$", unit or ""):
        return {"ok": False, "log": "invalid unit"}
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
        return {"ok": False, "log": "invalid arguments"}
    if n < -20 or n > 19:
        return {"ok": False, "log": "priority out of range -20..19"}
    r = _run(["renice", "-n", str(n), "-p", str(pid)], timeout=10)
    if r["ok"] and not r["log"]:
        r["log"] = f"nice={n} for PID {pid}"
    return r

UNIT_DIR = "/etc/systemd/system"
_UNIT_RE = re.compile(r"^[\w@.\-]+\.(service|timer|socket|mount|path|target)$")

def unit_read(name):
    if not _UNIT_RE.match(name or ""):
        return {"ok": False, "log": "name like name.service"}
    etc = os.path.join(UNIT_DIR, name)
    if os.path.isfile(etc):
        try:
            with open(etc) as f:
                return {"ok": True, "name": name, "path": etc, "editable": True, "base": False, "content": f.read()}
        except OSError as e:
            return {"ok": False, "log": str(e)}
    r = _run(["systemctl", "cat", name], timeout=10)
    return {"ok": True, "name": name, "path": "", "editable": True, "base": True,
            "content": r.get("log") or "# base unit; saving will create an override in " + UNIT_DIR}

def unit_write(name, content, create=False):
    if not _UNIT_RE.match(name or ""):
        return {"ok": False, "log": "name like name.service"}
    path = os.path.join(UNIT_DIR, name)
    if create and os.path.exists(path):
        return {"ok": False, "log": "unit already exists"}
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
        return {"ok": False, "log": "name like name.service"}
    path = os.path.realpath(os.path.join(UNIT_DIR, name))
    if not path.startswith(UNIT_DIR + os.sep):
        return {"ok": False, "log": "outside the units directory"}
    if not os.path.isfile(path):
        return {"ok": False, "log": "this is a base system unit — it cannot be deleted here"}
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
        return {"ok": False, "log": "unknown action"}
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
#  Operation history (uploads, copies, USB import).
#  It used to live only in the browser's localStorage: the phone had its own,
#  empty one. The client posts completed operations here; USB import is written
#  by the server itself, even if the panel is not open in any browser.
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
    """One completed operation. uid — dedup key (panel restart, two browsers)."""
    if not isinstance(e, dict):
        return {"ok": False, "log": "invalid entry"}
    uid = str(e.get("uid") or "")[:80]
    state = e.get("state")
    if not uid or state not in _OPS_STATES:
        return {"ok": False, "log": "invalid entry"}
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
#  MySpeed — widget with the latest internet speed measurement.
#  We fetch the data ourselves: CORS would get in the browser's way, and the
#  password (if enabled) is passed as a header and must not leak into the client.
#  Service absent or silent → {"ok": false}, and the widget hides entirely.
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
    """Last successful measurement + brief statistics. Cached so the widget doesn't hammer the service."""
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
                # MySpeed returns newest first, but there's no reason to rely on the order
                newest = lambda seq: max(seq, key=lambda x: str(x.get("created") or ""),
                                         default=None)
                last = newest(rows)
                # A failed measurement is stored as -1/-1/-1 — these numbers must not
                # be shown. We take the figures from the last successful one and report the failure separately.
                good = newest([x for x in rows if _myspeed_ok(x)]) or {}
                out = {"ok": True, "url": base, "count": len(rows),
                       "download": good.get("download"), "upload": good.get("upload"),
                       "ping": good.get("ping"), "created": good.get("created"),
                       "failed": not _myspeed_ok(last), "failed_at": last.get("created"),
                       "error": last.get("error")}
                try:                       # statistics are optional — the widget lives without them
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
#  Auto-detect the service URL by container name and its INTERNAL port.
#  We read the actual port mapping from live Docker, so the port in compose can
#  be changed freely — the panel always picks up the current one. Container
#  absent or stopped → None, and the caller simply shows nothing (no errors).
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
#  Scrutiny — disk health (device_status), temperature and power-on hours from
#  its collector's data. The key that matches our disks is the serial. Container
#  absent → {ok:False}, and disks work as before, on direct SMART.
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
                        "status": dev.get("device_status", 0),   # 0 = healthy
                        "temp": sm.get("temp"),
                        "power_on_hours": sm.get("power_on_hours"),
                        "uuid": dev.get("scrutiny_uuid") or uuid}
            out = {"ok": True, "url": base, "devices": by_serial}
        except Exception:
            out = {"ok": False}
    with _scrutiny_lock:
        _scrutiny_cache["t"] = time.time(); _scrutiny_cache["data"] = out
    return out

# Human-readable names of key Scrutiny SMART attributes (NVMe + SATA) for the large summary.
_SCR_NAMES = {
    "percentage_used": ("Wear", "%"), "available_spare": ("Spare blocks", "%"),
    "media_errors": ("Media errors", ""), "num_err_log_entries": ("Error log entries", ""),
    "critical_warning": ("Critical warnings", ""), "unsafe_shutdowns": ("Unsafe shutdowns", ""),
    "power_cycles": ("Power cycles", ""), "power_cycle_count": ("Power cycles", ""),
    "reallocated_sector_ct": ("Reallocated sectors", ""), "current_pending_sector": ("Pending sectors", ""),
    "offline_uncorrectable": ("Uncorrectable sectors", ""), "udma_crc_error_count": ("Cable errors (CRC)", ""),
    "data_units_written": ("Written", "TB"), "temperature": ("Temperature", "°C"),
}
_SCR_ORDER = ["percentage_used", "available_spare", "data_units_written", "media_errors",
              "reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable",
              "udma_crc_error_count", "unsafe_shutdowns", "power_cycles", "power_cycle_count"]

# Verdict for each metric (good/warn/bad/info) + a human-readable hint.
# level(value) → level; bare numbers don't read on their own, so we explain.
_SCR_META = {
    "percentage_used": ("SSD write-endurance wear. Fine up to ~80%, closer to 100% — plan a replacement.",
                        lambda v: "good" if v < 70 else "warn" if v < 90 else "bad"),
    "available_spare": ("SSD spare-block reserve. 100% — perfect; dropping toward 10% — alarm.",
                        lambda v: "good" if v > 20 else "warn" if v > 10 else "bad"),
    "media_errors": ("Uncorrectable media errors. Normal — 0.", lambda v: "good" if v == 0 else "bad"),
    "num_err_log_entries": ("Controller error-log entries. Normal — 0.", lambda v: "good" if v == 0 else "warn"),
    "critical_warning": ("NVMe critical warnings. Normal — 0.", lambda v: "good" if v == 0 else "bad"),
    "reallocated_sector_ct": ("Reallocated bad sectors. Normal — 0; growth — surface wear.",
                              lambda v: "good" if v == 0 else "warn"),
    "current_pending_sector": ("Sectors awaiting reallocation. Normal — 0; nonzero — a bad sign.",
                               lambda v: "good" if v == 0 else "bad"),
    "offline_uncorrectable": ("Uncorrectable sectors. Normal — 0.", lambda v: "good" if v == 0 else "bad"),
    "udma_crc_error_count": ("SATA cable transfer errors — usually the cable/contact is at fault, not the disk.",
                             lambda v: "good" if v == 0 else "warn"),
    "unsafe_shutdowns": ("How many times the disk lost power without a proper shutdown. Not a fault, but many — a reason for a UPS.",
                         lambda v: "info"),
    "power_cycles": ("Number of disk power-ons. Informational.", lambda v: "info"),
    "power_cycle_count": ("Number of disk power-ons. Informational.", lambda v: "info"),
    "data_units_written": ("Total written to the disk. Informational (TBW endurance depends on the model).",
                           lambda v: "info"),
    "temperature": ("Current temperature.", lambda v: "good" if v < 60 else "warn" if v < 70 else "bad"),
}

def _scr_verdict(key, value, status):
    """Metric level: first Scrutiny's opinion (the status flag), then our thresholds."""
    hint, lvlfn = _SCR_META.get(key, ("", None))
    if status:                              # Scrutiny itself flagged the attribute as problematic
        return "bad", hint
    if lvlfn is not None and isinstance(value, (int, float)):
        try:
            return lvlfn(value), hint
        except Exception:
            pass
    return "info", hint

def scrutiny_device(serial):
    """Detailed attributes of one disk from Scrutiny: wear, spare, errors, temperature
    history, flagged problem attributes. No Scrutiny/data → {ok:False}."""
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
                val = round(raw * 512000 / 1e12, 2)          # NVMe: units of 512000 bytes → TB
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
#  vnstat — traffic counter for the primary interface (today/month/total).
#  System package; no binary/data → {ok:False}, and the widget hides.
#  vnstat 2.x (json v2) returns rx/tx in BYTES.
# --------------------------------------------------------------------------- #
_vnstat_cache = {"t": 0, "data": {"ok": False}}
_vnstat_lock = threading.Lock()
VNSTAT_TTL = 30

# Physical uplinks (eth0/wlan0/en*), but not docker bridges (br-*), veth*, lo.
# We sum them: when switching cable↔Wi-Fi (netguard) traffic goes now over eth0,
# now over wlan0 — the sum gives a whole picture instead of "losing" history on a link change.
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
#  What's Up Docker (WUD) — how many containers are awaiting an image update.
#  REST /api/containers. No container → {ok:False}, we show only a hint badge.
#  Polling is rare: WUD itself hits the registries on cron (6h), no need to poke more often.
# --------------------------------------------------------------------------- #
_wud_cache = {"t": 0, "data": {"ok": False}}
_wud_lock = threading.Lock()
WUD_TTL = 45      # WUD recomputes "update available" on a docker event instantly;
                  # we keep the cache short so the badge clears quickly, also after external updates

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
    """Drop the updates cache: after a stack/container action (pull/up) the
    "update available" state changes, otherwise the badge would linger until the TTL."""
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
    ("pool",     "Pool",             "Pool"),
    ("backup",   "Backup (all)",     "Backup (all)"),
    ("nbnext",   "Next backup",      "Next backup"),
    ("avail",    "Uptime · 24h",     "Uptime 24h"),
    ("avail30",  "Uptime · 30d",     "Uptime 30d"),
    ("cputemp",  "CPU temp",         "CPU temp"),
    ("disktemp", "Disk temp",        "Disk temp"),
    ("cpu",      "CPU",              "CPU"),
    ("load",     "Load",             "Load"),
    ("ram",      "RAM",              "RAM"),
    ("rootfs",   "System",           "System SSD"),
    ("uptime",   "Booted",           "Booted"),
    ("net",      "Network",          "Network"),
    ("netspeed", "Net speed",        "Net speed"),
    ("inet",     "Internet",         "Internet"),
    ("traffic",  "Traffic",          "Traffic"),
    ("speed",    "Speedtest",        "Speedtest"),
    ("docker",   "Docker",           "Docker"),
    ("wud",      "Images",           "Images"),
    ("updates",  "Updates",          "Updates"),
    ("snapraid", "SnapRAID",         "SnapRAID"),
    ("events",   "Events",           "Events"),
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
                cat.append(("nb:" + pr["id"], "Backup · " + nm, "Backup · " + nm))
    for s in _glance_scripts():
        cat.append((s["id"], s["name"], s["name"]))
    # mounted volumes: free space as a tile ("gauge" view = fill level)
    for d in (_safe(_screen_heavy, {}) or {}).get("disks") or []:
        mts = d.get("mounts") or []
        nm = "System" if "/" in mts else (os.path.basename(mts[0]) if mts else d.get("name") or "?")
        cat.append(("dk:" + (d.get("name") or "?"), "Disk · " + nm, "Disk · " + nm))
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
            st, txt = "warn", "timeout"
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

# ---------------------------------------------------------------------------
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


def load_glance():
    """Open status feed for external displays: just an access token and the
    availability polling interval. Layout/tile selection is done by the device
    itself — the endpoint returns the WHOLE set of metrics (see glance_payload)."""
    d = _json_load_strict(GLANCE_FILE, {})
    return {"enabled": bool(d.get("enabled")),
            "token": d.get("token") or "",
            "ping_interval": int(d.get("ping_interval") or 30)}


def save_glance(d):
    cur = load_glance()
    if "enabled" in d:
        cur["enabled"] = bool(d["enabled"])
    act = d.get("token_action")
    if act == "new":
        cur["token"] = secrets.token_hex(16)
    elif act == "revoke":
        cur["token"] = ""
    pi = d.get("ping_interval")
    if pi in (15, 30, 60, 120):
        cur["ping_interval"] = pi
        # availability check interval = netguard timer interval; the drop-in changes
        # the base 30 s without touching the wizard-managed unit
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

def apply_glance_timer():
    """Re-assert the availability-poll cadence from glance.json at startup. The panel
    writes the systemd timer drop-in in save_glance, but a reinstall restores glance.json
    (via the settings backup) WITHOUT that global /etc override — so the interval would
    silently revert to the wizard's 30 s until the user re-saved. Idempotent."""
    try:
        pi = int(load_glance().get("ping_interval") or 30)
    except (ValueError, TypeError):
        return
    ov = "/etc/systemd/system/nas-netguard.timer.d/override.conf"
    try:
        if pi in (15, 60, 120) and not os.path.isfile(ov):
            os.makedirs(os.path.dirname(ov), exist_ok=True)
            with open(ov, "w") as f:
                f.write("[Timer]\nOnUnitActiveSec=\nOnUnitActiveSec=%ds\n" % pi)
            subprocess.run(["systemctl", "daemon-reload"], timeout=15)
            subprocess.run(["systemctl", "restart", "nas-netguard.timer"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass

def _avail_segments(path=None):
    """RLE journal: "state changed to X at moment T". Lines are read as intervals
    "until the next line", so ORDER MATTERS.

    And the order is not guaranteed: the guard appends the "was off" line with a
    BACKDATED timestamp (beat+30) after boot, and with a stale beat it lands BEFORE
    the last record. A naive read then produced a negative interval (lost) and a
    single three-hour "off" covering a period the journal calls "up": the widget
    tooltip showed two overlapping outages, and an hour for which 2 minutes are
    known was painted fully red.

    We fix it on read: time is monotonic (a record from the past is clamped to the
    previous one), consecutive identical states are collapsed."""
    raw = []
    try:
        with open(path or AVAIL_LOG) as f:
            for ln in f:
                p = ln.split()
                if len(p) == 2 and p[0].isdigit() and p[1] in ("up", "local", "off"):
                    raw.append((int(p[0]), p[1]))
    except OSError:
        return []
    fixed = []
    prev_t = 0
    for t, s in raw:
        t = max(t, prev_t)          # a record "from the past" does not move time backward
        prev_t = t
        fixed.append((t, s))
    segs = []
    for i, (t, s) in enumerate(fixed):
        nxt = fixed[i + 1][0] if i + 1 < len(fixed) else None
        if nxt is not None and nxt <= t:
            continue                # zero length — a clamping artifact, not a period
        if segs and segs[-1][1] == s:
            continue                # same state — a new segment doesn't start
        segs.append((t, s))
    return segs

def avail_bars(hours=24, slots=96, path=None):
    """RLE timeline -> per-slot worst state + uptime %. 2=up 1=local 0=off -1=no data.

    Beside the worst state we return `frac` — the uptime SHARE of each slot (0..1,
    None = no data. Worst-state alone is useless once a slot is a whole day: one
    30-second blip painted the entire day red, so a month of 99.9% uptime looked
    like a month of outages. The share lets the UI grade the colour instead.

    `path` reuses the same parser for other RLE journals (host watch)."""
    segs = _avail_segments(path)
    now = int(time.time())
    start = now - hours * 3600
    rank = {"off": 0, "local": 1, "up": 2}
    bars = [-1] * slots
    slot_w = hours * 3600.0 / slots
    up_s = [0.0] * slots      # per-slot uptime / known time
    kn_s = [0.0] * slots
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
            # the slot fraction covered by this segment: the segment may lie in the
            # slot entirely, start/end inside it, or pass all the way through
            ov = min(b, start + (k + 1) * slot_w) - max(a, start + k * slot_w)
            if ov <= 0:
                continue
            kn_s[k] += ov
            if s == "up":
                up_s[k] += ov
    frac = [round(up_s[k] / kn_s[k], 4) if kn_s[k] > 0 else None for k in range(slots)]
    # cov — what SHARE of the slot is known at all. An hour the journal knows two
    # minutes of must not be painted fully red: 4% uptime out of two known minutes
    # is not "an hour of outage", it's "we know almost nothing about this hour".
    # The client dims the color toward "no data" proportionally to cov.
    cov = [round(min(1.0, kn_s[k] / slot_w), 4) if slot_w else 0 for k in range(slots)]
    pct = round(100.0 * up_t / known_t, 1) if known_t else None
    return {"bars": bars, "frac": frac, "cov": cov, "pct": pct, "hours": hours,
            "start": start, "now": now,
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
    """Short age: '3d'/'3d', '5h'/'5h', '12m'/'12m'."""
    sec = max(0, int(sec))
    if sec >= 172800:
        return "%dd" % (sec // 86400) if not en else "%dd" % (sec // 86400)
    if sec >= 5400:
        return "%dh" % round(sec / 3600) if not en else "%dh" % round(sec / 3600)
    return "%dm" % (sec // 60) if not en else "%dm" % (sec // 60)

def _gl_gb(n, en):
    """Free space as a short number + unit tuple."""
    n = float(n or 0) / 1024 ** 3
    if n >= 1000:
        return ("%.1f" % (n / 1024), "TB" if not en else "TB")
    return ("%d" % n, "GB" if not en else "GB")

def _gl_bytes(n, en):
    """fmt_bytes with latin units for lang=en (TFT default fonts have no cyrillic)."""
    s = fmt_bytes(n)
    if en:
        for a, b in (("KB", "KB"), ("MB", "MB"), ("GB", "GB"), ("TB", "TB"), ("PB", "PB"), ("B", "B")):
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

# --------------------------------------------------------------------------- #
#  Temperature of the PRIMARY-STORAGE disk (the one the "main storage" choice
#  points at — pool or a picked USB disk). Follows storage.json automatically:
#  change the main volume and this tracks the new disk. Cached + backup-aware so
#  we never hammer a USB bridge with smartctl mid-rsync (see the emergency_ro grabli).
# --------------------------------------------------------------------------- #
_MAIN_TEMP = {"t": 0.0, "c": None, "dev": ""}
_MAIN_TEMP_TTL = 120     # same cadence as the disktemp tile — don't wake/poll the disk more often
_MAIN_DEVS = {"t": 0.0, "d": []}
_DIO_CACHE = {}          # per device-set: (time, cumulative sectors r+w) for the throughput delta

def _main_disk_devs():
    """Whole-disk block devices backing storage_root(). Pool → all branches, plain → one.
    Cached 30 s — the primary volume rarely changes, and this runs on every stats() call."""
    now = time.time()
    if now - _MAIN_DEVS["t"] < 30:
        return _MAIN_DEVS["d"]
    out = _main_disk_devs_scan()
    _MAIN_DEVS.update(t=now, d=out)
    return out

def _main_disk_devs_scan():
    root = storage_root()
    if not root:
        return []
    def src_of(path):
        try:
            return subprocess.run(["findmnt", "-nro", "SOURCE", "--target", path],
                                  capture_output=True, text=True, timeout=8).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    raw = []
    if _safe(lambda: storage_state().get("pool")) and root == STORAGE:
        for b in sorted(glob.glob("/mnt/disk*")):     # mergerfs branches
            s = src_of(b)
            if s.startswith("/dev/"):
                raw.append(s)
    if not raw:
        s = src_of(root)
        if s.startswith("/dev/"):
            raw.append(s)
    out = []
    for d in raw:                                     # sdc3 → sdc, nvme0n1p2 → nvme0n1
        m = re.match(r"^(/dev/(?:sd[a-z]+|nvme\d+n\d+|mmcblk\d+))(?:p?\d+)?$", d)
        base = m.group(1) if m else d
        if base not in out:
            out.append(base)
    return out

def _main_disk_temp():
    """(°C or None, short label). Cached; prefers hwmon (no disk wake); smartctl only
    when no backup is running. Pool → the hottest branch."""
    now = time.time()
    if now - _MAIN_TEMP["t"] < _MAIN_TEMP_TTL:
        return _MAIN_TEMP["c"], _MAIN_TEMP["dev"]
    devs = _main_disk_devs()
    if not devs:
        _MAIN_TEMP.update(t=now, c=None, dev="")
        return None, ""
    label = os.path.basename(devs[0]) if len(devs) == 1 else "pool"
    hw = {dv: t for dv, t in _hwmon_disk_temps()}
    temps = [hw[os.path.basename(d)] for d in devs if os.path.basename(d) in hw]
    if len(temps) < len(devs) and not nb_any_active():
        for d in devs:
            if os.path.basename(d) in hw:
                continue
            try:
                c = (_smartctl_json(["-n", "standby", "-A"], d, timeout=8).get("temperature") or {}).get("current")
                if isinstance(c, (int, float)) and c > 0:
                    temps.append(int(c))
            except Exception:
                pass
    c = max(temps) if temps else _MAIN_TEMP["c"]      # keep last known if this pass read nothing
    _MAIN_TEMP.update(t=now, c=c, dev=label)
    return c, label

def disk_io_rate(devs):
    """Read+write throughput (bytes/s) across the primary-storage disk(s), as a delta
    since the last call (same idea as net_rate). Pool → summed over branches."""
    if not devs:
        return 0
    key = ",".join(sorted(devs))
    tot = 0
    for d in devs:
        p = _read("/sys/block/%s/stat" % os.path.basename(d)).split()
        if len(p) >= 7:
            try:
                tot += int(p[2]) + int(p[6])          # sectors read + written (512 B each)
            except ValueError:
                pass
    now = time.time()
    prev = _DIO_CACHE.get(key)
    _DIO_CACHE[key] = (now, tot)
    if not prev:
        return 0
    dt = now - prev[0] or 1
    return max(0, int((tot - prev[1]) * 512 / dt))

def _nb_last_ok(pid):
    """Timestamp of the profile's last completed run (ok or warn), 0 if none."""
    for h in nb_history(pid):
        if h.get("result") in ("ok", "warn"):
            return h.get("ts", 0)
    return 0

def _gl_backup_tile(best, en):
    if not best:
        return {"value": "—", "unit": "", "state": "warn",
                "note": "never ran" if not en else "never ran", "raw": None}
    age = time.time() - best
    st = "danger" if age > 7 * 86400 else ("warn" if age > 2 * 86400 else "ok")
    return {"value": _gl_ago(age, en), "unit": "ago" if not en else "ago", "state": st,
            "raw": {"ts": int(best), "age_s": int(age)}}

# The cost of EACH response field is multiplied by the polling frequency (a trap from CLAUDE.md).
# glance is polled by the constructor (?all=1, live values) and ESP32 displays every
# 3 s — and the "updates" tile honestly ran apt-get -s upgrade ON EVERY poll:
# five parallel apt caught at load 14. Each source has its own TTL.
GLANCE_TILE_TTL = {"updates": 300, "disktemp": 120, "snapraid": 120, "inet": 60,
                   "docker": 15, "pool": 10, "rootfs": 10, "uptime": 5}
_GL_TILES = {}
_GL_TILES_LOCK = threading.Lock()


def _glance_tile_cached(tid, en):
    key = (tid, en)
    ttl = GLANCE_TILE_TTL.get(tid.split(":")[0] if ":" in tid else tid, 2)
    now = time.time()
    with _GL_TILES_LOCK:
        c = _GL_TILES.get(key)
        if c and now - c[0] < ttl:
            return c[1]
    d = _safe(lambda: _glance_tile(tid, en))
    with _GL_TILES_LOCK:
        _GL_TILES[key] = (now, d)
    return d


def _glance_tile(tid, en):
    """Build one tile -> {value, unit, state, raw[, note]} or None to hide it.
    value/unit are display-ready strings; raw is the machine-readable source
    for anyone building their own UI on top of /api/glance."""
    if tid == "pool":
        di = disk_info(STORAGE) if os.path.ismount(STORAGE) else None
        if not di:
            return {"value": "—", "unit": "", "state": "danger",
                    "note": "pool not mounted" if not en else "pool not mounted", "raw": None}
        v, u = _gl_gb(di["free"], en)
        st = "danger" if di["pct"] >= 90 else ("warn" if di["pct"] >= 80 else "ok")
        return {"value": v, "unit": u + (" free" if not en else " free"), "state": st,
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
                "unit": "until run" if not en else "until run", "state": "ok", "note": name,
                "raw": {"ts": int(best), "in_s": int(best - time.time()), "profile": name}}
    if tid.startswith("sc:"):
        s = next((x for x in _glance_scripts() if x["id"] == tid), None)
        if not s:
            return None
        fall = {"ok": "OK", "warn": "WARN", "danger": "FAIL"}[s["state"]]
        return {"value": s["text"] or fall, "unit": "", "state": s["state"],
                "raw": {"state": s["state"], "text": s["text"]}}
    if tid == "bright":
        # brightness slider: the value lives ON the device, the server only provides the tile
        return {"value": "", "unit": "", "state": "ok", "raw": {"local": True}}
    if tid.startswith("dk:"):
        d0 = next((x for x in (_safe(_screen_heavy, {}) or {}).get("disks") or []
                   if x.get("name") == tid[3:]), None)
        if not d0 or d0.get("used_pct") is None:
            return None
        pct = round(d0["used_pct"])
        st = "danger" if pct >= 95 else ("warn" if pct >= 90 else "ok")
        return {"value": _fmt_b(d0.get("free") or 0), "unit": "free" if en else "free",
                "state": st, "note": (d0.get("model") or d0.get("name") or ""),
                "raw": {"pct": pct, "free": d0.get("free"), "used": d0.get("used"),
                        "size": d0.get("size"), "temp": d0.get("temp")}}
    if tid in ("avail", "avail30"):
        hours = 24 if tid == "avail" else 720
        av = avail_bars(hours, 48 if tid == "avail" else 30)
        if av["pct"] is None:
            return None
        st = "ok" if av["pct"] >= 99 else ("warn" if av["pct"] >= 95 else "danger")
        unit = ("% / 24h" if not en else "% / 24h") if tid == "avail" else \
               ("% / 30d" if not en else "% / 30d")
        # bars in raw: the availability tile can be placed/stretched like a normal one,
        # and the device draws uptime-kuma bars for it (bars view)
        return {"value": "%.1f" % av["pct"], "unit": unit, "state": st,
                "raw": {"pct": av["pct"], "hours": hours,
                        "bars": av.get("bars") or [], "frac": av.get("frac") or []}}
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
        # rxh/txh — ready-made strings for a two-line render on the device
        # (download in green, upload in red, without arrow icons)
        if load_settings().get("netUnits") == "bits":  # panel-wide unit choice
            def _mb(x):
                x = (x or 0) * 8 / 1e6
                return "%d" % x if x >= 100 else "%.1f" % x
            u = "Mbit/s" if not en else "Mbit/s"
            raw["rxh"] = _mb(raw["rx"]) + " " + u
            raw["txh"] = _mb(raw["tx"]) + " " + u
            return {"value": "↓%s ↑%s" % (_mb(raw["rx"]), _mb(raw["tx"])),
                    "unit": u, "state": "ok", "raw": raw}
        us = "/s" if not en else "/s"
        raw["rxh"] = _gl_bytes(raw["rx"], en) + us
        raw["txh"] = _gl_bytes(raw["tx"], en) + us
        return {"value": "↓%s ↑%s" % (_gl_bytes(raw["rx"], en), _gl_bytes(raw["tx"], en)),
                "unit": us, "state": "ok", "raw": raw}
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
        return {"value": v, "unit": u + (" free" if not en else " free"), "state": st,
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
        return {"value": ("up" if not en else "up") if ok else ("down" if not en else "down"),
                "unit": "", "state": "ok" if ok else "danger", "raw": {"ok": ok}}
    if tid == "traffic":
        v = _safe(vnstat_state) or {}
        if not v.get("ok"):
            return None
        td = v.get("today") or {}
        return {"value": "↓%s ↑%s" % (_gl_bytes(td.get("rx"), en).replace(" ", ""),
                                       _gl_bytes(td.get("tx"), en).replace(" ", "")),
                "unit": "today" if not en else "today", "state": "ok",
                "raw": {"rx": td.get("rx", 0), "tx": td.get("tx", 0)}}
    if tid == "speed":
        m = _safe(myspeed_state) or {}
        if not m.get("ok") or m.get("download") is None:
            return None
        return {"value": "↓%s ↑%s" % (m.get("download"), m.get("upload")),
                "unit": "Mbit" if not en else "Mbit",
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
        return {"value": str(n), "unit": "upd" if not en else "upd",
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
        return {"value": str(unseen), "unit": "new" if not en else "new", "state": "ok",
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
        d = _glance_tile_cached(tid, en)
        if d:
            out.append(dict(d, id=tid, label=(en_l if en else ru)))
    _GL_PAL_CACHE[lang] = {"t": time.time(), "data": out}
    return out

_TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
             "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
             "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
             "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
             "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


_TR_SYM = {"↑": "^", "↓": "v", "→": ">", "←": "<", "—": "-", "–": "-", "…": "..."}


def _translit(s):
    """Cyrillic -> Latin for displays whose fonts are Latin-1-only. Arrows and
    dashes map to ASCII lookalikes; Latin-1 itself (°, ·, ±) passes through —
    the device fonts have those glyphs."""
    out = []
    for ch in str(s):
        lo = ch.lower()
        if lo in _TRANSLIT:
            t = _TRANSLIT[lo]
            out.append(t.capitalize() if ch.isupper() else t)
        elif ch in _TR_SYM:
            out.append(_TR_SYM[ch])
        elif ord(ch) < 0x100:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def glance_payload(lang="ru", screen=""):
    """Open status feed: ALL available tiles as one flat list (a metric may
    disappear — then it's simply gone), plus availability 24h/30d, status colors
    and an overall verdict. Layout is done by the device. Cache a couple of seconds,
    seq grows only on change (the screen isn't redrawn for nothing)."""
    en = (lang == "en")
    cfg = load_glance()
    with _GL_LOCK:
        c = _GL_CACHE["langs"].get(lang)
        if c and time.time() - c["t"] < 3:
            return c["payload"]
    labels = {t[0]: (t[2] if en else t[1]) for t in glance_catalog()}
    tiles, problems = [], []
    for tid in labels:
        d = _glance_tile_cached(tid, en)
        if not d:
            continue
        d = dict(d, id=tid, label=_translit(labels[tid]) if en else labels[tid])
        if en:
            if d.get("note"):
                d["note"] = _translit(d["note"])
            if d.get("value") and not str(d["value"]).isascii():
                d["value"] = _translit(str(d["value"]))
        if tid in GLANCE_SPARKS:
            sp = _safe(lambda t=tid: _gl_spark(GLANCE_SPARKS[t]))
            if sp:
                d["spark"] = sp
        tiles.append(d)
        if d["state"] != "ok":
            problems.append("%s: %s %s" % (d["label"], d["value"], d.get("note") or d.get("unit") or ""))
    status = "ok"
    if any(t["state"] == "danger" for t in tiles):
        status = "danger"
    elif any(t["state"] == "warn" for t in tiles):
        status = "warn"
    av = avail_bars(24, 96)
    av30 = avail_bars(720, 30)
    ds0 = _safe(load_settings, {}) or {}
    dk0 = (ds0.get("themeProfiles") or {}).get("dark") or {}
    def _hue(k, dflt):
        v = dk0.get(k) or ds0.get(k) or dflt
        return v if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v) else dflt
    colors = {"ok": _hue("goodHex", "#1FA971"), "warn": _hue("warnHex", "#CF881B"),
              "danger": _hue("dangerHex", "#DE4E48"), "accent": _hue("accentHex", "#12B0A6")}
    payload = {"v": 3, "host": socket.gethostname(), "status": status,
               "problems": problems[:6], "tiles": tiles, "colors": colors,
               "avail": {"bars": av["bars"], "pct24": av["pct"]},
               "avail30": {"bars": av30["bars"], "pct": av30["pct"]},
               "ts": int(time.time())}
    sig = json.dumps([tiles, problems, status, av["bars"], av30["bars"], colors],
                     sort_keys=True, ensure_ascii=False)
    with _GL_LOCK:
        c = _GL_CACHE["langs"].setdefault(lang, {"t": 0, "sig": "", "seq": 0, "payload": None})
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
        # notes are not a backup: we must not lose them because a disk was pulled, so with
        # storage NOT mounted we honestly fall back to the home folder, not its mountpoint
        root = os.path.join(storage_root(), "notes") if storage_root() \
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
        raise ValueError("path outside the notes folder")
    return p

_NOTE_FM = re.compile(r"^---\n(.*?)\n---\n?", re.S)
# HTML notes can't carry a bare "---" front-matter (it would render as text when
# the file is opened directly as a page), so their metadata lives in a leading
# HTML comment. Both forms parse to the same title/tags/updated/pinned dict.
_NOTE_FM_HTML = re.compile(r"^<!--nas-note\n(.*?)\n-->\n?", re.S)

def _note_kind(rel):
    return "html" if str(rel or "").lower().endswith(".html") else "md"

def _note_parse(text):
    meta, body = {}, text
    m = _NOTE_FM.match(text) or _NOTE_FM_HTML.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    tags = [t.strip().lstrip("#") for t in (meta.get("tags") or "").split(",") if t.strip()]
    return meta.get("title") or "", tags, body

def _note_dump(title, tags, body, pinned=False, kind="md"):
    head = "title: %s\ntags: %s\nupdated: %s%s" % (
        str(title).replace("\n", " "), ", ".join(tags),
        time.strftime("%Y-%m-%d %H:%M"),
        "\npinned: 1" if pinned else "")
    if kind == "html":
        return "<!--nas-note\n%s\n-->\n%s" % (head, body)
    return "---\n%s\n---\n%s" % (head, body)

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
            lf = f.lower()
            if not (lf.endswith(".md") or lf.endswith(".html")):
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
                          "title": title or os.path.splitext(f)[0], "tags": tags, "prev": prev,
                          "kind": "html" if lf.endswith(".html") else "md",
                          "pinned": _note_meta(head).get("pinned") == "1",
                          "mtime": int(st.st_mtime), "size": st.st_size})
    return {"root": root, "dirs": dirs, "notes": notes, "stats": stats}

def _note_meta(text):
    m = _NOTE_FM.match(text) or _NOTE_FM_HTML.match(text)
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
        raise ValueError("bad version")
    fp = os.path.join(_nt_hist_dir(rel), ver)
    if not os.path.isfile(fp):
        raise ValueError("no such version")
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
        raise ValueError("not the trash")
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
        rl = rel.lower()
        if not (rl.endswith(".md") or rl.endswith(".html")) or os.path.isfile(os.path.join(root, rel)):
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
            lf = f.lower()
            if lf.endswith(".md") or lf.endswith(".html"):
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
        raise ValueError("no such note")
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    title, tags, body = _note_parse(text)
    return {"path": rel.strip("/"), "title": title, "tags": tags, "md": body,
            "kind": _note_kind(rel),
            "pinned": _note_meta(text).get("pinned") == "1",
            "mtime": int(os.stat(p).st_mtime)}

def _note_slug(name):
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "", str(name or "").strip())
    return name[:80] or "Note"

def note_save(rel, title, tags, md, pinned=False, base_mtime=0, force=False, conflict_copy=False):
    p = _notes_abs(rel)
    lp = p.lower()
    if not (lp.endswith(".md") or lp.endswith(".html")):
        raise ValueError("not a note file")
    kind = _note_kind(rel)
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
        word = "conflict" if (load_settings().get("lang") or "en") == "ru" else "conflict"
        root, ext = os.path.splitext(p)          # keep the note's own extension (.md/.html)
        stem = root + " (" + word + time.strftime(" %Y-%m-%d %H-%M") + ")"
        cp, i = stem + ext, 1
        while os.path.exists(cp):
            i += 1
            cp = "%s %d%s" % (stem, i, ext)
        out["conflict_copy"] = os.path.basename(cp)
        rel, p = None, cp
    if rel:
        _safe(lambda: _nt_snapshot(rel))            # history before overwriting
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tags = [_note_slug(t) for t in (tags or []) if str(t).strip()][:20]
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_note_dump(title or "", tags, str(md or ""), pinned, kind))
    os.replace(tmp, p)
    _chown_user(p)
    out["mtime"] = int(os.stat(p).st_mtime)
    return out

def note_new(folder, title, kind="md"):
    ext = ".html" if kind == "html" else ".md"
    lang = load_settings().get("lang") or "en"
    base = _note_slug(title or ("New note" if lang == "ru" else "New note"))
    d = _notes_abs(folder)
    os.makedirs(d, exist_ok=True)
    _chown_user(d)
    name, i = base, 1
    while os.path.exists(os.path.join(d, name + ext)):
        i += 1
        name = "%s %d" % (base, i)
    rel = ((folder.strip("/") + "/") if (folder or "").strip("/") else "") + name + ext
    # title = deduped file name ("New note 2"), so duplicates are tellable apart
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
        raise ValueError("no source")
    if os.path.exists(dst):
        raise ValueError("that name is already taken")
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
        raise ValueError("no such path")
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
        raise ValueError("file larger than 30 MB")
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
        if n.get("kind") == "html":     # search/snippet on rendered text, not the tags
            text = _htmllib.unescape(re.sub(r"<[^>]*>", " ", text))
            text = " ".join(text.split())
        low = text.lower()
        if q in low or q in n["title"].lower() or any(q in t.lower() for t in n["tags"]):
            i = low.find(q)
            out.append(dict(n, snip=text[max(0, i - 40):i + 80].replace("\n", " ") if i >= 0 else ""))
        if len(out) >= 60:
            break
    return {"hits": out}

def notes_migrate(new_root):
    """Move the whole notes tree to another folder (Settings → Notes)."""
    old = notes_root()
    new_root = (new_root or "").strip()
    if not new_root.startswith("/"):
        raise ValueError("an absolute path is required")
    new = os.path.realpath(new_root)
    if new == old:
        return {"ok": True, "log": "already there", "root": new}
    if old.startswith(new + os.sep) or new.startswith(old + os.sep):
        raise ValueError("folders are nested in each other")
    os.makedirs(new, exist_ok=True)
    _chown_user(new)
    moved = 0
    for name in os.listdir(old):
        if os.path.exists(os.path.join(new, name)):
            raise ValueError("the new folder already contains \"%s\"" % name)
        shutil.move(os.path.join(old, name), os.path.join(new, name))
        moved += 1
    cur = load_notes_conf()
    cur["root"] = new
    _json_save(NOTES_CONF, cur, indent=2)
    return {"ok": True, "log": "objects moved: %d" % moved, "root": new}

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
    # MERGE, not overwrite: a partial update (e.g. {lang} from the wizard)
    # must not wipe the other settings
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
        # no pool means no pool stats either (it used to silently substitute the
        # system card, and "pool usage" showed nonsense)
        "disk_pool": disk_info(STORAGE) if os.path.ismount(STORAGE) else None,
        "disk_root": disk_info("/"),
        "net": net_rate(iface),
        "dio": _safe(lambda: disk_io_rate(_main_disk_devs()), 0),   # primary-storage disk throughput B/s
        "iface": iface,
        "uptime": uptime_s(),
        "load": list(os.getloadavg()),
        # is any backup running (dock icon indicator). nb_run_active also verifies
        # the transient unit is alive, so a stale "running" flag left by a power
        # loss can't spin the icon forever; systemctl is only called while the
        # state flag is set, so the idle path stays cheap.
        "nb_running": any(nb_run_active(p["id"]) for p in nb_profiles()),
        # USB import runs in the background (udev), the panel may have been closed — show it
        # in the shared operations center, not just on the USB settings tab
        "usb_import": _safe(lambda: usb_import_progress()["jobs"], []),
        "ts": int(time.time()),
    }

# --------------------------------------------------------------------------- #
#  Monitor notifications (temp / throttle / pool → Pushover)
# --------------------------------------------------------------------------- #
MONITOR_FILE = os.path.join(NAS_CONFIG, "monitor.json")
NOTIFY_CONF = "/etc/nas-wizard/notify.conf"
_MON_LAST = {}
_MON_BOOT_SENT = False
_MON_SMART_LAST = 0
_MON_DEVS = None      # set of volumes with a FS on the previous tick (for disk_add/disk_remove)
_MON_IP = None        # last known local IP (for ip_changed)
_MON_IFACE = None     # active default interface (for link_changed)
_MON_HEAT = 0         # counter of consecutive "hot" ticks (for sustained_heat)
_MON_HOURLY = {}      # key → time of the last "hourly" check (updates, docker_space, …)
_MON_WEEKLY = time.time()   # time of the last weekly report (don't send right after a restart)
_MON_DISKSTAT = None  # previous /proc/diskstats snapshot (for slow_disk)
_MON_HOG = {}         # pid → how many consecutive ticks the process has been hogging resources
_MON_USBIMP = time.time()   # don't replay old import-log entries
_KNOWN_IPS_FILE = os.path.join(NAS_CONFIG, "known-ips.json")

# Event catalog: on (default), priority (Pushover: -2 quiet … 2 emergency), threshold.
# priority hint: 2 = data-loss risk (requires acknowledgement), 1 = important/urgent,
# 0 = normal, -1 = informational (no sound), -2 = badge only.
def _def_monitor():
    return {"enabled": False, "cooldown": 1800, "events": {
        # --- disks: attach/detach/mode ---
        "disk_add":    {"on": True,  "priority": 0},
        "disk_remove": {"on": True,  "priority": 1},
        "readonly":    {"on": True,  "priority": 2},
        "fserror":     {"on": True,  "priority": 1},
        # --- disk health (SMART, every 10 min) ---
        "smart":       {"on": True,  "priority": 2},
        "smart_wear":  {"on": True,  "priority": 1, "threshold": 1},
        "disktemp":    {"on": True,  "priority": 1, "threshold": 60},
        # --- space ---
        "pool":        {"on": True,  "priority": 0, "threshold": 90},
        "diskfull":    {"on": True,  "priority": 0, "threshold": 90},
        # --- Pi: power/temperature/resources ---
        "temp":        {"on": True,  "priority": 1, "threshold": 75},
        "throttle":    {"on": True,  "priority": 1},
        "undervolt":   {"on": True,  "priority": 2},
        "cfg_corrupt": {"on": True,  "priority": 1},
        "mem":         {"on": False, "priority": 0, "threshold": 92},
        "swap":        {"on": False, "priority": 0, "threshold": 60},
        "load":        {"on": False, "priority": 0, "threshold": 8},
        # --- services and containers ---
        "svcfail":     {"on": True,  "priority": 1},
        "container":   {"on": True,  "priority": 0},
        "container_loop": {"on": True, "priority": 1},
        "docker_space":{"on": False, "priority": 0, "threshold": 20},
        # --- access (panel login / SSH) ---
        "panel_new":   {"on": True,  "priority": 1},
        "panel_fail":  {"on": True,  "priority": 1, "threshold": 5},
        "ssh_login":   {"on": False, "priority": 0},
        # --- network ---
        "ip_changed":  {"on": True,  "priority": 0},
        "link_changed":{"on": True,  "priority": 0},
        "vpn_offline": {"on": True,  "priority": 1},
        # --- data protection (SnapRAID / mergerfs / backup) ---
        "snap_ok":     {"on": False, "priority": -1},
        "snap_err":    {"on": True,  "priority": 1},
        "scrub_err":   {"on": True,  "priority": 2},
        "delete_block":{"on": True,  "priority": 1},
        "backup":      {"on": False, "priority": 0},
        "mergerfs":    {"on": True,  "priority": 1},
        # --- file history (fswatch); priority 2 = Pushover emergency (retries
        #     every minute until acknowledged) — not used by default ---
        "fsw_corrupt": {"on": True,  "priority": 1},
        "fsw_guard":   {"on": True,  "priority": 1},
        "fsw_root":    {"on": True,  "priority": 1},
        "fsw_del":     {"on": True,  "priority": 0, "threshold": 50},
        "fsw_scan":    {"on": True,  "priority": -1},
        # --- maintenance ---
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
        # --- behavioral ---
        "traffic":     {"on": False, "priority": 0, "threshold": 50},
        "slow_disk":   {"on": False, "priority": 0, "threshold": 250},
        "proc_hog":    {"on": False, "priority": 0, "threshold": 80},
        "inodes":      {"on": True,  "priority": 1, "threshold": 90},
        "boot":        {"on": False, "priority": -1},
        # --- USB auto-import (SD/flash copy result; Pushover off — the import
        #     script can send on its own, to avoid duplicates) ---
        "usb_import":  {"on": False, "priority": 0, "desk": True},
        # --- backup of the main NAS onto this NAS (run result) ---
        "nas_backup":  {"on": True, "priority": 0, "desk": True},
        # --- backup health (periodic checks, every 30 min) ---
        "nb_conn":     {"on": True,  "priority": 1, "desk": True},
        "nb_srcmiss":  {"on": False, "priority": 1, "desk": True},
        "nb_stale":    {"on": True,  "priority": 1, "threshold": 7,  "desk": True},
        "nb_size":     {"on": False, "priority": 0, "threshold": 40, "desk": True},
        "nb_dest":     {"on": True,  "priority": 1, "threshold": 95, "desk": True},
        "nb_guard":    {"on": True,  "priority": 2, "desk": True},   # --max-delete guard fired
        "nb_verify":   {"on": True,  "priority": 1, "desk": True},   # checksum verification found mismatches
        # --- reliability: disk reconnected on its own (auto-mount) ---
        "disk_remount":{"on": True, "priority": 0, "desk": True},
        # --- active thermal protection (warning/action) ---
        "thermal_guard":{"on": True, "priority": 1, "desk": True},
        # --- daily/weekly status summary ---
        "daily_summary":{"on": True, "priority": -1, "desk": False},
    }}

def _monitor_defaults_desk(d):
    """desk = show as a card on the desktop; by default — everything important (priority>=1)."""
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
#  tr() once translated server-built strings (Pushover, screen labels) through the
#  web/i18n.js dictionary. The UI is English-only now, so this is an identity pass
#  kept only so existing call sites stay valid.
# --------------------------------------------------------------------------- #
def tr(text, lang=None):
    return text  # UI is English-only; runtime i18n layer removed. Identity pass for call-site compat.

def push_notify(title, msg, priority=0):
    title = tr(title); msg = tr(msg)      # Pushover goes around the client — translate here
    try:
        priority = max(-2, min(2, int(priority)))
    except (ValueError, TypeError):
        priority = 0
    n = load_notify()
    if n["user"] and n["token"]:
        try:
            body = {"token": n["token"], "user": n["user"], "title": title,
                    "message": msg, "priority": priority}
            if priority == 2:            # emergency: Pushover requires retry/expire + acknowledgement
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
#  Event log (notification center): everything important — monitor events, user
#  actions, errors — is written here and kept for ~a month. Pushover/desktop are
#  just delivery methods, the log is always kept.
# --------------------------------------------------------------------------- #
EVENTS_FILE = os.path.join(NAS_CONFIG, "events.json")
EVENTS_CAP  = 3000
EVENTS_DAYS = 31
_events = None
_events_lock = threading.Lock()
# Condition on the same lock: long-poll /api/events sleeps on it, log_event wakes it —
# the panel learns of an event instantly, not on the next poll.
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

# catalog event category → log section (for filters in the notifications window)
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
    """Write an event to the log. lvl: info|ok|warn|crit. desk=None → from the
    event settings (whether to show as a card on the desktop)."""
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
        # dedup: the same non-critical event with the same title within 4 h → counter ×N.
        # We merge ONLY unread entries: if the user has already read the old
        # one, a repeat must create a new one (otherwise the badge/card won't come alive).
        # Critical ones are always added anew — that is the periodic reminder.
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
        _events_cond.notify_all()      # wake long-poll waiters on /api/events
        return ev["seq"]

def events_list(after=0, limit=400, wait=0):
    """wait>0 (sec) — long-poll: if there are no new events, hold the request up to wait
    seconds until log_event wakes it. This way the panel reacts instantly, not
    waiting for the next poll. We cap wait below the handler socket timeout."""
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
    with _events_cond:                          # same lock as _events_lock
        while True:
            ev = _events_load()
            # return immediately: there's something new, or this is a normal poll (not long-poll)
            if ev["seq"] > after or not after or wait <= 0:
                return snapshot(ev)
            remain = deadline - time.time()
            if remain <= 0:
                return snapshot(ev)             # timeout — empty response with the current seq
            _events_cond.wait(min(remain, 10))  # sleep; log_event wakes us sooner

def events_seen(eid):
    try:
        eid = int(eid)
    except (ValueError, TypeError):
        return {"ok": False, "log": "bad id"}
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

# --- logging of user actions (called from do_POST) ------------
_SYSD_RU = {"start": "start", "stop": "stop", "restart": "restart",
            "enable": "autostart on", "disable": "autostart off", "reload": "reload"}

def _act_title(p, b):
    """Log entry title for the action, or None if the action isn't logged."""
    g = lambda k: str(b.get(k) or "")
    if p == "/api/power":
        return {"reboot": "Reboot by command from the panel",
                "poweroff": "Shutdown by command from the panel"}.get(g("action"))
    if p == "/api/systemd":
        return "Service %s: %s" % (g("unit"), _SYSD_RU.get(g("action"), g("action")))
    if p == "/api/stack/action":
        return "Stack %s: %s" % (g("name"), g("action"))
    if p == "/api/container/action":
        return "Container %s: %s" % (g("id")[:12], g("action"))
    if p == "/api/docker/prune":
        return "Docker: prune (%s)" % g("what")
    if p == "/api/disk/format":
        return None if b.get("dry") else "Formatting %s → %s (%s)" % (g("dev"), g("fs") or "ext4", g("role") or "data")
    if p == "/api/disk/eject":
        return "Ejected disk %s" % g("dev")
    if p == "/api/disk/mount":
        return ("Unmounted: %s" if b.get("unmount") else "Mounted: %s") % g("target")
    if p == "/api/disk/mount-dev":
        return "Mounting disk %s" % g("dev")
    if p == "/api/disk/label":
        return "Disk label %s → %s" % (g("dev"), g("label"))
    if p == "/api/disk/spindown":
        m = b.get("minutes") or 0
        return "Disk sleep %s: %s" % (g("dev"), ("%s min" % m) if m else "off")
    if p == "/api/disk/smart-test":
        return "SMART test %s (%s)" % (g("dev"), g("kind") or "short")
    if p == "/api/fs/trash/empty":
        return "Trash emptied"
    if p == "/api/usb-import/run":
        return "USB import started manually (%s)" % g("dev")
    if p == "/api/usb-import":
        return "USB import settings changed"
    if p == "/api/motd":
        return "SSH greeting changed"
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
    log_event("action", title + ("" if ok else " — error"), msg,
              "ok" if ok else "warn", kind=kind, desk=False)

def _phys_devs():
    try:
        return ["/dev/" + d for d in os.listdir("/dev") if re.match(r"^(sd[a-z]|nvme\d+n\d+)$", d)]
    except OSError:
        return []

def _smart_scan():
    """One smartctl pass over all physical disks → dict with health/wear/temp."""
    res = {}
    for dev in _phys_devs():
        j = _smartctl_json(["-n", "standby", "-H", "-A"], dev, timeout=15)   # don't wake sleeping disks
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
        if isinstance(temp, int) and temp > 200:      # some firmwares put garbage in raw
            temp = temp & 0xff
        res[dev] = {"passed": passed, "realloc": realloc, "pending": pending, "temp": temp}
    return res

def _block_volumes():
    """Devices with a filesystem: path → label (for attached/detached events)."""
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
    """Paths of removable media (flash drives, SD) together with partitions: partitions
    inherit the parent's rm flag. USB-SATA bridges with permanent disks do NOT land here —
    they have rm=0, and alerts for them must fire."""
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
    """Mounted ro that looks like a FS failure. We exclude removable media:
    a flash drive/card brought up read-only by automount (dirty ext4, vfat with
    an error) is routine, not a NAS failure."""
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
        if mp.startswith(amb) or src in rem:      # removable — not our concern
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
        # 0/143(SIGTERM)/137(SIGKILL) — normal stop (docker stop), not a failure
        m = re.search(r"Exited \((\d+)\)", status)
        if state == "exited" and m and int(m.group(1)) not in (0, 143, 137):
            bad.append("%s (crashed, code %s)" % (name, m.group(1)))
        elif "unhealthy" in status.lower():
            bad.append("%s (unhealthy)" % name)
    return bad

# --------------------------------------------------------------------------- #
#  Unified sending of a notification event (checks on/cooldown/priority).
#  Called both from monitor_tick and from the panel-login hook.
# --------------------------------------------------------------------------- #
def _safe(fn, default=None):
    """Call a detector, swallowing the exception — one failing detector must not
    stop the whole monitor_tick."""
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
        log_event(ev_name, title, msg)      # log — always
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
    """Journal lines within the interval containing any of the patterns (case-insensitive)."""
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
    """Parse the tail of /var/log/snapraid.log (NASRESULT markers from the wrapper)."""
    log = _read("/var/log/snapraid.log")
    if not log:
        return None
    tail = log.splitlines()[-80:]
    ev = {}
    for l in tail:
        if "ABORT: files deleted" in l:
            ev["delete_blocked"] = l.strip()[-160:]
        mm = re.search(r"(\d+) errors", l)          # scrub: error counter > 0
        if (mm and int(mm.group(1)) > 0) or "silent error" in l.lower():
            ev["scrub_err"] = l.strip()[-160:]
    for l in reversed(tail):                          # last sync result
        if "NASRESULT sync ok" in l:
            ev.setdefault("sync_ok", l.replace("NASRESULT ", "").strip()); break
        if "NASRESULT sync err" in l:
            ev.setdefault("sync_err", l.replace("NASRESULT ", "").strip()); break
    return ev or None

def _usb_import_events(since):
    """New USB-import log entries (/var/log/nas-usb-import.log) after the since mark.
    → ([(lvl, title, msg, ts), ...], new mark)."""
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
            out.append(("ok", "USB import finished", "Copied to " + rest.split("->", 1)[1].strip(), ts))
        elif rest.startswith("import FAIL"):
            out.append(("warn", "USB import: error", "Failed to copy (" + rest[len("import FAIL"):].strip() + ") — details in /var/log/nas-usb-import.log", ts))
        elif rest.startswith("import ") and "->" in rest:
            out.append(("info", "USB import started", rest[len("import "):], ts))
    return out, latest

def _mergerfs_missing():
    """Data disks from fstab that are NOT currently mounted (a branch dropped out of the pool)."""
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

_APT_CACHE = {"t": 0.0, "d": None}
_APT_LOCK = threading.Lock()


def apt_updates(refresh=False):
    """List of packages available to upgrade: [{name, cur, new, security}].

    5-minute cache + single entry: apt-get -s upgrade costs ~a second and holds
    ~130 MB, and it was called from everywhere (glance tiles, screen, panel)."""
    with _APT_LOCK:
        if not refresh and _APT_CACHE["d"] is not None \
                and time.time() - _APT_CACHE["t"] < 300:
            return _APT_CACHE["d"]
        return _apt_updates_run(refresh)


def _apt_updates_run(refresh=False):
    if refresh:
        _run(["apt-get", "update"], timeout=180)
    r = _run(["apt-get", "-s", "-o", "Debug::NoLocking=true", "upgrade"], timeout=60)
    pkgs = []
    for l in (r.get("log") or "").splitlines():
        # format: Inst bash [5.2.15-2] (5.2.15-3 Debian:12/stable [arm64])
        m = re.match(r"^Inst (\S+) \[([^\]]*)\] \((\S+)\s+([^)]*)\)", l)
        if m:
            src = m.group(4)
            pkgs.append({"name": m.group(1), "cur": m.group(2), "new": m.group(3),
                         "security": "security" in src.lower()})
    pkgs.sort(key=lambda p: (not p["security"], p["name"]))
    res = {"ok": True, "count": len(pkgs), "packages": pkgs}
    _APT_CACHE["t"] = time.time()
    _APT_CACHE["d"] = res
    return res

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
    # chrony often keeps NTPSynchronized=no while being synced — ask chrony itself first
    if shutil.which("chronyc"):
        r = _run(["chronyc", "-n", "tracking"], timeout=6)
        log = r.get("log") or ""
        if "Leap status" in log:
            return "Not synchronised" in log
    r = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=6)
    return (r.get("log") or "").strip() == "no"

def _diskstat_await():
    """await (ms/operation) from /proc/diskstats since the last snapshot → dict dev→await."""
    global _MON_DISKSTAT
    cur = {}
    for l in _read("/proc/diskstats").splitlines():
        f = l.split()
        if len(f) < 14:
            continue
        dev = f[2]
        if not re.match(r"^(sd[a-z]|nvme\d+n\d+|mmcblk\d+)$", dev):
            continue
        # fields: reads(3) ... read_ticks(6), writes(7) ... write_ticks(10)
        ios = int(f[3]) + int(f[7]); ticks = int(f[6]) + int(f[10])
        cur[dev] = (ios, ticks)
    out = {}
    if _MON_DISKSTAT:
        for dev, (ios, ticks) in cur.items():
            p = _MON_DISKSTAT.get(dev)
            if p:
                dios, dticks = ios - p[0], ticks - p[1]
                if dios > 20:                       # only under noticeable load
                    out[dev] = dticks / dios
    _MON_DISKSTAT = cur
    return out

def _proc_hog(cpu_thr):
    """A process steadily hogging CPU (over several ticks)."""
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
        if n == 3:                                  # ~3 ticks in a row
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
    # Detection and the LOG always work. Event settings only control
    # delivery: Pushover = cfg.enabled + ev.on (checked in fire),
    # the desktop card = ev.desk (checked by the client via the log).
    on  = lambda k: True
    pri = lambda k: ev.get(k, {}).get("priority", 0)
    thr = lambda k, dv: ev.get(k, {}).get("threshold", dv)
    now = time.time()
    cd = cfg.get("cooldown", 1800)
    if len(_MON_LAST) > 400:            # don't accumulate keys forever (ssh:/panel_new: by IP)
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
    if not s:                       # without base metrics, skip the tick (the next one retries)
        return
    host = s.get("host", "NAS")

    # --- system startup ---
    if not _MON_BOOT_SENT:
        try:
            log_event("boot", "System started", "%s is back online" % host, "ok")
        except Exception:
            pass
        if cfg.get("enabled") and ev.get("boot", {}).get("on"):
            push_notify("NAS: system started", "%s is back online" % host, pri("boot"))
        _MON_BOOT_SENT = True

    # --- corrupted settings files (queue filled by the loader) ---
    while _BAD_CONFIGS:
        bad = _BAD_CONFIGS.pop(0)
        fire("cfg_corrupt:%s" % bad, "NAS: settings file corrupted",
             "%s is unreadable — saved as %s.bad, defaults applied. "
             "Check your settings." % (bad, bad), pri("cfg_corrupt"),
             ev_name="cfg_corrupt", lvl="warn")

    # --- USB auto-import: copy results from the import log ---
    imp = _safe(lambda: _usb_import_events(_MON_USBIMP))
    if imp:
        evs, _MON_USBIMP = imp
        for lvl_, title_, msg_, ts_ in evs:
            fire("usbimp:%d" % int(ts_), title_, msg_, pri("usb_import"),
                 ev_name="usb_import", lvl=lvl_)

    # --- disk attach / detach (by change in the set of volumes) ---
    vols = _safe(_block_volumes)
    if vols is not None:            # on failure don't corrupt _MON_DEVS (otherwise false add/remove)
        if _MON_DEVS is not None:
            added   = [vols[d] for d in vols if d not in _MON_DEVS]
            removed = [_MON_DEVS[d] for d in _MON_DEVS if d not in vols]
            if added and on("disk_add"):
                fire("disk_add", "NAS: disk connected", "Appeared: " + ", ".join(map(str, added)), pri("disk_add"))
            if removed and on("disk_remove"):
                fire("disk_remove", "NAS: disk disconnected", "Gone: " + ", ".join(map(str, removed)), pri("disk_remove"))
        _MON_DEVS = vols

    # --- auto-refresh of the space analyzer (self-throttles to once per 15 min) ---
    _safe(lambda: _duscan_auto(load_maintenance().get("duscan_hours", 0)))
    # --- firewall auto-sync: keep the needed ports open (panel/SSH/
    #     shares + docker), so a new container/port change isn't left behind UFW ---
    _safe(ufw_autosync)

    # --- filesystem in "read-only" mode (data risk) ---
    if on("readonly"):
        ro = _safe(_readonly_mounts, [])
        if ro:
            fire("readonly", "NAS: disk is read-only",
                 "Mounted ro (FS failure?): " + ", ".join(ro), pri("readonly"))

    # --- FS/IO errors in the kernel log ---
    if on("fserror"):
        errs = _safe(_kernel_fs_errors, [])
        if errs:
            fire("fserror", "NAS: disk errors in the kernel log", "\n".join(errs), pri("fserror"))

    # --- Pi: temperature / throttling / memory / swap / load ---
    t = s.get("temp")
    if on("temp") and t and t >= thr("temp", 75):
        fire("temp", "NAS: overheating", "Temperature %s°C (threshold %s°C)" % (t, thr("temp", 75)), pri("temp"))
    tr = s.get("throttled") or {}
    # Undervolt is about the power supply, throttling is about cooling. Different events:
    # the undervolt flag clears only when power is removed, so report it right away.
    if on("undervolt") and tr.get("undervolt"):
        fire("undervolt", "NAS: undervoltage",
             "The PSU cannot supply enough current (flags %s) — the board may power off without warning. "
             "Check the PSU and cable." % tr.get("raw", ""), pri("undervolt"), lvl="warn")
    if on("throttle") and tr.get("throttle"):
        fire("throttle", "NAS: frequency throttling",
             "CPU lowered its frequency (flags %s)" % tr.get("raw", ""), pri("throttle"))
    m = (s.get("mem") or {}).get("pct", 0)
    if on("mem") and m >= thr("mem", 92):
        fire("mem", "NAS: low memory", "RAM usage at %s%%" % m, pri("mem"))
    mem = _safe(mem_info, {})
    if on("swap") and mem.get("swap_total"):
        swp = round(100 * (mem["swap_total"] - mem["swap_free"]) / mem["swap_total"])
        if swp >= thr("swap", 60):
            fire("swap", "NAS: active swap", "Swap usage at %s%% — not enough RAM" % swp, pri("swap"))
    load1 = (s.get("load") or [0])[0]
    if on("load") and load1 >= thr("load", 8):
        fire("load", "NAS: high load", "Load average 1m = %.2f" % load1, pri("load"))

    # --- space: pool + individual disks ---
    pool = s.get("disk_pool") or {}
    if on("pool") and pool.get("pct", 0) >= thr("pool", 90):
        fire("pool", "NAS: storage full", "%s usage at %s%%" % (pool.get("path", "pool"), pool.get("pct")), pri("pool"))
    if on("diskfull"):
        for mp, pct in _safe(_data_mounts_usage, []):
            if mp == "/mnt/storage":
                continue
            if pct >= thr("diskfull", 90):
                fire("diskfull:" + mp, "NAS: disk filling up", "%s usage at %s%%" % (mp, pct), pri("diskfull"))

    # --- services and containers ---
    if on("svcfail"):
        r = _run(["systemctl", "list-units", "--failed", "--no-legend", "--plain", "--no-pager"], timeout=10)
        failed = [l.split()[0] for l in (r.get("log") or "").splitlines() if l.strip()]
        if failed:
            fire("svcfail", "NAS: service failure", "Went down: " + ", ".join(failed[:8]), pri("svcfail"))
    if on("container") and shutil.which("docker"):
        bad = _safe(_bad_containers, [])
        if bad:
            fire("container", "NAS: container problem", "; ".join(bad[:8]), pri("container"))

    # --- reboot required (kernel/libc updates) ---
    if on("reboot_req") and os.path.exists("/var/run/reboot-required"):
        fire("reboot_req", "NAS: reboot required", "Updates will apply after a reboot", pri("reboot_req"))

    # --- SMART: single pass every N minutes (smart_scan_min setting) ---
    _scan_s = max(300, (_safe(load_maintenance, {}) or {}).get("smart_scan_min", 10) * 60)
    if (on("smart") or on("smart_wear") or on("disktemp")) and now - _MON_SMART_LAST >= _scan_s:
        _MON_SMART_LAST = now
        scan = _safe(_smart_scan, {})
        for dev, d in scan.items():
            if on("smart") and d.get("passed") is False:
                fire("smart:" + dev, "NAS: disk failed SMART", "%s — SMART FAIL, replace the disk" % dev, pri("smart"))
            if on("smart_wear"):
                bad = []
                if isinstance(d.get("realloc"), int) and d["realloc"] >= thr("smart_wear", 1):
                    bad.append("reallocated sectors: %d" % d["realloc"])
                if isinstance(d.get("pending"), int) and d["pending"] >= thr("smart_wear", 1):
                    bad.append("pending: %d" % d["pending"])
                if bad:
                    fire("wear:" + dev, "NAS: disk wear", "%s — %s" % (dev, ", ".join(bad)), pri("smart_wear"), ev_name="smart_wear")
            if on("disktemp") and isinstance(d.get("temp"), int) and d["temp"] >= thr("disktemp", 60):
                fire("dtemp:" + dev, "NAS: disk overheated", "%s — %s°C" % (dev, d["temp"]), pri("disktemp"), ev_name="disktemp")

    # --- containers: restart-loop + bloated docker ---
    if shutil.which("docker"):
        if on("container_loop"):
            loops = _safe(_restart_loops, [])
            if loops:
                fire("cloop", "NAS: container won't start", "Restarting in a loop: " + ", ".join(loops[:8]), pri("container_loop"), ev_name="container_loop")
        if on("docker_space") and _hourly("docker_space"):
            gb = _safe(_docker_reclaimable_gb, 0) or 0
            if gb >= thr("docker_space", 20):
                fire("dspace", "NAS: docker bloated", "Can free ~%s GB (prune)" % gb, pri("docker_space"), ev_name="docker_space")

    # --- SSH login ---
    if on("ssh_login"):
        for user, ip in _safe(_ssh_logins, []):
            fire("ssh:" + ip, "NAS: SSH login", "%s from %s" % (user, ip), pri("ssh_login"), ev_name="ssh_login")

    # --- network: IP / link / VPN change (via fire → cooldown, no spam on flapping) ---
    ip = s.get("ip")
    if _MON_IP is not None and ip and ip != _MON_IP and on("ip_changed"):
        fire("ip_changed", "NAS: IP changed", "Was %s → now %s" % (_MON_IP, ip), pri("ip_changed"))
    _MON_IP = ip or _MON_IP
    iface = s.get("iface")
    # only real transitions between non-empty interfaces (otherwise null flapping → spam)
    if _MON_IFACE is not None and iface and iface != _MON_IFACE and on("link_changed"):
        fire("link_changed", "NAS: network changed", "Active interface: %s → %s" % (_MON_IFACE, iface), pri("link_changed"))
    _MON_IFACE = iface or _MON_IFACE
    if on("vpn_offline") and _safe(_tailscale_offline):
        fire("vpn", "NAS: VPN offline", "Tailscale is offline — remote access unavailable", pri("vpn_offline"), ev_name="vpn_offline")

    # --- backup health of the main NAS (connection/folders/staleness/size/space) ---
    _safe(lambda: nb_health_tick(fire, ev, pri, thr, now))
    # --- data protection: SnapRAID + mergerfs + backup ---
    # take the last sync/scrub with a date (snapraid_status) and include the date in the dedup key —
    # so each event notifies ONCE, not every cooldown while the line sits in the log tail
    sn = _safe(snapraid_status, {}) or {}
    ls, lsc = sn.get("last_sync") or {}, sn.get("last_scrub") or {}
    if on("snap_ok") and ls.get("result") == "ok":
        fire("snapok:" + str(ls.get("date")), "NAS: SnapRAID sync OK", "Parity sync succeeded (%s)" % (ls.get("date") or ""), pri("snap_ok"), ev_name="snap_ok", lvl="ok")
    if on("snap_err") and ls.get("result") == "err":
        fire("snaperr:" + str(ls.get("date")), "NAS: SnapRAID sync error", "Parity sync failed (%s)" % (ls.get("date") or ""), pri("snap_err"), ev_name="snap_err")
    if on("scrub_err") and lsc.get("result") == "err":
        fire("scruberr:" + str(lsc.get("date")), "NAS: SnapRAID scrub error", "Check found a problem (%s)" % (lsc.get("date") or ""), pri("scrub_err"), ev_name="scrub_err")
    if on("delete_block") and sn.get("blocked"):
        fire("delblk", "NAS: sync stopped by protection", sn["blocked"], pri("delete_block"), ev_name="delete_block")
    if on("mergerfs"):
        miss = _safe(_mergerfs_missing, [])
        if miss:
            fire("mfs", "NAS: disk dropped from the pool", "Not mounted: " + ", ".join(miss), pri("mergerfs"), ev_name="mergerfs")
    if on("backup"):
        blog = _read("/var/log/nas-backup.log")
        if blog:
            last = blog.splitlines()[-1]
            if re.search(r"\b(FAIL|failed|error)\b", last, re.I):
                fire("bkp", "NAS: backup failed", last[-160:], pri("backup"), ev_name="backup", lvl="warn")
            elif re.search(r"\b(OK|success|done)\b", last, re.I):
                fire("bkp", "NAS: backup completed", last[-160:], pri("backup"), ev_name="backup", lvl="ok")

    # --- maintenance ---
    root = _safe(lambda: disk_info("/")) or {}
    if on("root_full") and root.get("pct", 0) >= thr("root_full", 90):
        fire("rootfull", "NAS: low space on the system card", "Partition / usage at %s%%" % root.get("pct"), pri("root_full"), ev_name="root_full")
    if on("sd_degrade"):
        sd = _safe(_sd_errors, [])
        if sd:
            fire("sderr", "NAS: SD card errors", "\n".join(sd), pri("sd_degrade"), ev_name="sd_degrade")
    if on("sustained_heat"):
        hot = (t and t >= thr("temp", 75)) or not tr.get("ok", True)
        _MON_HEAT = _MON_HEAT + 1 if hot else 0
        if _MON_HEAT >= thr("sustained_heat", 10):
            fire("heat", "NAS: sustained overheating/throttling", "Already %d min in a row — check cooling/power" % _MON_HEAT, pri("sustained_heat"), ev_name="sustained_heat")
    if on("fan_stall"):
        rpm = _safe(_fan_rpm)
        if rpm == 0 and t and t >= thr("temp", 75):
            fire("fan", "NAS: fan stopped", "0 rpm at %s°C — check the cooler" % t, pri("fan_stall"), ev_name="fan_stall")
    if on("cron_failed"):
        cf = _safe(_cron_failures, [])
        if cf:
            fire("cron", "NAS: scheduled task failed", "Error: " + ", ".join(map(str, cf)), pri("cron_failed"), ev_name="cron_failed")
    if on("time_drift") and _safe(_ntp_unsynced):
        fire("ntp", "NAS: time not synchronized", "The clock may drift — check chrony/timesyncd", pri("time_drift"), ev_name="time_drift")
    if on("updates") and _hourly("updates"):
        n = _safe(_apt_upgradable, 0) or 0
        if n > 0:
            fire("upd", "NAS: updates available", "Packages available to update: %d" % n, pri("updates"), ev_name="updates")
    if on("sec_updates") and _hourly("sec_updates"):
        su = _safe(_sec_updates_recent, [])
        if su:
            fire("secupd", "NAS: security updates applied", su[-1], pri("sec_updates"), ev_name="sec_updates")

    # --- behavioral ---
    tx = (s.get("net") or {}).get("tx", 0)
    if on("traffic") and tx >= thr("traffic", 50) * 1024 * 1024:
        fire("traffic", "NAS: heavy outbound traffic", "Upload %s/s — check that this is expected" % fmt_bytes(tx), pri("traffic"))
    if on("slow_disk"):
        for dev, aw in (_safe(_diskstat_await, {}) or {}).items():
            if aw >= thr("slow_disk", 250):
                fire("slow:" + dev, "NAS: disk responding slowly", "%s — latency %d ms/operation" % (dev, round(aw)), pri("slow_disk"), ev_name="slow_disk")
    if on("proc_hog"):
        hog = _safe(lambda: _proc_hog(thr("proc_hog", 80)))
        if hog:
            fire("hog", "NAS: a process is hogging the system", hog, pri("proc_hog"), ev_name="proc_hog")
    if on("inodes"):
        ino = _safe(lambda: _inodes_full(thr("inodes", 90)), [])
        if ino:
            fire("inodes", "NAS: running out of inodes", "; ".join(ino), pri("inodes"))

    # --- weekly "alive" report ---
    if now - _MON_WEEKLY >= 7 * 86400:
        _MON_WEEKLY = now
        wmsg = ("%s · uptime %s · CPU %s%% · temp %s°C · pool %s%%"
                % (host, fmt_uptime(s.get("uptime", 0)), s.get("cpu"),
                   s.get("temp") or "—", (pool.get("pct") if pool else "—")))
        try:
            log_event("weekly", "Weekly report", wmsg, "info")
        except Exception:
            pass
        if cfg.get("enabled") and ev.get("weekly", {}).get("on"):
            push_notify("NAS: weekly report", wmsg, pri("weekly"))

def _hourly(key):
    """True at most once per hour (for heavy checks)."""
    now = time.time()
    if now - _MON_HOURLY.get(key, 0) >= 3600:
        _MON_HOURLY[key] = now
        return True
    return False

def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return "%.0f %s" % (n, u)
        n /= 1024
    return "%.0f PB" % n

def fmt_uptime(sec):
    sec = int(sec or 0); d, h = sec // 86400, (sec % 86400) // 3600
    return ("%dd %dh" % (d, h)) if d else ("%dh %dm" % (h, (sec % 3600) // 60))

# --------------------------------------------------------------------------- #
#  Metrics history (lightweight time-series for the day's graphs)
# --------------------------------------------------------------------------- #
HISTORY_FILE = os.path.join(NAS_CONFIG, "history.json")
HISTORY_CAP  = 1500          # ~25 hours at a 60 s step
HISTORY_LONG_FILE = os.path.join(NAS_CONFIG, "history-long.json")
HISTORY_LONG_CAP  = 4600     # 10 min step → ~32 days
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

# period → (which series, window in seconds, point step)
_HIST_RANGES = {"1h": ("fine", 3600, 60), "6h": ("fine", 6 * 3600, 60),
                "24h": ("fine", 25 * 3600, 60), "7d": ("long", 7 * 86400, 600),
                "30d": ("long", 31 * 86400, 600)}

def history_snapshot(rng="24h"):
    """A copy of the history for the period under lock — safe to serialize in the HTTP thread."""
    series, span, step = _HIST_RANGES.get(rng, _HIST_RANGES["24h"])
    cutoff = time.time() - span
    with _hist_lock:
        h = _load_history() if series == "fine" else _load_history_long()
        return {"history": [p for p in h if p.get("t", 0) >= cutoff], "step": step, "range": rng}

def history_sample():
    """Take one metrics point and append it to the history (called once per minute)."""
    global _hist_dirty
    try:
        s = stats()
    except Exception:
        return
    pt = {"t": int(time.time()), "cpu": s.get("cpu"), "temp": s.get("temp"),
          "mem": (s.get("mem") or {}).get("pct"),
          "rx": (s.get("net") or {}).get("rx"), "tx": (s.get("net") or {}).get("tx"),
          "pool": (s.get("disk_pool") or {}).get("pct"),
          "dtemp": _safe(lambda: _main_disk_temp()[0]),   # main-storage disk temp (cached, backup-safe)
          "dio": s.get("dio")}                            # main-storage disk throughput B/s
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
        # long series: every 10 minutes — aggregate of minute points (avg, max for temp)
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
                       "pool": pt.get("pool"), "dtemp": mx("dtemp"), "dio": avg("dio")})
            if len(hl) > HISTORY_LONG_CAP:
                del hl[:len(hl) - HISTORY_LONG_CAP]
            snap_long = list(hl)
    if write:                            # write to disk outside the lock (don't hold up monitoring)
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
#  Time Machine — this NAS as a macOS Time Machine target (Samba + vfs_fruit +
#  Avahi). A fully separate feature: its own include-config, its own avahi-service,
#  its own folder. The apply engine is nas-wizard.sh api timemachine[-off].
# =========================================================================== #
TM_CONF   = "/etc/nas-wizard/timemachine.conf"          # persisted params
TM_INC    = "/etc/samba/nas-timemachine.conf"
TM_AVAHI  = "/etc/avahi/services/nas-timemachine.service"
TM_BUNDLE_EXT = (".sparsebundle", ".backupbundle")

def _tm_read_conf():
    """Read /etc/nas-wizard/timemachine.conf (key=value) → dict."""
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
    """Time Machine target state: installation, activity, folder, quota,
    space and the list of Mac backups (sparsebundle)."""
    conf = _tm_read_conf()
    path = conf.get("path") or storage_sub("TimeMachine") or (STORAGE + "/TimeMachine")
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
    # space on the partition with the folder
    space = None
    try:
        st = os.statvfs(path if os.path.isdir(path) else os.path.dirname(path) or "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        space = {"total": total, "free": free, "used": total - free}
    except OSError:
        pass
    # Mac backups: each *.sparsebundle/*.backupbundle is a separate machine
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
#  Auto-maintenance: daily background tasks (auto trash cleanup, etc.)
# --------------------------------------------------------------------------- #
# =========================================================================== #
#  Backup of the main NAS onto this NAS (rsync daemon or SSH), a separate mini-app
# =========================================================================== #
NB_CONF   = "/etc/nas-os/nas-backup.json"                 # secrets → root 600
NB_QUEUE  = os.path.join(NAS_CONFIG, "nas-backup-queue.json")
NB_MAIN   = "main"                        # id of the first (legacy) profile
NB_MAX_PROFILES = 8
_NB_PID_RE = re.compile(r"^[a-z0-9]{1,12}$")

# A backup run is launched as a SEPARATE process in a transient systemd unit
# (outside the service cgroup) → it survives a restart/update of nas-web. The driver
# writes output to a log file and status to json; the UI/server read them and reconnect.
# All state is PER-FILE PER-PROFILE. The legacy profile has names without a suffix,
# so migration doesn't lose the history and status of the existing backup.
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
    """Whether a run is in progress: the state flag AND a live transient unit (so it doesn't stick after a crash)."""
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
    """Whether any profile's run is in progress (only one allowed at a time)."""
    return any(nb_run_active(p["id"]) for p in nb_profiles())

# ---- queue: parallel runs are forbidden, extras wait (see _nb_queue_drain) ----
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
# allowed roots for local destination folders (not system directories)
_NB_DEST_OK = ("/mnt/", "/media/", "/srv/", "/home/")

def _nb_defaults():
    # direction: pull = grab from another NAS to here; push = send from this NAS
    # to an external disk (transport=local) or to another server (transport=ssh)
    return {"direction": "pull", "verify": False,
         "transport": "rsync", "host": "", "user": "", "password": "", "ssh_port": 22,
         # auth: "password" (sshpass, as before) or "key" — a key created by the panel.
         # provider: "" = a regular server, "rsyncnet" = an rsync.net account (restricted
         # shell, paths FROM the HOME folder, no retention needed — they have ZFS snapshots).
         "auth": "password", "provider": "",
         "dst2": {},          # SSH destination for a pull profile (SSH→SSH bridge mode)
         "remote_sudo": False,
         "dest_mode": "single", "dest_base": (storage_sub("nas-backup") or "/mnt/storage/nas-backup"),
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
    """All backup profiles. Always at least one. The old flat config (v1)
    is read as a single "Default" profile — we write nothing to disk,
    _nb_migrate() does the write once at startup."""
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
    """Profile by id; without id — the first (compatibility with old code and API)."""
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
    """v1 (flat config) → v2 (list of profiles). Once, with a backup copy."""
    raw = _nb_read_raw()
    if not isinstance(raw, dict) or isinstance(raw.get("profiles"), list):
        return False
    if not raw:                       # no config yet — nothing to write
        return False
    try:
        shutil.copy2(NB_CONF, NB_CONF + ".v1.bak")
    except OSError:
        pass
    _nb_write_profiles(nb_profiles())
    log_event("info", "NAS backup: config migrated to profile format", "", "ok",
              kind="backup", desk=False)
    return True

# --------------------------------------------------------------------------
# Two independent sides: source and destination, each — local or remote.
# On disk the profile still stores the OLD fields (direction/transport/host/…):
# they remain the source of truth for code outside backup (screen tiles, dest_off,
# retention) and for old profiles. src/dst are DERIVED from them, and nb_save does
# the reverse folding. This way a free choice of sides appears without migrating the file
# and without the risk of an old profile breaking.
def _nb_sides(cfg):
    """(src, dst) — normalized sides. kind: local|ssh|rsyncd."""
    cfg = cfg or {}
    conn = {"host": cfg.get("host", ""), "user": cfg.get("user", ""),
            "password": cfg.get("password", ""), "port": int(cfg.get("ssh_port", 22) or 22),
            "auth": cfg.get("auth") or "password", "sudo": bool(cfg.get("remote_sudo")),
            "provider": cfg.get("provider") or ""}
    tr = cfg.get("transport") or "rsync"
    if cfg.get("direction") == "push":
        src = {"kind": "local"}
        dst = dict(conn, kind="ssh") if tr == "ssh" else {"kind": "local"}
    else:
        src = dict(conn, kind=("rsyncd" if tr == "rsync" else "ssh"))
        # a pull profile's destination can also be remote — that is SSH→SSH
        d2 = cfg.get("dst2") or {}
        if d2.get("kind") == "ssh":
            dst = {"kind": "ssh", "host": d2.get("host", ""), "user": d2.get("user", ""),
                   "password": d2.get("password", ""), "port": int(d2.get("port", 22) or 22),
                   "auth": d2.get("auth") or "password", "sudo": False,
                   "provider": d2.get("provider") or ""}
        else:
            dst = {"kind": "local"}
    dst["base"] = cfg.get("dest_base") or ""
    return src, dst


def _nb_remote_both(cfg):
    """SSH→SSH: rsync can't do this ("source and destination cannot both be
    remote"), so we mount the source over sshfs and copy as if from a local
    folder. Data goes through the NAS — a cost the panel warns about."""
    src, dst = _nb_sides(cfg)
    return src["kind"] != "local" and dst["kind"] != "local"


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
        # mirror the folder tree relative to the ACTUAL storage root, not a literal
        # /mnt/storage (on a pool-less box the root is the mounted USB path)
        root = (storage_base() or STORAGE).strip("/")
        if root and (rel == root or rel.startswith(root + "/")):
            rel = rel[len(root):].lstrip("/")
    return os.path.normpath(base + "/" + rel)

def nb_save(patch, pid=None):
    cur = nb_load(pid)
    if patch.get("direction") in ("pull", "push"):
        cur["direction"] = patch["direction"]
    if "verify" in patch:
        cur["verify"] = bool(patch["verify"])
    # Free choice of sides: the UI sends {"src":{...},"dst":{...}}. We unpack it
    # into profile fields (direction/transport/host/…), so all the rest of the code —
    # screen tiles, dest_off, retention — keeps working unchanged.
    for sd in ("src", "dst"):
        if not isinstance(patch.get(sd), dict):
            continue
        q = patch[sd]
        kind = q.get("kind")
        if kind not in ("local", "ssh", "rsyncd"):
            kind = None
        cs, cd = _nb_sides(cur)
        side = dict(cs if sd == "src" else cd)
        if kind:
            side["kind"] = kind
        for k in ("host", "user", "port", "auth", "provider"):
            if k in q:
                side[k] = q[k]
        if q.get("password"):
            side["password"] = str(q["password"])
        if sd == "src":
            cs = side
        else:
            cd = side
        # fold back into the profile
        if cs["kind"] == "local":
            cur["direction"] = "push"
            cur["transport"] = "ssh" if cd["kind"] == "ssh" else "local"
            far = cd if cd["kind"] == "ssh" else {}
            cur["dst2"] = {}
        else:
            cur["direction"] = "pull"
            cur["transport"] = "rsync" if cs["kind"] == "rsyncd" else "ssh"
            far = cs
            # a pull profile's remote destination = SSH→SSH (bridge through the NAS)
            cur["dst2"] = {k: cd.get(k, "") for k in
                           ("host", "user", "password", "port", "auth", "provider")} \
                if cd["kind"] == "ssh" else {}
            if cur["dst2"]:
                cur["dst2"]["kind"] = "ssh"
        if far:
            cur["host"] = str(far.get("host", ""))
            cur["user"] = str(far.get("user", ""))
            if far.get("password"):
                cur["password"] = str(far["password"])
            try:
                cur["ssh_port"] = max(1, min(65535, int(far.get("port", 22) or 22)))
            except (TypeError, ValueError):
                cur["ssh_port"] = 22
            if far.get("auth") in ("password", "key"):
                cur["auth"] = far["auth"]
            cur["provider"] = "rsyncnet" if far.get("provider") == "rsyncnet" else ""
    if patch.get("auth") in ("password", "key"):
        cur["auth"] = patch["auth"]
    if "provider" in patch:
        cur["provider"] = "rsyncnet" if patch.get("provider") == "rsyncnet" else ""
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
            src = str(j.get("src", "")).strip().rstrip("/")   # keep the leading / (SSH abs paths)
            dst = os.path.normpath(str(j.get("dest", "")).strip())
            if not src or not dst_ok(dst): continue
            job = {"src": src, "dest": dst, "enabled": bool(j.get("enabled", True))}
            # per-job excludes: anchored rsync patterns relative to src (leading /).
            # so unchecking a nested folder excludes it, while the parent copies
            # everything else — including what appears in it later.
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
    cur["saved"] = int(time.time())   # the last-touched profile is the one to open
    profs = [cur if p["id"] == cur["id"] else p for p in nb_profiles()]
    _nb_write_profiles(profs)
    return cur

def nb_public(cfg=None):
    """Config for the UI without leaking the password."""
    c = dict(cfg or nb_load())
    c["has_password"] = bool(c.get("password"))
    c["password"] = ""
    c["key"] = _safe(nb_key_info, {}) or {}      # panel key: whether it exists, the public part
    src, dst = _nb_sides(cfg or nb_load())
    for sd in (src, dst):
        sd["has_password"] = bool(sd.pop("password", ""))
    c["src"], c["dst"] = src, dst                # free sides for the UI
    c["both_remote"] = src["kind"] != "local" and dst["kind"] != "local"
    return c

def _nb_pid(pid):
    """Normalize the profile id: None/garbage/unknown -> the first profile."""
    return nb_load(pid)["id"]

def _nb_qpid(q):
    """profile id from the query (?p=…); empty -> the first profile."""
    v = (q.get("p") or [""])[0]
    return v if _NB_PID_RE.match(v or "") else None

def _nb_bpid(b):
    """profile id from the POST body."""
    v = str((b or {}).get("p") or "")
    return v if _NB_PID_RE.match(v) else None

def nb_profiles_public():
    """A short summary per profile — for the tab bar."""
    out = []
    for p in nb_profiles():
        pid = p["id"]
        # push to a local disk is configured without a host — a destination and jobs are enough
        conn = bool(p.get("host")) or (p.get("direction") == "push" and p.get("transport") == "local"
                                       and bool(p.get("dest_base")))
        st = _nb_run_state_read(pid)
        out.append({"id": pid, "name": p["name"], "direction": p.get("direction") or "pull",
                    "running": nb_run_active(pid), "queued": nb_queued(pid),
                    "jobs": len(p.get("jobs") or []),
                    "configured": bool(conn and p.get("jobs")),
                    # which window to open with: "last touched" = a config edit or a run
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
    """Unique destination folder: two profiles must not write into the same base —
    their "mass-deletion guard" and _deleted archive would mix together."""
    cand, n = base, 2
    while cand in taken and n < 50:
        cand = "%s-%d" % (base.rstrip("/"), n); n += 1
    return cand

def nb_profile_add(name="", clone_from="", direction=""):
    profs = nb_profiles()
    if len(profs) >= NB_MAX_PROFILES:
        return {"ok": False, "log": "cannot have more than %d profiles" % NB_MAX_PROFILES}
    pid = _nb_new_pid({p["id"] for p in profs})
    if not pid:
        return {"ok": False, "log": "could not allocate an id"}
    # default name is language-neutral: the panel defaults to English,
    # and a profile name is data — it does not go through i18n
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
            return {"ok": False, "log": "no such profile"}
        # copy the connection, excludes and policies; SOURCES and schedule — no:
        # paths on the other NAS differ, and two enabled schedules at once is a surprise
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
    log_event("action", "NAS backup: profile «%s» created" % name, "", "ok", kind="backup", desk=False)
    return {"ok": True, "id": pid, "config": nb_public(new)}

def nb_profile_rename(pid, name):
    name = str(name or "").strip()[:40]
    if not name:
        return {"ok": False, "log": "empty name"}
    profs = nb_profiles()
    if not any(p["id"] == pid for p in profs):
        return {"ok": False, "log": "no such profile"}
    for p in profs:
        if p["id"] == pid:
            p["name"] = name
    _nb_write_profiles(profs)
    return {"ok": True}

def nb_profile_delete(pid, confirm=""):
    """Foolproofing: the last profile cannot be deleted; remove all sources first;
    the name must be typed by hand. Data in the destination is NOT touched — only
    the config, history and logs disappear."""
    profs = nb_profiles()
    if len(profs) <= 1:
        return {"ok": False, "log": "cannot delete the last profile"}
    p = next((x for x in profs if x["id"] == pid), None)
    if not p:
        return {"ok": False, "log": "no such profile"}
    if p.get("jobs"):
        return {"ok": False, "log": "remove all sources first (%d left)" % len(p["jobs"])}
    if nb_run_active(pid):
        return {"ok": False, "log": "a run is in progress — stop it first"}
    if str(confirm or "").strip() != p["name"]:
        return {"ok": False, "log": "profile name did not match"}
    _nb_queue_remove(pid)
    _nb_write_profiles([x for x in profs if x["id"] != pid])
    for f in (nb_status_file(pid), nb_run_log(pid), nb_run_state(pid),
              nb_run_cancel(pid), nb_history_file(pid), nb_health_file(pid)):
        try: os.remove(f)
        except OSError: pass
    log_event("action", "NAS backup: profile «%s» deleted" % p["name"],
              "the copied data in the destination is left untouched", "ok", kind="backup", desk=False)
    return {"ok": True}

# Keep the backup key separate from root's system keys: the panel creates and
# shows it, and it is installed on the destination. rsync.net (and any decent
# server) wants a key rather than a password for automation.
NB_KEY = "/root/.ssh/nas-backup"


def nb_key_info():
    pub = _read(NB_KEY + ".pub", "").strip()
    return {"exists": bool(pub and os.path.isfile(NB_KEY)), "pubkey": pub, "path": NB_KEY}


def nb_key_gen(force=False):
    if os.path.isfile(NB_KEY) and not force:
        return dict(nb_key_info(), ok=True)
    os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
    for f in (NB_KEY, NB_KEY + ".pub"):
        _safe(lambda p=f: os.unlink(p))
    r = _run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "nas-backup@" + socket.gethostname(),
              "-f", NB_KEY], timeout=30)
    if not r["ok"]:
        return {"ok": False, "log": (r.get("log") or "")[-200:]}
    _safe(lambda: os.chmod(NB_KEY, 0o600))
    return dict(nb_key_info(), ok=True)


def nb_key_install(cfg, side="dst"):
    """Install the public key on the chosen side, logging in ONCE with a password."""
    src_s, dst_s = _nb_sides(cfg)
    sd = src_s if side == "src" else dst_s
    if sd["kind"] != "ssh":
        return {"ok": False, "log": "this side is not SSH"}
    cfg = dict(cfg, host=sd.get("host", ""), user=sd.get("user", ""),
               password=sd.get("password", ""), ssh_port=sd.get("port", 22),
               provider=sd.get("provider", ""))
    if not (cfg.get("host") and cfg.get("user")):
        return {"ok": False, "log": "address/user not set"}
    if not nb_key_info()["exists"]:
        g = nb_key_gen()
        if not g.get("ok"):
            return g
    pw = cfg.get("password") or ""
    if not pw:
        return {"ok": False, "log": "the server password is required — enter it once and the key gets installed"}
    if not shutil.which("sshpass"):
        return {"ok": False, "log": "sshpass is missing — copy the key to the server manually"}
    port = str(int(cfg.get("ssh_port", 22) or 22))
    tgt = "%s@%s" % (cfg["user"], cfg["host"])
    env = dict(_C_ENV, SSHPASS=pw)
    r = _run(["sshpass", "-e", "ssh-copy-id", "-i", NB_KEY + ".pub", "-p", port,
              "-o", "StrictHostKeyChecking=accept-new", tgt], timeout=45, env=env)
    if not r["ok"]:
        log = (r.get("log") or "").strip()
        # ssh-copy-id doesn't work on rsync.net and other restricted shells (no sh),
        # but they have their own way — install the key with their command
        if cfg.get("provider") == "rsyncnet":
            pub = nb_key_info()["pubkey"]
            r2 = _run(["sshpass", "-e", "ssh", "-p", port,
                       "-o", "StrictHostKeyChecking=accept-new", tgt,
                       "echo %s >> .ssh/authorized_keys" % shlex.quote(pub)],
                      timeout=45, env=env)
            if r2["ok"]:
                return {"ok": True, "log": "key installed"}
            log = (r2.get("log") or log).strip()
        return {"ok": False, "log": _nb_err(log) or log[-200:]}
    return {"ok": True, "log": "key installed"}


def _nb_ssh_auth(cfg):
    """ssh auth arguments: (list of options, env). Key — if chosen and
    created; otherwise a password via sshpass, as before."""
    if cfg.get("auth") == "key" and nb_key_info()["exists"]:
        return (["-i", NB_KEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"],
                dict(_C_ENV), False)
    pw = cfg.get("password", "")
    if pw and shutil.which("sshpass"):
        return ([], dict(_C_ENV, SSHPASS=pw), True)
    return (["-o", "BatchMode=yes"], dict(_C_ENV), False)


def _nb_side_env(side):
    """(path prefix, env, extra rsync args) for ONE side."""
    kind = (side or {}).get("kind") or "local"
    if kind == "local":
        return ("", dict(_C_ENV), [])
    user, host = side.get("user", ""), side.get("host", "")
    if kind == "ssh":
        port = int(side.get("port", 22) or 22)
        opts, env, use_pw = _nb_ssh_auth(side)
        base = "ssh -p %d -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 %s" % (
            port, " ".join(opts))
        rsh = ("sshpass -e " + base) if use_pw else base
        return ("%s@%s:" % (user, host), env, ["-e", rsh.strip()])
    env = dict(_C_ENV, RSYNC_PASSWORD=side.get("password", ""))
    return ("%s@%s::" % (user, host), env, [])            # rsync daemon — password via RSYNC_PASSWORD


def _nb_remote_env(cfg):
    """Compatibility: the profile's "remote side" (which one depends on the
    direction). New code gets sides via _nb_sides()/_nb_side_env()."""
    src, dst = _nb_sides(cfg)
    side = dst if src["kind"] == "local" else src
    return _nb_side_env(side)

def _nb_ssh_run(cfg, remote_cmd, timeout=30):
    """Run a command on the DESTINATION side (mkdir / remote-archive cleanup).
    Uses the resolved dest side: that is the top-level cfg for a push profile, but
    cfg['dst2'] for an SSH->SSH pull — so mkdir and the retention `rm -rf` never
    land on the SOURCE server (they used to, via cfg.host/user/ssh_port)."""
    side = _nb_dst_side(cfg)
    port = int(side.get("port", 22) or 22)
    opts, env, use_pw = _nb_ssh_auth(side)
    argv = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10"] + opts
    if use_pw:
        argv = ["sshpass", "-e"] + argv
    tgt = "%s@%s" % (side.get("user", ""), side.get("host", ""))
    return _run(argv + [tgt, remote_cmd], timeout=timeout, env=env)

def _nb_err(raw):
    """Short human-readable explanation of an rsync/ssh error (no full-screen wall of text)."""
    low = (raw or "").lower()
    if "sshpass" in low and ("not found" in low or "no such" in low):
        return "sshpass is not installed (needed for SSH password auth)"
    if "sudo:" in low or "not in the sudoers" in low:      # sudo on the source (before the shared password check)
        if "not allowed" in low or "not in the sudoers" in low or "may not run" in low:
            return "sudo on the source isn't allowed for rsync — add a NOPASSWD rule to sudoers"
        if "command not found" in low:
            return "sudo not found on the source"
        return "sudo on the source needs a password/TTY — set up NOPASSWD sudo for rsync (see the tooltip)"
    if "invalid path" in low:
        return ("the destination only accepts module-style paths (a NAS with a forced rsync daemon) — "
                "set the path WITHOUT a leading /, e.g. HDD6TB/Downloads/backup; «Check» lists the available roots")
    if "permission denied" in low or "auth" in low or "password" in low:
        return "access denied — check the username and password (or key)"
    if "connection refused" in low:
        return "connection refused — service/port unavailable on the source"
    if "timed out" in low or "timeout" in low:
        return "connection timeout"
    if "@error" in low:
        return "rsync daemon rejected the request (module or password)"
    if "host key" in low or "remote host identification" in low:
        return "problem with the source's SSH host key"
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    return (lines[-1] if lines else "unknown error")[:180]

def _nb_test_side(cfg, side, what):
    """Check one side: local — folder/disk, remote — connectivity and path."""
    if side["kind"] == "local":
        if what == "dst":
            base = side.get("base") or ""
            if not base:
                return {"ok": False, "log": "no destination folder selected"}
            if _dest_disk_absent(base):
                return {"ok": False, "log": "the destination disk is not mounted (%s)" % base}
        return {"ok": True, "log": "local folder is in place"}
    if not (side.get("host") and side.get("user")):
        return {"ok": False, "log": "address/user not set"}
    if subprocess.run(["ping", "-c", "1", "-W", "3", "--", side["host"]],
                      capture_output=True, timeout=10).returncode != 0:
        return {"ok": False, "log": "%s does not respond to ping" % side["host"]}
    if side["kind"] == "ssh" and side.get("auth") != "key" and side.get("password") \
            and not shutil.which("sshpass"):
        return {"ok": False, "log": "SSH password auth needs sshpass — or use a key"}
    prefix, env, rsh = _nb_side_env(side)
    r = subprocess.run(["rsync"] + rsh + ["--list-only", prefix], capture_output=True,
                       text=True, env=env, timeout=25)
    if r.returncode != 0:
        return {"ok": False, "log": _nb_err((r.stderr or r.stdout)[-300:])}
    return {"ok": True, "log": "connection OK"}


def nb_test(cfg=None):
    """Connectivity test + module list (for rsync daemon). For push-local —
    checks that a destination folder is chosen and its disk is mounted."""
    cfg = cfg or nb_load()
    if _nb_remote_both(cfg):
        # SSH→SSH: check both sides, each in its own way
        src, dst = _nb_sides(cfg)
        a1 = _nb_test_side(cfg, src, "src")
        if not a1.get("ok"):
            return {"ok": False, "log": "source: " + (a1.get("log") or "")}
        a2 = _nb_test_side(cfg, dst, "dst")
        if not a2.get("ok"):
            return {"ok": False, "log": "destination: " + (a2.get("log") or "")}
        if not shutil.which("sshfs"):
            return {"ok": False, "log": "SSH to SSH mode requires sshfs"}
        return {"ok": True, "log": "both sides are reachable · the copy will go through this NAS"}
    if cfg.get("transport") == "local":
        base = cfg.get("dest_base") or ""
        if not base:
            return {"ok": False, "log": "no destination folder selected"}
        if _dest_disk_absent(base):
            return {"ok": False, "log": "the destination disk is not mounted (%s)" % base}
        return {"ok": True, "log": "destination is in place"}
    if not cfg.get("host") or not cfg.get("user"):
        return {"ok": False, "log": "address/user not set"}
    if subprocess.run(["ping", "-c", "1", "-W", "3", "--", cfg["host"]],
                      capture_output=True, timeout=10).returncode != 0:
        return {"ok": False, "log": "%s does not respond to ping" % cfg["host"]}
    remote, env, rsh = _nb_remote_env(cfg)
    if cfg.get("transport") == "ssh":
        if cfg.get("password") and not shutil.which("sshpass"):
            return {"ok": False, "log": "SSH password auth needs sshpass (reinstall/update the system) — or use a key"}
        extra = ["--rsync-path=sudo rsync"] if cfg.get("remote_sudo") else []   # verify the sudo path too
        # push: list the root WITHOUT «/» — on a forced-rsync-daemon NAS
        # (UGREEN/Synology) «/» is an invalid path while an empty path lists modules
        r = _run(["rsync"] + rsh + extra + ["--list-only", remote if _nb_push(cfg) else remote + "/"],
                 timeout=25, env=env)
        ok_msg = "SSH connection works" + (" · sudo on source OK" if cfg.get("remote_sudo") else "")
        if r["ok"] and _nb_push(cfg):
            roots = []
            for l in (r.get("log") or "").splitlines():
                m = re.match(r"^d\S*\s+[\d,]+\s+\S+\s+\S+\s+(.+)$", l)
                if m and m.group(1) not in (".", ""):
                    roots.append(m.group(1))
            if roots:
                ok_msg += " · roots: " + ", ".join(roots[:8])
        return {"ok": r["ok"], "log": ok_msg if r["ok"] else _nb_err(r["log"])}
    r = subprocess.run(["rsync", remote], capture_output=True, text=True, env=env, timeout=25)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "auth failed" in out or "@ERROR" in out:
        return {"ok": False, "log": _nb_err(out)}
    mods = [l.split("\t")[0].split()[0] for l in out.splitlines() if l.strip() and not l.startswith("@")]
    return {"ok": True, "modules": [m for m in mods if m], "log": "connection works"}

_NB_JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized",
            ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems", ".apdisk"}
def _nb_is_junk(name):
    # OS junk/service files — not shown in the picker (AppleDouble ._*, .DS_Store, etc.)
    return name in _NB_JUNK or name.startswith("._")

_NB_ROOT_SKIP = {"proc", "sys", "dev", "run", "tmp", "boot", "lost+found"}

def _nb_ls(cfg, spec, timeout=30, side=None):
    """--list-only of a remote path (spec is appended to the transport prefix as-is).
    side — a specific side (source/destination); without it the profile's remote
    side is used, as before."""
    remote, env, rsh = _nb_side_env(side) if side else _nb_remote_env(cfg)
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

def _nb_dst_side(cfg):
    return _nb_sides(cfg)[1]


def _nb_remote_shell_fs(cfg, side=None):
    """True = remote rsync sees the real filesystem (plain shell mode).
    False = forced rsync daemon (UGREEN/Synology): paths only from «modules», and
    SSH shell commands live in a DIFFERENT path namespace. The probe is /etc/: it
    exists on any Linux, while a module named like that is exotic.

    rsync.net short-circuits to False on purpose: the account has a restricted
    shell (no mkdir/find to lean on) and every path is relative to the account
    home — exactly the same handling as a module-only NAS, so rsync --mkpath
    creates the folders and we never shell out."""
    side = side or _nb_dst_side(cfg)
    if (side.get("provider") or (cfg or {}).get("provider")) == "rsyncnet":
        return False
    return bool(_nb_ls(cfg, "/etc/", timeout=15, side=side).get("ok"))

def nb_browse_dest(cfg, path):
    """DESTINATION folder picker (remote). The root depends on the server mode:
    a normal shell — «/», a forced daemon and rsync.net — a module list / home."""
    dside = _nb_dst_side(cfg)
    if dside["kind"] != "ssh":
        return {"ok": False, "log": "the destination is not SSH"}
    path = str(path or "").strip().rstrip("/")
    if ".." in path:
        return {"ok": False, "log": "invalid path"}
    if not path:
        if _nb_remote_shell_fs(cfg, dside):
            r = _nb_ls(cfg, "/", side=dside)
            return dict(r, path="", abs=True) if r.get("ok") else \
                {"ok": False, "log": _nb_err(r.get("log") or "")}
        r = _nb_ls(cfg, "", side=dside)
        return dict(r, path="", abs=False) if r.get("ok") else \
            {"ok": False, "log": _nb_err(r.get("log") or "")}
    r = _nb_ls(cfg, path + "/", side=dside)
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
            return {"ok": False, "log": "invalid path"}
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
        # rsync daemon root = list of modules
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
        # format: "drwxr-xr-x  4096 2024/01/01 12:00:00 name"
        m = re.match(r"^(.)\S*\s+[\d,]+\s+\S+\s+\S+\s+(.+)$", l)
        if not m: continue
        name = m.group(2)
        if name in (".", ""): continue
        if _nb_is_junk(name): continue        # don't clutter the picker with junk (.DS_Store, ._*, Thumbs.db…)
        entries.append({"name": name, "dir": m.group(1) == "d"})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"ok": True, "path": path, "entries": entries}

def _nb_prune(cfg):
    """Retention of the deleted-files archive (_deleted/DATE): by days AND by total size (GB)."""
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
        snaps.sort()  # oldest first
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
    """Expand deleted-folder template tokens ({date}/{year}/{month}/… as in USB import)."""
    t = t or time.localtime()
    rep = {"{date}": time.strftime("%Y-%m-%d", t), "{time}": time.strftime("%H-%M-%S", t),
           "{datetime}": time.strftime("%Y-%m-%d_%H-%M-%S", t), "{year}": time.strftime("%Y", t),
           "{month}": time.strftime("%m", t), "{month-name}": _NB_MONTHS[t.tm_mon],
           "{day}": time.strftime("%d", t), "{hour}": time.strftime("%H", t),
           "{minute}": time.strftime("%M", t)}
    s = tpl or ""
    for k, v in rep.items():
        s = s.replace(k, v)
    s = re.sub(r"\{[^}]*\}", "", s)                              # drop unknown tokens
    s = re.sub(r"[^\w \-.А-Яа-яЁё/]", "", s).replace("..", "")
    s = re.sub(r"/+", "/", s).strip("/")
    return s

def nb_deleted_top(cfg):
    """Top (static) folder of the deleted-files archive — for exclude and retention."""
    top = _nb_render_tpl((cfg.get("deleted_dir") or "_deleted/{date}").split("/")[0])
    return top or "_deleted"

def nb_deleted_rel(cfg, t=None):
    rel = _nb_render_tpl(cfg.get("deleted_dir") or "_deleted/{date}", t)
    return rel or ("_deleted/" + time.strftime("%Y-%m-%d", t or time.localtime()))

def nb_build_cmd(cfg, job, dry, prev_files=0, mkpath=False, allow_delete=False, stage=None):
    """rsync command (+env) for one job. prev_files — the number of files in the
    previous run (for the percentage --max-delete guard). stage — sshfs mount of
    the source (SSH→SSH mode)."""
    env, rsh = _nb_cmd_ctx(cfg, stage)
    dest = job["dest"].rstrip("/") + "/"
    owner = TARGET_USER
    limited = nb_dest_fs(cfg, job.get("dest")) in NB_FS_LIMITED
    if limited:
        # exFAT/NTFS/FAT: no symlinks or special files there — drop «-l»/«-D», and rsync
        # simply skips them (code 0) instead of failing with 23 every run.
        # --modify-window=1: on FAT-like filesystems file times are rounded to 2 s, without it
        # rsync thinks the files changed and re-copies them EVERY time.
        # --chown isn't needed either: such an FS doesn't store an owner (it comes from uid= in mount)
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
        # guard: don't delete more than N% of files (of the previous run's count) — otherwise rsync
        # exits with code 25 and deletes NOTHING. Saves you from "the source was wiped/unmounted".
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
        args.append("--exclude=/" + nb_deleted_top(cfg)) # don't back up the deleted-files archive itself
    for ex in cfg.get("excludes", []):
        args.append("--exclude=" + ex)
    for ex in job.get("excludes", []):          # per-job: nested folders unchecked in the tree
        args.append("--exclude=" + str(ex))
    bw = int(cfg.get("bwlimit", 0) or 0)
    if bw > 0:
        args.append("--bwlimit=%d" % bw)                 # KB/s
    if cfg.get("transport") == "ssh" and cfg.get("remote_sudo"):
        args.append("--rsync-path=sudo rsync")   # read files the user can't access (needs NOPASSWD sudo on the source)
    if mkpath:
        args.append("--mkpath")   # forced daemon: rsync itself creates module folders (≥3.2.3)
    if dry:
        args.append("--dry-run")
    args += _nb_src_dst(cfg, job, stage)
    return args, env

NB_STAGE_DIR = "/mnt/nb-src"


def _nb_stage_mount(cfg, pid, writer):
    """Mount the remote SOURCE over sshfs (read-only) — this turns SSH→SSH into
    a "local folder → remote destination", which rsync can do. The daemon lives
    in its own transient unit: a panel restart (our usual development cycle) must
    not tear the mount away mid-run."""
    src, _ = _nb_sides(cfg)
    if src["kind"] != "ssh":
        return None, "a source over an rsync daemon cannot be mounted — choose SSH"
    if not shutil.which("sshfs"):
        return None, "sshfs is missing (reinstall the system — the wizard installs the package)"
    mp = os.path.join(NB_STAGE_DIR, re.sub(r"[^\w-]", "", str(pid))[:24] or "x")
    _nb_stage_umount(pid)
    os.makedirs(mp, mode=0o755, exist_ok=True)
    unit = "nas-nbsrc-" + os.path.basename(mp)
    opts = ["reconnect", "ServerAliveInterval=15", "ServerAliveCountMax=3",
            "ro", "allow_other", "StrictHostKeyChecking=accept-new",
            "port=%d" % int(src.get("port", 22) or 22)]
    argv = ["systemd-run", "--unit", unit, "--collect",
            "--property=Restart=on-failure",
            "--property=ExecStopPost=/bin/umount -l " + shlex.quote(mp)]
    penv = None
    if src.get("auth") == "key" and nb_key_info()["exists"]:
        opts.append("IdentityFile=" + NB_KEY)
    else:
        pw = src.get("password") or ""
        if not pw:
            return None, "the source needs a password or a key"
        # password — via the unit's environment (only root reads it), NOT in argv:
        # /proc/<pid>/cmdline is visible to everyone. --setenv=NAME (no value) makes
        # systemd-run import it from OUR env, so the value never touches any argv.
        argv.append("--setenv=SSHFS_PW")
        opts.append("password_stdin")
        penv = dict(_C_ENV, SSHFS_PW=pw)
    tgt = "%s@%s:%s" % (src.get("user", ""), src.get("host", ""), "/")
    cmd = "sshfs -f -o " + ",".join(opts) + " " + shlex.quote(tgt) + " " + shlex.quote(mp)
    if "password_stdin" in opts:
        cmd = "printf '%s\n' \"$SSHFS_PW\" | " + cmd
    r = _run(argv + ["/bin/bash", "-c", cmd], timeout=40, env=penv)
    if not r["ok"]:
        return None, (r.get("log") or "").strip()[-200:]
    for _ in range(30):                       # wait until the mount actually appears
        time.sleep(0.4)
        try:
            os.statvfs(mp)
            if os.path.ismount(mp):
                writer("source mounted (sshfs): %s@%s" % (src.get("user"), src.get("host")))
                return mp, ""
        except OSError:
            pass
    _nb_stage_umount(pid)
    return None, "could not mount the source over sshfs"


def _nb_stage_umount(pid):
    mp = os.path.join(NB_STAGE_DIR, re.sub(r"[^\w-]", "", str(pid))[:24] or "x")
    _run(["systemctl", "stop", "nas-nbsrc-" + os.path.basename(mp)], timeout=20)
    _run(["umount", "-l", mp], timeout=20)


def _nb_cmd_ctx(cfg, stage=None):
    """(env, extra rsync args) for the run. After the sshfs stage there can be only
    ONE remote side — rsync won't accept more than one «-e» anyway."""
    src, dst = _nb_sides(cfg)
    if stage:
        src = {"kind": "local"}
    side = None
    if src["kind"] != "local":
        side = src
    elif dst["kind"] != "local":
        side = dst
    if not side:
        return (dict(_C_ENV), [])
    _, env, rsh = _nb_side_env(side)
    return (env, rsh)


def _nb_src_dst(cfg, job, stage=None):
    """[src, dst] for rsync from the profile's sides. stage — the sshfs mount path
    of the source (SSH→SSH): then the source looks like a local folder."""
    src, dst = _nb_sides(cfg)
    sp, _, _ = _nb_side_env(src)
    dp, _, _ = _nb_side_env(dst)
    if stage:
        s = stage.rstrip("/") + "/" + str(job["src"]).lstrip("/") + "/"
    elif src["kind"] == "local":
        s = "/" + str(job["src"]).lstrip("/") + "/"
    else:
        s = sp + job["src"] + "/"
    d = (dp if dst["kind"] != "local" else "") + job["dest"].rstrip("/") + "/"
    return [s, d]

def nb_verify_cmd(cfg, job, stage=None):
    """Post-run verify command: rsync --checksum --dry-run re-reads files on both
    sides; every «>f…» line = a file whose content differs from the source."""
    env, rsh = _nb_cmd_ctx(cfg, stage)
    args = ["rsync", "-rltDn", "--checksum", "--out-format=%i %n"] + rsh
    if cfg.get("delete_mode", "archive") == "archive":
        args.append("--exclude=/" + nb_deleted_top(cfg))
    for ex in cfg.get("excludes", []):
        args.append("--exclude=" + ex)
    for ex in job.get("excludes", []):
        args.append("--exclude=" + str(ex))
    if cfg.get("transport") == "ssh" and cfg.get("remote_sudo"):
        args.append("--rsync-path=sudo rsync")
    args += _nb_src_dst(cfg, job, stage)
    return args, env

def _mountpoint_of(p):
    """Nearest mount point upward for a path (existing or not)."""
    p = os.path.abspath(p)
    while p != "/" and not os.path.ismount(p):
        p = os.path.dirname(p)
    return p

# Filesystems that cannot hold what a Linux backup normally carries: no symlinks,
# no sockets/fifos, no owner/perms, and they forbid «:» and CR in names. rsync
# hits this EVERY run and exits with code 23 — so such destinations are handled
# differently (see nb_build_cmd) rather than pretending it's a random failure.
NB_FS_LIMITED = {"exfat", "vfat", "msdos", "fat", "fat32", "ntfs", "ntfs3", "fuseblk", "hfsplus"}
NB_FS_FAT     = {"vfat", "msdos", "fat", "fat32"}   # hard 4 GiB per-file cap — aborts the run
# "file doesn't fit this FS", not a "failure": a forbidden name (22), symlink/socket (1),
# the FS lacks the capability (ENOSYS)
_NB_FS_ERR_RX = re.compile(r"failed: (Invalid argument \(22\)|Operation not permitted \(1\)|"
                           r"Function not implemented)")

def _fs_type(path):
    """FS type for a path (from /proc/mounts, nearest mount point). "" — unknown."""
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
    """Destination FS type — only when it is LOCAL (we don't probe a push over SSH)."""
    if _nb_push_ssh(cfg):
        return ""
    d = dest or cfg.get("dest_base") or ""
    return _fs_type(d) if d.startswith("/") else ""

def _dest_disk_absent(dest):
    """A dest under /mnt|/media|/srv implies a separate medium. If it is NOT mounted,
    the path falls through to root (mountpoint = '/') — writing there is unsafe:
    rsync would silently fill the system disk. The pool /mnt/storage is mounted →
    its mountpoint isn't '/', the check passes. This keeps removable disks safe too."""
    return bool(re.match(r"^/(mnt|media|srv)/", dest or "")) and _mountpoint_of(dest) == "/"

def _nb_owner_access(dest):
    """Give the destination OWNER (the panel user) access to the backed-up folders.
    Restrictive source perms (e.g. UGREEN/Synology d--------- guarded by ACLs) land on
    ext4 as literal 000 dirs — root still reads them (restore works), but the user can't
    browse the backup. Add owner rwx to dirs / rw to files; only touch items that LACK it
    (idempotent, fast on later runs). Never loosens group/other."""
    d = (dest or "").rstrip("/")
    if not d or not os.path.isabs(d) or not os.path.isdir(d):
        return
    for typ, need, add in (("d", "-700", "u+rwx"), ("f", "-600", "u+rw")):
        try:
            subprocess.run(["find", d, "-xdev", "-type", typ, "!", "-perm", need,
                            "-exec", "chmod", add, "{}", "+"],
                           capture_output=True, timeout=1800)
        except (OSError, subprocess.SubprocessError):
            pass

def nb_run(cfg, dry, writer, cancel=lambda: False, on_job=None, allow_delete=False):
    """Run all enabled jobs. writer(line) — output; cancel() — interruption.
    on_job(done, total) — after every finished job, so the UI can paint folder dots
    live instead of waiting for the whole run to end.
    allow_delete — a ONE-TIME permission from the user: lift the --max-delete guard for
    this run (they deleted many files on the source themselves and confirmed it in the panel).
    NOT written to the config: the next run is guarded again."""
    cfg = cfg or nb_load()
    jobs = [j for j in cfg.get("jobs", []) if j.get("enabled", True)]
    if not jobs:
        writer("no jobs to back up"); return {"ok": False, "jobs": []}
    t = nb_test(cfg)
    if not t.get("ok"):
        writer("CONNECTION ERROR: " + t.get("log", "")); return {"ok": False, "unreachable": True, "jobs": []}
    pid = cfg.get("id") or NB_MAIN
    try:
        with open(nb_status_file(pid)) as f:
            prevf = {x.get("src"): x.get("files", 0) for x in json.load(f).get("jobs", [])}
    except (OSError, ValueError):
        prevf = {}
    t0 = time.time()
    push, push_ssh = _nb_push(cfg), _nb_push_ssh(cfg)
    # SSH→SSH: rsync can't do this — mount the source over sshfs and copy as if
    # from a local folder (data goes through the NAS, which the panel warns about)
    stage = None
    if _nb_remote_both(cfg):
        writer("source and destination are both remote — bringing up a bridge through this NAS")
        stage, err = _nb_stage_mount(cfg, pid, writer)
        if not stage:
            writer("could not mount the source: %s" % err)
            return {"ok": False, "jobs": []}
    sides_dst = _nb_sides(cfg)[1]
    push_ssh = push_ssh or (sides_dst["kind"] == "ssh")   # remote destination — the same branch
    shell_fs = None   # push-ssh: real FS (mkdir over SSH) vs forced daemon (--mkpath)
    if push_ssh:
        shell_fs = _nb_remote_shell_fs(cfg)
        if not shell_fs:
            writer("the destination is an rsync daemon with «modules»: rsync will create the folders itself")
    if allow_delete:
        writer("USER PERMISSION: the mass-deletion guard is lifted for THIS run — "
               "extra files in the copy will be deleted (in «archive» mode — moved to the deleted-files archive)")
    dest_fs = nb_dest_fs(cfg)
    if dest_fs in NB_FS_LIMITED:
        writer("destination on %s: this filesystem does not store symlinks, special files or permissions — "
               "they will be skipped; it also rejects names with «:» and newlines"
               % dest_fs)
    # The FAT family caps a single file at 4 GiB. Unlike the notes above this is not a
    # "some files are skipped" nuisance: rsync dies with «File too large (27)» and the whole
    # job stops, so it has to be said loudly and up front (2026-07-12: a backup of game
    # repacks onto a FAT32 stick died on the first multi-gigabyte archive).
    if dest_fs in NB_FS_FAT:
        writer("WARNING: %s can't handle files larger than 4 GiB — on the first such file the run "
               "will ABORT with «File too large». Reformat the destination to ext4 (or exfat, "
               "if the disk is also needed on Windows/Mac)" % dest_fs)
    results = []
    def emit(r):
        results.append(r)
        if on_job:
            try: on_job(list(results), len(jobs))
            except Exception: pass
    for j in jobs:
        if cancel():
            writer("— cancelled —"); break
        writer("")
        writer("=== %s → %s ===" % (j["src"], j["dest"]))
        if push and not os.path.exists("/" + j["src"].lstrip("/")):
            writer("⚠ SKIPPED: the source is missing on this NAS (/%s) — the folder was deleted "
                   "or the disk is not mounted." % j["src"].lstrip("/"))
            emit({"src": j["src"], "ok": False, "src_missing": True}); continue
        if push_ssh:
            # plain server: mkdir over SSH (works with any remote rsync).
            # Forced daemon (UGREEN/Synology): the shell lives in a DIFFERENT path
            # namespace — mkdir can't reach it, rsync --mkpath creates module folders
            if shell_fs:
                mk = _nb_ssh_run(cfg, "mkdir -p " + shlex.quote(j["dest"]), timeout=25)
                if not mk["ok"]:
                    writer("could not create the folder on the destination: %s" % (mk.get("log") or "").strip()[-160:])
                    emit({"src": j["src"], "ok": False}); continue
        else:
            # belt: a local destination must be an absolute allowed path — a relative
            # one (left over from a transport switch) would be created under cwd (/)
            if not _nb_valid_dest(j["dest"]):
                writer("⚠ SKIPPED: invalid local destination (%s) — choose "
                       "a folder in /mnt, /media, /srv or /home." % j["dest"])
                emit({"src": j["src"], "ok": False}); continue
            if _dest_disk_absent(j["dest"]):
                writer("⚠ SKIPPED: the target disk is not mounted (%s leads into the system "
                       "partition). Backup skipped so as NOT to fill the system disk — "
                       "connect the target disk." % j["dest"])
                emit({"src": j["src"], "ok": False, "not_mounted": True}); continue
            try:
                os.makedirs(j["dest"], exist_ok=True)
            except OSError as e:
                writer("could not create the folder: %s" % e); emit({"src": j["src"], "ok": False}); continue
        args, env = nb_build_cmd(cfg, j, dry, prev_files=prevf.get(j["src"], 0),
                                 mkpath=bool(push_ssh and not shell_fs),
                                 allow_delete=allow_delete, stage=stage)
        stat_lines = []
        try:
            p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            writer("could not start rsync: %s" % e); emit({"src": j["src"], "ok": False}); continue
        # "Stop" = a flag file, and rsync can stay silent for minutes (building the file list) —
        # no check happens inside the read loop there at all. The watcher checks the flag itself,
        # so stopping takes ≤1 s rather than "some amount of time"
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
                        # the destination physically cannot accept this file (name with «:» or CR,
                        # symlink, socket) — not a breakage, but the limit of its filesystem
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
        ok = p.returncode in (0, 24)     # 24 = vanished files — not an error
        stt = _nb_parse_stats(stat_lines)
        if not ok and push_ssh and not shell_fs and p.returncode == 1:
            # old receiver rsync (<3.2.3) rejects --mkpath as unknown option
            writer("hint: if «unknown option» appears above — the destination has an old rsync "
                   "without --mkpath; create the folders on it manually (with the NAS file manager)")
        if p.returncode == 25:           # the --max-delete guard tripped
            pf = prevf.get(j["src"], 0)
            pctv = int(cfg.get("max_delete_pct", 20) or 0)
            res_limit = max(1, int(pf * pctv / 100.0)) if (pf and pctv) else 0
            stt.setdefault("guard_limit", res_limit)
            stt.setdefault("guard_pct", pctv)
            writer("⚠ STOPPED BY THE GUARD: too many files would be deleted (> %d%%). "
                   "Deletions were capped at the %d-file guard limit and FURTHER deletions "
                   "skipped%s. Check the source before the next run."
                   % (int(cfg.get("max_delete_pct", 20) or 0), int(res_limit or 0),
                      "" if cfg.get("delete_mode", "archive") == "archive"
                      else " — mirror mode has no _deleted archive, so the capped deletions are permanent"))
        sz = None
        if not dry and not push_ssh:
            try: sz = _du_bytes(j["dest"])
            except Exception: sz = None
            # local destination: make the copied folders accessible to the owner (see helper)
            if ok and cfg.get("dest_owner_access", True):
                _safe(lambda: _nb_owner_access(j["dest"]))
        res = {"src": j["src"], "dest": j["dest"], "ok": ok, "code": p.returncode, "size": sz,
               "files": stt.get("files", prevf.get(j["src"], 0)), "xfer": stt.get("xfer", 0),
               "xfer_bytes": stt.get("xfer_bytes", 0), "deleted": stt.get("deleted", 0)}
        if p.returncode == 25:      # the UI shows the threshold that applied and offers to allow deletion
            res["guard_limit"] = stt.get("guard_limit", 0)
            res["guard_pct"] = stt.get("guard_pct", 0)
        if cancel():
            # we killed rsync ourselves on "Stop" — not a transfer error (otherwise the panel
            # would show "rsync error, code -9", and good luck guessing it was your own stop)
            res["stopped"] = True
        elif not ok:
            if err_lines:
                res["err"] = err_lines[0][:180]
                res["errn"] = errs
            if p.returncode == 23 and fs_bad and fs_bad == errs:
                # ALL complaints are about the destination FS limit. The rest copied; a red
                # dot here would lie every run, so this is a separate, yellow outcome
                res["fs_limit"] = fs_bad
                res["fs_files"] = fs_files
                res["fs"] = dest_fs
        if ok and not dry and cfg.get("verify") and not cancel():
            vb, vn, ve = _nb_verify_job(cfg, j, writer, cancel, stage=stage)
            res["verify_bad"], res["verify_new"], res["verify_err"] = vb, vn, ve
        emit(res)
        extra = " · %d files, transferred %s" % (stt.get("xfer", 0), fmt_bytes(stt.get("xfer_bytes", 0))) if stt else ""
        writer("[%s] %s%s%s" % ("OK" if ok else ("stopped" if p.returncode == 25 else "error %d" % p.returncode),
                                j["src"], (" · " + fmt_bytes(sz)) if sz else "", extra))
    vbad = sum(int(r.get("verify_bad") or 0) for r in results)
    verr = any(r.get("verify_err") for r in results)
    stopped = cancel()          # stopped by the user — this is not a "backup with errors"
    allok = (all(r["ok"] for r in results) and len(results) == len(jobs)
             and not vbad and not verr and not stopped)
    if stage:
        _safe(lambda: _nb_stage_umount(pid))
        writer("source bridge unmounted")
    if not dry:
        try: pruned = _nb_prune_remote(cfg, writer, shell_fs) if push_ssh else _nb_prune(cfg)
        except Exception: pruned = 0
        if pruned: writer("old deleted-files snapshots cleaned: %d" % pruned)
        _nb_write_status(pid, results)
        try: _nb_history_add(pid, {"ts": int(time.time()), "dur": int(time.time() - t0),
                              "result": "stopped" if stopped else ("ok" if allok else "warn"),
                              "jobs": results})
        except Exception: pass
    return {"ok": allok, "jobs": results, "verify_bad": vbad, "verify_err": verr,
            "stopped": stopped}

def _nb_verify_job(cfg, job, writer, cancel, stage=None):
    """Post-run verify of one job: rsync -c -n re-reads both sides and compares
    checksums. Returns (mismatches, new-after-run, verify-error).
    «>f» without «+» = content differs; «>f+++» = the file appeared after the run."""
    writer("— checksum verification (re-reads all files — may take a while)…")
    args, env = nb_verify_cmd(cfg, job, stage)
    try:
        p = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        writer("verification failed to start: %s" % e); return 0, 0, True
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
        writer("— verification interrupted —")   # user cancel is not a verification failure
        return len(bad), new, False
    if p.returncode not in (0, 24):
        writer("verification finished with an error (code %d)" % p.returncode)
        return len(bad), new, True
    if bad:
        writer("⚠ VERIFY: the content of %d file(s) differs from the source%s" %
               (len(bad), " (first 20 shown)" if len(bad) > 20 else ""))
    else:
        writer("verification OK — no mismatches" +
               (" · %d new files appeared after the run" % new if new else ""))
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
        writer("module-style destination: auto-cleanup of the deleted-files archive is unavailable — clean %s manually" % top)
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
                writer("could not remove old snapshot %s: %s" % (p, (rr.get("log") or "").strip()[-120:]))
    return removed

def _nb_parse_stats(lines):
    """Extract numbers from the rsync --stats block."""
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
    hist = hist[-50:]                    # last 50 runs
    try:
        _json_save(nb_history_file(pid), hist)
    except OSError:
        pass

def _nb_run_bytes(run):
    """Transferred bytes of one history entry: the run itself carries no totals,
    they live in its per-source jobs."""
    return sum(int(j.get("xfer_bytes") or 0)
               for j in (run.get("jobs") or []) if isinstance(j, dict))

def _nb_run_files(run):
    return sum(int(j.get("xfer") or 0)
               for j in (run.get("jobs") or []) if isinstance(j, dict))

def nb_history(pid=None):
    pid = _nb_pid(pid)
    try:
        with open(nb_history_file(pid)) as f:
            hist = json.load(f)
        return list(reversed(hist)) if isinstance(hist, list) else []
    except (OSError, ValueError):
        return []

def nb_history_clear(pid=None, ts=None):
    """ts=None → wipe the whole profile history; otherwise delete one entry by its ts."""
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
    """The destination's actual state NOW: whether the job folders exist and are non-empty.
    Fast (isdir/listdir, no du). Catches manual deletion of folders from the destination —
    which the last run's dots don't see."""
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
    """Tail of the current/last run's log (from file) — for UI reconnection."""
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
    cur_line = ""                            # the last rsync progress line (for the UI bar)
    for l in reversed(lines[-8:]):
        if "%" in l and "/s" in l:
            cur_line = l.strip(); break
    base = 0
    if len(lines) > 2000:                    # cap the response size
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
#  Backup health — periodic checks (events nb_conn/nb_srcmiss/nb_stale/
#  nb_size/nb_dest). Run at most once every 30 min and only if at least one
#  check is enabled AND the backup is configured (has an address and jobs) — otherwise silent.
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
    """Periodic backup checks; fire()/ev/pri/thr — from monitor_tick.
    Run per profile separately: its own cooldown, its own health file."""
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
    # with multiple profiles "NAS backup: source unreachable" is useless —
    # append the name, and make the cooldown key per-profile
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
            fire_p("nb_conn", "Backup: destination unreachable" if push else "NAS backup: source unreachable",
                 "Cannot connect to %s: %s" % (cfg.get("host"), t.get("log", "")),
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
            fire_p("nb_srcmiss", "NAS backup: source folder disappeared",
                 "Missing on the source: " + ", ".join(missing), pri("nb_srcmiss"), ev_name="nb_srcmiss", lvl="warn")
    # --- no run for a long time ---
    if ev.get("nb_stale", {}).get("on"):
        days = thr("nb_stale", 7)
        try:
            with open(nb_status_file(pid)) as f:
                ts = json.load(f).get("ts", 0)
        except (OSError, ValueError):
            ts = 0
        if days > 0 and not nb_run_active(pid):
            if not ts:
                fire_p("nb_stale", "NAS backup: never run yet",
                     "The backup is configured, but there hasn't been a single run", pri("nb_stale"), ev_name="nb_stale", lvl="warn")
            elif now - ts > days * 86400:
                fire_p("nb_stale", "NAS backup: not updated for a long time",
                     "Last run %d days ago (threshold %d)" % (int((now - ts) / 86400), days),
                     pri("nb_stale"), ev_name="nb_stale", lvl="warn")
    if push_ssh:
        return   # do not monitor size/space of a destination on a foreign server over SSH
    # --- sharp change in destination size ---
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
                        arrow = "grew" if cur_sz > prev else "shrank"
                        fire_p("nb_size", "NAS backup: size changed sharply",
                             "Destination %s by %.0f%% (%s → %s)" % (arrow, delta, fmt_bytes(prev), fmt_bytes(cur_sz)),
                             pri("nb_size"), ev_name="nb_size", lvl="warn")
                hs["dest_size"] = cur_sz
    # --- destination: free space / whether it is mounted ---
    if ev.get("nb_dest", {}).get("on"):
        base_dir = cfg.get("dest_base") or "/mnt/storage/nas-backup"
        top = "/mnt/storage" if base_dir.startswith("/mnt/storage") else base_dir
        if top == "/mnt/storage" and not os.path.ismount("/mnt/storage"):
            fire_p("nb_dest", "NAS backup: destination not mounted",
                 "The pool /mnt/storage is not mounted — nowhere to write the backup", pri("nb_dest"), ev_name="nb_dest", lvl="warn")
        elif not os.path.isdir(base_dir):
            pass   # destination missing (USB disk unplugged) — nothing to say about space
        else:
            try:
                st = os.statvfs(base_dir)
                used = 100.0 * (st.f_blocks - st.f_bfree) / max(st.f_blocks, 1)
                lim = thr("nb_dest", 95)
                if used >= lim:
                    fire_p("nb_dest", "NAS backup: low space on the destination",
                         "%s is %.0f%% full (threshold %d%%)" % (base_dir, used, lim),
                         pri("nb_dest"), ev_name="nb_dest", lvl="warn")
            except OSError:
                pass
    _nb_health_save(pid, hs)

def _nb_start_unit(pid, dry, allow_delete=False):
    """Bring up a transient unit with the run driver. True — the process started."""
    try:
        if os.path.exists(nb_run_cancel(pid)):
            os.remove(nb_run_cancel(pid))
    except OSError:
        pass
    # initial status (the driver overwrites it) — so the UI immediately sees "running"
    _nb_run_state_write(pid, {"running": True, "started": int(time.time()), "dry": bool(dry),
                              "cur": "", "result": None})
    cmd = ["systemd-run", "--collect", "--quiet", "--unit", nb_unit(pid),
           "--setenv=SUDO_USER=" + TARGET_USER, "--setenv=HOME=" + HOME,   # the same NAS_CONFIG as the service
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
    """Start a profile run. EXACTLY ONE run happens at a time: while busy,
    the rest wait in a queue (two rsyncs on one HDD only get in each other's way)."""
    cfg = nb_load(pid); pid = cfg["id"]
    if nb_run_active(pid):
        return {"ok": False, "log": "already running"}
    if nb_any_active():
        _nb_queue_add(pid, dry, allow_delete)
        return {"ok": True, "queued": True, "log": "queued"}
    _nb_queue_remove(pid)
    if not _nb_start_unit(pid, dry, allow_delete):
        return {"ok": False, "log": "failed to start"}
    return {"ok": True, "queued": False, "log": "started"}

def _nb_queue_drain():
    """Once a minute: if nothing is running — start the first one in the queue."""
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
    """Run driver (started in a transient unit). Writes the log to a file and
    the status to json — regardless of whether the main nas-web process is alive."""
    cfg = nb_load(pid); pid = cfg["id"]
    many = len(nb_profiles()) > 1
    sfx = (" · " + cfg["name"]) if many else ""
    started = int(time.time())
    _nb_run_state_write(pid, {"running": True, "started": started, "dry": bool(dry),
                              "cur": "", "result": None, "pid": os.getpid()})
    try:
        logf = open(nb_run_log(pid), "w", buffering=1)     # truncate and open for line-buffered appends
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
    title = ("Backup from this NAS" if push else "Main NAS backup") + sfx
    res = None
    try:
        r = nb_run(cfg, dry, w, cancel, on_job, allow_delete)
        res = ("ok" if r.get("ok") else "stopped" if r.get("stopped")
               else "unreachable" if r.get("unreachable") else "warn")
        if not dry and r.get("stopped"):
            try: log_event("nas_backup", title, "stopped by hand", "info", kind="backup", desk=True)
            except Exception: pass
        if not dry and not r.get("stopped"):
            guarded = [j.get("src") for j in r.get("jobs", []) if j.get("code") == 25]
            if guarded:      # the mass-deletion guard tripped — a separate important notification
                try: notify_event("nb_guard", "nb_guard:" + pid, "NAS backup: stopped by guard" + sfx,
                                  "Mass-deletion guard triggered: " + ", ".join(guarded) +
                                  ". Nothing was deleted — check the source (did you wipe or unmount it?).",
                                  "crit", cooldown=0)
                except Exception: pass
            vbad, verr = int(r.get("verify_bad") or 0), bool(r.get("verify_err"))
            if vbad or verr:      # checksum verify found mismatches / could not finish
                try: notify_event("nb_verify", "nb_verify:" + pid, "Backup: verification found problems" + sfx,
                                  ("The content of %d file(s) in the copy differs from the source — "
                                   "details in the run log." % vbad) if vbad
                                  else "The verification could not finish — see the run log.",
                                  "warn", cooldown=0)
                except Exception: pass
            msg = ("all tasks done" + (" · verification OK" if cfg.get("verify") and not vbad and not verr else "")) if r.get("ok") \
                else (("destination unreachable — skipped" if push else "main NAS unreachable — skipped") if r.get("unreachable")
                      else ("verification: %d mismatches" % vbad if vbad else "some tasks had errors"))
            try: log_event("nas_backup", title, msg, "ok" if r.get("ok") else "warn", kind="backup", desk=True)
            except Exception: pass
    except Exception as e:
        res = "warn"; w("failure: %s" % e)
        try: log_event("nas_backup", title, "failure: %s" % e, "warn", kind="backup", desk=True)
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

# --------------------------------------------------------------------------- #
#  Compare with the source: an on-demand rsync dry-run (--itemize) that answers
#  "is the backup identical to the server?" without copying anything. Runs in its
#  OWN transient unit (as root, like the backup) so it survives closing the window
#  and reads restrictively-permissioned folders the panel user can't. Two modes:
#  quick (size+time, seconds-minutes) and deep (--checksum, proves byte-identity).
# --------------------------------------------------------------------------- #
def nb_compare_state_file(pid): return _nb_f(pid, "compare", "json")
def nb_compare_cancel(pid):     return _nb_f(pid, "compare", "cancel")
def nb_compare_unit(pid):       return "nas-backup-cmp" + ("" if pid == NB_MAIN else "-" + pid)

def _systemd_active(unit):
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=8)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False

def nb_compare_cmd(cfg, job, deep, stage=None):
    """rsync dry-run that only REPORTS differences (never writes). %i|%l|%n =
    itemize flags | length | name — enough to classify new/changed/deleted + size."""
    env, rsh = _nb_cmd_ctx(cfg, stage)
    # mirror the real run's fs-aware flags: on FAT/exFAT/NTFS the run uses -rt with
    # --modify-window=1 and drops -l/-D — without matching that, compare flags routine
    # 2s-rounded times and skipped symlinks/specials as differences (false non-identical)
    limited = nb_dest_fs(cfg, job.get("dest")) in NB_FS_LIMITED
    args = ["rsync", "-n"] + (["-rt", "--modify-window=1"] if limited else ["-rltD"]) + \
           ["--no-owner", "--no-group", "--no-perms",
            "--delete", "--stats", "--out-format=%i|%l|%n"] + rsh
    if deep:
        args.append("--checksum")                       # re-read every file → byte-identity proof
    args.append("--exclude=/" + nb_deleted_top(cfg))    # our own _deleted archive isn't a "difference"
    for ex in cfg.get("excludes", []):
        args.append("--exclude=" + ex)
    for ex in job.get("excludes", []):
        args.append("--exclude=" + str(ex))
    if cfg.get("transport") == "ssh" and cfg.get("remote_sudo"):
        args.append("--rsync-path=sudo rsync")
    args += _nb_src_dst(cfg, job, stage)
    return args, env

_CMP_DEPTH = 7          # aggregate the folder tree down to this many levels (deeper rolls up)
_CMP_FILES = 80         # sample changed files listed per folder node (rest → node "fo" overflow count)

def _nb_compare_job(cfg, job, deep, on_scan, cancelled):
    """Run one job's compare, return {summary, tree}. tree nodes: {n,c,d,nb,cb,ch,f,fo}."""
    args, env = nb_compare_cmd(cfg, job, deep)
    tree = {"n": 0, "c": 0, "d": 0, "nb": 0, "cb": 0, "ch": {}}
    summ = {"new": 0, "changed": 0, "deleted": 0, "new_bytes": 0, "changed_bytes": 0,
            "identical": 0, "total": 0}
    def add(path, kind, size):
        parts = [p for p in path.rstrip("/").split("/") if p]
        if not parts:
            return
        dirs, fname = parts[:-1], parts[-1]             # roll counts up into ancestors, keep the file itself
        def bump(nd):
            nd[kind] = nd.get(kind, 0) + 1
            if kind == "n": nd["nb"] = nd.get("nb", 0) + size
            elif kind == "c": nd["cb"] = nd.get("cb", 0) + size
        node = tree
        bump(node)
        used = 0
        for part in dirs:
            if used >= _CMP_DEPTH:
                break
            node = node["ch"].setdefault(part, {"n": 0, "c": 0, "d": 0, "nb": 0, "cb": 0, "ch": {}})
            bump(node)
            used += 1
        # attach the file to its nearest folder node; files below the depth cap keep their tail path
        label = fname if used == len(dirs) else "/".join(parts[used:])
        fl = node.setdefault("f", [])
        if len(fl) < _CMP_FILES:
            fl.append([label, kind, size])
        else:
            node["fo"] = node.get("fo", 0) + 1
    reg_total = None
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=env, text=True, bufsize=1)
    except (OSError, subprocess.SubprocessError) as e:
        return {"summary": summ, "tree": tree, "error": str(e)}
    scanned = 0
    for line in p.stdout:
        if cancelled():
            try: p.terminate()
            except OSError: pass
            break
        line = line.rstrip("\n")
        if not line:
            continue
        if "|" in line and line[0] in ">c.*":
            flags, _, rest = line.partition("|")
            length, _, name = rest.partition("|")
            if not name:
                continue
            if flags.startswith("*deleting"):
                add(name, "d", 0); summ["deleted"] += 1
            elif len(flags) >= 2 and flags[0] in ">c" and flags[1] != "d":
                try: size = int(length or 0)
                except ValueError: size = 0
                if flags[2:] and flags[2:].strip("+") == "":     # all '+' → brand new
                    add(name, "n", size); summ["new"] += 1; summ["new_bytes"] += size
                else:                                            # size/time/checksum differs
                    add(name, "c", size); summ["changed"] += 1; summ["changed_bytes"] += size
            scanned += 1
            if scanned % 400 == 0:
                on_scan(scanned)
        elif line.startswith("Number of files:"):
            m = re.search(r"reg:\s*([\d,]+)", line)
            if m:
                reg_total = int(m.group(1).replace(",", ""))
    try: p.wait(timeout=10)
    except (OSError, subprocess.SubprocessError): pass
    if reg_total is not None:
        summ["total"] = reg_total
        summ["identical"] = max(0, reg_total - summ["new"] - summ["changed"])
    return {"summary": summ, "tree": tree}

def nb_compare_run(pid=None, deep=False):
    """Compare driver (runs in the transient unit, as root)."""
    cfg = nb_load(pid); pid = cfg["id"]
    state = {"running": True, "deep": bool(deep), "started": int(time.time()),
             "cur": "", "scanned": 0, "shares": [], "done": None, "ts": int(time.time())}
    last = [0.0]
    def flush(force=False):
        now = time.time()
        if force or now - last[0] >= 2:
            last[0] = now; state["ts"] = int(now)
            _json_save(nb_compare_state_file(pid), state, indent=None)
    flush(True)
    def cancelled():
        return os.path.exists(nb_compare_cancel(pid))
    def on_scan(n):
        state["scanned"] = n; flush()
    try:
        for job in [j for j in cfg.get("jobs", []) if j.get("enabled", True)]:
            if cancelled():
                break
            state["cur"] = str(job.get("src") or job.get("dest")); flush(True)
            res = _nb_compare_job(cfg, job, deep, on_scan, cancelled)
            res["src"] = str(job.get("src") or job.get("dest"))
            state["shares"].append(res); flush(True)
    except Exception as e:
        state["error"] = str(e)
    finally:
        ov = {"new": 0, "changed": 0, "deleted": 0, "new_bytes": 0, "changed_bytes": 0,
              "identical": 0, "total": 0}
        for s in state["shares"]:
            for k in ov:
                ov[k] += (s.get("summary", {}) or {}).get(k, 0)
        state["summary"] = ov
        state["running"] = False; state["cur"] = ""; state["done"] = int(time.time())
        state["stopped"] = cancelled()
        _json_save(nb_compare_state_file(pid), state, indent=None)
        try:
            if os.path.exists(nb_compare_cancel(pid)): os.remove(nb_compare_cancel(pid))
        except OSError: pass

def nb_compare_state(pid=None):
    cfg = nb_load(pid); pid = cfg["id"]
    st = _json_load_strict(nb_compare_state_file(pid), {})
    if st.get("running") and time.time() - (st.get("started") or 0) > 15 \
            and not _systemd_active(nb_compare_unit(pid)):
        st["running"] = False; st["done"] = st.get("done") or int(time.time())   # orphaned (crash/reboot)
    return st

def nb_compare_bg(pid=None, deep=False):
    cfg = nb_load(pid); pid = cfg["id"]
    if nb_compare_state(pid).get("running"):
        return {"ok": False, "log": "compare already running"}
    try:
        if os.path.exists(nb_compare_cancel(pid)): os.remove(nb_compare_cancel(pid))
    except OSError:
        pass
    _json_save(nb_compare_state_file(pid), {"running": True, "deep": bool(deep),
               "started": int(time.time()), "cur": "", "scanned": 0, "shares": []}, indent=None)
    cmd = ["systemd-run", "--collect", "--quiet", "--unit", nb_compare_unit(pid),
           "--setenv=SUDO_USER=" + TARGET_USER, "--setenv=HOME=" + HOME,
           sys.executable, os.path.join(HERE, "nas-web.py"), "backup-compare", pid] \
        + (["deep"] if deep else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        _json_save(nb_compare_state_file(pid), {"running": False, "error": str(e)}, indent=None)
        return {"ok": False, "log": str(e)}
    if r.returncode != 0:
        _json_save(nb_compare_state_file(pid), {"running": False, "error": r.stderr[:200]}, indent=None)
        return {"ok": False, "log": (r.stderr or "failed to start")[:200]}
    return {"ok": True, "log": "started"}

def nb_compare_cancel_req(pid=None):
    cfg = nb_load(pid); pid = cfg["id"]
    try:
        with open(nb_compare_cancel(pid), "w") as f: f.write("1")
    except OSError:
        pass
    try:
        subprocess.run(["systemctl", "stop", nb_compare_unit(pid)], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    st = _json_load_strict(nb_compare_state_file(pid), {})
    st["running"] = False; st["stopped"] = True; st["done"] = int(time.time())
    _json_save(nb_compare_state_file(pid), st, indent=None)
    return {"ok": True}

def nb_schedule_due(cfg, nowt):
    """Whether it's time to run on schedule (called once a minute from monitor_loop)."""
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
    # 0 = disabled (for *_days); all values go into the settings backup along with the file
    d = {"trash_days": 30, "pool_alias": "",
         "duscan_hours": 0,         # auto-refresh of the space analyzer: rescan volumes with a cache older than N hours (0 = off)
         "thumb_cache_mb": 512,     # FM thumbnail cache limit, MB (0 = no limit)
         "import_stale_hours": 24,  # remove abandoned .incomplete-* older than N hours (0 = leave alone)
         "import_keep_days": 0,     # delete imports older than N days (0 = keep forever)
         "import_warm_thumbs": True,  # warm up previews right after the import
         "myspeed_url": "http://127.0.0.1:5216",  # MySpeed widget ("" = disable)
         "myspeed_password": "",                  # if a password is enabled in MySpeed
         "smart_scan_min": 10,      # background SMART-status polling, minutes
         "smart_short_days": 7,     # short disk self-test, every N days (at night)
         "smart_long_days": 30,     # long self-test, every N days (at night)
         "backup_days": 7,          # auto settings backup, every N days
         "backup_keep": 10,         # how many backups to keep
         "settings_backup_dir": "",     # settings backup path ("" = /mnt/storage/nas-settings-backup)
         "settings_backup_hide": True,  # hide this folder in the file manager (default yes)
         "snap_sync_time": "03:00",   # SnapRAID: daily sync
         "snap_scrub_dow": "Sun",     # SnapRAID: day of week for scrub
         "snap_scrub_time": "05:00",  # SnapRAID: scrub time
         "automount_recover": True,   # auto-remount of a dropped disk
         "summary_enabled": False,    # status summary (to Pushover/journal)
         "summary_freq": "daily",     # daily | weekly
         "summary_time": "09:00",     # HH:MM
         "summary_dow": "Mon",        # for weekly
         "thermal_mode": "warn",      # off | warn | auto (active thermal protection)
         "thermal_hot": 80,           # "hot" threshold, °C
         "thermal_crit": 85}          # "critical" threshold (in auto — stop the container)
    saved = _json_load_strict(MAINT_FILE, {})
    if isinstance(saved, dict):
        d.update(saved)
    return d

_ALIAS_RESERVED = ("/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/media", "/mnt",
                   "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var")

_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def _snap_sched_apply(cfg):
    """SnapRAID schedule: a drop-in override for the wizard's timers.
    If the timers don't exist yet (SnapRAID not configured) — quietly exit; it
    applies at service start after configuration."""
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
    """Symlink alias for the pool (e.g. /volume2 → /mnt/storage). Returns ''/error.
    Remove the old alias only if it is OUR symlink to the pool."""
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
            return "path %s already exists and is not a pool alias" % alias
        os.symlink(STORAGE, alias)
        return ""
    except OSError as e:
        return str(e)

def save_maintenance(d):
    cur = load_maintenance()
    err = ""
    # numeric settings: key → (min, max)
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
    # SnapRAID schedule
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
                log_event("action", "SnapRAID schedule changed",
                          "sync %s · scrub %s %s" % (cur["snap_sync_time"], cur["snap_scrub_dow"], cur["snap_scrub_time"]),
                          "ok", kind="protect", desk=False)
            except Exception:
                pass
    if "pool_alias" in d:
        v = str(d["pool_alias"] or "").strip().rstrip("/")
        if v and (not re.match(r"^/[A-Za-z0-9._-]{1,32}$", v) or v.lower() in _ALIAS_RESERVED):
            err = "invalid name: a single word at the root, latin letters/digits (e.g. /volume2)"
        else:
            err = _pool_alias_apply(v, cur.get("pool_alias", ""))
            if not err:
                if v != cur.get("pool_alias", ""):
                    try:
                        log_event("action", ("Pool alias: %s → /mnt/storage" % v) if v
                                  else "Pool alias disabled", "", "ok", kind="disk", desk=False)
                    except Exception:
                        pass
                cur["pool_alias"] = v
    # path and hiding of the settings backup folder
    if "settings_backup_dir" in d:
        v = str(d["settings_backup_dir"] or "").strip().rstrip("/")
        if v == "" or (v.startswith("/") and len(v) > 1 and ".." not in v and v not in _ALIAS_RESERVED):
            cur["settings_backup_dir"] = v
        else:
            err = err or "invalid path: must be absolute, not a system root"
    if "settings_backup_hide" in d:
        cur["settings_backup_hide"] = bool(d["settings_backup_hide"])
    # --- reliability/reports/thermal protection ---
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
    """Once a day: auto-cleanup of the trash + weekly auto settings backup."""
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
#  Backup of ALL settings: nas-config (without journals/history), wizard configs,
#  the web password, samba, stack compose files; fstab/snapraid — for reference
#  (not restored automatically: the hardware may differ).
# --------------------------------------------------------------------------- #
import tarfile, io

BACKUP_KEEP = 10
_BK_NAME_RE = re.compile(r"^nas-settings-[\w.-]+\.tar\.gz$")
# archive prefix → (source, whether to restore automatically)
_BK_EXCLUDE = ("events.json", "history.json", "history-long.json", "sessions.json")

# Sections for selective restore: (key, title, secret?, prefixes in the archive).
# The order matters twice: it's also the order in the dialog, and a file's section is the FIRST
# matching prefix (otherwise "etc/nas-wizard/" from maint would swallow notify.conf).
# Secret sections are unchecked by default in the dialog: restoring an old archive
# must not silently replace the login password with the one from half a year ago.
_BK_SECTIONS = (
    ("desktop",   "Desktop",                   False, ("nas-config/desktop.json", "nas-config/winpos.json",
                                                       "nas-config/wallpaper.", "nas-config/fm-favorites.json",
                                                       "nas-config/icons/")),
    ("notify",    "Notifications",             False, ("nas-config/monitor.json", "etc/nas-wizard/notify.conf")),
    ("maint",     "Maintenance and schedules", False, ("nas-config/maintenance.json", "etc/nas-wizard/")),
    ("samba",     "Shared folders (Samba)",    False, ("etc/samba/", "var/lib/samba/")),
    ("stacks",    "Docker stacks",             False, ("opt/stacks/",)),
    ("disks",     "Disks and pool",            False, ("nas-config/fstab.",)),
    ("webauth",   "Panel password",            True,  ("etc/nas-os/webauth.json",)),
    ("nasbackup", "Main NAS backup",           True,  ("etc/nas-os/nas-backup.json", "nas-config/nas-backup-",
                                                       # store.json / remotes.json contain SSH passwords,
                                                       # root/.ssh/nas-backup — the key for push backups to servers,
                                                       # credentials.json — the per-service login/password store
                                                       "nas-config/store.json", "nas-config/remotes.json",
                                                       "nas-config/credentials.json",
                                                       "root/.ssh/nas-backup")),
    ("smbpw",     "SMB passwords (cleartext)",  True,  ("etc/nas-os/smb-users.json",)),
    ("network",   "Network (Wi-Fi, IP)",       True,  ("reference/etc/netplan/",)),   # Wi-Fi password → secret; restore by hand (reference)
    ("other",     "Other",                     False, ()),      # everything not matched above
)

# a short human "what gets overwritten" for each section (restore dialog)
_BK_DESC = {
    "desktop":   "theme, wallpaper, window and shortcut layout",
    "notify":    "notification rules and Pushover",
    "maint":     "maintenance and auto-backup schedules",
    "samba":     "the list of shared folders and their access passwords",
    "stacks":    "stack compose and .env (the container data itself is NOT touched)",
    "disks":     "the saved disk configuration (for reference)",
    "webauth":   "the web-panel login password",
    "nasbackup": "«Backup» app profiles, SSH passwords and key",
    "smbpw":     "cleartext SMB user passwords (so the panel can show them)",
    "network":   "netplan network profiles: Wi-Fi and static IP (placed alongside, applied manually)",
    "other":     "external screen token, main storage choice, operation history",
}

def _bk_section(nm):
    for key, _title, _secret, prefixes in _BK_SECTIONS:
        for p in prefixes:
            if nm.startswith(p):
                return key
    return "other"

def _bk_restorable(m, nm):
    """Archive members that can be restored at all (reference/* — reference only).
    .git is rejected on restore too: old archives carry it inside, and spilling
    a foreign history over the working nas-config repository is not allowed."""
    parts = nm.split("/")
    return m.isreg() and nm != "manifest.json" and not nm.startswith("reference/") \
        and ".." not in parts and ".git" not in parts and not nm.startswith("/")

def settings_backup_inspect(path):
    """Which sections are in the archive — for the checkbox dialog."""
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
        return {"ok": False, "log": "bad archive: %s" % e}
    return {"ok": True, "sections": [
        {"key": k, "title": t, "secret": s, "files": seen[k][0], "bytes": seen[k][1],
         "desc": _BK_DESC.get(k, "")}
        for k, t, s, _p in _BK_SECTIONS if k in seen]}

def settings_backup_path():
    """Where to put the settings backup: a custom path from maintenance or the default.
    Default — on the pool (survives reinstall), a separate folder (not the shared «backups»)."""
    custom = (load_maintenance().get("settings_backup_dir") or "").strip().rstrip("/")
    if custom and custom.startswith("/") and ".." not in custom:
        return custom
    # the settings backup must ALWAYS exist (it's used to bring the system up after
    # a reinstall), so without mounted storage — onto the system disk
    return os.path.join(storage_root(), "nas-settings-backup") if storage_root() \
        else "/var/backups/nas-os"        # storage_root() = the pool OR the chosen USB

def settings_backup_dir():
    d = settings_backup_path()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d

def _bk_add_file(tar, src, arc):
    try:
        if os.path.isfile(src) and os.path.getsize(src) <= 32 * 1024 * 1024:  # covers a 30 MB wallpaper
            tar.add(src, arcname=arc, recursive=False)
            return arc
    except OSError:
        pass
    return None

# Recreatable and endless: the space analyzer cache, backup run logs,
# leftovers after a failure. The .git directory of the nas-config repository even
# more so: it weighed more than all settings combined and spilled over a foreign history.
_BK_SKIP_DIRS = (".git",)
_BK_SKIP_FILE = re.compile(r"^duscan-.+\.json$|\.(log|tmp|bad)$")

def _bk_sources():
    """(src, arcname) of all backup files. Directories are walked whole —
    future settings land in the backup automatically."""
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
                   ("/etc/samba/nas-shares.conf", "etc/samba/nas-shares.conf"),   # panel-managed shares
                   ("/etc/nas-os/smb-users.json", "etc/nas-os/smb-users.json"),   # cleartext SMB passwords → secret section
                   ("/var/lib/samba/private/passdb.tdb", "var/lib/samba/private/passdb.tdb"),
                   # SSH key of the «Backup» app (key-based push to servers): without it
                   # key-based push profiles stop authenticating after a reinstall
                   (NB_KEY, "root/.ssh/nas-backup"),
                   (NB_KEY + ".pub", "root/.ssh/nas-backup.pub"),
                   ("/etc/fstab", "reference/etc/fstab"),
                   ("/etc/snapraid.conf", "reference/etc/snapraid.conf"),
                   ("/etc/exports", "reference/etc/exports")):
        out.append((p, arc))
    # Network: netplan profiles (Wi-Fi with a password/key, static IP). Under reference —
    # restore by hand: names are tied to hardware/UUID, blindly applying over a
    # fresh image is not allowed, but having the Wi-Fi password and settings at hand
    # after a reinstall is priceless (otherwise the NAS may be left without network).
    for np in sorted(glob.glob("/etc/netplan/*.yaml")):
        out.append((np, "reference/etc/netplan/" + os.path.basename(np)))
    if os.path.isdir(STACKS_DIR):            # stack compose/env (not volume data)
        for root, _, files in os.walk(STACKS_DIR):
            for fn in files:
                if re.match(r"^(compose|docker-compose)\.ya?ml$|^\.env$|\.(yml|yaml|env|txt|md)$", fn):
                    p = os.path.join(root, fn)
                    out.append((p, "opt/stacks/" + os.path.relpath(p, STACKS_DIR)))
    return out

def settings_backup_make(auto=False):
    d = settings_backup_dir()
    try:
        os.chmod(d, 0o700)      # inside the archives — the main NAS password and the panel password hash
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
    # rotation (how many to keep — the backup_keep setting)
    try:
        keep = load_maintenance().get("backup_keep", BACKUP_KEEP)
        old = sorted(f for f in os.listdir(d) if _BK_NAME_RE.match(f))
        for f in old[:-keep]:
            os.remove(os.path.join(d, f))
    except OSError:
        pass
    try:
        log_event("action", "Settings backup created" + (" (scheduled)" if auto else ""),
                  "%s · files: %d" % (path, len(added)), "ok", kind="action", desk=False)
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
    custom = bool((cfg.get("settings_backup_dir") or "").strip())
    # ephemeral = sits on the system disk (pool not mounted and no path set):
    # such a backup is wiped on an OS reinstall — the very thing it's made for
    ephemeral = (not custom) and d.startswith("/var/")
    return {"ok": True, "dir": d, "days": cfg.get("backup_days", 7),
            "keep": cfg.get("backup_keep", BACKUP_KEEP), "list": out,
            "ephemeral": ephemeral, "custom": custom,
            "pool": bool(storage_root())}

def settings_backup_restore(path, sections=None):
    """Restore from an archive. Check every member: regular files only,
    no ../, only known prefixes, a sane size. reference/* is not touched.
    sections=None — restore everything (compatibility with the old client), otherwise
    only the listed sections from _BK_SECTIONS."""
    global _events, _history, _history_long
    sel = None if sections is None else {s for s in sections if isinstance(s, str)}
    if sel is not None and not sel:
        return {"ok": False, "log": "no section selected"}
    # archive prefixes → where to restore (STACKS_DIR is defined further down the file)
    restore_map = [("etc/nas-wizard/", "/etc/nas-wizard"),
                   ("etc/nas-os/webauth.json", "/etc/nas-os/webauth.json"),
                   ("etc/nas-os/nas-backup.json", "/etc/nas-os/nas-backup.json"),
                   ("etc/samba/smb.conf", "/etc/samba/smb.conf"),
                   ("var/lib/samba/private/passdb.tdb", "/var/lib/samba/private/passdb.tdb"),
                   ("opt/stacks/", STACKS_DIR),
                   ("root/.ssh/nas-backup", "/root/.ssh/nas-backup"),
                   ("root/.ssh/nas-backup.pub", "/root/.ssh/nas-backup.pub")]
    restored, skipped, deselected = [], [], 0
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar:
                nm = m.name.lstrip("./")
                parts = nm.split("/")
                # reject non-files, path escapes, and any .git member (an old archive
                # could otherwise spill a foreign git tree into ~/nas-config)
                if not m.isreg() or ".." in parts or nm.startswith("/") or ".git" in parts:
                    continue
                # an unchecked section is not an error but a deliberate choice: not in skipped
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
                # secrets: the panel password, Pushover keys, the main NAS password
                if any(x in dest for x in ("webauth", "notify.conf", "nas-backup.json",
                                           "store.json", "remotes.json", "credentials.json")):
                    os.chmod(dest, 0o600)
                # push-backup SSH key: private key 0600, its dir 0700 (else ssh refuses it)
                if dest.startswith("/root/.ssh/"):
                    _safe(lambda: os.chmod(os.path.dirname(dest), 0o700))
                    if not dest.endswith(".pub"):
                        _safe(lambda: os.chmod(dest, 0o600))
                restored.append(nm)
    except (OSError, tarfile.TarError) as e:
        return {"ok": False, "log": "bad archive: %s" % e}
    if not restored:
        return {"ok": False, "log": "no files in the selected sections" if deselected
                else "the archive has no NAS-OS settings files"}
    # reset in-memory caches, apply disk spindown
    with _events_lock:
        _events = None
    with _hist_lock:
        _history = None; _history_long = None
    try:
        apply_spindown_all()
    except Exception:
        pass
    titles = dict((k, t) for k, t, _s, _p in _BK_SECTIONS)
    what = ("sections: " + ", ".join(titles[k] for k, _t, _s, _p in _BK_SECTIONS if k in sel)) \
        if sel is not None else "all sections"
    try:
        log_event("action", "Settings restored from backup",
                  "%s · files: %d%s" % (what, len(restored),
                                         (" · skipped: %d" % len(skipped)) if skipped else ""),
                  "warn", kind="action", desk=False)
    except Exception:
        pass
    return {"ok": True, "restored": len(restored), "skipped": len(skipped),
            "log": "files restored: %d — refresh the page" % len(restored)}

def _settings_backup_auto():
    """Auto backup every backup_days days (0 = disabled); called from maintenance_daily."""
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

# --- periodic SMART self-tests (short/long) at night ------------------
SMARTTEST_FILE = os.path.join(NAS_CONFIG, "smart-selftest.json")

def _smart_selftest_tick():
    """Every N days run a disk self-test (between 03:00–06:00, one kind per night;
    the long one takes priority). The test runs inside the disk and doesn't interfere."""
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
                log_event("action", "SMART self-test (%s) started" % ("long" if kind == "long" else "short"),
                          "Disks: " + ", ".join(devs), "info", kind="disk", desk=False)
            except Exception:
                pass
        break                      # one kind of test per night

def _nb_sched_tick():
    """Start the main NAS backup on schedule (once a minute, no repeat in the same minute)."""
    global _nb_last_sched
    now = time.time(); slot = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    if slot != _nb_last_sched:
        _nb_last_sched = slot
        for cfg in nb_profiles():
            if nb_schedule_due(cfg, now):
                nb_run_bg(cfg["id"], dry=False)
    _nb_queue_drain()      # freed up — take the next one from the queue

def notify_event(name, key, title, msg, lvl=None, priority=None, cooldown=1800):
    """Deliver an event outside monitor_tick: always journal + Pushover, if enabled and ev.on.
    key — the cooldown key (reuses _MON_LAST, like fire())."""
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

# ---- reliability: auto-remount of a dropped disk ----
_MOUNT_TRY = {}   # mp -> time of the last attempt (at most once every 5 min)

def _fstab_targets():
    """Data/pool mount points from fstab (under /mnt) that should be mounted."""
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
        return False   # a hung/dead mount must not hold the request thread forever

def _stale_endpoint(mp):
    """A dead FUSE endpoint (mergerfs crashed): stat() gives ENOTCONN «Transport endpoint
    is not connected». mount on it fails — the point must be unmounted first (umount -l)."""
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
        stale = _stale_endpoint(mp)           # is the FUSE endpoint hung (mergerfs pool)
        if _mounted(mp) and not stale:
            _MOUNT_TRY.pop(mp, None)
            continue
        # mergerfs hung — retry more often (once a minute), a normal remount — at most every 5 min
        if now - _MOUNT_TRY.get(mp, 0) < (55 if stale else 300):
            continue
        _MOUNT_TRY[mp] = now
        if stale:
            # tear down the hung endpoint, otherwise mount gives «Transport endpoint is not connected»
            _run(["umount", "-l", mp], timeout=20)
            _run(["fusermount", "-uz", mp], timeout=20)
        _run(["mount", mp], timeout=30)
        if _mounted(mp):
            notify_event("disk_remount", "remount:%s" % mp,
                         "NAS: pool reconnected" if stale else "NAS: disk reconnected",
                         "%s was automatically remounted" % mp, "ok", cooldown=120)
    # The mergerfs pool is held by a systemd service with Restart=always — it comes up on its own in seconds.
    # Backstop in case the service is failed: the point is dead/absent → restart the service.
    if os.path.exists("/etc/systemd/system/nas-mergerfs.service"):
        if (_stale_endpoint(STORAGE) or not _mounted(STORAGE)) and \
                now - _MOUNT_TRY.get(STORAGE, 0) >= 55:
            _MOUNT_TRY[STORAGE] = now
            _run(["systemctl", "restart", "nas-mergerfs.service"], timeout=40)
            if _mounted(STORAGE):
                notify_event("disk_remount", "remount:%s" % STORAGE, "NAS: pool reconnected",
                             "%s brought up by the nas-mergerfs service" % STORAGE, "ok", cooldown=120)

def _pool_recovery():
    """State of the mergerfs pool service: whether it is active and how many times
    systemd auto-restarted it (= FUSE crashes recovered) since boot.
    None — if the pool isn't on a service yet (the old scheme via fstab)."""
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

# ---- daily/weekly status summary ----
_LAST_SUMMARY = ""

def _build_summary():
    s = _safe(stats) or {}
    host = s.get("host", "NAS")
    lines = []
    up = s.get("uptime")
    if isinstance(up, (int, float)) and up > 0:
        d, rem = divmod(int(up), 86400); h, rem = divmod(rem, 3600); mi = rem // 60
        lines.append("Uptime: " + (("%dd %dh" % (d, h)) if d else ("%dh %dm" % (h, mi)) if h else ("%dm" % mi)))
    elif up:
        lines.append("Uptime: %s" % up)
    t = s.get("temp")
    if isinstance(t, (int, float)):
        lines.append("Temperature: %d°C" % t)
    mem = (s.get("mem") or {}).get("pct")
    if isinstance(mem, (int, float)):
        lines.append("Memory: %d%%" % mem)
    try:
        u = shutil.disk_usage(STORAGE)
        lines.append("Pool: %d%% used (%.0f GB free)" % (
            round(100 * u.used / u.total), u.free / 1024**3))
    except OSError:
        pass
    try:
        h = health_report()
        bad = [c for c in (h.get("checks") or []) if c.get("lvl") in ("warn", "bad")]
        lines.append("Health: %s" % ("all normal" if not bad
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
            lines.append("NAS backup%s: %d/%d OK" % (_nm, okn, len(st.get("jobs", []))))
    except Exception:
        pass
    return "NAS: summary (%s)" % host, "\n".join(lines) or "no data"

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

# ---- active thermal protection ----
_THERM = {"hot": 0, "cool": 0, "acted": {}}   # acted: name -> {"cpus": orig, "paused": bool}
# What thermal protection stopped/throttled — persisted to disk. Otherwise a crash/reboot
# of the service while a container is paused would lose this list: the container would stay
# paused FOREVER, and the panel would "forget" it paused it itself. On start we clear orphans.
THERM_FILE = os.path.join(NAS_CONFIG, "thermal-acted.json")

def _therm_save():
    try:
        _json_save(THERM_FILE, _THERM["acted"])
    except OSError:
        pass

def _therm_recover():
    """One-time recovery at start: release what thermal protection left stopped
    before a crash/reboot. If it's still hot — the tick reacts again."""
    acted = _json_load_strict(THERM_FILE, {})
    if isinstance(acted, dict) and acted:
        _THERM["acted"] = acted
        _therm_restore()   # unpauses/restores cpus and clears the file

def _hottest_container():
    """(name, %CPU) of the most CPU-loading container, or (None, 0)."""
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
                notify_event("thermal_guard", "therm:restore", "NAS: temperature back to normal",
                             "cooled to %d°C — container limits lifted" % t, "ok", cooldown=300)
        return
    else:
        return
    if _THERM["hot"] < 3:      # react only to sustained overheating (~3 min)
        return
    victim, cpu = _hottest_container()
    if mode == "warn":
        notify_event("thermal_guard", "therm:warn",
                     "NAS: overheating %d°C" % t,
                     "temperature holding ≥%d°C%s — check cooling" % (
                         hot, (" (load: %s, %.0f%% CPU)" % (victim, cpu)) if victim else ""),
                     "warn", cooldown=1800)
        return
    # auto: throttle/pause the greediest container
    if not victim:
        notify_event("thermal_guard", "therm:auto", "NAS: overheating %d°C" % t,
                     "no culprit container — reduce load manually", "warn", cooldown=1800)
        return
    if t >= crit:
        try:
            _run(["docker", "pause", victim], timeout=15)
            _THERM["acted"].setdefault(victim, {"cpus": _container_cpus(victim)})["paused"] = True
            _therm_save()
        except Exception:
            pass
        notify_event("thermal_guard", "therm:auto",
                     "NAS: critical overheating %d°C" % t,
                     "container %s paused until it cools down" % victim, "crit", cooldown=600)
    else:
        if victim not in _THERM["acted"]:
            _THERM["acted"][victim] = {"cpus": _container_cpus(victim), "paused": False}
            _therm_save()
            _run(["docker", "update", "--cpus=0.5", victim], timeout=15)
            notify_event("thermal_guard", "therm:auto", "NAS: overheating %d°C" % t,
                         "load of %s limited (0.5 CPU) until cooldown" % victim, "warn", cooldown=900)

def _container_cpus(name):
    r = _run(["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", name], timeout=10)
    try:
        return round(int((r.get("log") or "0").strip()) / 1e9, 2) or 0
    except (ValueError, TypeError):
        return 0

# Signal file: udev hooks (USB mount/eject) touch it, the watcher wakes
# monitor_loop immediately — disk insertion is detected in ~1-2s, not on the 60s tick.
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
            if last is not None:      # first pass only remembers — don't wake on startup
                _mon_wake.set()
            last = m
        time.sleep(1.5)

def monitor_loop():
    _safe(_therm_recover)      # release containers orphaned by thermal guard before a crash/reboot
    threading.Thread(target=_poke_watcher, daemon=True).start()
    last_full = 0.0
    while True:
        poked = _mon_wake.wait(60); _mon_wake.clear()
        # on poke (disk insert/eject) run only the change-related checks — fast and
        # lean: history/schedules/self-tests are left alone, they run on their own cadence.
        # BUT a flapping USB bridge pokes every second and would keep starving the full
        # tick (thermal guard, history) — exactly when hardware misbehaves. So force the
        # full set if ~60s elapsed since it last ran, no matter how often we're poked.
        now = time.monotonic()
        full = (not poked) or (now - last_full >= 55)
        if full:
            last_full = now
        funcs = ((history_sample, monitor_tick, maintenance_daily, _smart_selftest_tick,
                  _nb_sched_tick, _automount_tick, _summary_tick, _thermal_tick, usb_ops_sync,
                  _fsw_tick, _replica_tick, _remotes_tick, _screen_tick) if full else
                 (monitor_tick, _automount_tick, usb_ops_sync))
        for fn in funcs:
            try:
                fn()
            except Exception:
                pass

# --------------------------------------------------------------------------- #
#  Docker services / stacks (GUI manager)
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
        return {"ok": False, "log": "name"}
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
            "image": c.get("Image", ""), "url": _app_host_url(url.group(1)) if url else "",
            "icon": ico.group(1) if ico else "",
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
        # stack icon: web-desktop.icon label → catalog meta.json (services/<id>/meta.json)
        icon = (next((c["icon"] for c in conts if c["icon"]), "")
                or _safe(lambda: _store_meta(nm).get("icon") or "", ""))
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
                or _safe(lambda: _store_meta(key).get("icon") or "", ""))
        out.append({"name": key, "path": wd or os.path.join(STACKS_DIR, key),
                    "has_compose": False, "orphan": True,
                    "containers": conts, "running": running, "total": len(conts),
                    "url": next((c["url"] for c in conts if c["url"]), ""),
                    "icon": icon, "note": notes.get(key, "")})
    return {"ok": True, "stacks": out, "dir": STACKS_DIR}

def stack_validate(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "name"}
    r = _dc(name, "config", "-q", timeout=30)
    return {"ok": r["ok"], "log": (r.get("log") or "").strip() or ("OK" if r["ok"] else "error")}

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
    """Summary for the Docker dashboard: containers, space (system df), version."""
    if not shutil.which("docker"):
        return {"ok": False, "log": "docker is not installed"}
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
    _w = wud_state()      # image updates (if WUD is installed)
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
        return {"ok": False, "log": "unknown"}
    return _run(cmds[what], timeout=180)

def docker_image_rm(iid):
    if not re.match(r"^[\w:./@-]+$", iid or ""):
        return {"ok": False, "log": "id"}
    return _run(["docker", "image", "rm", iid], timeout=60)

def docker_volume_rm(name):
    if not re.match(r"^[\w.-]+$", name or ""):
        return {"ok": False, "log": "name"}
    return _run(["docker", "volume", "rm", name], timeout=60)

def _read_file(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""

def stack_read(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "invalid name"}
    cp = _compose_path(name)
    return {"ok": True, "name": name, "compose": _read_file(cp),
            "env": _read_file(os.path.join(STACKS_DIR, name, ".env")),
            "exists": os.path.isfile(cp)}

def stack_save(name, compose, env, create=False):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "name: letters/digits/._-"}
    d = os.path.join(STACKS_DIR, name)
    cp = _compose_path(name)
    if create and os.path.isdir(d) and os.path.isfile(cp):
        return {"ok": False, "log": "stack already exists"}
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
        return {"ok": False, "log": "invalid name"}
    wud_invalidate()      # images may have updated — the "update available" badge will recompute
    if action == "rebuild-nocache":
        r = _dc(name, "build", "--no-cache", timeout=900)
        if not r["ok"]:
            return r
        return _dc(name, "up", "-d", timeout=300)
    amap = {"up": ["up", "-d"], "down": ["down"], "restart": ["restart"],
            "stop": ["stop"], "start": ["start"], "pull": ["pull"], "build": ["build"],
            "rebuild": ["up", "-d", "--build"], "recreate": ["up", "-d", "--force-recreate"]}
    if action not in amap:
        return {"ok": False, "log": "invalid action"}
    to = 900 if action in ("rebuild", "build", "pull") else 200
    return _dc(name, *amap[action], timeout=to)

def stack_delete(name):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "invalid name"}
    d = os.path.realpath(os.path.join(STACKS_DIR, name))
    if not d.startswith(STACKS_DIR + os.sep):
        return {"ok": False, "log": "path outside the stacks directory"}
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
    log_event("action", "Docker: removed orphaned stack %s" % name,
              "containers: %d" % len(ids), "ok", kind="svc", desk=False)
    return {"ok": True, "log": "containers removed: %d" % len(ids)}

def stack_logs(name, tail=200):
    if not _STACK_RE.match(name or ""):
        return {"ok": False, "log": "invalid name"}
    try:
        n = max(10, min(2000, int(tail)))
    except (ValueError, TypeError):
        n = 200
    r = _dc(name, "logs", "--tail", str(n), "--no-color", "--no-log-prefix", timeout=20)
    return {"ok": True, "name": name, "log": r.get("log", "")}

def container_action(cid, action):
    if not re.match(r"^[a-zA-Z0-9_.-]+$", cid or ""):
        return {"ok": False, "log": "invalid container"}
    wud_invalidate()      # recreate/restart may have changed the image — the badge will recompute
    if action not in ("start", "stop", "restart", "rm"):
        return {"ok": False, "log": "invalid action"}
    args = ["rm", "-f", cid] if action == "rm" else [action, cid]
    return _run(["docker", *args], timeout=60)

# --------------------------------------------------------------------------- #
#  services/ recipes: curated compose + meta.json tuned for this box. The old
#  standalone "App Shop" tab is gone — instead recipes that aren't installed yet
#  show up in the Docker-window sidebar as "available" stacks the user can bring
#  up in one click (install = folder copy + .env from dialog fields + compose up,
#  streamed). Custom desktop-card shortcuts were dropped. Replica (Immich) stays:
#  SSH DB dump + media rsync + version-pinned restore; store.json holds replica
#  configs (SSH passwords → secret section of the settings backup).
# --------------------------------------------------------------------------- #
SERVICES_DIR = os.path.join(HERE, "services")
STORE_FILE = os.path.join(NAS_CONFIG, "store.json")

def _store_load():
    return _json_load_strict(STORE_FILE, {})

def _store_save(d):
    _json_save(STORE_FILE, d, indent=2)
    _safe(lambda: os.chmod(STORE_FILE, 0o600))   # holds the replica SSH password

def _store_meta(sid):
    return _json_load_strict(os.path.join(SERVICES_DIR, sid, "meta.json"), {})

def _store_compose_src(sid):
    d = os.path.join(SERVICES_DIR, sid)
    for fn in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return None

def _store_subst(val):
    """Placeholders in meta defaults: {storage} {tz} {host} {rand}."""
    if not isinstance(val, str):
        return val
    if "{tz}" in val:
        val = val.replace("{tz}", _read("/etc/timezone") or "UTC")
    if "{rand}" in val:
        val = val.replace("{rand}", secrets.token_urlsafe(12))
    # {storage} in stack compose is also the primary storage (on a box without a pool
    # that's the external volume, not the nonexistent /mnt/storage)
    return val.replace("{storage}", storage_base() or STORAGE).replace("{host}", lan_ip())

def _stack_env(name):
    """KEY=VALUE map from the installed stack's .env (empty if none)."""
    out = {}
    for line in _read(os.path.join(STACKS_DIR, name, ".env")).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out

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

def stack_catalog():
    """Recipes in services/ surfaced to the sidebar. Each carries `installed`
    (already in /opt/stacks → shown as a normal stack), install `fields`, and a
    `replica` block when the recipe defines one (e.g. Immich). Not-installed
    recipes appear as "available" entries the user can bring up in one click."""
    try:
        have = set(os.listdir(STACKS_DIR))
    except OSError:
        have = set()
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
        installed = sid in have
        item = {"id": sid, "name": m.get("name") or sid, "desc": m.get("desc") or "",
                "category": m.get("category") or "tools", "icon": m.get("icon") or "",
                "port": m.get("port"), "installed": installed,
                "fields": [dict(f, default=_store_subst(f.get("default")))
                           for f in (m.get("fields") or []) if f.get("key")]}
        if installed and item["fields"]:
            # prefill fields from the live .env; secrets are never echoed back
            env = _stack_env(sid)
            for f in item["fields"]:
                cur = env.get(f["key"])
                if cur is None:
                    continue
                if f.get("secret"):
                    f["has_value"] = bool(cur)
                else:
                    f["default"] = cur
        rep = m.get("replica")
        if rep:
            cfg = (st.get("replica") or {}).get(sid) or {}
            item["replica"] = {"desc": rep.get("desc") or "",
                               "cfg": {k: v for k, v in cfg.items() if k != "pass"},
                               "has_pass": bool(cfg.get("pass")),
                               "dest_default": _stack_env(sid).get(rep.get("data_env") or "", ""),
                               "state": _replica_state(sid)}
        out.append(item)
    return {"ok": True, "apps": out}

def stack_recipe(sid):
    """Raw recipe compose + an .env preview (field defaults, substituted) so the
    install pane can show/let-the-user-edit them before bringing the stack up."""
    src = _store_compose_src(sid) if _STACK_RE.match(sid or "") else None
    if not src:
        return {"ok": False, "log": "no such app"}
    m = _store_meta(sid)
    env = []
    for f in (m.get("fields") or []):
        k = f.get("key")
        if k:
            env.append("%s=%s" % (k, _store_subst(f.get("default") or "")))
    return {"ok": True, "compose": _read_file(src),
            "env": ("\n".join(env) + "\n") if env else ""}

def stack_install(sid, values, compose=None, env_text=None):
    """Copy a recipe into /opt/stacks + write .env, then the caller streams
    `compose up` via /api/stack/stream. `compose`/`env_text` (if given) are the
    user's edited versions from the install pane — they win over the recipe copy
    and the field-built .env respectively."""
    src = _store_compose_src(sid) if _STACK_RE.match(sid or "") else None
    if not src:
        return {"ok": False, "log": "no such app"}
    m = _store_meta(sid)
    src_dir, dst = os.path.join(SERVICES_DIR, sid), os.path.join(STACKS_DIR, sid)
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src_dir):          # extra files (custom.css etc.) go along
        sp = os.path.join(src_dir, f)
        if f == "meta.json" or f.endswith(".example") or not os.path.isfile(sp):
            continue
        shutil.copy2(sp, os.path.join(dst, "compose.yaml" if sp == src else f))
    if compose is not None and str(compose).strip():   # user edited the compose in the pane
        with open(os.path.join(dst, "compose.yaml"), "w") as fh:
            fh.write(compose if compose.endswith("\n") else compose + "\n")
    if env_text is not None:                # user edited raw .env — write verbatim
        with open(os.path.join(dst, ".env"), "w") as fh:
            fh.write(env_text if env_text.endswith("\n") else env_text + "\n")
    else:
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
    log_event("action", "Install %s" % (m.get("name") or sid), "", "ok",
              kind="svc", desk=False)
    return {"ok": True}

# ---- app replica from another NAS (recipe in meta.json:replica) ----
def store_replica_save(sid, cfg):
    if not _store_meta(sid).get("replica"):
        return {"ok": False, "log": "the app has no replica recipe"}
    st = _store_load()
    cur = st.setdefault("replica", {}).setdefault(sid, {})
    for k in ("host", "user", "src_data", "dest_data"):
        if k in cfg:
            v = str(cfg.get(k) or "").strip()
            # host/user go UNQUOTED into a root ssh command — restrict the charset
            # (paths are shlex.quoted in the script, so they stay free-form)
            if k in ("host", "user") and v and not re.match(r"^[A-Za-z0-9._-]+$", v):
                return {"ok": False, "log": "invalid %s: only letters, digits, . _ -" % k}
            cur[k] = v
    if "auto" in cfg:                       # "HH:MM" = daily auto-sync, "" = off
        a = str(cfg.get("auto") or "").strip()
        cur["auto"] = a if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", a) else ""
    if cfg.get("pass"):                     # empty field = leave the password untouched
        cur["pass"] = str(cfg["pass"])
    if cfg.get("clear_pass"):
        cur.pop("pass", None)
    _store_save(st)
    return {"ok": True}

def _replica_ssh(cfg):
    """(ssh command, env) — sshpass when a password is set, otherwise key-based login."""
    tgt = "%s@%s" % (cfg.get("user") or "root", cfg["host"])
    opts = "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
    if cfg.get("pass"):
        if not shutil.which("sshpass"):
            raise ValueError("password login requires sshpass: sudo apt install sshpass")
        return "sshpass -e ssh %s %s" % (opts, tgt), {"SSHPASS": cfg["pass"]}
    return "ssh %s %s" % (opts, tgt), {}

def store_replica_script(sid, mode):
    """(bash script, env) for streaming: mode=sync (dump+rsync) | restore (bring up the replica).
    ValueError with human-readable text if something is not configured."""
    rep = _store_meta(sid).get("replica") or {}
    if not rep:
        raise ValueError("the app has no replica recipe")
    cfg = (_store_load().get("replica") or {}).get(sid) or {}
    rd = _replica_dir(sid)
    dump = os.path.join(rd, "dump.sql.gz")
    if mode == "sync":
        if not cfg.get("host") or not cfg.get("src_data"):
            raise ValueError("replica not configured: source address and media library path")
        dest = cfg.get("dest_data") or _stack_env(sid).get(rep.get("data_env") or "", "")
        if not dest:
            raise ValueError("no data folder set on this NAS (bring the app up or specify a path)")
        os.makedirs(rd, exist_ok=True)
        ssh, env = _replica_ssh(cfg)
        rsync_e = ssh.rsplit(" ", 1)[0]     # the same command without host — for rsync -e
        q = shlex.quote
        script = """set -e
echo "== version on the source"
VER=$(%(ssh)s %(vcmd)s); echo "$VER"
printf '%%s' "$VER" > %(vfile)s
echo "== database dump on the source (pg_dumpall | gzip)"
%(ssh)s %(dcmd)s > %(dump)s.part
# the dump pipe (pg_dumpall | gzip) runs on the REMOTE shell, so a pg_dumpall failure
# is masked by gzip and ssh still exits 0. Validate the gzip before promoting it —
# else a truncated/empty dump is silently accepted and the replica is empty on restore.
gzip -t %(dump)s.part
test -s %(dump)s.part
mv %(dump)s.part %(dump)s
ls -lh %(dump)s
echo "== rsync of the media library (the first run may take a while)"
mkdir -p %(dest)s
rsync -a --delete --info=progress2 -e %(re)s %(tgt)s:%(srcd)s/ %(dest)s/
echo "== sync complete: source version $VER"
""" % {"ssh": ssh, "vcmd": q(rep["version_cmd"]), "vfile": q(os.path.join(rd, "version")),
       "dcmd": q(rep["dump_cmd"]), "dump": q(dump), "re": q(rsync_e),
       "tgt": "%s@%s" % (cfg.get("user") or "root", cfg["host"]),
       "srcd": q(cfg["src_data"].rstrip("/")), "dest": q(dest.rstrip("/"))}
        return script, env
    # restore: bring up the replica with the same version the source had at dump time
    comp = _compose_path(sid)
    if not os.path.isfile(comp):
        raise ValueError("the app is not installed on this NAS — bring it up first")
    if not os.path.isfile(dump):
        raise ValueError("no dump — run «Sync» first")
    ver = _read(os.path.join(rd, "version"))
    tag = ver.rsplit(":", 1)[-1] if ":" in ver else ""
    if not tag:
        raise ValueError("could not determine the source version — repeat the sync")
    # tag comes from the source NAS and is interpolated into sed/echo inside the
    # root restore script — a quote in it would be command injection. Docker image
    # tags are [A-Za-z0-9][\w.-]*, so anything else is hostile: refuse it.
    if not re.match(r"^[A-Za-z0-9][\w.-]*$", tag):
        raise ValueError("unexpected version tag from the source: %r" % tag[:40])
    q = shlex.quote
    script = """set -e
set -o pipefail   # else a failed `gunzip -c dump | psql` still reports the restore as success
cd %(dir)s
echo "== bringing up the replica with the source version: %(tag)s"
if grep -q '^%(vkey)s=' .env 2>/dev/null; then
  sed -i 's|^%(vkey)s=.*|%(vkey)s=%(tag)s|' .env
else
  echo '%(vkey)s=%(tag)s' >> .env
fi
DC="docker compose -f %(comp)s -p %(sid)s"
echo "== stopping the stack"
$DC down --remove-orphans
echo "== bringing up the database"
$DC up -d %(dbsvc)s
echo "== waiting for Postgres to be ready"
docker exec %(dbc)s %(wait)s
echo "== restoring the dump (psql output hidden)"
gunzip -c %(dump)s | docker exec -i %(dbc)s %(psql)s > /dev/null
echo "== pulling images and starting everything"
$DC pull --quiet || true
$DC up -d
echo "== replica updated: version %(tag)s, dump from $(date -r %(dump)s '+%%F %%T')"
""" % {"dir": q(os.path.join(STACKS_DIR, sid)), "comp": q(comp), "sid": q(sid),
       "tag": tag, "vkey": rep.get("version_env") or "VERSION",
       "dbsvc": rep.get("db_service") or "database",
       "dbc": rep.get("db_container") or (sid + "_db"),
       "wait": rep.get("wait_db_cmd") or "true", "psql": rep.get("psql_cmd") or "psql",
       "dump": q(dump)}
    return script, {}

# ---- replica auto-sync (store.json: replica.<id>.auto = "HH:MM") ----
_REPLICA_RUN = set()          # sids syncing right now (manual runs don't set this)

def _replica_tick():
    """Kick the replica sync at the configured wall-clock minute, once a day."""
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
            log_event("replica_auto", "Replica %s: auto-sync not started" % sid, str(e),
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
                          "Replica %s: %s" % (sid, "synced" if ok else "auto-sync error"),
                          "" if ok else "\n".join(out.splitlines()[-5:])[:400],
                          "ok" if ok else "warn", kind="svc", desk=not ok)
            except Exception as e:
                log_event("replica_auto", "Replica %s: auto-sync error" % sid, str(e),
                          "warn", kind="svc", desk=True)
            finally:
                _REPLICA_RUN.discard(sid)
        _REPLICA_RUN.add(sid)
        threading.Thread(target=run, daemon=True).start()

# --------------------------------------------------------------------------- #
#  External SSH servers in the file manager: sshfs mounts in /mnt/remote/<id>.
#  A mounted server is a regular folder; all FM operations work as-is.
# --------------------------------------------------------------------------- #
REMOTES_FILE = os.path.join(NAS_CONFIG, "remotes.json")
REMOTE_MNT = "/mnt/remote"
_REMOTE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,30}$")

def _remotes_load():
    d = _json_load_strict(REMOTES_FILE, {})
    return d.get("remotes") or []

def _remotes_save(lst):
    _json_save(REMOTES_FILE, {"remotes": lst}, indent=2)
    _safe(lambda: os.chmod(REMOTES_FILE, 0o600))   # holds sshfs passwords

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
        return {"ok": False, "log": "check the address and user"}
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
                # empty = home folder (sshfs "host:"): on rsync.net and similar
                # SFTP hosting the root / is closed, only home is accessible
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
        return {"ok": False, "log": "no such connection"}
    if _remote_mounted(rid):
        return {"ok": True, "mount": _remote_mp(rid)}
    _remote_unstale(rid)          # dead daemon still in /proc/mounts → clear it, then remount
    if not shutil.which("sshfs"):
        return {"ok": False, "log": "sshfs is not installed: sudo apt install sshfs"}
    mp = _remote_mp(rid)
    os.makedirs(mp, exist_ok=True)
    # reconnect+ServerAlive: a wandering Wi-Fi doesn't leave a "dead" mount, a listing
    # fails with an error in seconds instead of hanging; allow_other — the folder is visible to samba/oleg too
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
    penv = None
    if r.get("pass"):
        # The password goes through the unit's environment, never argv — argv is readable
        # by anyone via /proc, the environment only by root. systemd re-applies it on every
        # restart, so a reconnect is fed the password as well. (StandardInputText would be
        # the obvious way, but systemd-run refuses it on a transient unit.)
        # --setenv=NAME (no value) imports it from OUR env, so it never lands in any argv.
        inner = 'printf %s "$NAS_SSHFS_PW" | ' + inner
        cmd += ["--setenv=NAS_SSHFS_PW"]
        penv = dict(os.environ, NAS_SSHFS_PW=r["pass"])
    cmd += ["/bin/sh", "-c", inner]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=penv)
    except subprocess.TimeoutExpired:
        return {"ok": False, "log": "the server did not respond within 30s"}
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
               or "failed to mount").strip()[-300:]
        _run(["systemctl", "stop", unit], timeout=15)
        # sshfs = SFTP: if SSH lets you in but no data flows, the server most
        # likely has the SFTP service disabled (common on Synology)
        if "Input/output error" in msg or "Connection reset" in msg:
            msg += " — looks like SFTP is disabled on the server. Synology: Control Panel → File Services → FTP → enable SFTP."
        return {"ok": False, "log": msg}
    log_event("action", "Server connected: %s" % (r.get("name") or r["host"]), "", "ok",
              kind="files", desk=False)
    return {"ok": True, "mount": mp}

# auto-mount: those flagged auto connect themselves (after a reboot, dropout, unavailability);
# 5-minute backoff so we don't hammer a powered-off server on every tick
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

# resolve the "real" path right on the server: readlink -f + /proc/self/mountinfo
# expand BOTH symlinks AND bind mounts (Ugreen: /Backup is a bind onto /volume2/Backup,
# over sftp it's a regular directory, a symlink walk won't catch it)
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
  # the path exists only in the SFTP chroot facade (Ugreen/Synology): look up the share by name
  # across volumes and return candidates with mtime — the panel will cross-check against the sftp view
  share="${p#/}"; share="${share%%/*}"
  rest="${p#/"$share"}"
  for c in /volume*/"$share"; do
    [ -e "$c$rest" ] && echo "CAND $c$rest $(stat -c %Y -- "$c$rest" 2>/dev/null || echo 0)"
  done
fi'''

def _remote_realpath_ssh(r, remote_path):
    """Ask the server itself for the real path; None if there's no shell/awk there."""
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
    """The real path IN THE SERVER'S NAMESPACE for a file inside an sshfs mount.
    You can neither resolve it locally (realpath would escape the mount) nor blindly
    glue on the base prefix: on NAS boxes (Ugreen/Synology) the folders at the root of the SSH view
    are symlinks onto /volumeN/…. Walk it component by component: lstat/readlink over sshfs
    return the link TARGETS as server paths — the answer is assembled from those."""
    r = next((x for x in _remotes_load() if x["id"] == rid), None)
    mp = _remote_mp(rid)
    lp0 = os.path.normpath(local_path or "")
    if not r or not (lp0 == mp or lp0.startswith(mp + os.sep)):
        return {"ok": False, "log": "path outside the mount"}
    rp_cfg = (r.get("path") or "").strip()
    home_mode = rp_cfg == ""                 # home folder mount: server paths are relative
    base = "" if rp_cfg in ("", "/") else rp_cfg.rstrip("/")
    def to_local(remote_abs):
        # server absolute path → local via the mount (if reachable)
        if not base:
            return mp + remote_abs
        if remote_abs == base or remote_abs.startswith(base + "/"):
            return mp + remote_abs[len(base):]
        return None
    # walk only the tail FROM the base folder: the base's own components aren't visible
    # through the mount (and don't need resolving — the user gave the base literally)
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
        if lp is None:      # link target outside the base folder — can't peek further,
            break           # but the server path itself is already assembled correctly
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
            else:                            # relative link — from the current res
                parts = [p for p in tgt.split("/") if p] + tail
                i = 0
            continue
        res, i = nxt, i + 1
    if i < len(parts):
        res = res + "/" + "/".join(parts[i:])
    res = res or "/"
    # the server knows more precisely: bind mounts and SFTP chroot facades aren't
    # visible through sshfs. No shell on the far side (rsync.net etc.) — we stay on the sshfs resolve
    ask = (res.lstrip("/") or ".") if home_mode else res
    got = _remote_realpath_ssh(r, ask)
    if got:
        real, cands = got
        if real:
            return {"ok": True, "path": real}
        if len(cands) == 1:
            return {"ok": True, "path": cands[0][0]}
        if len(cands) > 1:
            # a share with this name exists on several volumes — compare the directory's
            # mtime over sftp against the candidates
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
        _run(["fusermount", "-uz", _remote_mp(rid)], timeout=15)   # lazy: don't wait on hung io
    ok = not _remote_listed(rid)
    return {"ok": ok, "log": "" if ok else "failed to unmount"}

# --------------------------------------------------------------------------- #
#  Docker services
# --------------------------------------------------------------------------- #
def _docker_ps():
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=8).stdout
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

def _app_host_url(url):
    """A web-desktop.url label bakes in whatever hostname the recipe author typed
    (our recipes historically hardcoded a now-dead 'pi5.local'), so the shortcut
    points at the wrong box. The app runs HERE — rewrite the host to this box's
    LAN address, keeping scheme/port/path. Makes recipes host-agnostic and fixes
    already-installed stacks without touching their compose."""
    if not url:
        return url
    try:
        u = urlparse(url)
        ip = lan_ip()
        if not u.hostname or not ip:
            return url
        port = ":%d" % u.port if u.port else ""
        tail = u.path or ""
        if u.query:
            tail += "?" + u.query
        return "%s://%s%s%s" % (u.scheme or "http", ip, port, tail)
    except (ValueError, TypeError):
        return url

def discover_desktop_apps():
    """Desktop shortcuts from web-desktop.* docker labels on any containers.
    Labels: web-desktop.name / .url / .icon / .enable(=false → hide).
    Sees containers regardless of who started them (including from Dockge)."""
    try:
        ids = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}}"],
                             capture_output=True, text=True, timeout=8).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    if not ids:
        return []
    US, RS = "\x1f", "\x1e"   # separators that never occur in label values
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
            "url": _app_host_url(g("url") or ""),
            "icon": g("icon") or "",
            "running": status == "running",
            "status": status,
            "_proj": labels.get("com.docker.compose.project") or "",
        })
    for a in apps:
        a.pop("_proj", None)
    apps.sort(key=lambda a: a["name"].lower())
    return apps

# --------------------------------------------------------------------------- #
#  Shortcut icon cache (web-desktop.icon).  The browser loads the icon not from the
#  internet but from the NAS: the server fetches the image by URL once and stores it in ~/nas-config/icons/.
#  The cache key = the URL itself, so changing the label (a new URL) = a fresh download,
#  and the old file simply stops being used.
# --------------------------------------------------------------------------- #
ICON_CACHE_DIR = os.path.join(NAS_CONFIG, "icons")
ICON_MAX_BYTES = 2 * 1024 * 1024          # 2 MB per icon — the cap
_icon_sem = threading.Semaphore(4)        # don't hammer the network with dozens of threads
_ICON_CT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
    "image/avif": ".avif", "image/bmp": ".bmp",
}
_ICON_EXTS = (".png", ".jpg", ".svg", ".ico", ".gif", ".webp", ".avif", ".bmp", "")

def _icon_cached_path(url):
    """The ready cache file for a URL (look for <hash>.* among known extensions) or None."""
    h = hashlib.sha1(url.encode("utf-8", "surrogatepass")).hexdigest()
    base = os.path.join(ICON_CACHE_DIR, h)
    for ext in _ICON_EXTS:
        p = base + ext
        if os.path.isfile(p):
            return p
    return None

def fetch_icon(url):
    """Path to a local copy of the icon by http(s) URL (downloads if missing) or None.
    custom:// — user-uploaded shortcut icons: cache only, we don't download."""
    if re.match(r"custom://", url or "", re.I):
        return _icon_cached_path(url)
    if not re.match(r"https?://", url or "", re.I):
        return None
    hit = _icon_cached_path(url)
    if hit:
        return hit
    with _icon_sem:
        hit = _icon_cached_path(url)          # a parallel request may have downloaded it
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
        if not ext:                            # no type came back — guess from the URL
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
#  Cronmaster — proxy to its REST API (one origin, no CORS or keys)
# --------------------------------------------------------------------------- #
def _cron(method, path, body=None, timeout=12):
    """Request to cronmaster. Returns {ok, status, data} or {ok:False, offline?, log}."""
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
        return {"ok": False, "offline": True, "log": "Cronmaster is not running (install it via Wizard → Dockge)"}

def cron_jobs():
    r = _cron("GET", "/api/cronjobs")
    if not r.get("ok"):
        return r
    d = r["data"]                              # cronmaster wraps it: {success, data:[...]}
    jobs = d.get("data") if isinstance(d, dict) else d
    return {"ok": True, "jobs": jobs or []}

def cron_stats():
    r = _cron("GET", "/api/system-stats")      # flat object {uptime, memory, cpu, network}
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
#  File manager (native, running as root — the whole FS). A LAN admin tool.
# --------------------------------------------------------------------------- #
FS_TEXT_MAX = 3 * 1024 * 1024   # larger — we don't load into the editor

def _fs_entry(full):
    st = os.lstat(full)
    isdir = os.path.isdir(full)   # follows a symlink to a directory
    return {"name": os.path.basename(full) or full, "path": full,
            "type": "dir" if isdir else "file",
            "size": 0 if isdir else st.st_size, "mtime": int(st.st_mtime),
            "mode": oct(st.st_mode & 0o777)[2:], "link": os.path.islink(full)}

def _chown_user(path, stop=None):
    """Hand off what was created to the regular user: the panel runs as root, otherwise
    uploaded files and folders stay root:root and can only be edited via sudo.
    stop — the directory above which not to climb (we don't change ownership there)."""
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

# ---- thumbnails (cache + generation via ffmpeg/pdftoppm) ----
THUMBS_DIR = "/var/cache/nas-thumbs"
THUMB_PX   = 320
THUMB_MAX_SWEEP = 400     # don't prewarm giant directories in one go
_THUMB_IMG = {"png","jpg","jpeg","gif","webp","bmp","ico","avif","tif","tiff","svg","heic","heif"}
_THUMB_VID = {"mp4","mkv","avi","mov","webm","m4v","ogv","wmv","flv","3gp","mpg","mpeg"}
_THUMB_AUD = {"mp3","flac","m4a","aac","ogg","opus","wma"}
_THUMB_PDF = {"pdf"}
# iPhone HEIC is sliced into 512×512 tiles (e.g. 8×6 for 4032×3024). ffmpeg reads
# it with the mov demuxer and returns the FIRST TILE — the thumbnail came out as a corner piece.
# libheif (heif-convert) assembles the whole image. Browsers don't render TIFF at all.
_HEIF_EXT  = {"heic","heif"}
_VIEW_CONV = {"heic","heif","tif","tiff"}   # what needs converting to JPEG for display
# Large photos (a camera puts out 26 MP / 17 MB) are downscaled too: pushing the original to
# the browser is pointless — 40× the traffic and heavy decoding on the client.
# gif/svg/ico left alone: animation and vectors would be lost.
_VIEW_BIG_EXT = {"jpg","jpeg","png","webp","bmp","avif"}
VIEW_BIG_BYTES = 2 * 1024 * 1024
VIEW_PX    = 2560
_thumb_sem = threading.Semaphore(2)   # limit concurrent ffmpeg (thumbnails)
_view_sem  = threading.Semaphore(2)   # viewing shouldn't wait on the thumbnail queue

def _ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def _heif_decode(src, out_jpg):
    """HEIC/HEIF → JPEG via libheif. True if it worked.
    The intermediate format must be JPEG, not PNG: libheif decodes
    a 12-megapixel photo in a fraction of a second, but PNG's zlib compression (~14 MB)
    took another ~4.7s per photo. JPEG q=92 yields the same image
    (PSNR 45 dB vs. the PNG path) 12 times faster.
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
    """Path to the ready preview (generates it if missing/stale) or None."""
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
    # fit into a THUMB_PX×THUMB_PX box (portrait/vertical ones don't become huge)
    scale = "scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease" % (THUMB_PX, THUMB_PX)
    # unique suffix on EVERY call (pid is the same across all threads, up to 3 generations at once)
    uniq = "%d.%s" % (os.getpid(), secrets.token_hex(4))
    tmp = tp + "." + uniq + ".tmp.jpg"
    ok = False
    with _thumb_sem:
        try:
            if kind == "img":
                # HEIC/HEIF: assemble via libheif first — otherwise ffmpeg grabs a single tile
                if _ext(src) in _HEIF_EXT:
                    heif_tmp = tp + "." + uniq + ".heif.jpg"
                    if not _heif_decode(src, heif_tmp):
                        raise RuntimeError("heif-convert failed")
                    ff_in = heif_tmp
                else:
                    ff_in = src
                # PNG/WebP transparency → lay a white background under it (otherwise JPEG draws garbage where alpha was)
                cmd = ["ffmpeg","-y","-v","error","-i",ff_in,"-filter_complex",
                       "color=c=white:s=2x2[bg];[0:v]%s[fg];[bg][fg]scale2ref[bg2][fg2];[bg2][fg2]overlay=format=auto[o]" % scale,
                       "-map","[o]","-frames:v","1",tmp]
            elif kind == "vid":
                ss = 3.0   # fallback if the duration is unknown
                try:
                    pr = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                                         "-of","default=nk=1:nw=1", src],
                                        capture_output=True, text=True, timeout=10)
                    dur = float((pr.stdout or "").strip() or 0)
                    if dur > 0:
                        ss = max(1.0, dur * 0.1)   # ~10% of the duration
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
            # clean up all temp files from this call (including pdftoppm base-*.jpg variants)
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
    """Background generation of missing previews for a directory (on listing)."""
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
    """Duration + codecs + list of audio tracks and subtitles (for the player/transcode)."""
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
                # only text subs can be served as WebVTT (image-based pgs/dvdsub — no)
                cn = (s.get("codec_name") or "").lower()
                subs.append({"i": si, "codec": cn, "lang": lang, "title": title,
                             "text": cn in ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")})
                si += 1
    except Exception:
        pass
    return {"ok": True, "duration": dur, "vcodec": vc, "acodec": ac,
            "audios": audios, "subs": subs}

def thumbs_sweep(dirs):
    """Recursive prewarming of the preview cache (for the nightly timer)."""
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
    """(total size in bytes, number of files) of the thumbnail cache."""
    total = n = 0
    for root, _dirs, files in os.walk(THUMBS_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f)); n += 1
            except OSError:
                pass
    return total, n

def thumbs_cache_clear():
    """Fully clear the thumbnail cache. Returns the number of files removed."""
    n = 0
    for root, _dirs, files in os.walk(THUMBS_DIR):
        for f in files:
            try:
                os.remove(os.path.join(root, f)); n += 1
            except OSError:
                pass
    return n

def thumbs_cache_gc(limit_mb):
    """Keep the cache within the limit: remove the oldest (by mtime) until it fits.
    limit_mb<=0 → no limit (do nothing). Returns the number of files removed."""
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
    files.sort()          # oldest first
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
        return {"ok": False, "log": "not a directory: " + path}
    entries = []
    try:
        names = os.listdir(path)
    except PermissionError:
        return {"ok": False, "log": "no access to " + path}
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

# Trees that must NEVER be the target of a destructive file manager operation —
# even for an authorized admin: the engine itself, the OS root, system
# directories. The sharp edge is the empty path: os.path.realpath("") is the process's
# working directory (/opt/nas-os); it slipped past the naive depth check and once dropped
# the engine into the trash on an empty request body. Reading is NOT restricted (this is an admin panel).
_FS_PROTECTED = (HERE, "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
                 "/boot", "/proc", "/sys", "/dev", "/run", "/var")

def _fs_guard(path, into=False):
    """Normalise a user path for a MUTATING operation.
    → (realpath, None) if allowed, else (None, error message).

    Two different questions, and mixing them up cost us a false alarm:
      into=False — the path itself is destroyed/overwritten (delete, rename,
        move, write). Depth < 2 (/home, /mnt) is forbidden: a slip there takes
        out a whole top-level tree.
      into=True  — we only CREATE something inside the path (mkdir, upload,
        restore from trash). The container is not touched, so depth-1 is fine:
        making /home/backups is as harmless as making /home/oleg/backups.
        Blocked here: the root itself and the protected system trees."""
    if not path or not str(path).strip():
        return None, "empty path"
    rp = os.path.realpath(path)
    if rp == "/" or (not into and rp.count("/") < 2):
        return None, "path is too dangerous: " + rp
    for prot in _FS_PROTECTED:
        if rp == prot or rp.startswith(prot.rstrip("/") + os.sep):
            return None, "protected system path: " + rp
    return rp, None

def fs_read(path):
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        return {"ok": False, "log": "not a file"}
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
    # guard against overwriting the engine/system files via the FM editor (for that
    # there are separate flows; an empty path here is realpath("")=/opt/nas-os)
    path, err = _fs_guard(path)
    if err:
        return {"ok": False, "log": err}
    if not os.path.isdir(os.path.dirname(path)):
        return {"ok": False, "log": "directory does not exist"}
    if os.path.isdir(path):
        return {"ok": False, "log": "this is a directory"}
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

# ---- file download by URL (the server streams it into a folder, with progress) ----
_FETCH_JOBS = {}
_FETCH_LOCK = threading.Lock()

def fs_fetch_start(path, url, name=""):
    from urllib.parse import urlparse as _up, unquote
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return {"ok": False, "log": "an http(s) URL is required"}
    d, err = _fs_guard(path, into=True)   # don't download into system trees/the engine
    if err:
        return {"ok": False, "log": err}
    if not os.path.isdir(d):
        return {"ok": False, "log": "destination is not a directory"}
    fname = os.path.basename((name or "").strip()) or os.path.basename(unquote(_up(url).path)) or ""
    jid = hashlib.md5((url + str(time.time())).encode()).hexdigest()[:12]
    job = {"id": jid, "name": fname or "…", "total": 0, "got": 0,
           "done": False, "ok": False, "log": "", "path": "", "cancel": False}
    with _FETCH_LOCK:
        # light cleanup of old finished jobs
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
                            raise IOError("cancelled by the user")
                        if job["got"] > limit:
                            raise IOError("file larger than 40 GB — aborted")
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
        return {"ok": False, "log": "job not found"}
    return {"ok": True, "job": dict(job)}

def fs_fetch_cancel(jid):
    with _FETCH_LOCK:
        job = _FETCH_JOBS.get(jid)
    if not job:
        return {"ok": False, "log": "job not found"}
    job["cancel"] = True
    return {"ok": True}

def fs_mkdir(path, name):
    # we create INSIDE the directory — it isn't affected, so /home and /mnt are allowed
    parent, err = _fs_guard(path, into=True)
    if err:
        return {"ok": False, "log": err}
    d = _child(parent, name)
    if not os.path.basename(d):
        return {"ok": False, "log": "empty name"}
    try:
        os.makedirs(d, exist_ok=False)
    except FileExistsError:
        return {"ok": False, "log": "already exists"}
    except OSError as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": d}

def fs_rename(src, name):
    src, err = _fs_guard(src)
    if err:
        return {"ok": False, "log": err}
    base = os.path.basename((name or "").strip())
    if not base:
        return {"ok": False, "log": "empty name"}
    dst = os.path.join(os.path.dirname(src), base)
    if os.path.exists(dst):
        return {"ok": False, "log": "already exists"}
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
    parent, err = _fs_guard(path, into=True)   # open('wb') truncates — guard protected trees / the engine dir
    if err:
        return {"ok": False, "log": err}
    full = _child(parent, name)
    if not os.path.basename(full):
        return {"ok": False, "log": "no file name"}
    try:
        raw = base64.b64decode((data_b64 or "").split(",")[-1])
        with open(full, "wb") as f:
            f.write(raw)
    except (OSError, ValueError) as e:
        return {"ok": False, "log": str(e)}
    return {"ok": True, "path": full, "size": len(raw)}

def fs_newfile(path, name):
    parent, err = _fs_guard(path, into=True)
    if err:
        return {"ok": False, "log": err}
    full = _child(parent, name)
    if not os.path.basename(full):
        return {"ok": False, "log": "empty name"}
    if os.path.exists(full):
        return {"ok": False, "log": "already exists"}
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
        return {"ok": False, "log": "no source"}
    if not os.path.isdir(dst_dir):
        return {"ok": False, "log": "target is not a directory"}
    if os.path.isdir(src) and _into_self(src, dst_dir):
        return {"ok": False, "log": "cannot copy into itself"}
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
    src, err = _fs_guard(src)          # the source is moved away — guard against system trees
    if err:
        return {"ok": False, "log": err}
    dst_dir = os.path.realpath(dst_dir)
    if not os.path.exists(src):
        return {"ok": False, "log": "no source"}
    if not os.path.isdir(dst_dir):
        return {"ok": False, "log": "target is not a directory"}
    if _into_self(src, dst_dir) or os.path.dirname(src) == dst_dir:
        return {"ok": False, "log": "cannot move here"}
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
        return {"ok": False, "log": "path is too dangerous: " + path}
    try:
        m = int(str(mode).strip(), 8)
    except ValueError:
        return {"ok": False, "log": "invalid mode (octal required, e.g. 644)"}
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
        return {"ok": False, "log": "path is too dangerous: " + path}
    try:
        uid = _resolve_uid(owner)
        gid = _resolve_gid(group)
    except (KeyError, ValueError):
        return {"ok": False, "log": "no such user or group"}
    if uid == -1 and gid == -1:
        return {"ok": False, "log": "no owner or group specified"}
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
#  Disk usage analyzer (DaisyDisk-like). Background walk of a single volume (within
#  one FS, like `du -x`), build a flat map of directories with sizes, cache it
#  in ~/nas-config/duscan-<hash>.json. The frontend requests one node at a time (lazy
#  drill-down) → small responses even on terabyte pools.
# --------------------------------------------------------------------------- #
DUSCAN_TOPF = 12     # keep the top-N largest files per directory
DUSCAN_MAXCH = 60    # max children in a node (the rest → "other")
DUSCAN_BIGN = 300    # global top-N largest files of the volume
DUSCAN_DUPMIN = 1024 * 1024      # duplicate candidates — from 1 MiB (small stuff isn't interesting)
DUSCAN_DUPCAP = 6000             # max candidate files in the cache (protection against giant trees)
_duscan = {}         # root -> scan status/progress
_duscache = {}       # root -> loaded tree {nodes, ts, size, files, dirs}
_duscan_lock = threading.Lock()

def _duscan_cache_path(root):
    h = hashlib.md5(root.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return os.path.join(NAS_CONFIG, "duscan-" + h + ".json")

def _duscan_build(root):
    dev = os.stat(root).st_dev
    own = {}; topf = {}; nfiles = {}; kids = {}; parent = {}; order = []
    tb = [0]
    bigheap = []          # min-heap (size, path) — global top of largest files
    dupmap = {}; dupn = [0]  # size -> [paths] for finding duplicates (>= DUPMIN)
    for dp, dns, fns in os.walk(root, topdown=True, onerror=lambda e: None, followlinks=False):
        keep = []
        for d in dns:                       # don't cross the FS boundary and don't follow symlinks
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
            # global top of largest files
            if len(bigheap) < DUSCAN_BIGN:
                heapq.heappush(bigheap, (sz, fp))
            elif sz > bigheap[0][0]:
                heapq.heapreplace(bigheap, (sz, fp))
            # duplicate candidates: group by size (large ones only)
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
    for d in sorted(order, key=lambda x: x.count("/"), reverse=True):   # children before parents
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
                ch.append({"n": "… %d more" % extra, "s": esz, "o": 1})
        ch.sort(key=lambda x: x["s"], reverse=True)
        if len(ch) > DUSCAN_MAXCH:
            rest = ch[DUSCAN_MAXCH:]; ch = ch[:DUSCAN_MAXCH]
            ch.append({"n": "… other (%d)" % len(rest), "s": sum(x["s"] for x in rest), "o": 1})
        nodes[d] = {"s": total.get(d, 0), "ch": ch}
    bigfiles = [{"p": p, "s": s} for s, p in sorted(bigheap, reverse=True)]
    # duplicate candidates: only sizes seen more than once; sorted by potential savings
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
    """Periodically refresh ALREADY scanned volumes (cache older than N hours). 0 = off.
    Called from monitor_tick (once a minute), but checks no more than once every ~15 min;
    one scan per pass — du loads the disk, no point running them in a batch."""
    global _duscan_auto_last
    try:
        hours = float(hours or 0)
    except (ValueError, TypeError):
        hours = 0
    if hours <= 0:
        return
    now = time.time()
    if now - _duscan_auto_last < 900:      # no more than once every 15 minutes
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
        duscan_start(root)      # background; refreshes the cache and its ts
        break                   # one per pass — the rest refresh on the next checks

def duscan_start(root):
    root = os.path.realpath(root or "/")
    if not os.path.isdir(root):
        return {"ok": False, "log": "not a directory: " + root}
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
        return {"ok": False, "log": "no data — run a scan"}
    nodes = data.get("nodes", {})
    # we used to fail if the path wasn't in the scan; now we show a live listing (new folders after the scan)
    if path not in nodes and not os.path.isdir(path):
        return {"ok": False, "log": "no data for this path (re-scan)"}
    try:
        depth = max(1, min(3, int(depth)))
    except (ValueError, TypeError):
        depth = 1
    # build MERGES the real subfolders (os.listdir) with sizes from the scan: new folders
    # are flagged new (no size), deleted ones aren't shown, files are taken from the scan.
    def build(p, dep):
        nd = nodes.get(p)
        scan_dirs, scan_rest = {}, []
        if nd:
            for c in nd.get("ch", []):
                if c.get("d") and c.get("p"):
                    scan_dirs[c["p"]] = c
                else:
                    scan_rest.append(c)   # files + the «… more/other» aggregates
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
                    it["s"] = 0; it["new"] = 1   # folder not in the scan — not counted
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
    """Global top of the volume's largest files (collected during the scan)."""
    data = _duscan_load_cache(os.path.realpath(root or "/"))
    if not data:
        return {"ok": False, "log": "no data — run a scan"}
    # filter out those deleted after the scan
    bf = [f for f in data.get("bigfiles", []) if os.path.isfile(f["p"])]
    return {"ok": True, "root": root, "files": bf[:300], "scanTs": data.get("ts")}

def _file_hash_partial(p, sz):
    """Fast fingerprint: head+tail of 256 KB each + size. For equal sizes this
    practically rules out false matches (good enough for a dedup tool)."""
    h = hashlib.md5()
    with open(p, "rb") as f:
        h.update(f.read(262144))
        if sz > 524288:
            f.seek(-262144, 2); h.update(f.read(262144))
    h.update(str(sz).encode())
    return h.hexdigest()

def duscan_dups(root):
    """Find duplicates among the scan candidates (equal size → fingerprint check)."""
    data = _duscan_load_cache(os.path.realpath(root or "/"))
    if not data:
        return {"ok": False, "log": "no data — run a scan"}
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
#  File history & integrity ("File history"): incremental manifest of the
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
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return ("%d %s" if u == "B" else "%.1f %s") % (n, u)
        n /= 1024
    return "%.1f TB" % n

def fsw_load():
    d = {"enabled": True, "roots": [], "exclude": list(FSW_DEF_EXCLUDE),
         "exclude_re": [], "time": "02:30", "verify_days": 30,
         "verify_minutes": 20, "guard_pct": 25, "interval_days": 1}
    try:
        with open(FSW_CFG) as f:
            u = json.load(f)
        for k in d:
            if k in u:
                d[k] = u[k]
    except (OSError, ValueError):
        pass
    # ismount, NOT isdir: an empty /mnt/storage folder (no pool) is the system
    # card, and the thumbnail indexer would faithfully walk it
    if not d["roots"] and storage_root():
        d["roots"] = [storage_root()]
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
                      ("guard_pct", 0, 100), ("interval_days", 1, 90)):
        if k in patch:
            try:
                cur[k] = max(lo, min(hi, int(patch[k])))
            except (ValueError, TypeError):
                pass
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    _json_save(FSW_CFG, cur)
    return {"ok": True, "config": cur}

def _fsw_db_open():
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

def _fsw_db():
    try:
        return _fsw_db_open()
    except sqlite3.DatabaseError:
        # a malformed index (realistic on SD power-loss) would otherwise wedge the
        # scanner forever with no recovery path — every open re-raises. Move it aside
        # and start a fresh baseline; the actual files are untouched, only the bitrot
        # index is reset (a full re-hash happens on the next scan).
        _safe(lambda: os.replace(FSW_DB, FSW_DB + ".corrupt"))
        for suf in ("-wal", "-shm"):
            _safe(lambda s=suf: os.remove(FSW_DB + s) if os.path.exists(FSW_DB + s) else None)
        _safe(lambda: log_event("fsw", "File-history index was corrupt — rebuilt",
                                "The integrity DB was unreadable and has been reset; "
                                "a fresh baseline is taken on the next scan.",
                                "warn", kind="svc", desk=True))
        return _fsw_db_open()

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
    db = None
    try:
        db = _fsw_db()          # a malformed DB (power-loss) raises here — keep it inside the
                                # try so status becomes 'error', not a permanent 'scan' wedge
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
            _fsw_fire("fsw_root", "NAS: watched folder unavailable",
                      "Cannot see contents: %s. Disk not mounted? Deletions not recorded."
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
            _fsw_fire("fsw_guard", "NAS: mass file disappearance",
                      "%d of %d watched files gone. Events not recorded — "
                      "confirm the deletion in “File history” or check the disks."
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
                                "content changed with the same date and size"))
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
            _fsw_fire("fsw_corrupt", "NAS: corrupted files (bitrot)",
                      "%d file(s) changed without a change in date/size: %s"
                      % (len(corrupt), ", ".join(os.path.basename(x) for x in corrupt[:3])),
                      lvl="crit")
        try:
            thr = int(((load_monitor().get("events") or {}).get("fsw_del") or {})
                      .get("threshold", 50))
        except Exception:
            thr = 50
        if dels and thr and len(dels) >= thr:
            _fsw_fire("fsw_del", "NAS: %d files deleted" % len(dels),
                      "%d files vanished since the last scan (%s). Details in “File history”."
                      % (len(dels), _fsw_human(sum(s for _, s in dels))), lvl="warn")
        if baseline:
            _fsw_fire("fsw_scan", "File history: index built",
                      "Indexed %d files (%s)." % (n_files, _fsw_human(n_size)), lvl="ok")
        elif manual:
            _fsw_fire("fsw_scan", "File history: scan finished",
                      "+%d −%d ~%d →%d · corrupt: %d · verified %s in %d sec" %
                      (summary["added"], summary["removed"], summary["modified"],
                       summary["moved"], summary["corrupt"], _fsw_human(vbytes),
                       summary["dur"]), lvl="ok")
        with _fsw_lock:
            _fsw.update({"status": "idle", "cancel": False})
    except _FswCancel:
        if db:
            db.commit()
        with _fsw_lock:
            _fsw.update({"status": "idle", "cancel": False})
    except Exception as e:
        with _fsw_lock:
            _fsw.update({"status": "error", "error": str(e)[:200], "cancel": False})
    finally:
        if db:
            db.close()

def fsw_start(deep=False, manual=True):
    with _fsw_lock:
        if _fsw.get("status") in ("scan", "verify"):
            return {"ok": False, "log": "a scan is already running"}
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
        return {"ok": False, "log": "a scan is already running"}
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
    _fsw_auto_day = day
    try:
        db = _fsw_db()
        try:
            last = json.loads(_fsw_meta(db, "last") or "null")
        finally:
            db.close()
    except Exception:
        last = None
    # only scan every N days (user-configurable), not necessarily nightly — saves SD writes
    interval = max(1, int(cfg.get("interval_days", 1) or 1))
    last_ts = (last or {}).get("ts", 0)
    if last_ts and time.time() - last_ts < interval * 86400 - 3600:   # not due yet (1 h slack)
        return
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
    # Atomic: a non-atomic write on crash/race tore index.json → the trash
    # "emptied", while files in files/ were left orphaned and silently piled up gigabytes.
    _json_save(os.path.join(TRASH, "index.json"), items)

def _mount_of(path):
    """Mount point of the filesystem that holds `path` (walk up until st_dev changes)."""
    path = os.path.realpath(path)
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return "/"
    while path != "/":
        parent = os.path.dirname(path)
        try:
            if os.stat(parent).st_dev != dev:
                return path
        except OSError:
            return path
        path = parent
    return "/"

def _vol_trash_dir(path):
    """The trash 'files' dir on the SAME filesystem as `path` — so moving to the trash is an
    instant rename, not a cross-device copy. Deleting a big folder on a USB/data disk used to
    copy every byte onto the system SD card (slow, SD wear, and it failed outright when the item
    was bigger than the free space on /). Items already on the system disk keep the central trash
    (same fs → the move is a rename there too). Falls back to central when a per-volume dir can't
    be created (read-only mount, no permission)."""
    central = os.path.join(TRASH, "files")
    try:
        pdev = os.stat(path).st_dev
        if pdev == os.stat(HOME).st_dev:
            return central
        cand = os.path.join(_mount_of(path), ".nas-trash", "files")
        os.makedirs(cand, exist_ok=True)
        if os.stat(cand).st_dev == pdev:      # confirm the rename really stays on one device
            return cand
    except OSError:
        pass
    return central

def _trash_store_dirs():
    """Every 'files' dir the trash may use: the central one plus any per-volume
    .nas-trash on a mounted data volume (for orphan sweeps and emptying)."""
    out = [os.path.join(TRASH, "files")]
    for pat in ("/media/*/.nas-trash/files", "/media/*/*/.nas-trash/files",
                "/mnt/*/.nas-trash/files", "/srv/*/.nas-trash/files"):
        out += glob.glob(pat)
    seen, uniq = set(), []
    for d in out:
        rp = os.path.realpath(d)
        if rp not in seen:
            seen.add(rp); uniq.append(d)
    return uniq

def _trash_orphans(known_ids):
    """Directories in any files/ dir that are not in the index (the index was
    corrupted/reset but the files remain). Return them as trash entries — otherwise the space can't be reclaimed."""
    out = []
    for store_dir in _trash_store_dirs():
        try:
            entries = os.listdir(store_dir)
        except OSError:
            continue
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

def _dir_size(p):
    total = 0
    if not p or not os.path.isdir(p) or os.path.islink(p):
        return 0
    for root, _, files in os.walk(p):
        for f in files:
            try: total += os.path.getsize(os.path.join(root, f))
            except OSError: pass
    return total

def fs_trash(path, size=None):
    path, err = _fs_guard(path)
    if err:
        return {"ok": False, "log": err}
    if not os.path.lexists(path):
        # the panel list may be stale: the object was already deleted or moved by another window
        return {"ok": False, "log": "already deleted or moved"}
    if path == TRASH or path.startswith(TRASH + os.sep):
        return {"ok": False, "log": "already in trash"}
    if os.path.basename(path) == ".nas-trash":
        return {"ok": False, "log": "already in trash"}
    store_dir = _vol_trash_dir(path)      # same-fs → move is an instant rename, no cross-device copy
    try:
        os.makedirs(store_dir, exist_ok=True)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    tid = hashlib.md5((path + str(time.time())).encode()).hexdigest()[:12]
    dest = os.path.join(store_dir, tid + "__" + os.path.basename(path))
    isdir = os.path.isdir(path) and not os.path.islink(path)
    size = 0
    try:
        # a dir's size is NOT walked here (that would negate the instant rename); the caller
        # (space analyzer) may pass a known size, otherwise it is filled in lazily on first listing
        size = (int(size) if size else 0) if isdir else os.path.getsize(path)
    except (OSError, ValueError, TypeError):
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
    indexed = _trash_load()
    changed = False
    for it in indexed:      # fill in folder sizes lazily (a walk when the window is opened, then cached)
        if it.get("isdir") and not it.get("size") and os.path.lexists(it.get("store", "")):
            it["size"] = _dir_size(it.get("store")); changed = True
    if changed:
        _trash_save(indexed)
    items = []
    for it in indexed:
        it = dict(it)
        it["exists"] = os.path.lexists(it.get("store", ""))
        items.append(it)
    # orphaned files (in files/ but not in the index) — show them too, otherwise
    # their space can't be reclaimed from the UI; mark orphan, restore is unavailable for them (orig="")
    for orp in _trash_orphans({i.get("id") for i in indexed}):
        orp["exists"] = True
        items.append(orp)
    items.sort(key=lambda x: x.get("deleted", 0), reverse=True)
    return {"ok": True, "items": items}

def fs_trash_stat():
    """Cheap trash summary for the dock icon: count + total of KNOWN sizes (reads the index only,
    never walks — folder sizes land here once the trash window has been opened, or via the size the
    space analyzer passes on delete)."""
    items = _trash_load()
    return {"ok": True, "count": len(items),
            "bytes": sum(int(i.get("size") or 0) for i in items)}

def fs_trash_restore(tid, dest_dir=""):
    """Restore from trash. dest_dir empty → to the original folder (orig); set →
    to the chosen one. Orphaned entries (orig unknown) can only be restored by choosing a folder."""
    items = _trash_load()
    hit = next((i for i in items if i.get("id") == tid), None)
    orphan = False
    if not hit:                                  # not in the index — search among orphans
        hit = next((o for o in _trash_orphans({i.get("id") for i in items})
                    if o.get("id") == tid), None)
        orphan = bool(hit)
    if not hit:
        return {"ok": False, "log": "not found in trash"}
    store = hit.get("store")
    if not store or not os.path.lexists(store):
        if not orphan:
            _trash_save([i for i in items if i.get("id") != tid])
        return {"ok": False, "log": "file missing from storage"}
    if dest_dir:
        d, err = _fs_guard(dest_dir, into=True)   # place into the directory, don't delete it
        if err:
            return {"ok": False, "log": err}
        if not os.path.isdir(d):
            return {"ok": False, "log": "target is not a directory"}
        target = os.path.join(d, hit.get("name") or os.path.basename(store))
    elif hit.get("orig"):
        target = hit["orig"]
    else:
        return {"ok": False, "log": "original path unknown — choose a folder"}
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
    if not hit:                                  # not in the index — possibly an orphan
        hit = next((o for o in _trash_orphans({i.get("id") for i in items})
                    if o.get("id") == tid), None)
    if not hit:
        return {"ok": False, "log": "not found"}
    try:
        _trash_rm(hit.get("store"))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _trash_save([i for i in items if i.get("id") != tid])
    return {"ok": True}

def fs_trash_empty():
    # Clean the WHOLE files/ folder, not just the index: otherwise orphans (index
    # was corrupted/reset) would stay on disk and an "empty" trash would pile up gigabytes.
    items = _trash_load()
    removed = 0
    for it in items:                       # indexed items live wherever their store points (per-volume or central)
        try:
            _trash_rm(it.get("store")); removed += 1
        except OSError:
            pass
    for store_dir in _trash_store_dirs():  # then sweep every files/ dir for orphans
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
        return {"ok": False, "log": "target is not a directory"}
    name = (name or "archive").strip() or "archive"
    if not name.endswith(".zip"):
        name += ".zip"
    out = _uniq(os.path.join(dest, os.path.basename(name)))
    items = [os.path.realpath(i) for i in (items or []) if i]
    if not items:
        return {"ok": False, "log": "nothing to archive"}
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
        return {"ok": False, "log": "no archive"}
    dest = os.path.realpath(dest) if dest else os.path.dirname(path)
    try:
        os.makedirs(dest, exist_ok=True)
        shutil.unpack_archive(path, dest)
    except (shutil.ReadError, OSError, ValueError) as e:
        return {"ok": False, "log": "cannot extract: " + str(e)}
    return {"ok": True, "path": dest}

HP_CATALOG = [
    ("Dockge",       5001, False, "dockge",     "Docker stack manager"),
    ("Dozzle",       8083, False, "dozzle",     "Container logs"),
    ("Scrutiny",     8084, False, "scrutiny",   "SMART disk health"),
    ("Syncthing",    8384, False, "syncthing",  "File synchronization"),
    ("NextExplorer", 3000, False, "mdi-folder", "File manager"),
]
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
#  Web UI authentication: password (PBKDF2 on disk) + in-memory sessions.
#  The file is created lazily by the server — the installer needs to do nothing.
# --------------------------------------------------------------------------- #
AUTH_FILE   = "/etc/nas-os/webauth.json"
SESS_FILE   = "/etc/nas-os/sessions.json"   # sessions survive a service restart
SESSION_TTL = 30 * 86400
_sess_lock  = threading.Lock()
_login_fail = {"n": 0, "t": 0.0}        # anti-bruteforce: pause after a run of failures
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

_sessions   = _load_sessions()          # token -> unix expiry time

def _pw_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()

def auth_configured():
    return os.path.isfile(AUTH_FILE)

def auth_set_password(password):
    if len(password or "") < 4:
        return {"ok": False, "log": "password too short (minimum 4 characters)"}
    salt = secrets.token_bytes(16)
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"kdf": "pbkdf2-sha256-200k", "salt": salt.hex(),
                   "hash": _pw_hash(password, salt)}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)
    # a password change revokes ALL prior sessions (incl. a possibly stolen cookie);
    # the caller immediately gets a new session in the handler
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
        # sliding renewal; write to disk only on a noticeable shift (at most ~once a day)
        if exp - now < SESSION_TTL - 86400:
            _sessions[tok] = now + SESSION_TTL
            _save_sessions()
        return True

def session_drop(tok):
    with _sess_lock:
        if _sessions.pop(tok, None) is not None:
            _save_sessions()

# --------------------------------------------------------------------------- #
#  Bridge to the nas-wizard.sh api engine
# --------------------------------------------------------------------------- #
ENGINE_ACTION_RE = re.compile(r"^[a-z0-9-]{1,40}$")

def _engine_env(params, dry):
    """Build the NASW_* environment, filtering dangerous input: parameter names —
    only [A-Za-z0-9_], values — no control characters and a reasonable length."""
    env = dict(os.environ)
    env["NASW_DRYRUN"] = "1" if dry else "0"
    for k, v in (params or {}).items():
        if not re.match(r"^[A-Za-z0-9_]{1,32}$", str(k)):
            raise ValueError("invalid parameter name: %r" % k)
        v = str(v)
        if len(v) > 4096 or re.search(r"[\x00-\x1f]", v):
            raise ValueError("invalid value for parameter %s" % k)
        env["NASW_" + k.upper()] = v
    return env

def engine(action, params=None, dry=False):
    if not ENGINE_ACTION_RE.match(action or ""):
        return {"ok": False, "code": -1, "log": "invalid action: %r" % action}
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
#  System settings: read current state + write (both directions).
#  Packages/scripts are installed by the nas-wizard.sh engine (api pi|security|shares);
#  simple config edits and service toggling are done here directly.
# --------------------------------------------------------------------------- #
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
SIZE_RE     = re.compile(r"^\d{1,6}[KMG]?$")
IP_RE       = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

def _sc(*args, timeout=15):
    """Short system call -> stdout as a string ('' on error)."""
    try:
        return subprocess.run(list(args), capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def _svc(units):
    """Service state (the first existing one from the list of alternative names)."""
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
    """Whether there is an active zram swap in /proc/swaps."""
    for l in _read("/proc/swaps").splitlines():
        if l.startswith("/dev/zram"):
            return True
    return False

def _zram_status():
    """zram-swap state: standard (systemd-zram-generator/rpi-swap) or
    legacy zram-tools (zramswap.service). On modern Pi OS zram is brought up by
    the generator, while we mute zramswap.service — so we take the status by fact."""
    for u in ("dev-zram0.swap", "systemd-zram-setup@zram0.service"):
        s = _svc(u)
        if s["installed"]:
            # generated/static units: "enabled" by fact of an active swap
            s["enabled"] = s["active"] or _zram_swap_active()
            return s
    return _svc("zramswap")

def _zram_off():
    """Disable zram-swap: both the standard generator and legacy zramswap."""
    # legacy
    if _svc("zramswap")["installed"]:
        _svc_toggle("zramswap", False)
    # standard: remove zram on the next boot
    try:
        if os.path.isfile("/etc/rpi/swap.conf"):
            os.makedirs("/etc/rpi/swap.conf.d", exist_ok=True)
            with open("/etc/rpi/swap.conf.d/60-nas-os.conf", "w") as f:
                f.write("# NAS-OS: zram-swap disabled from the web panel. See swap.conf(5).\n"
                        "[Main]\nMechanism=swapfile\n")
        elif os.path.isfile("/etc/systemd/zram-generator.conf") or \
                os.path.isfile("/usr/lib/systemd/zram-generator.conf"):
            with open("/etc/systemd/zram-generator.conf", "w") as f:
                f.write("# NAS-OS: zram-swap disabled (no [zram0] section).\n")
    except OSError:
        pass
    _run(["systemctl", "daemon-reload"], timeout=20)
    # take it down live (best-effort)
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
    """Idempotently add/remove an active line in config.txt (with a backup)."""
    if not path:
        return {"ok": False, "log": "config.txt not found"}
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
            "log": "enabled (applies after a reboot)" if on else "disabled"}

def _cmdline_set(add=(), remove_prefixes=()):
    """Edit cmdline.txt (a single line, space-separated tokens)."""
    path = _cmdline_path()
    if not path:
        return {"ok": False, "log": "cmdline.txt not found"}
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
            "log": "applies after a reboot"}

def _throttled_decode():
    m = re.search(r"0x[0-9a-fA-F]+", _sc("vcgencmd", "get_throttled"))
    v = int(m.group(0), 16) if m else 0
    bits = {0: "undervoltage", 1: "frequency capped",
            2: "throttling", 3: "near thermal limit"}
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
    """Ports the firewall must keep open, with "what for" labels.
    System ones (panel/SSH/shares) + all published docker ports —
    the latter are picked up automatically on a port change or a new container."""
    ports = {}
    def add(p, label):
        ports.setdefault(p, label)
    add("%d/tcp" % PORT, "NAS web panel")
    add("22/tcp", "SSH")
    add("5353/udp", "Discovery (mDNS / .local)")   # otherwise UFW cuts avahi → pi5.local drops off
    if shutil.which("smbd") or os.path.exists("/etc/samba/smb.conf"):
        add("445/tcp", "Files (Samba)")
    if os.path.exists("/etc/exports") and os.path.getsize("/etc/exports") > 0:
        add("2049/tcp", "Files (NFS)")
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
    """Keep the needed ports open while UFW is active: a new docker container or
    a port change must not stay behind the firewall. Throttle — once every 2 minutes."""
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
        if port == "22/tcp" and ssh_ok:      # the OpenSSH rule already covers SSH
            continue
        if port not in have:
            _run(["ufw", "allow", port], timeout=15)

def _ufw_state():
    out = _sc("ufw", "status")
    ports = sorted(set(m.group(1) for m in re.finditer(r"(?m)^(\S+)\s+ALLOW", out)))
    labels = dict(_ufw_managed_ports())
    labels.setdefault("OpenSSH", "SSH")      # ufw app rules → human-readable label
    labels.setdefault("Samba", "Files (Samba)")
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
        return {"ok": False, "log": "fail2ban not installed"}
    try:
        mr = max(1, min(100, int(maxretry)))
    except (ValueError, TypeError):
        return {"ok": False, "log": "threshold: a number"}
    bt = str(bantime or "").strip()
    if bt != "-1" and not re.match(r"^\d+[smhdw]?$", bt):     # 600 / 30m / 1h / 1d / -1 (forever)
        return {"ok": False, "log": "ban time: e.g. 30m, 1h, 1d or -1 (forever)"}
    try:
        with open(F2B_CONF, "w") as f:
            f.write("[sshd]\nenabled = true\nmaxretry = %d\nbantime = %s\n" % (mr, bt))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _run(["systemctl", "reload-or-restart", "fail2ban"], timeout=20)
    return {"ok": True, "log": "saved"}

def fail2ban_unban(ip):
    if not re.match(r"^[0-9A-Fa-f.:]+$", ip or ""):
        return {"ok": False, "log": "bad IP"}
    r = _run(["fail2ban-client", "set", "sshd", "unbanip", ip], timeout=15)
    return {"ok": r["ok"], "log": (r.get("log") or "")[:200]}

def ufw_port(action, port):
    if not shutil.which("ufw"):
        return {"ok": False, "log": "ufw not installed"}
    if not re.match(r"^\d{1,5}(/(tcp|udp))?$", port or ""):
        return {"ok": False, "log": "port: e.g. 8080 or 8080/tcp"}
    if action == "deny":
        # cannot close the panel's own port or SSH — otherwise the user locks themselves out
        num = port.split("/")[0]
        if num in (str(PORT), "80", "22"):
            return {"ok": False, "log": "cannot close the panel port or SSH — you would lose access"}
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
    """Full current state of all configurable parameters."""
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
    return {"ok": True, "log": "Wi-Fi power saving " +
            ("disabled" if off else "enabled")}

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
    return {"ok": True, "log": "watchdog " + ("enabled" if on else "disabled")}

def _set_governor(val):
    av = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors").split()
    if val not in av:
        return {"ok": False, "log": "governor unavailable: " + ", ".join(av)}
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
        note = " (adaptive governor disabled — otherwise it would overwrite)"
    return {"ok": n > 0, "log": f"governor={val} on {n} cores" + note}

def _net_apply(method, extra):
    ifc = _primary_iface()
    conn = _active_conn(ifc)
    if not conn:
        return {"ok": False, "log": "no active connection found"}
    if method == "auto":
        r = _run(["nmcli", "connection", "modify", conn, "ipv4.method", "auto",
                  "ipv4.addresses", "", "ipv4.gateway", "", "ipv4.dns", ""])
    else:
        ip, gw = extra.get("ip", ""), extra.get("gw", "")
        dns, prefix = extra.get("dns", ""), str(extra.get("prefix", "24"))
        if not IP_RE.match(ip):
            return {"ok": False, "log": "invalid IP address"}
        args = ["nmcli", "connection", "modify", conn, "ipv4.method", "manual",
                "ipv4.addresses", f"{ip}/{prefix}"]
        args += ["ipv4.gateway", gw] if gw else []
        args += ["ipv4.dns", dns] if dns else []
        r = _run(args)
    if r["ok"]:
        _run(["nmcli", "connection", "up", conn], timeout=30)
        r["log"] = r.get("log") or "network settings applied"
    return r

def sysconf_set(key, val, extra=None):
    extra = extra or {}
    b = bool(val)
    try:
        if key == "hostname":
            if not HOSTNAME_RE.match(str(val or "")):
                return {"ok": False, "log": "invalid hostname"}
            r = _run(["hostnamectl", "set-hostname", val])
            r["log"] = r.get("log") or ("hostname: " + val)
            return r
        if key == "timezone":
            if not os.path.isfile("/usr/share/zoneinfo/" + str(val)):
                return {"ok": False, "log": "unknown timezone"}
            r = _run(["timedatectl", "set-timezone", val])
            r["log"] = r.get("log") or ("timezone: " + val)
            return r
        if key == "journald_max":
            if not SIZE_RE.match(str(val or "")):
                return {"ok": False, "log": "size like 200M / 1G"}
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
            return {"ok": True, "log": "automatic updates disabled"}
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
            _safe(ufw_autosync)      # open docker ports at once, don't wait for the tick
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
            return {"ok": True, "count": n, "log": f"{n} updates available"}
        if key == "restart_web":
            subprocess.Popen(["systemctl", "restart", "nas-web"])
            return {"ok": True, "log": "restarting service…"}
        return {"ok": False, "log": "unknown setting: " + str(key)}
    except Exception as e:
        return {"ok": False, "log": repr(e)}

# --------------------------------------------------------------------------- #
#  Desktop wallpaper: upload from a PC (base64) or download by URL.
#  The active image is cached locally (~/nas-config/wallpaper.<ext>) and
#  served from /api/wallpaper/img — stable across reboots and clients.
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

def _wallpaper_scr_path():
    # the touchscreen's own source image (separate from the desktop wallpaper).
    # "wallpaper-scr.*" never matches the "wallpaper.*" glob above (no "wallpaper." prefix).
    g = sorted(glob.glob(os.path.join(NAS_CONFIG, "wallpaper-scr.*")))
    return g[0] if g else ""

def _wallpaper_screen_refresh():
    """Wallpaper changed — rebuild the screen-sized copy without waiting for the first request."""
    threading.Thread(target=lambda: _safe(_wallpaper_screen), daemon=True).start()


def _wallpaper_save(data, screen=False):
    ext = _img_ext(data)
    if not ext:
        return {"ok": False, "log": "not an image (need jpg/png/webp/gif)"}
    if len(data) > 30 * 1024 * 1024:
        return {"ok": False, "log": "file too large (>30 MB)"}
    os.makedirs(NAS_CONFIG, exist_ok=True)
    base = "wallpaper-scr" if screen else "wallpaper"   # screen keeps its own source
    for old in glob.glob(os.path.join(NAS_CONFIG, base + ".*")):
        try:
            os.remove(old)
        except OSError:
            pass
    try:
        with open(os.path.join(NAS_CONFIG, base + ext), "wb") as f:
            f.write(data)
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _wallpaper_screen_refresh()      # prepare the small-screen copy right away
    return {"ok": True, "ext": ext}

def wallpaper_fetch(url, screen=False):
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "log": "need an http(s) URL"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nas-web"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(30 * 1024 * 1024 + 1)
    except Exception as e:
        return {"ok": False, "log": "failed to download: " + str(e)}
    return _wallpaper_save(data, screen)

def wallpaper_upload(b64, screen=False):
    try:
        data = base64.b64decode((b64 or "").split(",")[-1])
    except Exception:
        return {"ok": False, "log": "bad image data"}
    return _wallpaper_save(data, screen)

# --------------------------------------------------------------------------- #
#  USB auto-import: on inserting a flash drive, copy its contents into a chosen folder
#  (udev hook → helper script → rsync; copy only, nothing is deleted from the drive)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  SSH greeting (MOTD). The /etc/update-motd.d/20-nas-os script is installed by the wizard;
#  here we only edit the user text and two flags. The text is printed
#  via `cat` — not executed, so there is nothing to escape.
# --------------------------------------------------------------------------- #
MOTD_CONF   = "/etc/nas-wizard/motd.conf"
# The greeting is assembled from several sources, and our script is only one of them.
# pam_motd runs EVERYTHING in update-motd.d and prints files from /etc/motd.d,
# while "Last login" is added by sshd itself. Give a toggle for each.
MOTD_UNAME_SH   = "/etc/update-motd.d/10-uname"
MOTD_SSHD_CONF  = "/etc/ssh/sshd_config.d/99-nas-motd.conf"
MOTD_TXT    = "/etc/nas-wizard/motd.txt"
MOTD_SCRIPT = "/etc/update-motd.d/20-nas-os"
MOTD_MAX    = 4000

_MOTD_FLAGS = {"MOTD_LOGO": "show_logo", "MOTD_TEXT": "show_text", "MOTD_INFO": "show_info",
               "MOTD_UNAME": "show_uname", "MOTD_LASTLOG": "show_lastlog"}

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
    # on-disk state beats what was recorded: someone else may have changed the files
    cfg["has_uname"] = os.path.isfile(MOTD_UNAME_SH)
    return cfg

def _motd_extras_apply(cfg):
    """Suppress/restore other parts of the greeting. Called from motd_save and at startup,
    so the setting survives a reinstall (motd.conf is in the settings backup)."""
    # 1) the kernel line: strip the execute bit, pam_motd will skip it
    if os.path.isfile(MOTD_UNAME_SH):
        try:
            os.chmod(MOTD_UNAME_SH, 0o755 if cfg.get("show_uname", True) else 0o644)
        except OSError:
            pass
    # 2) "Last login" is printed by sshd, not pam_motd
    try:
        want = not cfg.get("show_lastlog", True)
        have = os.path.isfile(MOTD_SSHD_CONF)
        if want and not have:
            os.makedirs(os.path.dirname(MOTD_SSHD_CONF), exist_ok=True)
            with open(MOTD_SSHD_CONF, "w") as f:
                f.write("# nas-wizard: the \"Last login\" line was disabled in the panel\nPrintLastLog no\n")
        elif not want and have:
            os.remove(MOTD_SSHD_CONF)
        else:
            return
        if _run(["sshd", "-t"], timeout=8)["ok"]:          # don't reload a broken config
            _run(["systemctl", "reload", "ssh"], timeout=10)
        elif have and not want:
            pass
        else:                                              # config rejected — roll back
            try: os.remove(MOTD_SSHD_CONF)
            except OSError: pass
    except OSError:
        pass

def motd_preview():
    if not os.path.isfile(MOTD_SCRIPT):
        return ""
    env = dict(os.environ); env["NO_COLOR"] = "1"   # preview without ANSI codes
    r = _run(["/bin/bash", MOTD_SCRIPT], timeout=15, env=env)
    return r["log"]

def motd_save(b):
    if not os.path.isfile(MOTD_SCRIPT):
        return {"ok": False, "log": "greeting not installed: nas-wizard.sh api motd"}
    text = b.get("text", "")
    if not isinstance(text, str) or len(text) > MOTD_MAX:
        return {"ok": False, "log": "text too long (max %d characters)" % MOTD_MAX}
    try:
        with open(MOTD_TXT, "w") as f:
            f.write(text if text.endswith("\n") or not text else text + "\n")
        with open(MOTD_CONF, "w") as f:
            f.write("# nas-wizard: what to show on SSH login\n")
            for key, name in _MOTD_FLAGS.items():
                f.write("%s=%d\n" % (key, 1 if b.get(name, True) else 0))
    except OSError as e:
        return {"ok": False, "log": str(e)}
    _motd_extras_apply(motd_load())
    return {"ok": True, "log": "saved", "preview": motd_preview()}

# --------------------------------------------------------------------------- #
#  Samba shares & users — managed straight from the panel.
#  Source of truth for shares: /etc/samba/nas-shares.conf — a macOS-friendly
#  [global] header + one section per share, included ONCE at the END of
#  smb.conf. (include right after [global] mis-attributes every later global to
#  the last share — verified with testparm — so it must go at the end.)
#  Passwords are ALSO mirrored in cleartext (root 0600) so the panel can show
#  them; the authoritative hash stays in passdb.tdb.
# --------------------------------------------------------------------------- #
SMB_CONF_F   = "/etc/samba/smb.conf"
SMB_INC      = "/etc/samba/nas-shares.conf"
SMB_PW       = "/etc/nas-os/smb-users.json"
SMB_AVAHI    = "/etc/avahi/services/nas-shares.service"
SMB_TM_AVAHI = "/etc/avahi/services/nas-timemachine.service"
SMB_INC_LINE = "include = " + SMB_INC
# fruit/vfs = clean macOS behaviour (proper metadata, no ._ turds); fruit:model
# is what puts a server icon on the NAS in the Finder → Network list.
SMB_GLOBAL = (
    "# NAS-OS shared folders — managed by the panel. Do not edit by hand.\n"
    "[global]\n"
    "   min protocol = SMB2\n"
    "   vfs objects = catia fruit streams_xattr\n"
    "   fruit:metadata = stream\n"
    "   fruit:model = RackMac\n"
    "   fruit:posix_rename = yes\n"
    "   fruit:veto_appledouble = no\n"
    "   fruit:nfs_aces = no\n"
    "   fruit:wipe_intentionally_left_blank_rfork = yes\n"
    "   fruit:delete_empty_adfiles = yes\n"
    # keep the browse list clean: no printer shares, no ad-hoc usershares
    "   load printers = no\n"
    "   printing = bsd\n"
    "   disable spoolss = yes\n"
    "   usershare max shares = 0\n")
_SMB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$")
_SMB_DEFAULTS = ("homes", "printers", "print$")   # Debian's own sections — never ours

def _smb_installed():
    return bool(shutil.which("smbd") or os.path.exists("/usr/sbin/smbd"))

def _smb_atomic(path, text, mode=0o644):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)

def _smb_primary_user():
    """The human account (first uid>=1000 with a /home dir). Guest shares map
    to it via `force user` so guests can actually write to the folder."""
    try:
        import pwd
        for u in sorted(pwd.getpwall(), key=lambda x: x.pw_uid):
            if 1000 <= u.pw_uid < 65534 and (u.pw_dir or "").startswith("/home"):
                return u.pw_name
    except Exception:
        pass
    return "root"

def _smb_start_dir():
    """Where the folder picker opens by default. Works pool or no-pool: the actual
    storage root (mergerfs pool OR the chosen external disk via storage_root()),
    else the box's home dir — never a hardcoded /mnt/storage that may not exist."""
    r = storage_root()
    if r and os.path.isdir(r):
        return r
    try:
        import pwd
        d = pwd.getpwnam(_smb_primary_user()).pw_dir
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    return "/root" if os.path.isdir("/root") else "/"

def smb_load_pw():
    return _json_load_strict(SMB_PW, {})

def _smb_save_pw(d):
    _smb_atomic(SMB_PW, json.dumps(d, indent=2, ensure_ascii=False), 0o600)

def _smb_parse(text):
    """Parse share sections from smb.conf-style text → [{name,path,guest,users,readonly}].
    Skips [global] and the Debian default sections."""
    shares, cur = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            nm = line[1:-1].strip()
            cur = None if (nm.lower() == "global" or nm.lower() in _SMB_DEFAULTS) else \
                {"name": nm, "path": "", "guest": False, "users": [], "readonly": False}
            if cur is not None:
                shares.append(cur)
            continue
        if cur is None or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().lower(); v = v.strip()
        if k == "path":
            cur["path"] = v
        elif k in ("guest ok", "public"):
            cur["guest"] = v.lower() in ("yes", "true", "1")
        elif k == "valid users":
            cur["users"] = [u for u in re.split(r"[,\s]+", v) if u and not u.startswith("@")]
        elif k in ("read only", "writable", "writeable"):
            b = v.lower() in ("yes", "true", "1")
            cur["readonly"] = b if k == "read only" else (not b)
    return shares

def smb_get_shares():
    return _smb_parse(_read(SMB_INC))

def _smb_block(s):
    out = ["[%s]" % s["name"], "   path = %s" % s["path"], "   browseable = yes"]
    if s.get("guest"):
        out += ["   guest ok = yes", "   guest only = yes"]
    else:
        out.append("   valid users = " + " ".join(u for u in (s.get("users") or []) if _SMB_NAME.match(u)))
    # Operate on disk AS ROOT: the share can then read/write/DELETE any file
    # regardless of who owns it — root-owned USB imports, files another user made,
    # anything. One uniform rule that works for every folder without ever chowning
    # it. Samba confines this to the share's own path (no escape above it).
    out += ["   force user = root", "   force group = root"]
    out.append("   read only = " + ("yes" if s.get("readonly") else "no"))
    if not s.get("readonly"):
        out += ["   create mask = 0664", "   directory mask = 0775"]
    return "\n".join(out) + "\n"

def smb_write_shares(shares):
    _smb_atomic(SMB_INC, SMB_GLOBAL + "\n" + "\n".join(_smb_block(s) for s in shares))

def smb_write_avahi():
    # If Time Machine already advertises _device-info (its own model), don't add a
    # second, conflicting one — just advertise the SMB service.
    dev = "" if os.path.exists(SMB_TM_AVAHI) else (
        "  <service>\n    <type>_device-info._tcp</type>\n    <port>0</port>\n"
        "    <txt-record>model=RackMac</txt-record>\n  </service>\n")
    _smb_atomic(SMB_AVAHI,
        "<?xml version=\"1.0\" standalone='no'?>\n"
        "<!DOCTYPE service-group SYSTEM \"avahi-service.dtd\">\n"
        "<service-group>\n"
        "  <name replace-wildcards=\"yes\">%h</name>\n"
        "  <service>\n    <type>_smb._tcp</type>\n    <port>445</port>\n  </service>\n"
        + dev + "</service-group>\n")

def _smb_disable_defaults():
    """Comment out the Debian default shares ([homes]/[printers]/[print$]) so the
    panel presents a clean slate: otherwise a Mac browsing the NAS shows 'nobody'
    (the [homes] section under `map to guest = bad user`) and the printer shares,
    even when the user has created nothing. Reversible (';' comments), idempotent."""
    lines = _read(SMB_CONF_F).splitlines()
    out, skip, changed = [], False, False
    for ln in lines:
        s = ln.strip()
        indented = ln[:1] in (" ", "\t")
        if s.startswith("[") and s.endswith("]"):     # an ACTIVE section header
            skip = s[1:-1].strip().lower() in ("homes", "printers", "print$")
            out.append((";" + ln) if skip else ln)
            changed = changed or skip
        elif skip and s and not indented and not s.startswith(";"):
            # a top-level directive (e.g. `include = …`) is NOT part of the section
            # body — stop hiding here so we never comment out our own include line
            skip = False
            out.append(ln)
        elif skip and s and indented and not s.startswith(";"):
            out.append(";" + ln); changed = True       # indented section-body param → hide
        else:
            out.append(ln)                             # blanks / already-commented lines pass through
    if changed:
        _smb_atomic(SMB_CONF_F, "\n".join(out) + "\n")

def smb_ensure():
    """Idempotent: make sure the include exists at the END of smb.conf, the
    managed file exists, the Debian default shares are hidden, and the avahi
    service is in place. Safe to call anytime."""
    if not os.path.isfile(SMB_INC):
        smb_write_shares(smb_get_shares())
    conf = _read(SMB_CONF_F)
    if SMB_INC_LINE not in conf:
        # drop any stray earlier include of our file, then append at the very end
        keep = [l for l in conf.splitlines() if l.strip() != SMB_INC_LINE]
        _smb_atomic(SMB_CONF_F, "\n".join(keep).rstrip("\n") + "\n\n" + SMB_INC_LINE + "\n")
    _smb_disable_defaults()
    smb_write_avahi()

def smb_reload():
    t = _run(["testparm", "-s"], timeout=20)
    if t.get("code") not in (0, None) and "Loaded services file OK" not in t["log"]:
        return {"ok": False, "log": "testparm rejected the config:\n" + t["log"][-600:]}
    _run(["smbcontrol", "all", "reload-config"], timeout=15)
    if _run(["systemctl", "is-active", "smbd"], timeout=8).get("code") != 0:
        _run(["systemctl", "enable", "--now", "smbd"], timeout=30)
        _run(["systemctl", "enable", "--now", "nmbd"], timeout=20)
    return {"ok": True}

def smb_users():
    pw = smb_load_pw()
    names = set()
    r = _run(["pdbedit", "-L"], timeout=12)
    if r["ok"]:
        for line in r["log"].splitlines():
            n = line.split(":")[0].strip()
            if n:
                names.add(n)
    return [{"name": n, "password": pw.get(n, "")} for n in sorted(names)]

def smb_overview():
    if not _smb_installed():
        return {"ok": True, "installed": False, "shares": [], "users": [],
                "host": socket.gethostname(), "primary": _smb_primary_user(),
                "start": _smb_start_dir()}
    try:
        smb_ensure()
    except OSError:
        pass
    return {"ok": True, "installed": True, "host": socket.gethostname(),
            "primary": _smb_primary_user(), "start": _smb_start_dir(),
            "running": _run(["systemctl", "is-active", "smbd"], timeout=6).get("code") == 0,
            "shares": smb_get_shares(), "users": smb_users()}

def smb_share_set(b):
    name = str(b.get("name") or "").strip()
    old = str(b.get("old") or "").strip()
    path = str(b.get("path") or "").strip().rstrip("/")
    if not _SMB_NAME.match(name):
        return {"ok": False, "log": "invalid share name (letters, digits, space, . _ -)"}
    if name.lower() in _SMB_DEFAULTS + ("global",):
        return {"ok": False, "log": "reserved name"}
    if not path.startswith("/") or ".." in path:
        return {"ok": False, "log": "pick a folder"}
    guest = bool(b.get("guest"))
    users = [u for u in (b.get("users") or []) if _SMB_NAME.match(str(u))]
    if not guest and not users:
        return {"ok": False, "log": "add at least one user, or make the share open (guest)"}
    if not os.path.isdir(path):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            return {"ok": False, "log": "cannot create folder: " + str(e)}
    shares = [s for s in smb_get_shares() if s["name"] != name and (not old or s["name"] != old)]
    shares.append({"name": name, "path": path, "guest": guest, "users": users,
                   "readonly": bool(b.get("readonly"))})
    smb_ensure()
    smb_write_shares(shares)
    r = smb_reload()
    if not r["ok"]:
        return r
    return {"ok": True, "shares": smb_get_shares()}

def smb_share_del(name):
    name = str(name or "").strip()
    smb_ensure()
    smb_write_shares([s for s in smb_get_shares() if s["name"] != name])
    smb_reload()
    return {"ok": True, "shares": smb_get_shares()}

def smb_user_set(b):
    name = str(b.get("name") or "").strip()
    pwd_ = str(b.get("password") or "")
    if not _SMB_NAME.match(name) or "$" in name:
        return {"ok": False, "log": "invalid user name (letters, digits, . _ -)"}
    if len(pwd_) < 4:
        return {"ok": False, "log": "password too short (min 4 characters)"}
    import pwd as _pwmod
    try:
        _pwmod.getpwnam(name)
        exists = True
    except KeyError:
        exists = False
    if not exists:
        r = _run(["useradd", "-M", "-N", "-s", "/usr/sbin/nologin", name], timeout=20)
        if not r["ok"]:
            return {"ok": False, "log": "useradd: " + r["log"]}
    p = subprocess.run(["smbpasswd", "-a", "-s", name],
                       input="%s\n%s\n" % (pwd_, pwd_), capture_output=True, text=True, timeout=20)
    if p.returncode != 0:
        return {"ok": False, "log": "smbpasswd: " + (p.stdout + p.stderr).strip()}
    _run(["smbpasswd", "-e", name], timeout=10)      # make sure the account is enabled
    store = smb_load_pw()
    store[name] = pwd_
    _smb_save_pw(store)
    return {"ok": True, "users": smb_users()}

def smb_user_del(name):
    name = str(name or "").strip()
    if not _SMB_NAME.match(name):
        return {"ok": False, "log": "bad name"}
    _run(["smbpasswd", "-x", name], timeout=15)
    # userdel only if it's a samba-only account we made (nologin shell) — never a real login user
    try:
        import pwd as _pwmod
        u = _pwmod.getpwnam(name)
        if (u.pw_shell or "").endswith(("nologin", "false")) and u.pw_uid >= 1000:
            _run(["userdel", name], timeout=20)
    except KeyError:
        pass
    store = smb_load_pw()
    store.pop(name, None)
    _smb_save_pw(store)
    # drop the user from any share's valid-users list
    shares = smb_get_shares()
    changed = False
    for s in shares:
        if name in (s.get("users") or []):
            s["users"] = [u for u in s["users"] if u != name]; changed = True
    if changed:
        smb_write_shares(shares); smb_reload()
    return {"ok": True, "users": smb_users(), "shares": smb_get_shares()}

USB_IMPORT_CONF = "/etc/nas-wizard/usb-import.conf"
USB_IMPORT_SH   = "/usr/local/bin/nas-usb-import.sh"
USB_IMPORT_RULE = "/etc/udev/rules.d/98-nas-usb-import.rules"
_USB_DEFAULT = {"enabled": False, "dest": "/mnt/storage/imports",
                "subdir": "{label}-{date}-{time}", "notify": False, "eject": False,
                "media_only": False, "restrict": False, "allow": []}
_USB_SH = r'''#!/bin/bash
# nas-wizard: auto-import the contents of an inserted USB into a chosen folder.
# Copy is only-forward: nothing is deleted from the drive. Staging in .incomplete
# with a rename on success — an interrupted import is visible and not confused with a finished one.
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
    log "skip $dev: medium was inserted before boot — auto-import only on a hot insertion"
    exit 0
  fi
fi
# udev triggers us separately for EACH partition of the disk. Register a job so the
# eject at the end does not yank the device out from under a still-copying sibling.
# Doing this via pgrep won't work: the script's subshell has the same command
# line but a different PID — you'd see yourself and wait forever.
pk="$(lsblk -no PKNAME "$dev" 2>/dev/null | head -1)"
JOBD="/run/nas-usb-import.jobs/${pk:-none}"
mkdir -p "$JOBD" 2>/dev/null && : > "$JOBD/$$" 2>/dev/null
trap 'rm -f "$JOBD/$$" 2>/dev/null' EXIT
# how many LIVE siblings are copying this same disk (dead entries are cleaned up)
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
# allowlist: if the restriction is on — auto-import ONLY from allowed devices
# (by VID:PID). Manual "import now" (IMPORT_FORCE=1) ignores the list.
# UAS bridges don't give ID_VENDOR_ID — fall back to ID_USB_VENDOR_ID. Normalize case:
# in the config VID:PID is stored lowercase.
if [ "${IMPORT_FORCE:-0}" != "1" ] && [ "${IMPORT_RESTRICT:-0}" = "1" ]; then
  props="$(udevadm info -q property -n "$dev" 2>/dev/null)"
  prop(){ printf '%s\n' "$props" | sed -n "s/^$1=//p" | head -1; }
  vid="$(prop ID_VENDOR_ID)"; [ -n "$vid" ] || vid="$(prop ID_USB_VENDOR_ID)"
  pid="$(prop ID_MODEL_ID)";  [ -n "$pid" ] || pid="$(prop ID_USB_MODEL_ID)"
  did="$(printf '%s:%s' "$vid" "$pid" | tr 'A-Z' 'a-z')"
  if [ -z "$vid" ] || [ -z "$pid" ]; then
    log "skip $dev: could not determine VID:PID"; notify "USB skipped" "Could not identify the device"; exit 0
  fi
  case " $(printf '%s' "${IMPORT_ALLOW}" | tr 'A-Z' 'a-z') " in
    *" $did "*) : ;;
    *) log "skip $dev ($did): not in the list of allowed devices"; notify "USB skipped" "Device $did is not in the auto-import list"; exit 0 ;;
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
    # Race with automount: both scripts hang on the same udev ADD event.
    # findmnt above said "not mounted", but while we were headed to mount, automount
    # managed to grab the medium — then don't fail, read from its mount point.
    rmdir "$mp" 2>/dev/null
    mp="$(findmnt -n -o TARGET --source "$dev" 2>/dev/null | head -1)"
    [ -n "$mp" ] || { log "import FAIL $dev: failed to mount"; exit 1; }
    log "medium already mounted by automount — reading from $mp"
  fi
fi
cleanup(){ [ "$selfmount" = "1" ] && { umount "$mp" 2>>"$LOG"; rmdir "$mp" 2>/dev/null; }; }
base="${IMPORT_DEST:-/mnt/storage/imports}"
# A destination under /mnt|/media|/srv implies a SEPARATE medium. Not connected —
# the path falls through to root, and rsync silently fills the system card (that's how 8 GB
# of an import once landed in /mnt/storage on the SD, because there was no pool at all).
# The same check as in backup (_dest_disk_absent in the panel): the nearest mount point
# above must NOT be "/". The directory may not exist yet —
# walk up by dirname until we hit a mounted one.
mnt_of(){ local p; p="$(readlink -f "$1")"
  while [ "$p" != "/" ] && ! mountpoint -q "$p" 2>/dev/null; do p="$(dirname "$p")"; done
  printf '%s' "$p"; }
case "$base" in
  /mnt/*|/media/*|/srv/*)
    if [ "$(mnt_of "$base")" = "/" ]; then
      log "import FAIL $dev: destination $base not mounted — destination medium not connected"
      notify "USB import canceled" "Destination $base not mounted — the import would fill the system disk"
      cleanup; exit 1
    fi ;;
esac
# guard against importing itself (e.g. the drive is mounted inside the destination)
case "$(readlink -f "$base")/" in "$(readlink -f "$mp")"/*) log "self-import guard: $mp inside $base"; cleanup; exit 0;; esac
# subfolder layout: a template with tokens {label}/{date}/{time}/{year}/{month}/
# {month-name}/{day}/{hour}/{minute}/{datetime}. Legacy keys are mapped to templates.
# WARNING: curly braces in the default value break the ${VAR:-...} parse —
# the first '}' closes the substitution, and the tail glues on as text. So
# the default is set on a separate line. We test for "is it set" (+set), not for
# "non-empty": an empty template is a deliberate "no subfolder" mode.
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
  # path safety: strip .., leading slashes, collapse repeats, trim spaces on segments.
  # Curly braces are cleaned out: after substitution only a typo in a token
  # ({lable}) leaves them — let it be "lable", not junk in the folder name.
  sub="$(printf '%s' "$sub" | tr -d '{}' | sed 's#\.\.##g; s#^/*##; s#/*$##; s#/\{2,\}#/#g')"
fi
# "photos/videos only" filter (case-insensitive)
filter=(); if [ "${IMPORT_MEDIA_ONLY:-0}" = "1" ]; then
  filter=(--include='*/')
  for e in jpg jpeg png gif heic heif webp tif tiff bmp dng raw arw cr2 cr3 nef orf rw2 raf srw \
           mp4 mov avi mkv m4v mts m2ts 3gp mpg mpeg wmv webm; do
    u="$(printf '%s' "$e" | tr a-z A-Z)"; filter+=(--include="*.$e" --include="*.$u")
  done
  filter+=(--exclude='*')
fi
# free space check (needed + 5% margin)
need="$(du -sb "$mp" 2>/dev/null | cut -f1)"; avail="$(df -PB1 "$base" 2>/dev/null | awk 'NR==2{print $4}')"
if [ -n "$need" ] && [ -n "$avail" ] && [ "$avail" -lt "$((need + need/20 + 10485760))" ]; then
  log "no space: need=$need avail=$avail"; notify "USB import: low space" "\"$label\" won't fit in $base"; cleanup; exit 1
fi
# Staging — ONE top-level folder. Previously the template was substituted right into the name
# (.incomplete-$$-{year}/{month}/...), and with a nested template mv failed: the destination
# directory doesn't exist yet, while ".incomplete-123-2026/07/..." is already three levels.
if [ -n "$sub" ]; then
  dest="$base/$sub"
  stage="$base/.incomplete-$$-$label"
else
  dest="$base"
  stage="$dest"
fi
mkdir -p "$stage" 2>>"$LOG"
log "import $dev ($label) -> $dest"
notify "USB import started" "Copying \"$label\" → $dest"

# --- progress for the panel --------------------------------------------------
# rsync --info=progress2 updates the line via \r; we push it into a file in /run.
# We write the pid so the panel can tell "running" from "process killed".
PROGD=/run/nas-usb-import.progress
PROG="$PROGD/${dev##*/}"
START="$(date +%s)"
mkdir -p "$PROGD" 2>/dev/null
prog(){                       # $1=status  $2=rsync line
  { printf 'pid=%s\ndev=%s\nlabel=%s\ndest=%s\ntotal=%s\nstarted=%s\nstatus=%s\n' \
      "$$" "$dev" "$label" "$dest" "${need:-0}" "$START" "$1"
    [ "$1" != "running" ] && printf 'finished=%s\n' "$(date +%s)"
    printf 'line=%s\n' "$2"
  } > "$PROG.tmp" 2>/dev/null && mv -f "$PROG.tmp" "$PROG" 2>/dev/null
  return 0
}
prog running ""
trap 'rm -f "$JOBD/$$" "$PROG.tmp" 2>/dev/null' EXIT

# LC_ALL=C — so the thousands separator is a comma and the parser doesn't guess by locale.
# --no-inc-recursive: scans everything up front, but the percentage is honest, not "of
# what's been seen so far".
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
    # don't merge with an existing folder: mv would place the staging INSIDE it
    d="$dest"; n=2
    while [ -e "$d" ]; do d="$dest ($n)"; n=$((n+1)); done
    if mv "$stage" "$d" 2>>"$LOG"; then
      dest="$d"
    else
      log "import FAIL $dev: could not move $stage -> $d (data left in staging)"
      notify "USB import: error" "Failed to sort \"$label\" into folders"
      prog fail "mv"; cleanup; exit 1
    fi
  fi
  log "import OK -> $dest"; notify "USB import done" "\"$label\" copied to $dest"
  # keep the last rsync line: otherwise the byte count and total drop out of "done"
  prog done "$(sed -n 's/^line=//p' "$PROG" 2>/dev/null | tail -1)"
else
  log "import FAIL $dev rc=$rc (partial in $stage)"; notify "USB import: error" "Failed to copy \"$label\" (rc=$rc)"
  prog fail "rc=$rc"
fi
cleanup
if [ "${IMPORT_EJECT:-0}" = "1" ] && [ -n "$pk" ]; then
  # wait for sibling partitions of the same disk (cap 2 hours)
  waited=0
  while [ "$(siblings)" -gt 0 ] && [ "$waited" -lt 7200 ]; do
    [ "$waited" = 0 ] && log "eject waiting: other partitions of /dev/$pk are still copying"
    sleep 1; waited=$((waited+1))
  done
  # automount may have brought partitions up in /media — while they're mounted, power-off
  # refuses ("drive in use") and only a rough eject with a hanging mount remains
  for part in $(lsblk -lnpo NAME "/dev/$pk" 2>/dev/null); do
    for mp in $(findmnt -rno TARGET -S "$part" 2>/dev/null); do
      umount "$mp" 2>>"$LOG" || udisksctl unmount -b "$part" >>"$LOG" 2>&1 || true
    done
  done
  sync
  # First gently eject the MEDIUM, and only if that fails — power off the DEVICE.
  # For a card reader, power-off removes the reader itself from the bus: there's nowhere
  # to insert a card afterward until you re-plug the cable. eject, however, ejects the card, the reader stays alive and
  # the next insertion starts an import again. For a flash drive, eject stops
  # the device — data is flushed, safe to remove.
  if eject "/dev/$pk" >>"$LOG" 2>&1; then
    log "eject media /dev/$pk"
  elif udisksctl power-off -b "/dev/$pk" >>"$LOG" 2>&1; then
    log "power-off /dev/$pk"
  else
    log "eject /dev/$pk failed"
  fi
  touch /run/nas-web-refresh 2>/dev/null   # disk vanished — wake the panel immediately
fi
'''
# match by ID_USB_DRIVER (usb-storage/uas), not ID_BUS==usb — otherwise USB-SATA
# bridges (ID_BUS=ata) don't fire.
# --unit gives a readable name instead of run-p564-i565.service (otherwise "service
# run-p… failed" says nothing), --collect removes a failed unit at once: without it it
# stays failed and the next insertion of the same medium couldn't start.
_USB_RULE = ('ACTION=="add", SUBSYSTEM=="block", ENV{ID_USB_DRIVER}=="?*", '
            'ENV{ID_FS_USAGE}=="filesystem", '
            'RUN+="/usr/bin/systemd-run --no-block --collect '
            '--unit=nas-usb-import-%k /usr/local/bin/nas-usb-import.sh $devnode"\n')

def usb_import_load():
    cfg = dict(_USB_DEFAULT)
    cfg["dest"] = storage_sub("imports") or _USB_DEFAULT["dest"]   # follows storage
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
# "  1,234,567  45%   12.34MB/s    0:00:12" — rsync --info=progress2 format under LC_ALL=C
_RSYNC_PROG = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)")

def usb_import_progress():
    """Active and recently finished import jobs. The files live in /run,
    so a reboot cleans them up on its own."""
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
        # process killed (or reboot of the udev job) — otherwise the job would hang "running" forever
        if st == "running" and pid and not os.path.isdir("/proc/" + pid):
            st = "aborted"
        fin = int(meta.get("finished") or 0)
        if st != "running" and fin and now - fin > 600:
            continue
        job = {"dev": meta.get("dev"), "label": meta.get("label"), "dest": meta.get("dest"),
               "status": st, "started": int(meta.get("started") or 0),
               "finished": fin or None,
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
    """Whether transcoding is needed for display: the browser can't handle the format OR the file is huge."""
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
    """A large JPEG for the viewer: browsers don't render HEIC/HEIF and TIFF,
    and there's no point pushing 17-megabyte camera shots whole.
    Cached next to the thumbnails, cleaned by the same GC."""
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
                    raise RuntimeError("heif-convert failed")
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
    """Tidy the import destination: abandoned .incomplete-* stagings and, if
    asked, old imports. Work ONLY inside the destination folder and only
    if it's under an allowed root — otherwise recursive deletion is too dangerous."""
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
        # abandoned staging: the import process died long ago, the folder is junk
        if stale and stale_hours > 0 and age > stale_hours * 3600:
            try:
                shutil.rmtree(p); removed += 1
                log_event("info", "USB import: removed abandoned staging", name, "ok",
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
    """Warm up previews of the imported folder in the background. os.nice — so the disk
    card and panel don't stall: a thumbnail of a 26 MP shot costs ~0.8 s."""
    try:
        os.nice(15)
    except OSError:
        pass
    try:
        n = thumbs_sweep([dest])
        if n:
            log_event("info", "Previews prepared", "%s: %d items" % (dest, n), "ok",
                      kind="files", desk=False)
    except Exception:
        pass

def usb_ops_sync():
    """Record finished import jobs into the operations history. Called from
    monitor_loop, so the history grows even with the panel closed. A new
    (non-duplicate) successful import also triggers preview warming."""
    warm = load_maintenance().get("import_warm_thumbs", True)
    for j in usb_import_progress()["jobs"]:
        if j["status"] == "running":
            continue
        bits = [j.get("label") or j.get("dev") or ""]
        if j.get("bytes") is not None:
            bits.append(fmt_bytes(j["bytes"]) + (" of " + fmt_bytes(j["total"]) if j.get("total") else ""))
        if j.get("dest"):
            bits.append(j["dest"])
        r = ops_hist_add({"uid": "usb:%s:%s" % (j["dev"], j["started"]),
                          "state": "done" if j["status"] == "done" else "err",
                          "ts": j.get("finished") or j.get("started") or int(time.time()),
                          "title": "USB import", "label": " · ".join(x for x in bits if x)})
        # warm exactly once per import: dup means we've already seen it
        if warm and r.get("ok") and not r.get("dup") and j["status"] == "done" \
           and j.get("dest") and os.path.isdir(j["dest"]):
            threading.Thread(target=_thumbs_warm_bg, args=(j["dest"],), daemon=True).start()

def usb_import_history(n=8):
    """Finished imports, newest first — for the wall screen.

    The progress files live in /run and die with the box, so a reboot would wipe
    the tile clean. The lasting trace is the one usb_ops_sync() writes to
    ops-history.json (uid = usb:<dev>:<started>) — read it back from there."""
    out = []
    for e in ops_hist_list():
        if not str(e.get("uid") or "").startswith("usb:"):
            continue
        bits = [b.strip() for b in str(e.get("label") or "").split("·")]
        # "130 MB of 130 MB" — only the copied amount goes into the tile string
        full = next((b for b in bits[1:] if not b.startswith("/")), "")
        out.append({"name": (bits[0] if bits and bits[0] else "USB"),
                    "ts": int(e.get("ts") or 0),
                    "ok": e.get("state") == "done",
                    "size": re.split(r"\s+(?:of|of)\s+", full)[0],
                    "size_full": full,
                    "dest": next((b for b in bits if b.startswith("/")), "")})
    out.sort(key=lambda x: -x["ts"])
    return out[:n]

def _usb_sh_sync():
    """Rewrite the helper and udev rule if they diverged from the code. Previously both
    were updated only when settings were saved, so after a panel update
    the disk kept the old version with old bugs."""
    changed = []
    # _read() strips the trailing newline — compare stripped, or every service
    # start "updates" an identical helper and spams the event log
    if os.path.isfile(USB_IMPORT_SH) and _read(USB_IMPORT_SH) != _USB_SH.strip():
        with open(USB_IMPORT_SH, "w") as f:
            f.write(_USB_SH)
        os.chmod(USB_IMPORT_SH, 0o755)
        changed.append("helper")
    # touch the rule only if it is already present: its absence = import disabled
    if os.path.isfile(USB_IMPORT_RULE) and _read(USB_IMPORT_RULE) != _USB_RULE.strip():
        with open(USB_IMPORT_RULE, "w") as f:
            f.write(_USB_RULE)
        _run(["udevadm", "control", "--reload"], timeout=15)
        changed.append("udev rule")
    if changed:
        log_event("info", "USB import: %s updated to the current version" % " + ".join(changed),
                  "", "ok", kind="disk", desk=False)

def _usb_install(enabled):
    try:
        with open(USB_IMPORT_SH, "w") as f:      # helper always (needed for "import now" too)
            f.write(_USB_SH)
        os.chmod(USB_IMPORT_SH, 0o755)
        if enabled:                               # udev hook only when enabled
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
        return {"ok": False, "log": "invalid destination path"}
    # layout: legacy key OR template with tokens (allow letters/digits/space/-_.{}/ )
    subdir = str(cfg.get("subdir", "{label}-{date}-{time}")).strip()
    if subdir not in ("dated", "label", "flat"):
        subdir = re.sub(r"[^\w \-.{}/А-Яа-яЁё]", "", subdir).replace("..", "").strip("/")[:120]
        if not subdir:
            subdir = "flat"
    try:
        os.makedirs("/etc/nas-wizard", exist_ok=True)
        with open(USB_IMPORT_CONF, "w") as f:
            f.write("IMPORT_ENABLED=%d\n" % (1 if cfg.get("enabled") else 0))
            # QUOTED VALUES: the config is sourced by the shell, and the template and path may
            # contain spaces. Without quotes bash sees "VAR=x cmd args" and the variable
            # never reaches the shell at all — the default template silently kicked in.
            f.write('IMPORT_DEST="%s"\n' % dest)
            f.write('IMPORT_SUBDIR="%s"\n' % subdir)
            f.write("IMPORT_NOTIFY=%d\n" % (1 if cfg.get("notify") else 0))
            f.write("IMPORT_EJECT=%d\n" % (1 if cfg.get("eject") else 0))
            f.write("IMPORT_MEDIA_ONLY=%d\n" % (1 if cfg.get("media_only") else 0))
            # device allow-list (VID:PID hex, space-separated) + restrict flag
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
        return {"ok": True, "warn": "rsync is not installed — import will not run (installed during the \"System\" stage)"}
    return {"ok": True, "log": "saved"}

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
            usb = usb_ancestor or n.get("tran") == "usb"   # SD/NVMe (tran mmc/nvme) excluded
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
    """USB drives right now: VID:PID/model/serial — for the auto-import allow-list."""
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
        # UAS bridges report ID_BUS=ata (and lsblk TRAN may not be "usb") — match by
        # ID_USB_DRIVER, it is set for both usb-storage and uas.
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
        return {"ok": False, "log": "invalid device"}
    if not os.path.isfile(USB_IMPORT_SH):
        return {"ok": False, "log": "save the import settings first"}
    env = dict(os.environ); env["IMPORT_FORCE"] = "1"
    try:
        p = subprocess.run([USB_IMPORT_SH, dev], env=env, capture_output=True,
                           text=True, timeout=3600)
        return {"ok": p.returncode == 0,
                "log": (p.stdout + p.stderr).strip() or ("import started" if p.returncode == 0 else "error")}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "log": str(e)}

# --------------------------------------------------------------------------- #
#  Native terminal: WebSocket <-> PTY (bash), no password, as the current user
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
# CodeMirror 5 (editor in the file manager) — modes are self-contained, attach to the CodeMirror global
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
            print(f"  downloaded {fn}")
        except OSError as e:
            print(f"  failed to download {fn}: {e}")

# --------------------------------------------------------------------------- #
#  HTTP
# --------------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = True          # worker threads do not hold up service shutdown
    def handle_error(self, request, client_address):
        # the browser aborted the load (closed the tab, cancelled an image) — this is normal,
        # not a failure: a full traceback in journal is just noise. Everything else — as before.
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
_SCR = {"touch": time.time(), "last": None, "spd": None, "spd_run": False,
        "sleep": False}


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
            "idle_min": _i("idle_min", 0, 0, 240),      # 0 = do not dim on idle
            "clock_min": _i("clock_min", 3, 0, 240),    # clock on idle; 0 = off
            "poll": _i("poll", 1500, 500, 30000),       # how often the screen polls for data, ms
            "actions": d.get("actions") is not False,
            "lang": "ru" if d.get("lang") == "ru" else "en"}


def save_screen(d):
    cur = load_screen()
    was = cur["enabled"]
    if isinstance(d, dict):
        cur.update({k: v for k, v in d.items() if k in cur})
    _json_save(SCREEN_FILE, cur, indent=2)
    _safe(lambda: _screen_apply(force=True))
    if cur["enabled"] != was:
        # the kiosk service itself is enabled/disabled by the engine (wizard), not the panel
        threading.Thread(target=lambda: _safe(
            lambda: engine("screen", {"enable": "1" if cur["enabled"] else "0"})),
            daemon=True).start()
    return load_screen()


def _bl_dir():
    """First backlight device (the DSI panel), '' if the box has no screen."""
    try:
        for n in sorted(os.listdir("/sys/class/backlight")):
            return "/sys/class/backlight/" + n
    except OSError:
        return ""
    return ""


_I2C_SLAVE_FORCE = 0x0706


def _attiny_led(on):
    """True backlight off for the RPi/Waveshare DSI panel. PWM=0 (and bl_power=4,
    which the driver maps to the same PWM write) leaves the ATTINY's LED driver
    ENABLED — the panel keeps a visible glow in a dark room. The real switch is
    PC_LED_EN (bit0 of PORTC, reg 0x83). The firmware's registers are write-only
    (reads return junk), so we blind-write the driver's steady-state values:
    0x0f = run, 0x0e = LED off with bridge/LCD/touch resets untouched — touch
    stays alive to wake the screen. The i2c bus is shared with the touch
    controller: a raced poll costs it one EIO+retry, so write ONCE per
    transition (callers only act on brightness changes), never per tick."""
    d = _bl_dir()
    m = re.match(r"^(\d+)-00([0-9a-f]+)$", os.path.basename(d or ""))
    if not m:
        return False
    try:  # only this exact panel driver — never poke unknown i2c hardware
        drv = os.path.basename(os.readlink(d + "/device/driver"))
    except OSError:
        return False
    if drv != "rpi_touchscreen_attiny":
        return False
    dev = "/dev/i2c-" + m.group(1)
    if not os.path.exists(dev):
        _run(["modprobe", "i2c-dev"], timeout=10)
    try:
        fd = os.open(dev, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.ioctl(fd, _I2C_SLAVE_FORCE, int(m.group(2), 16))
        # The Waveshare clone answers the official ATTINY protocol (PORTC bit0 =
        # LED_EN) *and* its own native one (0xAA backlight enable, 0xAD panel
        # power — the exact regs panel-waveshare-dsi.c toggles for DPMS, so the
        # panel comes back from 0xAD=0 without re-init; touch at 0x38 verified
        # alive through both). Firmware revisions differ in which switch they
        # honour — write them all, extras are ignored.
        if on:
            seq = ((0xAD, 0x01), (0xAA, 0x01), (0x83, 0x0F))
        else:
            seq = ((0x83, 0x0E), (0xAA, 0x00), (0xAD, 0x00))
        for reg, val in seq:
            os.write(fd, bytes([reg, val]))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def screen_bright_set(v):
    d = _bl_dir()
    if not d:
        return False
    try:
        mx = int((_read(d + "/max_brightness") or "255").strip() or 255)
    except ValueError:
        mx = 255
    try:
        v = max(0, min(mx, int(v)))
        if v > 0:
            _safe(lambda: _attiny_led(True))   # LED first, then PWM
        try:
            with open(d + "/bl_power", "w") as f:
                f.write("4" if v == 0 else "0")
        except OSError:
            pass                      # driver has no bl_power — at least set PWM to zero
        with open(d + "/brightness", "w") as f:
            f.write(str(v))
        if v == 0:
            _safe(lambda: _attiny_led(False))  # PWM to zero, then LED truly off
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
    # the window almost always crosses midnight (23:00 -> 07:00), so not "a <= cur < b"
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
        want = cfg["bright"]                       # alarm — light up the screen
        _SCR["sleep"] = False                      # ... and wake it forcibly
    elif _SCR["sleep"]:
        want = 0                                   # put to sleep by button — sleep until touched
    elif now - _SCR["touch"] < 60:
        want = cfg["bright"]                       # always lit for a minute after a touch
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


def _nb_live(pid):
    """Live run progress: state + log tail. Reading the whole log is not allowed —
    the screen is polled every one and a half seconds."""
    rs = _nb_run_state_read(pid)
    out = {"cur": rs.get("cur") or "", "done": len(rs.get("jobs") or []),
           "total": rs.get("total") or 0, "started": rs.get("started") or 0,
           "dry": bool(rs.get("dry")), "stopping": bool(rs.get("stopping"))}
    try:
        with open(nb_run_log(pid), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            tail = f.read().decode("utf-8", "replace").split("\n")
    except OSError:
        tail = []
    for line in reversed(tail[-14:]):
        m = _RSYNC_PROG.match(line.strip())
        if m:
            out["pct"] = int(m.group(2))
            out["speed"] = m.group(3)
            out["eta"] = m.group(4)
            break
    return out


# How many seconds a tile lives in cache. "updates" runs apt-get -s upgrade, and
# "disktemp"/"snapraid" hit the disks — they must not be computed on every screen poll.
SCREEN_TILE_TTL = {"updates": 300, "disktemp": 120, "snapraid": 120, "inet": 60,
                   "docker": 15, "pool": 10, "rootfs": 10}
_SCR_TILES = {}
_SCR_HEAVY = {"t": 0, "d": {}}


def _screen_heavy():
    """Disks + containers: don't recompute on every poll (the screen asks every one
    and a half seconds), keep our own cache."""
    now = time.time()
    if now - _SCR_HEAVY["t"] < 10 and _SCR_HEAVY["d"]:
        return _SCR_HEAVY["d"]
    dk = []
    for d in (_safe(disks, []) or []):
        # On the screen we show only MOUNTED volumes: an empty card reader is
        # a disk without a filesystem, it has neither usage nor temperature,
        # and a "— · —" line on the wall is just alarming.
        if not (d.get("mounts") or []):
            continue
        sm = d.get("smart") or {}
        dk.append({"name": d.get("name"), "model": d.get("model") or "",
                   "size": d.get("size"), "temp": sm.get("temp") or d.get("temp"),
                   "healthy": sm.get("healthy"), "role": d.get("role") or "",
                   "tran": d.get("tran") or "", "mounts": d.get("mounts") or [],
                   "used_pct": (d.get("usage") or {}).get("pct"),
                   "free": (d.get("usage") or {}).get("free"),
                   "used": (d.get("usage") or {}).get("used"),
                   "dev": d.get("path") or "",
                   "removable": bool(d.get("removable")) or d.get("tran") == "usb",
                   "serial": d.get("serial") or "",
                   "hours": sm.get("hours") or sm.get("power_on_hours")})
    # system — always first: the screen fits 4 rows, and it must not
    # scroll out of them because of alphabetical order
    dk.sort(key=lambda x: (0 if (x["role"] == "system" or "/" in x["mounts"]) else 1,
                           x["name"] or ""))
    ct = []
    for c in (_safe(_docker_ps, []) or []):
        ct.append({"name": str(c.get("Names") or "?").split(",")[0],
                   "state": str(c.get("State") or ""),
                   "status": str(c.get("Status") or "")})
    ct.sort(key=lambda c: (c["state"] != "running", c["name"]))
    stacks = []
    for st_ in ((_safe(docker_stacks, {}) or {}).get("stacks") or []):
        stacks.append({"name": st_.get("name"), "running": st_.get("running") or 0,
                       "total": st_.get("total") or 0, "icon": st_.get("icon") or "",
                       "url": st_.get("url") or ""})
    stacks.sort(key=lambda x: x["name"] or "")
    _SCR_HEAVY["d"] = {"disks": dk, "containers": ct, "stacks": stacks}
    _SCR_HEAVY["t"] = now
    return _SCR_HEAVY["d"]


# Screen page two. The sources here are expensive (vnstat, apt, cron, tm) and change
# rarely — compute them once every 30 s and ONLY when the screen is actually on this page
# (the client requests ?p2=1). Otherwise the fast first-page poll would drag them along —
# exactly the bug that sent the box into load 14.
_SCR_P2 = {"t": 0, "d": {}}


def _screen_page2():
    now = time.time()
    if now - _SCR_P2["t"] < 30 and _SCR_P2["d"]:
        return _SCR_P2["d"]
    tm = _safe(tm_status, {}) or {}
    vn = _safe(vnstat_state, {}) or {}
    ap = _safe(apt_updates, {}) or {}
    wd = _safe(wud_state, {}) or {}
    cr = _safe(cron_jobs, {}) or {}
    hist = []
    sched = []
    for p in (_safe(nb_profiles_public, []) or []):
        for h in (_safe(lambda pid=p["id"]: nb_history(pid), []) or [])[:3]:
            # a run record has no files/size fields — they live in its jobs
            hist.append({"name": p["name"], "ts": h.get("ts") or 0,
                         "result": h.get("result") or "", "files": _nb_run_files(h),
                         "size": _nb_run_bytes(h), "dur": h.get("dur")})
        # scheduled run of the profile — for the "Next runs" tile
        pc = _safe(lambda pid=p["id"]: nb_load(pid), {}) or {}
        t = _safe(lambda: _nb_next_run(pc))
        if t:
            sched.append({"name": p["name"], "next": int(t), "kind": "backup"})
    hist.sort(key=lambda x: -x["ts"])
    d = {
        # Time Machine: the service may not be configured — then just empty
        "tm": {"enabled": bool(tm.get("enabled")), "installed": bool(tm.get("installed")),
               "path": tm.get("path") or "", "size": tm.get("size"),
               "free": (tm.get("space") or {}).get("free") if isinstance(tm.get("space"), dict) else tm.get("free"),
               "backups": tm.get("backups"), "mtime": tm.get("mtime") or 0,
               "quota_gb": tm.get("quota_gb")},
        # vnstat: no service -> ok:false, the tile is simply not drawn
        "traffic": {"ok": bool(vn.get("ok")), "today": vn.get("today") or {},
                    "month": vn.get("month") or {}, "total": vn.get("total") or {}},
        "updates": {"apt": ap.get("count") or 0, "security": ap.get("security") or 0,
                    "packages": [p.get("name") for p in (ap.get("packages") or [])][:6],
                    "images": (wd.get("count") or 0) if wd.get("ok") else None,
                    "image_list": [{"n": u.get("name"), "c": u.get("current"), "l": u.get("latest")}
                                   for u in (wd.get("updates") or [])][:6]},
        "cron": [{"name": j.get("name") or j.get("id") or "?", "next": j.get("next") or 0,
                  "last": j.get("last") or 0, "ok": j.get("ok")}
                 for j in (cr.get("jobs") or [])][:6],
        "sched": sorted(sched, key=lambda x: x["next"])[:8],
        "nbhist": hist[:8],
        "graphs": {"cpu": _safe(lambda: _gl_spark("cpu"), []) or [],
                   "temp": _safe(lambda: _gl_spark("temp"), []) or [],
                   "net": _safe(lambda: _gl_spark("net"), []) or [],
                   "mem": _safe(lambda: _gl_spark("mem"), []) or [],
                   "dtemp": _safe(lambda: _gl_spark("dtemp"), []) or [],
                   "pool": _safe(lambda: _gl_spark("pool"), []) or [],
                   "dio": _safe(lambda: _gl_spark("dio"), []) or []},
    }
    _SCR_P2["d"] = d
    _SCR_P2["t"] = now
    return d


def _screen_usb_cfg():
    c = usb_import_load()
    dest = c.get("dest") or ""
    return {"enabled": bool(c.get("enabled")), "dest": dest,
            "dest_off": bool(dest and _dest_disk_absent(dest))}


def screen_payload(lang="", p2=False):
    # the screen language comes from screen.json, not the kiosk browser; the UI is
    # English-only now, so lang is effectively always "en"
    cfg0 = load_screen()
    lang = lang or cfg0["lang"]
    en = (lang == "en")
    st = _safe(stats, {}) or {}
    # _glance_tile returns value/unit/state/raw, but NOT label — glance_payload
    # mixes it in from the catalog; here we do the same
    labels = {t[0]: (t[2] if en else t[1]) for t in glance_catalog()}
    tiles = {}
    now = time.time()
    for tid in SCREEN_TILES:
        ttl = SCREEN_TILE_TTL.get(tid, 2)
        c = _SCR_TILES.get(tid)
        if c and c["lang"] == lang and now - c["t"] < ttl:
            if c["d"]:
                tiles[tid] = c["d"]
            continue
        d = _safe(lambda t=tid: _glance_tile(t, en))
        if d:
            d = dict(d, label=labels.get(tid, tid))
            if tid in GLANCE_SPARKS:
                sp = _safe(lambda t=tid: _gl_spark(GLANCE_SPARKS[t]))
                if sp:
                    d["spark"] = sp
            tiles[tid] = d
        _SCR_TILES[tid] = {"t": now, "lang": lang, "d": d}
    hp = _safe(health_report, {}) or {}
    problems = [{"name": c.get("name"), "value": c.get("value"),
                 "lvl": c.get("lvl"), "hint": c.get("hint") or ""}
                for c in (hp.get("checks") or []) if c.get("lvl") in ("bad", "warn")]
    ev = _safe(lambda: events_list(0, 40), {}) or {}
    events = list(reversed(ev.get("events") or []))[:30]   # newest on top
    bks = []
    for p in (_safe(nb_profiles_public, []) or []):
        h = _safe(lambda pid=p["id"]: nb_history(pid), []) or []   # newest first
        last = h[0] if h else {}
        # the destination may be on an ejected disk — on the wall this must be SEEN, not
        # learned from a failed run
        pc = _safe(lambda pid=p["id"]: nb_load(pid), {}) or {}
        pbase = pc.get("dest_base") or ""
        b = {"id": p["id"], "name": p["name"],
             "running": bool(p.get("running")), "queued": bool(p.get("queued")),
             "configured": bool(p.get("configured")),
             "last_ts": int(last.get("ts") or 0),
             "last": last.get("result") or "",
             "last_bytes": _nb_run_bytes(last), "last_files": _nb_run_files(last),
             "dest": pbase,
             "dest_off": bool(pc.get("direction") == "push"
                              and pc.get("transport") == "local"
                              and pbase and _dest_disk_absent(pbase))}
        if b["running"]:
            b["live"] = _safe(lambda pid=p["id"]: _nb_live(pid), {}) or {}
        bks.append(b)
    # 24 bars = one per hour, 30 = one per day (as on status pages). The color is computed by the
    # client from frac (the slot's uptime fraction), not by the "worst state".
    av = _safe(lambda: avail_bars(24, 24), {}) or {}    # 2=up 1=local 0=off -1=no data
    av30 = _safe(lambda: avail_bars(720, 30), {}) or {}
    hv = _safe(_screen_heavy, {}) or {}
    _dt = _safe(_main_disk_temp, (None, "")) or (None, "")   # main-storage disk temp (cached fallback)
    # Show the SAME temperature as the Disks card below — both from THIS _screen_heavy read — so the
    # gauge and the card never disagree (they used to be two independent smartctl reads at different
    # times). Fall back to the standalone reading only if the card has no temp for the main disk yet.
    try:
        _mbases = {os.path.basename(x) for x in (_main_disk_devs() or [])}
        _ct = [d.get("temp") for d in (hv.get("disks") or [])
               if d.get("name") in _mbases and isinstance(d.get("temp"), (int, float))]
        if _ct:
            _dt = (max(_ct), _dt[1] or (sorted(_mbases)[0] if _mbases else ""))
    except Exception:
        pass
    # the wallpaper and its processing are THE SAME as on the desktop: the screen reads desktop.json,
    # so changing wallpaper/dimming in the browser reaches the panel on its own (wpVer in URL)
    ds = _safe(load_settings, {}) or {}
    # The screen is ALWAYS dark, even if the panel is switched to a light theme: it hangs on
    # the wall. So we take the style keys from the DARK profile (SET.themeProfiles.dark),
    # not the active one — otherwise a theme change in the browser would paint the wall white.
    dark = (ds.get("themeProfiles") or {}).get("dark") or {}
    look = {"wpVer": ds.get("wpVer") or 0, "theme": "dark"}
    for k in ("fxDim", "fxBlur", "fxNoise", "wdgOp", "wdgBlur", "wdgSat",
              "mbOp", "mbBlur", "mbSat", "elevStep", "elevLight", "elevBaseDark",
              "tintDark", "wdgDark", "accentHex", "goodHex", "warnHex", "dangerHex",
              "radius", "perf",
              "mbGrad", "mbGradH", "mbGradInt", "mbGradOp"):
        if k in dark:
            look[k] = dark[k]
        elif k in ds:
            look[k] = ds[k]
    # Touchscreen may override the wallpaper and its effects (Appearance → Wallpaper →
    # "Separate wallpaper for the touchscreen"). wpVer bumps → the client re-fetches ?screen=1,
    # which _wallpaper_screen() now builds from the screen's own source.
    if ds.get("scrWpOwn"):
        look["wpVer"] = ds.get("scrWpVer") or ds.get("wpVer") or 0
        for src_k, dst_k in (("scrFxDim", "fxDim"), ("scrFxBlur", "fxBlur"), ("scrFxNoise", "fxNoise")):
            if src_k in ds:
                look[dst_k] = ds[src_k]
    # Touchscreen may use one shared glass material for every surface (Appearance → Touchscreen →
    # "Own material"). The wall panel has no separate top bar: look["uni"] tells screen.html to paint
    # the bar with the EXACT card glass, so we only need to feed the cards' wdg* keys here.
    if ds.get("scrWdgOwn"):
        look["uni"] = True
        col = ds.get("scrWdgColor")
        if col:
            look["wdgDark"] = col
        for src_k, dst_k in (("scrWdgOp", "wdgOp"), ("scrWdgBlur", "wdgBlur"), ("scrWdgSat", "wdgSat")):
            if src_k in ds:
                look[dst_k] = ds[src_k]
    cfg = load_screen()
    host = st.get("host") or socket.gethostname()
    return {"host": host, "mdns": host + ".local", "ip": st.get("ip") or "",
            "iface": st.get("iface") or "", "net": st.get("net") or {"rx": 0, "tx": 0},
            "uptime": st.get("uptime") or 0, "cpu": st.get("cpu"),
            "temp": st.get("temp"), "load": st.get("load") or [],
            "mem": st.get("mem") or {}, "pool": st.get("disk_pool"),
            "root": st.get("disk_root"), "overall": hp.get("overall") or "ok",
            "tiles": tiles, "problems": problems, "events": events, "backups": bks,
            "avail": {"bars": av.get("bars") or [], "frac": av.get("frac") or [],
                      "pct": av.get("pct")},
            "avail30": {"bars": av30.get("bars") or [], "frac": av30.get("frac") or [],
                        "pct": av30.get("pct")}, "look": look,
            "disks": hv.get("disks") or [], "containers": hv.get("containers") or [],
            "stacks": hv.get("stacks") or [],
            "usb": _safe(lambda: usb_import_progress()["jobs"], []) or [],
            # import history survives reboot (ops-history.json), unlike /run
            "usbhist": _safe(lambda: usb_import_history(8), []) or [],
            # network speed units — a shared panel setting (MB/s vs Mbit/s)
            "netUnits": ds.get("netUnits") or "",
            # import destination: whether auto-import is enabled and whether the media is present.
            # An import failure shows in "Events", but that is the PAST — the tile needs
            # the current state: "disk ejected, nowhere to import".
            "usbcfg": _safe(_screen_usb_cfg, {}) or {},
            "swap": (st.get("mem") or {}).get("swap_total"),
            "throttled": st.get("throttled"), "psu_ma": st.get("psu_ma"),
            "dtemp": _dt[0], "dtemp_dev": _dt[1],   # main-storage disk temperature (+ short label)
            "dio": st.get("dio"),                   # main-storage disk throughput B/s (read+write)
            "storage_name": _safe(lambda: storage_conf().get("label") or _dt[1] or "storage"),
            "fsw_scan": _safe(lambda: (fsw_status().get("progress") or {}).get("status", "idle") != "idle"),
            "asleep": bool(_SCR["sleep"]),
            # the backlight is currently off (sleep/night/idle): the first tap should
            # only wake, not press the tile under the finger — the client puts up a shield
            "dark": _SCR["last"] == 0,
            "speed": _SCR["spd"], "speed_running": bool(_SCR["spd_run"]),
            "actions": cfg["actions"], "lang": cfg["lang"], "poll": cfg["poll"],
            "clock": cfg["clock_min"],
            "p2": (_safe(_screen_page2, {}) or {}) if p2 else None,
            "op": _safe(screen_op_state, {}) or {},   # background system-update progress (apt / images)
            "ts": int(time.time())}


SCREEN_OP_FILE = os.path.join(NAS_CONFIG, "screen-op.json")

def screen_op_state():
    st = _json_load_strict(SCREEN_OP_FILE, {})
    if st.get("running") and time.time() - (st.get("started") or 0) > 20 \
            and not _systemd_active("nas-screen-op"):     # crashed/killed → don't spin forever
        st["running"] = False
        st["done"] = st.get("done") or int(time.time())
    return st

def screen_op_bg(op):
    """Start a system update from the touchscreen (apt / docker images) in the background.
    Blocked while a backup runs (don't fight for disk/network); one at a time."""
    if nb_any_active():
        return {"ok": False, "log": "a backup is running — try again after it finishes"}
    if screen_op_state().get("running"):
        return {"ok": False, "log": "an update is already running"}
    _json_save(SCREEN_OP_FILE, {"running": True, "op": op, "started": int(time.time()),
                                "line": "starting…", "ok": None, "done": None}, indent=None)
    cmd = ["systemd-run", "--collect", "--quiet", "--unit", "nas-screen-op",
           "--setenv=SUDO_USER=" + TARGET_USER, "--setenv=HOME=" + HOME,
           sys.executable, os.path.join(HERE, "nas-web.py"), "screen-op", op]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        _json_save(SCREEN_OP_FILE, {"running": False, "ok": False, "line": str(e)}, indent=None)
        return {"ok": False, "log": str(e)}
    if r.returncode != 0:
        _json_save(SCREEN_OP_FILE, {"running": False, "ok": False, "line": (r.stderr or "failed")[:120]}, indent=None)
        return {"ok": False, "log": (r.stderr or "failed to start")[:120]}
    return {"ok": True}

def screen_op_run(op):
    """Driver in the transient unit. Writes progress to screen-op.json for the screen to poll."""
    st = {"running": True, "op": op, "started": int(time.time()), "line": "starting…",
          "ok": None, "done": None}
    def w(line, **kw):
        st["line"] = line; st["ts"] = int(time.time()); st.update(kw)
        _json_save(SCREEN_OP_FILE, st, indent=None)
    ok = True
    try:
        if op == "apt":
            w("checking for updates…")
            _run(["apt-get", "update"], timeout=300)
            w("installing package updates…")
            env = dict(_C_ENV, DEBIAN_FRONTEND="noninteractive")
            r = _run(["apt-get", "-y", "-o", "Dpkg::Options::=--force-confold",
                      "-o", "Dpkg::Options::=--force-confdef", "upgrade"], timeout=3600, env=env)
            ok = bool(r.get("ok"))
            try: log_event("action", "System update from the screen", "", "ok" if ok else "warn", kind="action", desk=True)
            except Exception: pass
        elif op == "images":
            try:
                names = [n for n in sorted(os.listdir(STACKS_DIR))
                         if os.path.isfile(os.path.join(STACKS_DIR, n, "compose.yaml"))
                         or os.path.isfile(os.path.join(STACKS_DIR, n, "docker-compose.yml"))]
            except OSError:
                names = []
            n = len(names)
            if not n:
                w("no docker stacks")
            for i, name in enumerate(names):
                w("%s — pulling images (%d/%d)…" % (name, i + 1, n))
                _dc(name, "pull", timeout=900)
                w("%s — recreating (%d/%d)…" % (name, i + 1, n))
                if not _dc(name, "up", "-d", timeout=300).get("ok"):
                    ok = False
            _safe(wud_invalidate)
            try: log_event("action", "Docker images updated from the screen", "", "ok" if ok else "warn", kind="action", desk=True)
            except Exception: pass
        else:
            ok = False; st["line"] = "unknown update"
    except Exception as e:
        ok = False; st["line"] = "error: %s" % e
    st.update(running=False, ok=ok, done=int(time.time()),
              line="done" if ok else "finished with errors")
    _json_save(SCREEN_OP_FILE, st, indent=None)

def screen_action(b):
    """Actions from the local screen. 'touch' is not an action — it is the wake
    signal, so it works even when the action buttons are switched off."""
    a = str(b.get("a") or "")
    if a == "touch":
        # a click on the "dim" button arrives together with pointerdown -> touch, and that
        # could arrive AFTER sleep and wake the screen right away. Half a second of grace.
        if _SCR["sleep"] and time.time() - _SCR.get("slept_at", 0) < 1.0:
            return {"ok": True, "ignored": True}
        _SCR["touch"] = time.time()
        _SCR["sleep"] = False                      # any touch wakes
        _safe(lambda: _screen_apply(force=True))
        return {"ok": True}
    if a == "sleep":
        _SCR["sleep"] = True
        _SCR["slept_at"] = time.time()
        _safe(lambda: _screen_apply(force=True))
        return {"ok": True}
    cfg = load_screen()
    if not cfg["actions"]:
        return {"ok": False, "log": "actions from the screen are disabled"}
    if a == "backup":
        return nb_run_bg(_nb_bpid(b))
    if a == "backup_stop":
        pid = _nb_bpid(b) or next((pr["id"] for pr in (_safe(nb_profiles, []) or [])
                                   if _safe(lambda x=pr: nb_run_active(x["id"]))), NB_MAIN)
        _safe(lambda: _nb_queue_remove(pid))
        try:
            open(nb_run_cancel(pid), "w").close()   # the driver checks this file and stops gracefully
        except OSError:
            pass
        return {"ok": True}
    if a == "eject":
        # disk_eject sam refuses system/pool disks; the screen only offers the
        # button for mounted USB drives, but the server must not trust that
        dev = str(b.get("dev") or "")
        if not dev.startswith("/dev/"):
            dev = "/dev/" + dev
        r = disk_eject(dev)
        if r.get("ok"):
            _SCR_HEAVY["t"] = 0            # the disk is gone — the list must see it immediately
        return r
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
        # the speed test blocks for 10-18 s — the screen must not hang on fetch
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "running": True}
    if a == "stack":
        op = str(b.get("op") or "")
        if op not in ("up", "down", "restart", "stop", "start"):
            return {"ok": False, "log": "unknown operation"}
        return stack_action(str(b.get("name") or ""), op)
    if a == "apt_update":
        return screen_op_bg("apt")
    if a == "img_update":
        return screen_op_bg("images")
    if a == "fsw_accept":       # "I deleted those on purpose" — accept the deletions as the new normal
        return _safe(fsw_accept, {"ok": False}) or {"ok": False}
    if a in ("reboot", "poweroff"):
        log_event("screen_power", "From the screen: " + ("reboot" if a == "reboot" else "shutdown"),
                  "requested by a button on the local screen", "warn", "system")
        return power(a)
    return {"ok": False, "log": "unknown action"}


# Wallpaper for the local screen: the source is for the desktop (2-4 MB, 2560+ px), but the panel
# needs 800x480. Chromium on the Pi 4 pulled the full frame into memory AND blurred it with blur(27px) —
# that is noticeable work on a weak GPU. We keep a downscaled copy next to the original and serve it
# to the kiosk; we rebuild it when the original changes (the wallpaper rotates on a timer).
_WALL_SCR_LOCK = threading.Lock()


def _wallpaper_screen(w=800, h=480):
    # source: the touchscreen's own image when the user enabled it AND uploaded one;
    # otherwise the desktop wallpaper (reduced to the panel size, as before).
    src = ""
    try:
        if (load_settings() or {}).get("scrWpOwn"):
            src = _wallpaper_scr_path()
    except Exception:
        pass
    if not src:
        src = _wallpaper_path()
    if not src:
        return ""
    dst = os.path.join(NAS_CONFIG, "wallpaper-screen.webp")
    marker = os.path.join(NAS_CONFIG, "wallpaper-screen.src")
    # Cache key = source path + its mtime: a plain "dst newer than src" check is wrong here,
    # because switching to an OLDER source (e.g. turning the separate wallpaper off) must rebuild.
    try:
        want = "%s:%d" % (src, int(os.path.getmtime(src)))
    except OSError:
        want = src
    if os.path.isfile(dst) and _read(marker) == want:
        return dst
    with _WALL_SCR_LOCK:
        if os.path.isfile(dst) and _read(marker) == want:   # built while we waited for the lock
            return dst
        tmp = dst + ".tmp.webp"
        vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (w, h, w, h))
        try:
            p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                                "-vf", vf, "-quality", "82", tmp],
                               capture_output=True, text=True, timeout=60)
            if p.returncode != 0 or not os.path.isfile(tmp):
                return src                     # failed — serve the original, don't crash
            os.replace(tmp, dst)
            try:
                with open(marker, "w") as f:   # remember which source this copy was built from
                    f.write(want)
            except OSError:
                pass
        except (OSError, subprocess.SubprocessError):
            return src
    return dst


class H(BaseHTTPRequestHandler):
    server_version = "nas-web"
    # Socket timeout: without it a hung/slow connection (slowloris) holds a
    # thread forever. The thread pool is unbounded, so eternal threads = crash.
    timeout = 30
    def log_message(self, *a):  # quiet
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

    # ---- authentication ----
    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "nasauth":
                return v
        return ""

    def _client_ip(self):
        # the server listens on 0.0.0.0 directly (no trusted proxy), so we take
        # the real socket address, NOT the client-spoofable X-Forwarded-For
        try:
            return self.client_address[0]
        except (AttributeError, IndexError):
            return ""

    def _authed(self):
        return session_valid(self._cookie_token())

    def _local(self):
        """Request came from the box itself (the local screen's kiosk browser)."""
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _origin_ok(self):
        """Protection against CSRF and cross-site WebSocket: Origin (if sent) must
        match Host. Requests without Origin (curl) are allowed — a session is still required."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == (self.headers.get("Host") or "").strip()

    def _session_cookie(self, tok=None):
        if tok is None:
            return "nasauth=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        return "nasauth=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax" % (tok, SESSION_TTL)

    def _auth_endpoints(self, p):
        """Open /api/auth/* endpoints. Returns True if the request was handled."""
        if p == "/api/auth/state":
            self._json({"configured": auth_configured(), "authed": self._authed()})
        elif p == "/api/auth/login":
            b = self._body()
            # anti-bruteforce under lock: gate BEFORE checking the password, so parallel
            # requests don't slip through in a batch (ThreadingHTTPServer)
            with _login_lock:
                now = time.time()
                if now - _login_fail["t"] > 60:           # attempt window — one minute
                    _login_fail["n"] = 0
                blocked = _login_fail["n"] >= 8
            if blocked:
                time.sleep(1.0)
                self._json({"ok": False, "log": "too many attempts, wait a minute"}, 429)
            elif auth_configured() and auth_check_password(b.get("password", "")):
                with _login_lock:
                    _login_fail["n"] = 0
                ip = self._client_ip()
                if ip and ip not in _known_ips():         # login from a new address
                    _remember_ip(ip)
                    threading.Thread(target=mon_notify, args=("panel_new:" + ip,
                        "NAS: panel login from a new address", "Successful login from %s" % ip, "panel_new"),
                        daemon=True).start()
                self._json({"ok": True}, cookie=self._session_cookie(session_new()))
            else:
                with _login_lock:
                    _login_fail["n"] += 1; _login_fail["t"] = time.time(); n = _login_fail["n"]
                if n >= load_monitor().get("events", {}).get("panel_fail", {}).get("threshold", 5):
                    threading.Thread(target=mon_notify, args=("panel_fail",
                        "NAS: panel password guessing attempt", "%d failed login attempts (last from %s)"
                        % (n, self._client_ip() or "?"), "panel_fail"), daemon=True).start()
                time.sleep(0.5)     # slow down bruteforce
                self._json({"ok": False, "log": "wrong password"}, 403)
        elif p == "/api/auth/setup":
            # initial password setup; with an active session — password change
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
        """Run the engine and stream stdout line by line (for the live log in the wizard)."""
        if not ENGINE_ACTION_RE.match(action or ""):
            self._json({"error": "invalid action"}, 400); return
        env = _engine_env(params, dry)   # ValueError propagates out -> 400
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
            self.wfile.write(("launch error: %s\n__EXIT__1\n" % e).encode()); return
        # watchdog: kill a hung engine (e.g. blocked on a dead mount) to avoid leaking a thread/socket
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
                log_event("action", "Wizard: %s%s" % (action, "" if ok else " — error"),
                          "", "ok" if ok else "warn", kind="action", desk=False)
            except Exception:
                pass

    def _stream_cmd(self, cmd, env=None, timeout=1800):
        """Stream an arbitrary command's stdout line by line with an __EXIT__ marker (like _stream_engine)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            p = subprocess.Popen(cmd, env=env or _C_ENV, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self.wfile.write(("launch error: %s\n__EXIT__1\n" % e).encode()); return
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
        # Cap BEFORE reading: otherwise Content-Length: 2000000000 (even before auth,
        # on /api/auth/login) would force reading 2 GB into a single thread and kill the service
        # with OOM on the Pi. Large binary uploads go through the streaming _upload_raw.
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
        # Graceful stub for the removed runtime i18n. Devices with a pre-english-only
        # shell cached (SW/PWA) still request /i18n.js; a 404 leaves nasTr undefined and
        # every tapped handler throws (button shows :active, does nothing). Serve a no-op
        # identity so those stale shells keep working until they reload the new shell.
        if rel == "i18n.js" and not os.path.isfile(full):
            body = b'window.nasTr=function(s){return s};window.NAS_LANG="en";'
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
        ext = os.path.splitext(full)[1]
        # ETag from mtime+size: no-cache without a validator was treated by mobile browsers
        # as "can be taken from cache" — that is how the old i18n.js got stuck (whole UI in Russian
        # under EN). With a validator revalidation WORKS: unchanged → 304, changed →
        # fresh file. HTML stays no-store (there the validator was not trusted at all).
        try:
            st_ = os.stat(full)
            etag = '"%x-%x"' % (int(st_.st_mtime), st_.st_size)
        except OSError:
            etag = None
        if etag and ext != ".html" and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # HTML — do NOT cache at all (mobile browsers with no-cache without a validator
        # still showed the old shell); revalidate JS/CSS by ETag.
        if ext == ".html":
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
        elif ext in (".js", ".css"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            if etag:
                self.send_header("ETag", etag)
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
        # HTTP Range → video/audio seeking works + streaming without loading the file into memory
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
            pass   # browser seeked/closed — normal
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
        # the URL key is stable → safe to keep in the browser cache for a long time
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
        """Streaming transcode into browser-friendly mp4 (for HEVC/exotics). t = start, sec."""
        q = parse_qs(urlparse(self.path).query)
        src = os.path.realpath((q.get("path") or [""])[0])
        try:
            t = max(0.0, float((q.get("t") or ["0"])[0]))
        except ValueError:
            t = 0.0
        try:
            aidx = max(0, int((q.get("a") or ["0"])[0]))   # selected audio track (0:a:aidx)
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
        # h264 is already supported by the browser — remux only (fast, no load); otherwise re-encode
        if vcodec == "h264":
            vargs = ["-c:v", "copy"]
        else:
            vargs = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                     "-vf", "scale='min(1280,iw)':-2", "-pix_fmt", "yuv420p"]
        cmd = ["ffmpeg", "-v", "error"]
        if t > 0:
            cmd += ["-ss", "%.3f" % t]
        # mapping: video + selected audio track ('?' — don't fail if it's missing)
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
        """Extract the embedded TEXT subtitle track (0:s:idx) as WebVTT."""
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
        """Streaming binary upload (no base64 — doesn't crash the tab on large files).
        rel — subfolder path inside the destination (uploading a whole folder)."""
        q = parse_qs(urlparse(self.path).query)
        d, err = _fs_guard((q.get("path") or [""])[0], into=True)   # don't upload into system trees/engine
        if err:
            return {"ok": False, "log": err}
        name = os.path.basename(((q.get("name") or [""])[0]).strip())
        if not os.path.isdir(d):
            return {"ok": False, "log": "destination is not a directory"}
        if not name:
            return {"ok": False, "log": "no file name"}
        # subfolders when uploading a folder. Each segment is checked separately: ".." and
        # absolute paths won't get through here, nor will a symlink pointing out (we verify realpath).
        rel = ((q.get("rel") or [""])[0]).strip().replace("\\", "/")
        if rel:
            segs = [x for x in rel.split("/") if x not in ("", ".")]
            if len(segs) > 32 or any(x == ".." or "/" in x or "\0" in x for x in segs):
                return {"ok": False, "log": "invalid path"}
            sub = os.path.realpath(os.path.join(d, *segs))
            if sub != d and not sub.startswith(d + os.sep):
                return {"ok": False, "log": "path outside the destination directory"}
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
        if got < n:                      # abort/cancel — don't leave a truncated file
            try:
                os.remove(dest)
            except OSError:
                pass
            return {"ok": False, "log": "upload aborted"}
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
        sock.settimeout(None)      # the WS terminal lives long and idles between keystrokes —
                                   # the global timeout=30 (anti-slowloris) would tear it every 30s
        ex = (parse_qs(urlparse(self.path).query).get("exec") or [""])[0]
        ex = ex if re.match(r"^[a-zA-Z0-9_.-]+$", ex or "") else ""
        pid, master = pty.fork()
        if pid == 0:                       # child -> bash or docker exec
            os.environ["TERM"] = "xterm-256color"
            if ex:                         # exec into a container — stay root (need access to docker.sock)
                os.execvp("docker", ["docker", "exec", "-it", ex, "sh", "-c",
                                     "command -v bash >/dev/null && exec bash || exec sh"])
                os._exit(1)
            try:
                if os.geteuid() == 0:      # if the server is root — drop privileges to the user
                    u = pwd.getpwnam(TARGET_USER)
                    # initgroups before setuid: otherwise the shell has no supplementary groups (video,
                    # docker, gpio…) and vcgencmd/docker fail without sudo. setgid AFTER.
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
                    # idle — don't drop the connection, send a WS ping: this is keepalive
                    # (the terminal doesn't drop) and also detects a dead peer (send will fail)
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
                        if pay[:1] == b"\x01":          # control: resize
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
            # compare as bytes: compare_digest on a non-ASCII str raises TypeError,
            # which on this pre-auth path would 500 instead of a clean 401
            tok_ok = bool(cfg["enabled"] and cfg["token"]
                          and hmac.compare_digest(tok.encode("utf-8", "ignore"),
                                                  str(cfg["token"]).encode("utf-8")))
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
            # from the LAN the page can be viewed, but it needs /api/screen/* —
            # without a session they return 401, and you got a "dark screen with icons"
            if not (self._local() or self._authed()):
                self.send_response(302)
                self.send_header("Location", "/?next=/screen")
                self.end_headers()
                return
            self._static("/screen.html"); return
        if p == "/api/icon" and (self._local() or self._authed()):
            self._send_icon((q.get("u") or [""])[0]); return   # stack icons for the screen
        if p == "/api/wallpaper/img" and (self._local() or self._authed()):
            # the kiosk draws the same wallpaper, but sized to its panel (see _wallpaper_screen)
            wp = (_safe(_wallpaper_screen) if (q.get("screen") or [""])[0]
                  else None) or _wallpaper_path()
            if wp:
                self._sendraw(wp)
            else:
                self.send_error(404)
            return
        if p == "/api/screen/config" and self._local():
            u = _run(["systemctl", "is-enabled", "nas-screen"], timeout=5)
            self._json({"config": load_screen(), "present": bool(_bl_dir()),
                        "unit": (u.get("log") or "").strip()}); return
        if p == "/api/screen/data":
            # the local screen works without a session; from the LAN — only with a password
            if not (self._local() or self._authed()):
                self._json({"error": "auth"}, 401); return
            self._json(screen_payload((q.get("lang") or [""])[0],
                                      p2=bool((q.get("p2") or [""])[0]))); return
        if p.startswith("/api/") and not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        try:
            if p == "/api/stats":
                self._json(stats())
            elif p == "/api/screen/config":
                # _run() returns a DICT {ok,code,log}: calling .strip() on it dropped the endpoint
                # to 500, and the settings tab showed only the header
                u = _run(["systemctl", "is-enabled", "nas-screen"], timeout=5)
                self._json({"config": load_screen(), "present": bool(_bl_dir()),
                            "unit": (u.get("log") or "").strip()})
            elif p == "/api/glance/config":
                self._json({"config": load_glance()})
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
                    self._json({"error": "no file"}, 404)
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
                    self._json({"ok": False, "log": "no such backup"}, 404)
                else:
                    self._json(settings_backup_inspect(fp))
            elif p == "/api/settings-backup/get":
                nm = (q.get("name") or [""])[0]
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "no such backup"}, 404)
                else:
                    self._sendraw(fp, True)
            elif p == "/api/health":
                self._json(health_report())
            elif p == "/api/desktop":
                self._json({"apps": discover_desktop_apps(), "volumes": external_volumes(),
                            "home": HOME, "user": TARGET_USER})
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
            elif p == "/api/fs/trash/stat":
                self._json(fs_trash_stat())
            elif p == "/api/fs/zip":
                self._send_zip(q.get("item") or [], (q.get("name") or ["archive.zip"])[0])
            elif p == "/api/storage":
                self._json(storage_state())
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
                self._json(stack_catalog())
            elif p == "/api/store/recipe":
                self._json(stack_recipe((q.get("id") or [""])[0]))
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
            elif p == "/api/sysconf":
                self._json(sysconf())
            elif p == "/api/usb-import":
                self._json({"config": usb_import_load(), "drives": usb_removable()})
            elif p == "/api/usb-devices":
                self._json(usb_devices())
            elif p == "/api/motd":
                self._json({"config": motd_load(), "preview": motd_preview()})
            elif p == "/api/smb":
                self._json(smb_overview())
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
                # screen=1 -> a copy for the small screen (the same logic as for the kiosk;
                # previously the parameter worked only with loopback, and the LAN got the original)
                wp = (_safe(_wallpaper_screen) if (q.get("screen") or [""])[0]
                      else None) or _wallpaper_path()
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
            elif p == "/api/backup/compare":
                self._json(nb_compare_state(_nb_qpid(q)))
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
        except Exception as e:  # don't crash the server
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
            # touch on the box itself = physical access; we don't ask for a password on this screen.
            # From the panel (session) it's allowed too — there you already logged in with a password.
            if not (self._local() or self._authed()):
                self._json({"error": "forbidden"}, 403); return
            self._json(screen_action(self._body())); return
        if p == "/api/agent/notify":
            # local shell agents (nas-notify.sh) — pre-auth, loopback only
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self._json({"error": "forbidden"}, 403); return
            self._json(agent_notify(self._body())); return
        if not self._authed():
            self._json({"error": "auth", "configured": auth_configured()}, 401); return
        # action logging: cache the body and intercept the response; in finally
        # be sure to remove the instance attributes (keep-alive reuses the handler)
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
                self._json(note_new(b.get("folder", ""), b.get("title", ""),
                                    b.get("kind") or "md"))
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
            elif p == "/api/smb/share":
                self._json(smb_share_set(self._body()))
            elif p == "/api/smb/share/delete":
                self._json(smb_share_del(self._body().get("name", "")))
            elif p == "/api/smb/user":
                self._json(smb_user_set(self._body()))
            elif p == "/api/smb/user/delete":
                self._json(smb_user_del(self._body().get("name", "")))
            elif p == "/api/ops":
                self._json(ops_hist_add(self._body()))
            elif p == "/api/ops/clear":
                self._body(); self._json(ops_hist_clear())
            elif p == "/api/wallpaper/fetch":
                b = self._body()
                self._json(wallpaper_fetch(b.get("url", ""), bool(b.get("screen"))))
            elif p == "/api/wallpaper/upload":
                b = self._body()
                self._json(wallpaper_upload(b.get("data", ""), bool(b.get("screen"))))
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
                    ok = push_notify("NAS: test", "Notification test from the control panel")
                    self._json({"ok": ok, "log": "" if ok else "Pushover is not configured: fill in the keys"})
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
                    self._json({"ok": False, "log": "no such backup"}, 404)
                else:
                    secs = b.get("sections")
                    self._json(settings_backup_restore(
                        fp, secs if isinstance(secs, list) else None))
            elif p == "/api/settings-backup/delete":
                b = self._body(); nm = b.get("name", "")
                fp = os.path.join(settings_backup_dir(), nm)
                if not _BK_NAME_RE.match(nm) or not os.path.isfile(fp):
                    self._json({"ok": False, "log": "no such backup"}, 404)
                else:
                    os.remove(fp); self._json({"ok": True})
            elif p == "/api/settings-backup/upload":
                # restore from a file on the computer: request body = tar.gz
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n <= 0 or n > 64 * 1024 * 1024:
                    self._json({"ok": False, "log": "bad archive size"}, 400)
                else:
                    tmp = os.path.join(settings_backup_dir(), ".upload.tmp")
                    with open(tmp, "wb") as f:
                        remaining = n
                        while remaining > 0:
                            chunk = self.rfile.read(min(262144, remaining))
                            if not chunk:
                                break
                            f.write(chunk); remaining -= len(chunk)
                    # Don't restore right away: put the archive in the list and return its name,
                    # so the client shows the same section-selection dialog. Otherwise
                    # a foreign file would silently overwrite the panel password.
                    info = settings_backup_inspect(tmp) if remaining == 0 \
                        else {"ok": False, "log": "upload interrupted"}
                    if not info.get("ok") or not info.get("sections"):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        self._json({"ok": False, "log": info.get("log")
                                    or "the archive contains no NAS-OS settings files"}, 400)
                    else:
                        # date in the name — so rotation by backup_keep cuts by age
                        name = "nas-settings-%s-up.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
                        dst = os.path.join(settings_backup_dir(), name)
                        os.replace(tmp, dst); os.chmod(dst, 0o600)
                        self._json({"ok": True, "name": name, "sections": info["sections"],
                                    "log": "archive uploaded"})
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
            elif p == "/api/storage":
                self._json(storage_save(self._body()))
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
                    self._json({"ok": False, "log": "invalid device"}, 400)
                elif not re.match(r"^[A-Za-z0-9._-]{1,16}$", label or ""):
                    self._json({"ok": False, "log": "invalid label (Latin letters/digits/._-, up to 16)"}, 400)
                else:
                    self._json(engine("label-disk", {"dev": dev, "label": label}))
            elif p == "/api/disk/format":
                b = self._body(); dev = b.get("dev", ""); label = b.get("label", "")
                if not re.match(r"^/dev/[\w-]+$", dev or ""):
                    self._json({"ok": False, "log": "invalid device"}, 400)
                elif b.get("role", "data") not in ("data", "parity", "usb"):
                    self._json({"ok": False, "log": "invalid role"}, 400)
                elif b.get("fs", "ext4") not in ("ext4", "xfs", "btrfs", "exfat", "ntfs", "vfat"):
                    self._json({"ok": False, "log": "invalid filesystem"}, 400)
                elif label and not re.match(r"^[A-Za-z0-9._-]{1,16}$", label):
                    self._json({"ok": False, "log": "invalid label (Latin letters/digits/._-, up to 16)"}, 400)
                elif any(mp in _SYS_MPS or mp == STORAGE or mp.startswith("/mnt/disk") or mp.startswith("/mnt/parity")
                         for mp in _disk_mountpoints(dev)):
                    # web-side guard on top of the engine: don't format the system/pool disk
                    self._json({"ok": False, "log": "this is a system or pool disk — formatting is not allowed"}, 400)
                else:
                    self._json(engine("format-disk", {"dev": dev, "role": b.get("role", "data"),
                        "fs": b.get("fs", "ext4"), "label": label}, dry=b.get("dry", False)))
            elif p == "/api/disk/mount-dev":
                b = self._body(); dev = b.get("dev", ""); target = (b.get("target") or "").strip()
                if not re.match(r"^/dev/[\w-]+$", dev or ""):
                    self._json({"ok": False, "log": "invalid device"}, 400)
                elif target and (not re.match(r"^/[A-Za-z0-9._/ -]{1,120}$", target) or ".." in target
                                 or target in ("/", "/etc", "/usr", "/bin", "/boot", "/home", "/var", "/root")):
                    self._json({"ok": False, "log": "invalid mount point"}, 400)
                else:
                    params = {"dev": dev}
                    if target:
                        params["target"] = target
                    self._json(engine("mount-dev", params))
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
                    self._json({"ok": False, "log": "invalid user name"}, 400)
                elif not re.match(r"^/[A-Za-z0-9._/-]{1,120}$", base or "") or ".." in base:
                    self._json({"ok": False, "log": "invalid base directory"}, 400)
                else:
                    self._json(engine("automount", {"enable": "1" if b.get("enable", True) else "0",
                        "user": user, "base": base}))
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
                b = self._body(); self._json(fs_trash(b.get("path", ""), b.get("size")))
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
                self._json(stack_install(b.get("id", ""), b.get("values") or {},
                                         b.get("compose"), b.get("env")))
            elif p == "/api/store/replica":
                b = self._body()
                self._json(store_replica_save(b.get("id", ""), b))
            elif p == "/api/store/replica/run":
                # replica sync/restore — a long bash with a log stream
                b = self._body()
                try:
                    script, env = store_replica_script(b.get("id", ""),
                                                       "restore" if b.get("mode") == "restore" else "sync")
                except ValueError as e:
                    self._json({"ok": False, "log": str(e)}); return
                log_event("action", "Replica %s: %s" % (b.get("id", ""),
                          "restore" if b.get("mode") == "restore" else "sync"),
                          "", "ok", kind="svc", desk=False)
                self._stream_cmd(["bash", "-c", script],
                                 env=dict(_C_ENV, **env), timeout=86400)
            elif p == "/api/stack/stream":
                # long compose actions (up on install, pull) — via stream
                b = self._body()
                name, action = b.get("name", ""), b.get("action", "")
                amap = {"up": ["up", "-d"], "pull": ["pull"], "update": ["pull"],
                        "rebuild": ["up", "-d", "--build"]}
                if not _STACK_RE.match(name) or not os.path.isfile(_compose_path(name)) \
                        or action not in amap:
                    self._json({"ok": False, "log": "invalid stack/action"}); return
                wud_invalidate()
                args = ["docker", "compose", "-f", _compose_path(name), "-p", name] + amap[action]
                if action == "update":       # pull + restart with one button
                    self._stream_cmd(["bash", "-c",
                        "%s && docker compose -f %s -p %s up -d" %
                        (" ".join(map(shlex.quote, args)),
                         shlex.quote(_compose_path(name)), shlex.quote(name))], timeout=3600)
                else:
                    self._stream_cmd(args, timeout=3600)
            elif p == "/api/updates/apply":
                # installing apt updates with a log stream; DEBIAN_FRONTEND so it asks no questions
                self._body()
                env = dict(_C_ENV, DEBIAN_FRONTEND="noninteractive")
                self._stream_cmd(["apt-get", "-y",
                                  "-o", "Dpkg::Options::=--force-confold",
                                  "-o", "Dpkg::Options::=--force-confdef", "upgrade"],
                                 env=env, timeout=3600)
                try:
                    log_event("action", "apt package update", "", "ok", kind="action", desk=True)
                except Exception:
                    pass
            elif p == "/api/backup/config":
                b = self._body()
                cfg = nb_save(b.get("config", {}), _nb_bpid(b))
                self._json({"config": nb_public(cfg), "profile": cfg["id"],
                            "profiles": nb_profiles_public()})
            elif p == "/api/backup/test":
                self._json(nb_test(nb_load(_nb_bpid(self._body()))))
            elif p == "/api/backup/key":
                b = self._body()
                act = str(b.get("action") or "")
                if act == "gen":
                    self._json(nb_key_gen(force=bool(b.get("force"))))
                elif act == "install":
                    self._json(nb_key_install(nb_load(_nb_bpid(b)),
                                              "src" if b.get("side") == "src" else "dst"))
                else:
                    self._json(nb_key_info())
            elif p == "/api/backup/browse":
                b = self._body()
                cfg = nb_load(_nb_bpid(b))
                self._json(nb_browse_dest(cfg, b.get("path", "")) if b.get("dest")
                           else nb_browse(cfg, b.get("path", "")))
            elif p == "/api/backup/run-bg":
                b = self._body()
                self._json(nb_run_bg(_nb_bpid(b), dry=bool(b.get("dry", False)),
                                     allow_delete=bool(b.get("allow_delete", False))))
            elif p == "/api/backup/compare":
                b = self._body()
                self._json(nb_compare_bg(_nb_bpid(b), deep=bool(b.get("deep", False))))
            elif p == "/api/backup/compare/cancel":
                self._json(nb_compare_cancel_req(_nb_bpid(self._body())))
            elif p == "/api/backup/cancel":
                b = self._body()
                pid = nb_load(_nb_bpid(b))["id"]
                _nb_queue_remove(pid)                        # if it hasn't started yet — just drop it from the queue
                try:
                    open(nb_run_cancel(pid), "w").close()    # flag for the driver process in the unit
                except OSError:
                    pass
                # mark the stop in the state: the panel shows "stopping…" even
                # if the page was reloaded while rsync finishes dying
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
                        self._json({"ok": False, "log": "the path must be absolute without .."}); return
                    params["tm_path"] = path
                user = str(b.get("user", "")).strip()
                if user:
                    if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", user):
                        self._json({"ok": False, "log": "invalid user name"}); return
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
                               else {"ok": False, "log": "no ts"})
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
                    self._json({"ok": False, "log": "unknown action"})
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
        apply_spindown_all()          # restore disk sleep settings after start/reboot
    except Exception:
        pass
    try:
        apply_glance_timer()          # re-assert availability-poll cadence from glance.json
    except Exception:
        pass
    try:
        _pool_alias_apply(load_maintenance().get("pool_alias", ""))   # pool symlink (e.g. /volume2)
    except Exception:
        pass
    try:
        _snap_sched_apply(load_maintenance())    # SnapRAID schedule from settings
    except Exception:
        pass
    try:
        _usb_sh_sync()      # the import helper on disk may have gone stale after git pull
    except Exception:
        pass
    try:
        _nb_migrate()       # old flat backup config -> list of profiles
    except Exception:
        pass
    try:
        _motd_extras_apply(motd_load())   # third-party greeting fragments — per setting
    except Exception:
        pass
    threading.Thread(target=monitor_loop, daemon=True).start()
    srv = _Server(("0.0.0.0", PORT), H)
    ip = lan_ip()
    print(f"nas-web started:  http://{ip}:{PORT}   (http://{socket.gethostname()}.local:{PORT})")
    print(f"  web/     : {WEB_DIR}")
    print(f"  services : {SERVICES}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 2 and sys.argv[1] == "thumbs-sweep":
        print("thumbs-sweep: generated", thumbs_sweep(sys.argv[2:]), "previews")
    elif len(sys.argv) > 1 and sys.argv[1] == "backup-run":
        _args = sys.argv[2:]
        _pid = next((a for a in _args if a not in ("dry", "allow-delete")), NB_MAIN)
        nb_run_cli(_pid, dry=("dry" in _args), allow_delete=("allow-delete" in _args))
    elif len(sys.argv) > 1 and sys.argv[1] == "backup-compare":
        _args = sys.argv[2:]
        _pid = next((a for a in _args if a != "deep"), NB_MAIN)
        nb_compare_run(_pid, deep=("deep" in _args))
    elif len(sys.argv) > 2 and sys.argv[1] == "screen-op":
        screen_op_run(sys.argv[2])
    else:
        main()
