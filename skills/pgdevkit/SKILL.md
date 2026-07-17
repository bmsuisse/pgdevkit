---
name: pgdevkit
plugin: coding
description: >
  Use pgdevkit for any PostgreSQL work in a Python project: a local
  Docker/Podman test database (`pgdb testdb`), importable ORM-free CRUD
  helpers (`pgdevkit.db`), and the `database/`-folder schema-as-code
  convention. Supersedes the old postgres-test-setup, postgres-best-practices,
  and database-in-source skills — pgdevkit is a real dependency now, not
  copy-pasted reference files. Use whenever the user wants to set up a local
  test Postgres, write or review psycopg code, add a table/view/function to
  a `database/` folder, or asks things like "add a database query", "create
  a repository", "set up a test database", "reset the database", "add a
  table", "migration script", "backfill missing objects", or mentions
  psycopg/psycopg2/asyncpg/SQLAlchemy where the user seems open to a
  different approach.
---

# pgdevkit — Postgres for Python projects

One dependency covers three things that used to be three separate skills
with copy-pasted reference files:

| Old skill | Replaced by |
|---|---|
| `postgres-test-setup` | `pgdb testdb` (container + schema apply) |
| `postgres-best-practices` | `pgdevkit.db` (importable CRUD helpers) |
| `database-in-source` | The `database/` folder convention — see [docs/database-layout.md](../../docs/database-layout.md) |

Core rules for every piece of database code in a project using pgdevkit:

- **No ORM** — use [psycopg](https://www.psycopg.org/psycopg3/) directly, via `pgdevkit.db`'s helpers or hand-written queries.
- **Inline SQL** — trivial queries of **4 lines or fewer** may be written inline in Python. Anything with JOINs, subqueries, CTEs, aggregations, or multiple conditions lives in its own `.sql` file.
- **Named parameters** — always `%(name)s` style, never positional `%s`.
- **The `database/` folder is the source of truth for the schema** — see [docs/database-layout.md](../../docs/database-layout.md) for the layer/object-type/file-naming conventions.
- **Result mapping** — every query result maps to a Pydantic model; table-mapped models extend `pgdevkit.db.PostgresTableModel`.

---

## Install

```bash
uv add pgdevkit[cli,db]
```

`cli` pulls in `typer`/`rich` for the `pgdb` command; `db` pulls in `pydantic`/`psycopg-pool` for the importable CRUD helpers. Skip either extra if the project doesn't need it (e.g. a project using only `pgdb testdb` doesn't need `db`).

---

## Local test database — `pgdb testdb`

Add to `pyproject.toml`:

```toml
[tool.pgdevkit]
database_dir = "database"       # optional, defaults to "database"
env_prefix = "MDM_"              # optional, defaults to "{name.upper()}_"
extensions = ["vector"]          # optional, CREATE EXTENSION IF NOT EXISTS
```

`name` is optional too — it falls back to the repo directory name.

In `tests/conftest.py`:

```python
import pytest
from pgdevkit.testdb import ensure_testdb

@pytest.fixture(scope="session", autouse=True)
def _testdb_env():
    env = ensure_testdb()
    for key, value in env.items():
        os.environ[key] = value
```

`ensure_testdb()` starts the shared `pgdevkit-postgres` container if needed (via `podman`), creates a database scoped to this project+branch (so different worktrees/branches never collide), and applies every `.sql` file under `database_dir` in dependency order, seeding any `.test_data.json` sidecar files.

**`migrations/` is never applied here, on purpose.** If the test schema is missing something, that's a sign the base `tables/`/`views`/... file has drifted behind a migration that was only ever run manually against a real database — fix the base file, don't add migration-replay to `apply_schema()` (tried once, reverted: a migration can't be judged "safe to re-run" from its SQL text alone — see `docs/database-layout.md`'s Migrations section).

| What do you need? | Command |
|---|---|
| First-time setup / apply new files | `pgdb testdb up` |
| Breaking change (rename/drop column) | `pgdb testdb reset` |
| Inspect test DB data | `pgdb testdb run-sql --sql "SELECT ..." --results` |
| Re-apply one file (e.g. a function/view) | `pgdb testdb run-sql database/path/to/file.sql` |
| Drop this workspace's database | `pgdb testdb clean` |
| Drop every database for this project (all branches) | `pgdb testdb clean --all` |

### CI

Podman needs to be available on the runner (`apt-get install -y podman` on `ubuntu-latest` if not preinstalled) — `ensure_testdb()`/`ensure_container()` shell out to it directly. There's no `PGDEVKIT_SKIP_CONTAINER` + service-container escape hatch wired through every fixture yet — if a project's CI can't run podman, it needs its own workaround for now.

---

## Application-side DB code — `pgdevkit.db`

```
app/
├── db/
│   ├── queries/
│   │   ├── users/
│   │   │   ├── get_user_by_id.sql
│   │   │   └── list_active_users.sql
│   └── repositories/
│       └── user_repository.py
├── models/
│   └── user_models.py         # Pydantic models for the user domain
```

SQL files live under `db/queries/<topic>/`. Every custom query gets its own file — no multi-statement files that lump unrelated queries together.

### Connection pool

```python
# db/connection.py
from pgdevkit.db import PgPool

pool = PgPool(env_prefix="APP_POSTGRES_")  # matches ensure_testdb()'s {env_prefix}POSTGRES_* vars

async def startup():
    await pool.open()
```

### Models

```python
# models/user_models.py
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from pgdevkit.db import PostgresTableModel

class UserRow(PostgresTableModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str
    created_at: datetime

    @staticmethod
    def get_table_name() -> tuple[str, str]:
        return ("public", "users")

    @staticmethod
    def get_primary_key() -> list[str]:
        return ["id"]

class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    display_name: str
```

Models that represent partial results (joins, aggregations, partial selects) extend `BaseModel` directly instead of `PostgresTableModel`.

### CRUD helpers

```python
from pgdevkit.db import pg_retrieve, pg_insert, pg_upsert, pg_delete
```

| Helper | Purpose |
|--------|---------|
| `pg_retrieve` | Fetch single row by PK |
| `pg_retrieve_many` | Fetch rows matching filter dict |
| `pg_insert` | Insert one row, `RETURNING *` |
| `pg_update` / `pg_update_dict` | Update by PK |
| `pg_upsert` / `pg_upsert_dict` | `INSERT ... ON CONFLICT ... DO UPDATE` |
| `pg_upsert_many` / `pg_upsert_many_dict` | Batch upsert via `executemany` |
| `pg_insert_many` | Batch insert via `executemany` |
| `pg_delete` / `pg_delete_dict` | Delete by PK, returns deleted row |

Use these for simple CRUD. For custom `WHERE` clauses, joins, aggregations, or ordering, write a dedicated `.sql` file and a repository method.

### Loading `.sql` files

```python
# db/loader.py
from pathlib import Path
from pgdevkit.db import SqlLoader

sql = SqlLoader(Path(__file__).parent / "queries")
```

```python
# db/repositories/user_repository.py
from psycopg.rows import dict_row
from pgdevkit.db import pg_retrieve, pg_delete
from db.connection import pool
from db.loader import sql
from models.user_models import UserRow, UserSummary

class UserRepository:
    async def get_by_id(self, user_id: int) -> UserRow | None:
        async with pool.connection() as conn:
            return await pg_retrieve(conn, UserRow, {"id": user_id})

    async def list_active(self, limit: int = 100) -> list[UserSummary]:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql.load_sql("users", "list_active_users"), {"limit": limit})
                rows = await cur.fetchall()
        return [UserSummary.model_validate(r) for r in rows]

    async def delete(self, user: UserRow) -> UserRow | None:
        async with pool.connection() as conn:
            return await pg_delete(conn, user, UserRow)
```

Named parameters, `%(name)s` style, dict argument — never positional `%s`, never f-strings or `str.format()` for SQL text.

### Dynamic SQL

Avoid it whenever possible — a static `.sql` file is always clearer. See [`references/dynamic-sql.md`](references/dynamic-sql.md) for t-string templates (3.14+) and `psycopg.sql` (< 3.14) when column/table names genuinely vary at runtime.

### Avoid `LATERAL JOIN` — use a CTE instead

```sql
with latest_order as (
    select
        o.user_id,
        o.total,
        row_number() over (partition by o.user_id order by o.created_at desc) as rn
    from orders as o
)
select u.id, u.email, lo.total
from users as u
join latest_order as lo on lo.user_id = u.id and lo.rn = 1
```

### Temporal tables

See [`references/temporal-tables.md`](references/temporal-tables.md) for row-level history via `nearform/temporal_tables`.

### Custom Postgres types (composites, enums)

`pgdevkit.db.complex_types.ComplexHelper` detects a table's composite/enum/JSONB
columns and converts plain dict/list values into the psycopg-registered types
those columns need — used automatically by both `pgdevkit.testdb.schema`'s
test-data seeding and `pgdevkit.db.crud`'s CRUD helpers. Pass a `normalizers`
dict (keyed by composite type name) if a project needs to reshape a value
before conversion (e.g. backfilling missing locale keys).

### SQL formatting

```bash
uv add --dev shandy-sqlfmt[jinjafmt]
sqlfmt db/queries/          # format
sqlfmt --check db/queries/  # CI check
```

---

## The `database/` folder & backfilling untracked objects

See [docs/database-layout.md](../../docs/database-layout.md) for the full convention: layer directories, object-type subfolders and their apply order, file-naming rules (`.test_data.json`, `.init.sql`, `.prod`), and how migrations are organised.

If a table, view, or function was created directly on the database and never got a `.sql` file:

```bash
pgdb fetch-missing database/ --url postgresql://... # dry run, lists what's missing
pgdb fetch-missing database/ --url postgresql://... --write
```

It diffs the live schema against `database/`, reverse-engineers DDL for anything untracked, and writes it into the matching layer folder's `tables/`, `views/`, `scalar_functions/`, or `table_functions/` subfolder (matched by schema name against existing top-level directories, ignoring their leading sort number).

---

## Comparing scripts to a live database

```bash
pgdb compare --url postgresql://... database/
```

Reports drift between the `database/` `.sql` files and the actual schema — tables, views, functions, enums, composite types, and indexes. Pass `--report-extra-db` to also flag objects that exist in the database but aren't tracked (this is what `pgdb fetch-missing` uses internally).

---

## Quick checklist

- [ ] `[tool.pgdevkit]` configured in `pyproject.toml`; `tests/conftest.py` calls `ensure_testdb()`
- [ ] New table/view/function/type gets its own `.sql` file under the right layer + object-type folder (see [docs/database-layout.md](../../docs/database-layout.md))
- [ ] Simple CRUD uses `pgdevkit.db`'s `pg_*` helpers; custom queries use `.sql` files loaded via `SqlLoader`
- [ ] Inline SQL only for trivial queries ≤ 4 lines; anything with JOINs/CTEs/aggregations/subqueries uses a `.sql` file
- [ ] All parameters use `%(name)s` style with a dict argument
- [ ] Results mapped to a Pydantic model; table-mapped models extend `PostgresTableModel`
- [ ] No `LATERAL JOIN` — use a CTE that groups/aggregates first, then joins it
- [ ] `.prod` files are production-only and skipped by `pgdb testdb`
- [ ] Every table (and non-obvious column) has a `COMMENT ON`, placed in the object's own `.sql` file
- [ ] Untracked DB objects backfilled via `pgdb fetch-missing`, not left undocumented
