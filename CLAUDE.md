# llm-eval-harness

A from-scratch eval harness for scoring prompt/model quality on a bug-triage classification task. Built as a learning project — the point is understanding evals well enough to trust (or distrust) later, more ambitious LLM projects, particularly `llm-task-router` (sibling repo, `../llm-task-router`), a cross-model-tier router that routes a task to the cheapest model tier clearing a per-category quality floor. This repo's benchmark runs *are* how that quality floor gets measured — `llm-task-router/tiers.py`'s tier→model mapping should only be populated from real runs recorded here, never guessed. See the `calibrate-tier` skill in this repo for that workflow, and `llm-task-router/CLAUDE.md`'s "Adding a provider" section for the other side of it.

## Why it's built this way

- **No `anthropic` API key, no separate billing.** Every model call shells out to headless `claude -p` (see `eval_harness/claude_cli.py`), which runs on the existing Claude Code subscription instead of pay-per-token API billing. This was a deliberate cost/learning tradeoff, not an oversight — see conversation history for the reasoning if it ever needs revisiting.
- Each `claude -p` call strips the default system prompt and disables tools/MCP (`--system-prompt`, `--disallowed-tools "*"`, `--strict-mcp-config`). Without that, every call pays for and caches Claude Code's full system prompt (~$0.07/call observed); stripped down it's ~$0.003-0.005/call. Don't remove those flags without expecting a real cost jump.
- Zero third-party dependencies (no `pydantic`, no `anthropic` SDK) — stdlib only (`dataclasses`, `json`, `subprocess`, `argparse`). Deliberate, to keep this cheap and dependency-free.
- **Codex is a second provider, not a second harness.** `eval_harness/codex_cli.py` shells out to headless `codex exec` the same way `claude_cli.py` shells out to `claude -p` — same cost-avoidance rationale (subscription CLI, not API key). `runner.py`/`cli.py` take a `--provider` flag; `scorers.py`'s `rule_based_score`/`judge_score` are completely unaware of which provider produced a `ModelOutput` and needed zero changes. The judge call in `judge_score` stays hardcoded to Claude regardless of provider under test, on purpose — grading Codex output with a different model lineage than the one under test avoids correlated errors (same-family models can converge on the same wrong answer).

## Architecture

```
eval_harness/
  schema.py      - TestCase, ModelOutput, ScoredResult, RunSummary dataclasses
  cases/*.jsonl  - golden test sets, one JSON object per line
  prompts/*.txt  - versioned system prompts under test (e.g. v1_naive, v2_rubric)
  claude_cli.py  - subprocess wrapper around `claude -p --output-format json`
  codex_cli.py   - subprocess wrapper around `codex exec --output-last-message`
  jsonutil.py    - robust JSON extraction (models sometimes wrap/prefix JSON with stray text)
  runner.py      - runs a prompt version against every case in a suite, for either provider
  scorers.py     - rule_based_score (exact-match) + judge_score (LLM-as-judge on reasoning coherence)
  report.py      - aggregates a run, persists it to runs/, diffs against the last run for the same prompt+model
  cli.py         - `python -m eval_harness run|compare`
runs/            - one JSON file per run, named {timestamp}__{prompt}__{model}.json - never hand-edit these
```

## Why two scorers, not one

`rule_based_score` checks whether the final label matches the golden set. `judge_score` separately checks whether the model's stated one-sentence reasoning actually holds up against the report — a model can land on the right label for reasoning that doesn't support it, or a defensible-but-different label with solid reasoning. Aggregate accuracy alone hides both. Don't remove either scorer without a reason; they catch different failure modes (see the bt-02 case in run history for a real example of a label-correct-but-reasoning-wrong case the judge caught).

## Adding a new eval suite

1. Add `eval_harness/cases/<suite>.jsonl` - one `TestCase` per line (id, bug_report, expected_severity, expected_category, notes).
2. Add prompt version(s) to `eval_harness/prompts/`. The output contract models must follow: `{"reasoning": "...", "severity": "...", "category": "..."}` - keep new prompts on this same shape unless the schema in `schema.py` changes too.
3. `python -m eval_harness run --suite <suite> --prompt <version> --model haiku`

## Commands

```
python -m eval_harness run --prompt v1_naive --model haiku [--no-judge]
python -m eval_harness run --prompt v1_naive --provider codex --model gpt-5.6-terra [--no-judge]
python -m eval_harness compare --prompt-a v1_naive --prompt-b v2_rubric --model haiku
```

`--provider` defaults to `claude`. `--model` has no cross-provider default:
omitting it resolves to `haiku` for Claude, but is a hard error for Codex
(`cli.py`'s `resolve_model` raises rather than guessing) - a real run without
`--model` got silently saved to disk as `codex/None`, which is useless as
calibration data since you no longer know which model actually produced it.
Always pass an explicit `--model` when benchmarking Codex.

`--no-judge` skips the LLM-judge pass for a cheaper/faster rule-based-only run - useful when iterating on the runner/parsing itself rather than the prompt.

## Known rough edges

- `find_previous_run` in `report.py` only reconstructs aggregate metrics from saved JSON, not per-case results — fine for the diff/regression display, but don't assume `previous.results` is populated.
- No retry/backoff on `claude -p` failures — a transient CLI error currently just records a `parse_error` on that one case rather than retrying.
- Codex runs always report `total_cost_usd: 0.0`. No dollar-cost field exists anywhere in `codex exec`'s output (confirmed against a real authenticated call) — only `duration_ms` is real there (self-measured via `time.perf_counter()`, since Codex doesn't report that either). Don't compare `total_cost_usd` across a Claude run and a Codex run in the same report; it isn't apples to apples yet.

## Router tier calibration status (`bug_triage` / `v1_naive`, as of 2026-07-23)

Full judged benchmark across every model on both providers this account has
access to — the first real cross-model quality-floor data for
`llm-task-router/tiers.py`. Saved as individual runs in `runs/`; don't
re-derive this table from disk without re-checking, it will drift as new
runs get added.

| provider | model | severity acc | category acc | fully correct | judge coherence |
|---|---|---|---|---|---|
| claude | haiku (cheap) | 60.0% | 100.0% | 60.0% | 0.85 |
| claude | sonnet (mid) | 66.7% | 100.0% | 66.7% | 0.85 |
| claude | opus (flagship) | 73.3% | 100.0% | 73.3% | 0.84 |
| codex | gpt-5.4-mini | 53.3% | 86.7% | 40.0% | 0.83 |
| codex | gpt-5.6-luna | 60.0% | 80.0% | 46.7% | 0.84 |
| codex | gpt-5.6-terra | 60.0% | 86.7% | 46.7% | 0.85 |
| codex | gpt-5.5 | 66.7% | 93.3% | 60.0% | 0.79 |

**Verdict: no Codex model clears Claude's cheap-tier (haiku) floor on this
suite.** Every Codex model tested is at or below haiku on category accuracy
and fully-correct rate; `gpt-5.5` is the closest (ties haiku on severity and
fully-correct) but still short on category accuracy and has the weakest
judge coherence of the whole table. This is a real negative result, not a
gap in coverage — don't add a Codex entry to `tiers.py` off this data.

**Codex model access is account-dependent and was probed directly, not
assumed** — on this ChatGPT-account login, `gpt-5.6-sol` (the flagship-
equivalent slug), `gpt-5.3-codex`, `gpt-5.1-codex-mini`, `gpt-5.4-nano`,
`gpt-5.4`, and `gpt-5.2` all fail with a 400 `invalid_request_error`
("not supported when using Codex with a ChatGPT account"). A run against an
inaccessible model produces a `parse_error` on every case and an all-zero
`avg_judge_score` that looks like a real (terrible) score if you don't
check the error text — that's not a quality-floor result, it's a bogus run
and should be deleted rather than left in `runs/` (the `gpt-5.6-sol` attempt
here was deleted for exactly this reason). Re-probe access with a single
cheap `codex exec "..." --model <name>` call before running the full suite
on any new model.
