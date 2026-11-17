"""A time drum and a time & event drum side by side, for the drum probes.

``drum_trace`` reads one event drum scan by scan.  The probes ask the same kind of
question of ``time_drum`` and ``time_event_drum``.  So a scenario here runs one of each
on the same inputs (:class:`Pair`) and records them together.

A drum's state at the end of a scan is two digits, ``S`` then ``X``.  ``S`` is the step,
and ``X = 4 * flag + 2 * B + A``.  So 22 is step 2 with B on, 24 is step 2 complete (flag
on, outputs off) and 10 is step 1 with both outputs off.  A snapshot packs the time
drum's state above the time & event drum's.  2222 means both are on step 2 with B on.
2224 means the time drum is on step 2 with B on and the other drum is complete.
:func:`snap` copies one snapshot into a slot field on a named scan.

The step registers are DS, because CLICK's editor accepts no other type for a drum.  The
accumulators are TD, and the completion flags and outputs are C bits.  A time drum moves
by a jog, or by the Acc that a case stages past a preset, because a preset cannot be
timed in scans over the wire.  Its presets are otherwise 30 s, so no scan's time pushes
a trace over.  A time & event drum's steps are event-only (levels), so its events move
it.  The time drum moves by time or by the jog instead.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from pyrung.core import Or, Rung, copy, math, out, time_drum, time_event_drum
from tests.conformance.memory import c

#: A time drum step's preset in ms.  It is longer than any trace.
LONG = 30000


class Pool:
    """Hands out C bits in order from a case's own run of the group's range."""

    def __init__(self, numbers: Sequence[int]) -> None:
        self._free: Iterator[int] = iter(numbers)

    def __call__(self, n: int = 1) -> list[Any]:
        return [c[next(self._free)] for _ in range(n)]


def pool_of(*runs: range) -> Callable[[int, int], Pool]:
    """Return ``pool(index, size)``, which returns the *index*-th run of *size* bits."""
    numbers = [n for run in runs for n in run]

    def pool(index: int, size: int) -> Pool:
        part = numbers[index * size : (index + 1) * size]
        if len(part) < size:
            raise ValueError(f"case {index}: the group's C bits run out ({len(numbers)})")
        return Pool(part)

    return pool


@dataclass(frozen=True)
class Resources:
    """Holds a group's raw memory, cut into a run per case.

    The memory is C bits, DS step registers, TD accumulators and DD states (``memory.OWNED``).
    """

    bits: Callable[[int, int], Pool]
    #: DS step registers.  Case *i* gets ``raw_counts[i]`` of them, in order.
    raw: Sequence[int]
    raw_counts: Sequence[int]
    tds: Sequence[int]
    states: Sequence[int]
    bits_per_case: int = 26

    def rig(self, slot: Any, index: int) -> Rig:
        return Rig(slot, self, index)


class Rig:
    """Holds the tags of case *index*: up to two scenarios of a pair of drums each."""

    def __init__(self, slot: Any, res: Resources, index: int) -> None:
        from tests.conformance.memory import dd, ds, td

        self.slot = slot
        self.take = res.bits(index, res.bits_per_case)
        first = sum(res.raw_counts[:index])
        self.raw = [ds[n] for n in res.raw[first : first + res.raw_counts[index]]]
        self.td = [td[n] for n in res.tds[4 * index : 4 * index + 4]]
        self.state = [dd[n] for n in res.states[4 * index : 4 * index + 4]]

    def _tags(self, which: int, own_steps: tuple[Any, Any] | None) -> tuple[Any, ...]:
        if own_steps is not None:
            step_t, step_e = own_steps
        else:
            step_t = self.slot.Result1 if which == 0 else self.raw[1]
            step_e = self.raw[0] if which == 0 else self.raw[2]
        return (
            step_t,
            step_e,
            self.td[2 * which],
            self.td[2 * which + 1],
            self.state[2 * which],
            self.state[2 * which + 1],
        )

    def pair(
        self, which: int = 0, *, own_steps: tuple[Any, Any] | None = None, **options: Any
    ) -> Pair:
        """Return scenario *which* (0 or 1).

        Scenario 0's time drum steps in Result1, unless *own_steps* names the two step
        registers.  A case that needs Result1 for itself passes *own_steps*.
        """
        return make_pair(self.slot, self.take, *self._tags(which, own_steps), **options)

    def alloc(self, which: int = 0, **options: Any) -> Pair:
        """Return the tags of scenario *which*, emitting no rung (see :func:`emit_pair`)."""
        return alloc_pair(self.take, *self._tags(which, None), **options)


@dataclass(frozen=True)
class Side:
    """Holds the tags of one drum of a pair."""

    step: Any
    acc: Any
    a: Any
    b: Any
    flag: Any
    state: Any


@dataclass(frozen=True)
class Pair:
    time: Side
    event: Side
    #: The time drum's jog, and the events of the time & event drum's steps.
    jog: Any
    events: tuple[Any, ...]
    jump: Any
    #: The drum rung's condition besides ``Cmd != 0``.  ``None`` means always.
    pause: Any


def pattern_for(steps: int) -> list[list[int]]:
    """Return the output pattern: step 1 is A, step 2 is B, step 3 is both, then A again."""
    rows = [[1, 0], [0, 1], [1, 1], [1, 0], [0, 1]]
    return rows[:steps]


def alloc_pair(
    take: Pool,
    step_t: Any,
    step_e: Any,
    acc_t: Any,
    acc_e: Any,
    state_t: Any,
    state_e: Any,
    *,
    steps: int = 2,
    jog: bool = False,
    te_jog: bool = False,
    jump: bool = False,
    pausable: bool = False,
) -> Pair:
    """Return the pair's tags and bits.  Emit no rung yet."""
    a_t, b_t, f_t, a_e, b_e, f_e = take(6)
    jog_bit = take(1)[0] if (jog or te_jog) else None
    jump_bit = take(1)[0] if jump else None
    events = tuple(take(steps))
    pause = take(1)[0] if pausable else None
    return Pair(
        Side(step_t, acc_t, a_t, b_t, f_t, state_t),
        Side(step_e, acc_e, a_e, b_e, f_e, state_e),
        jog_bit,
        events,
        jump_bit,
        pause,
    )


def emit_pair(
    slot: Any,
    pair: Pair,
    *,
    presets: Sequence[int] | None = None,
    te_presets: Sequence[int | None] | None = None,
    jog: bool = False,
    te_jog: bool = False,
    jump_step: Any = None,
) -> None:
    """Emit the two drum rungs, with ``~pause`` and ``Cmd != 0`` as their condition."""
    steps = len(pair.events)
    gate = [slot.Cmd != 0]
    if pair.pause is not None:
        gate.append(~pair.pause)
    pattern = pattern_for(steps)
    reset = slot.Cmd == 0
    with Rung(*gate):
        t = time_drum(
            outputs=[pair.time.a, pair.time.b],
            presets=list(presets) if presets is not None else [LONG] * steps,
            unit="ms",
            pattern=pattern,
            current_step=pair.time.step,
            accumulator=pair.time.acc,
            completion_flag=pair.time.flag,
        ).reset(reset)
        if pair.jump is not None:
            t = t.jump(pair.jump, step=jump_step)
        if pair.jog is not None and jog:
            t.jog(pair.jog)
    with Rung(*gate):
        e = time_event_drum(
            outputs=[pair.event.a, pair.event.b],
            presets=list(te_presets) if te_presets is not None else [None] * steps,
            events=list(pair.events),
            unit="ms",
            pattern=pattern,
            current_step=pair.event.step,
            accumulator=pair.event.acc,
            completion_flag=pair.event.flag,
        ).reset(reset)
        if pair.jump is not None:
            e = e.jump(pair.jump, step=jump_step)
        if pair.jog is not None and te_jog:
            e.jog(pair.jog)


def make_pair(
    slot: Any,
    take: Pool,
    step_t: Any,
    step_e: Any,
    acc_t: Any,
    acc_e: Any,
    state_t: Any,
    state_e: Any,
    *,
    steps: int = 2,
    presets: Sequence[int] | None = None,
    jog: bool = False,
    te_jog: bool = False,
    jump: bool = False,
    jump_step: Any = None,
    te_presets: Sequence[int | None] | None = None,
    pausable: bool = False,
    above: Callable[[Pair], None] | None = None,
) -> Pair:
    """Allocate a pair, call *above* with it, then emit its two drums.

    *jump* with *jump_step* (a tag or int) gives both drums a jump.  *jog* gives the time
    drum a jog pin, and *te_jog* gives the other drum one.  Both share a bit.  *pausable*
    puts both rungs under ``~pause``.  *above* emits the rungs that drive the drums,
    above them.  The caller adds :func:`track_both` and :func:`snap` below.
    """
    pair = alloc_pair(
        take,
        step_t,
        step_e,
        acc_t,
        acc_e,
        state_t,
        state_e,
        steps=steps,
        jog=jog,
        te_jog=te_jog,
        jump=jump,
        pausable=pausable,
    )
    if above is not None:
        above(pair)
    emit_pair(
        slot,
        pair,
        presets=presets,
        te_presets=te_presets,
        jog=jog,
        te_jog=te_jog,
        jump_step=jump_step,
    )
    return pair


def on_scans(slot: Any, bit: Any, *scans: int) -> None:
    """Turn *bit* on with an Out on exactly the scans *scans* (a rung above the drums)."""
    with Rung(_any_scan(slot, scans)):
        out(bit)


def _any_scan(slot: Any, scans: Sequence[int]) -> Any:
    if len(scans) == 1:
        return slot.Scratch == scans[0]
    return Or(*[slot.Scratch == scan for scan in scans])


def put(slot: Any, scan: int, value: Any, into: Any) -> None:
    """Copy *value* into *into* on scan *scan* only (a rung above the drums)."""
    with Rung(slot.Scratch == scan):
        copy(value, into)


def track(slot: Any, side: Side) -> None:
    """Set ``side.state`` to the drum's state (``SX``) every scan.  The rungs go below the drum.

    The rungs are not gated on the case running or on the window.  A compare contact costs
    3 steps, and only :func:`snap` reads the state, on a scan of the window.
    """
    with Rung():
        math(side.step * 10, side.state)
    with Rung(side.flag):
        math(side.state + 4, side.state)
    with Rung(side.b):
        math(side.state + 2, side.state)
    with Rung(side.a):
        math(side.state + 1, side.state)


def snap(slot: Any, pair: Pair, scan: int, into: Any) -> None:
    """Set *into* to the time drum's state above the time & event drum's, as of scan *scan*."""
    with Rung(slot.Scratch == scan):
        math(pair.time.state * 100 + pair.event.state, into)


def track_both(slot: Any, pair: Pair) -> None:
    track(slot, pair.time)
    track(slot, pair.event)
