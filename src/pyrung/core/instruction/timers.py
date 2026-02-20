"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from .base import Instruction
from .utils import (
    resolve_setpoint_ctx,
    to_condition,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


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

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

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
            dt_units = self.time_unit.dt_to_units(dt) + frac
            int_units = int(dt_units)
            new_frac = dt_units - int_units

            # Update accumulator, clamp at INT16_MAX (32767)
            acc_value = min(acc_value + int_units, 32767)

            # Compute done bit (resolve setpoint dynamically)
            sp = resolve_setpoint_ctx(self.setpoint, ctx)
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

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False

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
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        frac_key = f"_frac:{self.accumulator.name}"

        if enabled:
            # While enabled: done = True, acc = 0
            ctx.set_memory(frac_key, 0.0)
            ctx.set_tags({self.done_bit.name: True, self.accumulator.name: 0})
        else:
            # Disabled: count up towards (and past) setpoint
            acc_value = ctx.get_tag(self.accumulator.name, 0)
            sp = resolve_setpoint_ctx(self.setpoint, ctx)

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
