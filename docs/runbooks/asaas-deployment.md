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
`CHECK (roster_revision >= 0)` stay until migration 036 (phase E5, not yet
written) drops them. Until then the backend writes the constant `0` and never
reads the column, so a stored value is not evidence. By the owner's plan,
migration 036 will refuse to run while any unexpired readiness receipt
exists. Readiness and every rollout transition take the per-environment
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
   instead of skipping, and runs `alembic upgrade head` on a service
   database. To add or remove a suite, edit the function; CI and this gate
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
```

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
   from the live rollback target `neurostack-staging-app:8` at plan time. It
   keeps everything else byte for byte, including the image reference (unless
   `app_image_override` names an existing ECR digest), the containers `app`
   and `otel` (`otel` keeps an empty environment and no secrets) and the tags.
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
   (`F1_protected_path_image_repository_mismatch`). Before the first
   protected deploy, also resolve F1 residual 5 below. It also needs the build
   network access listed under
   [Frontend build and deployment](#frontend-build-and-deployment).
5. **Gate: migrations 033-035 (separate approval).** In an expand-only
   window, read back the exact `alembic_version` first (it was `032` on
   2026-10-17), continue only if it is still `032`, then run the
   `neurostack-staging-migrations` one-off task on the same image as the
   deployment with the container command overridden to the exact revision
   `alembic upgrade 035`. Never upgrade to the head: once migrations `036`
   and `037` (decouple and drop the legacy objects) are on main, the image
   carries them and the head would apply them in the same run. Each of them
   needs its own explicit owner instruction, 036 before 037, after a fresh
   SELECT showing zero legacy rows. **Verify:** read back `alembic_version`
   == `035`. Never downgrade.
6. **Gate: service deployment (separate approval).** Two paths (see F1
   below):
   - **Path A (cheapest).** If the owner approves, deploy the registered D3
     revision on the current image with the current tooling, outside the
     protected path:
     `aws ecs update-service --cluster neurostack-staging --service neurostack-staging-app --task-definition <new ARN>`.
     The minimal F1 change is implemented (F1 below). Before the first
     signed-image deploy, regenerate the protected template from the deployed
     D3 revision (F1, "Regenerating the protected template").
   - **Path B.** One protected deployment that carries the signed image and
     the wiring together. It needs everything Path A's F1 change needs, plus
     a reviewed exception to the runtime fingerprint equality check.

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

Registration:

- `AsaasWebhookProvisioner.reconcile()` creates or converges the one webhook
  this environment owns: name `neurostack-billing-staging`, the configured
  URL, the closed event set, `SEQUENTIALLY`, API version 3, and the token from
  the secret. `readback()` is its dry run: it reads `GET /v3/webhooks` and
  `GET /v3/webhooks/{id}` and returns `ready` or closed-vocabulary reasons
  (`absent`, `ambiguous`, `url`, `events`, `disabled`, `interrupted`,
  `token_absent`, `token_unconfigured` and so on).
- **Known block F3:** neither method has an operator entry point today. Only
  the in-process readiness refresher calls `readback()`. Follow-up F3 is a
  small operator command, run as a one-off ECS task from the deployed
  revision so that the secrets never reach the operator. Its `--dry-run`
  default prints only the readback verdict. Its `--apply` mode calls
  `reconcile()` once and prints the verdict and a SHA-256 of the webhook ID.
  It needs code review and separate approval for the provider write.
- After the dry run reports `absent` and the owner approves, run `--apply`
  once, then the dry run again.

Read-only verification after registration:

1. The dry run reports `ready` with no reasons.
2. The gauge `neurostack_billing_webhook_config_ready` reads 1 on every worker
   within one refresher period (300 s).
3. `GET /v1/billing/summary` without a JWT still returns 401, so the receiver
   did not open any other path.
4. Optional, inside the deployment window: an unauthenticated
   `POST /v1/billing/webhook/asaas` with body `{}` returns 401 and persists
   nothing. This is the token smoke that `AsaasWebhookProvisioner.smoke()`
   automates.

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
  `sdk`), and the lifespan starts and stops the refresher. `boto3` must become
  a locked main dependency: it is not in `poetry.lock` today.
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
     With today's normalization that difference alone changes the
     fingerprint (item 5). `plan` still prints
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
  5. **Residual, not fixed (found 2026-10-19): `DATABASE_URL` normalization.**
     The template must carry the plain `DATABASE_URL` secret ARN
     (`database_url_source_selector_must_be_default`), and
     `build_replacement_task_definition` pins it to `<ARN>:::<VersionId>`.
     `_task_definition_runtime_fingerprint` normalizes a plain ARN to `<ARN>`
     but a pinned reference to `<ARN>:<json key>` (here `<ARN>:`). A serving
     revision with a plain ARN (revision 8 and the D3 revision) and its
     replacement therefore never have equal fingerprints. The first protected
     deploy would update the service, fail `compare_rollout_readbacks` with
     `readback_runtime_fingerprint_sha256_changed`, and roll back. In
     bootstrap mode that consumes the one-time slot. **Do not start a
     protected deploy until this is decided and fixed** with its own review.

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
- **F3, webhook registration entry point (blocks gate 8).** See D4 above.
- **Re-pinning after later deployments.** The manifest and the planner pin
  revision 8 as the source and rollback target. Once a later revision serves
  (for example after the signed-image gate), `plan` reports
  `service_not_on_rollback_target:<ARN>`. Moving the pin is a reviewed change
  to the manifest and to `ROLLBACK_REVISION` in the planner.
- **Terraform drift (owner or infra question).** The execution role and the
  task definition family are tagged `ManagedBy=terraform`. Whether that
  Terraform manages inline policies exclusively, or the service's task
  definition, was not verified. Mirror the D3 grant and revision in Terraform,
  or confirm that a later apply leaves them alone.

## Frontend build and deployment

Since PRs #90 and #91, the production `Dockerfile` builds the SPA in the
`frontend-build` stage (Node `22.22.3-bookworm-slim` pinned by digest, pnpm
`11.1.2` pinned by sha512 through corepack, `pnpm install --frozen-lockfile
--ignore-scripts --filter "@workspace/neurostack..."`). Only
`dist/public` is copied to `/app/spa`, and the image sets
`SPA_DIST_DIR=/app/spa`. The API serves the SPA at exact routes
(`src/api/spa.py`); `/billing` is the hosted-checkout return page named by
`ASAAS_CHECKOUT_RETURN_URL`. No `docker build` of this stage has been run on a
Docker host yet, and the browser CSP check is still open.

Network access the image build needs (from the Dockerfile; no build was run
here):

- **Docker Hub** (`registry-1.docker.io`, `auth.docker.io` and its blob CDN):
  `python:3.11-slim`, which is pinned by tag only, and
  `node:22.22.3-bookworm-slim@sha256:e21fc383…`.
- **Debian package mirrors** (`deb.debian.org`): `build-essential`, `curl`,
  `libpq-dev`.
- **PyPI** (`pypi.org`, `files.pythonhosted.org`): `poetry==1.8.2` and the
  locked main dependencies.
- **npm registry** (`registry.npmjs.org`):
  - The pnpm tarball, fetched by `corepack prepare` and checked against the
    sha512 in `PNPM_PIN`. The host is corepack's documented default registry
    (overridable with `COREPACK_NPM_REGISTRY`), which would make the URL
    `https://registry.npmjs.org/pnpm/-/pnpm-11.1.2.tgz`. **Unverified:** no
    build was observed.
  - The frontend packages: `frontend/.npmrc` sets no registry and the lockfile
    has no off-registry tarballs. The lockfile closure of
    `@workspace/neurostack...` is 476 package versions, including
    platform-specific optional binaries that pnpm skips on linux/amd64. The
    earlier measurement of about 412 downloads was not repeated here.
- At task start, not at build time, Fargate also pulls
  `public.ecr.aws/aws-observability/aws-otel-collector:latest` for `otel`.
  That is a mutable tag, and it predates this work.
