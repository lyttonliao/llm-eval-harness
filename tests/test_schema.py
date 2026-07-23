from eval_harness.schema import ScoredResult


def _result(**checks: bool) -> ScoredResult:
    return ScoredResult(
        test_id="bt-01",
        predicted={"severity": "high", "category": "backend"},
        checks=checks,
        judge_score=0.9,
        judge_rationale="grounded in the report",
        cost_usd=0.001,
        duration_ms=500,
    )


def test_fully_correct_true_when_both_correct():
    assert _result(severity=True, category=True).fully_correct is True


def test_fully_correct_false_when_only_severity_correct():
    assert _result(severity=True, category=False).fully_correct is False


def test_fully_correct_false_when_only_category_correct():
    assert _result(severity=False, category=True).fully_correct is False


def test_fully_correct_false_when_both_incorrect():
    assert _result(severity=False, category=False).fully_correct is False


def test_fully_correct_true_when_single_check_passes():
    assert _result(tests_passed=True).fully_correct is True


def test_fully_correct_false_when_single_check_fails():
    assert _result(tests_passed=False).fully_correct is False
