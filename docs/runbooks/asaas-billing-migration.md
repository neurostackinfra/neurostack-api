# ASAAS billing migration and reconciliation

This runbook is for a controlled legacy AbacatePay subscription migration. It
does not authorize a production provider request, deployment, rollout, or
secret rotation. Those actions require the separately approved production
change window.

## Safety boundary

The control plane starts fail-closed. A migration is roster-owned and its
`provider` is `NULL` from `cancel-begin` until the exact cancellation and ASAAS
provider-flip receipts are committed in their respective transactions. Do not
infer a provider outcome from a timeout, a UI screenshot, or a repeated
request.

The São Paulo schedule is fixed at T (renewal date), T-7 (open) and T-2
(cutover). Both deadlines are inclusive through 23:59:59 São Paulo time of that
day; from 00:00:00 of the next local day the attempt is late. A late roster
import (`billing_migration_open_late`), a late `cancel-begin`, `recreate-reopen`
or `recreate-reserve` (`billing_migration_cutover_late`) is refused before any
change: the legacy binding stays AbacatePay/active, the roster revision and the
saga operation are untouched, and the refusal increments
`neurostack_billing_migration_operations_total{outcome="rejected"}`. The stored
target snapshot, T dates, and attempt sequence are never recalculated during
recreate.

## Local preparation

1. Obtain three active, distinct JWT `super_admin` assignments: deployer,
   approver, reviewer. All must submit the same operation ID, environment,
   from/to state, deployment digest, budget digest, and expiry (at most
   `BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS`, 24 h by default, ahead).
2. Each operator appends their own receipt through
   `/v1/admin/billing/operations/receipts` with the material, their
   `assignment_id` and a fresh `nonce`; the backend signs it (a `signature`
   field is refused). A nonce and assignment are both single-use. Never place
   a JWT, key, or provider response in the receipt payload or ticket.
3. Import only an existing canonical legacy roster entry through
   `/v1/admin/billing/migrations/roster`. The API rejects non-roster entries
   and new builder checkout remains blocked while a migration exists.

## Transition sequence

1. Call `cancel-begin`. The database increments the roster revision and makes
   the legacy provider unknown locally. This endpoint does not call AbacatePay.
2. Observe the AbacatePay panel and submit the fresh observation (at most 15
   minutes old) via `cancel-reconcile`. With `cancelled=true` the backend
   writes the immutable `cancel_observation` receipt for the exact legacy ID
   and operation in the same transaction that moves the saga to
   `legacy_cancelled`. A negative or incomplete observation goes to
   `manual_intervention`; it must not reopen legacy billing.
3. If ASAAS shows no recurrence, record two provider-signed empty reads via
   `/v1/admin/billing/migrations/absence-reads`, both observed after the
   convergence limit (the later of the cancellation and the prior attempt plus
   90 minutes), at least 5 minutes apart, with distinct provider event IDs. The
   backend binds each read to the latest prior checkout attempt; the second
   read also appends the single `recreate_authorized` receipt. Then use
   `recreate-reopen` and `recreate-reserve` with that receipt. One read, a
   repeated event, a read for another migration/operation/attempt or before
   the limit is refused, and the database refuses an authorization without
   both reads. Each reservation copies the original snapshot and renewal date.
   Commit before any separately approved egress.
4. If a checkout was reserved before general rollout and the worker crashed,
   call `recover-checkout`; it moves to `terminal_pending_readback` and
   `recreate_pending`, never guesses whether ASAAS created state.
5. `provider-flip` takes the provider-signed ASAAS observation envelope
   verbatim; the ASAAS subscription ID is its signed `external_reference`.
   The backend verifies the Ed25519 signature, binds the target ID (unique per
   migration and per subscription), persists the envelope as the
   `provider_flip` receipt and flips the subscription in one transaction. The
   database validates the cancellation lineage, target snapshot, current
   roster revision and immutable receipt before it makes ASAAS authoritative.

## Reconcile backlog, provisioning and smoke callers

Use `scripts/asaas-billing-migration.py` only against a loopback API and a
mode-`0600` JWT file. It is intentionally an API client, not an ASAAS client.

```bash
install -d -m 0700 "$HOME/billing-export"   # outside the repository
scripts/asaas-billing-migration.py --api-url http://127.0.0.1:8000 \
  --jwt-file .private/super-admin.jwt export-roster \
  --output "$HOME/billing-export/roster.jsonl"
```

The provisioning and smoke callers are a production change-window procedure:
record their opaque digests in the signed operation material, then use the
administrative transition/capability endpoints. Do not use a migration script
to provision ASAAS or to rotate credentials.

The authenticated local caller accepts only the billing-admin endpoint
allowlist and a mode-`0600` JSON request file. It rejects PII, credentials,
tokens and screenshots before sending the loopback request. Reconciliation,
provisioning and smoke use the following local-only forms:

```bash
# reconcile_backlog: signed cancellation/readback transition
scripts/asaas-billing-migration.py --api-url http://127.0.0.1:8000 \
  --jwt-file .private/super-admin.jwt call \
  --endpoint /v1/admin/billing/migrations/cancel-reconcile \
  --input .private/reconcile-backlog.json

# provisioning: reserve only; it does not contact ASAAS
scripts/asaas-billing-migration.py --api-url http://127.0.0.1:8000 \
  --jwt-file .private/super-admin.jwt call \
  --endpoint /v1/admin/billing/migrations/recreate-reserve \
  --input .private/provisioning.json

# smoke: production-required verification; requires a configured receiver and
# a receiver-signed Ed25519 acknowledgement for the backend-issued challenge
scripts/verify-asaas-observability.py --rules-digest <sha256> \
  --smoke-file config/billing-observability-smoke.json --smoke-digest <sha256> \
  --require-non-loopback --verify-mounted-rule-digest --production-required
```

The production-required verifier requires a receipt signed by the configured
receiver key for the backend-issued challenge nonce and challenge digest. The
receipt is bound to the receiver URL, rules digest, smoke digest, and freshness
window; a static acknowledgement in the smoke artifact never substitutes for
that runtime proof. Production compose must provide the signed
`BILLING_OBSERVABILITY_ACKNOWLEDGEMENT`, its digest, the challenge nonce/digest,
and the receiver public key before readiness can pass. Static token, URL, or
digest presence alone is insufficient. Local tests use a deterministic fake
receiver key/fixture and do not call the production receiver.

## Export retention

Roster export contains only opaque migration/subscription IDs, state, counter
and dates. Its directory must be a `0700` directory owned by the operator,
reached without symbolic links and outside the repository (and outside the main
repository of a linked worktree). It rejects links, hardlinks, modes other than
`0600`, stale files, count or digest mismatch, and unapproved fields; the bytes
are read from the same descriptor that was checked. Verify it immediately, then
discard it within 24 hours (the discard receipt records bytes, count, actor,
disposition and, when disposal is refused, the failure code):

```bash
scripts/asaas-billing-migration.py --api-url http://127.0.0.1:8000 \
  --jwt-file .private/super-admin.jwt verify-export \
  --input "$HOME/billing-export/roster.jsonl" --digest <sha256> --count <count>
scripts/asaas-billing-migration.py --api-url http://127.0.0.1:8000 \
  --jwt-file .private/super-admin.jwt discard-expired \
  --input "$HOME/billing-export/roster.jsonl"
```

Never export customer data, credentials, provider payloads, or screenshots.
