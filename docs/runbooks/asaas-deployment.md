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
2. Runtime configuration exists outside the repository and was read back:
   ASAAS secrets in the task definition, the AppConfig profile with
   `financial_egress_enabled`, `ASAAS_WEBHOOK_URL`,
   `ASAAS_CHECKOUT_RETURN_URL` (HTTPS page pointing at the published
   `/billing` route), `ASAAS_WEBHOOK_NOTIFICATION_EMAIL`, the provider webhook
   registration and the public exposure of `/v1/billing/webhook/asaas`. None
   of these is defined in this repository; how the webhook is registered and
   exposed in production is UNKNOWN from the repo.
3. Schema changes are expand-only and applied in a separately approved
   window; read back the exact head before deploying the image digest, and
   verify `/ready` on every task before any external effect.
4. Evidence records only opaque IDs, statuses, timestamps and SHA-256
   digests. Never record tokens, provider payloads, customer details or card
   data.

## Frontend build and deployment

From the repository, how the builder UI reaches production is **UNKNOWN**.
The production `Dockerfile` builds only the Python API and does not build or
serve the frontend, and no GitHub workflow builds or publishes it. The SPA is
`frontend/artifacts/neurostack` (pnpm workspace, `pnpm --filter
@workspace/neurostack run build`, which requires `PORT` and `BASE_PATH` and
writes `dist/public`; the Vite dev server proxies `/v1` to the API).
`frontend/Dockerfile` builds an `artifacts/api-server` package that does not
exist in this repository, and `frontend/.replit` declares a Replit autoscale
deployment for `artifacts/api-server` and `artifacts/mockup-sandbox`, not for
`artifacts/neurostack`. Before relying on the billing page, the operator must
establish, outside this repository, which build is published and confirm that
`ASAAS_CHECKOUT_RETURN_URL` points at its `/billing` route.
