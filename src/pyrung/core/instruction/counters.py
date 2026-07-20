"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition
from pyrung.core.tag import Tag

from .advance import AdvanceProfile, ConditionDemand, monotone_profile
from .base import Instruction
from .conversions import (
    _clamp_dint,
)
from .utils import (
    instruction_condition_view,
    resolve_preset_ctx,
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
    - Done bit: True when acc ≥ preset.
    - Reset condition: clears acc to 0 and done to False (level-sensitive).

    Args:
        done_bit: BOOL tag set when acc ≥ preset.
        accumulator: DINT tag storing the current count.
        preset: Target count (constant or DINT tag).
        up_condition: Rung power condition (injected automatically by DSL).
        reset_condition: Tag or condition that resets acc and done.
        down_condition: Optional tag or condition for bidirectional counting.
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _reads = ("preset",)
    _writes = ("done_bit", "accumulator")
    _conditions = ("up_condition", "down_condition", "reset_condition")
    _structural_fields = ()
    _exclusive_fields = ("accumulator",)
    _status_fields = ("cu_bit", "cd_bit")

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        preset: Tag | int,
        up_condition: Any,
        reset_condition: Any,
        down_condition: Any = None,
        cu_bit: Tag | None = None,
        cd_bit: Tag | None = None,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.preset = preset
        self.cu_bit = cu_bit
        self.cd_bit = cd_bit

        # Convert Tags to Conditions if needed
        self.up_condition = to_condition(up_condition)
        self.reset_condition = to_condition(reset_condition)
        self.down_condition = to_condition(down_condition)

    def _status_tags(self, cu: bool, cd: bool) -> dict[str, bool]:
        tags: dict[str, bool] = {}
        if self.cu_bit is not None:
            tags[self.cu_bit.name] = cu
        if self.cd_bit is not None:
            tags[self.cd_bit.name] = cd
        return tags

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        condition_view = instruction_condition_view(ctx)

        reset_active = self.reset_condition is not None and self.reset_condition.evaluate(
            condition_view
        )
        down_active = self.down_condition is not None and self.down_condition.evaluate(
            condition_view
        )
        acc_value = ctx.get_tag(self.accumulator.name, 0)
        sp = resolve_preset_ctx(self.preset, ctx)

        if reset_active:
            ctx.set_tags(
                {
                    self.done_bit.name: False,
                    self.accumulator.name: 0,
                    **self._status_tags(enabled, down_active),
                }
            )
        else:
            delta = (1 if enabled else 0) - (1 if down_active else 0)
            acc_value = _clamp_dint(acc_value + delta)
            ctx.set_tags(
                {
                    self.done_bit.name: acc_value >= sp,
                    self.accumulator.name: acc_value,
                    **self._status_tags(enabled, down_active),
                }
            )

    def is_terminal(self) -> bool:
        return True

    def advance_profile(self) -> AdvanceProfile:
        return monotone_profile(
            channels=(self.accumulator, self.done_bit),
            accumulator=self.accumulator,
            done=self.done_bit,
            active=None,
            preset=self.preset,
            direction=1,
            rate_per_scan=lambda _dt: 1.0,
            advance=ConditionDemand(self.up_condition),
            advance_is_pulse=isinstance(
                self.up_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
            restore=(
                ConditionDemand(self.reset_condition) if self.reset_condition is not None else None
            ),
            restore_is_pulse=isinstance(
                self.reset_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
        )


class CountDownInstruction(Instruction):
    """Count-Down (CTD) counter.

    Decrements the accumulator every scan the rung is True.  The accumulator
    starts at 0 and counts negative; done = True when acc ≤ −preset.

    .. warning::
        Not edge-triggered. The accumulator decrements by one per scan while
        the enable condition is True.  Wrap `rise()` around the rung condition
        to count leading edges instead.

    - Accumulator type: DINT (32-bit signed, clamps at ±2 147 483 647).
    - Reset condition: resets acc to 0 and done to False (level-sensitive).

    Args:
        done_bit: BOOL tag set when acc ≤ −preset.
        accumulator: DINT tag storing the current (negative) count.
        preset: Target magnitude (positive constant or DINT tag).
        down_condition: Rung power condition (injected automatically by DSL).
        reset_condition: Tag or condition that resets acc and done.
    """

    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _reads = ("preset",)
    _writes = ("done_bit", "accumulator")
    _conditions = ("down_condition", "reset_condition")
    _structural_fields = ()
    _exclusive_fields = ("accumulator",)
    _status_fields = ("cu_bit", "cd_bit")

    def __init__(
        self,
        done_bit: Tag,
        accumulator: Tag,
        preset: Tag | int,
        down_condition: Any,
        reset_condition: Any,
        cu_bit: Tag | None = None,
        cd_bit: Tag | None = None,
    ):
        self.done_bit = done_bit
        self.accumulator = accumulator
        self.preset = preset
        self.cu_bit = cu_bit
        self.cd_bit = cd_bit

        # Convert Tags to Conditions if needed
        self.down_condition = to_condition(down_condition)
        self.reset_condition = to_condition(reset_condition)

    def _status_tags(self, cu: bool, cd: bool) -> dict[str, bool]:
        tags: dict[str, bool] = {}
        if self.cu_bit is not None:
            tags[self.cu_bit.name] = cu
        if self.cd_bit is not None:
            tags[self.cd_bit.name] = cd
        return tags

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        condition_view = instruction_condition_view(ctx)

        reset_active = self.reset_condition is not None and self.reset_condition.evaluate(
            condition_view
        )
        acc_value = ctx.get_tag(self.accumulator.name, 0)
        sp = resolve_preset_ctx(self.preset, ctx)

        if reset_active:
            ctx.set_tags(
                {
                    self.done_bit.name: False,
                    self.accumulator.name: 0,
                    **self._status_tags(False, enabled),
                }
            )
        else:
            if enabled:
                acc_value -= 1
            acc_value = _clamp_dint(acc_value)
            ctx.set_tags(
                {
                    self.done_bit.name: acc_value <= -sp,
                    self.accumulator.name: acc_value,
                    **self._status_tags(False, enabled),
                }
            )

    def is_terminal(self) -> bool:
        return True

    def advance_profile(self) -> AdvanceProfile:
        return monotone_profile(
            channels=(self.accumulator, self.done_bit),
            accumulator=self.accumulator,
            done=self.done_bit,
            active=None,
            preset=self.preset,
            direction=-1,
            rate_per_scan=lambda _dt: 1.0,
            advance=ConditionDemand(self.down_condition),
            advance_is_pulse=isinstance(
                self.down_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
            restore=(
                ConditionDemand(self.reset_condition) if self.reset_condition is not None else None
            ),
            restore_is_pulse=isinstance(
                self.reset_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
        )
