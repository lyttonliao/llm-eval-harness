import json
from datetime import datetime, timezone
from pathlib import Path

from eval_harness.schema import RunSummary, ScoredResult

RUNS_DIR = Path(__file__).parent.parent / "runs"


def build_summary(prompt_version: str, model: str, results: list[ScoredResult]) -> RunSummary:
    n = len(results) or 1
    return RunSummary(
        prompt_version=prompt_version,
        model=model,
        total_cases=len(results),
        severity_accuracy=sum(r.severity_correct for r in results) / n,
        category_accuracy=sum(r.category_correct for r in results) / n,
        fully_correct_rate=sum(r.fully_correct for r in results) / n,
        avg_judge_score=sum(r.judge_score for r in results) / n,
        total_cost_usd=sum(r.cost_usd for r in results),
        results=results,
    )


def _run_key(prompt_version: str, model: str) -> str:
    return f"{prompt_version}__{model}"


def save_run(summary: RunSummary) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RUNS_DIR / f"{ts}__{_run_key(summary.prompt_version, summary.model)}.json"

    payload = {
        "prompt_version": summary.prompt_version,
        "model": summary.model,
        "total_cases": summary.total_cases,
        "severity_accuracy": summary.severity_accuracy,
        "category_accuracy": summary.category_accuracy,
        "fully_correct_rate": summary.fully_correct_rate,
        "avg_judge_score": summary.avg_judge_score,
        "total_cost_usd": summary.total_cost_usd,
        "results": [
            {
                "test_id": r.test_id,
                "predicted_severity": r.predicted_severity,
                "predicted_category": r.predicted_category,
                "severity_correct": r.severity_correct,
                "category_correct": r.category_correct,
                "judge_score": r.judge_score,
                "judge_rationale": r.judge_rationale,
                "cost_usd": r.cost_usd,
                "duration_ms": r.duration_ms,
            }
            for r in summary.results
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


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
        severity_accuracy=data["severity_accuracy"],
        category_accuracy=data["category_accuracy"],
        fully_correct_rate=data["fully_correct_rate"],
        avg_judge_score=data["avg_judge_score"],
        total_cost_usd=data["total_cost_usd"],
    )


def _fmt_delta(new: float, old: float) -> str:
    delta = new - old
    sign = "+" if delta >= 0 else ""
    flag = "" if abs(delta) < 0.001 else ("  <- regression" if delta < 0 else "  <- improved")
    return f"{new:.1%} ({sign}{delta:.1%} vs last run){flag}"


def print_report(summary: RunSummary, previous: RunSummary | None = None) -> None:
    print()
    print(f"=== {summary.prompt_version} / {summary.model} ({summary.total_cases} cases) ===")
    if previous:
        print(f"severity accuracy:    {_fmt_delta(summary.severity_accuracy, previous.severity_accuracy)}")
        print(f"category accuracy:    {_fmt_delta(summary.category_accuracy, previous.category_accuracy)}")
        print(f"fully correct:        {_fmt_delta(summary.fully_correct_rate, previous.fully_correct_rate)}")
        print(f"avg judge coherence:  {_fmt_delta(summary.avg_judge_score, previous.avg_judge_score)}")
    else:
        print(f"severity accuracy:    {summary.severity_accuracy:.1%}")
        print(f"category accuracy:    {summary.category_accuracy:.1%}")
        print(f"fully correct:        {summary.fully_correct_rate:.1%}")
        print(f"avg judge coherence:  {summary.avg_judge_score:.2f}")
    print(f"total cost:           ${summary.total_cost_usd:.4f}")

    failures = [r for r in summary.results if not r.fully_correct]
    if failures:
        print(f"\n{len(failures)} case(s) missed:")
        for r in failures:
            what = []
            if not r.severity_correct:
                what.append(f"severity->{r.predicted_severity}")
            if not r.category_correct:
                what.append(f"category->{r.predicted_category}")
            print(f"  - {r.test_id}: wrong {', '.join(what)} (judge coherence {r.judge_score:.2f})")
    print()
