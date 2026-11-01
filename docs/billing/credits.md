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
clinic moved to another builder is refused to the old builder's token). A
clinic of another builder and a tenant that does not exist answer the same
`403 {"detail": {"code": "tenant_override_forbidden"}}` -- same status, body
and headers -- before any `credit_*` or `billing_*` table is read, so before
any lock, order, ledger, outbox, storage or receipt. Builder finances are
builder-only, as on `main` (`/v1/billing/*` requires the builder JWT). A
standalone tenant (no builder) has no account: zeros and state `pending`
(`standalone`).

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
  rendered.
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
