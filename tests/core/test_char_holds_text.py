"""A Char tag holds only text, whatever instruction writes it.

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

import math
import struct
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, get_args

import pytest

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
from pyrung.core.instruction.send_receive import ModbusAddress, RegisterType, receive
from pyrung.core.instruction.send_receive import _core as modbus_core
from pyrung.core.instruction.send_receive._core import _RequestResult
from pyrung.core.storage import StoreTransform, store_value
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
    """Assert that every Char tag holds one character on every backend after two scans."""
    watch = _char_tags(program)
    results = _on_every_backend(program, monkeypatch, watch)
    for backend, values in results.items():
        for tag, value in zip(watch, values, strict=True):
            assert isinstance(value, str) and len(value) == 1, (backend, tag.name, value)
    assert len(set(results.values())) == 1, results
    program.check()
    _build_prior_facts(program)


# --- The guard in each runtime's plain Char store ----------------------------------------


@pytest.mark.parametrize("value", [5, True, float("inf"), None], ids=repr)
def test_a_plain_char_store_raises_on_a_value_that_is_not_text(value: Any) -> None:
    """Only a write that skipped the build rules reaches the guard, so the sweeps below never
    trip it.  A float Inf would otherwise store 0."""
    with pytest.raises(RuntimeError, match="This is a bug in pyrung"):
        store_value(value, StoreTransform("char"))
    namespace: dict[str, Any] = {"math": math, "_store_real": float}
    exec("\n".join(_HELPER_DEFS["_store_copy_value_to_type"]), namespace)  # noqa: S102
    with pytest.raises(RuntimeError, match="This is a bug in pyrung"):
        namespace["_store_copy_value_to_type"](value, "CHAR")
    assert namespace["_store_copy_value_to_type"]("", "CHAR") == "\x00"


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
    *(Source(repr(value), lambda value=value: value) for value in (True, 5, 12, 1.5, "A", "ABC")),
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


#: The cases that build, and whose first scan raises ``ValueError`` on every backend.  A
#: TXT holds one character.  laddercodec refuses this Fill when you export the program.
RAISES_VALUE_ERROR = {("fill-plain", "'ABC'")}

WRITES = [
    *(("copy", name, _copy, convert) for name, convert in _COPY_CONVERTERS.items()),
    ("fill", "plain", _fill, None),
    *(("blockcopy", name, _blockcopy, convert) for name, convert in _BLOCK_COPY_CONVERTERS.items()),
]


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
    try:
        program = _build(lambda: write(source.make(), convert, txt), txt[1])
    except (TypeError, ValueError):
        return
    if (name, source.name) in RAISES_VALUE_ERROR:
        _raises_on_every_backend(program, monkeypatch, ValueError, "one 8-bit character")
        return
    _holds_text(program, monkeypatch)


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
    """Three characters read two registers.  Each register holds its first character in the
    low byte, as pyrung's Modbus server stores TXT.  The last high byte is left unread.

    The compiled kernel sends no Modbus request, so the core half runs only on ``PLC``.
    """
    registers = (0x4241, 0x5843)
    txt = Block("T", TagType.CHAR, 1, 3)
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
                dest=txt.select(1, 3),
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
    assert [plc.current_state.tags[f"T{i}"] for i in (1, 2, 3)] == ["A", "B", "C"]
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
    assert [eval(ctx.symbol_for_tag(txt[i]), namespace) for i in (1, 2, 3)] == ["A", "B", "C"]


# --- run_function -----------------------------------------------------------------------


# The generated code copies a function's source, so each function is a plain def.
def _five(*_args: Any) -> dict[str, Any]:
    return {"result": 5}


def _true(*_args: Any) -> dict[str, Any]:
    return {"result": True}


def _letter(*_args: Any) -> dict[str, Any]:
    return {"result": "A"}


@pytest.mark.parametrize("enabled", [False, True], ids=["run_function", "run_enabled_function"])
@pytest.mark.parametrize(
    "fn, value", [(_five, 5), (_true, True), (_letter, "A")], ids=["5", "True", "'A'"]
)
def test_a_function_result_into_a_char_tag_must_be_text(
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
    if isinstance(value, str):
        _holds_text(program, monkeypatch)
        return
    _raises_on_every_backend(program, monkeypatch, TypeError, f"returned {value!r} for 'result'")
