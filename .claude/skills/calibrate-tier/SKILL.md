---
name: calibrate-tier
description: Benchmark a provider/model against the eval suite and compare it against existing Claude baselines in runs/ to recommend a router tier (cheap/mid/flagship) for llm-task-router's tiers.py. Use when asked to calibrate, benchmark, or evaluate a new model or provider for the router.
---

Goal: produce a real, judged quality-floor data point for one (provider, model)
pair, and a tier recommendation for `llm-task-router/tiers.py`'s `TIER_MODELS` -
not to edit that file directly. Tier assignments cross a repo boundary and
should be a deliberate, confirmed edit, not a side effect of a benchmark run.

## Steps

1. Confirm with the user (if not already given): suite (default `bug_triage`),
   prompt version (default `v1_naive`), provider, and model. For `--provider
   codex`, `--model` is mandatory - `cli.py` will refuse to guess one (see
   CLAUDE.md's rough edges: a run without it once got silently saved as
   `codex/None`, which is useless as calibration data).

2. Run the full benchmark **with the judge pass**, not `--no-judge` - tier
   calibration needs `avg_judge_score`, not just label accuracy:
   ```
   uv run python -m eval_harness run --suite <suite> --prompt <version> --provider <provider> --model <model>
   ```
   This costs real money/quota for every case in the suite (currently 15) -
   say so before running if the user hasn't already acknowledged it.

3. Read the saved run file, then read the existing `runs/*.json` history for
   the **same suite + prompt version** on Claude's tiers (haiku/sonnet/opus)
   as the baseline to compare against - `report.find_previous_run` only
   matches same prompt+model, so pull multiple files directly for a
   cross-model view rather than relying on that function here.

4. Compare `check_accuracies` (per-suite check names - e.g. `severity`/
   `category` for `bug_triage`, `tests_passed` for `code_gen`) and
   `avg_judge_score` against the Claude baselines. Per the router's
   quality-floor concept: a model belongs in the cheapest tier whose
   accuracy/judge-coherence is not meaningfully worse than the tier above it
   for this task category. Do not weigh `total_cost_usd` for a Codex run -
   it's always `0.0` (no dollar-cost field exists in `codex exec`'s output,
   see CLAUDE.md) - compare cost only between two Claude runs, never Claude
   vs. Codex. Watch for a ceiling effect on small suites (e.g. `code_gen`'s
   8 cases): if every tier scores ~100%, that's a signal the suite isn't
   discriminating between tiers yet, not that every model clears the floor.

5. Present a recommendation (cheap/mid/flagship) with the numbers behind it.
   Only edit `llm-task-router/tiers.py`'s `TIER_MODELS` if the user confirms -
   it's a different repo and a router-behavior change, not just a benchmark
   artifact.
