#!/usr/bin/env bash
# Credit integration gate (neurostack-credit-system): run the given pytest
# paths and accept ONLY a run in which every collected test passed.
#
#   bash scripts/check-credit-integration.sh <test path or node id>...
#
# The credit integration tests skip when the env vars of their services are
# missing, so full_suite_gate and `make quality` stay green without them; this
# gate is the one place that refuses any skip.  Callers guard the env vars
# they need first (": "${POSTGRES_SMOKE_DATABASE_URL:?...}"").  Always invoke
# it with bash: the repository runs with core.filemode=false.
#
# Refused (exit 1): a given path or node id that selects no test on its own
# (checked first, with --collect-only, so an emptied module cannot hide
# behind the others); a non-zero pytest exit (failure, error, interruption,
# usage error, an empty selection); any non-zero count of failed, error(s),
# skipped, xfailed, xpassed or deselected in the transcript; a transcript
# without exactly one complete "N passed" summary; and an N that is not the
# sum of the per-path collected counts (a run cut short -- pytest.exit with
# returncode 0, a crashed worker -- passes fewer tests than it collected).
# Usage errors (no argument, an option instead of a path, the same path
# twice) exit 2 before pytest runs.  Paths must not overlap (a directory and
# a file inside it): the collected counts would add up twice and the run is
# refused, never silently accepted.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$#" -eq 0 ]; then
    echo "usage: bash scripts/check-credit-integration.sh <test path or node id>..." >&2
    exit 2
fi
declare -A seen=()
for argument in "$@"; do
    case "$argument" in
        "" | -*)
            echo "credit integration gate takes test paths or node ids only, not '$argument'" >&2
            exit 2
            ;;
    esac
    if [ -n "${seen[$argument]:-}" ]; then
        echo "credit integration gate: '$argument' is given twice" >&2
        exit 2
    fi
    seen[$argument]=1
done

transcript="$(mktemp /tmp/neurostack-credit-integration-pytest.XXXXXX)" || exit 1
collected="$(mktemp /tmp/neurostack-credit-integration-pytest.XXXXXX)" || {
    rm -f -- "$transcript"
    exit 1
}
cleanup() {
    local file
    for file in "${transcript:-}" "${collected:-}"; do
        case "$file" in
            /tmp/neurostack-credit-integration-pytest.*) rm -f -- "$file" ;;
        esac
    done
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

expected=0
for argument in "$@"; do
    (
        cd "$ROOT" &&
            poetry run pytest --collect-only -q -p no:cacheprovider "$argument"
    ) >"$collected" 2>&1
    collect_status=$?
    if [ "$collect_status" -ne 0 ]; then
        cat -- "$collected" >&2
        echo "credit integration gate: '$argument' selects no test (collection exited $collect_status)" >&2
        exit 1
    fi
    # "N tests collected" (or "N/M tests collected (K deselected)": N selected).
    count="$(grep -Eo '[0-9]+(/[0-9]+)? tests? collected' "$collected" | tail -n 1 | grep -Eo '^[0-9]+')"
    if [ -z "$count" ] || [ "$count" -eq 0 ]; then
        cat -- "$collected" >&2
        echo "credit integration gate: could not read how many tests '$argument' collects" >&2
        exit 1
    fi
    expected=$((expected + count))
done

set +e
(
    cd "$ROOT" &&
        poetry run pytest -q -rs -p no:cacheprovider "$@"
) 2>&1 | tee "$transcript"
pipeline_status=("${PIPESTATUS[@]}")
set -e

if [ "${pipeline_status[0]}" -ne 0 ]; then
    echo "credit integration pytest exited ${pipeline_status[0]}" >&2
    exit 1
fi
if [ "${pipeline_status[1]}" -ne 0 ]; then
    echo "could not capture credit integration evidence" >&2
    exit 1
fi
if grep -Eiq '(^|[ ,=])[1-9][0-9]* (failed|errors?|skipped|xfailed|xpassed|deselected)\b' "$transcript"; then
    echo "credit integration run contains a forbidden pytest outcome" >&2
    exit 1
fi
summary_pattern='^[= ]*[1-9][0-9]* passed(, [0-9]+ warnings?)? in [0-9]+([.][0-9]+)?s( [(][0-9]+:[0-9]{2}:[0-9]{2}[)])?[= ]*$'
summary_count="$(grep -Ec "$summary_pattern" "$transcript" || true)"
if [ "$summary_count" -ne 1 ]; then
    echo "credit integration run lacks one complete N-passed summary" >&2
    exit 1
fi
passed="$(grep -E "$summary_pattern" "$transcript" | grep -Eo '[1-9][0-9]* passed' | grep -Eo '^[0-9]+')"
if [ "$passed" -ne "$expected" ]; then
    echo "credit integration run passed $passed of the $expected tests its paths collect" >&2
    exit 1
fi

echo "credit integration evidence valid (${passed} passed of ${expected} collected, 0 skipped/xfail/deselected)"
