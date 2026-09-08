-- Roles are cluster-wide, not per-database, so this can race against another
-- pytest process (e.g. a second worktree) hitting the same shared testdb
-- container concurrently -- both can pass the IF NOT EXISTS check before
-- either commits. Catching duplicate_object (mirroring _ensure_database()'s
-- own DuplicateDatabase guard in pgdevkit/testdb/api.py) is race-safe in a
-- way "IF NOT EXISTS (SELECT ...) THEN CREATE" is not, since it lets
-- Postgres's own uniqueness check -- not a separate lookup -- be the source
-- of truth.
DO $$
BEGIN
    CREATE ROLE pgdevkit_test_reader LOGIN PASSWORD 'testpwd';
EXCEPTION WHEN duplicate_object THEN
    NULL;
END
$$;

GRANT USAGE ON SCHEMA app TO pgdevkit_test_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO pgdevkit_test_reader;
