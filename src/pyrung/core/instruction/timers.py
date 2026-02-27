"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from .base import Instruction
from .utils import (
    resolve_preset_ctx,
    to_condition,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class OnDelayInstruction(Instruction):
    """On-Delay Timer — TON (auto-reset) or RTON (manual reset).

    Accumulates elapsed time while the rung is True. When the accumulator
    reaches `preset`, the done bit is set True.

    **TON (no reset arg):**
    Resets the accumulator and done bit immediately when the rung goes False.

    **RTON (`.reset(tag)` provided):**
    Holds the accumulator and done bit when the rung goes False.
    The reset condition clears both regardless of rung state.

    The accumulator is an INT tag (max 32 767). It clamps at the maximum and
    never overflows. `preset` may be an INT tag (dynamic) or a constant.

    Args:
        done_bit: BOOL tag set when acc ≥ preset.
        accumulator: INT tag storing elapsed time in the selected `unit`.
        preset: Target value (constant or INT tag).
        enable_condition: Rung power condition (injected automatically by DSL).
        reset_condition: Optional condition to reset acc+done (creates RTON).
        unit: Time unit for accumulator. Default `Tms` (milliseconds).
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        preset: Tag | int,
        enable_condition: Any,
        reset_condition: Any = None,
        unit: TimeUnit = TimeUnit.Tms,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.preset = preset
        self.unit = unit
        self.has_reset = reset_condition is not None

        # Convert Tags to Conditions if needed
        self.enable_condition = to_condition(enable_condition)
        self.reset_condition = to_condition(reset_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        frac_key = f"_frac:{self.accumulator.name}"

        # Check reset condition first
        if self.reset_condition is not None:
            reset_active = self.reset_condition.evaluate(ctx)
            if reset_active:
                # Clear fractional accumulator too
                ctx.set_memory(frac_key, 0.0)
                ctx.set_tags({self.done_bit.name: False, self.accumulator.name: 0})
                return

        if enabled:
            # Get dt from context (injected by runner)
            dt = ctx.get_memory("_dt", 0.0)

            # Get current accumulator and fractional remainder
            acc_value = ctx.get_tag(self.accumulator.name, 0)
            frac = ctx.get_memory(frac_key, 0.0)

            # Convert dt to timer units and add fractional remainder
            dt_units = self.unit.dt_to_units(dt) + frac
            int_units = int(dt_units)
            new_frac = dt_units - int_units

            # Update accumulator, clamp at INT16_MAX (32767)
            acc_value = min(acc_value + int_units, 32767)

            # Compute done bit (resolve preset dynamically)
            sp = resolve_preset_ctx(self.preset, ctx)
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

    def is_terminal(self) -> bool:
        return self.has_reset


class OffDelayInstruction(Instruction):
    """Off-Delay Timer (TOF).

    Keeps the done bit True for a specified time after the rung goes False.

    - **Rung True:** done = True, accumulator = 0 (resets immediately)
    - **Rung False:** accumulator counts up; done = False once acc ≥ preset
    - **Re-enabling:** re-enables immediately (acc and done reset)

    If `preset` is a dynamic tag that increases past the current accumulator
    after the timer has already fired, done re-enables until acc catches up.

    Args:
        done_bit: BOOL tag that stays True until delay expires.
        accumulator: INT tag storing elapsed off-time in `unit` ticks.
        preset: Delay duration (constant or INT tag).
        enable_condition: Rung power condition (injected automatically by DSL).
        unit: Time unit for accumulator. Default `Tms` (milliseconds).
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        preset: Tag | int,
        enable_condition: Any,
        unit: TimeUnit = TimeUnit.Tms,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.preset = preset
        self.unit = unit

        # Convert Tags to Conditions if needed
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        frac_key = f"_frac:{self.accumulator.name}"

        if enabled:
            # While enabled: done = True, acc = 0
            ctx.set_memory(frac_key, 0.0)
            ctx.set_tags({self.done_bit.name: True, self.accumulator.name: 0})
        else:
            # Disabled: count up towards (and past) preset
            acc_value = ctx.get_tag(self.accumulator.name, 0)
            sp = resolve_preset_ctx(self.preset, ctx)

            # Always count while disabled (accumulator continues to max int)
            dt = ctx.get_memory("_dt", 0.0)
            frac = ctx.get_memory(frac_key, 0.0)

            dt_units = self.unit.dt_to_units(dt) + frac
            int_units = int(dt_units)
            new_frac = dt_units - int_units

            # Update accumulator, clamp at INT16_MAX (32767)
            acc_value = min(acc_value + int_units, 32767)

            # Done is True while acc < preset, False when acc >= preset
            done = acc_value < sp

            ctx.set_memory(frac_key, new_frac)
            ctx.set_tags({self.done_bit.name: done, self.accumulator.name: acc_value})
