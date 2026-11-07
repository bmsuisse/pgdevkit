from __future__ import annotations

from pgdevkit.testdb.query import split_mariadb_statements


def test_splits_on_semicolons_and_drops_empty_statements():
    assert split_mariadb_statements("SELECT 1;; SELECT 2;\n") == ["SELECT 1", "SELECT 2"]


def test_semicolons_inside_literals_and_identifiers_do_not_split():
    sql = "INSERT INTO t VALUES ('a;b', \"c;d\", 'it''s', 'x\\';y'); SELECT `we;ird` FROM t"
    assert split_mariadb_statements(sql) == [
        "INSERT INTO t VALUES ('a;b', \"c;d\", 'it''s', 'x\\';y')",
        "SELECT `we;ird` FROM t",
    ]


def test_comments_are_opaque_and_comment_only_statements_are_dropped():
    sql = "-- lead; in\n# hash; comment\n/* block; */\nSELECT 1; -- trailing;\n/* only a comment */;\nSELECT 2"
    statements = split_mariadb_statements(sql)
    assert [s.splitlines()[-1].split("--")[0].strip() for s in statements] == ["SELECT 1", "SELECT 2"]
    assert len(statements) == 2


def test_double_dash_without_space_is_not_a_comment():
    assert split_mariadb_statements("SELECT 5--3;SELECT 1") == ["SELECT 5--3", "SELECT 1"]


def test_executable_comment_is_kept():
    assert split_mariadb_statements("/*!40101 SET @a = 1 */;") == ["/*!40101 SET @a = 1 */"]


def test_delimiter_directive_wraps_a_body_with_semicolons():
    sql = (
        "DROP PROCEDURE IF EXISTS p;\n"
        "DELIMITER $$\n"
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND$$\n"
        "DELIMITER ;\n"
        "SELECT 3;"
    )
    assert split_mariadb_statements(sql) == [
        "DROP PROCEDURE IF EXISTS p",
        "CREATE PROCEDURE p()\nBEGIN\n  SELECT 1;\n  SELECT 2;\nEND",
        "SELECT 3",
    ]


def test_empty_script_has_no_statements():
    assert split_mariadb_statements("  \n-- nothing\n") == []


def test_delimiter_inside_a_comment_does_not_split():
    sql = (
        "DELIMITER $$\n"
        "CREATE PROCEDURE p()\nBEGIN\n  -- ends with $$\n  /* or $$ here */\n  # nor $$ here\n  SELECT 1;\nEND$$\n"
        "DELIMITER ;"
    )
    assert split_mariadb_statements(sql) == [
        "CREATE PROCEDURE p()\nBEGIN\n  -- ends with $$\n  /* or $$ here */\n  # nor $$ here\n  SELECT 1;\nEND"
    ]
