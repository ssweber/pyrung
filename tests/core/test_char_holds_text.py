"""A Char tag holds one character with a code of 0..255, whatever instruction writes it.

``check()`` and the how() prior compare a Char tag's values as text, so a number or a bit
in a Char tag makes them raise.  Each case here is refused when the program is built, or
leaves text in every Char tag after two scans.  The scans run on the interpreter, the
compiled kernel and the generated CircuitPython, and all three must agree.  ``check()``
and the how() prior then run on the program.

Three lists keep the cases complete:

- every write field of every instruction class, from the walker in
  ``test_flagged_writes_every_instruction.py``;
- every kind of operand that the builders parse (``Value``, ``Span`` and ``Dest``);
- every converter that a builder takes.
"""

from __future__ import annotations

import asyncio
import math
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, get_args

import pytest
from pyclickplc.server import ClickServer, MemoryDataProvider

from pyrung.circuitpy import P1AM, RunStopConfig, generate_circuitpy
from pyrung.circuitpy.codegen._helpers import _HELPER_DEFS
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    CompiledPLC,
    Int,
    Program,
    TagType,
    blockcopy,
    copy,
    fill,
    out,
    run_enabled_function,
    run_function,
    rung,
    to_ascii,
    to_binary,
    to_text,
    to_value,
)
from pyrung.core.analysis.prove import _build_prior_facts
from pyrung.core.instruction.operands import Dest, Span, Value, parse_value
from pyrung.core.instruction.send_receive import (
    ModbusAddress,
    ModbusTcpTarget,
    RegisterType,
    receive,
    send,
)
from pyrung.core.instruction.send_receive import _core as modbus_core
from pyrung.core.instruction.send_receive._core import _RequestResult
from pyrung.core.storage import CHAR_CODE_MAX, StoreTransform, store_value
from tests.circuitpy.test_codegen import (
    _context_for_program,
    _IdleBase,
    _run_single_scan_source,
)
from tests.circuitpy.test_modbus_codegen import _RAW_TARGET, _mbap, _run_raw_client_job
from tests.validators import test_flagged_writes_every_instruction as walker

# --- The program around each case -------------------------------------------------------


def _hardware() -> P1AM:
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    return hw


def _char_tags(program: Program) -> list[Any]:
    """Return every Char tag that *program* reads or writes."""
    tags = _context_for_program(program, _hardware()).referenced_tags.values()
    return sorted((tag for tag in tags if tag.type is TagType.CHAR), key=lambda tag: tag.name)


def _generated_scans(program: Program, monkeypatch: pytest.MonkeyPatch, scans: int) -> dict:
    """Run *scans* scans of the generated CircuitPython and return its namespace."""
    result = generate_circuitpy(
        program,
        _hardware(),
        target_scan_ms=10.0,
        runstop=RunStopConfig(debounce_ms=0, run_when_high=False),
    )
    return _run_single_scan_source(
        result.code, monkeypatch, _IdleBase(), runtime_source=result.runtime, scans=scans
    )


def _on_every_backend(
    program: Program, monkeypatch: pytest.MonkeyPatch, watch: list[Any]
) -> dict[str, tuple]:
    """Return *watch*'s values after two scans, keyed by backend."""
    results: dict[str, tuple] = {}
    for name, cls in (("interpreted", PLC), ("compiled", CompiledPLC)):
        plc = cls(program)
        plc.step()
        plc.step()
        tags = plc.current_state.tags
        results[name] = tuple(tags.get(tag.name, tag.default) for tag in watch)
    namespace = _generated_scans(program, monkeypatch, scans=2)
    ctx = _context_for_program(program, _hardware())
    # A block element's symbol is an index into the block's list, such as "_b_T[1]".
    results["circuitpy"] = tuple(eval(ctx.symbol_for_tag(tag), namespace) for tag in watch)
    return results


def _build(write: Callable[[], Any], compared: Any) -> Program:
    """Return a program of *write* and a rung that compares the Char tag *compared*.

    Without the comparison, ``check()`` and the prior never sort the Char tag's values.
    """
    with Program(strict=False) as program:
        write()
        with rung(compared == Char("Other", default="Q")):
            out(Bool("Same"))
    return program


def _holds_text(program: Program, monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert that every Char tag holds one 8-bit character on every backend after two scans."""
    watch = _char_tags(program)
    results = _on_every_backend(program, monkeypatch, watch)
    for backend, values in results.items():
        for tag, value in zip(watch, values, strict=True):
            assert isinstance(value, str) and len(value) == 1, (backend, tag.name, value)
            assert ord(value) <= CHAR_CODE_MAX, (backend, tag.name, value)
    assert len(set(results.values())) == 1, results
    program.check()
    _build_prior_facts(program)


# --- The guard in each runtime's plain Char store ----------------------------------------


@pytest.mark.parametrize("value", [5, True, float("inf"), None, "AB", "\u20ac"], ids=repr)
def test_a_plain_char_store_raises_on_a_value_that_no_char_holds(value: Any) -> None:
    """Only a write that skipped the build rules reaches the guard, so the sweeps below never
    trip it.  A float Inf would otherwise store 0."""
    with pytest.raises(RuntimeError, match="This is a bug in pyrung"):
        store_value(value, StoreTransform("char"))
    namespace: dict[str, Any] = {"math": math, "_store_real": float}
    exec("\n".join(_HELPER_DEFS["_store_copy_value_to_type"]), namespace)  # noqa: S102
    with pytest.raises(RuntimeError, match="This is a bug in pyrung"):
        namespace["_store_copy_value_to_type"](value, "CHAR")
    assert namespace["_store_copy_value_to_type"]("", "CHAR") == "\x00"
    assert namespace["_store_copy_value_to_type"]("\xff", "CHAR") == "\xff"


@pytest.mark.parametrize(
    ("default", "error"),
    [(5, TypeError), (True, TypeError), ("AB", ValueError), ("\u20ac", ValueError)],
    ids=repr,
)
def test_a_char_default_is_one_character(default: Any, error: type[Exception]) -> None:
    with pytest.raises(error, match="holds one character with a code of 0..255"):
        Char("Default", default=default)
    assert Char("Blank", default="").default == "\x00"
    assert Char("Top", default="\xff").default == "\xff"


@pytest.mark.parametrize(
    "configure, error",
    [
        pytest.param(lambda txt: txt.slot(1, default=5), TypeError, id="slot"),
        pytest.param(lambda txt: (txt[1], txt.slot(1, default=5)), TypeError, id="slot-after-tag"),
        pytest.param(lambda txt: txt.slot(1, 3, default=7), TypeError, id="slot-range"),
        pytest.param(
            lambda txt: (txt[2], txt.slot(1, 3, default=7)), TypeError, id="range-after-tag"
        ),
        pytest.param(lambda txt: txt.slot(1, default="AB"), ValueError, id="slot-two"),
    ],
)
def test_a_slot_default_is_one_character(
    configure: Callable[[Block], Any], error: type[Exception]
) -> None:
    # slot() raises when it is called, before a later txt[1] makes the tag.
    txt = Block("TxtSlot", TagType.CHAR, 1, 4)
    with pytest.raises(error, match=r"Slot TxtSlot\[\d\] got the default"):
        configure(txt)
    txt.slot(1, default="A")
    assert txt[1].default == "A"


@pytest.mark.parametrize("made_first", [False, True], ids=["slot-first", "tag-first"])
def test_a_slot_default_of_none_gives_the_blank(made_first: bool) -> None:
    txt = Block("TxtNone", TagType.CHAR, 1, 4)
    if made_first:
        txt[1]
        txt[2]
    txt.slot(1, default=None)
    txt.slot(2, 3, default=None)
    assert [txt[i].default for i in (1, 2, 3)] == ["\x00"] * 3


@pytest.mark.parametrize("seed, error", [(5, TypeError), ("AB", ValueError)])
def test_a_block_factory_default_is_one_character(seed: Any, error: type[Exception]) -> None:
    txt = Block("TxtFactory", TagType.CHAR, 1, 4, default_factory=lambda addr: seed)
    with pytest.raises(error, match="holds one character with a code of 0..255"):
        txt[1]


# --- Every write field ------------------------------------------------------------------

#: The write fields that build with a Char tag.  Every other field refuses one.  The
#: status bits of a timer or a counter build, and the instruction never writes the Char.
BUILDS_WITH_CHAR = {
    "ResetInstruction.target",
    "OnDelayInstruction.en_bit",
    "OnDelayInstruction.tt_bit",
    "OffDelayInstruction.en_bit",
    "OffDelayInstruction.tt_bit",
    "CountUpInstruction.cu_bit",
    "CountUpInstruction.cd_bit",
    "CountDownInstruction.cu_bit",
    "CountDownInstruction.cd_bit",
    "CopyInstruction.dest",
    "FillInstruction.dest",
    "FunctionCallInstruction._outs",
    "EnabledFunctionCallInstruction._outs",
}

#: The fields whose write raises during the scan.  The walker's function returns 1, which a
#: Char tag cannot hold.
RAISES_AT_SCAN = {"FunctionCallInstruction._outs", "EnabledFunctionCallInstruction._outs"}


def _char_destination(case: walker.Case) -> Any:
    return Block("W", TagType.CHAR, 1, 4) if case.block else Char("W")


def _compared(destination: Any) -> Any:
    return destination[1] if isinstance(destination, Block) else destination


def test_every_write_field_of_every_instruction_has_a_case() -> None:
    # The walker's own test checks that its cases cover every declared write field.
    walked = {(case.instruction, case.field) for case in walker.CASES}
    declared = {
        (cls.__name__, field)
        for cls in walker._concrete_instruction_classes()
        for field in (*cls._writes, *getattr(cls, "_status_fields", ()))
    }
    assert walked == declared - walker._NOT_A_USER_TAG
    assert BUILDS_WITH_CHAR <= {case.id for case in walker.ALL_CASES}


@pytest.mark.parametrize("case", walker.ALL_CASES, ids=lambda case: case.id)
def test_a_char_destination_is_refused_or_holds_text(
    case: walker.Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The walker gates each rung on an enable that is off.  Turn it on, so the write runs.
    monkeypatch.setattr(walker, "_en", lambda name: Bool(f"{name}_En", default=True))
    destination = _char_destination(case)
    if case.id not in BUILDS_WITH_CHAR:
        with pytest.raises((TypeError, ValueError)):
            _build(lambda: case.build(destination), _compared(destination))
        return
    program = _build(lambda: case.build(destination), _compared(destination))
    if case.id in RAISES_AT_SCAN:
        _raises_on_every_backend(program, monkeypatch, TypeError, "returned 1 for 'result'")
        return
    _holds_text(program, monkeypatch)


def _raises_on_every_backend(
    program: Program, monkeypatch: pytest.MonkeyPatch, error: type[Exception], message: str
) -> None:
    """Assert that the first scan raises *error* with *message* on every backend."""
    for cls in (PLC, CompiledPLC):
        plc = cls(program)
        with pytest.raises(error, match=message):
            plc.step()
    with pytest.raises(error, match=message):
        _generated_scans(program, monkeypatch, scans=1)


# --- Every kind of source ---------------------------------------------------------------

_SOURCE_TYPES = (TagType.BOOL, TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL, TagType.CHAR)
_SEEDS = {
    TagType.BOOL: True,
    TagType.INT: 7,
    TagType.DINT: 70000,
    TagType.WORD: 0x00AB,
    TagType.REAL: 2.5,
    TagType.CHAR: "7",
}


@dataclass(frozen=True)
class Source:
    """One source for a write into Char tags, made fresh for each program."""

    name: str
    make: Callable[[], Any]


def _block(tag_type: TagType) -> Block:
    """Return a block of *tag_type* whose every element starts at a value other than 0."""
    seed = _SEEDS[tag_type]
    return Block(f"S{tag_type.name}", tag_type, 1, 4, default_factory=lambda addr: seed)


def _first() -> Int:
    return Int("First", default=2)


def _last() -> Int:
    return Int("Last", default=3)


def _shapes(tag_type: TagType) -> list[Source]:
    name = tag_type.name.capitalize()
    return [
        Source(f"{name} tag", lambda: _block(tag_type)[2]),
        Source(f"{name} pointer", lambda: _block(tag_type)[_first()]),
        Source(f"{name} range", lambda: _block(tag_type).select(2, 3)),
        Source(f"{name} pointer range", lambda: _block(tag_type).select(_first(), _last())),
    ]


SOURCES = [
    *(source for tag_type in _SOURCE_TYPES for source in _shapes(tag_type)),
    *(
        Source(repr(value), lambda value=value: value)
        for value in (True, 5, 12, 1.5, "A", "ABC", "", "\xe9", "\u20ac", "A\u20ac")
    ),
    Source("Int expression", lambda: _block(TagType.INT)[2] + 1),
    Source("Real expression", lambda: _block(TagType.REAL)[2] * 2),
]

_COPY_CONVERTERS = {
    "plain": None,
    "to_text()": to_text(),
    "to_text(exponential=True)": to_text(exponential=True),
    "to_binary": to_binary,
    "to_value": to_value,
    "to_ascii": to_ascii,
}
_BLOCK_COPY_CONVERTERS = {"plain": None, "to_value": to_value, "to_ascii": to_ascii}


def _txt() -> Block:
    return Block("T", TagType.CHAR, 1, 8, default_factory=lambda addr: "Z")


def _copy(source: Any, convert: Any, txt: Block) -> None:
    with rung():
        copy(source, txt[1], convert=convert)


def _fill(source: Any, convert: Any, txt: Block) -> None:
    with rung():
        fill(source, txt.select(1, 3))


def _blockcopy(source: Any, convert: Any, txt: Block) -> None:
    with rung():
        blockcopy(source, txt.select(1, 2), convert=convert)


WRITES = [
    *(("copy", name, _copy, convert) for name, convert in _COPY_CONVERTERS.items()),
    ("fill", "plain", _fill, None),
    *(("blockcopy", name, _blockcopy, convert) for name, convert in _BLOCK_COPY_CONVERTERS.items()),
]

_NUMBER_REGISTERS = (
    "Int tag",
    "Int pointer",
    "Dint tag",
    "Dint pointer",
    "Word tag",
    "Word pointer",
)
_TO_TEXT = (*_NUMBER_REGISTERS, "Real tag", "Real pointer", "5", "12", "1.5")

#: The sources that build for each write, so a rule that refuses too much fails here.  The
#: build refuses every other pair in the sweep.
BUILDS = {
    "copy-plain": {
        *_TO_TEXT,
        "Char tag",
        "Char pointer",
        "'A'",
        "'ABC'",
        "''",
        repr("\xe9"),
    },
    "copy-to_text()": set(_TO_TEXT),
    "copy-to_text(exponential=True)": set(_TO_TEXT),
    "copy-to_binary": {*_NUMBER_REGISTERS, "5", "12"},
    "fill-plain": {"Char tag", "Char pointer", "True", "5", "'A'", "''", repr("\xe9")},
    "blockcopy-plain": {"Char range", "Char pointer range"},
}


def test_the_sources_cover_every_kind_of_parsed_operand() -> None:
    kinds = {type(parse_value(source.make())) for source in SOURCES}
    assert kinds == set(get_args(Value | Span | Dest))


@pytest.mark.parametrize("source", SOURCES, ids=lambda source: source.name)
@pytest.mark.parametrize(
    "name, write, convert",
    [(f"{w[0]}-{w[1]}", w[2], w[3]) for w in WRITES],
    ids=[f"{w[0]}-{w[1]}" for w in WRITES],
)
def test_a_source_into_char_tags_is_refused_or_stores_text(
    name: str,
    write: Callable[[Any, Any, Block], None],
    convert: Any,
    source: Source,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    txt = _txt()
    if source.name not in BUILDS.get(name, set()):
        with pytest.raises((TypeError, ValueError)):
            _build(lambda: write(source.make(), convert, txt), txt[1])
        return
    program = _build(lambda: write(source.make(), convert, txt), txt[1])
    _holds_text(program, monkeypatch)


def test_every_pair_that_builds_is_in_the_sweep() -> None:
    names = {f"{w[0]}-{w[1]}" for w in WRITES}
    sources = {source.name for source in SOURCES}
    assert set(BUILDS) <= names
    assert set().union(*BUILDS.values()) <= sources


def test_a_fill_of_more_than_one_character_is_refused() -> None:
    # CLICK's editor refuses it: "Only one character is allowed in the Source" (OBJ-023).
    with pytest.raises(ValueError, match=r"fill\(\) writes one character into every Char tag"):
        _build(lambda: _fill("AB", None, _txt()), _txt()[1])


@pytest.mark.parametrize("write", [_copy, _fill], ids=["copy", "fill"])
def test_text_past_code_255_is_refused(write: Callable[[Any, Any, Block], None]) -> None:
    with pytest.raises(ValueError, match="'\u20ac' is past 255"):
        _build(lambda: write("\u20ac", None, _txt()), _txt()[1])


def test_a_copy_of_text_writes_one_character_into_each_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    txt = _txt()
    with Program(strict=False) as program:
        with rung():
            copy("AB\xe9", txt[1])
        # The generated code holds only the tags that a rung reads or writes.
        with rung(txt[2] == txt[3], txt[4] == "Z"):
            out(Bool("Same"))
    results = _on_every_backend(program, monkeypatch, [txt[1], txt[2], txt[3], txt[4]])
    assert results == {name: ("A", "B", "\xe9", "Z") for name in results}


# --- Receive ----------------------------------------------------------------------------

_CLICK_BANKS = (
    "X1",
    "Y1",
    "C1",
    "T1",
    "CT1",
    "SC1",
    "DS1",
    "DD1",
    "DH1",
    "DF1",
    "XD0",
    "YD0",
    "TD1",
    "CTD1",
    "SD1",
    "TXT1",
)
_RAW = {kind.name: ModbusAddress(1, kind) for kind in RegisterType}


@pytest.mark.parametrize(
    "remote_start", [*_CLICK_BANKS, *_RAW], ids=[*_CLICK_BANKS, *(f"raw {k}" for k in _RAW)]
)
def test_a_receive_into_char_tags_reads_only_text(remote_start: str) -> None:
    txt = _txt()

    def write() -> None:
        with rung():
            receive(
                target="peer",
                remote_start=_RAW.get(remote_start, remote_start),
                dest=txt.select(1, 2),
            )

    if remote_start in ("TXT1", "HOLDING", "INPUT"):
        _build(write, txt[1])
        return
    with pytest.raises(TypeError, match="Char tags"):
        _build(write, txt[1])


def test_a_raw_receive_reads_two_characters_from_each_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four characters read two registers.  Each register holds its first character in the
    low byte, as pyrung's Modbus server stores TXT.

    The compiled kernel sends no Modbus request, so the core half runs only on ``PLC``.
    """
    registers = (0x4241, 0x4443)
    txt = Block("T", TagType.CHAR, 1, 4)
    status = {
        "receiving": Bool("Receiving"),
        "success": Bool("Success"),
        "error": Bool("Error"),
        "exception_response": Int("ExCode"),
    }
    with Program(strict=False) as program:
        with rung():
            receive(
                target=_RAW_TARGET,
                remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
                dest=txt.select(1, 4),
                **status,
            )

    requests: list[dict[str, Any]] = []

    def submit(**request: Any) -> Future[_RequestResult]:
        requests.append(request)
        done: Future[_RequestResult] = Future()
        done.set_result(_RequestResult(ok=True, exception_code=0, values=registers))
        return done

    monkeypatch.setattr(modbus_core, "_submit_raw_receive_request", submit)
    plc = PLC(program)
    plc.step()
    plc.step()
    assert [plc.current_state.tags[f"T{i}"] for i in (1, 2, 3, 4)] == ["A", "B", "C", "D"]
    assert [request["count"] for request in requests] == [2]

    namespace, sockets = _run_raw_client_job(monkeypatch, program)
    job = next(
        v for k, v in namespace.items() if k.startswith("_mb_client_") and isinstance(v, dict)
    )
    assert job["request"] == _mbap(1, struct.pack(">BHH", 3, 0x100, 2), uid=17)
    namespace["service_modbus_client"]()
    namespace["service_modbus_client"]()
    sockets[-1].recv_chunks.append(_mbap(1, struct.pack(">BBHH", 3, 4, *registers), uid=17))
    namespace["service_modbus_client"]()
    ctx = _context_for_program(program, _hardware())
    assert [eval(ctx.symbol_for_tag(txt[i]), namespace) for i in (1, 2, 3, 4)] == [
        "A",
        "B",
        "C",
        "D",
    ]


@pytest.mark.parametrize("verb", ["send", "receive"])
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("kind", [RegisterType.HOLDING, RegisterType.INPUT], ids=repr)
def test_a_raw_send_or_receive_of_an_odd_count_of_char_tags_is_refused(
    verb: str, count: int, kind: RegisterType
) -> None:
    """CLICK's dialogs refuse an odd TXT count (OBJ-010 and OBJ-029).  Its Send of one
    register, FC06, always sends two characters, so one Char tag has no form either."""
    if verb == "send" and kind is RegisterType.INPUT:
        pytest.skip("send() refuses an INPUT register for any tag")
    txt = Block("T", TagType.CHAR, 1, 4)
    call = send if verb == "send" else receive
    local = "source" if verb == "send" else "dest"
    with pytest.raises(ValueError, match="Use an even number of Char tags"):
        with Program(strict=False):
            with rung():
                call(
                    target="peer",
                    remote_start=ModbusAddress(0x100, kind),
                    **{local: txt.select(1, count)},
                )


@pytest.mark.parametrize("verb", ["send", "receive"])
def test_a_raw_send_or_receive_of_one_char_tag_is_refused(verb: str) -> None:
    call = send if verb == "send" else receive
    local = "source" if verb == "send" else "dest"
    with pytest.raises(ValueError, match="Use an even number of Char tags"):
        with Program(strict=False):
            with rung():
                call(
                    target="peer",
                    remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
                    **{local: Char("One")},
                )


# --- Send -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [RegisterType.HOLDING, RegisterType.COIL], ids=repr)
def test_a_raw_send_of_char_tags_goes_only_to_a_register(kind: RegisterType) -> None:
    txt = _txt()

    def write() -> None:
        with rung():
            send(target="peer", remote_start=ModbusAddress(1, kind), source=txt.select(1, 2))

    if kind is RegisterType.HOLDING:
        _build(write, txt[1])
        return
    with pytest.raises(
        TypeError, match="Send to a HOLDING register. Each register holds two characters"
    ):
        _build(write, txt[1])


def test_a_raw_send_packs_two_characters_into_each_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four characters write two registers, the first character in each low byte.  The
    core half runs only on ``PLC``, as for a receive."""
    txt = Block("T", TagType.CHAR, 1, 4, default_factory=lambda addr: "ABCD"[addr - 1])
    registers = [0x4241, 0x4443]
    with Program(strict=False) as program:
        with rung():
            send(
                target=_RAW_TARGET,
                remote_start=ModbusAddress(0x100, RegisterType.HOLDING),
                source=txt.select(1, 4),
                success=Bool("Success"),
            )

    requests: list[dict[str, Any]] = []

    def submit(**request: Any) -> Future[_RequestResult]:
        requests.append(request)
        done: Future[_RequestResult] = Future()
        done.set_result(_RequestResult(ok=True, exception_code=0))
        return done

    monkeypatch.setattr(modbus_core, "_submit_raw_send_request", submit)
    plc = PLC(program)
    plc.step()
    assert requests[0]["registers"] == registers

    namespace, _sockets = _run_raw_client_job(monkeypatch, program)
    job = next(
        v for k, v in namespace.items() if k.startswith("_mb_client_") and isinstance(v, dict)
    )
    pdu = struct.pack(">BHHB", 16, 0x100, 2, 4) + struct.pack(">HH", *registers)
    assert job["request"] == _mbap(1, pdu, uid=17)


@pytest.fixture
def soft_plc() -> Iterator[tuple[int, MemoryDataProvider]]:
    """Serve a CLICK's Modbus map from memory on a free local port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provider = MemoryDataProvider()
    ready, stop = threading.Event(), threading.Event()

    async def serve() -> None:
        async with ClickServer(provider, host="127.0.0.1", port=port):
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.02)

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(10)
    yield port, provider
    stop.set()
    thread.join(timeout=5)


def test_text_sent_to_a_soft_plc_receives_back(
    soft_plc: tuple[int, MemoryDataProvider],
) -> None:
    """A raw send of four characters to TXT1 lands in TXT1..TXT4, and a raw receive reads
    them back."""
    port, provider = soft_plc
    peer = ModbusTcpTarget(name="soft", ip="127.0.0.1", port=port)
    txt1 = ModbusAddress(36864, RegisterType.HOLDING)
    sent = Block("S", TagType.CHAR, 1, 4, default_factory=lambda addr: "AB\xe9Z"[addr - 1])
    back = Block("R", TagType.CHAR, 1, 4)
    sent_ok, back_ok = Bool("SentOk"), Bool("BackOk")
    with Program(strict=False) as program:
        with rung(~sent_ok):
            send(target=peer, remote_start=txt1, source=sent.select(1, 4), success=sent_ok)
        with rung(sent_ok):
            receive(target=peer, remote_start=txt1, dest=back.select(1, 4), success=back_ok)

    plc = PLC(program)
    deadline = time.monotonic() + 10
    while not plc.current_state.tags.get("BackOk") and time.monotonic() < deadline:
        plc.step()
        time.sleep(0.005)
    assert [provider.read(f"TXT{i}") for i in (1, 2, 3, 4)] == ["A", "B", "\xe9", "Z"]
    assert [plc.current_state.tags[f"R{i}"] for i in (1, 2, 3, 4)] == ["A", "B", "\xe9", "Z"]


# --- run_function -----------------------------------------------------------------------


# The generated code copies a function's source, so each function is a plain def.
def _five(*_args: Any) -> dict[str, Any]:
    return {"result": 5}


def _true(*_args: Any) -> dict[str, Any]:
    return {"result": True}


def _letter(*_args: Any) -> dict[str, Any]:
    return {"result": "A"}


def _blank(*_args: Any) -> dict[str, Any]:
    return {"result": ""}


def _two(*_args: Any) -> dict[str, Any]:
    return {"result": "AB"}


def _euro(*_args: Any) -> dict[str, Any]:
    return {"result": "\u20ac"}


@pytest.mark.parametrize("enabled", [False, True], ids=["run_function", "run_enabled_function"])
@pytest.mark.parametrize(
    "fn, value",
    [(_five, 5), (_true, True), (_letter, "A"), (_blank, ""), (_two, "AB"), (_euro, "\u20ac")],
    ids=["5", "True", "'A'", "''", "'AB'", "euro"],
)
def test_a_function_result_into_a_char_tag_must_be_one_character(
    fn: Callable[..., dict[str, Any]], value: Any, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    txt = _txt()

    def write() -> None:
        with rung():
            if enabled:
                run_enabled_function(fn, outs={"result": txt[1]})
            else:
                run_function(fn, outs={"result": txt[1]})

    program = _build(write, txt[1])
    if value in ("A", ""):
        _holds_text(program, monkeypatch)
        return
    error = ValueError if isinstance(value, str) else TypeError
    message = f"returned {value!r} for 'result'. The Char tag T1 holds one character"
    _raises_on_every_backend(program, monkeypatch, error, message)
