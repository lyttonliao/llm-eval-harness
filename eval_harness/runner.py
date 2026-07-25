import json
from pathlib import Path

from eval_harness.claude_cli import call_claude
from eval_harness.codex_cli import call_codex
from eval_harness.jsonutil import extract_json
from eval_harness.schema import ModelOutput, TestCase

CASES_DIR = Path(__file__).parent / "cases"
PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_cases(suite: str = "bug_triage") -> list[TestCase]:
    path = CASES_DIR / f"{suite}.jsonl"
    cases = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            cases.append(TestCase(**json.loads(line)))
    return cases


def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"{version}.txt").read_text()


def run_suite(
    suite: str, prompt_version: str, provider: str = "claude", model: str = "haiku", samples: int = 1
) -> list[ModelOutput]:
    """samples > 1 calls the model `samples` times per case instead of once,
    appending all of them to the flat return list (same test_id repeated) -
    see scorers.score_all, which groups by test_id rather than deduping, so
    N outputs per case become N ScoredResults per case. Built for multi_step
    (see CLAUDE.md's "Router tier calibration status" - single-sample
    check_accuracies can't distinguish a real miss from that sample's
    wording), not wired into any other suite yet."""
    cases = load_cases(suite)
    system_prompt = load_prompt(prompt_version)
    outputs = []

    # Resolved fresh on every call (not a module-level dict built once at
    # import time) so that patch("eval_harness.runner.call_claude"/"call_codex")
    # in tests actually takes effect - a dict built at import time would
    # capture the pre-patch function reference and silently ignore the mock.
    call_model = call_claude if provider == "claude" else call_codex

    for i, case in enumerate(cases, 1):
        for sample in range(1, samples + 1):
            label = f"  [{i}/{len(cases)}] {case.id}"
            if samples > 1:
                label += f" (sample {sample}/{samples})"
            print(f"{label}...", end=" ", flush=True)
            result = call_model(system_prompt, case.input, model=model)

            if result.error:
                print(f"ERROR: {result.error}")
                outputs.append(
                    ModelOutput(
                        test_id=case.id, raw_text=result.text, predicted={},
                        cost_usd=result.cost_usd,
                        duration_ms=result.duration_ms,
                        token_usage=result.token_usage,
                        parse_error=result.error,
                    )
                )
                continue

            try:
                parsed = extract_json(result.text)
                outputs.append(
                    ModelOutput(
                        test_id=case.id,
                        raw_text=result.text,
                        predicted=parsed,
                        cost_usd=result.cost_usd,
                        duration_ms=result.duration_ms,
                        token_usage=result.token_usage,
                    )
                )
                token_count = result.token_usage.get("total_tokens")
                token_label = f", {token_count} tokens" if token_count is not None else ""
                print(f"${result.cost_usd:.4f}{token_label}")
            except (json.JSONDecodeError, AttributeError) as e:
                print(f"PARSE ERROR: {e}")
                outputs.append(
                    ModelOutput(
                        test_id=case.id, raw_text=result.text, predicted={},
                        cost_usd=result.cost_usd, duration_ms=result.duration_ms,
                        token_usage=result.token_usage,
                        parse_error=str(e),
                    )
                )

    return outputs
