"""One error envelope for everything: {"error": {code, message, request_id, reason?}}.

Messages are written for the caller and never contain stack traces, SQL or driver text.
The detail (validator message, rejected SQL) goes to the audit log instead.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from guardsql.hallucination.pipeline import PipelineResult


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        reason: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.reason = reason
        self.headers = headers or {}


def error_response(request_id: str, error: ApiError) -> JSONResponse:
    body: dict[str, object] = {
        "code": error.code,
        "message": error.message,
        "request_id": request_id,
    }
    if error.reason:
        body["reason"] = error.reason
    return JSONResponse(
        status_code=error.status,
        content={"error": body},
        headers={**error.headers, "X-Request-ID": request_id},
    )


_USER_FIXABLE = {"QUESTION_EMPTY", "QUESTION_TOO_LONG", "CANNOT_ANSWER"}

_EXECUTE: dict[str, tuple[int, str, str]] = {
    "COST_TOO_HIGH": (
        422,
        "QUERY_TOO_EXPENSIVE",
        "That question needs too much work. Try narrowing it, for example with a date range.",
    ),
    "TIMEOUT": (504, "QUERY_TIMEOUT", "The query took too long and was stopped."),
    "UNAVAILABLE": (503, "DATABASE_UNAVAILABLE", "The database is unavailable. Try again shortly."),
    "DATABASE_ERROR": (502, "QUERY_FAILED", "The generated query failed. Try rephrasing."),
}


def to_api_error(result: PipelineResult) -> ApiError:
    """Map a failed pipeline result to an HTTP error. Unknown combinations become 500."""
    stage, code = result.stage, str(result.code)
    if stage == "generate":
        if code in _USER_FIXABLE:
            return ApiError(422, code, result.message or "The question cannot be answered.")
        if code == "LLM_ERROR":
            return ApiError(
                503,
                "MODEL_UNAVAILABLE",
                "The language model is unavailable. Try again shortly.",
                headers={"Retry-After": "5"},
            )
        if code == "NO_SQL":
            return ApiError(
                502, "MODEL_BAD_OUTPUT", "The model did not produce a usable query. Try rephrasing."
            )
    elif stage == "validate":
        return ApiError(
            422,
            "QUERY_BLOCKED",
            "The generated query was blocked by the safety policy.",
            reason=code,
        )
    elif stage == "execute" and code in _EXECUTE:
        status, public_code, message = _EXECUTE[code]
        return ApiError(status, public_code, message)
    return ApiError(500, "INTERNAL", "Something went wrong on our side.")
