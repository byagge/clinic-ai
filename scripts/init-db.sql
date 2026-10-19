-- Database bootstrap, executed once by the Postgres entrypoint.
--
-- Two things happen here:
--
-- 1. Extensions are created by the superuser. The application role is not a
--    superuser (on purpose, see below), so it could not create them itself,
--    and the Alembic migration only has to run `CREATE EXTENSION IF NOT
--    EXISTS`, which is then a no-op.
--
-- 2. A dedicated, non-superuser application role is created. This is not
--    cosmetic: Postgres skips row level security for superusers, so connecting
--    the app as `postgres` would silently disable the tenant-isolation
--    policies the migration installs.
\set ON_ERROR_STOP on
\set app_user `echo "${ARIX_APP_USER:-arix_app}"`
\set app_password `echo "${ARIX_APP_PASSWORD:-arix}"`

CREATE EXTENSION IF NOT EXISTS btree_gist;   -- visits exclusion constraint
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy knowledge-base search

-- Optional: present in the pgvector image, absent in plain postgres. Embedding
-- search degrades to full text when it is missing, so never fail the bootstrap.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector is not available, knowledge-base search will use full text only';
END $$;

CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password';

-- The app role owns the tables it creates through Alembic, which is what lets
-- it enable RLS on them; FORCE ROW LEVEL SECURITY (set in the migration) keeps
-- the policies applying to the owner too.
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_user";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
