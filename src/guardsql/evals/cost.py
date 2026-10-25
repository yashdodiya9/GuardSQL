"""Turn measured eval token counts into a notional paid-API cost.

Prices are USD per million tokens, copied from the providers' pages on PRICES_AS_OF. They change,
so every table states the date. Token counts come from the local model's tokenizer; other
providers' tokenizers differ, so read the results as an order of magnitude, not an invoice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PRICES_AS_OF = "2026-10-24"


@dataclass(frozen=True)
class Price:
    input_per_mtok: float
    output_per_mtok: float
    source: str


_ANTHROPIC = "platform.claude.com pricing page"
_GEMINI = "third-party summary of ai.google.dev, checked 2026-08-21 (verify before quoting)"

PRICES: dict[str, Price] = {
    "Claude Haiku 5.5": Price(0.10, 0.50, f"{_ANTHROPIC}, prompts up to 100k tokens"),
    "Claude Sonnet 5.5": Price(2.00, 10.00, _ANTHROPIC),
    "Claude Opus 5.5": Price(4.00, 20.00, _ANTHROPIC),
    "Gemini 3.5 Flash-Lite": Price(0.30, 2.50, _GEMINI),
    "Gemini 3.5 Flash": Price(1.50, 9.00, _GEMINI),
}


@dataclass(frozen=True)
class Run:
    label: str
    correct: int
    total: int
    tokens_in: int
    tokens_out: int


_CONFIG = re.compile(r"^Configuration: (.+)$", re.MULTILINE)
_TOKENS = re.compile(r"\| Tokens per question \(in / out\) \| (\d+) / (\d+) \|")
_ACCURACY = re.compile(r"\| Execution accuracy \| (\d+)/(\d+)")


def parse_report(text: str, source: str = "report") -> Run:
    config_match = _CONFIG.search(text)
    tokens = _TOKENS.search(text)
    accuracy = _ACCURACY.search(text)
    if not (config_match and tokens and accuracy):
        raise ValueError(f"{source}: not an eval report (config, accuracy or token line missing)")
    pairs = (part.split("=", 1) for part in config_match.group(1).split(", ") if "=" in part)
    config = {key: value for key, value in pairs}
    samples = config.get("samples", "?")
    label = f"{config.get('model', '?')} · {samples} sample{'' if samples == '1' else 's'}"
    if config.get("judge") == "True":
        label += " + judge"
    if config.get("strong_model", "") not in ("", "None"):
        label += " + routing"
    return Run(
        label=label,
        correct=int(accuracy.group(1)),
        total=int(accuracy.group(2)),
        tokens_in=int(tokens.group(1)),
        tokens_out=int(tokens.group(2)),
    )


def per_thousand(tokens_in: int, tokens_out: int, price: Price) -> float:
    """Dollars to answer 1,000 questions with this many tokens each."""
    return 1000 * (tokens_in * price.input_per_mtok + tokens_out * price.output_per_mtok) / 1e6


def _money(value: float) -> str:
    return f"${value:,.3f}"


def render_cost_table(runs: list[Run], prices: dict[str, Price]) -> str:
    names = list(prices)
    lines = [
        "| Run | Correct | Tokens in / out | " + " | ".join(names) + " |",
        "|---|---|---|" + "---|" * len(names),
    ]
    for run in runs:
        costs = " | ".join(
            _money(per_thousand(run.tokens_in, run.tokens_out, prices[name])) for name in names
        )
        lines.append(
            f"| {run.label} | {run.correct}/{run.total} | "
            f"{run.tokens_in} / {run.tokens_out} | {costs} |"
        )
    return "\n".join(lines)


def render_cache_table(run: Run, prices: dict[str, Price], rates: list[float]) -> str:
    names = list(prices)
    lines = [
        "| Assumed cache hit rate | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for rate in rates:
        costs = " | ".join(
            _money(per_thousand(run.tokens_in, run.tokens_out, prices[name]) * (1 - rate))
            for name in names
        )
        lines.append(f"| {rate:.0%} | {costs} |")
    return "\n".join(lines)


def render_document(
    runs: list[Run], basis: Run, rates: list[float], prices: dict[str, Price]
) -> str:
    sources = "\n".join(f"- {name}: {price.source}" for name, price in prices.items())
    return (
        f"# Notional API cost per 1,000 questions (prices as of {PRICES_AS_OF})\n\n"
        "Measured tokens per question from saved eval runs, priced at current paid-API rates. "
        "Nothing here was paid: the runs used local Ollama.\n\n"
        f"{render_cost_table(runs, prices)}\n\n"
        f"## Effect of a query cache on `{basis.label}`\n\n"
        "A cache hit skips the LLM entirely, so cost scales with (1 - hit rate). The hit rates "
        "are assumptions, not measurements: real traffic decides them.\n\n"
        f"{render_cache_table(basis, prices, rates)}\n\n"
        "## Prices and caveats\n\n"
        f"{sources}\n\n"
        "- Token counts come from the Qwen tokenizer; other providers' tokenizers differ.\n"
        "- Prompt caching of the static system prefix is not applied, so these are upper bounds.\n"
        "- Prices change; the date above is when they were copied.\n"
    )
