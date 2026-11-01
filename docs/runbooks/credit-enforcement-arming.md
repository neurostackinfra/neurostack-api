# Credit enforcement arming runbook

> **Nothing in this runbook authorizes arming, accepting a cutoff, a
> deployment or any other live action.** Each step below needs the product
> owner's explicit order at the moment of that step (asaas-rollout-opening
> Decision 7). An authorization given earlier does not cover a later run.
> These tasks run only on the owner's explicit order. **Who** starts the
> RunTask (the owner, or the orchestrator on the owner's order) is an open
> owner decision (see the end of this runbook). Whoever runs it never
> receives a user token.

This runbook covers arming the credit enforcement: step L10 of the
asaas-rollout-opening live sequence (Decisions 6 and 11). The only code path
that can arm is the one-off task `python -m src.ops.credit_enforcement`
(`src/ops/credit_enforcement.py`). No HTTP route, worker, migration or script
reaches `CreditRolloutService.arm_enforcement` or `accept_cutoff`. The fence
in `tests/integration/test_credit_enforcement_rollout_fence.py` enforces this
over the Python sources (AST) and over the shell scripts, Makefile, workflows
and infrastructure files (text).

## What arming does, and why it cannot be undone

`arm` writes `credit_enforcement_state.credit_enforcement_at` once, at the
database clock of its own transaction. No code clears it or moves it, and
nothing may edit that row by hand. From that commit on, every process whose
credit switch is effective enforces the credits:

- a builder with traffic and no coverage (no current subscription binding,
  no reconciled top-up) is served on credit and enters the **72-hour debt**
  lifecycle;
- a **standalone tenant** (no builder) is `credit_pending`, so the agent
  stops answering it;
- Uazapi/Evolution traffic stays unauthenticated: it is never enforced and
  never debited.

**An acceptance is not an exemption** (Decision 11). Accepting a cutoff
finding only lets the arming transaction proceed with that tenant as it is.
After arming, the tenant follows its normal rule above.

Until L10, the credits grant allowance and meter usage. They neither charge
nor block.

## Prerequisites

All of these must hold before `arm`. The owner confirms them for this step:

1. The owner gives the explicit order for this step now.
2. The billing rollout is `general`, and the builders with traffic already
   have subscriptions (Decision 11). Arming earlier sends those builders to
   72-hour debt and standalone tenants to `credit_pending`.
3. The credit switch is effective in the service (L5 is done):
   `CREDIT_HOT_PATH_ENABLED=true` in the service's task definition, and
   `alembic_version` at or after `042`. `status` reports
   `switch.effective: true` with reason `schema_ready`.
4. `status` reports `readiness.ready_after_arm_closures: true` and
   `readiness.orphans_after_arm_closures.open: 0`. `arm` itself first closes
   the orphan effects of **deleted tenants** (`deletion_closures_arm_writes`
   of them) inside its own transaction, and only then evaluates readiness.
   Those effects therefore make `readiness.ready` false and
   `readiness.open_orphans` non-zero in `status` without blocking `arm`.
   Do not repair them by hand. Any other open orphan class blocks `arm` with
   `orphans_open`, and `status` shows it in `orphans_after_arm_closures`.
5. The owner has reviewed `report` tenant by tenant. Each finding is either
   resolved (for example, the builder subscribed) or accepted by the owner,
   with a written rationale in the owner's private record (see "Owner record
   per finding" below).
6. The owner has an **active `super_admin`** account. Its user id is the
   `--author-user-id` of every acceptance.

## Running a step

Each subcommand is one Fargate task on the service's **current** task
definition. That way the task has the service's environment
(`DATABASE_URL`, `CREDIT_HOT_PATH_ENABLED`, ...) and the ECS task metadata
endpoint. Run one task at a time and wait for it to stop before starting the
next.

Work in a private directory (the override file holds the rationale). Set
the three values of the service, in single quotes:

```bash
TASK_DEFINITION='arn:aws:ecs:us-east-1:...:task-definition/...'  # the service's CURRENT one
SUBNETS='subnet-...,subnet-...'          # the service's subnets
SECURITY_GROUPS='sg-...'                 # the service's security groups
```

Then define `run_step` once in that shell. It refuses to call `run-task`
unless `overrides.json` is a well-formed override of this task. Each step
joins its `jq` and `run_step` with `&&`, so a failing `jq` stops the step.
A failed `jq` would leave an empty or partial file behind, which this check
also refuses. (`set -e` is not used: in an interactive shell it would close
the terminal at the first error.) A value left unset stops `jq` with
`parameter null or not set` (`${VAR:?}`):

```bash
run_step() {  # $1: the subcommand; sends ./overrides.json
  jq -e --arg step "${1:?}" '
    (.containerOverrides | length) == 1
    and .containerOverrides[0].name == "app"
    and .containerOverrides[0].command[0:4] == ["python", "-m", "src.ops.credit_enforcement", $step]
    and .containerOverrides[0].environment == [{"name": "DEBUG", "value": "false"}]
  ' overrides.json > /dev/null \
    || { echo "overrides.json is not a valid override for $1" >&2; return 1; }
  aws ecs run-task --region us-east-1 --cluster neurostack-staging \
    --task-definition "${TASK_DEFINITION:?}" \
    --launch-type FARGATE --count 1 \
    --network-configuration "awsvpcConfiguration={subnets=[${SUBNETS:?}],securityGroups=[${SECURITY_GROUPS:?}],assignPublicIp=DISABLED}" \
    --started-by "neurostack-credit-enforcement-$1" \
    --overrides file://overrides.json
}
```

Each step below builds `overrides.json` with `jq -n` and named `--arg`
variables, so a value with an apostrophe, double quotes or a backslash
reaches the task unchanged. `tests/ops/test_credit_enforcement.py` runs these
blocks with a stand-in `aws` and checks the exact strings that arrive.

Every override sets `DEBUG=false` in the task's environment. **`DEBUG` must
not be `true` in the task**: with it, SQLAlchemy's statement echo writes
plain-text lines to stderr (statements only, parameters hidden, no
password). They break the "every stderr line is JSON" rule below.

The result is in the task's log stream (the task definition's `awslogs`
configuration). stdout holds exactly one JSON document. stderr holds that
same document plus `event: credit_enforcement_ops`, `timestamp`,
`duration_ms` and `error_class`, as its last line. Before it, stderr may hold
JSON log lines from the code the task ran (for example `Failed to initialize
database`, with its traceback). Those lines are JSON whatever `LOG_FORMAT`
says, and their tracebacks carry no local variables. The one exception is
`DEBUG=true` (above), which the override prevents. The exit code is the
container's exit code.

Every subcommand first re-reads the credit switch **in its own process**. It
refuses (`switch_off`) unless the switch is effective there. `status` and
`report` run in a read-only transaction and cannot write. `accept` and `arm`
need the task's ARN from the ECS metadata endpoint. Run outside ECS, they
refuse with `task_identity_unavailable`.

### 1. `status` (read-only)

```bash
jq -n '{containerOverrides: [{name: "app",
  environment: [{name: "DEBUG", value: "false"}],
  command: ["python", "-m", "src.ops.credit_enforcement", "status"]}]}' > overrides.json \
  && run_step status
```

Read the output as follows:

- `switch` holds `effective`, `reason` and `revision`.
- `enforcement.armed` should be `false`. `credit_enforcement_at`, `set_by`
  and `set_at` are filled once armed.
- `readiness` holds `ready`, `reconciliation_complete`,
  `readback_complete`, `has_open_gap`, `has_backlog`, `has_alert` and
  `open_orphans`, all as of now. `has_alert` is also true while any orphan
  is open.
- `readiness.orphans` breaks `open_orphans` down by class:
  `decision_without_effect`, `effect_without_decision`,
  `trace_partial_debit`, `orphan_outbox` and `handoff_without_effect`.
- `readiness.deletion_closures_arm_writes` is how many deleted-tenant
  effects `arm` would close in its own transaction.
  `readiness.orphans_after_arm_closures` (same classes) and
  `readiness.ready_after_arm_closures` are what `arm` would then see. They
  are computed by reading only, with the same selection and the same orphan
  and readiness code that `arm` runs. `arm` refuses unless
  `ready_after_arm_closures` is `true` (prerequisite 4).
- `cutoff` holds `digest`, `ready`, `finding_count` and `accepted_count`.

### 2. `report` (read-only)

```bash
jq -n '{containerOverrides: [{name: "app",
  environment: [{name: "DEBUG", value: "false"}],
  command: ["python", "-m", "src.ops.credit_enforcement", "report"]}]}' > overrides.json \
  && run_step report
```

`cutoff.findings` lists one entry per finding, each with `tenant_id`,
`builder_id` (`null` for a standalone tenant), `reason` and `accepted`
(whether an acceptance exists for **this** digest). The reason codes are:

| `reason` | Meaning |
|---|---|
| `standalone` | traffic from a tenant with no builder: `credit_pending` once armed |
| `builder_without_coverage` | the tenant's current builder has no current binding nor a reconciled top-up: 72-hour debt once armed |
| `message_without_observation` | `message.in` events without any observational credit effect |
| `unknown_unauthenticated_traffic` | Uazapi/Evolution or unknown, unauthenticated traffic |

The report covers the current and the previous São Paulo civil month,
evaluated at the database clock. The digest changes whenever the findings
change: new traffic, a new subscription, a tenant re-parented, or the month
turning over. An empty report always has the digest
`4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`.

### 3. `accept` (one finding per task)

This step is only for a finding the owner decides to accept as it is.
Before the task runs, the owner writes the rationale into the private record
(see "Owner record per finding").

**The rationale carries no personal data: no names, e-mail addresses or
phone numbers.** It is printed in the task log. The task refuses a rationale
that contains something shaped like an e-mail address (`usage`, exit 30).

Set the six values first. `DIGEST` is `cutoff.digest` from `report`;
`TENANT`, `BUILDER` (or `none` for a standalone tenant) and `REASON` are
the finding's; `AUTHOR` is the owner's `super_admin` user id. Paste the
rationale with `read -r`, which keeps apostrophes, quotes and backslashes
as typed (one line, at most 1000 characters):

```bash
DIGEST='...'; TENANT='...'; BUILDER='...'; REASON='...'; AUTHOR='...'
read -r RATIONALE
```

```bash
jq -n --arg digest "${DIGEST:?}" --arg tenant "${TENANT:?}" --arg builder "${BUILDER:?}" \
  --arg reason "${REASON:?}" --arg author "${AUTHOR:?}" --arg rationale "${RATIONALE:?}" \
  '{containerOverrides: [{name: "app",
    environment: [{name: "DEBUG", value: "false"}],
    command: ["python", "-m", "src.ops.credit_enforcement", "accept",
      "--report-digest", $digest, "--tenant-id", $tenant,
      "--builder-id", $builder, "--reason-code", $reason,
      "--author-user-id", $author, "--rationale", $rationale]}]}' > overrides.json \
  && run_step accept
```

The task re-evaluates the report at the database clock and refuses in
these cases:

- `report_digest_mismatch`: the report changed. Run `report` again.
- `finding_not_in_report`: the triple is not a finding of the current report.
- `author_not_active_super_admin`.
- `already_armed` (exit 10). The accept transaction takes the same rollout
  lock as `arm` and re-reads `credit_enforcement_at` under it, so no
  acceptance is written once armed, even when `arm` commits while `accept`
  runs.

`--author-user-id` is checked only for being an active `super_admin`. Anyone
allowed to start this task can therefore record an acceptance in the
owner's name. This is an **accepted risk pending the owner's decision**
(O3 below). Its audit trail is the CloudTrail `RunTask` event: who started
the task, with which overrides.

On success (`accepted`), `acceptance` is the row as read back in a fresh
session: `id`, the triple, `report_digest`, `author_user_id`, `accepted_at`
(database clock) and `accepted_on` (São Paulo date). The rationale and the
task ARN appear only in the output and log, because the acceptance has no
column for them. The owner copies the acceptance `id` into the private
record.

Acceptances never expire. An acceptance keeps satisfying `arm` whenever the
same finding set (the same digest) recurs, even long after it was given.
This is pending the owner's decision (O4 below).

### 4. `arm`

`DIGEST` is the `cutoff.digest` the owner reviewed:

```bash
jq -n --arg digest "${DIGEST:?}" \
  '{containerOverrides: [{name: "app",
    environment: [{name: "DEBUG", value: "false"}],
    command: ["python", "-m", "src.ops.credit_enforcement", "arm",
      "--report-digest", $digest]}]}' > overrides.json \
  && run_step arm
```

`arm` calls `arm_enforcement(set_by=<this task's ARN>, at=None,
expected_report_digest=<--report-digest>)`. Under the rollout lock, in one
transaction, it closes deleted-tenant orphans, requires that no other orphan
is open (`orphans_open`) and that readiness holds (`readiness_incomplete`),
and evaluates the cutoff report at the database clock. It refuses when that
report's digest differs from `--report-digest`, even if the new report would
be accepted. It refuses when any finding lacks an acceptance for that exact
triple under that exact digest. Only then does it write
`credit_enforcement_at`.

## Verifying after the commit

1. In the `arm` task's own output: `verdict: armed`, exit code 0, and
   `enforcement` = `{armed: true, credit_enforcement_at, set_by: <the arm
   task's ARN>, set_at}`. The task has already re-read this row in a fresh
   session after its commit. Any difference is `readback_mismatch`.
2. Run `status` as a new task. `enforcement` must be identical, with
   `set_by` equal to the ARN of the `arm` task (check it against the RunTask
   response).
3. Run `arm` again only if the owner asks for proof that it is idempotent.
   It must exit `10` (`already_armed`) and change nothing.

## Verdicts and exit codes

| Verdict | Exit | Meaning and action |
|---|---|---|
| `ok` / `accepted` / `armed` | 0 | the command's success |
| `already_armed` | 10 | enforcement was already armed, and this run changed nothing (for `arm`, also the loser of two concurrent runs; for `accept`, also an arm committed while it ran) |
| `refused` | 20 | a gate refused, and nothing was written. `reasons`: `report_digest_mismatch`, `report_digest_malformed` (the service's own boundary check; the task already refuses a malformed `--report-digest` as `usage`), `cutoff_unaccepted`, `finding_not_in_report`, `author_not_active_super_admin`, `readiness_incomplete`, `orphans_open`, `clock_refused`, `refused_unclassified`. Fix the cause, then re-run `report` / `status` |
| `switch_off` | 21 | the switch is not effective in the task's process (`switch.reason`), and nothing was written. With the configuration off, nothing was read either. With the configuration on, the switch read `alembic_version` (for example `schema_below_minimum`) |
| `unconfigured` | 30 | `usage` (malformed invocation, including an e-mail address in the rationale), `settings_invalid`, or `task_identity_unavailable` |
| `readback_mismatch` | 40 | **the commit happened**, but the row read back differs from what the transaction wrote. Stop, do not retry, run `status` and escalate to the owner |
| `error` | 50 | unexpected failure (`error_class` in the log). With the configuration on and the database unreachable (wrong subnets or security groups in the RunTask), the task ends here, not with `switch_off`: within seconds when the connection is refused, after about 60 s when it hangs. For `arm`/`accept`, run `status`: the commit may have happened before the failure |

## Containment: switch the credits off

There is no disarm. If the enforced behavior must stop, the containment is
the credit switch:

1. Register a task-definition revision of the service that changes only
   `CREDIT_HOT_PATH_ENABLED` to `false`, and deploy it (owner order).
2. With the switch off, every credit surface is inert: no debit, no debt,
   no `credit_pending`, no credit worker tick. Requests are served as before
   the credits. `status` then exits `switch_off`.
3. **Switching the credits off also pauses the grants delivered by the fact
   transport.** The credit consumer of `billing.fact.v1`
   (`CreditFactConsumer.handle` in `src/services/credit_fact_consumer.py`)
   raises `CreditSwitchOff` before it reads or writes anything. No inbox row
   and no ack are written, so no subscription or top-up allowance is
   granted while the switch is off. What happens to the fact then depends
   on the build (see `docs/runbooks/asaas-billing-reconciliation.md`):
   - **Before Group 2c** (an image without Group 2c wires
     `transport=None`, so nothing is presented at all). In a build that presents facts without Group
     2c, `CreditSwitchOff` is a counted failed attempt. After
     `BILLING_FACT_WORKER_MAX_ATTEMPTS` (default 10, about 5 h) the
     delivery is `blocked`, with no ack and the `retry cap exhausted`
     alert. Once the switch is back on, `python -m
     src.ops.billing_fact_requeue` (`plan`, then `requeue`) reopens exactly
     that kind of delivery, with a full attempt budget.
   - **From Group 2c on**, which live step L6 and therefore L10 require, a
     switch-off becomes `TransportNotReady`. No attempt is counted, the
     fact stays `pending` (only the stale alert fires), nothing becomes
     `blocked`, and the fact is delivered once the switch is back on.
     Group 2c's `transport_ready` hook and the translation of
     `CreditSwitchOff` exist (`src/api/main.py`), with unit tests and
     PostgreSQL tests, but they were NOT verified live (O6).
4. `credit_enforcement_at` stays set. Turning the switch back on resumes
   enforcement from that boundary. Usage while the switch is off is not
   metered.

Never edit or delete the `credit_enforcement_state` row, and never insert
`credit_cutoff_acceptances` rows by hand (that table is append-only).

## Owner record per finding

The rationale is written by the owner, not by the operator or an agent.
There is no database column for it. Besides the `accept` task log, its only
durable record is one the owner keeps **privately, outside this
repository**. This repository is public, so no filled-in row belongs here.
Task ARNs contain the AWS account id. Tenant and builder ids and the
rationale text belong only in that private record.

For each accepted finding, the private record holds at least:

| Date (UTC) | Report digest | Reason code | Acceptance id | Owner's written rationale |
|---|---|---|---|---|

It also holds the finding's tenant and builder ids and the `accept` task ARN,
copied from the task's output.

## Open owner decisions

The runbook applies a conservative default to each of these until the owner
decides:

- **O1, who runs the tasks.** Default: they run only on the owner's
  explicit order. Whether the owner or the orchestrator starts the RunTask
  is open.
- **O2, the per-finding record.** Default: kept privately outside this
  public repository. The runbook keeps only a column template without task
  ARNs or tenant/builder ids. Where the private record lives is open.
- **O3, `--author-user-id` is not authenticated.** Anyone allowed to run the
  task can record an acceptance in the owner's name (only "active
  `super_admin`" is checked). Default: accepted risk, with the CloudTrail
  `RunTask` caller as the audit trail.
- **O4, acceptances never expire.** Default: stated, not enforced. An old
  acceptance satisfies `arm` again if the same finding set recurs.
- **O6, containment pauses fact-delivered grants.** Default: stated in
  "Containment" above, for both builds. Still open: whether Group 2c's
  `transport_ready` really turns a switch-off into `TransportNotReady` in a
  live deployment (the hook and its unit and PostgreSQL tests exist since
  Group 2c; it was not verified live). Without it, a long switch-off ends
  in `blocked` deliveries that need the requeue task.
