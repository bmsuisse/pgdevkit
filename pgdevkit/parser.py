from __future__ import annotations

import re
import logging
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from .dialect import Dialect, POSTGRES, resolve_dialect
from .models import (
    ColumnDef, ConstraintDef, CompositeTypeDef, DatabaseSchema,
    EnumDef, FunctionDef, IndexDef, TableDef, ViewDef,
)

logger = logging.getLogger(__name__)

# Regex to extract dollar-quoted body (Postgres-only construct)
_DOLLAR_BODY = re.compile(r'\$(\w*)\$(.*?)\$\1\$', re.DOTALL | re.IGNORECASE)

# Regex for CREATE TYPE AS ENUM inside DO blocks (Postgres-only construct)
_DO_ENUM = re.compile(
    r'CREATE\s+TYPE\s+(\w+(?:\.\w+)?)\s+AS\s+ENUM\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL,
)

# Regex for CREATE TYPE AS composite inside DO blocks (Postgres-only construct)
_DO_COMPOSITE = re.compile(
    r'CREATE\s+TYPE\s+(\w+(?:\.\w+)?)\s+AS\s*\(([^)]+)\)',
    re.IGNORECASE | re.DOTALL,
)


def parse_directory(scripts_dir: Path, *, dialect: str | Dialect = "postgres") -> DatabaseSchema:
    resolved = resolve_dialect(dialect)
    db_schema = DatabaseSchema()
    for sql_file in sorted(scripts_dir.rglob("*.sql")):
        _parse_file(sql_file, db_schema, resolved)
    return db_schema


def _parse_file(path: Path, db_schema: DatabaseSchema, dialect: Dialect = POSTGRES) -> None:
    content = path.read_text(encoding="utf-8")
    try:
        exprs = sqlglot.parse(content, dialect=dialect.sqlglot_name, error_level=sqlglot.ErrorLevel.WARN)
    except Exception as e:
        logger.warning("sqlglot failed on %s: %s", path.name, e)
        exprs = []

    for expr in exprs:
        if expr is None:
            continue
        try:
            _handle_expr(expr, content, db_schema, dialect)
        except Exception as e:
            logger.debug("Skipping expression in %s: %s", path.name, e)

    # DO $$ ... $$ blocks are a Postgres-only construct; T-SQL has no
    # equivalent, so this scan simply doesn't apply to other dialects.
    if dialect.name == "postgres":
        _extract_do_block_objects(content, db_schema)


def _handle_expr(expr: exp.Expression, raw: str, db_schema: DatabaseSchema, dialect: Dialect) -> None:
    if not isinstance(expr, exp.Create):
        return
    kind = (expr.args.get("kind") or "").upper()
    if kind == "TABLE":
        _handle_table(expr, db_schema, dialect)
    elif kind == "VIEW":
        _handle_view(expr, db_schema, dialect)
    elif kind in ("FUNCTION", "PROCEDURE"):
        _handle_function(expr, raw, db_schema, kind.lower(), dialect)
    elif kind == "TYPE":
        _handle_type(expr, db_schema, dialect)
    elif kind == "SCHEMA":
        _handle_schema_create(expr, db_schema)
    elif kind == "INDEX":
        _handle_index(expr, db_schema, dialect)


def _resolve_name(expr: exp.Create, dialect: Dialect) -> tuple[str, str] | None:
    """Return (schema, name) from a CREATE expression."""
    this = expr.this
    if isinstance(this, exp.Schema):
        table_node = this.this
    else:
        table_node = this

    if isinstance(table_node, exp.Table):
        db_node = table_node.args.get("db")
        schema = db_node.name if db_node else dialect.default_schema
        return schema, table_node.name
    return None


def _handle_table(expr: exp.Create, db_schema: DatabaseSchema, dialect: Dialect) -> None:
    result = _resolve_name(expr, dialect)
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

    pk_columns: set[str] = set()
    for item in items:
        if isinstance(item, exp.ColumnDef):
            col = _parse_column_def(item, dialect)
            if col:
                table.columns.append(col)
        else:
            constr = _parse_table_constraint(item, dialect)
            if constr:
                table.constraints.append(constr)
            pk_columns |= _extract_primary_key_columns(item)

    for col in table.columns:
        if col.name in pk_columns:
            col.is_nullable = False

    db_schema.tables[table.qualified_name] = table


def _extract_primary_key_columns(item: exp.Expression) -> set[str]:
    inner = item
    if isinstance(item, exp.Constraint):
        inner = item.args.get("kind") or (item.expressions[0] if item.expressions else None)
    if isinstance(inner, exp.PrimaryKey):
        return {e.name for e in inner.expressions if hasattr(e, "name")}
    return set()


def _parse_column_def(col: exp.ColumnDef, dialect: Dialect) -> ColumnDef | None:
    name = col.name
    if not name or col.kind is None:
        return None
    data_type = col.kind.sql(dialect=dialect.sqlglot_name).lower()
    is_serial = col.kind.this in (
        exp.DataType.Type.SERIAL, exp.DataType.Type.SMALLSERIAL, exp.DataType.Type.BIGSERIAL,
    )
    is_nullable = not is_serial
    default = None
    is_generated = False

    for c in col.constraints:
        ck = c.kind
        if isinstance(ck, exp.PrimaryKeyColumnConstraint):
            is_nullable = False
        elif isinstance(ck, exp.NotNullColumnConstraint):
            # sqlglot represents both "NOT NULL" and an explicit "NULL"
            # (common T-SQL style) as this same node, distinguished only by
            # allow_null -- true for the latter, which must NOT mark the
            # column non-nullable.
            if not ck.args.get("allow_null"):
                is_nullable = False
        elif isinstance(ck, exp.DefaultColumnConstraint):
            default = ck.this.sql(dialect=dialect.sqlglot_name) if ck.this else None
        elif isinstance(ck, exp.GeneratedAsIdentityColumnConstraint):
            is_generated = True
            is_nullable = False
        elif isinstance(ck, exp.ComputedColumnConstraint):
            is_generated = True

    return ColumnDef(name=name, data_type=data_type, is_nullable=is_nullable, default=default, is_generated=is_generated)


def _parse_table_constraint(item: exp.Expression, dialect: Dialect) -> ConstraintDef | None:
    name = None
    kind = "UNKNOWN"

    if isinstance(item, exp.Constraint):
        name = item.name or None
        inner = item.args.get("kind") or (item.expressions[0] if item.expressions else None)
    else:
        inner = item

    if isinstance(inner, exp.PrimaryKey):
        kind = "PRIMARY KEY"
    elif isinstance(inner, exp.UniqueColumnConstraint):
        kind = "UNIQUE"
    elif isinstance(inner, exp.ForeignKey):
        kind = "FOREIGN KEY"
    elif isinstance(inner, exp.Check):
        kind = "CHECK"
    else:
        return None

    definition = item.sql(dialect=dialect.sqlglot_name).lower()
    return ConstraintDef(name=name, kind=kind, definition=definition)


def _handle_view(expr: exp.Create, db_schema: DatabaseSchema, dialect: Dialect) -> None:
    result = _resolve_name(expr, dialect)
    if not result:
        return
    vschema, vname = result
    query = expr.expression
    definition = query.sql(dialect=dialect.sqlglot_name).lower() if query else ""
    view = ViewDef(schema=vschema, name=vname, definition=definition)
    db_schema.views[view.qualified_name] = view


def _handle_function(expr: exp.Create, raw: str, db_schema: DatabaseSchema, kind: str, dialect: Dialect) -> None:
    # Get name/schema from sqlglot
    func_node = expr.this
    fname = func_node.name if hasattr(func_node, "name") else ""
    if fname:
        db_node = func_node.args.get("db") if hasattr(func_node, "args") else None
        fschema = db_node.name if db_node else dialect.default_schema
    else:
        # sqlglot (30.11.0) parses "CREATE FUNCTION myapp.greet(...)" as a
        # UserDefinedFunction wrapping a Table (this=Identifier(greet),
        # db=Identifier(myapp)). UserDefinedFunction.name doesn't unwrap
        # that nested Table, so pull the name/schema from it directly.
        inner = func_node.this if hasattr(func_node, "this") else None
        if isinstance(inner, exp.Table) and inner.name:
            fname = inner.name
            db_node = inner.args.get("db")
            fschema = db_node.name if db_node else dialect.default_schema
        else:
            result = _resolve_name(expr, dialect)
            if not result:
                return
            fschema, fname = result

    # Extract args, return type, language, body with regex on raw SQL --
    # dialect-specific since Postgres (dollar-quoted body, LANGUAGE clause)
    # and T-SQL (AS BEGIN...END, no LANGUAGE clause) use different syntax.
    if dialect.name == "mssql":
        args, return_type, language, body = _parse_function_details_tsql(raw)
    else:
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


# T-SQL has no LANGUAGE clause (it's always effectively "sql") and no
# dollar-quoting -- a function/procedure body is just "AS [BEGIN] ... [END]"
# running to the end of the statement. This is a first-cut regex covering
# the common single-object-per-file convention this project uses; it isn't
# meant to handle every T-SQL corner case (nested BEGIN/END blocks with
# their own trailing semicolons, WITH ENCRYPTION/SCHEMABINDING options
# between the signature and AS, etc).
_FUNC_SIG_TSQL = re.compile(
    r'CREATE\s+(?:OR\s+ALTER\s+)?(?:FUNCTION|PROCEDURE|PROC)\s+'
    r'(?:\[?\w+\]?\.)?\[?\w+\]?\s*'
    r'(\(([^)]*)\))?\s*'
    r'(?:RETURNS\s+([^\s(]+(?:\s*\([^)]*\))?))?\s*'
    r'AS\b',
    re.IGNORECASE | re.DOTALL,
)

_BEGIN_END_WRAPPER = re.compile(r'^\s*BEGIN\b(.*)\bEND\s*;?\s*$', re.IGNORECASE | re.DOTALL)


def _parse_function_details_tsql(sql: str) -> tuple[str, str, str, str]:
    args, return_type, body = "", "", ""

    m = _FUNC_SIG_TSQL.search(sql)
    if m:
        args = re.sub(r'\s+', ' ', m.group(2) or "").strip().lower()
        return_type = (m.group(3) or "").strip().lower()
        raw_body = sql[m.end():].strip()
        wrapper = _BEGIN_END_WRAPPER.match(raw_body)
        if wrapper:
            raw_body = wrapper.group(1)
        lines = [l.strip() for l in raw_body.splitlines()]
        body = "\n".join(l.lower() for l in lines if l)

    return args, return_type, "sql", body


def _handle_type(expr: exp.Create, db_schema: DatabaseSchema, dialect: Dialect) -> None:
    # CREATE TYPE ... AS ENUM / AS (composite fields) are Postgres-only
    # constructs. T-SQL's CREATE TYPE forms (table types, alias types) parse
    # to different AST shapes that simply won't match the isinstance checks
    # below, so this naturally no-ops for dialects without enum/composite
    # support rather than needing an explicit dialect branch.
    if not dialect.supports_enums and not dialect.supports_composites:
        return

    result = _resolve_name(expr, dialect)
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
                fields.append((col.name, col.kind.sql(dialect=dialect.sqlglot_name).lower()))
        comp = CompositeTypeDef(schema=tschema, name=tname, fields=fields)
        db_schema.composites[comp.qualified_name] = comp


def _handle_schema_create(expr: exp.Create, db_schema: DatabaseSchema) -> None:
    this = expr.this
    name = this.name if hasattr(this, "name") else ""
    if not name:
        # sqlglot (30.11.0) parses "CREATE SCHEMA myapp" as a Table node
        # whose `db` arg holds the schema name and `.name` (the table
        # identifier) is empty.
        db_node = this.args.get("db") if hasattr(this, "args") else None
        name = db_node.name if db_node else ""
    if name:
        db_schema.schemas.add(name)


def _handle_index(expr: exp.Create, db_schema: DatabaseSchema, dialect: Dialect) -> None:
    this = expr.this
    index_name = this.name if hasattr(this, "name") else ""
    table_node = expr.find(exp.Table)
    if not table_node:
        return
    db_node = table_node.args.get("db")
    tschema = db_node.name if db_node else dialect.default_schema
    tname = table_node.name
    definition = expr.sql(dialect=dialect.sqlglot_name).lower()
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
