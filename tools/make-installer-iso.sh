#!/usr/bin/env bash
# make-installer-iso.sh — build a flash-and-forget NAS-OS installer ISO.
# Works on macOS (primary use case: the old box is dead and only the Mac is
# around) and on any Linux, including the NAS itself.
#
# Debian has no Raspberry Pi Imager: the netinst asks twenty questions a NAS
# owner should never have to answer. This script plays the Imager's role — it
# takes the official netinst ISO and bakes in a preseed with every answer plus
# a first-boot unit that installs NAS-OS itself. Flash the result with
# balenaEtcher / Raspberry Pi Imager ("Use custom"), boot the box from USB,
# walk away: Debian installs, the box reboots, NAS-OS installs, and the panel
# comes up on http://<hostname>.local — where the setup wizard offers to
# restore the old box's settings if its disks are plugged in.
#
#   ./make-installer-iso.sh --user oleg --host nas [--ref v2026.08.04.1] \
#        [--tz Europe/Madrid] [--out ~/nas-os-installer.iso]
#
# macOS prerequisites:  brew install xorriso
# Linux prerequisites:  apt install xorriso
#
# The password is asked interactively (never lands in argv or shell history);
# it goes into the preseed as a SHA-512 crypt hash.
#
# Target disk: the preseed picks the eMMC (mmcblk) when the box has one, else
# the smallest disk — exactly right for a NAS: the small soldered disk is the
# system, the big ones are data. EVERYTHING ON THAT DISK IS ERASED. The other
# disks are not touched: they are configured (or restored) later in the panel.
#
# Wi-Fi is deliberately not configured here: a NAS installs on a cable; Wi-Fi
# (as a fallback) is set up later in the panel's Network tab.
set -euo pipefail

USER_NAME="nas"
HOST_NAME="nas"
REF="main"
TZ_VAL=""
OUT=""
MIRROR_ISO="https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"

while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="$2"; shift 2 ;;
    --host) HOST_NAME="$2"; shift 2 ;;
    --ref)  REF="$2"; shift 2 ;;
    --tz)   TZ_VAL="$2"; shift 2 ;;
    --out)  OUT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
command -v xorriso >/dev/null || {
  echo "xorriso is missing."
  echo "  macOS:  brew install xorriso"
  echo "  Linux:  sudo apt install xorriso"
  exit 1
}
if [ -z "$TZ_VAL" ]; then
  # macOS has no timedatectl; /etc/localtime is a symlink into the zoneinfo tree there
  TZ_VAL="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  [ -n "$TZ_VAL" ] || TZ_VAL="$(readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||')"
  [ -n "$TZ_VAL" ] || TZ_VAL="Etc/UTC"
fi

# --- portability shims (BSD vs GNU userland) ---------------------------------
sha256_check() {   # sha256_check <expected-hex> <file>
  if command -v sha256sum >/dev/null; then
    echo "$1  $2" | sha256sum -c - >/dev/null
  else
    echo "$1  $2" | shasum -a 256 -c - >/dev/null
  fi
}
sed_i() {          # in-place sed that works on both seds
  if sed --version >/dev/null 2>&1; then sed -i "$@"; else
    local expr="$1"; shift; sed -i '' "$expr" "$@"; fi
}
crypt_sha512() {   # portable SHA-512 crypt: openssl 3 has -6; LibreSSL (macOS) may not
  local out
  out="$(openssl passwd -6 "$1" 2>/dev/null || true)"
  case "$out" in '$6$'*) printf '%s' "$out"; return 0 ;; esac
  out="$(python3 - "$1" <<'PY' 2>/dev/null || true
import sys
try:
    import crypt
    print(crypt.crypt(sys.argv[1], crypt.mksalt(crypt.METHOD_SHA512)))
except Exception:
    pass
PY
)"
  case "$out" in '$6$'*) printf '%s' "$out"; return 0 ;; esac
  echo "" ; return 1
}

printf 'Password for user %s (panel/SSH login on the new box): ' "$USER_NAME"
read -rs PW1; echo
printf 'Repeat: '; read -rs PW2; echo
[ "$PW1" = "$PW2" ] || { echo "passwords differ"; exit 1; }
[ -n "$PW1" ] || { echo "empty password"; exit 1; }
HASH="$(crypt_sha512 "$PW1")" || {
  echo "could not produce a SHA-512 crypt hash."
  echo "  macOS fix:  brew install openssl@3   and re-run as:"
  echo "  PATH=\"\$(brew --prefix openssl@3)/bin:\$PATH\" $0 …"
  exit 1
}

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# --- fetch the current netinst -----------------------------------------------
ISO_NAME="$(curl -fsS "$MIRROR_ISO/" | grep -o 'debian-[0-9.]*-amd64-netinst\.iso' | head -1)"
[ -n "$ISO_NAME" ] || { echo "could not resolve the current netinst name"; exit 1; }
echo "▸ downloading $ISO_NAME…"
curl -fL --progress-bar -o src.iso "$MIRROR_ISO/$ISO_NAME"
echo "▸ verifying checksum…"
EXPECTED="$(curl -fsS "$MIRROR_ISO/SHA256SUMS" | grep " $ISO_NAME\$" | cut -d' ' -f1)"
sha256_check "$EXPECTED" src.iso && echo "  checksum OK"

# --- unpack ------------------------------------------------------------------
mkdir iso
xorriso -osirrox on -indev src.iso -extract / iso >/dev/null 2>&1
chmod -R u+w iso

# The netinst is itself an isohybrid image: its first 432 bytes ARE the MBR
# template isolinux ships as isohdpfx.bin. Taking it from the source ISO frees
# the build from the isolinux package — which macOS does not have at all.
dd if=src.iso of=mbr.bin bs=1 count=432 2>/dev/null

# --- the answers file --------------------------------------------------------
cat > iso/preseed.cfg <<PRESEED
# NAS-OS automated install (generated by make-installer-iso.sh)
d-i debian-installer/locale string en_US.UTF-8
d-i keyboard-configuration/xkb-keymap select us
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string ${HOST_NAME}
d-i netcfg/get_domain string local
d-i netcfg/hostname string ${HOST_NAME}
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
# root stays locked — the user below gets sudo
d-i passwd/root-login boolean false
d-i passwd/user-fullname string ${USER_NAME}
d-i passwd/username string ${USER_NAME}
d-i passwd/user-password-crypted password ${HASH}
d-i clock-setup/utc boolean true
d-i time/zone string ${TZ_VAL}
d-i clock-setup/ntp boolean true
# Target disk: prefer the eMMC (mmcblk) — on a NAS the small soldered disk is the
# system and the big ones are data; otherwise take the SMALLEST disk for the same
# reason. list-devices lists real disks only (not the installer USB stick).
d-i partman/early_command string \\
    DISK="\$(list-devices disk | grep -m1 mmcblk || true)"; \\
    if [ -z "\$DISK" ]; then DISK="\$(for d in \$(list-devices disk); do echo "\$(blockdev --getsize64 \$d) \$d"; done | sort -n | head -1 | cut -d' ' -f2)"; fi; \\
    debconf-set partman-auto/disk "\$DISK"; \\
    debconf-set grub-installer/bootdev "\$DISK"
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-efi/non_efi_system boolean true
# no desktop: standard system + ssh, everything else comes with NAS-OS
tasksel tasksel/first multiselect standard, ssh-server
d-i pkgsel/include string curl ca-certificates
popularity-contest popularity-contest/participate boolean false
d-i grub-installer/only_debian boolean true
d-i finish-install/reboot_in_progress note
# First boot installs NAS-OS itself: a oneshot unit, logged to the journal and
# the console so a plugged-in screen shows what is happening. Done in the booted
# system, not the installer chroot: docker and friends want the real thing.
d-i preseed/late_command string \\
    in-target mkdir -p /etc/nas-os; \\
    in-target sh -c 'echo ${REF} > /etc/nas-os/install-ref'; \\
    in-target sh -c 'cat > /etc/systemd/system/nas-os-firstboot.service <<UNIT
[Unit]
Description=NAS-OS first-boot install
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/opt/nas-os/nas-web.py

[Service]
Type=oneshot
TimeoutStartSec=0
StandardOutput=journal+console
ExecStart=/bin/sh -c "NASOS_REF=\\\$(cat /etc/nas-os/install-ref) NASOS_FORCE_OS=1; export NASOS_REF NASOS_FORCE_OS; curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | bash"

[Install]
WantedBy=multi-user.target
UNIT'; \\
    in-target systemctl enable nas-os-firstboot.service
PRESEED

# --- boot menus: automated entry is the default, short timeout ----------------
# The Beelink boots UEFI (grub); the isolinux menu covers old BIOS boxes.
if [ -f iso/isolinux/txt.cfg ]; then
  sed_i 's/^default .*/default nasos/' iso/isolinux/isolinux.cfg 2>/dev/null || true
  sed_i 's/^timeout .*/timeout 30/' iso/isolinux/isolinux.cfg 2>/dev/null || true
  cat > iso/isolinux/txt.cfg <<'TXT'
default nasos
label nasos
	menu label ^Install NAS-OS (automatic — ERASES the system disk)
	kernel /install.amd/vmlinuz
	append vga=788 initrd=/install.amd/initrd.gz auto=true priority=critical preseed/file=/cdrom/preseed.cfg ---
label install
	menu label ^Manual Debian install
	kernel /install.amd/vmlinuz
	append vga=788 initrd=/install.amd/initrd.gz ---
TXT
fi
if [ -f iso/boot/grub/grub.cfg ]; then
  cat > grub.head <<'GRUB'
set timeout=3
set default=0
menuentry "Install NAS-OS (automatic - ERASES the system disk)" {
    linux /install.amd/vmlinuz auto=true priority=critical preseed/file=/cdrom/preseed.cfg ---
    initrd /install.amd/initrd.gz
}
GRUB
  cat grub.head iso/boot/grub/grub.cfg > grub.new
  mv grub.new iso/boot/grub/grub.cfg
fi

# --- md5 file the installer's self-check reads (python: portable hashing) -----
( cd iso && python3 - <<'PY'
import hashlib, os
with open("md5sum.txt", "w") as out:
    for root, dirs, files in os.walk("."):
        for f in sorted(files):
            p = os.path.join(root, f)
            if p == "./md5sum.txt":
                continue
            h = hashlib.md5()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out.write("%s  %s\n" % (h.hexdigest(), p))
PY
)

# --- rebuild a hybrid BIOS+UEFI ISO ------------------------------------------
OUT="${OUT:-$HOME/nas-os-installer.iso}"
echo "▸ building $OUT…"
# positional parameters as the portable argv array (macOS ships bash 3.2)
set -- -as mkisofs -o "$OUT" \
  -r -V "NASOS-INSTALL" -J -joliet-long \
  -isohybrid-mbr mbr.bin \
  -b isolinux/isolinux.bin -c isolinux/boot.cat \
  -no-emul-boot -boot-load-size 4 -boot-info-table \
  -eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot \
  -isohybrid-gpt-basdat \
  iso
xorriso "$@" >/dev/null 2>&1
ls -lh "$OUT"
echo
echo "✔ Done. Flash it (balenaEtcher / Raspberry Pi Imager → Use custom), boot the box"
echo "  from USB, and wait: Debian installs, reboots, NAS-OS installs itself (ref: ${REF})."
echo "  Then open http://${HOST_NAME}.local — user «${USER_NAME}» with the password you typed."
echo "  ⚠ The AUTOMATIC entry erases the target system disk (eMMC/smallest) without asking."
