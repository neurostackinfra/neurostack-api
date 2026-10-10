#!/usr/bin/env bash

# NeuroStack API - streaming restore of an age-encrypted PostgreSQL backup

set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
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

require_command age
require_command age-keygen
require_command python3
require_command mktemp

if [[ $# -ne 1 ]]; then
    error "Usage: scripts/restore_backup.sh <postgres_*.sql.gz.age>"
    exit 2
fi
backup_file="$1"

if [[ -z "${BACKUP_AGE_IDENTITY:-}" ]]; then
    error "BACKUP_AGE_IDENTITY is required"
    exit 1
fi
if [[ ! -f "$BACKUP_AGE_IDENTITY" ]]; then
    error "BACKUP_AGE_IDENTITY must name a readable identity file"
    exit 1
fi
if ! age-keygen -y -- "$BACKUP_AGE_IDENTITY" >/dev/null 2>&1; then
    error "BACKUP_AGE_IDENTITY is not a valid age identity"
    exit 1
fi
if [[ -z "${RESTORE_DATABASE_URL:-}" ]]; then
    error "RESTORE_DATABASE_URL is required"
    exit 1
fi
if [[ -z "${RESTORE_EXPECTED_DATABASE:-}" ]]; then
    error "RESTORE_EXPECTED_DATABASE is required"
    exit 1
fi
if [[ ! -f "$backup_file" ]]; then
    error "Encrypted backup file was not found"
    exit 1
fi

require_command gzip
require_command psql
require_command rmdir

umask 077
credential_dir=""
pgpass_file=""
cleanup() {
    if [[ -n "$credential_dir" ]]; then
        rm -f -- "$pgpass_file" \
            "$credential_dir/host" "$credential_dir/port" \
            "$credential_dir/user" "$credential_dir/database"
        rmdir -- "$credential_dir" 2>/dev/null || true
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

credential_dir="$(mktemp -d "${TMPDIR:-/tmp}/neurostack-restore.XXXXXX")"
pgpass_file="$credential_dir/pgpass"
if ! CREDENTIAL_DIR="$credential_dir" python3 <<'PY'
import os
import pathlib
import sys
import urllib.parse


def reject_control(value: str, name: str) -> str:
    if not value or any(ord(character) < 32 for character in value):
        raise ValueError(f"invalid {name}")
    return value


try:
    url = urllib.parse.urlsplit(os.environ["RESTORE_DATABASE_URL"])
    if url.scheme not in {"postgres", "postgresql"} or url.query or url.fragment:
        raise ValueError
    host = reject_control(url.hostname or "", "host")
    user = reject_control(urllib.parse.unquote(url.username or ""), "user")
    database = reject_control(
        urllib.parse.unquote(url.path.removeprefix("/")), "database"
    )
    if "/" in database:
        raise ValueError
    password = urllib.parse.unquote(url.password or "")
    if any(ord(character) < 32 for character in password):
        raise ValueError
    port = str(url.port or 5432)
    destination = pathlib.Path(os.environ["CREDENTIAL_DIR"])
    for name, value in {
        "host": host,
        "port": port,
        "user": user,
        "database": database,
    }.items():
        (destination / name).write_text(value + "\n", encoding="utf-8")
    escape = lambda value: value.replace("\\", "\\\\").replace(":", "\\:")
    (destination / "pgpass").write_text(
        ":".join(map(escape, (host, port, database, user, password))) + "\n",
        encoding="utf-8",
    )
except Exception:
    sys.exit(1)
PY
then
    error "RESTORE_DATABASE_URL is invalid"
    exit 1
fi
chmod 600 "$pgpass_file"
unset PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE
unset PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME PGCONNECT_TIMEOUT
unset PGSSLMODE PGREQUIRESSL PGSSLCOMPRESSION PGSSLCERT PGSSLKEY
unset PGSSLROOTCERT PGSSLCRL PGSSLCRLDIR PGSSLSNI PGREQUIREPEER
unset PGCHANNELBINDING PGTARGETSESSIONATTRS PGGSSENCMODE PGKRBSRVNAME
unset PGGSSLIB PGSYSCONFDIR PGLOCALEDIR PGCLIENTENCODING PGREQUIREAUTH
unset PGSSLNEGOTIATION PGLOADBALANCEHOSTS PGREALM
IFS= read -r PGHOST <"$credential_dir/host"
IFS= read -r PGPORT <"$credential_dir/port"
IFS= read -r PGUSER <"$credential_dir/user"
IFS= read -r PGDATABASE <"$credential_dir/database"
export PGHOST PGPORT PGUSER PGDATABASE PGPASSFILE="$pgpass_file"
unset RESTORE_DATABASE_URL

if ! connected_database="$(printf 'SELECT current_database();\n' \
    | psql -X --set=ON_ERROR_STOP=1 --tuples-only --no-align 2>/dev/null)"; then
    error "Unable to verify restore target database"
    exit 1
fi
if [[ "$connected_database" != "$RESTORE_EXPECTED_DATABASE" \
    || "$PGDATABASE" != "$RESTORE_EXPECTED_DATABASE" ]]; then
    error "Restore target database does not match RESTORE_EXPECTED_DATABASE"
    exit 1
fi
case "$connected_database" in
    postgres|template0|template1)
        error "Refusing restore into a PostgreSQL system database"
        exit 1
        ;;
esac

info "Starting encrypted PostgreSQL restore"
if ! age --decrypt --identity "$BACKUP_AGE_IDENTITY" -- "$backup_file" \
    2>/dev/null \
    | gzip -dc \
    | psql -X --set=ON_ERROR_STOP=1 \
        --single-transaction >/dev/null 2>&1; then
    error "Encrypted PostgreSQL restore pipeline failed"
    exit 1
fi
info "Encrypted PostgreSQL restore completed successfully"
