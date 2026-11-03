"""Run the test files this branch adds under --fold-check, before the full lockstep stage.

A test file new since the last full `make test-fold` can go red there, after the cheaper stages
have passed. This is the mergeq `fold-new` stage (`make test-fold-new`): it lists the test files
added (or renamed in) since the merge-base with the base branch, plus untracked ones, and runs
only those with `--fold-check`. A branch that adds no test file pays for one git call.

Run it in a worktree to check your own new files: `uv run python devtools/fold_new_tests.py`.
Agents without the heavy lock run `pytest --fold-check <file>` on the file directly instead.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The suites `make test-fold` leaves out; the same marker expression and ignore.
MARKERS = "not integration and not hypothesis and not soundness and not fuzz and not tumbler and not conformance"
SKIPPED_DIRS = ("tests/fuzz/", "tests/conformance/", "tests/tumbler/")
NO_TESTS_COLLECTED = 5


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def new_test_files(base: str) -> list[str]:
    added = _git("diff", "--name-only", "--diff-filter=AR", f"{base}...HEAD", "--", "tests")
    untracked = _git("ls-files", "--others", "--exclude-standard", "--", "tests")
    names = sorted(set(added) | set(untracked))
    return [
        n
        for n in names
        if n.endswith(".py")
        and Path(n).name.startswith("test_")
        and not n.startswith(SKIPPED_DIRS)
        and (ROOT / n).is_file()
    ]


def main() -> int:
    with (ROOT / "devtools" / "mergeq" / "mergeq.toml").open("rb") as fh:
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
        [sys.executable, "-m", "pytest", "-m", MARKERS, "--fold-check", "-q", *files], cwd=ROOT
    ).returncode
    return 0 if rc == NO_TESTS_COLLECTED else rc


if __name__ == "__main__":
    sys.exit(main())
