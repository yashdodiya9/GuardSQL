import pytest

from guardsql.llm.extract import DEFAULT_REFUSAL, extract_sql

SQL = "SELECT status, count(*) FROM orders GROUP BY status"


@pytest.mark.parametrize(
    "text",
    [
        f"```sql\n{SQL}\n```",
        f"```SQL\n{SQL}\n```",
        f"```\n{SQL}\n```",
        f"```postgresql\n{SQL}\n```",
        f"Here you go:\n\n```sql\n{SQL}\n```\n\nThis groups orders by status.",
        f"\n\n  ```sql\n  {SQL}  \n```  \n",
        f"```sql\r\n{SQL}\r\n```",
        f"```sql\n{SQL}\n",  # unterminated fence (stream cut off)
        SQL,  # no fence at all
        f"  \n{SQL};\n",
        f"<think>The user wants a count... SELECT nothing</think>\n```sql\n{SQL}\n```",
        f"<think>\nmulti\nline\n</think>{SQL}",
        f"```\nnotes\n```\n```sql\n{SQL}\n```",  # first block is not SQL: skip it
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "(SELECT 1) UNION (SELECT 2)",
    ],
)
def test_extracts_sql(text: str) -> None:
    result = extract_sql(text)
    assert result.refusal is None
    assert result.sql is not None and result.sql.upper().lstrip("( ").startswith(("SELECT", "WITH"))


def test_extracted_sql_is_stripped_but_otherwise_untouched() -> None:
    result = extract_sql("```sql\n   SELECT 1;   \n```")
    assert result.sql == "SELECT 1;"  # the validator, not the extractor, deals with semicolons


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("CANNOT_ANSWER: there is no revenue column", "there is no revenue column"),
        ("cannot_answer : no such table", "no such table"),
        ("  CANNOT_ANSWER:   too\n  many   spaces  ", "too many spaces"),
        ("```\nCANNOT_ANSWER: inside a fence\n```", "inside a fence"),
        ("<think>hmm</think>CANNOT_ANSWER: after thinking", "after thinking"),
        ("CANNOT_ANSWER", DEFAULT_REFUSAL),
        ("CANNOT_ANSWER:", DEFAULT_REFUSAL),
    ],
)
def test_extracts_refusal(text: str, reason: str) -> None:
    result = extract_sql(text)
    assert result.sql is None and result.refusal == reason


def test_long_refusal_is_truncated() -> None:
    result = extract_sql("CANNOT_ANSWER: " + "x" * 1000)
    assert result.refusal is not None and len(result.refusal) == 200


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n ",
        "I'm sorry, I can't help with that.",
        "You could run SELECT * FROM orders to see them.",  # SQL buried in prose: do not guess
        "```python\nprint('hi')\n```",
        "DELETE FROM orders",  # not SQL we would ever pass on
        "<think>still thinking, ran out of tokens",
        "<think>SELECT 1</think>",  # only reasoning, no answer
    ],
)
def test_returns_nothing_when_unsure(text: str) -> None:
    result = extract_sql(text)
    assert result.sql is None and result.refusal is None


def test_a_destructive_statement_in_a_sql_fence_is_not_treated_as_sql() -> None:
    assert extract_sql("```sql\nDROP TABLE orders\n```").sql is None


def test_reasoning_cannot_smuggle_sql_past_the_extractor() -> None:
    text = "<think>```sql\nSELECT secret FROM x\n```</think>No answer."
    assert extract_sql(text).sql is None
