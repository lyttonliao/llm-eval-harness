import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from eval_harness.codex_cli import call_codex


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr)


def _token_count_event(**usage: int) -> str:
    return json.dumps({"payload": {"info": {"total_token_usage": usage}}})


def test_builds_expected_command_with_concatenated_prompt_and_readonly_sandbox():
    """codex exec has no --system-prompt flag, unlike claude -p, so
    system_prompt/user_message get concatenated into one prompt string.
    --sandbox read-only is the closest analog to claude_cli.py's
    --disallowed-tools "*"; --ask-for-approval is deliberately absent since
    it's invalid on `codex exec` (a real call failed with "unexpected
    argument" when it was passed)."""
    with patch("eval_harness.codex_cli.subprocess.run", return_value=_completed()) as mock_run:
        call_codex("sys prompt", "user message", model="gpt-5")

    args, kwargs = mock_run.call_args
    cmd = args[0]
    assert cmd[:3] == ["codex", "exec", "sys prompt\n\nuser message"]
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5"
    assert "--sandbox" in cmd and cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ask-for-approval" not in cmd
    assert "--json" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "--output-last-message" in cmd
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 60
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_omits_model_flag_when_model_is_none():
    """Valid model names are account-dependent (a guessed name 400'd on a
    real account) - omit --model entirely rather than guess, letting codex
    fall back to its own configured default."""
    with patch("eval_harness.codex_cli.subprocess.run", return_value=_completed()) as mock_run:
        call_codex("sys", "msg", model=None)

    cmd = mock_run.call_args.args[0]
    assert "--model" not in cmd


def test_success_path_reads_text_from_output_last_message_file_and_records_token_usage():
    def fake_run(cmd, **kwargs):
        path = cmd[cmd.index("--output-last-message") + 1]
        with open(path, "w") as f:
            f.write("the answer\n")
        return _completed(
            stdout="\n".join(
                [
                    json.dumps({"type": "other"}),
                    _token_count_event(input_tokens=100, cached_input_tokens=40, output_tokens=15, total_tokens=115),
                ]
            )
        )

    with patch("eval_harness.codex_cli.subprocess.run", side_effect=fake_run):
        result = call_codex("sys", "msg", model="gpt-5")

    assert result.text == "the answer"
    assert result.error == ""
    assert result.cost_usd == 0.0
    assert result.duration_ms >= 0
    assert result.token_usage == {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 15,
        "total_tokens": 115,
    }


def test_token_usage_uses_last_cumulative_event_and_ignores_malformed_json():
    events = "\n".join(
        [
            _token_count_event(input_tokens=10, output_tokens=1, total_tokens=11),
            "not json",
            _token_count_event(input_tokens=30, output_tokens=4, reasoning_output_tokens=2, total_tokens=34),
        ]
    )
    with patch("eval_harness.codex_cli.subprocess.run", return_value=_completed(stdout=events)):
        result = call_codex("sys", "msg", model="gpt-5")

    assert result.token_usage == {
        "input_tokens": 30,
        "output_tokens": 4,
        "reasoning_output_tokens": 2,
        "total_tokens": 34,
    }


def test_last_message_file_is_cleaned_up_after_success():
    captured_path = {}

    def fake_run(cmd, **kwargs):
        path = cmd[cmd.index("--output-last-message") + 1]
        captured_path["path"] = path
        with open(path, "w") as f:
            f.write("the answer")
        return _completed()

    with patch("eval_harness.codex_cli.subprocess.run", side_effect=fake_run):
        call_codex("sys", "msg", model="gpt-5")

    assert not Path(captured_path["path"]).exists()


def test_nonzero_exit_code_returns_error_from_stderr():
    with patch(
        "eval_harness.codex_cli.subprocess.run",
        return_value=_completed(returncode=1, stderr="auth error: not logged in"),
    ):
        result = call_codex("sys", "msg", model="gpt-5")

    assert result.error == "auth error: not logged in"
    assert result.text == ""


def test_invalid_model_name_returns_error_from_real_observed_failure_shape():
    """Regression guard for a real failure seen against a live account: an
    unsupported model name gets a non-zero exit with no output file, and the
    API's 400 invalid_request_error surfaced on stderr."""
    stderr = (
        'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"The \'not-a-real-model\' model is not supported when using Codex '
        'with a ChatGPT account."}}'
    )
    with patch(
        "eval_harness.codex_cli.subprocess.run",
        return_value=_completed(returncode=1, stderr=stderr),
    ):
        result = call_codex("sys", "msg", model="not-a-real-model")

    assert result.text == ""
    assert "not supported" in result.error


def test_timeout_expired_returns_timeout_error():
    with patch(
        "eval_harness.codex_cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=60),
    ):
        result = call_codex("sys", "msg", model="gpt-5")

    assert result.error == "timeout"
    assert result.duration_ms == 60_000


def test_missing_output_file_returns_empty_text():
    with patch("eval_harness.codex_cli.subprocess.run", return_value=_completed()):
        result = call_codex("sys", "msg", model="gpt-5")

    assert result.text == ""
    assert result.error == ""


def test_no_system_prompt_uses_user_message_alone():
    with patch("eval_harness.codex_cli.subprocess.run", return_value=_completed()) as mock_run:
        call_codex("", "just the task", model="gpt-5")

    cmd = mock_run.call_args.args[0]
    assert cmd[2] == "just the task"
