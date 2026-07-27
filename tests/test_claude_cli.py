import json
import subprocess
from unittest.mock import patch

from eval_harness.claude_cli import call_claude


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_builds_expected_command_with_cost_guardrail_flags():
    """--disallowed-tools "*" and --strict-mcp-config strip the default Claude
    Code system prompt (see CLAUDE.md: ~$0.07/call without them vs ~$0.003-0.005
    with). A regression here silently reintroduces that cost - assert the exact
    command list, not just that subprocess.run was called."""
    payload = json.dumps({"result": "ok", "total_cost_usd": 0.001, "duration_ms": 500})
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout=payload)) as mock_run:
        call_claude("sys prompt", "user message", model="haiku")

    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd == [
        "claude",
        "-p",
        "user message",
        "--system-prompt",
        "sys prompt",
        "--disallowed-tools",
        "*",
        "--strict-mcp-config",
        "--model",
        "haiku",
        "--output-format",
        "json",
    ]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 240


def test_success_path_parses_cost_duration_and_result():
    payload = json.dumps({"result": "the answer", "total_cost_usd": 0.0042, "duration_ms": 1234})
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout=payload)):
        result = call_claude("sys", "msg")

    assert result.text == "the answer"
    assert result.cost_usd == 0.0042
    assert result.duration_ms == 1234
    assert result.error == ""


def test_nonzero_exit_code_returns_error_from_stderr():
    with patch(
        "eval_harness.claude_cli.subprocess.run",
        return_value=_completed(returncode=1, stderr="auth error: not logged in"),
    ):
        result = call_claude("sys", "msg")

    assert result.error == "auth error: not logged in"
    assert result.text == ""
    assert result.cost_usd == 0.0
    assert result.duration_ms == 0


def test_nonzero_exit_code_with_empty_stderr_uses_fallback_message():
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(returncode=1, stderr="")):
        result = call_claude("sys", "msg")

    assert result.error == "nonzero exit"


def test_timeout_expired_returns_timeout_error():
    with patch(
        "eval_harness.claude_cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=240),
    ):
        result = call_claude("sys", "msg")

    assert result.error == "timeout"
    assert result.duration_ms == 240_000
    assert result.cost_usd == 0.0
    assert result.text == ""


def test_stdout_not_valid_json_returns_parse_error():
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout="not json at all")):
        result = call_claude("sys", "msg")

    assert result.error == "could not parse CLI json output"
    assert result.text == "not json at all"
    assert result.cost_usd == 0.0


def test_payload_is_error_true_returns_error_from_result_field():
    payload = json.dumps({"is_error": True, "result": "rate limited"})
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout=payload)):
        result = call_claude("sys", "msg")

    assert result.error == "rate limited"
    assert result.text == ""


def test_payload_is_error_true_without_result_uses_fallback_message():
    payload = json.dumps({"is_error": True})
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout=payload)):
        result = call_claude("sys", "msg")

    assert result.error == "unknown CLI error"


def test_payload_missing_optional_fields_defaults_gracefully():
    payload = json.dumps({"result": "ok"})
    with patch("eval_harness.claude_cli.subprocess.run", return_value=_completed(stdout=payload)):
        result = call_claude("sys", "msg")

    assert result.text == "ok"
    assert result.cost_usd == 0.0
    assert result.duration_ms == 0
    assert result.error == ""
