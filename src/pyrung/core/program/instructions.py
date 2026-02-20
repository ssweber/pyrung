from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from pyrung.core._source import (
    _capture_source,
)
from pyrung.core.instruction import (
    BlockCopyInstruction,
    CallInstruction,
    CopyInstruction,
    EnabledFunctionCallInstruction,
    FillInstruction,
    FunctionCallInstruction,
    LatchInstruction,
    MathInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    ResetInstruction,
    ReturnInstruction,
    SearchInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import Tag, TagType

from .context import Program, SubroutineFunc, _require_rung_context


def _iter_coil_tags(target: Tag | BlockRange) -> list[Tag]:
    """Normalize a coil target to concrete tags."""
    if isinstance(target, Tag):
        return [target]
    if isinstance(target, BlockRange):
        return target.tags()
    raise TypeError(f"Expected Tag or BlockRange from .select(), got {type(target).__name__}")


def _validate_function_call(
    fn: Any,
    ins: dict[str, Any] | None,
    outs: dict[str, Any] | None,
    *,
    func_name: str,
    has_enabled: bool = False,
) -> None:
    """Validate callback contract for run_function/run_enabled_function DSL entry points."""
    if not callable(fn):
        raise TypeError(f"{func_name}() fn must be callable, got {type(fn).__name__}")

    if inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(type(fn).__call__):
        raise TypeError(f"{func_name}() fn must be synchronous (async def is not supported)")

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{func_name}() could not inspect function signature") from exc

    if ins is not None:
        if not isinstance(ins, dict):
            raise TypeError(f"{func_name}() ins must be a dict, got {type(ins).__name__}")
    elif has_enabled:
        ins = {}

    if ins is not None:
        bind_kwargs = {k: object() for k in ins}
        bind_args = (object(),) if has_enabled else ()
        try:
            signature.bind(*bind_args, **bind_kwargs)
        except TypeError as exc:
            raise TypeError(
                f"{func_name}() ins keys {sorted(ins.keys())} incompatible with "
                f"{getattr(fn, '__name__', repr(fn))!r} signature"
            ) from exc

    if outs is not None and not isinstance(outs, dict):
        raise TypeError(f"{func_name}() outs must be a dict, got {type(outs).__name__}")


def out(target: Tag | BlockRange, oneshot: bool = False) -> Tag | BlockRange:
    """Output coil instruction (OUT).

    Sets target to True when rung is true.
    Resets to False when rung goes false.

    Example:
        with Rung(Button):
            out(Light)
            out(Y.select(1, 4))
    """
    ctx = _require_rung_context("out")
    source_file, source_line = _capture_source(depth=2)
    _iter_coil_tags(target)
    instr = OutInstruction(target, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return target


def latch(target: Tag | BlockRange) -> Tag | BlockRange:
    """Latch/Set instruction (SET).

    Sets target to True. Unlike OUT, does NOT reset when rung goes false.
    Use reset() to turn off.

    Example:
        with Rung(StartButton):
            latch(MotorRunning)
            latch(C.select(1, 8))
    """
    ctx = _require_rung_context("latch")
    source_file, source_line = _capture_source(depth=2)
    _iter_coil_tags(target)
    instr = LatchInstruction(target)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return target


def reset(target: Tag | BlockRange) -> Tag | BlockRange:
    """Reset/Unlatch instruction (RST).

    Sets target to its default value (False for bits, 0 for ints).

    Example:
        with Rung(StopButton):
            reset(MotorRunning)
            reset(C.select(1, 8))
    """
    ctx = _require_rung_context("reset")
    source_file, source_line = _capture_source(depth=2)
    _iter_coil_tags(target)
    instr = ResetInstruction(target)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return target


def copy(
    source: Any,
    target: Tag | IndirectRef | IndirectExprRef,
    oneshot: bool = False,
) -> Tag | IndirectRef | IndirectExprRef:
    """Copy instruction (CPY/MOV).

    Copies source value to target.

    Example:
        with Rung(Button):
            copy(5, StepNumber)
    """
    ctx = _require_rung_context("copy")
    source_file, source_line = _capture_source(depth=2)
    instr = CopyInstruction(source, target, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return target


def run_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
    *,
    oneshot: bool = False,
) -> None:
    """Execute a synchronous function when rung power is true."""
    ctx = _require_rung_context("run_function")
    source_file, source_line = _capture_source(depth=2)
    _validate_function_call(fn, ins, outs, func_name="run_function")
    instr = FunctionCallInstruction(fn, ins, outs, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def run_enabled_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
) -> None:
    """Execute a synchronous function every scan with rung enabled state."""
    ctx = _require_rung_context("run_enabled_function")
    source_file, source_line = _capture_source(depth=2)
    _validate_function_call(fn, ins, outs, func_name="run_enabled_function", has_enabled=True)
    enable_condition = ctx._rung._get_combined_condition()
    instr = EnabledFunctionCallInstruction(fn, ins, outs, enable_condition)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def blockcopy(source: Any, dest: Any, oneshot: bool = False) -> None:
    """Block copy instruction.

    Copies values from source BlockRange to dest BlockRange.
    Both ranges must have the same length.

    Example:
        with Rung(CopyEnable):
            blockcopy(DS.select(1, 10), DD.select(1, 10))

    Args:
        source: Source BlockRange or IndirectBlockRange from .select().
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("blockcopy")
    source_file, source_line = _capture_source(depth=2)
    instr = BlockCopyInstruction(source, dest, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def fill(value: Any, dest: Any, oneshot: bool = False) -> None:
    """Fill instruction.

    Writes a constant value to every element in a BlockRange.

    Example:
        with Rung(ClearEnable):
            fill(0, DS.select(1, 100))

    Args:
        value: Value to write (literal, Tag, or Expression).
        dest: Dest BlockRange or IndirectBlockRange from .select().
        oneshot: If True, execute only once per rung activation.
    """
    ctx = _require_rung_context("fill")
    source_file, source_line = _capture_source(depth=2)
    instr = FillInstruction(value, dest, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def pack_bits(bit_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack BOOL tags from a BlockRange into a register destination."""
    ctx = _require_rung_context("pack_bits")
    source_file, source_line = _capture_source(depth=2)
    instr = PackBitsInstruction(bit_block, dest, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def pack_words(word_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack two 16-bit tags from a BlockRange into a 32-bit destination."""
    ctx = _require_rung_context("pack_words")
    source_file, source_line = _capture_source(depth=2)
    instr = PackWordsInstruction(word_block, dest, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def pack_text(
    source_range: Any,
    dest: Any,
    *,
    allow_whitespace: bool = False,
    oneshot: bool = False,
) -> None:
    """Pack Copy text mode: parse a TXT/CHAR range into a numeric destination."""
    ctx = _require_rung_context("pack_text")
    source_file, source_line = _capture_source(depth=2)
    instr = PackTextInstruction(
        source_range,
        dest,
        allow_whitespace=allow_whitespace,
        oneshot=oneshot,
    )
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def unpack_to_bits(source: Any, bit_block: Any, oneshot: bool = False) -> None:
    """Unpack a register source into BOOL tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_bits")
    source_file, source_line = _capture_source(depth=2)
    instr = UnpackToBitsInstruction(source, bit_block, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def unpack_to_words(source: Any, word_block: Any, oneshot: bool = False) -> None:
    """Unpack a 32-bit register source into two 16-bit tags in a BlockRange."""
    ctx = _require_rung_context("unpack_to_words")
    source_file, source_line = _capture_source(depth=2)
    instr = UnpackToWordsInstruction(source, word_block, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def math(expression: Any, dest: Tag, oneshot: bool = False, mode: str = "decimal") -> Tag:
    """Math instruction.

    Evaluates an expression and stores the result in dest, with
    truncation to the destination tag's bit width (modular wrapping).

    Key differences from copy():
    - Truncates result to destination tag's type width
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes

    Example:
        with Rung(Enable):
            math(DS1 * DS2 + DS3, Result)
            math(MaskA & MaskB, MaskResult, mode="hex")

    Args:
        expression: Expression, Tag, or literal to evaluate.
        dest: Destination tag (type determines truncation width).
        oneshot: If True, execute only once per rung activation.
        mode: "decimal" (signed arithmetic) or "hex" (unsigned 16-bit wrap).

    Returns:
        The dest tag.
    """
    ctx = _require_rung_context("math")
    source_file, source_line = _capture_source(depth=2)
    instr = MathInstruction(expression, dest, oneshot, mode)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return dest


def search(
    condition: str,
    value: Any,
    search_range: BlockRange | IndirectBlockRange,
    result: Tag,
    found: Tag,
    continuous: bool = False,
    oneshot: bool = False,
) -> Tag:
    """Search instruction.

    Scans a selected range and writes the first matching address into `result`.
    Writes `found` True on hit; on miss writes `result=-1` and `found=False`.
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if condition not in {"==", "!=", "<", "<=", ">", ">="}:
        raise ValueError(
            f"Invalid search condition: {condition!r}. Expected one of: ==, !=, <, <=, >, >="
        )
    if not isinstance(search_range, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            "search() expects search_range from .select() "
            f"(BlockRange or IndirectBlockRange), got {type(search_range).__name__}"
        )
    if found.type != TagType.BOOL:
        raise TypeError(f"search() found must be BOOL, got {found.type.name}")
    if result.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"search() result must be INT or DINT, got {result.type.name}")

    ctx = _require_rung_context("search")
    source_file, source_line = _capture_source(depth=2)
    instr = SearchInstruction(condition, value, search_range, result, found, continuous, oneshot)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
    return result


def call(target: str | SubroutineFunc) -> None:
    """Call a subroutine instruction.

    Executes the named subroutine when the rung is true.
    Accepts either a string name or a @subroutine-decorated function.

    Example:
        with Rung(Button):
            call("init_sequence")

        with subroutine("init_sequence"):
            with Rung():
                out(Light)

        # Or with decorator:
        @subroutine("init")
        def init_sequence():
            with Rung():
                out(Light)

        with Program() as logic:
            with Rung(Button):
                call(init_sequence)
    """
    ctx = _require_rung_context("call")
    source_file, source_line = _capture_source(depth=2)
    prog = Program.current()
    if prog is None:
        raise RuntimeError("call() must be used inside a Program context")

    if isinstance(target, SubroutineFunc):
        name = target.name
        if name not in prog.subroutines:
            target._register(prog)
    else:
        name = target

    instr = CallInstruction(name, prog)
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)


def return_() -> None:
    """Return from the current subroutine.

    Example:
        with subroutine("my_sub"):
            with Rung(Abort):
                return_()
    """
    ctx = _require_rung_context("return_")
    source_file, source_line = _capture_source(depth=2)
    prog = Program.current()
    if prog is None or prog._current_subroutine is None:
        raise RuntimeError("return_() must be used inside a subroutine")
    instr = ReturnInstruction()
    instr.source_file, instr.source_line = source_file, source_line
    ctx._rung.add_instruction(instr)
