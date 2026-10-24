"""Builds the whole stack once at startup and exposes the few things the API needs."""

from __future__ import annotations

import dataclasses
from typing import Protocol

import psycopg

from guardsql.cache import CachingPipeline, cache_fingerprint
from guardsql.config import Settings
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.guardrails.policy import SchemaPolicy
from guardsql.hallucination.judge import SQLJudge
from guardsql.hallucination.pipeline import AnswerPipeline, Asker, PipelineConfig, PipelineResult
from guardsql.llm.factory import build_client, with_model
from guardsql.llm.generator import SQLGenerator
from guardsql.llm.prompts import SYSTEM_TEMPLATE
from guardsql.routing import EscalatingPipeline
from guardsql.schema.introspect import introspect


class Answerer(Protocol):
    """What the API depends on. Tests plug a stub in here."""

    def ask(self, question: str) -> PipelineResult: ...

    def ping(self) -> bool: ...

    def schema(self) -> dict[str, list[str]]: ...

    def close(self) -> None: ...


class GuardSQLService:
    def __init__(self, pipeline: Asker, executor: ReadOnlyExecutor, policy: SchemaPolicy) -> None:
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
        config = PipelineConfig.from_settings(settings)
        primary = AnswerPipeline(generator, executor, policy, config=config, judge=judge)
        asker: Asker = primary
        if settings.strong_model:
            strong_generator = SQLGenerator(
                build_client(with_model(settings, settings.strong_model)),
                schema_prompt,
                max_question_chars=settings.max_question_chars,
                max_output_tokens=settings.llm_max_output_tokens,
                temperature=settings.llm_temperature,
            )
            strong = AnswerPipeline(
                strong_generator, executor, policy, config=dataclasses.replace(config, samples=1)
            )
            asker = EscalatingPipeline(primary, strong)

        if settings.cache_max_entries > 0:
            asker = CachingPipeline(
                asker,
                executor,
                policy,
                fingerprint=cache_fingerprint(
                    SYSTEM_TEMPLATE,
                    schema_prompt,
                    settings.llm_provider,
                    settings.ollama_model,
                    settings.gemini_model,
                    settings.strong_model,
                    str(settings.consistency_samples),
                ),
                max_entries=settings.cache_max_entries,
                ttl_seconds=settings.cache_ttl_seconds,
            )
        return cls(asker, executor, policy)

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
