# llm-eval-harness

A from-scratch eval harness for scoring prompt/model quality across multiple task shapes (bug-triage classification, code generation, summarization, more to come). Built as a learning project — the point is understanding evals well enough to trust (or distrust) later, more ambitious LLM projects, particularly `llm-task-router` (sibling repo, `../llm-task-router`), a cross-model-tier router that routes a task to the cheapest model tier clearing a per-category quality floor. `llm-task-router/classifier.py`'s task types (`triage, code_gen, summarization, multi_step, code_review, refactor, architecture`) are the roadmap for which suites get added here next. This repo's benchmark runs *are* how that quality floor gets measured — `llm-task-router/tiers.py`'s tier→model mapping should only be populated from real runs recorded here, never guessed. See the `calibrate-tier` skill in this repo for that workflow, and `llm-task-router/CLAUDE.md`'s "Adding a provider" section for the other side of it.

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
  cases/*.jsonl  - golden test sets, one JSON object per line, one file per suite (bug_triage, code_gen, summarization, code_review, refactor, multi_step)
  prompts/*.txt  - versioned system prompts under test, suite-scoped by naming convention (v1_naive/v2_rubric for bug_triage, code_gen_v1 for code_gen, summarization_v1 for summarization, code_review_v1 for code_review, refactor_v1 for refactor, multi_step_v1 for multi_step)
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

### Reference-free scoring: `summarization`'s fact-constraint grading

`summarization` is the first suite with no golden label and no executable spec to grade against - there's no single "correct" summary to string-match. `rule_based_score_summarization` (in `scorers.py`) grades by fact-level constraints on the `expected` dict instead of a literal comparison:

- `must_include`: a list of *groups*, each group a list of acceptable phrasings for one required fact (any phrase in the group counts as a match - summaries paraphrase, so this can't be a single literal string). All groups must match for `key_facts_included` to pass.
- `must_exclude`: a flat list of substrings that must not appear - used for hallucinated facts, reversed directions of change (e.g. "increased" when the source decreased), or misattributed claims.
- `max_words`: a word-count ceiling on the `summary` field, checked as `length_ok`.

All three checks are always present in every case (even when a case's `must_include`/`must_exclude` is empty) so `check_accuracies` aggregates the same key set across cases, same as `bug_triage`'s fixed `severity`/`category` pair.

This grading style is more paraphrase-fragile than `code_gen`'s executable tests - a model can state a required fact in wording the case author didn't anticipate and get marked wrong. The first real haiku run caught exactly this: `sum-08`'s `must_exclude` originally included the bare phrase `"root cause is"`, which matched the model's *correct* "the root cause is unknown" and scored a hallucination-free summary as a hallucination; `sum-05`'s `must_include` required `"rolled back"`/`"reverted"` but not the gerund `"rolling back"`, which a real model output used. Both were case-design bugs, not model failures - fixed by widening the phrase groups, not by changing the scorer. Any new `must_include`/`must_exclude` entry should be validated the same two ways as `code_gen` cases (a correct reference summary passes, a plausible-wrong one fails) *and* sanity-checked against at least one real model output before trusting a run's numbers - substring matching on free text has more false-positive surface than label match or test execution.

### `summarization.jsonl` case design (16 cases, as of 2026-07-24)

`sum-01` through `sum-06` are baseline cases - each checks one obvious requirement (a decision + date, a numeric fact, a list of independent items, etc.). `sum-07` through `sum-16` target known summarization failure modes, the same way `code_gen.jsonl`'s adversarial cases target known code-gen failure modes:

- **Negation dropping** (`sum-07`) - source states a fix explicitly did *not* work; tests whether the model reports the true (negative) outcome instead of pattern-matching "a fix was applied" into "the fix worked."
- **Hallucination bait** (`sum-08`) - source explicitly says the cause is unknown; tests whether the model invents a plausible-sounding cause anyway rather than preserving the uncertainty.
- **Superseded information** (`sum-09`) - source gives a preliminary estimate, then corrects it; tests whether the summary reports the corrected figure as final rather than the stale initial one.
- **Direction-of-change inversion** (`sum-10`) - tests whether "decreased"/"increased" survives compression, since both use similar-sounding numeric phrasing.
- **Misattribution** (`sum-11`) - two named people hold opposing positions; tests whether the person-to-position mapping survives compression instead of getting swapped.
- **Scope/qualifier dropping** (`sum-12`) - source narrows applicability (new customers only, one country, one date); tests whether an overgeneralized summary drops the qualifier.
- **Critical detail buried in filler** (`sum-13`) - a routine-sounding update buries one urgent finding among unrelated administrative items; tests whether the summary surfaces it rather than treating everything as equally weighted.
- **Exact-number preservation** (`sum-14`) - tests whether precise pass/fail counts survive, instead of rounding into vague language ("most passed, a few failed") that loses the actual scale.
- **Explicit length-constraint compliance** (`sum-15`) - the source text itself issues a hard word-limit instruction; tests whether the standout fact survives an aggressive 15-word compression.
- **Sentiment preservation** (`sum-16`) - source is unambiguously negative feedback; tests whether the summary keeps that framing rather than neutralizing it toward a more diplomatic tone.

Every case (baseline and adversarial) was validated two ways before being trusted as calibration data: a hand-written correct summary passes all three checks, and a hand-written plausible-wrong summary fails at least one - same discipline as `code_gen`, adapted to free text via the fact-constraint grading described above.

### `code_review`'s scope, and severity tagging

`code_review` grades whether a model can find planted issues in a single, self-contained Python snippet - it does not grade "does this change fulfill a stated task" (that's a superset problem needing a task description + diff, and would need `code_gen`'s execution-based grading to actually verify behavior, not just review it). `code_review` was deliberately scoped down to "find issues in a snippet" first, since that's a prerequisite for judging fulfillment anyway. No repo/dependency context is given to the model, same limitation `code_gen` already accepts (see "Adding a new eval suite" above) - a real gap, not a v1 blocker.

Like `summarization`, there's no golden answer to string-match - `rule_based_score_code_review` (in `scorers.py`) grades by fact-constraint groups, same shape as `summarization`'s `must_include`/`must_exclude`:

- `must_flag`: a list of planted-issue groups, each `{"phrases": [...], "severity": "bug"|"security"|"style"}`. Any phrase in a group counts as catching that issue (reviews paraphrase, same reasoning as `summarization`'s groups). All groups must be caught for `issues_flagged`.
- `must_not_flag`: a flat list of substrings that must not appear in any finding - false-positive bait (a plausible-looking non-issue, or an over-generalized nitpick).
- `severity_correct` is evaluated only over groups that were actually caught, not all groups - severity of an issue the model never found is undefined. This is what makes "caught a real bug but tagged it as a minor style nit" (the original motivating complaint - not all flagged issues get the priority they deserve) score as a `severity_correct` failure distinct from a pure recall miss on a *different* group. The one exception: if a case has planted issues and the model caught none of them at all, `severity_correct` fails outright rather than reporting a vacuous 100% next to a 0% `issues_flagged` - that combination read as deceptively clean in the aggregate `check_accuracies` table, since there's no actual tagging to have gotten right. Partial catches keep the narrower, independent signal. See the scorer's docstring for the same rationale in code.

Severity has three tiers - `security`, `bug`, `style` - chosen instead of a bug_triage-style numeric/urgency scale specifically so it maps directly onto router tiers later (security/bug findings matter more than style, and are expected to be rarer per real-world defect distributions - see conversation history). No sub-severity within a tier (e.g. no "critical" vs "minor" bug) to avoid inviting the model into follow-up-question territory that would itself need routing.

### `code_review.jsonl` case design (14 cases, as of 2026-07-24)

`cr-01` through `cr-06` are baseline cases - one planted issue each, one per severity tier repeated across bug/security/style so each tier has multiple examples. `cr-07` through `cr-14` target known review failure modes, the same way `code_gen.jsonl`'s and `summarization.jsonl`'s adversarial cases target known failure modes for their task types:

- **Multi-issue recall** (`cr-07`) - two independent security issues (SQL injection, cardnumber logged in cleartext) in one snippet; tests whether the model reports both instead of stopping at the first obvious one - directly targets "not all errors were resolved/caught."
- **Justified pattern, not a bug** (`cr-08`) - a caught exception returning a default looks like the silent-failure smell from `bug_triage`/`code_gen`, but an inline comment establishes it's the documented, intentional contract; tests whether the model over-flags on pattern-match alone versus actually reading the justification.
- **Silent failure, no justification** (`cr-09`) - the same shape as `cr-08` but without the comment, so it should be flagged; the pair only works as a discriminating signal because both were validated to differ in exactly one respect.
- **Resource leak** (`cr-10`) - a file opened without `close()`/context manager.
- **Concurrency bug requiring actual reasoning** (`cr-11`) - a check-then-act race condition that's only wrong under concurrent access; not pattern-matchable from a keyword the way most other cases are, since the code reads correctly in isolation.
- **Hardcoded secret** (`cr-12`) - a live-looking API key committed directly in source.
- **False-positive bait via correct recursion** (`cr-13`) - properly-bounded recursion that superficially resembles an infinite-recursion risk; tests whether the model verifies the base case before flagging.
- **Combined severity-mistagging target** (`cr-14`) - a real SQL injection, a plaintext-password comparison, and a genuine PascalCase naming nit all in one snippet; the case that most directly stresses `severity_correct` versus `issues_flagged`, since a model that catches the injection but calls it "style" passes recall while failing severity.

Every case (baseline and adversarial) needs the same two-way validation discipline as `code_gen`/`summarization` before being trusted as calibration data - not yet run against a real model, so this set hasn't been calibration-validated yet. First real run should double-check every `must_flag`/`must_not_flag` phrase group against actual model output the same way `summarization`'s `sum-05`/`sum-08` case-design bugs were caught, before trusting any cross-model comparison drawn from it.

### `refactor`'s scope, and hybrid execution + structural grading

`refactor` grades whether a model can clean up a single, self-contained Python snippet (deduplicate, extract a magic number, remove dead code, fix a footgun) while leaving its externally observable behavior unchanged - restructuring, not `code_gen`'s new-behavior-from-a-spec or `code_review`'s find-but-don't-fix. Same no-repo-context limitation as `code_gen`/`code_review` - a real gap, not a v1 blocker.

Unlike `code_review`/`summarization`, this suite reuses `code_gen`'s execution-based grading (`sandbox.run_pytest_check`) as a hard gate - `tests_passed` fails outright if the refactor breaks the existing test suite, no exceptions. But execution alone can't tell a real refactor from a no-op: returning the original code verbatim would pass every `tests_passed` check. `rule_based_score_refactor` (in `scorers.py`) adds a second, independent check - `smells_removed` - a structural inspection of the generated code text via `case.expected["structural_checks"]`, a list of typed checks:

- `not_contains`: a regex that must not appear anywhere post-refactor (dead code, a mutable-default-argument signature).
- `max_occurrences`: a regex that's fine to still appear, but only up to `max` times - the shape needed for "consolidate this magic number/duplicated expression into one place" rather than "delete it entirely."

Some cases (the bug-preservation and interface-preservation adversarial cases below) plant `[]`/no `structural_checks` at all and rely entirely on `tests_passed` - the discriminating signal there is behavioral, not structural, so `smells_removed` is vacuously true, same reasoning as `summarization`'s always-present checks with an empty constraint list (see "Why two scorers, not one" and the `check_accuracies` consistency requirement).

This is the first suite where a rule scorer inspects the model's raw code text directly (not just its execution result or its prose) - regex was chosen over an AST-based check to stay consistent with the repo's stdlib-only, no-new-import discipline (see "Why it's built this way"); it's more whitespace/formatting-fragile than an AST check would be, so every `structural_checks` pattern was validated against both a hand-written correct refactor and a plausible-wrong one (see case design below) before being trusted, same discipline as every other suite's constraint groups.

### `refactor.jsonl` case design (12 cases, as of 2026-07-24)

`rf-01` through `rf-06` are baseline cases - each plants one structural smell (duplicated loop/logic, a repeated magic number, dead code, a mutable default argument, redundant `== True`/`== False` comparisons, a duplicated validation message across two functions) with a `structural_checks` entry that would catch a no-op "refactor." `rf-07` through `rf-12` target failure modes specific to refactoring - fixing behavior is a genuinely different kind of mistake here than in `code_gen`, since the code already works and the risk is changing behavior that should have been left alone:

- **Bug preservation / scope discipline** (`rf-07`) - the code has a real off-by-one bug (skips the last element via `range(len(items) - 1)`), but the instruction only asks for a rename and the existing tests encode the current (buggy) output on purpose. Tests whether the model resists "helpfully" fixing behavior that's out of scope for a refactor task - no other suite has this failure mode, since `code_gen`/`bug_triage` both want the bug fixed and `code_review` only has to report it, never touch the code.
- **False-consolidation bait** (`rf-08`) - two branches (`electronics`/`clothing`) really are identical and should merge, but a third (`other`) only superficially resembles them (no `>100` tier, always the flat rate); tests whether the model verifies branches are actually identical before folding them together, the refactor-specific analog of `code_review`'s `cr-13` (verify the base case before flagging).
- **Public interface preservation** (`rf-09`) - the instruction only asks to clean up an internal helper, but the test suite imports the outer function by name; a model that renames the public entry point while refactoring internals breaks the import. This falls directly out of `sandbox.run_pytest_check`'s existing `from solution import <name>` requirement - no new grading mechanism needed, just a case that exercises it deliberately.
- **Multi-branch rule-following** (`rf-10`) - an if/elif chain converted to a dict lookup; tests whether the fallback branch for unrecognized input survives the conversion, same family as `code_gen`'s `cg-16`/`cg-17` and `summarization`'s scope-dropping cases.
- **Silent-failure family, ported to refactor** (`rf-11`) - a "simplify the validation" instruction that could tempt swallowing a documented `raise` into a silent default return; same theme as `bug_triage`'s `bt-10` and `code_gen`'s `cg-09`/`cg-10`.
- **Combined stress case** (`rf-12`) - two independent smells (a magic number reused in two unrelated conditionals, and a duplicated fee expression across branches) in one snippet, the refactor analog of `code_review`'s `cr-14`: a model that fixes one but not the other fails specifically on the un-fixed smell, not a single pass/fail blur.

Every case (baseline and adversarial) was validated two ways before being trusted as calibration data: a hand-written correct refactor passes both `tests_passed` and `smells_removed`, and a hand-written plausible-wrong one (either a no-op that leaves the smell in place, or a "helpful" fix that changes behavior it shouldn't) fails at least one check - same discipline as every suite before it. Not yet run against a real model, so - like `code_review` - this set hasn't been calibration-validated yet; first real run should sanity-check every `structural_checks` regex against actual model output before trusting a cross-model comparison drawn from it, since regex-on-code-text has similar false-positive/negative surface to `summarization`/`code_review`'s substring matching on free text.

### `multi_step`'s scope, and ordering + gap-filling grading

`multi_step` grades whether a model can produce a correctly-ordered, sufficiently-complete plan for a multi-phase task (a migration, rollout, integration, or general multi-phase workflow) - not code, not a review, not a summary. It's the first suite where the grading target is a plan's *structure* (which steps, in what relative order) rather than code correctness, fact accuracy, or issue recall. Scoped to a single self-contained scenario per case, same no-repo-context limitation every prior suite accepted (see `code_gen`'s note in "Adding a new eval suite").

Design questions were resolved with Lytton before building (see conversation history for the full options considered): the grading axis is **both** step ordering and gap-filling in one suite (not split into two), the output contract is an ordered array of `{"phase": ..., "detail": ...}` step objects (not a flat string array, and not a fixed phase-vocabulary enum - phase is a free-form short slug, matched as part of the same searchable text as `detail`, not validated against a controlled list), and cases draw from a broad mix of infra/data scenarios (matching `classifier.py`'s migrate/rollout/orchestrate/integrate keyword list) plus a couple of non-infra general workflows, rather than infra-only.

Like `summarization`/`code_review`, there's no golden plan to string-match - `rule_based_score_multi_step` (in `scorers.py`) grades by fact-constraint groups, same shape as those suites', plus one new axis:

- `required_steps`: a list of groups (acceptable phrasings, matched against `"{phase} {detail}"` per step - plans paraphrase, same reasoning as every prior suite's groups). All groups must be matched somewhere in the output for `step_coverage` to pass. Some groups represent steps the scenario never explicitly asked for (a rollback path, a kill switch) - `step_coverage` is where this suite's gap-filling requirement actually gets checked, not a separate mechanism.
- `ordering_constraints`: a list of `[earlier_group_idx, later_group_idx]` pairs into `required_steps`. A constraint is satisfied if the earliest output step matching the earlier group has a lower array index than the earliest step matching the later group. `ordering_correct` only evaluates constraints where *both* referenced groups were actually matched somewhere in the output - same reasoning as `code_review`'s `severity_correct` only scoring groups that were caught (a constraint referencing a step the model never produced can't be judged as correctly or incorrectly placed). But mirroring that precedent's other half: if a case has ordering constraints and *none* of them were evaluable (every referenced group missing), `ordering_correct` fails outright rather than vacuously passing - a plan with nothing to order isn't a well-ordered plan. Cases with no `ordering_constraints` (pure coverage/false-positive cases) get a vacuous pass, same as `refactor`/`summarization`'s always-present, sometimes-empty checks.
- `must_not_include`: a flat list of substrings that must not appear - false-positive bait for premature or unsafe shortcuts (skipping a required approval gate, cutting over before the thing it depends on exists), same shape as `code_review`'s `must_not_flag`.

The most direct new failure mode this suite exists to catch: a model that reproduces the *order information was presented in the scenario's prose* rather than deriving the actual dependency order. Several adversarial cases below deliberately narrate events out of their correct execution order to test for this - it's a distinct failure mode from `refactor`'s "don't fix out-of-scope behavior" theme, since here reordering *is* the correct behavior and mirroring the input is the trap.

### `multi_step.jsonl` case design (12 cases, as of 2026-07-24)

`ms-01` through `ms-06` are baseline cases - each a fully sequential plan (a zero-downtime column migration, a canary rollout, a legacy-provider integration, a CI/CD pipeline consolidation, a customer onboarding workflow, a content publishing pipeline) where the correct order is unambiguous and stated straightforwardly. `ms-05`/`ms-06` are deliberately non-infra, to check the suite isn't only infra-shaped despite `classifier.py`'s keyword list skewing that way. `ms-07` through `ms-12` target failure modes specific to planning:

- **Input-order mismatch** (`ms-07`) - the scenario narrates decommissioning an old cache before mentioning the cache-warming and bake-period steps that must actually happen first; tests whether the model derives the real dependency order (warm → bake/parallel-run → decommission) instead of mirroring the sentence order it was told in - the core new failure mode this suite was built to catch.
- **Gap-filling** (`ms-08`) - the instruction asks only for a sandboxed test and a small live rollout of a new payment provider, never mentioning a kill switch; `required_steps` still demands one, since shipping live payment traffic with no way to disable it is exactly the kind of unstated-but-necessary step a competent plan includes without being asked.
- **Hidden dependency / false-parallelization bait** (`ms-09`) - the prompt explicitly flags two steps (enabling dual-write, backfilling historical records) as "seemingly independent," but backfilling before dual-write starts loses any record written during the backfill window; tests whether the model catches the real dependency instead of treating the two as freely reorderable.
- **Premature-action bait** (`ms-10`) - a stated company policy requires manual sign-off before expansion beyond a canary; `must_not_include` catches a plan that expands early or treats the sign-off as an afterthought, the ordering-suite analog of `code_review`'s `must_not_flag` false-positive bait.
- **Shared prerequisite across independent branches** (`ms-11`) - a compliance approval gates two workstreams that can otherwise proceed in either relative order to each other; `ordering_constraints` only constrains each workstream against the shared approval step and the final go-live, not against each other, testing whether the model preserves the one real prerequisite without over- or under-constraining the rest.
- **Combined stress** (`ms-12`) - the scenario narrates a wrong order (cut over before the shared template even exists), never asks for a rollback path, and includes false-positive bait about deleting the old pipelines immediately - the `multi_step` analog of `code_review`'s `cr-14`/`refactor`'s `rf-12`: a model that gets two of the three dimensions right still fails on whichever it missed.

Every case (baseline and adversarial) was validated two ways before being trusted as calibration data: a hand-written correct plan passes `step_coverage`, `ordering_correct`, and `no_false_positives`, and a hand-written plausible-wrong plan (reordered, missing a gap-fill step, or containing bait language) fails at least the intended check - same discipline as every suite before it. One phrasing lesson surfaced during that validation and is worth carrying forward: keep each `required_steps` group's phrases distinctive enough that they can't accidentally match a *different* step's `detail` text (an early draft of `ms-04` had the design step's phrase match inside the migrate step's detail because both mentioned "shared template," which silently produced the wrong match index instead of an obvious failure) - the same paraphrase-fragility lesson `summarization`'s `sum-05`/`sum-08` taught, just showing up here as index confusion instead of a false hallucination flag. Not yet run against a real model, so - like `code_review`/`refactor` - this set hasn't been calibration-validated yet; first real run should sanity-check every `required_steps`/`must_not_include` phrase group against actual model output before trusting a cross-model comparison drawn from it.

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

## `code_gen` re-sweep against the 17-case suite (2026-07-23/24, Claude leg only)

`code_gen.jsonl` grew from 8 to 17 cases same day (cg-09 through cg-17,
targeting known LLM code-gen failure modes - see "`code_gen.jsonl` case
design" above). Re-ran the judged sweep to see whether the harder cases
actually discriminate.

| provider | model | tests_passed | fully correct | judge coherence |
|---|---|---|---|---|
| claude | haiku (cheap) | 100.0% | 100.0% | 0.91 |
| claude | sonnet (mid) | 100.0% | 100.0% | 0.70 |
| claude | opus (flagship) | 100.0% | 100.0% | 0.79 |

**All three Claude tiers still hit 100% tests_passed, even on the
adversarial cases** - the mutable-default-argument, currency-rounding,
parameterized-SQL, subprocess-arg-list, and rule-generalization cases
didn't trip up any Claude tier. This is a genuine (if unexciting) result,
not underpowered-suite noise the way the 8-case sweep was: these cases were
each individually confirmed to fail a plausible-wrong implementation before
being trusted (see case design notes), so a 100% here means Claude models
actually handle these specific failure modes well, not that the cases
don't discriminate. Judge coherence does vary now (haiku 0.91, opus 0.79,
sonnet 0.70) even though pass/fail doesn't - worth a closer read of which
specific cases pulled sonnet's judge score down before trusting it as a
tier signal on its own.

**Codex leg not run.** This ChatGPT account's Codex/Codex CLI usage quota
is exhausted account-wide - a direct `codex exec` call (unrelated to any
harness code) returned `"You've hit your usage limit... try again at Aug
22nd, 2026."` This is a real account-level lockout, not a harness bug or a
code_gen-difficulty finding. A first attempt at `gpt-5.4-mini` produced the
same bogus-run shape CLAUDE.md already warns about (every case a parse
error, `avg_judge_score` reading 0.0 like a real bad score) - deleted
rather than kept, per established practice. **Don't attempt the Codex leg
of this sweep again before 2026-08-22** - it will just fail account-wide
the same way regardless of which model is requested.

## Router tier calibration status (`summarization` / `summarization_v1`, as of 2026-07-24)

First judged benchmark for the new `summarization` suite (16 cases - see
"`summarization.jsonl` case design" above), Claude tiers only. Codex leg
skipped for the same account-wide quota lockout as the `code_gen` re-sweep
above (blocked until 2026-08-22).

| provider | model | key facts | no hallucination | length ok | fully correct | judge coherence |
|---|---|---|---|---|---|---|
| claude | haiku (cheap) | 100.0% | 100.0% | 87.5% | 87.5% | 0.85 |
| claude | sonnet (mid) | 100.0% | 100.0% | 62.5% | 62.5% | 0.82 |
| claude | opus (flagship) | 100.0% | 100.0% | 75.0% | 75.0% | 0.80 |

**Verdict: non-monotonic result, and every miss on every tier is a
`length_ok` failure, not a fact error - haiku is the strongest tier on this
suite, not the weakest.** All three tiers hit 100% on `key_facts_included`
and `no_hallucination` - none fabricated a fact, dropped a negation,
flipped a direction of change, or misattributed a claim, on any of the 16
adversarial cases. The entire spread comes from word-count-limit
compliance: haiku missed 2/16 cases (`sum-05`, `sum-13`, both multi-fact
incident-style sources where it didn't compress enough), opus missed 4/16
(same two plus `sum-15`'s 15-word hard limit and `sum-16`), and sonnet
missed 6/16 (the same set plus `sum-07` and `sum-09`) - sonnet was
consistently the most verbose of the three, even though its summaries
weren't more factually complete. This is a genuine tier-inverting result
specific to this suite's grading, not noise: bigger/pricier Claude tiers
default to more thorough, hedged prose, and this task's word-count
constraints penalize that directly. `sum-15`'s judge coherence is the
sharpest illustration - opus scored 0.15 there because its stated
reasoning ("kept all numbers... within the 15-word limit") directly
contradicted its own 18-word output, exactly the reasoning-vs-output
mismatch `judge_score` exists to catch (see "Why two scorers, not one").

**Don't read this as "use haiku for summarization" without checking prompt
sensitivity first.** The prompt (`summarization_v1.txt`) already instructs
"If the source gives an explicit length limit, honor it exactly," and
sonnet/opus still overran it - a stronger prompt (e.g. restating the word
budget as a hard numeric constraint right before the length-sensitive
cases, or adding a self-check step) might close this gap without changing
models. Re-run this suite against a revised prompt before trusting
`haiku` > `opus` as a router-tier conclusion; right now this table shows a
real prompt-following gap, not necessarily a capability gap.

**Codex leg not run** - same account-wide Codex CLI quota lockout as the
`code_gen` 17-case re-sweep (blocked until 2026-08-22, see above). Don't
attempt it before then.
