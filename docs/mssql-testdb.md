# MSSQL test database (`pgdb testdb` / `pgdevkit.testdb`)

Add `engine = "mssql"` to `[tool.pgdevkit]` (or set
`PGDEVKIT_TESTDB_ENGINE=mssql` for a one-off run) to manage a shared SQL
Server container instead of Postgres — same one-container-per-machine,
one-database-per-workspace model. Requires the `mssql` extra (`pip install pgdevkit[mssql]`).

Container defaults (`localhost:14330`, `sa`/a generated complexity-valid
password) can be overridden with `PGDEVKIT_TESTDB_MSSQL_HOST`, `_PORT`,
`_USER`, `_PASSWORD`, `_IMAGE`, `_MEMORY_LIMIT_MB`. The container only
bootstraps the `sa` login — additional logins are a known limitation.

The same Docker-level resource knobs as the Postgres container are
available here too: `PGDEVKIT_TESTDB_MSSQL_SHM_SIZE`,
`PGDEVKIT_TESTDB_MSSQL_MEM_LIMIT` (a container-level cgroup limit, distinct
from `_MEMORY_LIMIT_MB` above which only tunes SQL Server's own internal
memory management; like the Postgres one it also caps memory+swap, override via
`PGDEVKIT_TESTDB_MSSQL_MEMSWAP_LIMIT`), `PGDEVKIT_TESTDB_MSSQL_CPUS`, and
`PGDEVKIT_TESTDB_MSSQL_TMPFS`. As with the Postgres container, these only
take effect when pgdevkit creates the container, not when restarting an
existing stopped one (`docker rm -f pgdevkit-mssql` first if you've changed
any of them).

`PGDEVKIT_TESTDB_MSSQL_CONTAINER_USER` (unset by default, i.e. the image's own
user) is passed as the container's `--user`.

**Rootless Podman recipe.** The SQL Server image's default user can't start
there (`exec container process /opt/mssql/bin/launch_sqlservr.sh: Permission
denied`); running the container as root *inside* the user namespace fixes it
and doesn't give the container any real host privileges:

```bash
export PGDEVKIT_CONTAINER_ENGINE=podman          # no podman.sock needed
export PGDEVKIT_TESTDB_MSSQL_CONTAINER_USER=0
```

**Generated schemas.** When the DDL is produced by code (e.g. from a metadata
export) instead of `database/` files, pass a hook to `ensure_testdb` /
`reset_testdb`. It receives an autocommit `mssql_python` connection to this
workspace's database, after the `database/` files are applied — or instead of
them with `apply_scripts=False`:

```python
def create_tables(conn) -> None:
    cur = conn.cursor()
    for ddl in generate_ddl():  # your generator
        cur.execute(ddl)

ensure_testdb(apply=create_tables, apply_scripts=False)
```

Like the files, the hook runs on every call, so make it idempotent (or check
whether the objects exist first). Not available for Postgres.

**Fast reset with database snapshots.** Build the schema (and seed data) once,
then capture it and revert to it per test instead of rebuilding:

```python
from pgdevkit.testdb import ensure_testdb, restore_testdb, snapshot_testdb

ensure_testdb(apply=create_tables, apply_scripts=False, force_reset=True)
snapshot_testdb()          # once per session; name="template" by default

restore_testdb()           # per test: kicks other sessions off, reverts, keeps the snapshot
```

This uses SQL Server database snapshots (Developer/Enterprise edition — what
the managed container runs). A revert is only possible from a database's *sole*
snapshot, so `restore_testdb` raises if another snapshot of the database
exists, instead of dropping it. Snapshot files (sparse) are written next to the
database's data files, so they follow it onto a `PGDEVKIT_TESTDB_MSSQL_TMPFS`
mount; `PGDEVKIT_TESTDB_MSSQL_SNAPSHOT_DIR` (a path inside the container)
overrides that. Snapshots are dropped together with their database
(`force_reset`, `clean_testdb`) and never reported as orphans.
