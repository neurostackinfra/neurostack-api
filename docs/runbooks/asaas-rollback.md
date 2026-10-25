# ASAAS billing rollback runbook

> **Nothing in this runbook authorizes any rollout transition, deployment,
> migration, refund, cancellation, secret access or other live action.** Each
> of those needs its own explicit instruction from the product owner and its
> own approved operation. This document describes what the code supports
> today.

Rollback is application-only. Never downgrade the schema as part of a
rollback, and never infer a financial outcome from a browser return, a
redirect or a UI state.

Once migration 036 is applied, no image built before Group 10 E5 can run
(its ORM reads the dropped `roster_revision`/`migration_id` columns), so roll
back only to an image that contains E5. Downgrading 036 is a separate owner
decision, refused while any subscription or readiness row exists; see
"Migration 036" in [`asaas-deployment.md`](./asaas-deployment.md).

Once migration 037 is applied, roll back only to an image that contains
Group 10 E6. The E5 image still maps the dropped legacy tables: its ORM
deletion of a builder (`DELETE /v1/admin/builders/{id}`) fails with
`relation "billing_migrations" does not exist` and rolls back; its other
paths do not touch them. Running the E5 image again needs the 037 downgrade
first, a separate owner decision; see "Migration 037" in
[`asaas-deployment.md`](./asaas-deployment.md).

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

The rollback target is the manifest field `rollback_target`
(`infra/production/asaas-production-wiring.yaml`), which `plan` reports as
`rollback_target`. It has three phases:

1. **Until the re-pin is merged: revision 8**
   (`arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8`),
   the revision the service ran when the wiring was prepared. This phase
   covers revision 9's 24 h observation period.
2. **After the re-pin is merged: revision 9**
   (`arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9`,
   the D3 revision on the bridge image
   `neurostack-staging@sha256:18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24`).
   **Merge condition:** the re-pin (manifest field to `revision: 9`, no
   `repin` block) merges only after revision 9's observation period has
   closed, about **2026-10-23 18:30 UTC**, with revision 9 still serving and
   verified, and before migration 036.
3. **After the second re-pin is merged: revision 10**
   (`arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:10`,
   revision 9 with `BILLING_FACT_WORKER_ENABLED=true` on the same bridge
   image). **Merge condition:** the second re-pin (manifest field to
   `revision: 10`, no `repin` block) merges only after revision 10 has
   served, been verified and the observation of the worker has closed: 6
   hours after its deployment completed (2026-10-25 01:51 UTC), so at
   **2026-10-25 07:51 UTC**, with the actual closing time recorded by the
   orchestrator in the manifest marker `closed_at` before the merge
   ([`asaas-deployment.md`](./asaas-deployment.md), "Merge condition of the
   re-pin (9 to 10)"), and before the image bump (revision 11, a new app image digest) is deployed:
   a rollback to revision 9 would also turn the worker off. Once revision 11
   serves, the way back is revision 10
   ([`asaas-deployment.md`](./asaas-deployment.md), "The image bump").

**Revision 8 is unsafe after migration 036**: it reads columns 036 drops, so
once 036 has run, roll back to the field's revision (10 since the second
re-pin), never to 8. Read "revision R" below as the field's current value:
revision 8 in phase 1, revision 9 in phase 2, revision 10 in phase 3 (check
with `plan`, `rollback_target`). A failed post-deploy webhook
registration (`DEPLOYED_WEBHOOK_PENDING`) is an alert, never a reason to roll
back.

- **Before the service deployment gate**, nothing needs rolling back. The
  stack and the extra task definition revision were inert while the service
  ran revision 8 (history: the service has run revision 9 since
  2026-10-22).
- **Automatic rollback.** The service has the deployment circuit breaker
  enabled with rollback (`resetOnHealthyTask`, threshold `BOUNDED_PERCENT`
  50), minimum healthy 100 % and maximum 200 % (read back 2026-10-18). New
  tasks start next to the old ones, and the old tasks keep serving. If new
  tasks cannot reach a steady state, ECS marks the deployment failed and
  redeploys the last completed revision. Examples: the execution role cannot
  read a referenced secret, the API key fails settings validation, or
  `/health` never passes. The exact failed-task count that trips the breaker
  is computed by ECS from that setting and was not verified here. Read back
  the service afterwards: one PRIMARY deployment on the last completed
  revision, `COMPLETED`.
- **Manual rollback (separately approved; it is a deployment).**

  ```bash
  aws ecs update-service --region us-east-1 --cluster neurostack-staging \
    --service neurostack-staging-app --task-definition neurostack-staging-app:R
  aws ecs wait services-stable --region us-east-1 --cluster neurostack-staging \
    --services neurostack-staging-app
  ```

  `R` is the rollback target: `8` only until the re-pin is merged, `9` after
  it, `10` after the second re-pin, and never `8` once migration 036 has
  run.

  **Verify:** `plan` shows `current_task_definition` on revision R, the target
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
  delivering to `/v1/billing/webhook/asaas`. Revision 8 (the phase 1
  target) has no `ASAAS_WEBHOOK_TOKEN` (revision 9 carries it); the current
  receiver answers such deliveries with 503 before reading the body (the
  behaviour of the revision 8 image was not verified). ASAAS may then interrupt the webhook queue and email
  `ASAAS_WEBHOOK_NOTIFICATION_EMAIL`.
  After a later re-deploy, the readback reports `interrupted` until the
  webhook is reconciled. The automatic `reconcile` that runs after each deploy
  (`python -m src.ops.asaas_webhook reconcile`, see the D4 section of
  [`asaas-deployment.md`](./asaas-deployment.md)) **resumes an interrupted
  queue by default, and ASAAS then re-sends the backlog** (result reason
  `queue_resumed`); add `--no-resume` to keep it paused. It **never
  re-enables a webhook that is disabled** (`enabled: false`): that stays
  `disabled` / `webhook_disabled_by_operator` until someone runs
  `reconcile --allow-enable` on the owner's instruction. Pausing or deleting
  the provider webhook is a manual provider operation with its own approval;
  to keep ASAAS from delivering during a rollback, disable the webhook in the
  ASAAS panel (an interruption alone is undone by the next deploy).

Evidence records only opaque IDs, statuses, timestamps and SHA-256 digests;
no tokens, provider payloads, customer details or card data.
