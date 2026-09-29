#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_NODE="tests/integration/test_handoff_sla_postgres.py::test_handoff_sla_postgres_smoke"

if [ -z "${POSTGRES_SMOKE_DATABASE_URL:-}" ]; then
    echo "POSTGRES_SMOKE_DATABASE_URL is required for the handoff SLA PostgreSQL gate" >&2
    exit 2
fi

transcript="$(mktemp /tmp/neurostack-handoff-sla-pytest.XXXXXX)" || exit 1
cleanup() {
    case "${transcript:-}" in
        /tmp/neurostack-handoff-sla-pytest.*) rm -f -- "$transcript" ;;
    esac
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

set +e
(
    cd "$ROOT" &&
        poetry run pytest "$SMOKE_NODE" -q -rs
) 2>&1 | tee "$transcript"
pipeline_status=("${PIPESTATUS[@]}")
set -e

if [ "${pipeline_status[0]}" -ne 0 ]; then
    echo "handoff SLA PostgreSQL smoke exited ${pipeline_status[0]}" >&2
    exit 1
fi
if [ "${pipeline_status[1]}" -ne 0 ]; then
    echo "could not capture handoff SLA PostgreSQL smoke evidence" >&2
    exit 1
fi
if grep -Eiq '(^|[ ,=])[1-9][0-9]* (failed|errors?|skipped|xfailed|xpassed)\b' "$transcript"; then
    echo "handoff SLA PostgreSQL smoke contains a forbidden pytest outcome" >&2
    exit 1
fi
summary_count="$(
    grep -Ec '^[= ]*1 passed(, [0-9]+ warnings?)? in [0-9]+([.][0-9]+)?s[= ]*$' \
        "$transcript" || true
)"
if [ "$summary_count" -ne 1 ]; then
    echo "handoff SLA PostgreSQL smoke lacks one complete 1-passed summary" >&2
    exit 1
fi

echo "handoff SLA PostgreSQL smoke evidence valid (1 passed, 0 skipped/xfail)"
