from __future__ import annotations

import hashlib

from guardsql.execution.executor import ExecutionResult


def fingerprint(result: ExecutionResult) -> str | None:
    """Order-insensitive hash of a result set. Column names are ignored (aliases differ
    between samples) but the column count is not. None for a failed execution."""
    if not result.ok:
        return None
    digest = hashlib.sha256()
    digest.update(str(len(result.columns)).encode())
    for row in sorted(repr(r) for r in result.rows):
        digest.update(row.encode())
        digest.update(b"\n")
    return digest.hexdigest()
