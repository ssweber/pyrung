"""Tests for mergeq.py's cancel safety, targeted slots and the fold-new stage's file selection.

Run: timeout 120 uv run pytest devtools/mergeq -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Iterator
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


class _Slots:
    """Real `mergeq run` processes against a private lock directory."""

    def __init__(self, tmp_path: Path, slots: int) -> None:
        self.tmp = tmp_path
        self.slots = slots
        self.hold = tmp_path / "hold.py"
        self.hold.write_text(
            "import pathlib, sys, time\n"
            "pathlib.Path(sys.argv[1]).touch()\n"
            "while not pathlib.Path(sys.argv[2]).exists():\n"
            "    time.sleep(0.05)\n",
            encoding="utf-8",
        )

    def env(self, wait: int) -> dict[str, str]:
        config = self.tmp / f"mergeq-{wait}.toml"
        config.write_text(
            f'home = "{self.tmp.as_posix()}/home"\n'
            f'lock_file = "{self.tmp.as_posix()}/.heavy.lock"\n'
            f"targeted_slots = {self.slots}\n"
            f"targeted_wait = {wait}\n",
            encoding="utf-8",
        )
        # The suite itself runs inside a slot when the hook wraps it; these runs must not inherit that.
        env = {k: v for k, v in os.environ.items() if k != "MERGEQ_IN_SLOT"}
        return {**env, "MERGEQ_CONFIG": str(config)}

    def argv(self, *run_args: str) -> list[str]:
        return [sys.executable, "-I", str(HERE / "mergeq.py"), "run", *run_args]

    def start(self, tag: str, *run_args: str, wait: int = 60) -> subprocess.Popen:
        cmd = [sys.executable, str(self.hold), str(self.started(tag)), str(self.release(tag))]
        return subprocess.Popen(
            [*self.argv("--timeout", "60", *run_args, "--"), *cmd],
            env=self.env(wait),
            stderr=subprocess.PIPE,
            text=True,
        )

    def started(self, tag: str) -> Path:
        return self.tmp / f"{tag}.started"

    def release(self, tag: str) -> Path:
        return self.tmp / f"{tag}.release"

    def wait_started(self, tag: str) -> None:
        deadline = time.monotonic() + 30
        while not self.started(tag).exists():
            assert time.monotonic() < deadline, f"{tag} never started"
            time.sleep(0.05)

    def finish(self, tag: str, proc: subprocess.Popen) -> str:
        self.release(tag).touch()
        _, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 0, stderr
        return stderr


@pytest.fixture
def slots(tmp_path: Path) -> Iterator[_Slots]:
    held = _Slots(tmp_path, 2)
    yield held
    for release in tmp_path.glob("*.started"):
        release.with_suffix(".release").touch()


def _fill(slots: _Slots) -> tuple[subprocess.Popen, subprocess.Popen]:
    first, second = slots.start("first"), slots.start("second")
    slots.wait_started("first")
    slots.wait_started("second")
    return first, second


def test_third_targeted_run_waits_and_takes_the_freed_slot(slots: _Slots) -> None:
    first, second = _fill(slots)
    third = slots.start("third")
    time.sleep(3)  # past one poll, and past third's whole --timeout were it counting already
    assert not slots.started("third").exists()
    slots.finish("first", first)
    slots.wait_started("third")
    assert "waiting for a targeted slot" in slots.finish("third", third)
    slots.finish("second", second)


def test_targeted_wait_expires_without_running_the_command(slots: _Slots) -> None:
    first, second = _fill(slots)
    third = slots.start("third", wait=1)
    _, stderr = third.communicate(timeout=30)
    assert third.returncode == 125, stderr
    assert "no targeted slot" in stderr
    assert not slots.started("third").exists()
    slots.finish("first", first)
    slots.finish("second", second)


def test_timeout_starts_once_the_run_has_a_slot(slots: _Slots) -> None:
    first, second = _fill(slots)
    third = subprocess.Popen(
        [*slots.argv("--timeout", "2", "--"), sys.executable, "-c", "pass"],
        env=slots.env(60),
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(3)
    assert third.poll() is None
    slots.finish("first", first)
    _, stderr = third.communicate(timeout=30)
    assert third.returncode == 0, stderr
    slots.finish("second", second)


def test_killed_holder_frees_its_slot(slots: _Slots) -> None:
    first, second = _fill(slots)
    first.kill()
    first.communicate(timeout=30)
    third = slots.start("third")
    slots.wait_started("third")
    slots.finish("third", third)
    slots.finish("second", second)


def test_heavy_run_takes_no_slot(slots: _Slots) -> None:
    first, second = _fill(slots)
    heavy = slots.start("heavy", "--heavy")
    slots.wait_started("heavy")
    slots.finish("heavy", heavy)
    slots.finish("first", first)
    slots.finish("second", second)


def test_nested_run_takes_no_slot(tmp_path: Path) -> None:
    only = _Slots(tmp_path, 1)
    inner = only.argv("--timeout", "30", "--") + [sys.executable, "-c", "pass"]
    result = subprocess.run(
        [*only.argv("--timeout", "60", "--"), *inner],
        env=only.env(1),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_lock_status_counts_slots_in_use(slots: _Slots) -> None:
    def status() -> str:
        argv = [sys.executable, "-I", str(HERE / "mergeq.py"), "lock", "status"]
        return subprocess.run(
            argv, env=slots.env(60), capture_output=True, text=True, timeout=30, check=True
        ).stdout

    assert "0 of 2 in use" in status()
    first = slots.start("first")
    slots.wait_started("first")
    assert "1 of 2 in use" in status()
    slots.finish("first", first)


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
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]" + chr(10) + 'python_files = ["*.py"]' + chr(10)
    )
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
        "tests/conformance/test_cf.py",
        "tests/core/test_new.py",
        "tests/helper.py",
        "tests/test_untracked.py",
        "tests/tumbler/test_tb.py",
    ]
    git("switch", "dev")  # a branch that adds nothing
    assert fold_new.new_test_files("dev") == ["tests/test_untracked.py"]
