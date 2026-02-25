"""Rich Program fixture for reviewing generated CircuitPython code.

This example intentionally exercises a broad instruction surface so reviewers can
inspect one generated file that includes:
- inline pointer refs (DS[idx], DD[idx + 1])
- inline expressions in calc/copy
- control-flow (call/subroutine/return_early, forloop)
- timers/counters
- block/search/shift/pack/unpack instructions
- function call embedding (run_function/run_enabled_function)
"""

from pyrung import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    all_of,
    any_of,
    blockcopy,
    calc,
    call,
    copy,
    count_down,
    count_up,
    fall,
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
    rise,
    run_enabled_function,
    run_function,
    search,
    shift,
    subroutine,
    system,
    unpack_to_bits,
    unpack_to_words,
)

# BOOL control/state tags
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

# Timer/counter tags
TonDone = Bool("TonDone")
TonAcc = Int("TonAcc")
TofDone = Bool("TofDone")
TofAcc = Int("TofAcc")
CtuDone = Bool("CtuDone")
CtuAcc = Dint("CtuAcc")
CtdDone = Bool("CtdDone")
CtdAcc = Dint("CtdAcc")

# Data tags
Idx = Int("Idx", default=1)
Span = Int("Span", default=2)
LoopCount = Int("LoopCount", default=3)
Source = Int("Source")
CalcOut = Int("CalcOut")
FnOut = Int("FnOut")
FoundAddr = Int("FoundAddr")
PackedWord = Int("PackedWord")
PackedDword = Dint("PackedDword")

# Internal memory blocks
DS = Block("DS", TagType.INT, 1, 20)
DD = Block("DD", TagType.INT, 1, 20)
TXT = Block("TXT", TagType.CHAR, 1, 8)
BITS = Block("BITS", TagType.BOOL, 1, 32)
WORDS = Block("WORDS", TagType.INT, 1, 2)


def plus_offset(value, offset):
    return {"result": int(value) + int(offset)}


def gated_scale(enabled, value, factor):
    scaled = int(value) * int(factor)
    return {"result": scaled if enabled else int(value)}


with Program(strict=False) as logic:
    # Basic run-latch handling plus edge conditions for _prev coverage.
    with Rung(any_of(Enable, rise(Start), fall(Stop))):
        latch(Running)
    with Rung(any_of(Stop, Abort)):
        reset(Running)

    # Timers + counters.
    with Rung(Running):
        on_delay(TonDone, TonAcc, preset=250).reset(ShiftReset)
    with Rung(Running):
        off_delay(TofDone, TofAcc, preset=100)
    with Rung(Running):
        count_up(CtuDone, CtuAcc, preset=50).reset(Stop)
    with Rung(Running):
        count_down(CtdDone, CtdAcc, preset=5).reset(ShiftReset)

    # Inline expressions + inline pointer refs + block range operations.
    with Rung(all_of(Running, TonDone)):
        copy(120, Source)
        calc((Source * 2) + (Idx << 1) - 3, CalcOut, mode="decimal")
        copy(DS[Idx], DD[Idx + 1])
        copy(CalcOut // 2, DS[Idx + Span])
        blockcopy(DS.select(1, 4), DS.select(2, 5))
        fill(CalcOut, DD.select(Idx, Idx + Span))

    # Numeric + text search.
    with Rung(Running):
        search(">=", CalcOut, DD.select(1, 20), result=FoundAddr, found=Found, continuous=True)
        search("==", "AB", TXT.select(1, 8), result=FoundAddr, found=Found)

    # Shift is terminal, so keep it on its own rung.
    with Rung(Running):
        shift(BITS.select(1, 8)).clock(Clock).reset(ShiftReset)
    # Pack/unpack family.
    with Rung(Running):
        pack_bits(BITS.select(1, 16), PackedWord)
        pack_words(WORDS.select(1, 2), PackedDword)
        pack_text(TXT.select(1, 8), PackedDword, allow_whitespace=True)
        unpack_to_bits(PackedDword, BITS.select(1, 32))
        unpack_to_words(PackedDword, WORDS.select(1, 2))

    # Function calls, for-loop, and subroutine call.
    with Rung(all_of(Running, AutoMode)):
        run_function(plus_offset, ins={"value": CalcOut, "offset": 5}, outs={"result": FnOut})
        run_enabled_function(
            gated_scale,
            ins={"value": FnOut, "factor": 2},
            outs={"result": FnOut},
        )
        with forloop(LoopCount, oneshot=True) as loop:
            copy(loop.idx + Idx, DD[loop.idx + 1])
        call("service")

    # Indirect compare condition and SD command points.
    with Rung(DD[Idx] > 0):
        out(StepDone)
    with Rung(any_of(AutoMode, Found)):
        out(system.storage.sd.save_cmd)
    with Rung(Abort):
        out(system.storage.sd.delete_all_cmd)
    with Rung(Stop):
        out(system.storage.sd.eject_cmd)

    with subroutine("service"):
        with Rung(Abort):
            return_early()
        with Rung(all_of(Running, Found)):
            copy(DD[Idx], DS[10])
            copy(FnOut + 1, DS[11])
