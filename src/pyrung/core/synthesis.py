"""Synthesis overlay — feedback couplings and PILOT holds as bracketing rungs.

The soft-exec runner scans synthesis rungs at the **top of the scan, before the
user program reads its inputs** (the input-read phase)::

    plant (input-read)  →  PILOT holds  →  user rungs

``plant`` synthesizes feedback from the *previous* scan's committed commands (the
harness couplings — a sensor reflecting the outputs the program already wrote), so
the feedback is an input image that lags the command by one scan.  ``holds`` then
steer inputs before the program reads them (PILOT's input vector for this scan).
Both are ordinary :class:`~pyrung.core.rung.Rung` objects built programmatically
here, so the reader, compile, and causal subsystems consume them with no
special case — synthesis *is* rungs.

Synthesis lives on the soft-exec :class:`~pyrung.core.runner.PLC` only (see
``PLC._synthesis``); it is **never** part of the user :class:`Program`, so deploy
(Click ladder / CircuitPython codegen) and the ``prove`` verifier — which walk the
user program's rungs and subroutines — never see it.  That is the structural
"two roots" discipline: the brackets are a property of the soft fork, not of the
program.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.rung import Rung

if TYPE_CHECKING:
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

PLANT_RUNG_NAMESPACE = "plant"
PILOT_RUNG_NAMESPACE = "PILOT"


@dataclass
class Synthesis:
    """The two synthetic rung lists scanned before the user program.

    ``plant`` builds the input image, then ``holds`` steer managed inputs.  An
    empty ``Synthesis`` is equivalent to no overlay — the runner skips the
    bracket entirely (zero overhead).
    """

    holds: list[Rung] = field(default_factory=list)
    plant: list[Rung] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.holds and not self.plant

    def all_rungs(self) -> Iterator[Rung]:
        """Yield every synthesis rung in scan order — for compile walks."""
        yield from self.plant
        yield from self.holds


def _rung_holding(condition: Condition | Tag | None, instruction: Any) -> Rung:
    """Build a one-instruction rung; unconditional when *condition* is ``None``.

    The rung carries no source location — synthesis rungs are not user code, so
    they never anchor a debugger step or a source-line report.
    """
    rung = Rung() if condition is None else Rung(condition)
    rung.add_instruction(instruction)
    return rung


_TIMER_UNITS_MS = (("Tms", 1), ("Ts", 1_000), ("Tm", 60_000), ("Th", 3_600_000), ("Td", 86_400_000))
_TIMER_ACC_MAX = 32767


def plant_timer(delay_ms: int) -> tuple[int, str, int]:
    """``(preset, unit, ms per unit)`` for a plant timer that waits *delay_ms*.

    A timer's accumulator stops at 32767 of its unit, so a preset past that is never
    done.  This takes the finest unit the delay fits in, rounding up to a whole one:
    milliseconds up to 32.767 s, then seconds, minutes, hours and days.
    """
    for unit, unit_ms in _TIMER_UNITS_MS:
        preset = -(-delay_ms // unit_ms)
        if preset <= _TIMER_ACC_MAX:
            return preset, unit, unit_ms
    raise ValueError(
        f"a feedback delay of {delay_ms} ms is longer than a timer can count (32767 days)"
    )


def bool_feedback_rungs(
    *,
    enable: Condition,
    fb_tag: Tag,
    ton_done: Tag,
    ton_acc: Tag,
    tof_acc: Tag,
    on_delay_ms: int,
    off_delay_ms: int,
) -> list[Rung]:
    """Lower a bool coupling to a real on-delay/off-delay timer pair (dwell).

    Two plant rungs, exactly what a hand-written ladder would use::

        Rung(enable):   on_delay(ton_done, ton_acc, on_delay_ms)
        Rung(ton_done): off_delay(fb_tag,  tof_acc, off_delay_ms)

    ``enable`` is sustained for ``on_delay`` before ``ton_done`` rises; feeding
    ``ton_done`` through the off-delay keeps ``fb_tag`` asserted for
    ``off_delay`` after the enable drops.  A glitch shorter than ``on_delay``
    resets the accumulator, so feedback that was never sustained is never
    fabricated — the dwell semantics, now native (no transport-delay heap).
    Presets are in the unit :func:`plant_timer` picks, ms unless the delay is too
    long for it, so a held enable crosses after ``ceil(delay_ms / dt_ms)`` scans.
    """
    from pyrung.core.condition import BitCondition
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    on_preset, on_unit, _ = plant_timer(on_delay_ms)
    off_preset, off_unit, _ = plant_timer(off_delay_ms)
    ton = OnDelayInstruction(ton_done, ton_acc, on_preset, enable, unit=on_unit)
    ton_power = BitCondition(ton_done)
    tof = OffDelayInstruction(fb_tag, tof_acc, off_preset, ton_power, unit=off_unit)
    return [_rung_holding(enable, ton), _rung_holding(ton_power, tof)]


def analog_feedback_rungs(
    *,
    enable: Condition,
    disable: Condition,
    fb_tag: Tag,
    armed: Tag,
    dt_tag: Tag,
    up: float,
    down: float,
) -> list[Rung]:
    """Lower a linear analog coupling (a :class:`~pyrung.core.physical.Ramp`) to plant rungs.

    Rates are per **second**, applied against ``dt_tag`` (``sys.dt``), so the ramp
    is stable across scan periods.  The rungs are exactly what a hand-written
    plant-sim ladder would use::

        Rung(enable):            calc(fb + up*dt,   fb)   # advance while energized
        Rung(enable):            latch(armed)             # wake on first energize
        Rung(armed AND disable): calc(fb + down*dt, fb)   # decay once armed, off-enable only

    ``disable`` is the caller-supplied negation of ``enable`` (the harness knows
    whether the enable is a bit or an ``== trigger`` compare, so it builds the
    exact complement).  ``down == 0`` means "hold on enable fall" — no decay, so
    the arm latch and decay rung are omitted (nothing to gate) and only the
    advance rung is built.  The ``armed`` latch is the rung-native replacement for
    the old enable-edge ``active`` monitor: a plant sits at rest until first
    energized, and unlike a monitor it recomputes deterministically under replay.
    """
    from pyrung.core.condition import AllCondition, BitCondition
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.coils import LatchInstruction

    rungs = [_rung_holding(enable, CalcInstruction(fb_tag + up * dt_tag, fb_tag))]
    if down != 0.0:
        rungs.append(_rung_holding(enable, LatchInstruction(armed)))
        decay_guard = AllCondition(BitCondition(armed), disable)
        rungs.append(_rung_holding(decay_guard, CalcInstruction(fb_tag + down * dt_tag, fb_tag)))
    return rungs


def analog_approach_rung(
    *,
    enable: Condition,
    fb_tag: Tag,
    toward: Tag | float,
    rate: float,
    dt_tag: Tag,
) -> list[Rung]:
    """Lower a first-order analog coupling (an :class:`~pyrung.core.physical.Approach`).

    One guarded ``calc`` plant rung — ``fb += rate*(toward - fb)*dt`` while
    energized, holding on enable fall::

        Rung(enable): calc(fb + rate*(toward - fb)*dt, fb)

    ``toward`` is a constant setpoint or a setpoint tag.  There is no decay/arm:
    the plant holds its last value when disabled (a first-order lag has no
    ambient drift term), so a never-energized plant simply stays at rest.  The
    slope depends on ``fb`` itself, so ``how()`` measures the coast empirically.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    expr = fb_tag + rate * (toward - fb_tag) * dt_tag
    return [_rung_holding(enable, CalcInstruction(expr, fb_tag))]


def pulse_feedback_rungs(
    *,
    enable: Condition,
    disable: Condition,
    fb_tag: Tag,
    on_done: Tag,
    on_acc: Tag,
    off_done: Tag,
    off_acc: Tag,
    on_dwell_ms: int,
    off_dwell_ms: int,
) -> list[Rung]:
    """Lower a bool pulse-train coupling (a :class:`~pyrung.core.physical.Pulse`).

    An astable oscillator in plant rungs — the classic ladder "flasher".  While
    enabled, ``fb`` cycles high for ``on_dwell`` then low for ``off_dwell``; when
    disabled it rests low::

        Rung(enable & fb):             on_delay(on_done,  on_acc,  on_dwell)   # high dwell
        Rung(enable & ~fb):            on_delay(off_done, off_acc, off_dwell)  # low dwell
        Rung(enable & ~fb & off_done): latch(fb)   # low long enough → go high
        Rung(enable & fb & on_done):   reset(fb)   # high long enough → go low
        Rung(~enable):                 reset(fb)   # disabled → low

    Each dwell timer is a real on-delay that auto-resets when its guard drops
    (``fb`` flips), so the two ping-pong.  Each rung reads ``fb`` and the timers
    as the rungs above it left them, so a half-cycle costs one scan of slop —
    inconsequential for a pulse train.
    Presets are in the unit :func:`plant_timer` picks, ms unless the dwell is too long
    for it, so a dwell lasts ``ceil(dwell_ms/dt_ms)`` scans.
    """
    from pyrung.core.condition import AllCondition, BitCondition
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.timers import OnDelayInstruction

    fb_high = BitCondition(fb_tag)
    fb_low = ~fb_tag
    on_guard = AllCondition(enable, fb_high)
    off_guard = AllCondition(enable, fb_low)
    on_preset, on_unit, _ = plant_timer(on_dwell_ms)
    off_preset, off_unit, _ = plant_timer(off_dwell_ms)
    ton_high = OnDelayInstruction(on_done, on_acc, on_preset, on_guard, unit=on_unit)
    ton_low = OnDelayInstruction(off_done, off_acc, off_preset, off_guard, unit=off_unit)
    return [
        _rung_holding(on_guard, ton_high),
        _rung_holding(off_guard, ton_low),
        _rung_holding(
            AllCondition(enable, fb_low, BitCondition(off_done)), LatchInstruction(fb_tag)
        ),
        _rung_holding(
            AllCondition(enable, fb_high, BitCondition(on_done)), ResetInstruction(fb_tag)
        ),
        _rung_holding(disable, ResetInstruction(fb_tag)),
    ]


def copy_hold_rung(
    *,
    value: Any,
    dest: Tag,
    guard: Condition | Tag | None = None,
) -> Rung:
    """Build a hold rung that drives *dest* to *value*.

    ``guard=None`` is a steady hold (drive every scan); a ``guard`` makes it a
    self-releasing hold — drive ``dest`` to ``value`` only while the guard holds
    (the conditional-hold / reactive-re-assert shape).

    Boolean constants use their native coil instructions: ``latch(dest)`` for
    ``True`` and ``reset(dest)`` for ``False``. Other destination/value pairs
    remain ``copy(value, dest)``.
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import TagType

    if dest.type == TagType.BOOL and value is True:
        instruction = LatchInstruction(dest)
    elif dest.type == TagType.BOOL and value is False:
        instruction = ResetInstruction(dest)
    else:
        instruction = CopyInstruction(value, dest)
    return _rung_holding(guard, instruction)


def conditional_hold_rung(
    *,
    dest: Tag,
    rules: list[tuple[Any, Condition | Tag | None]],
) -> Rung:
    """Build a multi-rule hold: drive ``dest`` to the last active rule's value.

    Each ``(value, guard)`` rule drives ``value`` while its ``guard`` holds; a
    later active rule wins, and with no rule active ``dest`` keeps its value.
    Every guard means the state the bracket starts from.  The rules compile
    like :func:`guarded_copy_rung`'s, so a liveness oscillator's "drive True
    while ``dest != True``" + "drive False while ``dest != False``" is one
    ``out(dest)`` whose condition reads ``dest`` before writing it, and
    ``dest`` flips every scan.  Values must be constants, as there.

    A single rule is better expressed by :func:`copy_hold_rung`; this is for the
    multi-rule (oscillating) case.
    """
    return _guarded_rows([(dest, value, guard) for value, guard in rules], keep=True)


def guarded_copy_rung(
    rules: list[tuple[Tag, Any, Condition | Tag]],
) -> Rung:
    """Build one rung from ordered guarded writes: the last active write wins.

    A Boolean destination returns to ``False`` when none of its rules is
    active; a non-Boolean destination keeps its prior (plant-supplied) value.
    Every guard means the state the bracket starts from, before any of its
    writes, including a guard that reads the destination it drives.

    Every value must be a constant (``bool``, ``int``, ``float`` or ``str``).
    A tag or expression value would be read when the scan reaches its
    ``copy``, after earlier writes of the bracket, so it raises ``TypeError``.

    Each instruction reads its conditions when the scan reaches it, as in any
    user rung, so the rung is laid out so that no guard reads an earlier write
    of the bracket:

    * Each destination has one writer.  A Boolean destination whose values
      are all ``True``/``False`` (or ``1``/``0``) is one ``out(dest)`` whose
      condition folds the ordered rules (a ``True`` rule ORs its guard in, a
      ``False`` rule ANDs its complement in).  Any other destination has one
      ``copy`` per distinct value, guarded by "the last active rule has this
      value", so its copies are exclusive.
    * A destination whose guards read another destination sits above that
      destination's writer.
    * A guard that would still read an earlier write (a cycle between
      destinations, a multi-copy destination whose guards read itself, or a
      complement with no contact form) is first solved onto an internal
      ``__frz{n}__{dest}`` bit at the top of the rung, and the writers read
      that bit instead: decide first, then act, as a ladder programmer would.
    """
    return _guarded_rows(list(rules), keep=False)


_ALWAYS: Any = object()
_NEVER: Any = object()


def _condition_or(term: Any, rest: Any) -> Any:
    from pyrung.core.condition import AnyCondition

    if term is _ALWAYS or rest is _ALWAYS:
        return _ALWAYS
    if term is _NEVER:
        return rest
    if rest is _NEVER:
        return term
    return AnyCondition(term, rest)


def _condition_and(term: Any, rest: Any) -> Any:
    from pyrung.core.condition import AllCondition

    if term is _NEVER or rest is _NEVER:
        return _NEVER
    if term is _ALWAYS:
        return rest
    if rest is _ALWAYS:
        return term
    return AllCondition(term, rest)


def _condition_complement(condition: Any) -> Any | None:
    """The exact complement of *condition* in contact form, or ``None``.

    Compares, contacts and INT truthiness have a complement contact; ``And`` and
    ``Or`` complement by De Morgan; a condition may supply ``_complement()``.
    Edge contacts (they keep memory of their own) and anything else opaque have
    no complement here, so the caller freezes them onto a bit instead.
    """
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        IntTruthyCondition,
        NormallyClosedCondition,
    )
    from pyrung.core.tag import TagType

    if condition is _ALWAYS:
        return _NEVER
    if condition is _NEVER:
        return _ALWAYS
    complements: dict[type, type] = {
        CompareEq: CompareNe,
        CompareNe: CompareEq,
        CompareLt: CompareGe,
        CompareGe: CompareLt,
        CompareLe: CompareGt,
        CompareGt: CompareLe,
    }
    kind = type(condition)
    if kind in complements:
        return complements[kind](condition.tag, condition.value)
    if kind is BitCondition:
        return NormallyClosedCondition(condition.tag)
    if kind is NormallyClosedCondition:
        return BitCondition(condition.tag)
    if kind is IntTruthyCondition and condition.tag.type in (TagType.INT, TagType.DINT):
        return CompareEq(condition.tag, 0)
    if kind in (AllCondition, AnyCondition):
        parts: list[Any] = []
        for part in condition.conditions:
            flipped = _condition_complement(part)
            if flipped is None:
                return None
            parts.append(flipped)
        return AnyCondition(*parts) if kind is AllCondition else AllCondition(*parts)
    complement = getattr(condition, "_complement", None)
    if callable(complement):
        return complement()
    return None


def _static_reads(condition: Any) -> set[str]:
    if condition is _ALWAYS or condition is _NEVER:
        return set()
    from pyrung.core.analysis.pdg import _extract_tag_names

    return _extract_tag_names(condition, {})


def _value_key(value: Any) -> Any:
    return ("const", value)


def _is_bool_constant(value: Any) -> bool:
    return isinstance(value, bool) or (type(value) is int and value in (0, 1))


@dataclass
class _DestRules:
    """One destination's ordered ``(rule index, value, guard)`` rules."""

    dest: Tag
    rules: list[tuple[int, Any, Any]]

    @property
    def coil(self) -> bool:
        from pyrung.core.tag import TagType

        return self.dest.type == TagType.BOOL and all(
            _is_bool_constant(value) for _index, value, _guard in self.rules
        )

    def row_count(self, *, keep: bool) -> int:
        from pyrung.core.tag import TagType

        if self.coil:
            return 1
        values = {_value_key(value) for _index, value, _guard in self.rules}
        if not keep and self.dest.type == TagType.BOOL:
            values.add(_value_key(False))
        return len(values)

    def reads(self) -> set[str]:
        found: set[str] = set()
        for _index, _value, guard in self.rules:
            found |= _static_reads(guard)
        return found


def _guarded_rows(rules: list[tuple[Tag, Any, Any]], *, keep: bool) -> Rung:
    """Compile ordered ``(dest, value, guard)`` rules to one rung.

    ``keep`` leaves a destination with no active rule at its value; otherwise a
    Boolean destination returns to ``False``.  See :func:`guarded_copy_rung`.
    """
    from pyrung.core.condition import BitCondition, NormallyClosedCondition, _as_condition
    from pyrung.core.instruction.coils import OutInstruction, ResetInstruction
    from pyrung.core.tag import Bool, TagType

    groups: dict[str, _DestRules] = {}
    for index, (dest, value, guard) in enumerate(rules):
        if not isinstance(value, (bool, int, float, str)):
            raise TypeError(
                f"synthesis bracket rule {index} for {dest.name}: value {value!r} is not "
                "a constant (bool, int, float or str); a tag or expression value would "
                "be read after earlier writes of the bracket"
            )
        group = groups.setdefault(dest.name, _DestRules(dest, []))
        group.rules.append((index, value, _ALWAYS if guard is None else _as_condition(guard)))
    reads = {name: group.reads() for name, group in groups.items()}

    # A destination goes above every destination its guards read.  A cycle is
    # broken at the earliest destination in rule order.
    order: list[str] = []
    pending = list(groups)
    while pending:
        ready = next(
            (
                name
                for name in pending
                if not any(name in reads[other] for other in pending if other != name)
            ),
            pending[0],
        )
        order.append(ready)
        pending.remove(ready)

    frozen: dict[int, Any] = {}
    frozen_rows: list[Rung] = []
    writer_rows: list[Rung] = []

    def freeze(index: int, dest: Tag, guard: Any) -> Any:
        bit = frozen.get(id(guard))
        if bit is None:
            tag = Bool(f"__frz{index}__{dest.name}", retentive=False)
            frozen_rows.append(_rung_holding(guard, OutInstruction(tag)))
            bit = frozen[id(guard)] = BitCondition(tag)
        return bit

    def complement(index: int, dest: Tag, guard: Any) -> Any:
        negated = _condition_complement(guard)
        if negated is not None:
            return negated
        return NormallyClosedCondition(freeze(index, dest, guard).tag)

    written: set[str] = set()
    for name in order:
        group = groups[name]
        dest = group.dest
        unsafe = set(written)
        if group.row_count(keep=keep) > 1:
            unsafe.add(name)
        guards = [
            (index, value, freeze(index, dest, guard) if _static_reads(guard) & unsafe else guard)
            for index, value, guard in group.rules
        ]

        if group.coil:
            power: Any = BitCondition(dest) if keep else _NEVER
            for index, value, guard in guards:
                if value:
                    power = _condition_or(guard, power)
                elif power is not _NEVER:
                    power = _condition_and(complement(index, dest, guard), power)
            if power is _NEVER:
                writer_rows.append(_rung_holding(None, ResetInstruction(dest)))
            else:
                condition = None if power is _ALWAYS else power
                writer_rows.append(_rung_holding(condition, OutInstruction(dest)))
        else:
            buckets: dict[Any, list[Any]] = {}
            if not keep and dest.type == TagType.BOOL:
                buckets[_value_key(False)] = [False, _ALWAYS]
            for index, value, guard in guards:
                key = _value_key(value)
                buckets.setdefault(key, [value, _NEVER])
                for bucket_key, bucket in buckets.items():
                    if bucket_key == key:
                        bucket[1] = _condition_or(guard, bucket[1])
                    elif bucket[1] is not _NEVER:
                        bucket[1] = _condition_and(complement(index, dest, guard), bucket[1])
            for value, power in buckets.values():
                if power is not _NEVER:
                    condition = None if power is _ALWAYS else power
                    writer_rows.append(copy_hold_rung(value=value, dest=dest, guard=condition))
        written.add(name)

    outer = Rung()
    for row in (*frozen_rows, *writer_rows):
        outer.add_branch(row)
    return outer


def function_rung(
    fn: Any,
    *,
    ins: dict[str, Any],
    outs: dict[str, Any],
    guard: Condition | Tag | None = None,
) -> Rung:
    """Build a guarded rung that runs an opaque ``fn`` with declared ins/outs.

    The ``ins``/``outs`` are declared, so the dataflow stays visible to PDG /
    trace / causal even though the body is opaque (the analog profile, a
    sandbox probe).  ``guard=None`` runs every scan; a guard arms it (e.g. an
    analog coupling that ticks only while its enable has activated it).
    """
    from pyrung.core.instruction.control import FunctionCallInstruction

    return _rung_holding(guard, FunctionCallInstruction(fn, ins=ins, outs=outs))
