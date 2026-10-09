---
name: pgdevkit
plugin: coding
description: >
  Use pgdevkit for any PostgreSQL work in a Python project: a local
  Docker/Podman test database (`pgdb testdb`), importable ORM-free CRUD and
  query helpers (`pgdevkit.db`: `pg_*`, `fetch_all`/`fetch_one`/`fetch_scalar`/`execute`, `readonly_transaction`, `PgPool`, `SqlLoader`), streaming a query as JSON from a
  FastAPI endpoint with query cancellation on client disconnect (`pgdevkit.fastapi.PostgresJsonResponse`: large grid
  endpoints), and the `database/`-folder schema-as-code convention. Supersedes the old postgres-test-setup, postgres-best-practices,
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
- **Result mapping** — every query result maps to a Pydantic model (or is deliberately returned as dicts / through `row_mapper`); table-mapped models extend `pgdevkit.db.PostgresTableModel`.

---

## Install

```bash
uv add pgdevkit[cli,db]
```

`cli` pulls in `typer`/`rich` for the `pgdb` command; `db` pulls in `pydantic`/`psycopg-pool` for the importable CRUD helpers; `fastapi` adds `pgdevkit.fastapi` (streaming JSON responses; FastAPI apps use `pgdevkit[cli,db,fastapi]`). Skip any extra the project doesn't need (e.g. a project using only `pgdb testdb` doesn't need `db`).

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

`ensure_testdb()` starts the shared `pgdevkit-postgres` container if needed (via the Docker API — works against a real Docker daemon or Podman's socket, no CLI binary required), creates a database scoped to this project+branch (so different worktrees/branches never collide), and applies every `.sql` file under `database_dir` in dependency order, seeding any `.test_data.json` sidecar files.

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

`ensure_testdb()`/`ensure_container()` talk to whatever Docker-compatible API is reachable (`DOCKER_HOST`, the default Docker socket, or Podman's socket as a fallback) — `ubuntu-latest`'s preinstalled Docker daemon just works, no setup step needed. There's no `PGDEVKIT_SKIP_CONTAINER` + service-container escape hatch wired through every fixture yet — if a project's CI has neither Docker nor Podman reachable, it needs its own workaround for now.

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
from pgdevkit.db import PgPool, set_default_pool

pool = PgPool(env_prefix="APP_POSTGRES_")  # matches ensure_testdb()'s {env_prefix}POSTGRES_* vars

async def startup():
    await pool.open()
    set_default_pool(pool)  # lets `fetch_all()` & co. borrow from it when no `con=`/`pool=` is given
```

`set_default_pool()` takes anything with a `.connection()` method (`PgPool`), or a plain callable returning an async
connection context manager (typed `PoolLike`; e.g. `set_default_pool(lambda: get_pool().connection())`; only the
default pool accepts a callable, the `pool=` keyword wants a `ConnectionSource`). `await pool.open()` is idempotent but
still has to run once at startup; there is no lazy auto-open. `PgPool` also takes `min_size=`, `timeout=` (acquire
timeout), `configure=` (async hook per new connection) and `check=`; its password and `dsn_params` are quoted for you.

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
| `pg_retrieve_many` | Fetch rows matching a filter dict; optional `where=` (t-string / `psycopg.sql` + `params=`), `order_by=`, `limit=` |
| `pg_insert` | Insert one row, `RETURNING *` |
| `pg_update` / `pg_update_dict` | Update by PK |
| `pg_upsert` / `pg_upsert_dict` | `INSERT ... ON CONFLICT ... DO UPDATE` |
| `pg_upsert_many` / `pg_upsert_many_dict` | Batch upsert via `executemany` |
| `pg_insert_many` | Batch insert via `executemany` |
| `pg_delete` / `pg_delete_dict` | Delete by PK, returns deleted row |

Use these for simple CRUD. `pg_retrieve_many(con, Model, {"status": "open"}, where=t"price > {minimum}", order_by=[("created", "desc")], limit=50)` covers a bit more than equality; for joins, aggregations or anything bigger write a dedicated `.sql` file and run it with `fetch_all` (below) in a repository method.

### Loading `.sql` files

```python
# db/loader.py
from pathlib import Path
from pgdevkit.db import SqlLoader

sql = SqlLoader(Path(__file__).parent / "queries")
```

```python
# db/repositories/user_repository.py
from pgdevkit.db import fetch_all, pg_retrieve, pg_delete
from db.connection import pool
from db.loader import sql
from models.user_models import UserRow, UserSummary

class UserRepository:
    async def get_by_id(self, user_id: int) -> UserRow | None:
        async with pool.connection() as conn:
            return await pg_retrieve(conn, UserRow, {"id": user_id})

    async def list_active(self, limit: int = 100) -> list[UserSummary]:
        return await fetch_all(sql.load_sql("users", "list_active_users"), {"limit": limit}, model=UserSummary)

    async def delete(self, user: UserRow) -> UserRow | None:
        async with pool.connection() as conn:
            return await pg_delete(conn, user, UserRow)
```

Named parameters, `%(name)s` style, dict argument — never positional `%s`, never f-strings or `str.format()` for SQL text.

### `fetch_all`, `fetch_one`, `fetch_scalar`, `execute` — custom queries without cursor boilerplate

```python
from pgdevkit.db import execute, fetch_all, fetch_one, fetch_scalar, set_default_pool

set_default_pool(pool)  # once, at startup -- the connection source when none is passed

users = await fetch_all(sql.load_sql("users", "list_active_users"), {"limit": 10}, model=UserSummary)  # list[UserSummary]
rows = await fetch_all(sql.load_sql("users", "list_active_users"), {"limit": 10})                      # list[dict]
async with pool.connection() as conn:  # inside a transaction you already hold
    users = await fetch_all(query, params, model=UserSummary, con=conn)

user = await fetch_one(sql.load_sql("users", "get_user_by_id"), {"id": 1}, model=UserSummary)  # UserSummary | None
email = await fetch_scalar("SELECT email FROM users WHERE id = %(id)s", {"id": 1})           # str | None
deleted = await execute("DELETE FROM sessions WHERE user_id = %(id)s", {"id": 1})              # row count
```

- Siblings with the same keywords (`con=`, `pool=`, `cancel=`, `statement_timeout=`, t-string queries): `fetch_one(...)` returns the first row or `None` (`model=`/`row_mapper=` work as in `fetch_all`; add `LIMIT 1` yourself), `fetch_scalar(...)` the first column of the first row or `None` (e.g. `await fetch_scalar("SELECT count(*) FROM users")`), `execute(...)` the row count of a write without `RETURNING` (`await execute("DELETE FROM sessions WHERE expires < now()")`; on a pooled connection it commits, on your `con=` it does not; with `PgPool`, a custom connection source decides for itself).
- `model=` validates each row into a Pydantic model; `row_mapper=` maps each dict row to anything else (exclusive with `model`).
- Passing `con=` runs on *your* connection: `fetch_all` never commits, closes or releases it. Without it, a connection is borrowed from `pool=` (or the `set_default_pool()` pool) and released afterwards.
- `statement_timeout=<seconds>` (optional) lets Postgres abort the query after that long, raising `QueryCanceled`; it is applied with `SET LOCAL` semantics for this call only (a borrowed `con` gets its previous value back). A plain value *replaces* the connection's own setting in both directions, so a per-call 900 s overrides a database-level safety net of 600 s; `statement_timeout=at_most(120)` (`from pgdevkit.db import at_most`) never loosens it: the effective timeout is `min(current, 120)`.
- `cancel=` takes an `asyncio.Event`: set it (e.g. when the HTTP client disconnects) and the running query is cancelled on the server, raising `psycopg.errors.QueryCanceled`. If the awaiting task is cancelled instead, the server-side query is cancelled too and `CancelledError` propagates as usual (not `QueryCanceled`), so no query keeps running unattended. Don't use `cancel=` on a `con` that concurrent tasks share (it aborts whatever statement is running on it); if the cancel request itself can't be delivered, the connection is closed. After `QueryCanceled` a borrowed `con` is usable again once you `await con.rollback()` (autocommit connections need nothing). To stream a whole result as JSON from FastAPI (it wires the disconnect itself) see `PostgresJsonResponse` below.
- `query` is typed `SqlQuery`: a literal string (`SqlLoader.load_sql()`), a sqlglot expression (any `exp.Expr`, so Select, Union, Subquery and other `exp.Query` values are fine), a `psycopg.sql` composable or a t-string. A plain `str` fails type checking on purpose, so don't build SQL with f-strings/concatenation. A sqlglot expression is only as safe as the strings it was built from (its builders parse plain strings as SQL): pass user values as `exp.Placeholder` + `params`, never as literals/raw text. A literal `%` in a sqlglot expression (`LIKE 'a%'`, modulo) just works, with or without `params`; in a plain string or `psycopg.sql` composable it is psycopg's rule: write `%%` when you pass `params`. A t-string carries its own values, so don't also pass `params`.

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
columns and converts plain dict/list values into the Python objects psycopg sends
for those columns. `pgdevkit.testdb.schema`'s test-data seeding uses it automatically; the `pg_*` CRUD helpers only do
when you pass `complex_helper="auto"` (or `complex_helper=ComplexHelper(con, normalizers={...})` to reshape a value
before conversion, keyed by composite type name, e.g. backfilling missing locale keys); without it values are not
converted and reads are a plain `SELECT *`.

**The helper never registers types on the connection** (psycopg's registration is connection-global and would stick
to a pooled connection: a plain `SELECT` of an enum would return psycopg's `Enum` member instead of the label, #54).
Writes register the enum/composite adapters on the one cursor that runs the statement, only for the columns where a
dict/list value needed converting; reads (`pg_retrieve*`) use `to_jsonb(...)` and register nothing. So after any
`pg_*` call the connection reads enums as label strings and composites as text (`'(1,2)'`), as before the call. `RETURNING` rows of
`pg_insert`/`pg_update_dict`/`pg_upsert_dict` (hence `pg_update`/`pg_upsert`) also use `to_jsonb(...)` for such
columns: with a helper they are plain Python values (a composite as a dict, an enum as its label) whichever columns
were written; without one it is a plain `RETURNING *`.
Using `recursive_convert` yourself: take `info` from `load_all_complex_types`/`load_complex_type`, call
`await helper.register_on(cur, info)` on your cursor, and execute the converted value on that same `cur` (the
connection as context would be a global registration again).

What the helper learns from the catalog (types and column types) is cached **per connection**, so
creating a `ComplexHelper(con)` per call (`pg_retrieve(con, M, pks, complex_helper=ComplexHelper(con))`) queries the
catalog only on that connection's first use, not every time. The cache cannot see later schema changes
(`CREATE TYPE`, `ALTER TYPE ... ADD VALUE`, new columns), nor DDL that was rolled back after it had been cached (test
fixtures that create types inside a rolled-back transaction on a reused connection): call `helper.clear_cache()` after
such DDL. `ComplexHelper(con, cache=False)` gives the helper a private cache (the pre-cache behaviour).

### Read-only transactions — `readonly_transaction`

```python
from pgdevkit.db import fetch_all, readonly_transaction

async with pool.connection() as con, readonly_transaction(con):
    rows = await fetch_all(sql_text, con=con)  # a write raises psycopg.errors.ReadOnlySqlTransaction
```

It sets `read_only` before the transaction (committed on success, rolled back on error) and **always** puts it back in
a `finally`, so a pooled connection is never handed on read-only. `con` must not be inside a transaction already
(psycopg refuses to change `read_only` then). **A safety net, not a sandbox:** SQL without bound parameters may contain
several statements, and `COMMIT; INSERT ...` leaves the read-only transaction; TEMP tables stay writable. For untrusted
(e.g. LLM-written) SQL also use a role with only `SELECT` privileges.

### Streaming JSON from FastAPI — `pgdevkit.fastapi`

For large/grid-style read endpoints, return a `PostgresJsonResponse` instead of `fetch_all()` + a list of
dicts: Postgres builds the JSON, rows are streamed, and the query is **cancelled in Postgres when the client
disconnects**. Needs the `fastapi` extra (FastAPI >= 0.118). The pool registered with `set_default_pool()` (see
*Connection pool*) is used, as in `fetch_all`.

```python
from pgdevkit.fastapi import PostgresJsonResponse

@router.get("/articles", responses={200: {"model": list[ArticleOut]}})
async def articles(lng: str) -> PostgresJsonResponse:
    return PostgresJsonResponse(sql.load_sql("articles", "list_articles"), {"lng": lng})
```

**Cancel `fetch_all`-based read endpoints when the client leaves:** Granian does not cancel handlers on disconnect, so a frontend abort only stops `PostgresJsonResponse`. Put other read endpoints (including POSTs that only read, e.g. grid/search) on `APIRouter(route_class=CancelOnDisconnectRoute)` (`from pgdevkit.fastapi import CancelOnDisconnectRoute`): the handler task is cancelled on `http.disconnect` and the statement is cancelled in Postgres. Never for writes. Await shared single-flight work through `asyncio.shield`, or one aborted request cancels it for all waiters.

- Query: from a `.sql` file via `SqlLoader` like any non-trivial query (or `psycopg.sql`, sqlglot or a t-string for dynamic SQL), values via `params` (`%(name)s`; none with a t-string, it carries its own). That it is a literal is enforced by the type checker only: never build it from user input. Data-modifying CTE or custom JSON: `query_produces_json=True` (one text column per row).
- `responses={200: {"model": ...}}` keeps the OpenAPI schema typed for the generated frontend client; `response_model` is ignored and rows are **not validated**.
- Connections as in `fetch_all`: default pool, `pool=`, or `con=`. Prefer the pool: a `con` you opened must outlive the response (`Depends` with `yield`, never `async with ... as conn: return PostgresJsonResponse(q, con=conn)`), and a disconnect aborts its transaction.
- Errors show as `{"error": "Internal Server Error"}` (500), not FastAPI's `{"detail": ...}`: set `error_key = "detail"` in a subclass if the frontend expects that. `expose_errors = IS_DEV` in a subclass shows the text (leaks SQL/values: never in prod). An error after the first byte leaves the array unterminated, so clients fail to parse it.
- A pooled connection is held until the client has read the whole body, so slow readers (and many parallel streams) can exhaust the pool (`PgPool` defaults to 40): size it for that (`PgPool(max_size=...)`) and enforce a proxy write timeout.
- `timestamptz` columns come out as `2026-01-01T10:00:00+00:00` (Postgres' `row_to_json`, with the session time zone's offset), where FastAPI/Pydantic emit `...Z` for UTC: equivalent for any ISO-8601 parser, but a frontend comparing strings sees a difference. `NaN`/`Infinity` come out as the strings `"NaN"`/`"Infinity"`.
- `statement_timeout=<seconds>` (optional, as in `fetch_all`): Postgres aborts a slow query; before the first byte the client gets a 504. It does not free a client that stopped reading (use a proxy write timeout), and needs a non-autocommit connection.
- It does no authorization; scope the query yourself. Full reference: the `pgdevkit.fastapi` section of the README.

### SQL formatting

```bash
uv add --dev shandy-sqlfmt[jinjafmt]
sqlfmt db/queries/          # format
sqlfmt --check db/queries/  # CI check
```

---

## The `database/` folder & backfilling untracked objects

See [docs/database-layout.md](../../docs/database-layout.md) for the full convention: layer directories, object-type subfolders and their apply order, file-naming rules (`.test_data.json`, `.init.sql`, `.<env>.sql`), and how migrations are organised.

If a table, view, or function was created directly on the database and never got a `.sql` file:

```bash
pgdb fetch-missing database/ --url postgresql://... # dry run, lists what's missing
pgdb fetch-missing database/ --url postgresql://... --write
```

It diffs the live schema against `database/`, reverse-engineers DDL for anything untracked, and writes it into the matching layer folder's `tables/`, `views/`, `scalar_functions/`, or `table_functions/` subfolder (matched by schema name against existing top-level directories, ignoring their leading sort number).

---

## Table & column stats — `pgdb update-stats` / `pgdb get-stats`

Stats live next to the schema files, in `database/_stats/`, so they can be committed and read without a DB connection:

```
database/_stats/
├── _tables.json             # one entry per table, keyed by "schema.table", keys sorted
└── public.users.json        # column stats for one table, keyed by column name
```

```json
// _tables.json
{ "public.users": { "row_count": 1200, "row_count_exact": false,
                    "table_bytes": 98304, "index_bytes": 32768, "total_bytes": 131072 } }
// public.users.json
{ "email": { "data_type": "text", "null_fraction": 0.0, "n_distinct": -1, "avg_width": 24 } }
```

`row_count` is the planner estimate (`null` if the table was never analyzed) unless `--exact` was used. Column stats come from `pg_stats` (`n_distinct` < 0 means a fraction of the row count, as in Postgres) and are `null` until the table is analyzed — pass `--analyze`.

```bash
pgdb update-stats database/ --url postgresql://... --analyze            # all tables
pgdb update-stats database/ --url postgresql://... --table public.users --exact   # partial update, keeps other entries
pgdb get-stats database/ public.users public.orders                     # JSON to stdout, no DB needed
pgdb get-stats database/ --no-columns                                   # all tables, table-level stats only
```

MSSQL: add `--dialect mssql` (needs the `mssql` extra). Row counts and sizes come from `sys.dm_db_partition_stats`; SQL Server has no `pg_stats`, so column `null_fraction`/`n_distinct`/`avg_width` are `null` unless `--exact` is given, which scans each table to compute them. `--analyze` runs `UPDATE STATISTICS`.

To read the stats from code or a script, just `json.load` `database/_stats/_tables.json` (and `database/_stats/<schema.table>.json` for columns).

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
- [ ] Custom read queries use `fetch_all(...)`/`fetch_one(...)`/`fetch_scalar(...)` and writes without `RETURNING` use `execute(...)` (`model=` where the shape is stable; pass `con=` inside a transaction); results mapped to a Pydantic model; table-mapped models extend `PostgresTableModel`
- [ ] Untrusted SQL runs under a SELECT-only role (plus `readonly_transaction(con)` and an `at_most(...)` timeout as safety nets, not as a sandbox)
- [ ] `set_default_pool()` at startup; `helper.clear_cache()` after DDL on a long-lived connection when using `complex_helper=`
- [ ] Large/grid reads from FastAPI may use `pgdevkit.fastapi.PostgresJsonResponse` (streams, cancels on disconnect, no model validation)
- [ ] No `LATERAL JOIN` — use a CTE that groups/aggregates first, then joins it
- [ ] `.<env>.sql` files (e.g. `.prod.sql`) are skipped by `pgdb testdb` unless it's run with a matching `--env`
- [ ] Every table (and non-obvious column) has a `COMMENT ON`, placed in the object's own `.sql` file
- [ ] Untracked DB objects backfilled via `pgdb fetch-missing`, not left undocumented
