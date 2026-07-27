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
  cases/*.jsonl  - golden test sets, one JSON object per line, one file per suite (bug_triage, code_gen, summarization, code_review, refactor, multi_step, architecture)
  prompts/*.txt  - versioned system prompts under test, suite-scoped by naming convention (v1_naive/v2_rubric for bug_triage, code_gen_v1 for code_gen, summarization_v1 for summarization, code_review_v1 for code_review, refactor_v1 for refactor, multi_step_v1 for multi_step, architecture_v1 for architecture)
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

### `architecture`'s scope, and constraint + tradeoff grading

`architecture` is the sixth and final suite matching `llm-task-router/classifier.py`'s task types, and the highest-stakes one to calibrate: `TYPE_DOMAIN_GRID` maps `architecture` to flagship (`H`) tier across *every* domain (frontend/backend/infra/data/other) - the only task type where that's uniform - and `classify_description` routes any task shape it can't recognize here rather than to the cheap tier, on the principle that an expensive false escalation beats silently underrouting an ambiguous task. A miscalibrated floor here doesn't just mis-grade one suite, it mis-grades the router's default "when in doubt" behavior.

Scoped deliberately to "design from constraints," not "critique an existing design" - a case states a scenario plus explicit non-functional requirements (scale, availability, cost, compliance, latency) and asks the model to propose an architecture with rationale. This keeps clean separation from `code_review` (owns critique-of-existing-code) and `multi_step` (owns step sequencing): `architecture` owns deciding the shape of a system and justifying it, not reviewing one or ordering the work to build it.

Like `summarization`/`code_review`/`multi_step`, there's no golden design to string-match - `rule_based_score_architecture` (in `scorers.py`) grades by fact-constraint groups, same shape as those suites', plus one genuinely new axis. The output contract has three fields, each with a distinct grading role:

- `design`: the proposed architecture - the actual chosen answer.
- `reasoning`: one-sentence rationale, pooled with `design` for `must_include` matching but not for `must_not_include`.
- `alternatives_considered`: a list of `{"option": ..., "rejected_because": ...}` - used *only* for the new `tradeoffs_articulated` axis, never pooled into the fact-constraint search text.

Two deliberate scorer choices worth calling out:

1. **`must_not_include` (anti-pattern bait) is checked against `design` only**, excluding `reasoning` and `alternatives_considered`. A model that correctly *names* a bad approach in order to reject it ("avoided a single-region deployment given the HA requirement") shouldn't fail `no_anti_patterns` for saying the bait phrase - that's `code_review`'s `cr-08` "justified pattern, not a bug" lesson, ported here. Only the design actually being proposed is graded for containing an anti-pattern.
2. **`tradeoffs_articulated` requires *genuine* alternatives**: both a named `option` and a non-empty `rejected_because`, deduplicated by option text. A padded list of options with no stated rejection reason, or several reworded names for the same option, doesn't inflate the count. This is the suite's one real new mechanism (not just another fact-constraint group) - a structural check on response shape like `refactor`'s `structural_checks`, just counting distinct dicts instead of regex matches. It exists because judge_score alone (reasoning-coherence scoring) can be fooled by confident-sounding prose that never actually names a competing option - this axis checks the tradeoff was *articulated*, not just that the prose reads well.

### `architecture.jsonl` case design (12 cases, as of 2026-07-24)

`ar-01` through `ar-06` are baseline cases, infra/backend-weighted with one data and one non-infra case for sanity (per the domain-spread decision made with Lytton before building) - each states one explicit constraint with an unambiguous correct shape: a rate limiter needing cross-instance consistency, IoT ingestion needing hot/cold storage tiering, a partner-file pipeline that must not halt entirely on one bad file, multi-region failover with a stated RTO, an async job system that must survive a worker crash, and CDN caching that must balance freshness against origin protection. `ar-07` through `ar-12` target failure modes specific to design reasoning, the same way every suite's adversarial half targets known failure modes for its task type:

- **Over-engineering bait** (`ar-07`) - a small-scale internal tool (30 users, <100 req/day); `must_not_include` catches reaching for microservices/Kubernetes/Kafka/multi-region when the stated scale doesn't call for it. No other suite has this failure mode in quite this shape - `code_gen`/`refactor` grade code correctness, not solution *sizing*.
- **Dropped compliance constraint** (`ar-08`) - PCI-DSS is stated explicitly for card storage; `must_not_include` catches storing raw card numbers, `must_include` requires tokenization or an out-of-scope processor. Direct analog to `code_review`'s hardcoded-secret case (`cr-12`) and `code_gen`'s security-sensitive-generation cases (`cg-13`/`cg-14`), ported to a design decision instead of a code snippet.
- **SPOF despite explicit HA requirement** (`ar-09`) - a 99.99%-uptime payment service; `must_not_include` deliberately avoids the bare phrase "single instance" in favor of more specific phrases like "runs on a single instance," since a *correct* redundant design will often say something like "no single instance is a point of failure" while explaining itself - caught during two-way validation (see below), same class of false-positive-phrase bug `summarization`'s `sum-08` and this suite's own `ar-05` (below) hit.
- **Cost-constraint violation bait** (`ar-10`) - an explicit hard budget ceiling; `must_not_include` catches an enterprise-scale managed stack sized for far more load than the scenario describes.
- **Genuine tradeoff case** (`ar-11`, `min_alternatives: 2`) - a real CAP-flavored tension (strong global consistency and low-latency global reads, both wanted for a shopping cart); tests whether the model actually names and weighs the tension instead of asserting a design that gets both for free. This is the case `tradeoffs_articulated` exists for.
- **Combined stress case** (`ar-12`, `min_alternatives: 2`) - bundles an HA requirement, a budget ceiling, and a real consistency-vs-cost tradeoff in one scenario, the `architecture` analog of `cr-14`/`rf-12`/`ms-12`: a model that nails two of three axes still fails on whichever it missed.

Every case (baseline and adversarial) was validated two ways before being trusted as calibration data: a hand-written correct design passes `key_considerations_addressed`, `no_anti_patterns`, and `tradeoffs_articulated`, and a hand-written plausible-wrong design fails at least one - same discipline as every suite before it. Two case-design bugs surfaced during that validation and were fixed before trusting the file, both worth carrying forward as lessons: `ar-05`'s `must_include` group listed "retry" but not "retried" - a correct reference answer naturally phrased as "the job is retried by another worker" didn't match, the same paraphrase-fragility class `summarization`'s `sum-05` hit; and `ar-09`'s `must_not_include` originally used the bare phrase "single instance," which a *correct* redundant design's own explanation naturally triggered (see above) - fixed by narrowing to phrases specific to an actually-single-instance design rather than the generic concept. Not yet run against a real model, so - like every suite's first landing - this set hasn't been calibration-validated yet; first real run should sanity-check every `must_include`/`must_not_include` phrase group against actual model output before trusting a cross-model comparison drawn from it.

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

## Router tier calibration status (`code_review` / `code_review_v1`, as of 2026-07-25)

First judged benchmark for `code_review` (14 cases), Claude tiers only. Codex
leg skipped for the same account-wide quota lockout as `code_gen`/
`summarization` above (blocked until 2026-08-22).

| provider | model | issues flagged | severity correct | no false positives | fully correct | judge coherence |
|---|---|---|---|---|---|---|
| claude | haiku (cheap) | 85.7% | 92.9% | 92.9% | 78.6% | 0.88 |
| claude | sonnet (mid) | 92.9% | 85.7% | 92.9% | 78.6% | 0.70 |
| claude | opus (flagship) | 100.0% | 100.0% | 100.0% | 100.0% | 0.85 |

**Verdict: opus clearly clears the floor; sonnet does not earn its cost
premium over haiku on this suite.** haiku and sonnet tie exactly on
fully-correct (78.6%, 11/14) but miss different cases and sonnet's judge
coherence is markedly worse (0.70 vs. haiku's 0.88) - same accuracy, weaker
stated reasoning, higher cost. Only opus reaches a clean 100%. **Don't add
a mid-tier=sonnet assumption for code_review off this table without
re-checking** - this is one sample per tier (see caveat below), and
`llm-task-router/tiers.py`'s `TIER_MODELS` isn't suite-specific, so this
data point argues against sonnet's mid-tier placement holding uniformly
across suites, not for changing `tiers.py` outright.

**Real case-design bugs found and fixed during this run** (same paraphrase-
fragility class documented throughout this file - `sum-05`/`sum-08`,
`ar-05`/`ar-09`):
- `cr-03`: bare `"86400"` in `must_flag` cross-matched an unrelated finding
  that happened to also cite the literal value while describing a different
  (real) bug - narrowed to `"magic number"`/`"named constant"`/`"unexplained
  constant"`.
- `cr-05`: bare `"os.system"` cross-matched an unrelated finding about the
  same function call's ignored return value - dropped, kept the more
  specific injection-phrase group.
- `cr-07`, `cr-14`: both had a real miss where a model correctly identified
  the planted issue (card number logged in cleartext; password compared
  unhashed) in wording no phrase in the group covered (`"printed in
  cleartext"` vs. `"card number is logged"`; `"compared in cleartext...
  raw equality"` vs. `"not hashed"`) - both groups widened. Confirmed by
  local re-scoring of the saved run (no new model calls) that widening
  didn't flip any other case's result before trusting it.

**Known caveat, deliberately not chased further: this suite showed the same
single-sample instability `multi_step` surfaced first.** A second haiku
sample (after the `cr-03`/`cr-05` fix) produced an entirely different miss
pattern (`cr-03` missed for a genuinely different reason; `cr-14` missed a
group the first sample had caught), and `cr-13`'s "clean case, don't
false-positive on bounded recursion" check was tripped by a haiku finding
that's arguably a legitimate practical point (`RecursionError` on deep
input) rather than the "claims infinite recursion" false-positive the case
was built to catch. Sonnet separately missed `cr-01`, `cr-08`, `cr-09` -
`cr-01` and `cr-08` look like they could be the same phrase-collision class
as the fixed cases, but with only one sample each, patching now would be
sample-chasing, not a confirmed fix. **Decision made explicitly (2026-07-25,
matching the `multi_step` discussion): don't patch cases on n=1 evidence
going forward** - only `cr-03`/`cr-05`/`cr-07`/`cr-14` were fixed because
the collision mechanism was directly confirmed (the colliding phrase
literally appears in a separate, unrelated finding's text, a structural
bug independent of which sample surfaced it), not because a single sample
missed. `cr-01`, `cr-08`, `cr-09` (the one exception - see below), `cr-13`
stay open. Treat every `check_accuracies` number in this table as N=1 per
model - a real confidence interval needs the N-sample scoring mechanism
this suite (and `multi_step`) are both waiting on, not yet built.

One caveat resolved rather than left open: `cr-09` (the no-justification
silent-failure case) failed identically on **both** haiku and sonnet, for
the same reason both times (neither model flagged the silent
default-return) - reproducing across two different models is real signal,
not phrase noise, so this one is trusted as a genuine finding: both cheap
and mid tiers miss this specific silent-failure pattern on this suite.

**A bogus run was caught and discarded, not trusted:** the first opus run's
judge pass failed with "nonzero exit" starting at `cr-03` (rule-based
scoring was unaffected and already showed 100%), producing `avg_judge_score
0.13` - the exact bogus-run shape this file already warns about elsewhere
(looks like a real bad score if you don't check the error text). Deleted
and re-ran rather than trusted; the re-run's `avg_judge_score` (0.85) is
what's in the table above.

## Router tier calibration status (`refactor` / `refactor_v1`, as of 2026-07-25)

First judged benchmark for `refactor` (12 cases), Claude tiers only. Codex
leg skipped for the same account-wide quota lockout as every suite above
(blocked until 2026-08-22).

| provider | model | tests passed | smells removed | fully correct | judge coherence |
|---|---|---|---|---|---|
| claude | haiku (cheap) | 100.0% | 100.0% | 100.0% | 0.88 |
| claude | sonnet (mid) | 100.0% | 100.0% | 100.0% | 0.87 |
| claude | opus (flagship) | 100.0% | 100.0% | 100.0% | 0.90 |

**Verdict: all three Claude tiers clear the floor cleanly - a genuine
result, same class as `code_gen`'s 17-case re-sweep, not the original
8-case ceiling-effect noise.** Every case here (baseline and adversarial)
was individually confirmed to fail a plausible-wrong refactor before being
trusted (see "`refactor.jsonl` case design" above), so a 100% across tiers
means Claude models actually handle duplicated-logic consolidation,
magic-number extraction, dead-code removal, mutable-default-argument
fixes, and (critically) the refactor-specific failure modes - resisting
out-of-scope bug fixes (`rf-07`), verifying branches are truly identical
before merging (`rf-08`), preserving a public interface (`rf-09`) - without
tripping on any of them at any tier. Judge coherence stayed flat and
healthy (0.87-0.90) too, unlike `summarization`'s tier-inverting spread -
no evidence of a verbosity or hedging penalty on this task shape.

**One real, deterministic case-design bug found and fixed** (not sample
noise - confirmed by construction, not by a second sample): `rf-12`'s
`max_occurrences` check for `\b20\b` was set to 1, but the source snippet's
own pre-existing error message (`"subtotal below minimum of 20"`) already
contains the literal number, unrelated to the duplicated-comparison smell
the check targets - `max=1` was unsatisfiable by *any* refactor that kept
that informative string, confirmed when a correct haiku consolidation
(single named constant, message left intact) still failed. Raised to
`max=2`; re-verified a no-op refactor (3 occurrences: two comparisons plus
the string) still correctly fails.

**A second bogus opus run was caught and discarded** - the same
`judge call failed: nonzero exit` pattern as `code_review`'s opus run,
starting partway through the case list (rule-based scoring unaffected,
100% both times). Deleted and re-ran; this is now the second time in two
suites the judge pass has failed mid-run with no retry - `claude_cli.py`
has no retry/backoff (a documented rough edge), and two occurrences in one
session is enough to flag as a real reliability gap worth a retry/backoff
pass, not just a one-off fluke to keep manually re-running around.

## Router tier calibration status (`architecture` / `architecture_v1`, as of 2026-07-25)

First judged benchmark for `architecture` (12 cases), Claude tiers only.
This is the suite `TYPE_DOMAIN_GRID` routes to flagship tier uniformly
across every domain and the fallback for unrecognized task shapes (see
`llm-task-router/classifier.py`) - the highest-stakes suite to calibrate
correctly. Codex leg skipped for the same account-wide quota lockout as
every suite above (blocked until 2026-08-22).

**Infra fix made before this run could complete cleanly:** `claude_cli.py`'s
subprocess timeout (already bumped from 60s to 120s in an earlier,
uncommitted session for `multi_step`) still wasn't enough for
architecture's long, multi-paragraph design responses - 3 of 12 haiku
cases hit `ERROR: timeout` with empty `predicted={}` on the first attempt.
Bumped to 240s (`claude_cli.py`, `test_claude_cli.py`); the retry at 240s
completed all 12 cases cleanly. Worth remembering for any future suite
whose expected output is long-form prose (design docs, multi-step plans) -
the generation timeout that's fine for a one-paragraph review or a code
diff isn't automatically fine for a several-hundred-word design writeup.

| provider | model | key considerations | no anti-patterns | tradeoffs articulated | fully correct | judge coherence |
|---|---|---|---|---|---|---|
| claude | haiku (cheap) | 91.7% | 100.0% | 100.0% | 91.7% | 0.83 |
| claude | sonnet (mid) | 100.0% | 91.7% | 100.0% | 91.7% | 0.87 |
| claude | opus (flagship) | 66.7% | 91.7% | 91.7% | 58.3% | 0.79 |

**Don't read opus's 58.3% as "opus is worse than haiku/sonnet at
architecture" - it's almost certainly a scorer-strictness artifact, not a
real capability gap, and shouldn't inform a tier decision as-is.** Two
distinct problems inflate opus's miss count here, neither of which is a
real design defect:

1. **A genuine generation-format hiccup**: `ar-11` came back as malformed
   JSON (`PARSE ERROR: Expecting ',' delimiter...`) on the final opus
   sample, scoring 0 across the board with an empty `predicted={}` - not a
   harness bug, but also not evidence of a design flaw (there's no design
   to evaluate).
2. **A suite-wide phrase-strictness pattern, confirmed across multiple
   cases in the same sample, not a single one-off**: `ar-02` said "hot
   **store**" where the phrase list wanted "hot storage"/"hot tier"; `ar-05`
   named the concrete technology "**SQS**" where the phrase list wanted the
   abstract "durable queue"/"message broker"; `ar-06` gave the concrete
   value "**`s-maxage=60`**" where the phrase list wanted the abstract
   "short TTL". All three designs were technically sound - they answered
   with specific implementation detail instead of restating the
   requirement's own vocabulary, which is arguably the *more* competent
   answer, and the fact-constraint scorer penalized it anyway.

**Explicitly not chased further this session - this is a suite-level
finding, not a per-case bug list.** `ar-03`/`ar-05`/`ar-11` (see below) were
fixed earlier in this same session because each was an isolated, clearly
bounded word-form gap. By the time `ar-02`/`ar-05`(again)/`ar-06` turned up
in one single re-generated sample, the pattern had stopped looking like
"a few missed synonyms" and started looking like a structural mismatch
between how these cases were authored (abstract requirement-phrases) and
how competent technical answers are actually written (concrete
implementation nouns) - the same "stop patching individual phrases,
recognize the systemic shape" moment `multi_step` hit earlier, just via a
different mechanism (concrete-vs-abstract vocabulary instead of
step-ordering non-determinism). **Before trusting a tier decision off this
suite, it needs either a scorer redesign (broader default synonym coverage
that includes common concrete technology names, or shifting more of
`key_considerations_addressed`'s weight onto `judge_score` instead of
strict substring matching) or the same N-sample mechanism `multi_step` is
waiting on - not more manual phrase-list patching.**

**Real, confirmed structural fixes made earlier in this run** (word-form
gaps, not sample noise - each confirmed by directly inspecting why a
correct answer didn't match, same discipline as every fix in this file):
- `ar-03`: `"idempotent"` is not a substring of `"idempotency"` (diverge at
  the 10th character) - widened; isolation-phrase group also widened for
  "marked invalid and skipped / continues for other files" phrasing.
- `ar-05`: `"durable queue"` didn't match `"durable **job** queue"` (word
  inserted mid-phrase) - widened.
- `ar-11`: required the exact bigram `"tension between"` and didn't cover
  the verb form `"trades"` - both confirmed against real output (one haiku
  sample said "trades", one opus sample said "the CAP **tension**" without
  "between") and widened. A second haiku sample, checked after widening,
  genuinely never mentioned tradeoff/tension anywhere in `reasoning` or
  `design` - a real, reproducible haiku gap on this specific case (2/2
  haiku samples), left as-is.

**A structural (not phrase-list) false-positive was also found and left
undocumented-but-unfixed by design, not oversight**: `ar-07`'s
`no_anti_patterns` checks the `design` field for bait words like
"kubernetes", specifically excluding `alternatives_considered` so a model
that names-and-rejects an anti-pattern there doesn't get penalized (the
`cr-08`/`ar-09` precedent). But **haiku, sonnet, and opus all independently
wrote their own rejection of Kubernetes inline in the `design` field's own
prose** ("Explicitly not included, and why: no Kubernetes...") rather than
in `alternatives_considered` - three different models, same pattern. This
is a real scorer-scoping gap (the exclusion only covers one of two places
a model can reasonably explain what it didn't choose), not something a
phrase-list edit fixes. Left open rather than patched; worth a scorer
change (pool `design` + `alternatives_considered`'s `rejected_because`
text for `must_not_include`, not `design` alone) as its own follow-up.

## `multi_step` N-sample validation (2026-07-25) - the mechanism worked, and it found something bigger than expected

Built `--samples N` (see the `--samples` flag commit) specifically to
answer whether `multi_step`'s single-sample instability (documented
earlier this session, and in the conversation that motivated this work)
was real model non-determinism or a case-design artifact. Ran the decided
validation: N=5, haiku, judged, all 12 cases (`python -m eval_harness run
--suite multi_step --prompt multi_step_v1 --model haiku --samples 5`,
$2.57, saved as `runs/20260725T225357Z__multi_step_v1__haiku.json`).

**Result: 15.0% fully-correct across 60 samples, with 8 of 12 cases at or
near 0% and only one case (`no_false_positives`) at a clean 100%.** This
number is real but **must not be read as "haiku is bad at multi-step
planning"** - inspecting the actual failures shows the dominant cause is
scorer phrase-strictness, not planning quality, and it's severe and
pervasive enough that this is a scorer-methodology finding, not a
per-case bug list:

- `ms-09` (dual-write/backfill/reconcile/cutover), sample 2: step
  `copy_historical_records` says *"Bulk copy all existing user records
  from legacy to new datastore"* - doesn't match `"backfill"` /
  `"copy over existing"` / `"copy historical records"` (different verb
  choice/word order). Step `validate_copy` says *"Verify migration
  integrity: row counts match, checksums align..."* - a textbook
  reconciliation step that doesn't match `"reconcile"` /
  `"verify consistency"` / `"compare record counts"`. Both steps are
  substantively correct; the plan just isn't phrased the way the case
  anticipated.
- This pattern repeats across nearly every case and nearly every
  `required_steps` group, not one or two isolated ones - the same
  concrete-vs-anticipated-phrasing gap `architecture`'s `ar-02`/`ar-05`/
  `ar-06` showed this session, but far more prevalent here because a
  13-step migration plan has many more independent phrases that all have
  to land correctly for `step_coverage` to pass, and `ordering_correct`
  inherits every one of those misses (a group that never matches can't be
  placed in the order check either).

**This is exactly the question the N-sample mechanism was built to
answer, and it answered it conclusively - just not in the direction
originally expected.** The hypothesis going in was "is single-sample
instability real model variance or case noise, and how much." The N=5
data doesn't leave that ambiguous: with the *same* phrase-matching
approach failing on 40+ independent, well-formed, differently-worded
correct plans across 5 samples, the problem is structural under-coverage
in how `multi_step.jsonl`'s phrase groups were authored, not noise. This
validates the mechanism worked as designed (it turned "I think this might
be noise" into "here is unambiguous evidence of which one it is") even
though the fix now needed - a systematic rewrite of most `required_steps`
phrase groups across all 12 cases, validated against real generated
language rather than hand-guessed phrasing - is larger than "widen 2-3
groups" and **deliberately not attempted in this session**. Patching 40+
groups in one pass without individually validating each against a
plausible-wrong plan (the two-way discipline every other suite's fixes in
this file followed) would risk trading a strict-but-honest scorer for a
loose-and-untrustworthy one.

**A separate, real infra observation, not related to the phrase issue:**
`ms-02` timed out on 4 of its 5 samples even at the 240s timeout (bumped
from 60s earlier this session). The one sample that did complete
succeeded normally ($0.0758, well within normal cost range for this
suite), and every other case's samples completed without a single
timeout - so this doesn't look like `ms-02`'s content specifically
provoking runaway generation, more likely a transient rate-limit/latency
window during that portion of the 60-call run that happened to land on
`ms-02`. Worth watching for recurrence rather than acting on from one
run.

**Recommended follow-up (not done this session):** don't extend
`--samples` to `code_review`/`architecture` yet (per the "multi_step
only, for now" decision) until `multi_step.jsonl`'s phrase groups get a
real revision pass - re-running N-sample against a case file with this
much scorer noise would just produce a more expensive version of the same
misleading number. The revision itself should follow the same discipline
`code_gen`/`summarization`/every suite's case-design section describes:
widen each group against real model output, confirm a plausible-wrong
plan still fails after widening, don't just guess broader phrasing.

## `multi_step.jsonl` phrase-group revision (2026-07-26) - the deferred fix from above, done against real N=5 output

Went case-by-case through the saved `runs/20260725T225357Z__multi_step_v1__haiku.json`
(the N=5 run above) rather than guessing broader phrasing, per that run's own
recommendation. Two design questions got resolved before touching any case file:

**Embeddings-based semantic matching was evaluated and rejected, empirically, before
being built.** The instinct going in was that a local embedding model (no API billing,
so it wouldn't break the "no separate billing" rationale - see "Why it's built this
way") could replace brittle substring matching entirely. Tested directly against real
phrase pairs from this suite's own N=5 failures (`fastembed`, `BAAI/bge-small-en-v1.5`
and `bge-base-en-v1.5`, cosine similarity, no training involved - a vector database
was never the right frame either, since there are only a handful of phrases per group
to compare, not a corpus to index): word-form variance (`"writes to status_id only"`
vs `"write to status_id only"`) scored 0.91-0.95, a huge safe margin. But the actual
motivating problem - concrete implementation nouns vs. abstract requirement phrasing
(`"SQS"` vs `"durable queue"`, `"s-maxage=60"` vs `"short TTL"`, percentage ramps vs.
`"staged rollout"`) - scored 0.54-0.60, and a same-case phrase that must NOT match
(`"rollback"` vs. an unrelated group's `"deploy a shared template"`) scored 0.515,
barely below the true positives. No single threshold safely separates real synonyms
from same-case noise on the exact problem embeddings were brought in to solve, and a
bigger model didn't meaningfully improve the separation. **Decision: don't add the
dependency.** This is the same "validate before trusting" discipline every case-design
section in this file already follows, just applied to a scoring-mechanism choice
before writing code, not to a phrase group after writing it.

**Regex support was added to `rule_based_score_multi_step` instead** (`scorers.py`) -
a group's `required_steps` entry may now carry an optional `"patterns"` list (regexes,
`re.search`, case-insensitive) alongside `"phrases"`. This covers phrasing that's
genuinely regular in shape (numeric ramps, "verb + separator + noun" combinations
across underscore/hyphen/space) but too combinatorial to enumerate as a flat substring
list - a middle ground between plain substrings and the rejected embeddings approach.
Covered by two new tests in `test_scorers.py`.

**Result of the revision pass, re-scored against the same saved N=5 outputs (no new
model calls - `rule_based_score_multi_step` recomputed directly over the existing
run's saved `predicted` dicts):**

| check | before | after |
|---|---|---|
| step_coverage | 18/60 (30%) | 39/60 (65%) |
| ordering_correct | 29/60 (48%) | 31/60 (52%) |
| no_false_positives | 60/60 (100%) | 60/60 (100%) |

Per-case fully-correct: `ms-04` 0→2/5, `ms-05` 0→1/5, `ms-06` 2→4/5, `ms-09` 0→2/5,
`ms-11` 0→3/5 (the `ms-11` fix alone closed a 5/5 miss - see below). `ms-01`, `ms-02`
(still the timeout case), `ms-03`, `ms-08` stayed at 0/5 - see "left unfixed" below.

**The single highest-value fix: `ms-11`'s `required_steps[2]` missed on all 5 samples
for the same reason every time** - the phrase group literally required the compound
wording `"implement and validate access-control"`, which no natural single-step plan
writes (every real sample split implementation and validation into two separate
steps, as any reasonable plan would). Widened to phrase stems `"access control polic"`
/ `"access-control polic"` (catches policy/policies, hyphen or space) - this one change
alone fixed 3 of 5 samples outright.

**Two fixes caused real regressions on the first pass, both self-inflicted phrase
collisions, not model errors - caught by re-scoring the full N=5 set rather than
trusting the first attempt:**
- `ms-04`'s widened `"both frontend and backend"` (meant to catch a final
  stability-confirmation step) also matched an early negotiation step ("present audit
  findings **to** both frontend and backend teams"), pulling that group's matched
  index earlier than the actual migrate step and flipping `ordering_correct` from a
  vacuous true to a false. Narrowed to `"for both frontend and backend"` (the actual
  preposition the intended step uses) - fixed without losing the intended match.
- `ms-12`'s widened `"existing pipelines"` collided the same way with a step that
  merely referenced the future template in passing. Narrowed to the fuller, more
  specific `"of both existing pipelines"` - resolved.

**One exposed-but-not-introduced ordering collision left open, same class as the
`ms-04` "shared template" lesson from the prior session:** in `ms-12` sample 0, group1
(`"shared template"`) already matched inside group0's own audit step (which happens to
say "...that **the shared template** must support" as a forward reference) - this was
already true before any edit this session, just invisible because group0 never
matched at all, so the `[0,1]` ordering constraint was never evaluated. Fixing group0
(above) made the constraint evaluable, exposing this pre-existing group1 ambiguity as
a `ordering_correct` regression on that one sample. The plan's real step order is
correct (audit at index 0, design at index 1); the false negative is a phrase-matching
artifact in group1, not a group0 problem, and not something this pass's group0 fix
should be reverted over. Left open rather than chased further, same reasoning as
`cr-13`/`ar-07` staying open elsewhere in this file - would need reworking how
`first_match_index` prefers a step's "primary" content over an incidental mention,
which is out of scope for a phrase-list revision pass.

**Deliberately left unfixed, and why:**
- `ms-01` group0 (`dual write`), sample 0: the plan does a one-time `UPDATE` backfill
  *before* deploying app code that writes the new column - a real gap (any write
  between backfill and deploy goes unreflected), not a phrasing miss.
- `ms-03` group1 (`shadow`), sample 2: no shadow/dual-processing step exists in the
  plan at all before real traffic - a real gap, not a phrasing miss.
- `ms-03` group2 (`staged rollout`, percentage phrasing, 4/5 miss): attempted a numeric
  regex fix, rejected on inspection - this case's own `shadow` step independently
  mentions "100% of transactions" in an unrelated sense (routing to shadow execution,
  not the staged-traffic ramp), so a bare percentage pattern risks matching the wrong
  step and colliding with group1's own index. Left as an open scorer gap, same
  concrete-vs-abstract class as the rejected embeddings approach above - not safely
  fixable with regex either, unlike `ms-04`'s/`ms-12`'s verb-based gaps.
- `ms-09` group3 (`cutover`), single miss (1/5): real cause identified (`"Migrate
  remaining read traffic"` vs. `"cut over reads"`) but only one sample affected -
  left per the established "don't patch on n=1 evidence" rule (same discipline as
  `code_review`'s `cr-01`/`cr-08`/`cr-13` staying open - only `ms-11`-style 4-5/5
  patterns and directly-confirmed causes were fixed this pass).

**Not re-run against the model yet.** Every number above comes from re-scoring the
existing saved run's `predicted` dicts - zero new `claude -p` calls, zero new cost.
A fresh N-sample run (to see whether `haiku`'s *actual* fully-correct rate moves, not
just the re-scored one) is the natural next step before trusting this suite's numbers
for a tier decision, but wasn't done in this session.

## `multi_step` fresh N-sample run after the phrase-group revision (2026-07-26) - real gain confirmed, smaller than the offline re-score predicted

Ran the fresh N-sample validation the revision above deferred: `python -m eval_harness
run --suite multi_step --prompt multi_step_v1 --model haiku --samples 5` against 12
newly-generated haiku samples per case (not a re-score of old ones), $2.75, saved as
`runs/20260726T212852Z__multi_step_v1__haiku.json`.

| check | prior N=5 (pre-revision) | offline re-score (same old samples, new scorer) | fresh N=5 (new samples, new scorer) |
|---|---|---|---|
| step_coverage | 30% | 65% | 51.7% |
| ordering_correct | 48% | 52% | 51.7% |
| no_false_positives | 100% | 100% | 100% |
| fully_correct | 15.0% | n/a | 28.3% |

**Verdict: the revision produced a real, non-trivial improvement on genuinely new
model output - fully-correct nearly doubled (15.0% -> 28.3%) - but the offline
re-score overstated the gain (65% predicted vs. 51.7% actual on step_coverage).**
This is expected, not a bug: re-scoring reused the exact same 5 old samples the
phrase-group fixes were tuned against, so it measures "did this fix behave as
intended on the sample that motivated it," not "how well does this generalize to
a fresh draw." Treat offline re-scores as a fix-verification step, not a stand-in
for a real run, going forward.

**`ms-04` is the clearest illustration of that gap.** The offline re-score predicted
`ms-04` would go from 0/5 to 2/5 after narrowing `required_steps[3]`'s "for both
frontend and backend" phrase. The fresh run still shows `ms-04` at 0/5 - inspected
directly (no new model calls needed, `predicted` dicts are in the saved run file):
all 3 step_coverage misses have a substantively correct final-confirmation step
(e.g. "both teams must confirm before proceeding to stability window", "verify all
expected steps... are working identically to before", "Both teams have now
independently validated the template under real-world load") that simply doesn't
contain any phrase in group 3's list. The earlier narrowing fix was correct as far
as it went (it stopped a false regression on the old samples) but doesn't generalize
- this group still needs broadening, not narrowing, and is a concrete instance of
the "systematic rewrite is larger than 2-3 groups" conclusion the revision section
above already flagged rather than a new finding.

**No `ms-02` timeouts this run** (0 of 60 calls) - the prior N=5 run's 4/5 timeout
rate on `ms-02` even at the 240s timeout did not recur, consistent with that being a
transient rate-limit window rather than something specific to `ms-02`'s content.

**Four cases are now consistently 0/5**: `ms-01`, `ms-04`, `ms-08`, `ms-10`. Per the
"don't patch on n=1/re-scored evidence" discipline this file has followed since the
`code_review` caveat, none of these should be patched off this single fresh run
alone - `ms-04`'s cause is directly confirmed above (a phrase-coverage gap, real and
reproducible across all 3 misses in this sample), but `ms-01`/`ms-08`/`ms-10` haven't
had the same direct-inspection treatment yet and shouldn't be assumed to be the same
class of gap without it.

**Don't treat 28.3% fully-correct as this suite's real ceiling for a tier decision
yet.** The dominant failure mode across both the pre- and post-revision runs is still
scorer phrase-strictness on a substantively correct plan, not planning quality itself
- the revision closed part of that gap but, per the analysis above, real generalization
is smaller than hoped and the suite likely still needs the broader phrase-group rewrite
this file has deferred twice now rather than another narrow patching pass.

## `multi_step` targeted fix pass on the four 0/5 cases, done offline against both saved N=5 runs (2026-07-26)

Went case-by-case through `ms-01`, `ms-04`, `ms-08`, `ms-10` (the four cases the fresh
N=5 run above left at 0/5 and explicitly flagged as not-yet-inspected, except `ms-04`
whose cause was already confirmed) - all analysis and validation done by directly
re-scoring the 120 real `predicted` dicts already sitting in the two saved run files
(`20260725T225357Z` pre-revision + `20260726T212852Z` post-revision), zero new
`claude -p` calls. Two additional cases (`ms-02`, `ms-12`) turned up real, multi-sample-confirmed
phrase bugs of the exact same class while inspecting the ones above and got fixed too,
per this file's existing "fix when the mechanism is directly confirmed across multiple
samples, not on n=1" rule; `ms-03`'s equally-low scores were inspected and are
deliberately left alone - see below.

**`ms-04`'s confirmed final-confirmation gap (`required_steps[3]`) turned out to be two
separate bugs, not one.** Direct inspection of all 10 samples (not just the 3 the prior
session flagged) showed the bare `"confirm both teams"` phrase was actively harmful, not
just under-matching: it's a substring of unrelated design/staging-approval gates
("**Confirm both teams** approve the design before proceeding to implementation.",
"**Confirm both teams** have access and understand how to trigger validation runs.") that
say nothing about deploys ever going well. Because `first_match_index` returns the first
hit, this false-matched an early step and corrupted `ordering_correct` for samples where
it fired (group 3 landing before group 2 broke the `[2,3]` constraint) - `ms-04`'s R2
`ordering_correct` was 1/5 before this fix, masked by `confirm both teams` "passing"
`step_coverage` on the wrong step. Fixed by removing the bare phrase and replacing it
with a same-sentence-scoped regex, `both (teams|repos)[^.]{0,100}(stably|stable|
stability|successfully|validated|regress|incident|working)` - restricting the keyword
search to not cross a period is what lets it catch "both teams **complete** at least one
full release cycle with no pipeline-induced failures, **regress**ions" and "**Both
teams** have now independently **validated** the template under real-world load" while
correctly excluding both false positives above (neither has a matching keyword before
its own sentence ends). Two dead ends worth recording: a first attempt used a flat
40-char proximity window instead of same-sentence scoping and was too narrow (missed the
genuine "regressions" match, which sits ~70 chars after "both teams"); a second attempt
included bare `"deploy"` as a keyword and immediately regressed a different, previously-passing
sample, because an early staging step says "run both teams' test suites and full
**deploy**ment workflows against the shared template" - "deploy" alone doesn't distinguish
"we deployed for a test" from "both teams deployed successfully in production."
`required_steps[2]` (migrate one team, then the other) also needed broadening - real
phrasings included "switch **the smaller team**", "switch **team A**'s production CI/CD",
and "switch **the first repo**" - none matched the pattern's original verb list
(`cut over|cutover|migrate|update|flip`) or object list (`frontend|backend|first/second/
one/lower-risk/higher-risk team`). Added `switch` as a verb and `team a|team b|first
repo|second repo|smaller team|remaining team` as objects, validated the broadened
pattern doesn't collide with any of the frequent "kill switch"/"switchback" mentions
elsewhere in this suite (checked directly - none of those are followed by a team/repo
reference within the pattern's 50-char window).

**`ms-01` had three independent, confirmed bugs, and inspecting them surfaced a fourth
finding that's a real model behavior, not a scorer bug.** `required_steps[1]` (backfill)
used a bare `"backfill"` phrase that matched an *earlier* schema-setup step mentioning
backfill only as a forward reference - "this column remains unpopulated **until
backfill**", "will reference order_statuses **after backfill**", "to ensure no data loss
**during backfill**" - not the actual backfill action, which always comes later in every
sample checked. Fixed with a regex requiring "backfill" not be immediately preceded by
`until `/`after `/`during `/`before ` (four independent fixed-width negative lookbehinds,
each individually valid even though the four phrases differ in length).
`required_steps[3]` (drop old column) had two distinct problems: word-insertion variance
("drop **orders.**status column", "drop the **legacy** status column", "drop the
`status` **string** column" - all missed by the flat phrase list) fixed with a
word-insertion-tolerant regex (`drop[\w\s\`.-]{0,20}status[\w\s\`.-]{0,15}column`); and a
literal substring collision - the existing flat phrase `"drop column status"` is a
substring of `"drop column status_id"`, which false-matched a *rollback* step
hypothetically describing dropping the **new** column ("(2) run `ALTER TABLE orders DROP
CONSTRAINT fk_orders_status_id`, (3) run `ALTER TABLE orders DROP COLUMN status_id`") as
if it were the real final drop of the *old* column - fixed by converting that one phrase
into `drop column status\b` (a word boundary rejects the trailing `_id`, since underscore
is a word character and produces no boundary). None of these three fixes were the cause
of `ms-01`'s stubbornly low `ordering_correct` (1-2/10 even after all three fixes),
though - direct inspection of `[0,1]` (dual-write before backfill) across all 10 samples
found that in 7-8 of them, the model's *own plan* backfills before deploying the
dual-write code, which is a real, reproducible planning defect (not a phrasing gap): if
existing writes to `orders.status` continue between an early backfill and a
later-deployed dual-write path, any row updated in that window has a stale `status_id`
that backfill already stamped and dual-write never revisits. This is worth surfacing as
a genuine calibration finding on this suite, not something to patch away with more
phrase engineering - left as-is. One sample (`R1 #1`) hit a different, structural issue
instead: the model's own text combines both ideas into one step ("dual-write allows new
writes to populate status_id **while backfill** processes historical data"), so both
groups match the *same* step index, and the ordering check's strict less-than can never
be satisfied by a tie - a scorer limitation for combined narration, not a case bug, and
the same shape of issue `ms-08` hit independently below.

**`ms-08`'s `required_steps[2]` (observe canary) was missing on all 10 samples for one
reason: every sample says "**monitor**", never "observe"/"watch"/any of the other listed
phrases.** Fixed with a co-occurrence pattern, `(?=.*(monitor|observ|watch))(?=.*canary)`,
scoped to require "canary" in the same step specifically so it can't false-match the
unrelated, always-earlier `monitoring_setup`-style infra step (confirmed directly - that
step never mentions "canary" in any of the 10 samples). This raised `step_coverage` from
0/10 to 9/10, but `ordering_correct` dropped from a vacuous 10/10 (the constraint was
never evaluable before, since group 2 never matched) to 6/10 - in 4 samples, the model
writes one combined step ("enable_canary enable provider for 1-2% of real transactions;
activate all monitoring... to begin observing real-world performance") that satisfies
both the "start the canary" and "observe the canary" groups at the identical index,
which the strict-less-than ordering check can never pass. This is the same tied-index
class `ms-01` hit above, not a new bug, and not something narrowing the pattern further
would fix without also losing real coverage - left open. Net effect is still a clear win:
`fully_correct` went from a hard 0/10 (coverage always failed) to 5/10.

**`ms-10`'s `required_steps[3]` (full rollout) needed three small regexes for three
distinct real phrasings**, all word-insertion variants of already-listed phrases: `"100%
of production traffic"`/`"100% of traffic"` (inserted words between the literal `"100%
traffic"` phrase - fixed with `100%[\w\s]{0,20}traffic`, direction-limited rather than an
order-agnostic lookahead specifically because one sample's *intra-canary* traffic ramp
("Traffic to canary gradually increased in stages (5% → 25% → 50% → 100%)") mentions
"traffic" and "100%" in the same sentence too, just in the opposite order - confirmed the
directional, distance-limited version doesn't match that sentence before trusting it);
`"all remaining production regions"` (fixed with `remaining[\w\s-]{0,20}regions`,
matching the case's own existing plural-only word choice); and `"rollout completed to
all regions"` / `"traffic fully shifted... at 100%"` (fixed with `(complet(e|ed)|
shift(ed)?)[\w\s;,.]{0,30}(all regions|100%)`). All three were checked against the one
sample (of 5, in the fresh run) that plans a percentage ramp with no separate "all
regions"/"100% traffic" sentence at all ("increase traffic percentage to new algorithm
(5% → 25% → 50% → 100%) or expand to additional regions") - none of the three regexes
match it, correctly, since that plan genuinely never states unambiguously that it
reaches full rollout across all regions rather than just a traffic percentage; left as a
genuine, if minor, coverage gap rather than forced to pass. `step_coverage` went from
6/10 to 9/10 without breaking any of the already-passing samples.

**Two bugs found by extension, not by the four flagged cases:** `ms-02`'s
`required_steps[2]` (expand) missed on 2 of 6 real samples (4 of the 10 total samples in
these two runs are `ms-02`'s well-documented parse/timeout failures, unrelated to
phrasing - see the N=5 section above) for the same reason both times: the plan says
"repeat for each **remaining region**" (singular), and the existing phrase list only had
the plural "remaining regions." Fixed by adding the singular phrase directly (a plain
substring addition, no regex needed, and confirmed safe - every real occurrence of
"remaining region" in this suite's samples is genuinely about region-by-region
expansion, no false-positive risk found). `ms-04`'s and `ms-12`'s
`required_steps[1]` (shared template) share the exact same group definition for the
same "consolidate two CI/CD pipelines" scenario, and both showed the same word-insertion
gap on multiple samples: "the **shared ci/cd pipeline** template", "the **shared
pipeline infrastructure and** template", "the **consolidated pipeline** template" - none
matching the flat `"shared template"`/`"unified pipeline"`/`"common template"` phrases.
Fixed once with `(shared|unified|consolidated|common)[\w\s/-]{0,25}(template|pipeline)`
and applied to both cases, since it's confirmed the same underlying group and the same
failure mechanism, not two coincidentally-similar bugs.

**Deliberately left open, and why - same "don't patch on n=1, don't force a match"
discipline this file has followed throughout:**
- `ms-01`'s remaining `[0,1]` ordering misses (7-8/10 samples) - a real, reproducible
  model planning defect (backfill before dual-write), not a scorer bug; documented above,
  not chased further.
- `ms-01` sample `R1 #1` and several `ms-08` samples - tied-index ordering failures from
  the model narrating two required concepts inside one combined step; a scorer
  limitation (strict less-than can't score a tie), not a phrase-list problem, and out of
  scope for a phrase-widening pass.
- `ms-02`'s `required_steps[1]` (monitor) bare-word collision with an earlier, unrelated
  `monitoring_setup` infra-provisioning step - the same class of bug `ms-08`'s fix
  targets, confirmed present here too, but *not* applied in this pass to avoid further
  scope creep beyond the four originally-flagged cases plus the two directly-adjacent
  bugs found while inspecting them. Good candidate for the next pass, using the same
  `(?=.*monitor)(?=.*canary-or-region-specific-term)` co-occurrence approach.
- `ms-03`'s `required_steps[1]` (shadow/dual-write) misses - inspected both real misses
  directly; neither is a phrasing gap. Both plans jump from staging reconciliation
  straight to a live canary percentage without ever describing a production
  shadow/dual-processing step first - a genuine plan gap, not a word-choice problem.
- `ms-03`'s `required_steps[2]` (staged rollout/percentage) - re-confirmed still open
  per the precedent already recorded in the prior revision section: a bare percentage
  regex was tried and rejected again this pass for the same reason as before (the
  case's own `shadow`/dual-write step independently mentions percentages like "100% of
  transactions" for an unrelated purpose in several samples, which a percentage-based
  regex can't safely distinguish from the real staged-rollout ramp without also
  requiring semantic context embeddings already ruled out).

**Before/after, re-scored offline against both saved run files using the final,
committed `multi_step.jsonl` (zero new model calls):**

| check | R1 before | R1 after | R2 before | R2 after |
|---|---|---|---|---|
| step_coverage | 39/60 (65.0%) | 47/60 (78.3%) | 31/60 (51.7%) | 48/60 (80.0%) |
| ordering_correct | 31/60 (51.7%) | 31/60 (51.7%) | 31/60 (51.7%) | 33/60 (55.0%) |
| no_false_positives | 60/60 (100%) | 60/60 (100%) | 60/60 (100%) | 60/60 (100%) |
| fully_correct | 19/60 (31.7%) | 24/60 (40.0%) | 17/60 (28.3%) | 24/60 (40.0%) |

Combined across both runs (120 samples): `fully_correct` 36/120 (30.0%) -> 48/120
(40.0%), `step_coverage` 70/120 (58.3%) -> 95/120 (79.2%), `ordering_correct` 62/120
(51.7%) -> 64/120 (53.3%). No case's `fully_correct` count decreased in either run after
these fixes - the `ordering_correct` dip inside `ms-08` (5/5 -> 3/5 in each run, discussed
above) is the one metric that got *worse* per-case, and it's a fully understood tradeoff
(a previously-vacuous pass becoming a real, sometimes-failing check now that
`step_coverage` actually finds the group), not a hidden regression - `ms-08`'s
`fully_correct` still rose in both runs (0/5 -> 3/5 and 0/5 -> 2/5) because coverage was
the binding constraint before.

**Not re-run against the model.** Every number above comes from re-scoring the two
existing saved runs' `predicted` dicts - zero new `claude -p` calls, zero new cost, per
this task's constraint. A fresh N-sample run is the natural next step before trusting
these numbers for a tier decision, for the same reason the prior revision pass's fresh
run diverged from its own offline prediction (65% predicted vs. 51.7% actual on
`step_coverage`) - every fix here was validated against the same 120 samples that
motivated it, which confirms the fix behaves as intended but not that it generalizes to
new generations. `ms-02`'s and `ms-12`'s `required_steps` fixes in particular haven't
been checked against fresh output at all.

## `multi_step` fresh N-sample run against the fixed case file (2026-07-27) - one bogus run discarded, the honest re-run generalizes much better than the prior cycle

Ran the deferred fresh validation: `python -m eval_harness run --suite multi_step
--prompt multi_step_v1 --model haiku --samples 5` against the just-fixed
`multi_step.jsonl`. First attempt (`runs/20260726T232906Z__multi_step_v1__haiku.json`)
was discarded, not trusted - 13 of 60 generation calls failed outright (`nonzero exit`,
empty `predicted={}`, concentrated in `ms-10`/`ms-11`/`ms-12`) and all 60 judge calls
failed the same way (`avg_judge_score` read a flat 0.0, the exact bogus-run shape this
file has warned about since the `bug_triage` section), both far beyond any prior
single-suite reliability blip recorded here (previously it was the judge pass failing
partway through one run, never generation itself failing this broadly). A direct health
check (`claude -p` with a trivial prompt) succeeded cleanly immediately after, so this
reads as a transient outage during that specific run's execution window, not an ongoing
break - deleted per established practice and re-run.

The re-run (`runs/20260727T024430Z__multi_step_v1__haiku.json`, $3.00, zero generation
failures, judge rationales all real prose) is trusted:

| check | offline-predicted (post-fix, R1+R2 combined) | fresh N=5 (new samples) |
|---|---|---|
| step_coverage | 79.2% | 68.3% |
| ordering_correct | 53.3% | 55.0% |
| fully_correct | 40.0% | 36.7% |
| no_false_positives | 100% | 100% |

**The generalization gap is real but much smaller than the prior revision cycle's** (that
one predicted 65% step_coverage and got 51.7% actual, a 13pp miss; this one predicted
79.2% and got 68.3%, an 11pp miss, but `ordering_correct` and `fully_correct` landed
within ~3pp of prediction rather than diverging by 10+pp). Direct inspection of the two
cases still at 0/5 (`ms-10`, `ms-12`) found two distinct, real causes, not one repeated
pattern:

- **`ms-12` group3 (rollback) has a genuine remaining phrase gap, not yet touched by
  this pass**: one sample's `killswitch_setup` step says "revert either team to old
  pipeline if critical" - not matching any of the four flat phrases (`"rollback"`,
  `"revert to the old pipeline"`, `"fall back"`, `"revert to their old pipeline"`), and
  this group has no regex/pattern entry at all, unlike the groups this pass actually
  edited. A real, fixable gap - just not one this pass's targeted fixes (which touched
  groups 0-2, not group3) happened to cover.
- **`ms-10`'s remaining misses are the concrete-vs-abstract vocabulary gap this file has
  hit before** (`architecture`'s `ar-02`/`ar-05`/`ar-06`, this suite's own N=5 finding),
  not a new bug class: samples phrase full-rollout completion as "mark rollout as
  complete" (no "all regions"/"100%" nearby), "full adoption... 30+ days post-full-
  launch" (no "complete"/"shift" verb), or ramp within-canary to 100% while separately
  describing multi-region expansion in unrelated wording - none of which the three
  regexes this pass added actually cover, and one of which (the within-canary 100% ramp)
  is a case where a *broader* regex would have been actively wrong to add, per the
  pattern already rejected in the earlier revision pass for exactly this scenario.

**A third, cross-cutting cause recurred independently in this run, now confirmed across
enough cases to call it a real scorer limitation rather than a fluke**: `ms-12` sample 0
has its audit step (group0) and shared-template step (group1) land at the *same* array
index because the model wrote one combined step doing both
("Audit both teams'... design a unified pipeline template..."), which the strict
less-than ordering check can never pass on a tie. This is the identical shape already
found independently in `ms-01` (dual-write/backfill combined) and `ms-08` (canary-start/
observe combined) during the fix pass above - three different cases, same root cause
(the scorer has no way to treat "both required ideas landed in one step, correctly
un-ordered because there's nothing to order between them" as a pass). Worth treating as
a real scorer enhancement candidate (e.g. `<=` instead of strict `<` when both groups
match the same index) rather than continuing to document it per-case, since it's now
recurred three times with a consistent, well-understood cause.

**Not chased further this session.** Following this file's own "don't keep patching
narrowly, recognize the systemic shape" precedent (`architecture`'s concrete-vs-abstract
finding, `multi_step`'s own scorer-phrase-strictness finding): the tied-index issue in
particular has enough independent confirmation now to justify a scorer change rather
than another case-by-case documentation entry, and `ms-10`'s abstract-completion-phrase
gap is the same class of problem the embeddings approach was rejected for solving
safely - both are flagged as decisions for the next session rather than acted on here.

## `multi_step` tied-index scorer fix + two more phrase fixes (2026-07-27) - the deferred scorer decision, done, all offline

Picked up the two decisions the prior session flagged and deferred. Zero new `claude -p`
calls this pass - every number below comes from re-scoring the same two saved run files
(`20260726T212852Z`, `20260727T024430Z`, 120 real haiku samples total) against code/case
changes, per this project's established "verify against existing data before paying for
a new run" discipline.

**Tied-index fix**: `rule_based_score_multi_step`'s `ordering_correct` check
(`scorers.py`) changed `early_idx < late_idx` to `early_idx <= late_idx`. This directly
targets the scorer limitation confirmed independently three times in the prior
session (`ms-01`, `ms-08`, `ms-12`) - a model narrating two required ideas in one
combined step lands both groups at the same array index, which is a correct,
un-ordered plan (there's nothing to order between two ideas expressed in the same
sentence), not a reversed one. Covered by a new test
(`test_rule_based_score_multi_step_ordering_passes_when_both_groups_match_same_step`).

**Two more phrase-group fixes**, both confirmed by direct inspection of real saved
output before being applied (same discipline as every fix in this file):
- `ms-02` group1 (monitor): the bare `"monitor"` phrase was a false-positive magnet -
  it matched an early, generic `monitoring_setup`/`deploy_monitoring_and_alerts`-style
  infra-provisioning step in most samples, which falsely satisfied `step_coverage` at
  an index *before* the canary step even started, corrupting `ordering_correct` for
  the `[0,1]` constraint. Removed the bare phrase; added a co-occurrence pattern
  requiring a monitor-word (`monitor|observ|watch|check`) alongside `"canary"` in the
  *same* step - confirmed this doesn't collide with the false early step (verified: none
  of the observed `monitoring_setup`-style steps mention "canary") while still finding
  the real canary-monitoring step in every sample checked. `"check"` was added after the
  first version (canary + monitor/observ/watch only) still missed one real sample
  phrased as "Check metrics... against baseline thresholds" - confirmed the addition
  only gains matches, never flips an existing real match to `None`, before keeping it.
- `ms-12` group3 (rollback): one sample phrased the kill-switch step as "revert either
  team to old pipeline if critical" - didn't match any of the four flat phrases. Added
  a scoped pattern (`revert[\w\s]{0,20}old pipeline`) rather than a bare `"revert"`
  phrase; confirmed against every `ms-12` sample in both saved runs that it only matches
  genuine rollback statements and never the unrelated cutover/decommission/parallel-run
  steps that also happen to mention "old pipeline" without "revert" nearby.

**Combined effect, re-scored offline against the same 120 samples (zero new cost):**

| check | before (post prior-session fixes) | after (this pass) |
|---|---|---|
| step_coverage | 58.3% | 75.0% |
| ordering_correct | 51.7% (still `<`) | 70.8% (with `<=`) |
| no_false_positives | 100% | 100% |
| fully_correct | 30.0% | 52.5% |

**This 52.5% is an offline re-score, not a fresh run, and should not be quoted as this
suite's real accuracy without a new sample.** The prior two revision cycles both showed
offline predictions overstate real generalization (one predicted 65% step_coverage and
got 51.7% actual; another predicted 79.2% and got 68.3% actual) - there's no reason to
expect this pass is different, and it hasn't been checked against fresh output. The
last *actual* fresh-run number for this suite remains **36.7%** fully_correct
(`runs/20260727T024430Z__multi_step_v1__haiku.json`, N=5) - that is the number to use
for any calibration decision until a new run happens, not 52.5%.

**Not re-run against the model, deliberately** - the user explicitly did not want
another paid run this session. A fresh N-sample run to see whether the real
fully_correct rate actually moves is the natural next step whenever that constraint
lifts.

## Router tier synthesis across all 7 suites (2026-07-27) - first cross-suite `TYPE_DOMAIN_GRID` reconciliation

`llm-task-router/classifier.py`'s `TYPE_DOMAIN_GRID` (task_type x domain -> tier) is a
hand-authored heuristic table that predates any calibration run in this repo. With all
7 task types now having at least one judged Claude-tier benchmark on record, went
through the full set to check the grid against real data and correct what the data
actually contradicts - without a new paid run, per the same constraint as the scorer
work above. Full per-suite verdicts already exist earlier in this file; this section is
the cross-suite synthesis and the resulting `TYPE_DOMAIN_GRID` change.

**Two factual corrections made before trusting anything, both verified directly against
raw run JSON/git history rather than taken from memory:**
- `multi_step`'s real trusted number is **36.7%** (the last fresh N=5 run), not the
  52.5% offline re-score from this session's scorer-fix pass above - see that section's
  own explicit caveat against quoting it as real accuracy.
- `triage`'s "monotonic, not-a-ceiling-effect" story (haiku 60% < sonnet 66.7% < opus
  73.3%) is thinner than it reads: there are three haiku `v1_naive` runs on disk, not
  one. The canonical judged run (`20260720T072220Z`, 60.0% fully_correct / 100%
  category acc) is what this file's earlier table cites, but two more real, rule-scored
  (`--no-judge`, confirmed via `judge_rationale: "judging disabled"`, not a silent
  failure) haiku runs exist - `20260720T022514Z` (33.3% / 80%) and `20260723T080420Z`
  (40% / 80%, recorded incidentally during Codex-provider-flag verification) - both
  meaningfully below the canonical number on both metrics. Sonnet and opus each have
  exactly one run. The whole triage row is N=1-per-tier on the judged number, with a
  real, unreconciled second/third rule-scored haiku sample sitting on disk - the exact
  situation this file's own "don't conclude on N=1" policy (already applied to
  `code_review` and `multi_step`) hasn't been applied to until now.

**Two rows changed - `code_gen` and `refactor`, both to uniform `L`:** both suites show
zero measured discrimination across haiku/sonnet/opus (100% tests_passed/fully_correct
on all three tiers), on suites specifically hardened with adversarial cases individually
validated to fail plausible-wrong implementations (not a ceiling-effect artifact - see
each suite's case-design section above). No case in either suite shows a higher tier
outperforming haiku, so the grid's prior `H`/`M` cells for these rows reflected pillar
22's original heuristic guess, not a demonstrated need for a pricier tier. A "conservative
middle ground" (H->M) was considered and rejected - M is exactly as unsupported by this
data as H, so it would just be a smaller unjustified expense instead of a larger one.
**Caveat stated explicitly, not hidden:** neither suite is domain-segmented, so this
removes a domain-*blind* judgment call, it does not add domain-*aware* evidence that
e.g. infra-specific code-gen (Terraform/K8s manifests) is safe at the cheap tier -
acceptable given the no-new-runs constraint, but a real gap, not a resolved one.

**Five rows left untouched, each with its own specific unblock condition (not silently
dropped):**
- `summarization`: already uniform `L` - data confirms it's correct, haiku is the
  empirically *best* tier here (87.5% vs sonnet 62.5%/opus 75.0%), not just adequate.
  No unblock needed.
- `triage`: `L`/`M` split unchanged, but its confidence is downgraded in this writeup
  from "confirmed by data" to the same bucket as `code_review` below - directionally
  plausible, not yet confirmed. Unblock: a `--samples N` re-run reconciling the three
  conflicting haiku numbers above.
- `code_review`: `M` cells unchanged. Data suggests they're questionable (sonnet ties
  haiku's accuracy with worse judge coherence - see that suite's own table above), but
  this is explicitly N=1 data and the project's own established rule for this exact
  suite is not to act on N=1. Unblock: a `--samples N` re-run, same as triage.
- `architecture`: uniform `H` unchanged. Confirmed, not just suspected, scorer bug:
  opus's real `ar-07` output rejects Kubernetes correctly inside its own `design` field
  prose ("Explicitly not included, and why: no Kubernetes..."), but `must_not_include`
  only checks `design` for anti-pattern bait and has no way to distinguish "proposing X"
  from "explicitly rejecting X" when that rejection isn't in the separate
  `alternatives_considered` field - directly deflating opus's 58.3% number alongside an
  unrelated `ar-11` JSON parse failure. Also the highest-stakes row (flagship fallback
  for unrecognized task shapes - see `classify_description`'s docstring). Unblock: fix
  the `must_not_include` scorer to pool `design` + `alternatives_considered`'s
  `rejected_because` text (already flagged as a follow-up in this suite's own section
  above), then a clean re-run.
- `multi_step`: `M`/`H` unchanged. Zero sonnet/opus data exists for this suite at all -
  haiku-only, and its own real (non-scorer) findings (`ms-01`'s backfill-before-dual-write
  ordering defect, `ms-03`'s missing shadow-traffic step) argue for caution, not
  relaxation. No comparative basis exists to change tier assignment either direction yet.
  Unblock: a sonnet/opus `multi_step` run.

**Implementation**: `llm_task_router/classifier.py`'s `TYPE_DOMAIN_GRID` rows for
`code_gen`/`refactor` changed to uniform `"L"`; module docstring updated to stop
claiming the whole grid is pillar 22's *confirmed* table (only two rows now are).
`tests/test_classifier.py`'s `test_infra_code_gen_escalates_high` (no longer true)
replaced with `test_code_gen_is_uniformly_low` and `test_refactor_is_uniformly_low`,
mirroring the existing `test_summarization_is_uniformly_low` pattern. Both repos' test
suites pass (68/68 in `llm-task-router`, 128/128 here). No new model calls, no cost.
