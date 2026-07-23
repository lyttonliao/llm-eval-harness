import pytest

from eval_harness.cli import resolve_model


def test_resolve_model_defaults_claude_to_haiku():
    assert resolve_model("claude", None) == "haiku"


def test_resolve_model_passes_through_explicit_model():
    assert resolve_model("claude", "sonnet") == "sonnet"
    assert resolve_model("codex", "gpt-5.6-terra") == "gpt-5.6-terra"


def test_resolve_model_raises_for_codex_without_explicit_model():
    """A real run without --model got silently saved to disk as
    "codex/None" - benchmark results need a known model name to be usable
    as calibration data, so this must fail loudly instead of guessing."""
    with pytest.raises(ValueError, match="--model is required"):
        resolve_model("codex", None)
