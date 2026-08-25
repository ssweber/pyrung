"""Instruction-level coverage for the analysis toolchain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    blockcopy,
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
    run_enabled_function,
    run_function,
    search,
    shift,
    subroutine,
    time_drum,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import PENDING, Intractable, _classify_dimensions
from pyrung.core.analysis.prove.elision.slice import _collect_hidden_memory_info
from pyrung.core.analysis.reverse_edges import back_propagate_value, build_reverse_edge_map
from pyrung.core.analysis.sp_values import _written_value_for_tag
from pyrung.core.copy_converters import to_ascii, to_binary, to_text, to_value
from pyrung.core.instruction.send_receive import receive, send
from pyrung.core.state import SystemState


@dataclass(frozen=True)
class GraphCase:
    name: str
    instruction: str
    build: Callable[[], Program]
    condition_reads: frozenset[str] = frozenset()
    data_reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    implicit_writes: frozenset[str] = frozenset()
    calls: tuple[str, ...] = ()
    scope: str = "main"
    subroutine: str | None = None
    rung_index: int = 0


def _node_index(graph, case: GraphCase) -> int:
    for idx, node in enumerate(graph.rung_nodes):
        if (
            node.scope == case.scope
            and node.subroutine == case.subroutine
            and node.rung_index == case.rung_index
            and node.branch_path == ()
        ):
            return idx
    raise AssertionError(f"node not found for {case.name}")


def _status(prefix: str) -> tuple[Bool, Bool, Bool, Int]:
    return (
        Bool(f"{prefix}Busy"),
        Bool(f"{prefix}Success"),
        Bool(f"{prefix}Error"),
        Int(f"{prefix}Exception"),
    )


def _callback(**kwargs: Any) -> dict[str, Any]:
    return {"result": next(iter(kwargs.values()), 1)}


def _enabled_callback(enabled: bool, **kwargs: Any) -> dict[str, Any]:
    return {"result": next(iter(kwargs.values()), 1) if enabled else 0}


def _program_with_one_rung(fn: Callable[[], None], *conditions: Any) -> Program:
    with Program(strict=False) as logic:
        with Rung(*conditions):
            fn()
    return logic


def _out_case() -> Program:
    enable = Bool("OutEnable")
    light = Bool("OutLight")
    return _program_with_one_rung(lambda: out(light), enable)


def _latch_case() -> Program:
    enable = Bool("LatchEnable")
    target = Bool("Latched")
    return _program_with_one_rung(lambda: latch(target), enable)


def _reset_case() -> Program:
    enable = Bool("ResetEnable")
    target = Bool("ResetTarget")
    return _program_with_one_rung(lambda: reset(target), enable)


def _copy_case() -> Program:
    src = Int("CopySource")
    dest = Int("CopyDest")
    return _program_with_one_rung(lambda: copy(src, dest), Bool("CopyEnable"))


def _copy_string_char_fanout_case() -> Program:
    chars = Block("CopyText", TagType.CHAR, 1, 4)
    return _program_with_one_rung(lambda: copy("ABC", chars[1]), Bool("CopyTextEnable"))


def _copy_to_value_case() -> Program:
    chars = Block("ValueChars", TagType.CHAR, 1, 2)
    dest = Int("ValueDest")
    return _program_with_one_rung(lambda: copy(chars[1], dest, convert=to_value), Bool("ValueEn"))


def _copy_to_ascii_case() -> Program:
    chars = Block("AsciiChars", TagType.CHAR, 1, 2)
    dest = Int("AsciiDest")
    return _program_with_one_rung(lambda: copy(chars[1], dest, convert=to_ascii), Bool("AsciiEn"))


def _copy_to_text_case() -> Program:
    src = Int("TextSource")
    chars = Block("TextChars", TagType.CHAR, 1, 8)
    return _program_with_one_rung(lambda: copy(src, chars[1], convert=to_text()), Bool("TextEn"))


def _copy_to_text_unsuppressed_case() -> Program:
    src = Int("UnsuppressedSource")
    chars = Block("UnsuppressedChars", TagType.CHAR, 1, 8)
    return _program_with_one_rung(
        lambda: copy(src, chars[1], convert=to_text(suppress_zero=False)),
        Bool("UnsuppressedEn"),
    )


def _copy_to_text_exponential_case() -> Program:
    src = Real("ExpSource")
    chars = Block("ExpChars", TagType.CHAR, 1, 16)
    return _program_with_one_rung(
        lambda: copy(src, chars[1], convert=to_text(exponential=True)),
        Bool("ExpEn"),
    )


def _copy_to_text_nul_case() -> Program:
    src = Int("NulSource")
    chars = Block("NulChars", TagType.CHAR, 1, 8)
    return _program_with_one_rung(
        lambda: copy(src, chars[1], convert=to_text(termination_code=0)),
        Bool("NulEn"),
    )


def _copy_to_text_hex_term_case() -> Program:
    src = Int("HexTermSource")
    chars = Block("HexTermChars", TagType.CHAR, 1, 8)
    return _program_with_one_rung(
        lambda: copy(src, chars[1], convert=to_text(termination_code="$0D")),
        Bool("HexTermEn"),
    )


def _copy_to_binary_case() -> Program:
    src = Int("BinarySource")
    chars = Block("BinaryChars", TagType.CHAR, 1, 2)
    return _program_with_one_rung(
        lambda: copy(src, chars[1], convert=to_binary),
        Bool("BinaryEn"),
    )


def _run_function_case() -> Program:
    src = Int("FnSource")
    dest = Int("FnDest", min=0, max=10)
    return _program_with_one_rung(
        lambda: run_function(_callback, ins={"value": src}, outs={"result": dest}),
        Bool("FnEnable"),
    )


def _run_enabled_function_case() -> Program:
    src = Int("EnabledFnSource")
    dest = Int("EnabledFnDest", min=0, max=10)
    return _program_with_one_rung(
        lambda: run_enabled_function(
            _enabled_callback,
            ins={"value": src},
            outs={"result": dest},
        ),
        Bool("EnabledFnEnable"),
    )


def _blockcopy_case() -> Program:
    src = Block("BlockSrc", TagType.INT, 1, 2)
    dest = Block("BlockDest", TagType.INT, 1, 2)
    return _program_with_one_rung(
        lambda: blockcopy(src.select(1, 2), dest.select(1, 2)),
        Bool("BlockCopyEnable"),
    )


def _blockcopy_to_value_case() -> Program:
    src = Block("BlockValueChars", TagType.CHAR, 1, 2)
    dest = Block("BlockValueDest", TagType.INT, 1, 2)
    return _program_with_one_rung(
        lambda: blockcopy(src.select(1, 2), dest.select(1, 2), convert=to_value),
        Bool("BlockValueEnable"),
    )


def _blockcopy_to_ascii_case() -> Program:
    src = Block("BlockAsciiChars", TagType.CHAR, 1, 2)
    dest = Block("BlockAsciiDest", TagType.INT, 1, 2)
    return _program_with_one_rung(
        lambda: blockcopy(src.select(1, 2), dest.select(1, 2), convert=to_ascii),
        Bool("BlockAsciiEnable"),
    )


def _fill_case() -> Program:
    src = Int("FillSource")
    dest = Block("FillDest", TagType.INT, 1, 2)
    return _program_with_one_rung(
        lambda: fill(src, dest.select(1, 2)),
        Bool("FillEnable"),
    )


def _pack_bits_case() -> Program:
    bits = Block("PackBits", TagType.BOOL, 1, 2)
    dest = Word("PackedBits")
    return _program_with_one_rung(
        lambda: pack_bits(bits.select(1, 2), dest),
        Bool("PackBitsEnable"),
    )


def _pack_words_case() -> Program:
    words = Block("PackWords", TagType.INT, 1, 2)
    dest = Dint("PackedWords")
    return _program_with_one_rung(
        lambda: pack_words(words.select(1, 2), dest),
        Bool("PackWordsEnable"),
    )


def _pack_text_case() -> Program:
    chars = Block("PackTextChars", TagType.CHAR, 1, 2)
    dest = Int("ParsedText")
    return _program_with_one_rung(
        lambda: pack_text(chars.select(1, 2), dest),
        Bool("PackTextEnable"),
    )


def _unpack_to_bits_case() -> Program:
    src = Word("UnpackBitsSource")
    bits = Block("UnpackBits", TagType.BOOL, 1, 2)
    return _program_with_one_rung(
        lambda: unpack_to_bits(src, bits.select(1, 2)),
        Bool("UnpackBitsEnable"),
    )


def _unpack_to_words_case() -> Program:
    src = Dint("UnpackWordsSource")
    words = Block("UnpackWords", TagType.INT, 1, 2)
    return _program_with_one_rung(
        lambda: unpack_to_words(src, words.select(1, 2)),
        Bool("UnpackWordsEnable"),
    )


def _calc_case() -> Program:
    a = Int("CalcA")
    b = Int("CalcB")
    result = Int("CalcResult")
    return _program_with_one_rung(lambda: calc(a + b, result), Bool("CalcEnable"))


def _search_case() -> Program:
    data = Block("SearchData", TagType.INT, 1, 2)
    needle = Int("SearchNeedle")
    result = Int("SearchResult")
    found = Bool("SearchFound")
    return _program_with_one_rung(
        lambda: search(data.select(1, 2) == needle, result=result, found=found),
        Bool("SearchEnable"),
    )


def _shift_case() -> Program:
    bits = Block("ShiftBits", TagType.BOOL, 1, 2)
    return _program_with_one_rung(
        lambda: shift(bits.select(1, 2)).clock(Bool("ShiftClock")).reset(Bool("ShiftReset")),
        Bool("ShiftData"),
    )


def _event_drum_case() -> Program:
    output = Bool("EventDrumOutput")
    step = Int("EventDrumStep")
    done = Bool("EventDrumDone")
    jump_step = Int("EventDrumJumpStep")
    return _program_with_one_rung(
        lambda: (
            event_drum(
                outputs=[output],
                events=[Bool("EventDrumEvent1"), Bool("EventDrumEvent2")],
                pattern=[[0], [1]],
                current_step=step,
                completion_flag=done,
            )
            .reset(Bool("EventDrumReset"))
            .jump(Bool("EventDrumJump"), step=jump_step)
            .jog(Bool("EventDrumJog"))
        ),
        Bool("EventDrumAuto"),
    )


def _time_drum_case() -> Program:
    output = Bool("TimeDrumOutput")
    step = Int("TimeDrumStep")
    acc = Int("TimeDrumAcc")
    done = Bool("TimeDrumDone")
    jump_step = Int("TimeDrumJumpStep")
    preset_1 = Int("TimeDrumPreset1")
    preset_2 = Int("TimeDrumPreset2")
    return _program_with_one_rung(
        lambda: (
            time_drum(
                outputs=[output],
                presets=[preset_1, preset_2],
                pattern=[[0], [1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            )
            .reset(Bool("TimeDrumReset"))
            .jump(Bool("TimeDrumJump"), step=jump_step)
            .jog(Bool("TimeDrumJog"))
        ),
        Bool("TimeDrumAuto"),
    )


def _on_delay_case() -> Program:
    timer = Timer.clone("AnalysisTon")
    preset = Int("TonPreset")
    return _program_with_one_rung(
        lambda: on_delay(timer, preset=preset).reset(Bool("TonReset")),
        Bool("TonEnable"),
    )


def _off_delay_case() -> Program:
    timer = Timer.clone("AnalysisTof")
    preset = Int("TofPreset")
    return _program_with_one_rung(
        lambda: off_delay(timer, preset=preset),
        Bool("TofEnable"),
    )


def _count_up_case() -> Program:
    counter = Counter.clone("AnalysisCtu")
    preset = Int("CtuPreset")
    return _program_with_one_rung(
        lambda: count_up(counter, preset=preset).down(Bool("CtuDown")).reset(Bool("CtuReset")),
        Bool("CtuUp"),
    )


def _count_down_case() -> Program:
    counter = Counter.clone("AnalysisCtd")
    preset = Int("CtdPreset")
    return _program_with_one_rung(
        lambda: count_down(counter, preset=preset).reset(Bool("CtdReset")),
        Bool("CtdDown"),
    )


def _call_case() -> Program:
    gate = Bool("CallEnable")
    with Program(strict=False) as logic:
        with subroutine("worker"):
            with Rung():
                out(Bool("CallSubOutput"))
        with Rung(gate):
            call("worker")
    return logic


def _return_early_case() -> Program:
    abort = Bool("ReturnAbort")
    with Program(strict=False) as logic:
        with subroutine("guarded"):
            with Rung(abort):
                return_early()
            with Rung():
                copy(1, Int("ReturnTail"))
        with Rung():
            call("guarded")
    return logic


def _forloop_case() -> Program:
    count = Int("LoopCount")
    src = Int("LoopSource")
    dest = Int("LoopDest")
    with Program(strict=False) as logic:
        with Rung(Bool("LoopEnable")):
            with forloop(count):
                copy(src, dest)
    return logic


def _send_case() -> Program:
    src = Int("SendSource")
    sending, success, error, ex = _status("Send")
    return _program_with_one_rung(
        lambda: send(
            target="peer",
            remote_start="DS1",
            source=src,
            sending=sending,
            success=success,
            error=error,
            exception_response=ex,
        ),
        Bool("SendEnable"),
    )


def _receive_case() -> Program:
    dest = Int("ReceiveDest", min=0, max=3)
    receiving, success, error, ex = _status("Receive")
    return _program_with_one_rung(
        lambda: receive(
            target="peer",
            remote_start="DS1",
            dest=dest,
            receiving=receiving,
            success=success,
            error=error,
            exception_response=ex,
        ),
        Bool("ReceiveEnable"),
    )


_COPY_FAULTS = frozenset({"fault.address_error", "fault.out_of_range"})
_CALC_FAULTS = frozenset({"fault.division_error", "fault.out_of_range"})


GRAPH_CASES: tuple[GraphCase, ...] = (
    GraphCase("out", "out", _out_case, frozenset({"OutEnable"}), writes=frozenset({"OutLight"})),
    GraphCase(
        "latch",
        "latch",
        _latch_case,
        frozenset({"LatchEnable"}),
        writes=frozenset({"Latched"}),
    ),
    GraphCase(
        "reset",
        "reset",
        _reset_case,
        frozenset({"ResetEnable"}),
        writes=frozenset({"ResetTarget"}),
    ),
    GraphCase(
        "copy",
        "copy",
        _copy_case,
        frozenset({"CopyEnable"}),
        data_reads=frozenset({"CopySource"}),
        writes=frozenset({"CopyDest"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:string_char_fanout",
        "copy",
        _copy_string_char_fanout_case,
        frozenset({"CopyTextEnable"}),
        writes=frozenset({"CopyText1", "CopyText2", "CopyText3"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_value",
        "copy",
        _copy_to_value_case,
        frozenset({"ValueEn"}),
        data_reads=frozenset({"ValueChars1"}),
        writes=frozenset({"ValueDest"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_ascii",
        "copy",
        _copy_to_ascii_case,
        frozenset({"AsciiEn"}),
        data_reads=frozenset({"AsciiChars1"}),
        writes=frozenset({"AsciiDest"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_text",
        "copy",
        _copy_to_text_case,
        frozenset({"TextEn"}),
        data_reads=frozenset({"TextSource"}),
        writes=frozenset({"TextChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_text_unsuppressed",
        "copy",
        _copy_to_text_unsuppressed_case,
        frozenset({"UnsuppressedEn"}),
        data_reads=frozenset({"UnsuppressedSource"}),
        writes=frozenset({"UnsuppressedChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_text_exponential",
        "copy",
        _copy_to_text_exponential_case,
        frozenset({"ExpEn"}),
        data_reads=frozenset({"ExpSource"}),
        writes=frozenset({"ExpChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_text_nul",
        "copy",
        _copy_to_text_nul_case,
        frozenset({"NulEn"}),
        data_reads=frozenset({"NulSource"}),
        writes=frozenset({"NulChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_text_hex_term",
        "copy",
        _copy_to_text_hex_term_case,
        frozenset({"HexTermEn"}),
        data_reads=frozenset({"HexTermSource"}),
        writes=frozenset({"HexTermChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "copy:to_binary",
        "copy",
        _copy_to_binary_case,
        frozenset({"BinaryEn"}),
        data_reads=frozenset({"BinarySource"}),
        writes=frozenset({"BinaryChars1"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "run_function",
        "run_function",
        _run_function_case,
        frozenset({"FnEnable"}),
        data_reads=frozenset({"FnSource"}),
        writes=frozenset({"FnDest"}),
    ),
    GraphCase(
        "run_enabled_function",
        "run_enabled_function",
        _run_enabled_function_case,
        frozenset({"EnabledFnEnable"}),
        data_reads=frozenset({"EnabledFnSource"}),
        writes=frozenset({"EnabledFnDest"}),
    ),
    GraphCase(
        "blockcopy",
        "blockcopy",
        _blockcopy_case,
        frozenset({"BlockCopyEnable"}),
        data_reads=frozenset({"BlockSrc1", "BlockSrc2"}),
        writes=frozenset({"BlockDest1", "BlockDest2"}),
        implicit_writes=frozenset({"fault.out_of_range"}),
    ),
    GraphCase(
        "blockcopy:to_value",
        "blockcopy",
        _blockcopy_to_value_case,
        frozenset({"BlockValueEnable"}),
        data_reads=frozenset({"BlockValueChars1", "BlockValueChars2"}),
        writes=frozenset({"BlockValueDest1", "BlockValueDest2"}),
        implicit_writes=frozenset({"fault.out_of_range"}),
    ),
    GraphCase(
        "blockcopy:to_ascii",
        "blockcopy",
        _blockcopy_to_ascii_case,
        frozenset({"BlockAsciiEnable"}),
        data_reads=frozenset({"BlockAsciiChars1", "BlockAsciiChars2"}),
        writes=frozenset({"BlockAsciiDest1", "BlockAsciiDest2"}),
        implicit_writes=frozenset({"fault.out_of_range"}),
    ),
    GraphCase(
        "fill",
        "fill",
        _fill_case,
        frozenset({"FillEnable"}),
        data_reads=frozenset({"FillSource"}),
        writes=frozenset({"FillDest1", "FillDest2"}),
    ),
    GraphCase(
        "pack_bits",
        "pack_bits",
        _pack_bits_case,
        frozenset({"PackBitsEnable"}),
        data_reads=frozenset({"PackBits1", "PackBits2"}),
        writes=frozenset({"PackedBits"}),
    ),
    GraphCase(
        "pack_words",
        "pack_words",
        _pack_words_case,
        frozenset({"PackWordsEnable"}),
        data_reads=frozenset({"PackWords1", "PackWords2"}),
        writes=frozenset({"PackedWords"}),
    ),
    GraphCase(
        "pack_text",
        "pack_text",
        _pack_text_case,
        frozenset({"PackTextEnable"}),
        data_reads=frozenset({"PackTextChars1", "PackTextChars2"}),
        writes=frozenset({"ParsedText"}),
        implicit_writes=frozenset({"fault.out_of_range"}),
    ),
    GraphCase(
        "unpack_to_bits",
        "unpack_to_bits",
        _unpack_to_bits_case,
        frozenset({"UnpackBitsEnable"}),
        data_reads=frozenset({"UnpackBitsSource"}),
        writes=frozenset({"UnpackBits1", "UnpackBits2"}),
    ),
    GraphCase(
        "unpack_to_words",
        "unpack_to_words",
        _unpack_to_words_case,
        frozenset({"UnpackWordsEnable"}),
        data_reads=frozenset({"UnpackWordsSource"}),
        writes=frozenset({"UnpackWords1", "UnpackWords2"}),
    ),
    GraphCase(
        "calc",
        "calc",
        _calc_case,
        frozenset({"CalcEnable"}),
        data_reads=frozenset({"CalcA", "CalcB"}),
        writes=frozenset({"CalcResult"}),
        implicit_writes=_CALC_FAULTS,
    ),
    GraphCase(
        "search",
        "search",
        _search_case,
        frozenset({"SearchEnable"}),
        data_reads=frozenset({"SearchData1", "SearchData2", "SearchNeedle"}),
        writes=frozenset({"SearchResult", "SearchFound"}),
    ),
    GraphCase(
        "shift",
        "shift",
        _shift_case,
        frozenset({"ShiftData", "ShiftClock", "ShiftReset"}),
        data_reads=frozenset({"ShiftBits1", "ShiftBits2"}),
        writes=frozenset({"ShiftBits1", "ShiftBits2"}),
    ),
    GraphCase(
        "event_drum",
        "event_drum",
        _event_drum_case,
        frozenset(
            {
                "EventDrumAuto",
                "EventDrumEvent1",
                "EventDrumEvent2",
                "EventDrumReset",
                "EventDrumJump",
                "EventDrumJog",
            }
        ),
        data_reads=frozenset({"EventDrumStep", "EventDrumJumpStep"}),
        writes=frozenset({"EventDrumOutput", "EventDrumStep", "EventDrumDone"}),
    ),
    GraphCase(
        "time_drum",
        "time_drum",
        _time_drum_case,
        frozenset({"TimeDrumAuto", "TimeDrumReset", "TimeDrumJump", "TimeDrumJog"}),
        data_reads=frozenset(
            {
                "TimeDrumPreset1",
                "TimeDrumPreset2",
                "TimeDrumJumpStep",
                "TimeDrumStep",
            }
        ),
        writes=frozenset({"TimeDrumOutput", "TimeDrumStep", "TimeDrumAcc", "TimeDrumDone"}),
    ),
    GraphCase(
        "on_delay",
        "on_delay",
        _on_delay_case,
        frozenset({"TonEnable", "TonReset"}),
        data_reads=frozenset({"TonPreset"}),
        writes=frozenset(
            {
                "AnalysisTon_Done",
                "AnalysisTon_Acc",
                "AnalysisTon_EN",
                "AnalysisTon_TT",
            }
        ),
    ),
    GraphCase(
        "off_delay",
        "off_delay",
        _off_delay_case,
        frozenset({"TofEnable"}),
        data_reads=frozenset({"TofPreset"}),
        writes=frozenset(
            {
                "AnalysisTof_Done",
                "AnalysisTof_Acc",
                "AnalysisTof_EN",
                "AnalysisTof_TT",
            }
        ),
    ),
    GraphCase(
        "count_up",
        "count_up",
        _count_up_case,
        frozenset({"CtuUp", "CtuDown", "CtuReset"}),
        data_reads=frozenset({"CtuPreset"}),
        writes=frozenset(
            {
                "AnalysisCtu_Done",
                "AnalysisCtu_Acc",
                "AnalysisCtu_CU",
                "AnalysisCtu_CD",
            }
        ),
    ),
    GraphCase(
        "count_down",
        "count_down",
        _count_down_case,
        frozenset({"CtdDown", "CtdReset"}),
        data_reads=frozenset({"CtdPreset"}),
        writes=frozenset(
            {
                "AnalysisCtd_Done",
                "AnalysisCtd_Acc",
                "AnalysisCtd_CU",
                "AnalysisCtd_CD",
            }
        ),
    ),
    GraphCase(
        "call",
        "call",
        _call_case,
        frozenset({"CallEnable"}),
        calls=("worker",),
    ),
    GraphCase(
        "return_early",
        "return_early",
        _return_early_case,
        frozenset({"ReturnAbort"}),
        scope="subroutine",
        subroutine="guarded",
        rung_index=0,
    ),
    GraphCase(
        "forloop",
        "forloop",
        _forloop_case,
        frozenset({"LoopEnable"}),
        data_reads=frozenset({"LoopCount", "LoopSource"}),
        writes=frozenset({"_forloop_idx", "LoopDest"}),
        implicit_writes=_COPY_FAULTS,
    ),
    GraphCase(
        "send",
        "send",
        _send_case,
        frozenset({"SendEnable"}),
        data_reads=frozenset({"SendSource"}),
        writes=frozenset({"SendBusy", "SendSuccess", "SendError", "SendException"}),
    ),
    GraphCase(
        "receive",
        "receive",
        _receive_case,
        frozenset({"ReceiveEnable"}),
        writes=frozenset(
            {"ReceiveDest", "ReceiveBusy", "ReceiveSuccess", "ReceiveError", "ReceiveException"}
        ),
    ),
)


PUBLIC_RUNG_INSTRUCTIONS = frozenset(
    {
        "out",
        "latch",
        "reset",
        "copy",
        "run_function",
        "run_enabled_function",
        "blockcopy",
        "fill",
        "pack_bits",
        "pack_words",
        "pack_text",
        "unpack_to_bits",
        "unpack_to_words",
        "calc",
        "search",
        "call",
        "return_early",
        "count_up",
        "count_down",
        "event_drum",
        "shift",
        "on_delay",
        "off_delay",
        "time_drum",
        "forloop",
        "send",
        "receive",
    }
)


def test_graph_matrix_covers_public_rung_instruction_checklist() -> None:
    assert frozenset(case.instruction for case in GRAPH_CASES) == PUBLIC_RUNG_INSTRUCTIONS


@pytest.mark.parametrize("case", GRAPH_CASES, ids=lambda case: case.name)
def test_instruction_graph_matrix(case: GraphCase) -> None:
    graph = build_program_graph(case.build())
    idx = _node_index(graph, case)
    node = graph.rung_nodes[idx]

    assert node.condition_reads == case.condition_reads
    assert node.data_reads == case.data_reads
    assert node.writes == case.writes
    assert node.implicit_writes == case.implicit_writes
    assert node.calls == case.calls

    for tag_name in case.condition_reads | case.data_reads:
        assert idx in graph.readers_of[tag_name]
        assert tag_name in graph.def_use_chains

    for tag_name in case.writes:
        assert idx in graph.writers_of[tag_name]
        assert tag_name in graph.def_use_chains
        assert any(version.defined_at == idx for version in graph.def_use_chains[tag_name])

    for tag_name in case.implicit_writes:
        assert tag_name not in graph.writers_of
        assert tag_name in graph.tags


def test_reverse_edges_cover_supported_and_conservative_copy_family_shapes() -> None:
    a = Int("RevA")
    b = Int("RevB")
    c = Int("RevC")
    fill_src = Int("RevFillSource")
    block_a = Block("RevAData", TagType.INT, 1, 2)
    block_b = Block("RevBData", TagType.INT, 1, 2)
    chars = Block("RevChars", TagType.CHAR, 1, 2)
    converted = Int("RevConverted")
    converted_block = Block("RevConvertedBlock", TagType.INT, 1, 2)

    with Program(strict=False) as logic:
        with Rung():
            copy(a, b)
            calc(a + 5, c)
            fill(fill_src, block_a.select(1, 2))
            blockcopy(block_a.select(1, 2), block_b.select(1, 2))
            copy(chars[1], converted, convert=to_value)
            blockcopy(chars.select(1, 2), converted_block.select(1, 2), convert=to_ascii)

    edge_map = build_reverse_edge_map(logic)

    assert ("RevB",) == tuple(target for target, _invert in edge_map["RevA"] if target == "RevB")
    assert back_propagate_value(edge_map, "RevB", 9)["RevA"] == 9
    assert back_propagate_value(edge_map, "RevC", 14)["RevA"] == 9
    assert {"RevAData1", "RevAData2"} <= {target for target, _invert in edge_map["RevFillSource"]}
    assert ("RevBData1",) == tuple(
        target for target, _invert in edge_map["RevAData1"] if target == "RevBData1"
    )
    assert "RevChars1" not in edge_map
    assert "RevChars2" not in edge_map


def test_sp_values_cover_literal_tag_fill_reset_latch_and_calc_priors() -> None:
    flag = Bool("SpFlag")
    other_flag = Bool("SpOtherFlag")
    source = Int("SpSource")
    dest = Int("SpDest")
    block = Block("SpBlock", TagType.INT, 1, 2)

    with Program(strict=False) as logic:
        with Rung():
            latch(flag)
            reset(other_flag)
            copy(source, dest)
            fill(7, block.select(1, 2))
            calc(dest + 1, dest)

    from pyrung.core.crossing import Affine, Literal, StoreTransform

    rung = logic.rungs[0]
    assert _written_value_for_tag(rung, "SpFlag") == Literal(True)
    assert _written_value_for_tag(rung, "SpOtherFlag") == Literal(False)
    # copy-from-named-tag classifies as an affine pass-through (scale 1, offset 0)
    # so cause(to=)/effect(from_=) can trace the value through the copy.
    assert _written_value_for_tag(rung, "SpDest") == Affine(
        source="SpSource",
        scale=1,
        offset=0,
        storage=StoreTransform("clamp", -32768, 32767),
    )
    assert _written_value_for_tag(rung, "SpBlock1") == Literal(7)


@pytest.mark.parametrize(
    ("builder", "target", "state_tags", "label"),
    [
        (
            lambda: _program_with_one_rung(
                lambda: copy(
                    Block("WhyChars", TagType.CHAR, 1, 1)[1], Int("WhyConverted"), convert=to_value
                ),
                Bool("WhyCopyEnable"),
            ),
            "WhyConverted",
            {"WhyCopyEnable": True, "WhyChars1": "7", "WhyConverted": 7},
            "copy",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: fill(3, Block("WhyFill", TagType.INT, 1, 2).select(1, 2)),
                Bool("WhyFillEnable"),
            ),
            "WhyFill1",
            {"WhyFillEnable": True, "WhyFill1": 3},
            "fill",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: blockcopy(
                    Block("WhyBlockSrc", TagType.INT, 1, 2).select(1, 2),
                    Block("WhyBlockDest", TagType.INT, 1, 2).select(1, 2),
                ),
                Bool("WhyBlockEnable"),
            ),
            "WhyBlockDest1",
            {"WhyBlockEnable": True, "WhyBlockSrc1": 5, "WhyBlockDest1": 5},
            "blockcopy",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: pack_text(
                    Block("WhyPackText", TagType.CHAR, 1, 2).select(1, 2), Int("WhyParsed")
                ),
                Bool("WhyPackTextEnable"),
            ),
            "WhyParsed",
            {"WhyPackTextEnable": True, "WhyPackText1": "4", "WhyPackText2": "2", "WhyParsed": 42},
            "pack_text",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: unpack_to_bits(
                    Word("WhyWord"), Block("WhyBits", TagType.BOOL, 1, 2).select(1, 2)
                ),
                Bool("WhyUnpackEnable"),
            ),
            "WhyBits1",
            {"WhyUnpackEnable": True, "WhyWord": 1, "WhyBits1": True},
            "unpack_to_bits",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: (
                    shift(Block("WhyShift", TagType.BOOL, 1, 2).select(1, 2))
                    .clock(Bool("WhyShiftClock"))
                    .reset(Bool("WhyShiftReset"))
                ),
                Bool("WhyShiftData"),
            ),
            "WhyShift1",
            {
                "WhyShiftData": True,
                "WhyShiftClock": True,
                "WhyShiftReset": False,
                "WhyShift1": True,
            },
            "shift",
        ),
        (
            lambda: _program_with_one_rung(
                lambda: run_enabled_function(
                    _enabled_callback,
                    ins={"value": Int("WhyEnabledFnSource")},
                    outs={"result": Int("WhyEnabledFnDest", min=0, max=9)},
                ),
                Bool("WhyEnabledFnEnable"),
            ),
            "WhyEnabledFnDest",
            {"WhyEnabledFnEnable": True, "WhyEnabledFnSource": 6, "WhyEnabledFnDest": 6},
            "run_enabled_function",
        ),
        (
            lambda: _send_case(),
            "SendSuccess",
            {"SendEnable": True, "SendSuccess": True},
            "send",
        ),
        (
            lambda: _receive_case(),
            "ReceiveDest",
            {"ReceiveEnable": True, "ReceiveDest": 2},
            "receive",
        ),
    ],
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_causal_why_labels_instruction_writers(builder, target, state_tags, label) -> None:
    logic = builder()
    plc = PLC(logic=logic, initial_state=SystemState().with_tags(state_tags))

    result = plc.why(target)
    labels = {step.transition.tag_name: step.instruction for step in result.steps}
    assert labels[target] == label


def test_causal_why_labels_child_writer_inside_forloop() -> None:
    source = Int("WhyLoopSource")
    dest = Int("WhyLoopDest")

    with Program(strict=False) as logic:
        with Rung(Bool("WhyLoopEnable")):
            with forloop(2):
                copy(source, dest)

    state = SystemState().with_tags({"WhyLoopEnable": True, "WhyLoopSource": 8, "WhyLoopDest": 8})
    plc = PLC(logic=logic, initial_state=state)

    result = plc.why("WhyLoopDest")
    labels = {step.transition.tag_name: step.instruction for step in result.steps}
    assert labels["WhyLoopDest"] == "copy"


def test_prover_domains_cover_callbacks_receive_search_and_timer_counter_abstractions() -> None:
    callback_enable = Bool("DomainCallbackEnable", external=True)
    callback_source = Int("DomainCallbackSource", external=True, min=0, max=2)
    callback_dest = Int("DomainCallbackDest", min=0, max=2)
    receive_dest = Int("DomainReceiveDest", min=0, max=3)
    receiving, success, error, ex = _status("DomainReceive")
    data = Block("DomainSearchData", TagType.INT, 1, 2)
    search_result = Int("DomainSearchResult")
    search_found = Bool("DomainSearchFound")
    timer = Timer.clone("DomainTimer")
    counter = Counter.clone("DomainCounter")

    with Program(strict=False) as logic:
        with Rung(callback_enable):
            run_enabled_function(
                _enabled_callback,
                ins={"value": callback_source},
                outs={"result": callback_dest},
            )
        with Rung(callback_dest == 2):
            out(Bool("DomainCallbackHit"))
        with Rung(Bool("DomainReceiveEnable", external=True)):
            receive(
                target="peer",
                remote_start="DS1",
                dest=receive_dest,
                receiving=receiving,
                success=success,
                error=error,
                exception_response=ex,
            )
        with Rung():
            search(
                data.select(1, 2) == Int("DomainNeedle", external=True, min=0, max=2),
                result=search_result,
                found=search_found,
            )
        with Rung(search_found):
            out(Bool("DomainSearchHit"))
        with Rung(search_result >= 1):
            out(Bool("DomainSearchAddressed"))
        with Rung(Bool("DomainTimerEnable", external=True)):
            on_delay(timer, preset=10).reset(Bool("DomainTimerReset", external=True))
        with Rung(Bool("DomainCounterEnable", external=True)):
            count_up(counter, preset=2).reset(Bool("DomainCounterReset", external=True))

    result = _classify_dimensions(logic)
    assert not isinstance(result, Intractable), result.reason
    stateful, nondeterministic, _combinational, _done_acc, _done_presets, _done_kinds = result

    assert set(stateful["DomainCallbackDest"]) == {0, 1, 2}
    assert set(nondeterministic["DomainReceiveDest"]) == {0, 1, 2, 3}
    assert set(stateful["DomainSearchFound"]) == {False, True}
    assert set(stateful["DomainSearchResult"]) >= {-1, 1, 2}
    assert stateful["DomainTimer_Done"] == (False, PENDING, True)
    assert stateful["DomainCounter_Done"] == (False, PENDING, True)
    assert "DomainTimer_Acc" not in stateful
    assert "DomainCounter_Acc" not in stateful


def test_hidden_memory_info_tracks_shift_and_drums() -> None:
    logic = _shift_case()
    graph = build_program_graph(logic)

    hidden_writes, oneshot_writes = _collect_hidden_memory_info(logic, graph.tags)

    assert {"ShiftBits1", "ShiftBits2"} <= set(hidden_writes)
    assert oneshot_writes == ()
