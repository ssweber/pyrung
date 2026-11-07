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

import math
import re
from collections.abc import Iterable, Iterator
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
TIMER_ACC_MAX = 32767


def plant_timer(delay_ms: int) -> tuple[int, str, int]:
    """``(preset, unit, ms per unit)`` for a plant timer that waits *delay_ms*.

    A timer's accumulator stops at 32767 of its unit, so a preset past that is never
    done.  This takes the finest unit the delay fits in, rounding up to a whole one:
    milliseconds up to 32.767 s, then seconds, minutes, hours and days.
    """
    for unit, unit_ms in _TIMER_UNITS_MS:
        preset = -(-delay_ms // unit_ms)
        if preset <= TIMER_ACC_MAX:
            return preset, unit, unit_ms
    raise ValueError(
        f"a feedback delay of {delay_ms} ms is longer than a timer can count (32767 days)"
    )


def plant_acc_name(stem: str, fb_name: str, delay_ms: int) -> str:
    """The accumulator of a plant timer on *fb_name*, in the unit it counts *delay_ms* in.

    A count means nothing without its unit, so a delay in another unit counts in another
    tag, and any state reads the same whichever coupling runs on it, a fork from before
    the link changed included.  Milliseconds have the plain name.
    """
    _preset, unit, _unit_ms = plant_timer(delay_ms)
    return _acc_name_in(unit, stem, fb_name)


def plant_acc_registers(stem: str, fb_name: str) -> tuple[tuple[str, int], ...]:
    """Each accumulator a plant timer on *fb_name* could count in, and its ms per unit."""
    return tuple((_acc_name_in(unit, stem, fb_name), ms) for unit, ms in _TIMER_UNITS_MS)


def _acc_name_in(unit: str, stem: str, fb_name: str) -> str:
    return f"{stem}__{fb_name}" if unit == "Tms" else f"{stem}_{unit}__{fb_name}"


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

        Rung(enable):            math(fb + up*dt,   fb)   # advance while energized
        Rung(enable):            latch(armed)             # wake on first energize
        Rung(armed AND disable): math(fb + down*dt, fb)   # decay once armed, off-enable only

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

        Rung(enable): math(fb + rate*(toward - fb)*dt, fb)

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
    while ``dest != True``" + "drive False while ``dest != False``" is one row
    that reads ``dest`` before it writes it, and ``dest`` flips every scan.
    Values must be constants, as there.

    A single rule is better expressed by :func:`copy_hold_rung`; this is for the
    multi-rule (oscillating) case.
    """
    return _guarded_rows([(dest, value, guard) for value, guard in rules], keep=True)


def guarded_copy_rung(
    rules: list[tuple[Tag, Any, Condition | Tag]],
) -> Rung:
    """Build one rung from ordered guarded writes.  The last active write wins.

    A Boolean destination returns to ``False`` when none of its rules is
    active; a non-Boolean destination keeps its prior (plant-supplied) value.
    Every guard means the state the bracket starts from, before any of its
    writes, including a guard that reads the destination it drives.

    Every value must be a constant (``bool``, ``int``, ``float`` or ``str``).
    A tag or expression value would be read when the scan reaches its
    ``copy``, after earlier writes of the bracket, so it raises ``TypeError``.

    Each instruction reads its conditions when the scan reaches it, as in any
    user rung, so the rung is laid out so that no guard reads an earlier write
    of the bracket.  Decide first, then act, as a ladder programmer would:

    * A destination with two or more rules gives each rule a bit of its own,
      ``__on{n}__{dest}`` or ``__off{n}__{dest}`` for a Boolean value and
      ``__set{n}__{dest}`` for any other, numbered from 1 by kind in rule
      order.  Every bit row sits at the top of the rung, above every
      destination row, so each bit reads the state the bracket starts from.
    * A Boolean destination whose values are all ``True``/``False`` (or
      ``1``/``0``) is then one ``out(dest)`` over its bits (a ``True`` rule
      ORs its bit in, a ``False`` rule ANDs the bit's complement in).  Any
      other destination has one ``copy`` per bit, in rule order, so the last
      active rule writes last.  A Boolean destination given any other value
      gets a reset row above its copies, so it is off when no rule is on.
    * A destination with two or more rules gets one row and no bits when its rules
      simplify exactly to contacts in series with no ``Or``.  None of the rules may read
      another destination of the rung.  A Boolean destination's row is the
      ``out(dest)`` its bits would power, with each bit's guard in the bit's place.  Any
      other destination merges into one ``copy`` only when all its rules give one value.
    * A destination with one rule is one row on its guard.  It sits above
      every destination its guard reads.  Its rule takes a bit instead in
      these cases:

      - its guard would still read an earlier write (a cycle between
        destinations)
      - its destination is a Boolean given another value, whose reset row
        comes first
      - it is a kept ``False`` rule whose guard has no exact complement

    * A rule that can never decide the value is left out.  That is every rule
      before an unguarded one, and a ``False`` rule before every ``True`` rule
      of a Boolean destination.
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
    # One flat Or prints as one contact list.  An Or inside an Or is the same logic.
    return AnyCondition(*_flat(AnyCondition, term), *_flat(AnyCondition, rest))


def _condition_and(term: Any, rest: Any) -> Any:
    from pyrung.core.condition import AllCondition

    if term is _NEVER or rest is _NEVER:
        return _NEVER
    if term is _ALWAYS:
        return rest
    if rest is _ALWAYS:
        return term
    return AllCondition(*_flat(AllCondition, term), *_flat(AllCondition, rest))


def _flat(kind: type, condition: Any) -> tuple[Any, ...]:
    return tuple(condition.conditions) if type(condition) is kind else (condition,)


class _NotExact(Exception):
    """A contact the simplifier can't treat as a plain value.  The condition is left as it is."""


_EXACT_FLIPS = {
    "xic": "xio",
    "xio": "xic",
    "eq": "ne",
    "ne": "eq",
    "lt": "ge",
    "ge": "lt",
    "le": "gt",
    "gt": "le",
}


def _simplified(condition: Any) -> Any:
    """*condition* without terms that cannot change it, or *condition* as it was.

    Combining rules leaves terms a ladder programmer would cut.  Two examples are a contact
    already required beside an Or that contains it, and a branch that needs a contact the rest
    of the rung has open.  Only exact rules are used, so the rung does the same:

    - the rules of ``simplified.simplify``
    - inside an And, each part may assume the contacts beside it are closed
    - inside an Or, each part may assume the contacts beside it are open

    These leave the condition as it was:

    - an edge contact, which keeps memory of its own
    - an immediate contact
    - an indirect or computed compare
    - anything opaque
    """
    from pyrung.core.analysis.simplified import (
        And,
        Atom,
        Const,
        Or,
        _condition_to_expr,
        _expr_key,
        simplify,
    )
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
    from pyrung.core.tag import ImmediateRef, Tag, TagType

    if condition is _ALWAYS or condition is _NEVER:
        return condition
    plain = (BitCondition, NormallyClosedCondition, IntTruthyCondition)
    compares = (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)
    originals: dict[tuple[Any, ...], Any] = {}
    # Ordered compares that may see a NaN.  Each is false when its opposite is false too.
    unflippable: set[tuple[Any, ...]] = set()

    def lower(part: Any) -> Any:
        kind = type(part)
        if kind in (AllCondition, AnyCondition):
            terms = tuple(lower(child) for child in part.conditions)
            return And(terms) if kind is AllCondition else Or(terms)
        if kind not in plain + compares or isinstance(part.tag, ImmediateRef):
            raise _NotExact
        if kind in compares and not isinstance(part.value, (Tag, bool, int, float, str)):
            raise _NotExact
        if (
            kind in (CompareEq, CompareNe)
            and part.tag.type == TagType.BOOL
            and isinstance(part.value, bool)
        ):
            # A Bool compared to a constant is a contact and prints as one, so both get one key.
            closed = part.value is (kind is CompareEq)
            part = BitCondition(part.tag) if closed else NormallyClosedCondition(part.tag)
        atom = _condition_to_expr(part)
        originals.setdefault(_expr_key(atom), part)
        if kind in compares and kind not in (CompareEq, CompareNe) and not _nan_free(part):
            unflippable.add(_expr_key(atom))
        return atom

    def flip(atom: Any) -> tuple[Any, ...] | None:
        form = _EXACT_FLIPS.get(atom.form)
        if form is None or _expr_key(atom) in unflippable:
            return None
        return _expr_key(Atom(atom.tag, form, atom.operand, operand_is_tag=atom.operand_is_tag))

    def resolve(expr: Any, known: dict[tuple[Any, ...], bool]) -> Any:
        if isinstance(expr, Atom):
            key = _expr_key(expr)
            if key in known:
                return Const(known[key])
            flipped = flip(expr)
            if flipped is not None and flipped in known:
                return Const(not known[flipped])
            return expr
        if isinstance(expr, (And, Or)):
            holds = isinstance(expr, And)
            terms = []
            for index, term in enumerate(expr.terms):
                beside = dict(known)
                for other_index, other in enumerate(expr.terms):
                    if other_index != index and isinstance(other, Atom):
                        beside[_expr_key(other)] = holds
                terms.append(resolve(term, beside))
            return And(tuple(terms)) if holds else Or(tuple(terms))
        return expr

    def raise_back(expr: Any) -> Any:
        if isinstance(expr, Const):
            return _ALWAYS if expr.value else _NEVER
        if isinstance(expr, Atom):
            return originals[_expr_key(expr)]
        parts = [raise_back(term) for term in expr.terms]
        return AllCondition(*parts) if isinstance(expr, And) else AnyCondition(*parts)

    try:
        expr = simplify(lower(condition))
    except _NotExact:
        return condition
    for _ in range(20):
        reduced = simplify(resolve(expr, {}))
        if reduced == expr:
            break
        expr = reduced
    return raise_back(expr)


def _flattened(condition: Any) -> Any:
    """*condition* as the same logic with no And directly inside an And, nor Or inside an Or."""
    from pyrung.core.condition import AllCondition, AnyCondition

    kind = type(condition)
    if kind not in (AllCondition, AnyCondition):
        return condition
    parts: list[Any] = []
    for part in condition.conditions:
        parts.extend(_flat(kind, _flattened(part)))
    return kind(*parts)


def _may_be_nan(operand: Any) -> bool:
    """True when *operand* can read as NaN, so a compare on it has no plain opposite.

    These can read as NaN:

    - a ``Real`` tag or a NaN constant
    - an indirect ref, range or expression that reads a ``Real``
    - an expression that holds an infinite constant, since ``inf - inf`` is NaN
    """
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )
    from pyrung.core.tag import ImmediateRef, Tag, TagType

    if isinstance(operand, ImmediateRef):
        return _may_be_nan(operand.value)
    if isinstance(operand, Tag):
        return operand.type == TagType.REAL
    if isinstance(operand, float):
        return operand != operand
    if isinstance(operand, (IndirectRef, IndirectExprRef, BlockRange, IndirectBlockRange)):
        return operand.block.type == TagType.REAL
    if isinstance(operand, Expression):
        return any(
            (isinstance(part, float) and not math.isfinite(part)) or _may_be_nan(part)
            for key, part in vars(operand).items()
            if not key.startswith("_")
        )
    if isinstance(operand, (list, tuple)):
        return any(_may_be_nan(part) for part in operand)
    return False


def _nan_free(compare: Any) -> bool:
    """True when neither side of *compare* can be NaN.

    ``R < 5`` and ``R >= 5`` are both False when ``R`` holds NaN, so one is the other's
    complement only when no side can be NaN.
    """
    return not (_may_be_nan(compare.tag) or _may_be_nan(compare.value))


def _condition_complement(condition: Any) -> Any | None:
    """The exact complement of *condition* in contact form, or ``None``.

    Compares, contacts and INT truthiness have a complement contact.  ``And`` and
    ``Or`` complement by De Morgan, and a condition may supply ``_complement()``.
    ``R < 5`` and ``R >= 5`` are both False when ``R`` holds NaN, so the complement of
    ``R < 5`` is ``Or(R >= 5, R != R)``.  The conditions below have no complement, so the
    caller puts the rule on a bit instead:

    - an edge contact, which keeps memory of its own
    - an ordered compare against a NaN constant
    - anything else opaque
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
    from pyrung.core.tag import Tag, TagType

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
        flipped = complements[kind](condition.tag, condition.value)
        if kind in (CompareEq, CompareNe) or _nan_free(condition):
            return flipped
        value = condition.value
        if isinstance(value, float) and value != value:
            return None
        # ``X != X`` is True exactly when X holds NaN.
        nan_reads = [CompareNe(condition.tag, condition.tag)] if _may_be_nan(condition.tag) else []
        if isinstance(value, Tag) and _may_be_nan(value):
            nan_reads.append(CompareNe(value, value))
        elif _may_be_nan(value):
            nan_reads.append(value != value)
        return AnyCondition(flipped, *nan_reads)
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


def _is_bool_constant(value: Any) -> bool:
    return isinstance(value, bool) or (type(value) is int and value in (0, 1))


@dataclass
class _DestRules:
    """One destination's ordered ``(value, guard)`` rules."""

    dest: Tag
    rules: list[tuple[Any, Any]]

    @property
    def coil(self) -> bool:
        from pyrung.core.tag import TagType

        return self.dest.type == TagType.BOOL and all(
            _is_bool_constant(value) for value, _guard in self.rules
        )

    def deciding(self, *, keep: bool) -> list[tuple[Any, Any]]:
        """The rules that can decide the value, in order."""
        rules = [(value, guard) for value, guard in self.rules if guard is not _NEVER]
        unguarded = [index for index, (_value, guard) in enumerate(rules) if guard is _ALWAYS]
        if unguarded:
            # An unguarded rule is always on, so no earlier rule is ever the last one on.
            rules = rules[unguarded[-1] :]
        if self.coil and not keep:
            # A coil with no rule on is off anyway.  So a False rule before every True one
            # changes nothing.
            first_on = next((index for index, (value, _g) in enumerate(rules) if value), None)
            rules = [] if first_on is None else rules[first_on:]
        return rules

    def bit_kind(self, value: Any) -> str:
        return (_ON if value else _OFF) if self.coil else _SET


_ON, _OFF, _SET = "on", "off", "set"
# The debugger hides the same names (``pyrung.dap.internal_tags``).
RULE_BIT_PREFIX = rf"__(?:{_ON}|{_OFF}|{_SET})\d+__"
_RULE_BIT = re.compile(rf"{RULE_BIT_PREFIX}(?=.)", re.DOTALL)
_DECIDE_FIRST_COMMENT = (
    "Decide first, so the rows below read the state from before this logic wrote."
)


def _is_rule_bit(name: str | None) -> bool:
    return name is not None and _RULE_BIT.match(name) is not None


def _guarded_rows(
    rules: list[tuple[Tag, Any, Any]],
    *,
    keep: bool,
    merge: bool = True,
) -> Rung:
    """Compile ordered ``(dest, value, guard)`` rules to one rung.

    ``keep`` leaves a destination with no active rule at its value; otherwise a
    Boolean destination returns to ``False``.  See :func:`guarded_copy_rung`.
    ``merge=False`` keeps every bit, so a test can check a merged row against its bits.
    """
    from pyrung.core.condition import BitCondition, _as_condition
    from pyrung.core.instruction.coils import OutInstruction, ResetInstruction
    from pyrung.core.tag import Bool, TagType

    groups: dict[str, _DestRules] = {}
    # Each guard's reads, by the guard's id.  The layout below asks for them more than once.
    guard_reads: dict[int, set[str]] = {}
    for index, (dest, value, guard) in enumerate(rules):
        if not isinstance(value, (bool, int, float, str)):
            raise TypeError(
                f"synthesis bracket rule {index} for {dest.name}: value {value!r} is not "
                "a constant (bool, int, float or str); a tag or expression value would "
                "be read after earlier writes of the bracket"
            )
        guard = _ALWAYS if guard is None else _simplified(_flattened(_as_condition(guard)))
        # The bracket writes its rule bits by name, so a tag of the same name would be
        # overwritten under the rule that reads or drives it.
        guard_reads[id(guard)] = _static_reads(guard)
        for name in sorted({dest.name, *guard_reads[id(guard)]}):
            if _is_rule_bit(name):
                raise ValueError(
                    f"how() names a bit of its temporary logic {name!r}, the same name as "
                    "your tag. Rename the tag."
                )
        group = groups.setdefault(dest.name, _DestRules(dest, []))
        group.rules.append((value, guard))
    deciding = {name: group.deciding(keep=keep) for name, group in groups.items()}

    def single_row(group: _DestRules, value: Any, guard: Any) -> Rung | None:
        """The one row a lone rule needs, or None when its guard must go on a bit."""
        dest = group.dest
        if not group.coil:
            if not keep and dest.type == TagType.BOOL:
                # Its reset comes before its copy, so the copy's guard goes on a bit.
                return None
            return copy_hold_rung(value=value, dest=dest, guard=_none_if_always(guard))
        power: Any = BitCondition(dest) if keep else _NEVER
        if value:
            power = _condition_or(power, guard)
        elif power is not _NEVER:
            negated = _condition_complement(guard)
            if negated is None:
                return None
            power = _condition_and(power, negated)
        return coil_row(dest, power)

    def coil_row(dest: Tag, power: Any) -> Rung:
        power = _simplified(power)
        if power is _NEVER:
            return _rung_holding(None, ResetInstruction(dest))
        return _rung_holding(None if power is _ALWAYS else power, OutInstruction(dest))

    def merged_row(name: str, group: _DestRules, kept: list[tuple[Any, Any]]) -> Rung | None:
        """The tag's rules as one row of contacts in series, or None when they need bits.

        A row reads its contacts when the scan reaches it, after the rows above have
        written.  So the row stands in for the bits only when it reads no other tag the
        rung writes.
        """
        others = set(groups) - {name}
        if any(guard_reads[id(guard)] & others for _value, guard in kept):
            return None
        dest = group.dest
        if group.coil:
            power: Any = BitCondition(dest) if keep else _NEVER
            for value, guard in kept:
                if value:
                    power = _condition_or(power, guard)
                elif power is not _NEVER:
                    negated = _condition_complement(guard)
                    if negated is None:
                        return None
                    power = _condition_and(power, negated)
            power = _simplified(_flattened(power))
            return coil_row(dest, power) if _is_series(power) else None
        value = kept[0][0]
        if dest.type == TagType.BOOL or any(
            type(other) is not type(value) or other != value for other, _guard in kept
        ):
            return None
        guard: Any = _NEVER
        for _value, rule_guard in kept:
            guard = _condition_or(guard, rule_guard)
        guard = _simplified(_flattened(guard))
        if not _is_series(guard):
            return None
        return copy_hold_rung(value=value, dest=dest, guard=_none_if_always(guard))

    # A destination goes above every destination its guard reads.  A destination on bits reads
    # only its bits, which sit above them all.  A cycle is broken at the earliest destination in
    # rule order.
    reads = {
        name: guard_reads[id(deciding[name][0][1])] if len(deciding[name]) == 1 else set()
        for name in groups
    }
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

    bit_rows: list[Rung] = []
    writer_rows: list[Rung] = []
    written: set[str] = set()
    for name in order:
        group = groups[name]
        dest = group.dest
        kept = deciding[name]
        reads_a_write = len(kept) == 1 and bool(guard_reads[id(kept[0][1])] & written)
        written.add(name)
        if not kept:
            if not keep and dest.type == TagType.BOOL:
                # No rule can turn it on, so it is off for as long as the logic is in.
                writer_rows.append(_rung_holding(None, ResetInstruction(dest)))
            continue
        if len(kept) == 1 and not reads_a_write:
            row = single_row(group, *kept[0])
            if row is not None:
                writer_rows.append(row)
                continue
        if merge and len(kept) > 1:
            row = merged_row(name, group, kept)
            if row is not None:
                writer_rows.append(row)
                continue

        counts: dict[str, int] = {}
        conditions: list[tuple[Any, Any]] = []
        for value, guard in kept:
            if guard is _ALWAYS:
                conditions.append((value, _ALWAYS))
                continue
            kind = group.bit_kind(value)
            counts[kind] = counts.get(kind, 0) + 1
            bit = Bool(f"__{kind}{counts[kind]}__{dest.name}", retentive=False)
            row = _rung_holding(guard, OutInstruction(bit))
            row.comment = _DECIDE_FIRST_COMMENT
            bit_rows.append(row)
            conditions.append((value, BitCondition(bit)))

        if group.coil:
            power: Any = BitCondition(dest) if keep else _NEVER
            for value, condition in conditions:
                if value:
                    power = _condition_or(power, condition)
                elif power is not _NEVER:
                    power = _condition_and(power, _condition_complement(condition))
            writer_rows.append(coil_row(dest, power))
            continue
        if not keep and dest.type == TagType.BOOL:
            writer_rows.append(_rung_holding(None, ResetInstruction(dest)))
        for value, condition in conditions:
            writer_rows.append(
                copy_hold_rung(value=value, dest=dest, guard=_none_if_always(condition))
            )

    outer = Rung()
    for row in (*bit_rows, *writer_rows):
        outer.add_branch(row)
    return outer


def _is_series(condition: Any) -> bool:
    """True when *condition* is contacts and plain compares in series, with no ``Or``.

    Only these lower to the exact simplifier, so a series of them was simplified exactly.
    """
    from pyrung.core.condition import (
        AllCondition,
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
    from pyrung.core.tag import Tag

    if condition is _ALWAYS or condition is _NEVER:
        return True
    contacts = (BitCondition, NormallyClosedCondition, IntTruthyCondition)
    compares = (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)
    parts = condition.conditions if type(condition) is AllCondition else (condition,)
    return all(
        (type(part) in contacts and isinstance(part.tag, Tag))
        or (
            type(part) in compares
            and isinstance(part.tag, Tag)
            and isinstance(part.value, (Tag, bool, int, float, str))
        )
        for part in parts
    )


def _none_if_always(condition: Any) -> Any:
    return None if condition is _ALWAYS else condition


def _row_target(instruction: Any) -> Any:
    target = getattr(instruction, "target", None)
    return getattr(instruction, "dest", None) if target is None else target


def _row_writes(row: Rung) -> str | None:
    if not row._instructions:
        return None
    return getattr(_row_target(row._instructions[0]), "name", None)


def _render_row(row: Rung) -> tuple[str, str]:
    from pyrung.core.condition import AllCondition
    from pyrung.core.validation.render import operand_name, render_instruction, with_rung_line

    conditions = list(row._conditions)
    if len(conditions) == 1 and isinstance(conditions[0], AllCondition):
        conditions = list(conditions[0].conditions)
    instruction = row._instructions[0]
    code, _caret = render_instruction(instruction, operand_name(_row_target(instruction)))
    return with_rung_line(conditions), code


def bracket_lines(rung: Rung, writes: Iterable[str] | None = None) -> tuple[str, ...]:
    """The rows of a rung built here, as pyrung source, read off the rung itself.

    The text comes from the rung that runs, not from the rules it was built from, so the two
    cannot disagree.  With *writes*, only the rows writing those tags are printed, and above them
    the bit rows they read.  A row's comment prints above it, once for a run of rows with the
    same comment.  Rows that share a condition print under one ``with rung(...)`` line unless a
    comment comes between them.
    """
    rows = list(rung._branches)
    if writes is not None:
        wanted = set(writes)
        kept = [row for row in rows if _row_writes(row) in wanted]
        reads: set[str] = set()
        for row in kept:
            for condition in row._conditions:
                reads |= _static_reads(condition)
        kept_ids = {id(row) for row in kept}
        rows = [
            row
            for row in rows
            if id(row) in kept_ids or (_is_rule_bit(_row_writes(row)) and _row_writes(row) in reads)
        ]
    lines: list[str] = []
    previous: str | None = None
    said: str | None = None
    for row in rows:
        header, code = _render_row(row)
        if row.comment and row.comment != said:
            text = row.comment.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'comment("{text}")')
            previous = None
        said = row.comment
        if header != previous:
            lines.append(header)
            previous = header
        lines.append(f"  {code}")
    return tuple(lines)


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
