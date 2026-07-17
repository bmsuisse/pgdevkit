DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'dimensions' AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'app')
    ) THEN
        CREATE TYPE app.dimensions AS (
            width int,
            height int
        );
    END IF;
END$$;
