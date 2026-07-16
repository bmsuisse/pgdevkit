from __future__ import annotations

import os

CONTAINER_NAME = "pgdevkit-postgres"
IMAGE = "pgvector/pgvector:pg18-trixie"
HOST = os.environ.get("PGDEVKIT_TESTDB_HOST", "localhost")
PORT = int(os.environ.get("PGDEVKIT_TESTDB_PORT", "54322"))
USER = os.environ.get("PGDEVKIT_TESTDB_USER", "postgres")
PASSWORD = os.environ.get("PGDEVKIT_TESTDB_PASSWORD", "testpwd")
PG_SPEED_FLAGS = ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
