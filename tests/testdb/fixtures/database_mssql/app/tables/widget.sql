IF NOT EXISTS (SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = 'app' AND t.name = 'widget')
BEGIN
    CREATE TABLE app.widget (
        id INT PRIMARY KEY,
        name NVARCHAR(100) NOT NULL,
        -- NVARCHAR(MAX)-storing-JSON, not the native `json` type (SQL
        -- Server 2025+) -- the CI container image (2022) predates it, and
        -- this convention is what the write-side JSON-encoding fix in
        -- db/mssql_sql.json_encode_value covers either way (mssql-python
        -- has no auto dict/list serialization for either column style).
        tags NVARCHAR(MAX) NULL
    );
END
