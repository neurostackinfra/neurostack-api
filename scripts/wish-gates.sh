#!/usr/bin/env bash
# Shared validation gates for the neurostack-core extraction wish.

_WISH_GATES_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_WISH_BASELINES="$_WISH_GATES_ROOT/tests/baselines"
_WISH_OUTCOME_FLOOR="$_WISH_BASELINES/neurostack-core-extraction-outcome-floor.txt"
_WISH_PASS_FLOOR="$_WISH_BASELINES/neurostack-core-extraction-pass-floor.txt"
_WISH_FAILURES="$_WISH_BASELINES/neurostack-core-extraction-known-failures.txt"
_WISH_SHA="$_WISH_BASELINES/neurostack-core-extraction-branch.txt"
_WISH_API="$_WISH_BASELINES/neurostack-core-extraction-public-api.json"

_pytest_gate() (
    local mode="$1"
    shift
    local transcript
    transcript="$(mktemp /tmp/neurostack-${mode}-pytest.XXXXXX)" || return 1
    [ -n "$transcript" ] || return 1
    _pytest_cleanup() {
        case "${transcript:-}" in
            /tmp/neurostack-*-pytest.*) rm -f -- "$transcript" ;;
        esac
    }
    trap _pytest_cleanup EXIT
    trap '_pytest_cleanup; exit 130' HUP INT TERM
    local pytest_status=0
    if (
        cd "$_WISH_GATES_ROOT" && poetry run pytest -q -p no:cacheprovider "$@"
    ) 2>&1 | tee "$transcript"; then
        pytest_status=${PIPESTATUS[0]}
    else
        pytest_status=${PIPESTATUS[0]}
    fi
    if { [ "$mode" = focused ] && [ "$pytest_status" -ne 0 ]; } ||
        { [ "$mode" = full ] && [ "$pytest_status" -ne 0 ] && [ "$pytest_status" -ne 1 ]; }; then
        echo "pytest exited $pytest_status; refusing an incomplete run" >&2
        return 1
    fi
    local floor="$_WISH_OUTCOME_FLOOR"
    [ "$mode" = focused ] && floor="$_WISH_PASS_FLOOR"
    poetry run python "$_WISH_GATES_ROOT/scripts/validate-pytest-completion.py" \
        --output "$transcript" --floor "$floor" --failures "$_WISH_FAILURES" \
        --mode "$mode"
    local validation_status=$?
    return "$validation_status"
)

core_suite_gate() {
    _pytest_gate focused tests/srm tests/flow tests/policy tests/prompt
}

inventory_gate() (
    cd "$_WISH_GATES_ROOT" || return 1
    poetry run python scripts/pt-br-inventory.py --max 0 "$@"
)

preset_loader_gate() (
    cd "$_WISH_GATES_ROOT" || return 1
    poetry run pytest -q -p no:cacheprovider tests/presets/test_preset_loader.py
)

pt_br_preset_unchanged() (
    cd "$_WISH_GATES_ROOT" || return 1
    sha256sum --check tests/fixtures/presets/pt-br-preset.sha256
)

parity_tests_unchanged() (
    cd "$_WISH_GATES_ROOT" || return 1
    local base
    base="$(git merge-base HEAD origin/main)" || return 1
    # Named, versioned exceptions — exactly two files owned by the
    # ``handoff-contextual-resumption`` wish Group 3 from 2026-10-02:
    #
    #   * tests/flow/test_flow_manager.py — hosts the rendered-prompt A4
    #     test and the FlowManager episode-boundary / cross-worker tests
    #     (wish deliverable 4 / A4). The gate itself still forbids any
    #     ``tests/prompt`` path, so the A4 test lives here, next to the
    #     FlowManager suite that the same deliverable touches.
    #   * tests/srm/test_handoff_context_injection.py — new adversarial
    #     suite (wish deliverable 8 / A6). ``tests/srm/test_prompt_
    #     injection.py`` keeps its ``TestPromptConfigInjection`` scope
    #     unchanged — the gate still rejects any diff to that file or any
    #     other file under the four protected directories.
    #
    # No other file under tests/srm, tests/flow, tests/policy or
    # tests/prompt may change (added, modified or untracked). Expanding
    # this list requires editing this gate explicitly (wish deliverable 9,
    # AC B2).
    local allowed='tests/flow/test_flow_manager.py tests/srm/test_handoff_context_injection.py'
    local candidate
    local offending=0
    while IFS= read -r candidate; do
        [ -z "$candidate" ] && continue
        local matched=0
        for allowed_path in $allowed; do
            if [ "$candidate" = "$allowed_path" ]; then
                matched=1
                break
            fi
        done
        if [ "$matched" -ne 1 ]; then
            echo "parity-oracle test modified outside the exception list: $candidate" >&2
            offending=1
        fi
    done < <(
        {
            git diff --name-only "$base" -- tests/srm tests/flow tests/policy tests/prompt
            git ls-files --others --exclude-standard -- \
                tests/srm tests/flow tests/policy tests/prompt
        } | sort -u
    )
    if [ "$offending" -ne 0 ]; then
        return 1
    fi
)

full_suite_gate() {
    _pytest_gate full tests
}

_typed_marker_gate() (
    set -eu
    local temporary
    temporary="$(mktemp -d /tmp/neurostack-typed.XXXXXX)"
    trap 'case "$temporary" in /tmp/neurostack-typed.*) rm -rf -- "$temporary" ;; esac' EXIT
    cp -R "$_WISH_GATES_ROOT/packages/neurostack-core" "$temporary/package"
    printf 'from neurostack_core import enable_metrics\nenable_metrics.no_such_attribute()\n' \
        > "$temporary/consumer.py"
    python3 -m venv "$temporary/venv"

    rm "$temporary/package/neurostack_core/py.typed"
    poetry run python -m build --wheel --outdir "$temporary/untyped" "$temporary/package" >/dev/null
    "$temporary/venv/bin/pip" install --no-deps "$temporary"/untyped/*.whl >/dev/null
    if ! poetry run mypy --no-incremental \
        --python-executable "$temporary/venv/bin/python" "$temporary/consumer.py" \
        >"$temporary/untyped.out" 2>&1; then
        echo "py.typed probe did not become untyped when the marker was removed" >&2
        cat "$temporary/untyped.out" >&2
        return 1
    fi

    : > "$temporary/package/neurostack_core/py.typed"
    poetry run python -m build --wheel --outdir "$temporary/typed" "$temporary/package" >/dev/null
    "$temporary/venv/bin/pip" install --force-reinstall --no-deps \
        "$temporary"/typed/*.whl >/dev/null
    if poetry run mypy --no-incremental \
        --python-executable "$temporary/venv/bin/python" "$temporary/consumer.py" \
        >"$temporary/typed.out" 2>&1; then
        echo "py.typed probe stayed untyped with the marker installed" >&2
        return 1
    fi
    grep -q 'no_such_attribute' "$temporary/typed.out"
)

# Public-API coverage scope — honest statement (wish
# ``handoff-contextual-resumption`` Group 4 deliverable 7 / A8b):
#
# ``static_gate`` extracts the public API tree by walking the six
# ``CONSUMERS`` files hard-coded in ``scripts/extract-branch-public-api.py``
# (``src/api/routes/policies.py``, ``src/core/pipeline.py``,
# ``src/memory/__init__.py``, ``src/memory/models.py``,
# ``src/services/business_hours.py``, ``src/services/trace_adapter.py``)
# and resolving the symbols they import from the four ``CORE_PREFIXES``
# (``src.flow``, ``src.policy``, ``src.prompt``, ``src.srm``) under a
# frozen ``git archive <sha>`` tree, then ``cmp``'ing the resulting JSON
# against ``tests/baselines/neurostack-core-extraction-public-api.json``.
#
# This gate therefore proves facade stability for the subset of symbols
# those six consumers actually import. It does NOT re-check the full
# surface of ``packages/neurostack-core/neurostack_core/flow/manager.py``
# (or any other package module) beyond the specific symbols the
# consumers reach through ``src.flow``/``src.policy``/``src.prompt``/
# ``src.srm``. Changes to ``flow/manager.py`` internals that no consumer
# re-exports — e.g. new private helpers, altered cache mechanics — are
# intentionally outside this gate's scope and are gated instead by
# ``core_suite_gate`` / ``full_suite_gate``.
static_gate() (
    local generated archive_tree
    generated="$(mktemp /tmp/neurostack-public-api.XXXXXX.json)" || return 1
    [ -n "$generated" ] || return 1
    archive_tree="$(mktemp -d /tmp/neurostack-public-api-tree.XXXXXX)" || {
        case "$generated" in /tmp/neurostack-public-api.*.json) rm -f -- "$generated" ;; esac
        return 1
    }
    [ -n "$archive_tree" ] || return 1
    _static_cleanup() {
        case "${generated:-}" in
            /tmp/neurostack-public-api.*.json) rm -f -- "$generated" ;;
        esac
        case "${archive_tree:-}" in
            /tmp/neurostack-public-api-tree.*)
                [ -d "$archive_tree" ] && rm -rf -- "$archive_tree"
                ;;
        esac
    }
    trap _static_cleanup EXIT
    trap '_static_cleanup; exit 130' HUP INT TERM
    local sha status=0
    sha="$(tr -d '[:space:]' < "$_WISH_SHA")"
    (
        cd "$_WISH_GATES_ROOT" &&
        git archive "$sha" | tar -x -C "$archive_tree" &&
        poetry run python scripts/check-package-boundary.py packages/neurostack-core &&
        poetry run black --check src tests packages &&
        poetry run isort --check-only src tests packages &&
        poetry run flake8 src tests packages &&
        poetry run mypy src packages/neurostack-core/neurostack_core &&
        _typed_marker_gate &&
        poetry run python scripts/extract-branch-public-api.py \
            --tree "$archive_tree" --output "$generated" &&
        cmp --silent "$generated" "$_WISH_API"
    ) || status=$?
    return "$status"
)

_copy_install_project() {
    local destination="$1"
    mkdir -p "$destination/packages"
    cp "$_WISH_GATES_ROOT/pyproject.toml" "$_WISH_GATES_ROOT/poetry.lock" \
        "$_WISH_GATES_ROOT/README.md" "$destination/"
    cp -R "$_WISH_GATES_ROOT/packages/neurostack-core" "$destination/packages/"
}

_origin_check() {
    local python="$1" source="$2" cwd="$3"
    (
        cd "$cwd" &&
        env -u PYTHONPATH PYTHONTZPATH="" "$python" \
            "$_WISH_GATES_ROOT/scripts/assert-neurostack-core-origin.py" \
            --mode editable --expected-source "$source"
    )
}

_install_poetry_main() {
    local workspace="$_INSTALL_TEMP_ROOT/poetry-main"
    _copy_install_project "$workspace/project"
    env -u VIRTUAL_ENV -u CONDA_PREFIX -u POETRY_ACTIVE \
        POETRY_VIRTUALENVS_CREATE=true POETRY_VIRTUALENVS_PATH="$workspace/venvs" \
        poetry -C "$workspace/project" \
        install --only main --no-root --no-interaction --no-ansi || return
    local python
    python="$(env -u VIRTUAL_ENV -u CONDA_PREFIX -u POETRY_ACTIVE \
        POETRY_VIRTUALENVS_CREATE=true POETRY_VIRTUALENVS_PATH="$workspace/venvs" \
        poetry -C "$workspace/project" env info --executable)" || return
    local prefix
    prefix="$("$python" -c 'import sys; print(sys.prefix)')" || return
    case "$prefix" in
        "$workspace/venvs"/*) ;;
        *) echo "Poetry interpreter escaped temporary root: $python (sys.prefix=$prefix)" >&2; return 1 ;;
    esac
    _origin_check "$python" "$workspace/project/packages/neurostack-core" "$workspace"
}

_install_poetry_dev() {
    local workspace="$_INSTALL_TEMP_ROOT/poetry-dev"
    _copy_install_project "$workspace/project"
    env -u VIRTUAL_ENV -u CONDA_PREFIX -u POETRY_ACTIVE \
        POETRY_VIRTUALENVS_CREATE=true POETRY_VIRTUALENVS_PATH="$workspace/venvs" \
        poetry -C "$workspace/project" \
        install --no-root --no-interaction --no-ansi || return
    local python
    python="$(env -u VIRTUAL_ENV -u CONDA_PREFIX -u POETRY_ACTIVE \
        POETRY_VIRTUALENVS_CREATE=true POETRY_VIRTUALENVS_PATH="$workspace/venvs" \
        poetry -C "$workspace/project" env info --executable)" || return
    local prefix
    prefix="$("$python" -c 'import sys; print(sys.prefix)')" || return
    case "$prefix" in
        "$workspace/venvs"/*) ;;
        *) echo "Poetry interpreter escaped temporary root: $python (sys.prefix=$prefix)" >&2; return 1 ;;
    esac
    "$python" -m build "$workspace/project/packages/neurostack-core" || return
    _origin_check "$python" "$workspace/project/packages/neurostack-core" "$workspace"
}

_copy_pip_project() {
    local destination="$1"
    mkdir -p "$destination/packages"
    cp "$_WISH_GATES_ROOT/requirements.txt" "$_WISH_GATES_ROOT/requirements-dev.txt" "$destination/"
    cp -R "$_WISH_GATES_ROOT/packages/neurostack-core" "$destination/packages/"
}

_install_pip_main() {
    local workspace="$_INSTALL_TEMP_ROOT/pip-main"
    _copy_pip_project "$workspace/project"
    python3 -m venv "$workspace/venv" || return
    (cd "$workspace/project" && "$workspace/venv/bin/pip" install -r requirements.txt) || return
    _origin_check "$workspace/venv/bin/python" "$workspace/project/packages/neurostack-core" "$workspace"
}

_install_pip_dev() {
    local workspace="$_INSTALL_TEMP_ROOT/pip-dev"
    _copy_pip_project "$workspace/project"
    python3 -m venv "$workspace/venv" || return
    (cd "$workspace/project" && "$workspace/venv/bin/pip" install -r requirements-dev.txt) || return
    "$workspace/venv/bin/python" -m build "$workspace/project/packages/neurostack-core" || return
    _origin_check "$workspace/venv/bin/python" "$workspace/project/packages/neurostack-core" "$workspace"
}

_install_compose() {
    docker compose -p "$_INSTALL_COMPOSE_PROJECT" -f "$_WISH_GATES_ROOT/docker-compose.yml" \
        run --build --rm --no-deps -e PYTHONTZPATH="" api python - \
        --mode editable --expected-source /app/packages/neurostack-core \
        < "$_WISH_GATES_ROOT/scripts/assert-neurostack-core-origin.py"
}

_install_production_image() {
    docker build --target production -t "$_INSTALL_IMAGE" "$_WISH_GATES_ROOT" || return
    docker run --rm -i -e PYTHONTZPATH="" "$_INSTALL_IMAGE" python - \
        --mode editable --expected-source /app/packages/neurostack-core \
        < "$_WISH_GATES_ROOT/scripts/assert-neurostack-core-origin.py"
}

_run_install_row() {
    local label="$1" function_name="$2"
    echo "==> install surface: $label"
    if "$function_name"; then
        echo "PASS $label"
        return 0
    fi
    echo "FAIL $label" >&2
    return 1
}

install_surface_gate() (
    local phase
    if [ "${1:-}" = "--phase" ] && [ "$#" -eq 2 ]; then
        phase="$2"
    else
        echo "usage: install_surface_gate --phase foundation|moved|artifact" >&2
        return 2
    fi
    case "$phase" in foundation|moved|artifact) ;; *)
        echo "invalid install phase: $phase" >&2
        return 2
    esac

    _INSTALL_TEMP_ROOT="$(mktemp -d /tmp/neurostack-install.XXXXXX)" || return 1
    local token
    token="$(printf '%s-%s-%s' "$phase" "$$" "$RANDOM" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-')"
    _INSTALL_COMPOSE_PROJECT="neurostack-core-$token"
    _INSTALL_IMAGE="neurostack-core-$token:production"
    export _INSTALL_TEMP_ROOT _INSTALL_COMPOSE_PROJECT _INSTALL_IMAGE

    _install_cleanup() {
        if [ -n "${_INSTALL_COMPOSE_PROJECT:-}" ]; then
            docker compose -p "$_INSTALL_COMPOSE_PROJECT" \
                -f "$_WISH_GATES_ROOT/docker-compose.yml" down --volumes \
                --remove-orphans --rmi local >/dev/null 2>&1 || true
        fi
        if [ -n "${_INSTALL_IMAGE:-}" ]; then
            docker image rm "$_INSTALL_IMAGE" >/dev/null 2>&1 || true
        fi
        case "${_INSTALL_TEMP_ROOT:-}" in
            /tmp/neurostack-install.*)
                [ -d "$_INSTALL_TEMP_ROOT" ] && rm -rf -- "$_INSTALL_TEMP_ROOT"
                ;;
        esac
    }
    trap _install_cleanup EXIT
    trap '_install_cleanup; exit 130' HUP INT TERM

    local status=0
    _run_install_row "poetry-main" _install_poetry_main || status=1
    _run_install_row "poetry-dev" _install_poetry_dev || status=1
    _run_install_row "pip-main" _install_pip_main || status=1
    _run_install_row "pip-dev" _install_pip_dev || status=1
    _run_install_row "compose-api" _install_compose || status=1
    _run_install_row "production-image" _install_production_image || status=1
    return "$status"
)

# Renders the merged dev + production Compose files with the synthetic inputs
# in tests/fixtures/compose/production-synthetic.env (the same set used by
# tests/scripts/test_production_compose.py). The caller's environment is
# dropped with `env -i` and `--env-file` replaces the project `.env`, so no
# real secret is read; a missing Docker CLI fails, it is never skipped. Every
# step carries an explicit status: bash ignores `set -e` inside a function
# called from an `||`, `if` or `!` context.
_production_compose_render() (
    set -euo pipefail
    local fake_env="$_WISH_GATES_ROOT/tests/fixtures/compose/production-synthetic.env"
    local line name
    local -a fake_vars=() passthrough=("PATH=$PATH")
    [ -f "$fake_env" ] || {
        echo "missing synthetic compose inputs: $fake_env" >&2
        return 1
    }
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in '' | '#'*) continue ;; esac
        fake_vars+=("$line")
    done <"$fake_env"
    # Only what the Docker CLI needs to locate itself and its daemon/context.
    for name in HOME DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_CERT_PATH \
        DOCKER_TLS_VERIFY XDG_RUNTIME_DIR; do
        if [ -n "${!name:-}" ]; then
            passthrough+=("$name=${!name}")
        fi
    done
    cd "$_WISH_GATES_ROOT" || return 1
    env -i "${passthrough[@]}" "${fake_vars[@]}" \
        docker compose --env-file "$fake_env" \
        -f docker-compose.yml -f docker-compose.production.yml config --quiet || {
        echo "production Compose render failed" >&2
        return 1
    }
)

# Secret/PII rules for the release-candidate scans, one `name<TAB>ERE` per
# line. POSIX classes replace `\s` and `[$]` replaces `\$` so one expression
# means the same to `git grep -E` and to awk. Split literals keep this scanner
# from matching its own source. Findings are printed as `path:line: rule`
# only: the matched text and the line content are never printed, because
# gate transcripts are captured as evidence.
_asaas_scan_rules() {
    local forbidden_cpf='cpf'"Cnpj"
    local forbidden_card='credit'"Card"
    local payment_security_term='c'"vv"
    printf '%s\t%s\n' \
        asaas_api_key '[$]aact_(prod|hmlg)_[A-Za-z0-9]+' \
        asaas_access_token_header 'asaas-access-token[[:space:]]*[:=][[:space:]]*[^$<{]' \
        bearer_token 'Bearer [A-Za-z0-9._-]+' \
        forbidden_cpf_field "$forbidden_cpf" \
        forbidden_card_field "$forbidden_card" \
        payment_security_term "$payment_security_term"
}

# Reads stdin and prints `path:line: rule` per hit; exits 3 when anything
# matched. ASAAS_SCAN_MODE=file numbers the input lines; ASAAS_SCAN_MODE=diff
# reads a single-file `git diff --unified=0` and scans only `+` lines inside
# hunks, numbered from each `@@ ... +start` header. With --unified=0 every body
# line starts with `+`, `-` or `\`, so the header block before the first `@@`
# is the only place a `+++ ` header can appear: an added line whose text starts
# with `++ ` is content, never a file switch.
_ASAAS_SCAN_AWK='
BEGIN {
    count = split(ENVIRON["ASAAS_SCAN_RULES"], specs, "\n")
    for (i = 1; i <= count; i++) {
        tab = index(specs[i], "\t")
        if (tab == 0) continue
        rules++
        rule_name[rules] = substr(specs[i], 1, tab - 1)
        rule_re[rules] = substr(specs[i], tab + 1)
    }
    if (rules == 0) { print "no scan rules" > "/dev/stderr"; exit 2 }
    path = ENVIRON["ASAAS_SCAN_PATH"]
    mode = ENVIRON["ASAAS_SCAN_MODE"]
    if (mode != "file" && mode != "diff") { print "bad scan mode" > "/dev/stderr"; exit 2 }
}
function check(text, number,    j) {
    for (j = 1; j <= rules; j++) {
        if (text ~ rule_re[j]) { print path ":" number ": " rule_name[j]; hits++ }
    }
}
mode == "file" { check($0, NR); next }
/^@@ / {
    header = $0
    if (!sub(/^@@ -[0-9]+(,[0-9]+)? \+/, "", header)) { print "bad hunk header" > "/dev/stderr"; exit 2 }
    number = header + 0
    in_hunk = 1
    next
}
!in_hunk { next }
/^\+/ { check(substr($0, 2), number); number++; next }
/^ / { number++; next }
END { if (hits > 0) exit 3 }
'

# Fatal scan: lines added since BASE_REF in tracked files (committed, staged or
# unstaged) plus every untracked, non-ignored regular file under the roots.
# Paths come from NUL-separated listings and each file is diffed on its own, so
# no patch header is parsed for a path. Renames are detected explicitly (`-M`,
# whatever `diff.renames` says), so a moved file contributes only the lines it
# gained and moving a pre-existing fixture stays review-only.
# `diff.noprefix`/`mnemonicPrefix` are pinned so a user config cannot change
# the patch shape. Files git treats as
# binary (a NUL byte, or a `binary`/`-diff` attribute) are listed by path and
# not scanned. Untracked symlinks and other non-regular entries are listed by
# path and never followed, so nothing outside the repository is read.
# Returns 0 clean, 1 on a finding, 2 when the scan itself failed.
_asaas_added_line_scan() {
    local base_ref="$1" work="$2" rules="$3"
    shift 3
    local record added rest path old_path rc status=0
    local -a git_diff=(
        git -c diff.noprefix=false -c diff.mnemonicPrefix=false -c core.quotepath=false
        diff --no-ext-diff --no-textconv --no-color --find-renames
        --src-prefix=a/ --dst-prefix=b/
    )
    local -a pathspecs
    "${git_diff[@]}" --numstat -z "$base_ref" -- "$@" >"$work/numstat" || {
        echo "git diff --numstat failed" >&2
        return 2
    }
    while IFS= read -r -d '' record; do
        added="${record%%$'\t'*}"
        rest="${record#*$'\t'}"
        path="${rest#*$'\t'}"
        # -z --numstat writes a rename as `added<TAB>deleted<TAB><NUL>old<NUL>new<NUL>`.
        if [ -z "$path" ]; then
            IFS= read -r -d '' old_path && IFS= read -r -d '' path || {
                echo "truncated rename record in git diff --numstat" >&2
                return 2
            }
            pathspecs=(":(literal)$old_path" ":(literal)$path")
        else
            pathspecs=(":(literal)$path")
        fi
        case "$added" in
            -) echo "skipped binary path (not scanned): $path"; continue ;;
            0) continue ;;
        esac
        "${git_diff[@]}" --unified=0 "$base_ref" -- "${pathspecs[@]}" >"$work/patch" || {
            echo "git diff failed for a changed path" >&2
            return 2
        }
        rc=0
        ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_MODE=diff ASAAS_SCAN_PATH="$path" \
            LC_ALL=C awk "$_ASAAS_SCAN_AWK" <"$work/patch" || rc=$?
        case "$rc" in
            0) ;;
            3) status=1 ;;
            *) echo "added-line scan failed with exit $rc" >&2; return 2 ;;
        esac
    done <"$work/numstat"

    git ls-files -z --others --exclude-standard -- "$@" >"$work/untracked" || {
        echo "git ls-files failed" >&2
        return 2
    }
    while IFS= read -r -d '' path; do
        if [ -L "$path" ] || [ ! -f "$path" ]; then
            echo "skipped non-regular untracked path: $path"
            continue
        fi
        if [ -s "$path" ] && ! LC_ALL=C grep -qI '' -- "$path"; then
            echo "skipped binary path (not scanned): $path"
            continue
        fi
        rc=0
        ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_MODE=file ASAAS_SCAN_PATH="$path" \
            LC_ALL=C awk "$_ASAAS_SCAN_AWK" <"$path" || rc=$?
        case "$rc" in
            0) ;;
            3) status=1 ;;
            *) echo "untracked-file scan failed with exit $rc" >&2; return 2 ;;
        esac
    done <"$work/untracked"
    return "$status"
}

# Review-only scan of the whole tree under the roots, through `git grep
# --untracked`: tracked files plus untracked, non-ignored ones. Ignored files
# are never read, symlinks are not followed and binary files are skipped.
# Returns 0 with findings, 1 without, 2 when git grep failed.
_asaas_baseline_scan() {
    local work="$1" rules="$2"
    shift 2
    local name expression path line _content rc found=1
    while IFS=$'\t' read -r name expression; do
        [ -n "$name" ] || continue
        rc=0
        LC_ALL=C git grep --untracked -I -n -z -E -e "$expression" -- "$@" \
            >"$work/grep" || rc=$?
        case "$rc" in
            0) found=0 ;;
            1) continue ;;
            *) echo "baseline git grep failed with exit $rc" >&2; return 2 ;;
        esac
        while IFS= read -r -d '' path && IFS= read -r -d '' line &&
            IFS= read -r _content; do
            echo "  $path:$line: $name"
        done <"$work/grep"
    done <<<"$rules"
    return "$found"
}

# ASAAS release-candidate aggregate (asaas-payment-gateway Group 6). Local
# only: Compose is validated without starting services, and no provider call,
# database migration, deploy, rollout transition or secret-manager operation is
# invoked. BASE_REF names the branch point explicitly instead of guessing it,
# and POSTGRES_SMOKE_DATABASE_URL is required so the PostgreSQL suites cannot
# pass by skipping. The Docker CLI is required (the Compose render fails, it is
# not skipped, without it); no production secret is needed or read, because
# the render uses the synthetic inputs in
# tests/fixtures/compose/production-synthetic.env. Every step fails through an
# explicit `|| return 1`, so the gate is non-zero whether it is called with
# `&&`, `||`, `if` or `!` (bash ignores `set -e` in the last three). Usage:
#   BASE_REF=origin/main POSTGRES_SMOKE_DATABASE_URL=... \
#     bash -c 'source scripts/wish-gates.sh && asaas_release_candidate_gate'
#
# Known legacy references, removed by later phases of asaas-payment-gateway
# Group 10 and not by this gate: tests/integration/test_asaas_billing_postgres.py
# and tests/integration/test_asaas_subscription_triggers_postgres.py still
# exercise the legacy migration tables and roster triggers with raw SQL until
# those database objects are dropped. The migration saga service, its admin
# routes and operator client were removed in Group 10 phase E2;
# docker-compose.production.yml no longer requires ABACATEPAY_WEBHOOK_SECRET
# (phase 1) nor ASAAS_PROVIDER_EVIDENCE_* (phase E2).
asaas_release_candidate_gate() (
    set -euo pipefail
    local root="$_WISH_GATES_ROOT"
    local base_ref="${BASE_REF:?set BASE_REF to the branch point, e.g. origin/main}"
    : "${POSTGRES_SMOKE_DATABASE_URL:?set POSTGRES_SMOKE_DATABASE_URL to a disposable loopback PostgreSQL}"
    local rules work scan_status
    # The same git pathspecs for the review-only full-tree scan and for the
    # fatal added-lines scan; the glob is quoted so git, not bash, expands it.
    local -a scan_roots=(
        src tests scripts config docs 'docker-compose*.yml' .env.example env.example
        frontend/artifacts/neurostack/src
    )

    cd "$root" || return 1
    command -v docker >/dev/null 2>&1 || {
        echo "docker CLI unavailable: the production Compose render is required, not optional" >&2
        return 1
    }
    git rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null || {
        echo "BASE_REF does not name a commit: $base_ref" >&2
        return 1
    }
    poetry run pytest -p no:cacheprovider \
        tests/contracts/test_billing_fact_producer_consumer.py \
        tests/scripts/test_deploy_vps_gate.py \
        tests/scripts/test_production_compose.py \
        tests/scripts/test_asaas_observability.py \
        tests/scripts/test_validate_conventional_commits.py -v || {
        echo "release-candidate contract suites failed" >&2
        return 1
    }
    poetry run pytest -p no:cacheprovider -rs \
        tests/integration/test_asaas_billing_postgres.py \
        tests/integration/test_asaas_checkout_service_postgres.py \
        tests/integration/test_asaas_webhook_postgres.py \
        tests/integration/test_asaas_subscription_triggers_postgres.py \
        tests/integration/test_billing_producer_handoff_postgres.py -v || {
        echo "release-candidate PostgreSQL suites failed" >&2
        return 1
    }
    _production_compose_render || return 1

    rules="$(_asaas_scan_rules)" || return 1
    work="$(mktemp -d /tmp/neurostack-asaas-scan.XXXXXX)" || return 1
    [ -n "$work" ] || return 1
    # shellcheck disable=SC2064 # expand now: $work is local to this subshell
    trap "case '$work' in /tmp/neurostack-asaas-scan.*) rm -rf -- '$work' ;; esac" EXIT

    echo "secret/PII scan: rules $(printf '%s' "$rules" | cut -f1 | paste -sd, -);" \
        "findings print path:line: rule, never the matched text;" \
        "files git treats as binary (a NUL byte) are not scanned"

    # Pre-existing findings stay visible so maintainers can tell fixtures that
    # predate this candidate from new ones. They never fail the gate: only the
    # added-lines scan below does.
    scan_status=0
    _asaas_baseline_scan "$work" "$rules" "${scan_roots[@]}" \
        >"$work/baseline" || scan_status=$?
    case "$scan_status" in
        0) echo "baseline secret/PII findings (review required):"; cat -- "$work/baseline" ;;
        1) echo "baseline secret/PII scan: no findings" ;;
        *) echo "baseline secret/PII scan failed" >&2; return 1 ;;
    esac

    scan_status=0
    _asaas_added_line_scan "$base_ref" "$work" "$rules" "${scan_roots[@]}" || scan_status=$?
    case "$scan_status" in
        0) echo "added-line secret/PII scan: no findings" ;;
        1) echo "secret/PII pattern found in added lines" >&2; return 1 ;;
        *) echo "added-line secret/PII scan failed" >&2; return 1 ;;
    esac

    poetry run python scripts/validate-conventional-commits.py \
        --range "${base_ref}..HEAD" || return 1
)
