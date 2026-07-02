from __future__ import annotations

import re
import logging
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from .models import (
    ColumnDef, ConstraintDef, CompositeTypeDef, DatabaseSchema,
    EnumDef, FunctionDef, IndexDef, TableDef, ViewDef,
)

logger = logging.getLogger(__name__)

# Regex to extract dollar-quoted body
_DOLLAR_BODY = re.compile(r'\$(\w*)\$(.*?)\$\1\$', re.DOTALL | re.IGNORECASE)

# Regex for CREATE TYPE AS ENUM inside DO blocks
_DO_ENUM = re.compile(
    r'CREATE\s+TYPE\s+(\w+(?:\.\w+)?)\s+AS\s+ENUM\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL,
)

# Regex for CREATE TYPE AS composite inside DO blocks
_DO_COMPOSITE = re.compile(
    r'CREATE\s+TYPE\s+(\w+(?:\.\w+)?)\s+AS\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL,
)


def parse_directory(scripts_dir: Path) -> DatabaseSchema:
    db_schema = DatabaseSchema()
    for sql_file in sorted(scripts_dir.rglob("*.sql")):
        _parse_file(sql_file, db_schema)
    return db_schema


def _parse_file(path: Path, db_schema: DatabaseSchema) -> None:
    content = path.read_text(encoding="utf-8")
    try:
        exprs = sqlglot.parse(content, dialect="postgres", error_level=sqlglot.ErrorLevel.WARN)
    except Exception as e:
        logger.warning("sqlglot failed on %s: %s", path.name, e)
        exprs = []

    for expr in exprs:
        if expr is None:
            continue
        try:
            _handle_expr(expr, content, db_schema)
        except Exception as e:
            logger.debug("Skipping expression in %s: %s", path.name, e)

    _extract_do_block_objects(content, db_schema)


def _handle_expr(expr: exp.Expression, raw: str, db_schema: DatabaseSchema) -> None:
    if not isinstance(expr, exp.Create):
        return
    kind = (expr.args.get("kind") or "").upper()
    if kind == "TABLE":
        _handle_table(expr, db_schema)
    elif kind == "VIEW":
        _handle_view(expr, db_schema)
    elif kind in ("FUNCTION", "PROCEDURE"):
        _handle_function(expr, raw, db_schema, kind.lower())
    elif kind == "TYPE":
        _handle_type(expr, db_schema)
    elif kind == "SCHEMA":
        _handle_schema_create(expr, db_schema)
    elif kind == "INDEX":
        _handle_index(expr, db_schema)


def _resolve_name(expr: exp.Create) -> tuple[str, str] | None:
    """Return (schema, name) from a CREATE expression."""
    this = expr.this
    if isinstance(this, exp.Schema):
        table_node = this.this
    else:
        table_node = this

    if isinstance(table_node, exp.Table):
        db_node = table_node.args.get("db")
        schema = db_node.name if db_node else "public"
        return schema, table_node.name
    return None


def _handle_table(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    result = _resolve_name(expr)
    if not result:
        return
    tschema, tname = result
    properties = expr.args.get("properties")
    is_partition = any(
        isinstance(p, exp.PartitionedOfProperty) for p in (properties.expressions if properties else [])
    )
    table = TableDef(schema=tschema, name=tname, is_partition=is_partition)

    this = expr.this
    items = this.expressions if isinstance(this, exp.Schema) else []

    for item in items:
        if isinstance(item, exp.ColumnDef):
            col = _parse_column_def(item)
            if col:
                table.columns.append(col)
        else:
            constr = _parse_table_constraint(item)
            if constr:
                table.constraints.append(constr)

    db_schema.tables[table.qualified_name] = table


def _parse_column_def(col: exp.ColumnDef) -> ColumnDef | None:
    name = col.name
    if not name or col.kind is None:
        return None
    data_type = col.kind.sql(dialect="postgres").lower()
    is_nullable = True
    default = None
    is_generated = False

    for c in col.constraints:
        ck = c.kind
        if isinstance(ck, exp.NotNullColumnConstraint):
            is_nullable = False
        elif isinstance(ck, exp.DefaultColumnConstraint):
            default = ck.this.sql(dialect="postgres") if ck.this else None
        elif isinstance(ck, (exp.GeneratedAsIdentityColumnConstraint, exp.ComputedColumnConstraint)):
            is_generated = True

    return ColumnDef(name=name, data_type=data_type, is_nullable=is_nullable, default=default, is_generated=is_generated)


def _parse_table_constraint(item: exp.Expression) -> ConstraintDef | None:
    name = None
    kind = "UNKNOWN"

    if isinstance(item, exp.Constraint):
        name = item.name or None
        inner = item.args.get("kind") or (item.expressions[0] if item.expressions else None)
    else:
        inner = item

    if isinstance(inner, exp.PrimaryKey):
        kind = "PRIMARY KEY"
    elif isinstance(inner, (exp.UniqueKey, exp.Unique)):
        kind = "UNIQUE"
    elif isinstance(inner, exp.ForeignKey):
        kind = "FOREIGN KEY"
    elif isinstance(inner, exp.Check):
        kind = "CHECK"
    else:
        return None

    definition = item.sql(dialect="postgres").lower()
    return ConstraintDef(name=name, kind=kind, definition=definition)


def _handle_view(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    result = _resolve_name(expr)
    if not result:
        return
    vschema, vname = result
    query = expr.expression
    definition = query.sql(dialect="postgres").lower() if query else ""
    view = ViewDef(schema=vschema, name=vname, definition=definition)
    db_schema.views[view.qualified_name] = view


def _handle_function(expr: exp.Create, raw: str, db_schema: DatabaseSchema, kind: str) -> None:
    # Get name/schema from sqlglot
    func_node = expr.this
    if hasattr(func_node, "name"):
        fname = func_node.name
        db_node = func_node.args.get("db") if hasattr(func_node, "args") else None
        fschema = db_node.name if db_node else "public"
    else:
        result = _resolve_name(expr)
        if not result:
            return
        fschema, fname = result

    # Extract args, return type, language, body with regex on raw SQL
    args, return_type, language, body = _parse_function_details(raw)

    func = FunctionDef(
        schema=fschema,
        name=fname,
        args=args,
        return_type=return_type,
        language=language,
        body=body,
        kind=kind,
    )
    db_schema.functions[func.qualified_name] = func


_FUNC_SIG = re.compile(
    r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+'
    r'(?:\w+\.)?(\w+)\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)\s*'
    r'(?:RETURNS\s+((?:TABLE\s*\([^)]+\)|SETOF\s+\S+|\S+)))?\s*'
    r'LANGUAGE\s+(\w+)',
    re.IGNORECASE | re.DOTALL,
)


def _parse_function_details(sql: str) -> tuple[str, str, str, str]:
    args, return_type, language, body = "", "", "", ""

    m = _FUNC_SIG.search(sql)
    if m:
        args = re.sub(r'\s+', ' ', m.group(2) or "").strip().lower()
        return_type = (m.group(3) or "").strip().lower()
        language = (m.group(4) or "").strip().lower()

    dm = _DOLLAR_BODY.search(sql)
    if dm:
        raw_body = dm.group(2)
        lines = [l.strip() for l in raw_body.splitlines()]
        body = "\n".join(l.lower() for l in lines if l)

    return args, return_type, language, body


def _handle_type(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    result = _resolve_name(expr)
    if not result:
        return
    tschema, tname = result

    expression = expr.expression
    if expression is None:
        return

    if isinstance(expression, exp.DataType) and expression.this == exp.DataType.Type.ENUM:
        values = [lit.name for lit in expression.expressions if isinstance(lit, exp.Literal)]
        enum = EnumDef(schema=tschema, name=tname, values=values)
        db_schema.enums[enum.qualified_name] = enum
    elif isinstance(expression, exp.Schema):
        # Composite type: fields are ColumnDef-like
        fields = []
        for col in expression.expressions:
            if isinstance(col, exp.ColumnDef) and col.kind:
                fields.append((col.name, col.kind.sql(dialect="postgres").lower()))
        comp = CompositeTypeDef(schema=tschema, name=tname, fields=fields)
        db_schema.composites[comp.qualified_name] = comp


def _handle_schema_create(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    this = expr.this
    if hasattr(this, "name"):
        db_schema.schemas.add(this.name)


def _handle_index(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    this = expr.this
    index_name = this.name if hasattr(this, "name") else ""
    table_node = expr.find(exp.Table)
    if not table_node:
        return
    db_node = table_node.args.get("db")
    tschema = db_node.name if db_node else "public"
    tname = table_node.name
    definition = expr.sql(dialect="postgres").lower()
    idx = IndexDef(schema=tschema, table=tname, name=index_name, definition=definition)
    db_schema.indexes[idx.qualified_name] = idx


def _extract_do_block_objects(sql: str, db_schema: DatabaseSchema) -> None:
    """Extract CREATE TYPE statements from DO $$ ... $$ blocks using regex."""
    for dm in _DOLLAR_BODY.finditer(sql):
        block = dm.group(2)
        for m in _DO_ENUM.finditer(block):
            qualified = m.group(1)
            parts = qualified.split(".")
            tschema, tname = (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])
            values_raw = m.group(2)
            values = [v.strip().strip("'\"") for v in values_raw.split(",") if v.strip()]
            enum = EnumDef(schema=tschema, name=tname, values=values)
            db_schema.enums.setdefault(enum.qualified_name, enum)
        for m in _DO_COMPOSITE.finditer(block):
            qualified = m.group(1)
            parts = qualified.split(".")
            tschema, tname = (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])
            fields_raw = m.group(2)
            fields = []
            for field_def in fields_raw.split(","):
                parts2 = field_def.strip().split()
                if len(parts2) >= 2:
                    fields.append((parts2[0], " ".join(parts2[1:]).lower()))
            comp = CompositeTypeDef(schema=tschema, name=tname, fields=fields)
            db_schema.composites.setdefault(comp.qualified_name, comp)
