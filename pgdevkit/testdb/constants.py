from __future__ import annotations

CONTAINER_NAME = "pgdevkit-postgres"
IMAGE = "pgvector/pgvector:pg18-trixie"
HOST = "localhost"
PORT = 54322
USER = "postgres"
PASSWORD = "testpwd"
PG_SPEED_FLAGS = ["-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]
