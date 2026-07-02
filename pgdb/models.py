from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ColumnDef:
    name: str
    data_type: str
    is_nullable: bool
    default: str | None
    is_generated: bool = False


@dataclass
class ConstraintDef:
    name: str | None
    kind: str  # PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK
    definition: str


@dataclass
class TableDef:
    schema: str
    name: str
    columns: list[ColumnDef] = field(default_factory=list)
    constraints: list[ConstraintDef] = field(default_factory=list)
    is_partition: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class ViewDef:
    schema: str
    name: str
    definition: str

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class FunctionDef:
    schema: str
    name: str
    args: str
    return_type: str
    language: str
    body: str
    kind: str = "function"

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class EnumDef:
    schema: str
    name: str
    values: list[str]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class CompositeTypeDef:
    schema: str
    name: str
    fields: list[tuple[str, str]]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class IndexDef:
    schema: str
    table: str
    name: str
    definition: str

    @property
    def qualified_name(self) -> str:
        return self.name


@dataclass
class DatabaseSchema:
    schemas: set[str] = field(default_factory=set)
    tables: dict[str, TableDef] = field(default_factory=dict)
    views: dict[str, ViewDef] = field(default_factory=dict)
    functions: dict[str, FunctionDef] = field(default_factory=dict)
    enums: dict[str, EnumDef] = field(default_factory=dict)
    composites: dict[str, CompositeTypeDef] = field(default_factory=dict)
    indexes: dict[str, IndexDef] = field(default_factory=dict)
