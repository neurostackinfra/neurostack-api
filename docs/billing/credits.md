# Credits: read API, billing UI and top-up purchase

Group 6 of the `neurostack-credit-system` wish. This page documents the
credit read API (`GET /v1/credits/*`), how the billing, knowledge-base and
media pages present it, and how a credit package is bought. The ledger rules
themselves (grants, monthly-then-FEFO consumption, debt, reversal, expiry,
the exhaustion lifecycle) live in `src/services/credit_ledger.py` and
`src/services/credit_lifecycle.py`; this API only reads them.

## Units

- Amounts are integer **millicredits** (`*_mc`); 1 credit = 1,000 mc. The
  API never returns a float; the UI divides by 1,000 only to display.
- One governed decision (trace) costs 1,000 mc. RAG: 1 credit per 5,000
  characters. Media storage: 1 credit per 20 MB (decimal). Every media credit
  covered in a window includes 200 MB of downloads; downloads beyond cost
  1 credit per 100 MB. `GET /v1/credits/catalog` returns these values.

## The switch and the mode

- Every route is a credit surface (`credit_router`): while the credit switch
  is not effective (config `credit_hot_path_enabled` off, or a database below
  the credit revision) it answers `503 {"code": "credit_unavailable"}` before
  any dependency, authentication refusal or SQL.
- With the switch effective and before `credit_enforcement_at`, the mode is
  `observational`: nothing is charged. The consumption route shows what the
  observational metering *would* have debited (`observed`), and the ledger
  holds only grants. After `credit_enforcement_at` the mode is `enforced` and
  `charged` shows the ledger's debits and the consumption debt they created.
- No request, header, query value or frontend setting turns the switch, the
  enforcement, the commercial top-up or the purge on. Purge stays
  `purge_disabled` (`POST /v1/admin/credits/purges`, Group 5).

## Who can read

The account belongs to a **builder**; the builder is never named by the
client.

| Caller | Reads |
|---|---|
| JWT `builder` (with its `builder_id` claim) | its own builder, through its own tenant or, with `?tenant_id=`, one of its clinics |
| JWT `super_admin` | the builder of the tenant it selects with `?tenant_id=` |
| API key, `clinic_admin`, any other role | `403 {"detail": {"code": "credit_scope_forbidden"}}`, before any SQL |

The builder is `tenants.builder_id` **re-read** for the tenant (a plain read,
lock-order step 0, no lock) and, for a builder, must equal its JWT claim (a
clinic moved to another builder is refused to the old builder's token). One
exception: a builder's **own** tenant (no `tenant_id`, or `tenant_id` equal
to the token's) that is linked to no builder (`tenants.builder_id` NULL, the
usual home tenant of a builder user) reads the builder's own account, the
JWT `builder_id` claim, as `/v1/billing/*` does, once the token's `users`
row is re-read (by the token's `sub`) and still is an active `builder` of
that builder and that tenant -- a user since deactivated, deleted, demoted,
moved to another builder or to another tenant is refused like an absent
tenant, with no `credit_*` or `billing_*` read. The same `users` statement
also refuses the exception when a builder user of ANOTHER builder (or of
none) shares that unlinked tenant: it is then no single builder's home, and
every builder's token on it is refused like an absent tenant (a second user
of the same builder, or a user of another role such as `clinic_admin`, does
not block it). An own tenant linked to another builder is still refused. A
clinic override is never covered by that exception: a clinic of another
builder (or of none) and a tenant that does not exist answer the same
`403 {"detail": {"code": "tenant_override_forbidden"}}` -- same status, body
and headers -- before any `credit_*` or `billing_*` table is read, so before
any lock, order, ledger, outbox, storage or receipt. Builder finances are
builder-only, as on `main` (`/v1/billing/*` requires the builder JWT). A
standalone tenant (no builder) read by a super admin -- a clinic of no
builder, or a builder user's unlinked home tenant -- has no account: zeros
and state `pending` (`standalone`). A builder reading its own unlinked home
tenant sees its builder's account instead, while a super admin viewing that
same tenant still sees the standalone zeros.

`GET /v1/credits/export` applies the same rule to a builder: the tenant's
stored builder must equal the claim, with the same single fallback (the
builder's own tenant linked to no builder, confirmed by the `users` row and
shared with no other builder's user) and the same `403
tenant_override_forbidden` for everything else. Both use
`src/api/helpers/builder_home_tenant.py`.

## Routes

All are `GET`, accept no body, take only the optional `tenant_id` query
parameter (any other query value is ignored) and answer `Cache-Control:
no-store`. They are documented in the OpenAPI under the `Credits` tag.

| Route | Answer |
|---|---|
| `/v1/credits/catalog` | units, the four detached packages (500/1,000/2,000/4,000 credits, card or PIX, valid 6 months), the subscription's payment method (card only, monthly) and the checkout endpoint |
| `/v1/credits/summary` | every panel below from **one** snapshot (same `as_of`) |
| `/v1/credits/balance` | `monthly_available_mc`, `monthly_not_started_mc` (an early renewal, usable from its `cycle_start`), `topup_available_mc`, `available_mc`, `open_debt_mc`, `net_available_mc`, `reserved_mc` (what the window's reservations debited) and the next expiry |
| `/v1/credits/lots` | every grant with `status` (`active`, `not_started`, `exhausted`, `expired`), granted, available, consumed, reversed and expired amounts |
| `/v1/credits/expiries` | lots still holding capacity, earliest expiry first |
| `/v1/credits/debt` | open debt and each open debt's origin (`consumption` or `reversal`) |
| `/v1/credits/reservations` | the current accounting window (`cycle`, `continuation` or `calendar`) with the RAG, media and egress coverage, what was debited, the download allowance and the bytes delivered |
| `/v1/credits/thresholds` | capacity in force, available, used, `used_percent`, the 50/90/100 % thresholds reached and the alerts recorded in the current arm |
| `/v1/credits/state` | `active`, `pending`, `grace`, `suspended` or `deletion_due`, the mode, `credit_enforcement_at` and, for an open exhaustion episode, `exhausted_at`, `suspension_due_at`, `deletion_due_at` and `export_deadline_at` |
| `/v1/credits/consumption` | the window's consumption by effect kind, `charged` (ledger) and `observed` (never charged) |
| `/v1/credits/export` | Group 5: the ZIP of the tenant's bytes (available in every state) |

The courtesy routes (creditos-cortesia, see [Cortesia](#cortesia)) are
super-admin admin routes, not builder reads:

| Route | Answer |
|---|---|
| `POST /v1/admin/credits/courtesy-grants` | `201`: grants a builder a courtesy lot (`{"builder_id", "credits", "reason"}`, header `Idempotency-Key`) |
| `POST /v1/admin/credits/courtesy-grants/{lot_id}/removals` | `201`: removes `credits` whole credits or, with `remove_all: true`, the lot's whole spendable balance (`{"credits"}` or `{"remove_all": true}`, plus `"reason"`; header `Idempotency-Key`) |
| `GET /v1/admin/credits/courtesy-grants?builder_id=` | the builder's courtesy lots (granted, available, consumed, `removed_mc`, expired, status and expiry) and every grant and removal with its actor and reason (super admins only); `404 builder_not_found` |

Balance and lot fields added by creditos-cortesia: `/v1/credits/balance`
carries `courtesy_available_mc` (the courtesy lots' spendable balance,
**already included** in `available_mc`, never added on top); every lot of
`/v1/credits/lots` and every entry of `/v1/credits/expiries` carries
`removed_mc` (what a super admin took back; `0` for monthly and top-up lots),
and a lot's `kind` may be `courtesy`.

### How the figures are computed

- Read-only: the stored rows of the builder (lots, per-lot entry sums,
  debts, settlements, ledger effect ids, releases) are assembled into a
  `LedgerBook` and swept at the database clock **in memory** -- the expiries
  due and the start catch-up of a started monthly allowance are applied to
  the view and never persisted. Usable capacity is
  `LedgerBook.available_mc`; open debt is the swept `LedgerBook.open_debt_mc`;
  the stored derived balance is never shown as usable. On PostgreSQL the
  rows come from one `REPEATABLE READ, READ ONLY` transaction, so a writer
  committing meanwhile never splits the snapshot and the database refuses
  any write. A read never creates the account, takes a lock or writes a row.
- `state` precedence: `deletion_due` > `suspended` > `grace` (the recorded
  transitions of the open episode; episodes exist only after
  `credit_enforcement_at`) > `pending` (`credit_pending`: a quarantined ASAAS
  order or cycle, a standalone tenant, or once enforced an unusable binding
  coverage (`reconciliation`)) > `active`. `pending_reasons` is a
  closed list: `quarantine`, `standalone` and, once enforced, `reconciliation`
  -- the binding coverage the enforced request path checks is not usable (an
  identity conflict between a current cycle and its binding, a current cycle
  without a binding outside its grace, or a top-up without an accepted
  fact -- the last two only when the builder has neither a monthly binding
  nor a usable top-up), so an
  enforced request answers `credit_pending` and the state is never an
  optimistic `active`. The reason is listed under a lifecycle state too; the
  quarantine is checked first and excludes it. A deadline already
  passed but not yet recorded by the lifecycle worker or the request gate is
  not anticipated: the state changes when the transition is recorded.
- The export deadline is the open episode's `deletion_due_at` (30 civil
  days from `exhausted_at`, America/Sao_Paulo).

## Buying credits

Credits sell nothing. ASAAS owns checkout, the subscription, the top-up
order, the payment, the catalog and `topup_commercial_enabled`; the credit
code never creates an order.

- A package is bought only through `POST /v1/billing/checkout` with
  `{"offer_id": "package_credits_<N>_v1", "method": "credit_card" | "pix"}`.
  The request schema forbids any other field: an amount, credits, a
  quantity, a builder or tenant, a validity, a callback or a coupon on a
  top-up is `422` and creates nothing.
- The subscription is a monthly credit-card subscription only: PIX, Pix
  Automático, an annual cycle and installments are refused.
- With `topup_commercial_enabled` false on the server (the default) a clean
  top-up is refused by the server and creates nothing.
- A package becomes balance only when its settled fact reached the credit
  inbox and its grant was applied to the ledger. A checkout return, a PIX QR
  code, a pending payment or a forged callback never changes a figure.

## Per-builder mode

cobranca-credito-por-builder: an operator can charge a chosen builder (the wish
scopes one; the code does not limit it) for real
before the global enforcement, with the one-off command `credit_charge_mark`
([`credit-charge-mark.md`](../runbooks/credit-charge-mark.md)). No route
writes a mark.

- The **effective mode** of a builder is the global `credit_enforcement_at`
  if set, else the builder's latest event of `credit_enforcement_marks` when
  it is a `mark` (from its `occurred_at`), else observational
  (`CreditRepository.effective_enforcement`).
- `mode` (summary, state, consumption) is computed from the account's builder,
  `scope.builder_id`, which since #219 also covers the builder's home tenant
  with no `builder_id`: `enforced` for a marked builder while the global is
  unset.
- `GET /v1/admin/builders/subscriptions` carries a read-only `charging` per
  row: `{"marked_at": <UTC ISO 8601>}` when the builder's latest event is a
  `mark`, else `null`; `null` on every row while the credit switch is off,
  the database is below `047` or the marks read failed. It means "operator
  mark", not "being charged" (master-admin badge "Cobrança ativa").

## Cortesia

Courtesy credits are credits a super admin gives a builder for free
(creditos-cortesia). They are a ledger lot of kind `courtesy`; nothing about
them touches ASAAS, a subscription, an order, the catalog,
`topup_commercial_enabled` or any billing table, and the builder pays
nothing.

- **Who grants**: a JWT `super_admin` only, through the master-admin
  "Créditos" action of the "Assinaturas por Builder" table (dialog "Dar
  cortesia" / "Tirar créditos") or the `courtesy-grants` API above. Any
  other caller is `403` (`jwt_required`, `super_admin_jwt_required`) before
  any credit SQL. The builder must exist (`404 builder_not_found`) and be
  active to receive a grant (`409 builder_inactive`); a removal stays
  allowed for an inactive builder.
- **Cap**: 1 to 100,000 whole credits per grant, a free amount; the dialog
  offers shortcuts of 5,000, 15,000 and 35,000 credits. A reason is
  required.
- **Validity**: fixed: the lot expires at the start of the civil day
  (America/Sao_Paulo) of the grant's civil date + 30 days, the same form as
  a top-up; "Expira em" shows that day. It is not configurable and is never
  extended (to extend, grant another courtesy).
- **Removal**: partial (`credits`, at most the lot's whole spendable
  credits) or total (`remove_all: true`, which also removes the millicredit
  fraction a partial removal cannot name). Only what the lot can still
  spend is removed, never what was consumed; an expired or zeroed lot
  refuses `remove_all` (both are `409 courtesy_removal_exceeds_balance`).
  A removal **reduces the funded capacity**: it is never counted as usage,
  so `used_mc` does not move and the lot shows the amount in `removed_mc`
  (a lot zeroed by removals is `exhausted`). `used_percent` is consumption
  over the reduced capacity: with nothing consumed it stays where it was;
  with consumption it rises (10,000 granted, 6,000 removed, 1,000 consumed
  reads 25 %), and a removal can cross the 50/90/100 % thresholds.
- **Capacity, thresholds and FEFO**: a courtesy lot adds to the capacity in
  force and to `available_mc` like any funded lot, so the 50/90/100 %
  thresholds are computed over it. Consumption spends the monthly lot first,
  then the other lots by earliest expiry (FEFO): a courtesy lot is spent
  after the monthly allowance and before a top-up that expires later.
  Under the courtesy rule a rule builder spends only its courtesy lots (see
  "Courtesy rule").
- **L10 coverage**: the cutoff report counts a builder whose only coverage
  is a usable courtesy lot (`usable_courtesy`: unexpired, positive balance)
  as `covered`, unless it is quarantined or in an identity conflict, like
  any source. It never feeds `usable` nor `reconciliation_failure`
  (`usable`, the absence of a quarantine, an identity conflict or a
  reconciliation failure, is unchanged), so a courtesy never hides an ASAAS
  divergence. `GET /v1/admin/credits/rollout/bindings/{builder_id}` reports
  `usable_courtesy`. Courtesy never changes the global enforcement instant;
  under the courtesy rule a builder's own instant is its first grant (see
  "Courtesy rule").
- **Idempotency**: every POST requires an `Idempotency-Key` (1 to 200
  characters), unique across **every** builder. The request's digest covers
  the operation, the builder, the lot of a removal and the whole body: the
  same key with the same request returns the recorded result (`201`,
  nothing written again); the same key on another operation, builder, lot
  or body is `409 idempotency_key_reused`. Refusals write nothing.
- **Gate and deployment order**: the routes answer `503 credit_unavailable`
  unless the credit switch is effective **and** the database is at `046`
  (a database at `045` keeps them closed with no SQL). The order is: the
  bridge image (contains `046`, writes no courtesy) → window `046` → the
  courtesy release. Never the reverse: window `046` under an image whose
  chain ends at `045` turns the whole credit switch off
  (`revision_outside_image_chain`) until an image containing `046` serves.
  See
  [`asaas-deployment.md`](../runbooks/asaas-deployment.md) ("Migration 046")
  and [`asaas-rollback.md`](../runbooks/asaas-rollback.md).
- **Do not grant courtesy before the new frontend is live**: an older
  `/billing` contract guard accepts only the `monthly` and `topup` lot
  kinds (it ignores the new fields), so a builder holding a courtesy lot
  would see a failed credit read.
- Operations: [`credit-courtesy.md`](../runbooks/credit-courtesy.md).

## Courtesy rule

debito-cortesia-automatico: a builder with no global enforcement and no
operator mark that ever received a `courtesy` lot is charged from its
courtesy automatically. Operations, the pre-deploy list and the rollback:
[`credit-courtesy-rule.md`](../runbooks/credit-courtesy-rule.md).

- **Precedence**: global > mark > courtesy. `CreditRepository.effective_enforcement`
  returns `origin` `courtesy`, `at` = the first grant's `occurred_at`, and
  `state` `charged` (spendable courtesy > 0) or `blocked` (0 spendable, no
  paid plan in force); with 0 spendable and a paid plan in force the
  builder is protected (`origin` `none`, observational). Gated on `046`
  (`tests/integration/test_credit_courtesy_rule_postgres.py`).
- **Paid plan in force** (`CreditRepository.paid_plan_in_force`): a
  `monthly`/`topup` lot with a future `expires_at`; an active subscription
  inside `credit_binding_grace_days` of its last active binding's
  `cycle_end`; or an active subscription whose current cycle is paid with
  no binding yet.
- **Debits**: only courtesy lots, FEFO; the rest is forgiven, never debt;
  `monthly` and `topup` are never touched. A turn with no reply
  (`trace_no_response`) costs zero.
- **Block**: immediate, no episode: `POST /v1/events` 402
  `credit_suspended`; webhooks 200 `{"ok": true, "skipped":
  "credit_suspended"}`; RAG/media uploads 402 before the body is read or
  anything is written (API key and panel); RAG search and media download by
  API key 402; `/v1/credits/state` `suspended` with null deadlines; the
  panel's reads and the export stay open
  (`test_charged_then_blocked_then_unblocked_over_http`,
  `tests/integration/test_credit_courtesy_surfaces_postgres.py`).
- **Channels**: `/v1/events`, Z-API, Meta, and Uazapi/Evolution only under
  the rule; under the rule `/v1/events` without `Idempotency-Key` answers
  400 `idempotency_key_required`.
- **Quarantine**: a charged builder in quarantine is served from courtesy;
  `/v1/credits/state` keeps `quarantine`, never `reconciliation`; the top-up
  stays blocked
  (`test_a_charged_builder_in_quarantine_is_served_from_courtesy_only`).
- `GET /v1/admin/builders/subscriptions` carries a read-only
  `courtesy_rule` per row (master-admin "Cortesia: cobrando" / "Cortesia:
  bloqueado").

## UI

The billing page is `frontend/artifacts/neurostack/src/pages/billing.tsx`
("Pagamento"); its credit area is
`src/components/billing/credits-and-topups.tsx` (`CreditsAndTopups`), fed
only by the page. The page reads the credit routes through `readCredits`
(`src/pages/billing-contract.ts`), which keeps the outcomes apart:
`unavailable` is **only** `503 {"code": "credit_unavailable"}`; another 5xx,
a network failure or a body that fails the contract guard (including a
catalog whose `millicredits_per_credit` is not 1,000) is an error: a failed
credit summary or lots read is shown with a retry, a failed catalog read
blocks the top-up with a text reason and no retry control; `401`/`403` are
refusals. What may be shown or bought is derived in
`src/pages/billing-view.ts`, from server readbacks only.

- **Reads** (`/billing`, builder): one `GET /v1/credits/summary` per visit
  (and on "Atualizar estado"); only after it answered `200`, the lots
  (`GET /v1/credits/lots`) and the credit catalog (`GET /v1/credits/catalog`).
  Nothing is polled, cached or paginated.
- **Credits not launched**: with `503 credit_unavailable` the whole area
  "Créditos e recargas" is absent (heading, number and any line); the page
  shows "Assinatura e uso" and "Planos" (01, 02) and asks nothing more. Any
  other credit failure keeps the area with an announced error and a retry.
- **Balance**: the balance by origin (monthly allowance, next cycle's
  allowance, top-ups, debt, cycle reservations, net available), expiries,
  open debt, the window's allocations, the capacity used and the thresholds,
  the consumption (charged, or estimated and not charged while
  observational), the lifecycle notice (grace with the suspension deadline,
  suspended, deletion due, with "Exporte seus dados até ...") and the export
  button.
- **Hold**: `creditLedgerStanding` (`billing-view.ts`) is the single rule.
  The `pending` state, or **any** `pending_reasons` entry other than
  `standalone` (`quarantine`, `reconciliation`, a reason the UI does not
  know) under **every** lifecycle state (`active`, `grace`, `suspended`,
  `deletion_due`), puts the ledger on hold: the balance figures are withheld,
  each reason is named as text, the lifecycle's deadline and export date
  stay, the history labels spendable lots "Retido", and the top-up is
  blocked with its reason. `standalone` (no builder) as the **only** reason,
  under any state, reads "Não se aplica", without figures or history,
  and offers no top-up action; next to another reason it reads "Não se
  aplica" under `pending` and is a hold under any other state.
- **History**: the lots are the only history source (date, origin, credits,
  status, "Expira em"), newest first, unpaginated; `lot_id` is never
  rendered. A courtesy lot reads origin "Cortesia" (never a top-up) and,
  when something was removed, "Retirados"; the balance shows a "Cortesia"
  line (`courtesy_available_mc`).
- **Top-ups** (`deriveTopupState`): `Recarregar` exists only when
  `GET /v1/billing/summary` read back `topup_commercial_enabled: true`, the
  credit summary answered, the ledger is not on hold, no checkout is live
  (the summary's `live_session` or this tab's stored session, read or not
  yet read: it blocks subscribing too) and the credit catalog answered; the
  packages are the catalog's, filtered by `summary.checkout.available_offer_ids`.
  While the catalog is loading nothing is shown; a failed catalog read is a
  blocked state with its reason. A closed sale has no card or dead button
  (there is no "Recargas indisponíveis" card). The client sends only
  `offer_id` and `method`, follows the backend-built `checkout_url` of an
  `active` session, and otherwise moves focus to the checkout status. The
  subscription stays in "Planos", card only.
- **After a return or a PIX QR code** the page re-reads the order
  (`GET /v1/billing/checkout/{id}`) and the ledger (`GET
  /v1/credits/summary`); it never adds a package to the balance by itself
  and never polls.
- **Export**: the button calls `GET /v1/credits/export` with the builder
  session and downloads the ZIP; a `503` makes it unavailable.
- **Knowledge base and media**:
  `src/components/plan-usage-summary.tsx` keeps only the allocation notice
  (`readCredit`, which hides it on any answer other than a valid `200`): a
  builder or super admin viewing a clinic sees that clinic's builder's RAG
  or media allocation of the current window
  (`GET /v1/credits/reservations?tenant_id=...`); clinic admins read nothing
  and, with the switch off, nothing renders.

## Tests

- API: `tests/api/test_credits.py`, `tests/api/test_credit_tenant_boundaries.py`,
  `tests/api/test_billing_topups.py`, `tests/api/test_credit_openapi.py`.
- Dominance (real PostgreSQL at `037` and at the credit head, config off and
  on, with and without the enforcement): the Group 6 cases of
  `tests/fixtures/credit_dominance/requests.json`, asserted by
  `tests/integration/test_credit_schema_dominance_postgres.py`
  (`bash scripts/check-credit-integration.sh ...` with
  `POSTGRES_SMOKE_DATABASE_URL`).
- Forced switch: `pytest -p tests.credit_switch_forced_plugin ...` counts
  the credit read snapshots in its summary.
- UI (under `frontend/artifacts/neurostack/`): `src/pages/billing.test.tsx`
  (page: reads, absent area, hold, top-ups, focus, return),
  `src/pages/billing-view.test.ts` (`creditLedgerStanding`,
  `deriveTopupState`, plan marker), `src/pages/billing-contract.test.ts`
  (guards, `readCredits`), `src/components/billing/credits-and-topups.test.tsx`,
  `src/components/plan-usage-summary.test.tsx`,
  `src/pages/knowledge-base.test.tsx`, `src/pages/midias.test.tsx`, and the
  Playwright suite `tests/e2e/billing.spec.ts`.
