from __future__ import annotations
import re
from dataclasses import dataclass
from enum import Enum

import sqlglot
import sqlglot.expressions as exp

from .models import DatabaseSchema, FunctionDef, IndexDef, TableDef


class DiffKind(str, Enum):
    MISSING_IN_DB = "missing_in_db"
    MISSING_IN_SCRIPTS = "missing_in_scripts"
    MISMATCH = "mismatch"


@dataclass
class DiffEntry:
    kind: DiffKind
    object_type: str
    object_name: str
    detail: str = ""


def compute_diff(scripts: DatabaseSchema, db: DatabaseSchema, report_extra_db: bool = False) -> list[DiffEntry]:
    diffs: list[DiffEntry] = []

    _diff_set("schema", scripts.schemas, db.schemas, diffs, report_extra_db)

    tables_missing_in_db: set[str] = set()
    tables_missing_in_scripts: set[str] = set()

    for name, obj in scripts.tables.items():
        if name not in db.tables:
            tables_missing_in_db.add(name)
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "table", name))
        else:
            _diff_table(name, obj, db.tables[name], diffs)
    if report_extra_db:
        for name in db.tables:
            if name not in scripts.tables:
                tables_missing_in_scripts.add(name)
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "table", name))

    for name, obj in scripts.views.items():
        if name not in db.views:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "view", name))
        else:
            s = _norm_sql(obj.definition)
            d = _norm_sql(db.views[name].definition)
            if s != d:
                diffs.append(DiffEntry(DiffKind.MISMATCH, "view", name, "definition differs"))
    if report_extra_db:
        for name in db.views:
            if name not in scripts.views:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "view", name))

    for name, obj in scripts.functions.items():
        if name not in db.functions:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "function", name))
        else:
            _diff_function(name, obj, db.functions[name], diffs)
    if report_extra_db:
        for name in db.functions:
            if name not in scripts.functions:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "function", name))

    for name, obj in scripts.enums.items():
        if name not in db.enums:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "enum", name))
        elif obj.values != db.enums[name].values:
            diffs.append(DiffEntry(DiffKind.MISMATCH, "enum", name, f"values: {obj.values} vs {db.enums[name].values}"))
    if report_extra_db:
        for name in db.enums:
            if name not in scripts.enums:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "enum", name))

    for name, obj in scripts.composites.items():
        if name not in db.composites:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "composite_type", name))
        elif obj.fields != db.composites[name].fields:
            diffs.append(DiffEntry(DiffKind.MISMATCH, "composite_type", name, "fields differ"))
    if report_extra_db:
        for name in db.composites:
            if name not in scripts.composites:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "composite_type", name))

    for name, obj in scripts.indexes.items():
        if name not in db.indexes:
            if f"{obj.schema}.{obj.table}" not in tables_missing_in_db:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "index", name))
        else:
            _diff_index(name, obj, db.indexes[name], diffs)
    if report_extra_db:
        for name, obj in db.indexes.items():
            if name not in scripts.indexes:
                if f"{obj.schema}.{obj.table}" not in tables_missing_in_scripts:
                    diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "index", name))

    return diffs


def _diff_set(obj_type: str, scripts_set: set, db_set: set, diffs: list[DiffEntry], report_extra: bool) -> None:
    for item in scripts_set:
        if item not in db_set:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, obj_type, item))
    if report_extra:
        for item in db_set:
            if item not in scripts_set:
                diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, obj_type, item))


def _diff_table(name: str, s: TableDef, d: TableDef, diffs: list[DiffEntry]) -> None:
    if s.is_partition or d.is_partition:
        return

    scols = {c.name: c for c in s.columns}
    dcols = {c.name: c for c in d.columns}
    for cname, sc in scols.items():
        if cname not in dcols:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "column", f"{name}.{cname}"))
        else:
            dc = dcols[cname]
            issues = []
            if _norm_type(sc.data_type) != _norm_type(dc.data_type):
                issues.append(f"type: {sc.data_type!r} vs {dc.data_type!r}")
            if sc.is_nullable != dc.is_nullable:
                issues.append(f"nullable: {sc.is_nullable} vs {dc.is_nullable}")
            if issues:
                diffs.append(DiffEntry(DiffKind.MISMATCH, "column", f"{name}.{cname}", "; ".join(issues)))
    for cname in dcols:
        if cname not in scols:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_SCRIPTS, "column", f"{name}.{cname}"))


def _diff_function(name: str, s: FunctionDef, d: FunctionDef, diffs: list[DiffEntry]) -> None:
    issues = []
    if _norm_type(s.return_type) != _norm_type(d.return_type):
        issues.append(f"return_type: {s.return_type!r} vs {d.return_type!r}")
    if _norm_body(s.body) != _norm_body(d.body):
        issues.append("body differs")
    if issues:
        diffs.append(DiffEntry(DiffKind.MISMATCH, "function", name, "; ".join(issues)))


def _diff_index(name: str, s: IndexDef, d: IndexDef, diffs: list[DiffEntry]) -> None:
    s_info = _parse_index_def(s.definition)
    d_info = _parse_index_def(d.definition)

    if s_info is None or d_info is None:
        if _norm_sql(s.definition) != _norm_sql(d.definition):
            diffs.append(DiffEntry(DiffKind.MISMATCH, "index", name, "definition differs"))
        return

    issues = []
    if s_info["unique"] != d_info["unique"]:
        issues.append(f"unique: {s_info['unique']} vs {d_info['unique']}")
    if s_info["using"] != d_info["using"]:
        issues.append(f"using: {s_info['using']} vs {d_info['using']}")
    if s_info["columns"] != d_info["columns"]:
        issues.append(f"columns: ({', '.join(s_info['columns'])}) vs ({', '.join(d_info['columns'])})")
    if s_info["where_ast"] != d_info["where_ast"]:
        issues.append(f"where: {s_info['where_display']!r} vs {d_info['where_display']!r}")

    if issues:
        diffs.append(DiffEntry(DiffKind.MISMATCH, "index", name, "; ".join(issues)))


def _parse_index_def(definition: str) -> dict | None:
    try:
        parsed = sqlglot.parse_one(definition, dialect="postgres")
    except Exception:
        return None
    if not isinstance(parsed, exp.Create):
        return None
    index_node = parsed.this
    if not isinstance(index_node, exp.Index):
        return None

    params = index_node.args.get("params")
    using_node = params.args.get("using") if params else None
    using = using_node.name.lower() if using_node else "btree"

    columns = []
    for col in (params.args.get("columns") if params else None) or []:
        columns.append(_norm_sql(col.sql(dialect="postgres")))

    where_node = params.args.get("where") if params else None
    where_ast = _unwrap_paren(where_node.this) if where_node else None
    where_display = _norm_sql(where_node.this.sql(dialect="postgres")) if where_node else None

    return {
        "unique": bool(parsed.args.get("unique")),
        "using": using,
        "columns": columns,
        "where_ast": where_ast,
        "where_display": where_display,
    }


def _unwrap_paren(node):
    return node.transform(lambda n: n.this if isinstance(n, exp.Paren) else n)


def _norm_sql(s: str) -> str:
    return " ".join(s.lower().split())


def _norm_body(s: str) -> str:
    lines = [l.strip() for l in s.splitlines()]
    return "\n".join(l.lower() for l in lines if l)


_TYPE_SYNONYMS = {
    "int": "integer", "int4": "integer",
    "int2": "smallint",
    "int8": "bigint",
    "float4": "real",
    "float8": "double precision",
    "bool": "boolean",
    "decimal": "numeric",
    "varchar": "character varying",
    "char": "character", "bpchar": "character",
    "timestamptz": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timetz": "time with time zone",
    "time": "time without time zone",
    "varbit": "bit varying",
    "serial": "integer", "serial4": "integer",
    "smallserial": "smallint", "serial2": "smallint",
    "bigserial": "bigint", "serial8": "bigint",
}


def _norm_type(t: str) -> str:
    s = " ".join(t.lower().split())

    array_suffix = ""
    while s.endswith("[]"):
        array_suffix += "[]"
        s = s[:-2].strip()

    match = re.search(r"\(([^)]*)\)", s)
    params = f"({match.group(1)})" if match else ""
    base = (s[: match.start()] + s[match.end() :]).strip() if match else s
    base = " ".join(base.split())

    base = _TYPE_SYNONYMS.get(base, base)
    return f"{base}{params}{array_suffix}"
