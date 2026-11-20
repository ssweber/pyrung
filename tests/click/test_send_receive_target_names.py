"""A quote, a comma or a bracket in a Modbus target name survives export and import."""

from __future__ import annotations

import csv

import pytest
from laddercodec import Send

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung_project, pyrung_to_ladder
from pyrung.core.instruction.send_receive import ModbusRtuTarget, ModbusTcpTarget, send
from pyrung.core.program import Program, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

NAMES = ['a"b', 'a""b', '"', "a,b", 'a")b', "a(b", 'a"b,c""d']


def _exported(name: str, rtu: bool, tmp_path):
    target = ModbusRtuTarget(name) if rtu else ModbusTcpTarget(name, "1.2.3.4")
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            send(target=target, remote_start="DS1", source=ds[1])
    pyrung_to_ladder(prog, TagMap([], include_system=False)).write(tmp_path)
    (line,) = [
        ln
        for ln in (tmp_path / "main.csv").read_text(encoding="utf-8").splitlines()
        if "send(" in ln
    ]
    return line


@pytest.mark.parametrize("rtu", [False, True])
@pytest.mark.parametrize("name", NAMES)
def test_a_target_name_reads_back_as_written(name, rtu, tmp_path):
    line = _exported(name, rtu, tmp_path)
    token = next(csv_cell for csv_cell in _cells(line) if csv_cell.startswith("send("))
    assert Send.from_csv_token(token).target.name == name
    files = ladder_to_pyrung_project(tmp_path, workspace_kind="temporary")
    imported = files["src/plc/main.py"]
    assert f"name={_python_text(name)}" in imported


def _cells(line: str) -> list[str]:
    return next(csv.reader([line]))


def _python_text(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
