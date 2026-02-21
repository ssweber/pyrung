"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .base import Instruction
from .conversions import (
    _clamp_dint,
)
from .utils import (
    resolve_setpoint_ctx,
    to_condition,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class CountUpInstruction(Instruction):
    """Count-Up (CTU) — and optionally bidirectional — counter.

    Increments the accumulator every scan the rung is True.  Optionally
    decrements when a separate down condition is True (bidirectional counter).

    .. warning::
        Not edge-triggered. The accumulator advances by one per scan while
        the enable condition is True.  Wrap `rise()` around the rung condition
        to count leading edges instead.

    - Accumulator type: DINT (32-bit signed, clamps at ±2 147 483 647).
    - Done bit: True when acc ≥ setpoint.
    - Reset condition: clears acc to 0 and done to False (level-sensitive).

    Args:
        done_bit: BOOL tag set when acc ≥ setpoint.
        accumulator: DINT tag storing the current count.
        setpoint: Target count (constant or DINT tag).
        up_condition: Rung power condition (injected automatically by DSL).
        reset_condition: Tag or condition that resets acc and done.
        down_condition: Optional tag or condition for bidirectional counting.
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

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
        self.up_condition = to_condition(up_condition)
        self.reset_condition = to_condition(reset_condition)
        self.down_condition = to_condition(down_condition)

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
        sp = resolve_setpoint_ctx(self.setpoint, ctx)
        done = acc_value >= sp

        # Update tags
        ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})


class CountDownInstruction(Instruction):
    """Count-Down (CTD) counter.

    Decrements the accumulator every scan the rung is True.  The accumulator
    starts at 0 and counts negative; done = True when acc ≤ −setpoint.

    .. warning::
        Not edge-triggered. The accumulator decrements by one per scan while
        the enable condition is True.  Wrap `rise()` around the rung condition
        to count leading edges instead.

    - Accumulator type: DINT (32-bit signed, clamps at ±2 147 483 647).
    - Reset condition: resets acc to 0 and done to False (level-sensitive).

    Args:
        done_bit: BOOL tag set when acc ≤ −setpoint.
        accumulator: DINT tag storing the current (negative) count.
        setpoint: Target magnitude (positive constant or DINT tag).
        down_condition: Rung power condition (injected automatically by DSL).
        reset_condition: Tag or condition that resets acc and done.
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

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
        self.down_condition = to_condition(down_condition)
        self.reset_condition = to_condition(reset_condition)

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
        sp = resolve_setpoint_ctx(self.setpoint, ctx)
        done = acc_value <= -sp

        # Update tags
        ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})
