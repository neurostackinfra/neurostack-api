# Billing catalog provenance

The immutable `payment-2026-10-03` catalog was transcribed from the external
source document `NeuroStack_Estrutura_de_Pagamento.pdf`, updated 2026-10-03,
with 6 pages and SHA-256:

```text
58e71858ae25d12b76db4c72aa7f6838cb760187ecdb841fdf56d729aca86aaf
```

The PDF is intentionally not copied into this repository. The release source
of truth is the backend-owned snapshot in `src/services/billing_catalog.py`.

Published catalog versions are append-only contracts. A commercial change
must introduce a new `catalog_version_id`; it must never edit a previously
published version retroactively. Additional-clinic charges always grant zero
credits.

## Release UI contract (checkout wiring)

This section covers only how the builder page wires the hosted checkout. The
page layout, plan comparison, credits area and state matrix belong to the
separate `tela-pagamentos` wish.

- The page offers the three `MONTHLY` offers (`plan_plus_monthly_v1`,
  `plan_pro_monthly_v1`, `plan_max_monthly_v1`) and sends only
  `{offer_id, method: "credit_card"}`, plus an optional server-validated
  `coupon_code`. It never sends price, cycle, quantity, installments or
  payment details, and never offers annual billing or PIX for a subscription.
- It navigates to the `checkout_url` the API returns, which is built by the
  backend only for an `active` session.
- A return from the hosted checkout is navigation only. Status is read back
  from `GET /v1/billing/summary` and `GET /v1/billing/checkout/{id}`; the
  query string never activates a plan and the page does not poll.
- Credit packages (`DETACHED` offers) are not purchasable from the page. A
  disabled browser control is not the security boundary: top-up creation is
  gated server-side and the commercial top-up is deferred.
