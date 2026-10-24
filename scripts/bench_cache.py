"""Benchmark the query cache against the live stack: cold vs warm vs reformatted repeats."""

from __future__ import annotations

import statistics
import time

from guardsql.config import Settings
from guardsql.service import GuardSQLService

QUESTIONS = [
    "How many customers are there?",
    "How many products are there?",
    "How many orders are there per status?",
    "What are the 5 most expensive products?",
    "What is the total revenue from all orders?",
    "How many orders did each customer place?",
]


def reformat(question: str) -> str:
    return "  " + question.upper().rstrip("?") + "  ??"


def run(service: GuardSQLService, questions: list[str]):
    results, latencies = [], []
    for question in questions:
        start = time.perf_counter()
        results.append(service.ask(question))
        latencies.append((time.perf_counter() - start) * 1000)
    return results, latencies


def row_sets(results):
    return [r.execution.rows if r.ok and r.execution else None for r in results]


def report(name, results, latencies, baseline_rows) -> None:
    hits = sum(r.cache_hit for r in results)
    calls = sum(r.llm_calls for r in results)
    tokens = sum(r.input_tokens + r.output_tokens for r in results)
    same = sum(a == b for a, b in zip(row_sets(results), baseline_rows, strict=True))
    print(
        f"{name:<14} p50={statistics.median(latencies):8.0f} ms  "
        f"hits={hits}/{len(results)}  llm_calls={calls}  tokens={tokens}  "
        f"same_rows_as_cold={same}/{len(results)}"
    )


def main() -> None:
    service = GuardSQLService.build(Settings())
    try:
        cold, cold_ms = run(service, QUESTIONS)
        baseline = row_sets(cold)
        warm, warm_ms = run(service, QUESTIONS)
        variant, variant_ms = run(service, [reformat(q) for q in QUESTIONS])
        print("cold failures:", [q for q, r in zip(QUESTIONS, cold, strict=True) if not r.ok])
        report("cold", cold, cold_ms, baseline)
        report("warm (same)", warm, warm_ms, baseline)
        report("warm (reformat)", variant, variant_ms, baseline)
    finally:
        service.close()


if __name__ == "__main__":
    main()
