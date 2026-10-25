# Architecture Decisions

## ADR-001: Treat the LLM as an untrusted component
**Context:** LLM-generated SQL can be destructive, hallucinated, or expensive.
**Decision:** Safety never depends on the model behaving. Deterministic checks
(parser, allowlists, DB permissions) wrap every generated query.
**Consequences:** More engineering than a prompt-only approach, but guarantees
hold even when the model is wrong or manipulated.

## ADR-002: Database privileges are the final enforcement layer
**Context:** Application-level validation can have bugs.
**Decision:** The executor connects as a role with SELECT-only, column-level
grants. Session settings (timeouts, read-only flag) are defaults, not guarantees,
because a session can change them; privileges cannot be changed by the session.
**Consequences:** A validator bypass still cannot write data or read PII columns.

## ADR-003: uv for dependency management
**Context:** Reproducible installs matter for CI and deployment.
**Decision:** Use uv with a committed `uv.lock`; CI installs with `--locked`.
**Consequences:** Identical dependency versions locally and in CI, with fast installs.

## ADR-004: Execute the regenerated SQL, never the original string
**Context:** Parsers and databases can disagree about comments, quoting and dollar strings.
**Decision:** The validator parses to a syntax tree, checks it, regenerates SQL from the tree
(comments dropped) and only that text is executed.
**Consequences:** The text we checked is the text we run. Output must re-validate unchanged.

## ADR-005: Functions are default-deny
**Context:** Postgres exposes functions that sleep, read files, change settings or open connections.
**Decision:** Only allowlisted functions pass; everything else is rejected.
**Consequences:** Safe by default, but some valid queries are rejected. The eval harness (M8)
measures this false-positive rate and the allowlist grows from data.

## ADR-006: Introspect through information_schema, not pg_catalog
**Context:** pg_catalog lists every column to every role, ignoring column grants.
**Decision:** Introspection reads information_schema.columns as the read-only role.
**Consequences:** Restricted column names never reach the LLM prompt or the validator policy.

## ADR-007: Two independent limits on query cost
**Context:** A valid SELECT can still be ruinously expensive (large cross joins).
**Decision:** An EXPLAIN-based cost gate rejects queries the planner predicts are too
expensive, and a per-transaction statement timeout cancels anything that runs too long anyway.
**Consequences:** The gate is cheap but estimate-based; the timeout is exact but only acts
after resources are spent. Neither alone is enough. MAX_PLAN_COST is relative to data size
and is calibrated from measured query costs in the eval harness.

## ADR-008: One pooled, read-only execution path
**Context:** Generated SQL must never reach the database through an unchecked route.
**Decision:** Only ReadOnlyExecutor runs SQL. It accepts only successful validation results,
uses a bounded pool of READ ONLY connections, applies limits with transaction-local settings,
and always rolls back.
**Consequences:** A single place to audit, log and test. Limits cannot leak between requests.

## ADR-009: Guardrail policy lives in configuration
**Context:** Which columns are sensitive differs per deployment.
**Decision:** Restricted columns, row limits and cost limits are settings with validated bounds.
**Consequences:** Policy changes need no code change. Invalid values fail at startup.

## ADR-010: Provider-neutral LLM interface with normalized errors
**Context:** Free tiers change, and different models suit different stages (cheap first, stronger on retry).
**Decision:** The app depends only on an LLMClient protocol. Ollama and Gemini are thin adapters;
a scripted fake serves tests and keyless demos. Every provider failure becomes one LLMError
with a kind, and only transient kinds (timeout, outage, rate limit) are retried.
**Consequences:** Providers swap via configuration. CI never calls a real model.
Retries are bounded so a request never hangs.

## ADR-011: The model's output is a candidate, never a command
**Context:** Prompts cannot reliably stop prompt injection or hallucination.
**Decision:** The generator only extracts a candidate SELECT (or an explicit refusal). Anything
else, including SQL buried in prose, is treated as "no SQL". Candidates then go through the
validator and the read-only executor.
**Consequences:** Safety does not depend on the prompt. The prompt only improves accuracy.

## ADR-012: Static prompt prefix, variable suffix
**Context:** Schema and rules are long and identical on every request; the question is short.
**Decision:** Rules and schema live in the system prompt, byte-identical per request. The date
and question go last.
**Consequences:** Providers that cache repeated prefixes can bill and process the long part once.
Cache savings will be measured in M9.

## ADR-013: Repair only honest mistakes
**Context:** The model sometimes invents a table, column or function. The validator rejects these, and the rejection message is enough for the model to fix them. Other rejections (DML, multiple statements, restricted columns) point to adversarial or out-of-policy requests, not honest mistakes.
**Decision:** Validator rejections with `PARSE_ERROR`, `UNKNOWN_TABLE`, `UNKNOWN_COLUMN`, `SCHEMA_NOT_ALLOWED`, `FUNCTION_NOT_ALLOWED` or `UNANALYZABLE` are fed back to the model, up to `MAX_REPAIRS` times. All other rejections are final and are never retried.
**Consequences:** Honest mistakes are fixed without the user seeing an error. A request for restricted data is never quietly turned into a different answer, so "show me salaries" fails visibly. Each repair costs one extra LLM call, and a repaired answer is penalised in the confidence score.

## ADR-014: Self-consistency compares execution results
**Context:** SQL can be valid and still answer the wrong question, and the validator cannot see that. Different SQL text can give the same answer (`COUNT(*)` vs `count(1)`).
**Decision:** N−1 extra samples are generated at a higher temperature and executed. Results are compared by an order-insensitive hash of the rows, ignoring column names but not column count. Identical validated SQL is executed only once. Samples that fail to generate, validate or execute count as disagreement.
**Consequences:** Inconsistent model errors are detected without a labelled answer. Cost grows to N× LLM calls plus some extra read-only queries, which M9 will gate with routing. A model that is consistently wrong still agrees with itself, so M8 will measure how often that happens.

## ADR-015: Confidence is advisory, and the judge is capped
**Context:** Users and the API need a signal about how much to trust an answer. An LLM judge reads the untrusted question, so it can be talked into agreeing. It also shares the generator's blind spots.
**Decision:** Confidence is a transparent weighted sum of agreement, repairs and empty results, mapped to HIGH, MEDIUM or LOW. The judge is off by default and runs only in the grey zone. It can veto with a NO, but a YES can never lift a result into HIGH on its own. Judge errors count as UNSURE.
**Consequences:** The score is explainable in an interview and easy to unit test. The weights are heuristics until M8 calibrates them against the golden set. Confidence is never a security control: the validator and the read-only role remain the real boundary.
## ADR-016: Append-only metadata audit log that cannot fail a request
**Context:** Security and cost review needs a record of who asked what and what the system did. The log must not become a second place where sensitive data lives, and a full disk must not take the API down.
**Decision:** One JSON object per line, holding the question (truncated), outcome, stage and code, the SQL (including blocked SQL), confidence signals, token counts and latency. It never holds result rows. Writes are serialised with a lock, `json.dumps` escapes newlines so questions cannot forge lines, and an `OSError` is logged and swallowed.
**Consequences:** Blocked attempts are reviewable and the M8 and M9 numbers can be read straight from the log. A file is enough for the free tier, and it can be shipped elsewhere later. Questions can contain personal data, so retention should be limited in production.

## ADR-017: One stable error envelope; internals go to the audit log
**Context:** Clients need to branch on errors, and raw validator or driver messages can reveal schema details, SQL or credentials.
**Decision:** Every failure is `{"error": {code, message, request_id, reason?}}` with a stable public code (`QUERY_BLOCKED`, `MODEL_UNAVAILABLE`, `QUERY_TIMEOUT`, ...). Status codes separate user-fixable (422), upstream failures (502, 503, 504) and our bugs (500). Validator detail and rejected SQL are written to the audit log only, and unexpected exceptions return a generic 500.
**Consequences:** The contract is small and testable, and a frontend can show a helpful message per code. Debugging a user's report needs the request ID and the audit log. `CANNOT_ANSWER` text comes from the model, so a frontend must render it as plain text.

## ADR-018: Bounded concurrency with fast 429, sync endpoints
**Context:** Each question can trigger several LLM calls, and free tiers have tight rate limits. The pipeline uses blocking drivers.
**Decision:** Endpoints are plain `def`, so FastAPI runs them in its threadpool. A semaphore (`MAX_CONCURRENT_ASKS`, default 4, below the pool size of 10) limits questions in flight, and extra requests get an immediate `429` with `Retry-After`.
**Consequences:** A burst cannot exhaust the LLM quota or the database pool, and callers learn quickly instead of queueing. Per-client authentication and rate limits were added later (ADR-026).

## ADR-019: Score by execution accuracy, with intervals and calibration
**Context:** Text-to-SQL answers can be right with different SQL, so comparing strings undercounts, and using an LLM to grade answers adds an unreliable judge (the judge measured here flagged wrong answers with 25% precision). Presentation differences such as row order, column aliases, 2.50 versus 2.5, or a midnight timestamp versus a date must not count as errors, while wrong values or a wrong number of columns must.
**Decision:** Accuracy is execution accuracy. The model's SQL and a hand-written reference query run against the same seeded database and the result sets are compared: order-insensitive, numbers rounded to two decimals, midnight timestamps equal to dates, column names ignored but column count strict. Unanswerable questions are scored on whether no data was returned. Every proportion is reported with a Wilson 95% interval, and accuracy is also broken down by confidence level (calibration) and by difficulty.
**Consequences:** Scores do not depend on the model's phrasing or on a second model's opinion. With 42 answerable questions the intervals are wide (roughly 10 points either way), so a difference of one or two questions between runs is noise and is reported that way. The set was used to find the date-prompt defect, so its accuracy is an upper bound until a held-out set exists. Golden questions must pin down the output shape, or a correct but differently shaped answer is scored wrong.

## ADR-020: Test hostile inputs at two levels, with an independent leak check
**Context:** Safety tested only through prompts depends on the model's behaviour that day, and tests that only exercise the validator do not show that the whole system holds when a model misbehaves. A guardrail that grades its own work can also hide its own bugs.
**Decision:** Raw SQL payloads go straight to the validator as if a model had written them (writes, multiple statements, restricted columns, dollar-quoted strings, nested comments, whole-row references). Each lists the acceptable rejection codes, so the result holds for any model and needs no LLM. Plain-English attacks in five categories (pii, injection, dos, system, write) go through the real pipeline, and a separate check reads the SQL that actually executed and flags anything touching a restricted column or that is not a plain SELECT, without trusting the validator's verdict. Statements that pass the validator but must be stopped by the cost gate form a third list.
**Consequences:** "0 leaks in 22 attacks" is measured by an oracle independent of the code under test, and the outcome table shows which layer stopped each attack (the model refusing, the validator, or the database). A model refusing is not credited as a guardrail. The attack list is finite and written by the author, so no leaks is evidence and not proof, and new attack ideas should be added as cases first.

## ADR-021: Benign controls guard against false positives
**Context:** A security filter that rejects too much fails silently: rejections look like safety. An earlier validator treated sqlglot's typing of AND and OR as function calls, so every query using AND or OR was blocked, and 200+ unit tests and the whole adversarial suite still passed.
**Decision:** The eval package keeps a list of benign statements that must be accepted, covering common SQL syntax (boolean operators, predicates, arithmetic, casts, subqueries, set operations, window functions), and every golden reference must pass the validator offline. The golden references are also run against the database in an integration test.
**Consequences:** Over-blocking now fails the build, and accuracy numbers cannot be quietly depressed by the guardrails. New syntax the model produces and the validator wrongly rejects should be added as a control first, then fixed.

## ADR-022: Repair database errors, keep the message away from users
**Context:** An unknown column in HAVING passed validation and failed in Postgres, and the model never saw the error text, so the same mistake was final.
**Decision:** A DATABASE_ERROR from the executor feeds its one-line message back to the model in the same repair loop as validator rejections, sharing one budget (MAX_REPAIRS). Cost-gate failures, timeouts and outages are never repaired. The database message goes only into the repair prompt: the API returns a generic error and the audit log keeps the detail.
**Consequences:** Honest column and grouping mistakes are fixed at the cost of one more cheap call. The error text can echo a literal from the model's own SQL, so it is treated as untrusted input to the prompt, and it is capped at 200 characters by the executor.

## ADR-023: Escalate to a stronger model only on free signals, and sample once by default
**Context:** Measured on a 42-question golden set: self-consistency tripled latency and tokens with no accuracy gain, the LLM judge flagged wrong answers with 25% precision at 3.6x latency, and a 14B model was 5 points more accurate than the 7B at 3.7x the latency.
**Decision:** The default is one sample from the cheap model, no judge. A stronger model is tried only after a repairable validator rejection or a database error survives the repair budget, or when the result is empty or all NULL. Adversarial rejections, refusals, cost-gate failures, timeouts and outages never escalate, so bad prompts cannot force expensive calls.
**Consequences:** Most questions pay the cheap model's latency, and the strong model's cost lands on the few that look broken. Wrong answers that look healthy (misread questions) are invisible to these signals, and both models make them. Escalation rate and its accuracy gain are reported in every eval.

## ADR-024: Cache validated SQL per normalized question, never rows
**Context:** Repeated questions pay the full LLM cost every time (about 470 input tokens and 1.4 s on the 7B), and the same question under the same prompt and schema yields the same SQL.
**Decision:** An in-memory LRU with a TTL maps (fingerprint, normalized question) to the validated SQL. The fingerprint hashes the system prompt, schema prompt, provider, both model names and the sample count. A hit skips generation but is re-validated, cost-gated and executed again, so rows are always fresh and a stricter validator takes effect immediately; a failed replay evicts the entry and runs the full pipeline. Only successful, non-empty, non-LOW answers are cached, and questions with relative time are never cached because their SQL embeds a date literal. Matching is exact after normalization, with no semantic similarity.
**Consequences:** Repeats cost one query instead of an LLM call, and no result data is ever stored. A wrong answer stays cached until the TTL expires or the entry fails replay, so the TTL bounds how long an error repeats. The cache is per process, so workers do not share it, and two simultaneous identical misses both call the model. Hit rate depends on how often real users repeat questions, which the golden set cannot measure.

**Measured (6 questions against the live stack, 7B, one process):** cold pass p50 about 1.0 s with 6 LLM calls and 2,920 tokens; the same questions repeated, and again with different case, spacing and punctuation, hit 6/6 with 0 LLM calls, 0 tokens and p50 about 2 ms, returning rows identical to the cold pass. The benchmark first exposed a normalization bug (a space before trailing punctuation defeated the key), which is fixed and covered by a test. This shows the cost of a hit, not how often hits occur in real traffic.

## ADR-025: Report cost as a dated, notional price applied to measured tokens
**Context:** The service runs on local and free-tier models, so there is no invoice to quote, but "cost-optimized" needs a number a reader can check. Provider prices change, and token counts from a local tokenizer do not match a paid provider's.
**Decision:** Each eval run records tokens per question. A script converts them to dollars per 1,000 questions using a price table copied from the providers' pages, stamped with the date it was copied, and labelled notional. Cache savings are shown as a function of an assumed hit rate, never as a measured percentage. Schema pruning was not built: the schema is about 400 prompt tokens, so the possible saving is small next to the cost of getting pruning wrong.
**Consequences:** The numbers are an order of magnitude, not a billing forecast, and go stale as prices change. Self-consistency and the judge are shown at their real token cost next to their measured accuracy, so the cost of each optimization is visible beside what it bought. The static system prefix could be priced lower with provider-side prompt caching, which this table does not assume.

**Measured (golden set, 42 questions, tokens per question in / out):** 1 sample 480 / 32, 3 samples 1,323 / 90 (2.8x the tokens, same 37/42), judge 801 / 55 (1.7x, same 37/42), 14B at 1 sample 468 / 34 (same tokens as the 7B, 39/42). At current prices that is about $0.06 per 1,000 questions on Claude Haiku 5.5 and about $1.25 on Claude Sonnet 5.5 at 1 sample. Notional figures only: no money was spent, and the tokenizers differ from the local model's.

## ADR-026: API-key authentication and per-client rate limiting, failing closed in production
**Context:** The API sits in front of an LLM and a database, and an open endpoint lets anyone who finds it run unlimited questions at the owner's cost and probe the schema. Source-IP limits are unreliable behind a proxy, where every request shares one address.
**Decision:** Each client gets a random key; the server stores only its SHA-256 hash (API_KEYS=name:hash), so a leaked config does not leak usable keys, and compares in constant time over every entry. Authentication is a dependency that runs before the body is parsed, so unauthenticated callers cannot probe validation messages. Rate limiting is a per-client token bucket (burst plus a per-minute refill) that returns 429 with Retry-After and is audited with the client name. It is separate from the global concurrency cap, which protects the machine rather than sharing it fairly. With APP_ENV=production the app refuses to start without keys; in local mode auth is off with a warning, and nothing is rate limited because no client is identified. Health and readiness stay open for platform probes.
**Consequences:** One abusive client cannot starve the others, and the audit log shows who asked what without ever holding a key. Limits are per process, so several workers would each keep their own counters. The limiter bounds request rate, not LLM spend per client, so a daily quota is the next step if the service is offered to others. Key rotation means editing API_KEYS and restarting.
