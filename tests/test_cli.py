from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

import pytest

from pyrung import cli
from pyrung.core import Bool, Program, out, rung


def test_main_parses_ladder_check(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_check(args: argparse.Namespace) -> None:
        captured["module"] = args.module
        captured["select"] = args.select
        captured["ignore"] = args.ignore
        captured["dt"] = args.dt

    monkeypatch.setattr(cli, "_cmd_check", _fake_check)
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
    }


def test_check_prints_findings_and_exits_on_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    read_only = Bool("ReadOnly", readonly=True)
    with Program() as program:
        with rung():
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
        with rung(button):
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
        with rung():
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
