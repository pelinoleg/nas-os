# services/ — NAS docker stacks (live next to nas-wizard.sh)

Each service is a separate subfolder with a `docker-compose.yml` file
(or `docker-compose.yaml` / `compose.yml` / `compose.yaml`).

`nas-wizard.sh --stage docker` scans this folder, shows a checklist of
"which ones to bring up", and runs `docker compose up -d` for the selected ones (idempotent).
`~/nas-config/scripts/deploy.sh` brings everything up at once.

## Conventions (by spec)

- **Pinned image tags**, NOT `latest` — to avoid catching surprise updates.
  Tag policy (2026-07-20, all recipes converted): prefer the upstream **rolling
  major** (`dockge:1`, `dozzle:v10`, `wud:8`) — fixes arrive, breaking majors
  don't; when upstream publishes no rolling tag (LSIO images, myspeed,
  nextexplorer, immich-kiosk) — pin the **exact version**; for 0.x projects pin
  the **minor** (`zerobyte:v0.28`). WUD flags newer versions either way; refresh
  the pins when testing recipes on each new Debian stable.
- `restart: unless-stopped` for every service.
- Container configs → `/opt/docker/<service>/...`
- Large data (media, documents) → `/mnt/storage/<service>/...` (mergerfs pool).
- Secrets — in an `.env` file next to the compose file (do NOT commit to git; add to .gitignore).

## Add your own service

```bash
mkdir -p services/immich
$EDITOR services/immich/docker-compose.yml
sudo ./nas-wizard.sh --stage docker      # will find it and offer to bring it up
```

## What's here (ready-made templates, pinned tags)

| Service | Port | Purpose |
|--------|------|-----------|
| `dockge/` | 5001 | Docker-compose stack manager (web UI) |
| `dozzle/` | 8083 | Real-time container logs |
| `scrutiny/` | 8084 | Disk SMART monitoring (specify disks in `devices:`) |
| `syncthing/` | 8384 | File synchronization |
| `nextexplorer/` | 3000 | Web file manager (set `SESSION_SECRET`!) |
| `example-service/…example` | — | Blank template (rename to `docker-compose.yml`) |

Before the first launch, check in the templates: `SESSION_SECRET` (NextExplorer),
the disk list (Scrutiny), `PUBLIC_URL`/Pi address. Dozzle and Scrutiny ports are kept apart
(both default to 8080 → 8083 and 8084).
