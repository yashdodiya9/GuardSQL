"""Deterministic SQL guardrail: parse, check, rewrite, regenerate.

Core rule: the caller must execute `ValidationResult.sql` (regenerated from the checked
syntax tree), never the original string. That way the thing we checked is the thing we run.
Comments are dropped on regeneration (sqlglot keeps them unless told not to).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from guardsql.guardrails.policy import SchemaPolicy

DIALECT = "postgres"


class Code(StrEnum):
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    PARSE_ERROR = "PARSE_ERROR"
    TOO_COMPLEX = "TOO_COMPLEX"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_SELECT = "NOT_SELECT"
    FORBIDDEN_CONSTRUCT = "FORBIDDEN_CONSTRUCT"
    RECURSIVE_CTE = "RECURSIVE_CTE"
    FUNCTION_NOT_ALLOWED = "FUNCTION_NOT_ALLOWED"
    SCHEMA_NOT_ALLOWED = "SCHEMA_NOT_ALLOWED"
    UNKNOWN_TABLE = "UNKNOWN_TABLE"
    RESTRICTED_COLUMN = "RESTRICTED_COLUMN"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    UNANALYZABLE = "UNANALYZABLE"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    sql: str | None = None
    code: Code | None = None
    message: str | None = None
    tables: frozenset[str] = field(default_factory=frozenset)
    limit: int | None = None


def _reject(code: Code, message: str) -> ValidationResult:
    return ValidationResult(ok=False, code=code, message=message)


# Anything that can write, change settings, read files, or smuggle in parameters.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.Set,
    exp.Copy,
    exp.TruncateTable,
    exp.Into,  # SELECT ... INTO creates a table
    exp.Lock,  # SELECT ... FOR UPDATE takes row locks
    exp.Placeholder,
    exp.Parameter,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.Describe,
    exp.Analyze,
)

# Default-deny for functions. Typed sqlglot nodes are allowed only if listed here.
ALLOWED_FUNCTION_CLASSES: frozenset[str] = frozenset(
    {
        # aggregates
        "Count", "Sum", "Avg", "Min", "Max", "CountIf", "Stddev", "StddevPop",
        "StddevSamp", "Variance", "VariancePop", "ArrayAgg", "GroupConcat",
        # window functions
        "RowNumber", "Rank", "DenseRank", "Ntile", "Lag", "Lead", "FirstValue",
        "LastValue", "PercentRank", "CumeDist",
        # math and strings
        "Abs", "Ceil", "Floor", "Round", "Sqrt", "Pow", "Lower", "Upper", "Length",
        "Trim", "Substring", "Concat", "ConcatWs", "Replace", "Left", "Right", "Initcap",
        # null handling and conditionals
        "Coalesce", "Nullif", "Greatest", "Least", "If", "Case", "Cast", "Exists",
        # dates
        "Extract", "DateTrunc", "TimestampTrunc", "CurrentDate", "CurrentTimestamp",
        "TimeToStr",
    }
)  # fmt: skip

# Functions sqlglot does not model (parsed as "Anonymous") that we still allow.
ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset({"age"})


def validate_sql(sql: str, policy: SchemaPolicy) -> ValidationResult:
    """Validate and rewrite one LLM-generated query. Never raises."""
    try:
        return _validate(sql, policy)
    except RecursionError:
        return _reject(Code.TOO_COMPLEX, "Query is too deeply nested.")
    except SqlglotError as exc:
        return _reject(Code.UNANALYZABLE, f"Could not analyze query: {str(exc)[:120]}")


def _validate(sql: str, policy: SchemaPolicy) -> ValidationResult:
    if not sql or not sql.strip():
        return _reject(Code.EMPTY, "Query is empty.")
    if len(sql) > policy.max_sql_length:
        return _reject(Code.TOO_LONG, f"Query exceeds {policy.max_sql_length} characters.")

    # 1. Parse into a syntax tree. Exactly one statement.
    try:
        parsed = sqlglot.parse(sql, dialect=DIALECT)
    except (ParseError, TokenError) as exc:
        return _reject(Code.PARSE_ERROR, f"SQL could not be parsed: {str(exc)[:120]}")
    statements = [s for s in parsed if s is not None]
    if not statements:
        return _reject(Code.EMPTY, "Query is empty.")
    if len(statements) > 1:
        return _reject(Code.MULTIPLE_STATEMENTS, "Only a single statement is allowed.")

    root = statements[0]
    if isinstance(root, exp.Subquery):
        root = root.unnest()

    # 2. Must be a plain read: SELECT, or UNION/INTERSECT/EXCEPT of SELECTs.
    if not isinstance(root, exp.Select | exp.SetOperation):
        return _reject(Code.NOT_SELECT, f"Only SELECT queries are allowed, got {root.key.upper()}.")

    # sqlglot's parser is lenient: "SELECT ;" becomes a SELECT with no columns.
    if any(not select.expressions for select in root.find_all(exp.Select)):
        return _reject(Code.PARSE_ERROR, "SELECT has no columns.")

    root = normalize_identifiers(root, dialect=DIALECT)

    # 3. Nothing forbidden anywhere in the tree (catches writes hidden inside CTEs).
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return _reject(Code.FORBIDDEN_CONSTRUCT, f"{node.key.upper()} is not allowed.")
    for with_ in root.find_all(exp.With):
        if with_.args.get("recursive"):
            return _reject(Code.RECURSIVE_CTE, "Recursive CTEs are not allowed.")

    # 4. Functions: default-deny.
    for func in root.find_all(exp.Func):
        if isinstance(func, exp.Connector):
            continue  # AND / OR / XOR are operators; sqlglot also types them as functions
        if isinstance(func, exp.Anonymous):
            name = func.name.lower()
            if name not in ALLOWED_ANONYMOUS_FUNCTIONS:
                return _reject(Code.FUNCTION_NOT_ALLOWED, f"Function {name}() is not allowed.")
        elif type(func).__name__ not in ALLOWED_FUNCTION_CLASSES:
            return _reject(
                Code.FUNCTION_NOT_ALLOWED,
                f"Function {func.sql_name().lower()}() is not allowed.",
            )

    # 5. Tables: resolved through sqlglot scopes so a CTE cannot shadow a real table name.
    tables_used, error = _check_tables(root, policy)
    if error is not None:
        return error

    # 6. Restricted columns by name (clear error for the common PII case).
    for column in root.find_all(exp.Column):
        if column.name in policy.restricted_columns:
            return _reject(Code.RESTRICTED_COLUMN, f"Column '{column.name}' is restricted.")

    # 7. Column grounding and star expansion against the columns the role may read.
    schema: dict[str, object] = {
        policy.schema_name: {
            table: {col: "text" for col in cols} for table, cols in policy.tables.items()
        }
    }
    try:
        root = qualify(
            root,
            dialect=DIALECT,
            db=policy.schema_name,
            schema=schema,
            validate_qualify_columns=True,
            identify=False,
        )
    except SqlglotError as exc:
        message = str(exc)
        unknown = "could not be resolved" in message or message.startswith("Unknown column")
        code = Code.UNKNOWN_COLUMN if unknown else Code.UNANALYZABLE
        return _reject(code, message.split(" Line:")[0][:160])

    # 8. Whole-row references (SELECT c FROM customers c, c::text) would bypass column checks.
    row_ref = _whole_row_reference(root)
    if row_ref is not None:
        return _reject(
            Code.FORBIDDEN_CONSTRUCT,
            f"Whole-row reference '{row_ref}' is not allowed; name columns.",
        )

    # 8b. Readable output names instead of qualify()'s _col_N (cosmetic; after all checks).
    _name_outputs(root)

    # 9. Force a row cap on the outermost query.
    limit = _apply_limit(root, policy)

    return ValidationResult(
        ok=True,
        sql=root.sql(dialect=DIALECT, comments=False),
        tables=frozenset(tables_used),
        limit=limit,
    )


_GENERATED_ALIAS = re.compile(r"_col_\d+")


def _name_outputs(root: exp.Expression) -> None:
    """qualify() names unaliased outputs _col_0, _col_1, ... Give the outermost ones readable
    names (count, sum, column_3). Only aliases that qualify() invented are touched, and a new
    name never collides with a column or alias already used in the query, because Postgres
    resolves ORDER BY names against output names first."""
    node: Any = root
    for _ in range(20):  # peel set operations and parentheses down to the first SELECT
        if isinstance(node, exp.SetOperation):
            node = node.this
        elif isinstance(node, exp.Subquery):
            node = node.unnest()
        else:
            break
    if not isinstance(node, exp.Select):
        return

    taken = {c.name for c in root.find_all(exp.Column)}
    taken |= {a.alias for a in root.find_all(exp.Alias)}
    renames: dict[str, str] = {}
    for position, projection in enumerate(node.expressions, start=1):
        if not isinstance(projection, exp.Alias):
            continue
        if not _GENERATED_ALIAS.fullmatch(projection.alias):
            continue
        base = (
            projection.this.key
            if isinstance(projection.this, exp.AggFunc)
            else f"column_{position}"
        )
        name, suffix = base, 2
        while name in taken:
            name, suffix = f"{base}_{suffix}", suffix + 1
        taken.add(name)
        renames[projection.alias] = name
        projection.set("alias", exp.to_identifier(name))

    # If anything referred to a generated alias (for example in ORDER BY), follow the rename.
    if renames:
        for column in root.find_all(exp.Column):
            if not column.table and column.name in renames:
                column.set("this", exp.to_identifier(renames[column.name]))


def _check_tables(
    root: exp.Expression, policy: SchemaPolicy
) -> tuple[set[str], ValidationResult | None]:
    seen_nodes = 0
    used: set[str] = set()
    for scope in traverse_scope(root):
        for table in scope.tables:
            seen_nodes += 1
            source = scope.sources.get(table.alias_or_name)
            if not isinstance(source, exp.Table):
                continue  # reference to a CTE or derived table defined in this query
            if not isinstance(table.this, exp.Identifier):
                return used, _reject(
                    Code.FORBIDDEN_CONSTRUCT, "Table-valued functions are not allowed."
                )
            if table.catalog or (table.db and table.db != policy.schema_name):
                return used, _reject(
                    Code.SCHEMA_NOT_ALLOWED,
                    f"Only the '{policy.schema_name}' schema is allowed.",
                )
            if table.name not in policy.tables:
                return used, _reject(Code.UNKNOWN_TABLE, f"Table '{table.name}' does not exist.")
            used.add(table.name)
    # Fail closed: if sqlglot's scope walk missed any table node, we do not trust the analysis.
    if seen_nodes != len(list(root.find_all(exp.Table))):
        return used, _reject(Code.UNANALYZABLE, "Could not resolve every table reference.")
    return used, None


def _whole_row_reference(root: exp.Expression) -> str | None:
    """qualify() marks a bare table alias used as a value (a whole row) as a TableColumn."""
    for node in root.find_all(exp.TableColumn):
        return node.name or "row"
    return None


def _apply_limit(root: exp.Expression, policy: SchemaPolicy) -> int:
    existing = root.args.get("limit")
    wanted = policy.default_limit
    if isinstance(existing, exp.Limit):
        value = existing.expression
        if isinstance(value, exp.Literal) and value.is_int:
            wanted = min(int(value.this), policy.max_limit)
        else:  # LIMIT ALL or a computed expression
            wanted = policy.max_limit
    elif existing is not None:  # FETCH FIRST n ROWS
        wanted = policy.max_limit
    root.set("limit", exp.Limit(expression=exp.Literal.number(wanted)))
    return wanted
