"""Ask the configured model to write SQL for a question. Nothing is executed.

    uv run python scripts/try_llm.py "How many orders are there per status?"

To run the answer through the M3/M4 guardrails, print only the SQL and pass it on:

    SQL=$(uv run python scripts/try_llm.py --sql-only "orders per status")
    uv run python scripts/try_sql.py "$SQL"
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from guardsql.config import get_settings
from guardsql.llm.factory import build_client
from guardsql.llm.generator import SQLGenerator
from guardsql.schema.introspect import introspect


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("question")
    parser.add_argument("--sql-only", action="store_true", help="print only the SQL")
    args = parser.parse_args()

    settings = get_settings()
    with psycopg.connect(settings.readonly_conninfo, autocommit=True) as conn:
        snapshot = introspect(conn, settings.postgres_schema)

    client = build_client(settings)
    generator = SQLGenerator(
        client,
        snapshot.to_prompt(),
        max_question_chars=settings.max_question_chars,
        max_output_tokens=settings.llm_max_output_tokens,
        temperature=settings.llm_temperature,
    )
    result = generator.generate(args.question)

    if not result.ok:
        print(f"[{result.code}] {result.message}", file=sys.stderr)
        return 1
    if args.sql_only:
        print(result.sql)
        return 0

    response = result.response
    assert response is not None
    print(f"model:   {response.provider}/{response.model}")
    print(f"tokens:  {response.input_tokens} in, {response.output_tokens} out")
    print(f"latency: {response.latency_ms:.0f} ms (attempts: {response.attempts})")
    print(f"\n{result.sql}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
