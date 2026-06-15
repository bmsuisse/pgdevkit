from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .models import DatabaseSchema, FunctionDef, TableDef


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

    for name, obj in scripts.tables.items():
        if name not in db.tables:
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "table", name))
        else:
            _diff_table(name, obj, db.tables[name], diffs)
    if report_extra_db:
        for name in db.tables:
            if name not in scripts.tables:
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
            diffs.append(DiffEntry(DiffKind.MISSING_IN_DB, "index", name))
        elif _norm_sql(obj.definition) != _norm_sql(db.indexes[name].definition):
            diffs.append(DiffEntry(DiffKind.MISMATCH, "index", name, "definition differs"))
    if report_extra_db:
        for name in db.indexes:
            if name not in scripts.indexes:
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


def _norm_sql(s: str) -> str:
    return " ".join(s.lower().split())


def _norm_body(s: str) -> str:
    lines = [l.strip() for l in s.splitlines()]
    return "\n".join(l.lower() for l in lines if l)


def _norm_type(t: str) -> str:
    s = t.lower().strip()
    return {"int": "integer", "int2": "smallint", "int4": "integer", "int8": "bigint",
            "float4": "real", "float8": "double precision", "bool": "boolean",
            "decimal": "numeric"}.get(s, s)
