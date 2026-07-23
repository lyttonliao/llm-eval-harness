import json

import pytest

import eval_harness.report as report
from eval_harness.schema import RunSummary, ScoredResult


def _result(severity_correct=True, category_correct=True, judge_score=0.8, cost=0.001) -> ScoredResult:
    return ScoredResult(
        test_id="bt-01",
        predicted_severity="high",
        predicted_category="backend",
        severity_correct=severity_correct,
        category_correct=category_correct,
        judge_score=judge_score,
        judge_rationale="grounded",
        cost_usd=cost,
        duration_ms=500,
    )


@pytest.fixture(autouse=True)
def isolated_runs_dir(tmp_path, monkeypatch):
    """Never touch the real runs/ dir, which has real historical run data."""
    monkeypatch.setattr(report, "RUNS_DIR", tmp_path / "runs")
    return tmp_path / "runs"


# --- build_summary ----------------------------------------------------------


def test_build_summary_aggregates_correctly():
    results = [
        _result(severity_correct=True, category_correct=True, judge_score=1.0, cost=0.01),
        _result(severity_correct=True, category_correct=False, judge_score=0.5, cost=0.02),
        _result(severity_correct=False, category_correct=False, judge_score=0.0, cost=0.03),
    ]

    summary = report.build_summary("v1_naive", "haiku", results)

    assert summary.total_cases == 3
    assert summary.severity_accuracy == pytest.approx(2 / 3)
    assert summary.category_accuracy == pytest.approx(1 / 3)
    assert summary.fully_correct_rate == pytest.approx(1 / 3)
    assert summary.avg_judge_score == pytest.approx(1.5 / 3)
    assert summary.total_cost_usd == pytest.approx(0.06)


def test_build_summary_empty_results_does_not_divide_by_zero():
    summary = report.build_summary("v1_naive", "haiku", [])

    assert summary.total_cases == 0
    assert summary.severity_accuracy == 0.0
    assert summary.category_accuracy == 0.0
    assert summary.fully_correct_rate == 0.0
    assert summary.avg_judge_score == 0.0
    assert summary.total_cost_usd == 0.0


# --- save_run / find_previous_run --------------------------------------------


def test_save_run_writes_json_file_under_runs_dir(isolated_runs_dir):
    summary = report.build_summary("v1_naive", "haiku", [_result()])

    path = report.save_run(summary)

    assert path.exists()
    assert path.parent == isolated_runs_dir
    payload = json.loads(path.read_text())
    assert payload["prompt_version"] == "v1_naive"
    assert payload["model"] == "haiku"
    assert payload["total_cases"] == 1
    assert payload["results"][0]["test_id"] == "bt-01"


def test_find_previous_run_returns_none_when_no_prior_run():
    assert report.find_previous_run("v1_naive", "haiku") is None


def test_find_previous_run_returns_none_when_runs_dir_does_not_exist(isolated_runs_dir):
    assert not isolated_runs_dir.exists()
    assert report.find_previous_run("v1_naive", "haiku") is None


def test_save_run_and_find_previous_run_round_trip():
    summary = report.build_summary("v1_naive", "haiku", [_result()])
    saved_path = report.save_run(summary)

    previous = report.find_previous_run("v1_naive", "haiku", before=saved_path)

    # before= excludes the just-saved run, so with only one run on disk this is None
    assert previous is None


def _write_run_file(runs_dir, timestamp: str, prompt_version: str, model: str, avg_judge_score: float):
    """Bypass save_run's real-clock timestamp so ordering is deterministic
    without sleeping in the test."""
    runs_dir.mkdir(exist_ok=True)
    path = runs_dir / f"{timestamp}__{prompt_version}__{model}.json"
    summary = report.build_summary(prompt_version, model, [_result(judge_score=avg_judge_score)])
    payload = {
        "prompt_version": summary.prompt_version,
        "model": summary.model,
        "total_cases": summary.total_cases,
        "severity_accuracy": summary.severity_accuracy,
        "category_accuracy": summary.category_accuracy,
        "fully_correct_rate": summary.fully_correct_rate,
        "avg_judge_score": summary.avg_judge_score,
        "total_cost_usd": summary.total_cost_usd,
        "results": [],
    }
    path.write_text(json.dumps(payload))
    return path


def test_find_previous_run_picks_most_recent_of_multiple(isolated_runs_dir):
    _write_run_file(isolated_runs_dir, "20260101T000000Z", "v1_naive", "haiku", avg_judge_score=0.1)
    _write_run_file(isolated_runs_dir, "20260201T000000Z", "v1_naive", "haiku", avg_judge_score=0.99)

    previous = report.find_previous_run("v1_naive", "haiku")

    assert previous is not None
    assert previous.avg_judge_score == pytest.approx(0.99)


def test_find_previous_run_excludes_before_path(isolated_runs_dir):
    first_path = _write_run_file(isolated_runs_dir, "20260101T000000Z", "v1_naive", "haiku", avg_judge_score=0.1)
    second_path = _write_run_file(isolated_runs_dir, "20260201T000000Z", "v1_naive", "haiku", avg_judge_score=0.99)

    previous = report.find_previous_run("v1_naive", "haiku", before=second_path)

    assert previous is not None
    saved_first = json.loads(first_path.read_text())
    assert previous.total_cost_usd == pytest.approx(saved_first["total_cost_usd"])
    assert previous.avg_judge_score == pytest.approx(0.1)


def test_find_previous_run_scopes_by_prompt_and_model():
    v1_summary = report.build_summary("v1_naive", "haiku", [_result()])
    report.save_run(v1_summary)

    # different prompt/model combo should not be picked up
    assert report.find_previous_run("v2_rubric", "haiku") is None
    assert report.find_previous_run("v1_naive", "sonnet") is None


def test_find_previous_run_reconstructed_summary_has_empty_results():
    """Known rough edge from CLAUDE.md: find_previous_run only reconstructs
    aggregate metrics, not per-case results."""
    summary = report.build_summary("v1_naive", "haiku", [_result()])
    report.save_run(summary)

    previous = report.find_previous_run("v1_naive", "haiku")

    assert previous.results == []


def test_provider_round_trips_through_save_and_find_previous_run(isolated_runs_dir):
    summary = report.build_summary("v1_naive", "gpt-5", [_result()], provider="codex")
    saved_path = report.save_run(summary)

    payload = json.loads(saved_path.read_text())
    assert payload["provider"] == "codex"

    previous = report.find_previous_run("v1_naive", "gpt-5")
    assert previous.provider == "codex"


def test_find_previous_run_defaults_provider_to_claude_for_pre_existing_run_files(isolated_runs_dir):
    """Run files saved before Codex support existed have no "provider" key -
    find_previous_run must default it, not raise KeyError."""
    isolated_runs_dir.mkdir(exist_ok=True)
    path = isolated_runs_dir / "20260101T000000Z__v1_naive__haiku.json"
    payload = {
        "prompt_version": "v1_naive",
        "model": "haiku",
        "total_cases": 1,
        "severity_accuracy": 1.0,
        "category_accuracy": 1.0,
        "fully_correct_rate": 1.0,
        "avg_judge_score": 0.9,
        "total_cost_usd": 0.001,
        "results": [],
    }
    path.write_text(json.dumps(payload))

    previous = report.find_previous_run("v1_naive", "haiku")

    assert previous is not None
    assert previous.provider == "claude"
