"""CLICK v4 Out coil variants: toggle, debounce, flasher and timed.

Each variant is its own class and none subclasses ``OutInstruction``: the
analyses that dispatch on ``OutInstruction`` read it as "the coil equals its
rung", which no variant is.  The variants keep their state in tags: the target
bit and the owned Timers' ``Done``/``Acc``.  The one exception is toggle's
previous rung state, a memory key written every scan.  Timed phases carry the
usual ``_frac:<Acc>`` remainder.

None of them writes a Timer's ``EN`` or ``TT`` bit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.crossing import Eq
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.time_mode import _parse_time_unit

from .advance import AdvanceProfile, AdvanceStep, ConditionDemand, constraint_holds
from .base import Instruction
from .utils import resolve_preset_ctx, to_condition, validate_preset

if TYPE_CHECKING:
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


def _tick(ctx: ScanContext, acc: Tag, unit: TimeUnit) -> tuple[int, float]:
    """Advance *acc* by this scan's dt; return the new value and remainder."""
    frac = ctx.get_memory(f"_frac:{acc.name}", 0.0)
    dt = ctx.get_memory("_dt", 0.0)
    dt_units = unit.dt_to_units(dt) + frac
    int_units = int(dt_units)
    return min(ctx.get_tag(acc.name, 0) + int_units, _ACC_MAX), dt_units - int_units


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

    Each side is an on-delay timer: the on side times while the rung is true,
    the off side while it is false, and either resets when the rung changes.
    The target turns on when the on side is done and off when the off side is
    done; between the two it holds.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset", "off_preset")
    _writes = ("target", "on_done", "on_acc", "off_done", "off_acc")
    _conditions = ("enable_condition",)
    _structural_fields = ("unit",)
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

    def _side(
        self,
        ctx: ScanContext,
        timing: bool,
        done: Tag,
        acc: Tag,
        preset: Tag | int,
        tags: dict[str, Any],
    ) -> bool:
        """Run one side's on-delay; return whether that side is done."""
        if timing:
            acc_value, frac = _tick(ctx, acc, self.unit)
            is_done = acc_value >= resolve_preset_ctx(preset, ctx)
        else:
            acc_value, frac, is_done = 0, 0.0, False
        ctx.set_memory(f"_frac:{acc.name}", frac)
        tags[done.name] = is_done
        tags[acc.name] = acc_value
        return is_done

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        tags: dict[str, Any] = {}
        on_ready = self._side(ctx, enabled, self.on_done, self.on_acc, self.on_preset, tags)
        off_ready = self._side(ctx, not enabled, self.off_done, self.off_acc, self.off_preset, tags)
        if on_ready:
            tags[self.target.name] = True
        elif off_ready:
            tags[self.target.name] = False
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

    The on phase starts when the rung goes true.  ``on_done`` is set while the
    off phase runs, and ``off_done`` while a later on phase runs; each phase's
    accumulator holds its final value until that phase starts again.  The rung
    going false turns the target off and clears both timers.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset", "off_preset")
    _writes = ("target", "on_done", "on_acc", "off_done", "off_acc")
    _conditions = ("enable_condition",)
    _structural_fields = ("unit",)
    _exclusive_fields = ("on_done", "on_acc", "off_acc")

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
        if not enabled:
            ctx.set_memory(on_frac, 0.0)
            ctx.set_memory(off_frac, 0.0)
            ctx.set_tags(
                {
                    self.target.name: False,
                    self.on_done.name: False,
                    self.on_acc.name: 0,
                    self.off_done.name: False,
                    self.off_acc.name: 0,
                }
            )
            return

        tags: dict[str, Any] = {}
        off_phase = bool(ctx.get_tag(self.on_done.name, False))
        # The first scan of the first on phase (everything still cleared) shows
        # the target on even when an on time no longer than a scan is up already.
        first = (
            not off_phase
            and not ctx.get_tag(self.off_done.name, False)
            and ctx.get_tag(self.on_acc.name, 0) == 0
            and ctx.get_memory(on_frac, 0.0) == 0.0
        )
        if not off_phase:
            acc_value, frac = _tick(ctx, self.on_acc, self.unit)
            tags[self.on_acc.name] = acc_value
            if acc_value >= resolve_preset_ctx(self.on_preset, ctx):
                off_phase = True
                tags[self.on_done.name] = True
                tags[self.off_done.name] = False
                tags[self.off_acc.name] = 0
                ctx.set_memory(on_frac, 0.0)
                ctx.set_memory(off_frac, 0.0)
            else:
                ctx.set_memory(on_frac, frac)
                ctx.set_memory(off_frac, ctx.get_memory(off_frac, 0.0))
        else:
            acc_value, frac = _tick(ctx, self.off_acc, self.unit)
            tags[self.off_acc.name] = acc_value
            if acc_value >= resolve_preset_ctx(self.off_preset, ctx):
                off_phase = False
                tags[self.off_done.name] = True
                tags[self.on_done.name] = False
                tags[self.on_acc.name] = 0
                ctx.set_memory(on_frac, 0.0)
                ctx.set_memory(off_frac, 0.0)
            else:
                ctx.set_memory(off_frac, frac)
                ctx.set_memory(on_frac, ctx.get_memory(on_frac, 0.0))
        tags[self.target.name] = first or not off_phase
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
    on time runs out whatever the rung does.  ``Done`` is set when the on time
    is up and clears, with the accumulator, once the rung is false.
    """

    INERT_WHEN_DISABLED = False
    _reads = ("on_preset",)
    _writes = ("target", "timer_done", "timer_acc")
    _conditions = ("enable_condition",)
    _structural_fields = ("unit", "maintained")
    _exclusive_fields = ("target", "timer_done", "timer_acc")

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
        if ctx.get_tag(self.timer_done.name, False):
            if enabled:
                # Timed out with the rung still true: wait for it to go false.
                ctx.set_memory(frac_key, ctx.get_memory(frac_key, 0.0))
                ctx.set_tag(self.target.name, self.maintained)
                return
        elif enabled or ctx.get_tag(self.target.name, False):
            # Starting from idle, the target is on this scan even if the on time
            # is up already: a pulse lasts at least one scan.
            starting = not ctx.get_tag(self.target.name, False)
            acc_value, frac = _tick(ctx, self.timer_acc, self.unit)
            done = acc_value >= resolve_preset_ctx(self.on_preset, ctx)
            ctx.set_memory(frac_key, frac)
            ctx.set_tags(
                {
                    self.target.name: starting or (enabled and self.maintained) or not done,
                    self.timer_done.name: done,
                    self.timer_acc.name: acc_value,
                }
            )
            return
        ctx.set_memory(frac_key, 0.0)
        ctx.set_tags(
            {
                self.target.name: False,
                self.timer_done.name: False,
                self.timer_acc.name: 0,
            }
        )

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
