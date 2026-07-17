from __future__ import annotations

from typing import Any, Callable

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.sql import Identifier
from psycopg.types.composite import CompositeInfo, register_composite
from psycopg.types.enum import EnumInfo, register_enum
from psycopg.types.json import Jsonb

ComplexTypeInfo = CompositeInfo | EnumInfo | type[Jsonb] | None


class ComplexHelper:
    """psycopg adapter for PostgreSQL composite types, enums, and JSONB.

    Detects a table's non-scalar columns (composite types, enums, JSONB) and
    converts plain dict/list Python values into the psycopg-registered types
    those columns need, recursing into nested composite fields.

    `normalizers` lets a caller reshape a composite value before conversion,
    keyed by composite type name (e.g. a project with a `locale_labels`
    composite type that needs locale-key backfilling before it's built) —
    this is intentionally the only project-specific extension point; nothing
    else about a project's types is hardcoded here.
    """

    def __init__(
        self,
        con: AsyncConnection,
        normalizers: dict[str, Callable[[dict], dict]] | None = None,
    ) -> None:
        self.con = con
        self.system_complex_type_dict: dict[Any, tuple[str, str]] | None = None
        # Instance-scoped: a CompositeInfo/EnumInfo carries OIDs from a
        # specific connection/database, so caching it on the class (shared
        # across every connection) would leak stale OIDs across databases
        # that happen to reuse the same type name — exactly the case for
        # pgdevkit's per-worktree isolated test databases.
        self.complex_types: dict[tuple[str, str], CompositeInfo | EnumInfo] = {}
        self.registered: set[CompositeInfo | EnumInfo] = set()
        self._normalizers = normalizers or {}

    async def load_complex_type_dict(self) -> None:
        async with self.con.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
        SELECT t.oid,
                pg_catalog.format_type ( t.oid, NULL ) AS obj_name,
                t.typtype
            FROM pg_catalog.pg_type t
            JOIN pg_catalog.pg_namespace n
                ON n.oid = t.typnamespace
            WHERE ( t.typrelid = 0
                    OR ( SELECT c.relkind = 'c'
                            FROM pg_catalog.pg_class c
                            WHERE c.oid = t.typrelid ) )
                AND n.nspname <> 'pg_catalog'
                AND n.nspname <> 'information_schema'
                AND n.nspname !~ '^pg_toast'""")
            system_complex_types = await cur.fetchall()
            self.system_complex_type_dict = {r["oid"]: (r["obj_name"], r["typtype"]) for r in system_complex_types}

    async def _load_complex_type_from_colinfos(self, res: dict[str, Any] | None) -> ComplexTypeInfo:
        if not res:
            return None
        if res["data_type"].lower() == "jsonb":
            return Jsonb
        if res["data_type"].upper() == "ARRAY" and res["udt_name"] == "_jsonb":
            return Jsonb
        if (
            not res["is_enum"]
            and not res["is_user_defined"]
            and not (res["data_type"] == "ARRAY" and res["udt_schema"] != "pg_catalog")
        ):
            return None
        udt_schema: str = res["udt_schema"]
        udt_name: str = res["udt_name"]
        c = await self._get_complex_type(f"{udt_schema}.{udt_name}", res["is_enum"], self.con)
        await self._recurse_register(c, self.con)
        return c

    async def load_all_complex_types(
        self, table_name: tuple[str, str], include_generated: bool = False
    ) -> dict[str, ComplexTypeInfo]:
        if self.system_complex_type_dict is None:
            await self.load_complex_type_dict()
        colquery = """
        with enum_types as (
                            select n.nspname  as enum_schema, t.typname as enum_name from pg_type t
                                inner join pg_namespace n on n.oid=t.typnamespace
                                where typtype='e'
                        )
        select column_name, data_type,
            data_type='USER-DEFINED' as is_user_defined,
            udt_schema, udt_name,
            e.enum_name is not null as is_enum
              from information_schema.columns c
                 left join enum_types e on e.enum_schema=c.udt_schema and e.enum_name=c.udt_name
              where table_schema=%(schema)s and table_name = %(tbl)s and (is_generated <> 'ALWAYS' or %(include_generated)s)"""
        async with self.con.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                colquery,
                {
                    "schema": table_name[0],
                    "tbl": table_name[1],
                    "include_generated": include_generated,
                },
            )
            res = await cur.fetchall()
            return {r["column_name"]: await self._load_complex_type_from_colinfos(r) for r in res}

    async def load_complex_type(self, table_name: tuple[str, str], col_name: str) -> ComplexTypeInfo:
        if self.system_complex_type_dict is None:
            await self.load_complex_type_dict()
        colquery = """
        with enum_types as (
                            select n.nspname  as enum_schema, t.typname as enum_name from pg_type t
                                inner join pg_namespace n on n.oid=t.typnamespace
                                where typtype='e'
                        )
        select column_name, data_type,
            data_type='USER-DEFINED' as is_user_defined,
            udt_schema, udt_name,
            e.enum_name is not null as is_enum
              from information_schema.columns c
            left join enum_types e on e.enum_schema=c.udt_schema and e.enum_name=c.udt_name
              where table_schema=%(schema)s and table_name = %(tbl)s
                and column_name = %(col)s"""
        async with self.con.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                colquery,
                {"schema": table_name[0], "tbl": table_name[1], "col": col_name},
            )
            res = await cur.fetchone()
            return await self._load_complex_type_from_colinfos(res)

    async def _get_complex_type(self, name: str, is_enum: bool, con: AsyncConnection) -> CompositeInfo | EnumInfo:
        if name.endswith("[]"):
            name = name[:-2]
        schema, type_name = name.split(".") if "." in name else ("public", name)
        if type_name.startswith("_"):  # the array type in PostgreSQL starts with an underscore
            type_name = type_name[1:]
        if is_enum:
            ci = await EnumInfo.fetch(con, Identifier(schema, type_name))
            assert ci is not None, f"Enum type {name} not found in database"
            self.complex_types[(schema, type_name)] = ci
        if (schema, type_name) not in self.complex_types:
            ci = await CompositeInfo.fetch(con, Identifier(schema, type_name))
            assert ci is not None, f"Complex type {name} not found in database"
            self.complex_types[(schema, type_name)] = ci
        return self.complex_types[(schema, type_name)]

    async def _recurse_register(self, info: CompositeInfo | EnumInfo, con: AsyncConnection) -> None:
        assert self.system_complex_type_dict is not None, "System complex type dictionary not loaded"
        if info not in self.registered:
            if isinstance(info, EnumInfo):
                register_enum(info, con)
            else:
                register_composite(info, con)
            self.registered.add(info)
        if isinstance(info, EnumInfo):
            return
        for t in info.field_types:
            if t in self.system_complex_type_dict:
                name, typtype = self.system_complex_type_dict[t]
                ci = await self._get_complex_type(name, typtype == "e", con)
                await self._recurse_register(ci, con)

    async def recursive_convert(
        self,
        value: Any,
        info: ComplexTypeInfo,
        con: AsyncConnection,
    ) -> Any:
        if info is None:
            return value
        if value is None:
            return None
        if self.system_complex_type_dict is None:
            await self.load_complex_type_dict()
        if info == Jsonb:
            # A JSONB column's value is wrapped whole, even if it's a list —
            # only an array-of-composite/enum column recurses per element.
            return Jsonb(value)
        if isinstance(value, list):
            return [await self.recursive_convert(item, info, con) for item in value]
        prms = {}
        if isinstance(value, str):
            assert isinstance(info, EnumInfo), f"Expected EnumInfo, got {type(info)}"
            return getattr(info.enum, value)  # Enum
        assert isinstance(info, CompositeInfo), f"Expected CompositeInfo, got {type(info)}"
        assert self.system_complex_type_dict is not None, "System complex type dictionary not loaded"
        normalizer = self._normalizers.get(info.name)
        if normalizer is not None:
            value = normalizer(value)
        for k, v in value.items():
            if v is None:
                prms[k] = None
                continue
            fi = info.field_names.index(k)
            type_oid = info.field_types[fi]
            if type_oid in self.system_complex_type_dict:
                name, typtype = self.system_complex_type_dict[type_oid]
                ci = await self._get_complex_type(name, typtype == "e", con)
                if name.endswith("[]"):
                    prms[k] = [await self.recursive_convert(item, ci, con) for item in v]
                else:
                    prms[k] = await self.recursive_convert(v, ci, con)
            else:
                prms[k] = v
        assert info.python_type is not None, f"Python type for {info.name} is null, maybe an array?"
        return info.python_type(**prms) if prms else None
