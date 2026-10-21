"""Builds the whole stack once at startup and exposes the few things the API needs."""

from __future__ import annotations

from typing import Protocol

import psycopg

from guardsql.config import Settings
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.hallucination.judge import SQLJudge
from guardsql.hallucination.pipeline import AnswerPipeline, PipelineConfig, PipelineResult
from guardsql.llm.factory import build_client
from guardsql.llm.generator import SQLGenerator
from guardsql.schema.introspect import introspect


class Answerer(Protocol):
    """What the API depends on. Tests plug a stub in here."""

    def ask(self, question: str) -> PipelineResult: ...

    def ping(self) -> bool: ...

    def schema(self) -> dict[str, list[str]]: ...

    def close(self) -> None: ...


class GuardSQLService:
    def __init__(
        self, pipeline: AnswerPipeline, executor: ReadOnlyExecutor, policy: SchemaPolicy
    ) -> None:
        self._pipeline = pipeline
        self._executor = executor
        self._policy = policy

    @classmethod
    def build(cls, settings: Settings) -> GuardSQLService:
        with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
            snapshot = introspect(conn, settings.postgres_schema)
        policy = snapshot.to_policy(settings.restricted_column_set, **settings.policy_limits)
        client = build_client(settings)
        schema_prompt = snapshot.to_prompt()
        generator = SQLGenerator(
            client,
            schema_prompt,
            max_question_chars=settings.max_question_chars,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
        )
        judge = SQLJudge(client, schema_prompt) if settings.judge_enabled else None
        executor = ReadOnlyExecutor(settings)
        executor.open()
        pipeline = AnswerPipeline(
            generator,
            executor,
            policy,
            config=PipelineConfig.from_settings(settings),
            judge=judge,
        )
        return cls(pipeline, executor, policy)

    def ask(self, question: str) -> PipelineResult:
        return self._pipeline.ask(question)

    def ping(self) -> bool:
        return self._executor.ping()

    def schema(self) -> dict[str, list[str]]:
        restricted = self._policy.restricted_columns
        return {
            table: [c for c in columns if c not in restricted]
            for table, columns in self._policy.tables.items()
        }

    def close(self) -> None:
        self._executor.close()
