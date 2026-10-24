"""Cheap model first; escalate to a stronger model only when free signals say it struggled.

Escalating on adversarial rejections would let an attacker force expensive calls by sending
bad prompts, so only honest-mistake signals qualify (same split as the repair loop, ADR-013).
"""

from __future__ import annotations

import dataclasses

from guardsql.hallucination.pipeline import REPAIRABLE, Asker, PipelineResult

_ESCALATING_CODES = frozenset(code.value for code in REPAIRABLE)


def escalation_reason(result: PipelineResult) -> str | None:
    """Why this result deserves a second opinion from the strong model, or None."""
    if result.ok:
        if result.signals is not None and result.signals.empty_result:
            return "empty_result"
        return None
    if result.stage == "validate" and result.code in _ESCALATING_CODES:
        return f"validate:{result.code}"
    if result.stage == "execute" and result.code == "DATABASE_ERROR":
        return "execute:DATABASE_ERROR"
    return None


class EscalatingPipeline:
    def __init__(self, primary: Asker, strong: Asker) -> None:
        self._primary = primary
        self._strong = strong

    def ask(self, question: str) -> PipelineResult:
        first = self._primary.ask(question)
        reason = escalation_reason(first)
        if reason is None:
            return first
        second = self._strong.ask(question)
        # The strong answer wins when it works. If the cheap answer was merely empty and the
        # strong one failed, the cheap answer stands.
        chosen = second if (second.ok or not first.ok) else first
        return dataclasses.replace(
            chosen,
            escalated=True,
            escalation_reason=reason,
            llm_calls=first.llm_calls + second.llm_calls,
            input_tokens=first.input_tokens + second.input_tokens,
            output_tokens=first.output_tokens + second.output_tokens,
            usage_complete=first.usage_complete and second.usage_complete,
            strong_calls=second.llm_calls,
            strong_input_tokens=second.input_tokens,
            strong_output_tokens=second.output_tokens,
        )
