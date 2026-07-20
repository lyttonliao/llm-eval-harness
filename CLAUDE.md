# llm-eval-harness

A from-scratch eval harness for scoring prompt/model quality on a bug-triage classification task. Built as a learning project — the point is understanding evals well enough to trust (or distrust) later, more ambitious LLM projects, particularly a planned cross-model-tier router.

## Why it's built this way

- **No `anthropic` API key, no separate billing.** Every model call shells out to headless `claude -p` (see `eval_harness/claude_cli.py`), which runs on the existing Claude Code subscription instead of pay-per-token API billing. This was a deliberate cost/learning tradeoff, not an oversight — see conversation history for the reasoning if it ever needs revisiting.
- Each `claude -p` call strips the default system prompt and disables tools/MCP (`--system-prompt`, `--disallowed-tools "*"`, `--strict-mcp-config`). Without that, every call pays for and caches Claude Code's full system prompt (~$0.07/call observed); stripped down it's ~$0.003-0.005/call. Don't remove those flags without expecting a real cost jump.
- Zero third-party dependencies (no `pydantic`, no `anthropic` SDK) — stdlib only (`dataclasses`, `json`, `subprocess`, `argparse`). Deliberate, to keep this cheap and dependency-free.

## Architecture

```
eval_harness/
  schema.py      - TestCase, ModelOutput, ScoredResult, RunSummary dataclasses
  cases/*.jsonl  - golden test sets, one JSON object per line
  prompts/*.txt  - versioned system prompts under test (e.g. v1_naive, v2_rubric)
  claude_cli.py  - subprocess wrapper around `claude -p --output-format json`
  jsonutil.py    - robust JSON extraction (models sometimes wrap/prefix JSON with stray text)
  runner.py      - runs a prompt version against every case in a suite
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
python -m eval_harness compare --prompt-a v1_naive --prompt-b v2_rubric --model haiku
```

`--no-judge` skips the LLM-judge pass for a cheaper/faster rule-based-only run - useful when iterating on the runner/parsing itself rather than the prompt.

## Known rough edges

- `find_previous_run` in `report.py` only reconstructs aggregate metrics from saved JSON, not per-case results — fine for the diff/regression display, but don't assume `previous.results` is populated.
- No retry/backoff on `claude -p` failures — a transient CLI error currently just records a `parse_error` on that one case rather than retrying.
