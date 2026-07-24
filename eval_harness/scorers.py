"""Two different kinds of check, on purpose:

- rule_based_score_*: hard, deterministic - did the answer match the golden set.
- judge_score: soft - even when the answer happens to match, is the model's
  stated reasoning actually grounded, or a lucky guess? Real eval suites need
  both; exact-match alone hides "right answer, no idea why."

Suite-specific rule scoring lives behind _RULE_SCORERS since "correct" means
something different per suite (label match vs. tests passing), but judging
reasoning coherence doesn't change shape across suites, so there's only one
judge_score.
"""

import json

from eval_harness import sandbox
from eval_harness.claude_cli import call_claude
from eval_harness.jsonutil import extract_json
from eval_harness.schema import ModelOutput, ScoredResult, TestCase

_JUDGE_SYSTEM_PROMPT = """You are grading another model's stated reasoning for a task, not whether its final answer is correct.

Given a task, the model's stated one-sentence reasoning, and the answer it produced, score whether the \
reasoning is internally coherent and actually grounded in specific details from the task - as opposed to \
generic or unsupported. A model can produce the "right" answer with reasoning that doesn't actually justify \
it; that should score low. A model can also give solid reasoning for a defensible but different answer than \
expected; that should score high.

Respond with only a JSON object, no prose, no markdown fences:
{"coherence_score": <float 0.0-1.0>, "rationale": "<one sentence on why>"}
"""


def rule_based_score_bug_triage(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    severity_ok = (output.predicted.get("severity") or "").strip().lower() == case.expected["severity"].lower()
    category_ok = (output.predicted.get("category") or "").strip().lower() == case.expected["category"].lower()
    return {"severity": severity_ok, "category": category_ok}


def rule_based_score_code_gen(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    passed, _detail = sandbox.run_pytest_check(output.predicted.get("code", ""), case.expected["test_code"])
    return {"tests_passed": passed}


_RULE_SCORERS = {
    "bug_triage": rule_based_score_bug_triage,
    "code_gen": rule_based_score_code_gen,
}


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

    answer = {k: v for k, v in output.predicted.items() if k != "reasoning"}
    user_message = (
        f"Task: {case.input}\n\n"
        f"Model's reasoning: {reasoning!r}\n"
        f"Model's answer: {answer!r}"
    )

    result = call_claude(_JUDGE_SYSTEM_PROMPT, user_message, model=judge_model)
    if result.error:
        return 0.0, f"judge call failed: {result.error}"

    try:
        parsed = extract_json(result.text)
        return float(parsed.get("coherence_score", 0.0)), parsed.get("rationale", "")
    except (json.JSONDecodeError, ValueError):
        return 0.0, "judge returned unparseable output"


def score_all(suite: str, cases: list[TestCase], outputs: list[ModelOutput], run_judge: bool = True) -> list[ScoredResult]:
    rule_score = _RULE_SCORERS[suite]
    outputs_by_id = {o.test_id: o for o in outputs}
    results = []

    for i, case in enumerate(cases, 1):
        output = outputs_by_id.get(case.id)
        if output is None:
            continue

        checks = rule_score(case, output)

        judge_val, judge_rationale = (0.0, "judging disabled")
        if run_judge:
            print(f"  judging [{i}/{len(cases)}] {case.id}...", end=" ", flush=True)
            judge_val, judge_rationale = judge_score(case, output)
            print(f"{judge_val:.2f}")

        results.append(
            ScoredResult(
                test_id=case.id,
                predicted=output.predicted,
                checks=checks,
                judge_score=judge_val,
                judge_rationale=judge_rationale,
                cost_usd=output.cost_usd,
                duration_ms=output.duration_ms,
                token_usage=output.token_usage,
            )
        )

    return results
