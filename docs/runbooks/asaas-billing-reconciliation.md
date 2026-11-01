# ASAAS billing fact reconciliation (`billing.fact.v1`)

Target of the `runbook_url` on the three `neurostack_billing_facts` alerts.

## The real transport

The consumer of every `billing.fact.v1` fact is the credit system, **in the
same process**. There is no queue, broker or network hop.

**Status (read first).** The code describes what an image built from it
does, not what runs in production: check the deployed image and its task
definition. The code ships the two-phase attempt record (Group 2a of the
`asaas-rollout-opening` wish), the requeue below (Group 2b) and the
`BILLING_FACT_TRANSPORT_ENABLED` wiring in `src/api/main.py` (Group 2c). The
code guard `PRESENTATION_PRECONDITIONS_OPEN` in
`src/services/billing_outbox.py` is closed (`False`), so the worker may be
given a transport; it gets one only when `BILLING_FACT_TRANSPORT_ENABLED` is
`true` (default `false`). An image that predates these groups wires
`transport=None` and keeps the guard open. Check the image and the flag
value before you read a presentation failure as a consumer fault. The
rollout turns the transport on last, after the credit switch
(`CREDIT_HOT_PATH_ENABLED`).

The path of a fact:

1. Each API worker process with `BILLING_FACT_WORKER_ENABLED=true` runs one
   `BillingFactWorker` loop (`src/services/billing_fact_worker.py`).
   With `BILLING_FACT_TRANSPORT_ENABLED=true`, `src/api/main.py` hands it the
   transport `handle_billing_fact` (`src/services/credit_fact_consumer.py`)
   and a readiness hook (see "When the consumer is not ready" below).
   With the flag `false` (the default) the loop gets `transport=None` and the
   consumer module is not even loaded: nothing is presented, no attempt is
   counted, facts stay `pending` and the stale alert fires after the
   threshold.
2. A tick claims at most `BILLING_FACT_WORKER_BATCH_SIZE` due deliveries, one
   per short transaction, `FOR UPDATE OF billing_fact_deliveries SKIP
   LOCKED`. A delivery with **any** row in `billing_fact_acks` is never
   claimed. The presentation is bounded by `TRANSPORT_TIMEOUT_SECONDS`
   (10 s). With the two-phase attempt record, the attempt is committed before
   the fact is presented, so a crash after a send still counts against the
   cap.
3. `handle_billing_fact` runs in **its own** session and transaction. On
   success it appends the credit inbox row and the consumer-owned ack
   (`accepted`, or `rejected` for a problem in the fact itself) in that
   transaction. A replay of a fact already in the inbox is a no-op.
4. Any exception raised by the consumer is a **failed attempt** (the API log
   `billing fact presentation failed` carries only `failure_class` and
   `attempt_count`), except a not-ready turn-away (`TransportNotReady`, next
   section), which is not counted. The fact is presented again after an
   exponential backoff of 60 s doubling to at most 1 h. After
   `BILLING_FACT_WORKER_MAX_ATTEMPTS` (default 10, about 5 h in total) the
   delivery becomes `blocked` with the alert row `retry cap exhausted`. The
   fact is never deleted.
5. The maintenance phase (one process at a time, advisory lock) reads the
   ack: a valid one moves the delivery to `acknowledged`; a mismatching or
   `rejected` one moves it to `blocked` with `collision`, `digest_mismatch` or
   `schema_mismatch` evidence.

### When the consumer is not ready

The readiness hook `src/api/main.py` injects is false while the credit
dominance switch is not effective (`CREDIT_HOT_PATH_ENABLED` off, or the
schema below `042`) or the image fails the consumer's own contract check
(`consumer_contract_digests`: the #137 bundle manifest or the schema file is
unavailable, or the bundle, schema or catalog digest differs from the
consumer's pins). Those are faults of the image, not of one fact. A fact
whose handoff names another bundle digest is not: the image check passes, the
consumer raises `ConsumerContractDivergence` inside the presentation and the
attempt counts. Three cases turn a presentation away **without spending an attempt**:
the hook is false before the presentation; the consumer raises
`CreditSwitchOff` (before it reads or writes anything; `main.py` turns it
into `TransportNotReady`); or the presentation fails and the hook reads false
right after (the readiness turned off during it). The publisher gives the
leased attempt back, keeps `last_attempt_at` at the turn-away instant, and
the tick ends its batch. The fact stays `pending` (or `awaiting_ack`), so a
consumer that stays unready never takes a delivery to `blocked`; the stale
alert fires after the threshold.

How to see it: the WARN `billing fact consumer not ready` (with
`attempt_count`) and the label-less per-process counter
`neurostack_billing_fact_consumer_not_ready_total`, one per turn-away. The
WARN does not say which cause it was; tell them apart with the switch gauge
`neurostack_credit_switch_effective`: `0` means the switch or the schema
(`CREDIT_HOT_PATH_ENABLED`, `alembic_version` below `042`); `1` means the
image (its bundle manifest or schema file, or a bundle, schema or catalog
digest that is not the consumer's pin): redeploy an aligned image. The
attempts counter `neurostack_billing_fact_publish_attempts_total` does not
count it, and the tick still reports `completed`. As with the other
per-process counters, `/metrics` reaches one process per scrape: read it as a
hint, never as a rate. No alert rule reads it.

Backoff after a turn-away (**open owner decision, unchanged on purpose**):
the fact waits the backoff of its restored count again, from the turn-away
instant. A fact with `attempt_count = 2` waits 120 s, and a higher count up
to `max_backoff` (1 h), even if the consumer becomes ready seconds later. A
never-presented fact (count 0) waits the 60 s base backoff.

The switch verdict is per process and cached: each API worker process reads
it again every `CREDIT_SWITCH_REFRESH_SECONDS` (default 60, at most 300). A
schema downgrade below `042` is not seen until that process's next refresh,
and a presentation can still happen in between (reproduced on a disposable
PostgreSQL during review, not in production: a tick presented with
`alembic_version` already at `041`, before the refresh). The settings are
read once per process, so turning `CREDIT_HOT_PATH_ENABLED` off takes a new
task revision, never a refresh. Downgrade in this
order: set `BILLING_FACT_TRANSPORT_ENABLED=false` and roll it out, wait one
refresh interval (the configured `CREDIT_SWITCH_REFRESH_SECONDS`, 300 s when
unsure), and only then downgrade the schema.

Failure classes the consumer raises, and what fixes them:

| `failure_class` | Cause | Fix |
|---|---|---|
| `CreditSwitchOff` | the credit dominance switch is not effective (`CREDIT_HOT_PATH_ENABLED` off, or the schema is not at `042`) | turn the switch on **before** the transport (the rollout order puts the transport last). `main.py` turns this into `TransportNotReady`: no attempt is counted, the fact stays `pending` and only the stale alert fires; it never leads to `blocked`. It is never logged as `billing fact presentation failed`. |
| `ConsumerContractDivergence` | the fact's handoff names another bundle digest than the image's (per fact); or the image's bundle, schema or catalog differs from the consumer's pins (image level) | per fact: a counted failure, then `blocked` after the cap; check the handoff's deployment. Image level: the readiness hook reads it first and turns the fact away without an attempt (it surfaces here only if the image check changed during the presentation); a deploy that aligns the pins |
| `UnresolvableFact` | no outbox row or handoff for the presented event id | lineage gap: see the `gap` alerts below |
| `FactCollision` | the event id, or the aggregate revision, is already held by other bytes in the inbox | consumer contract bug: escalate, never edit the inbox |
| `CycleIdentityConflict` | a second settled cycle with the same subscription period | billing data bug: escalate to the owner |
| `LedgerClockAhead` | the builder's ledger clock is beyond the database clock plus tolerance | wait for the clocks to converge; check host time |
| `TimeoutError` / `CancelledError` | the consumer exceeded 10 s, or raised a stray cancellation | database health, lock waits |

With the transport off (`transport=None`) none of these is raised (see
Status). With it on, each of them except `CreditSwitchOff` ends, if it lasts
more than about 5 h while the consumer stays ready, in a `blocked` delivery
**without an ack**: that is the only case the requeue below may reopen.

## What each alert means

| Alert | Source | Meaning |
|---|---|---|
| `BillingFactDeliveryStale` | `max(neurostack_billing_fact_backlog{condition="stale"}) > 0` | At least one `pending`/`awaiting_ack` fact older than `BILLING_FACT_WORKER_STALE_AFTER_SECONDS` has no valid consumer ack. With the transport off, every fact reaches this state. A requeued fact counts here at once (its `created_at` is old) until its ack arrives. |
| `BillingFactDeliveryBlocked` | `max(neurostack_billing_fact_backlog{condition="blocked"}) > 0` | At least one delivery reached the terminal `blocked` state: the retry cap ran out (no ack), or the consumer ack does not match the fact. The fact is kept. |
| `BillingFactWorkerStalled` | last-tick timestamp > 0 and older than 600 s | An enabled worker has not completed a tick in over 10 minutes, counting from its start. This covers a worker that has failed every tick since boot. A disabled worker exports `0` and never fires it. |

The backlog gauge is recomputed from the database by every loop, so whichever
process a scrape reaches reports the global state. `ticks_total{outcome="failed"}`
is a per-process counter. Because `/metrics` is not in multiprocess mode, use it
only as a hint and never compute a rate over it.

## Telling cap exhaustion from an ack mismatch

Alert rows live in `billing_fact_reconciliation_alerts` with a bounded `kind`.
Their detail is stored only as SHA-256, so match it with `sha256()`:

```sql
SELECT a.outbox_event_id, a.kind,
       CASE a.detail_sha256
         WHEN encode(sha256('retry cap exhausted'::bytea), 'hex') THEN 'retry cap exhausted'
         WHEN encode(sha256('operator requeue'::bytea), 'hex')    THEN 'operator requeue'
         WHEN encode(sha256('ack overdue'::bytea), 'hex')         THEN 'ack overdue'
         WHEN encode(sha256('ack missing'::bytea), 'hex')         THEN 'ack missing'
         WHEN encode(sha256('ack schema'::bytea), 'hex')          THEN 'ack schema'
         WHEN encode(sha256('ack collision'::bytea), 'hex')       THEN 'ack collision'
         WHEN encode(sha256('ack digest'::bytea), 'hex')          THEN 'ack digest'
         WHEN encode(sha256('missing lineage'::bytea), 'hex')     THEN 'missing lineage'
         ELSE 'other (gap effect:<id>)'
       END AS detail, a.observed_at, d.state, d.attempt_count,
       EXISTS (SELECT 1 FROM billing_fact_acks k
               WHERE k.outbox_event_id = a.outbox_event_id) AS has_ack
FROM billing_fact_reconciliation_alerts a
LEFT JOIN billing_fact_deliveries d ON d.outbox_event_id = a.outbox_event_id
ORDER BY a.observed_at DESC
LIMIT 50;
```

- **Cap exhaustion:** `kind = 'backlog'`, detail `retry cap exhausted`,
  `state = 'blocked'` and `has_ack = false`. The consumer never acknowledged
  the fact. Find the `failure_class` in the API logs, fix the cause (table
  above), then requeue (below).
- **Ack mismatch:** `has_ack = true`, and the `kind` is `schema_mismatch`
  (`ack schema`), `collision` (`ack collision`: a different `event_id`, or
  `outcome = 'rejected'`) or `digest_mismatch` (`ack digest`). The consumer
  answered, but with the wrong contract. **Never requeue it**: see
  "Investigating a `blocked` delivery that has an ack".
- **Overdue / missing:** `backlog` with `ack overdue` or `ack missing` on a
  fact that is still `pending`. With the transport off this is the normal
  state, not a fault.
- **Requeued:** `backlog` with `operator requeue`: an operator reopened the
  delivery (one row per reopen, see the audit gap below).
- **Gap:** `kind = 'gap'` means either a payment effect has no fact
  (`effect:<id>`) or a fact has no delivery/handoff (`missing lineage`). Bounded
  ticks alert only the first page of gaps and orphans, ordered by id; gaps
  beyond that page are not alerted until earlier ones are repaired. The full
  `BillingFactReconciler.reconcile()` pass sees all of them.

`python -m src.ops.billing_fact_requeue plan` (below) gives the same
classification as counts, without SQL access.

## Requeueing deliveries that exhausted their retries

`src/ops/billing_fact_requeue.py` reopens **only** deliveries that are
`blocked`, have **no ack row at all**, and carry the publisher's
`retry cap exhausted` alert. Everything else blocked is reported and left
alone:

- `blocked_with_ack`: blocked by a divergent or `rejected` ack, or blocked
  by exhaustion and then given a late, valid ack the reconciler has not read
  yet. The publisher never claims a delivery that has an ack, so reopening it
  would never present the fact again. Investigate it (the section on a
  `blocked` delivery that has an ack).
- `blocked_without_evidence`: blocked, no ack, and no `retry cap exhausted`
  row. This code never produces that state (manual SQL or an unknown writer
  did). The classification query above is driven by alert rows, so it does
  not show such a delivery: use the listing below. Escalate to the owner;
  there is no sanctioned reopen for it. The command refuses it.

Reopening sets `state = 'pending'`, `attempt_count = 0` and
`last_attempt_at = NULL`, and appends one alert row `kind = 'backlog'`,
detail `'operator requeue'`, `observed_at` = the run's time, in the same
transaction. The fact, its handoff, its earlier alerts and its event id are
unchanged, so a consumer that already holds the fact treats it as a replay.
The worker presents it on its next tick with a full
`BILLING_FACT_WORKER_MAX_ATTEMPTS` budget. The command is idempotent: a
reopened row is no longer `blocked`, so a second run reopens nothing and
writes nothing; candidate rows are locked `SKIP LOCKED`, and every condition
is re-checked by the `UPDATE` itself.

### Listing every `blocked` delivery

Unlike the classification query, this one starts from the deliveries, so a
`blocked` delivery with no alert row at all still shows up. `has_ack = false`
and `exhausted = true` is what the requeue may reopen; `has_ack = true` is
`blocked_with_ack`; `has_ack = false` and `exhausted = false` is
`blocked_without_evidence`.

```sql
SELECT d.outbox_event_id, d.attempt_count, d.updated_at,
       EXISTS (SELECT 1 FROM billing_fact_acks k
               WHERE k.outbox_event_id = d.outbox_event_id) AS has_ack,
       EXISTS (SELECT 1 FROM billing_fact_reconciliation_alerts a
               WHERE a.outbox_event_id = d.outbox_event_id AND a.kind = 'backlog'
                 AND a.detail_sha256 = encode(sha256('retry cap exhausted'::bytea), 'hex'))
         AS exhausted
FROM billing_fact_deliveries d
WHERE d.state = 'blocked'
ORDER BY d.updated_at;
```

### Procedure

Two named people: one runs the task, one reviews the plan output and the
result. Record both names in the incident log (see the audit gap).

1. **Fix the cause first.** Classify with the query above and the API logs.
   Requeueing while the consumer still fails only burns another ~5 h of
   attempts and ends `blocked` again. Check that the credit switch is
   effective, the image's contract pins match, and the worker runs with
   `BILLING_FACT_TRANSPORT_ENABLED=true` (with it `false`, or on an image
   that predates the setting, a requeued fact is never presented).
2. **Plan (read-only).** Run a one-off ECS task on the service's own task
   definition, so the database credentials stay inside the task, with the
   command:

   ```
   python -m src.ops.billing_fact_requeue plan
   ```

   It prints one JSON line on stdout (`schema`
   `neurostack.billing_fact_requeue.v1`) with only counts:
   `eligible_before`, `requeued`, `eligible_remaining`, `blocked_with_ack`,
   `blocked_without_evidence`. The plan runs in a `READ ONLY` transaction.
   Exit codes: `0` `clean` (nothing to do), `10` `pending` (eligible
   deliveries exist), `20` `investigate` (a blocked delivery this command
   must not reopen exists; eligible ones may exist too), `30`
   `unconfigured` (usage or settings), `40` `error` (database or unexpected;
   the stderr log line has the exception class only). A `requeue` that exits
   `40` applied nothing: the whole run is one transaction, rolled back on the
   error. Run `plan` again before you retry.
3. **Requeue.** The reviewer confirms the plan counts, then:

   ```
   python -m src.ops.billing_fact_requeue requeue --limit 50
   ```

   `--limit` is 1 to 500 (default 50); the oldest eligible deliveries go
   first. `pending` (exit 10) after a `requeue` means some remain (the limit,
   or a row another session held): run it again. `investigate` (exit 20)
   after a `requeue` still reopened what it could (`requeued`); the rest
   needs the next section.
4. **Verify.** Within a few ticks the requeued facts should be
   `acknowledged`. If one is `blocked` again, stop: the cause was not fixed.
   List what was reopened, and when:

   ```sql
   SELECT a.outbox_event_id, a.observed_at, d.state, d.attempt_count
   FROM billing_fact_reconciliation_alerts a
   JOIN billing_fact_deliveries d ON d.outbox_event_id = a.outbox_event_id
   WHERE a.kind = 'backlog'
     AND a.detail_sha256 = encode(sha256('operator requeue'::bytea), 'hex')
   ORDER BY a.observed_at DESC;
   ```

The `operator requeue` row closes the same way as the fact's other `backlog`
rows: once the fact has an `accepted` ack, the credit reconciliation stops
counting it as open.

### Audit gap: no actor column (owner decision)

`billing_fact_reconciliation_alerts` has **no actor column**, and no other
table records who reopened a delivery. The database proves *that* and *when*
a delivery was requeued, never *who*. The only trace of the person is outside
the database: the ECS `RunTask` call in CloudTrail (the principal, the task
definition and the command override) and the task's log stream (the result
line). CloudTrail Event history keeps `RunTask` records for only 90 days
unless a trail persists them, so the incident log must also record the
CloudTrail `eventID` of the `RunTask` and the task ARN. Until the owner
decides, every requeue needs the incident-log entry with the two names, the
time of the `RunTask`, that `eventID` and that task ARN.

**Owner decision pending:** either accept this gap (the CloudTrail trail and
the incident log are the actor record), or authorize a migration that adds
an operator-identity column or table for requeues. This change ships no
migration.

## Investigating a `blocked` delivery that has an ack

Acks are consumer-owned, append-only (`trg_billing_fact_acks_immutable`) and
unique per fact (`uq_billing_fact_acks_outbox`). A fact with a divergent or
`rejected` ack can therefore **never** get a second, valid ack, and no code
path moves its delivery out of `blocked`: `BillingFactDeliveryBlocked` keeps
firing for it. Do not requeue it, do not delete or edit the ack, and do not
set the delivery to `acknowledged` by hand.

While any such dead-end delivery exists, `BillingFactDeliveryBlocked`
(`max > 0`) fires permanently and hides every new block. Silence it with an
expiry and the incident reference, and detect new exhaustion with `plan`
(`eligible_before > 0`) or the listing query ("Listing every `blocked`
delivery").

Read the ack against its fact:

```sql
SELECT k.outcome, k.schema_version,
       k.event_id = d.event_id                AS same_event,
       k.fact_sha256 = o.payload_sha256       AS same_fact,
       k.bundle_digest = h.bundle_digest      AS same_bundle,
       k.deployment_digest = h.deployment_digest AS same_deployment,
       k.acknowledged_at, o.aggregate_kind, o.financial_revision
FROM billing_fact_acks k
JOIN billing_fact_deliveries d ON d.outbox_event_id = k.outbox_event_id
JOIN billing_outbox o ON o.id = k.outbox_event_id
JOIN billing_fact_handoffs h ON h.outbox_event_id = k.outbox_event_id
WHERE d.state = 'blocked'
ORDER BY k.acknowledged_at DESC;
```

- `outcome = 'rejected'` (`ack collision`): the consumer found a problem in
  the fact itself (the payload digest differs from the outbox row, a
  `validate_fact` violation, or a disagreement with the outbox row or the
  pinned catalog) and applied **no** credit effect. The API logs show
  `credit fact rejected`. This is a producer/billing data bug: escalate to the
  owner with the outbox id. In the credit readiness the rejection is closed
  only by an `accepted` ack of a **later** `financial_revision` of the same
  aggregate; the delivery itself stays `blocked`.
- `same_bundle = false` with `outcome = 'accepted'` (`ack digest`): the
  consumer recorded another contract bundle. Deploy skew between pins cannot
  cause it: the consumer raises `ConsumerContractDivergence` before it writes
  anything when its bundle digest differs from the handoff's. It is a
  consumer code bug or a non-current consumer version. The credit effects
  **were written**: escalate.
- `same_fact = false` or `same_deployment = false` with
  `outcome = 'accepted'` (`ack digest`): the consumer recomputed another fact
  digest, or copied another deployment digest. That is a consumer code bug:
  escalate, with the credit inbox row for the event id.
- `same_event = false` (`ack collision`): the ack names another event id for
  this fact. The schema prevents it in normal operation; treat it as data
  corruption and escalate.
- `schema_version` is not the producer's ack schema (`ack schema`): the
  consumer wrote an ack the producer cannot read. Escalate.
- All `same_*` true and `outcome = 'accepted'`: a late, valid ack on a
  delivery blocked by exhaustion. It is not a dead end: the reconciler
  converges it to `acknowledged` on its next page. Re-run `plan`.

A real repair of any of these is a code change or a reviewed migration that
the owner authorizes, not an operator action.

## Safe first steps

1. Do not delete facts, deliveries, acks or alert rows, and do not mark a
   delivery `acknowledged` by hand. Only a valid consumer ack may do that.
2. Check that the worker is enabled where you expect it
   (`BILLING_FACT_WORKER_ENABLED` and `BILLING_FACT_TRANSPORT_ENABLED`; the
   start line `billing fact worker started` says `consumer=present` only
   with the transport on) and that
   the API logs show `billing fact worker started` and no repeated
   `billing fact worker tick failed`. The `failure_class` field carries only
   an exception class name.
3. For `BillingFactWorkerStalled`, check database reachability and the pool
   first. A loop that fails every tick never updates the gauge after its start
   stamp. A stray cancellation is logged in ERROR and the loop continues.
   `billing fact worker loop was not running` at shutdown means the loop had
   already died.
4. For `BillingFactDeliveryStale` with the transport off, confirm the
   backlog matches the facts produced, then acknowledge the alert as
   expected: with `transport=None` nothing is presented, so no fact becomes
   `blocked` this way. With the transport on, read the API logs: `billing
   fact presentation failed` means a stale fact is being retried and will be
   `blocked` in about 5 h unless the cause is fixed; `billing fact consumer
   not ready` (and a growing
   `neurostack_billing_fact_consumer_not_ready_total`) means the consumer is
   turning facts away, so they stay `pending` and no attempt is counted: fix
   the readiness cause (the switch, the schema, the image's bundle manifest
   or contract digests; `neurostack_credit_switch_effective` tells the switch
   and schema, at `0`, from the image, at `1`).
5. For `BillingFactDeliveryBlocked`, run the `plan` (or the classification
   query) before you change anything. Cap exhaustion: fix the cause, then
   requeue. An ack mismatch: investigate, never requeue. The old manual
   `UPDATE` of a delivery row is replaced by the requeue command; do not use
   hand-written SQL for it.

## Card captures, funds receipts and payment drift

`BillingReadbackBacklog` (`sum(neurostack_billing_readback_backlog) > 0`)
also routes here: an exact-ID payment readback that does not converge.

**Rule since 2026-10-18 (owner decision).** A card `PAYMENT_CONFIRMED`
(capture) is the settlement: it creates the only settlement effect,
`billing.fact.v1` revision, subscription cycle, activation and Transaction.
The `PAYMENT_RECEIVED` that follows about 32 days later (funds available) is
an inert confirmation: the delivery is `processed` and nothing else changes.
Pix has no capture, so its `PAYMENT_RECEIVED` settles. `paid_at` is the
capture day, written once: the first of `confirmedDate`, `clientPaymentDate`,
`paymentDate` (ASAAS's "data de liquidação", the card funds day, comes last)
that is not later than the São Paulo day of the earliest `PAYMENT_CONFIRMED`
envelope for that payment (else of the event, or of the readback clock).
Nothing is inferred from a timestamp: a capture without a usable date waits
for a readback. No new metric, alert kind or label exists for any of this:
every case below surfaces as the delivery status `readback_required` and the
readback backlog, and the contradictory ones also raise `kind=drift`.

Verified once in the ASAAS sandbox (2026-10-18, capture at 21:45 BRT, which
was already 00:45 UTC of the next day): `dateCreated`, `confirmedDate` and
`clientPaymentDate` carry the São Paulo date; `paymentDate` stays null while
`CONFIRMED`; `creditDate`/`estimatedCreditDate` are the capture day + 32 in
São Paulo time; refund entry timestamps are São Paulo wall-clock time without
an offset.

### A capture date later than its anchor day (bound dead end)

If ASAAS ever returns a `confirmedDate`/`clientPaymentDate` later than the
São Paulo day of the payment's earliest `PAYMENT_CONFIRMED` envelope, no date
is usable: the payment stays `readback_required` (an event, or `kind=drift`
on every read) and nothing is granted. Forcing more reads does not help,
because every read uses the same anchor. Compare the panel's dates with the
anchor:

```sql
SELECT min(provider_created_at) AT TIME ZONE 'America/Sao_Paulo' AS anchor_sp
FROM billing_webhook_deliveries
WHERE provider = 'asaas' AND resource_kind = 'payment'
  AND resource_id = :payment_id AND event_name = 'PAYMENT_CONFIRMED';
```

No repair path exists in code today. Do not edit or delete that delivery,
`paid_at` or any other billing row: the anchor is evidence, and moving it
would admit any later date, including the funds day. The only reviewed hand
edit, the forced-read `UPDATE` of one delivery's status below, does not help
here. The operator path is:

1. Record a reconciliation entry in the incident log: payment id, the
   panel's dates, the anchor day, the decision, and two named people.
2. If the charge should not stand, refund it in the ASAAS panel. The next
   read shows `REFUNDED` with a `DONE` refund and converges as the
   pre-settlement refund marker (no grant, no fact). For a canary payment,
   see the canary note below.
3. If the customer must keep the purchase, escalate to the owner: settling
   such a payment needs a code change (an approved `paid_at` path does not
   exist). Until then it stays open, ungranted and alerting.

### A card receipt seen before its capture (accepted funds-day `paid_at`)

**What happens.** The payment settles from the first settle-class evidence,
and `paid_at` is the first date of the chain that exists. A card
`PAYMENT_RECEIVED` that arrives first and carries only `paymentDate` (no
`confirmedDate`, no `clientPaymentDate`) settles with the funds day as
`paid_at`, roughly the capture day + 32. Everything anchored on `paid_at`
then runs about 32 days later than if the capture had been seen first: a
top-up's expiry (`paid_at` + 6 months) is about 32 days longer. The
subscription cycle is not affected, because it starts at `dueDate`. A later
`PAYMENT_CONFIRMED` is inert and never moves `paid_at`.

**Why it is accepted.** Owner decision 2026-10-18: do not refuse it. The
funds day is a date the provider sent, not an inferred one.

**When it can happen.** The `PAYMENT_CONFIRMED` event was lost, or delayed
until after the `PAYMENT_RECEIVED`, and that `RECEIVED` payload lacks
`confirmedDate` and `clientPaymentDate`. Not observed so far: the sandbox
never produced a card `RECEIVED`.

**How to detect it.** Read-only. It lists settled card payments whose
`paid_at` is more than 20 days after the São Paulo day of the earliest
`PAYMENT_CONFIRMED` delivery (the case itself), and settled card payments
with no `PAYMENT_CONFIRMED` delivery at all (candidates: compare `paid_at`
with the capture day shown in the ASAAS panel):

```sql
SELECT p.payment_id, p.status, p.paid_at, c.capture_day
FROM billing_payment_projections p
JOIN billing_payment_effects e
  ON e.projection_id = p.id AND e.effect_kind = 'settlement'
JOIN transactions t ON t.billing_payment_effect_id = e.id
LEFT JOIN LATERAL (
  SELECT (min(d.provider_created_at) AT TIME ZONE 'America/Sao_Paulo')::date
           AS capture_day
  FROM billing_webhook_deliveries d
  WHERE d.provider = p.provider AND d.resource_kind = 'payment'
    AND d.resource_id = p.payment_id AND d.event_name = 'PAYMENT_CONFIRMED'
) c ON true
WHERE p.provider = 'asaas' AND p.settled_cents > 0
  AND t.method = 'credit_card'
  AND (c.capture_day IS NULL OR p.paid_at > c.capture_day + 20)
ORDER BY p.paid_at;
```

**What to do.** Nothing in the database. `paid_at` is write-once. No billing
row is edited by hand; the only reviewed exception is the forced-read
`UPDATE` on one delivery below. If the shifted expiry matters, record a
reconciliation entry in the incident log (payment id, panel capture day,
`paid_at`, decision, two named people) and handle any credit adjustment
through the credit system's own process.

### Capture without a funds receipt after ~40 days

Expected: none. A capture that is not refunded should reach `RECEIVED`
about 32 days later.

```sql
SELECT p.payment_id, p.status, p.paid_at,
       min(c.provider_created_at) AS captured_at
FROM billing_payment_projections p
JOIN billing_webhook_deliveries c
  ON c.provider = p.provider AND c.resource_kind = 'payment'
 AND c.resource_id = p.payment_id AND c.event_name = 'PAYMENT_CONFIRMED'
WHERE p.provider = 'asaas' AND p.settled_cents > 0 AND p.reversed_cents = 0
  AND NOT EXISTS (
    SELECT 1 FROM billing_webhook_deliveries r
    WHERE r.provider = p.provider AND r.resource_kind = 'payment'
      AND r.resource_id = p.payment_id AND r.event_name = 'PAYMENT_RECEIVED')
GROUP BY p.payment_id, p.status, p.paid_at
HAVING min(c.provider_created_at) < now() - interval '40 days'
ORDER BY captured_at;
```

For each row, read the payment in the ASAAS panel. `RECEIVED` there means
the event was lost: nothing to repair, because the capture already settled.
A chargeback or refund there that we never received is the real risk: get a
readback for one of that payment's deliveries (see "Forcing one exact read"
below). The capture's access and credits stay granted until that read
reverses them. A different receivable plan (anticipation) only changes the
timing, so record it and close the row.

### A capture blocked by drift (intended fail closed)

A `PAYMENT_CONFIRMED` whose `value` differs from the checkout snapshot
(`billing_checkout_sessions.amount_cents`), or has no usable date, does not
settle: `kind=drift` (for a value mismatch), the delivery is
`readback_required`, and the builder gets no access or credits. The backlog
sweep settles it only when the exact read reports the snapshot amount and a
usable capture date. If ASAAS really charged a different amount, do not force
anything: refund or cancel through the provider's own approval, and the read
then converges.

### A settled capture later reported unpaid (H-2)

A newer `PAYMENT_REPROVED_BY_RISK_ANALYSIS`,
`PAYMENT_CREDIT_CARD_CAPTURE_REFUSED` or `PAYMENT_OVERDUE` on settled money
is `readback_required` with `kind=drift`. If the read still shows
`PENDING`/`AWAITING_RISK_ANALYSIS`/`OVERDUE` (or a status outside the
mapping), it drifts again on every sweep, because settled money never
un-settles. Whether this alert fires depends on arrival order: the same
refusal arriving before the capture is stale or ordinary and raises nothing,
arriving after it raises `kind=drift`. The final money after the reads is
the same either way. The named operator path:

1. Find the payment from the delivery
   (`billing_webhook_deliveries.resource_id`) and read it in the ASAAS panel.
2. `CONFIRMED` or `RECEIVED` there: the next read converges. Nothing to do.
3. The charge will not be honored: refund it at ASAAS (a manual provider
   operation with its own approval). The read then shows `REFUNDED` with a
   `DONE` refund and converges as a normal reversal: a new fact revision, the
   cycle `reversed`, and `current_period_end` falls back.
4. ASAAS keeps reporting an unpaid status for a charge that cannot be
   refunded: escalate to ASAAS support and the owner. The grant and the alert
   stay. No automated un-settle path exists. No billing row is edited by
   hand; the only reviewed exception is the forced-read `UPDATE` on one
   delivery below.

Related cases need a read but are not drift. A newer settle-class event on
a `chargeback_requested`/`chargeback_dispute`/`restoration_pending` row (a
won dispute ends in `CONFIRMED`/`RECEIVED`, H-1) or on a
`reversal_pending`/`partially_reversed`/`reversed` row (a capture or receipt
after a refund, L-3). An older capture that arrives after a newer refusal
(M2), or after the newer refund, chargeback or deletion event that created a
`readback_required` row (L-2). The read relabels or settles them; no second
settlement effect is ever created.

**Canary payment refunded before any settlement.** The pre-settlement refund
marker cannot be stored for a canary payment: the database refuses any canary
projection row until the Plus subscription is active. The guard (M-2) stops
only the event-driven first write: while one of the payment's refund or
chargeback deliveries is open, a later `PAYMENT_CONFIRMED`/`PAYMENT_RECEIVED`
event waits for a read instead of granting.

- When every read shows the completed refund, those deliveries stay
  `readback_required`, with `kind=drift` on every sweep. Nothing is granted
  and the canary slot is not spent.
- An exact read that shows the capture without a completed refund still
  settles. A read of a `PAYMENT_REFUND_IN_PROGRESS` delivery taken while the
  refund is still in progress settles too: the subscription becomes active,
  Plus is granted, the slot is spent and a Transaction is recorded with
  `event_type = PAYMENT_REFUND_IN_PROGRESS`. It stays that way until the read
  of the `REFUNDED` delivery reverses it.

This is expected. Do not close those deliveries by hand: an open one is what
stops a stale capture from granting. Record the case and close the canary
run through [`asaas-rollback.md`](./asaas-rollback.md).

### Forcing one exact read

The backlog sweep reads only deliveries in `readback_required`/`unknown`. To
re-read a payment whose deliveries are all `processed`, use reviewed manual
SQL, checked by a second person, that sets **one** of its deliveries back to
`readback_required`, then let the sweep run:

```sql
UPDATE billing_webhook_deliveries SET status = 'readback_required'
WHERE id = :delivery_id AND provider = 'asaas' AND resource_kind = 'payment'
  AND status = 'processed';
```

Change nothing else. Projections, effects, cycles, the outbox, Transactions
and `paid_at` are written only by the services.

### Preflight gate for this change

Run this before the image with this rule serves webhooks. Expected: `0`.
Nothing was live, and no production ASAAS billing data existed on
2026-10-17.

```sql
SELECT count(DISTINCT p.payment_id)
FROM billing_payment_projections p
JOIN billing_webhook_deliveries d
  ON d.provider = p.provider AND d.resource_kind = 'payment'
 AND d.resource_id = p.payment_id
WHERE p.provider = 'asaas' AND p.status = 'pending'
  AND d.event_name = 'PAYMENT_CONFIRMED' AND d.status = 'processed';
```

Each counted payment is a capture the previous code accepted without
granting. If the count is not zero, force one exact read per payment (above)
after the deploy. A `CONFIRMED` read then settles it once, with the capture
day as `paid_at`.

### Rollback is a forward fix

An image from before this change treats a capture's later `RECEIVED` (funds
day `paymentDate` differs from the stored `paid_at`) as a date mismatch. That
means `readback_required`, `kind=drift` and a growing readback backlog about
32 days after every capture. The old image also stops granting new captures.
Its rollback is financially safe, because no effect is undone or doubled, but
it splits semantics. Fix forward instead. If an application rollback is
unavoidable (see `asaas-rollback.md`), expect these drift alerts on captured
payments. Do not clear them by editing `paid_at`.

## Known, accepted limits

- Alert de-duplication is check-then-insert, with no unique constraint. It
  relies on the maintenance advisory lock (and the delivery row lock for the
  retry-cap row), not on the schema. The `operator requeue` row is not
  de-duplicated on purpose: it is written once per reopen that actually
  happened, under the delivery row lock. A delivery exhausted again after a
  requeue keeps its single `retry cap exhausted` row.
- A requeue resets `attempt_count`: the earlier attempt count survives only
  in the `retry cap exhausted` row's existence and the API logs, not in the
  delivery row.
- An image that predates the two-phase attempt record kept the presentation
  guard (`PRESENTATION_PRECONDITIONS_OPEN`) open and wired no transport; the
  legacy single-phase sender it guarded is gone from the code.
- A not-ready turn-away restarts the backoff of the restored count (see
  "When the consumer is not ready"; an open owner decision).
- A presentation cut off by the worker's own 10 s timeout is a counted
  attempt even if the readiness turned off while it ran: the timeout cancels
  it before the hook can be read again.
- A `blocked` delivery with a divergent or `rejected` ack has no exit path
  in code and keeps `BillingFactDeliveryBlocked` firing (see the
  investigation section).
- Label only, money is right: a fully reversed payment whose read reports
  `CONFIRMED`/`RECEIVED` with the full `DONE` refund (an unrealistic provider
  state, which would say `REFUNDED`) is relabelled `partially_reversed`. The
  cumulative cents stay fully reversed, and no effect or fact changes.
