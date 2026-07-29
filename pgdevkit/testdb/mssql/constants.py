from __future__ import annotations

import os
import string

CONTAINER_NAME = "pgdevkit-mssql"
# amd64-only -- overridable for Apple Silicon dev machines, where this runs
# emulated under Docker Desktop (slow, occasionally flaky startup). The
# documented escape hatch is mcr.microsoft.com/azure-sql-edge (multi-arch,
# missing a few full-SQL-Server features).
IMAGE = os.environ.get("PGDEVKIT_TESTDB_MSSQL_IMAGE", "mcr.microsoft.com/mssql/server:2025-latest")
HOST = os.environ.get("PGDEVKIT_TESTDB_MSSQL_HOST", "localhost")
# Deliberately not SQL Server's native 1433 -- avoids colliding with a
# locally-installed instance, mirroring how the Postgres container's own
# default (54322) differs from Postgres's native 5432.
PORT = int(os.environ.get("PGDEVKIT_TESTDB_MSSQL_PORT", "14330"))
# The container only bootstraps the `sa` login -- additional logins are a
# known limitation of this first cut.
USER = os.environ.get("PGDEVKIT_TESTDB_MSSQL_USER", "sa")
# SQL Server's password-complexity rule rejects the Postgres container's
# plain default ("testpwd") outright, so this needs its own complexity-valid
# default -- see validate_sa_password().
PASSWORD = os.environ.get("PGDEVKIT_TESTDB_MSSQL_PASSWORD", "TestPwd!2026")
MEMORY_LIMIT_MB = int(os.environ.get("PGDEVKIT_TESTDB_MSSQL_MEMORY_LIMIT_MB", "2048"))


def validate_sa_password(password: str) -> None:
    """Check SQL Server's SA-password complexity rule before starting a
    container with it, so a weak custom password fails fast with a clear
    message instead of surfacing as an opaque container crash-loop.

    Rule (per Microsoft's documented policy): at least 8 characters, and at
    least 3 of {uppercase, lowercase, digit, symbol}; must not contain the
    login name "sa"."""
    if len(password) < 8:
        raise ValueError("MSSQL_SA_PASSWORD must be at least 8 characters long")
    if "sa" in password.lower():
        raise ValueError("MSSQL_SA_PASSWORD must not contain the login name 'sa'")
    classes_met = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(c in string.punctuation for c in password),
    ])
    if classes_met < 3:
        raise ValueError(
            "MSSQL_SA_PASSWORD must contain at least 3 of: uppercase letter, "
            "lowercase letter, digit, symbol"
        )


def conninfo(dbname: str) -> str:
    """Build an mssql-python connection string from HOST/PORT/USER/PASSWORD.
    `Driver`/`APP` are deliberately omitted -- mssql-python controls those
    itself (it bundles its own driver, so no system ODBC driver install is
    needed) and raises if a caller tries to set them."""
    return (
        f"Server={HOST},{PORT};Database={dbname};UID={USER};PWD={PASSWORD};"
        "Encrypt=yes;TrustServerCertificate=yes"
    )
