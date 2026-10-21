"""Full M6 path: generate, validate (repair), execute, sample, score.

uv run python scripts/try_pipeline.py "How many orders are there per status?"
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from guardsql.config import get_settings
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.hallucination.judge import SQLJudge
from guardsql.hallucination.pipeline import AnswerPipeline, PipelineConfig
from guardsql.llm.factory import build_client
from guardsql.llm.generator import SQLGenerator
from guardsql.schema.introspect import introspect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    args = parser.parse_args()

    settings = get_settings()
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        snapshot = introspect(conn, settings.postgres_schema)
    policy = snapshot.to_policy(settings.restricted_column_set, **settings.policy_limits)

    client = build_client(settings)
    generator = SQLGenerator(
        client,
        snapshot.to_prompt(),
        max_question_chars=settings.max_question_chars,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
    )
    judge = SQLJudge(client, snapshot.to_prompt()) if settings.judge_enabled else None
    with ReadOnlyExecutor(settings) as executor:
        pipeline = AnswerPipeline(
            generator,
            executor,
            policy,
            config=PipelineConfig.from_settings(settings),
            judge=judge,
        )
        out = pipeline.ask(args.question)

    if not out.ok:
        print(f"[{out.stage}/{out.code}] {out.message}", file=sys.stderr)
        return 1
    assert out.execution is not None and out.signals is not None
    print(f"confidence: {out.confidence} ({out.score})")
    print(
        f"signals:    agree {out.signals.samples_agreeing}/{out.signals.samples_requested}, "
        f"repairs {out.signals.repairs}, judge {out.signals.judge}"
    )
    print(f"cost:       {out.llm_calls} LLM calls, {out.input_tokens} in / {out.output_tokens} out")
    print(f"\n{out.sql}\n")
    print(" | ".join(out.execution.columns))
    for row in out.execution.rows[:10]:
        print(" | ".join(str(v) for v in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
