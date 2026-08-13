# NAS-OS

A lightweight "NAS operating system" on top of Debian on an x86 box: a browser-based desktop
(monitoring, disks/SMART, docker stacks, file manager with previews and a player, terminal,
Pushover notifications) + a step-by-step setup wizard. Backend — Python standard library
(no pip), engine — bash. Runs as root (SMART, docker, mounting, power, PTY).

## Easiest install on x86: a flash-and-forget USB stick

Debian's installer asks twenty questions. This repo ships its own "
Imager": a script that builds a USB image with every answer baked in **plus**
NAS-OS itself. Runs on **macOS** (`brew install xorriso`) or any Linux —
including an existing NAS box:

```bash
./tools/make-installer-iso.sh --user oleg --host nas --ref v2026.08.04.1
```

It asks for a password (that becomes the SSH/panel user), downloads the current
Debian netinst, verifies its checksum, and produces `nas-os-installer.iso`.
Flash it with balenaEtcher ("Use custom"), plug the
stick into the new box with an **ethernet cable**, boot from USB, and walk
away. Debian installs onto the eMMC (or the smallest disk) — the data disks are
untouched — the box reboots, NAS-OS installs itself, and the panel appears on
`http://<hostname>.local`. If the old box's disks are plugged in, the setup
wizard opens with a **"Restore this box"** offer from their settings backup.
The stick is only needed for the install; pull it out afterwards.

## Install on a clean system — one command

```bash
curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | sudo bash
```

The command works on any x86 Debian box. For a box you
depend on, pin the install to a tested release instead of whatever `main` is right now:

```bash
curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | sudo NASOS_REF=v2026.08.04.1 bash
```

The script:
1. clones the project into `/opt/nas-os`;
2. installs the global base (packages, docker, ffmpeg/poppler for previews, directories, preview cache + nightly timer) — this is the wizard's system stage;
3. brings up the `nas-web` service (port 80, root) with autostart;
4. prints the address.

Then open `http://<hostname>.local` and go through the **wizard**: Disks → Pool/Parity → Apps → Access → Security → Tuning → Notifications → Backups (hardware-dependent and optional — not installed automatically).

### Install parameters (env)
- `NASOS_DEST` — directory (default `/opt/nas-os`)
- `NASOS_REF` — tag or branch to install (default `main`); `NASOS_BRANCH` is the older name for the same thing
- `NAS_WEB_PORT` — port (default `80`)

## What lives where
- Code: `/opt/nas-os/` (`nas-web.py`, `nas-wizard.sh`, `web/`, `services/`)
- Config/data (panel-owned): `/var/lib/nas-os` (desktop settings, access, snippets, favorites)
- Preview cache: `/var/cache/nas-thumbs` · docker stacks: `/opt/stacks`

## Update
Re-running `install.sh` pulls the latest version from git and restarts the service.
Edits to `web/*.html` take effect without a restart; edits to `nas-web.py` need `sudo systemctl restart nas-web`.

## Development
- `python3 -m py_compile nas-web.py` · `bash -n nas-wizard.sh` · `shellcheck nas-wizard.sh`
- Desktop JS: `node --check` on the extracted `<script>` (node is only for checking, not a dependency).

> **Rule:** any new global change (package, systemd unit, directory, file, permission) must
> be added to `nas-wizard.sh` (system stage) / `install.sh`, otherwise a from-scratch install breaks.
