# GuardSQL

A natural-language-to-SQL service that treats the language model as an **untrusted component**.
The model proposes SQL; deterministic code decides whether it may run. Every query passes an AST
validator, a cost gate and a read-only database role before any data is returned, and every answer
carries a confidence level.

**[Live demo](https://guardsql.dodiyayash28.workers.dev)** · API: `https://guardsql.onrender.com` · [Design decisions](DECISIONS.md) · [Eval reports](evals/results)

## Try it

1. Open **https://guardsql.dodiyayash28.workers.dev**
2. Paste this public demo key into the key box:

   ```text
	gsql_fC0IenhHsAx-UrNkAfUONvqKpHT-udKXo-wUa432OxE
   ```

3. Click an example question, or ask your own, such as *"Top 5 customers by total order value"*.

Things worth trying: ask for `salary` or `email` (refused), ask it to delete rows (refused), or ask
something the data cannot answer (it says so instead of guessing).

The key is limited to 5 requests a minute and 40 questions a day shared across all visitors, because the
model behind it is a free tier. The API runs on a free host that sleeps when idle, so **the first request
can take up to about a minute**; the page shows a "waking up" message while it retries.
The data is synthetic (a small online shop) and contains nothing real.

## What problem it solves

A model that writes SQL can be wrong, can be talked into doing something it should not
(prompt injection), and can invent columns that do not exist. GuardSQL does not try to make the model
trustworthy. It limits what a mistake can cost:

| Risk | Control | Where |
|---|---|---|
| Writes, DDL, multiple statements | Parse with sqlglot; only a single `SELECT` is accepted | `guardrails/ast_validator.py` |
| Reading restricted data (`email`, `phone`, `date_of_birth`, `salary`) | Blocked by the validator **and** by column-level grants on the database role | validator, `scripts/db/init/03_roles.sh` |
| Dangerous functions | Default-deny allow-list | `guardrails/policy.py` |
| Runaway queries | `EXPLAIN` cost gate, statement timeout, row cap | `guardrails/cost_gate.py`, `execution/executor.py` |
| Hallucinated tables or columns | Validated against the live schema, then a bounded repair loop | `hallucination/pipeline.py` |
| Wrong-but-valid answers | Confidence from repairs, sample agreement and an optional judge | `hallucination/confidence.py` |
| Abuse of the public endpoint | API keys (hashed), rate limit, concurrency cap, daily caps | `api/security.py` |

The database role is the last line of defence: even if every check above had a bug, the connection
physically cannot read the restricted columns or write ([ADR-002](DECISIONS.md)).

## Architecture: what happens to one question

The system has four parts. Only the API talks to the model and the database; the browser never does.

```text
┌───────────────────────────┐   HTTPS  POST /v1/ask       ┌───────────────────────────────┐
│ Web UI                    │ ──────────────────────────▶ │ API                           │
│ React + Vite + TypeScript │                             │ FastAPI + Docker on Render    │
│ Cloudflare static assets  │ ◀────────────────────────── │ guardrails, cache, audit log  │
└───────────────────────────┘   JSON response             └───────┬───────────────┬───────┘
                                                                  │               │
                                                       generate   │               │  SELECT as
                                                       SQL        │               │  read-only role
                                                                  ▼               ▼
                                                         ┌────────────────┐ ┌────────────────┐
                                                         │ Gemini API     │ │ Neon Postgres  │
                                                         │ free tier      │ │ synthetic data │
                                                         └────────────────┘ └────────────────┘
```

Inside the API, one question passes through these steps in order. Anything on the right is the error the
caller gets if that step refuses; nothing after a refusal runs.

```text
   POST /v1/ask  (question + X-API-Key)
          │
          ▼
┌────────────────────────────────────────────────────────────┐
│ 1  Edge checks                                             │   ──▶ 401 / 429
│    CORS, API key (hashed), rate and daily limits           │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 2  Cache                                                   │
│    validated SQL for this question; hit skips to 5         │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 3  Generate                                                │   ──▶ CANNOT_ANSWER
│    prompt = rules + live schema, sent to the model         │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 4  Validate                                                │   ──▶ QUERY_BLOCKED
│    sqlglot: one allowed SELECT; up to 2 repairs            │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 5  Cost gate                                               │   ──▶ QUERY_TOO_EXPENSIVE
│    EXPLAIN estimate before anything runs                   │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 6  Execute                                                 │   ──▶ QUERY_TIMEOUT
│    read-only role, timeout, row cap                        │
└────────────────────────────┬───────────────────────────────┘
                             │
                             ▼
┌────────────────────────────────────────────────────────────┐
│ 7  Respond                                                 │
│    confidence level, usage, audit log entry                │
└────────────────────────────────────────────────────────────┘
                             │
                             ▼
   JSON response  (sql, rows, confidence, usage)
```

Notes on the steps:

1. **Edge checks.** CORS allows only the UI's exact origin. The key is hashed and compared with stored
   hashes, so the server never holds a plain key. Rejections are fast and carry `Retry-After`.
2. **Cache.** Only SQL that already passed validation is cached, per normalised question, never rows
   ([ADR-024](DECISIONS.md)). A hit still goes through the cost gate and the read-only executor.
3. **Generate.** A static prefix (rules, schema) plus the question, so the prompt is stable and cacheable
   ([ADR-012](DECISIONS.md)). The model returns SQL or `CANNOT_ANSWER`.
4. **Validate.** The service executes the regenerated SQL, never the model's original string
   ([ADR-004](DECISIONS.md)). Honest mistakes such as an unknown column go back to the model for up to two
   repairs; policy violations are not repaired ([ADR-013](DECISIONS.md)).
5. **Cost gate.** `EXPLAIN` estimates the cost before anything runs.
6. **Execute.** A pooled connection as a read-only role, so even a validator bug cannot read restricted
   columns or write ([ADR-002](DECISIONS.md)).
7. **Respond.** Confidence is advisory. Every error uses one envelope,
   `{"error": {code, message, request_id}}`, with no stack traces or SQL. Detail goes to the audit log.

On the browser side, the UI keeps the key in memory and pings `/health` with retries before asking, which
covers the free host waking from sleep.

### How it is delivered

```text
┌────────────┐  git push  ┌────────────┐ ──▶ GitHub Actions: quality, integration, docker
│ Laptop     │ ─────────▶ │ GitHub     │ ──▶ Render: builds the Dockerfile, deploys the API
│ uv, Docker │            │ main       │ ──▶ Cloudflare: builds web/, serves the UI
└─────┬──────┘            └────────────┘
      │ scripts/db/provision.sh (admin credentials stay on the laptop)
      ▼
┌────────────────┐
│ Neon Postgres  │ ◀── Render connects with the read-only role only
└────────────────┘
```

The API container never receives the database admin password. Provisioning (schema, seed data, the
read-only role) runs from a laptop, and the container is given only the read-only role
([ADR-027](DECISIONS.md), [ADR-028](DECISIONS.md)).

## Results

Measured by **execution accuracy**: the generated SQL is run, and its result is compared with the result of
a reference query. A string match would be too strict and a plausible-looking query too lenient. The golden
set is 42 questions (15 easy, 17 medium, 10 hard) plus 22 adversarial prompts.

| | Gemini 3.5 Flash-Lite (deployed) | Qwen2.5-Coder 7B (local, Ollama) |
|---|---|---|
| Execution accuracy | **40/42 = 95.2%** (95% CI 84.2-98.7%) | 38/42 = 90.5% (95% CI 77.9-96.2%) |
| Latency p50 | 0.8 s | 1.8 s |
| Tokens per question (in / out) | 496 / 45 | about 480 / 32 |
| Adversarial leaks | **0 of 22** | 0 of 22 |
| Needed a repair | 0 of 42 | not reported in this table |

How to read these numbers honestly:

- The two confidence intervals **overlap**, so the data does not prove Gemini is better than the local model.
- The golden set was used while fixing a prompt problem, so these accuracies are an **upper bound**. A
  held-out set has not been built yet.
- With 42 questions, one question moves accuracy by about 2.4 points. Run-to-run noise is one or two
  questions.
- Confidence is not informative at one sample: every Gemini answer scored MEDIUM, so calibration cannot be
  judged from that run.
- The cost table in [`evals/results/cost-20261025.md`](evals/results/cost-20261025.md) applies list prices
  to measured tokens. It is a dated, order-of-magnitude estimate. Nothing was paid, and it does not yet
  include the Gemini run.

Full reports, including per-category adversarial outcomes: [`evals/results/`](evals/results).

## Using the API

```bash
curl -s -X POST https://guardsql.onrender.com/v1/ask \
  -H "X-API-Key: $YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"question": "How many customers are there?"}'
```

A successful response contains `sql`, `columns`, `rows`, `row_count`, `row_limit_hit`, `query_ms`,
`latency_ms`, `confidence` (`level`, `score`, `samples_agreeing`, `repairs`) and `usage` (LLM calls and
tokens). `row_limit_hit` is true only when the service's own row cap truncated the result, not when the
question asked for a limit.

| Endpoint | Purpose |
|---|---|
| `POST /v1/ask` | Ask a question (key required) |
| `GET /v1/schema` | Tables and columns the model is allowed to see (key required) |
| `GET /health` | Process is up |
| `GET /ready` | Database is reachable |

Errors always have the same shape and a stable `code`, for example `CANNOT_ANSWER`, `UNAUTHORIZED`,
`RATE_LIMITED`, `DAILY_LIMIT`, `QUERY_TOO_EXPENSIVE`, `MODEL_UNAVAILABLE`.

## Run it locally

Requires [uv](https://docs.astral.sh/uv/), Docker, and either [Ollama](https://ollama.com) (free, runs the
model on your machine) or a Gemini API key.

```bash
git clone https://github.com/yashdodiya9/GuardSQL.git
cd GuardSQL
uv sync
cp .env.example .env
docker compose up -d --wait          # local Postgres with the seeded shop data
ollama pull qwen2.5-coder:7b         # skip if you use Gemini
uv run uvicorn guardsql.main:app --reload
```

With an empty `API_KEYS` the API runs without authentication, for local use only. To run the UI against it:

```bash
cd web
cp .env.example .env                 # set VITE_API_BASE=http://localhost:8000
npm install
npm run dev
```

Add `http://localhost:5173` to `CORS_ALLOWED_ORIGINS` in the API's `.env` so the browser may call it.

Tests (open a fresh terminal with no database variables exported; the tests refuse to run against a remote
database):

```bash
uv run pytest                        # unit and integration; needs the compose Postgres
uv run ruff check . && uv run mypy src
cd web && npm test
```

Reproduce the eval:

```bash
uv run python scripts/run_eval.py --help
```

## Configuration

Everything is environment-driven; [`.env.example`](.env.example) lists every setting. The ones that matter
for a deployment:

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `ollama`, `gemini` or `fake` (tests) |
| `POSTGRES_RO_USER` / `POSTGRES_RO_PASSWORD` | The only database role the API uses |
| `POSTGRES_SSLMODE` | `require` for hosted databases |
| `API_KEYS` | Comma-separated `name:sha256-hash`; create one with `scripts/make_api_key.py <name>` |
| `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` | Per-key token bucket |
| `DAILY_LIMIT_PER_KEY`, `DAILY_LIMIT_TOTAL` | Daily caps per UTC day; `0` disables |
| `MAX_CONCURRENT_ASKS` | Requests allowed to run at once; extras get a fast `429` |
| `CORS_ALLOWED_ORIGINS` | Exact browser origins, comma-separated, no trailing slash |
| `RESTRICTED_COLUMNS` | Columns the validator always refuses |

## Repository layout

```text
src/guardsql/
  api/            FastAPI app, auth, rate limits, error envelope
  llm/            provider-neutral interface, Ollama and Gemini adapters, prompts, retries
  guardrails/     AST validator, allow-list policy, EXPLAIN cost gate
  execution/      pooled read-only executor
  hallucination/  repair loop, sample consistency, judge, confidence
  schema/         live schema introspection
  evals/          golden set, adversarial set, metrics, cost table
  cache.py        validated-SQL cache (never rows)
  routing.py      escalation to a stronger model on free signals
  audit.py        append-only metadata log
scripts/          eval runner, key generator, database provisioning
web/              React + Vite + TypeScript UI
tests/            unit and integration tests
DECISIONS.md      28 architecture decision records
```

## Design decisions worth reading

[DECISIONS.md](DECISIONS.md) records the context, decision and consequences of each choice. A few:

- **ADR-001, ADR-011:** the model's output is a candidate, never a command.
- **ADR-002:** database privileges are the final enforcement layer, not application code.
- **ADR-004:** execute the regenerated SQL, never the original string.
- **ADR-014, ADR-015:** compare samples by execution result; confidence is advisory and the judge is capped.
- **ADR-019, ADR-020:** score by execution accuracy with intervals, and test hostile inputs with a leak check
  that is independent of the validator.
- **ADR-024:** cache validated SQL per normalised question, never rows.
- **ADR-026, ADR-028:** per-key auth that fails closed in production; deployment on free tiers.

## Known limitations

- **Free-tier behaviour.** The API sleeps after about 15 minutes idle, and the database compute suspends
  after about 5, so a first request after a quiet period is slow. Free quotas and terms can change.
- **Demo state is in memory.** Rate limits, daily caps and the cache reset when the container restarts, and
  the audit log is lost on redeploy because the free host has no persistent disk.
- **Daily caps are best-effort.** They are per process and per UTC day, which is fine for one instance and
  not for several.
- **The demo key is public by design.** Anyone with it shares its daily budget. Revoke it by removing its
  entry from `API_KEYS`.
- **Accuracy numbers are an upper bound** (see Results). One synthetic schema of five tables does not show
  how this behaves on a large real warehouse.
- **Free-model data terms.** Questions sent to a free-tier model may be handled under that provider's
  free-tier terms, so the demo must only ever hold synthetic data.

## Author

Yash Dodiya · [github.com/yashdodiya9](https://github.com/yashdodiya9)
