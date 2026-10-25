from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from guardsql import __version__
from guardsql.api.errors import ApiError, error_response, to_api_error
from guardsql.api.schemas import (
    AskRequest,
    AskResponse,
    ConfidenceInfo,
    ErrorResponse,
    SchemaResponse,
    UsageInfo,
)
from guardsql.api.security import ClientAuth, RateLimiter, extract_key
from guardsql.audit import AuditEvent, AuditLog, JsonlAuditLog
from guardsql.config import get_settings
from guardsql.hallucination.pipeline import PipelineResult
from guardsql.service import Answerer, GuardSQLService

log = logging.getLogger("guardsql.api")

# Accept a caller's id only if it is boring: it ends up in logs and headers.
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")

router = APIRouter()

_ERRORS: dict[int | str, dict[str, object]] = {
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def authenticate(request: Request) -> str:
    """Identify the caller or raise 401. Runs before the body is parsed."""
    auth: ClientAuth = request.app.state.auth
    if not auth.enabled:
        return "anonymous"
    client = auth.identify(extract_key(request))
    if client is None:
        log.warning("auth failed request_id=%s", _request_id(request))
        raise ApiError(
            401,
            "UNAUTHORIZED",
            "A valid API key is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return client


Client = Annotated[str, Depends(authenticate)]


def _record(
    request: Request,
    question: str,
    *,
    status: int,
    started: float,
    result: PipelineResult | None = None,
    code: str | None = None,
    client: str | None = None,
) -> None:
    latency_ms = (time.perf_counter() - started) * 1000
    request.app.state.audit.write(
        AuditEvent.build(
            request_id=_request_id(request),
            question=question,
            http_status=status,
            latency_ms=latency_ms,
            result=result,
            code=code,
            client=client,
        )
    )
    log.info("ask request_id=%s status=%s ms=%.0f", _request_id(request), status, latency_ms)


def _to_response(result: PipelineResult, request_id: str, latency_ms: float) -> AskResponse:
    execution, signals = result.execution, result.signals
    if execution is None or signals is None or result.sql is None or result.confidence is None:
        raise RuntimeError("pipeline reported success without a complete result")
    return AskResponse(
        request_id=request_id,
        sql=result.sql,
        columns=list(execution.columns),
        rows=[list(row) for row in execution.rows],
        row_count=len(execution.rows),
        row_limit_hit=execution.row_limit_hit,
        query_ms=round(execution.elapsed_ms, 1),
        latency_ms=round(latency_ms, 1),
        confidence=ConfidenceInfo(
            level=str(result.confidence),
            score=result.score or 0.0,
            samples_agreeing=signals.samples_agreeing,
            samples_requested=signals.samples_requested,
            repairs=signals.repairs,
            judge=str(signals.judge) if signals.judge else None,
        ),
        usage=UsageInfo(
            llm_calls=result.llm_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            usage_complete=result.usage_complete,
        ),
    )


@router.post("/v1/ask", response_model=AskResponse, responses=_ERRORS, tags=["ask"])
def ask(body: AskRequest, request: Request, client: Client) -> AskResponse:
    started = time.perf_counter()
    auth: ClientAuth = request.app.state.auth
    limiter: RateLimiter = request.app.state.limiter
    wait = limiter.check(client) if auth.enabled else None  # nobody to limit when auth is off
    if wait is not None:
        _record(
            request, body.question, status=429, started=started, code="RATE_LIMITED", client=client
        )
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Too many requests from this client. Slow down and retry.",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
    slots: BoundedSemaphore = request.app.state.slots
    if not slots.acquire(blocking=False):
        _record(request, body.question, status=429, started=started, code="BUSY", client=client)
        raise ApiError(
            429,
            "BUSY",
            "Too many questions are being answered right now. Retry shortly.",
            headers={"Retry-After": "2"},
        )
    try:
        result: PipelineResult = request.app.state.service.ask(body.question)
    except Exception:
        _record(request, body.question, status=500, started=started, code="INTERNAL", client=client)
        raise
    finally:
        slots.release()

    if not result.ok:
        error = to_api_error(result)
        _record(
            request,
            body.question,
            status=error.status,
            started=started,
            result=result,
            client=client,
        )
        raise error
    latency_ms = (time.perf_counter() - started) * 1000
    response = _to_response(result, _request_id(request), latency_ms)
    _record(request, body.question, status=200, started=started, result=result, client=client)
    return response


@router.get(
    "/v1/schema", response_model=SchemaResponse, tags=["meta"], dependencies=[Depends(authenticate)]
)
def schema(request: Request) -> SchemaResponse:
    """The tables and columns that questions can use (restricted columns are excluded)."""
    return SchemaResponse(tables=request.app.state.service.schema())


@router.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database or the model."""
    return {"status": "ok", "version": __version__}


@router.get("/ready", tags=["meta"])
def ready(request: Request) -> dict[str, str]:
    """Readiness: the database answers. Orchestrators use this to route traffic."""
    if not request.app.state.service.ping():
        raise ApiError(503, "NOT_READY", "The database is not reachable.")
    return {"status": "ready"}


def create_app(
    service: Answerer | None = None,
    audit: AuditLog | None = None,
    max_concurrent_asks: int | None = None,
    auth: ClientAuth | None = None,
    limiter: RateLimiter | None = None,
) -> FastAPI:
    """Nothing here touches the database or reads settings until startup, so importing
    this module (and the /health test) needs no environment."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = service is None
        app.state.service = (
            service if service is not None else GuardSQLService.build(get_settings())
        )
        app.state.audit = (
            audit if audit is not None else JsonlAuditLog(Path(get_settings().audit_log_path))
        )
        limit = (
            max_concurrent_asks
            if max_concurrent_asks is not None
            else get_settings().max_concurrent_asks
        )
        app.state.slots = BoundedSemaphore(limit)
        app.state.auth = auth if auth is not None else ClientAuth.from_settings(get_settings())
        app.state.limiter = (
            limiter if limiter is not None else RateLimiter.from_settings(get_settings())
        )
        if not app.state.auth.enabled:
            log.warning("authentication is DISABLED (no API_KEYS); local development only")
        try:
            yield
        finally:
            if owned:
                app.state.service.close()

    app = FastAPI(
        title="GuardSQL",
        version=__version__,
        description="Text-to-SQL with guardrails and hallucination detection.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> Response:
        return error_response(_request_id(request), exc)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid(request: Request, exc: RequestValidationError) -> Response:
        # Deliberately does not echo the input back.
        return error_response(
            _request_id(request),
            ApiError(422, "INVALID_REQUEST", 'Send JSON like {"question": "..."}.'),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> Response:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        return error_response(
            _request_id(request), ApiError(exc.status_code, code, "Request could not be served.")
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        log.error(
            "unhandled error request_id=%s",
            _request_id(request),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return error_response(
            _request_id(request), ApiError(500, "INTERNAL", "Something went wrong on our side.")
        )

    app.include_router(router)
    return app
