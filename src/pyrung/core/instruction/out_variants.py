"""CLICK v4 Out coil variants: toggle, debounce, flasher and timed.

Each variant is its own class and none subclasses ``OutInstruction``: the
analyses that dispatch on ``OutInstruction`` read it as "the coil equals its
rung", which no variant is.  The variants keep their state in tags: the target
bit and the owned Timers' accumulators (CLICK's TD).  The one exception is
toggle's previous rung state, a memory key written every scan.  Timed phases
carry the usual ``_frac:<Acc>`` remainder, in nanoseconds.

None of them writes a Timer's ``Done`` bit (CLICK's T), ``EN`` or ``TT``.  A
CLICK Out coil claims the whole timer number but reads its phases from the TDs
alone and never turns T<n> on (hardware round 1, probe O6), so a contact on
that Timer's ``Done`` is always off.  The ``*_done`` fields only name the
claimed timer, for the Click mapping checks.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.crossing import Eq
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.time_mode import _parse_time_unit

from .advance import AdvanceProfile, AdvanceStep, ConditionDemand, constraint_holds
from .base import Instruction
from .utils import elapsed_units, resolve_preset_ctx, to_condition, validate_preset

if TYPE_CHECKING:
    from fractions import Fraction

    from pyrung.core.context import ScanContext
    from pyrung.core.crossing import Constraint
    from pyrung.core.instruction.advance import Snapshot
    from pyrung.core.time_mode import TimeUnit

_ACC_MAX = 32767


def _coil_tag(target: Tag | ImmediateRef) -> Tag:
    tag = target.value if isinstance(target, ImmediateRef) else target
    if not isinstance(tag, Tag):
        raise TypeError(f"Out coil variants write one Bool tag, got {type(tag).__name__}")
    return tag


def _tick(ctx: ScanContext, acc: Tag, unit: TimeUnit) -> tuple[int, int | Fraction]:
    """Advance *acc* by this scan's dt; return the new value and remainder."""
    frac = ctx.get_memory(f"_frac:{acc.name}", 0)
    int_units, rem = elapsed_units(frac, ctx.get_memory("_dt", 0.0), unit)
    return min(ctx.get_tag(acc.name, 0) + int_units, _ACC_MAX), rem


def _target_plan(
    target: Tag, step: Callable[[Eq, bool], AdvanceStep]
) -> Callable[[Constraint, Snapshot], AdvanceStep | None]:
    """A plan that answers only ``target == <bool>`` through *step*."""

    def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
        if not isinstance(constraint, Eq) or constraint.tag != target.name:
            return None
        if len(constraint.values) != 1:
            return None
        desired = next(iter(constraint.values))
        if not isinstance(desired, bool) or constraint_holds(constraint, snapshot) is True:
            return None
        return step(constraint, desired)

    return plan


class ToggleInstruction(Instruction):
    """Toggle coil: inverts the target on each OFF->ON transition of the rung.

    The target keeps its value while the rung is false.  The rung's previous
    state is memory, so a rung that is already true on the first scan toggles.
    """

    INERT_WHEN_DISABLED = False
    _reads = ()
    _writes = ("target",)
    _conditions = ("enable_condition",)
    _structural_fields = ()
    _exclusive_fields = ("target",)

    def __init__(self, target: Tag | ImmediateRef, enable_condition: Any = None):
        self.target = target
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        key = self.memory_key("_toggle")
        if enabled and not ctx.get_memory(key, False):
            name = _coil_tag(self.target).name
            ctx.set_tag(name, not ctx.get_tag(name, False))
        ctx.set_memory(key, bool(enabled))

    def advance_profile(self) -> AdvanceProfile | None:
        if self.enable_condition is None:
            return None
        target = _coil_tag(self.target)
        rung = self.enable_condition

        def step(constraint: Eq, desired: bool) -> AdvanceStep:
            return AdvanceStep(until=constraint, pulse=ConditionDemand(rung))

        return AdvanceProfile(channels=(target,), plan=_target_plan(target, step))


class DebounceInstruction(Instruction):
    """Debounce coil: the target follows the rung once it has held steady.

    The on side times while the rung is true, the off side while it is false.
    A side starts from 0 when the rung changes to it, and keeps its
    accumulator while the other side times (CLICK keeps the on side's TD after
    the rung falls, hardware round of 2026-11-05).  The rung's previous state
    is memory.  The target turns on when the on side's accumulator reaches its
    preset and off when the off side's does; between the two it holds.
    Neither Timer's ``Done`` is written.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset", "off_preset")
    _writes = ("target", "on_acc", "off_acc")
    _conditions = ("enable_condition",)
    # The claimed Timers' Done bits: named, never written.
    _structural_fields = ("unit", "on_done", "off_done")
    _exclusive_fields = ("target", "on_acc", "off_acc")

    def __init__(
        self,
        target: Tag,
        *,
        on_done: Tag,
        on_acc: Tag,
        on_preset: Tag | int,
        off_done: Tag,
        off_acc: Tag,
        off_preset: Tag | int,
        unit: str = "Tms",
        enable_condition: Any = None,
    ):
        validate_preset(on_preset, label="debounce() on preset")
        validate_preset(off_preset, label="debounce() off preset")
        self.target = target
        self.on_done = on_done
        self.on_acc = on_acc
        self.on_preset = on_preset
        self.off_done = off_done
        self.off_acc = off_acc
        self.off_preset = off_preset
        self.unit = _parse_time_unit(unit)
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        rung_key = self.memory_key("_debounce_rung")
        changed = bool(enabled) != bool(ctx.get_memory(rung_key, False))
        ctx.set_memory(rung_key, bool(enabled))
        if enabled:
            acc, preset, held = self.on_acc, self.on_preset, self.off_acc
        else:
            acc, preset, held = self.off_acc, self.off_preset, self.on_acc
        frac_key = f"_frac:{acc.name}"
        if changed:
            # The side the rung changed to starts from 0.
            ctx.set_tags({acc.name: 0})
            ctx.set_memory(frac_key, 0)
        acc_value, frac = _tick(ctx, acc, self.unit)
        ctx.set_memory(frac_key, frac)
        held_frac = f"_frac:{held.name}"
        ctx.set_memory(held_frac, ctx.get_memory(held_frac, 0))
        tags: dict[str, Any] = {acc.name: acc_value}
        if acc_value >= resolve_preset_ctx(preset, ctx):
            tags[self.target.name] = bool(enabled)
        ctx.set_tags(tags)

    def advance_profile(self) -> AdvanceProfile | None:
        if self.enable_condition is None:
            return None
        rung = self.enable_condition

        def step(constraint: Eq, desired: bool) -> AdvanceStep:
            return AdvanceStep(until=constraint, holds=(ConditionDemand(rung, desired),))

        return AdvanceProfile(channels=(self.target,), plan=_target_plan(self.target, step))


class FlasherInstruction(Instruction):
    """Flasher coil: alternates on and off phases while the rung is true.

    The on phase starts when the rung goes true.  The phase is read from the
    accumulators: the off phase runs while the on side's has reached its preset
    and the off side's has not, the on phase otherwise.  Each phase's
    accumulator holds its final value until that phase starts again, from 0.
    The rung going false turns the target off and freezes both accumulators;
    going true again restarts with an on phase, both accumulators from 0
    (hardware round of 2026-11-05: the off TD does not run on from where it
    froze).  The first on phase counts the rung's first scan, as on_delay
    does, so an on time of a scan or less (0 or below included) does not show
    then; nor does an on phase of 0 or below that an off phase ends.
    Neither Timer's ``Done`` is written.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset", "off_preset")
    _writes = ("target", "on_acc", "off_acc")
    _conditions = ("enable_condition",)
    # The claimed Timers' Done bits: named, never written.
    _structural_fields = ("unit", "on_done", "off_done")
    _exclusive_fields = ("on_acc", "off_acc")

    def __init__(
        self,
        target: Tag,
        *,
        on_done: Tag,
        on_acc: Tag,
        on_preset: Tag | int,
        off_done: Tag,
        off_acc: Tag,
        off_preset: Tag | int,
        unit: str = "Tms",
        enable_condition: Any = None,
    ):
        validate_preset(on_preset, label="flasher() on preset")
        validate_preset(off_preset, label="flasher() off preset")
        self.target = target
        self.on_done = on_done
        self.on_acc = on_acc
        self.on_preset = on_preset
        self.off_done = off_done
        self.off_acc = off_acc
        self.off_preset = off_preset
        self.unit = _parse_time_unit(unit)
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        on_frac = f"_frac:{self.on_acc.name}"
        off_frac = f"_frac:{self.off_acc.name}"
        idle_key = self.memory_key("_flash_idle")
        if not enabled:
            # Both accumulators (and their remainders) freeze where they are.
            ctx.set_memory(idle_key, True)
            ctx.set_tags({self.target.name: False})
            return

        tags: dict[str, Any] = {}
        restarting = ctx.get_memory(idle_key, True)
        ctx.set_memory(idle_key, False)
        on_acc = ctx.get_tag(self.on_acc.name, 0)
        off_acc = ctx.get_tag(self.off_acc.name, 0)
        if restarting:
            # Coming back always starts an on phase, both accumulators from 0.
            on_acc = off_acc = 0
            ctx.set_tags({self.on_acc.name: 0, self.off_acc.name: 0})
            ctx.set_memory(on_frac, 0)
            ctx.set_memory(off_frac, 0)
        on_preset = resolve_preset_ctx(self.on_preset, ctx)
        off_phase = on_acc >= on_preset and off_acc < resolve_preset_ctx(self.off_preset, ctx)
        if not off_phase:
            acc_value, frac = _tick(ctx, self.on_acc, self.unit)
            tags[self.on_acc.name] = acc_value
            if acc_value >= on_preset:
                lit = False
                tags[self.off_acc.name] = 0
                ctx.set_memory(on_frac, 0)
                ctx.set_memory(off_frac, 0)
            else:
                lit = True
                ctx.set_memory(on_frac, frac)
                ctx.set_memory(off_frac, ctx.get_memory(off_frac, 0))
        else:
            acc_value, frac = _tick(ctx, self.off_acc, self.unit)
            tags[self.off_acc.name] = acc_value
            if acc_value >= resolve_preset_ctx(self.off_preset, ctx):
                # The on phase starts from 0: on unless its time is up already.
                lit = on_preset > 0
                tags[self.on_acc.name] = 0
                ctx.set_memory(on_frac, 0)
                ctx.set_memory(off_frac, 0)
            else:
                lit = False
                ctx.set_memory(off_frac, frac)
                ctx.set_memory(on_frac, ctx.get_memory(on_frac, 0))
        tags[self.target.name] = lit
        ctx.set_tags(tags)

    def advance_profile(self) -> AdvanceProfile | None:
        # No profile: the target's writer trace (hold the rung true, then let
        # the phases run) reaches either value, and a profile's retained hold
        # would read as an action to try instead of a wait.
        return None


class TimedInstruction(Instruction):
    """Timed coil: a rung edge turns the target on for a set time.

    Not maintained, the target is on for exactly the preset and then off even
    if the rung is still true; another pulse needs the rung to go false and
    true again.  Maintained, the target is on for at least the preset and then
    follows the rung.  An edge during the on time does not restart it, and the
    on time runs out whatever the rung does.  The on time runs while the
    target is on and the accumulator is short of the preset; a rising edge
    outside it starts one from 0, and the accumulator otherwise holds, the
    rung false included (hardware round of 2026-11-05).  The rung's previous
    state is memory.  The edge's scan counts toward the time, as on_delay's
    first scan does, so a preset of a scan or less (0 or below included) gives
    no pulse.  The Timer's ``Done`` is not written.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset",)
    _writes = ("target", "timer_acc")
    _conditions = ("enable_condition",)
    # The claimed Timer's Done bit: named, never written.
    _structural_fields = ("unit", "maintained", "timer_done")
    _exclusive_fields = ("target", "timer_acc")

    def __init__(
        self,
        target: Tag,
        *,
        timer_done: Tag,
        timer_acc: Tag,
        on_preset: Tag | int,
        unit: str = "Tms",
        maintained: bool = False,
        enable_condition: Any = None,
    ):
        validate_preset(on_preset, label="timed() on preset")
        self.target = target
        self.timer_done = timer_done
        self.timer_acc = timer_acc
        self.on_preset = on_preset
        self.unit = _parse_time_unit(unit)
        self.maintained = maintained
        self.enable_condition = to_condition(enable_condition)

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        frac_key = f"_frac:{self.timer_acc.name}"
        rung_key = self.memory_key("_timed_rung")
        rose = bool(enabled) and not ctx.get_memory(rung_key, False)
        ctx.set_memory(rung_key, bool(enabled))
        preset = resolve_preset_ctx(self.on_preset, ctx)
        timing = bool(ctx.get_tag(self.target.name, False)) and (
            ctx.get_tag(self.timer_acc.name, 0) < preset
        )
        if rose and not timing:
            # A new on time, from 0.
            timing = True
            ctx.set_tags({self.timer_acc.name: 0})
            ctx.set_memory(frac_key, 0)
        if timing:
            acc_value, frac = _tick(ctx, self.timer_acc, self.unit)
            ctx.set_memory(frac_key, frac)
            ctx.set_tags(
                {
                    self.target.name: acc_value < preset or (self.maintained and bool(enabled)),
                    self.timer_acc.name: acc_value,
                }
            )
            return
        # Timed out, or never started: the accumulator holds.
        ctx.set_memory(frac_key, ctx.get_memory(frac_key, 0))
        ctx.set_tag(self.target.name, self.maintained and bool(enabled))

    def advance_profile(self) -> AdvanceProfile | None:
        if self.enable_condition is None:
            return None
        rung = self.enable_condition
        maintained = self.maintained

        def step(constraint: Eq, desired: bool) -> AdvanceStep:
            if desired:
                return AdvanceStep(until=constraint, pulse=ConditionDemand(rung))
            if maintained:
                return AdvanceStep(until=constraint, holds=(ConditionDemand(rung, False),))
            # It drops by itself once the on time is up.
            return AdvanceStep(until=constraint)

        return AdvanceProfile(channels=(self.target,), plan=_target_plan(self.target, step))
