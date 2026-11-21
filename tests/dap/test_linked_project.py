"""A project reached through a junction or a symlink still finds its modules."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.handlers.lifecycle_launch import module_in_folder
from tests.dap.test_reload_console import _repl, _send_request, _single_response


def _make_link(link: Path, target: Path) -> None:
    """Make *link* point at *target*, or skip the test where the machine allows neither form."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if result.returncode == 0:
            return
    pytest.skip("cannot make a symlink or a junction here")


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Write a project under ``real/`` and put it on ``sys.path`` by its ``link/`` name."""
    real = tmp_path / "real"
    (real / "plc").mkdir(parents=True)
    link = tmp_path / "link"
    _make_link(link, real)
    monkeypatch.syspath_prepend(str(link))
    for name in ("tags", "plc", "plc.tags", "plc.limits"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    (real / "plc" / "__init__.py").write_text("")
    (real / "plc" / "limits.py").write_text("LIMIT = 5\n")
    (real / "plc" / "tags.py").write_text(
        "from pyrung.click import ClickBlocks, TagMap\n"
        "from pyrung.core import Bool\n"
        "blocks = ClickBlocks()\n"
        "x, y, c, *_ = blocks\n"
        "Pump = Bool('Pump')\n"
        "mapping = TagMap({Pump: c[1]})\n"
    )
    (real / "run.py").write_text(
        "from pyrung import PLC\n"
        "from pyrung.core import Bool, Int, Program, copy, rung\n"
        "from plc.limits import LIMIT\n"
        "from plc.tags import Pump\n"
        "Go = Bool('Go')\n"
        "Out = Int('Out')\n"
        "with Program(strict=False) as logic:\n"
        "    with rung(Go):\n"
        "        copy(LIMIT, Out)\n"
        "runner = PLC(logic)\n"
    )
    return real, link


def _launch(tmp_path: Path, real: Path) -> tuple[DAPAdapter, io.BytesIO]:
    out = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out)
    response = _single_response(
        _send_request(
            adapter, out, seq=1, command="launch", arguments={"program": str(real / "run.py")}
        )
    )
    assert response["success"] is True, response.get("message")
    return adapter, out


def _cleanup() -> None:
    for name in ("plc", "plc.tags", "plc.limits"):
        sys.modules.pop(name, None)


def test_launch_through_a_link_finds_the_tag_map(tmp_path: Path, monkeypatch: Any):
    real, _link = _project(tmp_path, monkeypatch)
    try:
        adapter, _out = _launch(tmp_path, real)
    finally:
        _cleanup()
    assert adapter._tag_map is not None


def test_reload_through_a_link_runs_the_new_program(tmp_path: Path, monkeypatch: Any):
    real, _link = _project(tmp_path, monkeypatch)
    try:
        adapter, out = _launch(tmp_path, real)
        (real / "plc" / "limits.py").write_text("LIMIT = 9\n")
        response, _ = _repl(adapter, out, "reload", seq=11)
        assert response["success"] is True, response.get("message")
        assert adapter._runner is not None
        adapter._runner.patch({"Go": True})
        adapter._runner.step()
        assert adapter._runner.current_state.tags["Out"] == 9
    finally:
        _cleanup()


def test_module_in_folder_resolves_both_sides(tmp_path: Path, monkeypatch: Any):
    real, link = _project(tmp_path, monkeypatch)
    via_link = type("M", (), {"__file__": str(link / "plc" / "tags.py")})()
    via_real = type("M", (), {"__file__": str(real / "plc" / "tags.py")})()
    elsewhere = type("M", (), {"__file__": str(tmp_path / "other.py")})()
    for folder in (real, link):
        inside = module_in_folder(folder)
        assert inside(via_link) and inside(via_real)
        assert not inside(elsewhere)
        assert not inside(type("M", (), {})())
        assert not inside(type("M", (), {"__file__": None})())
