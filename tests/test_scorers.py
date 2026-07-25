from unittest.mock import patch

from eval_harness.claude_cli import CliResult
from eval_harness.schema import ModelOutput, TestCase
from eval_harness.scorers import (
    judge_score,
    rule_based_score_architecture,
    rule_based_score_bug_triage,
    rule_based_score_code_gen,
    rule_based_score_code_review,
    rule_based_score_multi_step,
    rule_based_score_refactor,
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


# --- rule_based_score_refactor ----------------------------------------------

REFACTOR_CASE = TestCase(
    id="rf-01",
    input="def f(items, seen=[]): ...",
    expected={
        "test_code": "from solution import f\n\n\ndef test_no_state_leak():\n    assert f([1]) == f([1])\n",
        "structural_checks": [
            {"type": "not_contains", "pattern": r"def \w+\([^)]*=\s*(\[\]|\{\})"},
            {"type": "max_occurrences", "pattern": r"0\.0825", "max": 1},
        ],
    },
)


def _refactor_output(code: str) -> ModelOutput:
    return ModelOutput(test_id="rf-01", raw_text="{}", predicted={"code": code}, cost_usd=0.001, duration_ms=500)


def test_rule_based_score_refactor_all_checks_pass():
    code = "TAX_RATE = 0.0825\n\n\ndef f(items, seen=None):\n    if seen is None:\n        seen = []\n    return items\n"
    output = _refactor_output(code)
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(True, "1 passed")):
        checks = rule_based_score_refactor(REFACTOR_CASE, output)
    assert checks == {"tests_passed": True, "smells_removed": True}


def test_rule_based_score_refactor_fails_tests_passed_when_sandbox_reports_failure():
    output = _refactor_output("def f(items, seen=None):\n    return items\n")
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(False, "1 failed")):
        checks = rule_based_score_refactor(REFACTOR_CASE, output)
    assert checks["tests_passed"] is False


def test_rule_based_score_refactor_fails_smells_removed_when_mutable_default_still_present():
    output = _refactor_output("def f(items, seen=[]):\n    return items\n")
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(True, "1 passed")):
        checks = rule_based_score_refactor(REFACTOR_CASE, output)
    assert checks["smells_removed"] is False


def test_rule_based_score_refactor_fails_smells_removed_when_magic_number_still_duplicated():
    code = "def f(items, seen=None):\n    a = 0.0825\n    b = 0.0825\n    return items\n"
    output = _refactor_output(code)
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(True, "1 passed")):
        checks = rule_based_score_refactor(REFACTOR_CASE, output)
    assert checks["smells_removed"] is False


def test_rule_based_score_refactor_no_structural_checks_trivially_passes():
    case = TestCase(id="rf-99", input="x", expected={"test_code": "def test_x():\n    assert True\n"})
    output = _refactor_output("anything at all, even code containing 0.0825 twice 0.0825")
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(True, "1 passed")):
        checks = rule_based_score_refactor(case, output)
    assert checks == {"tests_passed": True, "smells_removed": True}


def test_rule_based_score_refactor_handles_missing_code_key():
    output = ModelOutput(test_id="rf-01", raw_text="{}", predicted={"reasoning": "x"}, cost_usd=0.001, duration_ms=500)
    with patch("eval_harness.scorers.sandbox.run_pytest_check", return_value=(False, "collection error")) as mock_check:
        checks = rule_based_score_refactor(REFACTOR_CASE, output)
    assert checks["tests_passed"] is False
    mock_check.assert_called_once_with("", REFACTOR_CASE.expected["test_code"])


# --- rule_based_score_multi_step --------------------------------------------

MULTI_STEP_CASE = TestCase(
    id="ms-01",
    input="Plan a migration of a status column, given read/write traffic must not be interrupted.",
    expected={
        "required_steps": [
            {"phrases": ["backfill", "backfilling"]},
            {"phrases": ["cutover", "cut over"]},
            {"phrases": ["rollback", "kill switch"]},
        ],
        "ordering_constraints": [[0, 1]],
        "must_not_include": ["drop the old table", "delete legacy table"],
    },
)


def _multi_step_output(steps: list[dict]) -> ModelOutput:
    return ModelOutput(
        test_id="ms-01", raw_text="{}", predicted={"reasoning": "x", "steps": steps},
        cost_usd=0.001, duration_ms=500,
    )


def test_rule_based_score_multi_step_all_checks_pass():
    output = _multi_step_output([
        {"phase": "backfill", "detail": "backfill historical data into the new column"},
        {"phase": "cutover", "detail": "cut over reads to the new column"},
        {"phase": "rollback", "detail": "keep a rollback plan ready"},
    ])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks == {"step_coverage": True, "ordering_correct": True, "no_false_positives": True}


def test_rule_based_score_multi_step_fails_coverage_when_a_group_is_missing():
    output = _multi_step_output([
        {"phase": "backfill", "detail": "backfill historical data"},
        {"phase": "cutover", "detail": "cut over reads"},
    ])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks["step_coverage"] is False


def test_rule_based_score_multi_step_fails_ordering_when_steps_are_reversed():
    output = _multi_step_output([
        {"phase": "cutover", "detail": "cut over reads"},
        {"phase": "backfill", "detail": "backfill historical data"},
        {"phase": "rollback", "detail": "rollback ready"},
    ])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks["step_coverage"] is True
    assert checks["ordering_correct"] is False


def test_rule_based_score_multi_step_ordering_fails_outright_when_no_constraint_is_evaluable():
    # empty plan: neither group in the ordering constraint was matched at all -
    # there's no order to judge, so ordering_correct should not vacuously pass
    # just because nothing contradicted it (same precedent as code_review's
    # severity_correct failing outright when nothing was caught at all).
    output = _multi_step_output([])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks["step_coverage"] is False
    assert checks["ordering_correct"] is False


def test_rule_based_score_multi_step_ordering_scored_independently_on_partial_coverage():
    # one ordering constraint is evaluable and satisfied; a second constraint
    # references a group that was never produced at all. ordering_correct should
    # reflect only the evaluable constraint, not be dragged down by the separate
    # coverage miss - that distinction is what step_coverage is for.
    case = TestCase(
        id="ms-02",
        input="x",
        expected={
            "required_steps": [
                {"phrases": ["backfill"]},
                {"phrases": ["cutover"]},
                {"phrases": ["monitor"]},
            ],
            "ordering_constraints": [[0, 1], [1, 2]],
            "must_not_include": [],
        },
    )
    output = _multi_step_output([
        {"phase": "backfill", "detail": "backfill historical data"},
        {"phase": "cutover", "detail": "cut over reads"},
    ])
    checks = rule_based_score_multi_step(case, output)
    assert checks["step_coverage"] is False
    assert checks["ordering_correct"] is True


def test_rule_based_score_multi_step_fails_on_false_positive():
    output = _multi_step_output([
        {"phase": "backfill", "detail": "backfill historical data"},
        {"phase": "cutover", "detail": "cut over reads"},
        {"phase": "cleanup", "detail": "drop the old table immediately after cutover"},
        {"phase": "rollback", "detail": "rollback ready"},
    ])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks["no_false_positives"] is False


def test_rule_based_score_multi_step_no_constraints_trivially_passes():
    case = TestCase(
        id="ms-99", input="x",
        expected={"required_steps": [], "ordering_constraints": [], "must_not_include": ["bug", "issue"]},
    )
    output = _multi_step_output([])
    checks = rule_based_score_multi_step(case, output)
    assert checks == {"step_coverage": True, "ordering_correct": True, "no_false_positives": True}


def test_rule_based_score_multi_step_handles_missing_steps_key():
    output = ModelOutput(test_id="ms-01", raw_text="{}", predicted={"reasoning": "x"}, cost_usd=0.001, duration_ms=500)
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks == {"step_coverage": False, "ordering_correct": False, "no_false_positives": True}


def test_rule_based_score_multi_step_matches_case_insensitively():
    output = _multi_step_output([
        {"phase": "BACKFILL", "detail": "Backfill historical data"},
        {"phase": "Cutover", "detail": "CUT OVER reads"},
        {"phase": "Rollback", "detail": "Rollback plan ready"},
    ])
    checks = rule_based_score_multi_step(MULTI_STEP_CASE, output)
    assert checks == {"step_coverage": True, "ordering_correct": True, "no_false_positives": True}


# --- rule_based_score_architecture ------------------------------------------

ARCHITECTURE_CASE = TestCase(
    id="ar-01",
    input="Design a rate limiter for a public API across multiple server instances.",
    expected={
        "must_include": [["shared store", "redis", "centralized store"]],
        "must_not_include": ["single instance", "local in-memory counter"],
        "min_alternatives": 1,
    },
)


def _architecture_output(design: str, reasoning: str = "x", alternatives: list[dict] | None = None) -> ModelOutput:
    return ModelOutput(
        test_id="ar-01", raw_text="{}",
        predicted={"reasoning": reasoning, "design": design, "alternatives_considered": alternatives or []},
        cost_usd=0.001, duration_ms=500,
    )


def test_rule_based_score_architecture_all_checks_pass():
    output = _architecture_output(
        design="Use a shared Redis store to track request counts per API key across all instances.",
        alternatives=[{"option": "local in-memory counter per instance", "rejected_because": "can't enforce a consistent limit across instances"}],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks == {"key_considerations_addressed": True, "no_anti_patterns": True, "tradeoffs_articulated": True}


def test_rule_based_score_architecture_fails_key_considerations_when_missing():
    output = _architecture_output(
        design="Use a load balancer with round-robin routing across instances.",
        alternatives=[{"option": "sticky sessions", "rejected_because": "adds complexity and doesn't solve the limit consistency problem"}],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks["key_considerations_addressed"] is False


def test_rule_based_score_architecture_fails_no_anti_patterns_when_bait_in_design():
    output = _architecture_output(
        design="Use a local in-memory counter per instance, backed by a shared store for cross-instance sync.",
        alternatives=[{"option": "x", "rejected_because": "y"}],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks["no_anti_patterns"] is False


def test_rule_based_score_architecture_ignores_bait_that_only_appears_in_alternatives():
    # the bait phrases only show up inside a REJECTED alternative, explaining why it
    # wasn't chosen - naming a bad approach in order to reject it shouldn't fail
    # no_anti_patterns the way actually proposing it in `design` would.
    output = _architecture_output(
        design="Use a shared Redis store to track request counts per API key across all instances.",
        alternatives=[{
            "option": "single instance with a local in-memory counter",
            "rejected_because": "would not enforce a consistent limit across instances and becomes a single point of failure",
        }],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks["no_anti_patterns"] is True


def test_rule_based_score_architecture_tradeoffs_articulated_fails_below_min():
    output = _architecture_output(
        design="Use a shared Redis store to track request counts per API key across all instances.",
        alternatives=[],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks["tradeoffs_articulated"] is False


def test_rule_based_score_architecture_ignores_alternatives_with_empty_rejection_reason():
    output = _architecture_output(
        design="Use a shared Redis store to track request counts per API key across all instances.",
        alternatives=[{"option": "local in-memory counter", "rejected_because": ""}],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks["tradeoffs_articulated"] is False


def test_rule_based_score_architecture_dedupes_near_identical_option_text():
    case = TestCase(id="ar-11", input="x", expected={"must_include": [], "must_not_include": [], "min_alternatives": 2})
    output = _architecture_output(
        design="anything",
        alternatives=[
            {"option": "Local in-memory counter", "rejected_because": "doesn't work across instances"},
            {"option": "local in-memory counter", "rejected_because": "same issue restated"},
        ],
    )
    checks = rule_based_score_architecture(case, output)
    assert checks["tradeoffs_articulated"] is False  # only 1 distinct option despite 2 entries


def test_rule_based_score_architecture_no_constraints_trivially_passes():
    case = TestCase(id="ar-99", input="x", expected={"must_include": [], "must_not_include": ["bug"], "min_alternatives": 0})
    output = _architecture_output(design="anything at all", alternatives=[])
    checks = rule_based_score_architecture(case, output)
    assert checks == {"key_considerations_addressed": True, "no_anti_patterns": True, "tradeoffs_articulated": True}


def test_rule_based_score_architecture_handles_missing_keys():
    output = ModelOutput(test_id="ar-01", raw_text="{}", predicted={"reasoning": "x"}, cost_usd=0.001, duration_ms=500)
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks == {"key_considerations_addressed": False, "no_anti_patterns": True, "tradeoffs_articulated": False}


def test_rule_based_score_architecture_matches_case_insensitively():
    output = _architecture_output(
        design="Use a Shared REDIS store to track request counts.",
        alternatives=[{"option": "Local In-Memory Counter", "rejected_because": "Doesn't scale across instances"}],
    )
    checks = rule_based_score_architecture(ARCHITECTURE_CASE, output)
    assert checks == {"key_considerations_addressed": True, "no_anti_patterns": True, "tradeoffs_articulated": True}


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
