from __future__ import annotations

from pathlib import Path

import pytest

from pgdevkit import migrate
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
from tests.testdb.conftest import _make_project, requires_mssql

# Selected only by the dedicated `mssql-test` CI job (see test_compare_mssql_live.py).
pytestmark = pytest.mark.mssql

_TRACKING = """\
CREATE TABLE dbo.schema_migrations (
    filename nvarchar(450) NOT NULL PRIMARY KEY,
    applied_at datetimeoffset NOT NULL DEFAULT sysdatetimeoffset(),
    applied_by nvarchar(128) NOT NULL DEFAULT suser_sname()
)
"""


@requires_mssql
def test_migrations_apply_and_track_against_live_mssql(tmp_path: Path):
    project = _make_project(tmp_path, "mssqlmigrate", "main", engine="mssql")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_tracking.sql").write_text(_TRACKING, encoding="utf-8")
    (migrations / "002_widgets.sql").write_text(
        "CREATE SCHEMA mig_app\nGO\nCREATE TABLE mig_app.widget (id int NOT NULL PRIMARY KEY)\nGO\n"
        "CREATE VIEW mig_app.widget_v AS SELECT id FROM mig_app.widget\nGO\n",
        encoding="utf-8",
    )
    try:
        ensure_testdb(project)
        dsn = status(project)["dsn"]
        tracking = "dbo.schema_migrations"

        with pytest.raises(migrate.TrackingTableMissing):
            migrate.applied_migrations(dsn, tracking, "mssql")

        for path in migrate.list_migration_files(migrations):
            migrate.apply_migration(dsn, path, tracking, dialect="mssql")

        assert set(migrate.applied_migrations(dsn, tracking, "mssql")) == {"001_tracking.sql", "002_widgets.sql"}
        assert migrate.pending_migrations(migrations, dsn, tracking, dialect="mssql") == []
        assert migrate.already_fully_applied(dsn, migrations / "002_widgets.sql", "mssql") is True
    finally:
        clean_testdb(project)
