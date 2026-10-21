"""Question in, candidate SQL out. The result is a CANDIDATE: it is untrusted until
validate_sql() has accepted it, and nothing here touches the database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from guardsql.llm.errors import LLMError, LLMErrorKind
from guardsql.llm.extract import extract_sql
from guardsql.llm.prompts import build_system_prompt, build_user_prompt, sanitize_question
from guardsql.llm.types import LLMClient, LLMRequest, LLMResponse


class GenCode(StrEnum):
    QUESTION_EMPTY = "QUESTION_EMPTY"
    QUESTION_TOO_LONG = "QUESTION_TOO_LONG"
    LLM_ERROR = "LLM_ERROR"
    NO_SQL = "NO_SQL"  # the model answered, but not with SQL
    CANNOT_ANSWER = "CANNOT_ANSWER"  # the model said the schema cannot answer it


@dataclass(frozen=True)
class GenerationResult:
    ok: bool
    sql: str | None = None
    code: GenCode | None = None
    message: str | None = None
    # What the model actually said, kept for the audit log and for debugging.
    raw_text: str | None = None
    # Tokens, latency, provider and model. None if no model call succeeded.
    response: LLMResponse | None = None
    llm_error: LLMErrorKind | None = None


class SQLGenerator:
    def __init__(
        self,
        client: LLMClient,
        schema_prompt: str,
        *,
        max_question_chars: int = 500,
        max_output_tokens: int = 512,
        temperature: float = 0.0,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._client = client
        # Built once: identical bytes on every request, which provider prompt caches need.
        self._system = build_system_prompt(schema_prompt)
        self._max_question_chars = max_question_chars
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._today = today

    def generate(
        self,
        question: str,
        *,
        temperature: float | None = None,
        feedback: str | None = None,
    ) -> GenerationResult:
        """Never raises for expected failures; they come back as a result."""
        cleaned = sanitize_question(question)
        if not cleaned:
            return GenerationResult(
                ok=False, code=GenCode.QUESTION_EMPTY, message="Please ask a question."
            )
        if len(cleaned) > self._max_question_chars:
            return GenerationResult(
                ok=False,
                code=GenCode.QUESTION_TOO_LONG,
                message=f"Questions are limited to {self._max_question_chars} characters.",
            )

        user = build_user_prompt(cleaned, self._today())
        if feedback:
            user += f"\n\n{feedback}"
        request = LLMRequest(
            system=self._system,
            user=user,
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature if temperature is None else temperature,
        )
        try:
            response = self._client.generate(request)
        except LLMError as exc:
            return GenerationResult(
                ok=False, code=GenCode.LLM_ERROR, message=exc.message, llm_error=exc.kind
            )

        extracted = extract_sql(response.text)
        if extracted.sql is not None:
            return GenerationResult(
                ok=True, sql=extracted.sql, raw_text=response.text, response=response
            )
        if extracted.refusal is not None:
            return GenerationResult(
                ok=False,
                code=GenCode.CANNOT_ANSWER,
                message=extracted.refusal,
                raw_text=response.text,
                response=response,
            )
        return GenerationResult(
            ok=False,
            code=GenCode.NO_SQL,
            message="The model did not return a SQL query.",
            raw_text=response.text,
            response=response,
        )
