# MariaDB test database (`pgdb testdb` / `pgdevkit.testdb`)

Add `engine = "mariadb"` to `[tool.pgdevkit]` (or set
`PGDEVKIT_TESTDB_ENGINE=mariadb` for a one-off run) to manage a shared MariaDB
container instead of Postgres — same one-container-per-machine,
one-database-per-workspace model. Requires the `mariadb` extra
(`pip install pgdevkit[mariadb]`, which pulls in the pure-Python
[PyMySQL](https://github.com/PyMySQL/PyMySQL) — no client library needed).
`pgdb testdb shell` needs the `mariadb` client binary on `PATH`.

Container defaults (`localhost:13306`, `root`/`testpwd`, image
`docker.io/library/mariadb:11`) can be overridden with
`PGDEVKIT_TESTDB_MARIADB_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_IMAGE`. The
container only bootstraps the `root` login. The Docker-level resource knobs of
the other engines exist too — `_SHM_SIZE`, `_MEM_LIMIT`, `_MEMSWAP_LIMIT`,
`_CPUS`, `_TMPFS` (e.g. `/var/lib/mysql:size=512m`) and `_CONTAINER_USER` — and,
as for them, only take effect when pgdevkit creates the container
(`docker rm -f pgdevkit-mariadb` first after changing one).
`ensure_testdb()` returns `{PREFIX}MARIADB_HOST/PORT/DB/USER/PASSWORD`.

## Writing the `database/` files

The folder layout is the one in [database-layout.md](database-layout.md), with
these MariaDB differences:

- **No schemas.** In MariaDB a schema *is* a database, so everything lives in the
  workspace's single database. Write object names **unqualified**
  (`CREATE TABLE widget`, not `app.widget`); layer folders only group files.
  `schemas`/`exclude_schemas` raise, and a `schema/` file would create a second,
  unisolated database — don't use one.
- **Apply order** is the object-type order, then a retry: a file that fails (e.g.
  a view applied before the view it selects from, which can't be detected without
  schema qualifiers) is retried until a full pass makes no progress, like for Postgres.
- **One statement per `execute()`**: scripts are split on `;`, honoring
  `DELIMITER` lines (needed for procedure bodies), quoted strings/identifiers and
  comments. Make files idempotent (`CREATE TABLE IF NOT EXISTS`,
  `CREATE OR REPLACE VIEW`, `DROP PROCEDURE IF EXISTS`).
- **`.test_data.json`** values that are objects/arrays are inserted as JSON text.

## Not supported (yet)

`apply=`/`apply_scripts=False` hook, `snapshot_testdb`/`restore_testdb` (MariaDB
has no database snapshots — use `reset_testdb`), and `compare`/`migrate`/
`update-stats`/the `*_crud` helpers, which stay Postgres/MSSQL-only.

**Rootless Podman.** If the image's entrypoint can't start (`exec container
process ... Permission denied`), try `PGDEVKIT_CONTAINER_ENGINE=podman` with
`PGDEVKIT_TESTDB_MARIADB_CONTAINER_USER=0`.
