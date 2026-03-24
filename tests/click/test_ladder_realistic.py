"""Realistic end-to-end ladder CSV test.

Adapted from ``circuitpy_codegen_review.py`` — same instruction surface but
Click-compatible (no inline expressions in copy, no Python functions, no
CircuitPython system commands).

Exercises:  OR conditions, edge contacts, timers, counters, calc expressions,
copy (literal + indirect), blockcopy, fill, search (numeric + text), shift,
event/time drums, pack/unpack family, forloop, subroutine, branches,
send/receive, and an indirect compare condition.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pyrung.click import (
    ModbusTcpTarget,
    TagMap,
    c,
    ct,
    ctd,
    dd,
    ds,
    receive,
    send,
    t,
    td,
    txt,
    x,
    y,
)
from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    all_of,
    any_of,
    fall,
    rise,
)
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


def _build_program_and_mapping():
    """Build a Click-compatible program with broad instruction coverage."""

    # ── BOOL control / state tags ────────────────────────────────────────
    Enable = Bool("Enable")
    Start = Bool("Start")
    Stop = Bool("Stop")
    AutoMode = Bool("AutoMode")
    Clock = Bool("Clock")
    ShiftReset = Bool("ShiftReset")
    Abort = Bool("Abort")
    Running = Bool("Running")
    StepDone = Bool("StepDone")
    Found = Bool("Found")

    # ── Timer / counter tags ─────────────────────────────────────────────
    RTonDone = Bool("RTonDone")
    RTonAcc = Int("RTonAcc")
    TofDone = Bool("TofDone")
    TofAcc = Int("TofAcc")
    CtuDone = Bool("CtuDone")
    CtuAcc = Dint("CtuAcc")
    CtdDone = Bool("CtdDone")
    CtdAcc = Dint("CtdAcc")

    # ── Data tags ────────────────────────────────────────────────────────
    Idx = Int("Idx", default=1)
    Source = Int("Source")
    CalcOut = Int("CalcOut")
    FoundAddr = Int("FoundAddr")
    PackedWord = Int("PackedWord")
    PackedDword = Dint("PackedDword")

    # ── Drum tags ────────────────────────────────────────────────────────
    DrumStep = Int("DrumStep", default=1)
    DrumJumpStep = Int("DrumJumpStep", default=2)
    DrumAcc = Int("DrumAcc")
    DrumDone = Bool("DrumDone")
    DrumEvt1 = Bool("DrumEvt1")
    DrumEvt2 = Bool("DrumEvt2")
    DrumEvt3 = Bool("DrumEvt3")
    DrumEvt4 = Bool("DrumEvt4")
    DrumOut1 = Bool("DrumOut1")
    DrumOut2 = Bool("DrumOut2")
    DrumOut3 = Bool("DrumOut3")

    # ── Send / receive status tags ───────────────────────────────────────
    SendBusy = Bool("SendBusy")
    SendOk = Bool("SendOk")
    SendErr = Bool("SendErr")
    SendEx = Int("SendEx")
    RecvDest = Int("RecvDest")
    RecvBusy = Bool("RecvBusy")
    RecvOk = Bool("RecvOk")
    RecvErr = Bool("RecvErr")
    RecvEx = Int("RecvEx")

    # ── Blocks ───────────────────────────────────────────────────────────
    SrcBlk = Block("SrcBlk", TagType.INT, 1, 4)
    DstBlk = Block("DstBlk", TagType.INT, 1, 5)
    Chars = Block("Chars", TagType.CHAR, 1, 8)
    Bits = Block("Bits", TagType.BOOL, 1, 32)
    Words = Block("Words", TagType.INT, 1, 2)

    # ── Program ──────────────────────────────────────────────────────────
    with Program() as logic:
        # R1: Run-latch — OR with rising/falling edges
        with Rung(any_of(Enable, rise(Start), fall(Stop))):
            latch(Running)

        # R2: Stop conditions — simple OR
        with Rung(any_of(Stop, Abort)):
            reset(Running)

        # R3: Retentive on-delay with reset pin
        with Rung(Running):
            on_delay(RTonDone, RTonAcc, preset=250).reset(ShiftReset)

        # R4: Off-delay
        with Rung(Running):
            off_delay(TofDone, TofAcc, preset=100)

        # R5: Count-up with reset pin
        with Rung(Running):
            count_up(CtuDone, CtuAcc, preset=50).reset(Stop)

        # R6: Count-down with reset pin
        with Rung(Running):
            count_down(CtdDone, CtdAcc, preset=5).reset(ShiftReset)

        # R7: copy + calc + indirect copy + blockcopy + fill
        with Rung(Running, RTonDone):
            copy(120, Source)
            calc((Source * 2) + (Idx * 2) - 3, CalcOut)
            copy(SrcBlk[Idx], DstBlk[Idx])
            blockcopy(SrcBlk.select(1, 4), DstBlk.select(1, 4))
            fill(CalcOut, DstBlk.select(1, 5))

        # R8: Numeric search (continuous)
        with Rung(Running):
            search(
                DstBlk.select(1, 5) >= CalcOut,
                result=FoundAddr,
                found=Found,
                continuous=True,
            )

        # R9: Text search
        with Rung(Running):
            search(Chars.select(1, 8) == "AB", result=FoundAddr, found=Found)

        # R10: Shift with clock + reset
        with Rung(Running):
            shift(Bits.select(1, 8)).clock(Clock).reset(ShiftReset)

        # R11: Event drum with reset / jump (AND condition) / jog (AND condition)
        with Rung(Running):
            event_drum(
                outputs=[DrumOut1, DrumOut2, DrumOut3],
                events=[DrumEvt1, DrumEvt2, DrumEvt3, DrumEvt4],
                pattern=[
                    [1, 0, 0],
                    [0, 1, 0],
                    [0, 0, 1],
                    [1, 1, 0],
                ],
                current_step=DrumStep,
                completion_flag=DrumDone,
            ).reset(ShiftReset).jump((AutoMode, Found), step=DrumJumpStep).jog(Clock, Found)

        # R12: Time drum with reset / jump / jog
        with Rung(Running):
            time_drum(
                outputs=[DrumOut1, DrumOut2, DrumOut3],
                presets=[50, 100, 75, 200],
                pattern=[
                    [1, 0, 0],
                    [0, 1, 0],
                    [0, 0, 1],
                    [1, 1, 0],
                ],
                current_step=DrumStep,
                accumulator=DrumAcc,
                completion_flag=DrumDone,
            ).reset(ShiftReset).jump(Found, step=DrumJumpStep).jog(Start)

        # R13: Pack / unpack family
        with Rung(Running):
            pack_bits(Bits.select(1, 16), PackedWord)
            pack_words(Words.select(1, 2), PackedDword)
            pack_text(Chars.select(1, 8), PackedDword, allow_whitespace=True)
            unpack_to_bits(PackedDword, Bits.select(1, 32))
            unpack_to_words(PackedDword, Words.select(1, 2))

        # R14: For-loop (must be alone on rung for Click)
        with Rung(Running, AutoMode):
            with forloop(3, oneshot=True):
                out(StepDone)

        # R15: Subroutine call
        with Rung(Running, AutoMode):
            call("service")

        # R16: Branches — parallel paths under parent rung power
        with Rung(Running):
            copy(Idx, DstBlk[4])
            with branch(AutoMode):
                copy(CalcOut, DstBlk[1])
            with branch(Found, CtuDone):
                copy(FoundAddr, DstBlk[2])
            copy(Source, DstBlk[5])

        # R17: Send
        with Rung():
            send(
                target=ModbusTcpTarget("plc1", "10.0.0.1", port=502, device_id=1),
                remote_start="DS1",
                source=CalcOut,
                sending=SendBusy,
                success=SendOk,
                error=SendErr,
                exception_response=SendEx,
                count=1,
            )

        # R18: Receive
        with Rung():
            receive(
                target=ModbusTcpTarget("plc1", "10.0.0.1", port=502, device_id=1),
                remote_start="DS10",
                dest=RecvDest,
                receiving=RecvBusy,
                success=RecvOk,
                error=RecvErr,
                exception_response=RecvEx,
                count=1,
            )

        # R19: OR condition
        with Rung(AutoMode | Found):
            out(StepDone)

        # R20: Compare condition
        with Rung(CalcOut > 0):
            out(StepDone)

        # ── Subroutine ───────────────────────────────────────────────────
        with subroutine("service"):
            with Rung(Abort):
                return_early()
            with Rung(all_of(Running, Found)):
                copy(CalcOut, DstBlk[3])

    # ── TagMap ───────────────────────────────────────────────────────────
    mapping = TagMap(
        {
            # Input bits (X)
            Enable: x[1],
            Start: x[2],
            Stop: x[3],
            AutoMode: x[4],
            Clock: x[5],
            ShiftReset: x[6],
            Abort: x[7],
            # Output bits (Y)
            Running: y[1],
            StepDone: y[2],
            # Internal bits (C)
            Found: c[1],
            # Timers
            RTonDone: t[1],
            RTonAcc: td[1],
            TofDone: t[2],
            TofAcc: td[2],
            # Counters
            CtuDone: ct[1],
            CtuAcc: ctd[1],
            CtdDone: ct[2],
            CtdAcc: ctd[2],
            # Data (DS)
            Idx: ds[1],
            Source: ds[2],
            CalcOut: ds[3],
            FoundAddr: ds[4],
            PackedWord: ds[5],
            PackedDword: dd[1],
            # Drum
            DrumStep: ds[10],
            DrumJumpStep: ds[11],
            DrumAcc: td[10],
            DrumDone: c[10],
            DrumEvt1: x[11],
            DrumEvt2: x[12],
            DrumEvt3: x[13],
            DrumEvt4: x[14],
            DrumOut1: y[11],
            DrumOut2: c[11],
            DrumOut3: c[12],
            # Send / receive
            SendBusy: c[20],
            SendOk: c[21],
            SendErr: c[22],
            SendEx: ds[20],
            RecvDest: ds[22],
            RecvBusy: c[23],
            RecvOk: c[24],
            RecvErr: c[25],
            RecvEx: ds[23],
            # Blocks
            SrcBlk: ds.select(100, 103),
            DstBlk: ds.select(200, 204),
            Chars: txt.select(1, 8),
            Bits: c.select(100, 131),
            Words: ds.select(300, 301),
        },
        include_system=False,
    )

    return logic, mapping


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_realistic_to_ladder_succeeds():
    """The adapted codegen-review program exports without LadderExportError."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    # Every non-comment row must have exactly 33 columns.
    for row in bundle.main_rows:
        if row[0] == "#":
            continue
        assert len(row) == 33, f"Bad column count: {len(row)} in {row}"

    # Subroutine file present.
    assert len(bundle.subroutine_rows) == 1
    assert bundle.subroutine_rows[0][0] == "service"


def test_realistic_instruction_surface_coverage():
    """Every instruction family in the program appears in the AF column."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    main_tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != "" and row[0] != "#"]
    sub_tokens = [row[-1] for row in bundle.subroutine_rows[0][1][1:] if row[-1] != ""]
    tokens = main_tokens + sub_tokens

    expected_prefixes = [
        "latch(",
        "reset(",
        "on_delay(",
        "off_delay(",
        "count_up(",
        "count_down(",
        "copy(",
        "calc(",
        "blockcopy(",
        "fill(",
        "search(",
        "shift(",
        "event_drum(",
        "time_drum(",
        "pack_bits(",
        "pack_words(",
        "pack_text(",
        "unpack_to_bits(",
        "unpack_to_words(",
        "send(",
        "receive(",
        "for(",
        'call("',
    ]
    for prefix in expected_prefixes:
        assert any(t.startswith(prefix) for t in tokens), f"Missing token prefix: {prefix}"

    # Exact-match tokens.
    assert "next()" in tokens
    assert "return()" in sub_tokens
    assert ".reset()" in tokens
    assert ".clock()" in tokens
    assert ".jog()" in tokens
    assert any(t.startswith(".jump(") for t in tokens)

    # out() appears in main (OR rung, indirect compare, forloop body) and subroutine is not needed.
    assert any(t.startswith("out(") for t in main_tokens)


def test_realistic_branch_wiring():
    """The branch rung (R15) has correct continuation rows and T/- wiring."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    # Find the branch rung: parent instruction is copy(DS1,DS203)
    # DS1 = Idx, DstBlk[4] = DS203
    branch_start = None
    for i, row in enumerate(bundle.main_rows):
        if row[-1] == "copy(DS1,DS203)":
            branch_start = i
            break
    assert branch_start is not None, "Could not find branch rung start row"

    r0 = bundle.main_rows[branch_start]
    r1 = bundle.main_rows[branch_start + 1]
    r2 = bundle.main_rows[branch_start + 2]
    r3 = bundle.main_rows[branch_start + 3]

    # R row: parent condition (Y001=Running) + T wire + copy token
    assert r0[0] == "R"
    assert r0[1] == "Y001"  # Running
    assert "T" in r0  # split marker

    # Branch 1: AutoMode → copy(CalcOut, DstBlk[1])
    assert r1[0] == ""  # continuation
    assert any(cell in {"X004", "T:X004"} for cell in r1)  # AutoMode
    assert r1[-1] == "copy(DS3,DS200)"

    # Branch 2: Found, CtuDone → copy(FoundAddr, DstBlk[2])
    assert r2[0] == ""
    assert any(cell in {"C1", "T:C1"} for cell in r2)  # Found
    assert "CT1" in r2  # CtuDone
    assert r2[-1] == "copy(DS4,DS201)"

    # Trailing parent instruction: copy(Source, DstBlk[5])
    assert r3[0] == ""
    assert r3[-1] == "copy(DS2,DS204)"


def test_realistic_or_condition_expansion():
    """OR conditions expand into continuation rows with T/- wiring."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    # R1: any_of(Enable, rise(Start), fall(Stop)) — 3-way OR
    # First rung row should have latch(Y001) as AF token.
    r1_idx = None
    for i, row in enumerate(bundle.main_rows):
        if row[-1] == "latch(Y001)":
            r1_idx = i
            break
    assert r1_idx is not None

    # 3-way OR → 3 rows (main + 2 continuation)
    assert bundle.main_rows[r1_idx][0] == "R"
    assert bundle.main_rows[r1_idx + 1][0] == ""
    assert bundle.main_rows[r1_idx + 2][0] == ""

    # Main row carries the AF token; OR continuation rows don't.
    assert bundle.main_rows[r1_idx][-1] == "latch(Y001)"
    assert bundle.main_rows[r1_idx + 1][-1] == ""
    assert bundle.main_rows[r1_idx + 2][-1] == ""


def test_realistic_csv_roundtrip(tmp_path: Path):
    """Write CSV files and read them back; verify content survives roundtrip."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)

    assert (out_dir / "main.csv").exists()
    assert (out_dir / "sub_service.csv").exists()

    # Read back and compare row-by-row.
    with (out_dir / "main.csv").open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        read_back = [tuple(row) for row in reader]

    assert len(read_back) == len(bundle.main_rows)
    for expected, actual in zip(bundle.main_rows, read_back, strict=True):
        # Comment rows have fewer columns; compare as-is.
        if expected[0] == "#":
            assert actual == list(expected) or tuple(actual) == expected
        else:
            assert tuple(actual) == expected


def test_realistic_indirect_ref_tokens():
    """Indirect refs render as BANK[pointer+offset] in AF tokens."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    # copy(SrcBlk[Idx], DstBlk[Idx]) → DS pointers with offsets
    indirect_copies = [t for t in tokens if "DS[DS1" in t]
    assert len(indirect_copies) >= 1, "Expected at least one indirect-ref copy token"


def test_realistic_compare_condition():
    """The compare rung has DS3>0 in a condition cell."""
    logic, mapping = _build_program_and_mapping()
    bundle = mapping.to_ladder(logic)

    # R19: CalcOut > 0 → out(StepDone)
    # CalcOut maps to DS3 → condition cell should be DS3>0
    for row in bundle.main_rows:
        if row[-1] == "out(Y002)" and row[0] == "R":
            cells = row[1:-1]
            if "DS3>0" in cells:
                return
    pytest.fail("Could not find compare condition cell with DS3>0")
