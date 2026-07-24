"""Executes model-generated code to grade code_gen cases - the one suite
where "correct" means "passes tests" rather than "matches a label."

Trust model: subprocess + timeout, no container. Generated code runs
directly on this machine with only a wall-clock timeout as a guard, the same
trust model this repo already extends to `claude -p`/`codex exec` output.
Acceptable for a personal learning project benchmarking models you already
trust; would need real sandboxing (network-disabled container, resource
caps) before ever grading untrusted third-party input.

Shells out to a `pytest` binary the same way claude_cli.py/codex_cli.py
shell out to `claude`/`codex` - not a new Python import dependency for
eval_harness itself (the repo's own test suite already requires pytest on
PATH).
"""

import subprocess
import sys
import tempfile
from pathlib import Path


def run_pytest_check(generated_code: str, test_code: str, timeout: int = 10) -> tuple[bool, str]:
    """test_code must `from solution import <name>` to exercise generated_code."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "solution.py").write_text(generated_code)
        (tmp / "test_solution.py").write_text(test_code)

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "test_solution.py", "-q"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"

        output = (proc.stdout + proc.stderr).strip()
        tail = "\n".join(output.splitlines()[-20:])
        return proc.returncode == 0, tail
