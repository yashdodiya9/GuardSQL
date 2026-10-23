from datetime import date, datetime
from decimal import Decimal

from guardsql.evals.compare import result_key


def key(rows: list[tuple[object, ...]], cols: tuple[str, ...] = ("a",)):
    return result_key(cols, rows)


def test_row_order_ignored() -> None:
    assert key([(1,), (2,)]) == key([(2,), (1,)])


def test_column_names_ignored_but_count_matters() -> None:
    assert key([(1,)], ("x",)) == key([(1,)], ("y",))
    assert key([(1,)], ("a",)) != key([(1,)], ("a", "b"))


def test_numbers_compared_at_two_decimals() -> None:
    assert key([(Decimal("2.0049"),)]) == key([(2,)])
    assert key([(5,)]) == key([(Decimal("5.00"),)])
    assert key([(Decimal("16.7"),)]) != key([(Decimal("16.67"),)])


def test_midnight_timestamp_equals_date() -> None:
    assert key([(datetime(2024, 1, 1),)]) == key([(date(2024, 1, 1),)])
    assert key([(datetime(2024, 1, 1, 13, 0),)]) != key([(date(2024, 1, 1),)])


def test_nulls_and_bools_and_text() -> None:
    assert key([(None,)]) == key([(None,)])
    assert key([(True,)]) != key([(1,)])
    assert key([("a",)]) != key([("b",)])


def test_duplicates_matter() -> None:
    assert key([(1,), (1,)]) != key([(1,)])


def test_nan_does_not_crash() -> None:
    assert key([(float("nan"),)]) == key([(float("nan"),)])
