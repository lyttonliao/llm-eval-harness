from unittest.mock import patch

from eval_harness.claude_cli import CliResult
from eval_harness.schema import ModelOutput, TestCase
from eval_harness.scorers import (
    judge_score,
    rule_based_score_bug_triage,
    rule_based_score_code_gen,
    rule_based_score_code_review,
    rule_based_score_summarization,
    score_all,
)

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


# --- rule_based_score_code_gen ----------------------------------------------

CODE_GEN_CASE = TestCase(
    id="cg-01",
    input="Write an add(a, b) function.",
    expected={"test_code": "from solution import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"},
)


def test_rule_based_score_code_gen_passes_when_sandbox_reports_success():
    output = ModelOutput(
        test_id="cg-01", raw_text="{}", predicted={"code": "def add(a, b):\n    return a + b\n"},
        cost_usd=0.001, duration_ms=500,
    )
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(True, "1 passed")) as mock_check:
        checks = rule_based_score_code_gen(CODE_GEN_CASE, output)

    assert checks == {"tests_passed": True}
    mock_check.assert_called_once_with(
        "def add(a, b):\n    return a + b\n", CODE_GEN_CASE.expected["test_code"]
    )


def test_rule_based_score_code_gen_fails_when_sandbox_reports_failure():
    output = ModelOutput(
        test_id="cg-01", raw_text="{}", predicted={"code": "def add(a, b):\n    return a - b\n"},
        cost_usd=0.001, duration_ms=500,
    )
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(False, "1 failed")):
        checks = rule_based_score_code_gen(CODE_GEN_CASE, output)

    assert checks == {"tests_passed": False}


def test_rule_based_score_code_gen_handles_missing_code_key():
    output = ModelOutput(test_id="cg-01", raw_text="{}", predicted={}, cost_usd=0.001, duration_ms=500)
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(False, "no code")) as mock_check:
        checks = rule_based_score_code_gen(CODE_GEN_CASE, output)

    assert checks == {"tests_passed": False}
    mock_check.assert_called_once_with("", CODE_GEN_CASE.expected["test_code"])


# --- rule_based_score_summarization -----------------------------------------

SUMMARIZATION_CASE = TestCase(
    id="sum-01",
    input="The patch did not fix the leak; memory usage kept climbing.",
    expected={
        "must_include": [["did not fix", "not resolved"], ["memory"]],
        "must_exclude": ["fixed the leak", "resolved the leak"],
        "max_words": 12,
    },
)


def _summarization_output(summary: str) -> ModelOutput:
    return ModelOutput(
        test_id="sum-01", raw_text="{}", predicted={"reasoning": "x", "summary": summary},
        cost_usd=0.001, duration_ms=500,
    )


def test_rule_based_score_summarization_all_checks_pass():
    output = _summarization_output("The patch did not fix the leak; memory usage kept climbing.")
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks == {"key_facts_included": True, "no_hallucination": True, "length_ok": True}


def test_rule_based_score_summarization_matches_case_insensitively():
    output = _summarization_output("THE PATCH DID NOT FIX THE MEMORY LEAK.")
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks["key_facts_included"] is True


def test_rule_based_score_summarization_accepts_any_phrase_in_a_must_include_group():
    output = _summarization_output("The leak was not resolved; memory kept rising.")
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks["key_facts_included"] is True


def test_rule_based_score_summarization_fails_when_a_must_include_group_is_missing():
    output = _summarization_output("The patch did not fix the issue.")  # no "memory" mention
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks["key_facts_included"] is False


def test_rule_based_score_summarization_fails_on_hallucinated_phrase():
    output = _summarization_output("The patch fixed the leak; memory usage is stable.")
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks["no_hallucination"] is False


def test_rule_based_score_summarization_fails_when_over_word_limit():
    output = _summarization_output(
        "The patch did not fix the leak; memory usage kept climbing steadily over the next two days, well past the limit."
    )
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks["length_ok"] is False


def test_rule_based_score_summarization_handles_missing_summary_key():
    output = ModelOutput(test_id="sum-01", raw_text="{}", predicted={}, cost_usd=0.001, duration_ms=500)
    checks = rule_based_score_summarization(SUMMARIZATION_CASE, output)
    assert checks == {"key_facts_included": False, "no_hallucination": True, "length_ok": True}


def test_rule_based_score_summarization_no_constraints_trivially_passes():
    case = TestCase(
        id="sum-99", input="x", expected={"must_include": [], "must_exclude": [], "max_words": 100}
    )
    output = _summarization_output("anything at all")
    checks = rule_based_score_summarization(case, output)
    assert checks == {"key_facts_included": True, "no_hallucination": True, "length_ok": True}


# --- rule_based_score_code_review -------------------------------------------

CODE_REVIEW_CASE = TestCase(
    id="cr-01",
    input="def f(items):\n    for i in range(len(items) - 1):\n        ...",
    expected={
        "must_flag": [
            {"phrases": ["off-by-one", "skips the last"], "severity": "bug"},
            {"phrases": ["sql injection", "unsanitized"], "severity": "security"},
        ],
        "must_not_flag": ["style nit", "naming convention"],
    },
)


def _code_review_output(findings: list[dict]) -> ModelOutput:
    return ModelOutput(
        test_id="cr-01", raw_text="{}", predicted={"reasoning": "x", "findings": findings},
        cost_usd=0.001, duration_ms=500,
    )


def test_rule_based_score_code_review_all_checks_pass():
    output = _code_review_output([
        {"issue": "off-by-one loop bound skips the last item", "severity": "bug"},
        {"issue": "sql injection via unsanitized input", "severity": "security"},
    ])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks == {"issues_flagged": True, "severity_correct": True, "no_false_positives": True}


def test_rule_based_score_code_review_fails_recall_when_a_group_is_missing():
    output = _code_review_output([{"issue": "off-by-one loop bound", "severity": "bug"}])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks["issues_flagged"] is False


def test_rule_based_score_code_review_catches_issue_mistagged_with_wrong_severity():
    # the motivating scenario: a real security bug caught, but tagged as a
    # minor style nit - issues_flagged should still pass (it WAS found) but
    # severity_correct must fail (the tier is wrong).
    output = _code_review_output([
        {"issue": "off-by-one loop bound skips the last item", "severity": "bug"},
        {"issue": "sql injection via unsanitized input", "severity": "style"},
    ])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks["issues_flagged"] is True
    assert checks["severity_correct"] is False


def test_rule_based_score_code_review_severity_correct_fails_when_nothing_caught_at_all():
    # a model that catches zero planted issues has no severity claim to stand
    # behind - severity_correct must fail too, not report a misleading 100%
    # next to a 0% recall score.
    output = _code_review_output([])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks["issues_flagged"] is False
    assert checks["severity_correct"] is False


def test_rule_based_score_code_review_severity_correct_scored_independently_on_partial_catch():
    # one of two groups caught (and correctly tagged); the other missed
    # entirely. severity_correct should reflect only the caught group's
    # tagging, not be dragged down by the separate recall miss - that
    # distinction is what issues_flagged is for.
    output = _code_review_output([{"issue": "off-by-one loop bound skips the last item", "severity": "bug"}])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks["issues_flagged"] is False
    assert checks["severity_correct"] is True


def test_rule_based_score_code_review_fails_on_false_positive():
    output = _code_review_output([
        {"issue": "off-by-one loop bound skips the last item", "severity": "bug"},
        {"issue": "sql injection via unsanitized input", "severity": "security"},
        {"issue": "inconsistent naming convention", "severity": "style"},
    ])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks["no_false_positives"] is False


def test_rule_based_score_code_review_clean_case_with_no_findings_passes():
    case = TestCase(id="cr-99", input="x", expected={"must_flag": [], "must_not_flag": ["bug", "issue"]})
    output = _code_review_output([])
    checks = rule_based_score_code_review(case, output)
    assert checks == {"issues_flagged": True, "severity_correct": True, "no_false_positives": True}


def test_rule_based_score_code_review_handles_missing_findings_key():
    output = ModelOutput(
        test_id="cr-01", raw_text="{}", predicted={"reasoning": "x"}, cost_usd=0.001, duration_ms=500,
    )
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks == {"issues_flagged": False, "severity_correct": False, "no_false_positives": True}


def test_rule_based_score_code_review_matches_case_insensitively():
    output = _code_review_output([
        {"issue": "OFF-BY-ONE loop bound skips the last item", "severity": "BUG"},
        {"issue": "SQL INJECTION via unsanitized input", "severity": "Security"},
    ])
    checks = rule_based_score_code_review(CODE_REVIEW_CASE, output)
    assert checks == {"issues_flagged": True, "severity_correct": True, "no_false_positives": True}


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
