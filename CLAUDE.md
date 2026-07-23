# llm-eval-harness

A from-scratch eval harness for scoring prompt/model quality across multiple task shapes (bug-triage classification, code generation, more to come). Built as a learning project — the point is understanding evals well enough to trust (or distrust) later, more ambitious LLM projects, particularly `llm-task-router` (sibling repo, `../llm-task-router`), a cross-model-tier router that routes a task to the cheapest model tier clearing a per-category quality floor. `llm-task-router/classifier.py`'s task types (`triage, code_gen, summarization, multi_step, code_review, refactor, architecture`) are the roadmap for which suites get added here next. This repo's benchmark runs *are* how that quality floor gets measured — `llm-task-router/tiers.py`'s tier→model mapping should only be populated from real runs recorded here, never guessed. See the `calibrate-tier` skill in this repo for that workflow, and `llm-task-router/CLAUDE.md`'s "Adding a provider" section for the other side of it.

## Why it's built this way

- **No `anthropic` API key, no separate billing.** Every model call shells out to headless `claude -p` (see `eval_harness/claude_cli.py`), which runs on the existing Claude Code subscription instead of pay-per-token API billing. This was a deliberate cost/learning tradeoff, not an oversight — see conversation history for the reasoning if it ever needs revisiting.
- Each `claude -p` call strips the default system prompt and disables tools/MCP (`--system-prompt`, `--disallowed-tools "*"`, `--strict-mcp-config`). Without that, every call pays for and caches Claude Code's full system prompt (~$0.07/call observed); stripped down it's ~$0.003-0.005/call. Don't remove those flags without expecting a real cost jump.
- Zero third-party dependencies (no `pydantic`, no `anthropic` SDK) — stdlib only (`dataclasses`, `json`, `subprocess`, `argparse`). Deliberate, to keep this cheap and dependency-free.
- **Codex is a second provider, not a second harness.** `eval_harness/codex_cli.py` shells out to headless `codex exec` the same way `claude_cli.py` shells out to `claude -p` — same cost-avoidance rationale (subscription CLI, not API key). `runner.py`/`cli.py` take a `--provider` flag; `scorers.py`'s rule scorers/`judge_score` are completely unaware of which provider produced a `ModelOutput` and needed zero changes. The judge call in `judge_score` stays hardcoded to Claude regardless of provider under test, on purpose — grading Codex output with a different model lineage than the one under test avoids correlated errors (same-family models can converge on the same wrong answer).
- **The schema is suite-agnostic, the scoring isn't.** `TestCase`/`ModelOutput`/`ScoredResult` use generic `input`/`expected`/`predicted`/`checks` dicts (not fields hardcoded to one task shape) so `runner.py`, `report.py`, and `cli.py` don't need to change per suite. What counts as "correct" genuinely differs per suite though (label match vs. tests passing), so `scorers.py` keeps a small `_RULE_SCORERS` registry of suite-specific rule scorers behind that shared schema. `judge_score` (reasoning coherence) is suite-agnostic and shared by all suites — grading whether stated reasoning is grounded doesn't change shape across task types.

## Architecture

```
eval_harness/
  schema.py      - TestCase, ModelOutput, ScoredResult, RunSummary dataclasses (suite-agnostic)
  cases/*.jsonl  - golden test sets, one JSON object per line, one file per suite (bug_triage, code_gen)
  prompts/*.txt  - versioned system prompts under test, suite-scoped by naming convention (v1_naive/v2_rubric for bug_triage, code_gen_v1 for code_gen)
  claude_cli.py  - subprocess wrapper around `claude -p --output-format json`
  codex_cli.py   - subprocess wrapper around `codex exec --output-last-message`
  jsonutil.py    - robust JSON extraction (models sometimes wrap/prefix JSON with stray text)
  sandbox.py     - runs model-generated code against a pytest file in a temp dir, for suites (code_gen) that grade by execution rather than label match
  runner.py      - runs a prompt version against every case in a suite, for either provider
  scorers.py     - suite-specific rule scoring (_RULE_SCORERS registry) + one shared judge_score (LLM-as-judge on reasoning coherence)
  report.py      - aggregates a run, persists it to runs/, diffs against the last run for the same prompt+model
  cli.py         - `python -m eval_harness run|compare`
runs/            - one JSON file per run, named {timestamp}__{prompt}__{model}.json - never hand-edit these
```

## Why two scorers, not one

Rule scoring checks whether the answer matches the golden set (label match for `bug_triage`, tests-passing for `code_gen`). `judge_score` separately checks whether the model's stated one-sentence reasoning actually holds up — a model can land on the right answer for reasoning that doesn't support it, or a defensible-but-different answer with solid reasoning. Aggregate accuracy alone hides both. Don't remove either scorer without a reason; they catch different failure modes (see the bt-02 case in run history for a real example of a label-correct-but-reasoning-wrong case the judge caught).

## Adding a new eval suite

1. Add `eval_harness/cases/<suite>.jsonl` - one `TestCase` per line: `id`, `input` (the text shown to the model), `expected` (a dict whose shape is suite-specific - e.g. `{"severity": ..., "category": ...}` for a classification suite, `{"test_code": ...}` for a suite graded by execution), `notes`.
2. Add prompt version(s) to `eval_harness/prompts/`, named suite-first (e.g. `<suite>_v1.txt`) so it's unambiguous which suite a prompt targets - there's no runtime check tying a prompt to a suite. The output contract models must follow: `{"reasoning": "...", ...suite-specific fields...}` - the `reasoning` key is required (the judge reads it), everything else is suite-specific.
3. Add a `rule_based_score_<suite>(case, output) -> dict[str, bool]` function to `scorers.py` and register it in `_RULE_SCORERS`. If grading needs executing the model's output (like `code_gen`'s `sandbox.run_pytest_check`), see "Sandboxed execution trust model" below before adding a new execution path.
4. Don't stop at cases that check one obvious requirement each - `code_gen`'s first 8 cases were exactly that and turned out to have almost no power to discriminate between models (see "`code_gen.jsonl` case design" below). Include cases that target known failure modes for the task type, and validate every case two ways before trusting it: a correct reference implementation must pass, and at least one plausible-but-wrong implementation must fail.
5. `python -m eval_harness run --suite <suite> --prompt <version> --model haiku`

### Sandboxed execution trust model

`code_gen` is the first suite where "correct" means "passes tests" rather than "matches a label," so grading it means running model-generated code. `sandbox.py` does this with `subprocess` + a wall-clock timeout, no container - generated code runs directly on this machine, the same trust model this repo already extends to `claude -p`/`codex exec` output. That's a deliberate choice for a personal project benchmarking models already trusted enough to run other commands on this account; it would need real sandboxing (network-disabled container, resource caps) before ever grading untrusted third-party input. Any new suite that grades by execution should reuse `sandbox.run_pytest_check` (or extend it) rather than adding a second ad hoc execution path.

Cases that would need to actually *trigger* a vulnerability to prove it (e.g. real command injection through a real shell) don't fit this trust model - `cg-14` grades a command-injection case by mocking `subprocess.run` and inspecting how it was called, never letting a malicious payload reach a real shell. Any new security-flavored case should follow that pattern (structural inspection via mocking) rather than attempting real exploitation, even sandboxed.

### `code_gen.jsonl` case design (17 cases, as of 2026-07-23)

`cg-01` through `cg-08` are baseline cases - each checks a single, mostly-obvious requirement from the spec (a boundary condition, an empty-input case, etc.). `cg-09` through `cg-17` were added specifically because the baseline set turned out to have almost no discriminating power across models (see the calibration table below) - they target known LLM code-gen failure modes the same way `bug_triage.jsonl`'s cases target known severity-judgment biases:

- **Silent/ambiguous error contracts** (`cg-09`, `cg-10`) - spec explicitly says to raise on bad input; tests whether the model actually does, instead of defaulting to a silent `None`/default-value return. Direct analog to `bug_triage`'s "silent failure" theme (bt-10).
- **Classic language footguns** (`cg-11`, `cg-12`) - the mutable-default-argument trap, and float/currency rounding that must be applied explicitly rather than left as raw arithmetic.
- **Security-sensitive generation** (`cg-13`, `cg-14`) - SQL built via parameterized placeholders vs. f-string interpolation; a shell command invoked as an arg list vs. `shell=True` string interpolation. No `bug_triage` analog - this dimension is specific to a suite that actually writes code.
- **Performance-aware specs** (`cg-15`) - the spec states a scale requirement (up to 200k elements); grading relies on the sandbox's own timeout to fail an O(n²) implementation on the large-input test case rather than needing bespoke timing assertions.
- **Multi-branch rule-following** (`cg-16`, `cg-17`) - a spec with several ordered conditional rules stated narratively (English pluralization; case/whitespace-insensitive dedup), where a naive implementation satisfies the "obvious" cases but breaks on one that requires actually generalizing the rule rather than pattern-matching examples. Same family as `cg-08`.

Every case here (baseline and adversarial) was validated two ways before being trusted as calibration data: a correct reference implementation passes its `test_code`, and at least one plausible-but-wrong implementation fails it - confirming the test actually discriminates rather than just being satisfiable by anything.

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
- Run files saved before the schema generalization (everything predating `code_gen` support) have `severity_accuracy`/`category_accuracy` instead of `check_accuracies`. `find_previous_run` migrates them on load - don't hand-edit files in `runs/` to "fix" this.
- The `prompt`↔`suite` pairing is convention-only (naming, e.g. `code_gen_v1` vs `v1_naive`), not enforced. Running a suite against a prompt built for a different suite won't error - it'll just produce a garbage-but-confident-looking run. Double-check `--prompt` matches `--suite` before trusting a run's numbers.

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

## Router tier calibration status (`code_gen` / `code_gen_v1`, as of 2026-07-23)

Judged benchmark across the same 7 models as the `bug_triage` table above,
run right after `code_gen` was added.

| provider | model | tests_passed | fully correct | judge coherence |
|---|---|---|---|---|
| claude | haiku (cheap) | 100.0% | 100.0% | 0.91 |
| claude | sonnet (mid) | 100.0% | 100.0% | 0.78 |
| claude | opus (flagship) | 100.0% | 100.0% | 0.82 |
| codex | gpt-5.4-mini | 100.0% | 100.0% | 0.82 |
| codex | gpt-5.6-luna | 87.5% | 87.5% | 0.90 |
| codex | gpt-5.6-terra | 100.0% | 100.0% | 0.88 |
| codex | gpt-5.5 | 87.5% | 87.5% | 0.81 |

**Verdict: this suite doesn't have enough discriminating power yet to make
a tier call - don't add or change any `tiers.py` entry off this table.** 5
of 7 models score 100% on 8 cases; the only two misses (`gpt-5.6-luna`,
`gpt-5.5`) both landed on the exact same case (`cg-07`, the tie-break case)
and for two unrelated real bugs, not a shared weakness:
`gpt-5.6-luna` double-escaped a backslash in its JSON response (emitted
`\\\\W` where `\\W` was needed, corrupting a regex's `\W` shorthand into a
literal-character exclusion instead of "non-word-character"), while
`gpt-5.5` implemented the tie-break with `if count > best_count` (strict
inequality), which never lets a later word overtake an earlier one on a
tie even though the spec calls for first-occurrence-wins. Confirmed both
are real generated-code defects (not harness/grading bugs) by re-running
each verbatim `predicted["code"]` from the saved run file through
`sandbox.run_pytest_check` directly. With only 1 of 8 cases producing any
variance across 7 models, this table says more about the suite (too small
and too easy at n=8) than about the models - the fix is a bigger, harder
`code_gen.jsonl`, not a router change. Stopping here deliberately rather
than shipping a tier decision off underpowered data.
