"""Thin wrapper around headless `codex exec`, OpenAI Codex CLI's
non-interactive mode - the ChatGPT-subscription equivalent of `claude -p`.

Verified against a real install and a real authenticated call (codex-cli
0.145.0, ChatGPT-account login) while building llm-task-router:
- `codex exec` has no `--system-prompt` flag, so system_prompt and
  user_message are concatenated into one prompt string instead of passed
  separately like claude_cli.call_claude does.
- No dollar-cost field exists anywhere in its output, so cost_usd stays
  0.0 here. `--json` does emit token-count events, which are retained as
  token_usage. Unlike claude_cli.py, duration_ms is self-measured with
  time.perf_counter() around the call, since nothing else reports it.
- `--ask-for-approval` is a top-level `codex` option, not a valid `codex
  exec` flag (a real call fails with "unexpected argument" if you pass
  it) - exec auto-defaults to never-ask on its own. `--sandbox read-only`
  is the closest analog to claude_cli.py's --disallowed-tools "*", but
  not equivalent: the model can still run read-only shell commands to
  gather context before answering.
- Valid model names are account-dependent (a guessed name got a real 400
  invalid_request_error on this account) - when model is None, --model is
  omitted entirely so Codex falls back to its own configured default,
  rather than guessing a name that might not exist on the caller's plan.
"""

import json
import subprocess
import tempfile
import time
from pathlib import Path

from eval_harness.claude_cli import CliResult


def _extract_token_usage(events: str) -> dict[str, int]:
    """Return the final accumulated usage from Codex's JSONL event stream."""
    token_usage: dict[str, int] = {}
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("payload", {}).get("info", {}).get("total_token_usage")
        if not isinstance(usage, dict):
            continue
        parsed_usage = {
            name: value
            for name, value in usage.items()
            if isinstance(name, str) and isinstance(value, int) and not isinstance(value, bool)
        }
        if parsed_usage:
            token_usage = parsed_usage
    return token_usage


def call_codex(system_prompt: str, user_message: str, model: str | None = None) -> CliResult:
    prompt = f"{system_prompt}\n\n{user_message}" if system_prompt else user_message

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        last_message_path = Path(tmp.name)

    cmd = ["codex", "exec", prompt]
    if model is not None:
        cmd += ["--model", model]
    cmd += [
        "--json",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message_path),
    ]

    start = time.perf_counter()
    try:
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            return CliResult(text="", cost_usd=0.0, duration_ms=120_000, error="timeout")

        duration_ms = int((time.perf_counter() - start) * 1000)

        if proc.returncode != 0:
            return CliResult(
                text="",
                cost_usd=0.0,
                duration_ms=duration_ms,
                error=proc.stderr.strip() or "nonzero exit",
                token_usage=_extract_token_usage(proc.stdout),
            )

        text = last_message_path.read_text().strip() if last_message_path.exists() else ""
        return CliResult(
            text=text,
            cost_usd=0.0,
            duration_ms=duration_ms,
            token_usage=_extract_token_usage(proc.stdout),
        )
    finally:
        last_message_path.unlink(missing_ok=True)
