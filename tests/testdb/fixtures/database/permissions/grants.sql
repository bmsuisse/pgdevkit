DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pgdevkit_test_reader') THEN
        CREATE ROLE pgdevkit_test_reader LOGIN PASSWORD 'testpwd';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA app TO pgdevkit_test_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO pgdevkit_test_reader;
