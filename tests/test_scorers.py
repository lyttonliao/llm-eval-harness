from unittest.mock import patch

from eval_harness.claude_cli import CliResult
from eval_harness.schema import ModelOutput, TestCase
from eval_harness.scorers import judge_score, rule_based_score_bug_triage, score_all

CASE = TestCase(
    id="bt-01",
    input="Something broke.",
    expected={"severity": "High", "category": "Backend"},
)


def _output(**overrides) -> ModelOutput:
    defaults = dict(
        test_id="bt-01",
        raw_text='{"reasoning": "the API returns 500s", "severity": "high", "category": "backend"}',
        predicted={"reasoning": "the API returns 500s", "severity": "high", "category": "backend"},
        cost_usd=0.001,
        duration_ms=500,
        parse_error="",
    )
    defaults.update(overrides)
    return ModelOutput(**defaults)


def _predicted(**overrides) -> dict:
    defaults = {"reasoning": "the API returns 500s", "severity": "high", "category": "backend"}
    defaults.update(overrides)
    return defaults


# --- rule_based_score_bug_triage --------------------------------------------


def test_rule_based_score_matches_case_insensitively():
    checks = rule_based_score_bug_triage(CASE, _output())
    assert checks == {"severity": True, "category": True}


def test_rule_based_score_strips_surrounding_whitespace():
    output = _output(predicted=_predicted(severity="  high  ", category="  backend  "))
    checks = rule_based_score_bug_triage(CASE, output)
    assert checks == {"severity": True, "category": True}


def test_rule_based_score_handles_missing_predictions():
    output = _output(predicted={"reasoning": "unclear"})
    checks = rule_based_score_bug_triage(CASE, output)
    assert checks == {"severity": False, "category": False}


def test_rule_based_score_mismatch_is_false():
    output = _output(predicted=_predicted(severity="low", category="frontend"))
    checks = rule_based_score_bug_triage(CASE, output)
    assert checks == {"severity": False, "category": False}


# --- judge_score ----------------------------------------------------------


def test_judge_score_short_circuits_on_parse_error_without_calling_claude():
    output = _output(parse_error="could not parse CLI json output")
    with patch("eval_harness.scorers.call_claude") as mock_call:
        score, rationale = judge_score(CASE, output)

    mock_call.assert_not_called()
    assert score == 0.0
    assert "skipped judging" in rationale
    assert "could not parse CLI json output" in rationale


def test_judge_score_returns_zero_when_judge_call_errors():
    with patch("eval_harness.scorers.call_claude", return_value=CliResult(text="", cost_usd=0.0, duration_ms=0, error="rate limited")):
        score, rationale = judge_score(CASE, _output())

    assert score == 0.0
    assert "judge call failed" in rationale
    assert "rate limited" in rationale


def test_judge_score_returns_zero_on_unparseable_judge_output():
    with patch(
        "eval_harness.scorers.call_claude",
        return_value=CliResult(text="not json", cost_usd=0.001, duration_ms=200),
    ):
        score, rationale = judge_score(CASE, _output())

    assert score == 0.0
    assert rationale == "judge returned unparseable output"


def test_judge_score_happy_path_parses_coherence_and_rationale():
    judge_payload = '{"coherence_score": 0.85, "rationale": "reasoning cites the specific 500 error"}'
    with patch(
        "eval_harness.scorers.call_claude",
        return_value=CliResult(text=judge_payload, cost_usd=0.002, duration_ms=300),
    ) as mock_call:
        score, rationale = judge_score(CASE, _output())

    assert score == 0.85
    assert rationale == "reasoning cites the specific 500 error"
    mock_call.assert_called_once()


def test_judge_score_handles_unparseable_model_reasoning_gracefully():
    # raw_text itself isn't valid JSON - reasoning extraction should fall back
    # to an empty string rather than raising, and still call the judge.
    output = _output(raw_text="not json output from the model")
    judge_payload = '{"coherence_score": 0.1, "rationale": "no reasoning to evaluate"}'
    with patch(
        "eval_harness.scorers.call_claude",
        return_value=CliResult(text=judge_payload, cost_usd=0.001, duration_ms=100),
    ) as mock_call:
        score, rationale = judge_score(CASE, output)

    assert score == 0.1
    user_message = mock_call.call_args.args[1]
    assert "''" in user_message  # empty reasoning was substituted


# --- score_all --------------------------------------------------------------


def test_score_all_skips_cases_with_no_matching_output():
    cases = [CASE, TestCase(id="bt-02", input="x", expected={"severity": "low", "category": "frontend"})]
    outputs = [_output(test_id="bt-01")]  # no output for bt-02

    results = score_all("bug_triage", cases, outputs, run_judge=False)

    assert len(results) == 1
    assert results[0].test_id == "bt-01"


def test_score_all_ignores_outputs_with_no_matching_case():
    cases = [CASE]
    outputs = [_output(test_id="bt-01"), _output(test_id="bt-99")]

    results = score_all("bug_triage", cases, outputs, run_judge=False)

    assert len(results) == 1
    assert results[0].test_id == "bt-01"


def test_score_all_run_judge_false_never_calls_claude():
    cases = [CASE]
    outputs = [_output()]

    with patch("eval_harness.scorers.call_claude") as mock_call:
        results = score_all("bug_triage", cases, outputs, run_judge=False)

    mock_call.assert_not_called()
    assert results[0].judge_score == 0.0
    assert results[0].judge_rationale == "judging disabled"


def test_score_all_run_judge_true_calls_claude_once_per_case():
    cases = [CASE]
    outputs = [_output()]
    judge_payload = '{"coherence_score": 0.7, "rationale": "solid"}'

    with patch(
        "eval_harness.scorers.call_claude",
        return_value=CliResult(text=judge_payload, cost_usd=0.001, duration_ms=100),
    ) as mock_call:
        results = score_all("bug_triage", cases, outputs, run_judge=True)

    mock_call.assert_called_once()
    assert results[0].judge_score == 0.7
    assert results[0].checks == {"severity": True, "category": True}
