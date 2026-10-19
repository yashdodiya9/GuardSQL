from guardsql.config import Settings
from guardsql.execution.executor import ExecCode, ReadOnlyExecutor
from guardsql.guardrails.ast_validator import Code, ValidationResult


def test_unvalidated_sql_is_never_sent() -> None:
    """The executor is never opened here, so any attempt to reach the database would fail
    with UNAVAILABLE. Getting NOT_VALIDATED proves the check happens first."""
    executor = ReadOnlyExecutor(Settings(_env_file=None))
    rejected = ValidationResult(ok=False, code=Code.NOT_SELECT, message="nope")
    assert executor.execute(rejected).code == ExecCode.NOT_VALIDATED
    missing_sql = ValidationResult(ok=True, sql=None)
    assert executor.execute(missing_sql).code == ExecCode.NOT_VALIDATED
