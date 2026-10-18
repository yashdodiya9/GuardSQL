"""Read the schema as the read-only role sees it.

Important: we use information_schema.columns, which only lists columns the current role
has some privilege on. pg_catalog (pg_attribute) lists EVERY column to every role, so
reflecting through it would leak restricted column names into the LLM prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row

from guardsql.guardrails.policy import SchemaPolicy

COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type,
       (c.is_nullable = 'YES') AS nullable,
       col_description(
           (quote_ident(c.table_schema) || '.' || quote_ident(c.table_name))::regclass,
           c.ordinal_position::int
       ) AS description
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = %(schema)s AND t.table_type = 'BASE TABLE'
ORDER BY c.table_name, c.ordinal_position
"""

TABLE_COMMENTS_SQL = """
SELECT cl.relname AS table_name, obj_description(cl.oid, 'pg_class') AS description
FROM pg_class cl
JOIN pg_namespace n ON n.oid = cl.relnamespace
WHERE n.nspname = %(schema)s AND cl.relkind = 'r'
"""

# information_schema.table_constraints is empty for a SELECT-only role, so keys come from
# pg_constraint. We filter them against the visible columns afterwards.
CONSTRAINTS_SQL = """
SELECT cl.relname AS table_name, con.contype AS kind,
       (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum) AS columns,
       fcl.relname AS ref_table,
       (SELECT array_agg(a.attname::text ORDER BY k.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum) AS ref_columns
FROM pg_constraint con
JOIN pg_class cl ON cl.oid = con.conrelid
JOIN pg_namespace n ON n.oid = cl.relnamespace
LEFT JOIN pg_class fcl ON fcl.oid = con.confrelid
WHERE n.nspname = %(schema)s AND con.contype IN ('p', 'f')
ORDER BY cl.relname, con.conname
"""


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    description: str | None = None


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    description: str | None = None
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKey, ...] = field(default_factory=tuple)

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)

    @property
    def column_order(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


@dataclass(frozen=True)
class SchemaSnapshot:
    schema_name: str
    tables: tuple[Table, ...]

    def table(self, name: str) -> Table:
        for table in self.tables:
            if table.name == name:
                return table
        raise KeyError(name)

    def to_policy(
        self, restricted_columns: frozenset[str] = frozenset(), **limits: int
    ) -> SchemaPolicy:
        """What the validator may allow: exactly the tables and columns we just read."""
        return SchemaPolicy(
            schema_name=self.schema_name,
            tables={t.name: t.column_order for t in self.tables},
            restricted_columns=restricted_columns,
            **limits,
        )

    def to_prompt(self) -> str:
        """Compact, token-cheap description of the schema for the LLM."""
        lines = [f"Schema: {self.schema_name} (PostgreSQL)"]
        for table in self.tables:
            header = f"TABLE {table.name}"
            if table.description:
                header += f" -- {table.description}"
            lines.append(header)
            single_fks = {fk.columns[0]: fk for fk in table.foreign_keys if len(fk.columns) == 1}
            for col in table.columns:
                parts = [f"  {col.name} {col.data_type}"]
                if table.primary_key == (col.name,):
                    parts.append("PRIMARY KEY")
                if col.name in single_fks:
                    fk = single_fks[col.name]
                    parts.append(f"REFERENCES {fk.ref_table}({fk.ref_columns[0]})")
                if col.description:
                    parts.append(f"-- {col.description}")
                lines.append(" ".join(parts))
            for fk in table.foreign_keys:
                if len(fk.columns) > 1:
                    cols, refs = ", ".join(fk.columns), ", ".join(fk.ref_columns)
                    lines.append(f"  FOREIGN KEY ({cols}) REFERENCES {fk.ref_table}({refs})")
            if len(table.primary_key) > 1:
                lines.append(f"  PRIMARY KEY ({', '.join(table.primary_key)})")
        return "\n".join(lines)


def introspect(conn: psycopg.Connection, schema: str = "shop") -> SchemaSnapshot:
    """Build a snapshot using the given (read-only) connection."""
    params = {"schema": schema}
    with conn.cursor(row_factory=dict_row) as cur:
        column_rows = cur.execute(COLUMNS_SQL, params).fetchall()
        comment_rows = cur.execute(TABLE_COMMENTS_SQL, params).fetchall()
        constraint_rows = cur.execute(CONSTRAINTS_SQL, params).fetchall()

    columns: dict[str, list[Column]] = {}
    for row in column_rows:
        columns.setdefault(row["table_name"], []).append(
            Column(
                row["column_name"],
                row["data_type"],
                row["nullable"],
                row["description"],
            )
        )
    table_comments = {r["table_name"]: r["description"] for r in comment_rows}
    visible = {name: {c.name for c in cols} for name, cols in columns.items()}

    primary_keys: dict[str, tuple[str, ...]] = {}
    foreign_keys: dict[str, list[ForeignKey]] = {}
    for row in constraint_rows:
        table = row["table_name"]
        if table not in visible or not set(row["columns"]) <= visible[table]:
            continue
        if row["kind"] == "p":
            primary_keys[table] = tuple(row["columns"])
        else:
            ref = row["ref_table"]
            if ref in visible and set(row["ref_columns"]) <= visible[ref]:
                foreign_keys.setdefault(table, []).append(
                    ForeignKey(tuple(row["columns"]), ref, tuple(row["ref_columns"]))
                )

    tables = tuple(
        Table(
            name=name,
            columns=tuple(cols),
            description=table_comments.get(name),
            primary_key=primary_keys.get(name, ()),
            foreign_keys=tuple(foreign_keys.get(name, ())),
        )
        for name, cols in columns.items()
    )
    return SchemaSnapshot(schema_name=schema, tables=tables)
