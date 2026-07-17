-- Deliberately destructive: must be skipped by apply_schema's additive-only
-- filter, not applied. See test_apply_schema_skips_unsafe_migrations.
ALTER TABLE app.gadget DROP COLUMN note;
