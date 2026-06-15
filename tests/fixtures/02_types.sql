CREATE TYPE myapp.status AS ENUM ('active', 'inactive', 'pending');

CREATE TYPE myapp.address AS (
    street TEXT,
    city TEXT,
    zip TEXT
);
