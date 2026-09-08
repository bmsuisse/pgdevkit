-- Filename ("event.sql") deliberately sorts alphabetically before its own FK
-- target's filename ("event_kind.sql") -- '.' (0x2E) sorts before '_' (0x5F) --
-- so this file's first apply attempt always fails (app.event_kind doesn't
-- exist yet) and it only actually gets created via _iter_sql_files()'s
-- delayed-retry pass, which runs after every non-delayed file has already
-- been attempted. See test_apply_schema_applies_permissions_after_a_delayed_table.
CREATE TABLE IF NOT EXISTS app.event (
    id serial PRIMARY KEY,
    event_kind_id int NOT NULL REFERENCES app.event_kind(id)
);
