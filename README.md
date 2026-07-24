# LLM Eval Harness

A dependency-light Python harness for measuring prompt and model quality on
structured LLM tasks. It runs versioned prompts against hand-authored golden
cases, applies deterministic task-specific checks, optionally judges the
model's reasoning, and saves each run as a JSON artifact for comparison.

The project is the offline-calibration half of the sibling
[`llm-task-router`](../llm-task-router): benchmark results are intended to
identify the least expensive model tier that meets a quality floor for a task
type before live traffic is routed to it.

## What it evaluates

Three suites demonstrate different evaluation shapes:

| Suite | Cases | Model contract | Deterministic score |
| --- | ---: | --- | --- |
| `bug_triage` | 15 | JSON severity, category, and reasoning | Exact, case-insensitive severity and category match |
| `code_gen` | 17 | JSON Python source and reasoning | Generated code passes the case's pytest test file |
| `summarization` | 16 | JSON summary and reasoning | Required facts present, forbidden/hallucinated facts absent, word-count limit respected |

Each result also receives an optional LLM-as-judge **reasoning-coherence**
score. This complements exact-match scoring: a model can arrive at the right
answer with unsupported reasoning, or give a defensible answer that differs
from the golden label.

## Design

```text
versioned prompt + golden JSONL cases
                |
                v
  Claude CLI or Codex CLI provider adapter
                |
                v
  JSON extraction + suite-specific rule scorer
                |
                +---- optional Claude judge for reasoning coherence
                v
       persisted JSON run + regression report
```

- **Provider-independent scoring:** Claude and Codex outputs flow through the
  same schema, JSON extraction, and scoring pipeline.
- **Cross-family judging:** the reasoning judge uses Claude even when Codex is
  being evaluated, reducing the risk of same-family grading agreement.
- **Versioned, inspectable artifacts:** every run is written to `runs/` with
  its provider, model, aggregate metrics, and per-case results.
- **Minimal dependencies:** application code uses only the Python standard
  library; `pytest` is used for development and code-generation checks.

## Recorded pilot results

The checked-in artifacts include a 15-case `bug_triage` benchmark using the
same `v1_naive` prompt. Claude's fully-correct rate increased from 60.0% on
Haiku to 66.7% on Sonnet and 73.3% on Opus. A rubric-oriented prompt variant
reached 80.0% fully correct on Haiku in a separate recorded run.

A 16-case `summarization` benchmark using the `summarization_v1` prompt found
a non-monotonic result across Claude's tiers: Haiku scored 87.5% fully
correct, Opus 75.0%, and Sonnet 62.5% - every miss was a word-count-limit
violation, not a factual error (all three tiers hit 100% on the fact-inclusion
and no-hallucination checks). Larger tiers here are more verbose, and this
task rewards concision, so bigger isn't uniformly better on this suite.

These are small, hand-authored evaluation sets—not statistically conclusive
leaderboard results. They are useful calibration evidence and regression
signals, not claims of broad model superiority.

## Quick start

Requirements:

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- An authenticated `claude` CLI for Claude runs, or an authenticated `codex`
  CLI for Codex runs

```bash
uv sync --group dev

# Run the test suite
uv run pytest -q

# Evaluate the default bug-triage suite with Claude Haiku
uv run python -m eval_harness run --prompt v1_naive --model haiku

# Run code generation without the optional judge pass
uv run python -m eval_harness run \
  --suite code_gen --prompt code_gen_v1 --model haiku --no-judge

# Evaluate the summarization suite with Claude Sonnet
uv run python -m eval_harness run \
  --suite summarization --prompt summarization_v1 --model sonnet

# Evaluate an explicitly named Codex model
uv run python -m eval_harness run \
  --suite bug_triage --prompt v1_naive \
  --provider codex --model gpt-5.6-terra

# Run two prompts head-to-head
uv run python -m eval_harness compare \
  --prompt-a v1_naive --prompt-b v2_rubric --model haiku
```

`--provider` defaults to `claude`. Codex model names are account-dependent,
so `--model` is required for Codex runs. Codex CLI output does not expose a
dollar-cost field, so its `$0.00` cost is a placeholder; the harness records
the CLI's input, cached-input, output, reasoning-output, and total token
counts instead.

## Repository layout

```text
eval_harness/
  cases/          Golden JSONL datasets
  prompts/        Versioned prompt files
  runner.py       Provider dispatch and output parsing
  scorers.py      Rule scorers and LLM-as-judge scoring
  sandbox.py      Local pytest execution for code-generation cases
  report.py       Run persistence, comparison, and console reporting
  *_cli.py        Claude and Codex CLI adapters
tests/            Unit tests for schemas, parsing, adapters, scoring, and reports
runs/             Saved benchmark artifacts
```

See [`CLAUDE.md`](CLAUDE.md) for development notes, suite-extension guidance,
and the detailed calibration record used by the task-router prototype.
