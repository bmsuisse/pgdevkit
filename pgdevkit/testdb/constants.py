from __future__ import annotations

import os

from psycopg.conninfo import make_conninfo

CONTAINER_NAME = "pgdevkit-postgres"
IMAGE = os.environ.get("PGDEVKIT_TESTDB_IMAGE", "pgvector/pgvector:pg18-trixie")
HOST = os.environ.get("PGDEVKIT_TESTDB_HOST", "localhost")
PORT = int(os.environ.get("PGDEVKIT_TESTDB_PORT", "54322"))
USER = os.environ.get("PGDEVKIT_TESTDB_USER", "postgres")
PASSWORD = os.environ.get("PGDEVKIT_TESTDB_PASSWORD", "testpwd")
# Docker-level resource overrides, passed through to `containers.run()` --
# unset (the default) means "let Docker/Podman use their own defaults".
# TMPFS uses docker-CLI --tmpfs syntax; see _docker._parse_tmpfs().
TMPFS = os.environ.get("PGDEVKIT_TESTDB_TMPFS", "")
SHM_SIZE = os.environ.get("PGDEVKIT_TESTDB_SHM_SIZE", "")
MEM_LIMIT = os.environ.get("PGDEVKIT_TESTDB_MEM_LIMIT", "")
CPUS = os.environ.get("PGDEVKIT_TESTDB_CPUS", "")
PG_SPEED_FLAGS = ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
# Production connects with session timezone=UTC; the container image's own default
# (baked into its base OS, not something pgdevkit ever set) can differ, silently
# making any ::date cast of a timestamptz column disagree between test and prod for
# rows near local midnight. Forcing UTC here -- both as the server's own timezone GUC
# and as PGTZ/TZ for any client library that consults the environment instead -- keeps
# test parity with prod instead of depending on the host/image's locale.
PG_STARTUP_FLAGS = [*PG_SPEED_FLAGS, "-c", "timezone=UTC"]


def conninfo(dbname: str, *, connect_timeout: int | None = None) -> str:
    """Build a libpq conninfo string from HOST/PORT/USER/PASSWORD.

    PASSWORD is omitted when empty (PGDEVKIT_TESTDB_PASSWORD="") so HOST can
    be pointed at a unix socket directory (e.g. /var/run/postgresql) and
    authenticate via peer auth as the current OS user instead of a password.
    """
    params: dict[str, str] = {"host": HOST, "port": str(PORT), "user": USER, "dbname": dbname}
    if PASSWORD:
        params["password"] = PASSWORD
    if connect_timeout is not None:
        params["connect_timeout"] = str(connect_timeout)
    return make_conninfo(**params)
