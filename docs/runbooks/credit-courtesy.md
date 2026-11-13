# Credit courtesy runbook

> **Nothing in this runbook authorizes a deployment, a migration window or a
> live grant.** Each live step needs the product owner's explicit order at
> the moment of that step. A grant to a real builder in production is a live
> action.

Courtesy credits (creditos-cortesia) are credits a super admin gives a
builder for free. The contract (cap, validity, removal, FEFO, L10 coverage,
fields and routes) is in [`credits.md`](../billing/credits.md), section
"Cortesia". This runbook covers operating it. Nothing here touches ASAAS, a
subscription, an order or a payment.

## Before the first grant

- The serving image is the courtesy release and the database is at `046`,
  in this order: bridge image → window `046` → courtesy release
  ([`asaas-deployment.md`](./asaas-deployment.md), "Migration 046"). Below
  `046`, or with the credit switch not effective, every courtesy route
  answers `503 credit_unavailable` and the "Créditos" action does not appear.
- **The new frontend is live.** An older frontend rejects the `courtesy` lot
  kind: a builder holding courtesy would see the credit area fail on
  `/billing` (DESIGN Risk 2). Do not grant courtesy before it.
- Do not grant courtesy while the bridge image serves (see Rollback).

## Operations (master-admin)

All operations are in the master-admin page, table "Assinaturas por
Builder", action "Créditos" on the builder's row. Only a super admin sees
and uses it; the API equivalents are the `courtesy-grants` routes under
`/v1/admin/credits`.

### Give courtesy ("Dar cortesia")

1. Open "Créditos" on the builder's row; the "Dar cortesia" form is under
   the courtesy lots.
2. Enter the amount (1 to 100.000 whole credits; shortcuts 5.000, 15.000,
   35.000) and the required reason. The validity is fixed: 30 days from the
   grant (civil days, America/Sao_Paulo), shown as "Validade".
3. "Continuar", review, "Confirmar".

The table row then shows the courtesy badge ("Cortesia, expira em dd/mm");
a builder with no subscription and no courtesy reads "Sem plano" in the
table row.

### Remove an amount ("Tirar créditos")

1. Open "Créditos", choose "Tirar créditos" on the courtesy lot.
2. Enter whole credits (at most what the lot can still spend) and the
   reason. The dialog shows the balance that remains before you confirm.
3. Confirm. Only spendable credit is removed; what was consumed stays
   consumed. The removal reduces the funded capacity and is never counted
   as usage: the consumed amount does not change. The usage percentage is
   consumption over the reduced capacity, so it stays put only when nothing
   was consumed; otherwise it rises (see "After L10 enforcement is
   armed").

### Remove all ("Tirar tudo")

The "Tirar créditos" form with the "Tirar tudo" box ticked (en "Remove
all"; `remove_all: true`): removes the lot's whole spendable balance,
including the millicredit fraction a whole amount cannot name. The lot
becomes `exhausted` ("Esgotado") and the dialog no longer offers "Tirar
créditos" on it. A repeat that still reaches the server (through the API, or
from a dialog whose lot list was not reloaded) is refused with `409
courtesy_removal_exceeds_balance` and nothing is removed; the dialog shows it
as text ("A quantidade passa do saldo que sobra no lote. Nada foi
retirado.").

## What the builder sees (`/billing`)

- A "Cortesia" line in the balance (`courtesy_available_mc`, already part of
  the total available).
- In the history, the lot with origin "Cortesia" (never "Recarga") and
  "Expira em" 30 days after the grant; in the expiries, the lot with its
  date. After a removal, the removed amount appears ("Retirados" /
  "retirados").
- A builder without a subscription still reads "Esta conta não tem
  assinatura. Compare os planos abaixo." (en "This account has no
  subscription. Compare the plans below."): courtesy is not a plan and
  grants no subscription.
- The builder receives no notice of the grant or the removal.

## Under the courtesy rule (debito-cortesia-automatico)

Once the courtesy rule serves, a grant is no longer free capacity only: a
builder with no global and no mark that ever received a courtesy lot is
charged from its courtesy, and blocked at once when the courtesy runs out
and no paid plan is in force (`CreditRepository.effective_enforcement`,
`src/repositories/credit.py`; operating it:
[`credit-courtesy-rule.md`](./credit-courtesy-rule.md)).

- **The first grant puts the builder under the rule.** Its instant is the
  first grant's `occurred_at`; the window it falls in opens free of storage
  charge (`test_the_first_grant_window_is_free_and_later_ones_charge_courtesy_only`,
  `tests/integration/test_credit_courtesy_rule_postgres.py`).
- **A grant first pays old open debt**: a builder with old debt can stay
  blocked after a grant smaller than that debt
  (`test_a_grant_or_a_paid_plan_unblocks_and_old_debt_is_settled_first`).
- **"Tirar tudo" can block immediately.** The confirmation warns that
  without a paid plan in force the builder is blocked at once and the agent
  stops answering
  (`frontend/artifacts/neurostack/src/components/admin/courtesy-credits-dialog.tsx`).
  There is no 72-hour grace under the rule.
- A removal is never usage, also for the rule's thresholds
  (`test_a_removal_is_never_usage_for_the_courtesy_thresholds`).

## After L10 enforcement is armed (DESIGN Risk 3)

Once `credit_enforcement_at` is set, courtesy credit is real capacity and a
removal follows the ledger rules with no shortcut:

- removing credit from a live builder can push it across the 50 / 90 / 100 %
  thresholds and record those alerts;
- removing all of a builder's only coverage exhausts it: the exhaustion
  episode opens, requests run on credit into the **72-hour debt** window,
  and then the builder is suspended and later reaches `deletion_due` (the
  72-hour debt of
  [`credit-enforcement-arming.md`](./credit-enforcement-arming.md); the
  states and deadlines in [`credits.md`](../billing/credits.md),
  `/v1/credits/state`);
- the dialog shows what remains in that lot, not the builder's whole
  balance (a monthly allowance or a top-up may still cover it): read the
  builder's balance before removing from a live builder.

Courtesy counts for the L10 cutoff report only as `covered`
(`usable_courtesy`), never as `usable`.

## Refusals

Nothing is written for any refusal. A `403` answers `{"detail": {"code":
...}}`, a `422` FastAPI's `{"detail": [...]}`; every other refusal answers
`{"code": ...}`.

| Status | Code | Meaning |
|---|---|---|
| 403 | `jwt_required`, `super_admin_jwt_required` | not a super admin's JWT |
| 404 | `builder_not_found` | no such builder |
| 404 | `courtesy_lot_not_found` | no such lot, or the lot is monthly or top-up (same answer for all) |
| 409 | `builder_inactive` | the builder is inactive and cannot receive courtesy (a removal stays allowed) |
| 409 | `courtesy_removal_exceeds_balance` | more than the lot can still spend, or remove-all of an expired or zeroed lot |
| 409 | `idempotency_key_reused` | the key was already used for a different request |
| 422 | (validation) | `credits` outside 1..100,000 or not a whole number; a reason missing, only whitespace, over 500 characters or with a NUL; both or neither of `credits`/`remove_all` (or `remove_all: false`); an extra field; a missing or over-200-character `Idempotency-Key` |
| 503 | `credit_unavailable` | credit switch not effective, or the database below `046` |
| 503 | `credit_clock_ahead` | ledger clock ahead; nothing written, retry with the same key |

## Idempotency

Every grant and removal carries an `Idempotency-Key`. A resend of the same
body (network failure, an unknown 5xx, `credit_clock_ahead`, "Tentar de
novo", or "Voltar" and the same values confirmed again) reuses the same key:
if the first attempt was recorded it gets that result, otherwise it is
written once. Changing the amount, the reason or "Tirar tudo" makes a new
request with a new key, so after a network failure retry before changing
anything.
Through the API, never reuse a key for a different body (`409
idempotency_key_reused`).

## Test-account recipe

On a test builder with no subscription:

1. "Dar cortesia" 15.000 credits (shortcut 15.000, reason "teste").
2. Verify: the master-admin row shows "Cortesia, expira em dd/mm" (30 days
   ahead) and no "Trial"; as the builder, `/billing` shows "Esta conta
   não tem assinatura. Compare os planos abaixo.", the "Cortesia" line with
   15.000 and "Expira em" 30 days ahead.
3. "Tirar créditos" 5.000: 10.000 remain and the usage bar does not move.
4. "Tirar tudo": the lot is zeroed, shows "Esgotado" and no longer offers
   "Tirar créditos".

In production this runs only with the owner's order, on a real test builder,
and always ends with remove-all.

## Rollback

Follow [`asaas-rollback.md`](./asaas-rollback.md), section "Migration 046".
**Never an Alembic downgrade in production**; the migration comes back only
by a restore of the window's snapshot, by owner decision.

Rolling the courtesy release back to the bridge image keeps credit **on**,
but the bridge does not know courtesy rows: for a builder with a courtesy
lot (even one zeroed by "Tirar tudo" or expired, as the L5 test builder is),
the `/billing` credit reads (`/v1/credits/summary`, `balance`,
`lots`, `expiries`) fail, and removed courtesy credit counts again as
available and funded (thresholds and debits included). Builders without
courtesy are unaffected. **Give no courtesy while the bridge serves**;
prefer a forward fix to a corrected courtesy release.
