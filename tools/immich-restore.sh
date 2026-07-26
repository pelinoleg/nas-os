#!/bin/bash
# Bring Immich up on THIS box from the Mirror backup of another NAS.
#
# The backup holds two halves: the media library (rsync copy) and a nightly `pg_dump` of the
# database. Neither is enough alone, and the dump does not load as it stands — it is a single
# database dump, so it carries no roles and stops at the first «OWNER TO <role>» with
# ERROR: role "..." does not exist, rolling everything back. That one missing line is not
# something anyone recalls in an emergency, which is why this script exists.
#
#   sudo tools/immich-restore.sh --library /mnt/storage/immich          # real restore
#   sudo tools/immich-restore.sh --rehearse                             # prove it works, change nothing
#
# The rehearsal runs the whole path — stack, database, dump, web UI — against a throwaway
# library and stack, then removes everything it made. Run it after a big change; it is the
# only honest answer to «would this actually come up».
set -euo pipefail

BACKUP_DEFAULT="/media/nas/t7-4TB/Ugreen-Backup"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RECIPE="$HERE/services/immich/docker-compose.yml"

BACKUP="$BACKUP_DEFAULT"; LIBRARY=""; DUMP=""; STACK="/opt/stacks/immich"
DBDIR=""; PORT="2283"; REHEARSE=0; FORCE=0; YES=0

die(){ printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say(){ printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok(){  printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn(){ printf '\033[33m  ! %s\033[0m\n' "$*"; }

usage(){ sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --backup)  BACKUP="$2"; shift 2;;
    --library) LIBRARY="$2"; shift 2;;
    --dump)    DUMP="$2"; shift 2;;
    --stack)   STACK="$2"; shift 2;;
    --db-dir)  DBDIR="$2"; shift 2;;
    --port)    PORT="$2"; shift 2;;
    --rehearse) REHEARSE=1; shift;;
    --force)   FORCE=1; shift;;
    --yes|-y)  YES=1; shift;;
    -h|--help) usage;;
    *) die "unknown option: $1 (try --help)";;
  esac
done

[ "$(id -u)" = 0 ] || die "run this with sudo — it creates a stack and talks to docker"
command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is missing"
[ -f "$RECIPE" ] || die "recipe not found: $RECIPE"

# ---------------------------------------------------------------- the backup
[ -d "$BACKUP" ] || die "backup folder not found: $BACKUP (pass --backup)"
LIBSRC="$BACKUP/home/Photos Immich"
[ -d "$LIBSRC" ] || die "no Immich library inside the backup: $LIBSRC"

if [ -z "$DUMP" ]; then
  DUMP="$(ls -1t "$LIBSRC"/backups/*.sql.gz 2>/dev/null | head -1 || true)"
fi
[ -n "$DUMP" ] && [ -f "$DUMP" ] || die "no database dump found under $LIBSRC/backups"
gzip -t "$DUMP" 2>/dev/null || die "the dump is not a readable gzip: $DUMP"

AGE_DAYS=$(( ( $(date +%s) - $(stat -c %Y "$DUMP") ) / 86400 ))
say "Dump:    $(basename "$DUMP")  ($(du -h "$DUMP" | cut -f1), ${AGE_DAYS}d old)"
[ "$AGE_DAYS" -gt 3 ] && warn "that dump is ${AGE_DAYS} days old — the source may have stopped writing them"

# the source names its dumps immich-db-backup-<stamp>-v<version>-pg<pgver>.sql.gz
VERSION="$(basename "$DUMP" | sed -n 's/.*-\(v[0-9][0-9.]*\)-pg.*/\1/p')"
[ -n "$VERSION" ] || VERSION="$(sed -n 's/.*immich-server:\${IMMICH_VERSION:-\([^}]*\)}.*/\1/p' "$RECIPE" | head -1)"
[ -n "$VERSION" ] || die "could not tell which Immich version this dump came from — pass it in the .env by hand"
say "Version: $VERSION (taken from the dump's own name — the restored database only fits its own version)"
say "Library: $LIBSRC  ($(du -sh "$LIBSRC" 2>/dev/null | cut -f1))"

# ------------------------------------------------------- rehearsal or real
if [ "$REHEARSE" = 1 ]; then
  SCRATCH="$(mktemp -d /var/tmp/immich-rehearsal-XXXXXX)"
  STACK="$SCRATCH/stack"; LIBRARY="$SCRATCH/library"; DBDIR="$SCRATCH/db"; PORT="${PORT}"
  mkdir -p "$LIBRARY"
  # Immich checks its own marker files in every media folder and KILLS the microservices
  # worker when one is missing («Failed to read .../.immich»), restarting for ever. A real
  # restore has them — rsync copied them with the library — so the rehearsal must too, or it
  # would fail for a reason no real restore has. (Found by running this, not by reading docs.)
  for d in upload library thumbs profile encoded-video backups; do
    mkdir -p "$LIBRARY/$d"
    if [ -f "$LIBSRC/$d/.immich" ]; then cp "$LIBSRC/$d/.immich" "$LIBRARY/$d/.immich"
    else : > "$LIBRARY/$d/.immich"; fi
  done
  say "Rehearsal — everything goes into $SCRATCH and is removed at the end"
else
  [ -n "$LIBRARY" ] || die "say where the media library should live: --library <path>
   The backup copy is at: $LIBSRC
   Pointing Immich straight at it makes the BACKUP the live library — it will be written to,
   and the next Mirror run will see the changes. That may be the right call in a real disaster,
   but it must be a decision, not an accident. Either pass that path deliberately, or copy the
   library somewhere first and pass the copy."
  if [ "$LIBRARY" = "$LIBSRC" ]; then
    warn "you pointed Immich AT THE BACKUP COPY — it becomes the live library from now on"
    [ "$YES" = 1 ] || { read -r -p "  type «yes» to accept that: " a; [ "$a" = yes ] || die "stopped"; }
  fi
  # Immich refuses to run without its marker files and just restarts in a loop, with the
  # reason buried in the container log — check before spending twenty minutes on a restore.
  MISSING=""
  for d in upload library thumbs profile encoded-video backups; do
    [ -f "$LIBRARY/$d/.immich" ] || MISSING="$MISSING $d"
  done
  if [ -n "$MISSING" ]; then
    warn "this library has no Immich marker files in:$MISSING"
    warn "Immich will start and immediately restart for ever unless they are there."
    if [ -d "$LIBSRC/upload" ] && [ "$FORCE" != 1 ]; then
      read -r -p "  copy the markers from the backup copy now? [Y/n] " a
      [ "${a:-y}" = n ] || for d in $MISSING; do
        mkdir -p "$LIBRARY/$d"
        [ -f "$LIBSRC/$d/.immich" ] && cp "$LIBSRC/$d/.immich" "$LIBRARY/$d/.immich" || : > "$LIBRARY/$d/.immich"
        ok "marker restored: $d"
      done
    fi
  fi
fi
[ -n "$DBDIR" ] || DBDIR="$STACK/postgres"

if [ -e "$STACK" ] && [ "$FORCE" != 1 ]; then
  die "$STACK already exists — remove it first, or pass --force"
fi

FREE_GB=$(( $(stat -f -c '%a*%S' "$(dirname "$DBDIR")" 2>/dev/null || echo 0) / 1073741824 ))
[ "$FREE_GB" -ge 15 ] || die "only ${FREE_GB} GB free where the database would live ($DBDIR) — needs ~15 GB"

# ------------------------------------------------------------ build the stack
say "Creating the stack in $STACK"
mkdir -p "$STACK" "$DBDIR" "$LIBRARY"
cp "$RECIPE" "$STACK/compose.yaml"
DB_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
cat > "$STACK/.env" <<EOF
# written by tools/immich-restore.sh — a restore of the dump named below
UPLOAD_LOCATION=$LIBRARY
DB_DATA_LOCATION=$DBDIR
DB_PASSWORD=$DB_PASSWORD
DB_USERNAME=postgres
DB_DATABASE_NAME=immich
IMMICH_VERSION=$VERSION
TZ=$(cat /etc/timezone 2>/dev/null || echo UTC)
EOF
chmod 600 "$STACK/.env"
ok "compose + .env written (Immich $VERSION, library $LIBRARY)"

cd "$STACK"
COMPOSE=(docker compose --project-directory "$STACK" -f "$STACK/compose.yaml")
if [ "$REHEARSE" = 1 ]; then COMPOSE+=(-p "immich-rehearsal"); fi

REHEARSAL_FAILED=0
cleanup(){
  if [ "$REHEARSE" = 1 ]; then
    say "Tearing the rehearsal down"
    "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true
    rm -rf "$SCRATCH"
    ok "removed $SCRATCH — this box is exactly as it was"
    [ "${REHEARSAL_FAILED:-0}" = 1 ] && exit 1
  fi
  return 0
}
trap cleanup EXIT

# ------------------------------------------------------------- database first
say "Starting Postgres and waiting for it to really answer"
"${COMPOSE[@]}" up -d database >/dev/null
DB_CID=""
for _ in $(seq 1 60); do
  DB_CID="$("${COMPOSE[@]}" ps -q database 2>/dev/null || true)"
  [ -n "$DB_CID" ] && break
  sleep 2
done
[ -n "$DB_CID" ] || die "the database container never started — see: ${COMPOSE[*]} logs database"

# pg_isready lies during initdb: the entrypoint runs a temporary server, stops it, then starts
# the real one — so wait for a query to succeed several times in a row, not for the socket.
GOOD=0
for _ in $(seq 1 150); do
  if docker exec "$DB_CID" psql -U postgres -d postgres -c 'select 1' >/dev/null 2>&1; then
    GOOD=$((GOOD+1)); [ "$GOOD" -ge 3 ] && break
  else GOOD=0; fi
  sleep 2
done
[ "$GOOD" -ge 3 ] || die "Postgres never came up — see: ${COMPOSE[*]} logs database"
ok "Postgres is up ($(docker exec "$DB_CID" psql -U postgres -tAc 'show server_version'))"

# --------------------------------------------------------------- roles first
say "Creating the roles the dump expects (this is the step that bites)"
ROLES="$(gunzip -c "$DUMP" | grep -oE 'OWNER TO [A-Za-z0-9_]+' | awk '{print $3}' | sort -u | grep -v '^postgres$' || true)"
if [ -z "$ROLES" ]; then
  ok "the dump owns everything as postgres — no extra roles needed"
else
  for r in $ROLES; do
    docker exec "$DB_CID" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
      -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$r') THEN CREATE ROLE $r LOGIN; END IF; END \$\$;" >/dev/null
    ok "role $r"
  done
fi

# ------------------------------------------------------------- load the dump
say "Loading the dump — 400 MB compressed takes several minutes on a Pi"
T0=$(date +%s)
if ! gunzip -c "$DUMP" | docker exec -i "$DB_CID" \
      psql -U postgres -d immich -v ON_ERROR_STOP=1 -q > /var/tmp/immich-restore-psql.log 2>&1; then
  tail -20 /var/tmp/immich-restore-psql.log >&2
  die "the dump did not load (log: /var/tmp/immich-restore-psql.log)"
fi
ok "loaded in $(( $(date +%s) - T0 ))s"

ASSETS="$(docker exec "$DB_CID" psql -U postgres -d immich -tAc 'select count(*) from asset' 2>/dev/null \
        || docker exec "$DB_CID" psql -U postgres -d immich -tAc 'select count(*) from assets' 2>/dev/null || echo '?')"
USERS="$(docker exec "$DB_CID" psql -U postgres -d immich -tAc 'select count(*) from "user"' 2>/dev/null \
        || docker exec "$DB_CID" psql -U postgres -d immich -tAc 'select count(*) from users' 2>/dev/null || echo '?')"
ok "database holds $ASSETS assets for $USERS user(s)"

# ------------------------------------------------------------ the rest of it
say "Starting Immich itself"
"${COMPOSE[@]}" up -d >/dev/null
UP=0
for _ in $(seq 1 90); do
  if curl -fsS -m 5 "http://127.0.0.1:$PORT/api/server/ping" >/dev/null 2>&1 \
  || curl -fsS -m 5 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then UP=1; break; fi
  sleep 4
done
if [ "$UP" = 1 ]; then ok "Immich answers on http://$(hostname -I | awk '{print $1}'):$PORT"
else warn "Immich did not answer within 6 minutes — see: ${COMPOSE[*]} logs immich-server"; fi

if [ "$REHEARSE" = 1 ]; then
  if [ "$UP" = 1 ]; then
    printf '\n\033[32m✓ REHEARSAL PASSED\033[0m — the dump loads (%s assets) and Immich answers.\n' "$ASSETS"
    printf '  For a real restore run the same command with --library <where the photos go>.\n'
  else
    printf '\n\033[31m✗ REHEARSAL INCOMPLETE\033[0m — the dump loaded (%s assets) but Immich never answered.\n' "$ASSETS"
    printf '  The database half is proven; the stack is not. Look at the log line above before trusting this.\n'
  fi
  printf '  Left behind on purpose: the Immich images (~4.7 GB) — that is what makes a real\n'
  printf '  restore fast. Remove them with: docker image rm ghcr.io/immich-app/immich-server:%s \\\n' "$VERSION"
  printf '    ghcr.io/immich-app/immich-machine-learning:%s\n\n' "$VERSION"
  [ "$UP" = 1 ] || REHEARSAL_FAILED=1
else
  printf '\n\033[32m✓ RESTORED\033[0m — Immich %s, %s assets, library %s\n' "$VERSION" "$ASSETS" "$LIBRARY"
  cat <<TXT

  What to check next:
   · log in with the SAME account as on the old box — the users came with the dump
   · the library must be readable by the container (it runs as root here)
   · if the photos are still only in the backup copy, either move them to $LIBRARY
     or re-point UPLOAD_LOCATION in $STACK/.env and restart the stack
   · this box's own Mirror profile still backs up the OLD source — it will keep
     copying from a NAS that no longer exists until you change it

TXT
fi
