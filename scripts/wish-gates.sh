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

# docker-compose.yml is the development stack and aborts unless
# NEUROSTACK_DEV_STACK is set; this row is a deliberate dev-stack use under a
# disposable project name, so it sets the variable for its own commands only.
_install_compose() {
    NEUROSTACK_DEV_STACK=1 docker compose -p "$_INSTALL_COMPOSE_PROJECT" -f "$_WISH_GATES_ROOT/docker-compose.yml" \
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
            NEUROSTACK_DEV_STACK=1 docker compose -p "$_INSTALL_COMPOSE_PROJECT" \
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
# real secret is read; a missing Docker CLI fails, it is never skipped. The
# dev file aborts unless NEUROSTACK_DEV_STACK is set, so this config-only
# render passes it explicitly; the synthetic production inputs never carry it
# (tests/scripts/test_guard_alembic_upgrade_head.py pins that). Every
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
    env -i "${passthrough[@]}" "${fake_vars[@]}" NEUROSTACK_DEV_STACK=1 \
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
# gate transcripts are captured as evidence. `payment_security_term` matches
# both spellings of a card security code, the generic three-letter term and
# the provider's own field name (`c[c]v`), case-sensitively and anywhere on a
# line, as the generic term always did.
_asaas_scan_rules() {
    local forbidden_cpf='cpf'"Cnpj"
    local forbidden_card='credit'"Card"
    local payment_security_term='c[cv]'"v"
    printf '%s\t%s\n' \
        asaas_api_key '[$]aact_(prod|hmlg)_[A-Za-z0-9]+' \
        asaas_access_token_header 'asaas-access-token[[:space:]]*[:=][[:space:]]*[^$<{]' \
        bearer_token 'Bearer [A-Za-z0-9._-]+' \
        forbidden_cpf_field "$forbidden_cpf" \
        forbidden_card_field "$forbidden_card" \
        payment_security_term "$payment_security_term"
}

# Recorded secret/PII scan waivers, one
# `decision<TAB>path<TAB>rule<TAB>count<TAB>pattern` per line. OWNER DECISION
# 2026-10-18 (wish asaas-payment-gateway): the sandbox card capture tool is
# committed with a scoped exception instead of splitting its literals or
# skipping its files. It must send the provider's CPF/CNPJ, card and
# security-code field names and the public sandbox test card (sixteen 4s,
# CCV 123), its tests use fixed synthetic API keys, and its runbook
# (docs/runbooks/asaas-sandbox-card-capture.md) names the test card. A card
# number other than the public test card in those files still fails the gate.
#
# How narrow it is:
#   * `path` is compared byte for byte with the repo-relative path being
#     scanned: no prefix, glob, regex or case folding, so a copy or rename of
#     these lines to another path gets no waiver.
#   * `rule` is the only rule waived for that entry; every other rule still
#     runs on every line of the file. `pinned_line` waives nothing: it marks a
#     line the file must contain exactly `count` times (the card constants and
#     the payload lines that use them).
#   * `pattern` must match the WHOLE line (the scanner wraps it in `^(...)$`;
#     one trailing CR is ignored) and may hold only literal text: plain
#     characters, one-character bracket expressions such as `[.]`, `[$]`,
#     `[C]`, `[\\]` for a backslash, and one optional leading `[[:space:]]*`.
#     Anything else (`.`, `*`, `+`, `?`, `{}`, `()`, `|`, classes, ranges,
#     escapes) fails the gate up front, as do duplicate entries. Pinning the
#     line, not just the matched text, matters because the field-name rules
#     match a constant string. Rule literals are written as bracket
#     expressions so this table never matches the scan.
#   * `count` is the exact number of lines the entry may match in the file.
#     `_asaas_waived_path_audit` reads every path the table names (the tool,
#     its tests and its runbook) whole on every run: more
#     matches than recorded (a copied line) fails, a pinned line that does not
#     match exactly `count` times fails, and a card number written as a digit
#     run (optionally grouped with single spaces, dashes, dots or underscores)
#     of 13 or more digits, other than the sixteen 4s, fails as
#     `card_number_guard`. Other spellings (string concatenation, several-line
#     splits, double spaces or tabs, non-ASCII digits, encodings) are NOT
#     detected. Fewer matches of a waiver entry is a stale warning, never a
#     failure.
#
# A waived hit is never silent: each one prints `waived (<decision>):
# <path>:<line> rule=<rule>`, both scans print a waived count, and the audit
# warns about stale entries. The security-code rule matches the provider's
# field name (`c[c]v`) too, so the tool's payload line and its test assertion
# carry an explicit `payment_security_term` entry next to their pin or card
# entry; the pinned lines alone waive nothing. To remove the exception, delete
# these entries; the gate then fails on the tool's lines again. Expanding it
# needs a new recorded owner decision. See docs/runbooks/asaas-deployment.md.
_asaas_scan_waivers() {
    local decision='owner decision 2026-10-18, wish asaas-payment-gateway'
    local tool='scripts/asaas_sandbox_card_capture.py'
    local tests='tests/scripts/test_asaas_sandbox_card_capture.py'
    local runbook='docs/runbooks/asaas-sandbox-card-capture.md'
    local entry
    while IFS= read -r entry; do
        printf '%s\t%s\n' "$decision" "$entry"
    done < <(printf '%s\t%s\t%s\t%s\n' \
        "$runbook" pinned_line 1 \
        '[[:space:]]*`4444 4444 4444 4444`, with any future expiry and CCV `123`' \
        "$tool" pinned_line 1 \
        '[[:space:]]*"c[c]v": TEST_CARD_CCV,' \
        "$tool" pinned_line 1 \
        '[[:space:]]*"number": TEST_CARD_NUMBER,' \
        "$tool" pinned_line 1 \
        'TEST_CARD_CCV = "123"' \
        "$tool" pinned_line 1 \
        'TEST_CARD_NUMBER = "4444444444444444"' \
        "$tool" forbidden_cpf_field 2 \
        '[[:space:]]*"cpf[C]npj": identity[.]cpf,' \
        "$tool" forbidden_card_field 1 \
        '[[:space:]]*"credit[C]ard": [{]' \
        "$tool" payment_security_term 1 \
        '[[:space:]]*"c[c]v": TEST_CARD_CCV,' \
        "$tool" forbidden_card_field 1 \
        '[[:space:]]*"credit[C]ardHolderInfo": [{]' \
        "$tests" asaas_api_key 1 \
        'SANDBOX_KEY = "[$]aact_hmlg_100000000000capture-test-key-0001"' \
        "$tests" asaas_api_key 1 \
        'PROD_KEY = "[$]aact_prod_100000000000capture-test-key-0001"' \
        "$tests" forbidden_cpf_field 2 \
        '[[:space:]]*"cpf[C]npj": "52998224725",' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*"credit[C]ard": [{]"credit[C]ardNumber": "4444", "credit[C]ardToken": "tok"[}],' \
        "$tests" asaas_api_key 1 \
        '[[:space:]]*[(]"[$]aact_hmlg_xxxxxxxxxxxxxxxxxxxxaact_prod", "sandbox_key_is_production"[)],' \
        "$tests" asaas_api_key 1 \
        '[[:space:]]*[(]"[$]aact_hmlg_abc", "sandbox_key_malformed"[)],' \
        "$tests" asaas_api_key 1 \
        '[[:space:]]*[(]"[$]aact_hmlg_xxxxxxxxxxxxxxxxxxxx space", "sandbox_key_malformed"[)],' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"number"[]] == "4444444444444444"' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"c[c]v"[]] == "123"' \
        "$tests" payment_security_term 1 \
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"c[c]v"[]] == "123"' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*assert int[(]payload[[]"credit[C]ard"[]][[]"expiryYear"[]][)] > TODAY[.]year' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*"credit[C]ard": [{]"credit[C]ardNumber": "4444", "credit[C]ardToken": "secret-token"[}],' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*"credit[C]ardHolderInfo": [{]"name": "x"[}],' \
        "$tests" forbidden_card_field 1 \
        '[[:space:]]*assert clean[[]"_other_fields"[]][[]"credit[C]ard"[]] == "object"' \
        "$tests" forbidden_cpf_field 1 \
        '[[:space:]]*[{]"code": "invalid_cpf[C]npj", "description": "CPF 52998224725 inválido"[}],' \
        "$tests" forbidden_cpf_field 1 \
        '[[:space:]]*[{]"code": "invalid_cpf[C]npj", "reason": "unmapped"[}],' \
        "$tests" forbidden_cpf_field 1 \
        '[[:space:]]*assert sent_customer[[]"cpf[C]npj"[]] == identity[.]cpf  # the scrubbed value was real' \
        "$tests" asaas_api_key 1 \
        'UPPER_KEY = "[$]aact_hmlg_ABCDEFGHIJKLMNOPQRSTUVWXYZ"' \
        "$tests" asaas_api_key 1 \
        '[[:space:]]*key = "[$]aact_hmlg_abc4738010919XYZsecretTAIL"' \
        "$tests" asaas_api_key 1 \
        '[[:space:]]*broken[.]write_bytes[(]b"ASAAS_SANDBOX_API_KEY=[$]aact_hmlg_abc[\\]xff[\\]xfedefghijkl[\\]n"[)]')
}

# Reads stdin and prints `path:line: rule` per hit; exits 3 when anything
# not waived matched. ASAAS_SCAN_MODE=file numbers the input lines;
# ASAAS_SCAN_MODE=line scans one line numbered ASAAS_SCAN_LINE (a `git grep`
# hit) and fails if no rule reproduces it; ASAAS_SCAN_MODE=diff
# reads a single-file `git diff --unified=0` and scans only `+` lines inside
# hunks, numbered from each `@@ ... +start` header. With --unified=0 every body
# line starts with `+`, `-` or `\`, so the header block before the first `@@`
# is the only place a `+++ ` header can appear: an added line whose text starts
# with `++ ` is content, never a file switch. ASAAS_SCAN_MODE=audit reads one
# waived path's whole content (see `_asaas_waived_path_audit`).
#
# ASAAS_SCAN_WAIVERS holds the `_asaas_scan_waivers` table, validated and
# compiled in BEGIN whatever the mode, so a malformed entry fails even when no
# file is scanned. A hit is waived only when an entry's path and rule are
# equal (as strings) to this hit's and the entry's pattern, wrapped as
# `^(...)$`, matches the whole line. The parentheses are load-bearing: mawk
# lets a bare `^...$` match at an embedded NUL byte. A waived hit is printed,
# and its entry number is appended to ASAAS_WAIVER_LOG for the counts.
_ASAAS_SCAN_AWK='
function fail(message) { print message > "/dev/stderr"; failed = 1; exit 2 }
# Only literal text: plain characters, one-character bracket expressions
# (`[.]`, `[$]`, `[]]`, `[[]`, `[\\]` for a backslash) and one optional
# leading `[[:space:]]*`. No `.`, `*`, `+`, `?`, `{}`, `()`, `|`, `^`, `$`,
# ranges, classes, negations or backslash escapes.
function literal_pattern(re,    c) {
    if (substr(re, 1, 12) == "[[:space:]]*") re = substr(re, 13)
    if (re == "") return 0
    while (re != "") {
        if (substr(re, 1, 4) == "[\\\\]") { re = substr(re, 5); continue }
        c = substr(re, 1, 1)
        if (c == "[") {
            c = substr(re, 2, 1)
            if (substr(re, 3, 1) != "]" || c == "" || c == "^" || c == "\\") return 0
            re = substr(re, 4)
            continue
        }
        if (index("\\.[]()*+?{}|^$", c)) return 0
        re = substr(re, 2)
    }
    return 1
}
BEGIN {
    count = split(ENVIRON["ASAAS_SCAN_RULES"], specs, "\n")
    for (i = 1; i <= count; i++) {
        tab = index(specs[i], "\t")
        if (tab == 0) continue
        rules++
        rule_name[rules] = substr(specs[i], 1, tab - 1)
        rule_re[rules] = substr(specs[i], tab + 1)
        rule_index[rule_name[rules]] = rules
        if ("" ~ rule_re[rules]) fail("scan rule matches an empty line: " rule_name[rules])
    }
    if (rules == 0) fail("no scan rules")
    count = split(ENVIRON["ASAAS_SCAN_WAIVERS"], specs, "\n")
    for (i = 1; i <= count; i++) {
        if (specs[i] == "") continue
        n = waivers + 1
        if (split(specs[i], field, "\t") != 5 || field[1] == "" || field[2] == "" ||
            !((field[3] in rule_index) || field[3] == "pinned_line") ||
            field[4] !~ /^[1-9][0-9]*$/ || !literal_pattern(field[5])) {
            fail("malformed scan waiver entry " n)
        }
        key = field[2] "\t" field[3] "\t" field[5]
        if (key in seen) fail("duplicate scan waiver entry " n)
        seen[key] = 1
        waivers = n
        waiver_decision[n] = field[1]
        waiver_path[n] = field[2]
        waiver_rule[n] = field[3]
        waiver_count[n] = field[4] + 0
        waiver_re[n] = "^(" field[5] ")$"
        # Compiles the pattern now; a literal line is never empty.
        if ("" ~ waiver_re[n]) fail("scan waiver entry matches an empty line: " n)
    }
    waiver_log = ENVIRON["ASAAS_WAIVER_LOG"]
    if (waiver_log == "") fail("no waiver log")
    path = ENVIRON["ASAAS_SCAN_PATH"]
    prefix = ENVIRON["ASAAS_SCAN_PREFIX"]
    missing = ENVIRON["ASAAS_AUDIT_MISSING"] == "1"
    mode = ENVIRON["ASAAS_SCAN_MODE"]
    if (mode != "file" && mode != "diff" && mode != "line" && mode != "audit") {
        fail("bad scan mode")
    }
}
function waiver_for(j, line,    k) {
    for (k = 1; k <= waivers; k++) {
        if ((waiver_path[k] "") == (path "") && (waiver_rule[k] "") == (rule_name[j] "") &&
            line ~ waiver_re[k]) return k
    }
    return 0
}
function check(text, number,    j, k, line, matched) {
    line = text
    sub(/\r$/, "", line)
    for (j = 1; j <= rules; j++) {
        if (text !~ rule_re[j]) continue
        matched++
        k = waiver_for(j, line)
        if (k) {
            print prefix "waived (" waiver_decision[k] "): " path ":" number " rule=" rule_name[j]
            print k >> waiver_log
        } else {
            print prefix path ":" number ": " rule_name[j]
            hits++
        }
    }
    return matched
}
# A card number written as a digit run (optionally grouped with single
# spaces, dashes, dots or underscores) of 13 or more digits, other than the
# sixteen 4s of the public sandbox test card. Every run on the line is
# checked; the digits are never printed. Other spellings are NOT detected:
# string concatenation, a number split over several lines, double spaces or
# tabs between groups, non-ASCII digits, or any encoding.
function card_guard(text, number,    rest, run) {
    rest = text
    while (match(rest, /[0-9]([ ._-]?[0-9])*/)) {
        run = substr(rest, RSTART, RLENGTH)
        rest = substr(rest, RSTART + RLENGTH)
        gsub(/[ ._-]/, "", run)
        if (length(run) >= 13 && run != "4444444444444444") {
            print path ":" number ": card_number_guard"
            hits++
            return
        }
    }
}
function audit_line(text, number,    k, line) {
    line = text
    sub(/\r$/, "", line)
    card_guard(text, number)
    for (k = 1; k <= waivers; k++) {
        if ((waiver_path[k] "") != (path "") || line !~ waiver_re[k]) continue
        if (waiver_rule[k] == "pinned_line" || text ~ rule_re[rule_index[waiver_rule[k]]]) {
            found[k]++
        }
    }
}
# More matches than recorded fails (a copied line); a pinned line must match
# exactly its count; fewer matches of a waiver entry is a stale warning.
function audit_end(    k, n) {
    for (k = 1; k <= waivers; k++) {
        if ((waiver_path[k] "") != (path "")) continue
        n = found[k] + 0
        if (n > waiver_count[k] && waiver_rule[k] == "pinned_line") {
            print path ": pinned line entry " k " matches " n " lines, expected " waiver_count[k]
            hits++
        } else if (n > waiver_count[k]) {
            print path ": waiver entry " k " rule=" waiver_rule[k] " matches " n \
                " lines, more than the " waiver_count[k] " recorded"
            hits++
        } else if (n < waiver_count[k] && waiver_rule[k] == "pinned_line" && !missing) {
            print path ": pinned line entry " k " matches " n " lines, expected " waiver_count[k]
            hits++
        } else if (n < waiver_count[k]) {
            print "warning: stale secret/PII waiver entry " k " (" waiver_decision[k] "): " \
                path " rule=" waiver_rule[k] " matches " n " of " waiver_count[k] \
                " recorded lines" > "/dev/stderr"
        }
    }
}
mode == "audit" { audit_line($0, NR); next }
mode == "line" {
    if (!check($0, ENVIRON["ASAAS_SCAN_LINE"])) {
        fail("baseline hit not reproduced by the scan rules")
    }
    next
}
mode == "file" { check($0, NR); next }
/^@@ / {
    header = $0
    if (!sub(/^@@ -[0-9]+(,[0-9]+)? \+/, "", header)) fail("bad hunk header")
    number = header + 0
    in_hunk = 1
    next
}
!in_hunk { next }
/^\+/ { check(substr($0, 2), number); number++; next }
/^ / { number++; next }
END {
    if (failed) exit 2
    if (mode == "audit") audit_end()
    if (hits > 0) exit 3
}
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
# Waivers apply to the path as it is now (the new side of a rename), never to
# the path a file was moved or copied from; each waived hit's entry number is
# appended to `$work/waived-added`.
# Returns 0 clean, 1 on a finding, 2 when the scan itself failed.
_asaas_added_line_scan() {
    local base_ref="$1" work="$2" rules="$3" waivers="$4"
    shift 4
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
        ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_WAIVERS="$waivers" \
            ASAAS_WAIVER_LOG="$work/waived-added" ASAAS_SCAN_MODE=diff \
            ASAAS_SCAN_PATH="$path" LC_ALL=C awk "$_ASAAS_SCAN_AWK" <"$work/patch" || rc=$?
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
        ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_WAIVERS="$waivers" \
            ASAAS_WAIVER_LOG="$work/waived-added" ASAAS_SCAN_MODE=file \
            ASAAS_SCAN_PATH="$path" LC_ALL=C awk "$_ASAAS_SCAN_AWK" <"$path" || rc=$?
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
# are never read, symlinks are not followed and binary files are skipped. Each
# hit line goes through the same awk rules and waivers as the fatal scan, so a
# waived hit prints as waived here too; each waived hit's entry number is
# appended to `$work/waived-baseline`, which is how stale entries are found.
# Returns 0 with findings, 1 without, 2 when the scan failed.
_asaas_baseline_scan() {
    local work="$1" rules="$2" waivers="$3"
    shift 3
    local name expression path line content rc found=1
    local -a patterns=()
    while IFS=$'\t' read -r name expression; do
        [ -n "$name" ] && patterns+=(-e "$expression")
    done <<<"$rules"
    rc=0
    LC_ALL=C git grep --untracked -I -n -z -E "${patterns[@]}" -- "$@" \
        >"$work/grep" || rc=$?
    case "$rc" in
        0) ;;
        1) return 1 ;;
        *) echo "baseline git grep failed with exit $rc" >&2; return 2 ;;
    esac
    while IFS= read -r -d '' path && IFS= read -r -d '' line &&
        IFS= read -r content; do
        rc=0
        printf '%s\n' "$content" |
            ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_WAIVERS="$waivers" \
                ASAAS_WAIVER_LOG="$work/waived-baseline" ASAAS_SCAN_MODE=line \
                ASAAS_SCAN_LINE="$line" ASAAS_SCAN_PATH="$path" ASAAS_SCAN_PREFIX='  ' \
                LC_ALL=C awk "$_ASAAS_SCAN_AWK" || rc=$?
        case "$rc" in
            0) ;;
            3) found=0 ;;
            *) echo "baseline scan failed with exit $rc" >&2; return 2 ;;
        esac
    done <"$work/grep"
    return "$found"
}

# Fatal audit of every path the waiver table names, over the path's whole
# current content (tracked or untracked, never an ignored file), whether or
# not it changed since BASE_REF: card numbers (`card_number_guard`), copied
# waived lines and missing or changed pinned lines fail; stale entries only
# warn. A waived path that is a symlink or holds a NUL byte cannot be read as
# text, so it fails instead of being skipped. A path absent from the tree
# makes its entries stale. Returns 0 clean, 1 on a finding, 2 when the audit
# itself failed.
_asaas_waived_path_audit() {
    local rules="$1" waivers="$2"
    local path listed input missing rc status=0
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        listed="$(git ls-files --cached --others --exclude-standard -- ":(literal)$path")" || {
            echo "git ls-files failed for a waived path" >&2
            return 2
        }
        input="$path"
        missing=0
        if [ -z "$listed" ] || { [ ! -e "$path" ] && [ ! -L "$path" ]; }; then
            echo "waived path is not in the tree; its entries are stale: $path"
            input=/dev/null
            missing=1
        elif [ -L "$path" ] || [ ! -f "$path" ]; then
            echo "waived path is not a regular file and cannot be audited: $path"
            status=1
            continue
        elif [ "$(LC_ALL=C tr -dc '\000' <"$path" | wc -c)" -ne 0 ]; then
            echo "waived path contains a NUL byte and cannot be audited: $path"
            status=1
            continue
        fi
        rc=0
        ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_WAIVERS="$waivers" ASAAS_WAIVER_LOG=/dev/null \
            ASAAS_SCAN_MODE=audit ASAAS_SCAN_PATH="$path" ASAAS_AUDIT_MISSING="$missing" \
            LC_ALL=C awk "$_ASAAS_SCAN_AWK" <"$input" || rc=$?
        case "$rc" in
            0) ;;
            3) status=1 ;;
            *) echo "waived-path audit failed with exit $rc" >&2; return 2 ;;
        esac
    done < <(printf '%s\n' "$waivers" | cut -f2 | sort -u)
    return "$status"
}

# The release-candidate PostgreSQL suites: the single list run by
# asaas_release_candidate_gate below and by the `billing-postgres` job in
# .github/workflows/billing-postgres-integration.yml, so CI and the local gate
# cannot drift apart. Each suite creates and migrates its own databases from
# the `postgres` maintenance database that POSTGRES_SMOKE_DATABASE_URL names
# (a disposable loopback cluster, superuser role). The variable is required
# because the suites skip without it, and a skipped suite must not read as a
# pass. Usage:
#   POSTGRES_SMOKE_DATABASE_URL=... \
#     bash -c 'source scripts/wish-gates.sh && asaas_release_candidate_postgres_suites'
asaas_release_candidate_postgres_suites() (
    set -euo pipefail
    : "${POSTGRES_SMOKE_DATABASE_URL:?set POSTGRES_SMOKE_DATABASE_URL to a disposable loopback PostgreSQL}"
    cd "$_WISH_GATES_ROOT" || return 1
    poetry run pytest -p no:cacheprovider -rs \
        tests/integration/test_asaas_billing_postgres.py \
        tests/integration/test_asaas_checkout_service_postgres.py \
        tests/integration/test_asaas_webhook_postgres.py \
        tests/integration/test_asaas_subscription_triggers_postgres.py \
        tests/integration/test_billing_producer_handoff_postgres.py -v || {
        echo "release-candidate PostgreSQL suites failed" >&2
        return 1
    }
)

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
#
# Recorded scan exception (owner decision 2026-10-18, wish
# asaas-payment-gateway): `_asaas_scan_waivers` waives a closed list of exact
# synthetic lines of the sandbox card capture tool, its tests and its
# runbook, for named rules only, and `_asaas_waived_path_audit` reads those
# files whole on every run (card numbers other than the public test card,
# copied waived lines, changed card constants). Every waived hit is printed
# and counted; see the table for what is waived and how to remove it.
asaas_release_candidate_gate() (
    set -euo pipefail
    local root="$_WISH_GATES_ROOT"
    local base_ref="${BASE_REF:?set BASE_REF to the branch point, e.g. origin/main}"
    : "${POSTGRES_SMOKE_DATABASE_URL:?set POSTGRES_SMOKE_DATABASE_URL to a disposable loopback PostgreSQL}"
    local rules waivers work scan_status audit_status
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
    asaas_release_candidate_postgres_suites || return 1
    _production_compose_render || return 1

    rules="$(_asaas_scan_rules)" || return 1
    waivers="$(_asaas_scan_waivers)" || return 1
    work="$(mktemp -d /tmp/neurostack-asaas-scan.XXXXXX)" || return 1
    [ -n "$work" ] || return 1
    # shellcheck disable=SC2064 # expand now: $work is local to this subshell
    trap "case '$work' in /tmp/neurostack-asaas-scan.*) rm -rf -- '$work' ;; esac" EXIT
    : >"$work/waived-baseline"
    : >"$work/waived-added"
    # Parse the rules and the waiver table once up front, so a malformed
    # entry fails the gate even when no file reaches the scanner.
    ASAAS_SCAN_RULES="$rules" ASAAS_SCAN_WAIVERS="$waivers" ASAAS_WAIVER_LOG=/dev/null \
        ASAAS_SCAN_MODE=file ASAAS_SCAN_PATH=- LC_ALL=C awk "$_ASAAS_SCAN_AWK" </dev/null || {
        echo "secret/PII scan failed: malformed scan rules or waiver table" >&2
        return 1
    }

    echo "secret/PII scan: rules $(printf '%s' "$rules" | cut -f1 | paste -sd, -);" \
        "findings print path:line: rule, never the matched text;" \
        "files git treats as binary (a NUL byte) are not scanned;" \
        "$(printf '%s\n' "$waivers" | grep -c .) recorded waiver entries" \
        "(_asaas_scan_waivers), each waived hit printed as waived"

    # Pre-existing findings stay visible so maintainers can tell fixtures that
    # predate this candidate from new ones. They never fail the gate: only the
    # added-lines scan below does.
    scan_status=0
    _asaas_baseline_scan "$work" "$rules" "$waivers" "${scan_roots[@]}" \
        >"$work/baseline" || scan_status=$?
    case "$scan_status" in
        0) echo "baseline secret/PII findings (review required):"; cat -- "$work/baseline" ;;
        1) echo "baseline secret/PII scan: no findings"; cat -- "$work/baseline" ;;
        *) echo "baseline secret/PII scan failed" >&2; return 1 ;;
    esac
    echo "baseline secret/PII waived: $(grep -c . "$work/waived-baseline" || true)"

    # The waived files are audited whole on every run; a finding fails the
    # gate after the added-line scan has reported too.
    audit_status=0
    _asaas_waived_path_audit "$rules" "$waivers" || audit_status=$?
    case "$audit_status" in
        0) echo "waived-path audit: no findings" ;;
        1) echo "secret/PII waiver audit failed: a waived path breaks its recorded exception" >&2 ;;
        *) echo "waived-path audit could not run" >&2; return 1 ;;
    esac

    scan_status=0
    _asaas_added_line_scan "$base_ref" "$work" "$rules" "$waivers" "${scan_roots[@]}" ||
        scan_status=$?
    echo "added-line secret/PII waived: $(grep -c . "$work/waived-added" || true)"
    case "$scan_status" in
        0) echo "added-line secret/PII scan: no findings" ;;
        1) echo "secret/PII pattern found in added lines" >&2; return 1 ;;
        *) echo "added-line secret/PII scan failed" >&2; return 1 ;;
    esac
    [ "$audit_status" -eq 0 ] || return 1

    poetry run python scripts/validate-conventional-commits.py \
        --range "${base_ref}..HEAD" || return 1
)
