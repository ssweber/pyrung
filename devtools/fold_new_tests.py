"""Run the test files this branch adds under --fold-check, before the full lockstep stage.

A test file new since the last full `make test-fold` can go red there, after the cheaper stages
have passed. This is the mergeq `fold-new` stage (`make test-fold-new`): it lists the test files
added (or renamed in) since the merge-base with the base branch, plus untracked ones, and runs
only those with `--fold-check`. A branch that adds no test file pays for one git call.

It judges mismatches only (`--fold-check-allow-vacuous`): the share of runs that used the loop
is a verdict on a whole suite, and a few files whose long runs all decline honestly are fine.
Agents run `pytest --fold-check --fold-check-allow-vacuous <file>` on the file directly.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# What `make test-fold` selects: this marker expression, minus tests/fuzz (--ignore). The other
# excluded suites are left to `-m`, so a file new in them is collected the same way.
MARKERS = "not integration and not hypothesis and not soundness and not fuzz and not tumbler and not conformance"
SKIPPED_DIRS = ("tests/fuzz/",)
NO_TESTS_COLLECTED = 5


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _python_files() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["tool"]["pytest"]["ini_options"].get("python_files", ["test_*.py"])


def new_test_files(base: str) -> list[str]:
    added = _git("diff", "--name-only", "--diff-filter=AR", f"{base}...HEAD", "--", "tests")
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", "tests")
    names = sorted(set(added) | set(untracked))
    patterns = _python_files()
    return [
        n
        for n in names
        if any(fnmatch.fnmatch(Path(n).name, pat) for pat in patterns)
        and Path(n).name not in ("conftest.py", "__init__.py")
        and not n.startswith(SKIPPED_DIRS)
        and (ROOT / n).is_file()
    ]


def main() -> int:
    config = os.environ.get("MERGEQ_CONFIG") or ROOT / "devtools" / "mergeq" / "mergeq.toml"
    with open(config, "rb") as fh:
        base = tomllib.load(fh)["base"]
    files = new_test_files(base)
    if not files:
        print(f"fold-new: no test file added since {base}; nothing to check")
        return 0
    print(f"fold-new: {len(files)} new test file(s) under --fold-check:")
    for name in files:
        print(f"  {name}")
    sys.stdout.flush()
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            MARKERS,
            "--fold-check",
            "--fold-check-allow-vacuous",
            "-q",
            *files,
        ],
        cwd=ROOT,
    ).returncode
    return 0 if rc == NO_TESTS_COLLECTED else rc


if __name__ == "__main__":
    sys.exit(main())
