from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyrung import cli
from pyrung.core import Bool, Counter, Program, Rung, count_up, out


def test_run_with_optional_profile_passthrough() -> None:
    called: list[str] = []

    def _func(args: argparse.Namespace) -> None:
        assert args.profile is None
        called.append("func")

    cli._run_with_optional_profile(argparse.Namespace(profile=None), _func)

    assert called == ["func"]


def test_run_with_optional_profile_dumps_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class _Profiler:
        def enable(self) -> None:
            events.append("enable")

        def disable(self) -> None:
            events.append("disable")

        def dump_stats(self, path: str) -> None:
            events.append(("dump", path))

    monkeypatch.setitem(sys.modules, "cProfile", SimpleNamespace(Profile=_Profiler))

    def _func(_args: argparse.Namespace) -> None:
        events.append("func")

    profile_path = Path("lock.prof")
    cli._run_with_optional_profile(argparse.Namespace(profile=str(profile_path)), _func)

    assert events == [
        "enable",
        "func",
        "disable",
        ("dump", str(profile_path.absolute())),
    ]


def test_run_with_optional_profile_dumps_stats_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class _Profiler:
        def enable(self) -> None:
            events.append("enable")

        def disable(self) -> None:
            events.append("disable")

        def dump_stats(self, path: str) -> None:
            events.append(("dump", path))

    monkeypatch.setitem(sys.modules, "cProfile", SimpleNamespace(Profile=_Profiler))

    def _func(_args: argparse.Namespace) -> None:
        events.append("func")
        raise KeyboardInterrupt

    profile_path = Path("lock.prof")
    with pytest.raises(KeyboardInterrupt):
        cli._run_with_optional_profile(argparse.Namespace(profile=str(profile_path)), _func)

    assert events == [
        "enable",
        "func",
        "disable",
        ("dump", str(profile_path.absolute())),
    ]


def test_main_parses_lock_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_lock(_args: argparse.Namespace) -> None:
        return None

    def _fake_wrapper(args: argparse.Namespace, func: object) -> None:
        captured["profile"] = args.profile
        captured["module"] = args.module
        captured["func"] = func

    monkeypatch.setattr(cli, "_cmd_lock", _fake_lock)
    monkeypatch.setattr(cli, "_run_with_optional_profile", _fake_wrapper)
    monkeypatch.setattr(sys, "argv", ["pyrung", "lock", "main", "--profile", "lock.prof"])

    cli.main()

    assert captured == {
        "profile": "lock.prof",
        "module": "main",
        "func": _fake_lock,
    }


def test_main_parses_lock_check(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_wrapper(args: argparse.Namespace, func: object) -> None:
        captured["check"] = args.check
        captured["output"] = args.output
        captured["func"] = func

    monkeypatch.setattr(cli, "_run_with_optional_profile", _fake_wrapper)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pyrung", "lock", "main", "--check", "-o", "behavior.lock"],
    )

    cli.main()

    assert captured == {
        "check": True,
        "output": "behavior.lock",
        "func": cli._cmd_lock,
    }


def test_main_parses_ladder_check(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_wrapper(args: argparse.Namespace, func: object) -> None:
        captured["module"] = args.module
        captured["select"] = args.select
        captured["ignore"] = args.ignore
        captured["dt"] = args.dt
        captured["func"] = func

    monkeypatch.setattr(cli, "_run_with_optional_profile", _fake_wrapper)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pyrung",
            "check",
            "main:logic",
            "--select",
            "RUNG",
            "CMP",
            "--ignore",
            "CMP_STATIC_ON_LEFT",
            "--dt",
            "0.05",
        ],
    )

    cli.main()

    assert captured == {
        "module": "main:logic",
        "select": ["RUNG", "CMP"],
        "ignore": ["CMP_STATIC_ON_LEFT"],
        "dt": 0.05,
        "func": cli._cmd_check,
    }


def test_check_prints_findings_and_exits_on_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read_only = Bool("ReadOnly", readonly=True)
    with Program() as program:
        with Rung():
            out(read_only)
    monkeypatch.setattr(cli, "_find_program", lambda _module: (program, SimpleNamespace()))
    args = argparse.Namespace(
        module="main", select=None, ignore=None, extend_select=None, config=None, dt=0.010
    )

    with pytest.raises(SystemExit, match="1"):
        cli._cmd_check(args)

    output = capsys.readouterr().out
    assert "[TAG_READONLY_WRITE] error" in output
    assert "TAG_READONLY_WRITE: 1" in output


def test_check_clean_program_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    button = Bool("Button")
    light = Bool("Light")
    with Program() as program:
        with Rung(button):
            out(light)
    monkeypatch.setattr(cli, "_find_program", lambda _module: (program, SimpleNamespace()))
    args = argparse.Namespace(
        module="main", select=None, ignore=None, extend_select=None, config=None, dt=0.010
    )

    cli._cmd_check(args)

    assert capsys.readouterr().out == "No findings.\n"


def test_check_uses_project_config_and_explicit_config_override(tmp_path, monkeypatch, capsys):
    from pyrung.core.validation.config import CheckConfig, save_check_config

    read_only = Bool("ReadOnly", readonly=True)
    with Program() as program:
        with Rung():
            out(read_only)
    monkeypatch.setattr(cli, "_find_program", lambda _module: (program, SimpleNamespace()))
    monkeypatch.chdir(tmp_path)
    save_check_config(tmp_path / "pyproject.toml", CheckConfig(select=()))
    args = argparse.Namespace(
        module="main", select=None, ignore=None, extend_select=None, config=None, dt=0.010
    )
    cli._cmd_check(args)
    assert capsys.readouterr().out == "No checks selected.\n"
    save_check_config(tmp_path / "explicit.toml", CheckConfig(select=("TAG_READONLY_WRITE",)))
    args.config = str(tmp_path / "explicit.toml")
    with pytest.raises(SystemExit, match="1"):
        cli._cmd_check(args)
    assert "TAG_READONLY_WRITE" in capsys.readouterr().out
    args.ignore = ["TAG_READONLY_WRITE"]
    cli._cmd_check(args)
    assert capsys.readouterr().out == "No checks selected.\n"
    args.ignore = None
    args.select = ["PTR"]
    cli._cmd_check(args)
    assert capsys.readouterr().out == "No findings.\n"
    args.extend_select = ["TAG_READONLY_WRITE"]
    with pytest.raises(SystemExit, match="1"):
        cli._cmd_check(args)


def _lock_args(lock_path: Path, *, check: bool) -> argparse.Namespace:
    return argparse.Namespace(
        module="main",
        output=str(lock_path),
        project=None if check else ["B0", "C0_Done"],
        check=check,
        depth_budget=20,
        max_states=10_000,
    )


def test_lock_warns_when_the_depth_budget_cuts_the_search_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pulse = Bool("In0", external=True)
    down = Bool("B0")
    counter = Counter.clone("C0")
    with Program(strict=False) as program:
        with Rung(pulse):
            count_up(counter, 10).down(down).reset(Bool("B1"))
        with Rung():
            out(down)
    monkeypatch.setattr(cli, "_find_program", lambda _module: (program, SimpleNamespace()))
    lock_path = tmp_path / "pyrung.lock"

    cli._cmd_lock(_lock_args(lock_path, check=False))
    err = capsys.readouterr().err
    assert (
        "Warning: the search stopped at --depth-budget 20."
        " A state more than 20 scans from power-on may be missing from the lock file.\n"
        "Raise --depth-budget to search further."
        " See https://pyrung.com/pyrung/guides/verification/#cli-reference\n"
    ) in err
    assert lock_path.exists()

    cli._cmd_lock(_lock_args(lock_path, check=True))
    captured = capsys.readouterr()
    assert (
        "A state more than 20 scans from power-on may be missing,"
        " so the check can report it lost or miss a new one.\n"
    ) in captured.err
    assert "OK: program matches lock file" in captured.out


def test_lock_of_a_finished_search_has_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    button = Bool("Button", external=True)
    light = Bool("B0")
    with Program(strict=False) as program:
        with Rung(button):
            out(light)
    monkeypatch.setattr(cli, "_find_program", lambda _module: (program, SimpleNamespace()))
    args = _lock_args(tmp_path / "pyrung.lock", check=False)
    args.project = ["B0"]

    cli._cmd_lock(args)
    assert "Warning" not in capsys.readouterr().err
