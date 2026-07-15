# NAS-OS

A lightweight "NAS operating system" on top of Raspberry Pi OS Lite: a browser-based desktop
(monitoring, disks/SMART, docker stacks, file manager with previews and a player, terminal,
Pushover notifications) + a step-by-step setup wizard. Backend — Python standard library
(no pip), engine — bash. Runs as root (SMART, docker, mounting, power, PTY).

## Install on a clean system — one command

```bash
curl -fsSL https://raw.githubusercontent.com/pelinoleg/nas-os/main/install.sh | sudo bash
```

The script:
1. clones the project into `/opt/nas-os`;
2. installs the global base (packages, docker, ffmpeg/poppler for previews, directories, preview cache + nightly timer) — this is the wizard's system stage;
3. brings up the `nas-web` service (port 80, root) with autostart;
4. prints the address.

Then open `http://<hostname>.local` and go through the **wizard**: Disks → Pool/Parity → Apps → Access → Security → Tuning → Notifications → Backups (hardware-dependent and optional — not installed automatically).

### Install parameters (env)
- `NASOS_DEST` — directory (default `/opt/nas-os`)
- `NASOS_BRANCH` — branch (default `main`)
- `NAS_WEB_PORT` — port (default `80`)

## What lives where
- Code: `/opt/nas-os/` (`nas-web.py`, `nas-wizard.sh`, `web/`, `services/`)
- Config/data (per-user): `~/nas-config` (desktop settings, access, snippets, favorites)
- Preview cache: `/var/cache/nas-thumbs` · docker stacks: `/opt/stacks`

## Update
Re-running `install.sh` pulls the latest version from git and restarts the service.
Edits to `web/*.html` take effect without a restart; edits to `nas-web.py` need `sudo systemctl restart nas-web`.

## Development
- `python3 -m py_compile nas-web.py` · `bash -n nas-wizard.sh` · `shellcheck nas-wizard.sh`
- Desktop JS: `node --check` on the extracted `<script>` (node is only for checking, not a dependency).

> **Rule:** any new global change (package, systemd unit, directory, file, permission) must
> be added to `nas-wizard.sh` (system stage) / `install.sh`, otherwise a from-scratch install breaks.
