"""Drum instructions (event/time) with Click-style control chaining semantics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition
from pyrung.core.crossing import Cmp, Constraint, Eq
from pyrung.core.tag import Tag, TagType
from pyrung.core.time_mode import _parse_time_unit

from .advance import (
    AdvanceProfile,
    AdvanceStep,
    ConditionDemand,
    Snapshot,
    constraint_holds,
    eq,
    resolve_snapshot_value,
    scalar_boundary,
)
from .base import Instruction
from .utils import instruction_condition_view, to_condition

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext

_INT_MAX = 32767
_DINT_MAX = 2147483647


def _as_bool_matrix(pattern: Sequence[Sequence[bool | int]]) -> tuple[tuple[bool, ...], ...]:
    if not pattern:
        raise ValueError("drum pattern must contain at least 1 step")
    rows: list[tuple[bool, ...]] = []
    for row in pattern:
        if not row:
            raise ValueError("drum pattern rows must contain at least 1 output column")
        cooked: list[bool] = []
        for cell in row:
            if isinstance(cell, bool):
                cooked.append(cell)
                continue
            if isinstance(cell, int) and cell in {0, 1}:
                cooked.append(bool(cell))
                continue
            raise TypeError("drum pattern values must be bool or 0/1")
        rows.append(tuple(cooked))
    return tuple(rows)


def _validate_output_tags(outputs: Sequence[Tag]) -> tuple[Tag, ...]:
    if not outputs:
        raise ValueError("drum outputs must contain at least 1 tag")
    if len(outputs) > 16:
        raise ValueError("drum outputs max is 16")
    names: set[str] = set()
    cooked: list[Tag] = []
    for output in outputs:
        if not isinstance(output, Tag):
            raise TypeError(f"drum output must be Tag, got {type(output).__name__}")
        if output.type != TagType.BOOL:
            raise TypeError(
                f"drum output tags must be BOOL; got {output.type.name} at {output.name}"
            )
        if output.name in names:
            raise ValueError(f"drum output tags must be unique; duplicate {output.name!r}")
        names.add(output.name)
        cooked.append(output)
    return tuple(cooked)


def _validate_step_tag(current_step: Tag) -> Tag:
    if current_step.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"drum current_step must be INT or DINT, got {current_step.type.name}")
    return current_step


def _validate_completion_flag(completion_flag: Tag) -> Tag:
    if completion_flag.type != TagType.BOOL:
        raise TypeError(f"drum completion_flag must be BOOL, got {completion_flag.type.name}")
    return completion_flag


def _validate_jump_step(step: Tag | int | None) -> Tag | int | None:
    if step is None:
        return None
    if isinstance(step, Tag):
        if step.type not in {TagType.INT, TagType.DINT}:
            raise TypeError(f"drum jump step tag must be INT or DINT, got {step.type.name}")
        return step
    if isinstance(step, int):
        return step
    raise TypeError(f"drum jump step must be int or Tag, got {type(step).__name__}")


def _read_step_tag_value(ctx: ScanContext, tag: Tag, fallback: int = 1) -> int:
    value = ctx.get_tag(tag.name, tag.default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _resolve_step_value(step: Tag | int | None, ctx: ScanContext) -> int | None:
    if step is None:
        return None
    if isinstance(step, Tag):
        return _read_step_tag_value(ctx, step)
    return int(step)


def _resolve_preset_value(preset: Tag | int, ctx: ScanContext) -> int:
    if isinstance(preset, Tag):
        return _read_step_tag_value(ctx, preset, fallback=0)
    return int(preset)


def _snapshot_int(snapshot: Snapshot, tag: Tag, fallback: int) -> int:
    try:
        return int(snapshot.get(tag.name, tag.default))
    except (TypeError, ValueError):
        return fallback


def _condition_step(
    until: Eq | Cmp,
    condition: Any,
    *,
    holds: tuple[ConditionDemand, ...] = (),
    reports_progress: bool = False,
) -> AdvanceStep:
    demand = ConditionDemand(condition)
    if isinstance(condition, (RisingEdgeCondition, FallingEdgeCondition)):
        return AdvanceStep(
            until=until,
            holds=holds,
            pulse=demand,
            progress=demand if reports_progress else None,
        )
    return AdvanceStep(
        until=until,
        holds=(*holds, demand),
        progress=demand if reports_progress else None,
    )


def _desired_bool(constraint: Constraint) -> bool | None:
    if not isinstance(constraint, Eq) or len(constraint.values) != 1:
        return None
    value = next(iter(constraint.values))
    return value if isinstance(value, bool) else None


def _next_step_boundary(
    constraint: Constraint,
    snapshot: Snapshot,
    *,
    current_step: Tag,
    step_count: int,
) -> Eq | Cmp | None:
    """Return the next forward step, or step 1 when a restart is needed."""

    current = _snapshot_int(snapshot, current_step, 1)
    if not 1 <= current <= step_count:
        return Cmp(current_step.name, ">=", 1)
    if constraint_holds(constraint, snapshot) is True:
        return None

    if isinstance(constraint, Eq):
        numeric = sorted(
            int(value)
            for value in constraint.values
            if isinstance(value, int) and not isinstance(value, bool)
        )
        if any(value > current for value in numeric) and current < step_count:
            return Cmp(current_step.name, ">=", current + 1)
        if numeric:
            return eq(current_step, 1)
        return None

    if not isinstance(constraint, Cmp):
        return None
    boundary = scalar_boundary(constraint, snapshot)
    if boundary is None:
        return None
    if constraint.op in {">", ">=", "!=", "=="} and current < step_count:
        return Cmp(current_step.name, ">=", current + 1)
    return eq(current_step, 1)


def _is_step_one_restart(boundary: Eq | Cmp, current_step: Tag) -> bool:
    return (
        isinstance(boundary, Eq)
        and boundary.tag == current_step.name
        and boundary.values == frozenset((1,))
    )


class _DrumBaseInstruction(Instruction):
    ALWAYS_EXECUTES = True
    INERT_WHEN_DISABLED = False
    _exclusive_fields = ("current_step",)

    def __init__(
        self,
        *,
        outputs: Sequence[Tag],
        pattern: Sequence[Sequence[bool | int]],
        current_step: Tag,
        completion_flag: Tag,
        auto_condition: Any,
        reset_condition: Any,
        jump_condition: Any = None,
        jump_step: Tag | int | None = None,
        jog_condition: Any = None,
    ) -> None:
        self.outputs = _validate_output_tags(outputs)
        self.pattern = _as_bool_matrix(pattern)
        self.current_step = _validate_step_tag(current_step)
        self.completion_flag = _validate_completion_flag(completion_flag)
        self.auto_condition = to_condition(auto_condition)
        self.reset_condition = to_condition(reset_condition)
        self.jump_condition = to_condition(jump_condition)
        self.jump_step = _validate_jump_step(jump_step)
        self.jog_condition = to_condition(jog_condition)

        self.step_count = len(self.pattern)
        if self.step_count > 16:
            raise ValueError("drum step count max is 16")
        output_count = len(self.outputs)
        for row in self.pattern:
            if len(row) != output_count:
                raise ValueError("drum pattern size must match steps x outputs")

        if self.reset_condition is None:
            raise ValueError("drum reset condition is required")
        if self.jump_condition is None and self.jump_step is not None:
            raise ValueError("drum jump requires condition when step is provided")
        if self.jump_condition is not None and self.jump_step is None:
            raise ValueError("drum jump requires step when condition is provided")

    def is_terminal(self) -> bool:
        return True

    def set_jump(self, condition: Any, step: Tag | int) -> None:
        self.jump_condition = to_condition(condition)
        self.jump_step = _validate_jump_step(step)

    def set_jog(self, condition: Any) -> None:
        self.jog_condition = to_condition(condition)

    def _step_is_valid(self, step: int) -> bool:
        return 1 <= step <= self.step_count

    def _apply_outputs(self, ctx: ScanContext, step: int, *, off: bool = False) -> None:
        """Write *step*'s pattern row, or every output off when *off* (a completed drum)."""
        if off:
            ctx.set_tags({tag.name: False for tag in self.outputs})
            return
        row = self.pattern[step - 1]
        ctx.set_tags({tag.name: bool(row[idx]) for idx, tag in enumerate(self.outputs)})

    def _apply_reset(self, ctx: ScanContext) -> None:
        """Reset is a level that wins: step 1, completion flag off, every output off."""
        ctx.set_tags(
            {
                self.current_step.name: 1,
                self.completion_flag.name: False,
                **{tag.name: False for tag in self.outputs},
            }
        )

    def _jog_then_jump(
        self,
        ctx: ScanContext,
        step: int,
        *,
        jog_edge: bool,
        jump_edge: bool,
        jump_target: int | None,
    ) -> tuple[int, bool, bool, bool]:
        """Take a jog, then a jump, on their edges whether or not the drum is enabled.

        The jump comes last, so it beats a jog on the same scan; *jump_target*
        is the jump step as the drum read it with its pins.  Returns
        ``(step, step_changed, jogged, taken)``: *jogged* when the jog moved
        the drum, which ends a completed drum's all-outputs-off state (a jump
        does not, on a CLICK); *taken* when either moved it (a jump to a valid
        step counts even when it is the step it is on), which clears a time
        drum's accumulator.
        """
        changed = False
        jogged = False
        taken = False
        if jog_edge and step < self.step_count:
            step += 1
            changed = True
            jogged = True
            taken = True
        if jump_edge:
            target = jump_target
            if target is not None and self._step_is_valid(target):
                changed = changed or step != target
                step = target
                taken = True
        if taken:
            ctx.set_tag(self.current_step.name, step)
        return step, changed, jogged, taken

    def _read_pins(
        self, ctx: ScanContext, condition_view: Any
    ) -> tuple[bool, bool, bool, bool, bool, int | None]:
        """``(jump_curr, jump_edge, jog_curr, jog_edge, reset_active, jump_target)``.

        A box reads its pins before its own writes, as the compiled kernel's
        helper legs do: each drum reads them (an event drum with its step's
        event), and the jump step's value, before it corrects an invalid step
        or moves.  A time-event drum reads its step's event only once the
        step's time is up, from the box's condition view.  The accumulator and
        the presets are read from the live scan where the drum uses them, as
        the compiled kernel reads them.
        """
        jump_curr, jump_edge = self._resolve_jump_edge(ctx, condition_view)
        jog_curr, jog_edge = self._resolve_jog_edge(ctx, condition_view)
        reset_active = bool(self.reset_condition.evaluate(condition_view))
        # The jump step is read on a jump edge, the only scan that uses it.
        jump_target = _resolve_step_value(self.jump_step, ctx) if jump_edge else None
        return jump_curr, jump_edge, jog_curr, jog_edge, reset_active, jump_target

    def _resolve_jump_edge(self, ctx: ScanContext, condition_view: Any) -> tuple[bool, bool]:
        if self.jump_condition is None:
            return False, False
        jump_curr = bool(self.jump_condition.evaluate(condition_view))
        jump_prev = bool(ctx.get_memory(self.memory_key("_drum_jump_prev"), False))
        return jump_curr, jump_curr and not jump_prev

    def _resolve_jog_edge(self, ctx: ScanContext, condition_view: Any) -> tuple[bool, bool]:
        if self.jog_condition is None:
            return False, False
        jog_curr = bool(self.jog_condition.evaluate(condition_view))
        jog_prev = bool(ctx.get_memory(self.memory_key("_drum_jog_prev"), False))
        return jog_curr, jog_curr and not jog_prev

    def _write_control_prev_state(
        self, ctx: ScanContext, *, jump_curr: bool, jog_curr: bool
    ) -> None:
        if self.jump_condition is not None:
            ctx.set_memory(self.memory_key("_drum_jump_prev"), jump_curr)
        if self.jog_condition is not None:
            ctx.set_memory(self.memory_key("_drum_jog_prev"), jog_curr)


class EventDrumInstruction(_DrumBaseInstruction):
    _reads = ("jump_step", "current_step")
    _writes = ("outputs", "current_step", "completion_flag")
    _conditions = ("events", "auto_condition", "reset_condition", "jump_condition", "jog_condition")
    _structural_fields = ("pattern",)

    def __init__(
        self,
        outputs: Sequence[Tag],
        events: Sequence[Any],
        pattern: Sequence[Sequence[bool | int]],
        current_step: Tag,
        completion_flag: Tag,
        auto_condition: Any,
        reset_condition: Any,
        jump_condition: Any = None,
        jump_step: Tag | int | None = None,
        jog_condition: Any = None,
    ) -> None:
        super().__init__(
            outputs=outputs,
            pattern=pattern,
            current_step=current_step,
            completion_flag=completion_flag,
            auto_condition=auto_condition,
            reset_condition=reset_condition,
            jump_condition=jump_condition,
            jump_step=jump_step,
            jog_condition=jog_condition,
        )
        if len(events) != self.step_count:
            raise ValueError("event_drum events count must match step count")
        self.events = tuple(to_condition(event) for event in events)
        if any(event is None for event in self.events):
            raise ValueError("event_drum events must contain valid BOOL conditions")

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """One scan, in CLICK's order.

        Reset (a level) wins: step 1, flag off, every output off, and the
        scan's jump and jog are dropped (their edge memory still updates).
        Otherwise an enabled drum takes its step's event edge, then a jog and
        a jump act on their edges even while the drum is paused, the jump
        last.  Only an enabled drum writes its outputs: its step's row, or
        every output off once it completes (``_drum_done``) until it steps
        forward again, by its event or a jog.  A jump moves the step but
        leaves a completed drum's outputs off.
        """
        condition_view = instruction_condition_view(ctx)
        step = _read_step_tag_value(ctx, self.current_step)
        step_changed = False
        invalid = not self._step_is_valid(step)
        if invalid:
            step = 1

        # Every pin is read before the drum writes anything (see _read_pins).
        jump_curr, jump_edge, jog_curr, jog_edge, reset_active, jump_target = self._read_pins(
            ctx, condition_view
        )
        done_key = self.memory_key("_drum_done")
        done = bool(ctx.get_memory(done_key, False))
        event_curr = (
            bool(self.events[step - 1].evaluate(condition_view))
            if enabled and not reset_active
            else False
        )

        if invalid and enabled:
            ctx.set_tag(self.current_step.name, 1)
            step_changed = True

        if reset_active:
            step = 1
            step_changed = True
            done = False
            self._apply_reset(ctx)
        else:
            if enabled:
                last_step = int(ctx.get_memory(self.memory_key("_drum_last_step"), 0))
                event_ready = bool(ctx.get_memory(self.memory_key("_drum_event_ready"), True))
                event_prev = bool(ctx.get_memory(self.memory_key("_drum_event_prev"), False))
                if last_step != step or step_changed:
                    event_ready = not event_curr
                    event_prev = event_curr
                elif not event_ready and not event_curr:
                    event_ready = True

                if event_ready and event_curr and not event_prev:
                    if step < self.step_count:
                        step += 1
                        ctx.set_tag(self.current_step.name, step)
                        step_changed = True
                        done = False
                    else:
                        ctx.set_tag(self.completion_flag.name, True)
                        done = True

            step, moved, jogged, _taken = self._jog_then_jump(
                ctx, step, jog_edge=jog_edge, jump_edge=jump_edge, jump_target=jump_target
            )
            step_changed = step_changed or moved
            if jogged:
                done = False

            if enabled:
                self._apply_outputs(ctx, step, off=done)

        final_event_curr = bool(self.events[step - 1].evaluate(condition_view))
        event_ready = bool(ctx.get_memory(self.memory_key("_drum_event_ready"), True))
        if step_changed:
            event_ready = not final_event_curr
        elif not event_ready and not final_event_curr:
            event_ready = True
        ctx.set_memory(self.memory_key("_drum_event_ready"), event_ready)
        ctx.set_memory(self.memory_key("_drum_event_prev"), final_event_curr)
        ctx.set_memory(self.memory_key("_drum_last_step"), step)
        ctx.set_memory(done_key, done)

        self._write_control_prev_state(ctx, jump_curr=jump_curr, jog_curr=jog_curr)

    def advance_profile(self) -> AdvanceProfile:
        def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
            if not isinstance(constraint, (Eq, Cmp)):
                return None
            if constraint_holds(constraint, snapshot) is True:
                return None
            current = _snapshot_int(snapshot, self.current_step, 1)
            if not 1 <= current <= self.step_count:
                return _condition_step(
                    Cmp(self.current_step.name, ">=", 1),
                    self.reset_condition,
                )

            if constraint.tag == self.completion_flag.name:
                desired = _desired_bool(constraint)
                if desired is None:
                    return None
                if not desired:
                    return _condition_step(
                        eq(self.completion_flag, False),
                        self.reset_condition,
                    )
                boundary = (
                    eq(self.completion_flag, True)
                    if current == self.step_count
                    else Cmp(self.current_step.name, ">=", current + 1)
                )
            elif constraint.tag == self.current_step.name:
                boundary = _next_step_boundary(
                    constraint,
                    snapshot,
                    current_step=self.current_step,
                    step_count=self.step_count,
                )
                if boundary is None:
                    return None
                if _is_step_one_restart(boundary, self.current_step):
                    return _condition_step(boundary, self.reset_condition)
                current = _snapshot_int(snapshot, self.current_step, 1)
            else:
                return None

            event = self.events[current - 1]
            return AdvanceStep(
                until=boundary,
                holds=(ConditionDemand(self.auto_condition),),
                pulse=ConditionDemand(event),
            )

        return AdvanceProfile(
            channels=(self.current_step, self.completion_flag),
            plan=plan,
            done=self.completion_flag,
        )


class TimeDrumInstruction(_DrumBaseInstruction):
    _reads = ("presets", "jump_step", "current_step")
    _writes = ("outputs", "current_step", "accumulator", "completion_flag")
    _conditions = ("auto_condition", "reset_condition", "jump_condition", "jog_condition")
    _structural_fields = ("pattern", "unit")
    # The drum reads its accumulator every scan it times, like a timer does.
    _exclusive_fields = ("current_step", "accumulator")

    def __init__(
        self,
        outputs: Sequence[Tag],
        presets: Sequence[Tag | int],
        unit: str,
        pattern: Sequence[Sequence[bool | int]],
        current_step: Tag,
        accumulator: Tag,
        completion_flag: Tag,
        auto_condition: Any,
        reset_condition: Any,
        jump_condition: Any = None,
        jump_step: Tag | int | None = None,
        jog_condition: Any = None,
    ) -> None:
        super().__init__(
            outputs=outputs,
            pattern=pattern,
            current_step=current_step,
            completion_flag=completion_flag,
            auto_condition=auto_condition,
            reset_condition=reset_condition,
            jump_condition=jump_condition,
            jump_step=jump_step,
            jog_condition=jog_condition,
        )
        if len(presets) != self.step_count:
            raise ValueError("time_drum presets count must match step count")
        cooked_presets: list[Tag | int] = []
        for preset in presets:
            if isinstance(preset, Tag):
                if preset.type not in {TagType.INT, TagType.DINT}:
                    raise TypeError(
                        f"time_drum preset tags must be INT or DINT; got {preset.type.name}"
                    )
                cooked_presets.append(preset)
                continue
            if isinstance(preset, int):
                cooked_presets.append(preset)
                continue
            raise TypeError(f"time_drum presets must be int or Tag, got {type(preset).__name__}")
        self.presets = tuple(cooked_presets)

        if accumulator.type not in {TagType.INT, TagType.DINT}:
            raise TypeError(
                f"time_drum accumulator must be INT or DINT, got {accumulator.type.name}"
            )
        self.accumulator = accumulator
        self.unit = _parse_time_unit(unit)

    def _acc_max(self) -> int:
        return _INT_MAX if self.accumulator.type == TagType.INT else _DINT_MAX

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """One scan, in the event drum's order (see ``EventDrumInstruction.execute``).

        A jog, a jump, an advance and a reset each clear the accumulator.
        Completion turns every output off (``_drum_done``) until the drum
        steps forward again, by its time or a jog; a jump does not end it.
        """
        condition_view = instruction_condition_view(ctx)
        step = _read_step_tag_value(ctx, self.current_step)
        step_changed = False
        reset_step_data = False
        invalid = not self._step_is_valid(step)
        if invalid:
            step = 1

        # Every pin is read before the drum writes anything (see _read_pins).
        jump_curr, jump_edge, jog_curr, jog_edge, reset_active, jump_target = self._read_pins(
            ctx, condition_view
        )
        done_key = self.memory_key("_drum_done")
        done = bool(ctx.get_memory(done_key, False))

        if invalid and enabled:
            step_changed = True
            reset_step_data = True
            ctx.set_tag(self.current_step.name, 1)

        acc_value = _read_step_tag_value(ctx, self.accumulator, fallback=0)
        frac_key = f"_frac:{self.accumulator.name}"
        frac = float(ctx.get_memory(frac_key, 0.0))

        if reset_active:
            step = 1
            step_changed = True
            reset_step_data = True
            done = False
            self._apply_reset(ctx)
        else:
            if enabled:
                dt = float(ctx.get_memory("_dt", 0.0))
                dt_units = self.unit.dt_to_units(dt) + frac
                int_units = int(dt_units)
                frac = dt_units - int_units
                acc_value = min(acc_value + int_units, self._acc_max())
                preset = _resolve_preset_value(self.presets[step - 1], ctx)
                if acc_value >= preset:
                    if step < self.step_count:
                        step += 1
                        step_changed = True
                        reset_step_data = True
                        done = False
                        ctx.set_tag(self.current_step.name, step)
                    else:
                        ctx.set_tag(self.completion_flag.name, True)
                        done = True

            step, moved, jogged, taken = self._jog_then_jump(
                ctx, step, jog_edge=jog_edge, jump_edge=jump_edge, jump_target=jump_target
            )
            if taken:
                step_changed = step_changed or moved
                reset_step_data = True
            if jogged:
                done = False

            if enabled:
                self._apply_outputs(ctx, step, off=done)

        if reset_step_data:
            acc_value = 0
            frac = 0.0

        if enabled or reset_active or step_changed or reset_step_data:
            ctx.set_tags({self.accumulator.name: acc_value, self.current_step.name: step})
            ctx.set_memory(frac_key, frac)
        ctx.set_memory(done_key, done)

        self._write_control_prev_state(ctx, jump_curr=jump_curr, jog_curr=jog_curr)

    def advance_profile(self) -> AdvanceProfile:
        def _auto(until: Eq | Cmp) -> AdvanceStep:
            return _condition_step(
                until,
                self.auto_condition,
                reports_progress=True,
            )

        def _reset(until: Eq | Cmp) -> AdvanceStep:
            return _condition_step(until, self.reset_condition)

        def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
            if not isinstance(constraint, (Eq, Cmp)):
                return None
            if constraint_holds(constraint, snapshot) is True:
                return None

            current = _snapshot_int(snapshot, self.current_step, 1)
            if not 1 <= current <= self.step_count:
                return _reset(Cmp(self.current_step.name, ">=", 1))

            if constraint.tag == self.current_step.name:
                boundary = _next_step_boundary(
                    constraint,
                    snapshot,
                    current_step=self.current_step,
                    step_count=self.step_count,
                )
                if boundary is None:
                    return None
                return (
                    _reset(boundary)
                    if _is_step_one_restart(boundary, self.current_step)
                    else _auto(boundary)
                )

            if constraint.tag == self.completion_flag.name:
                desired = _desired_bool(constraint)
                if desired is None:
                    return None
                if not desired:
                    return _reset(eq(self.completion_flag, False))
                if current < self.step_count:
                    return _auto(Cmp(self.current_step.name, ">=", current + 1))
                return _auto(eq(self.completion_flag, True))

            if constraint.tag != self.accumulator.name:
                return None
            target = scalar_boundary(constraint, snapshot)
            if target is None:
                return None
            acc = _snapshot_int(snapshot, self.accumulator, 0)
            preset_raw = resolve_snapshot_value(self.presets[current - 1], snapshot)
            try:
                preset = int(preset_raw)
            except (TypeError, ValueError):
                return None

            # A non-final step clears Acc as it crosses its preset. Treat that
            # step transition as the observable boundary, then ask again.
            if current < self.step_count and (target >= preset or target < acc):
                return _auto(Cmp(self.current_step.name, ">=", current + 1))
            if current == self.step_count and target < acc:
                return _reset(eq(self.current_step, 1))
            return _auto(constraint)

        return AdvanceProfile(
            channels=(self.current_step, self.accumulator, self.completion_flag),
            plan=plan,
            accumulator=self.accumulator,
            done=self.completion_flag,
        )


class TimeEventDrumInstruction(_DrumBaseInstruction):
    """CLICK's Time & Event drum: a step ends when its time is up and its event is on.

    Each step has an optional preset and an optional **level** event; at least one
    is required.  The accumulator times every enabled scan from step entry and
    keeps counting (clamped) while a timed step waits for its event.  It is its
    own class, not a ``TimeDrumInstruction``: the prover and fold model a time
    drum as one whose every step ends on its preset.
    """

    _reads = ("presets", "jump_step", "current_step")
    _writes = ("outputs", "current_step", "accumulator", "completion_flag")
    _conditions = ("events", "auto_condition", "reset_condition", "jump_condition", "jog_condition")
    _structural_fields = ("pattern", "unit")
    # The drum reads its accumulator every scan it times, like a timer does.
    _exclusive_fields = ("current_step", "accumulator")

    def __init__(
        self,
        outputs: Sequence[Tag],
        presets: Sequence[Tag | int | None],
        events: Sequence[Any],
        unit: str,
        pattern: Sequence[Sequence[bool | int]],
        current_step: Tag,
        accumulator: Tag,
        completion_flag: Tag,
        auto_condition: Any,
        reset_condition: Any,
        jump_condition: Any = None,
        jump_step: Tag | int | None = None,
        jog_condition: Any = None,
    ) -> None:
        super().__init__(
            outputs=outputs,
            pattern=pattern,
            current_step=current_step,
            completion_flag=completion_flag,
            auto_condition=auto_condition,
            reset_condition=reset_condition,
            jump_condition=jump_condition,
            jump_step=jump_step,
            jog_condition=jog_condition,
        )
        if len(presets) != self.step_count:
            raise ValueError("time_event_drum presets count must match step count")
        if len(events) != self.step_count:
            raise ValueError("time_event_drum events count must match step count")
        cooked_presets: list[Tag | int | None] = []
        for preset in presets:
            if preset is None or (isinstance(preset, int) and not isinstance(preset, bool)):
                cooked_presets.append(preset)
                continue
            if isinstance(preset, Tag):
                if preset.type not in {TagType.INT, TagType.DINT}:
                    raise TypeError(
                        f"time_event_drum preset tags must be INT or DINT; got {preset.type.name}"
                    )
                cooked_presets.append(preset)
                continue
            raise TypeError(
                f"time_event_drum presets must be int, Tag or None, got {type(preset).__name__}"
            )
        self.presets = tuple(cooked_presets)
        self.events = tuple(None if event is None else to_condition(event) for event in events)
        for index, (preset, event) in enumerate(zip(self.presets, self.events, strict=True)):
            if preset is None and event is None:
                raise ValueError(
                    f"time_event_drum step {index + 1} needs a preset, an event, or both"
                )

        if accumulator.type not in {TagType.INT, TagType.DINT}:
            raise TypeError(
                f"time_event_drum accumulator must be INT or DINT, got {accumulator.type.name}"
            )
        self.accumulator = accumulator
        self.unit = _parse_time_unit(unit)

    def _acc_max(self) -> int:
        return _INT_MAX if self.accumulator.type == TagType.INT else _DINT_MAX

    def _step_ready(self, step: int, acc_value: int, ctx: ScanContext, condition_view: Any) -> bool:
        preset = self.presets[step - 1]
        if preset is not None and acc_value < _resolve_preset_value(preset, ctx):
            return False
        event = self.events[step - 1]
        return event is None or bool(event.evaluate(condition_view))

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """One scan, in the event drum's order (see ``EventDrumInstruction.execute``).

        Completion turns every output off (``_drum_done``), as on the event
        drum, until the drum steps forward again (its time and event, or a
        jog) or resets; a jump does not end it.
        """
        condition_view = instruction_condition_view(ctx)
        step = _read_step_tag_value(ctx, self.current_step)
        step_changed = False
        reset_step_data = False
        invalid = not self._step_is_valid(step)
        if invalid:
            step = 1

        # Every pin but the step's event is read before the drum writes
        # anything (see _read_pins).
        jump_curr, jump_edge, jog_curr, jog_edge, reset_active, jump_target = self._read_pins(
            ctx, condition_view
        )
        done_key = self.memory_key("_drum_done")
        done = bool(ctx.get_memory(done_key, False))

        if invalid and enabled:
            step_changed = True
            reset_step_data = True
            ctx.set_tag(self.current_step.name, 1)

        acc_value = _read_step_tag_value(ctx, self.accumulator, fallback=0)
        frac_key = f"_frac:{self.accumulator.name}"
        frac = float(ctx.get_memory(frac_key, 0.0))

        if reset_active:
            step = 1
            step_changed = True
            reset_step_data = True
            done = False
            self._apply_reset(ctx)
        else:
            if enabled:
                dt = float(ctx.get_memory("_dt", 0.0))
                dt_units = self.unit.dt_to_units(dt) + frac
                int_units = int(dt_units)
                frac = dt_units - int_units
                acc_value = min(acc_value + int_units, self._acc_max())
                if self._step_ready(step, acc_value, ctx, condition_view):
                    if step < self.step_count:
                        step += 1
                        step_changed = True
                        reset_step_data = True
                        done = False
                        ctx.set_tag(self.current_step.name, step)
                    else:
                        ctx.set_tag(self.completion_flag.name, True)
                        done = True

            step, moved, jogged, taken = self._jog_then_jump(
                ctx, step, jog_edge=jog_edge, jump_edge=jump_edge, jump_target=jump_target
            )
            if taken:
                step_changed = step_changed or moved
                reset_step_data = True
            if jogged:
                done = False

            if enabled:
                self._apply_outputs(ctx, step, off=done)

        if reset_step_data:
            acc_value = 0
            frac = 0.0

        if enabled or reset_active or step_changed or reset_step_data:
            ctx.set_tags({self.accumulator.name: acc_value, self.current_step.name: step})
        # Every scan, as the compiled kernel writes it.
        ctx.set_memory(frac_key, frac)
        ctx.set_memory(done_key, done)

        self._write_control_prev_state(ctx, jump_curr=jump_curr, jog_curr=jog_curr)

    def _advance(self, until: Eq | Cmp, step: int) -> AdvanceStep:
        """Hold the drum enabled, and *step*'s event on, until *until*.

        The event is a level, so it is a hold, not a pulse: an oscillating event
        would miss the scan the time comes up.
        """
        auto = ConditionDemand(self.auto_condition)
        holds: tuple[ConditionDemand, ...] = (auto,)
        pulse: ConditionDemand | None = None
        event = self.events[step - 1]
        if isinstance(event, (RisingEdgeCondition, FallingEdgeCondition)):
            pulse = ConditionDemand(event)
        elif event is not None:
            holds = (auto, ConditionDemand(event))
        return AdvanceStep(
            until=until,
            holds=holds,
            pulse=pulse,
            progress=auto if self.presets[step - 1] is not None else None,
        )

    def advance_profile(self) -> AdvanceProfile:
        def _reset(until: Eq | Cmp) -> AdvanceStep:
            return _condition_step(until, self.reset_condition)

        def _next(current: int) -> AdvanceStep:
            return self._advance(Cmp(self.current_step.name, ">=", current + 1), current)

        def plan(constraint: Constraint, snapshot: Snapshot) -> AdvanceStep | None:
            if not isinstance(constraint, (Eq, Cmp)):
                return None
            if constraint_holds(constraint, snapshot) is True:
                return None

            current = _snapshot_int(snapshot, self.current_step, 1)
            if not 1 <= current <= self.step_count:
                return _reset(Cmp(self.current_step.name, ">=", 1))

            if constraint.tag == self.current_step.name:
                boundary = _next_step_boundary(
                    constraint,
                    snapshot,
                    current_step=self.current_step,
                    step_count=self.step_count,
                )
                if boundary is None:
                    return None
                return (
                    _reset(boundary)
                    if _is_step_one_restart(boundary, self.current_step)
                    else self._advance(boundary, current)
                )

            if constraint.tag == self.completion_flag.name:
                desired = _desired_bool(constraint)
                if desired is None:
                    return None
                if not desired:
                    return _reset(eq(self.completion_flag, False))
                if current < self.step_count:
                    return _next(current)
                return self._advance(eq(self.completion_flag, True), current)

            if constraint.tag != self.accumulator.name:
                return None
            target = scalar_boundary(constraint, snapshot)
            if target is None:
                return None
            acc = _snapshot_int(snapshot, self.accumulator, 0)
            if target < acc:
                if current == self.step_count:
                    return _reset(eq(self.current_step, 1))
                return _next(current)
            preset_spec = self.presets[current - 1]
            if preset_spec is None:
                # An event-only step: its event may end it before Acc gets there.
                return None
            try:
                preset = int(resolve_snapshot_value(preset_spec, snapshot))
            except (TypeError, ValueError):
                return None
            if target < preset:
                # Acc counts whatever the event does.
                return _condition_step(constraint, self.auto_condition, reports_progress=True)
            if current < self.step_count and self.events[current - 1] is None:
                # A time-only step clears Acc as it crosses its preset.
                return _next(current)
            # Past the preset the event ends the step: no plan holds it off.
            return None

        return AdvanceProfile(
            channels=(self.current_step, self.accumulator, self.completion_flag),
            plan=plan,
            accumulator=self.accumulator,
            done=self.completion_flag,
        )
