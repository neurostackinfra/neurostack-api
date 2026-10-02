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
