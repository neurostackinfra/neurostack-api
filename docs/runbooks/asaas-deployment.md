# ASAAS billing deployment runbook

> **Nothing in this runbook authorizes any rollout transition, deployment,
> migration, secret access, provider call or other live action.** Each of
> those needs its own explicit instruction from the product owner, naming the
> target, and its own approved operation. This document describes the current
> state and the preconditions; it is not a permission.

## Current state (2026-10-17)

- The ECS service `neurostack-staging-app` serves production traffic. Any
  change to it is production-impacting. Image rollout for it follows
  [`asaas-producer-image-rollout-group5.md`](./asaas-producer-image-rollout-group5.md).
- Billing rollout is `disabled` and `topup_commercial_enabled=false`. Verify
  both from the server readback (`GET /v1/admin/billing/rollout/readback`,
  `super_admin` JWT; it answers only when the app runs with
  `ENVIRONMENT=staging`, which is how the production-serving service is
  configured), never from UI state or environment overrides.
- The commercial credit top-up is deferred and stays unavailable.
- 2026-10-18: D3 step 1 (the secret access stack) is applied. The rest of
  the Group 9 D3/D4 production wiring is prepared but not applied. The service
  still runs `neurostack-staging-app:8`, which carries no ASAAS secret or URL.
  See [Production wiring (Group 9 D3/D4)](#production-wiring-group-9-d3d4).
- The product owner decided that `general` opens only after a delivered,
  deployed credit consumer exists (`neurostack-credit-system`). Today paying
  for a plan records the subscription and an outbox fact, but no code grants
  credits or enforces plan entitlements. Until the credit consumer exists, no
  transition to `canary_allowlist` or `general` is planned.

## Rollout states and what each one means

The control row lives in `billing_rollout_control`. Allowed transitions
(`src/repositories/billing.py`, `transition_rollout`):

| From | To | Notes |
|------|----|-------|
| `disabled` | `canary_allowlist` | Requires a named canary builder. |
| `canary_allowlist` | `general` | Requires the four readiness receipts below. |
| `canary_allowlist` or `general` | `disabled` | Containment; see the rollback runbook. |

`disabled -> general` is rejected. Every transition needs a three-actor
quorum (deployer, approver, reviewer) of distinct `super_admin` accounts with
active assignments, bound to the current rollout revision.

**`canary_allowlist` is itself a real-money state.** It is not a dry run. The
schema pins a hard-coded budget: at most 2 checkouts, 2 settlements and
109,900 cents (R$ 1.099,00 = Plus R$ 999,00 + one R$ 100,00 credit package),
one lifetime subscription, and only the offer `plan_plus_monthly_v1` without
coupon. The canary top-up reservation does not consult
`topup_commercial_enabled`, so a canary top-up would charge real money for
credits that no code delivers yet. The financial canary run was waived by the
owner on 2026-10-17; entering this state still charges a real card.

**`general`** is reached only from `canary_allowlist`. It requires four
`BillingRolloutReadinessReceipt` rows with status `passed` and types
readiness, backlog, provisioning and smoke, each bound to the digest of
`config/prometheus_rules.yml` and to the billing fact bundle digest, plus the
transition quorum. In `general` any eligible builder can create a Checkout
for Plus, Pro or Max, with coupon. `general` does not open
`topup_commercial_enabled`.

### Readiness receipt content digest

Each readiness row is recorded through `POST /v1/admin/billing/rollout/receipts`
by the duty its kind belongs to (`readiness` by the deployer, `backlog`,
`provisioning` and `smoke` by the reviewer) and carries `content_sha256`. The
backend recomputes it at insert and again at the `general` transition. It is
the lowercase hex SHA-256 of the ASCII bytes of one JSON object with exactly
these nine keys, sorted by key, with no whitespace (separators `,` and `:`):

| Key | Value |
|-----|-------|
| `app_digest` | the deployment digest the quorum signed (64 lowercase hex) |
| `bundle_digest` | the billing fact bundle digest (`billing_fact_bundle_digest()`) |
| `environment` | `development`, `staging`, `production` or `test` |
| `operation_id` | the `canary_allowlist -> general` operation id, as a lowercase hyphenated UUID string |
| `operation_receipt_id` | the deployer's operation receipt id, as a lowercase hyphenated UUID string |
| `provider_receipt_id` | the reviewer's operation receipt id, as a lowercase hyphenated UUID string |
| `receipt_kind` | `readiness`, `backlog`, `provisioning` or `smoke` |
| `rollout_revision` | the current rollout revision, as a JSON integer |
| `rule_digest` | the SHA-256 of `config/prometheus_rules.yml` as deployed |

Shape of the hashed bytes (placeholders in angle brackets):
`{"app_digest":"<hex>","bundle_digest":"<hex>","environment":"staging","operation_id":"<uuid>","operation_receipt_id":"<uuid>","provider_receipt_id":"<uuid>","receipt_kind":"smoke","rollout_revision":3,"rule_digest":"<hex>"}`.

Group 10 phase E3 (2026-10-19) removed `roster_revision` from this list and
from the readiness request and response. A request that still sends it is
refused with 422, and a receipt whose digest included it (every receipt
issued before E3) no longer verifies. Production holds no readiness receipts
of consequence.

A rollout revision whose readiness rows were recorded before E3 cannot be
completed. The `general` transition refuses them
(`billing_rollout_readiness_unbound`, nothing is spent, the state stays
`canary_allowlist`), and they cannot be recorded again at that revision: each
kind is unique per environment and revision, and a revision accepts one
deployer/reviewer pair, so re-recording is refused
(`billing_rollout_readiness_duplicate` for a kind that pair already holds,
`billing_rollout_readiness_operation_conflict` for another pair). Recover
with a new revision, each step its own approved operation:

1. Move `canary_allowlist -> disabled` with a fresh rollback quorum.
2. Re-open the canary (`disabled -> canary_allowlist`, a new rollout
   revision) with a fresh canary quorum for the builder.
3. Sign a fresh `canary_allowlist -> general` quorum, record all four
   readiness kinds again at the new revision, then request `general`.

The table column
`billing_rollout_readiness_receipts.roster_revision` and its
`CHECK (roster_revision >= 0)` stay until migration 036 (phase E5, written,
not applied in production; see
[Migration 036](#migration-036-decouple-from-the-legacy-roster-group-10-e5))
drops them. Images built from E3 up to E4 write the constant `0` and never
read the column, so a stored value is not evidence; images from E5 on no
longer map it. Migration 036 refuses to run while any unexpired readiness
receipt exists. Readiness and every rollout transition take the per-environment
rollout control lock first and no longer take the legacy roster fence.
Since phase E4 the ASAAS webhook and its readbacks take that lock first as
well and no longer take the roster fence either, so the per-environment
rollout lock is the only lock that serializes subscription creation. One
environment per database is therefore a requirement: two environments
sharing a database are no longer serialized against each other, and both
could hand off a subscription for the same builder, leaving it with two
active subscriptions.

## Work in progress that changes these steps

- **Receipt signing is moving server-side.** Today the operation-receipt
  request carries an HMAC that the client computes with the server key. The
  owner decided the backend will compute and store the signature after
  validating the actor, role, assignment and nonce. Do not distribute the
  server signing key to operators.
- **Operator tooling is being rebuilt.** No tool in this repository currently
  produces receipt signatures or drives the quorum end to end. Until the new
  tooling ships and is reviewed, there is no supported operator path for a
  rollout transition.
- **Outbox publication is not scheduled yet.** The billing outbox publisher
  and reconciler have no runtime caller; facts would stay `pending` without
  bounded retry or alerting.

## Preconditions before any future transition request

These are prerequisites to check, not steps to execute now:

1. The candidate is an immutable, reviewed image digest; the Alembic head,
   fact-contract digest, Compose config, observability rules and the
   frontend test/typecheck/build were verified for that digest, and the
   aggregate gate passed:
   `BASE_REF=<branch point> POSTGRES_SMOKE_DATABASE_URL=<disposable loopback PG> bash -c 'source scripts/wish-gates.sh && asaas_release_candidate_gate'`.
   Those two are the only inputs. Do not export production secrets for it:
   the Compose render uses the synthetic values in
   `tests/fixtures/compose/production-synthetic.env` (the same set as
   `tests/scripts/test_production_compose.py`) under `env -i`, so the caller's
   environment and any local `.env` are never read. The Docker CLI must be
   installed; without it the gate fails rather than skipping the render.

   **The PostgreSQL suites also run in CI.** The gate's PostgreSQL step is
   `asaas_release_candidate_postgres_suites` in `scripts/wish-gates.sh`, the
   only list of release-candidate PostgreSQL suites. The `Billing PostgreSQL
   suites` job (`.github/workflows/billing-postgres-integration.yml`) runs
   that same function on every pull request to `main` and every push to
   `main`, against a disposable `postgres:16` service container (loopback
   port, throwaway password, no secrets). Before the suites it turns on
   `logging_collector`, so the server-log audit in
   `tests/integration/test_asaas_subscription_triggers_postgres.py` runs
   instead of skipping, and migrates a separate service database to the
   current Alembic head. To add or remove a suite, edit the function; CI and this gate
   follow together. A green CI job does not replace this gate. These run
   only here: the contract suites
   (`tests/contracts/test_billing_fact_producer_consumer.py`,
   `tests/scripts/test_deploy_vps_gate.py`,
   `tests/scripts/test_production_compose.py`,
   `tests/scripts/test_asaas_observability.py`,
   `tests/scripts/test_validate_conventional_commits.py`), the Compose
   render, the secret/PII scans and the commit check.

   **Recorded secret/PII scan exception (owner decision 2026-10-18).** The
   product owner decided to commit the sandbox card capture tool
   (`scripts/asaas_sandbox_card_capture.py`,
   `tests/scripts/test_asaas_sandbox_card_capture.py` and
   `docs/runbooks/asaas-sandbox-card-capture.md`) with a scoped exception,
   instead of splitting its literals or excluding its files from the scan.
   The tool must send the provider's CPF/CNPJ, card and security-code field
   names and the public sandbox test card (sixteen 4s, CCV 123), its tests
   use fixed synthetic API keys, and its runbook names the test card.
   `_asaas_scan_waivers` in `scripts/wish-gates.sh` lists each waived line
   as exact path, one rule, the number of lines it may match, and a literal
   pattern that must match the whole line. Every other rule still runs on
   those files, any other value on a pinned line fails, and copies or
   renames of these lines to another path get no waiver (a file renamed into
   a waived path is audited like the tool). On every run the gate also reads
   all three files whole and fails on a card number written as a digit run
   (optionally grouped with single spaces, dashes, dots or underscores) of
   13 or more digits other than the sixteen 4s, on a waived line that
   appears more often than recorded, and when a pinned card line
   (`TEST_CARD_NUMBER`, `TEST_CARD_CCV` and the payload lines that use them)
   is missing or appears a different number of times. Pinned lines are
   checked for existence and exact count only, not for being overridden by a
   later assignment elsewhere in the file. Other spellings of a card number
   (string concatenation, several-line splits, double spaces or tabs between
   groups, non-ASCII digits, encodings) are NOT detected. A waived line
   matching fewer times than recorded only warns; raising a count needs a
   table edit, which is visible in review. The tool's runbook is part of the
   whole-file audit because its test-card line is pinned: the card-number
   check is what fails on any other card number there, and every rule keeps
   running on it. Each waived hit prints as `waived (owner decision
   2026-10-18, wish asaas-payment-gateway): <path>:<line> rule=<rule>`, both
   scans print a waived count, and an entry that no longer matches is
   reported as a stale warning. The security-code rule matches both
   spellings of the three-letter term, the generic C-V-V and the provider's
   field name C-C-V (spelled with hyphens here because this runbook is
   itself scanned), in lowercase, anywhere on a line (owner decision
   2026-10-19); the tool's payload line and test assertion carry an explicit
   waiver for it, and the term anywhere else fails the gate in added lines.
   Residual exposure: this gate detects no CCV values, emails, CPF digits or
   private-key blocks anywhere (unless they change a pinned line), and no
   card numbers outside those three files. To remove the exception, delete
   the table's entries; any new entry needs a new recorded owner decision.
2. Runtime configuration was applied and read back as described in
   [Production wiring (Group 9 D3/D4)](#production-wiring-group-9-d3d4):
   ASAAS secrets in the task definition, `ASAAS_WEBHOOK_URL`,
   `ASAAS_CHECKOUT_RETURN_URL` (HTTPS page pointing at the published
   `/billing` route), `ASAAS_WEBHOOK_NOTIFICATION_EMAIL`, the provider webhook
   registration and the public exposure of `/v1/billing/webhook/asaas`.
   `financial_egress_enabled` stays closed: no AppConfig profile exists, and
   opening it needs the follow-up F2 below.
3. Schema changes are expand-only and applied in a separately approved
   window; read back the exact `alembic_version` before deploying the image
   digest, and verify `/ready` on every task before any external effect.
4. Evidence records only opaque IDs, statuses, timestamps and SHA-256
   digests. Never record tokens, provider payloads, customer details or card
   data.

## Migration 036: decouple from the legacy roster (Group 10 E5)

Written and tested on disposable PostgreSQL 16 only; **not applied in
production**. Running it needs its own explicit owner instruction, after
033-035 are applied and verified, and the production window stays pinned to
`alembic upgrade 035` until that instruction (see "How the pin moves" below).

### What 036 changes

- Drops the statement trigger `trg_billing_subscriptions_roster_fence` (the
  last database part of the legacy roster fence). The function
  `lock_billing_roster_statement()` stays for the legacy tables' own
  triggers until 037.
- Rewrites the four canary trigger functions
  (`protect_billing_canary_subscription`, `validate_billing_canary_checkout`,
  `validate_billing_canary_projection`, `validate_billing_canary_effect`) so
  that none reads `billing_migrations`, the roster or the dropped columns. A
  non-ASAAS subscription row (provider `abacatepay` or NULL) stays
  representable. Its snapshot, provider included, can no longer change
  (`legacy subscription snapshot is immutable`), and a self-asserted move to
  `legacy_cancelled` is still refused
  (`legacy cancellation cannot be self-asserted`).
- Drops `billing_subscriptions.roster_revision`,
  `billing_rollout_readiness_receipts.roster_revision` and
  `billing_checkout_sessions.migration_id` with their constraints
  (`ck_billing_subscriptions_roster_revision`,
  `ck_billing_rollout_readiness_roster_revision`,
  `fk_billing_checkout_sessions_builder_migration`). It re-adds
  `ck_billing_checkout_sessions_intent_target` and
  `ck_billing_checkout_sessions_canary_exact` without `migration_id`.
- Leaves out the optional hardening (owner decision 2026-10-17): no provider
  NOT NULL, no tighter status/cycle/provider CHECKs (`transactions`
  included), no unique open subscription per builder. The legacy tables
  (`billing_migrations*`, `billing_roster_control`) stay until 037, and two
  of their validators read what 036 drops, so from 036 on they fail closed
  (those tables must stay empty anyway):
  - `validate_billing_migration_receipt` reads
    `billing_subscriptions.roster_revision`: any new
    `billing_migration_receipts` row fails with
    `record "subscription" has no field "roster_revision"` (observed on
    PostgreSQL 16).
  - `validate_billing_migration_absence_read` reads
    `billing_checkout_sessions.migration_id`: any new
    `billing_migration_absence_reads` row fails on the missing column.
  - `billing_migrations` itself has no INSERT trigger and still accepts a
    raw-SQL row until 037.

  **Notes for 037 (E6):** its preflight must count `billing_migrations`,
  `billing_migration_receipts`, `billing_migration_absence_reads` and the
  roster control again, because 036's guarantees only hold at the moment
  it ran: `billing_migrations` stays writable by raw SQL, and the two
  validators above fail rather than guard.

### Preflight (SELECT only, immediately before the run)

Read back `alembic_version` (must be `035`), then run these on the target
database. Every count must be `0`:

```sql
SELECT 'billing_migrations' AS check, count(*) FROM billing_migrations
UNION ALL SELECT 'billing_migration_receipts', count(*) FROM billing_migration_receipts
UNION ALL SELECT 'billing_migration_absence_reads', count(*) FROM billing_migration_absence_reads
UNION ALL SELECT 'roster revision not 0', count(*) FROM billing_roster_control WHERE billing_roster_revision <> 0
UNION ALL SELECT 'subscriptions not asaas', count(*) FROM billing_subscriptions WHERE provider IS DISTINCT FROM 'asaas'
UNION ALL SELECT 'subscriptions roster_revision not 1', count(*) FROM billing_subscriptions WHERE roster_revision <> 1
UNION ALL SELECT 'checkouts with migration_id', count(*) FROM billing_checkout_sessions WHERE migration_id IS NOT NULL
UNION ALL SELECT 'readiness roster_revision not 0', count(*) FROM billing_rollout_readiness_receipts WHERE roster_revision <> 0
UNION ALL SELECT 'unexpired readiness', count(*) FROM billing_rollout_readiness_receipts WHERE expires_at > now();
```

These are the same checks the migration runs itself, under `ACCESS
EXCLUSIVE` locks on the seven tables. The migration also refuses when any
catalog object other than the five constraints listed above depends on a
dropped column (for example a hand-made index), because `DROP COLUMN` would
drop it silently and the downgrade could not restore it.
`transactions` and `billing_webhook_deliveries` are not read: 036 does not
touch them. If anything is non-zero, stop and return to the owner. When a
check fails the migration raises SQLSTATE `55000` with one message listing
every failing check, for example
`migration 036 refused (nothing was changed): billing_migrations has 1 row(s); billing_rollout_readiness_receipts has 1 unexpired row(s)`,
and changes nothing. Alembic runs every revision of one invocation in a
single transaction, so a refused 036 inside a multi-revision run also rolls
back the earlier revisions of that run. Run `alembic upgrade 036` on its own,
after 035 is verified.

**Expected output of the run:** one line
`Running upgrade 035 -> 036, decouple ASAAS billing from the legacy roster and migration saga`;
then `alembic_version` reads `036`.

**Lock wait.** Both directions start with `SET LOCAL lock_timeout = '5s'`
(as migrations 024 and 025 do). The preflight then takes `ACCESS EXCLUSIVE`
on the seven tables (three in the downgrade). An open transaction that
touched one of them, even a plain `SELECT` left idle in a transaction, blocks
that lock. While the migration waits, every new read of that table queues
behind it, which is why the wait is bounded. A timeout raises SQLSTATE
`55P03` (`canceling statement due to lock timeout`). **55P03 means nothing
changed**: the whole migration transaction rolled back and `alembic_version`
is unchanged. Wait for the blocking transaction to end (see
`pg_stat_activity` and `pg_locks` for the holder; never kill it without the
owner) and rerun the same one-off task.

### What the application needs

The image must contain this release (the E5 code), which no longer maps the
three columns. Verified on PostgreSQL 16 on 2026-10-19:

- Every earlier image fails on 036 at its first read of
  `billing_subscriptions`, `billing_checkout_sessions` or
  `billing_rollout_readiness_receipts` (`column ... does not exist`).
- The E5 image works on 035 for every read and for checkout writes. It cannot
  insert a subscription or a readiness row there (`roster_revision` is NOT
  NULL without default). That happens only through a handoff or a readiness
  record, which cannot happen while the rollout is `disabled` everywhere.

Order (each step stops on any divergence and returns to the owner):

1. **Deploy the E5 image** while every environment sharing the database is
   `disabled`. **Verify:** the ECS deployment is `COMPLETED` and every RUNNING
   task runs the new task definition revision: no task of an earlier image is
   left, because an earlier image cannot read the 036 schema. A `/ready` 200
   alone proves nothing here: under `ENVIRONMENT=staging` it does not probe
   the database.
2. **RDS snapshot** (owner decision 2026-10-19 b): take a manual snapshot
   named `pre-036-<date>` of the production database and wait until its
   status is `available` before going on.
3. **Preflight readback** (the SELECT above) right before the run; every
   count `0`, `alembic_version` == `035`.
4. **Run 036** as a one-off `neurostack-staging-migrations` task on the same
   E5 image, with the command fixed to the exact revision
   (`alembic upgrade 036`). The service itself never migrates. On `55P03`
   see "Lock wait" above.
5. **Verify** `alembic_version` == `036`.

Never run 036 under an earlier image.

### Rollback (downgrade 036 -> 035)

The downgrade is real and restores the 035 columns, constraints, functions
and trigger from `alembic/frozen/036_downgrade_restores_035.json`. That DDL
was extracted from a fresh 035 database by `scripts/alembic_frozen_ddl.py`,
its SHA-256 is pinned in the migration, and the fidelity test proves the
result equal to a brand-new 035 catalog. One difference remains: the
restored columns are appended, so their physical position differs.

- It refuses (SQLSTATE `55000`, nothing changed) while any
  `billing_subscriptions` or `billing_rollout_readiness_receipts` row exists.
  Restoring their NOT NULL `roster_revision` would mean inventing roster
  evidence. In that state an application rollback can only go back to an
  image that contains E5.
- **Once any such row exists there is no supported way back to 035.** No
  cleanup is supported: readiness rows are append-only, and a DELETE of a
  subscription is silently skipped by its trigger. The independent review
  could remove them only as a superuser with
  `session_replication_role = replica`, deleting across outbox, facts,
  transactions, effects, projections, cycles and checkouts, which is not an
  operation this runbook allows. The downgrade is usable today only because
  production held zero such rows (read-only count, 2026-10-17); re-read them
  before relying on it.
- It takes the same 5-second lock timeout as the upgrade: `55P03` means
  nothing changed (see "Lock wait").
- Checkout rows need no invented value: they come back with
  `migration_id = NULL`.
- An earlier image runs again only after the downgrade succeeded.

### How the pin moves (separate, deliberate step)

The production window stays `alembic upgrade 035`: the manifest
`infra/production/asaas-production-wiring.yaml`, the planner constants in
`scripts/asaas_production_wiring.py`, gate 5 above,
`docs/configuration.md` and their tests (`tests/scripts/test_production_migration_window.py`,
`tests/scripts/test_asaas_production_wiring.py`). Only after the owner's
explicit instruction to run 036, one reviewed change moves the pin to the
exact revision `036` in all of them together, with this section's preflight
as the readback before the run. 037 gets its own instruction and its own
change. The command always names the exact revision, never the head.

## Production wiring (Group 9 D3/D4)

The reviewed artifacts are:

- `infra/production/asaas-production-wiring.yaml`: the declarative manifest
  (target, the exact secret and environment names, the ordered steps, the
  gates and the known blocks).
- `infra/production/asaas-production-wiring-secret-access.yaml`: the
  CloudFormation template for the IAM delta and the new secret.
- `infra/production/asaas-production-wiring.params.yaml`: the non-secret
  parameters, all three confirmed by the owner on 2026-10-18:
  `ASAAS_CHECKOUT_RETURN_URL=https://payments.staging.api.neurostack.global/billing`,
  `ASAAS_WEBHOOK_URL=https://staging.api.neurostack.global/v1/billing/webhook/asaas`
  and `ASAAS_WEBHOOK_NOTIFICATION_EMAIL=neurostack.infra@gmail.com`. To change
  one at apply time, edit this file, have it reviewed, and run `plan` again.
- `scripts/asaas_production_wiring.py`: the read-only planner.

### The read-only planner

```bash
params=infra/production/asaas-production-wiring.params.yaml
python scripts/asaas_production_wiring.py check-manifest --params "$params"
python scripts/asaas_production_wiring.py plan           --params "$params"
python scripts/asaas_production_wiring.py render-taskdef --params "$params"
python scripts/asaas_production_wiring.py check          --params "$params"
python scripts/asaas_production_wiring.py render-migrations-taskdef --params "$params"
```

- The service rollback target is the manifest field `rollback_target`
  (revision 8 today). Every code path reads it from there; it moves to
  revision 9 only through its documented re-pin (gate 6). The D3 source
  revision (`task_definition.source_revision`, 8) is a separate, fixed value:
  revision 9 stays derived from it.
- Two gate blocks are facts computed at plan time, not pinned states.
  `F1_residual_5_database_url_fingerprint` (F1 residual 5; it replaced the
  id `F1_group5_deploy_validator_allowlist`, whose check passes since F1
  path A) is reported by the planner while the real runtime fingerprint of
  the rendering differs from that of its protected replacement; PR #118
  fixes it. `F3_webhook_registration_entry_point:missing` is reported only
  while `src/ops/asaas_webhook.py` (PR #120) is absent from the tree the
  planner runs from. A Group 5 template validator rejection of the rendering
  is reported as `F1_group5_template_validator_regression:<reason>`.

- Every AWS request goes through one allow-list of 20 `Describe*`, `Get*` and
  `List*` operations (the list is printed as `aws_operations` in the plan).
  `GetSecretValue` is never allowed, and a response that carries secret
  material is rejected. The live mode needs `boto3` and credentials for those
  read operations in account `388460142431`, `us-east-1`.
- `--readback-file` replays a recorded readback instead of calling AWS. Use it
  when the reads were collected by another read-only channel; every request
  must be present, or the plan fails closed with `readback_missing:<operation>`.
- `plan` exits 0 with `status` `ready` (changes pending) or `compliant`, and
  exits 2 with `status` `blocked` when a D3 step is blocked. A missing or
  invalid parameter fails before any AWS read (`blocked:
  parameter_missing:<NAME>` or `parameter_invalid:<NAME>`). Each URL must pass
  the application's own settings validator, use a host the load balancer
  serves (Route 53 alias plus certificate coverage), carry no port, userinfo,
  query or fragment, and use exactly `/billing` or
  `/v1/billing/webhook/asaas`.
- `gate_blocks` lists what blocks the separately approved gates. It does not
  change the exit code.
- `render-taskdef` and `check` compute the same plan first. They print
  nothing and exit 2 (`blocked: plan_blocked:<reasons>`) whenever that plan is
  blocked, and they also refuse until `secret_access_stack` is `compliant`.
  A revision must therefore reference real, verified ARNs only.
- Once the stack exists, `plan` also checks the stack as a whole. It must
  contain exactly the two reviewed resources (the smoke token secret, with
  the live ARN as its physical ID, and the IAM policy). The template it was
  created from must equal the reviewed template. The manifest's `applied`
  record (stack, change set, smoke token ARN) must match live state.
- `check` runs the existing evidence validators against the rendered request.
  Since the F1 path A change it exits 0: every structural validator and the
  Group 5 deploy template validator accept exactly the D3 names. What the
  protected deploy path still rejects is reported under `gate_blocks` by `plan`
  (known block F1 below).

### What D3 changes, and why

1. **IAM (additive).** The execution role `neurostack-staging-ecs-execution`
   is managed by Terraform (tag `ManagedBy=terraform`) and its inline policy
   `segredos-e-logs` grants `GetSecretValue` on eight secrets that do not
   include `asaas-api-key` or `asaas-webhook-token`. The wiring adds one
   separately named inline policy, `asaas-production-wiring-secret-read`, with
   one statement: `secretsmanager:GetSecretValue` on exactly the three secret
   ARNs. It never edits `segredos-e-logs`, which a later Terraform apply would
   revert. The same pattern already exists on the task role, where the release
   stack added `neurostack-asaas-producer-release-task-signing`. The existing
   `kms:Decrypt` grant on key `90eff49a-c2bf-4836-834f-d5666294d956` already
   covers all three secrets, which use that key.
2. **Task role: no change.** The app receives the three secrets as
   ECS-injected environment through the execution role. D3 adds no AppConfig,
   Secrets Manager or other AWS call to the running app.
3. **Secret.** `neurostack/staging/billing-observability-smoke-token` is
   created by the stack with a value that Secrets Manager generates: 64 ASCII
   letters and digits, no whitespace, same KMS key and tags as its siblings,
   `DeletionPolicy: Retain`. The value never appears in the repository, a
   parameter, an output or a shell.
4. **Task definition.** One new revision of `neurostack-staging-app`, derived
   from the live D3 source revision `neurostack-staging-app:8` at plan time
   (it was also the rollback target when the wiring was planned). It keeps
   everything else byte for byte, including the image reference (unless
   `app_image_override` names an existing ECR digest), the containers `app`
   and `otel` (`otel` keeps an empty environment and no secrets) and the
   other tags. Two non-D3 changes, both reviewed manifest fields: the `otel`
   image becomes `task_definition.otel_image`, the digest the mutable
   `:latest` tag resolved to on 2026-10-19 (no behaviour change, never a tag
   again), and the tags carry `ManagedBy=manual` (`task_definition.tag_overrides`,
   **pending owner confirmation**: owner approval A4 covered the two ECS
   roles; tagging the new revisions the same way is an inference, truthful
   and reversible, to confirm before registering).
   It adds the following to `app` only:
   - secrets, as `valueFrom` ARN references: `ASAAS_API_KEY`,
     `ASAAS_WEBHOOK_TOKEN`, `BILLING_OBSERVABILITY_SMOKE_TOKEN`;
   - environment: `ASAAS_WEBHOOK_URL`, `ASAAS_CHECKOUT_RETURN_URL`,
     `ASAAS_WEBHOOK_NOTIFICATION_EMAIL` and `BILLING_FACT_WORKER_ENABLED=false`.

   It never carries a name with a forbidden prefix (the legacy payment
   provider, or `APPCONFIG_`) and never sets `SPA_ENABLED` or `SPA_DIST_DIR`.
5. **Financial egress: closed by absence.** The app reads
   `financial_egress_enabled` only through the AppConfig Agent at a loopback
   origin, and only when `APPCONFIG_ENABLED` is true
   (`src/infrastructure/appconfig.py`). With no `APPCONFIG_*` name in the task
   definition, every read returns false before any I/O, so checkout creation
   fails with `financial_egress_disabled` before any provider POST. No
   AppConfig application exists in the account (read back 2026-10-18). This is
   the only supported initial state.

### Ordered procedure

Steps 1 to 3 are the D3 apply the owner approved on 2026-10-18, to be done
after an independent review of the manifest. Every gate after them needs its
own explicit owner approval and is never performed from the manifest.

0. **Read back.** Run `plan` and keep its output. Before step 1, the
   expected result was `status: ready`, `secret_access_stack: would_change`,
   `task_definition_revision: pending_dependency`,
   `financial_egress_closed: compliant`, and the two known gate blocks F1 and
   F3. Since step 1 was applied, expect `secret_access_stack: compliant` and
   `task_definition_revision: would_change`. Record `terraform_policy_sha256`
   from the `secret_access_stack` step.
1. **Secret access stack (D3). APPLIED on 2026-10-18** by the orchestrator,
   after the independent review declared this step safe. Stack
   `neurostack-asaas-production-wiring` is `CREATE_COMPLETE`. Change set
   `d3-wiring-review` made two Adds and was executed with
   `RetainExceptOnCreate`. The smoke token is
   `arn:aws:secretsmanager:us-east-1:388460142431:secret:neurostack/staging/billing-observability-smoke-token-mSR8Ux`,
   and `segredos-e-logs` was unchanged. The manifest records this under the
   step's `applied` key, and `plan` checks it against live state. The
   procedure, for the record or for any re-creation: create a change set,
   review it, then execute it. Use the two ARN parameter values printed by
   `plan`, exactly as printed:

   ```bash
   aws cloudformation create-change-set --region us-east-1 \
     --stack-name neurostack-asaas-production-wiring \
     --change-set-name d3-secret-access --change-set-type CREATE \
     --template-body file://infra/production/asaas-production-wiring-secret-access.yaml \
     --capabilities CAPABILITY_IAM \
     --parameters ParameterKey=AsaasApiKeySecretArn,ParameterValue=<from plan> \
                  ParameterKey=AsaasWebhookTokenSecretArn,ParameterValue=<from plan>
   aws cloudformation describe-change-set --region us-east-1 \
     --stack-name neurostack-asaas-production-wiring --change-set-name d3-secret-access
   aws cloudformation execute-change-set --region us-east-1 \
     --stack-name neurostack-asaas-production-wiring --change-set-name d3-secret-access \
     --retain-except-on-create
   aws cloudformation wait stack-create-complete --region us-east-1 \
     --stack-name neurostack-asaas-production-wiring
   ```

   Continue only if the change set adds exactly two resources (the secret and
   the IAM policy) and modifies nothing else. `--retain-except-on-create`
   (`RetainExceptOnCreate=True`) is **required**. Without it, a create that
   fails and rolls back keeps the smoke token secret, because the secret's
   `DeletionPolicy` is `Retain`. That orphan then makes every later `plan`
   report `smoke_token_secret_exists_outside_stack` and makes a re-create fail
   on the existing name. Recovery from an orphan, which needs separate owner
   approval because it is a secret deletion: confirm with `plan` that no task
   definition revision references it, delete the failed stack, delete the
   orphaned secret with `aws secretsmanager delete-secret
   --force-delete-without-recovery`, run `plan` again, and repeat this step.
   **Verify:** `plan` shows `secret_access_stack: compliant` with
   `wiring_policy_exact`, `stack_resources_exact`,
   `stack_template_matches_reviewed_template` and
   `applied_record_matches_live_state`, and `terraform_policy_sha256` is
   unchanged from step 0.
2. **Task definition revision (D3).** Run `plan` immediately before rendering.
   Continue only if it exits 0, reports no `blocks`, and shows
   `secret_access_stack: compliant` and
   `task_definition_revision: would_change`. `render-taskdef` repeats that
   check itself and prints nothing when the plan is blocked. Render into a
   private temporary directory, review the diff against revision 8, then
   register:

   ```bash
   params=infra/production/asaas-production-wiring.params.yaml
   python scripts/asaas_production_wiring.py plan --params "$params" > /dev/null || exit 1
   workdir="$(mktemp -d)" && chmod 700 "$workdir"
   umask 077
   python scripts/asaas_production_wiring.py render-taskdef --params "$params" > "$workdir/d3-taskdef.json" || exit 1
   aws ecs register-task-definition --region us-east-1 --cli-input-json "file://$workdir/d3-taskdef.json"
   rm -rf -- "$workdir"
   ```

   Registering changes nothing in the service.
   **Verify:** `plan` shows
   `task_definition_revision: registered_not_deployed:<new ARN>` and
   `status: compliant`. `check` shows `structural_status: pass`.
3. **Financial egress (D3).** No action. **Verify:** `plan` shows
   `financial_egress_closed: compliant`.
4. **Gate: signed image build and attestation (separate approval).** The
   running image (`neurostack-staging:asaas-gateway-staging-20261014-cfcb7f0b`,
   pushed 2026-10-14) predates the SPA and the latest billing code. Production
   images are built, signed and attested only by the protected workflows. The
   protected deploy path now accepts a serving revision (and the template
   captured from it) whose app image is in `neurostack-staging` **pinned by
   digest**, and always deploys the attested producer-repository digest (F1
   path A, below). Revision 8, and a D3 revision rendered without
   `app_image_override`, run a mutable tag and are still rejected
   (`F1_protected_path_image_repository_mismatch`). F1 residual 5 (the
   `DATABASE_URL` fingerprint) is fixed (F1 item 5 below). It also needs the build
   network access listed under
   [Frontend build and deployment](#frontend-build-and-deployment).
5. **Gate: migrations 033-035 (separate approval).** In an expand-only
   window, read back the exact `alembic_version` first (it was `032` on
   2026-10-17), continue only if it is still `032`. Revision 3 of the
   one-off family `neurostack-staging-migrations` bakes an unpinned upgrade,
   so it is **never run**. Register a new revision instead, on the same
   bridge image as the deployment (`app_image_override` in the parameters
   file), with the command baked to exactly `alembic upgrade 035` (the planner
   constant `MIGRATIONS_COMMAND`); the roles, the `database-url-migrations`
   secret, sizing and logs are read from revision 3, and the tags carry
   `ManagedBy=manual` (pending owner confirmation, as for the app revision).
   The readback file holds three reads:
   `ecs:DescribeTaskDefinition` of `neurostack-staging-migrations:3` and of
   `neurostack-staging-migrations` (latest), both with `"include": ["TAGS"]`,
   and `ecr:DescribeImages` of the bridge digest.

   ```bash
   params=infra/production/asaas-production-wiring.params.yaml
   workdir="$(mktemp -d)" && chmod 700 "$workdir"
   umask 077
   python scripts/asaas_production_wiring.py render-migrations-taskdef \
     --params "$params" --readback-file "$workdir/migrations-readback.json" \
     > "$workdir/migrations-taskdef.json" || exit 1
   ```

   It prints nothing and exits 2 for any command other than the pin, a
   revision 3 other than the reviewed shape (roles, the
   `database-url-migrations` secret as a plain ARN, one container, no entry
   point), an image missing from ECR, or a newer migrations revision that
   differs from the rendering, or a newest revision whose ARN is not
   `arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-migrations:<n>`
   with `n` at least 3 (`migrations_newest_revision_invalid`). If the newest
   revision already equals the rendering it prints nothing on stdout, `already registered: <ARN>` on
   stderr, and exits 3: run that revision instead of registering again
   (`--force-new-revision` prints the request anyway). Otherwise register
   `migrations-taskdef.json` (`ecs:RegisterTaskDefinition`), take a manual
   RDS snapshot (`pre-033-035-<date>`, status `available`), then run the new
   revision once as a one-off Fargate task in the service's private subnets
   with **no** command override. Never upgrade to the head: once migrations
   `036` and `037` (decouple and drop the legacy objects) are on main, the
   image carries them and the head would apply them in the same run. Each of
   them needs its own explicit owner instruction, 036 before 037, after a
   fresh SELECT showing zero legacy rows. **Verify:** read back
   `alembic_version` == `035`. Never downgrade.
6. **Gate: service deployment (separate approval).** Two paths (see F1
   below):
   - **Path A (owner approval A1, 2026-10-19).** Deploy the registered D3
     revision 9 (bridge image pinned by digest) with the current tooling,
     outside the protected path:
     `aws ecs update-service --cluster neurostack-staging --service neurostack-staging-app --task-definition <revision 9 ARN>`.
     The minimal F1 change is implemented (F1 below). Before the first
     signed-image deploy, regenerate the protected template from the deployed
     D3 revision (F1, "Regenerating the protected template").
   - **Path B.** One protected deployment that carries the signed image and
     the wiring together. It needs everything Path A's F1 change needs, plus
     a reviewed exception to the runtime fingerprint equality check. Not
     chosen.

   After the deployment is `COMPLETED`, run the
   [post-deploy webhook registration hook](#post-deploy-webhook-registration-hook).
   Its failure is the alert `DEPLOYED_WEBHOOK_PENDING`: the deployment stays.

   **Re-pinning the rollback target (8 to 9).** While revision 9 is under
   observation, `rollback_target` stays 8: rolling back to 8 is safe until
   migration 036 runs. Once revision 9 has served, been verified and its
   observation period (24 h) has closed, and **before** migration 036, edit
   the manifest field to `revision: 9`,
   `effective_after: bridge_revision_9_serving_verified_and_observation_closed`
   and remove its `repin` block (a reviewed change; the planner refuses any
   other value). Revision 8 is not safe after 036: it reads columns 036
   drops. `plan` then reports `rollback_target` = revision 9 and checks that
   it is `ACTIVE` and runs an image pinned by digest
   (`rollback_target_revision_unusable` otherwise).

   What the current image does with the D3 names (not verified for the image
   itself): it was pushed on 2026-10-14, and commit `55651b58` on
   `wish/asaas-payment-gateway` (2026-10-13) already parses and validates
   `ASAAS_CHECKOUT_RETURN_URL`, `ASAAS_WEBHOOK_URL` and
   `ASAAS_WEBHOOK_NOTIFICATION_EMAIL`. It applies the same production
   key-namespace check and starts the same read-only readiness refresher.
   Its settings use `extra="ignore"`, so `BILLING_FACT_WORKER_ENABLED` is
   ignored. Whether the image was built from that commit is unknown; the
   image tag's `cfcb7f0b` is not in this clone.

   Effects to expect: with the ASAAS key and webhook URL set, every worker
   process (4 per task) starts the webhook readiness refresher. The refresher
   makes **read-only ASAAS calls** (`GET /v3/webhooks`, then
   `GET /v3/webhooks/{id}`) with the production key: every 300 s when ready,
   otherwise with backoff from 30 s up to 300 s. This is not gated by
   financial egress, by design. Checkout creation stays closed (rollout
   `disabled`, financial egress closed, webhook not ready). The outbox worker
   stays off.

   Startup preconditions the owner checks without printing any value:
   - the API key secret is a plain string in the production key namespace
     (`Settings.validate_asaas_api_key`), or every new task fails settings
     validation and the circuit breaker rolls back;
   - the webhook token is a plain string of 32 to 255 printable ASCII
     characters, or readiness stays `token_unconfigured`.

   **Verify:** the deployment is `COMPLETED`, every task is RUNNING on the new
   revision, the target group is healthy, and `GET /ready` returns 200 on every
   task. Under `ENVIRONMENT=staging`, `/ready` proves that the process started
   and settings validated; it does not probe the database, Redis or Qdrant.
   Re-read the rollout state (`GET /v1/admin/billing/rollout/readback`); it
   must still be `disabled`. **Rollback:** see
   [`asaas-rollback.md`](./asaas-rollback.md).
7. **Gate: SPA serving (arrives with the signed image).** The task definition
   carries no SPA name. Images built with the `frontend-build` stage set
   `SPA_DIST_DIR=/app/spa` themselves, and `SPA_ENABLED` defaults to true. The
   current image has no `/app/spa` (inferred from dates; image contents were
   not inspected). **Verify:** `GET /` and `GET /billing` on both hosts return
   the SPA with the enforced CSP. Then run the post-deploy browser check: the
   builder session is stored per origin (`localStorage`), so a builder who
   starts checkout on `staging.api.neurostack.global` returns on
   `payments.staging.api.neurostack.global` and may have to log in again. This
   is a known behaviour to confirm, not a blocker.
8. **Gate: webhook exposure and registration, D4 (separate approval).** See
   [Webhook exposure and registration (D4)](#webhook-exposure-and-registration-d4).
9. **Gate: rollout transitions.** These are not planned until the credit
   system exists. See
   [Preconditions before any future transition request](#preconditions-before-any-future-transition-request).

### Webhook exposure and registration (D4)

Exposure, read back 2026-10-18 and checked by `plan`:

- The ALB `neurostack-staging-alb` is internet-facing. Port 80 redirects to
  HTTPS. Port 443 has only its default rule, which forwards every path to the
  service target group, so `/v1/billing/webhook/asaas` reaches the app on both
  hosts. The certificate covers `staging.api.neurostack.global` and
  `*.staging.api.neurostack.global`, and both hosts have Route 53 aliases to
  the ALB.
- `AuthMiddleware` makes `/v1/billing/webhook/asaas` public by exact match
  only: no prefix and no trailing slash. Every other path outside the existing
  public list still requires a JWT. The receiver authenticates the
  `asaas-access-token` header before parsing or persisting anything.
- The WAF web ACL `neurostack-staging` has default Allow, and all managed
  rule groups and the 2000/IP rate rule are in Count mode, so it blocks
  nothing. If any rule is moved to Block, `plan` reports
  `waf_rule_may_block_receiver`, and ASAAS deliveries must be re-checked
  (large bodies and provider IP reputation).

Registration (owner decision A2, 2026-10-19: automatic at deploy):

- The command is `python -m src.ops.asaas_webhook <subcommand>`
  (`src/ops/asaas_webhook.py`, shipped in the image with `src/`). The deploy
  hook runs it as a one-off ECS task on the service's own task definition, so
  the API key and the webhook token never leave the task. It reads only the
  process environment, never a `.env` file: `ENVIRONMENT` (`staging` or
  `production`; the owned name is `neurostack-billing-<ENVIRONMENT>`, so
  `neurostack-billing-staging` here), `ASAAS_API_KEY` (production namespace),
  `ASAAS_WEBHOOK_TOKEN` (32 to 255 printable ASCII characters),
  `ASAAS_WEBHOOK_URL`, `ASAAS_WEBHOOK_NOTIFICATION_EMAIL` and
  `ASAAS_USER_AGENT`. Any invalid value fails closed before any network call.
- **URL pin (deliberate safety net).** `ASAAS_WEBHOOK_URL` must be exactly
  `https://staging.api.neurostack.global/v1/billing/webhook/asaas`
  (`ALLOWED_WEBHOOK_URLS` in the module, the D3 parameter in
  `infra/production/asaas-production-wiring.params.yaml`; the host set
  `ALLOWED_WEBHOOK_HOSTS` is derived from it). Another host (including a port
  or different case) is `unconfigured` / `webhook_host_not_allowed`; the
  decided host with any other path is `unconfigured` /
  `webhook_url_not_allowed`; neither makes a network call. If the D3
  parameter ever changes, change both constants in the same reviewed change
  as the D3 parameters.
- Subcommands: `check-config` (configuration only, no network), `readback`
  (the default; read-only), `reconcile` (at most one write, then the smoke
  checks) and `smoke` (`readback` plus a second listing proving the webhook is
  still the only one). Flags, all for `reconcile` only and off by default:
  `--force-rewrite-token`, `--allow-enable` and `--no-resume`; plus
  `--deadline-seconds N` (10 to 600, default 120, the hard limit for the whole
  run). There is no `--help`: the only exit 0 is a `ready` verdict.
- `reconcile`: list every page of `GET /v3/webhooks`. A webhook is a
  candidate when its name is the owned name or its URL is `ASAAS_WEBHOOK_URL`.
  No candidate: `POST` once. One candidate with the owned name: read it by
  exact ID; when it drifted, `PUT` it; when it is exact, write nothing. Two or
  more candidates, or one that matches by URL only with another name:
  `ambiguous`, nothing written. Then re-read by ID and list again. Nothing
  ever deletes a webhook: the command's transport refuses every request other
  than `GET /v3/webhooks`, `GET /v3/webhooks/{id}`, `POST /v3/webhooks` and
  `PUT /v3/webhooks/{id}` (writes without a query string), and refuses the
  two writes outside `reconcile`.
- **Listing completeness.** A listing is used only when its pages are
  complete and contiguous (every page but the last is full and says
  `hasMore`, the last does not, each page starts where the items read so far
  end, a reported `totalCount` equals what was read), no webhook ID appears
  twice, and no page exceeds the client's 64 KiB response cap. Otherwise the
  verdict is `error` / `listing_inconsistent` before any decision or write.
  This catches short pages, pages that overlap (an ignored `offset`, an
  unstable sort) and totals that disagree. It cannot catch a provider that
  leaves a webhook out of every page and reports a total that matches what it
  served; no client can.
- **Operator pause and ASAAS queue interruption.** Every `PUT` sends
  `enabled: true` and `interrupted: false`.
  - A webhook **disabled** in ASAAS (`enabled: false`, an operator pause) is
    report-only: verdict `disabled` (exit 13), reason
    `webhook_disabled_by_operator`, and nothing is written, not even other
    drift. The pause outranks `requires_recreate`: a disabled webhook with
    `apiVersion` drift still reports `disabled` (both reasons are listed).
    `reconcile --allow-enable` re-enables it (reason `webhook_reenabled`).
    Reconcile never re-enables a disabled webhook on its own.
  - A queue ASAAS **interrupted** by itself after failed deliveries (for
    example while the service was rolled back and answered 503) is resumed by
    default, which makes ASAAS re-send the backlog; the result shows
    `queue_resumed`. `reconcile --no-resume` makes it report-only (verdict
    `interrupted`, exit 12, reason `queue_not_resumed`). When a webhook is
    both disabled and interrupted, a `PUT` needs both permissions.
- Drift in `apiVersion`, which ASAAS accepts only at creation, is never
  written: the verdict is `drift` with `requires_recreate` (procedure below).
  A read whose body omits `apiVersion` or sends `null` is not compared on it
  (reason `api_version_unreported`; the verdict can still be `ready`). Any
  other value that is not the integer 3 (for example `"3"`, `3.0`, `true`) is
  drift and is never `ready`. The in-process
  readiness refresher does not skip it yet, so in that case hosted checkout
  stays closed until `compare_webhook` gets the same rule. The notification
  e-mail is also creation-only and is not read back, so a change of
  `ASAAS_WEBHOOK_NOTIFICATION_EMAIL` does not reach an existing webhook.
- **Token rotation.** ASAAS exposes only `hasAuthToken`, never the token, so
  an exact webhook is never rewritten by default. After changing
  `ASAAS_WEBHOOK_TOKEN`, deploy the revision with the new token (the receiver
  then accepts only the new token) and run
  `reconcile --force-rewrite-token` once; it sends one `PUT` with the new
  token. Every successful `PUT` carries the current token and reports
  `token_rewritten`. ASAAS deliveries fail with 401 between the deploy and
  that run.
- **Failures.** 429, 408, 5xx, timeouts and connection errors are retried 3
  times per call (exponential backoff with jitter, capped at 8 s) inside the
  deadline, then the verdict is `error` with `transient`. 401 or 403 is `error`
  with `auth` at once. A `POST` is re-sent only when it provably never
  reached ASAAS (connection not made, or 429). When its outcome is unknown or
  it is rejected, the command lists again and adopts what it finds
  (`create_unconfirmed` or `create_rejected`), never posting twice. A result
  with `"writes": 1` and the reasons `transient` and `deadline_exceeded`
  means the deadline fired while a write was in flight: the write may have
  been applied, and the next run adopts what ASAAS holds.
- **Concurrency.** The command takes no lock: run one task at a time. If two
  runs overlap and both read an empty listing, both can create a webhook.
  Either ASAAS rejects the second creation and that run adopts the first
  run's webhook, or both runs exit 20 (`ambiguous`, `duplicate_detected`)
  when their final listing shows the duplicate; a run whose final listing
  came before the other's creation can still report `ready`, and every later
  run then reports `ambiguous`. Nothing is deleted: see the procedure below.
- **Output.** Exactly one JSON line on stdout and one JSON log line on
  stderr (`"event": "asaas_webhook_registration"`). Fields: `schema`
  (`neurostack.asaas_webhook_registration.v1`), `command`, `verdict`,
  `action` (`none`, `created` or `updated`), `reasons`, `webhook_id_sha256`,
  `counts`, `checks` (booleans), `force_rewrite_token`, `allow_enable`,
  `no_resume` and `exit_code`. They never contain the key, the token, the
  URL, the e-mail or the webhook ID.
- **Exit codes:** 0 `ready`, 10 `absent`, 11 `drift`, 12 `interrupted`, 13
  `disabled`, 20 `ambiguous`, 30 `unconfigured` (including a bad command line
  or settings that cannot load), 40 `error`. Any other code (for example 1)
  means the process crashed.
- The in-process refresher keeps calling `AsaasWebhookProvisioner.readback()`.
  Apart from the unreported `apiVersion` case above, a `ready` from this
  command is the same readiness predicate, so the gauge below follows within
  one refresher period.

Operator procedures (each is a manual provider operation in the ASAAS panel
with its own approval; the command itself never deletes or renames):

- **`requires_recreate`** (`apiVersion` drift, which a `PUT` cannot change):
  run `readback` and record `webhook_id_sha256`. With approval, in the ASAAS
  panel, delete the webhook named `neurostack-billing-staging` whose ID
  hashes to that value. Then run `reconcile` at once: it finds no candidate,
  creates the webhook once (`action: created`) and ends `ready`. Whether ASAAS
  delivers events that happen while no webhook exists is not verified, so
  keep that window short and check the affected payments by readback
  afterwards. **The new webhook is created ENABLED.** If the webhook is also
  disabled (the result lists `webhook_disabled_by_operator`), deleting and
  recreating it would undo that pause: never use this procedure to get
  around a pause; the person who paused it, or the owner, must decide
  explicitly first.
- **`foreign_name_on_url`** (a webhook with another name already points at
  `ASAAS_WEBHOOK_URL`): it is not ours by name, so the command refuses it.
  Identify it in the ASAAS panel and decide with the owner: if it belongs to
  another integration, move that integration to another URL; if it is an old
  NeuroStack webhook, delete it (approval) or rename it to the owned name
  (approval; the next `reconcile` then converges it). Run `readback` until it
  reports `absent` or `ready`, then `reconcile`.
- **`multiple_owned` / `duplicate_detected`**: delete all but one of the
  candidates in the ASAAS panel (approval), then run `reconcile`.
- **`webhook_disabled_by_operator`**: someone paused the webhook on purpose.
  Re-enable only on that person's or the owner's instruction, with
  `reconcile --allow-enable`.

Read-only verification after registration:

1. `smoke` (or `readback`) reports `ready`. Its reasons are empty, or only
   `api_version_unreported` (see the `apiVersion` note above; in that case
   step 2 does not turn green until `compare_webhook` gets the same rule).
2. The gauge `neurostack_billing_webhook_config_ready` reads 1 on every worker
   within one refresher period (300 s).
3. `GET /v1/billing/summary` without a JWT still returns 401, so the receiver
   did not open any other path.
4. Optional, inside the deployment window: an unauthenticated
   `POST /v1/billing/webhook/asaas` with body `{}` returns 401 and persists
   nothing. This is the token smoke that `AsaasWebhookProvisioner.smoke()`
   automates.

### Post-deploy webhook registration hook

Owner decision A2 (2026-10-19) made the ASAAS webhook registration
automatic at deploy. After every approved service deployment reaches
`COMPLETED`, one one-off ECS task on the deployed revision runs
`python -m src.ops.asaas_webhook reconcile`, so the ASAAS key and token never
leave the task. `scripts/asaas_deploy_hook.py` is pure, like the planner: it
reads a recorded readback file, renders the requests and verifies the result.
It never calls AWS; the orchestrator sends the requests with its own tools and
writes the responses back. The only requests it ever renders are
`ecs:RunTask`, `ecs:DescribeTasks`, `ecs:ListTasks` and `logs:GetLogEvents`.
It never renders or recommends `UpdateService`, `RegisterTaskDefinition`,
`StopTask` or a rollback; its output guard refuses to print one.

Files, in a private directory (`workdir="$(mktemp -d)" && chmod 700 "$workdir"; umask 077`):

| File | Content |
|------|---------|
| `hook-readback.json` | schema `asaas_deploy_hook_readback.v1`: `schema_version`, `account_id` `388460142431`, `region` `us-east-1`, `captured_at_utc` (aware ISO 8601, updated on every poll), `reads` (`service`, `operation`, `params`, `response`, one per exact request) and, after the run, `run_task` (`params`, `response`). An AWS error response is recorded as `{"__error_code__": "<Code>"}`. |
| `hook-plan.json` | output of `plan` |
| `hook-run-task.json` | output of `render-run-task`: the exact `ecs:RunTask` parameters |
| `hook-verify-reads.json` | output of `render-verify-reads` |
| `hook-result.json` | output of `verify-result` |

Sequence (`ARN` is the revision the deployment completed on):

1. **Immediately before RunTask**, read into `hook-readback.json` (and set
   `captured_at_utc` to the time of the reads):
   `ecs:DescribeServices` `{"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]}`;
   `ecs:ListTasks` `{"cluster": "neurostack-staging", "startedBy": "neurostack-asaas-webhook-reconcile"}`
   (`startedBy` must be the only filter; the default desired status `RUNNING`
   covers provisioning, pending and running tasks);
   `ecs:DescribeTaskDefinition` `{"taskDefinition": "<ARN>"}`; and
   `ecr:DescribeImages` `{"repositoryName": "<repository>", "imageIds": [{"imageDigest": "<digest>"}]}`
   for that revision's `app` image.
2. `python scripts/asaas_deploy_hook.py plan --expected-task-definition "$ARN" --readback-file "$workdir/hook-readback.json" > "$workdir/hook-plan.json"`.
   Live runs omit `--now`: the system clock judges freshness, so time spent
   between `plan` and `render-run-task` counts. `--now <aware ISO 8601>` is
   only for replays and tests. A readback older than 10 minutes
   (`readback_stale`) or dated more than 60 s in the future
   (`readback_captured_in_future`) is blocked: read again.
   Exit 0 is `status: ready`. Exit 2 is `blocked` (the reasons are in
   `blocks`, or on stderr as `blocked: <reason>` for a missing read or an
   invalid file): send no RunTask, raise `DEPLOYED_WEBHOOK_PENDING` with the
   reasons, and leave the deployment alone. Preconditions: exactly one
   deployment, `PRIMARY` and `COMPLETED`, on `ARN`, the service on `ARN`,
   `runningCount == desiredCount > 0` and nothing pending; no task with the
   hook's `startedBy` (single flight); Fargate; awsvpc with subnets, security
   groups and `assignPublicIp: DISABLED` on the service; the revision
   `ACTIVE` on awsvpc and Fargate, with exactly the containers `app` and
   `otel`, an essential `app` container, no entry point, the D3
   webhook wiring, an `awslogs` log configuration in `us-east-1` with a
   stream prefix, and an image pinned by digest that still exists in ECR; no
   RunTask already recorded in the file.
3. `python scripts/asaas_deploy_hook.py render-run-task --expected-task-definition "$ARN" --readback-file "$workdir/hook-readback.json" > "$workdir/hook-run-task.json"`
   (no `--now` in a live run: it re-checks freshness against the system
   clock; add `--force-rewrite-token` only for a token rotation, below;
   `--attempt N`, 1 to 20, only for a deliberate re-run of the same
   deployment). It refuses (exit 2, nothing printed) unless `plan` is ready.
   Keep `hook-run-task.json`: it is what crash recovery re-sends. Send
   `ecs:RunTask` with exactly that object as parameters, then record
   `"run_task": {"params": <hook-run-task.json>, "response": <RunTask response>}`.
4. `python scripts/asaas_deploy_hook.py render-verify-reads ... > "$workdir/hook-verify-reads.json"`
   prints the `ecs:DescribeTasks` request for the started task and the first
   `logs:GetLogEvents` page of its `app` stream
   (`<awslogs-stream-prefix>/app/<task id>`, `startFromHead: true`,
   `limit: 500`). Read the stream page by page: send the same request with
   `nextToken` set to the previous response's `nextForwardToken` until a
   response returns the token that was sent (the end of the stream; a page
   without `nextForwardToken` is not an end and is reported as
   `log_pages_incomplete`). Record every page; each page is its own entry (the `nextToken` makes the requests
   differ). Every 15 s, re-send `DescribeTasks` and the pages, replace their
   entries and update `captured_at_utc`.
5. `python scripts/asaas_deploy_hook.py verify-result ... > "$workdir/hook-result.json"`.
   Exit 0 `registered`; exit 3 `in_progress` (poll again: `next_requests`
   names the reads still needed, for example the next log page,
   `log_events_incomplete`); exit 4 `DEPLOYED_WEBHOOK_PENDING`; exit 2 the
   input is incomplete or invalid (fix the file; if it cannot be fixed, treat
   it as `DEPLOYED_WEBHOOK_PENDING`). The one-result-line check and the
   exposure scan run over every recorded page.

Crash recovery. If the orchestrator stops after sending RunTask but before
recording it:

- Re-send the saved `hook-run-task.json` unchanged. The same `clientToken`
  makes ECS return the task it already started instead of starting another
  (as long as ECS still honours the token); record that response and
  continue at step 4.
- If that cannot be done and the task is still running, `plan` is blocked
  (`registration_task_running`, with the task in
  `running_registration_tasks`) and `render-run-task` refuses: wait for that
  task to stop, then run `render-verify-reads --task-arn <task ARN>` and
  `verify-result --task-arn <task ARN>` against a readback file without a
  `run_task` record. The task is accepted only if its `startedBy`, task
  definition and command override are the hook's and it was created at or
  after the current PRIMARY deployment's `createdAt` on the expected revision
  (`recovered_task_predates_deployment` or
  `recovered_task_not_tied_to_deployment` otherwise); the result says
  `recovered_without_run_record: true`. Never start a second run while the
  first one may still be running.

The RunTask request: `cluster` `neurostack-staging`, `taskDefinition` the
service's current revision, `count` 1, `launchType` `FARGATE`, the service's
`platformVersion`, the service's subnets and security groups with
`assignPublicIp` `DISABLED`, `overrides.containerOverrides` exactly
`[{"name": "app", "command": ["python", "-m", "src.ops.asaas_webhook", "reconcile"]}]`,
`startedBy` `neurostack-asaas-webhook-reconcile`, `propagateTags` `NONE`,
`enableECSManagedTags` and `enableExecuteCommand` false, and a `clientToken`
derived from the deployment ID, the request and the attempt: a retried call
cannot start a second task. Single flight is the `ListTasks` precondition plus
that token; two operators planning at the same instant can still both start
a task (the window between the listing and the RunTask), in which case the
command itself reports `ambiguous` and deletes nothing.

Time bounds. ECS has no maximum runtime for a RunTask task, the container
`stopTimeout` cannot be overridden in RunTask (and is at most 120 s on
Fargate), and `StopTask` is outside what the hook may render. The run is
bounded by the command's own deadline: 120 s for all ASAAS I/O
(`--deadline-seconds` default, enforced in-process). The watch only decides
when to stop waiting: 600 s from the task's `createdAt`, which covers the
deadline plus a 180 s start budget (ENI, image pull, imports) and a 60 s stop
budget (exit, `otel` SIGTERM and its 30 s stop timeout), 360 s, with margin.
After that `verify-result` reports `task_watch_deadline_exceeded`. A stream
still empty within 120 s of `stoppedAt` is `in_progress`
(`log_events_not_delivered`); later it is a missing result line.

`verify-result` reads the `app` exit code and the stream's one result line
(the command's stdout document; its stderr twin, `"event":
"asaas_webhook_registration"`, is optional and must agree). The document is
checked against the closed schema `neurostack.asaas_webhook_registration.v1`
(`schema`, `command`, `verdict`, `action`, `reasons`, `webhook_id_sha256`,
`counts`, `checks`, `force_rewrite_token`, `allow_enable`, `no_resume`,
`exit_code`). The hook never passes `--allow-enable` (re-enabling a webhook an
operator paused is an owner decision) or `--no-resume` (an interrupted queue is
resumed by default), so both must read `false` (`result_options_mismatch`
otherwise). Any line in the
stream that carries a URL, a host of ours or of ASAAS, an e-mail address, an
ASAAS key, a PEM block, an AWS key, `authToken`, a bearer token or a webhook
path fails the verification (`log_forbidden_material`, alert kind
`secret_exposure_suspected`) and is never printed. The hook's own output
carries only ARNs, digests, counts and closed vocabularies; the webhook ID
appears only as the SHA-256 the command prints.

Failure semantics. Every outcome other than `registered` is the alert
`DEPLOYED_WEBHOOK_PENDING`: it is **never** a rollback and **never** a service
update. The deployed revision keeps serving; checkout stays closed because
webhook readiness is not ready (the gauge
`neurostack_billing_webhook_config_ready` stays 0).

| `app` exit | Command verdict | Hook outcome | Owner action |
|-----------|-----------------|--------------|--------------|
| 0 | `ready` | `registered` | none; the gauge usually reads 1 within 300 s (caveat below) |
| 10 | `absent` | `DEPLOYED_WEBHOOK_PENDING` | the webhook was not found after the run; check the ASAAS panel, then re-run |
| 11 | `drift` | `DEPLOYED_WEBHOOK_PENDING` | `requires_recreate` (apiVersion) needs a provider-side decision; otherwise the update did not converge: check the panel, then re-run |
| 12 | `interrupted` | `DEPLOYED_WEBHOOK_PENDING` | the queue was not resumed (`queue_not_resumed`) or did not converge; check the queue in the ASAAS panel, then re-run |
| 13 | `disabled` | `DEPLOYED_WEBHOOK_PENDING` | an operator paused the webhook (`webhook_disabled_by_operator`); the hook never re-enables it: the owner decides (enable it in the panel, then re-run) |
| 20 | `ambiguous` | `DEPLOYED_WEBHOOK_PENDING` | remove the duplicate in the ASAAS panel (nothing is deleted automatically), then re-run |
| 30 | `unconfigured` | `DEPLOYED_WEBHOOK_PENDING` | fix the configuration (`checks`, `settings_invalid:<field>`) in a new reviewed revision |
| 40 | `error` | `DEPLOYED_WEBHOOK_PENDING` | `auth`: check the key; `transient`: re-run later |
| any other, or none | crash, OOM, failed start | `DEPLOYED_WEBHOOK_PENDING` | `cli_exit_code_unknown` or `task_stopped_without_app_exit_code` with `stop_code` |
| 0 with another verdict, or a mismatch | contract violation | `DEPLOYED_WEBHOOK_PENDING` | `exit_code_verdict_mismatch`, `result_line_*`, `result_ready_inconsistent` |

Readiness-gauge caveat: after a `ready` that carries `api_version_unreported`
(ASAAS omitted `apiVersion`, so the command did not compare it), the gauge
`neurostack_billing_webhook_config_ready` can stay 0 until `compare_webhook`
in the readiness refresher gets the same rule. The registration is still
correct; record it with the alert text and do not re-run.

A re-run is a new owner-approved run of the same sequence: start again at
step 1 with a fresh `hook-readback.json` (a file that already records a
RunTask is refused with `run_task_already_recorded`) and pass `--attempt` one
higher than the previous run for the same deployment, so its `clientToken`
differs.

Hook-level reasons that are also `DEPLOYED_WEBHOOK_PENDING`: `run_task_failed`
(RunTask started nothing), `run_task_request_not_rendered_by_hook`,
`task_identity_mismatch` and `task_command_override_mismatch` (alert kind
`evidence_mismatch`), `task_watch_deadline_exceeded`, `log_events_truncated`
and `log_forbidden_material` (rotate per
[`asaas-secret-rotation.md`](./asaas-secret-rotation.md) if the line could
have carried a secret).

Token rotation. ASAAS never returns the token, so an exact webhook is never
rewritten by default. After `ASAAS_WEBHOOK_TOKEN` changes, deploy a revision
that reads the new value (new tasks read the secret at start) and wait for
`COMPLETED`; then run this hook once with
`render-run-task --force-rewrite-token`. Expect `registered` with `action`
`updated` (or `created`) and `token_rewritten` in the command's reasons; a
`ready` with `action: none` under the flag is `force_rewrite_not_applied`.
ASAAS deliveries fail with 401 between that deploy and the run.

### AppConfig and opening financial egress (follow-up F2, not written)

A third AppConfig Agent container would break the evidence validators, which
require exactly the containers `app` and `otel`. The smallest design that
keeps them true reads AppConfig from inside the `app` container:

- **Code:** add an `AppConfigDataFlagReader` to
  `src/infrastructure/appconfig.py`. It uses `appconfigdata`
  `StartConfigurationSession` and `GetLatestConfiguration`, and a
  per-process background refresher modelled on the webhook readiness gate.
  The refresher honours `NextPollIntervalInSeconds` (minimum 15 s). The reader
  returns false for any error, before the first successful read, and when the
  last success is older than a hard bound (for example 3 poll intervals). An
  empty `Configuration` means unchanged. Parsing reuses the current
  duplicate-rejecting flag parser. A setting selects the source (`agent` or
  `sdk`), and the lifespan starts and stops the refresher. `boto3` and
  `botocore` are locked main dependencies (added for the KMS signers).
- **IAM:** a separately named task-role policy that allows
  `appconfig:StartConfigurationSession` and
  `appconfig:GetLatestConfiguration` on the exact
  `arn:aws:appconfig:us-east-1:388460142431:application/<id>/environment/<id>/configuration/<id>`.
- **AppConfig resources:** an application, an environment, a configuration
  profile of type `AWS.AppConfig.FeatureFlags` with a hosted version where
  `financial_egress_enabled.enabled` is false, and a deployment strategy with
  no bake time, so that containment (deploying `false`) is fast.
- **Evidence validators:** the new `APPCONFIG_*` names join the F1 allowlist
  change.
- **Network:** the tasks reach AppConfig through the existing NAT gateway.
  There is no AppConfig VPC endpoint.

An alternative is to run the AWS AppConfig Agent binary inside the `app`
image. That keeps today's loopback client unchanged, but adds a second process
and a new binary to the image supply chain.

### Known blocks and follow-ups

- **F1, the protected deploy path (gate 4): Path A implemented.** Owner
  approval A1 (2026-10-19) chose Path A: register the D3 revision and deploy
  it with the current tooling, outside the protected path, then make one
  minimal reviewed change to the deploy-path validators. That change is in
  `scripts/verify_asaas_producer_image_rollout.py` and
  `scripts/asaas_producer_rollout_evidence.py` (the planner now evaluates the
  same image rule). State of the four problems, plus one found since:
  1. **Name sets: extended.** `validate_task_definition_template` accepts the
     revision 8 `app` names, or those plus exactly the D3 names, all or none.
     Environment: the six base names `ENVIRONMENT`, `HOST`, `LLM_MODEL`,
     `LOG_FORMAT`, `PORT`, `QDRANT_HOST`, plus `ASAAS_WEBHOOK_URL`,
     `ASAAS_CHECKOUT_RETURN_URL`, `ASAAS_WEBHOOK_NOTIFICATION_EMAIL` and
     `BILLING_FACT_WORKER_ENABLED`. Secrets: `DATABASE_URL`, `REDIS_URL`,
     `SECRET_KEY`, `LLM_API_KEY`, `BILLING_OPERATION_RECEIPT_SIGNING_KEY`,
     plus `ASAAS_API_KEY`, `ASAAS_WEBHOOK_TOKEN` and
     `BILLING_OBSERVABILITY_SMOKE_TOKEN`. The D3 environment names and the D3
     secrets must be present together (`task_definition_d3_wiring_partial`).
     Any other name, a missing name, or a secret given as plain environment
     fails (`task_definition_base_environment_names_mismatch`,
     `task_definition_secret_names_mismatch`). `validate_deployment_snapshot`
     accepts the same environment sets before deployment, and the same set
     plus the trust pins and commercial gates after it
     (`task_environment_name_set_mismatch`). It has no secret-name set: the
     sanitized snapshot keeps only `DATABASE_URL`, and the other secret
     references are covered only by the runtime fingerprint. That check runs
     in `compare_rollout_readbacks`, **after** the service update, and a
     mismatch is followed by a rollback: a template that points a D3 secret
     at a different secret passes every pre-deployment check and is rejected
     only then (`readback_runtime_fingerprint_sha256_changed`). A test pins the D3
     names to the manifest and to the planner. Revision 8's names still
     validate on purpose: the planner requires its rollback target to pass
     this validator. A revision 8 template still cannot drop the wiring of a
     serving D3 revision, because the template's environment fingerprints must
     equal the serving snapshot's (`base_task_environment_changed`).
  2. **Image repositories: separated.** The *source* repository is
     `388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging` (the
     hand-built bridge image). The *target* is the attested producer
     repository (`BILLING_PRODUCER_ECR_REPOSITORY_URI`,
     `neurostack-asaas-producer-release-producerimagerepository-jqtdjqst9o1j`,
     equal to `BOOTSTRAP_ECR_URI` in `scripts/asaas_build_release_preflight.py`).
     The template's app image, and the serving (before and cohort) snapshot's
     app image, must be either in the target repository
     (`<target>[:<tag>][@sha256:<digest>]`, exact repository, no prefix match)
     or exactly `neurostack-staging@sha256:<64 hex>` in this account and
     region, with no tag. A serving digest must equal the running tasks'
     digest. The deployed (after) snapshot must run exactly
     `<target>@<attested digest>`, and `build_replacement_task_definition`
     always writes that reference. The target must be an ECR repository URI
     in this account and region whose name follows the ECR grammar (lowercase
     alphanumeric components joined by a single `.`, `_`, `-` or `/`; no
     leading, trailing or doubled separator), and is never the source
     repository. A tag is at most 128 characters (the Docker limit) and a
     digest is lowercase hex. Anything
     else fails closed: `source_image_reference_not_digest_pinned`,
     `app_image_repository_not_allowed`, `app_image_reference_invalid` or
     `target_image_repository_invalid`, prefixed by
     `source_image_repository_mismatch:` (replacement) or
     `task_definition_image_mismatch:` (snapshot). `plan` still reports
     `F1_protected_path_image_repository_mismatch` while the rendering runs a
     tag: render the D3 revision with `app_image_override` set to the bridge
     image digest.
  3. **Runtime fingerprint equality: unchanged.** Path B was not chosen. Under
     Path A the replacement keeps every secret reference of the serving D3
     revision except `DATABASE_URL`: the serving revision has the plain ARN,
     and `build_replacement_task_definition` pins it to
     `<ARN>:<json key>::<VersionId>` (an empty JSON key for a plain ARN).
     The fingerprint binds only the `DATABASE_URL` ARN and JSON key, so that
     difference does not change it (item 5). `plan` still prints
     `F1_path_b_runtime_fingerprint_changes` for the revision 8 to D3
     transition; under Path A it is informational.
  4. **D3 values: fingerprinted.** The four D3 environment values join the six
     base values in the template's and the snapshot's
     `base_environment_sha256`, in the sanitizer's `value_sha256` (the value
     itself stays `<redacted>`) and in the runtime fingerprint. A D3 value
     that differs between the template and the serving revision fails before
     deployment (`base_task_environment_changed`). One that differs between
     the before and after readbacks fails the comparison
     (`readback_runtime_fingerprint_sha256_changed`). `plan` reports
     `d3_environment_values_fingerprinted: true`.
  5. **`DATABASE_URL` normalization: fixed (found and fixed 2026-10-19).**
     A plain secret ARN now fingerprints as `<ARN>:` (empty JSON key), like
     its pinned replacement `<ARN>:::<VersionId>`, so a plain-ARN serving
     revision (8 or D3) round-trips; pinned by
     `test_protected_deploy_round_trips_a_plain_database_url_serving_revision`
     in `tests/scripts/test_verify_asaas_producer_image_rollout.py`.

  **Regenerating the protected template from the deployed D3 revision.** This
  is an owner step on the GitHub side; no agent sets these secrets. Do it after
  Path A has deployed the D3 revision and before the first protected deploy:
  1. Run `plan` and continue only if it shows
     `registered_and_deployed:<ARN>` for the D3 revision, and that revision's
     app image is `neurostack-staging@sha256:<digest>`. A tag is refused by
     the deploy path (item 2).
  2. Read back that exact revision and build the template in a private
     directory, from the repository root. Only the canonical SHA-256 is
     printed:

     ```bash
     workdir="$(mktemp -d)" && chmod 700 "$workdir"
     umask 077
     aws ecs describe-task-definition --region us-east-1 \
       --task-definition neurostack-staging-app:<D3 revision> --include TAGS \
       > "$workdir/taskdef.json"
     python3 - "$workdir/taskdef.json" "$workdir/template.json" <<'PY'
     import json, sys
     from pathlib import Path
     from scripts import asaas_producer_rollout_evidence as evidence
     from scripts import asaas_production_wiring as wiring
     response = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
     template = {
         "schema_version": evidence.TASK_DEFINITION_TEMPLATE_SCHEMA,
         "source_task_definition_arn": response["taskDefinition"]["taskDefinitionArn"],
         "task_definition": wiring._request_from_response(response),
     }
     Path(sys.argv[2]).write_text(
         json.dumps(template, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
         encoding="utf-8",
     )
     print(evidence.canonical_sha256(template))
     PY
     ```

     `_request_from_response` drops the response-only fields and keeps the
     tags, exactly as the planner rendered the revision. The test
     `test_template_regenerated_from_revision_9_validates_clean` builds the
     template the same way.
  3. Validate it offline with `validate-template` and the Group 2 variables,
     as in
     [`asaas-producer-image-rollout-group5.md`](./asaas-producer-image-rollout-group5.md#prepare-the-protected-task-definition-template).
     Expect `task_definition_template: ready`.
  4. Store `ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON` (the file) and
     `ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256` (the printed hash) as
     environment secrets on `asaas-production-deployment`, as that runbook
     describes, then `rm -rf -- "$workdir"`.

  **Path B** (one deployment carrying image and wiring) would still need a
  reviewed exception to the fingerprint equality check for that one
  transition. It was not chosen.
- **F2, AppConfig reader (needed only to open financial egress).** See above.
- **F3, webhook registration entry point.** Resolved by
  `python -m src.ops.asaas_webhook` (see D4 above). The deploy hook that
  launches it after each deployment is separate work.
- **Re-pinning after later deployments.** The manifest and the planner pin
  revision 8 as the D3 source (`task_definition.source_revision`, fixed). The
  rollback target is the separate manifest field `rollback_target`, which the
  planner reads; its only allowed move is 8 to 9 (gate 6). Once a revision
  other than 8 or the D3 rendering serves (for example after the
  signed-image gate), `plan` reports `service_not_on_rollback_target:<ARN>`
  for the D3 step (the reason ID is historical: it names the D3 source). A
  later re-pin is a reviewed change to `ROLLBACK_TARGET_PINS` in the planner
  and to the manifest field.
- **Terraform drift (owner or infra question).** The execution role and the
  task definition family were tagged `ManagedBy=terraform`. Owner approval A4
  (2026-10-19) moves the two ECS roles to `ManagedBy=manual`. Every new
  revision rendered here (app and migrations) carries `ManagedBy=manual`;
  that part is pending owner confirmation (A4 named only the roles).
  Whether a Terraform state still manages inline policies exclusively, or
  the service's task definition, was not verified. Confirm that a later apply
  leaves them alone.

## Frontend build and deployment

Since PRs #90 and #91, the production `Dockerfile` builds the SPA in the
`frontend-build` stage (Node `22.22.3-bookworm-slim` pinned by digest, pnpm
`11.1.2` pinned by sha512 through corepack, `pnpm install --frozen-lockfile
--ignore-scripts --filter "@workspace/neurostack..."`). Only
`dist/public` is copied to `/app/spa`, and the image sets
`SPA_DIST_DIR=/app/spa`. The API serves the SPA at exact routes
(`src/api/spa.py`); `/billing` is the hosted-checkout return page named by
`ASAAS_CHECKOUT_RETURN_URL`. A local `linux/amd64` build of the whole image,
this stage included, and the offline image smoke below passed on 2026-10-19
(not the protected workflow); the browser CSP check is still open.

Network access the image build needs (from the Dockerfile; the 2026-10-19
build succeeded with it, the exact hosts were not captured):

- **Docker Hub** (`registry-1.docker.io`, `auth.docker.io` and its blob CDN):
  `python:3.11.17-slim-trixie@sha256:e88e9763…` (what `python:3.11-slim`
  resolved to on 2026-10-19; tag and digests are recorded in the Dockerfile)
  and `node:22.22.3-bookworm-slim@sha256:e21fc383…`.
- **Debian package mirrors** (`deb.debian.org`): `build-essential`, `curl`,
  `libpq-dev`.
- **PyPI** (`pypi.org`, `files.pythonhosted.org`), every download checked
  against a recorded sha256:
  - `pip==26.2.1`, `setuptools==84.0.0` and `packaging==26.2`, the base
    interpreter's own packages at their `poetry.lock` versions, from
    `scripts/requirements-production-base.txt` (the lock's wheel hashes,
    `pip install --require-hashes --only-binary :all:`).
  - `poetry==2.4.1` and its 48 dependencies, from
    `scripts/requirements-poetry.txt`, after the venv's bundled pip is
    replaced from the base file. Poetry 2.4.1 wrote `poetry.lock` and is the CI
    version. The file is regenerated from `scripts/requirements-poetry.in`
    with uv 0.11.6, outside the build:
    `uv pip compile scripts/requirements-poetry.in --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --generate-hashes --exclude-newer 2026-10-19T00:00:00Z --output-file scripts/requirements-poetry.txt`
    (move `--exclude-newer` forward on purpose when Poetry is upgraded).
  - The locked main dependencies, `boto3` and `botocore` included (the KMS
    receipt and grant signers fail closed without them), installed by Poetry
    from `poetry.lock`.

  Poetry runs from its own venv (`/opt/poetry`) in the `deps` stage and
  installs into the system interpreter, so neither Poetry nor its
  dependencies reach the production image, whose `site-packages` holds
  exactly the locked main group plus the base image's `pip`, `setuptools` and
  `wheel`. The production stage still builds on `base`, so the system
  compilers and headers of `build-essential` and `libpq-dev` (gcc, g++,
  make, ld, as, cpp, `pg_config`; about 353 MB uncompressed) are in the
  image. A compiler-free runtime stage is a separate owner decision.
- **npm registry** (`registry.npmjs.org`):
  - The pnpm tarball, fetched by `corepack prepare` and checked against the
    sha512 in `PNPM_PIN`. The host is corepack's documented default registry
    (overridable with `COREPACK_NPM_REGISTRY`), which would make the URL
    `https://registry.npmjs.org/pnpm/-/pnpm-11.1.2.tgz`. The download
    succeeded in the 2026-10-19 build; its URL was not captured.
  - The frontend packages: `frontend/.npmrc` sets no registry and the lockfile
    has no off-registry tarballs. The lockfile closure of
    `@workspace/neurostack...` is 476 package versions, including
    platform-specific optional binaries that pnpm skips on linux/amd64. The
    earlier measurement of about 412 downloads was not repeated here.
- At task start, not at build time, Fargate also pulls
  `public.ecr.aws/aws-observability/aws-otel-collector:latest` for `otel`.
  That is a mutable tag, and it predates this work.

### Image smoke (local, before the signed build)

The live chain's S5 dry run runs the smoke explicitly, from the reviewed
checkout, on a host with Docker and the build network:

```bash
RUN_DOCKER_IMAGE_SMOKE=1 poetry run pytest -p no:cacheprovider -rs \
  tests/scripts/test_production_image_smoke.py::test_production_image_builds_and_passes_the_smoke
```

or, for the JSON summary on stdout:

```bash
poetry run python scripts/production_image_smoke.py --expected-head 035 --build-log /tmp/build.log
```

It runs `docker buildx build --platform linux/amd64 --load --target production`
on the working tree (no push, no registry login) with `VCS_REF` set to the git
revision (`-dirty` when the tree is modified), then runs each check in the
image, addressed by its image ID with `--pull never` and `--network none`:
`import src.api.main, boto3, botocore`; the interpreter's Python
distributions are exactly the locked main group at the locked versions plus
`pip`, `setuptools` and `wheel` (no Poetry, no Python build backend other
than setuptools and wheel, no duplicate metadata; the system compilers are
not checked and are still present); every top-level import under `src/`
resolves; both KMS
signer factories build their clients (no KMS call); `alembic heads` prints
exactly `035 (head)`; `/app/spa/index.html` exists; and `alembic upgrade
032:035 --sql` emits the migration window as one transaction. The window
starts at `032` because migration `017` reads rows, which offline mode cannot
do. It prints a JSON summary (image ID, build time, image size, warnings,
checks) and exits 0 only when everything passes. Push the image by that image
ID (tag the ID, then push); never rebuild it for the push, or the pushed image
is not the one that was checked. The pytest end-to-end test is opt-in: without
`RUN_DOCKER_IMAGE_SMOKE=1` it is skipped with that reason, so the normal
`tests/scripts` run needs neither Docker nor the network; with it, an unusable
Docker fails the test instead of skipping it. The static Dockerfile and smoke
contract tests always run.

The image carries `org.opencontainers.image.revision` from the `VCS_REF` build
argument (default `unknown`). The protected build workflow passes no build
argument, so its images read `unknown` until that workflow changes.
