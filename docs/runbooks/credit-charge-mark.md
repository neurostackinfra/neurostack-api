# Credit charge mark runbook

> **Nothing in this runbook authorizes a deployment, a migration, a one-off
> task or a mark.** Each live step needs the product owner's explicit order at
> the moment of that step. A `mark` or `unmark` of a real builder in
> production is a live action, and so is writing a test API key.

State on 2026-11-11 (wish): production serves the bridge image with the
database at `047`; PR-B is neither merged nor deployed, and no command of this
runbook has run in production.

The charge mark (cobranca-credito-por-builder) charges a chosen builder (the
wish scopes one; the code does not limit it) for real while the global L10 enforcement (`credit_enforcement_at`) stays unset.
The contract (effective mode, `mode`, `charging`) is in
[`credits.md`](../billing/credits.md), section "Per-builder mode". This
runbook covers operating it.

## What the mark is

- A builder's state is the latest event of the append-only table
  `credit_enforcement_marks` (migration `047`): `mark`, `unmark`, `mark` is
  three rows; UPDATE and DELETE are refused
  ([`asaas-deployment.md`](./asaas-deployment.md), "Migration 047").
- **One door:** the operator command `python -m src.ops.credit_charge_mark`
  (`status`, `mark`, `unmark`). It is the only caller of
  `CreditRepository.insert_enforcement_mark` (module docstring of
  `src/ops/credit_charge_mark.py`; the AST fence in
  `tests/ops/test_credit_charge_mark.py`). There is no HTTP route and no UI
  control: the master-admin badge is read-only.
- The effective enforcement of a builder is the global
  `credit_enforcement_at` if set, else the builder's latest event when it is
  a `mark` (at its `occurred_at`), else none
  (`CreditRepository.effective_enforcement`, `src/repositories/credit.py`).
  `status` prints exactly that reader's answer.

## Preconditions (before the first `mark`)

1. **The PR-B image serves on EVERY task.** Wait for the service rollout to
   finish (deployment `COMPLETED`, every running task on the new revision):
   a task still running the bridge image does not read the mark, so the
   builder would be charged on some requests and not on others
   ([`asaas-rollback.md`](./asaas-rollback.md), "Migration 047").
2. **The database is at `047`.** Below it `status` prints
   `marks table absent` and `mark`/`unmark` refuse `marks_table_absent`.
3. **Run `status` first** and read, for the builder:
   - `builder.exists` is `true` and `tenants` lists exactly the tenant ids
     the owner expects for this builder (for the test, the Clínica Daniela
     tenant; an empty list charges nothing). Run `mark` with the same
     `--builder-id` string as that `status`;
   - `coverage.usable` is `true` and `coverage.reconciliation_failure` is
     `false` (no ASAAS quarantine, no identity conflict); otherwise
     `mark` refuses `coverage_unusable`, because enforced every request
     would answer `credit_pending`;
   - every entry of `tenants` has `whatsapp_provider` `zapi`, `meta_cloud`
     or `null`. `status` prints a known provider name as it is and any other
     stored value as `unrecognized`; for the test builder it must print
     `null`, not `unrecognized`; on `unrecognized`, stop: `mark` refuses
     `channel_not_allowed`, and fixing the clinic's stored provider is an
     owner decision. An empty string is refused by `mark` like
     any other value outside the allowed set (`channel_not_allowed`);
   - `enforcement.origin` is `none` (`global` means the L10 is armed: every
     builder is already charged and `mark` refuses
     `global_enforcement_armed`).
4. **An author:** `--author-user-id` is the id of an ACTIVE `super_admin`
   user, supplied by the owner (refused `author_not_active_super_admin`
   otherwise).
5. **A reason:** `--reason` is printed to stdout, stderr and the task logs
   and stored in an append-only table: **no personal data and no secret**.
   It is trimmed, 1 to 500 characters, one printable line with at least one
   letter or digit; e-mail-like text, control or format characters and
   invisible fillers (U+115F, U+1160, U+3164, U+FFA0, U+2800) are refused
   as `usage`. Invisible combining marks next to real text (such as U+034F)
   are still accepted.

## Running a step

Each command runs as a **one-off ECS task** on the current
`neurostack-prod-app` task definition revision in cluster `neurostack-prod`,
built as [`credit-enforcement-arming.md`](./credit-enforcement-arming.md)
"Running a step" builds its tasks, changing what that block names for
staging: cluster `neurostack-prod` (not `neurostack-staging`), the task
definition, subnets and security groups of the service `neurostack-prod-app`,
the module `src.ops.credit_charge_mark` in the guard, and the container name
of that task definition (read it there; it is not recorded in this
repository):

- `--region us-east-1 --launch-type FARGATE --count 1`;
- `DEBUG=false` in the override environment;
- the override built with `jq -n --arg`, so `--reason` arrives unchanged (no
  shell quotes inside the command array);

- the network configuration (subnets, security groups, no public IP) copied
  from the service `neurostack-prod-app`;
- a `startedBy` label naming the step (for example `charge-mark-status`);
- a container command override with exactly one command:

  ```
  python -m src.ops.credit_charge_mark status --builder-id <uuid>
  python -m src.ops.credit_charge_mark mark --builder-id <uuid> \
      --author-user-id <uuid> --reason "<why>"
  python -m src.ops.credit_charge_mark unmark --builder-id <uuid> \
      --author-user-id <uuid> --reason "<why>"
  ```

  UUIDs only in canonical lowercase hyphenated form (anything else is
  `usage`); there is no `--help`;
- **one task at a time**: wait for it to stop, read its exit code and its
  log (one JSON document on stdout, the same document plus
  `event: credit_charge_mark_ops` on stderr) before the next step.

`status` reads in one `REPEATABLE READ READ ONLY` transaction that is rolled
back: it cannot write. `mark` and `unmark` append one event or nothing (any
refusal rolls the transaction back). Each re-reads the credit switch in its
own process first.

## Verdicts, refusals and exit codes

| Verdict | Exit | Reasons | When |
|---|---|---|---|
| `ok` | 0 | | `status` read everything |
| `marked` | 0 | | `mark` wrote one `mark` event |
| `unmarked` | 0 | | `unmark` wrote one `unmark` event |
| `refused` | 20 | `builder_not_found` | no such builder (all three commands) |
| `refused` | 20 | `marks_table_absent` | `mark`/`unmark` with the database below `047` |
| `refused` | 20 | `author_not_active_super_admin` | the author does not exist, is inactive or is not a `super_admin` |
| `refused` | 20 | `global_enforcement_armed` | the global L10 is armed: a mark is redundant (both commands) |
| `refused` | 20 | `already_marked` | `mark` of a builder whose latest event is a `mark` |
| `refused` | 20 | `not_marked` | `unmark` of a builder whose latest event is not a `mark` |
| `refused` | 20 | `coverage_unusable` | `mark`: `binding_coverage(...).usable` is false |
| `refused` | 20 | `channel_not_allowed` | `mark`: a linked tenant's `whatsapp_provider` is not `zapi`, `meta_cloud` or none |
| `refused` | 20 | `window_already_opened` | `mark`: the current accounting window already has an opening |
| `refused` | 20 | `open_episode` | `unmark`: the builder has an open exhaustion episode |
| `refused` | 20 | `open_debt` | `unmark`: the builder has open debt |
| `switch_off` | 21 | `credit_switch_off` | the credit switch is not effective in this process |
| `unconfigured` | 30 | `usage` | malformed arguments (including a refused `--reason` or a non-canonical UUID) |
| `unconfigured` | 30 | `settings_invalid` | the settings cannot load |
| `error` | 50 | `import_failed` | the image cannot import the command |
| `error` | 50 | `unexpected` | any other exception |

`mark` reports every one of `coverage_unusable`, `channel_not_allowed` and
`window_already_opened` that applies, together. `unmark` reports both
`open_episode` and `open_debt` when both apply. Every other refusal stops
at the first one, in this order: `marks_table_absent`,
`author_not_active_super_admin`, `builder_not_found`,
`global_enforcement_armed`, `already_marked`/`not_marked`.

## What the marked builder sees

- `/billing` reads `mode` `enforced` (summary, state, consumption) and no longer shows the
  "Modo observacional" notice: the notice depends only on `mode`.
- The master-admin table "Assinaturas por Builder" shows the read-only badge
  **"Cobrança ativa"** on its row (field `charging` of
  `GET /v1/admin/builders/subscriptions`). It means "this builder has an
  operator mark", **not** "this builder is being charged": if the global
  enforcement is ever armed every builder is charged and only the marked
  rows still show the badge.

## What is charged and what is not

- **Charged** for a marked builder: Z-API and Meta traffic of its clinics
  (messages, and outgoing media on those channels: `PER_BUILDER_PROVIDERS` in
  `src/services/reply_media.py`) and `POST /v1/events` by API key (origin
  `production`).
- **Also charged**, as under the global: the clinics' stored content,
  reserved per accounting window (RAG 1 credit per 5,000 characters, media 1
  credit per 20 MB, `src/services/credit_allocations.py`). It is debited at
  the window's first enforced opening and on a knowledge or media upload that
  raises the window's target (only the part above the coverage already
  recorded, `charge_delta`), and media egress above the included amount is charged too. The first
  enforced event of a window can debit more than one credit.
- **Not charged:** Uazapi and Evolution stay observational, outgoing media
  included (they keep the global read). The panel JWT (simulator, origin
  `simulator`) never debits. The builder's home tenant without a
  `builder_id` is charged to nobody (a tenant with no builder only sees the
  global). Only clinics linked to the builder (`tenants.builder_id`) debit
  its lots.
- Accepted partial charging: a clinic linked to the builder after the
  `mark` is not re-checked against the channel rule, and a tenant holding
  Uazapi/Evolution keys with no `whatsapp_provider` passes it.

## Test recipe

1. **The test clinic's API key** (wish Decision 12). Generate the key (for
   example `secrets.token_urlsafe(32)`, as `src/api/routes/builder.py` does)
   and its bcrypt hash **locally**, under `umask 077`. Keep the key in a file
   that is **never printed** and never passed to a task, command line,
   override or log (an ECS override is visible in `DescribeTasks` and
   CloudTrail). Only the hash goes into the write task: a one-off task on the
   current `neurostack-prod-app` revision (the `consulta` family is
   read-only), running `UPDATE tenants SET api_key_hash = :new WHERE id =
   :clinic AND api_key_hash = :old`. It must report exactly 1 row; 0 means
   the hash changed in between, so stop. Keep the old hash as evidence:
   restoring it is the rollback. The clinic tenant must be active
   (`APIKeyStore.validate_api_key` in `src/api/middleware/auth.py` checks
   only active tenants). A read task confirms the new hash is stored
   **before the first call**: an invalid key logs its first 8 characters
   (`api_key_prefix`).
2. **`status`**, then **`mark`** with the owner's order.
3. **Traffic** by `POST /v1/events` with the linked clinic's API key in the
   `X-API-Key` header (`settings.api_key_header`), never as
   `Authorization: Bearer`, and
   **one `Idempotency-Key` per call** (without one the request answers
   `400 idempotency_key_required`; the same key with the same body replays the stored 200, with another
   body it answers 409).
   That traffic is origin `production`. The JWT simulator is origin
   `simulator` and does not debit.
4. **Bringing the balance to zero (QA):** at most 5,000 events (one credit per trace, `TRACE_COST_MC`; fewer when
   the window opening also reserved the clinic's stored content), or a removal through
   master-admin ("Tirar créditos" / "Tirar tudo",
   [`credit-courtesy.md`](./credit-courtesy.md)). In production, after
   exhaustion the builder runs on credit for the **72-hour** debt grace
   before suspension (`DEBT_GRACE` in `src/services/credit_lifecycle.py`).
5. **`unmark`** once the episode and the debt are closed.

## Lifecycle while marked

- At exhaustion the episode opens; 72 h later the builder is suspended (the
  states in [`credits.md`](../billing/credits.md), `/v1/credits/state`). The
  lifecycle worker evaluates only marked builders, each from its own mark
  instant, re-checked under the account lock. 30 days after exhaustion the state
  becomes `deletion_due`; content is never deleted automatically.
- **A lot that expires while the mark is active also exhausts the builder.**
  The test lot expires **2026-12-10**: if the mark is still active then, an
  episode opens and the builder is suspended 72 h later. Cover it with a new
  courtesy, or `unmark`, before that date.
- **`unmark` is refused** with an open episode (`open_episode`) or open debt
  (`open_debt`), and after the global L10 is armed
  (`global_enforcement_armed`).
- **A request already in flight may still debit after an `unmark`:** one
  that captured the mark and already holds, or is queued on, the account
  lock re-checks the mark only once it has the lock; the `unmark` waits for
  a holder, a request queued behind it sees the `unmark`, one admitted in
  between debits once more. The way out of such a charge is the usual one
  (a courtesy larger than the open debt; `status` until
  `lifecycle.open_episode` is null and `open_debt_mc` is 0; then `unmark`).
- **A `mark` of the wrong builder** is undone with `unmark` at once; anything
  it debited in between stays and is covered with a courtesy (first, when
  `unmark` is refused with `open_episode` or `open_debt`).
- **Re-mark after `unmark`:** a new `mark` is refused as
  `window_already_opened` until the next civil month (the next accounting
  window, America/Sao_Paulo) on a builder whose enforced traffic opened the
  current window.
- **An L10 arm that commits during a command is harmless:** the command
  takes no L10 rollout lock because the global always wins over a mark.

## How it fails

- **D1, per tenant:** once a process saw a tenant's request enforced by a
  mark, a later failure to read the mark (a failed read, or the `047` gate
  closed) answers `503 credit_enforcement_unavailable` for **that tenant
  only** (reason `enforcement_mark_unreadable_after_marked`), never the free
  path. Other builders' tenants answer as before.
- **The race:** when the enforcement under the account lock differs from
  the one the request captured (a request that captured the mark, whose
  tenant was relinked or whose builder was unmarked or re-marked in between;
  a request captured unmarked stays observational), the log line
  carries the JSON fields `"alert": "credit_enforcement_unavailable"` and
  `"reason": "enforcement_changed_under_lock"` at level `warning` and the request answers `503
  credit_enforcement_unavailable` before any write. It is repeatable only on
  `POST /v1/events` (retry it); on a webhook the message is lost, because
  the replay claim is already spent.
- A tenant relinked to another builder during a Meta/Z-API file handoff
  answers 503.

## Workers

- After the first mark ever, the orphan pass and its 15-minute full sweep
  (`ORPHAN_FULL_SWEEP_SECONDS = 900` in `src/services/credit_rollout.py`)
  run for ever, as they would after the global arm.
- With the global unset and the marks read failing (047 gate open), the three worker
  ticks (lifecycle, enforced reply publisher, orphan pass) fail on every
  interval: fail closed, nothing sent or changed.
- The publisher has no builder filter: every outbox row comes from an
  enforced request, so it also publishes for a builder already unmarked.

## The L10 report

A marked builder's tenants run the enforced path and write no observational
effect, so the cutoff report of
[`credit-enforcement-arming.md`](./credit-enforcement-arming.md) may raise
`message_without_observation` findings for them; the owner accepts them at
arming time (write the rationale under "Owner record per finding", then step
"3. `accept`").

## Rollback

This is the one full text of the PR-B rollback; see also
[`asaas-rollback.md`](./asaas-rollback.md), section "Migration 047". **Never an Alembic downgrade in
production.**

- **PR-B back to the bridge image:** credit stays on, but the bridge does not
  read the mark: a marked builder goes back to observational. Debits already
  written stay; pending enforced outbox rows are not recovered by the bridge
  publisher; `/v1/credits/state` still shows the open episodes. The mark stays in
  force: once PR-B serves again the builder is charged from its original
  mark instant; `unmark` only exists in PR-B.
- **Database restored below `047` while the PR-B image serves:** deploy the
  bridge image or restart the tasks. Otherwise, for up to 60 s (until the
  credit switch next refreshes, `credit_switch_refresh_seconds`),
  `/v1/credits/*` answers 500 and ledger writers fail for every builder; after that, in a process that saw
  them marked, the marked builder's tenants keep answering 503
  `credit_enforcement_unavailable` (D1) until the tasks restart.
- **After `047`, an image whose migration chain ends at `046`** turns credit
  off for everybody (`revision_outside_image_chain`). The way back is a
  forward fix. The snapshot `pre-047-<date>` is a last resort by the owner's
  decision.
