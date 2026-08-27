# pgdevkit

A helper for developing with Postgres.

## `pgdb compare`

Compare a directory of SQL scripts (see the `database-in-source` layout
convention) against a live database and report differences:

```bash
pgdb compare --url postgresql://user:pass@host:port/db path/to/database/
```

### Entra ID auth (Azure Postgres / Databricks Lakebase)

Pass `--entra-user <identity>` to `pgdb compare` to authenticate with an
Entra ID token instead of a static password. Which token flow is used is
auto-detected from the database hostname:

- **Azure Database for PostgreSQL** (`*.postgres.database.azure.com`,
  `*.postgres.cosmos.azure.com`) — the default: fetches a token via
  `DefaultAzureCredential` and uses it directly as the password. Requires
  the `azure` extra: `pip install pgdevkit[azure]`.
- **Databricks Lakebase** (`*.database.azuredatabricks.net`,
  `*.database.cloud.databricks.com`) — fetches a Databricks-scoped Entra
  token, then exchanges it for a short-lived Postgres credential via the
  Databricks workspace API. Also requires `--databricks-workspace-host`
  and `--databricks-instance`:

```bash
pgdb compare --url postgresql://instance-abc.database.azuredatabricks.net:5432/databricks_postgres \
  --entra-user alice@example.com \
  --databricks-workspace-host https://adb-123456789.azuredatabricks.net \
  --databricks-instance myinstance \
  path/to/database/
```

(`--url`'s own user/password, if any, are discarded and replaced — `--entra-user`
plus the fetched token become the connection's actual credentials.)

### MSSQL

`pgdb compare`/`pgdb fetch-missing` default to Postgres. Pass `--dialect mssql`
to compare against a SQL Server database instead:

```bash
pgdb compare --dialect mssql --url "Server=host,1433;Database=db;UID=user;PWD=pass" path/to/database/
```

Requires the `mssql` extra: `pip install pgdevkit[mssql]` (pulls in
[mssql-python](https://github.com/microsoft/mssql-python), which bundles its
own driver — no system ODBC driver install needed). MSSQL has no composite
type or native enum equivalent, so those areas of a `database/` tree don't
have a direct equivalent on this backend — see `docs/database-layout.md`.
Current Azure SQL/SQL Server (2025+) does have a native `json` column type,
which parses/introspects/diffs like any other column type; see
"`pgdevkit.db` — helpers for application code" below for how JSON values are
handled on the CRUD side (write-side serialization only, no auto-parsing on
read — `mssql-python` doesn't distinguish `json` columns from `nvarchar`).

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
`PGDEVKIT_TESTDB_USER`, `PGDEVKIT_TESTDB_PASSWORD`. Before touching the
Docker API, pgdevkit first checks (with a short timeout) whether Postgres
is already reachable at that address and skips container management if so.
Set `PGDEVKIT_SKIP_CONTAINER=1` to always assume it's already there and skip
that check too.

Container management goes through the Docker API (the `docker` package,
`docker.from_env()`, falling back to Podman's rootful/rootless socket) — it
works against a real Docker daemon or Podman transparently, no CLI binary
required either way.

To point at a local Postgres install instead of the container — useful when
neither is available, or you'd rather use peer authentication as the
current OS user — set `PGDEVKIT_TESTDB_HOST` to the unix socket
directory (e.g. `/var/run/postgresql`) and `PGDEVKIT_TESTDB_PASSWORD=""`.
The role named by `PGDEVKIT_TESTDB_USER` must exist and match your OS user
(`CREATE ROLE <user> SUPERUSER LOGIN;`) and `pg_hba.conf` must allow `peer`
auth for local connections (Debian/Ubuntu Postgres ships this by default).

### MSSQL

Add `engine = "mssql"` to `[tool.pgdevkit]` (or set
`PGDEVKIT_TESTDB_ENGINE=mssql` for a one-off run) to manage a shared SQL
Server container instead of Postgres — same one-container-per-machine,
one-database-per-workspace model. Requires the `mssql` extra (see above).

Container defaults (`localhost:14330`, `sa`/a generated complexity-valid
password) can be overridden with `PGDEVKIT_TESTDB_MSSQL_HOST`, `_PORT`,
`_USER`, `_PASSWORD`, `_IMAGE`, `_MEMORY_LIMIT_MB`. The container only
bootstraps the `sa` login — additional logins are a known limitation.
`pgdb testdb shell` execs into
[`sqlcmd`](https://github.com/microsoft/go-sqlcmd) (an external prerequisite,
the same category as `psql` for the Postgres path) rather than a Python
REPL.

## `pgdb migrate`

Applies numbered, forward-only SQL migration files from a directory to a live
Postgres database, tracking each one in a `schema.table` (default
`public.schema_migrations`) so repeat runs only apply what's pending. Postgres only —
not available for `--dialect mssql`.

```bash
pgdb migrate check  path/to/database/_migration_scripts --url postgresql://user:pass@host:port/db
pgdb migrate apply  path/to/database/_migration_scripts --url postgresql://user:pass@host:port/db
```

`--entra-user` works the same as `pgdb compare` (see above). The tracking
table needs `filename text primary key, applied_at timestamptz not null
default now(), applied_by text not null default current_user` (a migration
file that creates it, in the same directory, is the usual way to bootstrap
it — inserting into a not-yet-existing tracking table is tolerated so that
migration can still run).

The tracking table defaults to `public.schema_migrations`. Override it per-project in
`pyproject.toml`:

```toml
[tool.pgdevkit]
migrations_table = "myschema.migrations"
```

or per-invocation with `--tracking-table`, which takes precedence over the
pyproject.toml value.

`--ask` prints each pending file and asks yes/no/already-done/quit before
running it. Answering yes queues the file on a background worker and moves
straight to the next prompt — you can keep reviewing while earlier files are
still executing, instead of waiting on each one before seeing the next. A
`tqdm` progress bar tracks the queue; migrations still run one at a time, in
file order. Without `--ask`, `apply` queues every pending file up front and
shows the same progress bar. `--file <name>` applies a single file (still
through the same verify-and-track path) instead of walking all pending ones.
Pass `--yes` to skip the "about to run migrations against ..." confirmation
prompt (e.g. in CI).

After each file's DDL is applied, `apply` re-checks that every `CREATE TABLE`
statement's target actually exists (via `to_regclass`) before recording the
file as applied — catching a migration that silently rolled back. That check
parses each statement with `sqlglot` and only falls back to a regex (run
against comment-stripped SQL) for statements sqlglot's postgres dialect can't
parse, so a `CREATE TABLE` mentioned only in a `--` comment is never mistaken
for a real one.

`pgdevkit.migrate` is also usable directly as a library — `list_migration_files`,
`applied_migrations`, `pending_migrations`, and `apply_migration` are the same
functions the CLI calls, so a project can script around them without shelling
out.

## `pgdevkit.db` — helpers for application code

Install with the `db` extra: `pip install pgdevkit[db]`.

- **`TableModel`** (formerly `PostgresTableModel`, still importable under
  that name) — a `pydantic.BaseModel` base class for models that map 1:1 to
  a table row, for either engine. Implement `get_table_name()` (returns
  `(schema, table)`) and `get_primary_key()` on each model.
- **`PgPool`** — an async connection pool keyed off
  `{env_prefix}HOST/PORT/DB/USER/PASSWORD` env vars. Call `await pool.open()`
  once at startup, then use `async with pool.connection() as con:`.
  Pass `entra_user` to authenticate via Entra ID instead of a static
  password — same host-based auto-detection as `pgdb compare`'s
  `--entra-user`. For Lakebase hosts, also set the
  `{env_prefix}DATABRICKS_WORKSPACE_HOST` and `{env_prefix}DATABRICKS_INSTANCE`
  env vars.
- **CRUD functions** — `pg_retrieve`, `pg_retrieve_many`, `pg_insert`,
  `pg_insert_many`, `pg_update`, `pg_update_dict`, `pg_upsert`,
  `pg_upsert_dict`, `pg_upsert_many`, `pg_upsert_many_dict`, `pg_delete`,
  `pg_delete_dict` — typed (`TableModel`-based) or dict-based CRUD against a
  table, built on `psycopg` for safe identifier/value handling. The `mssql`
  extra provides an `mssql_*`-prefixed mirror of the same functions in
  `pgdevkit.db.mssql_crud`, built on `mssql-python` (`MERGE`-based upsert,
  `OUTPUT` instead of `RETURNING`) — MSSQL has no composite/enum equivalent,
  so `complex_helper` is always `None` on that path. It does have a native
  `json` column type on current versions (and the older
  `NVARCHAR(MAX)`-plus-`OPENJSON()` convention works on any version), but
  `mssql-python` has no auto-serialization for dict/list parameter values
  (binding one raises `TypeError`) and no way to distinguish a `json`
  column from `nvarchar` on fetch — so every `mssql_*` write function
  serializes dict/list values to JSON text automatically
  (`db.mssql_sql.json_encode_value`), while reads always come back as plain
  `str`; deserialize with `json.loads()` yourself if you need the parsed
  value back.
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
