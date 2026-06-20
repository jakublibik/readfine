#!/usr/bin/env bash
#
# Readfine database backup — logical pg_dump shipped to an off-site restic repo.
#
# Runs `pg_dump` inside the running Postgres container (so the dump tool always
# matches the server version), then stores it in a restic repository. restic
# handles encryption, compression, deduplication and retention. The default
# config targets Cloudflare R2 (S3-compatible), but any restic backend works.
#
# Designed to run unattended from cron. See the "Backups" section in README.md
# for setup (R2 bucket/token, restic install, cron entry) and restore steps.
#
# Usage:  ./backup.sh                 # uses ./backup.env
#         BACKUP_ENV_FILE=/path ./backup.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"; }
die() { echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ERROR: $*" >&2; exit 1; }

# ── Config ────────────────────────────────────────────────────────────────────
# Secrets and repo location come from an env file kept OUT of git (see
# backup.env.example). It must export: RESTIC_REPOSITORY, RESTIC_PASSWORD,
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (R2 token), AWS_DEFAULT_REGION=auto.
BACKUP_ENV_FILE="${BACKUP_ENV_FILE:-$SCRIPT_DIR/backup.env}"
[ -f "$BACKUP_ENV_FILE" ] || die "config not found: $BACKUP_ENV_FILE (copy backup.env.example)"
# shellcheck disable=SC1090
set -a; . "$BACKUP_ENV_FILE"; set +a

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in $BACKUP_ENV_FILE}"
: "${RESTIC_PASSWORD:?set RESTIC_PASSWORD in $BACKUP_ENV_FILE}"

COMPOSE_DIR="${COMPOSE_DIR:-$SCRIPT_DIR}"
DB_SERVICE="${DB_SERVICE:-db}"

# DB user/name default to the app's .env — the same file docker-compose reads via
# ${DB_USER:-readfine} — so the backup matches the deployment without duplicating
# config. backup.env can still override them; if neither is set, fall back to readfine.
APP_ENV="${APP_ENV:-$COMPOSE_DIR/.env}"
if [ -f "$APP_ENV" ]; then
  [ -z "${DB_USER:-}" ] && DB_USER="$(grep -E '^DB_USER=' "$APP_ENV" | tail -n1 | cut -d= -f2-)"
  [ -z "${DB_NAME:-}" ] && DB_NAME="$(grep -E '^DB_NAME=' "$APP_ENV" | tail -n1 | cut -d= -f2-)"
fi
DB_USER="${DB_USER:-readfine}"
DB_NAME="${DB_NAME:-readfine}"
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"   # optional dead-man's-switch ping on success

command -v restic >/dev/null 2>&1 || die "restic not installed"
command -v docker >/dev/null 2>&1 || die "docker not installed"

# ── Temp workspace (always cleaned up) ────────────────────────────────────────
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DUMP="$WORK/readfine.sql"   # verified on disk, then streamed into restic by name

# ── 1. Initialize the restic repo on first run ────────────────────────────────
if ! restic cat config >/dev/null 2>&1; then
  log "restic repo not initialized — running restic init"
  restic init
fi

# ── 2. Dump the database to a temp file, then verify it ───────────────────────
log "dumping database '$DB_NAME' from container service '$DB_SERVICE'"
if ! docker compose -f "$COMPOSE_DIR/docker-compose.yml" exec -T "$DB_SERVICE" \
      pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner > "$DUMP"; then
  die "pg_dump failed — nothing was uploaded"
fi
[ -s "$DUMP" ] || die "pg_dump produced an empty file — nothing was uploaded"
log "dump OK ($(du -h "$DUMP" | cut -f1))"

# ── 3. Back up to restic (encrypted + deduplicated off-site) ──────────────────
# Stream the (already verified) dump via stdin so every snapshot stores it under
# a stable path "/readfine.sql" — not the random mktemp dir — which keeps restore
# (`restic dump latest /readfine.sql`) predictable.
log "uploading to restic repo"
restic backup --tag readfine --stdin --stdin-filename readfine.sql < "$DUMP"

# ── 4. Apply retention and prune unreferenced data ────────────────────────────
log "applying retention (daily=$KEEP_DAILY weekly=$KEEP_WEEKLY monthly=$KEEP_MONTHLY)"
restic forget --tag readfine \
  --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
  --prune

# ── 5. Done ───────────────────────────────────────────────────────────────────
log "backup complete"
[ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 10 "$HEALTHCHECK_URL" >/dev/null 2>&1 || true
