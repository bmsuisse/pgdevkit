DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'mood' AND typnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'app')
    ) THEN
        CREATE TYPE app.mood AS ENUM ('happy', 'sad');
    END IF;
END$$;
