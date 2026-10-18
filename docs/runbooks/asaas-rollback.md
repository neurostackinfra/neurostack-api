# ASAAS billing rollback runbook

> **Nothing in this runbook authorizes any rollout transition, deployment,
> migration, refund, cancellation, secret access or other live action.** Each
> of those needs its own explicit instruction from the product owner and its
> own approved operation. This document describes what the code supports
> today.

Rollback is application-only. Never downgrade the schema as part of a
rollback, and never infer a financial outcome from a browser return, a
redirect or a UI state.

## Current state (2026-10-17)

Billing rollout is `disabled` and `topup_commercial_enabled=false`. While it
stays `disabled`, a rollback of the application does not need a billing
containment step; verify the state from
`GET /v1/admin/billing/rollout/readback` (answers only under
`ENVIRONMENT=staging`, the production-serving configuration) before and
after.

## Containment controls that exist in code

- **Rollout to `disabled`.** `POST /v1/admin/billing/rollout/transitions`
  with `to_state=disabled`, or `POST /v1/admin/billing/rollout/incident-disable`
  (see [`asaas-billing-incident.md`](./asaas-billing-incident.md)). Both need
  the three-actor quorum of distinct `super_admin` accounts and the current
  rollout revision. There is no single-actor kill switch in the billing
  control plane. The receipt flow is changing to server-side signing and the
  operator tooling is being rebuilt, so confirm the current procedure with
  the owner before relying on it.
- **Financial egress flag.** Setting the AppConfig flag
  `financial_egress_enabled=false` stops new Checkout creation before any
  provider POST. It is controlled outside the quorum.
- **What neither control does.** Checkouts already created at ASAAS are not
  cancelled by any code path, and settlement of an already-paid Checkout does
  not consult the rollout state. Refunds and subscription cancellations are
  manual provider operations with their own approval; no automated
  cleanup/refund saga exists.

## Application rollback outline

1. Contain first if billing is not `disabled` (quorum transition or the
   egress flag above), then re-read the rollout state.
2. Redeploy the previously verified immutable image digest. For the
   production-serving ECS service, follow the rollback rules in
   [`asaas-producer-image-rollout-group5.md`](./asaas-producer-image-rollout-group5.md)
   (roll back to the task-definition ARN recorded in its private
   predeployment readback). Keep the expand-compatible schema in place.
3. Re-read `/ready` on every task, the rollout state, webhook readiness and
   the billing outbox/fact backlog. A stale receipt or any mismatch keeps the
   system `disabled`.
4. Resolve every exact-ID reconciliation item (`reconciliation_required`,
   `terminal_pending_readback`) by readback before any later change window.
   Never retry an ambiguous checkout blindly.

Evidence records only opaque IDs, statuses, timestamps and SHA-256 digests;
no tokens, provider payloads, customer details or card data.
