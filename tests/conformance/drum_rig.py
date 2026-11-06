"""A time drum and a time & event drum side by side, for the S6 drum probes.

``drum_trace`` reads one event drum scan by scan.  The S6 probes ask the same
kind of question of ``time_drum`` and ``time_event_drum``, so a scenario here
runs one of each on the same inputs (:class:`Pair`) and records them together.

A drum's state at the end of a scan is two digits, ``S`` then ``X``: the step,
and ``X = 4 * flag + 2 * B + A``, so 22 is step 2 with B on, 24 is step 2
complete (flag on, outputs off) and 10 is step 1 with both outputs off.  A
snapshot packs the time drum's state above the time & event drum's: 2222 is
both on step 2 with B on, 2224 the time drum on step 2 with B on and the
other complete.  :func:`snap` copies one into a slot field on a named scan.

The step registers are DS (CLICK's editor takes no other for a drum), the
accumulators TD, the completion flags and outputs C bits.  A time drum
moves by a jog, or by the Acc a case stages past a preset, since over the
wire a preset cannot be timed in scans; its presets are otherwise 30 s, so no
scan's time pushes a trace over.  A time & event drum's steps are event-only
(levels), so its events move it where the time drum's time or jog does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from pyrung.core import Or, Rung, copy, math, out, time_drum, time_event_drum
from tests.conformance.memory import c

#: A time drum step's preset (ms): longer than any trace.
LONG = 30000


class Pool:
    """C bits handed out in order from a case's own run of the group's range."""

    def __init__(self, numbers: Sequence[int]) -> None:
        self._free: Iterator[int] = iter(numbers)

    def __call__(self, n: int = 1) -> list[Any]:
        return [c[next(self._free)] for _ in range(n)]


def pool_of(*runs: range) -> Callable[[int, int], Pool]:
    """``pool(index, size)``: the *index*-th run of *size* bits across the group's ranges."""
    numbers = [n for run in runs for n in run]

    def pool(index: int, size: int) -> Pool:
        part = numbers[index * size : (index + 1) * size]
        if len(part) < size:
            raise ValueError(f"case {index}: the group's C bits run out ({len(numbers)})")
        return Pool(part)

    return pool


@dataclass(frozen=True)
class Resources:
    """A group's raw memory, cut into a run per case: C bits, DS step registers,
    TD accumulators and DD states (``memory.OWNED``)."""

    bits: Callable[[int, int], Pool]
    #: DS step registers, ``raw_counts[i]`` of them to case *i* in order.
    raw: Sequence[int]
    raw_counts: Sequence[int]
    tds: Sequence[int]
    states: Sequence[int]
    bits_per_case: int = 26

    def rig(self, slot: Any, index: int) -> Rig:
        return Rig(slot, self, index)


class Rig:
    """The tags of case *index*: up to two scenarios of a pair of drums each."""

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
        """Scenario *which* (0 or 1): scenario 0's time drum steps in Result1, unless
        *own_steps* names the two step registers (a case that needs Result1 for itself)."""
        return make_pair(self.slot, self.take, *self._tags(which, own_steps), **options)

    def alloc(self, which: int = 0, **options: Any) -> Pair:
        """The tags of scenario *which* with no rung emitted (see :func:`emit_pair`)."""
        return alloc_pair(self.take, *self._tags(which, None), **options)


@dataclass(frozen=True)
class Side:
    """One drum of a pair: its tags."""

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
    #: The time drum's jog and the events of the time & event drum's steps.
    jog: Any
    events: tuple[Any, ...]
    jump: Any
    #: The drum rung's condition besides ``Cmd != 0``; ``None`` for always.
    pause: Any


def pattern_for(steps: int) -> list[list[int]]:
    """Step 1 A, step 2 B, step 3 both, then A again."""
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
    """The pair's tags and bits, and no rung yet."""
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
    """The two drum rungs, ``~pause`` and ``Cmd != 0`` their condition."""
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

    *jump* with *jump_step* (a tag or int) gives both drums a jump; *jog* gives
    the time drum a jog pin and *te_jog* the other (a bit both share).
    *pausable* puts both rungs under ``~pause``.  *above* emits the rungs that
    drive the drums, above them.  The caller adds :func:`track_both` and
    :func:`snap` below.
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
    """*bit* is on, by an Out, on exactly the scans *scans* (a rung above the drums)."""
    with Rung(_any_scan(slot, scans)):
        out(bit)


def _any_scan(slot: Any, scans: Sequence[int]) -> Any:
    if len(scans) == 1:
        return slot.Scratch == scans[0]
    return Or(*[slot.Scratch == scan for scan in scans])


def put(slot: Any, scan: int, value: Any, into: Any) -> None:
    """``into = value`` on scan *scan* only (a rung above the drums)."""
    with Rung(slot.Scratch == scan):
        copy(value, into)


def track(slot: Any, side: Side) -> None:
    """Every scan, ``side.state`` = the drum's state (``SX``).  Below the drum.

    Not gated on the case running or on the window: a compare contact costs 3 steps
    and the state is only read by :func:`snap` on a scan of the window.
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
    """*into* = the time drum's state above the time & event drum's, as of scan *scan*."""
    with Rung(slot.Scratch == scan):
        math(pair.time.state * 100 + pair.event.state, into)


def track_both(slot: Any, pair: Pair) -> None:
    track(slot, pair.time)
    track(slot, pair.event)
