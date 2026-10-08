# Encrypted media backup rollout

The media catalog must not be enabled in production until PostgreSQL, WAL, and
the backup destination have independently verified encryption at rest. The
repository backup path adds recipient-based `age` encryption; it does not
replace those infrastructure controls.

## Provisioning

1. Install `age` so both `age` and `age-keygen` are available to the backup
   operator.
2. Generate the production identity on an isolated administrator workstation.
   Store the private identity in the approved secret manager and recovery
   escrow. Do not place it in this repository, the application container, an
   environment file, shell history, or logs.
3. Put only the corresponding public recipient in the backup job's
   `BACKUP_AGE_RECIPIENT` secret. Set `BACKUP_DIR` and `RETENTION_DAYS` for the
   encrypted destination.
4. Give the backup operator write access to the destination and the minimum
   Docker/PostgreSQL access needed by `scripts/backup.sh`.

An intentionally invalid placeholder is present in `.env.example`; rollout
must replace it through the deployment secret manager. The script validates
`age`, `age-keygen`, and the recipient before invoking `pg_dump`.

## Backup and retention

Run from the repository directory:

```bash
BACKUP_AGE_RECIPIENT="$SECRET_MANAGER_RECIPIENT" scripts/backup.sh
```

The script streams `pg_dump` through gzip and `age` into a mode-`0600`
encrypted temporary in the destination. It publishes by creating an exclusive
same-filesystem hard link at `postgres_<timestamp>.sql.gz.age`, then removes the
temporary name. If that destination already exists, publication fails without
overwriting the existing recovery point and cleanup removes the losing
temporary. A failed pipeline stage likewise removes the encrypted temporary and
publishes nothing. No durable plaintext dump is created.

Automated retention applies only to regular files in `BACKUP_DIR` whose
basename matches `postgres_*.sql.gz.age`. Legacy `.sql.gz`, volume archives,
off-pattern `.age` files, directories, nested artifacts, and lookalikes are not
deleted. Handle them only under a separately approved migration or disposal
procedure after reconciling the legacy inventory.

## Isolated restore drill

Restore only into a newly created disposable or recovery database. Materialize
the private identity as a restrictive temporary secret file, set the target
URL without printing it, and run:

```bash
BACKUP_AGE_IDENTITY=/run/secrets/neurostack-backup-age-identity \
RESTORE_DATABASE_URL="$RECOVERY_DATABASE_URL" \
RESTORE_EXPECTED_DATABASE="$RECOVERY_DATABASE_NAME" \
scripts/restore_backup.sh /opt/backups/neurostack/postgres_<timestamp>.sql.gz.age
```

The expected database name is a required destructive-operation confirmation.
Before decrypting any backup bytes, the script moves URL credentials into a
mode-`0600` temporary libpq password file, removes the URL from its environment,
queries `current_database()`, and requires both the parsed and connected names
to equal `RESTORE_EXPECTED_DATABASE`. It always refuses `postgres`, `template0`,
and `template1`.

Validate application-specific sentinels, row counts, schema state, and owner
access before declaring the drill successful. Destroy the disposable database
and revoke or remove the temporary identity material after the drill. The
automated test generates a test-only identity, restores a sentinel into a
disposable PostgreSQL database, and cleans both databases and key material.

## Production evidence and rollback

Before rollout, attach evidence for:

- encrypted PostgreSQL data volumes and WAL;
- encrypted backup destination and access policy;
- a passing isolated backup/restore drill with durations and integrity checks;
- external identity custody, recovery access, rotation owner, and retention;
- review of `docs/evidence/media-legacy-backup-inventory.md`.

If any proof is missing, backup or restore fails, or a plaintext artifact is
observed, stop rollout. Preserve evidence, revoke exposed material if needed,
and repair forward; never weaken encryption or broaden retention deletion.
