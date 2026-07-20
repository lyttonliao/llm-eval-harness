import json
from pathlib import Path

from eval_harness.claude_cli import call_claude
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


def run_suite(suite: str, prompt_version: str, model: str = "haiku") -> list[ModelOutput]:
    cases = load_cases(suite)
    system_prompt = load_prompt(prompt_version)
    outputs = []

    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case.id}...", end=" ", flush=True)
        result = call_claude(system_prompt, case.bug_report, model=model)

        if result.error:
            print(f"ERROR: {result.error}")
            outputs.append(
                ModelOutput(
                    test_id=case.id, raw_text=result.text, predicted_severity=None,
                    predicted_category=None, cost_usd=0.0, duration_ms=0,
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
                    predicted_severity=parsed.get("severity"),
                    predicted_category=parsed.get("category"),
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                )
            )
            print(f"${result.cost_usd:.4f}")
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"PARSE ERROR: {e}")
            outputs.append(
                ModelOutput(
                    test_id=case.id, raw_text=result.text, predicted_severity=None,
                    predicted_category=None, cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms, parse_error=str(e),
                )
            )

    return outputs
