"""Tests for Click ladder CSV export (`TagMap.to_ladder`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyrung.click import LadderExportError, TagMap, c, ct, ctd, dd, ds, receive, sc, send, t, td, txt, x, y
from pyrung.core import Block, Bool, Dint, Int, Program, Rung, TagType, any_of
from pyrung.core.program import (
    blockcopy,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    event_drum,
    fill,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_words,
    pack_text,
    reset,
    return_early,
    search,
    shift,
    subroutine,
    time_drum,
    unpack_to_bits,
    unpack_to_words,
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
    main_tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert [name for name, _ in bundle.subroutine_rows] == ["alpha", "beta-two"]
    assert main_tokens == ['call("beta-two")', 'call("alpha")']
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


def test_tokens_cover_remaining_instruction_families_and_pin_rows():
    Enable = Bool("Enable")

    SrcBlock = Block("SrcBlock", TagType.INT, 1, 3)
    DstBlock = Block("DstBlock", TagType.INT, 1, 3)
    Bits = Block("Bits", TagType.BOOL, 1, 32)
    Words = Block("Words", TagType.INT, 1, 2)
    DWord = Dint("DWord")

    TonDone = Bool("TonDone")
    TonAcc = Int("TonAcc")
    TonReset = Bool("TonReset")
    TofDone = Bool("TofDone")
    TofAcc = Int("TofAcc")

    CdDone = Bool("CdDone")
    CdAcc = Dint("CdAcc")
    CdReset = Bool("CdReset")

    ShiftClock = Bool("ShiftClock")
    ShiftReset = Bool("ShiftReset")

    DrumReset = Bool("DrumReset")
    DrumJump = Bool("DrumJump")
    DrumJog = Bool("DrumJog")
    DrumStep = Int("DrumStep")
    DrumAcc = Int("DrumAcc")
    DrumDone = Bool("DrumDone")
    DrumOut1 = Bool("DrumOut1")
    DrumOut2 = Bool("DrumOut2")
    Event1 = Bool("Event1")
    Event2 = Bool("Event2")

    SendSource = Int("SendSource")
    SendBusy = Bool("SendBusy")
    SendSuccess = Bool("SendSuccess")
    SendError = Bool("SendError")
    SendEx = Int("SendEx")
    RecvDest = Int("RecvDest")
    RecvBusy = Bool("RecvBusy")
    RecvSuccess = Bool("RecvSuccess")
    RecvError = Bool("RecvError")
    RecvEx = Int("RecvEx")

    with Program() as logic:
        with Rung(Enable):
            blockcopy(SrcBlock.select(1, 3), DstBlock.select(1, 3), oneshot=True)
        with Rung(Enable):
            fill(7, DstBlock.select(1, 3), oneshot=True)
        with Rung(Enable):
            pack_bits(Bits.select(1, 16), DWord, oneshot=True)
        with Rung(Enable):
            pack_words(Words.select(1, 2), DWord, oneshot=True)
        with Rung(Enable):
            unpack_to_bits(DWord, Bits.select(1, 32), oneshot=True)
        with Rung(Enable):
            unpack_to_words(DWord, Words.select(1, 2), oneshot=True)
        with Rung(Enable):
            on_delay(TonDone, TonAcc, preset=100).reset(TonReset)
        with Rung(Enable):
            off_delay(TofDone, TofAcc, preset=50)
        with Rung(Enable):
            count_down(CdDone, CdAcc, preset=9).reset(CdReset)
        with Rung(Enable):
            shift(Bits.select(1, 8)).clock(ShiftClock).reset(ShiftReset)
        with Rung(Enable):
            event_drum(
                outputs=[DrumOut1, DrumOut2],
                events=[Event1, Event2],
                pattern=[[1, 0], [0, 1]],
                current_step=DrumStep,
                completion_flag=DrumDone,
            ).reset(DrumReset).jump(DrumJump, step=DrumStep).jog(DrumJog)
        with Rung(Enable):
            time_drum(
                outputs=[DrumOut1, DrumOut2],
                presets=[100, 200],
                pattern=[[1, 0], [0, 1]],
                current_step=DrumStep,
                accumulator=DrumAcc,
                completion_flag=DrumDone,
            ).reset(DrumReset).jump(DrumJump, step=DrumStep).jog(DrumJog)
        with Rung(Enable):
            send(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                source=SendSource,
                sending=SendBusy,
                success=SendSuccess,
                error=SendError,
                exception_response=SendEx,
                device_id=3,
                count=1,
            )
        with Rung(Enable):
            receive(
                host="127.0.0.1",
                port=502,
                remote_start="DS2",
                dest=RecvDest,
                receiving=RecvBusy,
                success=RecvSuccess,
                error=RecvError,
                exception_response=RecvEx,
                device_id=4,
                count=1,
            )

    mapping = TagMap(
        {
            Enable: x[1],
            SrcBlock: ds.select(100, 102),
            DstBlock: ds.select(200, 202),
            Bits: c.select(10, 41),
            Words: ds.select(300, 301),
            DWord: dd[1],
            TonDone: t[1],
            TonAcc: td[1],
            TonReset: x[2],
            TofDone: t[2],
            TofAcc: td[2],
            CdDone: ct[3],
            CdAcc: ctd[3],
            CdReset: x[3],
            ShiftClock: x[4],
            ShiftReset: x[5],
            DrumReset: x[6],
            DrumJump: x[7],
            DrumJog: x[8],
            DrumStep: ds[10],
            DrumAcc: td[10],
            DrumDone: c[2],
            DrumOut1: y[1],
            DrumOut2: c[1],
            Event1: x[9],
            Event2: sc[50],
            SendSource: ds[20],
            SendBusy: c[3],
            SendSuccess: c[4],
            SendError: c[5],
            SendEx: ds[21],
            RecvDest: ds[22],
            RecvBusy: c[6],
            RecvSuccess: c[7],
            RecvError: c[8],
            RecvEx: ds[23],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert tokens == [
        "blockcopy(DS100..DS102,DS200..DS202,1)",
        "fill(7,DS200..DS202,1)",
        "pack_bits(C10..C25,DD1,1)",
        "pack_words(DS300..DS301,DD1,1)",
        "unpack_to_bits(DD1,C10..C41,1)",
        "unpack_to_words(DD1,DS300..DS301,1)",
        "on_delay(T1,TD1,100,Tms,1)",
        ".reset()",
        "off_delay(T2,TD2,50,Tms)",
        "count_down(CT3,CTD3,9)",
        ".reset()",
        "shift(C10..C17)",
        ".clock()",
        ".reset()",
        "event_drum([Y001,C1],[X009,SC50],[[1,0],[0,1]],DS10,C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        "time_drum([Y001,C1],[100,200],Tms,[[1,0],[0,1]],DS10,TD10,C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        'send("127.0.0.1",502,"DS1",DS20,C3,C4,C5,DS21,3,1)',
        'receive("127.0.0.1",502,"DS2",DS22,C6,C7,C8,DS23,4,1)',
    ]

    assert ".clock()" in tokens
    assert ".jump(DS10)" in tokens
    assert ".jog()" in tokens


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
