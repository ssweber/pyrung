"""Under xdist the check still judges the whole session, and a bad run still fails.

Real sessions in a subprocess, on tests/conftest.py's own hooks: only the controller sets
the exit code, and it has run nothing itself, so whatever it doesn't get from a worker is
silently a pass.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import fold_check

REPO = Path(__file__).resolve().parent.parent
PASSES = "def test_passes():\n    pass\n"
# Too late for the fixture that fails the test that met a mismatch; only the session sees it.
MISMATCH_AFTER_THE_TEST = """
import pytest
from tests import fold_check

@pytest.fixture(scope="module", autouse=True)
def mismatch_at_the_end():
    yield
    fold_check.MISMATCHES.append(("somewhere", "met on a worker"))

def test_passes():
    pass
"""


def _session(tmp_path: Path, *options: str, **files: str) -> subprocess.CompletedProcess[str]:
    for name, source in files.items():
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "-p",
            "no:cacheprovider",
            f"--rootdir={tmp_path}",
            "-n",
            "2",
            "--dist=loadfile",
            "--max-worker-restart=0",
            *options,
            str(tmp_path),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _adds_to_stats(key: str, count: int) -> str:
    return f"""
from tests import fold_check

def test_adds():
    fold_check.STATS[{key!r}] += {count}
"""


def test_counts_from_every_worker_add_up_in_the_summary(tmp_path: Path) -> None:
    add = _adds_to_stats("a count", 2)
    result = _session(tmp_path, "--fold-check", test_one=add, test_two=add)
    assert result.returncode == 0, result.stdout
    assert "  a count: 4" in result.stdout


def test_a_session_vacuous_only_taken_together_fails(tmp_path: Path) -> None:
    """Each file is too small to judge, as a single worker's share may be."""
    add = _adds_to_stats("compiled runs long enough for the loop", fold_check.MIN_RUNS_TO_JUDGE - 1)
    result = _session(tmp_path, "--fold-check", test_one=add, test_two=add)
    assert result.returncode == 1, result.stdout
    assert "fold-check VACUOUS" in result.stdout


@pytest.mark.parametrize("options", [("--fold-check",), ()], ids=["fold_check", "plain"])
def test_a_mismatch_met_on_a_worker_fails_the_session(
    tmp_path: Path, options: tuple[str, ...]
) -> None:
    """Plain too: there only the worker has imported fold_check."""
    result = _session(tmp_path, *options, test_one=MISMATCH_AFTER_THE_TEST, test_two=PASSES)
    assert result.returncode == 1, result.stdout


def test_a_worker_killed_by_the_memory_cap_fails_and_names_the_test(tmp_path: Path) -> None:
    """The cap's os._exit, once, and the line mergeq's failure parser reads from it."""
    dies = "import os\n\ndef test_dies():\n    os._exit(99)\n"
    result = _session(tmp_path, test_one=dies, test_two=PASSES)
    assert result.returncode == 1, result.stdout
    failed = re.findall(r"^(?:FAILED|ERROR) (\S+)", result.stdout, re.MULTILINE)
    assert [Path(name).name for name in failed] == ["test_one.py::test_dies"], result.stdout
