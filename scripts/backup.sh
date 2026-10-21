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

if [[ ! -f docker-compose.yml ]]; then
    error "docker-compose.yml not found; run this script from neurostack-api"
    exit 1
fi
if [[ ! "$RETENTION_DAYS" =~ ^[0-9]+$ ]]; then
    error "RETENTION_DAYS must be a non-negative integer"
    exit 1
fi

# The PostgreSQL container is found with plain `docker`, never through
# `docker compose`: loading a compose file here would load the DEVELOPMENT
# docker-compose.yml (no -f), which refuses to load on a production checkout
# (NEUROSTACK_DEV_STACK), and docker-compose.production.yml needs variables the
# production .env cannot carry. Both files share the Compose project of this
# checkout, so the container is the one running PostgreSQL service of that
# project, identified by the labels Compose puts on it.

# Compose's default project name for this checkout: the directory name,
# lowercased, keeping only [a-z0-9_-], leading '_'/'-' removed (measured with
# `docker compose config`), or COMPOSE_PROJECT_NAME, which Compose accepts only
# when it is already valid. A non-ASCII directory name must set it explicitly.
compose_project_name() {
    local LC_ALL=C
    local directory name
    if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
        name="$COMPOSE_PROJECT_NAME"
        if [[ ! "$name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
            error "COMPOSE_PROJECT_NAME is not a valid Compose project name"
            return 1
        fi
    else
        directory="$(pwd -P)"
        directory="${directory##*/}"
        if [[ "$directory" =~ [^\ -~] ]]; then
            error "The checkout directory name is not ASCII; set COMPOSE_PROJECT_NAME"
            return 1
        fi
        name="${directory,,}"
        name="${name//[^a-z0-9_-]/}"
        while [[ "$name" == [_-]* ]]; do
            name="${name:1}"
        done
        if [[ -z "$name" ]]; then
            error "Cannot derive a Compose project name; set COMPOSE_PROJECT_NAME"
            return 1
        fi
    fi
    printf '%s\n' "$name"
}

# Exactly one running, non one-off container of the PostgreSQL service.
resolve_postgres_container() {
    local project="$1" listing
    local -a containers=()
    if ! listing="$(docker ps --quiet --no-trunc \
        --filter "label=com.docker.compose.project=$project" \
        --filter "label=com.docker.compose.service=$POSTGRES_SERVICE" \
        --filter "label=com.docker.compose.oneoff=False")"; then
        error "docker ps failed; cannot locate the PostgreSQL container"
        return 1
    fi
    if [[ -n "$listing" ]]; then
        mapfile -t containers <<<"$listing"
    fi
    if [[ "${#containers[@]}" -ne 1 ]]; then
        error "Expected exactly one running '$POSTGRES_SERVICE' container in Compose project '$project', found ${#containers[@]}"
        return 1
    fi
    if [[ ! "${containers[0]}" =~ ^[0-9a-f]{12,64}$ ]]; then
        error "docker ps returned an unexpected container id"
        return 1
    fi
    printf '%s\n' "${containers[0]}"
}

if ! compose_project="$(compose_project_name)"; then
    exit 1
fi
if ! postgres_container="$(resolve_postgres_container "$compose_project")"; then
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
if ! docker exec -i "$postgres_container" \
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
