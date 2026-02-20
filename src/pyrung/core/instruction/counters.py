"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .base import Instruction
from .conversions import (
    _clamp_dint,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


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

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
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
        if enabled:
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

    def is_inert_when_disabled(self) -> bool:
        return False


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

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
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
        if enabled:
            acc_value -= 1

        # Clamp to DINT range
        acc_value = _clamp_dint(acc_value)

        # Compute done bit (resolve setpoint dynamically)
        sp = self._resolve_setpoint_ctx(ctx)
        done = acc_value <= -sp

        # Update tags
        ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})

    def is_inert_when_disabled(self) -> bool:
        return False
