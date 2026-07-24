import argparse

from eval_harness.report import build_summary, find_previous_run, print_report, save_run
from eval_harness.runner import load_cases, run_suite
from eval_harness.scorers import score_all


def resolve_model(provider: str, model: str | None) -> str:
    """--model has no cross-provider default that makes sense: "haiku" is a
    Claude model name, and guessing a Codex model name risks a real 400 (see
    codex_cli.py) since valid names are account-dependent. For Claude, no
    --model falls back to "haiku". For Codex there's no safe name to guess -
    and letting a benchmark run silently proceed with an unknown model name
    would defeat the whole point of collecting calibration data (a real run
    without --model got saved to disk as "codex/None"), so this raises
    instead of guessing."""
    if model is not None:
        return model
    if provider == "claude":
        return "haiku"
    raise ValueError(
        "--model is required when --provider codex - valid model names are "
        "account-dependent (see codex_cli.py), and benchmark results need a "
        "known model name to be usable as calibration data"
    )


def run_once(suite: str, prompt: str, provider: str, model: str, judge: bool) -> None:
    print(f"Running suite '{suite}' with prompt '{prompt}' on {provider}/{model}...")
    if provider == "codex":
        print("note: Codex does not report per-call cost; $0.00 is a placeholder, while token usage is recorded")
    cases = load_cases(suite)
    outputs = run_suite(suite, prompt, provider=provider, model=model)

    if judge:
        print("\nScoring (rule-based + judge)...")
    else:
        print("\nScoring (rule-based only)...")
    results = score_all(suite, cases, outputs, run_judge=judge)

    summary = build_summary(prompt, model, results, provider=provider)
    saved_path = save_run(summary)
    previous = find_previous_run(prompt, model, before=saved_path)
    print_report(summary, previous=previous)
    print(f"saved: {saved_path.relative_to(saved_path.parent.parent)}")


def compare(suite: str, prompt_a: str, prompt_b: str, provider: str, model: str, judge: bool) -> None:
    for prompt in (prompt_a, prompt_b):
        run_once(suite, prompt, provider, model, judge)

    # pull the just-saved summaries back for a side-by-side
    a = find_previous_run(prompt_a, model)
    b = find_previous_run(prompt_b, model)
    if not a or not b:
        return

    print(f"=== head-to-head: {prompt_a} vs {prompt_b} ({provider}/{model}) ===")
    print(f"{'metric':<20}{prompt_a:>18}{prompt_b:>18}")
    for name in a.check_accuracies:
        av, bv = a.check_accuracies[name], b.check_accuracies.get(name, 0.0)
        print(f"{name + ' accuracy':<20}{av:>18.1%}{bv:>18.1%}")
    for label, key in [
        ("fully correct", "fully_correct_rate"),
        ("judge coherence", "avg_judge_score"),
        ("cost (usd)", "total_cost_usd"),
        ("total tokens", "total_tokens"),
    ]:
        av, bv = getattr(a, key), getattr(b, key)
        fmt = "{:.4f}" if key == "total_cost_usd" else ("{:,}" if key == "total_tokens" else "{:.1%}")
        print(f"{label:<20}{fmt.format(av):>18}{fmt.format(bv):>18}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m eval_harness")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one prompt version through the suite")
    p_run.add_argument("--suite", default="bug_triage")
    p_run.add_argument("--prompt", required=True, help="prompt filename without .txt, e.g. v1_naive")
    p_run.add_argument("--provider", default="claude", choices=["claude", "codex"])
    p_run.add_argument("--model", default=None, help="defaults to haiku for claude; required for codex")
    p_run.add_argument("--no-judge", action="store_true", help="skip the LLM-judge pass (cheaper, faster)")

    p_cmp = sub.add_parser("compare", help="run two prompt versions head-to-head")
    p_cmp.add_argument("--suite", default="bug_triage")
    p_cmp.add_argument("--prompt-a", required=True)
    p_cmp.add_argument("--prompt-b", required=True)
    p_cmp.add_argument("--provider", default="claude", choices=["claude", "codex"])
    p_cmp.add_argument("--model", default=None)
    p_cmp.add_argument("--no-judge", action="store_true")

    args = parser.parse_args()

    try:
        model = resolve_model(args.provider, args.model)
    except ValueError as e:
        parser.error(str(e))

    if args.command == "run":
        run_once(args.suite, args.prompt, args.provider, model, judge=not args.no_judge)
    elif args.command == "compare":
        compare(args.suite, args.prompt_a, args.prompt_b, args.provider, model, judge=not args.no_judge)


if __name__ == "__main__":
    main()
