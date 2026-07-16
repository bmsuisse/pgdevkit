from __future__ import annotations

from pathlib import Path
from typing import Callable

from typer.testing import CliRunner

from pgdevkit.cli import app
from pgdevkit.testdb import clean_testdb
from tests.testdb.conftest import requires_podman

runner = CliRunner()


@requires_podman
def test_testdb_up_and_status(project_factory: Callable[[str, str], Path], monkeypatch):
    project = project_factory("clitest", "main")
    monkeypatch.chdir(project)
    try:
        result = runner.invoke(app, ["testdb", "up"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["testdb", "status"])
        assert result.exit_code == 0, result.output
        assert "database:" in result.output
    finally:
        clean_testdb(project)


@requires_podman
def test_testdb_run_sql_inline_with_results(project_factory: Callable[[str, str], Path], monkeypatch):
    project = project_factory("clitest2", "main")
    monkeypatch.chdir(project)
    try:
        runner.invoke(app, ["testdb", "up"])
        result = runner.invoke(
            app, ["testdb", "run-sql", "--sql", "SELECT id, name FROM app.widget", "--results"]
        )
        assert result.exit_code == 0, result.output
        assert "sprocket" in result.output
    finally:
        clean_testdb(project)


@requires_podman
def test_testdb_run_sql_file_with_dollar_quoted_semicolon(
    project_factory: Callable[[str, str], Path], monkeypatch
):
    # A naive ';'-split would cut this function body in half at the embedded
    # semicolon inside the dollar-quoted body, breaking both statements.
    project = project_factory("clitest6", "main")
    monkeypatch.chdir(project)
    sql_file = project / "adhoc.sql"
    sql_file.write_text(
        """
        CREATE OR REPLACE FUNCTION app.answer() RETURNS int AS $$
        BEGIN
            RETURN 42;
        END;
        $$ LANGUAGE plpgsql;
        SELECT app.answer() AS answer;
        """,
        encoding="utf-8",
    )
    try:
        runner.invoke(app, ["testdb", "up"])
        result = runner.invoke(app, ["testdb", "run-sql", str(sql_file), "--results"])
        assert result.exit_code == 0, result.output
        assert "42" in result.output
    finally:
        clean_testdb(project)


@requires_podman
def test_testdb_run_sql_with_results_and_zero_rows(project_factory: Callable[[str, str], Path], monkeypatch):
    project = project_factory("clitest5", "main")
    monkeypatch.chdir(project)
    try:
        runner.invoke(app, ["testdb", "up"])
        result = runner.invoke(
            app, ["testdb", "run-sql", "--sql", "SELECT id, name FROM app.widget WHERE false", "--results"]
        )
        assert result.exit_code == 0, result.output
        assert "0 row" in result.output
    finally:
        clean_testdb(project)


@requires_podman
def test_testdb_reset(project_factory: Callable[[str, str], Path], monkeypatch):
    project = project_factory("clitest3", "main")
    monkeypatch.chdir(project)
    try:
        runner.invoke(app, ["testdb", "up"])
        result = runner.invoke(app, ["testdb", "reset"])
        assert result.exit_code == 0, result.output
    finally:
        clean_testdb(project)


@requires_podman
def test_testdb_clean(project_factory: Callable[[str, str], Path], monkeypatch):
    project = project_factory("clitest4", "main")
    monkeypatch.chdir(project)
    runner.invoke(app, ["testdb", "up"])
    result = runner.invoke(app, ["testdb", "clean"])
    assert result.exit_code == 0, result.output
