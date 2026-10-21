import pytest

from guardsql.hallucination.confidence import (
    Confidence,
    ConfidenceConfig,
    JudgeVerdict,
    Signals,
    apply_judge,
    base_score,
    level,
)

CFG = ConfidenceConfig()


def sig(**kw: object) -> Signals:
    base: dict[str, object] = {
        "samples_requested": 3,
        "samples_agreeing": 3,
        "repairs": 0,
        "empty_result": False,
    }
    base.update(kw)
    return Signals(**base)  # type: ignore[arg-type]


def test_full_agreement_is_high() -> None:
    s = base_score(sig(), CFG)
    assert s == pytest.approx(0.85)
    assert level(s, CFG) is Confidence.HIGH


def test_partial_agreement_is_medium() -> None:
    s = base_score(sig(samples_agreeing=2), CFG)
    assert s == pytest.approx(0.70)
    assert level(s, CFG) is Confidence.MEDIUM


def test_only_self_agreement_is_low_ish() -> None:
    s = base_score(sig(samples_agreeing=1), CFG)
    assert s == pytest.approx(0.55)


def test_single_sample_can_never_be_high() -> None:
    s = base_score(sig(samples_requested=1, samples_agreeing=1), CFG)
    assert level(s, CFG) is not Confidence.HIGH


def test_repairs_penalised_and_capped() -> None:
    one = base_score(sig(repairs=1), CFG)
    many = base_score(sig(repairs=9), CFG)
    assert one == pytest.approx(0.70)
    assert many == pytest.approx(0.55)  # capped at max_repairs_counted=2


def test_empty_result_penalised() -> None:
    assert base_score(sig(empty_result=True), CFG) == pytest.approx(0.75)


def test_score_is_clamped() -> None:
    worst = base_score(sig(samples_agreeing=0, repairs=9, empty_result=True), CFG)
    assert 0.0 <= worst <= 1.0


def test_judge_no_pulls_down() -> None:
    assert apply_judge(0.70, JudgeVerdict.NO, CFG) == pytest.approx(0.35)


def test_judge_no_clamped_at_zero() -> None:
    assert apply_judge(0.10, JudgeVerdict.NO, CFG) == 0.0


def test_judge_yes_cannot_reach_high_alone() -> None:
    raised = apply_judge(0.79, JudgeVerdict.YES, CFG)
    assert raised == pytest.approx(0.79)
    assert level(raised, CFG) is not Confidence.HIGH


def test_judge_yes_raises_within_medium() -> None:
    assert apply_judge(0.55, JudgeVerdict.YES, CFG) == pytest.approx(0.70)


def test_judge_unsure_and_none_change_nothing() -> None:
    assert apply_judge(0.6, JudgeVerdict.UNSURE, CFG) == 0.6
    assert apply_judge(0.6, None, CFG) == 0.6
