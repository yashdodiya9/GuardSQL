"""Run the golden set and the adversarial prompts against the real stack.

    uv run python scripts/run_eval.py                  # everything, config from .env
    uv run python scripts/run_eval.py --samples 1      # ablation: no self-consistency
    uv run python scripts/run_eval.py --suite golden --limit 5
    uv run python scripts/run_eval.py --delay 4        # free-tier rate limits

Writes <out>.json (raw per-case results) and <out>.md (the report) under evals/results/.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from guardsql.config import get_settings
from guardsql.evals.adversarial import PROMPT_ATTACKS
from guardsql.evals.golden import GOLDEN
from guardsql.evals.metrics import (
    render_attacks,
    render_golden,
    summarize_attacks,
    summarize_golden,
)
from guardsql.evals.results import AttackResult, CaseResult
from guardsql.evals.runner import preflight, run_attacks, run_golden
from guardsql.execution.executor import ReadOnlyExecutor
from guardsql.hallucination.judge import SQLJudge
from guardsql.hallucination.pipeline import AnswerPipeline, Asker, PipelineConfig
from guardsql.llm.factory import build_client, with_model
from guardsql.llm.generator import SQLGenerator
from guardsql.routing import EscalatingPipeline
from guardsql.schema.introspect import introspect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["golden", "attacks", "all"], default="all")
    parser.add_argument("--samples", type=int, default=0, help="override CONSISTENCY_SAMPLES")
    parser.add_argument("--judge", action="store_true", help="enable the LLM judge")
    parser.add_argument("--limit", type=int, default=0, help="only the first N cases per suite")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between questions")
    parser.add_argument("--out", default="", help="output path without extension")
    parser.add_argument("--strong-model", default="", help="enable routing to this model")
    args = parser.parse_args()

    settings = get_settings()
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
    judge = SQLJudge(client, schema_prompt) if (args.judge or settings.judge_enabled) else None
    base = PipelineConfig.from_settings(settings)
    config = dataclasses.replace(base, samples=args.samples or base.samples)

    golden = list(GOLDEN)[: args.limit or None]
    attacks = list(PROMPT_ATTACKS)[: args.limit or None]
    report_config = {
        "provider": client.provider,
        "model": client.model,
        "samples": config.samples,
        "max_repairs": config.max_repairs,
        "judge": judge is not None,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "strong_model": args.strong_model or settings.strong_model or None,
    }

    golden_results: list[CaseResult] = []
    attack_results: list[AttackResult] = []
    sections: list[str] = [
        "# GuardSQL eval report",
        "",
        "Rows from the golden set are scored by comparing execution results with the "
        "reference SQL.",
        "",
    ]
    with ReadOnlyExecutor(settings) as executor:
        primary = AnswerPipeline(generator, executor, policy, config=config, judge=judge)
        pipeline: Asker = primary
        strong_name = args.strong_model or settings.strong_model
        if strong_name:
            strong_generator = SQLGenerator(
                build_client(with_model(settings, strong_name)),
                schema_prompt,
                max_question_chars=settings.max_question_chars,
                max_output_tokens=settings.llm_max_output_tokens,
                temperature=settings.llm_temperature,
            )
            strong = AnswerPipeline(
                strong_generator, executor, policy, config=dataclasses.replace(config, samples=1)
            )
            pipeline = EscalatingPipeline(primary, strong)

        if args.suite in ("golden", "all"):
            keys = preflight(golden, policy, executor)
            total = len(golden)

            done = [0]

            def show(r: CaseResult) -> None:
                done[0] += 1
                print(
                    f"[{done[0]}/{total}] {r.case_id} {r.outcome} "
                    f"conf={r.confidence} {r.latency_ms / 1000:.1f}s",
                    file=sys.stderr,
                )

            golden_results = run_golden(pipeline, golden, keys, delay_s=args.delay, on_result=show)
            sections += [render_golden(summarize_golden(golden_results), report_config), ""]
        if args.suite in ("attacks", "all"):
            attack_results = run_attacks(
                pipeline, attacks, settings.restricted_column_set, delay_s=args.delay
            )
            sections += [render_attacks(summarize_attacks(attack_results)), ""]

    stem = args.out or (
        "evals/results/"
        + datetime.now().strftime("%Y%m%d-%H%M")
        + "-"
        + re.sub(r"[^A-Za-z0-9._-]+", "-", f"{client.provider}-{client.model}-s{config.samples}")
    )
    md_path, json_path = Path(f"{stem}.md"), Path(f"{stem}.json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    report = "\n".join(sections)
    md_path.write_text(report + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "config": report_config,
                "golden": [dataclasses.asdict(r) for r in golden_results],
                "attacks": [dataclasses.asdict(r) for r in attack_results],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(report)
    print(f"\nwrote {md_path} and {json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
