import json
from datetime import datetime, timezone
from pathlib import Path

from eval_harness.schema import RunSummary, ScoredResult

RUNS_DIR = Path(__file__).parent.parent / "runs"


def build_summary(
    prompt_version: str, model: str, results: list[ScoredResult], provider: str = "claude", samples_per_case: int = 1
) -> RunSummary:
    """check_accuracies/fully_correct_rate/avg_judge_score are plain means
    over `results` regardless of samples_per_case - with N samples per case,
    `results` holds N ScoredResults per test_id (see scorers.score_all), so
    these means already equal the average per-case pass rate across samples;
    no separate formula needed. total_cases stays a distinct case count
    (not len(results), which would double-count samples)."""
    n = len(results) or 1
    check_names = results[0].checks.keys() if results else []
    total_cases = len({r.test_id for r in results}) if samples_per_case > 1 else len(results)
    return RunSummary(
        prompt_version=prompt_version,
        model=model,
        total_cases=total_cases,
        check_accuracies={name: sum(r.checks[name] for r in results) / n for name in check_names},
        fully_correct_rate=sum(r.fully_correct for r in results) / n,
        avg_judge_score=sum(r.judge_score for r in results) / n,
        total_cost_usd=sum(r.cost_usd for r in results),
        total_tokens=sum(r.token_usage.get("total_tokens", 0) for r in results),
        results=results,
        provider=provider,
        samples_per_case=samples_per_case,
    )


def per_case_pass_rates(summary: RunSummary) -> dict[str, dict]:
    """Groups a multi-sample run's flat `results` list back up by test_id -
    only meaningful when samples_per_case > 1. Returns, per case, the
    fraction of samples that were fully correct and the fraction passing
    each individual check, so a case that's genuinely unstable (e.g. 3/5
    samples pass) is visible as a rate instead of collapsing to a single
    pass/fail that depends on which sample happened to run."""
    by_case: dict[str, list[ScoredResult]] = {}
    for r in summary.results:
        by_case.setdefault(r.test_id, []).append(r)

    out = {}
    for test_id, samples in by_case.items():
        n = len(samples)
        check_names = samples[0].checks.keys()
        out[test_id] = {
            "n_samples": n,
            "fully_correct_rate": sum(s.fully_correct for s in samples) / n,
            "check_pass_rates": {name: sum(s.checks[name] for s in samples) / n for name in check_names},
        }
    return out


def _run_key(prompt_version: str, model: str) -> str:
    return f"{prompt_version}__{model}"


def save_run(summary: RunSummary) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{ts}__{_run_key(summary.prompt_version, summary.model)}.json"

    payload = {
        "prompt_version": summary.prompt_version,
        "model": summary.model,
        "provider": summary.provider,
        "total_cases": summary.total_cases,
        "check_accuracies": summary.check_accuracies,
        "fully_correct_rate": summary.fully_correct_rate,
        "avg_judge_score": summary.avg_judge_score,
        "total_cost_usd": summary.total_cost_usd,
        "total_tokens": summary.total_tokens,
        "samples_per_case": summary.samples_per_case,
        "results": [
            {
                "test_id": r.test_id,
                "predicted": r.predicted,
                "checks": r.checks,
                "judge_score": r.judge_score,
                "judge_rationale": r.judge_rationale,
                "cost_usd": r.cost_usd,
                "duration_ms": r.duration_ms,
                "token_usage": r.token_usage,
            }
            for r in summary.results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def _check_accuracies_from_payload(data: dict) -> dict[str, float]:
    """Run files saved before check_accuracies existed (see runs/ history
    predating this generalization) used two fixed severity/category fields
    instead - migrate them into the generic shape rather than requiring a
    one-off rewrite of files under runs/, same pattern as the pre-Codex
    "provider" default below."""
    if "check_accuracies" in data:
        return data["check_accuracies"]
    return {"severity": data["severity_accuracy"], "category": data["category_accuracy"]}


def find_previous_run(prompt_version: str, model: str, before: Path | None = None) -> RunSummary | None:
    """Most recent prior run for the same prompt_version+model, for regression diffing."""
    if not RUNS_DIR.exists():
        return None
    key = _run_key(prompt_version, model)
    candidates = sorted(p for p in RUNS_DIR.glob(f"*__{key}.json") if p != before)
    if not candidates:
        return None
    data = json.loads(candidates[-1].read_text())
    return RunSummary(
        prompt_version=data["prompt_version"],
        model=data["model"],
        total_cases=data["total_cases"],
        check_accuracies=_check_accuracies_from_payload(data),
        fully_correct_rate=data["fully_correct_rate"],
        avg_judge_score=data["avg_judge_score"],
        total_cost_usd=data["total_cost_usd"],
        total_tokens=data.get("total_tokens", 0),
        # .get(), not [] - run files saved before the provider field existed
        # (see runs/ history predating Codex support) don't have this key.
        provider=data.get("provider", "claude"),
        samples_per_case=data.get("samples_per_case", 1),
    )


def _fmt_delta(new: float, old: float) -> str:
    delta = new - old
    sign = "+" if delta >= 0 else ""
    flag = "" if abs(delta) < 0.001 else ("  <- regression" if delta < 0 else "  <- improved")
    return f"{new:.1%} ({sign}{delta:.1%} vs last run){flag}"


def print_report(summary: RunSummary, previous: RunSummary | None = None) -> None:
    print()
    samples_note = f", {summary.samples_per_case} samples/case" if summary.samples_per_case > 1 else ""
    print(f"=== {summary.prompt_version} / {summary.provider}/{summary.model} ({summary.total_cases} cases{samples_note}) ===")
    for name, value in summary.check_accuracies.items():
        label = f"{name} accuracy: "
        prev_value = previous.check_accuracies.get(name) if previous else None
        if prev_value is None:
            print(f"{label:<23}{value:.1%}")
        else:
            print(f"{label:<23}{_fmt_delta(value, prev_value)}")
    if previous:
        print(f"fully correct:        {_fmt_delta(summary.fully_correct_rate, previous.fully_correct_rate)}")
        print(f"avg judge coherence:  {_fmt_delta(summary.avg_judge_score, previous.avg_judge_score)}")
    else:
        print(f"fully correct:        {summary.fully_correct_rate:.1%}")
        print(f"avg judge coherence:  {summary.avg_judge_score:.2f}")
    print(f"total cost:           ${summary.total_cost_usd:.4f}")
    if summary.provider == "codex":
        print(f"total tokens:         {summary.total_tokens:,}")

    if summary.samples_per_case > 1:
        print(f"\nper-case pass rate ({summary.samples_per_case} samples each):")
        for test_id, stats in sorted(per_case_pass_rates(summary).items()):
            rate = stats["fully_correct_rate"]
            n_pass = round(rate * stats["n_samples"])
            flag = "" if rate == 1.0 else "  <- unstable" if 0 < rate < 1.0 else "  <- always fails"
            print(f"  - {test_id}: {n_pass}/{stats['n_samples']} ({rate:.0%}){flag}")
            if rate < 1.0:
                failing_checks = [name for name, r in stats["check_pass_rates"].items() if r < 1.0]
                print(f"      unstable checks: {failing_checks}")
    else:
        failures = [r for r in summary.results if not r.fully_correct]
        if failures:
            print(f"\n{len(failures)} case(s) missed:")
            for r in failures:
                failed_checks = [name for name, ok in r.checks.items() if not ok]
                print(f"  - {r.test_id}: failed {failed_checks} (predicted={r.predicted}) (judge coherence {r.judge_score:.2f})")
    print()
