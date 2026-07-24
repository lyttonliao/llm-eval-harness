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


def rule_based_score_summarization(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    """No golden summary to string-match against, so "correct" is expressed as
    fact-level constraints instead: groups of acceptable phrasings that must
    appear (any phrasing in a group counts - summaries paraphrase), strings
    that must not appear (hallucinated or reversed facts), and a word-count
    ceiling. All three checks are always present (even when a case's
    must_include/must_exclude is empty) so check_accuracies aggregates the
    same key set across every case, matching bug_triage/code_gen."""
    summary = (output.predicted.get("summary") or "").lower()

    key_facts_included = all(
        any(phrase.lower() in summary for phrase in group)
        for group in case.expected["must_include"]
    )
    no_hallucination = not any(
        phrase.lower() in summary for phrase in case.expected["must_exclude"]
    )
    length_ok = len(summary.split()) <= case.expected["max_words"]

    return {
        "key_facts_included": key_facts_included,
        "no_hallucination": no_hallucination,
        "length_ok": length_ok,
    }


def rule_based_score_code_review(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    """No golden review to string-match, so grading is fact-constraint style like
    summarization: must_flag is a list of groups (planted issues), each group a list
    of acceptable phrasings plus the expected severity tier - any phrasing in a group
    counts as catching that issue (reviews paraphrase). must_not_flag is a flat list of
    substrings that must not appear in any finding - red herrings / false-positive bait.

    issues_flagged (pure recall - was the issue caught, regardless of severity) and
    severity_correct (was the tag right, for whatever WAS caught) answer different
    questions, but severity_correct is NOT vacuously true just because nothing was
    caught: a model that catches zero of the planted issues has no severity claim to
    stand behind, so that case fails severity_correct too, instead of reporting a
    misleading 100% next to a 0% recall score. Partial catches are still scored on
    their own merits, independent of the issues that were missed entirely - a real
    bug/security issue that gets caught but mistagged as "style" fails
    severity_correct on that specific miss, distinct from a genuinely missed issue
    (which fails issues_flagged instead). Only the all-miss case is disqualified
    outright; partial-recall cases keep the narrower, more informative signal."""
    findings = [f for f in (output.predicted.get("findings") or []) if isinstance(f, dict)]
    normalized = [
        ((f.get("issue") or "").lower(), (f.get("severity") or "").lower())
        for f in findings
    ]
    all_issue_text = " ".join(text for text, _ in normalized)

    def caught_by(group: dict) -> list[tuple[str, str]]:
        return [(text, sev) for text, sev in normalized if any(p.lower() in text for p in group["phrases"])]

    caught_groups = [caught_by(g) for g in case.expected["must_flag"]]

    issues_flagged = all(len(matches) > 0 for matches in caught_groups)
    if case.expected["must_flag"] and not any(caught_groups):
        severity_correct = False
    else:
        severity_correct = all(
            all(sev == group["severity"].lower() for _, sev in matches)
            for group, matches in zip(case.expected["must_flag"], caught_groups)
            if matches
        )
    no_false_positives = not any(phrase.lower() in all_issue_text for phrase in case.expected["must_not_flag"])

    return {
        "issues_flagged": issues_flagged,
        "severity_correct": severity_correct,
        "no_false_positives": no_false_positives,
    }


_RULE_SCORERS = {
    "bug_triage": rule_based_score_bug_triage,
    "code_gen": rule_based_score_code_gen,
    "summarization": rule_based_score_summarization,
    "code_review": rule_based_score_code_review,
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
