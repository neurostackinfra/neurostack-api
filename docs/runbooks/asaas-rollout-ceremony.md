# ASAAS billing rollout ceremony (three operators)

> **Nothing in this runbook authorizes a live action.** Every step that
> touches production (each `assign`, `receipt`, `readiness`, `transition`,
> `incident-disable`, `revoke`, and every production read) needs the
> product owner's explicit order **at that moment** (wish
> `asaas-rollout-opening`, Decision 7). An order given earlier does not cover
> a later step. The agent never holds a duty, never receives a credential and
> never runs these commands; three named people do.

This runbook moves the persisted billing rollout of the production-serving
service `disabled -> canary_allowlist -> general`, and back to `disabled`
(rollback or incident), with
[`scripts/asaas-rollout-operator.py`](../../scripts/asaas-rollout-operator.py)
instead of hand-built HTTP. It assumes nothing about the code: every step
says what must already be true (**Precondition**), what to run
(**Command**) and how to know it worked (**Verify**).

Related: [`asaas-deployment.md`](./asaas-deployment.md) (states, canary
budget, readiness digest), [`asaas-rollback.md`](./asaas-rollback.md)
(application rollback), [`asaas-billing-incident.md`](./asaas-billing-incident.md)
(incident handling).

## How the control works (read once)

- The control is one row per environment. The production-serving ECS service
  runs with `ENVIRONMENT=staging`, so the row that matters is `staging`, and
  the tool only ever builds `staging` material.
- Every move is one **operation**: a shared, non-secret *plan* (operation id,
  `from_state`, `to_state`, digests, expiry). Three **distinct**
  `super_admin` accounts each take one **duty** in it: **deployer**,
  **approver**, **reviewer**. The server refuses a second duty for the same
  person (`uq_billing_operation_assignments_actor`) and, at the transition,
  any quorum that is not exactly one receipt per duty from three people.
  Uniqueness is per person, not per duty, so a fourth person can join an
  operation (for example as a second deployer). Measured on PostgreSQL:
  - a fourth **receipt** strands the operation for good: the transition is
    refused (`billing_operation_quorum_incomplete`) and stays refused, and
    the stray receipt's assignment cannot be revoked
    (`billing_operation_assignment_consumed`): write a new plan;
  - a fourth **assignment** with no receipt does not strand it: the
    transition still applies with the three receipts;
  - any `super_admin` can revoke **another** operator's unused assignment
    (HTTP 204), and that person can never assign again in the same
    operation (the unique index stays): the operation needs a new plan.

  The control is therefore: only the three named people assign in an
  operation, and each checks their own assignment before signing.
- Each operator assigns the duty **to themselves** (the server requires the
  assignment's actor to be the credential's own account) and then asks the
  server to sign their **receipt**. **The server signs** and derives actor
  and role; the tool signs nothing and decides no authorization. Every
  refusal is the server's.
- A **transition** spends the three receipts of its plan once. Opening moves
  (`-> canary_allowlist`, `-> general`) only accept receipts signed **after**
  the current state began: never sign an opening quorum ahead of time. A move
  to `disabled` may spend an older, unspent quorum (see
  [Standby containment](#standby-containment)).
- Assignments, receipts and readiness rows expire at the plan's `expires_at`,
  which may be at most `BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS` (default
  24 h) after the **first** assignment of the operation. Finish the whole
  operation before `expires_at`.

## Who creates and who verifies what

| Item | Created by | Verified by |
|------|------------|-------------|
| Plan (one per move) | deployer (`plan`) | approver and reviewer: same `payload_digest` before anyone assigns |
| Assignment (one per duty) | each operator, for themselves (`assign`) | the tool checks the server's echo (role, actor, `payload_digest`); the reviewer checks three distinct `actor_user_id` in the ceremony record |
| Operation receipt (one per duty) | each operator, for their own assignment (`receipt`); the server signs it | the server, at the transition (quorum, signatures, freshness); the reviewer checks three receipts in the record |
| Readiness row `readiness` (general only) | **deployer** of the general plan | the server (binding to the quorum, digests, recorder duty); the approver checks it in the record |
| Readiness rows `backlog`, `provisioning`, `smoke` (general only) | **reviewer** of the general plan | the server; the approver checks them in the record |
| Transition / incident-disable | deployer (any super_admin may submit; the server takes the actor from the quorum) | reviewer and approver, each with their own `readback` |

The approver records nothing in readiness; a fourth super_admin cannot
record readiness either (the server refuses both).

## Credentials

Two credential kinds exist, and the **server** decides which route takes
which (measured in
`tests/scripts/test_asaas_rollout_operator.py::test_the_server_not_the_cli_decides_the_token_kind`):

| Kind | Issued by | Lifetime | Accepted by | Refused (HTTP 401 `unauthorized`) by |
|------|-----------|----------|-------------|-------------------------------------|
| Login JWT | `POST /v1/auth/login` | 24 h | every mutating step (`assign`, `receipt`, `readiness`, `transition`, `incident-disable`, `revoke`) | `readback` |
| Staging rehearsal token | `POST /v1/auth/staging-rehearsal-token` (answers only under `ENVIRONMENT=staging`; at most 5 per account and 30 in total per 15-minute window) | 10 min | `readback` | every mutating step |

Rules, for each operator:

- Use **your own** account only. Never share, paste into chat, screenshot or
  write a credential to a file, ticket, wish or evidence.
- The tool reads a credential **only** from an inherited descriptor:
  `--auth-fd 3 3< <(printf '%s' "$NS_JWT")`. It refuses descriptors 0-2, a
  regular file and a terminal, and closes the descriptor after reading.
  Never use a here-string (`3<<<`): some shells implement it as a temporary
  file (the tool then refuses it). There is no argument or environment
  variable that takes a token.
- Never `export` the variables, never run with `set -x` (it prints expanded
  commands, token included), and `unset NS_JWT NS_READBACK_JWT` when done.
- The code has no server-side revocation of an issued JWT (its `jti` is not
  checked against a denylist): a login JWT stays valid until it expires.
  Obtain it right before the ceremony.

**Obtaining a credential without exposing it.** In your own interactive
`bash`, define this function once per shell (the password is read without
echo, goes to `curl` on standard input, and the token goes into a shell
variable; nothing touches argv or disk):

```bash
ns_token() {  # usage: ns_token login | ns_token readback
  local path var email password
  case "$1" in
    login) path=/v1/auth/login; var=NS_JWT ;;
    readback) path=/v1/auth/staging-rehearsal-token; var=NS_READBACK_JWT ;;
    *) echo "usage: ns_token login|readback" >&2; return 2 ;;
  esac
  read -r -p 'NeuroStack e-mail: ' email
  read -r -s -p 'Password: ' password; echo
  printf -v "$var" '%s' "$(
    printf '%s\0%s' "$email" "$password" |
      python3 -c 'import json,sys; e,p=sys.stdin.buffer.read().split(b"\0",1); sys.stdout.write(json.dumps({"email":e.decode(),"password":p.decode()}))' |
      curl --silent --show-error --fail --proto '=https' --noproxy '*' \
        -H 'Content-Type: application/json' --data-binary @- \
        "https://staging.api.neurostack.global$path" |
      python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["token"])'
  )"
  unset password
}
```

Run it directly at the prompt (`ns_token login`, then `ns_token readback`
when a readback is due), never at the end of a pipeline (`... | ns_token`):
a pipeline runs it in a subshell and the variable is lost. Obtaining a
credential is the only HTTP built by hand in this runbook; it moves nothing.

**Verify** (no network): `whoami` prints your user id, role and token kind,
never the token:

```bash
python3 "$OP" whoami --auth-fd 3 3< <(printf '%s' "$NS_JWT")   # OP: see Preconditions
# expect "role": "super_admin", "token_kind": "login_jwt", "expired": false;
# "sub" is your user id: it is your --actor-user-id
```

The claims are read, not verified; the server verifies.

## The tool

Run it from a checkout of this repository at the commit all three operators
agreed on (it is not in the image), with Python 3.11 or newer and nothing
else installed: `python3 "$OP" <command> ...`, where `OP` is the tool's
path in that checkout (set in [Preconditions](#preconditions-once-per-ceremony)).
It talks only to `https://staging.api.neurostack.global` (fixed; TLS
verified; no proxy; no redirect). A network that requires an HTTP proxy
cannot be used.

| Command | Request | Credential |
|---------|---------|------------|
| `plan` | none: writes the plan JSON to stdout | none |
| `whoami` | none | either kind |
| `assign --plan P --role R --actor-user-id U` | `POST /v1/admin/billing/operations/assignments` | login JWT |
| `receipt --plan P --assignment-id A --nonce N` | `POST /v1/admin/billing/operations/receipts` | login JWT |
| `readiness --plan P --kind K --rollout-revision V --deployer-receipt-id D --reviewer-receipt-id E --rule-digest H --bundle-digest B --observed-at T` | `POST /v1/admin/billing/rollout/receipts` | login JWT |
| `transition --plan P --expected-revision V` | `POST /v1/admin/billing/rollout/transitions` | login JWT |
| `incident-disable --plan P --expected-revision V` | `POST /v1/admin/billing/rollout/incident-disable` | login JWT |
| `revoke --assignment-id A` | `DELETE /v1/admin/billing/operations/assignments/{A}` | login JWT |
| `readback` | `GET /v1/admin/billing/rollout/readback` | rehearsal token |

Every command except `plan` and `whoami` takes **exactly one** of
`--auth-fd N` or `--dry-run`. Every mutating command also takes
`--journal FILE` (required to send).

- **`--dry-run`** prints the exact method, URL, headers (the
  `Authorization` value is a placeholder) and body that would be sent, the
  `request_digest`, and whether your journal already holds the step. It
  reads no credential, sends nothing and writes nothing. Run it before every
  real step.
- **Plan.** The plan holds only ids, states and digests. `plan` computes the
  canary `budget_digest` from the builder (the server's
  `canary_budget_digest`); every other move signs the fixed value
  `NO_CANARY_BUDGET_DIGEST` (SHA-256 of
  `neurostack.billing.rollout.no_canary_budget.v1`), which the server does
  not interpret. `plan` refuses a move the server never accepts (for example
  `disabled -> general`). Every command refuses a plan that was edited.
- **Journal.** One file per person (`$JOURNAL`, set in
  [Preconditions](#preconditions-once-per-ceremony)), used for the whole
  ceremony; the tool creates it and its `.lock` file with mode `0600`. It
  holds request digests and the server's answers (ids, digests, states),
  never a credential. Repeating a step that was applied prints the
  recorded answer and sends nothing (`"outcome": "replayed"`): **every step
  can be repeated safely**. A step whose outcome was unknown is sent again
  on repeat (the server applies it at most once). A refused step is not
  recorded, unless an earlier attempt of it had an unknown outcome: then
  its entry stays `unknown` (the refusal may mean that attempt landed; see
  [Recovery](#recovery)). Changed inputs are a new request. Before reading
  the credential, the tool refuses a journal that is in use by another
  invocation, damaged, or whose entry does not match its request digest,
  and it re-checks a recorded answer exactly as it checks a live one, with
  the HTTP status. That check is only as strong as what the tool can know:
  `assign` (role, actor, operation, expiry, `payload_digest`), `readiness`
  (kind, revision, `content_sha256`, receipt ids), `transition` and
  `incident-disable` (state and revision) are compared with the request;
  for `receipt` the server derives `role` and `actor_user_id` itself, so
  the tool checks only their shape (a known duty, a UUID) and the
  operation and nonce. The journal is not signed: an edit that still looks
  like a valid answer (for example another well-formed `assignment_id`, or
  another duty or actor in a recorded receipt) is replayed with exit 0, so
  keep the journal private and compare it with the ceremony record.
- **Output.** One JSON object on stdout, short lines on stderr. Paste the
  stdout of every step into the ceremony record. Server refusals are printed
  as the server's fixed code (for example
  `refused: HTTP 409 billing_operation_conflict`) with a hint; a validation
  error lists the field names, never the values. Server text is printed
  only when it is a known server code or message (the codes of
  [Refusals](#refusals) and the fixed authentication messages); anything
  else is printed as `(server text withheld)` with the HTTP status, because
  it could hold part of a credential.

| Exit | `outcome` | Meaning | What to do |
|------|-----------|---------|------------|
| 0 | `applied`, `replayed`, `read`, `dry_run` | done | continue |
| 2 | (stderr `blocked:`) | refused locally; nothing was sent | fix the input |
| 3 | `refused` | the server refused; nothing was applied by this attempt | see [Refusals](#refusals) |
| 4 | `unknown` | no usable answer (network, 5xx, an answer that does not match the request) | repeat the same command; for a transition, `readback` first |

## The ceremony record

One shared, non-secret record for the ceremony (for example the wish's
evidence file): the owner's order for each step (who, when), the plan
files, and the stdout JSON of every step, in order. It contains only ids,
states, digests and UTC times. It never contains a credential, an e-mail
address or a screenshot.

## Preconditions (once per ceremony)

1. **Owner order** for this ceremony, and the three people named: who is
   deployer, approver and reviewer (Decision 2: three distinct `super_admin`
   accounts, confirmed by the owner; the live phase starts with a
   read-only check of those accounts).
2. Each operator: a checkout at the agreed commit, `python3 --version` is
   3.11 or newer, a synced clock (`timedatectl` shows
   `System clock synchronized: yes`, or the macOS equivalent), and, in the
   shell used for the whole ceremony, a private directory for the journal
   and plans:

   ```bash
   umask 077
   CHECKOUT="$HOME/neurostack-api"   # placeholder: YOUR checkout of the agreed commit
   OP="$CHECKOUT/scripts/asaas-rollout-operator.py"
   test -f "$OP" || echo "no tool at $OP: set CHECKOUT to your checkout" >&2
   mkdir -p "$HOME/.asaas-ceremony" && chmod 700 "$HOME/.asaas-ceremony"
   cd "$HOME/.asaas-ceremony"   # plans are written and read here
   JOURNAL="$HOME/.asaas-ceremony/journal.json"   # yours, for every step
   ROLE=deployer   # your duty: deployer, approver or reviewer
   ```

   `CHECKOUT` is a placeholder: replace it with the path of your own
   checkout of the agreed commit before running the block. Every command in
   this runbook runs the tool as `python3 "$OP" ...` from this directory.
   `--journal` must name a file (an empty value is a usage error, exit 2).
3. Each operator: `ns_token login`, then `whoami` shows their own user id,
   `super_admin`, `login_jwt`, not expired. Set `ME` to that `sub`
   (`ME=<the sub printed by whoami>`; a user id is not a secret). They
   write their user id in the ceremony record; the reviewer checks the three
   are distinct.
4. The **deployment digest** the quorum will sign: the 64 hex characters of
   the app image digest the service runs (the `sha256:` value of the
   serving revision's app container, from its deploy record in
   [`asaas-deployment.md`](./asaas-deployment.md), without the `sha256:`
   prefix). The server compares it only with what the operators signed, not
   with the running build (`src/services/billing_rollout.py`, "NOT
   IMPLEMENTED").
5. Current state: one operator runs [`readback`](#readback) and records
   `state` and `rollout_revision`. Each operator sets `R` to that
   `rollout_revision` (`R=<rollout_revision>`) and updates it after every
   applied move (`R+1`).

## Readback

**Precondition:** a rehearsal token (`ns_token readback`; it lives
10 minutes).

**Command:**

```bash
python3 "$OP" readback --auth-fd 3 3< <(printf '%s' "$NS_READBACK_JWT")
```

**Verify:** exit 0, `"outcome": "read"`; `response` holds `environment`
`staging`, `state`, `rollout_revision`, `topup_commercial_enabled` and
`topup_revision`. A `refused` 401 means the wrong token kind or an expired
token; a 404 `billing_rollout_readback_unavailable` means the service does
not run with `ENVIRONMENT=staging`: stop.

**If the rehearsal token cannot be issued.** The issuer needs Redis; it
answers HTTP 503 `issuer_unavailable` when it cannot count, and 429
`rate_limited` past its limits (5 per account, 30 in total, per 15
minutes). Readback is evidence, not a gate you may skip silently: without
it, record in the ceremony record that readback was unavailable (the
issuer's status and code, UTC time) and use instead (a) the transition's
own answer (`response.state` and `response.rollout_revision`, checked by
the tool against the plan's `to_state` and `expected_revision + 1`), and
(b) a SELECT-only read of the `staging` row of `billing_rollout_control`
(`state`, `rollout_revision`), a production read with its own owner
order. An opening move (`-> canary_allowlist`, `-> general`) does not
proceed on (a) alone; wait for the issuer or get (b).

## Procedure A: `disabled -> canary_allowlist`

`canary_allowlist` is a real-money state (see [The canary](#the-canary)).

**A1. Deployer writes the plan.**
- *Precondition:* readback shows `state` `disabled` at revision `R`; the
  owner named the canary builder (`CANARY_BUILDER_ID`, an existing builder;
  if the canary was ever open before, it must be the same builder, see
  [The canary](#the-canary)).
- *Command:* (`--expires-at`: an explicit UTC time 2 to 3 hours ahead, at
  most 24 h after the first assignment)

  ```bash
  python3 "$OP" plan --scope rollout \
    --from-state disabled --to-state canary_allowlist \
    --canary-builder-id "$CANARY_BUILDER_ID" \
    --deployment-digest "$DEPLOYMENT_DIGEST" \
    --expires-at 2026-11-02T15:00:00Z > canary-plan.json
  ```

- *Verify:* exit 0; stderr prints `payload_digest <64 hex>`. The deployer
  puts `canary-plan.json` in the ceremony record.

**A2. Approver and reviewer take the plan.**
- *Precondition:* A1 done.
- *Command:* save the file byte for byte as `canary-plan.json`, then
  `python3 "$OP" transition --plan canary-plan.json --expected-revision R --dry-run`.
- *Verify:* exit 0 (an edited plan is refused); `payload_digest` equals
  the deployer's; `would_send.body.canary_builder_id` is the builder the
  owner named. Each says so in the record. Anyone who sees a different
  digest stops the ceremony.

**A3. Each operator assigns their own duty** (in any order).
- *Precondition:* A2 confirmed by all three; owner order for this step.
- *Command* (`ROLE` is your duty, `ME` your user id from `whoami`):

  ```bash
  python3 "$OP" assign --plan canary-plan.json \
    --role "$ROLE" --actor-user-id "$ME" --journal "$JOURNAL" --dry-run
  python3 "$OP" assign --plan canary-plan.json \
    --role "$ROLE" --actor-user-id "$ME" --journal "$JOURNAL" \
    --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

- *Verify:* exit 0, `"outcome": "applied"`; `response.role` is your duty,
  `response.actor_user_id` is you, `response.payload_digest` equals the
  plan's, `response.active` is `true`. Note `response.assignment_id`.
  `refused: HTTP 409 billing_operation_conflict` here means you already hold
  a duty in this operation: one person, one duty.

**A4. Each operator gets their receipt signed.**
- *Precondition:* your A3 applied.
- *Command* (a new nonce, once; keep it for repeats):

  ```bash
  NONCE="$(python3 -c 'import uuid; print(uuid.uuid4())')"
  python3 "$OP" receipt --plan canary-plan.json \
    --assignment-id "$ASSIGNMENT_ID" --nonce "$NONCE" --journal "$JOURNAL" \
    --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

- *Verify:* exit 0; `response.role` is your duty, `response.applied` is
  `false` (not yet spent). The reviewer checks the record: three receipts,
  roles deployer, approver and reviewer, three distinct `actor_user_id`.

**A5. Deployer applies the move.**
- *Precondition:* the three receipts of A4 in the record; readback still
  shows `disabled` at `R`; owner order for this step.
- *Command:* the dry-run, then:

  ```bash
  python3 "$OP" transition --plan canary-plan.json \
    --expected-revision "$R" --journal "$JOURNAL" \
    --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

- *Verify:* exit 0; `response.state` `canary_allowlist`,
  `response.rollout_revision` `R+1`.

**A6. Reviewer and approver verify independently.**
- *Command:* each runs [`readback`](#readback) with their own rehearsal
  token.
- *Verify:* `state` `canary_allowlist`, `rollout_revision` `R+1`,
  `topup_commercial_enabled` `false`. Then, only if the owner ordered it
  (an open owner decision, see
  [Decision record: containment](#decision-record-containment-open-owner-decision)),
  sign the [standby containment](#standby-containment) quorum.

## The canary

- **Builder.** One builder, named by the owner, signed into the plan: the
  canary `budget_digest` is the SHA-256 of
  `{"canary_amount_limit_cents":109900,"canary_builder_id":"<uuid>","canary_checkout_limit":2,"canary_settlement_limit":2,"environment":"staging"}`
  (keys sorted, no whitespace), so nobody holding the quorum can open the
  canary for another builder (`billing_rollout_canary_budget_unbound`).
- **Budget** (fixed by the schema, not configurable): at most 2 checkouts,
  2 settlements and 109,900 cents (R$ 1.099,00) over the canary's
  **lifetime**: one Plus monthly subscription (`plan_plus_monthly_v1`,
  R$ 999,00, no coupon; any other offer or a coupon is refused) and one
  R$ 100,00 top-up. The slots are never reset by a rollback: re-opening the
  canary later must name the same builder once a slot is used (otherwise the
  transition is refused as `billing_operation_conflict`).
- **What the owner observes at the first payment**, in order:
  1. Before the builder pays: `readback` shows `canary_allowlist` at the
     revision of A5 and `topup_commercial_enabled` `false`.
  2. In the ASAAS panel (the owner's own account): exactly one new charge,
     R$ 999,00, for the canary builder's customer.
  3. After ASAAS confirms the payment: in
     `GET /v1/admin/builders/subscriptions` (admin panel) the canary
     builder shows `subscription_status` `active` and `subscription_plan`
     `plus` (set at settlement), and no other builder changed.
  4. A SELECT-only read (a production read with its own owner order) of the
     `staging` row of `billing_rollout_control`: `canary_checkout_count`
     `1`, `canary_amount_reserved_cents` `99900`,
     `canary_subscription_checkout_id` set, `canary_settlement_count` `1`
     once settled, `canary_topup_checkout_id` empty unless the R$ 100,00
     top-up was bought; and no open readback (the states listed for the
     `backlog` check in B5).
  5. Credits: the plan's franchise appears only if the credit hot path is
     on (`CREDIT_HOT_PATH_ENABLED`, wish Decision 12); otherwise no credit
     is granted yet.

  Do not take a silent `BillingReadbackBacklog` alert as evidence: the only
  writer of its gauge, `BillingWebhookService.reconcile_backlog`, has no
  production caller today, so the alert cannot fire.

  **Stop and contain** (incident-disable with the standby quorum if the
  owner ordered one and it is unspent and unexpired, otherwise
  [Containment without a quorum](#containment-without-a-quorum) followed by
  a quorum to `disabled`; then
  [`asaas-billing-incident.md`](./asaas-billing-incident.md)) on any other
  amount, a second charge, a charge for another customer, a subscription
  that does not activate, or an open readback. Never retry or refund from
  this runbook; refunds are manual provider operations with their own
  approval.

## Procedure B: `canary_allowlist -> general`

**B0. Preconditions** (all, recorded):
- the owner's order to open `general`, after observing the canary;
- readback shows `canary_allowlist` at revision `R`;
- the deployed image can compute the billing fact bundle digest. Issue #137
  (wish Decision 3; before the fix `billing_fact_bundle_digest()` read
  `tests/fixtures/contracts/billing_fact_v1/SHA256SUMS`, which the runtime
  image does not ship, so readiness and the `general` transition answered
  HTTP 500, `unknown`) is fixed by PR #162, commit `57f8d30d` on `main`. The
  deployed image must contain that commit; at the time of writing no
  deployed image does. Check in a checkout of the deployed commit that
  `git merge-base --is-ancestor 57f8d30d HEAD` succeeds and that
  `src/contracts/billing_fact_v1/SHA256SUMS` exists.
- the two evidence digests, computed by the reviewer in a checkout of the
  **deployed** commit and written in the record (nothing beyond the
  checkout and the standard tools is needed):

  ```bash
  DEPLOYED_CHECKOUT="$HOME/neurostack-deployed"   # placeholder: a checkout of the DEPLOYED commit
  cd "$DEPLOYED_CHECKOUT"
  git merge-base --is-ancestor 57f8d30d HEAD && test -f src/contracts/billing_fact_v1/SHA256SUMS
  RULE_DIGEST="$(sha256sum config/prometheus_rules.yml | cut -d' ' -f1)"
  BUNDLE_DIGEST="$(sha256sum src/contracts/billing_fact_v1/SHA256SUMS | cut -d' ' -f1)"
  cd "$HOME/.asaas-ceremony"
  ```

  (`shasum -a 256` where `sha256sum` is missing.) `BUNDLE_DIGEST` is the
  value `billing_fact_bundle_digest()` returns (the SHA-256 of the manifest
  bytes); at `57f8d30d` it starts `af630b3e`. The server recomputes both
  from its own files and refuses any other value
  (`billing_rollout_readiness_evidence_invalid`).

**B1. Deployer writes the plan;** **B2.** approver and reviewer confirm it;
**B3.** each operator assigns their duty; **B4.** each gets their receipt
signed. Exactly as A1 to A4, with:

```bash
python3 "$OP" plan --scope rollout \
  --from-state canary_allowlist --to-state general \
  --deployment-digest "$DEPLOYMENT_DIGEST" --expires-at <UTC, 2-3 h ahead> > general-plan.json
```

Sign this quorum only now, after the canary state exists; a quorum signed
before an intervening change of state is refused
(`billing_rollout_quorum_stale`). The deployer and the reviewer write their
`receipt_id` in the record (`DEPLOYER_RECEIPT_ID`, `REVIEWER_RECEIPT_ID`):
every readiness row names both.

**B5. The checks.** After B4, and before `expires_at`:
- deployer: the deployed application is ready (`GET /ready` answers 200 on
  every task of the serving revision);
- reviewer, `provisioning`: the ASAAS webhook `readback` verdict is
  `ready`; `smoke`: the webhook `smoke` check passes. Both run with the
  existing tooling of [`asaas-deployment.md`](./asaas-deployment.md)
  (`python -m src.ops.asaas_webhook readback` / `smoke`, one-off tasks of
  the service's task definition).
- reviewer, `backlog`: no open readback, by a SELECT-only read of the
  states `BillingWebhookService.reconcile_backlog` sweeps: no
  `billing_webhook_deliveries` row in `readback_required` or `unknown` (with
  a resource kind), no `billing_subscriptions` row in `readback_required` or
  `non_billable_pending_readback`, no `billing_checkout_sessions` row in
  `terminal_pending_readback` or `paid_pending_subscription`, and no
  orphaned `creating` or `reconciliation_required` checkout. Orphaned means
  still in that state and created (`created_at`) more than
  `ORPHANED_CREATING_AFTER` ago: 5 minutes (`src/services/billing_checkout.py`,
  the same cut-off the sweep in `src/services/billing_webhook.py` uses); such
  an attempt belongs to a crashed request. The
  `BillingReadbackBacklog` alert is not evidence (its gauge has no
  production writer today).
- Each of these is a production read with its own owner order.
- Each notes the UTC time the check passed: that is `--observed-at`. It
  must be after both receipts of B4 were created and not in the future for
  the server: keep at least two minutes after B4, and record it a minute or
  more after the observation.

**B6. Deployer records `readiness`; reviewer records `backlog`,
`provisioning` and `smoke`** (one command per kind, each with the recorder's
own credential and journal):

- *Precondition:* B5 passed for that kind; readback still shows
  `canary_allowlist` at `R`.
- *Command:*

  ```bash
  python3 "$OP" readiness --plan general-plan.json \
    --kind "$KIND" --rollout-revision "$R" \
    --deployer-receipt-id "$DEPLOYER_RECEIPT_ID" \
    --reviewer-receipt-id "$REVIEWER_RECEIPT_ID" \
    --rule-digest "$RULE_DIGEST" --bundle-digest "$BUNDLE_DIGEST" \
    --observed-at "$OBSERVED_AT" --journal "$JOURNAL" \
    --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

  The tool computes `content_sha256` (the nine-key digest documented in
  [`asaas-deployment.md`](./asaas-deployment.md#readiness-receipt-content-digest))
  and sets `app_digest` to the plan's deployment digest.
- *Verify:* exit 0; `response.receipt_kind` is the kind,
  `response.rollout_revision` is `R`. A
  `billing_rollout_readiness_recorder_invalid` means the wrong duty recorded
  it.

**B7. Approver checks the four rows** in the record: kinds `readiness`
(deployer), `backlog`, `provisioning`, `smoke` (reviewer), all at revision
`R`, all naming the same two receipt ids.

**B8. Deployer applies `general`.**
- *Precondition:* B7 confirmed; owner order; before `expires_at`.
- *Command:* `transition --plan general-plan.json --expected-revision "$R"`
  (dry-run first, then with `--journal` and `--auth-fd`, as in A5).
- *Verify:* `response.state` `general`, `rollout_revision` `R+1`; then A6
  (two independent readbacks). A
  [standby containment](#standby-containment) quorum signed after A6 still
  works from `general`; renew it if its `expires_at` is near.

## Rollback and containment

Two ways back to `disabled`, both a quorum and one call, plus one way to
stop new charges without any quorum
([Containment without a quorum](#containment-without-a-quorum)):

- **Rollback** (rollout scope): a planned return, `canary_allowlist ->
  disabled` or `general -> disabled`. The plan's `from_state` must be the
  current state (`billing_rollout_from_state_mismatch` otherwise).

  ```bash
  python3 "$OP" plan --scope rollout \
    --from-state "$CURRENT_STATE" --to-state disabled \
    --deployment-digest "$DEPLOYMENT_DIGEST" --expires-at <UTC> > rollback-plan.json
  ```

  Then A2 to A4 with that plan, and `transition --plan rollback-plan.json
  --expected-revision <current>`; verify `state` `disabled` by two
  readbacks.
- **Incident** (incident scope): containment.
  `plan --scope incident --from-state "$CURRENT_STATE" --to-state disabled ...`,
  A2 to A4, then:

  ```bash
  python3 "$OP" incident-disable \
    --plan incident-plan.json --expected-revision "$CURRENT_REVISION" \
    --journal "$JOURNAL" --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

  Verify `state` `disabled`; continue with
  [`asaas-billing-incident.md`](./asaas-billing-incident.md).

Neither cancels a checkout already created at ASAAS, and a payment already
made still settles; see [`asaas-rollback.md`](./asaas-rollback.md). The
application rollback (redeploying an image) is a separate procedure there.

### Standby containment

A move to `disabled` does not require a fresh quorum, its receipts do not
sign the rollout revision, and the incident route does not check the plan's
`from_state`. So right after A6 the three operators sign an **incident**
quorum in advance (plan with `--scope incident --from-state canary_allowlist
--to-state disabled`, then A2 to A4) and leave it unspent. Containment then
takes one call by any operator: `incident-disable --plan incident-plan.json
--expected-revision <current revision from readback>`. It stays usable from
`canary_allowlist` and, after B8, from `general`, until the plan's
`expires_at` (at most 24 h after its first assignment): before that, sign a
new one. An incident quorum can only ever disable. (Measured: the PostgreSQL
test `tests/scripts/test_asaas_rollout_operator.py::test_postgres_ceremony_runs_end_to_end_through_the_cli`
signs it in `canary_allowlist`, promotes to `general`, then contains with
it.)

A standby quorum lets **any single `super_admin`** disable the rollout, on
their own, until it expires: the server takes the actor from the quorum,
not from who submits. Whether to keep one is an **open owner decision**
(see [Decision record: containment](#decision-record-containment-open-owner-decision));
sign it only on the owner's order. If the standby is spent or expired, or a
quorum cannot be gathered in time, use
[Containment without a quorum](#containment-without-a-quorum).

### Containment without a quorum

Closes financial egress (new provider charges) by owner order, with no
operator quorum. It does **not** change the persisted rollout state.

- *Precondition:* the owner's order to close financial egress; the
  AppConfig resources of step L7 exist and the service reads the flag at
  runtime with the F2 reader (wish `asaas-rollout-opening` Group 4, PR #163
  "feat(billing): read the financial egress flag from AppConfig at runtime
  (F2)"; see the section "AppConfig and opening financial egress" of
  [`asaas-deployment.md`](./asaas-deployment.md#appconfig-and-opening-financial-egress-follow-up-f2-not-written)).
  This lever exists only once step L7 has created those resources; before
  L7 there is no AppConfig profile and financial egress already reads
  closed.
- *Command:* deploy, in AWS AppConfig, a new hosted version of the
  egress-flags profile where `financial_egress_enabled` has
  `"enabled": false` (keep `uazapi_egress_enabled` at its current value),
  with the profile's no-bake deployment strategy, as described in that
  section. Whoever holds the AWS permission does it on the owner's order;
  this tool takes no part.
- *Verify:* each task's reader normally closes the flag at its next poll,
  within one poll interval (at most 60 s) of the deployment; a failed poll
  closes it at once; 180 s (3 x 60 s) is the maximum, for a refresher whose
  polls have stalled. Each task logs
  `appconfig_egress_flags_changed` with `financial_egress_enabled` false.
  A new checkout then fails with `financial_egress_disabled` before any
  provider POST. A checkout already created at ASAAS is not cancelled and a
  payment already made still settles (see
  [`asaas-rollback.md`](./asaas-rollback.md)).
- *Then:* `readback` still shows the old `state` (for example
  `canary_allowlist`): the persisted rollout did not move. As soon as three
  operators can sign, follow with a rollback or incident quorum to
  `disabled` (above), and record both steps in the ceremony record.

`CREDIT_HOT_PATH_ENABLED` does not close financial egress; it is not a
containment lever.

## Recovery

- **Exit 4 (`unknown`).** Repeat the same command. The journal sends it
  again; the server applies each step at most once. If the repeat is
  refused with `"earlier_attempt_unknown": true`, the earlier attempt
  probably landed. What the server answers to an applied step sent again
  (measured by the PostgreSQL test of this tool):
  - `assign`: `billing_operation_conflict`. Your assignment exists but its
    id is lost (no route reads assignments back). Stop this operation:
    every operator abandons it (assignments expire at `expires_at`;
    `revoke` the ones whose id you have) and the deployer writes a new plan.
  - `receipt`: `billing_operation_assignment_invalid` (the receipt consumed
    the assignment; each assignment signs once). Your receipt exists. For
    canary, rollback and incident plans continue: if it did not land after
    all, the transition answers `billing_operation_quorum_incomplete` and
    spends nothing. For the `general` plan readiness needs the deployer's
    and the reviewer's receipt ids, so a lost one means a new plan.
  - `readiness`: `billing_rollout_readiness_duplicate`. The row exists;
    continue.
  - `transition` or `incident-disable`: `readback` first; `state` equal to
    the plan's `to_state` at `expected_revision + 1` means it landed. (A
    re-sent canary opening or rollback answers
    `billing_rollout_from_state_mismatch`, a re-sent `general`
    `billing_rollout_readiness_unbound`, a re-sent incident-disable
    `billing_operation_receipt_invalid`.)
- **Exit 4 with `"outcome": "applied"` and `"journal": "not_updated"`:**
  the step was applied but your journal could not be written. Paste the
  output in the ceremony record; it is the record of that step.
- **Revoke an unused assignment** (a wrong duty, an abandoned operation):

  ```bash
  python3 "$OP" revoke --assignment-id "$ASSIGNMENT_ID" \
    --journal "$JOURNAL" --auth-fd 3 3< <(printf '%s' "$NS_JWT")
  ```

  Exit 0 (HTTP 204). A consumed assignment cannot be revoked
  (`billing_operation_assignment_consumed`). A revoked duty cannot be taken
  again by the same person in that operation (the unique index stays): use
  a new plan.
- **A revision with no completable readiness set** (a stray receipt, an
  expired row): the `general` move is refused at that revision and cannot be
  repaired there. Roll back to `disabled`, re-open the canary (a new
  revision), and repeat Procedure B with a new plan
  ([`asaas-deployment.md`](./asaas-deployment.md#readiness-receipt-content-digest)).
- **Journal refused** as edited or damaged: keep it as evidence, start a new
  journal file. An applied step sent again from the new journal is refused
  by the server with the codes above (no double effect); a `revoke` is
  accepted again with no further effect.

## Refusals

The tool prints the server's code verbatim. The common ones:

| Code | HTTP | Meaning | Action |
|------|------|---------|--------|
| `billing_operation_conflict` | 409 | `assign`: you already hold a duty in this operation (one person, one duty), or this assignment already exists. `transition`/`incident-disable`: the expected revision is stale, the move is not allowed from the current state, or the canary slots belong to another builder | read the hint; `readback` |
| `billing_operation_actor_mismatch` | 403 | `--actor-user-id` is not the credential's account | use your own id (`whoami`) |
| `billing_operation_assignment_invalid` | 409 | not your assignment, revoked, already has its receipt (each assignment signs once), or made for another plan | after an unknown outcome: your receipt landed; otherwise check the plan file and id |
| `billing_operation_nonce_conflict` | 409 | the nonce is used by another receipt | a new nonce |
| `billing_operation_receipt_invalid` | 409 | a receipt of the plan is spent (the move already happened), expired, or differs | `readback` |
| `billing_operation_expired` / `billing_operation_expiry_too_long` | 409 | `expires_at` passed / more than the cap after now | new plan |
| `billing_operation_quorum_incomplete`, `billing_operation_roles_invalid`, `billing_operation_actor_conflict` | 409 | not exactly one unspent receipt per duty from three people | finish A3/A4; nothing was spent |
| `billing_rollout_quorum_stale` | 409 | a receipt predates the current state | new plan, new quorum |
| `billing_rollout_from_state_mismatch` | 409 | the plan's `from_state` is not the current state | `readback`; new plan |
| `billing_rollout_canary_budget_unbound` | 409 | the canary plan's budget does not sign this builder | new plan |
| `billing_rollout_readiness_incomplete` / `billing_rollout_readiness_unbound` | 409 | `general` without four valid rows at that revision, or its quorum already spent | `readback`; finish B6; see Recovery |
| `billing_rollout_readiness_duplicate` | 409 | this kind is already recorded at this revision | after an unknown outcome: it landed; continue |
| `billing_rollout_readiness_evidence_invalid` | 409 | rule, bundle or app digest is not the deployed one | recompute in the deployed checkout |
| `billing_rollout_readiness_recorder_invalid` | 409 | the wrong duty recorded the kind | the deployer records `readiness`, the reviewer the rest |
| `billing_rollout_readiness_receipt_invalid` / `billing_rollout_readiness_expired` | 409 | `--observed-at` before a receipt, in the future for the server, or past expiry | a correct observation time |
| `billing_rollout_readiness_revision_invalid` | 409 | `--rollout-revision` is not the current `rollout_revision` | `readback`; the current revision `R` |
| `unauthorized` | 401 | wrong token kind or expired token | login JWT for steps, rehearsal token for readback |
| `rate_limited` | 429 | too many requests; nothing was applied | wait and repeat the same command (the tool withholds the server's retry text on purpose) |
| `message` with `"message": "Super admin access required"` (stderr `refused: HTTP 403 message`) | 403 | the JWT's account is not a `super_admin` (for example a builder account) | your own `super_admin` account |

`billing_super_admin_jwt_required` cannot reach this tool: the server sends
it for a request authenticated by API key, and the tool always sends a
Bearer JWT. Any server text that is not a known code or message is printed
as `(server text withheld)`; read the HTTP status and repeat with
`--dry-run` to check the request.

## Decision record: credential type (open owner decision)

- **Default, implemented:** the 24 h login JWT of each operator's own
  account (`POST /v1/auth/login`), delivered to every step on an inherited
  descriptor. The readback takes only the 10-minute staging rehearsal token,
  because `GET /v1/admin/billing/rollout/readback` is one of the purpose-token
  targets in `src/api/middleware/auth.py`, where a token without purpose
  claims is refused.
- **Option, not implemented:** 10-minute purpose tokens for the ceremony's
  mutating routes. They need new entries in `src/api/middleware/auth.py`
  (`_STAGING_REHEARSAL_TARGETS`, ~L57-80, which today cover only the
  producer-grant and producer-receipt routes and the readback), and likely
  a scope of their own rather than `billing.producer.staging.rehearsal`.
  Trade-off: a 10-minute exposure window instead of 24 h, against the
  issuer's limit (5 tokens per account per 15 minutes) over a ceremony of
  several steps per person.
- **Status:** open; it enters only by the owner's decision. Nothing in this
  tool or runbook changes authentication; the tool works with whichever kind
  the server accepts for each route.

## Decision record: containment (open owner decision)

- **Question:** a pre-signed standby incident quorum, or containment
  without a quorum, or both. The wish lists "quórum de rollback pré-assinado
  ou contenção" as open, and its rollback column reads "quórum de 3, ou
  AppConfig `false`".
- **Pre-signed standby incident quorum**
  ([Standby containment](#standby-containment)): one call returns the
  persisted rollout to `disabled`. It lets **any single `super_admin`**
  disable, alone, for as long as it is unspent (at most 24 h after its
  first assignment), and it must be re-signed before it expires.
- **Containment without a quorum**
  ([Containment without a quorum](#containment-without-a-quorum)):
  AppConfig `financial_egress_enabled` `false`, by owner order. It needs no
  operator, but exists only after L7 and the F2 reader, closes normally
  within one poll (at most 60 s; 180 s for a stalled refresher), and leaves the persisted rollout state unchanged, so a 3-person
  rollback still follows.
- **Status:** open; it enters only by the owner's decision. Until then this
  runbook documents both, and the standby quorum is signed only on the
  owner's explicit order.
