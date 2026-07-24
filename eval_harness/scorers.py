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
import re

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


def _structural_checks_ok(code: str, checks: list[dict]) -> bool:
    """Each check targets a planted smell directly on the refactored source, not
    via test execution - see rule_based_score_refactor's docstring for why a
    regex/count check is needed alongside the pytest gate."""
    for check in checks:
        matches = re.findall(check["pattern"], code)
        if check["type"] == "not_contains" and matches:
            return False
        if check["type"] == "max_occurrences" and len(matches) > check["max"]:
            return False
    return True


def rule_based_score_refactor(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    """Behavior preservation (tests_passed) reuses code_gen's sandbox unchanged -
    a refactor that breaks the existing test suite is disqualified outright,
    same trust model as code_gen (see sandbox.py). That alone doesn't prove a
    refactor actually happened though: returning the original code verbatim
    would pass every tests_passed check. smells_removed is the counterpart -
    a structural check (regex presence/absence, or an occurrence count) run
    directly on the generated code, not on test output. not_contains catches
    smells that must disappear entirely (dead code, a mutable default arg);
    max_occurrences catches smells that are fine to still exist ONCE (a magic
    number moved into a single named constant) but not left duplicated inline.

    Some cases (see cases/refactor.jsonl's bug-preservation cases) plant no
    structural_checks at all and rely entirely on tests_passed: those cases'
    test_code encodes the code's current (possibly buggy) behavior on purpose,
    so the discriminating signal is whether the model left that behavior
    alone rather than "fixing" something out of scope - smells_removed is
    vacuously true there, same reasoning as summarization's always-present
    checks with an empty constraint list."""
    code = output.predicted.get("code", "")
    tests_passed, _detail = sandbox.run_pytest_check(code, case.expected["test_code"])
    smells_removed = _structural_checks_ok(code, case.expected.get("structural_checks", []))
    return {"tests_passed": tests_passed, "smells_removed": smells_removed}


def rule_based_score_multi_step(case: TestCase, output: ModelOutput) -> dict[str, bool]:
    """No golden plan to string-match, so grading combines fact-constraint groups
    (same shape as summarization/code_review) with a new axis this suite exists to
    test: relative step ORDER, not just step presence. required_steps is a list of
    groups (acceptable phrasings, matched against "phase detail" per step - reviews
    of a plan paraphrase just like code review findings do). ordering_constraints is
    a list of [earlier_group_idx, later_group_idx] pairs into required_steps; a
    constraint is satisfied if the earliest step matching the earlier group has a
    lower index in output.steps than the earliest step matching the later group.

    ordering_correct only evaluates constraints where BOTH referenced groups were
    actually matched somewhere in the output - a constraint referencing a group the
    model never produced at all can't be judged as correctly or incorrectly ordered,
    same reasoning as code_review's severity_correct only scoring groups that were
    caught. But mirroring severity_correct's other half: if a case HAS ordering
    constraints and literally none of them were evaluable (every referenced group
    missing), ordering_correct fails outright rather than vacuously passing - that
    combination would otherwise read as a clean order on a plan that's missing the
    steps needed to have an order at all. Cases with no ordering_constraints (pure
    coverage/false-positive cases) get a vacuous pass, same precedent as
    refactor/summarization's always-present, sometimes-empty checks.
    """
    steps = [s for s in (output.predicted.get("steps") or []) if isinstance(s, dict)]
    texts = [f"{s.get('phase') or ''} {s.get('detail') or ''}".lower() for s in steps]

    def first_match_index(phrases: list[str]) -> int | None:
        for i, text in enumerate(texts):
            if any(phrase.lower() in text for phrase in phrases):
                return i
        return None

    group_indices = [first_match_index(group["phrases"]) for group in case.expected["required_steps"]]
    step_coverage = all(idx is not None for idx in group_indices)

    constraint_results = []
    for early, late in case.expected["ordering_constraints"]:
        early_idx, late_idx = group_indices[early], group_indices[late]
        if early_idx is not None and late_idx is not None:
            constraint_results.append(early_idx < late_idx)

    if case.expected["ordering_constraints"] and not constraint_results:
        ordering_correct = False
    else:
        ordering_correct = all(constraint_results) if constraint_results else True

    all_text = " ".join(texts)
    no_false_positives = not any(phrase.lower() in all_text for phrase in case.expected["must_not_include"])

    return {
        "step_coverage": step_coverage,
        "ordering_correct": ordering_correct,
        "no_false_positives": no_false_positives,
    }


_RULE_SCORERS = {
    "bug_triage": rule_based_score_bug_triage,
    "code_gen": rule_based_score_code_gen,
    "summarization": rule_based_score_summarization,
    "code_review": rule_based_score_code_review,
    "refactor": rule_based_score_refactor,
    "multi_step": rule_based_score_multi_step,
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
