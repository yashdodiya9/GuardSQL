import pytest

from guardsql.evals.cost import (
    PRICES,
    PRICES_AS_OF,
    Price,
    parse_report,
    per_thousand,
    render_cache_table,
    render_document,
)

CONFIG_LINE = (
    "Configuration: provider=ollama, model=qwen2.5-coder:7b, samples=1, max_repairs=2, "
    "judge=False, date=2026-10-24, strong_model=qwen2.5-coder:14b"
)

REPORT = f"""# GuardSQL eval report

{CONFIG_LINE}

| Metric | Result |
|---|---|
| Execution accuracy | 38/42 = 90.5% (95% CI 77.9-96.2%) |
| Tokens per question (in / out) | 480 / 32 |
"""


def test_parse_report_reads_label_accuracy_and_tokens() -> None:
    run = parse_report(REPORT)
    assert run.label == "qwen2.5-coder:7b · 1 sample + routing"
    assert (run.correct, run.total) == (38, 42)
    assert (run.tokens_in, run.tokens_out) == (480, 32)


def test_judge_and_multiple_samples_show_in_the_label() -> None:
    text = REPORT.replace("samples=1", "samples=3").replace("judge=False", "judge=True")
    text = text.replace(", strong_model=qwen2.5-coder:14b", "")
    assert parse_report(text).label == "qwen2.5-coder:7b · 3 samples + judge"


def test_a_file_that_is_not_a_report_is_rejected_with_its_name() -> None:
    with pytest.raises(ValueError, match="notes.md"):
        parse_report("hello", "notes.md")


def test_cost_arithmetic() -> None:
    assert per_thousand(480, 32, PRICES["Claude Haiku 5.5"]) == pytest.approx(0.064)
    assert per_thousand(480, 32, PRICES["Claude Sonnet 5.5"]) == pytest.approx(1.28)
    assert per_thousand(480, 32, PRICES["Gemini 3.5 Flash-Lite"]) == pytest.approx(0.224)


def test_cache_hit_rate_scales_cost_linearly() -> None:
    price = {"M": Price(1.0, 2.0, "s")}
    table = render_cache_table(parse_report(REPORT), price, [0.0, 0.5])
    full = per_thousand(480, 32, price["M"])
    assert f"${full:,.3f}" in table
    assert f"${full / 2:,.3f}" in table


def test_document_states_the_price_date_and_the_assumption() -> None:
    run = parse_report(REPORT)
    document = render_document([run], run, [0.5], PRICES)
    assert f"as of {PRICES_AS_OF}" in document
    assert "assumptions, not measurements" in document
