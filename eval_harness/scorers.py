"""Two different kinds of check, on purpose:

- rule_based_score: hard, deterministic - did the label match the golden set.
- judge_score: soft - even when the label happens to match, is the model's
  stated reasoning actually grounded in the report, or a lucky guess? Real
  eval suites need both; exact-match alone hides "right answer, no idea why."
"""

import json

from eval_harness.claude_cli import call_claude
from eval_harness.jsonutil import extract_json
from eval_harness.schema import ModelOutput, ScoredResult, TestCase

_JUDGE_SYSTEM_PROMPT = """You are grading another model's bug-triage reasoning, not its final label.

Given a bug report and the model's stated one-sentence reasoning plus the severity/category it chose, \
score whether the reasoning is internally coherent and actually grounded in specific details from the \
report - as opposed to generic or unsupported. A model can state the "right" label with reasoning that \
doesn't actually justify it; that should score low. A model can also give solid reasoning for a defensible \
but different call than expected; that should score high.

Respond with only a JSON object, no prose, no markdown fences:
{"coherence_score": <float 0.0-1.0>, "rationale": "<one sentence on why>"}
"""


def rule_based_score(case: TestCase, output: ModelOutput) -> tuple[bool, bool]:
    severity_ok = (output.predicted_severity or "").strip().lower() == case.expected_severity.lower()
    category_ok = (output.predicted_category or "").strip().lower() == case.expected_category.lower()
    return severity_ok, category_ok


def judge_score(case: TestCase, output: ModelOutput, judge_model: str = "haiku") -> tuple[float, str]:
    """The judge call below is deliberately always call_claude, regardless of
    which provider produced `output` (see codex_cli.py/runner.py's provider
    param). Judging Codex output with a different model lineage than the one
    under test is intentional, not a missed parameterization - a same-family
    judge risks correlated errors (the model under test and its judge share
    training data/lineage and can converge on the same wrong answer)."""
    if output.parse_error:
        return 0.0, f"skipped judging: model output failed to parse ({output.parse_error})"

    try:
        reasoning = extract_json(output.raw_text).get("reasoning", "")
    except json.JSONDecodeError:
        reasoning = ""

    user_message = (
        f"Bug report: {case.bug_report}\n\n"
        f"Model's reasoning: {reasoning!r}\n"
        f"Model's severity: {output.predicted_severity}\n"
        f"Model's category: {output.predicted_category}"
    )

    result = call_claude(_JUDGE_SYSTEM_PROMPT, user_message, model=judge_model)
    if result.error:
        return 0.0, f"judge call failed: {result.error}"

    try:
        parsed = extract_json(result.text)
        return float(parsed.get("coherence_score", 0.0)), parsed.get("rationale", "")
    except (json.JSONDecodeError, ValueError):
        return 0.0, "judge returned unparseable output"


def score_all(cases: list[TestCase], outputs: list[ModelOutput], run_judge: bool = True) -> list[ScoredResult]:
    outputs_by_id = {o.test_id: o for o in outputs}
    results = []

    for i, case in enumerate(cases, 1):
        output = outputs_by_id.get(case.id)
        if output is None:
            continue

        severity_ok, category_ok = rule_based_score(case, output)

        judge_val, judge_rationale = (0.0, "judging disabled")
        if run_judge:
            print(f"  judging [{i}/{len(cases)}] {case.id}...", end=" ", flush=True)
            judge_val, judge_rationale = judge_score(case, output)
            print(f"{judge_val:.2f}")

        results.append(
            ScoredResult(
                test_id=case.id,
                predicted_severity=output.predicted_severity,
                predicted_category=output.predicted_category,
                severity_correct=severity_ok,
                category_correct=category_ok,
                judge_score=judge_val,
                judge_rationale=judge_rationale,
                cost_usd=output.cost_usd,
                duration_ms=output.duration_ms,
            )
        )

    return results
