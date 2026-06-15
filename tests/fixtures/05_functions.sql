CREATE OR REPLACE FUNCTION myapp.greet(name TEXT)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN 'Hello, ' || name || '!';
END;
$$;
