"""Decide whether two result sets are 'the same answer'.

Deliberately lenient about presentation (row order, column names, trailing decimals,
midnight timestamps vs dates) and strict about content and column count.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

from guardsql.execution.executor import ExecutionResult

ResultKey = tuple[int, tuple[tuple[str, ...], ...]]
_TWO_PLACES = Decimal("0.01")


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float | Decimal):
        try:
            return format(Decimal(str(value)).quantize(_TWO_PLACES), "f")
        except InvalidOperation:
            return str(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def result_key(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ResultKey:
    return (len(columns), tuple(sorted(tuple(_cell(v) for v in row) for row in rows)))


def key_of(result: ExecutionResult) -> ResultKey:
    return result_key(result.columns, result.rows)
