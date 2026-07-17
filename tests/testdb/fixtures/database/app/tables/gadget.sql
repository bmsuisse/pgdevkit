CREATE TABLE IF NOT EXISTS app.gadget (
    id serial PRIMARY KEY,
    mood app.mood NOT NULL,
    size app.dimensions,
    tags jsonb
);
