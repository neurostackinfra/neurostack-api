# ASAAS billing fact reconciliation (`billing.fact.v1`)

Target of the `runbook_url` on the three `neurostack_billing_facts` alerts.

**The worker has no transport and delivers nothing today.** No credit consumer
exists, production wires `transport=None`, and the code guard
`PRESENTATION_PRECONDITIONS_OPEN` in `src/services/billing_outbox.py` makes any
attempt to wire one raise. Facts stay `pending` and retriable. With the worker
enabled, a stale alert is the expected steady state until a consumer ships.

## What each alert means

| Alert | Source | Meaning |
|---|---|---|
| `BillingFactDeliveryStale` | `max(neurostack_billing_fact_backlog{condition="stale"}) > 0` | At least one `pending`/`awaiting_ack` fact older than `BILLING_FACT_WORKER_STALE_AFTER_SECONDS` has no valid consumer ack. Today, with no consumer, every fact reaches this state. |
| `BillingFactDeliveryBlocked` | `max(neurostack_billing_fact_backlog{condition="blocked"}) > 0` | At least one delivery reached the terminal `blocked` state: the retry cap ran out, or the consumer ack does not match the fact. The fact is kept. |
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
         WHEN encode(sha256('ack overdue'::bytea), 'hex')         THEN 'ack overdue'
         WHEN encode(sha256('ack missing'::bytea), 'hex')         THEN 'ack missing'
         WHEN encode(sha256('ack schema'::bytea), 'hex')          THEN 'ack schema'
         WHEN encode(sha256('ack collision'::bytea), 'hex')       THEN 'ack collision'
         WHEN encode(sha256('ack digest'::bytea), 'hex')          THEN 'ack digest'
         WHEN encode(sha256('missing lineage'::bytea), 'hex')     THEN 'missing lineage'
         ELSE 'other (gap effect:<id>)'
       END AS detail, a.observed_at, d.state, d.attempt_count
FROM billing_fact_reconciliation_alerts a
LEFT JOIN billing_fact_deliveries d ON d.outbox_event_id = a.outbox_event_id
ORDER BY a.observed_at DESC
LIMIT 50;
```

- **Cap exhaustion:** `kind = 'backlog'`, detail `retry cap exhausted`,
  `attempt_count = BILLING_FACT_WORKER_MAX_ATTEMPTS`, and no ack row. The
  consumer never acknowledged the fact.
- **Ack mismatch:** an ack row exists, and the `kind` is `schema_mismatch`
  (`ack schema`), `collision` (`ack collision`: a different `event_id`, or
  `outcome = 'rejected'`) or `digest_mismatch` (`ack digest`). The consumer
  answered, but with the wrong contract.
- **Overdue / missing:** `backlog` with `ack overdue` or `ack missing` on a
  fact that is still `pending`. This is the normal no-consumer state, not a
  fault.
- **Gap:** `kind = 'gap'` means either a payment effect has no fact
  (`effect:<id>`) or a fact has no delivery/handoff (`missing lineage`). Bounded
  ticks alert only the first page of gaps and orphans, ordered by id; gaps
  beyond that page are not alerted until earlier ones are repaired. The full
  `BillingFactReconciler.reconcile()` pass sees all of them.

## `blocked` has no requeue path yet

Only two things move a delivery out of `blocked`:

1. A **late valid ack** from the consumer. The reconciler then converges the
   fact to `acknowledged`.
2. **Manual SQL**, reviewed by a second person, that resets the delivery row
   (`state`, `attempt_count`, `last_attempt_at`). Never touch
   `billing_outbox`, `billing_fact_handoffs` or `billing_fact_acks`: they are
   append-only evidence.

No operator endpoint or script requeues a `blocked` fact. Building one is a
binding precondition before a transport may be wired.

## Safe first steps

1. Do not delete facts, deliveries, acks or alert rows, and do not mark a
   delivery `acknowledged` by hand. Only a valid consumer ack may do that.
2. Check that the worker is enabled where you expect it
   (`BILLING_FACT_WORKER_ENABLED`) and that the API logs show
   `billing fact worker started` and no repeated
   `billing fact worker tick failed`. The `failure_class` field carries only an
   exception class name.
3. For `BillingFactWorkerStalled`, check database reachability and the pool
   first. A loop that fails every tick never updates the gauge after its start
   stamp. A stray cancellation is logged in ERROR and the loop continues.
   `billing fact worker loop was not running` at shutdown means the loop had
   already died.
4. For `BillingFactDeliveryStale` with no consumer deployed, confirm the
   backlog matches the facts produced. Then acknowledge the alert as expected;
   there is nothing to deliver.
5. For `BillingFactDeliveryBlocked`, classify every blocked fact with the query
   above before you change anything. A mismatch is a consumer contract bug. Cap
   exhaustion needs the consumer to be healthy before any reviewed manual reset.

## Card captures, funds receipts and payment drift

`BillingMigrationReadbackBacklog` (`sum(neurostack_billing_readback_backlog) > 0`)
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
  retry-cap row), not on the schema.
- Once a transport exists, a crash or lost commit after a consumer-visible send
  is not counted. It is re-presented without limit, bypassing the backoff and
  the cap, until the attempt is leased before presentation (see
  `PRESENTATION_PRECONDITIONS_OPEN`).
- Label only, money is right: a fully reversed payment whose read reports
  `CONFIRMED`/`RECEIVED` with the full `DONE` refund (an unrealistic provider
  state, which would say `REFUNDED`) is relabelled `partially_reversed`. The
  cumulative cents stay fully reversed, and no effect or fact changes.
