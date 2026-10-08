#!/usr/bin/env bash

# NeuroStack API - encrypted PostgreSQL backup

set -Eeuo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/backups/neurostack}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-neurostack}"
POSTGRES_DB="${POSTGRES_DB:-neurostack}"

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "Required command is unavailable: $1"
        exit 1
    fi
}

# Encryption prerequisites are intentionally checked before Docker or pg_dump.
require_command age
require_command age-keygen
if [[ -z "${BACKUP_AGE_RECIPIENT:-}" ]]; then
    error "BACKUP_AGE_RECIPIENT is required"
    exit 1
fi
if ! printf '' | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" \
    >/dev/null 2>&1; then
    error "BACKUP_AGE_RECIPIENT is not a valid age recipient"
    exit 1
fi

require_command docker
require_command gzip
require_command find
require_command mktemp
require_command ln

if ! docker compose version >/dev/null 2>&1; then
    error "Docker Compose is unavailable"
    exit 1
fi
if [[ ! -f docker-compose.yml ]]; then
    error "docker-compose.yml not found; run this script from neurostack-api"
    exit 1
fi
if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    error "RETENTION_DAYS must be a non-negative integer"
    exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%d_%H%M%S)"
backup_file="$BACKUP_DIR/postgres_${timestamp}.sql.gz.age"
if [[ -e "$backup_file" ]]; then
    error "Backup destination already exists"
    exit 1
fi

temp_file=""
cleanup() {
    if [[ -n "$temp_file" && -e "$temp_file" ]]; then
        rm -f -- "$temp_file"
    fi
}
handle_signal() {
    local signal_status="$1"
    trap - HUP INT TERM
    cleanup
    exit "$signal_status"
}
trap cleanup EXIT
trap 'handle_signal 129' HUP
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM

temp_file="$(mktemp "$BACKUP_DIR/.postgres_${timestamp}.sql.gz.age.tmp.XXXXXX")"
chmod 600 "$temp_file"

info "Starting encrypted PostgreSQL backup"
if ! docker compose exec -T "$POSTGRES_SERVICE" \
    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" 2>/dev/null \
    | gzip -c \
    | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" \
        >"$temp_file" 2>/dev/null; then
    error "Encrypted PostgreSQL backup pipeline failed"
    exit 1
fi

if command -v sync >/dev/null 2>&1; then
    sync -f "$temp_file"
fi
if ! ln -- "$temp_file" "$backup_file"; then
    error "Backup destination already exists; encrypted recovery point preserved"
    exit 1
fi
rm -f -- "$temp_file"
temp_file=""

info "Encrypted PostgreSQL backup published"
info "Removing encrypted PostgreSQL backups older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name 'postgres_*.sql.gz.age' -mtime "+$RETENTION_DAYS" -delete

info "Backup completed successfully"
