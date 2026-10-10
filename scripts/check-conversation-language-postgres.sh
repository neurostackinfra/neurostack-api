#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POSTGRES_NODES=(
    "tests/integration/test_conversation_language_postgres.py::test_same_session_lock_serializes_and_observes_committed_language"
    "tests/integration/test_conversation_language_postgres.py::test_distinct_sessions_do_not_block"
    "tests/integration/test_conversation_language_postgres.py::test_tenant_scope_does_not_block_or_leak"
    "tests/integration/test_conversation_language_postgres.py::test_webhook_commit_releases_lock_before_finish_response"
)

if [ -z "${POSTGRES_SMOKE_DATABASE_URL:-}" ]; then
    echo "POSTGRES_SMOKE_DATABASE_URL is required for the conversation language PostgreSQL gate" >&2
    exit 2
fi

transcript="$(mktemp /tmp/neurostack-conversation-language-pytest.XXXXXX)" || exit 1
cleanup() {
    case "${transcript:-}" in
        /tmp/neurostack-conversation-language-pytest.*) rm -f -- "$transcript" ;;
    esac
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

set +e
(
    cd "$ROOT" &&
        poetry run pytest "${POSTGRES_NODES[@]}" -q -rs
) 2>&1 | tee "$transcript"
pipeline_status=("${PIPESTATUS[@]}")
set -e

if [ "${pipeline_status[0]}" -ne 0 ]; then
    echo "conversation language PostgreSQL smoke exited ${pipeline_status[0]}" >&2
    exit 1
fi
if [ "${pipeline_status[1]}" -ne 0 ]; then
    echo "could not capture conversation language PostgreSQL evidence" >&2
    exit 1
fi
if grep -Eiq '(^|[ ,=])[1-9][0-9]* (failed|errors?|skipped|xfailed|xpassed)\b' "$transcript"; then
    echo "conversation language PostgreSQL smoke contains a forbidden pytest outcome" >&2
    exit 1
fi
summary_count="$(
    grep -Ec '^[= ]*7 passed(, [0-9]+ warnings?)? in [0-9]+([.][0-9]+)?s[= ]*$' \
        "$transcript" || true
)"
if [ "$summary_count" -ne 1 ]; then
    echo "conversation language PostgreSQL smoke lacks one complete 7-passed summary" >&2
    exit 1
fi

echo "conversation language PostgreSQL evidence valid (7 passed, 0 skipped/xfail)"
