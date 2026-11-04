"""A constant never reads as an address in the workspace round trip."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung_project, pyrung_to_ladder
from pyrung.core import Block, Bool, Program, Real, TagType, Word, rung
from pyrung.core.program import copy

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _import_workspace(files: dict[str, str], tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "project"
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    sys.path.insert(0, str(root / "src"))
    try:
        importlib.import_module("plc.tags")
        importlib.import_module("plc.main")
    finally:
        sys.path.remove(str(root / "src"))
        for name in [m for m in sys.modules if m == "plc" or m.startswith("plc.")]:
            del sys.modules[name]
    return files


def _round_trip(program: Program, mapping: TagMap, tmp_path: Path) -> dict[str, str]:
    bundle = pyrung_to_ladder(program, mapping)
    csv_dir = tmp_path / "csv"
    bundle.write(csv_dir)
    files = ladder_to_pyrung_project(csv_dir, workspace_kind="temporary")
    return _import_workspace(files, tmp_path)


@pytest.mark.parametrize("value", [0xFFC0, 0xC000, 0x16C2, 0xD0D0, 0xDF00, 0x0CD0])
def test_hex_constant_is_not_an_address(value, tmp_path):
    Src = Word("Src")
    Dst = Word("Dst")
    with Program() as logic:
        with rung(Src == value):
            copy(value, Dst)
    mapping = TagMap({Src: dh[1], Dst: dh[2]}, include_system=False)
    files = _round_trip(logic, mapping, tmp_path)
    tags_py = files["src/plc/tags.py"]
    assert "c[0]" not in tags_py
    assert f"{value:04X}".lstrip("0") not in {
        line.split("=")[0].strip() for line in tags_py.splitlines()
    }


@pytest.mark.parametrize("value", [150.0, 1.5e20, 2.5e-5])
def test_real_constant_is_not_an_address(value, tmp_path):
    Src = Real("Src")
    Dst = Real("Dst")
    with Program() as logic:
        with rung(Src > value):
            copy(value, Dst)
    mapping = TagMap({Src: df[1], Dst: df[2]}, include_system=False)
    files = _round_trip(logic, mapping, tmp_path)
    assert "c[0]" not in files["src/plc/tags.py"]


@pytest.mark.parametrize("text", ["C1", "X1", "DS1", "T2", "CTD4", "TXT3", "DH10"])
def test_text_constant_is_not_an_address(text, tmp_path):
    Lamp = Bool("Lamp")
    Words = Block("Words", TagType.CHAR, 1, len(text))
    with Program() as logic:
        with rung(Lamp):
            copy(text, Words.select(1, len(text)))
    mapping = TagMap({Lamp: x[1], Words: txt.select(1, len(text))}, include_system=False)
    files = _round_trip(logic, mapping, tmp_path)
    assert f'"{text}"' in files["src/plc/main.py"]
    tags_py = files["src/plc/tags.py"]
    assert not any(line.startswith(f"{text} =") for line in tags_py.splitlines())
