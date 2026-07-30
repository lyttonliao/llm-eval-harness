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

Seven suites, 98 hand-authored cases, demonstrating four distinct evaluation
shapes — exact label match, execution against tests, fact-constraint matching
on free text, and structural inspection of the response itself:

| Suite | Cases | Model contract | Deterministic score |
| --- | ---: | --- | --- |
| `bug_triage` | 15 | JSON severity, category, and reasoning | Exact, case-insensitive severity and category match |
| `code_gen` | 17 | JSON Python source and reasoning | Generated code passes the case's pytest test file |
| `summarization` | 16 | JSON summary and reasoning | Required facts present, forbidden/hallucinated facts absent, word-count limit respected |
| `code_review` | 14 | JSON findings with severity, and reasoning | Planted issues flagged, severity tagged correctly, no false positives on bait |
| `refactor` | 12 | JSON Python source and reasoning | Existing tests still pass, and the targeted code smell is structurally gone |
| `multi_step` | 12 | JSON ordered plan steps, and reasoning | Required steps covered, dependency ordering respected, no premature actions |
| `architecture` | 12 | JSON design, alternatives considered, and reasoning | Stated constraints addressed, no anti-patterns proposed, real tradeoffs named |

Each suite pairs baseline cases with adversarial ones targeting known failure
modes for that task type. Every case is validated in both directions before it
is trusted: a correct reference answer must pass it, and a plausible-but-wrong
answer must fail it.

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

The four later suites are recorded with more caution. `code_gen` (17 cases)
and `refactor` (12 cases) show no measurable separation between Claude tiers —
all three reach 100%, on case sets specifically hardened with adversarial
cases confirmed to fail plausible-wrong implementations. `code_review` (14
cases) is the one suite where the flagship tier separates cleanly (100%, versus
78.6% for both cheaper tiers). `architecture` and `multi_step` are not yet
trustworthy as model comparisons: both are currently limited by scorer
strictness — substantively correct answers phrased in unanticipated wording
score as misses — which is a property of the grading, not the models.

These are small, hand-authored evaluation sets—not statistically conclusive
leaderboard results. Most are a single sample per model. They are useful
calibration evidence and regression signals, not claims of broad model
superiority.

## Setup

### 1. Prerequisites

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/) (recommended — all commands below use it)
- An authenticated `claude` CLI (`claude auth login --claudeai`) for Claude
  runs — required even for Codex-provider runs, since the reasoning-coherence
  judge is hardcoded to Claude regardless of which provider is under test
  (cross-family judging, see "Design")
- An authenticated `codex` CLI only if you also want to benchmark Codex
  models; find a reachable model name first (Codex model names are
  account-dependent — there's no safe default to guess, see "Commands")

### 2. Install dependencies

```bash
uv sync --group dev
```

Application code has zero third-party dependencies (stdlib only); `uv sync`
here just pulls in `pytest` for the dev group.

### 3. Verify

```bash
uv run pytest -q

uv run python -m eval_harness run --prompt v1_naive --model haiku
```

A completed run that saves a JSON artifact under `runs/` (rather than a
`claude` auth error) confirms the CLI adapter and package are wired up
correctly.

## Usage

### Running a suite

```bash
# Evaluate the default bug-triage suite with Claude Haiku
uv run python -m eval_harness run --prompt v1_naive --model haiku

# Run code generation without the optional judge pass (cheaper/faster while
# iterating on the runner or parsing, rather than on prompt quality)
uv run python -m eval_harness run \
  --suite code_gen --prompt code_gen_v1 --model haiku --no-judge

# Evaluate the summarization suite with Claude Sonnet
uv run python -m eval_harness run \
  --suite summarization --prompt summarization_v1 --model sonnet

# Evaluate an explicitly named Codex model
uv run python -m eval_harness run \
  --suite bug_triage --prompt v1_naive \
  --provider codex --model gpt-5.6-terra

# Run each case 5 times and report a per-case pass rate instead of pass/fail
# (cost/call-count scale by N — see "Working rules" in CLAUDE.md on not
# concluding from a single sample)
uv run python -m eval_harness run \
  --suite multi_step --prompt multi_step_v1 --model haiku --samples 5
```

`--suite` defaults to `bug_triage`; the seven available suites are
`bug_triage`, `code_gen`, `summarization`, `code_review`, `refactor`,
`multi_step`, `architecture` (matching `eval_harness/cases/*.jsonl`).
`--prompt` names a file in `eval_harness/prompts/` without the `.txt`
extension (e.g. `v1_naive`, `v2_rubric`, `summarization_v1`,
`code_gen_v1` — one file per suite/variant; see that directory for the
full list). `--provider` defaults to `claude`, with no cross-provider
default for `--model`: omitting it resolves to `haiku` for Claude but is a
hard error for Codex, since a run without an explicit model name once saved
to disk as unusable `codex/None` calibration data. Codex CLI output exposes
no dollar-cost field, so its `$0.00` cost is a placeholder — the harness
records the CLI's input/cached-input/output/reasoning-output/total token
counts instead.

### Comparing two prompts head-to-head

```bash
uv run python -m eval_harness compare \
  --prompt-a v1_naive --prompt-b v2_rubric --model haiku
```

Runs both prompts back-to-back on the same suite/model, then prints a
side-by-side of per-check accuracy, fully-correct rate, judge coherence,
cost, and total tokens.

### Reading results

Every run is saved as a timestamped JSON artifact under `runs/`
(`<timestamp>__<prompt>__<model>.json`), with the provider, model, aggregate
metrics, and per-case results — `print_report()` also diffs the new run
against the most recent previous run for the same prompt/model, so
regressions show up immediately in the console output. Delete a run from
`runs/` if it comes back as a bogus sample (every case `parse_error`, or a
flat `0.0` judge score usually means an inaccessible model, a quota
lockout, or a transient CLI failure — read the error text before trusting
the number; see "Working rules" in `CLAUDE.md`).

### Adding a new suite or prompt variant

See `CLAUDE.md`, "Adding a new eval suite" for the full checklist (case
schema, scorer wiring, validating each case in both directions before
trusting it). To add a prompt variant for an existing suite, drop a new
`.txt` file in `eval_harness/prompts/` and pass its filename (without
`.txt`) as `--prompt`.

### Calibrating router tiers

Use the `calibrate-tier` skill (`.claude/skills/calibrate-tier/SKILL.md`) to
turn a set of runs into the quality-floor data `llm-task-router`'s
`tiers.py`/`classifier.TYPE_DOMAIN_GRID` are calibrated from. See `CLAUDE.md`,
"Router tier calibration status" for what's already calibrated and "Router
tier synthesis across all 7 suites" for the full table — don't hand-derive
tier entries from a single run; follow that pointer instead.

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
docs/             Archived run-by-run development history
```

See [`CLAUDE.md`](CLAUDE.md) for development notes, suite-extension guidance,
and the current calibration record used by the task-router prototype, and
[`docs/lab-notebook.md`](docs/lab-notebook.md) for the run-by-run history
behind it.
