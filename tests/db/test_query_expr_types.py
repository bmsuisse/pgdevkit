"""sqlglot queries are typed as `exp.Expr`, so an `exp.Query` (base of Select/Union/Subquery) is accepted too.

In sqlglot 30 `exp.Query` is an `exp.Expr` but not an `exp.Expression`; the annotations used to say
`exp.Expression`, which a type checker such as pyright rejected for `exp.Query`-typed values (issue #56).
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlglot import exp, select

from pgdevkit.db.fetch import _render
from pgdevkit.fastapi import PostgresJsonResponse


def _union() -> exp.Query:
    return select("1 AS a").union(select("2 AS a"))


def _subquery() -> exp.Query:
    return select("a").from_(select("1 AS a").subquery("t"))


# --- runtime: no database needed for `_render` ---------------------------------------------------------------------


def test_render_union_and_subquery():
    assert _render(_union()) == "SELECT 1 AS a UNION SELECT 2 AS a"
    assert _render(_subquery()) == "SELECT a FROM (SELECT 1 AS a) AS t"
    assert _render(_union(), {}) == "SELECT 1 AS a UNION SELECT 2 AS a"


def test_postgres_json_response_accepts_union_and_subquery():
    for query in (_union(), _subquery()):
        assert _render(query) in PostgresJsonResponse(query).query.as_string()
    with pytest.raises(ValueError, match="Command"):
        PostgresJsonResponse(select("1").union(select(exp.Command(this="SELECT", expression=" version()"))))


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

