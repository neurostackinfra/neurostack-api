# Media legacy backup inventory

- Inventory time: 2026-10-07 (America/Sao_Paulo)
- Operator: `media-g2-backup`
- Mode: read-only; no legacy artifact was modified or deleted
- Roots inspected: repository fixtures and the configured default
  `/opt/backups/neurostack`
- Name classes: `postgres_*.sql.gz`, `postgres_*.sql.gz.age`,
  `*_data_*.tar.gz`

## Findings

| Artifact | Type | Size | Modified | Owner / custodian | Recommended action |
|---|---|---:|---|---|---|
| Repository fixtures: none found | — | — | — | Repository maintainers | Keep fixtures free of captured production backups. |
| `/opt/backups/neurostack`: root not present | Missing configured local root | — | — | Unresolved — production destination unavailable locally | Resolve the production custodian and inventory the real destination before rollout. |

The local discovery found no repository fixture requiring classification, but
it could not inspect the absent configured local root. The production
destination is not evidenced by repository configuration and remains
evidence-required; this inventory does not claim it is empty.

## Action policy

For every artifact found in a deployment environment, append its exact path,
type, size, modification time, encryption status, custodian, and one of these
actions: retain under restricted access, migrate to an approved encrypted
destination after a verified restore, or dispose under an independently
approved retention procedure. This inventory is evidence only. It must never
delete, rename, encrypt in place, or otherwise mutate a legacy backup.

The repository retention script intentionally ignores plaintext `.sql.gz`,
`*_data_*.tar.gz`, off-pattern `.age` files, directories, nested files, and
lookalike names. Their presence requires explicit review rather than automatic
cleanup.
