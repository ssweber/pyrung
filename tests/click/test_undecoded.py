"""laddercodec's undecoded marker: import, the stop it makes, the finding and the refusals."""

from __future__ import annotations

import io
import re
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from laddercodec import Undecoded, read_csv, write_csv
from laddercodec.csv.contract import CSV_HEADER
from laddercodec.undecoded import marker_rung

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import (
    ClickBlocks,
    TagMap,
    pyrung_to_ladder,
    undecoded,
    undecoded_notice,
    undecoded_rungs,
)
from pyrung.click.codegen.api import ladder_to_pyrung, ladder_to_pyrung_project
from pyrung.click.ladder.types import LadderExportError
from pyrung.click.undecoded import UndecodedInstruction
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


def _write_main(tmp_path: Path, rungs: list[Any]) -> Path:
    source = tmp_path / "ladder"
    source.mkdir()
    write_csv(source / "main.csv", rungs)
    return source


def test_the_import_refuses_a_subroutine_marker_that_says_end(tmp_path: Path) -> None:
    source = _write_project(tmp_path)
    sub = _rungs(tmp_path, [("X004", "out(Y003)")])
    # write_csv() writes stop=end unless it is told otherwise.
    marker = marker_rung(Undecoded(_SUB_ERROR, 1, 2, 31, "end"))
    write_csv(source / "subroutines" / "sub.csv", [sub[0], marker])
    expected = 'Subroutine "Sub" (sub.csv), rung 2: the undecoded marker says stop=end'
    with pytest.raises(ValueError, match=re.escape(expected)) as raised:
        ladder_to_pyrung(source)
    assert 'write_csv(..., stop="return")' in str(raised.value)


def test_the_import_refuses_a_main_marker_that_says_return(tmp_path: Path) -> None:
    main = _rungs(tmp_path, [("X001", "out(Y001)")])
    marker = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "return"))
    source = _write_main(tmp_path, [main[0], marker])
    expected = r"Main program \(main.csv\), rung 2: the undecoded marker says stop=return"
    with pytest.raises(ValueError, match=expected):
        ladder_to_pyrung(source)


def _lone_next_code(tmp_path: Path, main_afs: list[tuple[str, str]], marker_at: int) -> str:
    """Import a main.csv whose rung *marker_at* is a marker, and the rest follow *main_afs*."""
    rungs = _rungs(tmp_path, main_afs)
    marker = marker_rung(Undecoded(_MAIN_ERROR, marker_at, 0, 31, "end"))
    source = _write_main(tmp_path, [*rungs[:marker_at], marker, *rungs[marker_at:]])
    return ladder_to_pyrung(source)


_BODY_AND_NEXT = [("X001", "out(Y001)"), ("X002", "out(Y002)"), ("-", "next()")]

_PLACEHOLDER_COMMENT = (
    "CLICK rung 4 is a next() with no for() above it. "
    "Remove this empty rung when you write the loop in place of undecoded()."
)

_ONE_NEXT_NOTICE = (
    f"laddercodec could not decode CLICK rung 2 of the main program: {_MAIN_ERROR}. "
    "undecoded() stands in for that rung at main rung 2 in pyrung. "
    "CLICK rung 4 has a next() with no for() above it. "
    "CLICK rung 2 may have held that for(). "
    "Each scan ends at this rung, so no rung after it in main runs."
)


def test_the_import_takes_a_next_whose_for_became_a_marker(tmp_path: Path) -> None:
    code = _lone_next_code(tmp_path, _BODY_AND_NEXT, 1)
    namespace = _load(code)
    logic = namespace["logic"]
    assert 'stop="end", lone_next=[3])' in code
    assert (f"    comment({_PLACEHOLDER_COMMENT!r})\n    with rung():\n        pass\n") in code
    # Program leaves out the rungs after the marker, the placeholder included.
    assert len(logic.rungs) == 2
    assert undecoded_notice(logic) == _ONE_NEXT_NOTICE


def test_a_lone_nexts_own_comment_stays_in_the_placeholder(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, _BODY_AND_NEXT)
    rungs[2] = replace(rungs[2], comment="Loop end")
    marker = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "end"))
    code = ladder_to_pyrung(_write_main(tmp_path, [rungs[0], marker, rungs[1], rungs[2]]))
    assert f'comment("""\\\n        {_PLACEHOLDER_COMMENT}\n        Loop end""")' in code


def test_the_import_takes_a_lone_next_in_a_subroutine(tmp_path: Path) -> None:
    source = _write_project(tmp_path)
    sub = _rungs(tmp_path, [("X004", "out(Y003)"), ("X005", "out(Y004)"), ("-", "next()")])
    marker = marker_rung(Undecoded(_SUB_ERROR, 1, 2, 31, "return"))
    write_csv(source / "subroutines" / "sub.csv", [sub[0], marker, sub[1], sub[2]])
    logic = _load(ladder_to_pyrung(source))["logic"]
    assert len(logic.subroutines["Sub"]) == 2
    notice = undecoded_notice(logic).splitlines()[1]
    assert (
        "CLICK rung 4 has a next() with no for() above it. CLICK rung 2 may have held that for()."
    ) in notice


def test_the_import_takes_two_lone_nexts_after_one_marker(tmp_path: Path) -> None:
    afs = [*_BODY_AND_NEXT, ("X003", "out(Y003)"), ("-", "next()")]
    code = _lone_next_code(tmp_path, afs, 1)
    logic = _load(code)["logic"]
    assert code.splitlines().count("        pass") == 2
    assert "lone_next=[3, 5]" in code
    assert (
        "CLICK rungs 4 and 6 each have a next() with no for() above it. "
        "Each for() may have been in CLICK rung 2 or in a later rung that laddercodec could not "
        "decode."
    ) in undecoded_notice(logic)


def test_the_import_takes_nested_loops_whose_outer_for_is_the_marker(tmp_path: Path) -> None:
    afs = [
        ("X001", "out(Y001)"),
        ("-", "for(2)"),
        ("X002", "out(Y002)"),
        ("-", "next()"),
        ("-", "next()"),
    ]
    code = _lone_next_code(tmp_path, afs, 1)
    logic = _load(code)["logic"]
    # The inner loop is whole, so only the outer next() lacks a for().
    assert "lone_next=[5]" in code
    assert "forloop" in code and code.splitlines().count("        pass") == 1
    assert len(logic.rungs) == 2
    assert "CLICK rung 6 has a next() with no for() above it." in undecoded_notice(logic)


def test_the_finding_says_where_the_for_may_have_been(tmp_path: Path) -> None:
    logic = _load(_lone_next_code(tmp_path, _BODY_AND_NEXT, 1))["logic"]
    mapping = TagMap({}, include_system=False)
    report = mapping.validate(logic, mode="warn")
    everything = (*report.errors, *report.warnings, *report.hints)
    findings = [f for f in everything if f.code == "undecoded-rung"]
    assert len(findings) == 1
    assert (
        "CLICK rung 4 has a next() with no for() above it. CLICK rung 2 may have held that for()."
    ) in findings[0].message


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_a_scan_stops_at_a_marker_that_stands_above_a_lone_next(tmp_path: Path, engine) -> None:
    logic = _load(_lone_next_code(tmp_path, _BODY_AND_NEXT, 1))["logic"]
    runner = engine(logic, dt=0.010)
    runner.patch(_ALL_ON)
    runner.step()
    runner.run_for(0.2)
    tags = runner.current_state.tags
    assert tags["Y001"] is True
    assert not tags.get("Y002", False)


def test_the_import_names_only_the_first_of_two_markers(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, [*_BODY_AND_NEXT, ("X003", "out(Y003)"), ("-", "next()")])
    first = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "end"))
    second = marker_rung(Undecoded(_SUB_ERROR, 4, 0, 31, "end"))
    source = _write_main(
        tmp_path, [rungs[0], first, rungs[1], rungs[2], second, rungs[3], rungs[4]]
    )
    code = ladder_to_pyrung(source)
    logic = _load(code)["logic"]
    assert "lone_next=[3, 6]" in code
    assert len(logic.rungs) == 2
    assert (
        "CLICK rungs 4 and 7 each have a next() with no for() above it. "
        "Each for() may have been in CLICK rung 2 or in a later rung that laddercodec could not "
        "decode."
    ) in undecoded_notice(logic)


def test_the_project_import_takes_a_lone_next(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, _BODY_AND_NEXT)
    marker = marker_rung(Undecoded(_MAIN_ERROR, 1, 0, 31, "end"))
    source = _write_main(tmp_path, [rungs[0], marker, rungs[1], rungs[2]])
    files = ladder_to_pyrung_project(source, output_dir=tmp_path / "project")
    text = files["src/plc/main.py"]
    assert "lone_next=[3]" in text
    assert _PLACEHOLDER_COMMENT in text


@pytest.mark.parametrize("bad", [["3"], [-1], [1.5], [True], 3, "3", {3: 1}])
def test_undecoded_rejects_a_lone_next_that_is_not_rung_numbers(bad) -> None:
    with pytest.raises(ValueError, match="needs lone_next as a list of rung numbers"):
        with Program(strict=False):
            with rung():
                undecoded(_MAIN_ERROR, rung=0, stop="end", lone_next=bad)


def test_the_import_refuses_a_marker_that_replaced_the_next(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, [("X001", "for(3)"), ("X002", "out(Y002)")])
    marker = marker_rung(Undecoded(_MAIN_ERROR, 2, 0, 31, "end"))
    source = _write_main(tmp_path, [rungs[0], rungs[1], marker])
    with pytest.raises(ValueError, match="cannot run its undecoded marker inside a For loop"):
        ladder_to_pyrung(source)


def test_the_import_refuses_a_next_with_no_for(tmp_path: Path) -> None:
    rungs = _rungs(tmp_path, [("X001", "out(Y001)"), ("-", "next()")])
    source = _write_main(tmp_path, rungs)
    expected = (
        r"Main program \(main.csv\), rung 2: this next\(\) has no for\(\) above it\. "
        r"Either the program has no for\(\) rung, or laddercodec could not read the rung that "
        r"held it\. Add the for\(\) rung in CLICK, or remove the next\(\), then read"
    )
    with pytest.raises(ValueError, match=expected):
        ladder_to_pyrung(source)


# ---- A marker that stands for every rung to the end -------------------------------------------


def _to_the_end_project(tmp_path: Path, *, row: int | None = 0) -> Path:
    """Write the project of ``_write_project``, with ``to_the_end=1`` on both markers.

    The token text is written by hand, because the installed laddercodec has no such field.
    """
    source = _write_project(tmp_path)
    if row is None:
        for name in ("main.csv", "subroutines/sub.csv"):
            path = source / name
            text = path.read_text(encoding="utf-8")
            path.write_text(re.sub(r",row=\d+,column=31", "", text), encoding="utf-8")
    for name, stop in (("main.csv", "end"), ("subroutines/sub.csv", "return")):
        path = source / name
        text = path.read_text(encoding="utf-8")
        assert f"stop={stop})" in text
        path.write_text(text.replace(f"stop={stop})", f"stop={stop},to_the_end=1)"), "utf-8")
    return source


@pytest.mark.parametrize("row", [0, None])
def test_the_import_takes_to_the_end_in_main_and_in_a_subroutine(tmp_path: Path, row) -> None:
    code = ladder_to_pyrung(_to_the_end_project(tmp_path, row=row))
    main_where = ", row=0, column=31" if row is not None else ""
    sub_where = ", row=2, column=31" if row is not None else ""
    assert f'undecoded("{_MAIN_ERROR}", rung=1{main_where}, stop="end", to_the_end=True)' in code
    assert f'undecoded("{_SUB_ERROR}", rung=1{sub_where}, stop="return", to_the_end=True)' in code
    logic = _load(code)["logic"]
    main_marker = logic.rungs[1]._instructions[0]
    sub_marker = logic.subroutines["Sub"][1]._instructions[0]
    assert (main_marker.to_the_end, sub_marker.to_the_end) == (True, True)


def test_a_marker_without_to_the_end_says_so(imported) -> None:
    code, namespace = imported
    assert "to_the_end" not in code
    assert namespace["logic"].rungs[1]._instructions[0].to_the_end is False


def test_the_notice_and_the_finding_say_the_marker_stands_for_every_rung_after_it(
    tmp_path: Path,
) -> None:
    logic = _load(ladder_to_pyrung(_to_the_end_project(tmp_path)))["logic"]
    assert undecoded_notice(logic).splitlines() == [
        f"laddercodec stopped reading at CLICK rung 2 of the main program: {_MAIN_ERROR}. "
        "undecoded() stands in at main rung 2 in pyrung for that rung and every rung after it. "
        "Each scan ends at this rung, so no rung after it in main runs.",
        f"laddercodec stopped reading at CLICK rung 2 of its subroutine: {_SUB_ERROR}. "
        "undecoded() stands in at subroutine 'Sub' rung 2 in pyrung for that rung and every rung "
        "after it. The subroutine returns at this rung, so no rung after it runs.",
    ]
    report = TagMap({}, include_system=False).validate(logic, mode="warn")
    findings = [f for f in (*report.errors, *report.warnings) if f.code == "undecoded-rung"]
    assert len(findings) == 2
    assert all("and every rung after it" in f.message for f in findings)
    assert "laddercodec stopped reading at CLICK rung 2 of the main program" in (
        findings[0].message
    )


@pytest.mark.parametrize("bad", ["0", "2", "true", "True", ""])
def test_the_import_refuses_a_bad_to_the_end(tmp_path: Path, bad: str) -> None:
    source = _write_project(tmp_path)
    path = source / "main.csv"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("stop=end)", f"stop=end,to_the_end={bad})"), encoding="utf-8")
    with pytest.raises(ValueError, match=r"laddercodec writes to_the_end=1 or leaves it out"):
        ladder_to_pyrung(source)


def test_a_marker_from_laddercodec_0_5_0_imports_as_before(imported) -> None:
    code, namespace = imported
    assert f'undecoded("{_MAIN_ERROR}", rung=1, row=0, column=31, stop="end")' in code
    assert undecoded_notice(namespace["logic"]).splitlines()[0] == (
        f"laddercodec could not decode CLICK rung 2 of the main program: {_MAIN_ERROR}. "
        "undecoded() stands in for that rung at main rung 2 in pyrung. Each scan ends at this "
        "rung, so no rung after it in main runs."
    )


def test_undecoded_rejects_a_to_the_end_that_is_not_a_bool() -> None:
    with pytest.raises(ValueError, match="needs to_the_end as True or False"):
        with Program(strict=False), rung():
            undecoded(_MAIN_ERROR, rung=0, stop="end", to_the_end=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_a_scan_stops_at_a_to_the_end_marker(tmp_path: Path, engine) -> None:
    logic = _load(ladder_to_pyrung(_to_the_end_project(tmp_path)))["logic"]
    runner = engine(logic, dt=0.010)
    runner.patch(_ALL_ON)
    runner.step()
    runner.run_for(0.2)
    tags = runner.current_state.tags
    assert tags["Y001"] is True
    assert not any(tags.get(name, False) for name in ("Y002", "Y003", "Y004"))


@pytest.mark.parametrize("engine", [PLC, CompiledPLC])
def test_a_subroutine_returns_at_a_to_the_end_marker(engine) -> None:
    a, b, go = Bool("A"), Bool("B"), Bool("Go")
    with Program(strict=False) as logic:
        with rung(go):
            call("Sub")
        with subroutine("Sub"):
            with rung():
                out(a)
            with rung():
                undecoded(_SUB_ERROR, rung=1, stop="return", to_the_end=True)
            with rung():
                out(b)
    runner = engine(logic, dt=0.010)
    runner.patch({"Go": True})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["A"], tags.get("B", False)) == (True, False)


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
            # laddercodec counts CLICK rungs from 0, so rung=4 is CLICK rung 5.
            undecoded(_MAIN_ERROR, rung=4, row=0, column=31, stop="end")
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
    assert finding.message.startswith(
        f"laddercodec could not decode CLICK rung 5 of the main program: {_MAIN_ERROR}. "
        "undecoded() stands in for that rung at main.rung[2]"
    )
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
    logic = namespace["logic"]
    assert [(sub, index) for sub, index, _ in undecoded_rungs(logic)] == [(None, 1), ("Sub", 1)]
    assert undecoded_notice(logic).splitlines() == [
        f"laddercodec could not decode CLICK rung 2 of the main program: {_MAIN_ERROR}. "
        "undecoded() stands in for that rung at main rung 2 in pyrung. Each scan ends at this "
        "rung, so no rung after it in main runs.",
        f"laddercodec could not decode CLICK rung 2 of its subroutine: {_SUB_ERROR}. "
        "undecoded() stands in for that rung at subroutine 'Sub' rung 2 in pyrung. The "
        "subroutine returns at this rung, so no rung after it runs.",
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


def test_undecoded_keeps_the_values_of_a_generator_lone_next() -> None:
    with Program(strict=False) as logic:
        with rung():
            undecoded(_MAIN_ERROR, rung=1, stop="end", lone_next=(n for n in [3, 5]))
    assert logic.rungs[0]._instructions[0].lone_next == (3, 5)
