"""Tests for mergeq.py's cancel safety and the fold-new stage's file selection.

Run: timeout 120 uv run pytest devtools/mergeq -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mq():
    return _load("mergeq_under_test", HERE / "mergeq.py")


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_cancel_kills_the_runner_it_recorded(mq) -> None:
    proc = _sleeper()
    try:
        assert mq._kill_runner(f"{proc.pid} {mq._proc_start(proc.pid)}") is None
        proc.wait(timeout=10)
        assert proc.poll() is not None
    finally:
        proc.kill()


def test_cancel_spares_a_reused_pid(mq) -> None:
    proc = _sleeper()
    try:
        why = mq._kill_runner(f"{proc.pid} 123456789")  # a pid file from before a reboot
        assert why and "different process" in why
        assert proc.poll() is None
    finally:
        proc.kill()


def test_cancel_spares_a_process_it_cannot_verify(mq) -> None:
    proc = _sleeper()
    try:
        for text in (str(proc.pid), f"{proc.pid} "):  # the old format: a pid alone
            why = mq._kill_runner(text)
            assert why and "creation time" in why
        assert proc.poll() is None
    finally:
        proc.kill()


def test_cancel_reports_a_stale_or_unreadable_pid_file(mq) -> None:
    proc = _sleeper()
    start = mq._proc_start(proc.pid)
    proc.kill()
    proc.wait()
    why = mq._kill_runner(f"{proc.pid} {start}")
    assert why and "no longer running" in why
    for text in ("", "abc", "12x 5"):
        why = mq._kill_runner(text)
        assert why and "unreadable" in why
        assert why.isascii()


def test_creation_time_is_stable_for_a_live_process(mq) -> None:
    start = mq._proc_start(os.getpid())
    assert start
    assert mq._proc_start(os.getpid()) == start


def test_fold_new_lists_only_added_test_files(tmp_path: Path, monkeypatch) -> None:
    fold_new = _load("fold_new_under_test", HERE.parent / "fold_new_tests.py")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    def touch(name: str) -> None:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x = 1\n")

    git("init", "-b", "dev")
    touch("tests/test_old.py")
    git("add", ".")
    git("commit", "-m", "base")
    git("switch", "-c", "feature")
    for name in (
        "tests/core/test_new.py",
        "tests/helper.py",
        "tests/fuzz/test_fz.py",
        "tests/conformance/test_cf.py",
        "tests/tumbler/test_tb.py",
        "src/test_not_under_tests.py",
    ):
        touch(name)
    git("add", ".")
    git("commit", "-m", "add")
    (tmp_path / "tests/test_old.py").write_text("x = 2\n")  # modified, not new
    touch("tests/test_untracked.py")
    monkeypatch.setattr(fold_new, "ROOT", tmp_path)
    assert fold_new.new_test_files("dev") == [
        "tests/core/test_new.py",
        "tests/test_untracked.py",
    ]
    git("switch", "dev")  # a branch that adds nothing
    assert fold_new.new_test_files("dev") == ["tests/test_untracked.py"]
