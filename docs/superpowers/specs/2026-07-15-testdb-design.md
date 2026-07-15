# pgdevkit `testdb` — shared local Postgres for tests

Status: approved (sub-project A of the pgdevkit consolidation; B/C/D are separate future specs)

## Problem

Today, `ccmt2`, `OneSales`, and `MDMApp` (and others) each vendor a copy of a
`postgres-test-setup` skill's scripts (`start_postgres.py`, `run_sql.py`) into
a `test_server/` folder. Each project starts its own Docker container on its
own hardcoded port. This causes two concrete problems:

1. **Container proliferation.** Every project open at once means another
   running Postgres container.
2. **Cross-worktree corruption.** Because the container name and port are
   read from `pyproject.toml` — which is checked into git and therefore
   identical across all worktrees of the same repo — every worktree of a
   given project shares one container *and one database*. Two worktrees on
   different branches with different schemas stomp on each other's data
   when their test suites run.

Additionally, the current scripts use the `docker` Python package. Only
Podman is installed/used in practice; `docker` talks to it only incidentally
via a compatibility socket.

## Goal

A single, always-available, pgdevkit-managed Podman container that every
project's test suite talks to. Isolation between projects and between
worktrees of the same project is achieved via **separate databases inside
that one container**, not separate containers or ports.

This spec covers only the `pgdevkit testdb` capability (container lifecycle,
per-workspace database, schema apply, CLI). It does not cover migrating
`postgres-best-practices` or `database-in-source` content into pgdevkit
(future specs B/C/D), and it does not cover migrating OneSales/MDMApp/ccmt2
to use it (see "Migration" below).

## Architecture

- **One container**, name `pgdevkit-postgres`, image
  `pgvector/pgvector:pg18-trixie` (a superset — has the `vector` extension
  available for projects that want it, harmless for those that don't).
- **Fixed port 54322** (ccmt2's existing port, chosen as the shared
  standard), user `postgres`, password `testpwd` — matching what every
  project already uses today, so application code that reads these values
  doesn't need to change.
- **No persistent volume.** Test data must always be reproducible from
  `database/` + `.test_data.json` seed files, so there is nothing worth
  persisting across container restarts. Ephemeral storage lets the
  container run with `fsync=off`, `synchronous_commit=off`,
  `full_page_writes=off` for speed, which some projects already rely on.
- **Lazy, idempotent autostart.** No daemon to remember to start. Every
  `pgdb testdb` invocation (CLI or Python API) checks container state via
  `podman ps`/`podman inspect`, starts it if stopped, creates it if absent,
  then polls a real connection attempt (short retry loop, not a blind
  `sleep(20)`) until Postgres accepts connections.
- Managed via `subprocess` calls to the `podman` CLI directly — no `docker`
  Python package dependency.

## Workspace identity & config

Optional `[tool.pgdevkit]` table in a project's `pyproject.toml`:

```toml
[tool.pgdevkit]
name = "mdmapp"              # optional; defaults to the git repo root dir name
database_dir = "database"    # optional; defaults to "database"
env_prefix = "MDM_"          # optional; defaults to f"{name.upper()}_"
extensions = ["vector"]      # optional; defaults to []
```

`env_prefix` exists purely for backward compatibility — MDMApp's app code
already expects `MDM_POSTGRES_*` env vars during tests; OneSales uses the
legacy `CCMT_` prefix. Migrating a project's test infra to pgdevkit must not
require touching its application code.

The workspace database name is **computed at runtime**, never statically
declared, because it depends on the current git branch:

```
db_name = f"{slugify(config.name)}_{slugify(current_branch)}"
```

`current_branch` comes from `git rev-parse --abbrev-ref HEAD` in the current
worktree. `slugify` lowercases and replaces any character outside
`[a-z0-9_]` with `_`. If the result exceeds 30 characters, it's truncated to
30 and an 8-character hash of the full original string is appended
(`slug[:30] + "_" + sha256(slug)[:8]`), keeping the final `db_name` — two
such slugs joined with `_` — safely under Postgres's 63-byte identifier
limit.

## Python API (`pgdevkit.testdb`)

Used directly by project `conftest.py` fixtures:

```python
from pgdevkit.testdb import ensure_testdb

@pytest_asyncio.fixture(scope="session", autouse=True)
async def ensure_test_postgres():
    env = ensure_testdb()      # ensure container, ensure DB, apply schema
    for k, v in env.items():   # returns {PREFIX}POSTGRES_{HOST,PORT,DB,USER,PASSWORD}
        os.environ[k] = v
    yield
```

Key functions:

- `ensure_testdb(project_root=None, force_reset=False) -> dict[str, str]` —
  full orchestration: ensure container running, compute workspace DB name,
  create the DB if missing, apply `database/` schema + seed data, return the
  env-var dict.
- `reset_testdb(project_root=None) -> dict[str, str]` — drop + recreate
  **only the current workspace's database**, then reapply schema/seed.
- `clean_testdb(project_root=None, all=False) -> None` — drop the current
  workspace's database (no recreate). With `all=True`, drop every database
  whose name has this project's slug prefix (matched via `pg_database`),
  covering every worktree/branch of that project at once.
- `run_sql(sql_or_path, results=False, project_root=None)` — execute SQL
  against the current workspace's database.

Schema-apply logic (file-type ordering, cross-file FK dependency resolution
via `sqlglot`, `.test_data.json` seeding) is ported from the existing
`postgres-test-setup` skill's `start_postgres.py`/`run_sql.py`/
`complex_helper.py` essentially unchanged — it already implements the
`database-in-source` layout conventions correctly.

## CLI (`pgdb testdb ...`)

| Command | Purpose |
|---|---|
| `pgdb testdb up` | Ensure container running, DB exists, schema applied (idempotent) |
| `pgdb testdb reset` | Drop + recreate **only this workspace's** database, reapply schema + seed |
| `pgdb testdb run-sql` | Run a `.sql` file or `--sql` inline against this workspace's DB; `--results` for table output |
| `pgdb testdb status` | Show container state, this workspace's DB name, DSN |
| `pgdb testdb shell` | Drop into `psql` against this workspace's DB |
| `pgdb testdb clean [--all]` | Drop this workspace's database; `--all` drops every database belonging to this project across all worktrees/branches |

`reset`, `run-sql`, and `clean` (without `--all`) only ever target the
current workspace's own computed database name — there is no flag to point
them at another project's or another worktree's database. `clean --all`
is scoped by project-name prefix match, so it can never touch a different
project's databases.

## Safety & concurrency

- `CREATE DATABASE` races (e.g. two pytest sessions starting at once):
  catch `psycopg.errors.DuplicateDatabase` and treat as success. No locking.
- Container-create races: catch Podman's "name already in use" error and
  fall back to `podman start`.
- CI: `PGDEVKIT_SKIP_CONTAINER=1` skips Podman lifecycle management
  entirely. Workspace DB creation, schema apply, and seeding still run
  unchanged against whatever `HOST`/`PORT` a CI service container provides —
  same escape hatch as today's `SKIP_START_POSTGRES`.

## Testing strategy

- pgdevkit's own test suite (`tests/conftest.py`), which currently spins up
  its own ad hoc Docker container on port 54326, is migrated to dogfood
  `ensure_testdb()` — proving the new module works before anyone else
  depends on it.
- New unit tests: slugify/truncation, config resolution (defaults vs.
  `[tool.pgdevkit]` overrides), workspace DB name computation.
- New integration tests exercise the real Podman container: `up` is
  idempotent, `reset` only touches the current workspace, `clean --all`
  only touches this project's databases, concurrent `ensure_testdb()` calls
  don't error.

## Migration (out of scope for this implementation pass)

OneSales, MDMApp, and ccmt2 are **not** migrated as part of this work. A
migration checklist is written to a local, gitignored file
(`MIGRATION_NOTES.md` at the pgdevkit repo root, added to `.gitignore`) for
the user to follow later, per project:

1. Delete the vendored `test_server/start_postgres.py` and `run_sql.py`.
2. `uv add --dev pgdevkit`.
3. Add `[tool.pgdevkit]` to `pyproject.toml` (set `env_prefix` to the
   project's existing prefix so application code needs no changes).
4. Remove the old Postgres entries from `[tool.pytest_env]` (host/port/db
   are no longer static).
5. Rewrite `conftest.py`'s session fixture to call `ensure_testdb()`.
6. Replace any script that shelled out to `run_sql.py` with
   `pgdb testdb run-sql` or the `run_sql()` Python API.
