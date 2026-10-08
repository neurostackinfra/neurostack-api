# Media release evidence

- Document version: 2026-10-08
- Audience: operator who runs the rollout gate and the human approver who
  signs the go/no-go decision
- Scope: operational preflight for the `media-assets-catalog` wish; this
  file records the commands run, the environment measured, the capacity
  observed, and the explicit go/no-go verdict
- Policy: the rollout is fail-closed. Every section below must be filled
  with real values collected on the actual production target before the
  release is cleared. Absence of a measurement is BLOCKED, never a skip.

The gate `scripts/check_media_release.py` reads this document and refuses
to clear the release if a required section is missing, if the evidence is
empty, or if any placeholder marker (`TBD`, `pending`, `n/a`) remains.

## Commands

The authoritative preflight command is the exact invocation shipped with
Group 8 of this wish:

```bash
poetry install --sync && \
poetry run pytest tests/scripts/test_check_media_release.py -v && \
poetry run python scripts/check_media_release.py \
    --evidence docs/evidence/media-release-evidence.md \
    --junit-dir .artifacts/media-release && \
test ! -e .artifacts/media-release && \
test -z "$(git ls-files .artifacts)" && \
make quality && \
pnpm --dir frontend run typecheck && \
pnpm --dir frontend/artifacts/neurostack run test && \
pnpm --dir frontend/artifacts/neurostack run build
```

Operator must copy the exit code and the full stdout/stderr into their
run log. The gate accepts only `exit 0` as a go-signal; `exit 2` is a
BLOCKED (operational evidence missing); `exit 1` is a hard failure.

### Supplementary commands

- `poetry run python scripts/check_media_dependencies.py` — invoked
  automatically by the release gate but may be re-run alone to inspect
  dependency drift.
- `poetry run pip-audit --local --strict --format json` — invoked
  automatically; operator may re-run with `--format columns` for a human
  summary when triaging an advisory.
- `bash scripts/backup.sh && bash scripts/restore_backup.sh` — exercised
  on the target when gathering the DB/WAL/backup fixture measurements
  below.

## Owner

- Primary operator: fill with the pager-on-call engineer for the
  `media-assets-catalog` rollout (first + last name, SSO email).
- Secondary operator / observer: fill with the second engineer paired
  for the four-eyes principle during the rollout window.

The operator is accountable for executing the preflight command,
collecting the measurements, filling this document, and raising a
go/no-go decision. They must not clear the release without the explicit
approvals recorded in the Approvals section.

## Environment

- Deployment target name: fill with the production environment label
  (for example `prod-sa-east-1`); must match the record in the operator
  runbook.
- Target host(s): fill with the hostnames or managed service identifiers
  of the PostgreSQL primary and the backup destination.
- PostgreSQL version: fill with the exact server-reported version.
- Python interpreter used by the gate: fill with the exact
  `python --version` of the Poetry virtualenv on the operator host.
- `POSTGRES_SMOKE_DATABASE_URL`: must be set when the gate runs; its
  content is secret and must not be transcribed here. Only its presence
  and the DB/host it points to (role name, host, dbname) are recorded.
- `BACKUP_AGE_RECIPIENT`: must be set; only the public recipient prefix
  is recorded here. The private identity never appears in this file.
- Backup destination path: fill with the directory or object store URI
  where `.age` artefacts land.

## Approvals

- Rollout approver: fill with the name and role of the human who signed
  the go-decision (for example product lead, infrastructure lead).
- Security approver: fill with the name and role of the human who signed
  off on the encryption-at-rest and age key custody arrangements.
- Approval timestamp: fill with the exact UTC timestamp (ISO 8601) at
  which the two approvals were recorded in the audit log.

The approvals are a precondition for clearing the release. Without named
humans and a timestamp this section is incomplete and the gate must not
be considered passed.

## Encryption

Record the proof that data remains encrypted in motion, at rest, and in
backup form on the actual target:

- Database at rest: fill with the storage encryption mechanism
  (filesystem, block device, managed service encryption key id).
- WAL encryption: fill with the status of PostgreSQL WAL encryption
  (filesystem-level, archive-side encryption settings).
- Backup encryption: `scripts/backup.sh` pipes `pg_dump` into `age`
  with the configured recipient; record the recipient fingerprint and
  the output path of the fixture `.age` artefact.

An absent or default value in any of the three bullets must be treated
as evidence missing — not as a passing measurement.

## Age key custody

- Private identity file location: fill with the hardened path on the
  operator host (never a shared drive). The identity file must not be
  committed to the repository.
- Custodian: fill with the human responsible for rotation and
  disaster-recovery access.
- Rotation policy: fill with the retention/rotation schedule applied to
  the age identity (for example 90 days with a dual-custody re-key).
- External backup of the identity: fill with the sealed/offline
  destination that stores the recovery copy (for example a vault or
  hardware token identifier).

## Owners and headroom

Capture the current catalog shape so that the rollout decision can be
anchored to real numbers rather than a projection:

- Builder owners count: fill with the number of distinct Builder owners
  already present in the production database.
- Independent clinic owners count: fill with the number of distinct
  tenants that act as independent clinic owners.
- Per-owner storage cap: `MEDIA_STORAGE_CAP_BYTES = 500_000_000` bytes
  (500 MB) — not configurable at runtime.
- Allocated capacity: fill with the current `SUM(size_bytes)` divided by
  the aggregate cap `owners_count * 500_000_000` to show how much of the
  rated capacity is already consumed.
- Free capacity headroom: fill with the DB free space and the backup
  destination free space in bytes. A projection that drives either
  resource beyond 70% is a no-go trigger.

## Fixture measurements

The 500 MB fixture must be measured on the real target (not on the
operator's laptop):

- Fixture size: 500 MB (`500_000_000` bytes), composed of representative
  mixes of the five accepted media types.
- DB delta: fill with the measured growth of `pg_database_size` (bytes)
  before and after the fixture is inserted.
- WAL delta: fill with the measured growth of
  `pg_stat_wal.wal_bytes` (or equivalent collector) over the ingest
  window.
- Backup fixture size: fill with the size of the `.age` artefact
  produced for the 500 MB fixture.
- Backup duration: fill with the wall-clock duration of
  `scripts/backup.sh` for the fixture.
- Restore duration: fill with the wall-clock duration of
  `scripts/restore_backup.sh` for the same fixture into a disposable
  database.
- Fixture ingest completion status: fill with `success` or `failed`
  including the exact subprocess exit codes.

Any single value here that is unknown is a BLOCKED outcome: the capacity
trigger cannot be evaluated without numbers, and the release gate must
return exit code 2 until the measurement is collected.

## Decision

The go/no-go decision is derived from the measurements above using the
objective triggers from the wish:

- Trigger A: projected DB or backup destination occupancy of the current
  owners reaches or exceeds 70% of allocated capacity => NO-GO.
- Trigger B: fixture ingest drives DB, WAL, or backup destination to or
  past 70% of capacity => NO-GO.
- Trigger C: backup or restore of the fixture exceeds 30 minutes =>
  NO-GO.
- Trigger D: any required measurement is unavailable => BLOCKED (not a
  skip). The gate exits with code 2 and the rollout waits for the data.

If any trigger fires the operator records the no-go verdict here with a
timestamp, opens a dedicated object-storage wish, and does not clear the
release.

### Current status (2026-10-08)

- `POSTGRES_SMOKE_DATABASE_URL` is unset in the delivery environment used
  to prepare this document, so the DB/WAL/backup/restore measurements and
  the real owner counts cannot be gathered inside the engineering
  worktree.
- Consequence per Trigger D: the rollout is **BLOCKED — operational
  measurement pending**. No go/no-go verdict is recorded here. The gate
  (`scripts/check_media_release.py`) exits with code 2 under these
  conditions, which is the fail-closed path mandated by the wish.
- Next action: an operator with access to the production target must set
  `POSTGRES_SMOKE_DATABASE_URL`, re-run the preflight command, collect
  the measurements requested in the sections above, replace the
  placeholder bullets with real values, obtain the two approvals, and
  only then record the final verdict.

The document is published in this incomplete state so the gate has a
real file to validate; the gate itself refuses to clear the release
because every section demanding a real measurement still carries the
"fill with" instruction rather than captured data.
