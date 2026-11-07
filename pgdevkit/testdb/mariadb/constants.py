from __future__ import annotations

import os
from urllib.parse import quote

CONTAINER_NAME = "pgdevkit-mariadb"
# Fully qualified so Podman's short-name resolution never prompts.
IMAGE = os.environ.get("PGDEVKIT_TESTDB_MARIADB_IMAGE", "docker.io/library/mariadb:11")
HOST = os.environ.get("PGDEVKIT_TESTDB_MARIADB_HOST", "localhost")
# Deliberately not MariaDB's native 3306 -- avoids colliding with a
# locally-installed instance, like the Postgres (54322) and MSSQL (14330) ports.
PORT = int(os.environ.get("PGDEVKIT_TESTDB_MARIADB_PORT", "13306"))
# The container only bootstraps the `root` login (MARIADB_ROOT_PASSWORD).
USER = os.environ.get("PGDEVKIT_TESTDB_MARIADB_USER", "root")
PASSWORD = os.environ.get("PGDEVKIT_TESTDB_MARIADB_PASSWORD", "testpwd")
# Docker-level resource overrides, passed through to `containers.run()` --
# unset (the default) means "let Docker/Podman use their own defaults". TMPFS
# uses docker-CLI --tmpfs syntax; see _docker._parse_tmpfs().
TMPFS = os.environ.get("PGDEVKIT_TESTDB_MARIADB_TMPFS", "")
SHM_SIZE = os.environ.get("PGDEVKIT_TESTDB_MARIADB_SHM_SIZE", "")
MEM_LIMIT = os.environ.get("PGDEVKIT_TESTDB_MARIADB_MEM_LIMIT", "")
# Defaults to MEM_LIMIT (no swap) when that is set; "-1" allows unlimited swap.
MEMSWAP_LIMIT = os.environ.get("PGDEVKIT_TESTDB_MARIADB_MEMSWAP_LIMIT", "")
CPUS = os.environ.get("PGDEVKIT_TESTDB_MARIADB_CPUS", "")
# User the container runs as (`--user`); unset keeps the image's own default.
CONTAINER_USER = os.environ.get("PGDEVKIT_TESTDB_MARIADB_CONTAINER_USER", "")


def connect_kwargs(dbname: str | None = None, *, connect_timeout: int = 10) -> dict:
    """Keyword arguments for `pymysql.connect()` from HOST/PORT/USER/PASSWORD;
    `dbname=None` connects without selecting a database (admin work)."""
    kwargs: dict = {
        "host": HOST,
        "port": PORT,
        "user": USER,
        "password": PASSWORD,
        "connect_timeout": connect_timeout,
        "autocommit": True,
    }
    if dbname is not None:
        kwargs["database"] = dbname
    return kwargs


def dsn(dbname: str) -> str:
    return f"mysql://{quote(USER, safe='')}:{quote(PASSWORD, safe='')}@{HOST}:{PORT}/{dbname}"
