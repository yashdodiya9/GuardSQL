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