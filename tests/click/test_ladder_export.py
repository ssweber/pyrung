"""Tests for Click ladder CSV export (`TagMap.to_ladder`)."""

from __future__ import annotations

import csv
import functools
import re
from pathlib import Path

import pytest

from pyrung.click import (
    LadderExportError,
    ModbusTarget,
    TagMap,
    c,
    ct,
    ctd,
    dd,
    dh,
    ds,
    receive,
    sc,
    send,
    t,
    td,
    txt,
    x,
    y,
)
from pyrung.core import Block, Bool, Dint, Int, Program, Rung, TagType, all_of, any_of, immediate
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
    pack_text,
    pack_words,
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
        *tuple(
            [chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]
        ),
        "AF",
    )


def _row(marker: str, prefix: list[str], af: str) -> tuple[str, ...]:
    cells = list(prefix)
    cells.extend(["-"] * (31 - len(cells)))
    return tuple([marker, *cells, af])


def _blank_row(marker: str, prefix: list[str], af: str = "") -> tuple[str, ...]:
    """Like _row but pads with blanks (OR continuation rows have no wires)."""
    cells = list(prefix)
    cells.extend([""] * (31 - len(cells)))
    return tuple([marker, *cells, af])


_END_ROW = _row("R", [], "end()")


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
        _row("R", ["X001", "X002"], "out(Y001)"),
        _END_ROW,
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
        _row("R", ["X001", "T", "C1"], "out(Y001)"),
        _blank_row("", ["X002"]),
        _END_ROW,
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
        _row("R", ["X001", "T"], "out(Y001)"),
        _row("", ["", "X002"], "out(Y002)"),
        _END_ROW,
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
    assert bundle.main_rows[2][2] == "T:X002"
    assert bundle.main_rows[3][2] == "X003"
    assert bundle.main_rows[1][-1] == "out(Y001)"
    assert bundle.main_rows[2][-1] == "out(Y002)"
    assert bundle.main_rows[3][-1] == "out(Y003)"


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
        _row("R", ["X001", "T"], "out(Y001)"),
        _row("", ["", "T:X002"], "out(Y002)"),
        _row("", ["", "-"], "out(Y003)"),
        _END_ROW,
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
        _row("R", ["X001", "X002", "T"], "out(Y001)"),
        _row("", ["", "", "T"], "latch(Y002)"),
        _row("", ["", "", "-"], "reset(Y003)"),
        _END_ROW,
    )


def test_immediate_contact_and_coils_render_canonical_tokens():
    Start = Bool("Start")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")
    Y3 = Bool("Y3")

    with Program() as logic:
        with Rung(immediate(Start)):
            out(immediate(Y1))
            latch(immediate(Y2))
            reset(immediate(Y3))

    mapping = TagMap(
        {
            Start: x[1],
            Y1: y[1],
            Y2: y[2],
            Y3: y[3],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["immediate(X001)", "T"], "out(immediate(Y001))"),
        _row("", ["", "T"], "latch(immediate(Y002))"),
        _row("", ["", "-"], "reset(immediate(Y003))"),
        _END_ROW,
    )


def test_immediate_contiguous_range_renders_compact_token():
    Start = Bool("Start")
    Outputs = Block("Outputs", TagType.BOOL, 1, 4)

    with Program() as logic:
        with Rung(Start):
            out(immediate(Outputs.select(1, 4)))

    mapping = TagMap(
        {
            Start: x[1],
            Outputs: y.select(1, 4),
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows[1][-1] == "out(immediate(Y001..Y004))"


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
    assert bundle.main_rows[2][2] == "|"
    assert bundle.main_rows[3][2] == ""
    assert bundle.main_rows[2][3] == ""
    assert bundle.main_rows[3][3] == ""
    assert bundle.main_rows[1][-1] == "out(Y001)"
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

    assert bundle.main_rows[1][-1] == "count_up(CT1,CTD1,preset=5)"
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

    assert bundle.main_rows[1][-1] == "for(3,oneshot=1)"
    assert bundle.main_rows[2][0] == "R"
    assert bundle.main_rows[2][-1] == "out(Y001)"
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
    assert main_tokens == ['call("beta-two")', 'call("alpha")', "end()"]
    alpha_rows = dict(bundle.subroutine_rows)["alpha"]
    beta_rows = dict(bundle.subroutine_rows)["beta-two"]
    assert alpha_rows[-1][-1] == "return()"
    assert sum(1 for row in beta_rows if row[-1] == "return()") == 1

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)
    assert (out_dir / "main.csv").exists()
    assert (out_dir / "sub_alpha.csv").exists()
    assert (out_dir / "sub_beta_two.csv").exists()


def test_string_token_rendering_uses_doubled_quotes_without_backslash_escapes():
    Enable = Bool("Enable")
    Chars = Block("Chars", TagType.CHAR, 1, 4)
    Result = Int("Result")
    Found = Bool("Found")

    with Program() as logic:
        with Rung(Enable):
            search("==", 'sub"name', Chars.select(1, 4), result=Result, found=Found)
        with Rung(Enable):
            search("==", "normal", Chars.select(1, 4), result=Result, found=Found)

    mapping = TagMap(
        {
            Enable: x[1],
            Chars: txt.select(1, 4),
            Result: ds[1],
            Found: c[1],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert 'search("==",value="sub""name",search_range=TXT1..TXT4,result=DS1,found=C1)' in tokens
    assert 'search("==",value="normal",search_range=TXT1..TXT4,result=DS1,found=C1)' in tokens
    assert all('\\"' not in token for token in tokens)


def test_string_token_csv_roundtrip_requires_only_doubled_quote_unescape(tmp_path: Path):
    Enable = Bool("Enable")
    Chars = Block("Chars", TagType.CHAR, 1, 4)
    Result = Int("Result")
    Found = Bool("Found")

    with Program() as logic:
        with Rung(Enable):
            search("==", 'sub"name', Chars.select(1, 4), result=Result, found=Found)

    mapping = TagMap(
        {
            Enable: x[1],
            Chars: txt.select(1, 4),
            Result: ds[1],
            Found: c[1],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = 'search("==",value="sub""name",search_range=TXT1..TXT4,result=DS1,found=C1)'
    assert bundle.main_rows[1][-1] == expected

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)

    raw_csv = (out_dir / "main.csv").read_text(encoding="utf-8")
    assert '\\"' not in raw_csv

    with (out_dir / "main.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    af_token = rows[1][-1]
    assert af_token == expected
    assert '\\"' not in af_token

    value_literal = af_token[len('search("==",value=') :].split(",search_range=", maxsplit=1)[0]
    assert value_literal == '"sub""name"'
    assert value_literal[1:-1].replace('""', '"') == 'sub"name'


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
            search("==", 1, Data.select(1, 2), result=Result, found=Found)
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

    assert "out(Y001)" in tokens
    assert any(token.startswith("copy(") and "oneshot" not in token for token in tokens)
    assert any(",mode=decimal)" in token for token in tokens if token.startswith("calc("))
    assert any(token.startswith("search(") and "continuous" not in token for token in tokens)
    assert any(
        token.startswith("pack_text(") and "allow_whitespace" not in token for token in tokens
    )


def test_calc_hex_token_uses_inferred_hex_mode():
    Enable = Bool("Enable")
    H1 = Block("H1", TagType.WORD, 1, 1)
    H2 = Block("H2", TagType.WORD, 1, 1)
    Dest = Block("Dest", TagType.WORD, 1, 1)

    with Program() as logic:
        with Rung(Enable):
            calc(H1[1] | H2[1], Dest[1])

    mapping = TagMap(
        {
            Enable: x[1],
            H1[1]: dh[1],
            H2[1]: dh[2],
            Dest[1]: dh[3],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert any(",mode=hex)" in token for token in tokens if token.startswith("calc("))


def test_mixed_family_calc_fails_precheck_with_calc_mode_mixed_code():
    Enable = Bool("Enable")
    A = Int("A")
    H = Block("H", TagType.WORD, 1, 1)
    Dest = Int("Dest")

    with Program() as logic:
        with Rung(Enable):
            calc(A + H[1], Dest)

    mapping = TagMap(
        {
            Enable: x[1],
            A: ds[1],
            H[1]: dh[1],
            Dest: ds[2],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        mapping.to_ladder(logic)

    assert any("CLK_CALC_MODE_MIXED" in str(issue["message"]) for issue in exc_info.value.issues)


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
                target=ModbusTarget("plc1", "127.0.0.1", port=502, device_id=3),
                remote_start="DS1",
                source=SendSource,
                sending=SendBusy,
                success=SendSuccess,
                error=SendError,
                exception_response=SendEx,
                count=1,
            )
        with Rung(Enable):
            receive(
                target=ModbusTarget("plc2", "127.0.0.1", port=502, device_id=4),
                remote_start="DS2",
                dest=RecvDest,
                receiving=RecvBusy,
                success=RecvSuccess,
                error=RecvError,
                exception_response=RecvEx,
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
        "blockcopy(DS100..DS102,DS200..DS202,oneshot=1)",
        "fill(7,DS200..DS202,oneshot=1)",
        "pack_bits(C10..C25,DD1,oneshot=1)",
        "pack_words(DS300..DS301,DD1,oneshot=1)",
        "unpack_to_bits(DD1,C10..C41,oneshot=1)",
        "unpack_to_words(DD1,DS300..DS301,oneshot=1)",
        "on_delay(T1,TD1,preset=100,unit=Tms)",
        ".reset()",
        "off_delay(T2,TD2,preset=50,unit=Tms)",
        "count_down(CT3,CTD3,preset=9)",
        ".reset()",
        "shift(C10..C17)",
        ".clock()",
        ".reset()",
        "event_drum(outputs=[Y001,C1],events=[X009,SC50],pattern=[[1,0],[0,1]],current_step=DS10,completion_flag=C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        "time_drum(outputs=[Y001,C1],presets=[100,200],unit=Tms,pattern=[[1,0],[0,1]],current_step=DS10,accumulator=TD10,completion_flag=C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        'send(target=ModbusTarget(name="plc1",ip="127.0.0.1",port=502,device_id=3),remote_start="DS1",source=DS20,sending=C3,success=C4,error=C5,exception_response=DS21,count=1)',
        'receive(target=ModbusTarget(name="plc2",ip="127.0.0.1",port=502,device_id=4),remote_start="DS2",dest=DS22,receiving=C6,success=C7,error=C8,exception_response=DS23,count=1)',
        "end()",
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


def test_immediate_non_contiguous_range_fails_with_explicit_diagnostic():
    Start = Bool("Start")
    Outputs = Block("Outputs", TagType.BOOL, 1, 4)

    with Program() as logic:
        with Rung(Start):
            out(immediate(Outputs.select(1, 4)))

    mapping = TagMap(
        {
            Start: x[1],
            Outputs[1]: y[1],
            Outputs[2]: y[3],
            Outputs[3]: y[4],
            Outputs[4]: y[6],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        mapping.to_ladder(logic)

    issue = exc_info.value.issues[0]
    assert "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS" in str(issue["message"])


def test_immediate_in_copy_fails_with_context_diagnostic():
    Enable = Bool("Enable")
    Source = Bool("Source")
    Dest = Int("Dest")

    with Program() as logic:
        with Rung(Enable):
            copy(immediate(Source), Dest)

    mapping = TagMap(
        {
            Enable: x[1],
            Source: x[2],
            Dest: ds[1],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        mapping.to_ladder(logic)

    issue = exc_info.value.issues[0]
    assert "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED" in str(issue["message"])


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


# --- Rung comment rows ---


def test_comment_single_line():
    A = Bool("A")
    B = Bool("B")

    with Program() as logic:
        with Rung(A) as r:
            r.comment = "Turn on B when A is true."
            out(B)

    mapping = TagMap({A: x[1], B: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        ("#", "Turn on B when A is true."),
        _row("R", ["X001"], "out(Y001)"),
        _END_ROW,
    )


def test_comment_multi_line():
    A = Bool("A")
    B = Bool("B")

    with Program() as logic:
        with Rung(A) as r:
            r.comment = "Line one.\nLine two."
            out(B)

    mapping = TagMap({A: x[1], B: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        ("#", "Line one."),
        ("#", "Line two."),
        _row("R", ["X001"], "out(Y001)"),
        _END_ROW,
    )


def test_no_comment_no_extra_rows():
    A = Bool("A")
    B = Bool("B")

    with Program() as logic:
        with Rung(A):
            out(B)

    mapping = TagMap({A: x[1], B: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (
        _header(),
        _row("R", ["X001"], "out(Y001)"),
        _END_ROW,
    )


def test_comment_with_branches():
    A = Bool("A")
    Mode = Bool("Mode")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")

    with Program() as logic:
        with Rung(A) as r:
            r.comment = "Branching rung."
            with branch(Mode):
                out(Y1)
            with branch(~Mode):
                out(Y2)

    mapping = TagMap({A: x[1], Mode: c[1], Y1: y[1], Y2: y[2]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    # Comment row should be first, before the R row
    assert bundle.main_rows[1] == ("#", "Branching rung.")
    assert bundle.main_rows[2][0] == "R"


def test_comment_not_emitted_for_empty_branches():
    A = Bool("A")
    Mode = Bool("Mode")

    with Program() as logic:
        with Rung(A) as r:
            r.comment = "No rows should be emitted."
            with branch(Mode):
                pass

    mapping = TagMap({A: x[1], Mode: c[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)

    assert bundle.main_rows == (_header(), _END_ROW)


# --- Native topology golden suite (source: tests/fixtures/click_or_topology.csv) ---


_NATIVE_OR_TOPOLOGY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "click_or_topology.csv"
)


@functools.lru_cache(maxsize=1)
def _native_or_topology_rows() -> dict[int, tuple[tuple[str, ...], ...]]:
    patterns: dict[int, list[tuple[str, ...]]] = {}
    current_pattern: int | None = None

    with _NATIVE_OR_TOPOLOGY_FIXTURE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue

            marker = row[0]
            if marker == "#":
                text = row[1].strip() if len(row) > 1 else ""
                match = re.match(r"^(\d+)\.", text)
                if match is not None:
                    current_pattern = int(match.group(1))
                    patterns[current_pattern] = []
                continue

            if marker in {"R", ""} and current_pattern is not None:
                patterns[current_pattern].append(tuple(row))

    return {key: tuple(rows) for key, rows in patterns.items()}


def _assert_native_pattern(
    *,
    pattern_id: int,
    bundle_rows: tuple[tuple[str, ...], ...],
    expected_rows: tuple[tuple[str, ...], ...],
) -> None:
    assert _native_or_topology_rows()[pattern_id] == expected_rows
    assert bundle_rows == (_header(), *expected_rows, _END_ROW)


def test_native_or_topology_fixture_shape():
    patterns = _native_or_topology_rows()
    assert set(patterns) == set(range(1, 9))
    assert all(len(row) == 33 for rows in patterns.values() for row in rows)


def test_native_pattern_1_mid_rung_or():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(X001, any_of(X002, C1)):
            out(Y001)

    mapping = TagMap({X001: x[1], X002: x[2], C1: c[1], Y001: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T:X002", "T"], "out(Y001)"),
        _blank_row("", ["", "C1"]),
    )
    _assert_native_pattern(pattern_id=1, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_2_series_ors():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(any_of(X001, X002), any_of(C1, C2)):
            out(Y001)

    mapping = TagMap({X001: x[1], X002: x[2], C1: c[1], C2: c[2], Y001: y[1]}, include_system=False)
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T", "T:C1", "T"], "out(Y001)"),
        _blank_row("", ["X002", "", "C2"]),
    )
    _assert_native_pattern(pattern_id=2, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_3_or_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(any_of(X001, X002)):
            out(Y001)
            with branch(C1):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], C1: c[1], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "C1"], "out(Y002)"),
    )
    _assert_native_pattern(pattern_id=3, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_4_three_way_or_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(any_of(X001, X002, X003)):
            out(Y001)
            with branch(C1):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "|", "C1"], "out(Y002)"),
        _blank_row("", ["X003"]),
    )
    _assert_native_pattern(pattern_id=4, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_5_combined_or_multi_output_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    X004 = Bool("X004")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")
    Y003 = Bool("Y003")

    with Program() as logic:
        with Rung(X001, any_of(X002, X003, X004)):
            out(Y001)
            with branch(C1):
                out(Y002)
            out(Y003)

    mapping = TagMap(
        {
            X001: x[1],
            X002: x[2],
            X003: x[3],
            X004: x[4],
            C1: c[1],
            Y001: y[1],
            Y002: y[2],
            Y003: y[3],
        },
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T:X002", "T", "T"], "out(Y001)"),
        _row("", ["", "T:X003", "|", "T:C1"], "out(Y002)"),
        _row("", ["", "X004", "", "-"], "out(Y003)"),
    )
    _assert_native_pattern(pattern_id=5, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_6_mid_rung_or_with_nested_all_of():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    C2 = Bool("C2")
    C3 = Bool("C3")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(X001, any_of(X002, all_of(C1, C2, C3), X003)):
            out(Y001)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], C2: c[2], C3: c[3], Y001: y[1]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T:X002", "-", "-", "T:X003"], "out(Y001)"),
        _blank_row("", ["", "C1", "C2", "C3"]),
    )
    _assert_native_pattern(pattern_id=6, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_7_or_with_two_branches():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")
    Y003 = Bool("Y003")

    with Program() as logic:
        with Rung(any_of(X001, X002)):
            out(Y001)
            with branch(C1):
                out(Y002)
            with branch(C2):
                out(Y003)

    mapping = TagMap(
        {X001: x[1], X002: x[2], C1: c[1], C2: c[2], Y001: y[1], Y002: y[2], Y003: y[3]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "T:C1"], "out(Y002)"),
        _row("", ["", "", "C2"], "out(Y003)"),
    )
    _assert_native_pattern(pattern_id=7, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_8_series_ors_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(any_of(X001, X002), any_of(C1, C2)):
            out(Y001)
            with branch(X003):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], C2: c[2], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = mapping.to_ladder(logic)
    expected = (
        _row("R", ["X001", "T", "T:C1", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "C2", "", "X003"], "out(Y002)"),
    )
    _assert_native_pattern(pattern_id=8, bundle_rows=bundle.main_rows, expected_rows=expected)
