from __future__ import annotations

from typing import Callable
from pathlib import Path

import pytest

from pgdevkit.backends import get_backend
from pgdevkit.diff import compute_diff
from pgdevkit.parser import parse_directory
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
from tests.testdb.conftest import requires_mssql

# See tests/testdb/test_api_mssql_live.py's module docstring for why this is
# marked `mssql` (selected only by the dedicated CI job) rather than run
# everywhere. This test in particular is the main way the `sys.*` catalog
# queries in pgdevkit/mssql_introspect.py get validated against a real SQL
# Server at all -- there's no way to check their syntactic correctness
# offline.
pytestmark = pytest.mark.mssql


@requires_mssql
def test_introspection_matches_applied_scripts_with_no_diff(project_factory: Callable[..., Path]):
    project = project_factory("mssqlcompare", "main", engine="mssql")
    try:
        ensure_testdb(project)
        conninfo = status(project)["dsn"]

        scripts = parse_directory(project / "database", dialect="mssql")
        db = get_backend("mssql").introspect(conninfo)
        diffs = compute_diff(scripts, db, dialect="mssql")

        assert diffs == [], diffs
    finally:
        clean_testdb(project)
