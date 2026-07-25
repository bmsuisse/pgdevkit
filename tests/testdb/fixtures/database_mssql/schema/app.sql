IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'app')
BEGIN
    EXEC('CREATE SCHEMA app');
END
