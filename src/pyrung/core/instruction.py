"""Instruction classes for the immutable PLC engine.

Instructions execute within a ScanContext, writing to batched evolvers.
All state modifications are collected and committed at scan end.
"""

from __future__ import annotations

import math
import re
import struct
from abc import ABC, abstractmethod
from collections.abc import Callable
from operator import eq, ge, gt, le, lt, ne
from typing import TYPE_CHECKING, Any

from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )


_DINT_MIN = -2147483648
_DINT_MAX = 2147483647
_INT_MIN = -32768
_INT_MAX = 32767
_SEARCH_OPERATOR_MAP = {
    "==": eq,
    "!=": ne,
    "<": lt,
    "<=": le,
    ">": gt,
    ">=": ge,
}
_TAG_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def _clamp_dint(value: int) -> int:
    """Clamp integer to DINT (32-bit signed) range."""
    return max(_DINT_MIN, min(_DINT_MAX, value))


def _clamp_int(value: int) -> int:
    """Clamp integer to INT (16-bit signed) range."""
    return max(_INT_MIN, min(_INT_MAX, value))


def _int_to_float_bits(n: int) -> float:
    """Reinterpret a 32-bit unsigned integer bit pattern as IEEE 754 float."""
    return struct.unpack("<f", struct.pack("<I", int(n) & 0xFFFFFFFF))[0]


def _float_to_int_bits(f: float) -> int:
    """Reinterpret an IEEE 754 float bit pattern as 32-bit unsigned integer."""
    return struct.unpack("<I", struct.pack("<f", float(f)))[0]


def resolve_tag_or_value_ctx(
    source: Tag | IndirectRef | IndirectExprRef | Any, ctx: ScanContext
) -> Any:
    """Resolve tag (direct or indirect), expression, or return literal value using ScanContext.

    Args:
        source: Tag, IndirectRef, IndirectExprRef, Expression, or literal value.
        ctx: ScanContext for resolving values with read-after-write visibility.

    Returns:
        Resolved value from context or the literal value.
    """
    # Import here to avoid circular imports
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectExprRef
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    # Check for Expression first (includes TagExpr)
    if isinstance(source, Expression):
        return source.evaluate(ctx)
    # Check for IndirectExprRef
    if isinstance(source, IndirectExprRef):
        resolved_tag = source.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    # Check for IndirectRef
    if isinstance(source, IndirectRefType):
        resolved_tag = source.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    # Check for Tag
    if isinstance(source, Tag):
        return ctx.get_tag(source.name, source.default)
    # Literal value
    return source


def resolve_tag_ctx(target: Tag | IndirectRef | IndirectExprRef, ctx: ScanContext) -> Tag:
    """Resolve target to a concrete Tag (handling indirect) using ScanContext.

    Args:
        target: Tag, IndirectRef, or IndirectExprRef to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        The resolved Tag (with type info preserved).
    """
    # Import here to avoid circular imports
    from pyrung.core.memory_block import IndirectExprRef
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    # Check for IndirectExprRef first
    if isinstance(target, IndirectExprRef):
        return target.resolve_ctx(ctx)
    # Check for IndirectRef
    if isinstance(target, IndirectRefType):
        return target.resolve_ctx(ctx)
    # Regular Tag
    return target


def resolve_tag_name_ctx(target: Tag | IndirectRef | IndirectExprRef, ctx: ScanContext) -> str:
    """Resolve tag to its name (handling indirect) using ScanContext.

    Args:
        target: Tag, IndirectRef, or IndirectExprRef to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        The tag name string.
    """
    return resolve_tag_ctx(target, ctx).name


class Instruction(ABC):
    """Base class for all instructions.

    Instructions execute within a ScanContext, writing to batched evolvers.
    All state modifications are collected and committed at scan end.
    """

    source_file: str | None = None
    source_line: int | None = None

    @abstractmethod
    def execute(self, ctx: ScanContext) -> None:
        """Execute this instruction within the given context (internal)."""
        pass

    def always_execute(self) -> bool:
        """Whether this instruction should execute even when rung is false.

        Override to return True for terminal instructions like counters
        that need to check their conditions independently.
        """
        return False


class SubroutineReturnSignal(Exception):
    """Internal control-flow signal used by return_() inside subroutines."""


def resolve_block_range_tags_ctx(block_range: Any, ctx: ScanContext) -> list[Tag]:
    """Resolve a BlockRange or IndirectBlockRange to a list of Tags.

    Args:
        block_range: BlockRange or IndirectBlockRange to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        List of resolved Tag objects (with type info preserved).
    """
    return resolve_block_range_ctx(block_range, ctx).tags()


def resolve_block_range_ctx(block_range: Any, ctx: ScanContext) -> BlockRange:
    """Resolve a BlockRange or IndirectBlockRange to a concrete BlockRange."""
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(block_range, IndirectBlockRange):
        block_range = block_range.resolve_ctx(ctx)

    if not isinstance(block_range, BlockRange):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(block_range).__name__}"
        )

    return block_range


def resolve_coil_targets_ctx(
    target: Tag | BlockRange | IndirectBlockRange, ctx: ScanContext
) -> list[Tag]:
    """Resolve a coil target to one or more concrete Tags.

    Coil targets support:
    - Single Tag
    - BlockRange from `.select(start, end)`
    - IndirectBlockRange from dynamic `.select(...)`
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(target, Tag):
        return [target]
    if isinstance(target, (BlockRange, IndirectBlockRange)):
        return resolve_block_range_tags_ctx(target, ctx)
    raise TypeError(f"Expected Tag, BlockRange, or IndirectBlockRange, got {type(target).__name__}")


def _set_fault_out_of_range(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.out_of_range.name, True)


def _set_fault_division_error(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.division_error.name, True)


def _set_fault_address_error(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.address_error.name, True)


def _ascii_char_from_code(code: int) -> str:
    if code < 0 or code > 127:
        raise ValueError("ASCII code out of range")
    return chr(code)


def _as_single_ascii_char(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("CHAR value must be a string")
    if value == "":
        return value
    if len(value) != 1 or ord(value) > 127:
        raise ValueError("CHAR value must be blank or one ASCII character")
    return value


def _text_from_source_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise ValueError("text conversion source must resolve to str")


def _sequential_tags(start_tag: Tag, count: int) -> list[Tag]:
    if count <= 0:
        return []
    if count == 1:
        return [start_tag]

    match = _TAG_SUFFIX_RE.match(start_tag.name)
    if match is None:
        raise ValueError(f"Cannot expand sequential destination from {start_tag.name!r}")

    prefix, suffix = match.groups()
    width = len(suffix)
    base = int(suffix)
    tags = [start_tag]
    for offset in range(1, count):
        addr = base + offset
        name = f"{prefix}{addr:0{width}d}"
        tags.append(
            Tag(
                name=name,
                type=start_tag.type,
                retentive=start_tag.retentive,
                default=start_tag.default,
            )
        )
    return tags


def _termination_char(termination_code: int | str | None) -> str:
    if termination_code is None:
        return ""
    if isinstance(termination_code, str):
        if len(termination_code) != 1:
            raise ValueError("termination_code must be one character or int ASCII code")
        return _as_single_ascii_char(termination_code)
    if not isinstance(termination_code, int):
        raise TypeError("termination_code must be int, str, or None")
    return _ascii_char_from_code(termination_code)


def _store_numeric_text_digits(text: str, targets: list[Tag], *, mode: str) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if len(text) != len(targets):
        raise ValueError("source/destination text length mismatch")

    for char, target in zip(text, targets, strict=True):
        if mode == "value":
            if char < "0" or char > "9":
                raise ValueError("Copy Character Value accepts only digits 0-9")
            numeric = ord(char) - ord("0")
        elif mode == "ascii":
            if ord(char) > 127:
                raise ValueError("Copy ASCII Code Value accepts ASCII only")
            numeric = ord(char)
        else:
            raise ValueError(f"Unsupported text->numeric mode: {mode}")
        updates[target.name] = _store_copy_value_to_tag_type(numeric, target)
    return updates


def _format_int_text(value: int, width: int, suppress_zero: bool, *, signed: bool = True) -> str:
    if suppress_zero:
        return str(value)
    if not signed:
        return f"{value:0{width}X}"
    if value < 0:
        return f"-{abs(value):0{width}d}"
    return f"{value:0{width}d}"


def _render_text_from_numeric(
    value: Any,
    *,
    source_tag: Tag | None,
    suppress_zero: bool,
    exponential: bool,
) -> str:
    from pyrung.core.tag import TagType

    source_type = source_tag.type if source_tag is not None else None
    if source_type == TagType.REAL or isinstance(value, float):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("REAL source is not finite")
        return f"{numeric:.7E}" if exponential else f"{numeric:.7f}"

    number = int(value)
    if source_type == TagType.WORD:
        return _format_int_text(number & 0xFFFF, 4, suppress_zero, signed=False)
    if source_type == TagType.DINT:
        return _format_int_text(number, 10, suppress_zero)
    if source_type == TagType.INT:
        return _format_int_text(number, 5, suppress_zero)
    return str(number) if suppress_zero else f"{number:05d}"


def _parse_pack_text_value(text: str, dest_tag: Tag) -> Any:
    from pyrung.core.tag import TagType

    if text == "":
        raise ValueError("empty text cannot be parsed")

    if dest_tag.type in {TagType.INT, TagType.DINT}:
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("integer parse failed")
        parsed = int(text, 10)
        if dest_tag.type == TagType.INT and (parsed < _INT_MIN or parsed > _INT_MAX):
            raise ValueError("integer out of INT range")
        if dest_tag.type == TagType.DINT and (parsed < _DINT_MIN or parsed > _DINT_MAX):
            raise ValueError("integer out of DINT range")
        return parsed

    if dest_tag.type == TagType.WORD:
        if not re.fullmatch(r"[0-9A-Fa-f]+", text):
            raise ValueError("hex parse failed")
        parsed = int(text, 16)
        if parsed < 0 or parsed > 0xFFFF:
            raise ValueError("hex out of WORD range")
        return parsed

    if dest_tag.type == TagType.REAL:
        parsed = float(text)
        if not math.isfinite(parsed):
            raise ValueError("REAL parse produced non-finite value")
        # Ensure value can round-trip to 32-bit float
        struct.pack("<f", parsed)
        return parsed

    raise TypeError(
        f"pack_text destination must be INT, DINT, WORD, or REAL; got {dest_tag.type.name}"
    )


class OneShotMixin:
    """Mixin for instructions that support one-shot mode.

    One-shot instructions execute only once per rung activation.
    They must be reset when the rung goes false.
    """

    def __init__(self, oneshot: bool = False):
        self._oneshot = oneshot
        self._has_executed = False

    @property
    def oneshot(self) -> bool:
        return self._oneshot

    def should_execute(self) -> bool:
        """Check if instruction should execute (respects oneshot)."""
        if not self._oneshot:
            return True
        if self._has_executed:
            return False
        self._has_executed = True
        return True

    def reset_oneshot(self) -> None:
        """Reset oneshot state (call when rung goes false)."""
        self._has_executed = False


def _fn_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", type(fn).__name__)


class FunctionCallInstruction(OneShotMixin, Instruction):
    """Stateless function call: copy-in / execute / copy-out."""

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(**kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_function: {_fn_name(self._fn)!r} returned None but outs were declared"
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    f"run_function: {_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}"
                )
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))


class AsyncFunctionCallInstruction(Instruction):
    """Always-execute function call with enabled flag."""

    def __init__(
        self,
        fn: Callable[..., dict[str, Any]],
        ins: dict[str, Tag | IndirectRef | IndirectExprRef | Any] | None,
        outs: dict[str, Tag | IndirectRef | IndirectExprRef] | None,
        enable_condition: Condition | None,
    ):
        self._fn = fn
        self._ins = ins or {}
        self._outs = outs or {}
        self._enable_condition = enable_condition

    def always_execute(self) -> bool:
        return True

    def execute(self, ctx: ScanContext) -> None:
        enabled = True
        if self._enable_condition is not None:
            enabled = bool(self._enable_condition.evaluate(ctx))
        kwargs = {name: resolve_tag_or_value_ctx(src, ctx) for name, src in self._ins.items()}
        result = self._fn(enabled, **kwargs)
        if not self._outs:
            return
        if result is None:
            raise TypeError(
                f"run_enabled_function: {_fn_name(self._fn)!r} returned None but outs were declared"
            )
        for key, target in self._outs.items():
            if key not in result:
                raise KeyError(
                    "run_enabled_function: "
                    f"{_fn_name(self._fn)!r} missing key {key!r}; got {sorted(result)}"
                )
            resolved = resolve_tag_ctx(target, ctx)
            ctx.set_tag(resolved.name, _store_copy_value_to_tag_type(result[key], resolved))


class ForLoopInstruction(OneShotMixin, Instruction):
    """For-loop instruction.

    Executes a captured instruction list N times within one scan.
    """

    def __init__(
        self,
        count: Tag | IndirectRef | IndirectExprRef | Any,
        idx_tag: Tag,
        instructions: list[Instruction],
        coils: set[Tag],
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.count = count
        self.idx_tag = idx_tag
        self.instructions = instructions
        self.coils = coils

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        count_value = resolve_tag_or_value_ctx(self.count, ctx)
        iterations = max(0, int(count_value))

        for i in range(iterations):
            # Keep loop index in tag space so indirect refs resolve via ctx.get_tag().
            ctx.set_tag(self.idx_tag.name, i)
            for instruction in self.instructions:
                instruction.execute(ctx)

    def reset_oneshot(self) -> None:
        """Reset own oneshot state and propagate reset to captured children."""
        OneShotMixin.reset_oneshot(self)
        for instruction in self.instructions:
            reset_fn = getattr(instruction, "reset_oneshot", None)
            if callable(reset_fn):
                reset_fn()


class OutInstruction(OneShotMixin, Instruction):
    """Output coil instruction (OUT).

    Sets the target bit to True when executed.
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, True)


class LatchInstruction(Instruction):
    """Latch/Set instruction (SET).

    Sets the target bit to True. Unlike OUT, this is typically
    not reset when the rung goes false.
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange):
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, True)


class ResetInstruction(Instruction):
    """Reset/Unlatch instruction (RST).

    Sets the target to its default value (False for bits, 0 for ints).
    """

    def __init__(self, target: Tag | BlockRange | IndirectBlockRange):
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        for target in resolve_coil_targets_ctx(self.target, ctx):
            ctx.set_tag(target.name, target.default)


class CopyInstruction(OneShotMixin, Instruction):
    """Copy instruction (CPY/MOV).

    Copies a value from source to target.
    Source can be a literal value, Tag, or IndirectRef.
    Target can be a Tag or IndirectRef.
    """

    def __init__(
        self,
        source: Tag | IndirectRef | IndirectExprRef | Any,
        target: Tag | IndirectRef | IndirectExprRef,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        try:
            resolved_target = resolve_tag_ctx(self.target, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.target, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
                return
            raise

        if isinstance(self.source, CopyModifier):
            self._execute_modifier_copy(ctx, resolved_target, self.source)
            return

        try:
            value = resolve_tag_or_value_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
                return
            raise

        value = _store_copy_value_to_tag_type(value, resolved_target)
        ctx.set_tag(resolved_target.name, value)

    def _execute_modifier_copy(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        mode = modifier.mode
        if mode in {"value", "ascii"}:
            self._copy_text_to_numeric(ctx, resolved_target, modifier, mode=mode)
            return
        if mode == "text":
            self._copy_numeric_to_text(ctx, resolved_target, modifier)
            return
        if mode == "binary":
            self._copy_binary_to_text(ctx, resolved_target, modifier)
            return
        _set_fault_out_of_range(ctx)

    def _copy_text_to_numeric(
        self,
        ctx: ScanContext,
        resolved_target: Tag,
        modifier: CopyModifier,
        *,
        mode: str,
    ) -> None:
        try:
            source_value = resolve_tag_or_value_ctx(modifier.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            text = _text_from_source_value(source_value)
            targets = _sequential_tags(resolved_target, len(text))
            updates = _store_numeric_text_digits(text, targets, mode=mode)
        except (TypeError, ValueError):
            _set_fault_out_of_range(ctx)
            return
        ctx.set_tags(updates)

    def _copy_numeric_to_text(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        source_tag: Tag | None = None
        try:
            if isinstance(modifier.source, Tag):
                source_tag = modifier.source
            elif isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                source_tag = resolve_tag_ctx(modifier.source, ctx)
            value = resolve_tag_or_value_ctx(modifier.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            rendered = _render_text_from_numeric(
                value,
                source_tag=source_tag,
                suppress_zero=modifier.suppress_zero,
                exponential=modifier.exponential,
            )
            rendered += _termination_char(modifier.termination_code)
            targets = _sequential_tags(resolved_target, len(rendered))
            updates = {
                target.name: _as_single_ascii_char(char)
                for target, char in zip(targets, rendered, strict=True)
            }
        except (TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tags(updates)

    def _copy_binary_to_text(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        try:
            value = int(resolve_tag_or_value_ctx(modifier.source, ctx))
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            char = _ascii_char_from_code(value & 0xFF)
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tag(resolved_target.name, char)


class CallInstruction(Instruction):
    """Call subroutine instruction.

    Executes a named subroutine when the rung is true.
    The subroutine must be defined in the same Program.
    """

    def __init__(self, subroutine_name: str, program: Any):
        self.subroutine_name = subroutine_name
        self._program = program  # Reference to Program for subroutine lookup

    def execute(self, ctx: ScanContext) -> None:
        self._program.call_subroutine_ctx(self.subroutine_name, ctx)


class ReturnInstruction(Instruction):
    """Return from the current subroutine immediately."""

    def execute(self, ctx: ScanContext) -> None:  # noqa: ARG002
        raise SubroutineReturnSignal


class CountUpInstruction(Instruction):
    """Count Up (CTU) instruction.

    Terminal instruction that always executes and checks conditions independently:
    - UP condition: Increments accumulator EVERY SCAN when true
    - DOWN condition (optional): Decrements accumulator EVERY SCAN when true
    - RESET condition: Clears done bit and accumulator

    Click-specific:
    - Accumulator stored in CTD bank (DINT / 32-bit signed)
    - Done bit in CT bank
    - NOT edge-triggered - counts every scan while condition is true
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        up_condition: Any,
        reset_condition: Any,
        down_condition: Any = None,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.setpoint = setpoint

        # Convert Tags to Conditions if needed
        self.up_condition = self._to_condition(up_condition)
        self.reset_condition = self._to_condition(reset_condition)
        self.down_condition = self._to_condition(down_condition)

    def _resolve_setpoint_ctx(self, ctx: ScanContext) -> int:
        """Resolve setpoint to int value (supports Tag or literal)."""
        if isinstance(self.setpoint, Tag):
            return ctx.get_tag(self.setpoint.name, self.setpoint.default)
        return self.setpoint

    def _to_condition(self, obj: Any) -> Any:
        """Convert Tag to Condition if needed."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            else:
                raise TypeError(
                    f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                    "Use comparison operators: tag == value, tag > 0, etc."
                )
        return obj

    def always_execute(self) -> bool:
        """Counter always executes to check all conditions independently."""
        return True

    def execute(self, ctx: ScanContext) -> None:
        # Check reset condition first
        if self.reset_condition is not None:
            reset_active = self.reset_condition.evaluate(ctx)
            if reset_active:
                # Reset clears everything
                ctx.set_tags({self.done_bit.name: False, self.accumulator.name: 0})
                return

        # Get current accumulator value
        acc_value = ctx.get_tag(self.accumulator.name, 0)
        delta = 0

        # Check UP condition (counts every scan when true)
        up_curr = self.up_condition.evaluate(ctx) if self.up_condition else False
        if up_curr:
            delta += 1

        # Check DOWN condition (counts every scan when true, optional)
        if self.down_condition is not None:
            down_curr = self.down_condition.evaluate(ctx)
            if down_curr:
                delta -= 1

        # Apply net delta once, then clamp to DINT range
        acc_value = _clamp_dint(acc_value + delta)

        # Compute done bit (resolve setpoint dynamically)
        sp = self._resolve_setpoint_ctx(ctx)
        done = acc_value >= sp

        # Update tags
        ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})


class CountDownInstruction(Instruction):
    """Count Down (CTD) instruction.

    Terminal instruction that always executes and checks conditions independently:
    - DOWN condition: Decrements accumulator EVERY SCAN when true (starts at 0, goes negative)
    - RESET condition: Clears accumulator to 0 and clears done bit

    Click-specific:
    - Accumulator stored in CTD bank (DINT / 32-bit signed)
    - Done bit in CT bank
    - NOT edge-triggered - counts every scan while condition is true
    - Starts at 0 and counts down to negative values
    - Done bit activates when acc <= -setpoint
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        down_condition: Any,
        reset_condition: Any,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.setpoint = setpoint

        # Convert Tags to Conditions if needed
        self.down_condition = self._to_condition(down_condition)
        self.reset_condition = self._to_condition(reset_condition)

    def _resolve_setpoint_ctx(self, ctx: ScanContext) -> int:
        """Resolve setpoint to int value (supports Tag or literal)."""
        if isinstance(self.setpoint, Tag):
            return ctx.get_tag(self.setpoint.name, self.setpoint.default)
        return self.setpoint

    def _to_condition(self, obj: Any) -> Any:
        """Convert Tag to Condition if needed."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            else:
                raise TypeError(
                    f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                    "Use comparison operators: tag == value, tag > 0, etc."
                )
        return obj

    def always_execute(self) -> bool:
        """Counter always executes to check all conditions independently."""
        return True

    def execute(self, ctx: ScanContext) -> None:
        # Check reset condition first
        if self.reset_condition is not None:
            reset_active = self.reset_condition.evaluate(ctx)
            if reset_active:
                # CTD reset clears accumulator to 0
                ctx.set_tags({self.done_bit.name: False, self.accumulator.name: 0})
                return

        # Get current accumulator value (default 0)
        acc_value = ctx.get_tag(self.accumulator.name, 0)

        # Check DOWN condition (counts every scan when true)
        down_curr = self.down_condition.evaluate(ctx) if self.down_condition else False
        if down_curr:
            acc_value -= 1

        # Clamp to DINT range
        acc_value = _clamp_dint(acc_value)

        # Compute done bit (resolve setpoint dynamically)
        sp = self._resolve_setpoint_ctx(ctx)
        done = acc_value <= -sp

        # Update tags
        ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})


class OnDelayInstruction(Instruction):
    """On-Delay Timer (TON/RTON) instruction.

    Terminal instruction that accumulates time while enabled:
    - ENABLE condition (rung): Timer counts while true
    - RESET condition (optional): Clears done bit and accumulator

    Without reset (TON): Resets immediately when rung goes false.
    With reset (RTON): Holds value when rung goes false, manual reset required.

    Click-specific:
    - Accumulator stored in TD bank (INT type)
    - Done bit in T bank
    - Accumulator updates immediately (mid-scan visible)
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        reset_condition: Any = None,
        time_unit: TimeUnit = TimeUnit.Tms,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.setpoint = setpoint
        self.time_unit = time_unit
        self.has_reset = reset_condition is not None

        # Convert Tags to Conditions if needed
        self.enable_condition = self._to_condition(enable_condition)
        self.reset_condition = self._to_condition(reset_condition)

    def _resolve_setpoint_ctx(self, ctx: ScanContext) -> int:
        """Resolve setpoint to int value (supports Tag or literal)."""
        if isinstance(self.setpoint, Tag):
            return ctx.get_tag(self.setpoint.name, self.setpoint.default)
        return self.setpoint

    def _to_condition(self, obj: Any) -> Any:
        """Convert Tag to Condition if needed."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            else:
                raise TypeError(
                    f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                    "Use comparison operators: tag == value, tag > 0, etc."
                )
        return obj

    def always_execute(self) -> bool:
        """TON always executes to reset when rung goes false."""
        return True

    def execute(self, ctx: ScanContext) -> None:
        frac_key = f"_frac:{self.accumulator.name}"

        # Check reset condition first
        if self.reset_condition is not None:
            reset_active = self.reset_condition.evaluate(ctx)
            if reset_active:
                # Clear fractional accumulator too
                ctx.set_memory(frac_key, 0.0)
                ctx.set_tags({self.done_bit.name: False, self.accumulator.name: 0})
                return

        # Check enable condition
        enabled = self.enable_condition.evaluate(ctx) if self.enable_condition else True

        if enabled:
            # Get dt from context (injected by runner)
            dt = ctx.get_memory("_dt", 0.0)

            # Get current accumulator and fractional remainder
            acc_value = ctx.get_tag(self.accumulator.name, 0)
            frac = ctx.get_memory(frac_key, 0.0)

            # Convert dt to timer units and add fractional remainder
            dt_units = self.time_unit.dt_to_units(dt) + frac
            int_units = int(dt_units)
            new_frac = dt_units - int_units

            # Update accumulator, clamp at INT16_MAX (32767)
            acc_value = min(acc_value + int_units, 32767)

            # Compute done bit (resolve setpoint dynamically)
            sp = self._resolve_setpoint_ctx(ctx)
            done = acc_value >= sp

            # Update state
            ctx.set_memory(frac_key, new_frac)
            ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})
        else:
            # Disabled
            if self.has_reset:
                # RTON: Hold current values (do nothing)
                pass
            else:
                # TON: Reset immediately
                ctx.set_memory(frac_key, 0.0)
                ctx.set_tags({self.done_bit.name: False, self.accumulator.name: 0})


class OffDelayInstruction(Instruction):
    """Off-Delay Timer (TOF) instruction.

    Terminal instruction for off-delay timing:
    - While ENABLED: done = True, acc = 0
    - While DISABLED: acc counts up, done stays True until acc >= setpoint
    - When setpoint reached: done = False
    - Auto-resets when re-enabled

    Click-specific:
    - Accumulator stored in TD bank (INT type)
    - Done bit in T bank
    - Accumulator updates immediately (mid-scan visible)
    - If setpoint increases past accumulator after timeout, done re-enables
    """

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        setpoint: Tag | int,
        enable_condition: Any,
        time_unit: TimeUnit = TimeUnit.Tms,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.setpoint = setpoint
        self.time_unit = time_unit

        # Convert Tags to Conditions if needed
        self.enable_condition = self._to_condition(enable_condition)

    def _resolve_setpoint_ctx(self, ctx: ScanContext) -> int:
        """Resolve setpoint to int value (supports Tag or literal)."""
        if isinstance(self.setpoint, Tag):
            return ctx.get_tag(self.setpoint.name, self.setpoint.default)
        return self.setpoint

    def _to_condition(self, obj: Any) -> Any:
        """Convert Tag to Condition if needed."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            else:
                raise TypeError(
                    f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                    "Use comparison operators: tag == value, tag > 0, etc."
                )
        return obj

    def always_execute(self) -> bool:
        """Off-delay timers always execute (need to count while disabled)."""
        return True

    def execute(self, ctx: ScanContext) -> None:
        # Check enable condition
        enabled = self.enable_condition.evaluate(ctx) if self.enable_condition else True

        frac_key = f"_frac:{self.accumulator.name}"

        if enabled:
            # While enabled: done = True, acc = 0
            ctx.set_memory(frac_key, 0.0)
            ctx.set_tags({self.done_bit.name: True, self.accumulator.name: 0})
        else:
            # Disabled: count up towards (and past) setpoint
            acc_value = ctx.get_tag(self.accumulator.name, 0)
            sp = self._resolve_setpoint_ctx(ctx)

            # Always count while disabled (accumulator continues to max int)
            dt = ctx.get_memory("_dt", 0.0)
            frac = ctx.get_memory(frac_key, 0.0)

            dt_units = self.time_unit.dt_to_units(dt) + frac
            int_units = int(dt_units)
            new_frac = dt_units - int_units

            # Update accumulator, clamp at INT16_MAX (32767)
            acc_value = min(acc_value + int_units, 32767)

            # Done is True while acc < setpoint, False when acc >= setpoint
            done = acc_value < sp

            ctx.set_memory(frac_key, new_frac)
            ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})


class BlockCopyInstruction(OneShotMixin, Instruction):
    """Block copy instruction.

    Copies values from a source BlockRange to a destination BlockRange.
    Both ranges must have the same length.

    Source and dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    def __init__(self, source: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.dest = dest

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

        if isinstance(self.source, CopyModifier):
            self._execute_modifier_block_copy(ctx, self.source, dst_tags)
            return

        src_tags = resolve_block_range_tags_ctx(self.source, ctx)

        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        updates = {}
        for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
            value = ctx.get_tag(src_tag.name, src_tag.default)
            updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)

    def _execute_modifier_block_copy(
        self, ctx: ScanContext, modifier: CopyModifier, dst_tags: list[Tag]
    ) -> None:
        src_tags = resolve_block_range_tags_ctx(modifier.source, ctx)
        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        try:
            if modifier.mode in {"value", "ascii"}:
                updates = {}
                for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
                    char = _as_single_ascii_char(ctx.get_tag(src_tag.name, src_tag.default))
                    if char == "":
                        raise ValueError("empty CHAR cannot be converted to numeric")
                    updates[dst_tag.name] = _store_numeric_text_digits(
                        char, [dst_tag], mode=modifier.mode
                    )[dst_tag.name]
                ctx.set_tags(updates)
                return

            if modifier.mode == "text":
                rendered = "".join(
                    _render_text_from_numeric(
                        ctx.get_tag(src_tag.name, src_tag.default),
                        source_tag=src_tag,
                        suppress_zero=modifier.suppress_zero,
                        exponential=modifier.exponential,
                    )
                    for src_tag in src_tags
                )
                rendered += _termination_char(modifier.termination_code)
                if len(rendered) != len(dst_tags):
                    raise ValueError("formatted text length does not match destination range")
                updates = {
                    dst.name: _as_single_ascii_char(char)
                    for dst, char in zip(dst_tags, rendered, strict=True)
                }
                ctx.set_tags(updates)
                return

            if modifier.mode == "binary":
                updates = {}
                for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
                    updates[dst_tag.name] = _ascii_char_from_code(
                        int(ctx.get_tag(src_tag.name, src_tag.default)) & 0xFF
                    )
                ctx.set_tags(updates)
                return
        except (IndexError, TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        _set_fault_out_of_range(ctx)


def _store_copy_value_to_tag_type(value: Any, tag: Tag) -> Any:
    """Store value using copy/blockcopy/fill conversion semantics.

    - INT and DINT use saturating clamp.
    - Other destination types preserve current conversion behavior.
    """
    from pyrung.core.tag import TagType

    # Match existing conversion behavior for non-finite float sentinels.
    if isinstance(value, float) and (
        value != value or value == float("inf") or value == float("-inf")
    ):
        return 0

    if tag.type == TagType.INT:
        return _clamp_int(int(value))

    if tag.type == TagType.DINT:
        return _clamp_dint(int(value))

    return _truncate_to_tag_type(value, tag)


def _truncate_to_tag_type(value: Any, tag: Tag, mode: str = "decimal") -> Any:
    """Truncate a value to fit the destination tag's type.

    Implements hardware-verified modular wrapping used by math() result stores:
    - INT: 16-bit signed (-32768 to 32767)
    - DINT: 32-bit signed (-2147483648 to 2147483647)
    - WORD: 16-bit unsigned (0 to 65535)
    - REAL: 32-bit float (no truncation, just cast)
    - BOOL: truthiness
    - CHAR: no truncation

    In "hex" mode, all integer types wrap at 16-bit unsigned (0-65535).

    Args:
        value: The computed value to truncate.
        tag: The destination tag (used for type info).
        mode: "decimal" (default signed) or "hex" (unsigned 16-bit).

    Returns:
        Value truncated to the tag's type range.
    """
    from pyrung.core.tag import TagType

    # Handle division-by-zero sentinels (inf, nan)
    if isinstance(value, float) and (
        value != value or value == float("inf") or value == float("-inf")
    ):
        return 0

    if mode == "hex":
        # Hex mode: unsigned 16-bit wrap for all integer types
        return int(value) & 0xFFFF

    tag_type = tag.type

    if tag_type == TagType.BOOL:
        return bool(value)

    if tag_type == TagType.REAL:
        return float(value)

    if tag_type == TagType.CHAR:
        return value

    # Integer truncation with signed wrapping
    int_val = int(value)

    if tag_type == TagType.INT:
        # 16-bit signed: wrap to -32768..32767
        return ((int_val + 0x8000) & 0xFFFF) - 0x8000

    if tag_type == TagType.DINT:
        # 32-bit signed: wrap to -2147483648..2147483647
        return ((int_val + 0x80000000) & 0xFFFFFFFF) - 0x80000000

    if tag_type == TagType.WORD:
        # 16-bit unsigned: wrap to 0..65535
        return int_val & 0xFFFF

    # Fallback: no truncation
    return value


def _math_out_of_range_for_dest(value: Any, dest: Tag, mode: str) -> bool:
    """Return True if math result exceeds destination storage range."""
    from pyrung.core.tag import TagType

    if isinstance(value, float) and not math.isfinite(value):
        return False

    try:
        int_value = int(value)
    except (TypeError, ValueError, OverflowError):
        return False

    if mode == "hex":
        return int_value < 0 or int_value > 0xFFFF

    if dest.type == TagType.INT:
        return int_value < _INT_MIN or int_value > _INT_MAX
    if dest.type == TagType.DINT:
        return int_value < _DINT_MIN or int_value > _DINT_MAX
    if dest.type == TagType.WORD:
        return int_value < 0 or int_value > 0xFFFF
    return False


class MathInstruction(OneShotMixin, Instruction):
    """Math instruction.

    Evaluates an expression and stores the result in a destination tag,
    with truncation to the destination's type width.

    Key differences from CopyInstruction:
    - Truncates result to destination tag's bit width (modular wrapping)
    - Division by zero produces 0 (not infinity)
    - Supports "decimal" (signed) and "hex" (unsigned 16-bit) modes
    """

    def __init__(
        self,
        expression: Any,
        dest: Tag,
        oneshot: bool = False,
        mode: str = "decimal",
    ):
        OneShotMixin.__init__(self, oneshot)
        self.expression = expression
        self.dest = dest
        self.mode = mode

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        # Evaluate expression (handles Tag, Expression, IndirectRef, literal)
        try:
            value = resolve_tag_or_value_ctx(self.expression, ctx)
        except ZeroDivisionError:
            _set_fault_division_error(ctx)
            value = 0

        # Expression division may return non-finite sentinels for divide-by-zero.
        if isinstance(value, float) and not math.isfinite(value):
            _set_fault_division_error(ctx)
            value = 0

        if _math_out_of_range_for_dest(value, self.dest, self.mode):
            _set_fault_out_of_range(ctx)

        # Truncate to destination type
        value = _truncate_to_tag_type(value, self.dest, self.mode)

        # Resolve destination name (handles indirect)
        target_name = resolve_tag_name_ctx(self.dest, ctx)
        ctx.set_tag(target_name, value)


class FillInstruction(OneShotMixin, Instruction):
    """Fill instruction.

    Writes a constant value to every element in a destination BlockRange.

    Value can be a literal, Tag, or Expression (resolved once, written to all).
    Dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    def __init__(self, value: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.value = value
        self.dest = dest

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)
        if isinstance(self.value, CopyModifier):
            self._execute_modifier_fill(ctx, self.value, dst_tags)
            return

        value = resolve_tag_or_value_ctx(self.value, ctx)

        updates = {}
        for dst_tag in dst_tags:
            if dst_tag.type.name == "CHAR":
                updates[dst_tag.name] = _as_single_ascii_char(value)
            else:
                updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)

    def _execute_modifier_fill(
        self, ctx: ScanContext, modifier: CopyModifier, dst_tags: list[Tag]
    ) -> None:
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import TagType

        if not dst_tags:
            return

        if modifier.mode in {"value", "ascii"}:
            text = _text_from_source_value(resolve_tag_or_value_ctx(modifier.source, ctx))
            if len(text) != 1:
                raise ValueError("fill text->numeric conversion requires a single source character")
            numeric = _store_numeric_text_digits(text, [dst_tags[0]], mode=modifier.mode)[
                dst_tags[0].name
            ]
            updates = {tag.name: _store_copy_value_to_tag_type(numeric, tag) for tag in dst_tags}
            ctx.set_tags(updates)
            return

        if modifier.mode == "text":
            if any(tag.type != TagType.CHAR for tag in dst_tags):
                raise TypeError("fill(as_text(...)) requires CHAR destination range")

            source_tag: Tag | None = None
            if isinstance(modifier.source, Tag):
                source_tag = modifier.source
            elif isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                source_tag = resolve_tag_ctx(modifier.source, ctx)

            rendered = _render_text_from_numeric(
                resolve_tag_or_value_ctx(modifier.source, ctx),
                source_tag=source_tag,
                suppress_zero=modifier.suppress_zero,
                exponential=modifier.exponential,
            )
            rendered += _termination_char(modifier.termination_code)
            if len(rendered) > len(dst_tags):
                raise ValueError("formatted fill text exceeds destination range")

            updates: dict[str, Any] = {}
            for idx, dst in enumerate(dst_tags):
                updates[dst.name] = (
                    _as_single_ascii_char(rendered[idx]) if idx < len(rendered) else ""
                )
            ctx.set_tags(updates)
            return

        if modifier.mode == "binary":
            code = int(resolve_tag_or_value_ctx(modifier.source, ctx)) & 0xFF
            char = _ascii_char_from_code(code)
            updates = {tag.name: char for tag in dst_tags}
            ctx.set_tags(updates)
            return

        raise ValueError(f"Unsupported fill modifier mode: {modifier.mode}")


class SearchInstruction(OneShotMixin, Instruction):
    """Search instruction.

    Scans a selected range for the first value (or text window) matching
    the given condition and writes:
    - result: matched address, or -1 on miss
    - found: True on hit, False on miss
    """

    def __init__(
        self,
        condition: str,
        value: Any,
        search_range: BlockRange | IndirectBlockRange,
        result: Tag,
        found: Tag,
        continuous: bool = False,
        oneshot: bool = False,
    ):
        from pyrung.core.memory_block import BlockRange, IndirectBlockRange
        from pyrung.core.tag import TagType

        if condition not in _SEARCH_OPERATOR_MAP:
            raise ValueError(
                f"Invalid search condition: {condition!r}. Expected one of: ==, !=, <, <=, >, >="
            )
        if not isinstance(search_range, (BlockRange, IndirectBlockRange)):
            raise TypeError(
                "search_range must be BlockRange or IndirectBlockRange from .select(), "
                f"got {type(search_range).__name__}"
            )
        if found.type != TagType.BOOL:
            raise TypeError(f"search found tag must be BOOL, got {found.type.name}")
        if result.type not in {TagType.INT, TagType.DINT}:
            raise TypeError(f"search result tag must be INT or DINT, got {result.type.name}")

        OneShotMixin.__init__(self, oneshot)
        self.condition = condition
        self.value = value
        self.search_range = search_range
        self.result = result
        self.found = found
        self.continuous = continuous
        self._compare = _SEARCH_OPERATOR_MAP[condition]

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        resolved_range = resolve_block_range_ctx(self.search_range, ctx)
        addresses = list(resolved_range.addresses)
        tags = resolved_range.tags()

        if not addresses:
            self._write_miss(ctx)
            return

        cursor_index = self._resolve_cursor_index(
            addresses=addresses,
            reverse_order=resolved_range.reverse_order,
            ctx=ctx,
        )
        if cursor_index is None:
            self._write_miss(ctx)
            return

        if self._is_text_path(tags):
            matched_address = self._search_text(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                ctx=ctx,
            )
        else:
            matched_address = self._search_numeric(
                tags=tags,
                addresses=addresses,
                cursor_index=cursor_index,
                ctx=ctx,
            )

        if matched_address is None:
            self._write_miss(ctx)
            return

        ctx.set_tags({self.result.name: matched_address, self.found.name: True})

    def _write_miss(self, ctx: ScanContext) -> None:
        ctx.set_tags({self.result.name: -1, self.found.name: False})

    def _resolve_cursor_index(
        self, addresses: list[int], reverse_order: bool, ctx: ScanContext
    ) -> int | None:
        if not self.continuous:
            return 0

        current_result = int(ctx.get_tag(self.result.name, self.result.default))
        if current_result == 0:
            return 0
        if current_result == -1:
            return None

        if reverse_order:
            for idx, addr in enumerate(addresses):
                if addr < current_result:
                    return idx
            return None

        for idx, addr in enumerate(addresses):
            if addr > current_result:
                return idx
        return None

    def _is_text_path(self, tags: list[Tag]) -> bool:
        from pyrung.core.tag import TagType

        first_type = tags[0].type
        if first_type == TagType.CHAR:
            for tag in tags:
                if tag.type != TagType.CHAR:
                    raise TypeError(
                        "search text ranges must contain only CHAR tags; "
                        f"got {tag.type.name} at {tag.name}"
                    )
            return True

        if first_type in {TagType.INT, TagType.DINT, TagType.REAL, TagType.WORD}:
            return False

        raise TypeError(
            "search range tags must be INT, DINT, REAL, WORD, or CHAR; "
            f"got {first_type.name} at {tags[0].name}"
        )

    def _search_numeric(
        self, tags: list[Tag], addresses: list[int], cursor_index: int, ctx: ScanContext
    ) -> int | None:
        rhs_value = resolve_tag_or_value_ctx(self.value, ctx)

        for idx in range(cursor_index, len(tags)):
            candidate = ctx.get_tag(tags[idx].name, tags[idx].default)
            if self._compare(candidate, rhs_value):
                return addresses[idx]
        return None

    def _search_text(
        self, tags: list[Tag], addresses: list[int], cursor_index: int, ctx: ScanContext
    ) -> int | None:
        if self.condition not in {"==", "!="}:
            raise ValueError("Text search only supports '==' and '!=' conditions")

        rhs_text = str(resolve_tag_or_value_ctx(self.value, ctx))
        if rhs_text == "":
            raise ValueError("Text search value cannot be empty")

        window_len = len(rhs_text)
        if window_len > len(tags):
            return None

        last_start = len(tags) - window_len
        if cursor_index > last_start:
            return None

        for start in range(cursor_index, last_start + 1):
            candidate = "".join(
                str(ctx.get_tag(tags[start + offset].name, tags[start + offset].default))
                for offset in range(window_len)
            )
            if self.condition == "==" and candidate == rhs_text:
                return addresses[start]
            if self.condition == "!=" and candidate != rhs_text:
                return addresses[start]
        return None


class ShiftInstruction(Instruction):
    """Shift register instruction.

    Terminal instruction that always executes and checks:
    - data condition (rung combined condition) for inserted bit value
    - clock condition for OFF->ON edge shift trigger
    - reset condition (level) to clear all bits in the range
    """

    def __init__(
        self,
        bit_range: BlockRange | IndirectBlockRange,
        data_condition: Any,
        clock_condition: Any,
        reset_condition: Any,
    ):
        from pyrung.core.memory_block import BlockRange, IndirectBlockRange

        if not isinstance(bit_range, (BlockRange, IndirectBlockRange)):
            raise TypeError(
                f"shift bit_range must be BlockRange or IndirectBlockRange, "
                f"got {type(bit_range).__name__}"
            )

        self.bit_range = bit_range
        self.data_condition = self._to_condition(data_condition)
        self.clock_condition = self._to_condition(clock_condition)
        self.reset_condition = self._to_condition(reset_condition)
        self._prev_clock_key = f"_shift_prev_clock:{id(self)}"

        if self.clock_condition is None:
            raise ValueError("shift requires a clock condition")
        if self.reset_condition is None:
            raise ValueError("shift requires a reset condition")

    def _to_condition(self, obj: Any) -> Any:
        """Convert a BOOL tag to BitCondition for condition inputs."""
        from pyrung.core.condition import BitCondition
        from pyrung.core.tag import Tag as TagClass
        from pyrung.core.tag import TagType

        if obj is None:
            return None
        if isinstance(obj, TagClass):
            if obj.type == TagType.BOOL:
                return BitCondition(obj)
            raise TypeError(
                f"Non-BOOL tag '{obj.name}' cannot be used directly as condition. "
                "Use comparison operators: tag == value, tag > 0, etc."
            )
        return obj

    def _resolve_tags(self, ctx: ScanContext) -> list[Tag]:
        from pyrung.core.tag import TagType

        tags = resolve_block_range_tags_ctx(self.bit_range, ctx)
        if not tags:
            raise ValueError("shift bit_range resolved to an empty range")
        for tag in tags:
            if tag.type != TagType.BOOL:
                raise TypeError(
                    f"shift bit_range must contain only BOOL tags; "
                    f"got {tag.type.name} at {tag.name}"
                )
        return tags

    def always_execute(self) -> bool:
        """Shift must always run to capture clock edges while rung is false."""
        return True

    def execute(self, ctx: ScanContext) -> None:
        tags = self._resolve_tags(ctx)

        data_bit = self.data_condition.evaluate(ctx) if self.data_condition is not None else True
        clock_curr = bool(self.clock_condition.evaluate(ctx))
        clock_prev = bool(ctx.get_memory(self._prev_clock_key, False))
        rising_edge = clock_curr and not clock_prev

        if rising_edge:
            prev_values = [bool(ctx.get_tag(tag.name, tag.default)) for tag in tags]
            updates = {tags[0].name: bool(data_bit)}
            for idx, tag in enumerate(tags[1:], start=1):
                updates[tag.name] = prev_values[idx - 1]
            ctx.set_tags(updates)

        reset_active = bool(self.reset_condition.evaluate(ctx))
        if reset_active:
            ctx.set_tags({tag.name: False for tag in tags})

        ctx.set_memory(self._prev_clock_key, clock_curr)


class PackBitsInstruction(OneShotMixin, Instruction):
    """Pack BOOL tags from a BlockRange into a destination register."""

    def __init__(self, bit_block: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.bit_block = bit_block
        self.dest = dest

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"pack_bits destination must be INT, WORD, DINT, or REAL; got {dest_tag.type.name}"
            )

        bit_tags = resolve_block_range_tags_ctx(self.bit_block, ctx)
        width = 16 if dest_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"pack_bits destination width is {width} bits but block has {len(bit_tags)} tags"
            )

        packed = 0
        for bit_index, bit_tag in enumerate(bit_tags):
            if bit_tag.type != TagType.BOOL:
                raise TypeError(
                    f"pack_bits source tags must be BOOL; got {bit_tag.type.name} at {bit_tag.name}"
                )
            bit_value = ctx.get_tag(bit_tag.name, bit_tag.default)
            if bool(bit_value):
                packed |= 1 << bit_index

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackWordsInstruction(OneShotMixin, Instruction):
    """Pack two 16-bit tags into a 32-bit destination register."""

    def __init__(self, word_block: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.word_block = word_block
        self.dest = dest

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"pack_words destination must be DINT or REAL; got {dest_tag.type.name}"
            )

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(f"pack_words requires exactly 2 source tags; got {len(word_tags)}")
        if word_tags[0].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"pack_words source tags must be INT or WORD; got {word_tags[0].type.name} "
                f"at {word_tags[0].name}"
            )
        if word_tags[1].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"pack_words source tags must be INT or WORD; got {word_tags[1].type.name} "
                f"at {word_tags[1].name}"
            )

        lo_value = ctx.get_tag(word_tags[0].name, word_tags[0].default)
        hi_value = ctx.get_tag(word_tags[1].name, word_tags[1].default)
        packed = (int(hi_value) << 16) | (int(lo_value) & 0xFFFF)

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackTextInstruction(OneShotMixin, Instruction):
    """Pack Copy text mode: parse CHAR range into a numeric destination."""

    def __init__(
        self, source_range: Any, dest: Any, *, allow_whitespace: bool = False, oneshot: bool = False
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source_range = source_range
        self.dest = dest
        self.allow_whitespace = bool(allow_whitespace)

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL}:
            raise TypeError(
                f"pack_text destination must be INT, DINT, WORD, or REAL; got {dest_tag.type.name}"
            )

        src_tags = resolve_block_range_tags_ctx(self.source_range, ctx)
        for src in src_tags:
            if src.type != TagType.CHAR:
                raise TypeError(
                    f"pack_text source range must contain only CHAR tags; got {src.type.name} at {src.name}"
                )

        try:
            text = "".join(
                _as_single_ascii_char(ctx.get_tag(src.name, src.default)) for src in src_tags
            )
            if not self.allow_whitespace and text != text.strip():
                _set_fault_out_of_range(ctx)
                return
            if self.allow_whitespace:
                text = text.strip()
            parsed = _parse_pack_text_value(text, dest_tag)
        except (TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tag(dest_tag.name, _store_copy_value_to_tag_type(parsed, dest_tag))


class UnpackToBitsInstruction(OneShotMixin, Instruction):
    """Unpack a register value into individual BOOL tags in a BlockRange."""

    def __init__(self, source: Any, bit_block: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.bit_block = bit_block

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        if source_tag.type not in {TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL}:
            raise TypeError(
                "unpack_to_bits source must be INT, WORD, DINT, or REAL; "
                f"got {source_tag.type.name}"
            )

        bit_tags = resolve_block_range_tags_ctx(self.bit_block, ctx)
        width = 16 if source_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"unpack_to_bits source width is {width} bits but block has {len(bit_tags)} tags"
            )

        source_value = ctx.get_tag(source_tag.name, source_tag.default)
        if source_tag.type == TagType.REAL:
            bits = _float_to_int_bits(source_value)
        elif source_tag.type in {TagType.INT, TagType.WORD}:
            bits = int(source_value) & 0xFFFF
        else:  # DINT
            bits = int(source_value) & 0xFFFFFFFF

        updates = {}
        for bit_index, bit_tag in enumerate(bit_tags):
            if bit_tag.type != TagType.BOOL:
                raise TypeError(
                    f"unpack_to_bits destination tags must be BOOL; got "
                    f"{bit_tag.type.name} at {bit_tag.name}"
                )
            updates[bit_tag.name] = bool((bits >> bit_index) & 1)
        ctx.set_tags(updates)


class UnpackToWordsInstruction(OneShotMixin, Instruction):
    """Unpack a 32-bit register value into two 16-bit destination tags."""

    def __init__(self, source: Any, word_block: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.word_block = word_block

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        if source_tag.type not in {TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"unpack_to_words source must be DINT or REAL; got {source_tag.type.name}"
            )

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(
                f"unpack_to_words requires exactly 2 destination tags; got {len(word_tags)}"
            )
        if word_tags[0].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"unpack_to_words destination tags must be INT or WORD; got "
                f"{word_tags[0].type.name} at {word_tags[0].name}"
            )
        if word_tags[1].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"unpack_to_words destination tags must be INT or WORD; got "
                f"{word_tags[1].type.name} at {word_tags[1].name}"
            )

        source_value = ctx.get_tag(source_tag.name, source_tag.default)
        bits = (
            _float_to_int_bits(source_value)
            if source_tag.type == TagType.REAL
            else (int(source_value) & 0xFFFFFFFF)
        )

        lo_word = bits & 0xFFFF
        hi_word = (bits >> 16) & 0xFFFF

        updates = {
            word_tags[0].name: _truncate_to_tag_type(lo_word, word_tags[0]),
            word_tags[1].name: _truncate_to_tag_type(hi_word, word_tags[1]),
        }
        ctx.set_tags(updates)
