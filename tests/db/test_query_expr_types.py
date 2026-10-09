"""sqlglot queries are typed as `exp.Expr`, so an `exp.Query` (base of Select/Union/Subquery) is accepted too.

In sqlglot 30 `exp.Query` is an `exp.Expr` but not an `exp.Expression`; the annotations used to say
`exp.Expression`, which type checkers rejected for `exp.Query`-typed values (issue #56).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlglot import exp, select

from pgdevkit.db import PgPool, execute, fetch_all, fetch_one, fetch_scalar
from pgdevkit.db.fetch import _render
from pgdevkit.fastapi import PostgresJsonResponse


def _union() -> exp.Query:
    return select("1 AS a").union(select("2 AS a"))


def _subquery() -> exp.Query:
    return select("a").from_(select("1 AS a").subquery("t"))


# --- type level: never executed, only checked by pyright/ty (see test_type_checker_accepts_exp_query_arguments) ---


async def _typed_usage(pool: PgPool, query: exp.Query, expr: exp.Expr) -> None:
    await fetch_all(query, pool=pool)
    await fetch_one(query, pool=pool)
    await fetch_scalar(query, pool=pool)
    await execute(query, pool=pool)
    await fetch_all(expr, pool=pool)
    await fetch_all(_union(), pool=pool)
    PostgresJsonResponse(query, pool=pool)
    PostgresJsonResponse(expr, pool=pool)


@pytest.mark.parametrize("checker", ["pyright", "ty"])
def test_type_checker_accepts_exp_query_arguments(checker: str):
    # ty does not resolve `exp.Query`, so it passes even with the old annotation; pyright is the real guard.
    # (CI runs `ty check pgdevkit`, not the tests directory.)
    file = str(Path(__file__))
    cmd = (
        [sys.executable, "-m", "ty", "check", "--python", sys.executable, file]
        if checker == "ty"
        else [sys.executable, "-m", "pyright", "--pythonpath", sys.executable, file]
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        pytest.skip(f"{checker} timed out")
    output = result.stdout + result.stderr
    if f"No module named {checker}" in output:
        pytest.skip(f"{checker} is not installed")
    if checker == "pyright" and result.returncode != 0 and " - error:" not in output:
        pytest.skip(f"pyright could not start: {output[:200]}")  # e.g. its node download (nodeenv) failed
    assert result.returncode == 0, output


# --- runtime: no database needed for `_render` ---------------------------------------------------------------------


def test_render_union_and_subquery():
    assert _render(_union()) == "SELECT 1 AS a UNION SELECT 2 AS a"
    assert _render(_subquery()) == "SELECT a FROM (SELECT 1 AS a) AS t"
    assert _render(_union(), {}) == "SELECT 1 AS a UNION SELECT 2 AS a"


def test_render_keeps_placeholders_and_escapes_percent_inside_a_union():
    query = (
        select("name").from_("w").where(exp.column("id").eq(exp.Placeholder(this="id"))).where("name LIKE '50%'")
    ).union(select("'x%'"))
    rendered = _render(query, {"id": 1})
    assert "%(id)s" in rendered
    assert "'50%%'" in rendered
    assert "'x%%'" in rendered


def test_render_rejects_command_and_bad_placeholder_nested_in_a_union():
    command = exp.Command(this="SELECT", expression=" version()")
    with pytest.raises(ValueError, match="Command"):
        _render(select("1").union(select(command)))
    with pytest.raises(ValueError, match="Command"):
        _render(select("a").from_(select(command).subquery("t")))
    bad = exp.Placeholder(this="x); DROP TABLE t;--")
    with pytest.raises(ValueError, match="placeholder name"):
        _render(select("1").union(select("2").where(bad)), {"x": 1})
    with pytest.raises(ValueError, match="placeholder name"):
        _render(select("a").from_(select("2 AS a").where(bad).subquery("t")), {"x": 1})


def test_importing_pgdevkit_db_does_not_import_sqlglot():
    code = "import sys, pgdevkit.db; sys.exit('sqlglot' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0

