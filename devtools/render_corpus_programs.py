"""The programs devtools/render_corpus.py renders: one per branch of the instruction emitters.

Each ``VARIANTS`` entry is a label and a body that adds one or more instructions inside a
rung.  The tool builds each body three ways, so every emitter meets each enable form:

    rung     under ``Rung(Enable)``, with no fault flag referenced (fault bodies are ``pass``)
    faults   the same, plus a rung reading the four fault flags the emitters set
    for      inside a ``forloop`` body, where the enable is the literal ``True``

``PROGRAMS`` entries are whole programs: rung conditions, branches, subroutines and edges.
The tool adds the facts idiom corpus and the conformance programs beside these.

A label is a file name in the render, so it never changes once written: two renders are
compared by name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from pyrung import (
    Block,
    Bool,
    Char,
    Counter,
    Dint,
    Int,
    Or,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    blockcopy,
    branch,
    break_loop,
    call,
    continue_loop,
    copy,
    count_down,
    count_up,
    debounce,
    event_drum,
    fall,
    fill,
    flasher,
    forloop,
    latch,
    math,
    named_array,
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
    time_drum,
    time_event_drum,
    timed,
    to_ascii,
    to_binary,
    to_text,
    to_value,
    toggle,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.click import ClickBlocks, ModbusAddress
from pyrung.core.instruction.send_receive import receive, send
from pyrung.core.time_mode import TimeUnitStr

Body = Callable[[], object]


_current_blocks: dict[str, Block] = {}


def _blocks() -> dict[str, Block]:
    """The blocks of the program being built.  :func:`build_variant` makes them fresh."""
    if not _current_blocks:
        _current_blocks.update(
            bits=Block("Bits", TagType.BOOL, 1, 32),
            ints=Block("Ints", TagType.INT, 1, 8),
            dints=Block("Dints", TagType.DINT, 1, 8),
            words=Block("Words", TagType.WORD, 1, 8),
            reals=Block("Reals", TagType.REAL, 1, 8),
            txt=Block("Txt", TagType.CHAR, 1, 8),
        )
    return _current_blocks


def _rows(kind: Any) -> Any:
    """A named array of two fields at stride 4.  Its ranges are not a slice of one block."""

    @named_array(kind, count=2, stride=4)
    class Row:
        first = 0
        second = 0

    return Row


def _named() -> Iterator[tuple[str, Body]]:
    yield "out(Bool row)", lambda: out(_rows(Bool).instance(1))
    # reset() of a row raises KeyError(id(block)) in _coil_reset_value, so it is left out.
    yield (
        "blockcopy(Int row reversed -> Int row)",
        lambda: _row_blockcopy(),
    )
    yield "fill(7 -> Int row)", lambda: fill(7, _rows(Int).instance(2))
    yield (
        "search(Int row >= Int)",
        lambda: search(
            _rows(Int).instance_select(1, 2) >= Int("Wanted"),
            result=Int("Where"),
            found=Bool("Found"),
        ),
    )
    yield (
        "shift(Bool row)",
        lambda: shift(_rows(Bool).instance_select(1, 2)).clock(Bool("Clock")).reset(Bool("Clear")),
    )
    yield "pack_bits(Bool row -> Int)", lambda: pack_bits(_rows(Bool).instance(1), Int("Dst"))
    yield (
        "unpack_to_bits(Int -> Bool row)",
        lambda: unpack_to_bits(Int("Src"), _rows(Bool).instance(1)),
    )
    yield "pack_words(Int row -> Dint)", lambda: pack_words(_rows(Int).instance(1), Dint("Dst"))
    yield (
        "unpack_to_words(Dint -> Int row)",
        lambda: unpack_to_words(Dint("Src"), _rows(Int).instance(1)),
    )
    yield "pack_text(Char row -> Int)", lambda: pack_text(_rows(Char).instance(1), Int("Dst"))


def _row_blockcopy() -> None:
    rows = _rows(Int)
    blockcopy(rows.instance(1).reverse(), rows.instance(2))


def _coils() -> Iterator[tuple[str, Body]]:
    for name, fn in (("out", out), ("latch", latch), ("reset", reset)):
        yield f"{name}(Bool)", lambda fn=fn: fn(Bool("Coil"))
        yield f"{name}(Bool[1..4])", lambda fn=fn: fn(_blocks()["bits"].select(1, 4))
        yield f"{name}(Bool block element)", lambda fn=fn: fn(_blocks()["bits"][3])
    yield "out(Bool, oneshot)", lambda: out(Bool("Coil"), oneshot=True)
    yield "reset(Int)", lambda: reset(Int("Value"))
    yield "reset(Char)", lambda: reset(Char("Letter"))
    yield "reset(Int[1..4])", lambda: reset(_blocks()["ints"].select(1, 4))
    yield "toggle(Bool)", lambda: toggle(Bool("Coil"))
    yield "toggle(Bool block element)", lambda: toggle(_blocks()["bits"][2])


def _timers() -> Iterator[tuple[str, Body]]:
    for label, preset in (("literal", lambda: 50), ("Int", lambda: Int("Preset"))):
        yield f"on_delay(preset {label})", lambda p=preset: on_delay_(p())
        yield (
            f"on_delay(preset {label}, reset)",
            lambda p=preset: on_delay_(p()).reset(Bool("Clear")),
        )
        yield f"off_delay(preset {label})", lambda p=preset: off_delay_(p())
    yield "on_delay(unit s)", lambda: on_delay_(Int("P"), "s")
    yield "on_delay(unit min, reset)", lambda: on_delay_(Int("P"), "min").reset(Bool("Clear"))
    yield "off_delay(unit s)", lambda: off_delay_(Int("P"), "s")


def on_delay_(preset: Any, unit: TimeUnitStr = "ms") -> Any:
    return on_delay(Timer.clone("T"), preset, unit)


def off_delay_(preset: Any, unit: TimeUnitStr = "ms") -> Any:
    return off_delay(Timer.clone("T"), preset, unit)


def _counters() -> Iterator[tuple[str, Body]]:
    for label, preset in (("literal", lambda: 5), ("Dint", lambda: Dint("Preset"))):
        yield (
            f"count_up(preset {label}, reset)",
            lambda p=preset: count_up(Counter.clone("C"), p()).reset(Bool("Clear")),
        )
        yield (
            f"count_up(preset {label}, down, reset)",
            lambda p=preset: (
                count_up(Counter.clone("C"), p()).down(Bool("Down")).reset(Bool("Clear"))
            ),
        )
        yield (
            f"count_down(preset {label}, reset)",
            lambda p=preset: count_down(Counter.clone("C"), p()).reset(Bool("Clear")),
        )
    yield (
        "count_down(preset -5, reset)",
        lambda: count_down(Counter.clone("C"), -5).reset(Bool("Clear")),
    )


_TYPES: dict[str, Callable[[str], Any]] = {
    "Int": Int,
    "Dint": Dint,
    "Real": Real,
    "Word": Word,
}


def _copies() -> Iterator[tuple[str, Body]]:
    for src_label, src in _TYPES.items():
        for dst_label, dst in _TYPES.items():
            yield (
                f"copy({src_label} -> {dst_label})",
                lambda s=src, d=dst: copy(s("Src"), d("Dst")),
            )
    for literal in (7, 70000, -5, 2.5):
        for dst_label, dst in _TYPES.items():
            yield f"copy({literal!r} -> {dst_label})", lambda v=literal, d=dst: copy(v, d("Dst"))
    yield "copy(Char -> Char)", lambda: copy(Char("Src"), Char("Dst"))
    yield "copy('A' -> Char)", lambda: copy("A", Char("Dst"))
    yield "copy('ABC' -> Char block)", lambda: copy("ABC", _blocks()["txt"][2])
    yield "copy(Int -> Int, oneshot)", lambda: copy(Int("Src"), Int("Dst"), oneshot=True)
    yield "copy(Int[ptr] -> Int)", lambda: copy(_blocks()["ints"][Int("Ptr")], Int("Dst"))
    yield "copy(Int -> Int[ptr])", lambda: copy(Int("Src"), _blocks()["ints"][Int("Ptr")])
    yield "copy(Int[ptr + 1] -> Int)", lambda: copy(_blocks()["ints"][Int("Ptr") + 1], Int("Dst"))
    yield "copy(Int -> Int[ptr + 1])", lambda: copy(Int("Src"), _blocks()["ints"][Int("Ptr") + 1])
    yield "copy(Int block -> Dint block)", lambda: copy(_blocks()["ints"][1], _blocks()["dints"][2])
    yield "copy(CLICK DS[ptr] -> DS)", lambda: _click_copy_pointer()
    for conv_label, conv in (
        ("to_value", lambda: to_value),
        ("to_ascii", lambda: to_ascii),
    ):
        yield (
            f"copy(Char -> Int, {conv_label})",
            lambda c=conv: copy(Char("Src"), Int("Dst"), convert=c()),
        )
        yield (
            f"copy(Char block -> Int block, {conv_label})",
            lambda c=conv: copy(_blocks()["txt"][1], _blocks()["ints"][1], convert=c()),
        )
        yield (
            f"copy(Char -> Int[ptr], {conv_label})",
            lambda c=conv: copy(Char("Src"), _blocks()["ints"][Int("Ptr")], convert=c()),
        )
    yield "copy(Int -> Char, to_text)", lambda: copy(Int("Src"), Char("Dst"), convert=to_text())
    yield (
        "copy(Real -> Char block, to_text exp)",
        lambda: copy(
            Real("Src"),
            _blocks()["txt"][1],
            convert=to_text(suppress_zero=False, exponential=True),
        ),
    )
    yield (
        "copy(Dint -> Char[ptr + 1], to_text)",
        lambda: copy(Dint("Src"), _blocks()["txt"][Int("Ptr") + 1], convert=to_text()),
    )
    yield "copy(Int -> Char named in sequence, to_text)", lambda: _sequential_text()
    yield "copy(CLICK TXT[ptr] -> DS, to_value)", lambda: _click_text_pointer()
    yield "copy(Int -> Char, to_binary)", lambda: copy(Int("Src"), Char("Dst"), convert=to_binary)
    yield (
        "copy(Int[ptr] -> Char, to_text)",
        lambda: copy(_blocks()["ints"][Int("Ptr")], Char("Dst"), convert=to_text()),
    )


def _sequential_text() -> None:
    copy(Int("Src"), Char("Msg1"), convert=to_text())
    copy(Char("Msg2"), Char("Seen2"))
    copy(Char("Msg3"), Char("Seen3"))


def _click_text_pointer() -> None:
    click = ClickBlocks()
    copy(click.txt[Int("Ptr")], click.ds[1], convert=to_value)


def _click_copy_pointer() -> None:
    ds = ClickBlocks().ds
    copy(ds[Int("Ptr")], ds[1])


def _block_ops() -> Iterator[tuple[str, Body]]:
    for src_label, src_type in (("Int", "ints"), ("Dint", "dints"), ("Word", "words")):
        for dst_label, dst_type in (("Int", "ints"), ("Word", "words"), ("Real", "reals")):
            yield (
                f"blockcopy({src_label}[1..3] -> {dst_label}[2..4])",
                lambda s=src_type, d=dst_type: blockcopy(
                    _blocks()[s].select(1, 3), _blocks()[d].select(2, 4)
                ),
            )
    yield (
        "blockcopy(Int[ptr..ptr] -> Int[ptr..ptr])",
        lambda: blockcopy(
            _blocks()["ints"].select(Int("A"), Int("B")),
            _blocks()["dints"].select(Int("C"), Int("D")),
        ),
    )
    yield (
        "blockcopy(Int[ptr..ptr] reversed -> Int[1..3])",
        lambda: blockcopy(
            _blocks()["ints"].select(Int("A"), Int("B")).reverse(),
            _blocks()["dints"].select(1, 3),
        ),
    )
    yield (
        "blockcopy(Char[1..3] -> Int[1..3], to_value)",
        lambda: blockcopy(
            _blocks()["txt"].select(1, 3), _blocks()["ints"].select(1, 3), convert=to_value
        ),
    )
    yield (
        "blockcopy(Char[ptr..ptr] -> Int[1..3], to_ascii)",
        lambda: blockcopy(
            _blocks()["txt"].select(Int("A"), Int("B")),
            _blocks()["ints"].select(1, 3),
            convert=to_ascii,
        ),
    )
    yield (
        "blockcopy(Int[1..2] -> Int[3..4], oneshot)",
        lambda: blockcopy(
            _blocks()["ints"].select(1, 2), _blocks()["ints"].select(3, 4), oneshot=True
        ),
    )
    for dst_label, dst_type in (
        ("Int", "ints"),
        ("Dint", "dints"),
        ("Real", "reals"),
        ("Word", "words"),
    ):
        yield f"fill(7 -> {dst_label}[1..4])", lambda d=dst_type: fill(7, _blocks()[d].select(1, 4))
        yield (
            f"fill(Dint -> {dst_label}[1..4])",
            lambda d=dst_type: fill(Dint("Value"), _blocks()[d].select(1, 4)),
        )
    yield "fill(2.5 -> Real[1..4])", lambda: fill(2.5, _blocks()["reals"].select(1, 4))
    yield "fill('A' -> Char[1..4])", lambda: fill("A", _blocks()["txt"].select(1, 4))
    yield (
        "fill(Int -> Int[1..end])",
        lambda: fill(Int("Value"), _blocks()["ints"].select(1, Int("End"))),
    )


def _maths() -> Iterator[tuple[str, Body]]:
    for dst_label, dst in {**_TYPES, "Bool": Bool}.items():
        yield (
            f"math(Int * 2 + Int -> {dst_label})",
            lambda d=dst: math(Int("A") * 2 + Int("B"), d("Dst")),
        )
        yield f"math(Int / Int -> {dst_label})", lambda d=dst: math(Int("A") / Int("B"), d("Dst"))
    yield "math(Word | Word -> Word, hex)", lambda: math(Word("A") | Word("B"), Word("Dst"))
    yield "math(7 -> Int)", lambda: math(7, Int("Dst"))
    yield "math(Int[ptr] -> Int)", lambda: math(_blocks()["ints"][Int("Ptr")], Int("Dst"))
    yield "math(Int[ptr + 1] -> Int)", lambda: math(_blocks()["ints"][Int("Ptr") + 1], Int("Dst"))
    yield "math(Int + 1 -> Int, oneshot)", lambda: math(Int("A") + 1, Int("Dst"), oneshot=True)
    yield "math(Real ** Real -> Real)", lambda: math(Real("A") ** Real("B"), Real("Dst"))
    yield "math(Int % Int -> Int)", lambda: math(Int("A") % Int("B"), Int("Dst"))


def _compare(left: Any, op: str, right: Any) -> Any:
    import operator

    ops = {
        "==": operator.eq,
        "!=": operator.ne,
        "<": operator.lt,
        "<=": operator.le,
        ">": operator.gt,
        ">=": operator.ge,
    }
    return ops[op](left, right)


def _searches() -> Iterator[tuple[str, Body]]:
    for op in ("==", "!=", "<", "<=", ">", ">="):
        for value_label, value in (("literal", lambda: 3), ("Int", lambda: Int("Wanted"))):
            yield (
                f"search(Int[1..6] {op} {value_label})",
                lambda o=op, v=value: search(
                    _compare(_blocks()["ints"].select(1, 6), o, v()),
                    result=Int("Where"),
                    found=Bool("Found"),
                ),
            )
    yield (
        "search(Int[1..6] == Int, continuous)",
        lambda: search(
            _blocks()["ints"].select(1, 6) == Int("Wanted"),
            result=Int("Where"),
            found=Bool("Found"),
            continuous=True,
        ),
    )
    yield (
        "search(Int[ptr..ptr] >= Int, continuous)",
        lambda: search(
            _blocks()["ints"].select(Int("A"), Int("B")) >= Int("Wanted"),
            result=Int("Where"),
            found=Bool("Found"),
            continuous=True,
        ),
    )
    yield (
        "search(Real[1..6] >= Real)",
        lambda: search(
            _blocks()["reals"].select(1, 6) >= Real("Wanted"),
            result=Dint("Where"),
            found=Bool("Found"),
        ),
    )
    for op in ("==", "!="):
        yield (
            f"search(Char[1..6] {op} 'AB')",
            lambda o=op: search(
                _compare(_blocks()["txt"].select(1, 6), o, "AB"),
                result=Int("Where"),
                found=Bool("Found"),
            ),
        )
    yield (
        "search(Char[1..6] == Char)",
        lambda: search(
            _blocks()["txt"].select(1, 6) == Char("Wanted"),
            result=Int("Where"),
            found=Bool("Found"),
            continuous=True,
        ),
    )
    yield (
        "search(Char[ptr..ptr] == 'AB')",
        lambda: search(
            _blocks()["txt"].select(Int("A"), Int("B")) == "AB",
            result=Int("Where"),
            found=Bool("Found"),
        ),
    )
    yield (
        "search(Char[1..2] == 'ABC')",
        lambda: search(
            _blocks()["txt"].select(1, 2) == "ABC",
            result=Int("Where"),
            found=Bool("Found"),
        ),
    )
    yield (
        "search(Int[1..6] == Int, oneshot)",
        lambda: search(
            _blocks()["ints"].select(1, 6) == Int("Wanted"),
            result=Int("Where"),
            found=Bool("Found"),
            oneshot=True,
        ),
    )


def _shifts() -> Iterator[tuple[str, Body]]:
    yield (
        "shift(Bool[1..4])",
        lambda: shift(_blocks()["bits"].select(1, 4)).clock(Bool("Clock")).reset(Bool("Clear")),
    )
    yield (
        "shift(Bool[ptr..ptr])",
        lambda: (
            shift(_blocks()["bits"].select(Int("A"), Int("B")))
            .clock(Bool("Clock"))
            .reset(Bool("Clear"))
        ),
    )
    yield (
        "shift(Bool[1..4], edge clock)",
        lambda: (
            shift(_blocks()["bits"].select(1, 4)).clock(rise(Bool("Clock"))).reset(Bool("Clear"))
        ),
    )


def _outputs() -> list[Any]:
    return [Bool("A"), Bool("B")]


_PATTERN = [[1, 0], [0, 1], [1, 1]]


def _drums() -> Iterator[tuple[str, Body]]:
    def events() -> list[Any]:
        return [Bool("E1"), Bool("E2"), Bool("E3")]

    yield (
        "event_drum(3 steps)",
        lambda: event_drum(
            outputs=_outputs(),
            events=events(),
            pattern=_PATTERN,
            current_step=Int("Step"),
            completion_flag=Bool("Done"),
        ).reset(Bool("Clear")),
    )
    yield (
        "event_drum(3 steps, jump, jog)",
        lambda: (
            event_drum(
                outputs=_outputs(),
                events=events(),
                pattern=_PATTERN,
                current_step=Int("Step"),
                completion_flag=Bool("Done"),
            )
            .reset(Bool("Clear"))
            .jump(Bool("Jump"), step=Int("JumpTo"))
            .jog(Bool("Jog"))
        ),
    )
    for presets_label, presets in (
        ("literal presets", lambda: [100, 200, 80]),
        ("Int presets", lambda: [Int("P1"), Int("P2"), Int("P3")]),
    ):
        yield (
            f"time_drum(3 steps, {presets_label})",
            lambda p=presets: time_drum(
                outputs=_outputs(),
                presets=p(),
                pattern=_PATTERN,
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Clear")),
        )
    yield (
        "time_drum(3 steps, Dint acc, jump, jog)",
        lambda: (
            time_drum(
                outputs=_outputs(),
                presets=[10, 20, 30],
                pattern=_PATTERN,
                current_step=Int("Step"),
                accumulator=Dint("Acc"),
                completion_flag=Bool("Done"),
            )
            .reset(Bool("Clear"))
            .jump(Bool("Jump"), step=3)
            .jog(Bool("Jog"))
        ),
    )
    yield (
        "time_event_drum(mixed steps)",
        lambda: time_event_drum(
            outputs=_outputs(),
            presets=[Int("P1"), None, 80],
            events=[None, Bool("E2"), Bool("E3")],
            pattern=_PATTERN,
            current_step=Int("Step"),
            accumulator=Int("Acc"),
            completion_flag=Bool("Done"),
        ).reset(Bool("Clear")),
    )
    yield (
        "time_event_drum(every step timed)",
        lambda: (
            time_event_drum(
                outputs=_outputs(),
                presets=[10, 20, 30],
                events=[Bool("E1"), None, Bool("E3")],
                pattern=_PATTERN,
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            )
            .reset(Bool("Clear"))
            .jog(Bool("Jog"))
        ),
    )


def _coil_variants() -> Iterator[tuple[str, Body]]:
    yield (
        "debounce(Int, literal)",
        lambda: debounce(
            Bool("Coil"), on=(Timer.clone("On"), Int("OnP")), off=(Timer.clone("Off"), 20)
        ),
    )
    yield (
        "flasher(Int, literal)",
        lambda: flasher(
            Bool("Coil"), on=(Timer.clone("On"), Int("OnP")), off=(Timer.clone("Off"), 20)
        ),
    )
    yield "timed(Int)", lambda: timed(Bool("Coil"), Timer.clone("T"), Int("P"))
    yield (
        "timed(literal, maintained)",
        lambda: timed(Bool("Coil"), Timer.clone("T"), 30, maintained=True),
    )


def _packing() -> Iterator[tuple[str, Body]]:
    for dst_label, dst in (("Int", Int), ("Word", Word), ("Dint", Dint), ("Real", Real)):
        width = 16 if dst_label in ("Int", "Word") else 32
        yield (
            f"pack_bits(Bool[1..{width}] -> {dst_label})",
            lambda d=dst, w=width: pack_bits(_blocks()["bits"].select(1, w), d("Dst")),
        )
        yield (
            f"unpack_to_bits({dst_label} -> Bool[1..{width}])",
            lambda d=dst, w=width: unpack_to_bits(d("Src"), _blocks()["bits"].select(1, w)),
        )
    yield (
        "pack_bits(Bool[ptr..ptr] -> Int)",
        lambda: pack_bits(_blocks()["bits"].select(Int("A"), Int("B")), Int("Dst")),
    )
    yield (
        "unpack_to_bits(Int -> Bool[ptr..ptr])",
        lambda: unpack_to_bits(Int("Src"), _blocks()["bits"].select(Int("A"), Int("B"))),
    )
    for src_label, src_type in (("Word", "words"), ("Int", "ints")):
        for dst_label, dst in (("Dint", Dint), ("Real", Real)):
            yield (
                f"pack_words({src_label}[1..2] -> {dst_label})",
                lambda s=src_type, d=dst: pack_words(_blocks()[s].select(1, 2), d("Dst")),
            )
            yield (
                f"unpack_to_words({dst_label} -> {src_label}[1..2])",
                lambda s=src_type, d=dst: unpack_to_words(d("Src"), _blocks()[s].select(1, 2)),
            )
    yield (
        "pack_words(Word[ptr..ptr] -> Dint)",
        lambda: pack_words(_blocks()["words"].select(Int("A"), Int("B")), Dint("Dst")),
    )
    yield (
        "unpack_to_words(Dint -> Word[ptr..ptr])",
        lambda: unpack_to_words(Dint("Src"), _blocks()["words"].select(Int("A"), Int("B"))),
    )
    for dst_label, dst in (("Int", Int), ("Dint", Dint), ("Word", Word), ("Real", Real)):
        yield (
            f"pack_text(Char[1..4] -> {dst_label})",
            lambda d=dst: pack_text(_blocks()["txt"].select(1, 4), d("Dst")),
        )
    yield (
        "pack_text(Char[1..4] -> Int, allow_whitespace)",
        lambda: pack_text(_blocks()["txt"].select(1, 4), Int("Dst"), allow_whitespace=True),
    )
    yield (
        "pack_text(Char[ptr..ptr] -> Dint)",
        lambda: pack_text(_blocks()["txt"].select(Int("A"), Int("B")), Dint("Dst")),
    )
    yield (
        "pack_bits(CLICK C[1..16] -> DS)",
        lambda: _click_pack(),
    )


def _click_pack() -> None:
    click = ClickBlocks()
    pack_bits(click.c.select(1, 16), click.ds[1])
    unpack_to_bits(click.ds[2], click.c.select(17, 32))


def _double(x: int) -> dict[str, int]:
    return {"y": x * 2}


def _gated(enabled: bool, x: int) -> dict[str, int]:
    return {"y": x if enabled else 0}


def _control() -> Iterator[tuple[str, Body]]:
    yield (
        "run_function(Int -> Int)",
        lambda: run_function(_double, ins={"x": Int("X")}, outs={"y": Int("Y")}),
    )
    yield (
        "run_enabled_function(Int -> Int)",
        lambda: run_enabled_function(_gated, ins={"x": Int("X")}, outs={"y": Int("Y")}),
    )


def _modbus() -> Iterator[tuple[str, Body]]:
    yield (
        "send(Int[1..2])",
        lambda: send(
            target="peer",
            remote_start="DS1",
            source=_blocks()["ints"].select(1, 2),
            sending=Bool("Busy"),
            success=Bool("Ok"),
            error=Bool("Err"),
            exception_response=Int("Code"),
        ),
    )
    yield (
        "receive(Int)",
        lambda: receive(
            target="peer",
            remote_start="DS1",
            dest=Int("Dst"),
            receiving=Bool("Busy"),
            success=Bool("Ok"),
            error=Bool("Err"),
            exception_response=Int("Code"),
        ),
    )


def _modbus_raw() -> Iterator[tuple[str, Body]]:
    yield (
        "send(Int, raw address)",
        lambda: send(
            target="peer",
            remote_start=ModbusAddress(0x100),
            source=Int("Src"),
            sending=Bool("Busy"),
            success=Bool("Ok"),
            error=Bool("Err"),
            exception_response=Int("Code"),
        ),
    )
    yield (
        "receive(Bool[1..4], raw coils)",
        lambda: receive(
            target="peer",
            remote_start=ModbusAddress(1),
            dest=_blocks()["bits"].select(1, 4),
            receiving=Bool("Busy"),
            success=Bool("Ok"),
            error=Bool("Err"),
            exception_response=Int("Code"),
        ),
    )


def variants() -> list[tuple[str, Body]]:
    """Every body, in a fixed order."""
    groups = (
        _coils,
        _timers,
        _counters,
        _copies,
        _block_ops,
        _maths,
        _searches,
        _shifts,
        _drums,
        _coil_variants,
        _packing,
        _control,
        _modbus,
        _modbus_raw,
        _named,
    )
    return [variant for group in groups for variant in group()]


def _fault_rung() -> None:
    faults = (
        system.fault.out_of_range,
        system.fault.address_error,
        system.fault.division_error,
        system.fault.math_operation_error,
    )
    with Rung(Or(*faults)):
        out(Bool("SawFault"))


def build_variant(body: Body, flavor: str) -> Program:
    """*body* in a program of *flavor* (``rung``, ``faults`` or ``for``)."""
    _current_blocks.clear()
    with Program(strict=False) as program:
        with Rung(Bool("Enable")):
            if flavor == "for":
                with forloop(3):
                    body()
            else:
                body()
        if flavor != "rung":
            _fault_rung()
    return program


FLAVORS = ("rung", "faults", "for")


# -- Whole programs ----------------------------------------------------------


def _conditions() -> Program:
    ints = Block("Ints", TagType.INT, 1, 8)
    with Program(strict=False) as program:
        with Rung(Bool("A"), ~Bool("B"), Int("Truthy")):
            out(Bool("Y1"))
        with Rung(Or(Bool("A"), rise(Bool("B")), fall(Bool("C")))):
            out(Bool("Y2"))
        for i, op in enumerate(("==", "!=", "<", "<=", ">", ">=")):
            with Rung(_compare(Int("X"), op, Int("Y"))):
                out(Bool(f"Cmp{i}"))
            with Rung(_compare(ints[Int("Ptr")], op, 3)):
                out(Bool(f"Ind{i}"))
            with Rung(_compare(Int("X") + 1, op, Int("Y") * 2)):
                out(Bool(f"Expr{i}"))
        with Rung(ints[Int("Ptr") + 1] > Int("Y")):
            out(Bool("IndExpr"))
        with Rung(Real("R") > 1.5, Dint("D") <= Int("X")):
            out(Bool("Mixed"))
    return program


def _branches() -> Program:
    with Program(strict=False) as program:
        with Rung(Bool("Start")):
            out(Bool("Main"))
            with branch(Bool("B1"), rise(Bool("E1"))):
                out(Bool("Y1"))
                with branch(~Bool("B2")):
                    latch(Bool("Y2"))
            with branch(fall(Bool("E2"))):
                on_delay_(20)
        with Rung(Bool("Run")):
            on_delay_(20).reset(rise(Bool("Clear")))
        with Rung(Bool("Count")):
            count_up(Counter.clone("C"), 3).down(fall(Bool("Down"))).reset(Bool("Clear"))
        with Rung(rise(Bool("Start")), Bool("Main")):
            copy(1, Int("Seen"))
    return program


def _subroutines() -> Program:
    with Program(strict=False) as program:
        with Rung(Bool("Enable")):
            call("Sub")
        with Rung():
            call("Sub")
        with subroutine("Sub"):
            with Rung(Bool("Leave")):
                return_early()
            with Rung(Bool("A")):
                out(Bool("Y"))
            with Rung():
                return_early()
    return program


def _loops() -> Program:
    total = Int("Total")
    with Program(strict=False) as program:
        with Rung(Bool("Enable")):
            with forloop(Int("Count")):
                math(total + 1, total)
                with Rung(Bool("Stop")):
                    break_loop()
                with Rung(Bool("Skip")):
                    continue_loop()
        with Rung(Bool("Enable")):
            with forloop(Dint("Big")):
                math(total + 1, total)
                with Rung(Bool("Skip")):
                    continue_loop()
        with Rung(Bool("Enable")):
            with forloop(Real("RealCount")):
                math(total + 1, total)
        with Rung(Bool("Enable")):
            with forloop(70000):
                math(total + 1, total)
        with Rung(Bool("Enable")):
            with forloop(Int("Count"), oneshot=True):
                copy(1, Int("Once"))
        _fault_rung()
    return program


def programs() -> list[tuple[str, Callable[[], Program]]]:
    """Whole programs, in a fixed order."""
    return [
        ("conditions", _conditions),
        ("branches", _branches),
        ("subroutines", _subroutines),
        ("loops", _loops),
    ]
