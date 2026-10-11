-- PostgreSQL bootstrap intentionally owns extensions only.
--
-- Alembic is the sole schema authority.  The development Compose stack runs
-- `alembic upgrade head` in its mandatory migration service before the API can
-- start.  Do not create or stamp application tables here: doing so would leave
-- a partial, unstamped schema that cannot enter the revision chain safely.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

SELECT 'PostgreSQL extensions initialized; Alembic owns schema creation.' AS status;
