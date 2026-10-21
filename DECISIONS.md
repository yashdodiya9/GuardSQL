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

## ADR-013: Repair only honest mistakes. Validator rejections for unknown tables, columns or functions are fed back to the model, up to ## ADR-013: Repair only honest mistakes
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