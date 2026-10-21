-- PostgreSQL bootstrap intentionally owns extensions only.
--
-- Alembic is the sole schema authority.  The development Compose stack migrates
-- this local database to the head in its mandatory migration service, after
-- scripts/guard_alembic_upgrade_head.py accepts it as a development database,
-- before the API can start.  Every other environment is migrated to the
-- revision approved for it (`alembic upgrade <revision>`).  Do not create or
-- stamp application tables here: doing so would leave
-- a partial, unstamped schema that cannot enter the revision chain safely.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

SELECT 'PostgreSQL extensions initialized; Alembic owns schema creation.' AS status;
