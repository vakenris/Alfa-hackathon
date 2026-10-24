-- =============================================================================
-- PostgreSQL initialization script for Alfa Business News Analytics
-- Runs once on first container start (mounted as /docker-entrypoint-initdb.d/)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. pgvector extension
--    Required by Role 2 (Data/ETL) for embedding-based news similarity search.
--    Safe to run even if the extension is already installed.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 2. pg_trgm — trigram index for fast ILIKE / similarity search on company
--    names and news titles (used by feed filtering and search endpoints).
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- 3. btree_gin — allows GIN indexes on scalar columns (status, category, etc.)
--    useful for combined filters in analytics queries.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS btree_gin;

-- ---------------------------------------------------------------------------
-- 4. Default search configuration — Russian + English
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_ts_config WHERE cfgname = 'russian_english'
    ) THEN
        CREATE TEXT SEARCH CONFIGURATION russian_english (COPY = russian);
        ALTER TEXT SEARCH CONFIGURATION russian_english
            ALTER MAPPING FOR hword, hword_part, word
            WITH russian_stem, english_stem;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 5. Application user — grant permissions (user already created by POSTGRES_USER)
-- ---------------------------------------------------------------------------
GRANT ALL PRIVILEGES ON DATABASE alfa_analytics TO alfa_user;
GRANT ALL ON SCHEMA public TO alfa_user;

-- ---------------------------------------------------------------------------
-- 6. Confirm
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'Alfa Analytics DB initialised: vector=%, trgm=%, btree_gin=%',
        (SELECT installed_version FROM pg_available_extensions WHERE name = 'vector'),
        (SELECT installed_version FROM pg_available_extensions WHERE name = 'pg_trgm'),
        (SELECT installed_version FROM pg_available_extensions WHERE name = 'btree_gin');
END
$$;
