from __future__ import annotations

from pathlib import Path

import pymysql
import pytest

from pgdevkit import migrate
from pgdevkit.testdb.api import clean_testdb, ensure_testdb, status
from pgdevkit.testdb.mariadb import constants
from tests.testdb.conftest import _make_project, requires_mariadb

# Selected only by the dedicated `mariadb-test` CI job (see test_api_mariadb_live.py).
pytestmark = pytest.mark.mariadb

_TRACKING = """\
CREATE TABLE schema_migrations (
    filename varchar(255) NOT NULL PRIMARY KEY,
    applied_at timestamp NOT NULL DEFAULT current_timestamp,
    applied_by varchar(128) NOT NULL DEFAULT (current_user())
);
"""


@requires_mariadb
def test_migrations_apply_and_track_against_live_mariadb(tmp_path: Path):
    project = _make_project(tmp_path, "mdbmigrate", "main", engine="mariadb")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_tracking.sql").write_text(_TRACKING, encoding="utf-8")
    (migrations / "002_widgets.sql").write_text(
        "CREATE TABLE IF NOT EXISTS widget (id int NOT NULL PRIMARY KEY);\n"
        "CREATE VIEW widget_v AS SELECT id FROM widget;\n"
        "DELIMITER $$\nCREATE PROCEDURE widget_count()\nBEGIN\n  SELECT count(*) FROM widget;\nEND$$\nDELIMITER ;\n",
        encoding="utf-8",
    )
    (migrations / "003_note.sql").write_text("ALTER TABLE widget ADD COLUMN note varchar(20);\n", encoding="utf-8")
    try:
        ensure_testdb(project)
        info = status(project)
        url = constants.dsn(info["database"])
        tracking = migrate.default_tracking_table(tmp_path, dialect="mariadb")
        assert tracking == "schema_migrations"

        with pytest.raises(migrate.TrackingTableMissing):
            migrate.applied_migrations(url, tracking, "mariadb")

        for path in migrate.list_migration_files(migrations):
            result = migrate.apply_migration(url, path, tracking, dialect="mariadb")
            assert result.executed

        applied = migrate.applied_migrations(url, tracking, "mariadb")
        assert set(applied) == {"001_tracking.sql", "002_widgets.sql", "003_note.sql"}
        assert migrate.pending_migrations(migrations, url, tracking, dialect="mariadb") == []
        assert migrate.already_fully_applied(url, migrations / "003_note.sql", "mariadb") is True
        # the CREATE PROCEDURE statement is not a recognised shape, so the whole file is asked about
        assert migrate.already_fully_applied(url, migrations / "002_widgets.sql", "mariadb") is False
    finally:
        clean_testdb(project)


@requires_mariadb
def test_failed_migration_raises_and_is_not_recorded(tmp_path: Path):
    project = _make_project(tmp_path, "mdbmigrate2", "main", engine="mariadb")
    bad = tmp_path / "002_bad.sql"
    bad.write_text("CREATE TABLE ok_t (id int);\nINSERT INTO missing_table VALUES (1);\n", encoding="utf-8")
    try:
        ensure_testdb(project)
        url = constants.dsn(status(project)["database"])
        migrate.execute_sql_script(url, _TRACKING, "mariadb")
        with pytest.raises(pymysql.MySQLError):
            migrate.apply_migration(url, bad, "schema_migrations", dialect="mariadb")
        assert migrate.applied_migrations(url, "schema_migrations", "mariadb") == {}
    finally:
        clean_testdb(project)
