from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SchemaPolicy:
    """What a generated query is allowed to touch.

    `tables` maps table name -> columns the read-only role may read, in table order
    (order matters: SELECT * is expanded in this order). It is built from
    schema introspection (M2), so it can only ever contain what the database itself allows.
    """

    schema_name: str
    tables: Mapping[str, Sequence[str]]
    restricted_columns: frozenset[str] = field(default_factory=frozenset)
    default_limit: int = 100
    max_limit: int = 1000
    max_sql_length: int = 4000
