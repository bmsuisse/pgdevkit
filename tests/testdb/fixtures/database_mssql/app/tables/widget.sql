IF NOT EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = 'app' AND t.name = 'widget')
BEGIN
    CREATE TABLE app.widget (
        id INT PRIMARY KEY,
        name NVARCHAR(100) NOT NULL
    );
END
