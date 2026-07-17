from __future__ import annotations

import os

from psycopg.conninfo import make_conninfo

CONTAINER_NAME = "pgdevkit-postgres"
IMAGE = "pgvector/pgvector:pg18-trixie"
HOST = os.environ.get("PGDEVKIT_TESTDB_HOST", "localhost")
PORT = int(os.environ.get("PGDEVKIT_TESTDB_PORT", "54322"))
USER = os.environ.get("PGDEVKIT_TESTDB_USER", "postgres")
PASSWORD = os.environ.get("PGDEVKIT_TESTDB_PASSWORD", "testpwd")
PG_SPEED_FLAGS = ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]


def conninfo(dbname: str, *, connect_timeout: int | None = None) -> str:
    """Build a libpq conninfo string from HOST/PORT/USER/PASSWORD.

    PASSWORD is omitted when empty (PGDEVKIT_TESTDB_PASSWORD="") so HOST can
    be pointed at a unix socket directory (e.g. /var/run/postgresql) and
    authenticate via peer auth as the current OS user instead of a password.
    """
    params: dict[str, str | int] = {"host": HOST, "port": PORT, "user": USER, "dbname": dbname}
    if PASSWORD:
        params["password"] = PASSWORD
    if connect_timeout is not None:
        params["connect_timeout"] = connect_timeout
    return make_conninfo(**params)
