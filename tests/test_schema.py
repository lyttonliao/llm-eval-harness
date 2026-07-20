from eval_harness.schema import ScoredResult


def _result(severity_correct: bool, category_correct: bool) -> ScoredResult:
    return ScoredResult(
        test_id="bt-01",
        predicted_severity="high",
        predicted_category="backend",
        severity_correct=severity_correct,
        category_correct=category_correct,
        judge_score=0.9,
        judge_rationale="grounded in the report",
        cost_usd=0.001,
        duration_ms=500,
    )


def test_fully_correct_true_when_both_correct():
    assert _result(True, True).fully_correct is True


def test_fully_correct_false_when_only_severity_correct():
    assert _result(True, False).fully_correct is False


def test_fully_correct_false_when_only_category_correct():
    assert _result(False, True).fully_correct is False


def test_fully_correct_false_when_both_incorrect():
    assert _result(False, False).fully_correct is False
