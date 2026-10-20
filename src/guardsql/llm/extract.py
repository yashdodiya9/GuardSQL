"""Turn raw model text into either SQL or a refusal. Strict on purpose: when in doubt,
return nothing, because the validator should only ever see an intentional SQL answer."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Reasoning models (Qwen3, DeepSeek-R1, ...) may wrap their thinking in <think> tags.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_UNCLOSED = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)(?:```|\Z)", re.DOTALL)
_REFUSAL = re.compile(r"^\s*CANNOT_ANSWER\b\s*:?\s*(.*)$", re.DOTALL | re.IGNORECASE)
_LOOKS_LIKE_SQL = re.compile(r"^\s*(?:\(\s*)*(?:select|with)\b", re.IGNORECASE)

DEFAULT_REFUSAL = "The question cannot be answered from this schema."


@dataclass(frozen=True)
class Extracted:
    sql: str | None = None
    refusal: str | None = None


def _refusal_from(text: str) -> str | None:
    match = _REFUSAL.match(text)
    if match is None:
        return None
    return " ".join(match.group(1).split())[:200] or DEFAULT_REFUSAL


def extract_sql(text: str) -> Extracted:
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _THINK_UNCLOSED.sub("", cleaned).strip()
    if not cleaned:
        return Extracted()

    refusal = _refusal_from(cleaned)
    if refusal is not None:
        return Extracted(refusal=refusal)

    for block in _FENCE.finditer(cleaned):
        body = block.group(1).strip()
        if _LOOKS_LIKE_SQL.match(body):
            return Extracted(sql=body)
        refusal = _refusal_from(body)
        if refusal is not None:
            return Extracted(refusal=refusal)

    if _LOOKS_LIKE_SQL.match(cleaned):  # the model skipped the code fence
        return Extracted(sql=cleaned)
    return Extracted()
