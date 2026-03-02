"""Tests for Click ladder CSV export (`TagMap.to_ladder`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyrung.click import LadderExportError, TagMap, c, ct, ctd, ds, txt, x, y
from pyrung.core import Block, Bool, Dint, Int, Program, Rung, TagType, any_of
from pyrung.core.program import (
    branch,
    calc,
    call,
    copy,
    count_up,
    forloop,
    latch,
    out,
    pack_text,
    reset,
    return_early,
    search,
    subroutine,
)


def _header() -> tuple[str, ...]:
    return (
        "marker",
        *tuple([chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]),
        "AF",
    )


def _row(marker: str, prefix: list[str], af: str) -> tuple[str, ...]:
    cells = list(prefix)
    cells.extend(["-"] * (31 - len(cells)))
    return tuple([marker, *cells, af])


def test_header_and_width_invariants():
    A = Bool("A")
    B = Bool("B")

    with Program() as logic:
        with Rung(A):
            out(B)

    mapping = TagMap({A: x[1], B: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[0] == _header()
    assert all(len(row) == 33 for row in bundle.main_rows)


def test_and_example_golden():
    A = Bool("A")
    B = Bool("B")
    Y = Bool("Y")

    with Program() as logic:
        with Rung(A, B):
            out(Y)

    mapping = TagMap({A: x[1], B: x[2], Y: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001", "X002"], "out(Y001,0)"),
    )


def test_or_expansion_with_trailing_and_golden():
    A = Bool("A")
    B = Bool("B")
    Ready = Bool("Ready")
    Y = Bool("Y")

    with Program() as logic:
        with Rung(any_of(A, B), Ready):
            out(Y)

    mapping = TagMap({A: x[1], B: x[2], Ready: c[1], Y: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001", "T", "C1"], "out(Y001,0)"),
        _row("", ["X002", "-"], ""),
    )


def test_branch_row_is_continuation_after_parent_conditions():
    A = Bool("A")
    Mode = Bool("Mode")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")

    with Program() as logic:
        with Rung(A):
            out(Y1)
            with branch(Mode):
                out(Y2)

    mapping = TagMap({A: x[1], Mode: x[2], Y1: y[1], Y2: y[2]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001", "T"], "out(Y001,0)"),
        _row("", ["", "-", "X002"], "out(Y002,0)"),
    )


def test_multiple_branches_stack_vertical_markers():
    A = Bool("A")
    Mode1 = Bool("Mode1")
    Mode2 = Bool("Mode2")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")
    Y3 = Bool("Y3")

    with Program() as logic:
        with Rung(A):
            out(Y1)
            with branch(Mode1):
                out(Y2)
            with branch(Mode2):
                out(Y3)

    mapping = TagMap(
        {A: x[1], Mode1: x[2], Mode2: x[3], Y1: y[1], Y2: y[2], Y3: y[3]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[1][2] == "T"
    assert bundle.main_rows[2][2] == "+"
    assert bundle.main_rows[3][2] == "-"
    assert bundle.main_rows[2][3] == "X002"
    assert bundle.main_rows[3][3] == "X003"
    assert bundle.main_rows[1][-1] == "out(Y001,0)"
    assert bundle.main_rows[2][-1] == "out(Y002,0)"
    assert bundle.main_rows[3][-1] == "out(Y003,0)"


def test_parent_instruction_after_branch_stays_on_parent_path():
    A = Bool("A")
    Mode = Bool("Mode")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")
    Y3 = Bool("Y3")

    with Program() as logic:
        with Rung(A):
            out(Y1)
            with branch(Mode):
                out(Y2)
            out(Y3)

    mapping = TagMap(
        {A: x[1], Mode: x[2], Y1: y[1], Y2: y[2], Y3: y[3]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001", "T"], "out(Y001,0)"),
        _row("", ["", "+", "X002"], "out(Y002,0)"),
        _row("", ["", "-"], "out(Y003,0)"),
    )


def test_multiple_instruction_rows_share_powered_path():
    A = Bool("A")
    B = Bool("B")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")
    Y3 = Bool("Y3")

    with Program() as logic:
        with Rung(A, B):
            out(Y1)
            latch(Y2)
            reset(Y3)

    mapping = TagMap(
        {
            A: x[1],
            B: x[2],
            Y1: y[1],
            Y2: y[2],
            Y3: y[3],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001", "X002", "T"], "out(Y001,0)"),
        _row("", ["", "", "+"], "latch(Y002)"),
        _row("", ["", "", "-"], "reset(Y003)"),
    )


def test_vertical_wire_stack_for_three_or_branches():
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    Ready = Bool("Ready")
    Y = Bool("Y")

    with Program() as logic:
        with Rung(any_of(A, B, C), Ready):
            out(Y)

    mapping = TagMap(
        {A: x[1], B: x[2], C: x[3], Ready: c[1], Y: y[1]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[1][2] == "T"
    assert bundle.main_rows[2][2] == "+"
    assert bundle.main_rows[3][2] == "-"
    assert bundle.main_rows[2][3] == "-"
    assert bundle.main_rows[3][3] == "-"
    assert bundle.main_rows[1][-1] == "out(Y001,0)"
    assert bundle.main_rows[2][-1] == ""
    assert bundle.main_rows[3][-1] == ""


def test_builder_pin_rows_are_independent_continuations():
    Enable = Bool("Enable")
    Down = Bool("Down")
    ResetCond = Bool("ResetCond")
    Done = Bool("Done")
    Acc = Dint("Acc")

    with Program() as logic:
        with Rung(Enable):
            count_up(Done, Acc, preset=5).down(Down).reset(ResetCond)

    mapping = TagMap(
        {
            Enable: x[1],
            Down: x[2],
            ResetCond: x[3],
            Done: ct[1],
            Acc: ctd[1],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[1][-1] == "count_up(CT1,CTD1,5)"
    assert bundle.main_rows[2][-1] == ".down()"
    assert bundle.main_rows[3][-1] == ".reset()"
    assert bundle.main_rows[2][1] == "X002"
    assert bundle.main_rows[3][1] == "X003"


def test_forloop_lowers_to_for_body_and_next_rows():
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            with forloop(3, oneshot=True):
                out(Light)

    mapping = TagMap({Enable: x[1], Light: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[1][-1] == "for(3,1)"
    assert bundle.main_rows[2][0] == "R"
    assert bundle.main_rows[2][-1] == "out(Y001,0)"
    assert bundle.main_rows[3][0] == "R"
    assert bundle.main_rows[3][-1] == "next()"


def test_subroutine_files_sorted_slugged_and_return_tailed(tmp_path: Path):
    Start = Bool("Start")
    SubOut = Bool("SubOut")

    with Program() as logic:
        with Rung(Start):
            call("beta-two")
            call("alpha")

        with subroutine("alpha"):
            with Rung():
                out(SubOut)

        with subroutine("beta-two"):
            with Rung():
                return_early()

    mapping = TagMap({Start: x[1], SubOut: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert [name for name, _ in bundle.subroutine_rows] == ["alpha", "beta-two"]
    alpha_rows = dict(bundle.subroutine_rows)["alpha"]
    beta_rows = dict(bundle.subroutine_rows)["beta-two"]
    assert alpha_rows[-1][-1] == "return()"
    assert sum(1 for row in beta_rows if row[-1] == "return()") == 1

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)
    assert (out_dir / "main.csv").exists()
    assert (out_dir / "sub_alpha.csv").exists()
    assert (out_dir / "sub_beta_two.csv").exists()


def test_tokens_include_explicit_defaults_and_oneshot():
    Enable = Bool("Enable")
    Light = Bool("Light")
    Dest1 = Int("Dest1")
    Dest2 = Int("Dest2")
    Result = Int("Result")
    Found = Bool("Found")
    Data = Block("Data", TagType.INT, 1, 2)
    Chars = Block("Chars", TagType.CHAR, 1, 2)
    PackDest = Int("PackDest")

    with Program() as logic:
        with Rung(Enable):
            out(Light)
        with Rung(Enable):
            copy(5, Dest1)
        with Rung(Enable):
            calc(Dest1 + 1, Dest2)
        with Rung(Enable):
            search("==", 1, Data.select(1, 2), Result, Found)
        with Rung(Enable):
            pack_text(Chars.select(1, 2), PackDest)

    mapping = TagMap(
        {
            Enable: x[1],
            Light: y[1],
            Dest1: ds[1],
            Dest2: ds[2],
            Result: ds[3],
            Found: c[1],
            Data: ds.select(10, 11),
            Chars: txt.select(1, 2),
            PackDest: ds[4],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert "out(Y001,0)" in tokens
    assert any(token.startswith("copy(") and token.endswith(",0)") for token in tokens)
    assert any(",decimal,0)" in token for token in tokens if token.startswith("calc("))
    assert any(token.startswith("search(") and token.endswith(",0,0)") for token in tokens)
    assert any(token.startswith("pack_text(") and token.endswith(",0,0)") for token in tokens)


def test_precheck_and_issue_payload():
    A = Int("A")
    Dest = Int("Dest")

    with Program() as logic:
        with Rung():
            copy(A * 2, Dest)

    mapping = TagMap({A: ds[1], Dest: ds[2]}, include_system=False)

    with pytest.raises(LadderExportError) as exc_info:
        mapping.to_ladder(logic)

    issue = exc_info.value.issues[0]
    assert "main.rung[0]" in str(issue["path"])
    assert issue["source_file"] is None


def test_nested_subroutine_call_issue_includes_source_location():
    A = Bool("A")
    SubOut = Bool("SubOut")

    with Program() as logic:
        with Rung(A):
            call("outer")

        with subroutine("outer"):
            with Rung():
                call("inner")

        with subroutine("inner"):
            with Rung():
                out(SubOut)

    mapping = TagMap({A: x[1], SubOut: y[1]}, include_system=False)

    with pytest.raises(LadderExportError) as exc_info:
        mapping.to_ladder(logic)

    issue = exc_info.value.issues[0]
    assert "subroutine[outer]" in str(issue["path"])
    assert issue["source_file"] is not None
    assert issue["source_line"] is not None
