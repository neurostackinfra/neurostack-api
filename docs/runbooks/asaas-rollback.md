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
- **Financial egress flag.** `financial_egress_enabled` false stops new
  Checkout creation before any provider POST, outside the quorum. Today it is
  closed by absence: the task definition carries no `APPCONFIG_*` name, so
  every read returns false. No AppConfig profile exists to flip, so this is
  not yet a control that can be toggled. Making it one is follow-up F2 in
  [`asaas-deployment.md`](./asaas-deployment.md#appconfig-and-opening-financial-egress-follow-up-f2-not-written).
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

## Rolling back the Group 9 D3 wiring

The rollback target is `neurostack-staging-app:8`
(`arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8`),
the revision the service ran when the wiring was prepared. `plan` reports it
as `rollback_target`, and the manifest pins it.

- **Before the service deployment gate**, nothing needs rolling back. The
  stack and the extra task definition revision are inert while the service
  runs revision 8. Optionally deregister the extra revision.
- **Automatic rollback.** The service has the deployment circuit breaker
  enabled with rollback (`resetOnHealthyTask`, threshold `BOUNDED_PERCENT`
  50), minimum healthy 100 % and maximum 200 % (read back 2026-10-18). New
  tasks start next to the old ones, and the old tasks keep serving. If new
  tasks cannot reach a steady state, ECS marks the deployment failed and
  redeploys the last completed revision. Examples: the execution role cannot
  read a referenced secret, the API key fails settings validation, or
  `/health` never passes. The exact failed-task count that trips the breaker
  is computed by ECS from that setting and was not verified here. Read back
  the service afterwards: one PRIMARY deployment on revision 8, `COMPLETED`.
- **Manual rollback (separately approved; it is a deployment).**

  ```bash
  aws ecs update-service --region us-east-1 --cluster neurostack-staging \
    --service neurostack-staging-app --task-definition neurostack-staging-app:8
  aws ecs wait services-stable --region us-east-1 --cluster neurostack-staging \
    --services neurostack-staging-app
  ```

  **Verify:** `plan` shows `current_task_definition` on revision 8, the target
  group is healthy, `GET /ready` returns 200 on every task, and
  `GET /v1/admin/billing/rollout/readback` is still `disabled`.
- **IAM and secret.** The stack `neurostack-asaas-production-wiring` (applied
  2026-10-18) owns the policy `asaas-production-wiring-secret-read` and the
  smoke token. Both are additive, revision 8 does not reference them, and they
  should stay. The policy is what lets the execution role read **all three**
  secrets (`asaas-api-key`, `asaas-webhook-token` and the smoke token). Keep
  the stack while any revision the service runs, or could roll back to,
  references any of the three; deleting it earlier makes every new task of
  such a revision fail to start. Only after that may the stack be deleted.
  The smoke token secret is retained by `DeletionPolicy`; schedule its
  deletion separately, with its own approval, if wanted. Never edit
  `segredos-e-logs` as part of a rollback.
- **Provider webhook.** If the D4 registration already ran, ASAAS keeps
  delivering to `/v1/billing/webhook/asaas`. Revision 8 has no
  `ASAAS_WEBHOOK_TOKEN`; the current receiver answers such deliveries with
  503 before reading the body (the behaviour of the revision 8 image was not
  verified). ASAAS may then interrupt the webhook queue and email
  `ASAAS_WEBHOOK_NOTIFICATION_EMAIL`.
  After a later re-deploy, the readback reports `interrupted` until the
  webhook is reconciled. Pausing or deleting the provider webhook is a manual
  provider operation with its own approval.

Evidence records only opaque IDs, statuses, timestamps and SHA-256 digests;
no tokens, provider payloads, customer details or card data.
