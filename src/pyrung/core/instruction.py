"""Instruction classes for the immutable PLC engine.

Instructions execute within a ScanContext, writing to batched evolvers.
All state modifications are collected and committed at scan end.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectRef


_DINT_MIN = -2147483648
_DINT_MAX = 2147483647
_INT_MIN = -32768
_INT_MAX = 32767


def _clamp_dint(value: int) -> int:
    """Clamp integer to DINT (32-bit signed) range."""
    return max(_DINT_MIN, min(_DINT_MAX, value))


def _clamp_int(value: int) -> int:
    """Clamp integer to INT (16-bit signed) range."""
    return max(_INT_MIN, min(_INT_MAX, value))


def resolve_tag_or_value_ctx(source: Tag | IndirectRef | Any, ctx: ScanContext) -> Any:
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


def resolve_tag_ctx(target: Tag | IndirectRef, ctx: ScanContext) -> Tag:
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


def resolve_tag_name_ctx(target: Tag | IndirectRef, ctx: ScanContext) -> str:
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


def resolve_block_range_tags_ctx(block_range: Any, ctx: ScanContext) -> list[Tag]:
    """Resolve a BlockRange or IndirectBlockRange to a list of Tags.

    Args:
        block_range: BlockRange or IndirectBlockRange to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        List of resolved Tag objects (with type info preserved).
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(block_range, IndirectBlockRange):
        block_range = block_range.resolve_ctx(ctx)

    if not isinstance(block_range, BlockRange):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(block_range).__name__}"
        )

    return block_range.tags()


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


class OutInstruction(OneShotMixin, Instruction):
    """Output coil instruction (OUT).

    Sets the target bit to True when executed.
    """

    def __init__(self, target: Tag, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return
        ctx.set_tag(self.target.name, True)


class LatchInstruction(Instruction):
    """Latch/Set instruction (SET).

    Sets the target bit to True. Unlike OUT, this is typically
    not reset when the rung goes false.
    """

    def __init__(self, target: Tag):
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        ctx.set_tag(self.target.name, True)


class ResetInstruction(Instruction):
    """Reset/Unlatch instruction (RST).

    Sets the target to its default value (False for bits, 0 for ints).
    """

    def __init__(self, target: Tag):
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        ctx.set_tag(self.target.name, self.target.default)


class CopyInstruction(OneShotMixin, Instruction):
    """Copy instruction (CPY/MOV).

    Copies a value from source to target.
    Source can be a literal value, Tag, or IndirectRef.
    Target can be a Tag or IndirectRef.
    """

    def __init__(
        self, source: Tag | IndirectRef | Any, target: Tag | IndirectRef, oneshot: bool = False
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.target = target

    def execute(self, ctx: ScanContext) -> None:
        if not self.should_execute():
            return

        # Resolve source value (handles Tag, IndirectRef, or literal)
        value = resolve_tag_or_value_ctx(self.source, ctx)

        # Resolve target tag (handles Tag or IndirectRef)
        resolved_target = resolve_tag_ctx(self.target, ctx)

        # Copy-family store semantics: clamp signed integer overflow.
        value = _store_copy_value_to_tag_type(value, resolved_target)

        ctx.set_tag(resolved_target.name, value)


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

        src_tags = resolve_block_range_tags_ctx(self.source, ctx)
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

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
            value = 0

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

        value = resolve_tag_or_value_ctx(self.value, ctx)
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

        updates = {}
        for dst_tag in dst_tags:
            updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)
