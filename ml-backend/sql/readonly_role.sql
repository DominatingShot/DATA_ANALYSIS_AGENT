-- =====================================================================
-- Read-only role for the analytics app. Run as the database owner / an admin.
--
--   psql "<admin connection string>" -v reader_password="'change-me'" -f backend/sql/readonly_role.sql
--
-- Then point DATABASE_URL at this role:
--   postgresql://alankar_reader:change-me@host:5432/<db>
--
-- The app can only SELECT from dim_menu and fact_sales. Python-side checks
-- (sqlglot validation, READ ONLY transactions) are a second layer on top.
-- =====================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alankar_reader') THEN
        CREATE ROLE alankar_reader LOGIN;
    END IF;
END $$;

ALTER ROLE alankar_reader WITH LOGIN PASSWORD :reader_password
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT 20;

-- Belt and braces at the role level: every session starts read-only and time-boxed.
ALTER ROLE alankar_reader SET default_transaction_read_only = on;
ALTER ROLE alankar_reader SET statement_timeout = '15s';
ALTER ROLE alankar_reader SET idle_in_transaction_session_timeout = '60s';

-- Nothing by default, then exactly what is needed.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM alankar_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;           -- no-op on PG15+, needed on PG14 and older
REVOKE CREATE ON SCHEMA public FROM alankar_reader;
REVOKE TEMPORARY ON DATABASE :"DBNAME" FROM PUBLIC;   -- no temp tables either

GRANT CONNECT ON DATABASE :"DBNAME" TO alankar_reader;
GRANT USAGE ON SCHEMA public TO alankar_reader;
GRANT SELECT ON dim_menu, fact_sales TO alankar_reader;
