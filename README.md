# pgdevkit

A helper for developing with Postgres.

## `pgdb compare`

Compare a directory of SQL scripts (see the `database-in-source` layout
convention) against a live database and report differences:

```bash
pgdb compare --url postgresql://user:pass@host:port/db path/to/database/
```

## `pgdb testdb`

Manages a single shared, Podman-backed Postgres container for local tests
across all your projects — no more one-container-per-project-per-worktree.
Isolation between projects and worktrees is per-database, inside one
container.

Add to `pyproject.toml`:

```toml
[tool.pgdevkit]
name = "myproject"        # optional; defaults to the repo directory name
database_dir = "database" # optional; defaults to "database"
```

Add to `conftest.py`:

```python
import os
import pytest
from pgdevkit.testdb import ensure_testdb

@pytest.fixture(scope="session", autouse=True)
def ensure_test_postgres():
    for k, v in ensure_testdb().items():
        os.environ[k] = v
```

CLI: `pgdb testdb up|reset|run-sql|status|shell|clean`.

Container connection defaults (`localhost:54322`, `postgres`/`testpwd`) can
be overridden with `PGDEVKIT_TESTDB_HOST`, `PGDEVKIT_TESTDB_PORT`,
`PGDEVKIT_TESTDB_USER`, `PGDEVKIT_TESTDB_PASSWORD`. Before touching
podman/docker, pgdevkit first checks (with a short timeout) whether Postgres
is already reachable at that address and skips container management if so.
Set `PGDEVKIT_SKIP_CONTAINER=1` to always assume it's already there and skip
that check too.

To point at a local Postgres install instead of the podman container —
useful when podman isn't available, or you'd rather use peer authentication
as the current OS user — set `PGDEVKIT_TESTDB_HOST` to the unix socket
directory (e.g. `/var/run/postgresql`) and `PGDEVKIT_TESTDB_PASSWORD=""`.
The role named by `PGDEVKIT_TESTDB_USER` must exist and match your OS user
(`CREATE ROLE <user> SUPERUSER LOGIN;`) and `pg_hba.conf` must allow `peer`
auth for local connections (Debian/Ubuntu Postgres ships this by default).

## `pgdevkit.db` — helpers for application code

Install with the `db` extra: `pip install pgdevkit[db]`.

- **`PostgresTableModel`** — a `pydantic.BaseModel` base class for models
  that map 1:1 to a table row. Implement `get_table_name()` (returns
  `(schema, table)`) and `get_primary_key()` on each model.
- **`PgPool`** — an async connection pool keyed off
  `{env_prefix}HOST/PORT/DB/USER/PASSWORD` env vars. Call `await pool.open()`
  once at startup, then use `async with pool.connection() as con:`.
- **CRUD functions** — `pg_retrieve`, `pg_retrieve_many`, `pg_insert`,
  `pg_insert_many`, `pg_update`, `pg_update_dict`, `pg_upsert`,
  `pg_upsert_dict`, `pg_upsert_many`, `pg_upsert_many_dict`, `pg_delete`,
  `pg_delete_dict` — typed (`PostgresTableModel`-based) or dict-based CRUD
  against a table, built on `psycopg` for safe identifier/value handling.
- **`SqlLoader`** — loads and caches `.sql` files from
  `{root}/<topic>/<name>.sql`, for keeping hand-written queries out of
  Python source.

```python
from pgdevkit.db import PgPool, PostgresTableModel, pg_retrieve, pg_upsert

class Widget(PostgresTableModel):
    id: int
    name: str

    @staticmethod
    def get_table_name() -> tuple[str, str]:
        return ("public", "widget")

    @staticmethod
    def get_primary_key() -> list[str]:
        return ["id"]

pool = PgPool(env_prefix="POSTGRES_")
await pool.open()
async with pool.connection() as con:
    widget = await pg_retrieve(con, Widget, {"id": 1})
    await pg_upsert(con, Widget(id=1, name="thing"), Widget)
```
