# llm-eval-harness

A from-scratch eval harness for scoring prompt/model quality across seven task
shapes (`bug_triage`, `code_gen`, `summarization`, `code_review`, `refactor`,
`multi_step`, `architecture`). Built as a learning project - the point is
understanding evals well enough to trust (or distrust) later, more ambitious LLM
projects, particularly `llm-task-router` (sibling repo, `../llm-task-router`).

**This repo owns calibration data; `llm-task-router` owns the routing decision.**
That repo's `tiers.TIER_MODELS` and `classifier.TYPE_DOMAIN_GRID` should only
ever be populated from real runs recorded here, never guessed - see the
`calibrate-tier` skill for the workflow. The seven suites above map one-to-one
onto that repo's task types, deliberately.

**History lives in `docs/lab-notebook.md`**, archived out of this file on
2026-07-27 (it had grown to ~1,250 lines, re-read in full every session). That
notebook holds the run-by-run record, per-case design rationale, and the dead
ends. This file is current state only. If the two disagree, this file wins.

## Why it's built this way

- **No `anthropic` API key, no separate billing.** Every model call shells out
  to headless `claude -p` (`eval_harness/claude_cli.py`), running on the
  existing Claude Code subscription instead of pay-per-token API billing. A
  deliberate cost/learning tradeoff, not an oversight.
- Each `claude -p` call strips the default system prompt and disables tools/MCP
  (`--system-prompt`, `--disallowed-tools "*"`, `--strict-mcp-config`). Without
  that, every call pays for and caches Claude Code's full system prompt
  (~$0.07/call observed); stripped it's ~$0.003-0.005/call. Don't remove those
  flags without expecting a real cost jump. (`llm-task-router` deliberately does
  the opposite in *its* adapter - different consumption pattern, separate file.)
- **Zero third-party dependencies** - stdlib only (`dataclasses`, `json`,
  `subprocess`, `argparse`). This constraint has already decided one real design
  question; see "regex, not AST or embeddings" below.
- **Codex is a second provider, not a second harness.** `codex_cli.py` shells
  out to `codex exec` the same way. `runner.py`/`cli.py` take `--provider`;
  `scorers.py` is entirely unaware of which provider produced a `ModelOutput`.
  The judge call stays hardcoded to Claude regardless of provider under test, on
  purpose - grading Codex output with a different model lineage avoids
  correlated errors (same-family models converge on the same wrong answer).
- **The schema is suite-agnostic, the scoring isn't.** `TestCase`/`ModelOutput`/
  `ScoredResult` use generic `input`/`expected`/`predicted`/`checks` dicts, so
  `runner.py`, `report.py`, and `cli.py` never change per suite. What counts as
  "correct" genuinely differs per suite, so `scorers.py` keeps a `_RULE_SCORERS`
  registry behind that shared schema.

## Architecture

```
eval_harness/
  schema.py      - TestCase, ModelOutput, ScoredResult, RunSummary (suite-agnostic)
  cases/*.jsonl  - golden test sets, one JSON object per line, one file per suite
  prompts/*.txt  - versioned system prompts, suite-scoped by naming convention
                   (v1_naive/v2_rubric for bug_triage, <suite>_v1 for the rest)
  claude_cli.py  - subprocess wrapper around `claude -p --output-format json`
  codex_cli.py   - subprocess wrapper around `codex exec --output-last-message`
  jsonutil.py    - robust JSON extraction (models wrap/prefix JSON with stray text)
  sandbox.py     - runs model-generated code against a pytest file in a temp dir,
                   for suites (code_gen, refactor) that grade by execution
  runner.py      - runs a prompt version against every case in a suite
  scorers.py     - _RULE_SCORERS registry + one shared judge_score
  report.py      - aggregates a run, persists to runs/, diffs against the last run
  cli.py         - `python -m eval_harness run|compare`
runs/            - one JSON per run, {timestamp}__{prompt}__{model}.json - never hand-edit
docs/            - lab-notebook.md (archived history)
```

## Why two scorers, not one

Rule scoring checks whether the answer matches the golden set. `judge_score`
separately checks whether the model's stated reasoning actually holds up - a
model can land on the right answer via reasoning that doesn't support it, or a
defensible-but-different answer with solid reasoning. Aggregate accuracy alone
hides both. Don't remove either; they catch different failure modes.

The sharpest real example: on `sum-15`, opus scored 0.15 judge coherence because
its stated reasoning ("kept all numbers... within the 15-word limit")
contradicted its own 18-word output. Rule scoring saw one `length_ok` failure;
the judge saw the model misreporting what it had done.

## Adding a new eval suite

1. Add `eval_harness/cases/<suite>.jsonl` - one `TestCase` per line: `id`,
   `input`, `expected` (shape is suite-specific), `notes`. **Put the per-case
   rationale in `notes`** - baseline vs. adversarial, and which failure mode it
   targets. That field is the canonical per-case documentation; don't restate it
   in this file (that duplication is most of what got archived).
2. Add prompt version(s) to `eval_harness/prompts/`, named suite-first
   (`<suite>_v1.txt`) - there's no runtime check tying a prompt to a suite. The
   output contract: `{"reasoning": "...", ...suite-specific fields...}`. The
   `reasoning` key is required (the judge reads it).
3. Add `rule_based_score_<suite>(case, output) -> dict[str, bool]` to
   `scorers.py` and register it in `_RULE_SCORERS`. Every check key must be
   present on every case even when that case's constraint list is empty (a
   vacuous pass), so `check_accuracies` aggregates the same key set across cases.
4. **Validate every case two ways before trusting it as calibration data**: a
   correct reference answer must pass, and at least one plausible-but-wrong
   answer must fail. `code_gen`'s first 8 cases skipped this discipline's intent
   and turned out to have almost no power to discriminate between models. Don't
   stop at one obvious requirement per case; target known failure modes for the
   task type.
5. `python -m eval_harness run --suite <suite> --prompt <version> --model haiku`

### Sandboxed execution trust model

`sandbox.py` runs model-generated code with `subprocess` + a wall-clock timeout,
no container - generated code runs directly on this machine, the same trust
model this repo already extends to `claude -p`/`codex exec` output. Deliberate
for a personal project benchmarking models already trusted to run other commands
on this account; it would need real sandboxing (network-disabled container,
resource caps) before grading untrusted third-party input. Any new
execution-graded suite should reuse `sandbox.run_pytest_check`, not add a second
ad hoc execution path.

Cases that would need to actually *trigger* a vulnerability to prove it don't
fit this trust model - `cg-14` grades command injection by mocking
`subprocess.run` and inspecting how it was called, never letting a payload reach
a real shell. New security-flavored cases should follow that pattern
(structural inspection via mocking), not attempt real exploitation.

## How each suite is graded

Shared across every non-label suite: there's no golden answer to string-match,
so grading uses **phrase groups** - a list of acceptable phrasings for one
required idea, any of which counts as a match. Never a single literal string;
model outputs paraphrase. `judge_score` is suite-agnostic and applies to all.

| suite | "correct" means | suite-specific mechanism |
|---|---|---|
| `bug_triage` | exact case-insensitive label match | `severity` + `category` |
| `code_gen` | generated code passes the case's pytest file | `sandbox.run_pytest_check` |
| `refactor` | tests still pass **and** the smell is gone | `tests_passed` (hard gate) + `smells_removed`, a structural regex check over the generated text: `not_contains`, or `max_occurrences` for "consolidate to one place" rather than "delete" |
| `summarization` | required facts in, forbidden facts out, under the word cap | `must_include` (all groups), `must_exclude` (flat substrings), `max_words` |
| `code_review` | planted issues found, tagged right, no false positives | `must_flag` groups carry `{"phrases", "severity"}`; `must_not_flag` is bait |
| `multi_step` | plan covers the needed steps in a workable order | `required_steps` groups (`phrases` + optional `patterns` regexes), `ordering_constraints` as `[earlier_idx, later_idx]` pairs, `must_not_include` |
| `architecture` | design meets stated constraints and weighs alternatives | `must_include` pooled over `design`+`reasoning`; `must_not_include` over `design` **only**; `tradeoffs_articulated` counts genuine `{option, rejected_because}` dicts, deduped by option |

Four scorer rules that are non-obvious and were each arrived at the hard way:

- **`code_review`'s `severity_correct` is scored only over groups actually
  caught** - severity of an issue the model never found is undefined. This is
  what makes "caught a real bug but tagged it a style nit" a distinct failure
  from a recall miss. Exception: if a case has planted issues and the model
  caught *none*, it fails outright rather than reporting a vacuous 100% next to
  a 0% recall, which read as deceptively clean in the aggregate table.
- **`multi_step`'s `ordering_correct` uses `<=`, not `<`.** A model that narrates
  two required ideas in one combined step lands both groups at the same index -
  that's a correct, un-ordered plan (nothing to order between two ideas in one
  sentence), not a reversed one. Confirmed independently in `ms-01`, `ms-08`, and
  `ms-12` before the scorer was changed. Same "only score what's evaluable" rule
  as `code_review` above: constraints where either group is missing aren't
  judged, but if *none* are evaluable it fails rather than vacuously passing.
- **`architecture`'s `must_not_include` checks `design` only**, excluding
  `reasoning` and `alternatives_considered`. A model that names a bad approach in
  order to reject it ("avoided a single-region deployment given the HA
  requirement") shouldn't fail for saying the bait phrase. Only the design being
  proposed is graded for containing an anti-pattern. (This exclusion is
  incomplete - see "Open scorer gaps".)
- **regex, not AST or embeddings.** Structural checks use regex to stay inside
  the stdlib-only constraint. Embeddings were evaluated empirically as a
  replacement for phrase matching and **rejected on the data**: word-form
  variance scored 0.91-0.95 cosine similarity (safe), but the actual motivating
  problem - concrete implementation nouns vs. abstract requirement phrasing
  (`"SQS"` vs `"durable queue"`, `"s-maxage=60"` vs `"short TTL"`) - scored
  0.54-0.60, while a same-case phrase that must *not* match scored 0.515. No
  threshold separates real synonyms from same-case noise on the exact problem
  they were brought in for, and a bigger model didn't improve the separation.
  Don't re-litigate this without new evidence.

**Suite scope boundaries**, each deliberately narrow to keep the suites from
overlapping: `code_review` finds issues in a self-contained snippet (not "does
this change fulfill a task"); `refactor` restructures without changing
observable behavior (not new-behavior-from-a-spec); `architecture` designs from
stated constraints (not critique of an existing design); `multi_step` grades a
plan's structure (not code). No suite gives the model repo or dependency
context - a real gap, accepted rather than solved.

## Commands

```
python -m eval_harness run --suite <suite> --prompt <version> --model haiku
python -m eval_harness run --suite bug_triage --prompt v1_naive --provider codex --model gpt-5.6-terra
python -m eval_harness run --suite multi_step --prompt multi_step_v1 --model haiku --samples 5
python -m eval_harness compare --prompt-a v1_naive --prompt-b v2_rubric --model haiku
```

- `--suite` defaults to `bug_triage`.
- `--provider` defaults to `claude`. `--model` has no cross-provider default:
  omitting it resolves to `haiku` for Claude but is a hard error for Codex
  (`resolve_model` raises rather than guessing) - a real run without `--model`
  once saved to disk as `codex/None`, useless as calibration data. Always pass
  an explicit `--model` for Codex.
- `--samples N` runs each case N times and reports a per-case pass rate. Cost
  scales linearly: an N=5 run over `multi_step`'s 12 cases (60 calls) costs
  ~$2.60-3.00.
- `--no-judge` skips the judge pass - useful when iterating on the runner or
  parsing rather than on prompt quality.

## Working rules

These were each learned from a specific incident (see the notebook) and are
restated here once instead of per-suite:

1. **Don't conclude on N=1, and don't patch a case on n=1 evidence.** Fix a
   phrase group only when the mechanism is *directly confirmed* - the colliding
   phrase visibly appears in unrelated text - or when it reproduces across
   samples. A single sample missing is sample-chasing, not a confirmed bug.
2. **An offline re-score verifies a fix; it never demonstrates generalization.**
   Re-scoring saved `predicted` dicts against a new scorer measures "did this fix
   behave as intended on the samples that motivated it." Every cycle so far has
   overstated the real gain by 11-13pp on a fresh draw. Always take a fresh run
   before any tier decision.
3. **Recognize a bogus run and delete it.** Every case coming back `parse_error`,
   or a flat-0.0 `avg_judge_score`, looks exactly like a real terrible score.
   Read the error text before believing a number. Causes seen: an inaccessible
   Codex model (400 on every call), an account-wide quota lockout, and transient
   `claude -p` failures. Delete these rather than leaving them in `runs/`.
4. **Validate every case two ways** before trusting it as calibration data (see
   "Adding a new eval suite" step 4).
5. **Widen phrase groups against real model output, never guessed phrasing** -
   and confirm the widening doesn't collide with a different step or finding
   before keeping it. Widenings have caused real regressions by matching an
   earlier, unrelated step and corrupting the ordering check.
6. **Stop patching and name the pattern.** Twice now (`architecture`'s
   concrete-vs-abstract vocabulary, `multi_step`'s phrase strictness) a run of
   individual phrase fixes turned out to be a structural scorer problem. When the
   third similar fix appears, that's the signal to change the scorer or the
   methodology, not to widen another list.
7. **Only populate `llm-task-router`'s tables from real runs recorded here.**

## Router tier calibration status

Claude tiers, `fully_correct` unless noted. One judged run per tier unless the
notes say otherwise. Don't re-derive this from `runs/`; it will drift as runs
accumulate.

| suite | date | haiku | sonnet | opus | judge coherence | trust |
|---|---|---|---|---|---|---|
| `bug_triage` | 07-23 | 60.0% | 66.7% | 73.3% | 0.85 / 0.85 / 0.84 | **shaky** - see below |
| `code_gen` (17 cases) | 07-24 | 100% | 100% | 100% | 0.91 / 0.70 / 0.79 | genuine |
| `summarization` | 07-24 | 87.5% | 62.5% | 75.0% | 0.85 / 0.82 / 0.80 | genuine, tier-inverting |
| `code_review` | 07-25 | 78.6% | 78.6% | 100% | 0.88 / 0.70 / 0.85 | N=1 |
| `refactor` | 07-25 | 100% | 100% | 100% | 0.88 / 0.87 / 0.90 | genuine |
| `architecture` | 07-25 | 91.7% | 91.7% | 58.3% | 0.83 / 0.87 / 0.79 | opus depressed by a confirmed scorer bug |
| `multi_step` | 07-27 | 36.7% (N=5) | — | — | — | scorer-limited; haiku only |

Verdicts that actually affect a decision:

- **`code_gen` and `refactor` show zero discrimination across tiers** - and this
  is a real result, not a ceiling effect. Both suites were hardened with
  adversarial cases individually confirmed to fail plausible-wrong
  implementations (mutable default arguments, currency rounding, parameterized
  SQL, out-of-scope bug fixes, public-interface preservation). Claude handles
  them at every tier.
- **`summarization` inverts the ladder: haiku is the *best* tier here**, and
  every miss on every tier is a `length_ok` failure, not a fact error. All three
  tiers hit 100% on fact inclusion and hallucination avoidance across 16
  adversarial cases; the entire spread is word-count compliance, with sonnet the
  most verbose. **Don't read this as "use haiku for summarization" without
  checking prompt sensitivity first** - `summarization_v1.txt` already says to
  honor explicit length limits and sonnet/opus still overran, so a stronger
  prompt might close the gap without changing models.
- **`architecture`'s opus 58.3% is a scorer artifact, not a capability gap.**
  Two confirmed causes: one `ar-11` malformed-JSON generation failure (nothing to
  grade), and a suite-wide phrase-strictness pattern where opus answered with
  concrete implementation detail ("hot **store**", "**SQS**", "**`s-maxage=60`**")
  where the phrase lists wanted the abstract requirement vocabulary - arguably
  the more competent answer, penalized anyway. Do not act on this number.
- **`multi_step`'s trusted number is 36.7%** (fresh N=5,
  `runs/20260727T024430Z__multi_step_v1__haiku.json`). A later offline re-score
  reads 52.5%; **that is not a real accuracy figure** - see working rule 2. The
  dominant failure mode remains scorer phrase-strictness on substantively correct
  plans, though two genuine model defects did surface: in 7-8 of 10 samples the
  model backfills *before* deploying dual-write code (losing any row written in
  that window), and `ms-03` plans jump from staging straight to a live canary
  with no shadow-traffic step.
- **`bug_triage`'s monotonic ladder is thinner than it reads.** Three haiku
  `v1_naive` runs exist on disk, not one: the canonical judged run (60.0%) plus
  two rule-scored `--no-judge` runs at 33.3% and 40.0%. Sonnet and opus have one
  run each. This row is N=1-per-tier with a real unreconciled conflict.
- **`code_review`: opus is clean at 100%; sonnet doesn't earn its premium over
  haiku** (identical 78.6%, worse judge coherence). N=1, so directional only.

### Codex: no model clears the cheap-tier floor

Only `bug_triage` has a Codex leg (`v1_naive`, 2026-07-23, `fully_correct`):
`gpt-5.4-mini` 40.0%, `gpt-5.6-luna` 46.7%, `gpt-5.6-terra` 46.7%, `gpt-5.5`
60.0%. **Every one is at or below haiku's 60.0%**, and `gpt-5.5` - the closest -
has the weakest judge coherence in the table (0.79). A real negative result, not
a coverage gap. Don't add a Codex entry to `tiers.py` off this.

**Codex model access is account-dependent and was probed directly, not
assumed.** On this ChatGPT-account login: `gpt-5.4-mini`, `gpt-5.6-luna`,
`gpt-5.6-terra`, `gpt-5.5` are reachable; `gpt-5.6-sol` (the flagship-equivalent
slug), `gpt-5.3-codex`, `gpt-5.1-codex-mini`, `gpt-5.4-nano`, `gpt-5.4`, and
`gpt-5.2` all fail with a 400 `invalid_request_error` ("not supported when using
Codex with a ChatGPT account"). A different account/plan may differ - re-probe
with one cheap `codex exec --model <name>` call before running a full suite.

**Codex legs since `code_gen` are blocked account-wide until 2026-08-22** - the
ChatGPT account's Codex usage quota is exhausted ("try again at Aug 22nd,
2026"), confirmed against a direct `codex exec` call unrelated to any harness
code. Don't attempt a Codex leg before then; it fails regardless of model.

## Router tier synthesis across all 7 suites (2026-07-27)

First cross-suite reconciliation of `llm-task-router/classifier.py`'s
`TYPE_DOMAIN_GRID` (task_type x domain -> tier) against real data. That table
was hand-authored before any calibration run existed.

**Changed: `code_gen` and `refactor` rows -> uniform `L`.** Both show zero
measured discrimination across tiers on hardened suites (above), so the prior
`H`/`M` cells reflected a heuristic guess, not a demonstrated need for a pricier
tier. A conservative `H`->`M` middle ground was considered and rejected - `M` is
exactly as unsupported by this data as `H`, just a smaller unjustified expense.
**Caveat:** neither suite is domain-segmented, so this removes a domain-*blind*
judgment call; it does not establish that e.g. Terraform/K8s code-gen is safe at
the cheap tier.

**Unchanged, each with a specific unblock condition:**

| row | state | unblock |
|---|---|---|
| `summarization` | already uniform `L`, and data confirms haiku is the *best* tier | none needed |
| `triage` | `L`/`M` split; confidence downgraded to "directionally plausible" | `--samples N` re-run reconciling the three conflicting haiku numbers |
| `code_review` | `M` cells look questionable (sonnet ties haiku, worse coherence) | `--samples N` re-run; N=1 isn't actionable per working rule 1 |
| `architecture` | uniform `H`; highest-stakes row (flagship fallback for unrecognized task shapes) | fix the `must_not_include` scoping gap below, then re-run |
| `multi_step` | `M`/`H`; zero sonnet/opus data exists at all | a sonnet/opus run |

## Open scorer gaps

Flagged, understood, deliberately not fixed yet:

- **`architecture`'s `must_not_include` scoping is incomplete.** It excludes
  `alternatives_considered` so a named-and-rejected anti-pattern isn't penalized,
  but haiku, sonnet, and opus *all independently* wrote their rejection inline in
  `design`'s own prose ("Explicitly not included, and why: no Kubernetes...").
  Three models, same pattern. Fix: pool `design` + `alternatives_considered`'s
  `rejected_because` text for the check. This directly deflates `architecture`'s
  opus number and blocks that grid row.
- **`multi_step`'s `first_match_index` can't distinguish a step's primary content
  from an incidental mention.** A group whose phrase appears as a forward
  reference in an earlier step ("...that the shared template must support")
  matches the wrong index and corrupts the ordering check. Needs a
  primary-content notion, not another phrase edit.
- **`multi_step` phrase coverage is still the binding constraint**, specifically
  `ms-10`'s abstract completion phrasing ("mark rollout as complete") and
  `ms-03`'s staged-rollout percentages (a bare percentage regex was tried twice
  and rejected both times - the case's own shadow step mentions "100% of
  transactions" for an unrelated purpose).
- **`claude_cli.py` has no retry/backoff.** The judge pass has died mid-run twice
  in two different suites, and one `multi_step` run had 13 of 60 *generation*
  calls fail outright. Each time the run had to be deleted and re-paid for. This
  has crossed from fluke to real reliability gap.

## Known rough edges

- `find_previous_run` in `report.py` reconstructs only aggregate metrics from
  saved JSON, not per-case results - fine for the diff display, but don't assume
  `previous.results` is populated.
- Codex runs always report `total_cost_usd: 0.0`. No dollar-cost field exists
  anywhere in `codex exec`'s output (confirmed against a real authenticated
  call); only `duration_ms` is real there, self-measured via
  `time.perf_counter()`. Don't compare `total_cost_usd` across a Claude run and a
  Codex run.
- Run files predating the schema generalization have
  `severity_accuracy`/`category_accuracy` instead of `check_accuracies`.
  `find_previous_run` migrates them on load - don't hand-edit `runs/` to "fix"
  this.
- The `prompt`<->`suite` pairing is convention-only (naming), not enforced.
  Running a suite against another suite's prompt won't error, it'll produce
  garbage-but-confident-looking output. Check `--prompt` matches `--suite`.
- `claude_cli.py`'s subprocess timeout is **240s**, raised from 60s in two steps.
  Long-form prose suites needed it: `architecture` timed out on 3 of 12 haiku
  cases at 120s. A timeout that's fine for a one-paragraph review isn't
  automatically fine for a several-hundred-word design writeup.
