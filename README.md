# GuardSQL

Natural-language-to-SQL service that treats the LLM as an untrusted component:
deterministic SQL guardrails, hallucination detection, and measured cost/accuracy.

> Status: under construction. See [DECISIONS.md](DECISIONS.md) for design rationale.

## Quickstart

```bash
uv sync                          # install dependencies
cp .env.example .env
docker compose up -d --wait      # start Postgres
uv run pytest                    # run all tests
uv run uvicorn guardsql.main:app --reload
```