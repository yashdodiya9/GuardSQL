from guardsql.execution.executor import ExecCode, ExecutionResult
from guardsql.hallucination.consistency import fingerprint


def res(rows: tuple[tuple[object, ...], ...], cols: tuple[str, ...] = ("a",)) -> ExecutionResult:
    return ExecutionResult(ok=True, columns=cols, rows=rows)


def test_row_order_ignored() -> None:
    assert fingerprint(res(((1,), (2,)))) == fingerprint(res(((2,), (1,))))


def test_column_names_ignored() -> None:
    assert fingerprint(res(((1,),), ("x",))) == fingerprint(res(((1,),), ("y",)))


def test_different_values_differ() -> None:
    assert fingerprint(res(((1,),))) != fingerprint(res(((2,),)))


def test_column_count_matters() -> None:
    assert fingerprint(res(((1,),), ("a",))) != fingerprint(res(((1,),), ("a", "b")))


def test_duplicates_matter() -> None:
    assert fingerprint(res(((1,), (1,)))) != fingerprint(res(((1,),)))


def test_failed_result_has_no_fingerprint() -> None:
    assert fingerprint(ExecutionResult(ok=False, code=ExecCode.TIMEOUT)) is None
