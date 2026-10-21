from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A hard parse cap against huge bodies. The finer limit (MAX_QUESTION_CHARS) is the
    # generator's, and it is applied after whitespace is cleaned.
    question: str = Field(min_length=1, max_length=2000)


class ConfidenceInfo(BaseModel):
    level: str
    score: float
    samples_agreeing: int
    samples_requested: int
    repairs: int
    judge: str | None


class UsageInfo(BaseModel):
    llm_calls: int
    input_tokens: int
    output_tokens: int
    usage_complete: bool


class AskResponse(BaseModel):
    request_id: str
    sql: str
    columns: list[str]
    # Decimals, dates and UUIDs serialise as strings; nothing is silently rounded.
    rows: list[list[Any]]
    row_count: int
    row_limit_hit: bool
    query_ms: float
    latency_ms: float
    confidence: ConfidenceInfo
    usage: UsageInfo


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class SchemaResponse(BaseModel):
    tables: dict[str, list[str]]
