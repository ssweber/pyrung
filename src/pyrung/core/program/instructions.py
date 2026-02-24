from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core._source import (
    _capture_source,
)
from pyrung.core.instruction import (
    BlockCopyInstruction,
    CalcInstruction,
    CallInstruction,
    CopyInstruction,
    EnabledFunctionCallInstruction,
    FillInstruction,
    FunctionCallInstruction,
    LatchInstruction,
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

if TYPE_CHECKING:
    from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef


def _iter_coil_tags(target: Tag | BlockRange) -> list[Tag]:
    """Normalize a coil target to concrete tags."""
    if isinstance(target, Tag):
        return [target]
    if isinstance(target, BlockRange):
        return target.tags()
    raise TypeError(f"Expected Tag or BlockRange from .select(), got {type(target).__name__}")


def _attach_instruction(
    ctx: Any, instruction: Any, source_file: str | None, source_line: int | None
) -> None:
    """Attach source metadata and append an instruction to the current rung."""
    instruction.source_file, instruction.source_line = source_file, source_line
    ctx._rung.add_instruction(instruction)


def _capture_instruction_context(
    func_name: str,
    *,
    source_depth: int,
) -> tuple[Any, str | None, int | None]:
    """Capture required rung context and source location for a DSL instruction call."""
    ctx = _require_rung_context(func_name)
    ctx._assert_no_pending_required_builder(func_name)
    source_file, source_line = _capture_source(depth=source_depth)
    return ctx, source_file, source_line


def _add_instruction(
    func_name: str,
    instruction_cls: type,
    *args: Any,
    source_depth: int = 4,
    **kwargs: Any,
) -> Any:
    """Build an instruction, capture source metadata, and append it to the rung."""
    ctx, source_file, source_line = _capture_instruction_context(
        func_name,
        source_depth=source_depth,
    )
    instruction = instruction_cls(*args, **kwargs)
    _attach_instruction(ctx, instruction, source_file, source_line)
    return instruction


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
    ctx, source_file, source_line = _capture_instruction_context("out", source_depth=3)
    _iter_coil_tags(target)
    _attach_instruction(ctx, OutInstruction(target, oneshot), source_file, source_line)
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
    ctx, source_file, source_line = _capture_instruction_context("latch", source_depth=3)
    _iter_coil_tags(target)
    _attach_instruction(ctx, LatchInstruction(target), source_file, source_line)
    return target


def reset(target: Tag | BlockRange) -> Tag | BlockRange:
    """Reset/Unlatch instruction (RST).

    Sets target to its default value (False for bits, 0 for ints).

    Example:
        with Rung(StopButton):
            reset(MotorRunning)
            reset(C.select(1, 8))
    """
    ctx, source_file, source_line = _capture_instruction_context("reset", source_depth=3)
    _iter_coil_tags(target)
    _attach_instruction(ctx, ResetInstruction(target), source_file, source_line)
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
    _add_instruction("copy", CopyInstruction, source, target, oneshot)
    return target


def run_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
    *,
    oneshot: bool = False,
) -> None:
    """Execute a synchronous function when rung power is true."""
    ctx, source_file, source_line = _capture_instruction_context(
        "run_function",
        source_depth=3,
    )
    _validate_function_call(fn, ins, outs, func_name="run_function")
    _attach_instruction(
        ctx,
        FunctionCallInstruction(fn, ins, outs, oneshot),
        source_file,
        source_line,
    )


def run_enabled_function(
    fn: Callable[..., dict[str, Any]],
    ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None = None,
    outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None = None,
) -> None:
    """Execute a synchronous function every scan with rung enabled state."""
    ctx, source_file, source_line = _capture_instruction_context(
        "run_enabled_function",
        source_depth=3,
    )
    _validate_function_call(fn, ins, outs, func_name="run_enabled_function", has_enabled=True)
    enable_condition = ctx._rung._get_combined_condition()
    _attach_instruction(
        ctx,
        EnabledFunctionCallInstruction(fn, ins, outs, enable_condition),
        source_file,
        source_line,
    )


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
    _add_instruction("blockcopy", BlockCopyInstruction, source, dest, oneshot)


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
    _add_instruction("fill", FillInstruction, value, dest, oneshot)


def pack_bits(bit_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack BOOL tags from a BlockRange into a register destination."""
    _add_instruction("pack_bits", PackBitsInstruction, bit_block, dest, oneshot)


def pack_words(word_block: Any, dest: Any, oneshot: bool = False) -> None:
    """Pack two 16-bit tags from a BlockRange into a 32-bit destination."""
    _add_instruction("pack_words", PackWordsInstruction, word_block, dest, oneshot)


def pack_text(
    source_range: Any,
    dest: Any,
    *,
    allow_whitespace: bool = False,
    oneshot: bool = False,
) -> None:
    """Pack Copy text mode: parse a TXT/CHAR range into a numeric destination."""
    _add_instruction(
        "pack_text",
        PackTextInstruction,
        source_range,
        dest,
        allow_whitespace=allow_whitespace,
        oneshot=oneshot,
    )


def unpack_to_bits(source: Any, bit_block: Any, oneshot: bool = False) -> None:
    """Unpack a register source into BOOL tags in a BlockRange."""
    _add_instruction(
        "unpack_to_bits",
        UnpackToBitsInstruction,
        source,
        bit_block,
        oneshot,
    )


def unpack_to_words(source: Any, word_block: Any, oneshot: bool = False) -> None:
    """Unpack a 32-bit register source into two 16-bit tags in a BlockRange."""
    _add_instruction(
        "unpack_to_words",
        UnpackToWordsInstruction,
        source,
        word_block,
        oneshot,
    )


def calc(expression: Any, dest: Tag, oneshot: bool = False, mode: str = "decimal") -> Tag:
    """Calc instruction.

    Evaluates an expression and stores the result in dest, with
    truncation to the destination tag's bit width (modular wrapping).

    Key differences from copy():
    - Truncates result to destination tag's type width
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes

    Example:
        with Rung(Enable):
            calc(DS1 * DS2 + DS3, Result)
            calc(MaskA & MaskB, MaskResult, mode="hex")

    Args:
        expression: Expression, Tag, or literal to evaluate.
        dest: Destination tag (type determines truncation width).
        oneshot: If True, execute only once per rung activation.
        mode: "decimal" (signed arithmetic) or "hex" (unsigned 16-bit wrap).

    Returns:
        The dest tag.
    """
    _add_instruction("calc", CalcInstruction, expression, dest, oneshot, mode)
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

    _add_instruction(
        "search",
        SearchInstruction,
        condition,
        value,
        search_range,
        result,
        found,
        continuous,
        oneshot,
    )
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
    ctx, source_file, source_line = _capture_instruction_context("call", source_depth=3)
    prog = Program.current()
    if prog is None:
        raise RuntimeError("call() must be used inside a Program context")

    if isinstance(target, SubroutineFunc):
        name = target.name
        if name not in prog.subroutines:
            target._register(prog)
    else:
        name = target

    _attach_instruction(ctx, CallInstruction(name, prog), source_file, source_line)


def return_early() -> None:
    """Return from the current subroutine.

    Example:
        with subroutine("my_sub"):
            with Rung(Abort):
                return_early()
    """
    ctx, source_file, source_line = _capture_instruction_context("return_early", source_depth=3)
    prog = Program.current()
    if prog is None or prog._current_subroutine is None:
        raise RuntimeError("return_early() must be used inside a subroutine")
    _attach_instruction(ctx, ReturnInstruction(), source_file, source_line)
