# `database/` folder layout

The Postgres schema is versioned as plain `.sql` files under a `database/`
folder in the repo — that folder *is* the source of truth for the schema.
`pgdb testdb` (see [`pgdevkit/testdb/schema.py`](../pgdevkit/testdb/schema.py))
applies it to a local test database; a human applies the same files to
production.

This is the single source for the convention — don't duplicate this table or
these rules elsewhere; link here instead.

---

## Layer directories

Top-level directories group tables/objects by conceptual layer — one
directory per Postgres schema, or per logical grouping within one schema.
Names and count are entirely per-project; a generic example:

```
database/
├── schema.sql            # CREATE SCHEMA statements
├── 0_public/              # shared types/functions usable from anywhere
├── 1_reference_data/      # dimension / reference tables
├── 2_transactional/       # fact / transactional tables
├── 3_app/                 # user-editable, app-owned tables
├── 4_reporting/           # aggregated / statistics tables
├── permissions.sql        # grants
```

The leading number is a **display/sort aid only** — it groups related
folders together in a file listing so a human can scan them top-to-bottom in
a sensible order. It does not control apply order: `pgdb testdb` applies
files by object-type priority (below) and resolves cross-file dependencies
itself, regardless of which numbered folder a file sits in. Feel free to
renumber layers for readability without worrying about breaking anything.

---

## Object-type subfolders, in apply order

Within a layer directory, group files by object type. This is what actually
controls apply order, across all layer directories — cross-file dependencies
between objects of the same type (e.g. one view selecting from another) are
resolved automatically; this table only fixes the order *between* types.
This table must match `_TYPE_ORDER` / `_SCHEMA_QUALIFIED_TYPES` in
[`pgdevkit/testdb/schema.py`](../pgdevkit/testdb/schema.py) exactly — update
both together.

| Priority | Directory | Object type |
|---|---|---|
| 1 | `schema` | `CREATE SCHEMA` |
| 2 | `types` | Custom types / enums |
| 3 | `tables` | Tables |
| 4 | `scalar_functions` | Scalar functions |
| 5 | `functions` | Functions |
| 6 | `views` | Views |
| 7 | `table_functions` | Table functions |
| 8 | `procedures` | Procedures |
| 100 | `permissions` | Grants |
| 101 | `indexes` | Indexes |

One object per file: `tables/user.sql`, `views/all_edits.sql`,
`types/measurement_unit.sql`.

---

## File-naming conventions

| Suffix | Meaning |
|---|---|
| `<name>.sql` | The object's live definition (`CREATE TABLE`, `CREATE OR REPLACE VIEW`, ...) |
| `<name>.test_data.json` | Seed rows for a table — a JSON array of row objects, loaded after the table is created |
| `<name>.init.sql` | One-time setup for an object (e.g. a backfill), run once, kept separate from the reusable definition |
| `<name>.prod.sql` / `.prod` anywhere in the name | Production-only (real permission grants, real user accounts) — skipped by `pgdb testdb` |
| `all.sql` | Generated concatenation of the whole tree — not hand-edited, not committed |

---

## Migrations

One-off, non-idempotent changes (rename/drop column, backfill, data fix) go
in a `migrations/` (or `_migration_scripts/`) folder — one file per change,
named by date:

```
database/migrations/2026-07-10_customer_geocode.sql
```

Rules:

- Skipped by `pgdb testdb` — it only applies the layer directories above.
- Update the corresponding table/view `.sql` file in the same change so its
  definition already reflects the new shape — the migration and the
  source-of-truth file must never drift apart.
- Applied to production manually, once, by a human, after being verified
  locally.
- Never edited after being applied — a further change gets a new dated file.

---

## `COMMENT ON` — document schema in the object's own file

Add a `COMMENT ON` for every table, and for any column whose purpose isn't
obvious from its name and type (flags, status codes, denormalized fields,
units). Put it directly in the table's `.sql` file, right after the
`CREATE TABLE` — not in a migration, wiki, or README. It lives with the
definition it describes and survives `\d+` / `pg_catalog` inspection.

```sql
-- database/1_reference_data/tables/user.sql
create table dim.user (
    id bigint generated always as identity primary key,
    email text not null,
    is_active boolean not null default true
);

comment on table dim.user is 'End-user accounts; one row per registered person.';
comment on column dim.user.is_active is 'False once a user is soft-deleted; keep for audit trail.';
```

---

## Backfilling untracked objects

If a table, scalar function, or table function was created directly on the
database and never got a `.sql` file, use `pgdb fetch-missing` to find it and
generate one — see `pgdb fetch-missing --help`. It connects to Postgres,
diffs the live schema against what's already tracked under `database/`, and
for each object you select, reverse-engineers its DDL into the matching
layer folder's `tables/`, `views/`, `scalar_functions/`, or
`table_functions/` subfolder. The layer folder is matched by schema name
against the existing top-level directories under `database/`, ignoring their
leading sort number.

---

## Quick checklist

- [ ] New table/view/function/type gets its own `.sql` file under the right layer + object-type folder
- [ ] Object-type folder (`tables`, `views`, ...) matches the apply-order table above — that's what governs ordering, not the layer's leading number
- [ ] One-off changes go in `migrations/`, dated, never edited after applying
- [ ] The live `.sql` file is updated in the same change as any migration touching that object
- [ ] `.prod` files are production-only and skipped by `pgdb testdb`
- [ ] Every table (and non-obvious column) has a `COMMENT ON`, placed in the object's own `.sql` file
