"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition
from pyrung.core.tag import Tag

from .advance import (
    AdvanceProfile,
    ConditionDemand,
    monotone_profile,
    recorded_completion_controls,
)
from .base import Instruction
from .conversions import (
    _clamp_dint,
)
from .operands import Preset, TagArg, inner
from .utils import (
    instruction_condition_view,
    parse_preset,
    resolve_preset_ctx,
    status_arg,
    to_condition,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


#: Memory prefixes for a counter's count inputs as it last scanned them (see
#: :func:`count_edge`), keyed per instruction site: ``_cu:i{N}`` / ``_cd:i{N}``.
COUNT_UP_MEMORY = "_cu"
COUNT_DOWN_MEMORY = "_cd"


def count_edge(ctx: ScanContext, key: str, now: bool) -> bool:
    """Return True if a count input rose since this counter last ran, and remember it.

    A CLICK counter counts the rising edges of its count inputs, with a memory
    of its own for each.  The memory is the input as this instruction saw it
    the last time it was scanned.  It is written on every scan the instruction
    runs, reset scans included, so an input held on through a reset does not
    count again.  It starts off (absent), so an input already on at the first
    scan counts.
    """
    before = bool(ctx.get_memory(key, False))
    ctx.set_memory(key, now)
    return now and not before


class CountUpInstruction(Instruction):
    """Count-Up (CTU) — and optionally bidirectional — counter.

    The counter counts the rising edges of its rung.  The accumulator goes up
    by one on each scan where the rung is True after a scan where it was False,
    or on the first scan.  A separate down condition makes the counter
    bidirectional: it counts down on that condition's rising edges.  An up edge
    and a down edge on the same scan cancel.

    The counter keeps its own memory of each count input (``_cu:i{N}``,
    ``_cd:i{N}``), updated on every scan it runs, reset scans included.  An
    input held on through a reset does not count again.

    - Accumulator type: DINT (32-bit signed, clamps at ±2 147 483 647).
    - Done bit: True when acc ≥ preset, recomputed every scan it runs.
    - Reset condition: clears acc to 0 and done to False (level-sensitive,
      wins over a count on the same scan).

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
    _call_operands = (("counter", ("done_bit", "accumulator")),)
    _call_keyword_operands = (("preset", "preset"),)
    _call_keywords = ()
    _call_chained = (("down", "down_condition"), ("reset", "reset_condition"))

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
        parsed: Preset = parse_preset(preset, label="count_up() preset")
        self.done_bit = TagArg(done_bit)
        self.accumulator = TagArg(accumulator)
        self.preset = parsed
        self.cu_bit = status_arg(cu_bit)
        self.cd_bit = status_arg(cd_bit)

        # Convert Tags to Conditions if needed
        self.up_condition = to_condition(up_condition)
        self.reset_condition = to_condition(reset_condition)
        self.down_condition = to_condition(down_condition)

    def _status_tags(self, cu: bool, cd: bool) -> dict[str, bool]:
        tags: dict[str, bool] = {}
        if self.cu_bit is not None:
            tags[self.cu_bit.tag.name] = cu
        if self.cd_bit is not None:
            tags[self.cd_bit.tag.name] = cd
        return tags

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        condition_view = instruction_condition_view(ctx, self)

        reset_active = self.reset_condition is not None and self.reset_condition.evaluate(
            condition_view
        )
        down_active = self.down_condition is not None and self.down_condition.evaluate(
            condition_view
        )
        up_edge = count_edge(ctx, self.memory_key(COUNT_UP_MEMORY), bool(enabled))
        down_edge = self.down_condition is not None and count_edge(
            ctx, self.memory_key(COUNT_DOWN_MEMORY), bool(down_active)
        )
        acc = self.accumulator.tag.name
        done = self.done_bit.tag.name
        acc_value = ctx.get_tag(acc, 0)
        sp = resolve_preset_ctx(self.preset, ctx)

        if reset_active:
            ctx.set_tags(
                {
                    done: False,
                    acc: 0,
                    **self._status_tags(enabled, down_active),
                }
            )
        else:
            delta = (1 if up_edge else 0) - (1 if down_edge else 0)
            acc_value = _clamp_dint(acc_value + delta)
            ctx.set_tags(
                {
                    done: acc_value >= sp,
                    acc: acc_value,
                    **self._status_tags(enabled, down_active),
                }
            )

    def has_pins(self) -> bool:
        return True

    def advance_profile(self) -> AdvanceProfile:
        return monotone_profile(
            channels=(self.accumulator.tag, self.done_bit.tag),
            accumulator=self.accumulator.tag,
            done=self.done_bit.tag,
            progress=None,
            preset=inner(self.preset),
            direction=1,
            # One count per rising edge: an on scan, then an off scan.
            rate_per_scan=lambda _dt: 0.5,
            advance=ConditionDemand(self.up_condition),
            # The counter counts edges, so its rung must be pulsed, not held.
            advance_is_pulse=True,
            restore=(
                ConditionDemand(self.reset_condition) if self.reset_condition is not None else None
            ),
            restore_is_pulse=isinstance(
                self.reset_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
            completion_controls=recorded_completion_controls(self),
        )


class CountDownInstruction(Instruction):
    """Count-Down (CTD) counter.

    The counter counts the rising edges of its rung, down by one each.  The
    accumulator starts at 0 and counts negative.  Done is True when
    acc ≤ preset, so a down counter with a positive preset is done from its
    first scan, as on a CLICK.  Its memory of the rung (``_cd:i{N}``) is
    updated on every scan it runs, reset scans included.

    - Accumulator type: DINT (32-bit signed, clamps at ±2 147 483 647).
    - Done bit: recomputed every scan it runs.
    - Reset condition: resets acc to 0 and done to False (level-sensitive).

    Args:
        done_bit: BOOL tag set when acc ≤ preset.
        accumulator: DINT tag storing the current (negative) count.
        preset: Done threshold (constant or DINT tag).  The counter is done while acc ≤ preset.
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
    _call_operands = (("counter", ("done_bit", "accumulator")),)
    _call_keyword_operands = (("preset", "preset"),)
    _call_keywords = ()
    _call_chained = (("reset", "reset_condition"),)

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
        parsed: Preset = parse_preset(preset, label="count_down() preset")
        self.done_bit = TagArg(done_bit)
        self.accumulator = TagArg(accumulator)
        self.preset = parsed
        self.cu_bit = status_arg(cu_bit)
        self.cd_bit = status_arg(cd_bit)

        # Convert Tags to Conditions if needed
        self.down_condition = to_condition(down_condition)
        self.reset_condition = to_condition(reset_condition)

    def _status_tags(self, cu: bool, cd: bool) -> dict[str, bool]:
        tags: dict[str, bool] = {}
        if self.cu_bit is not None:
            tags[self.cu_bit.tag.name] = cu
        if self.cd_bit is not None:
            tags[self.cd_bit.tag.name] = cd
        return tags

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        condition_view = instruction_condition_view(ctx, self)

        reset_active = self.reset_condition is not None and self.reset_condition.evaluate(
            condition_view
        )
        down_edge = count_edge(ctx, self.memory_key(COUNT_DOWN_MEMORY), bool(enabled))
        acc = self.accumulator.tag.name
        done = self.done_bit.tag.name
        acc_value = ctx.get_tag(acc, 0)
        sp = resolve_preset_ctx(self.preset, ctx)

        if reset_active:
            ctx.set_tags(
                {
                    done: False,
                    acc: 0,
                    **self._status_tags(False, enabled),
                }
            )
        else:
            if down_edge:
                acc_value -= 1
            acc_value = _clamp_dint(acc_value)
            ctx.set_tags(
                {
                    done: acc_value <= sp,
                    acc: acc_value,
                    **self._status_tags(False, enabled),
                }
            )

    def has_pins(self) -> bool:
        return True

    def advance_profile(self) -> AdvanceProfile:
        return monotone_profile(
            channels=(self.accumulator.tag, self.done_bit.tag),
            accumulator=self.accumulator.tag,
            done=self.done_bit.tag,
            progress=None,
            preset=inner(self.preset),
            direction=-1,
            rate_per_scan=lambda _dt: 0.5,
            advance=ConditionDemand(self.down_condition),
            advance_is_pulse=True,
            restore=(
                ConditionDemand(self.reset_condition) if self.reset_condition is not None else None
            ),
            restore_is_pulse=isinstance(
                self.reset_condition, (RisingEdgeCondition, FallingEdgeCondition)
            ),
            completion_controls=recorded_completion_controls(self),
        )
