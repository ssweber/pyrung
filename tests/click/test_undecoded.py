"""laddercodec's undecoded marker: import, the stop it makes, the finding and the refusals."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest
from laddercodec import Undecoded, read_csv, write_csv
from laddercodec.csv.contract import CSV_HEADER
from laddercodec.undecoded import marker_rung

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, undecoded
from pyrung.click.codegen.api import ladder_to_pyrung, ladder_to_pyrung_project
from pyrung.click.ladder.types import LadderExportError
from pyrung.click.undecoded import UndecodedInstruction, undecoded_notice
from pyrung.core import (
    PLC,
    Bool,
    CompiledPLC,
    Program,
    branch,
    call,
    forloop,
    out,
    rung,
    subroutine,
)
from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message
from tests.core.kernel_step import step_compiled_kernel

_MAIN_ERROR = "bad bytes at offset 12"
_SUB_ERROR = "unknown instruction (40 bytes)"


def _rows(af_by_contact: list[tuple[str, str]]) -> str:
    lines = [",".join(CSV_HEADER)]
    for contact, af in af_by_contact:
        lines.append(",".join(["R", contact, *["-"] * 30, af]))
    return "\n".join(lines) + "\n"


def _rungs(tmp_path: Path, af_by_contact: list[tuple[str, str]]) -> list[Any]:
    seed = tmp_path / "_seed.csv"
    seed.write_text(_rows(af_by_contact), encoding="utf-8")
    rungs = read_csv(seed)
    seed.unlink()
    return rungs


def _write_project(tmp_path: Path) -> Path:
    """Write a CLICK export whose main and subroutine each hold a marker that laddercodec made.

    Main runs X001 -> Y001, the marker, X002 -> Y002 and a call of Sub. Sub runs
    X004 -> Y003, the marker and X005 -> Y004.
    """
    source = tmp_path / "ladder"
    (source / "subroutines").mkdir(parents=True)
    main = _rungs(tmp_path, [("X001", "out(Y001)"), ("X002", "out(Y002)"), ("X003", 'call("Sub")')])
    marker = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "end"), "Old comment")
    write_csv(source / "main.csv", [main[0], marker, main[1], main[2]])
    sub = _rungs(tmp_path, [("X004", "out(Y003)"), ("X005", "out(Y004)")])
    marker = marker_rung(Undecoded(_SUB_ERROR, 1, 2, 31, "return"))
    write_csv(source / "subroutines" / "sub.csv", [sub[0], marker, sub[1]])
    return source


def _load(code: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile(code, "<generated>", "exec"), namespace)
    return namespace


@pytest.fixture
def imported(tmp_path: Path) -> tuple[str, dict[str, Any]]:
    code = ladder_to_pyrung(_write_project(tmp_path))
    return code, _load(code)


def test_the_import_writes_each_marker_as_an_undecoded_call(imported) -> None:
    code, _ = imported
    assert "from pyrung.click import ClickBlocks, TagMap, undecoded" in code
    assert (
        f'undecoded("{_MAIN_ERROR}", rung=1, row=0, column=31, stop="end")' in code
        and f'undecoded("{_SUB_ERROR}", rung=1, row=2, column=31, stop="return")' in code
    )


def test_the_markers_comment_comes_through_as_a_rung_comment(imported) -> None:
    _, namespace = imported
    logic = namespace["logic"]
    assert logic.rungs[1].comment == f"UNDECODED: rung 1, row 0: {_MAIN_ERROR}\nOld comment"
    assert logic.subroutines["Sub"][1].comment == f"UNDECODED: rung 1, row 2: {_SUB_ERROR}"


def test_the_program_leaves_out_the_rungs_after_a_marker(imported) -> None:
    _, namespace = imported
    logic = namespace["logic"]
    assert len(logic.rungs) == 2 and len(logic.subroutines["Sub"]) == 2
    main_marker = logic.rungs[1]._instructions[0]
    assert isinstance(main_marker, UndecodedInstruction)
    assert (main_marker.error, main_marker.rung, main_marker.row, main_marker.stop) == (
        _MAIN_ERROR,
        1,
        0,
        "end",
    )


def test_the_project_import_runs_a_marker_in_a_subroutine_file(tmp_path: Path) -> None:
    out_dir = tmp_path / "project"
    files = ladder_to_pyrung_project(_write_project(tmp_path), output_dir=out_dir)
    assert "from pyrung.click import undecoded" in files["src/plc/subroutines/_sub_sub.py"]
    sys.path.insert(0, str(out_dir / "src"))
    try:
        from plc.main import logic
    finally:
        sys.path.remove(str(out_dir / "src"))
        for name in [name for name in sys.modules if name == "plc" or name.startswith("plc.")]:
            del sys.modules[name]
    assert len(logic.rungs) == 2 and len(logic.subroutines["Sub"]) == 2


def test_the_import_refuses_a_marker_inside_a_for_loop(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, [("X001", "for(3)"), ("-", "next()")])
    source = tmp_path / "loop"
    source.mkdir()
    marker = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "end"))
    write_csv(source / "main.csv", [rungs[0], marker, rungs[1]])
    with pytest.raises(ValueError, match="cannot run its undecoded marker inside a For loop"):
        ladder_to_pyrung(source)


# ---- The stop -----------------------------------------------------------------------------


_ALL_ON = {name: True for name in ("X001", "X002", "X003", "X004", "X005")}


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_no_rung_after_a_marker_runs(imported, engine) -> None:
    _, namespace = imported
    runner = engine(namespace["logic"], dt=0.010)
    runner.patch(_ALL_ON)
    runner.step()
    runner.run_for(0.5)
    tags = runner.current_state.tags
    # Main ends at its marker, so Y002 stays off and Sub is never called.
    assert tags["Y001"] is True
    assert not any(tags.get(name, False) for name in ("Y002", "Y003", "Y004"))


def _called_sub() -> Program:
    """Main calls Sub. Sub writes A, returns at its marker and never writes B."""
    a, b, go, after = Bool("A"), Bool("B"), Bool("Go"), Bool("After")
    with Program(strict=False) as logic:
        with rung(go):
            call("Sub")
        with rung(go):
            out(after)
        with subroutine("Sub"):
            with rung():
                out(a)
            with rung():
                undecoded(_SUB_ERROR, rung=1, stop="return")
            with rung():
                out(b)
    return logic


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_a_subroutine_returns_at_its_marker(engine) -> None:
    runner = engine(_called_sub(), dt=0.010)
    runner.patch({"Go": True})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["A"], tags.get("B", False), tags["After"]) == (True, False, True)


@pytest.mark.parametrize("blockless", [False, True])
def test_the_kernel_stops_at_the_marker_too(imported, blockless) -> None:
    _, namespace = imported
    compiled = compile_kernel(namespace["logic"], blockless=blockless)
    kernel = compiled.create_kernel()
    kernel.tags.update(_ALL_ON)
    step_compiled_kernel(compiled, kernel, dt=0.010)
    assert kernel.tags["Y001"] is True
    assert not kernel.tags.get("Y002", False)

    compiled = compile_kernel(_called_sub(), blockless=blockless)
    kernel = compiled.create_kernel()
    kernel.tags["Go"] = True
    step_compiled_kernel(compiled, kernel, dt=0.010)
    assert (kernel.tags["A"], kernel.tags.get("B", False), kernel.tags["After"]) == (
        True,
        False,
        True,
    )


# ---- Where the DSL takes a marker -----------------------------------------------------------


def test_a_marker_must_be_alone_in_a_rung_of_its_own() -> None:
    go, light = Bool("Go"), Bool("Light")
    alone = "must be the only thing in a rung with no conditions"
    with pytest.raises(RuntimeError, match=alone), Program(strict=False):
        with rung(go):
            undecoded(_MAIN_ERROR, rung=0, stop="end")
    with pytest.raises(RuntimeError, match=alone), Program(strict=False):
        with rung():
            out(light)
            undecoded(_MAIN_ERROR, rung=0, stop="end")
    with pytest.raises(RuntimeError, match=alone), Program(strict=False):
        with rung(), branch(go):
            undecoded(_MAIN_ERROR, rung=0, stop="end")
    with pytest.raises(RuntimeError, match=alone), Program(strict=False):
        with rung(), forloop(2):
            undecoded(_MAIN_ERROR, rung=0, stop="end")


def test_a_markers_stop_must_match_its_scope() -> None:
    with pytest.raises(RuntimeError, match="Pass stop='end' there"), Program(strict=False):
        with rung():
            undecoded(_MAIN_ERROR, rung=0, stop="return")
    with pytest.raises(RuntimeError, match="Pass stop='return' there"), Program(strict=False):
        with subroutine("Sub"), rung():
            undecoded(_MAIN_ERROR, rung=0, stop="end")


def test_laddercodec_checks_the_markers_fields() -> None:
    with pytest.raises(ValueError), Program(strict=False), rung():
        undecoded("", rung=0, stop="end")


# ---- The finding and the refusals -----------------------------------------------------------


def _main_marker() -> tuple[Program, TagMap]:
    go, light = Bool("Go"), Bool("Light")
    with Program(strict=False) as logic:
        with rung(go):
            out(light)
        with rung():
            undecoded(_MAIN_ERROR, rung=1, row=0, column=31, stop="end")
    blocks = ClickBlocks()
    return logic, TagMap({go: blocks.x[1], light: blocks.y[1]})


@pytest.mark.parametrize(("mode", "severity"), [("warn", "warning"), ("strict", "error")])
def test_click_validation_reports_the_marker(mode, severity) -> None:
    logic, mapping = _main_marker()
    report = mapping.validate(logic, mode=mode)
    findings = [
        finding
        for finding in (*report.errors, *report.warnings, *report.hints)
        if finding.code == "undecoded-rung"
    ]
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == severity
    assert f"could not decode: {_MAIN_ERROR}." in finding.message
    assert "Each scan ends at this rung" in finding.message
    assert "main.rung[2]" in finding.location
    assert finding.suggestion is not None and "CLICK cannot take undecoded()" in finding.suggestion


@pytest.mark.parametrize(
    "options", [{}, {"allow": ("undecoded-rung",)}, {"validate": False}], ids=str
)
def test_the_export_to_click_refuses_a_marker(options) -> None:
    logic, mapping = _main_marker()
    with pytest.raises(LadderExportError, match="CLICK cannot take undecoded"):
        pyrung_to_ladder(logic, mapping, **options)


def test_circuitpython_refuses_a_marker() -> None:
    hardware = P1AM()
    hardware.slot(1, "P1-08SIM")
    logic, _ = _main_marker()
    with pytest.raises(NotImplementedError, match=r"A code.py cannot run undecoded\(\)"):
        generate_circuitpy(logic, hardware, target_scan_ms=10.0)


def test_the_notice_names_each_marker(imported) -> None:
    _, namespace = imported
    assert undecoded_notice(namespace["logic"]).splitlines() == [
        f"undecoded() at main rung 2 stands in for a CLICK rung that laddercodec could not "
        f"decode: {_MAIN_ERROR}. Each scan ends at this rung, so no rung after it in main runs.",
        f"undecoded() at subroutine 'Sub' rung 2 stands in for a CLICK rung that laddercodec "
        f"could not decode: {_SUB_ERROR}. The subroutine returns at this rung, so no rung "
        "after it runs.",
    ]


# ---- The debugger ---------------------------------------------------------------------------


def _drain(stream: io.BytesIO) -> list[dict[str, Any]]:
    reader = io.BytesIO(stream.getvalue())
    messages = []
    while (message := read_message(reader)) is not None:
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _request(adapter: DAPAdapter, stream: io.BytesIO, seq: int, command: str, **arguments):
    adapter.handle_request(
        {"seq": seq, "type": "request", "command": command, "arguments": arguments}
    )
    return _drain(stream)


def _outputs(messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["body"]["output"]
        for message in messages
        if message.get("type") == "event" and message.get("event") == "output"
    ]


def test_the_debugger_says_where_the_scan_stops_at_launch_and_reload(
    imported, tmp_path: Path
) -> None:
    code, namespace = imported
    script = tmp_path / "logic.py"
    script.write_text(code + "\nrunner = PLC(logic, dt=0.010)\n", encoding="utf-8")
    script.write_text(
        "from pyrung import PLC\n" + script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=stream)
    messages = _request(adapter, stream, 1, "launch", program=str(script))
    adapter._drain_internal_events()
    messages += _drain(stream)
    notice = undecoded_notice(namespace["logic"])
    assert f"{notice}\n" in _outputs(messages)

    _request(adapter, stream, 2, "configurationDone")
    messages = _request(adapter, stream, 3, "evaluate", expression="reload", context="repl")
    response = next(message for message in messages if message.get("type") == "response")
    assert response["success"] is True
    assert notice in response["body"]["result"]
