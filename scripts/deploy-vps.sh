#!/bin/bash

set -euo pipefail

# Resolve the checked-out repository independently of the caller's directory and
# PATH. The supported VPS target is Linux, where coreutils installs readlink here.
readonly SCRIPT_PATH="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")"
readonly SCRIPT_DIR="${SCRIPT_PATH%/*}"
readonly REPO_ROOT="${SCRIPT_DIR%/*}"
readonly CHECKER="${SCRIPT_DIR}/validate_provider_availability_evidence.py"
readonly DATABASE_BUDGET_CHECKER="${SCRIPT_DIR}/check_database_connection_budget.py"
readonly DATABASE_BUDGET_MANIFEST="${REPO_ROOT}/config/database-connection-budgets.json"
readonly COMPOSE_FILE="${REPO_ROOT}/docker-compose.production.yml"
readonly DOCKERFILE="${REPO_ROOT}/Dockerfile"
readonly ENV_FILE="${REPO_ROOT}/.env"

# The one Alembic revision this deploy may migrate to. Bumping it is a
# deliberate, owner-approved change, exactly like the production migration
# window pin it mirrors: infra/production/asaas-production-wiring.yaml
# (migrations_033_035), window 035 of the window table that
# scripts/asaas_production_wiring.py reads (scripts/asaas_migration_window.py)
# and the migrations gate in
# docs/runbooks/asaas-deployment.md. Revisions 036 and 037 each need their own
# owner instruction first. tests/scripts/test_production_migration_window.py
# fails if this pin and the window disagree. Never use the head, heads, base or
# a relative step here: the value must be one exact revision id. Before anything
# starts, the pulled image's Alembic head must equal this pin, so an image that
# carries a newer migration is refused instead of running on an older schema.
# This guard protects against an unreviewed pin or image, not against a hostile
# caller environment (for example BASH_ENV or exported shell functions), which
# the operator running the deploy already controls.
readonly APPROVED_ALEMBIC_REVISION="035"

fail() {
    printf 'deploy blocked: %s\n' "$1" >&2
    return 1
}

# Runs before main, so a missing or non-exact pin executes nothing: no release
# checker, no .env read and no Docker command.
require_exact_alembic_revision() {
    case "${APPROVED_ALEMBIC_REVISION,,}" in
        head|heads|base)
            fail "APPROVED_ALEMBIC_REVISION must be one exact revision id, not a symbolic target"
            return 1
            ;;
    esac
    case "$APPROVED_ALEMBIC_REVISION" in
        ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*)
            fail "APPROVED_ALEMBIC_REVISION must be a plain revision id (letters, digits and underscore only)"
            return 1
            ;;
    esac
}

require_exact_alembic_revision

reject_weak_identifier() {
    local name="$1"
    local value="${!name:-}"
    local minimum="$2"
    local normalized="${value,,}"
    if [ "${#value}" -lt "$minimum" ]; then
        fail "$name is missing or shorter than $minimum characters"
        return 1
    fi
    case "$normalized" in
        postgres|neurostack|admin|root|database|db|prod|production|*dev*|*test*|*default*|*changeme*|*change-me*|*example*)
            fail "$name is a repository default or development/example value"
            return 1
            ;;
    esac
}

reject_weak_secret() {
    local name="$1"
    local value="${!name:-}"
    local minimum="$2"
    local normalized="${value,,}"
    local classes=0 unique
    if [ "${#value}" -lt "$minimum" ]; then
        fail "$name is missing or shorter than $minimum characters"
        return 1
    fi
    case "$normalized" in
        postgres|neurostack|password|secret|admin|root|*dev*|*test*|*default*|*changeme*|*change-me*|*example*|*password*)
            fail "$name is a repository default or development/example value"
            return 1
            ;;
    esac
    [[ "$value" =~ [a-z] ]] && classes=$((classes + 1))
    [[ "$value" =~ [A-Z] ]] && classes=$((classes + 1))
    [[ "$value" =~ [0-9] ]] && classes=$((classes + 1))
    [[ "$value" =~ [^a-zA-Z0-9] ]] && classes=$((classes + 1))
    unique="$(printf %s "$value" | /usr/bin/fold -w1 | /usr/bin/sort -u | /usr/bin/wc -l)"
    if [ "$classes" -lt 3 ] || [ "$unique" -lt 10 ]; then
        fail "$name must use at least 3 character classes and 10 distinct characters"
        return 1
    fi
}

load_runtime_environment() {
    [ -f "$ENV_FILE" ] || fail "$ENV_FILE must already exist and is never created by deploy"
    unset ENVIRONMENT DEBUG SECRET_KEY POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB
    unset REDIS_PASSWORD QDRANT_API_KEY ALLOWED_ORIGINS ALLOWED_HOSTS
    unset API_BIND_ADDRESS API_PORT
    unset DATABASE_POOL_SIZE DATABASE_MAX_OVERFLOW DATABASE_POOL_TIMEOUT
    local line key value line_number=0
    local -A seen=()
    while IFS= read -r line || [ -n "$line" ]; do
        line_number=$((line_number + 1))
        [ -n "$line" ] || continue
        [[ "$line" != *$'\r'* ]] || {
            fail ".env line $line_number contains a carriage return"
            return 1
        }
        [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]\$\`\;\|\&\<\>\\]*)$ ]] || {
            fail ".env line $line_number must use data-only KEY=value syntax"
            return 1
        }
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        case "$key" in
            ENVIRONMENT|DEBUG|SECRET_KEY|POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB|REDIS_PASSWORD|QDRANT_API_KEY|ALLOWED_ORIGINS|ALLOWED_HOSTS|API_BIND_ADDRESS|API_PORT|DATABASE_POOL_SIZE|DATABASE_MAX_OVERFLOW|DATABASE_POOL_TIMEOUT) ;;
            *)
                fail ".env line $line_number uses an unknown key"
                return 1
                ;;
        esac
        [ -z "${seen[$key]:-}" ] || {
            fail ".env line $line_number duplicates $key"
            return 1
        }
        seen[$key]=1
        printf -v "$key" '%s' "$value"
        export "$key"
    done < "$ENV_FILE"
}

validate_runtime_environment() {
    load_runtime_environment
    [ "$GEMINI_IMAGE_DIGEST" = "$CHECKER_APPROVED_IMAGE_DIGEST" ] || fail "checker-approved image digest changed unexpectedly"
    [ "${ENVIRONMENT:-production}" = "production" ] || fail "ENVIRONMENT must be production"
    case "${DEBUG:-false}" in false|False|FALSE|0) ;; *) fail "DEBUG must be false" ;; esac
    reject_weak_secret SECRET_KEY 32
    reject_weak_secret POSTGRES_PASSWORD 24
    reject_weak_secret REDIS_PASSWORD 24
    reject_weak_secret QDRANT_API_KEY 32
    reject_weak_identifier POSTGRES_USER 8
    reject_weak_identifier POSTGRES_DB 8
    if [ "$SECRET_KEY" = "$POSTGRES_PASSWORD" ] ||
       [ "$SECRET_KEY" = "$REDIS_PASSWORD" ] ||
       [ "$SECRET_KEY" = "$QDRANT_API_KEY" ] ||
       [ "$POSTGRES_PASSWORD" = "$REDIS_PASSWORD" ] ||
       [ "$POSTGRES_PASSWORD" = "$QDRANT_API_KEY" ] ||
       [ "$REDIS_PASSWORD" = "$QDRANT_API_KEY" ]; then
        fail "production secrets must be distinct"
        return 1
    fi
    [ -n "${ALLOWED_ORIGINS:-}" ] || fail "ALLOWED_ORIGINS is required"
    case "${ALLOWED_ORIGINS,,}" in *'*'*|*localhost*|*127.0.0.1*|*dev*|*test*)
        fail "ALLOWED_ORIGINS must name explicit production origins"
        ;;
    esac
    [ -n "${ALLOWED_HOSTS:-}" ] || fail "ALLOWED_HOSTS is required"
    case "${ALLOWED_HOSTS,,}" in *'*'*|*localhost*|*127.0.0.1*|*dev*|*test*)
        fail "ALLOWED_HOSTS must name explicit production hosts"
        ;;
    esac
}

main() {
    GEMINI_IMAGE_DIGEST="$(/usr/bin/python3 "$CHECKER" --phase release)" || return 1
    export GEMINI_IMAGE_DIGEST
    readonly CHECKER_APPROVED_IMAGE_DIGEST="$GEMINI_IMAGE_DIGEST"

    # Caller and .env values cannot select a binary or remote Docker daemon.
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
    printf 'NeuroStack production deploy: signed release gate passed\n'
    validate_runtime_environment
    /usr/bin/python3 "$DATABASE_BUDGET_CHECKER" \
        --manifest "$DATABASE_BUDGET_MANIFEST" \
        --profile vps-compose \
        --verify-vps-compose \
        --compose "$COMPOSE_FILE" \
        --dockerfile "$DOCKERFILE" \
        --env-file "$ENV_FILE"
    [ -x /usr/bin/docker ] || fail "Docker is required at /usr/bin/docker"
    /usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" version >/dev/null 2>&1 || fail "Docker Compose is required"

    /usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull api
    # Fail closed before anything starts: the pulled image must carry exactly the
    # approved revision as its single Alembic head. An image whose head is newer
    # (036, 037, ...), several heads, an error or no output is refused, because
    # an upgrade to the pin would be a no-op and leave newer code on an older
    # schema.
    local image_heads
    image_heads="$(/usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" run --rm --no-deps -T api alembic heads)" ||
        image_heads="unreadable (alembic heads failed)"
    if [ "$image_heads" != "$APPROVED_ALEMBIC_REVISION (head)" ]; then
        image_heads="${image_heads//[[:cntrl:]]/ }"
        fail "image alembic head '${image_heads:-<none>}' is not the approved revision $APPROVED_ALEMBIC_REVISION; bump APPROVED_ALEMBIC_REVISION only with the owner-approved migration window"
        return 1
    fi
    /usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
    # Guard: this migrates the VPS compose-local Postgres only, never the AWS
    # production database, whose window (033-035) is pinned to an exact revision
    # in docs/runbooks/asaas-deployment.md. It stops at APPROVED_ALEMBIC_REVISION,
    # which the image head check above proved is the image's only head; 036 and
    # 037 each need their own owner instruction and a deliberate pin bump first.
    /usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T api alembic upgrade "$APPROVED_ALEMBIC_REVISION"
    /usr/bin/docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
}

main "$@"
