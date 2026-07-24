"""Thin wrapper around headless `claude -p` so we run on the user's existing
Claude Code subscription instead of a separately-billed API key.

Each call strips the default Claude Code system prompt and disables tools/MCP
so we're paying for (and measuring) a plain single-turn completion, not a full
agent turn - see the cost difference this makes: ~$0.07/call with the default
system prompt vs ~$0.003/call stripped down, for the exact same question.
"""

import json
import subprocess
from dataclasses import dataclass, field


@dataclass
class CliResult:
    text: str
    cost_usd: float
    duration_ms: int
    error: str = ""
    token_usage: dict[str, int] = field(default_factory=dict)


def call_claude(system_prompt: str, user_message: str, model: str = "haiku") -> CliResult:
    cmd = [
        "claude",
        "-p",
        user_message,
        "--system-prompt",
        system_prompt,
        "--disallowed-tools",
        "*",
        "--strict-mcp-config",
        "--model",
        model,
        "--output-format",
        "json",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return CliResult(text="", cost_usd=0.0, duration_ms=60_000, error="timeout")

    if proc.returncode != 0:
        return CliResult(text="", cost_usd=0.0, duration_ms=0, error=proc.stderr.strip() or "nonzero exit")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return CliResult(text=proc.stdout, cost_usd=0.0, duration_ms=0, error="could not parse CLI json output")

    if payload.get("is_error"):
        return CliResult(text="", cost_usd=0.0, duration_ms=0, error=payload.get("result", "unknown CLI error"))

    return CliResult(
        text=payload.get("result", ""),
        cost_usd=payload.get("total_cost_usd", 0.0),
        duration_ms=payload.get("duration_ms", 0),
    )
