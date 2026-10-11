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
