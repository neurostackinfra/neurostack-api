# API provider settings: operational rollout

This runbook gates the provider-settings work before any runtime or UI change.
Wave 0 is a hard external-capability gate: a `no-go` stops later groups and
returns the wish to DESIGN with the exact failed criteria.

## Evidence boundary

Set `API_PROVIDER_EVIDENCE_DIR` to a dedicated directory outside the checkout.
The script creates it with mode `0700`; raw JSON files use mode `0600`. Never
place that directory below the repository, attach it to a ticket, or add it to
Git. Database, Redis, callback, and secret-manager credentials are supplied
only through environment injection. They are never command-line arguments or
versioned files.

Evidence uses separate Ed25519 keys for each role, artifact, and environment.
Inject raw 32-byte keys as base64 through the secret manager, using these names
(replace `ENV` with `LOCAL`, `DEVELOPMENT`, or `PRODUCTION`):

- collector raw: `API_PROVIDER_COLLECTOR_ENV_PRIVATE_KEY` only in `collect`,
  and `API_PROVIDER_COLLECTOR_ENV_PUBLIC_KEY` in `verify`;
- sanitized attestation: `API_PROVIDER_ATTESTATION_ENV_PRIVATE_KEY` only in
  `verify`, and `API_PROVIDER_ATTESTATION_ENV_PUBLIC_KEY` in `scan-tracked` and
  `release-decision`;
- operational receipts: `API_PROVIDER_RECEIPT_WEBHOOK_SMOKE_ENV_PRIVATE_KEY`,
  `API_PROVIDER_RECEIPT_REDIS_REHEARSAL_ENV_PRIVATE_KEY`, and
  `API_PROVIDER_RECEIPT_ROLLBACK_REHEARSAL_ENV_PRIVATE_KEY` only in their
  producer jobs; `collect`/`verify` receive only the matching `PUBLIC_KEY`.

Never expose a private key to scanners, release jobs, argv, logs, fixtures, or
artifacts. The legacy `API_PROVIDER_EVIDENCE_HMAC_KEY` is rejected. Signatures
bind a versioned role/artifact/environment domain and a public-key-derived key
ID; receipts also bind the canonical signed digest recorded by the collector.
Rotate one role/artifact/environment pair at a time: publish the new public key
to consumers, switch the producer private key, drain evidence signed by the old
key within the 24-hour freshness window, then retire the old public key. Never
reuse a pair across roles, artifacts, or environments. `API_PROVIDER_PREFLIGHT_NOW`
is only for unsigned fixture generation and is rejected by live verification
and release decisions.

Only the verifier may produce the tracked, allowlisted Wave 0 artifact:

```text
.genie/wishes/api-provider-settings/evidence/wave0-development.attestation.json
```

It contains aggregates and booleans only. Run `scan-tracked --root .` before
handoff; it rejects tracked raw evidence, capability URL segments, probable
credential values, and sensitive fields in attestations.

The repository intentionally ignores `.genie/` as a whole, including its
database and launch state. Do not weaken that protection to expose the evidence
subdirectory. After verification, stage only the named sanitized attestation
with `git add -f .genie/wishes/api-provider-settings/evidence/<name>.attestation.json`,
then immediately run `scan-tracked --root .`; the scanner reads the staged blob
from Git's index as well as a divergent worktree copy.

The index scan enumerates NUL-delimited entries and reads their exact staged
blob IDs into one path-to-content mapping, so unusual filenames cannot change
record boundaries or diverge from a second enumeration. Divergent worktree
files are opened component-by-component without following symlinks, must be
regular files, and are rejected before reading when they exceed the bounded
scan size. A literal POSIX backslash is rejected rather than rewritten as a
separator. Unsafe, oversized, or unreadable divergence fails closed.

## Wave 0 collection

Use an opaque deployment reference and a fixed 31-day UTC window. Live
collection reads PostgreSQL inside a read-only repeatable-read transaction and
uses only Redis INFO/CONFIG GET. It does not write either production store.
`API_PROVIDER_RUNTIME_ENV` and `DEPLOYMENT_ID` must identify the actual runtime
and must match the requested environment and deployment; a local process cannot
be relabelled as development. `collect` receives the opaque deployment ID from
the approved command; `webhook-smoke`, `redis-load`, and `rollback-rehearsal`
obtain it only from `DEPLOYMENT_ID`. Every live producer requires
`API_PROVIDER_RUNTIME_ENV` and rejects identity mismatches before evidence is
accepted. Workers must be positive. Each external aggregate
file is a current-user-owned regular file with mode `0600`, carries a timezone-qualified observation timestamp and
source/environment/deployment provenance, and the message window start/end
must span exactly 31 days. Inputs are opened relative to the mode-`0700`
evidence directory without following symlinks; do not use symlinked ancestors.

The current application does not persist callback `messageId` values in the
event store: `event_service` allowlists only audio fields from webhook metadata.
Consequently, provide exactly 31 aggregate daily unique-ID counts from the
observability system in `API_PROVIDER_MESSAGE_COUNTS_FILE`, outside the
worktree. Event counts are not a valid substitute. The same file records the
measured p95 Redis claim size.

First run `collect` in development to produce the signed pre-smoke raw named
`wave0-development.raw.json` or `release-development.raw.json`. This raw is
fail-closed and cannot verify as release evidence. Pass it to `webhook-smoke`
with `--collect-input <signed raw>` and the same `--environment development`.
The command uses runtime `DEPLOYMENT_ID` and securely opens the raw inside the
mode-`0700` evidence directory, verifies the development collector signature,
freshness, closed schema, live provenance, environment, and deployment, and
binds its signed digest and `active_zapi_count` into the smoke receipt.

When the signed count is greater than zero, run `webhook-smoke` through the
controlled callback-trigger service. Its URL,
bearer, and opaque sentinel are injected as `API_PROVIDER_ZAPI_SMOKE_TRIGGER_URL`,
`API_PROVIDER_ZAPI_SMOKE_BEARER`, and `API_PROVIDER_ZAPI_SENTINEL`. Inject the
independently configured exact destination hostname as
`API_PROVIDER_ZAPI_SMOKE_ALLOWED_HOST`; it must not be derived from the URL.
Only HTTPS port 443 is allowed, and oversized responses are rejected before
JSON decoding. Approval
requires the provider/edge/collector path to preserve the canonical sentinel
byte-for-byte and the external edge to log only the route template. Existing
application log/metric/span request-target leaks remain an explicit
`expected-fail`; Wave 0 must never represent them as a passing redaction gate.
The trigger must be an HTTPS public host. The command rejects userinfo,
fragments, private/reserved DNS results and redirects, pins the validated IP for
the TLS connection, and sends the bearer only to that one origin. URLs,
sentinels, and bearer values remain environment-only and are not printed.

When the signed count is exactly zero, `webhook-smoke` reads no trigger URL,
bearer, or sentinel and makes no callback or network request. It emits only a
signed development receipt with status
`not_applicable_no_active_channel`, `readiness=false`, `activation=false`, and
`requires_fresh_smoke_before_activation=true`. A count mismatch or an N/A
receipt for a positive count is rejected. The first later channel activation
must start with a fresh development collect raw and complete a real callback
smoke; the N/A receipt never authorizes activation.

`webhook-smoke` is development-only. Production `pre-cutover` consumes passive
read-only evidence through `API_PROVIDER_PASSIVE_OBSERVABILITY_FILE`; its Z-API
roundtrip and application-redaction thresholds are emitted exactly as
`pass=false` and `status=not-run-pre-cutover`.

Wave 0 uses exactly one signed collect, followed by `webhook-smoke` and
`verify --input-dir`; it does not run `redis-load`. Redis capacity and eviction
observations are already bound into that collect. The verifier securely reopens
the smoke receipt, verifies its kind, closed schema, signature, runtime identity,
freshness, and canonical collect digest, then combines both only in memory. It
never rewrites the raw or asks for a second collect.

Release rehearsal also starts with one canonical signed
`release-development.raw.json`. `redis-load` and `rollback-rehearsal` locate it
under `API_PROVIDER_EVIDENCE_DIR` without extra CLI arguments and bind its digest,
environment, deployment, active-channel count, and freshness into their signed
receipts. The Redis receipt must report inserted and cleaned equal to
`p95_unique_messages_day * 31`, the observed p95 claim size, zero residual, and
zero evictions. Release verification rejects missing, swapped, cross-kind, or
cross-collect receipts. Fixture collection is non-representative and cannot
satisfy development or production.

## Threshold and decision

The Redis gate is:

```text
used_memory_peak + (p95_unique_messages_day * 31 * p95_claim_bytes * 1.5)
  <= 0.70 * maxmemory
```

`evicted_keys` must also remain zero during the representative rehearsal.
Z-API path roundtrip, external-edge redaction capability, Redis capacity, the
fixed window, representative environment, and Meta readiness must all pass.
Any failure produces `no-go`, lists the exact `design_return_criteria`, blocks
checkout of Groups 2-8, and requires returning to DESIGN.

## Meta interruption acceptance

Active Meta tenants without an App Secret must be zero. The only exception is
a documented interruption acceptance containing all of:

- the aggregate affected quantity (no tenant, patient, phone, or message IDs);
- Product Owner approval as a boolean role assertion;
- Production Operations Owner approval as a boolean role assertion;
- an opaque change-management reference ID;
- a timezone-qualified approval timestamp.

Do not include approver names, email addresses, tenant lists, clinic names, or
credential values. If either owner, the aggregate quantity, reference, or
timestamp is absent, verification remains `no-go`.

## Rehearsal and rollback

`redis-load` is forbidden in production. In development it uses a bounded,
deployment-scoped unique run prefix, records aggregate memory
and eviction deltas, writes the observed p95-size aggregate in bounded pipeline
batches. Cleanup scans only the cryptographically unique run prefix, verifies
the ownership value, and compare-deletes owned keys in bounded batches. A lost
pipeline acknowledgement therefore cannot strand a write, and pre-existing or
foreign values are never deleted. Partial failure reports
inserted/cleaned/residual counts and fails if any owned residual remains.
`rollback-rehearsal`
requires the edge closed, every legacy worker stopped, only v2-aware workers,
deletion limited to configuration cache keys, no legacy unauthenticated webhook,
and a forward-fix decision. Rollback never restores raw-cache or legacy webhook
behavior.

`release-decision` requires the exact pair
`--development <release attestation>` and
`--production <pre-cutover attestation>`. Both use the closed attestation schema,
must be fresh, timezone-qualified, bound to different deployment IDs, contain a
real 31-day window and complete inventories, have strictly typed approvals, and
show every required threshold passing with no design-return criteria. A minimal
`{"decision":"go"}` document is always a `no-go`; missing evidence is never
inferred and no criterion can be overridden.

The development attestation also binds `active_zapi_count` to its sanitized
smoke threshold. A positive count requires status `passed_real_callback`,
readiness and activation allowed, and no fresh-smoke requirement. A zero count
requires exact status `not_applicable_no_active_channel`, `readiness=false`,
`activation=false`, and `requires_fresh_smoke_before_activation=true`. This is
an acceptable gate outcome but never authorizes activation. Neither state can be
relabelled as the other. Production remains passive/read-only and uses a
separate signing domain, key suffix, deployment identity, and pre-cutover
attestation.

`verify` requires `API_PROVIDER_RUNTIME_ENV` and `DEPLOYMENT_ID` to match the
signed raw and every operational receipt. `release-decision` additionally reads
`DEVELOPMENT_DEPLOYMENT_ID` and `PRODUCTION_DEPLOYMENT_ID`; each must exactly
match its signed attestation, and the two IDs must differ. Fresh valid evidence
for another deployment is rejected.

## Local validation

Run the focused tests and strict standalone type gate before operational use:

```bash
poetry run pytest tests/scripts/test_api_provider_preflight.py -v
poetry run mypy --no-incremental --strict --ignore-missing-imports scripts/api_provider_preflight.py
poetry run python scripts/api_provider_preflight.py scan-tracked --root .
```

The repository-wide `make quality` remains an additional aggregate gate; do not
weaken or bypass either the focused strict command or the tracked-evidence scan.

## STT Scribe/OpenRouter handoff and emission failure runbook

The Scribe/OpenRouter change is operational readiness, not public promotion or
live homologation. Deterministic mocks prove the fixed HTTP contract only; they
do not prove real credentials, account quota, currently enabled models, or
production audio acceptance. The provider catalog exclusively owns the public
matrix and cards. Its pre-work gate must keep `elevenlabs_scribe` as
`coming_soon`, keep `openrouter` absent from the public STT matrix, and verify
that no UI/matrix diff is introduced before it deliberately promotes either ID.

An accepted incumbent-only transition commits the complete fresh STT bundle,
invalidates the configuration cache, and then attempts one
`api_credentials_changed` emission before returning. The payload is allowlisted:
actor/role, tenant ID, capability `stt`, the four field names, previous provider
`outros`, new provider, and endpoint class `fixed`; it contains no credential,
URL, model, value, path, query, or digest.

This signal is **at-most-once**, not a durable audit record. There is no outbox
or retry. A process crash after the database commit and before the emitter call
leaves the configuration committed with no event. A crash or lost response
after emission can make the client uncertain, but an exact retry is a no-op and
does not emit again. Emission failure also never rolls back the committed
configuration or turns the successful PATCH into an error.

Each emitter exception increments the label-free Prometheus Counter
`api_credentials_change_emit_failures_total` exactly once. No-op, validation
failure, rollback and commit failure neither emit nor increment it. The alert
`ApiCredentialsChangeEmissionFailures` fires when
`increase(api_credentials_change_emit_failures_total[15m]) > 0` remains true
for five minutes, with severity `warning` and owner `platform-sre`. Triage the
alert using the published runbook:
https://docs.neurostack.io/runbooks/api-credentials-change-emission-failures.
Treat the committed database state as authoritative, inspect only aggregate
Counter/structured-log evidence, never reconstruct secret-bearing payloads,
and do not replay an event unless a separately approved durable mechanism owns
deduplication.

Backout must preserve readers and runtime dispatch for persisted
`elevenlabs_scribe` and `openrouter` bundles. It may halt new incumbent switches
and must keep the public catalog unpromoted, but must not remove either wire ID
from schema/recognition/readiness, relabel or rewrite it to `outros`, remove its
fixed-base validator/adapter/caller, or make stored tenants unreadable. If those
compatibility guarantees cannot be maintained, use a forward fix rather than a
code rollback. This makes handoff order-independent: the catalog may land later
without becoming a prerequisite for existing stored configurations.

For a reproducible handoff, run the Group 3 aggregate pytest command, byte
parity `cmp config/prometheus_rules.yml src/config/prometheus_rules.yml`, the
frontend Vitest that protects absence of promotion, and `make quality` against
the exact source commit. Record the source commit, resulting commit and tree
digest. These fixed commands—not a manifest—select execution; no manifest may
choose an executable, arguments, working directory, or shell.

## Edge (proxy / gateway) redaction contract

The Z-API webhook path template is
`/v1/webhook/zapi/{capability_token}`. The application already emits every
observability record (access log, metrics label, tracing span, exception
report) using that template instead of the raw path — the Uvicorn raw access
log is disabled unconditionally and `RequestIdMiddleware` emits the sole
`http_request` structured record.

The first controlled edge (Nginx, Cloudflare, or any TLS terminator) must
mirror that contract before Z-API can be activated in production. Minimum
configuration:

- Log the request line as `METHOD /v1/webhook/zapi/{capability_token} HTTP/x`
  or equivalent. Substitute the last path segment before any file logger,
  syslog forwarder, WAF audit trail, or SIEM shipper sees it.
- Never propagate the raw path into headers seen by the upstream
  application (`X-Original-URI`, `X-Original-Path`, `X-Real-URL`). If the
  edge forwards any of those, strip the last segment there as well.
- Keep `follow_redirects` off for any egress out of the application: the
  same fail-closed contract applies to sidecars.

### Sentinel probe (first-edge smoke)

Once the edge redaction is configured, exercise it with a sentinel:

```bash
export SENTINEL=zapi_capability_sentinel_$(date -u +%Y%m%dT%H%M%SZ)
curl -sS -o /dev/null -w '%{http_code}\n' \
  "https://<edge-host>/v1/webhook/zapi/$SENTINEL" -X POST \
  -H 'Content-Type: application/json' -d '{}'
```

The sentinel MUST NOT appear in:

- Nginx / Cloudflare access logs (`access.log`, HTTP request log).
- The application `http_request` log (which uses the route template).
- Prometheus `neurostack_request_latency_seconds` /
  `neurostack_requests_total` label values.
- OpenTelemetry spans (`http.route` attribute uses the template, never
  the raw path).
- Any exception report body — the global handler logs only
  `endpoint=<template>` and the error type, never the raw path or the
  exception string.

Any hit against the sentinel outside the application's expected 401 body is
a hard `no-go` for release and must block cutover.

### Application-side self-test

To prove the app-side contract without an external edge, run:

```bash
poetry run pytest tests/api/test_observability_redaction.py -v
```

The suite injects a synthetic capability sentinel through the Z-API webhook
route, exercises the tracing/performance middleware and the exception
handler, then scans every captured log record and Prometheus label for the
sentinel. Any hit fails the run — this is the redaction gate for Wave 2 and
must stay green through Group 8.

## Gemini: build imutável, smoke e receipt assinado

Execute esta sequência, sem pular etapas, em um runner protegido. A identidade
Git deve ser exatamente `Production Operations Owner`. `GEMINI_IMAGE_REPOSITORY`
é o repositório OCI aprovado, sem tag ou digest; não contém segredo.

```bash
set -euo pipefail
tested_commit="$(git rev-parse --verify HEAD^{commit})"
run_id="gemini-${tested_commit}-$(date -u +%Y%m%dT%H%M%SZ)"
GEMINI_IMAGE_REPOSITORY="${GEMINI_IMAGE_REPOSITORY:?set the approved OCI repository}"
export tested_commit run_id GEMINI_IMAGE_REPOSITORY

test -z "$(git status --porcelain=v1 --untracked-files=all)"
test "$(git rev-parse HEAD)" = "$tested_commit"
test -z "$(git ls-files -s | awk '$1 == 120000 || $1 == 160000 {print}')"
test "$(git config user.name)" = "Production Operations Owner"

tmpdir="$(mktemp -d)"
export tmpdir
chmod 700 "$tmpdir"
git archive --format=tar "$tested_commit" | docker buildx build \
  --file Dockerfile --target production --provenance=true --sbom=true --push \
  --tag "$GEMINI_IMAGE_REPOSITORY:$tested_commit" \
  --metadata-file "$tmpdir/build-metadata.json" -
```

O contexto de build acima são exatamente os bytes de `git archive` do candidate;
nenhum arquivo dirty entra na imagem. Extraia somente o digest retornado por
Buildx, nunca consultando a tag, e execute o smoke live na mesma imagem. As duas
entradas privadas são injetadas pelo secret store do runner e não aparecem em
argv, stdout ou receipt.

```bash
digest="$(python3 -c 'import json,re,sys; d=json.load(open(sys.argv[1], encoding="utf-8"))["containerimage.digest"]; assert re.fullmatch(r"sha256:[0-9a-f]{64}", d); print(d)' "$tmpdir/build-metadata.json")" || exit 1
image_digest="$GEMINI_IMAGE_REPOSITORY@$digest"
export image_digest

docker run --rm --read-only --tmpfs /tmp \
  --env GEMINI_API_KEY --env GEMINI_MODEL "$image_digest" \
  python -m src.llm.gemini_smoke --json \
  --tested-commit "$tested_commit" --image-digest "$image_digest" \
  --run-id "$run_id" >"$tmpdir/smoke.json"
chmod 600 "$tmpdir/smoke.json"
```

Metadata ausente/inválido ou smoke não-zero encerra o run. A saída deve ser uma
única linha JSON canônica terminada por LF. Construa os bytes exatos do receipt
com Python stdlib; o SHA é calculado sobre a linha JSON incluindo seu LF.

```bash
receipt_path=.genie/wishes/gemini-credentials/evidence/gemini-llm-receipt.md
export receipt_path
mkdir -p "${receipt_path%/*}"
python3 <<'PY'
import hashlib, json, os, pathlib
smoke = pathlib.Path(os.environ["tmpdir"] + "/smoke.json").read_bytes()
artifact = json.loads(smoke)
canonical = json.dumps(artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
assert smoke == canonical
sha = hashlib.sha256(smoke).hexdigest().encode()
receipt = b"# Gemini LLM release receipt\n\n- smoke_artifact_sha256: " + sha + b"\n\n```json\n" + smoke + b"```\n"
pathlib.Path(os.environ["receipt_path"]).write_bytes(receipt)
PY

if git cat-file -e "$tested_commit:$receipt_path" 2>/dev/null; then
  echo "receipt already exists in candidate" >&2
  exit 1
fi
git add -f -- "$receipt_path"
test "$(git diff --cached --name-status)" = "A$(printf '\t')$receipt_path"
git commit -S -m "chore(release): attest Gemini smoke"
```

O commit precisa ser assinado por GPG ou SSH. Para SSH, configure
`gpg.format=ssh` e `gpg.ssh.allowedSignersFile` com a principal Operations
aprovada. Para ambos, injete como variável protegida o fingerprint exato exibido
por `%GF`: 40/64 hex maiúsculos para GPG ou `SHA256:<base64>` para SSH.

Depois da assinatura, valide novamente o sucessor e só então entregue ao VPS:

```bash
receipt_head="$(git rev-parse HEAD)"
test "$(git rev-list --parents -n 1 "$receipt_head" | wc -w)" -eq 2
test "$(git rev-parse "$receipt_head^")" = "$tested_commit"
test "$(git diff-tree --no-commit-id --name-status -r "$tested_commit" "$receipt_head")" = "A$(printf '\t')$receipt_path"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
git verify-commit --raw "$receipt_head"
test "$(git show -s --format=%GF "$receipt_head")" = "$GEMINI_RECEIPT_SIGNER_FINGERPRINT"
test "$(git rev-parse HEAD)" = "$receipt_head"
python3 scripts/validate_provider_availability_evidence.py --phase release
```

No VPS, faça checkout exato desse `receipt_head`, configure a mesma trust store e
fingerprint protegido e use somente `scripts/deploy-vps.sh`.

### `.env` de produção

O arquivo é dado, nunca shell. Cada linha não vazia deve ser exatamente
`KEY=value`, sem espaços, comentários, aspas de shell, `$`, crase, `;`, pipe,
redirecionamento, barra invertida, função ou controle. As únicas chaves são
`ENVIRONMENT`, `DEBUG`, `SECRET_KEY`, `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_DB`, `REDIS_PASSWORD`, `QDRANT_API_KEY`, `ALLOWED_ORIGINS`,
`ALLOWED_HOSTS`, `API_BIND_ADDRESS` e `API_PORT`.
`PATH`, `DOCKER_HOST`, chaves desconhecidas e duplicatas são recusadas. Valores
sensíveis têm mínimos explícitos (32 para `SECRET_KEY`/Qdrant, 24 para senhas),
ao menos 3 classes e 10 caracteres distintos; defaults e valores repetidos são
recusados sem imprimir o conteúdo. O arquivo continua sendo passado ao Compose
por `--env-file`, mas nunca é executado pelo Bash.

O VPS requer Python 3.11+, Git, trust GPG/SSH, Docker e Docker Compose. A única
entrada é `scripts/deploy-vps.sh`; sua primeira operação chama diretamente o
checker stdlib. Não instale PyYAML ou qualquer outra dependência Python para o
gate.

Qualquer mudança posterior no candidate, módulo de smoke, receipt, imagem da API
ou digest de Postgres/Redis/Qdrant exige re-homologação completa a partir de um
novo `tested_commit`: novo archive-build, smoke live, receipt e sucessor assinado.
