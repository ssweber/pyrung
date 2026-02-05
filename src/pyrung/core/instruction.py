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
    from pyrung.core.memory_bank import IndirectRef


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
    from pyrung.core.memory_bank import IndirectExprRef
    from pyrung.core.memory_bank import IndirectRef as IndirectRefType

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


def resolve_tag_name_ctx(target: Tag | IndirectRef, ctx: ScanContext) -> str:
    """Resolve tag to its name (handling indirect) using ScanContext.

    Args:
        target: Tag, IndirectRef, or IndirectExprRef to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        The tag name string.
    """
    # Import here to avoid circular imports
    from pyrung.core.memory_bank import IndirectExprRef
    from pyrung.core.memory_bank import IndirectRef as IndirectRefType

    # Check for IndirectExprRef first
    if isinstance(target, IndirectExprRef):
        resolved_tag = target.resolve_ctx(ctx)
        return resolved_tag.name
    # Check for IndirectRef
    if isinstance(target, IndirectRefType):
        resolved_tag = target.resolve_ctx(ctx)
        return resolved_tag.name
    # Regular Tag
    return target.name


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


def resolve_block_range_ctx(block_range: Any, ctx: ScanContext) -> tuple[list[str], list[Any]]:
    """Resolve a BlockRange or IndirectBlockRange to tag names and defaults.

    Args:
        block_range: BlockRange or IndirectBlockRange to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        Tuple of (tag_names, defaults) lists.
    """
    from pyrung.core.memory_bank import BlockRange, IndirectBlockRange

    if isinstance(block_range, IndirectBlockRange):
        block_range = block_range.resolve_ctx(ctx)

    if not isinstance(block_range, BlockRange):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(block_range).__name__}"
        )

    tags = block_range.tags()
    return [t.name for t in tags], [t.default for t in tags]


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

        # Resolve target name (handles Tag or IndirectRef)
        target_name = resolve_tag_name_ctx(self.target, ctx)

        ctx.set_tag(target_name, value)


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
    - Accumulator stored in CTD bank (INT2 / 32-bit signed)
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

        # Check UP condition (counts every scan when true)
        up_curr = self.up_condition.evaluate(ctx) if self.up_condition else False
        if up_curr:
            acc_value += 1

        # Check DOWN condition (counts every scan when true, optional)
        if self.down_condition is not None:
            down_curr = self.down_condition.evaluate(ctx)
            if down_curr:
                acc_value -= 1

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
    - Accumulator stored in CTD bank (INT2 / 32-bit signed)
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

        src_names, src_defaults = resolve_block_range_ctx(self.source, ctx)
        dst_names, _ = resolve_block_range_ctx(self.dest, ctx)

        if len(src_names) != len(dst_names):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_names)} elements, "
                f"dest has {len(dst_names)} elements"
            )

        updates = {}
        for src_name, src_default, dst_name in zip(src_names, src_defaults, dst_names, strict=True):
            updates[dst_name] = ctx.get_tag(src_name, src_default)
        ctx.set_tags(updates)


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
        dst_names, _ = resolve_block_range_ctx(self.dest, ctx)

        updates = {name: value for name in dst_names}
        ctx.set_tags(updates)
