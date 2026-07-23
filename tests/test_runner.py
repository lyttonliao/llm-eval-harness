from unittest.mock import patch

from eval_harness.claude_cli import CliResult
from eval_harness.runner import load_cases, load_prompt, run_suite
from eval_harness.schema import TestCase


def test_load_cases_parses_real_bug_triage_fixture():
    cases = load_cases("bug_triage")

    assert len(cases) == 15
    assert all(isinstance(c, TestCase) for c in cases)
    first = cases[0]
    assert first.id == "bt-01"
    assert first.expected_severity == "medium"
    assert first.expected_category == "frontend"
    # ids are unique
    assert len({c.id for c in cases}) == len(cases)


def test_load_prompt_reads_real_prompt_file():
    text = load_prompt("v1_naive")
    assert isinstance(text, str)
    assert len(text) > 0


def _make_cases(n=2):
    return [
        TestCase(id=f"bt-0{i}", bug_report=f"report {i}", expected_severity="high", expected_category="backend")
        for i in range(1, n + 1)
    ]


def test_run_suite_error_path_records_parse_error_and_zero_cost():
    cases = _make_cases(1)
    with patch("eval_harness.runner.load_cases", return_value=cases), patch(
        "eval_harness.runner.load_prompt", return_value="sys prompt"
    ), patch(
        "eval_harness.runner.call_claude",
        return_value=CliResult(text="", cost_usd=0.0, duration_ms=0, error="rate limited"),
    ) as mock_call:
        outputs = run_suite("bug_triage", "v1_naive", model="haiku")

    mock_call.assert_called_once_with("sys prompt", "report 1", model="haiku")
    assert len(outputs) == 1
    out = outputs[0]
    assert out.test_id == "bt-01"
    assert out.parse_error == "rate limited"
    assert out.predicted_severity is None
    assert out.predicted_category is None
    assert out.cost_usd == 0.0


def test_run_suite_parse_error_path_when_output_is_not_json():
    cases = _make_cases(1)
    with patch("eval_harness.runner.load_cases", return_value=cases), patch(
        "eval_harness.runner.load_prompt", return_value="sys prompt"
    ), patch(
        "eval_harness.runner.call_claude",
        return_value=CliResult(text="not valid json", cost_usd=0.002, duration_ms=400),
    ):
        outputs = run_suite("bug_triage", "v1_naive", model="haiku")

    assert len(outputs) == 1
    out = outputs[0]
    assert out.test_id == "bt-01"
    assert out.parse_error != ""
    assert out.predicted_severity is None
    assert out.predicted_category is None
    # cost/duration are still recorded even on parse failure - call succeeded, parsing didn't
    assert out.cost_usd == 0.002
    assert out.duration_ms == 400


def test_run_suite_happy_path_parses_severity_and_category():
    cases = _make_cases(1)
    payload_text = '{"reasoning": "clear outage", "severity": "critical", "category": "backend"}'
    with patch("eval_harness.runner.load_cases", return_value=cases), patch(
        "eval_harness.runner.load_prompt", return_value="sys prompt"
    ), patch(
        "eval_harness.runner.call_claude",
        return_value=CliResult(text=payload_text, cost_usd=0.0031, duration_ms=800),
    ):
        outputs = run_suite("bug_triage", "v1_naive", model="haiku")

    assert len(outputs) == 1
    out = outputs[0]
    assert out.test_id == "bt-01"
    assert out.predicted_severity == "critical"
    assert out.predicted_category == "backend"
    assert out.cost_usd == 0.0031
    assert out.duration_ms == 800
    assert out.parse_error == ""
    assert out.raw_text == payload_text


def test_run_suite_calls_claude_once_per_case_never_hitting_real_cli():
    cases = _make_cases(3)
    payload_text = '{"reasoning": "x", "severity": "low", "category": "frontend"}'
    with patch("eval_harness.runner.load_cases", return_value=cases), patch(
        "eval_harness.runner.load_prompt", return_value="sys prompt"
    ), patch(
        "eval_harness.runner.call_claude",
        return_value=CliResult(text=payload_text, cost_usd=0.001, duration_ms=100),
    ) as mock_call:
        outputs = run_suite("bug_triage", "v1_naive", model="haiku")

    assert mock_call.call_count == 3
    assert len(outputs) == 3


def test_run_suite_dispatches_to_codex_when_provider_is_codex():
    """Regression guard for the early-binding bug found in llm-task-router:
    provider dispatch must be resolved fresh inside run_suite, not via a
    module-level dict built at import time, or this patch would silently be
    ignored and the real (unmocked) call_claude/call_codex would run."""
    cases = _make_cases(1)
    payload_text = '{"reasoning": "x", "severity": "low", "category": "frontend"}'
    with patch("eval_harness.runner.load_cases", return_value=cases), patch(
        "eval_harness.runner.load_prompt", return_value="sys prompt"
    ), patch(
        "eval_harness.runner.call_codex",
        return_value=CliResult(text=payload_text, cost_usd=0.0, duration_ms=100),
    ) as mock_codex, patch("eval_harness.runner.call_claude") as mock_claude:
        outputs = run_suite("bug_triage", "v1_naive", provider="codex", model="gpt-5")

    mock_codex.assert_called_once_with("sys prompt", "report 1", model="gpt-5")
    mock_claude.assert_not_called()
    assert len(outputs) == 1
    assert outputs[0].predicted_severity == "low"
