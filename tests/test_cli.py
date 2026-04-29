from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyrung import cli


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
