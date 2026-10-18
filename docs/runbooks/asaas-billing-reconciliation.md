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

## Known, accepted limits

- Alert de-duplication is check-then-insert, with no unique constraint. It
  relies on the maintenance advisory lock (and the delivery row lock for the
  retry-cap row), not on the schema.
- Once a transport exists, a crash or lost commit after a consumer-visible send
  is not counted. It is re-presented without limit, bypassing the backoff and
  the cap, until the attempt is leased before presentation (see
  `PRESENTATION_PRECONDITIONS_OPEN`).
