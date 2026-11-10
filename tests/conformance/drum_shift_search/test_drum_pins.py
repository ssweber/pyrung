"""Drum probes: a pin against a step's own end, and every other branch of the routine.

Three groups: ``drum_pins`` (part ``drum2``), ``drum_branch`` (``drum3``), ``drum_limits`` (``drum1``).  The cases
ask what no recording in ``test_drum.py``, ``test_drum_trace.py``, ``test_drum_rule.py``,
``test_drum_jump.py``, ``test_drum_settle.py``, ``test_drum_close.py`` or
``test_drum_idle.py`` separates.  ``drum_pins`` and ``drum_limits`` were read on a CLICK
on 2026-11-05, and ``drum_branch`` on 2026-11-05 and 2026-11-09.  Each case expects what
the CLICK read.

Snapshots as in ``test_drum_settle``: two digits a drum, ``S`` then ``4 * flag + 2 * B +
A``, the first drum named above the other.  On a 3-step drum row 1 is 11, row 2 is 22 and
row 3 is 33; complete with the outputs off is 14, 24 or 34.

``drum_pins``, Q1: a pin and the step's own end on one scan.  M1: the pin's move takes
the scan.  M2: the drum's own logic runs first, then the pins.  The CLICK read M1 in every
case, and 9 for ``acc_jog_completing``'s control.

=======================  ===========  ===========  ============================
Case                     M1           M2           Read
=======================  ===========  ===========  ============================
``event_and_jog``        2222         3333         Result2, Result3
``time_and_jog``         2222         3333         Result2, Result3
``end_and_jog_on_last``  2222         2424         Result2, Result3
``last_time_and_jump``   1111         1414         Result2, Result3
``last_event_and_jump``  1111         1414         Result2, Result3
``acc_jog_completing``   0, 0         above 0      Result2, Result4; Result3 the scan
=======================  ===========  ===========  ============================

- ``event_and_jog``: an event drum over a time & event drum whose steps are event-only,
  both on step 1 of 3, step 1's event and a jog on scan 3.  If the jog moves and the
  end is dropped it is step 2; if both move, step 3.
- ``time_and_jog``: the same with Acc staged past step 1's preset on the jog's scan, a time
  drum over a timed time & event drum.
- ``end_and_jog_on_last``: an event drum over a time drum, 2 steps, so M2's jog lands on the
  last step and completes (2424) where M1 reads 2222.
- ``last_time_and_jump``: on step 2 of 2, Acc past the preset, the event on and a jump to
  step 1 on scan 4.  M1 lands with step 1's row (1111).  M2 completes, then the jump moves
  a completed drum and leaves the outputs alone (1414).  Result4 reads the time drum's Acc.
- ``last_event_and_jump``: the same with step 2's event alone, on an event drum over an
  event-only time & event drum.
- ``acc_jog_completing``: Acc staged at 900 on the scan of a jog that completes the drum,
  the rung true; Acc - 900 on that scan is 0 if the jog takes the scan, a scan's time if
  own logic ticks first.  Result2 for a time drum, Result4 for a timed time & event drum.
  Result3 is the control, a time drum with no jog: what a scan's time shows.  Above 0, the
  readings above tell; 0, on a scan under a ms, they do not.

A jump that is not on the last step does not separate them: step 1's end and a jump to 3
read 33 under both, a jump to 1 reads 11 under both.

Q2: a paused forward jog with the flag on.  A jog forward with the flag on writes the
row when the rung is true (``drum_settle/after_jump_jog_alone``).  With the rung false the
CLICK read "never":

======================  ============  ============  ==================
Row written             at the jog    at the return  never
======================  ============  ============  ==================
Reading at the jog      2626          2424           2424
Reading at the return   2626          2626           2424
======================  ============  ============  ==================

- ``paused_jog_with_flag``: a time drum over a time & event drum, 2 steps: completed by a jog
  and jumped to step 1, 1414 (Result1, a control), paused on scans 7..8, the jog on scan 8
  (Result2), the return on scan 9 (Result3) and the scan after (Result4).
- ``paused_jog_with_flag_mid``: an event drum over a time drum, 3 steps, so the jog lands on
  a step that is not the last: the same readings.
- ``paused_flag_moves``: a jump with the flag on while paused: 1414 at the jump and 1414 at
  the return, or the return writes the row, 1515 (Result2, Result3).  A jog on the last
  step with the flag on while paused: 2424 both times, or the return writes the row, 2626
  (Result4, ErrorCode).  The CLICK read 1414 and 2424: the return writes no row.

``drum_branch``, the routine's other branches:

- ``jump_to_bad_step``: a jump to a step the drum does not have, from a DS: to 9 (Result1),
  to 0 (Result2), and to 4 with a jog on its scan (Result3).  Dropped reads 1111, 1111 and,
  the jog acting, 2222.  Standing reads 9191; clamping to the last step 3333.  ErrorCode is
  the time drum's Acc hundreds after the jump to 9: 9 if a dropped jump keeps the time,
  0 if it clears it.  The CLICK read (2026-11-05) 1111 for all three and ErrorCode 9: the jumps
  are dropped, even with a jog on the scan, and the time is kept.
- ``jump_on_bad_step``: step 5 staged on the scan of a jump to 2.  The jump lands with its
  row, 2222; the correction wins, 1111 (Result2, next scan Result1).  With a jog as well
  (Result3, Result4): the jump wins, 2222; the jog freezes the scan as it does alone, 5151.
  ErrorCode: step 5 on the scan of a jump to 9 from step 2, the correction writes nothing,
  1212.  The CLICK read (2026-11-05) 2222 in all four, so the jump wins with a jog as well, and
  5252 for ErrorCode: step 5 stands.
- ``correction_with_flag``: a completed drum with step 5 staged: corrects with the flag on,
  1414, or the flag stops it, 5454 (Result2, Result3).  Staged while paused, read paused,
  5454 (Result4), and on the return, 1414 (ErrorCode).  The CLICK read 1414, 1414, 5454 and
  1414, so the correction runs with the flag on.
- ``flag_cleared``: a jumped completed drum's flag reset from outside writes no row, 1010, or
  does, 1111 (Result2); its own end on the next scan moves it, 2222 (Result3).  A completed
  drum's flag reset while paused: the return writes the row, 2222, or leaves it, 2020
  (Result4).  The CLICK read 1010, 2222 and 2222.  Clearing the flag writes no row, and the
  return writes it.
- ``reset_paths``: a reset while paused on a completed drum, 1010 at the reset (Result2),
  and the return writes the row, 1111 (Result3).  A jump that rises on a reset's scan and
  holds is spent (Result4 on the next scan, ErrorCode on the one after), 1111, or lands
  when the reset ends, 3333.  The jog's twin is recorded (``drum/reset_swallows_held_jog``).
  The CLICK read 1010, 1111, 1111 and 1111, so the held jump is spent.
- ``written_and_paused_acc``: a step written with a jog on one scan moves from what was
  written, 3333, or from what the drum held, 2222 (Result2).  A paused drum's Acc holds, 9
  hundreds, or clears, 0 (Result3); a paused jog (Result4) and a paused jump (ErrorCode)
  clear it, 0, or leave it, 9.  The CLICK read 3333, 9, 0 and 0.  The jog moves from what was
  written.  A pause keeps Acc, and a paused jog or jump clears it.
- ``event_rises_in_pause``: step 1's event rises during a pause and holds through the return.
  An event drum that kept tracking its event never moves (1 in the top digit); one whose
  memory stood still moves after the return (2).  The time & event drum below takes the
  level on the scan after the return (2) as ``te_level_from_scan_1`` says.  Result2 on the
  return, Result3 and Result4 after it.  The CLICK read 1111, 1122 and 1122, so the event drum
  keeps tracking its event while paused.
- ``te_event_before_time``: on a timed time & event step, an event pulse before the time is up
  is lost, and with the time up later and no event the drum stays, 11; one latched moves,
  22 (top drum, Result2).  The drum below has the time up first and takes a later pulse, 22
  (Result3).  Recorded: a level event, and a held event (``te_level_event_already_on``).
  Result4: Acc 32760 on a time & event step waiting for its event, 5 scans on: 32767 (1,
  stops), negative (2, wraps), or 0 for neither.  The CLICK read 1111, 1122 and 1.  The early
  pulse is lost, and Acc stops at 32767.
- ``drum_in_loop``: a time drum in a For of 3 beside the same drum outside it, both from Acc
  0 for 150 ms by a free TON.  Result2 = 10 * inside / outside: 30 if the body's time adds up
  per pass, 10 if a scan's time is once.  Result3: a jog bit up once, in the loop: 2 (one
  move; the edge is spent on the first pass) or 4 (every pass).  An event drum with an
  event up on one scan (Result4) likewise.  The CLICK read 30, 2 and 2.  Each pass adds the
  scan's time, and an edge acts on the first pass only.  Result2 keeps the range 20..60,
  since it moves with the scan time.  On the first scan, pyrung's drum in the loop adds the
  scan's time on its second and third passes, and the drum outside adds none.  So a 10 ms scan
  reads 31, and the soft PLC's 30-40 ms scans read more.

``drum_limits``, last in the part since a paste may refuse one:

- ``preset_from_ds``: a preset in a DS lowered below Acc on a step: read live, the step ends
  that scan, 2222, read at entry it does not, 1111 (Result2, a time drum over a time & event
  drum).  Preset DS 0 (Result3) and -5 (Result4): the time drum's step ends on the next
  scan (2), or never (1).  The CLICK read 1111, 1 and 1: read at entry, and 0 or below never
  ends the step.  An Out coil's DS preset reads live (``out_more/preset_mid_phase``).
- ``units``: a time drum in seconds beside a free TON: Result2 is Acc after 2100 ms, 2 in
  seconds, 2100 if the unit does not reach the drum.  Result3 is the same in minutes, 0, or
  2100 likewise.  Result4 is the seconds drum on scan 3, 0 unless it starts at 1.  The
  CLICK read 2, 0 and 0.
- ``single_step``: 1-step drums.  A time drum's end (Result2 on scan 3) and an event drum's
  (Result3 on scan 5) complete them, 1411 then 1414; a jog completes an event-only time &
  event drum, 14 (Result4), or does nothing, 11.  The editor took a 1-step drum, and the
  CLICK read 1411, 1414 and 14.

Not asked: 16 steps and 16 outputs are the editor's limits, static; a drum twice called in
one scan is ``call_more/called_twice``.

The cases own the raw memory listed in ``memory.OWNED`` under these groups.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Or,
    Rung,
    copy,
    event_drum,
    forloop,
    math,
    on_delay,
    reset,
    time_drum,
    time_event_drum,
)
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, pattern_for, put, track
from tests.conformance.memory import ClickTimer, dd, ds, td
from tests.conformance.scans import count_scans, done, scans_for

_BITS = [
    n
    for run in (
        range(662, 681),
        range(682, 701),
        range(882, 901),
        range(904, 911),
        range(914, 921),
        range(924, 941),
        range(1030, 1041),
        range(1050, 1061),
        range(1070, 1081),
        range(1090, 1101),
        range(1277, 1301),
        range(1302, 1331),
        range(1472, 1481),
        range(1482, 1491),
        range(1492, 1501),
        range(1502, 1511),
        range(1512, 1521),
        range(1522, 1531),
        range(1532, 1541),
        range(1543, 1551),
        range(1552, 1561),
        range(1562, 1571),
        range(1573, 1581),
        range(1583, 1591),
        range(1593, 1601),
    )
    for n in run
]
#: The C bits of each chunk of raw memory (:func:`rig`), the sides' 3 each included.
_CHUNK_BITS = (
    10, 10, 9, 10, 10, 8, 5, 11, 9, 3, 11, 8, 11, 10, 8, 8, 9, 7,
    10, 8, 12, 9, 10, 9, 9, 9, 5, 9, 6, 8, 6, 6, 8, 4,
)  # fmt: skip
_STEPS = [*range(4231, 4243), *range(4248, 4263), *range(4267, 4283), *range(4403, 4411)]
_STEPS += [*range(4413, 4421), *range(4424, 4431), *range(4316, 4322)]
_TDS = [*range(182, 201), *range(313, 351), *range(212, 225)]
_STATES = [*range(860, 901), *range(133, 161)]
#: Each case's jump step and preset registers, in the order the cases take them.
_EXTRA = [ds[n] for n in (*range(4293, 4303), *range(3907, 3911))]


def restoring(*registers: Any) -> Any:
    """A case's ``init``: *registers* back to 0."""

    def init(slot: Any) -> None:
        for register in registers:
            copy(0, register)

    return init


@dataclass(frozen=True)
class Rig:
    """A chunk of raw memory: two drums (:meth:`side`) and what is left of 12 C bits."""

    index: int
    take: Pool

    def side(self, which: int) -> Side:
        a, b, flag = self.take(3)
        i = 2 * self.index + which
        return Side(ds[_STEPS[i]], td[_TDS[i]], a, b, flag, dd[_STATES[i]])


def rig(index: int) -> Rig:
    first = sum(_CHUNK_BITS[:index])
    return Rig(index, Pool(_BITS[first : first + _CHUNK_BITS[index]]))


def _gate(slot: Any, pause: Any) -> list[Any]:
    return [slot.Cmd != 0] if pause is None else [slot.Cmd != 0, ~pause]


def _pins(drum: Any, slot: Any, rst: Any, jog: Any, jump: Any, step: Any) -> None:
    drum = drum.reset(slot.Cmd == 0 if rst is None else Or(slot.Cmd == 0, rst))
    if jump is not None:
        drum = drum.jump(jump, step=step)
    if jog is not None:
        drum.jog(jog)


def time_box(
    slot: Any,
    s: Side,
    presets: Sequence[Any],
    *,
    unit: str = "ms",
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    step: Any = None,
    rst: Any = None,
) -> None:
    with Rung(*_gate(slot, pause)):
        drum = time_drum(
            outputs=[s.a, s.b],
            presets=list(presets),
            unit=unit,
            pattern=pattern_for(len(presets)),
            current_step=s.step,
            accumulator=s.acc,
            completion_flag=s.flag,
        )
        _pins(drum, slot, rst, jog, jump, step)


def te_box(
    slot: Any,
    s: Side,
    events: Sequence[Any],
    presets: Sequence[Any] | None = None,
    *,
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    step: Any = None,
    rst: Any = None,
) -> None:
    with Rung(*_gate(slot, pause)):
        drum = time_event_drum(
            outputs=[s.a, s.b],
            presets=list(presets) if presets is not None else [None] * len(events),
            events=list(events),
            unit="ms",
            pattern=pattern_for(len(events)),
            current_step=s.step,
            accumulator=s.acc,
            completion_flag=s.flag,
        )
        _pins(drum, slot, rst, jog, jump, step)


def event_box(
    slot: Any,
    s: Side,
    events: Sequence[Any],
    *,
    pause: Any = None,
    jog: Any = None,
    jump: Any = None,
    step: Any = None,
    rst: Any = None,
) -> None:
    with Rung(*_gate(slot, pause)):
        drum = event_drum(
            outputs=[s.a, s.b],
            events=list(events),
            pattern=pattern_for(len(events)),
            current_step=s.step,
            completion_flag=s.flag,
        )
        _pins(drum, slot, rst, jog, jump, step)


def snap(slot: Any, top: Side, under: Side, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        math(top.state * 100 + under.state, into)


def hundreds(slot: Any, s: Side, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        math(s.acc / 100, into)


def over(slot: Any, s: Side, base: int, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        math(s.acc - base, into)


#: A case's window past the default, for the drums that first run through their steps.
WIDE = 13


# ------------------------------------------------------------------- Q1


def prove_event_and_jog(slot):
    r = rig(0)
    ev, te = r.side(0), r.side(1)
    e1, e2, e3, jog = r.take(4)
    count_scans(slot)
    on_scans(slot, e1, 3)
    on_scans(slot, jog, 3)
    event_box(slot, ev, [e1, e2, e3], jog=jog)
    te_box(slot, te, [e1, e2, e3], jog=jog)
    track(slot, ev)
    track(slot, te)
    snap(slot, ev, te, 2, slot.Result1)
    snap(slot, ev, te, 3, slot.Result2)
    snap(slot, ev, te, 4, slot.Result3)
    done(slot)


def prove_time_and_jog(slot):
    r = rig(1)
    tm, te = r.side(0), r.side(1)
    e1, e2, e3, jog = r.take(4)
    count_scans(slot)
    on_scans(slot, e1, 3)
    on_scans(slot, jog, 3)
    put(slot, 3, LONG + 100, tm.acc)
    put(slot, 3, LONG + 100, te.acc)
    time_box(slot, tm, [LONG, LONG, LONG], jog=jog)
    te_box(slot, te, [e1, e2, e3], [LONG, LONG, LONG], jog=jog)
    track(slot, tm)
    track(slot, te)
    snap(slot, tm, te, 2, slot.Result1)
    snap(slot, tm, te, 3, slot.Result2)
    snap(slot, tm, te, 4, slot.Result3)
    done(slot)


def prove_end_and_jog_on_last(slot):
    r = rig(2)
    ev, tm = r.side(0), r.side(1)
    e1, e2, jog = r.take(3)
    count_scans(slot)
    on_scans(slot, e1, 3)
    on_scans(slot, jog, 3)
    put(slot, 3, LONG + 100, tm.acc)
    event_box(slot, ev, [e1, e2], jog=jog)
    time_box(slot, tm, [LONG, LONG], jog=jog)
    track(slot, ev)
    track(slot, tm)
    snap(slot, ev, tm, 2, slot.Result1)
    snap(slot, ev, tm, 3, slot.Result2)
    snap(slot, ev, tm, 4, slot.Result3)
    done(slot)


def prove_last_time_and_jump(slot):
    r = rig(3)
    tm, te = r.side(0), r.side(1)
    e1, e2, jog, jump = r.take(4)
    count_scans(slot)
    on_scans(slot, jog, 2)
    on_scans(slot, e2, 4)
    on_scans(slot, jump, 4)
    put(slot, 1, 1, _EXTRA[0])
    put(slot, 4, LONG + 100, tm.acc)
    put(slot, 4, LONG + 100, te.acc)
    time_box(slot, tm, [LONG, LONG], jog=jog, jump=jump, step=_EXTRA[0])
    te_box(slot, te, [e1, e2], [LONG, LONG], jog=jog, jump=jump, step=_EXTRA[0])
    track(slot, tm)
    track(slot, te)
    snap(slot, tm, te, 3, slot.Result1)
    snap(slot, tm, te, 4, slot.Result2)
    snap(slot, tm, te, 5, slot.Result3)
    hundreds(slot, tm, 4, slot.Result4)
    done(slot)


def prove_last_event_and_jump(slot):
    r = rig(4)
    ev, te = r.side(0), r.side(1)
    e1, e2, jog, jump = r.take(4)
    count_scans(slot)
    on_scans(slot, jog, 2)
    on_scans(slot, e2, 4)
    on_scans(slot, jump, 4)
    put(slot, 1, 1, _EXTRA[1])
    event_box(slot, ev, [e1, e2], jog=jog, jump=jump, step=_EXTRA[1])
    te_box(slot, te, [e1, e2], jog=jog, jump=jump, step=_EXTRA[1])
    track(slot, ev)
    track(slot, te)
    snap(slot, ev, te, 3, slot.Result1)
    snap(slot, ev, te, 4, slot.Result2)
    snap(slot, ev, te, 5, slot.Result3)
    done(slot)


def prove_acc_jog_completing(slot):
    r, q = rig(5), rig(6)
    tm, ctl = r.side(0), r.side(1)
    te = q.side(0)
    e1, e2 = q.take(2)
    jog, ctl_jog = r.take(2)
    count_scans(slot)
    on_scans(slot, jog, 2, 4)
    on_scans(slot, ctl_jog, 2)
    for side in (tm, ctl, te):
        put(slot, 4, 900, side.acc)
    time_box(slot, tm, [LONG, LONG], jog=jog)
    time_box(slot, ctl, [LONG, LONG], jog=ctl_jog)
    te_box(slot, te, [e1, e2], [LONG, LONG], jog=jog)
    track(slot, tm)
    track(slot, ctl)
    over(slot, tm, 900, 4, slot.Result2)
    over(slot, ctl, 900, 4, slot.Result3)
    over(slot, te, 900, 4, slot.Result4)
    snap(slot, tm, ctl, 4, slot.ErrorCode)
    done(slot)


# ------------------------------------------------------------------- Q2


def prove_paused_jog_with_flag(slot):
    r = rig(7)
    tm, te = r.side(0), r.side(1)
    e1, e2, jog, jump, pause = r.take(5)
    count_scans(slot, WIDE)
    on_scans(slot, jog, 2, 4, 8)
    on_scans(slot, jump, 5)
    on_scans(slot, pause, 7, 8)
    put(slot, 1, 1, _EXTRA[2])
    time_box(slot, tm, [LONG, LONG], pause=pause, jog=jog, jump=jump, step=_EXTRA[2])
    te_box(slot, te, [e1, e2], pause=pause, jog=jog, jump=jump, step=_EXTRA[2])
    track(slot, tm)
    track(slot, te)
    snap(slot, tm, te, 6, slot.Result1)
    snap(slot, tm, te, 8, slot.Result2)
    snap(slot, tm, te, 9, slot.Result3)
    snap(slot, tm, te, 10, slot.Result4)
    done(slot, WIDE)


def prove_paused_jog_with_flag_mid(slot):
    r = rig(8)
    ev, tm = r.side(0), r.side(1)
    e1, e2, e3 = r.take(3)
    q = rig(9)
    jog, jump, pause = q.take(3)
    count_scans(slot, WIDE)
    on_scans(slot, jog, 2, 4, 6, 10)
    on_scans(slot, jump, 7)
    on_scans(slot, pause, 9, 10)
    put(slot, 1, 1, _EXTRA[3])
    event_box(slot, ev, [e1, e2, e3], pause=pause, jog=jog, jump=jump, step=_EXTRA[3])
    time_box(slot, tm, [LONG, LONG, LONG], pause=pause, jog=jog, jump=jump, step=_EXTRA[3])
    track(slot, ev)
    track(slot, tm)
    snap(slot, ev, tm, 8, slot.Result1)
    snap(slot, ev, tm, 10, slot.Result2)
    snap(slot, ev, tm, 11, slot.Result3)
    snap(slot, ev, tm, 12, slot.Result4)
    done(slot, WIDE)


def prove_paused_flag_moves(slot):
    r, q = rig(10), rig(11)
    tm, te = r.side(0), r.side(1)
    tm2, te2 = q.side(0), q.side(1)
    e1, e2, jog, jump, pause = r.take(5)
    jog2, pause2 = q.take(2)
    count_scans(slot, WIDE)
    on_scans(slot, jog, 2, 4)
    on_scans(slot, jump, 7)
    on_scans(slot, pause, 6, 7, 8)
    on_scans(slot, jog2, 2, 4, 7)
    on_scans(slot, pause2, 6, 7, 8)
    put(slot, 1, 1, _EXTRA[4])
    time_box(slot, tm, [LONG, LONG], pause=pause, jog=jog, jump=jump, step=_EXTRA[4])
    te_box(slot, te, [e1, e2], pause=pause, jog=jog, jump=jump, step=_EXTRA[4])
    time_box(slot, tm2, [LONG, LONG], pause=pause2, jog=jog2)
    te_box(slot, te2, [e1, e2], pause=pause2, jog=jog2)
    for side in (tm, te, tm2, te2):
        track(slot, side)
    snap(slot, tm, te, 7, slot.Result2)
    snap(slot, tm, te, 9, slot.Result3)
    snap(slot, tm2, te2, 7, slot.Result4)
    snap(slot, tm2, te2, 9, slot.ErrorCode)
    done(slot, WIDE)


# --------------------------------------------------------------- branches


def prove_jump_to_bad_step(slot):
    r = rig(12)
    tm, te = r.side(0), r.side(1)
    e1, e2, e3, jog, jump = r.take(5)
    count_scans(slot)
    on_scans(slot, jump, 3, 5, 7)
    on_scans(slot, jog, 7)
    put(slot, 1, 9, _EXTRA[5])
    put(slot, 2, 900, tm.acc)
    put(slot, 4, 0, _EXTRA[5])
    put(slot, 6, 4, _EXTRA[5])
    time_box(slot, tm, [LONG, LONG, LONG], jog=jog, jump=jump, step=_EXTRA[5])
    te_box(slot, te, [e1, e2, e3], jog=jog, jump=jump, step=_EXTRA[5])
    track(slot, tm)
    track(slot, te)
    snap(slot, tm, te, 3, slot.Result1)
    snap(slot, tm, te, 5, slot.Result2)
    snap(slot, tm, te, 7, slot.Result3)
    hundreds(slot, tm, 3, slot.ErrorCode)
    done(slot)


def prove_jump_on_bad_step(slot):
    r, q, p = rig(13), rig(14), rig(15)
    a, b = r.side(0), r.side(1)
    d, e = q.side(0), q.side(1)
    g, h = p.side(0), p.side(1)
    e1, e2, e3, jump = r.take(4)
    jog, jump2 = q.take(2)
    jump3, jog3 = p.take(2)
    to_two, to_nine = _EXTRA[9], _EXTRA[10]
    count_scans(slot)
    put(slot, 1, 2, to_two)
    put(slot, 1, 9, to_nine)
    on_scans(slot, jump, 3)
    on_scans(slot, jump2, 3)
    on_scans(slot, jog, 3)
    on_scans(slot, jog3, 2)
    on_scans(slot, jump3, 4)
    for side in (a, b, d, e):
        put(slot, 3, 5, side.step)
    for side in (g, h):
        put(slot, 4, 5, side.step)
    time_box(slot, a, [LONG, LONG, LONG], jump=jump, step=to_two)
    te_box(slot, b, [e1, e2, e3], jump=jump, step=to_two)
    time_box(slot, d, [LONG, LONG, LONG], jog=jog, jump=jump2, step=to_two)
    te_box(slot, e, [e1, e2, e3], jog=jog, jump=jump2, step=to_two)
    time_box(slot, g, [LONG, LONG, LONG], jog=jog3, jump=jump3, step=to_nine)
    te_box(slot, h, [e1, e2, e3], jog=jog3, jump=jump3, step=to_nine)
    for side in (a, b, d, e, g, h):
        track(slot, side)
    snap(slot, a, b, 3, slot.Result2)
    snap(slot, a, b, 4, slot.Result1)
    snap(slot, d, e, 3, slot.Result3)
    snap(slot, d, e, 4, slot.Result4)
    snap(slot, g, h, 4, slot.ErrorCode)
    done(slot)


def prove_correction_with_flag(slot):
    r, q = rig(16), rig(17)
    a, b = r.side(0), r.side(1)
    d, e = q.side(0), q.side(1)
    e1, e2, jog = r.take(3)
    pause = q.take(1)[0]
    count_scans(slot)
    on_scans(slot, jog, 2, 4)
    on_scans(slot, pause, 6, 7)
    put(slot, 5, 5, a.step)
    put(slot, 5, 5, b.step)
    put(slot, 6, 5, d.step)
    put(slot, 6, 5, e.step)
    time_box(slot, a, [LONG, LONG], jog=jog)
    te_box(slot, b, [e1, e2], jog=jog)
    time_box(slot, d, [LONG, LONG], pause=pause, jog=jog)
    te_box(slot, e, [e1, e2], pause=pause, jog=jog)
    for side in (a, b, d, e):
        track(slot, side)
    snap(slot, a, b, 5, slot.Result2)
    snap(slot, a, b, 6, slot.Result3)
    snap(slot, d, e, 7, slot.Result4)
    snap(slot, d, e, 8, slot.ErrorCode)
    done(slot)


def prove_flag_cleared(slot):
    r, q = rig(18), rig(19)
    a, b = r.side(0), r.side(1)
    d, e = q.side(0), q.side(1)
    e1, e2, jog, jump = r.take(4)
    jog2, pause = q.take(2)
    count_scans(slot)
    on_scans(slot, jog, 2, 4)
    on_scans(slot, jump, 5)
    on_scans(slot, jog2, 2, 4)
    on_scans(slot, pause, 5, 6, 7)
    on_scans(slot, e1, 7)
    put(slot, 1, 1, _EXTRA[6])
    put(slot, 7, LONG + 100, a.acc)
    put(slot, 7, LONG + 100, b.acc)
    for side in (a, b):
        with Rung(slot.Scratch == 6):
            reset(side.flag)
    for side in (d, e):
        with Rung(slot.Scratch == 6):
            reset(side.flag)
    time_box(slot, a, [LONG, LONG], jog=jog, jump=jump, step=_EXTRA[6])
    te_box(slot, b, [e1, e2], [LONG, LONG], jog=jog, jump=jump, step=_EXTRA[6])
    time_box(slot, d, [LONG, LONG], pause=pause, jog=jog2)
    te_box(slot, e, [e1, e2], pause=pause, jog=jog2)
    for side in (a, b, d, e):
        track(slot, side)
    snap(slot, a, b, 6, slot.Result2)
    snap(slot, a, b, 7, slot.Result3)
    snap(slot, d, e, 8, slot.Result4)
    done(slot)


def prove_reset_paths(slot):
    r, q = rig(20), rig(21)
    a, b = r.side(0), r.side(1)
    d, e = q.side(0), q.side(1)
    e1, e2, e3, jog, pause, rst = r.take(6)
    jog2, jump, rst2 = q.take(3)
    count_scans(slot)
    on_scans(slot, jog, 2, 4)
    on_scans(slot, pause, 5, 6, 7)
    on_scans(slot, rst, 6)
    on_scans(slot, jog2, 2)
    on_scans(slot, jump, 5, 6, 7)
    on_scans(slot, rst2, 5)
    put(slot, 1, 3, _EXTRA[7])
    time_box(slot, a, [LONG, LONG], pause=pause, jog=jog, rst=rst)
    te_box(slot, b, [e1, e2], pause=pause, jog=jog, rst=rst)
    time_box(slot, d, [LONG, LONG, LONG], jog=jog2, jump=jump, step=_EXTRA[7], rst=rst2)
    te_box(slot, e, [e1, e2, e3], jog=jog2, jump=jump, step=_EXTRA[7], rst=rst2)
    for side in (a, b, d, e):
        track(slot, side)
    snap(slot, a, b, 6, slot.Result2)
    snap(slot, a, b, 8, slot.Result3)
    snap(slot, d, e, 6, slot.Result4)
    snap(slot, d, e, 7, slot.ErrorCode)
    done(slot)


def prove_written_and_paused_acc(slot):
    r, q = rig(22), rig(23)
    a, b = r.side(0), r.side(1)
    d, e = q.side(0), q.side(1)
    e1, e2, e3, jog = r.take(4)
    jog2, jump, pause = q.take(3)
    count_scans(slot)
    on_scans(slot, jog, 3)
    put(slot, 3, 2, a.step)
    put(slot, 3, 2, b.step)
    put(slot, 1, 2, _EXTRA[8])
    put(slot, 3, 900, d.acc)
    put(slot, 3, 900, e.acc)
    on_scans(slot, pause, 4, 5, 6, 7)
    on_scans(slot, jog2, 6)
    on_scans(slot, jump, 6)
    time_box(slot, a, [LONG, LONG, LONG], jog=jog)
    te_box(slot, b, [e1, e2, e3], jog=jog)
    time_box(slot, d, [LONG, LONG, LONG], pause=pause, jog=jog2)
    time_box(slot, e, [LONG, LONG, LONG], pause=pause, jump=jump, step=_EXTRA[8])
    for side in (a, b):
        track(slot, side)
    snap(slot, a, b, 3, slot.Result2)
    hundreds(slot, d, 5, slot.Result3)
    hundreds(slot, d, 6, slot.Result4)
    hundreds(slot, e, 6, slot.ErrorCode)
    done(slot)


def prove_event_rises_in_pause(slot):
    r = rig(24)
    ev, te = r.side(0), r.side(1)
    e1, e2, pause = r.take(3)
    count_scans(slot)
    on_scans(slot, pause, 3, 4, 5)
    on_scans(slot, e1, 4, 5, 6, 7, 8)
    event_box(slot, ev, [e1, e2], pause=pause)
    te_box(slot, te, [e1, e2], pause=pause)
    track(slot, ev)
    track(slot, te)
    snap(slot, ev, te, 6, slot.Result2)
    snap(slot, ev, te, 7, slot.Result3)
    snap(slot, ev, te, 8, slot.Result4)
    done(slot)


def prove_te_event_before_time(slot):
    r = rig(25)
    top, under = r.side(0), r.side(1)
    e1a, e1b, e2 = r.take(3)
    count_scans(slot)
    on_scans(slot, e1a, 3)
    on_scans(slot, e1b, 6)
    put(slot, 5, LONG + 100, top.acc)
    put(slot, 3, LONG + 100, under.acc)
    te_box(slot, top, [e1a, e2], [LONG, LONG])
    te_box(slot, under, [e1b, e2], [LONG, LONG])
    track(slot, top)
    track(slot, under)
    snap(slot, top, under, 5, slot.Result2)
    snap(slot, top, under, 6, slot.Result3)
    # A step waiting for its event goes on timing: from 32760, to the top of an Int.
    q = rig(26)
    te = q.side(0)
    e3, e4 = q.take(2)
    put(slot, 3, 32760, te.acc)
    te_box(slot, te, [e3, e4], [LONG, LONG])
    with Rung(slot.Scratch == 8, te.acc == 32767):
        copy(1, slot.Result4)
    with Rung(slot.Scratch == 8, te.acc < 0):
        copy(2, slot.Result4)
    done(slot)


def prove_preset_from_ds(slot):
    r, q = rig(29), rig(30)
    tm, te = r.side(0), r.side(1)
    zero, negative = q.side(0), q.side(1)
    e1, e2 = r.take(2)
    p1, p2, p3 = _EXTRA[11], _EXTRA[12], _EXTRA[13]
    count_scans(slot)
    put(slot, 1, LONG, p1)
    put(slot, 1, -5, p3)
    put(slot, 3, 500, tm.acc)
    put(slot, 3, 500, te.acc)
    put(slot, 5, 100, p1)
    on_scans(slot, e1, 5)
    time_box(slot, tm, [p1, LONG])
    te_box(slot, te, [e1, e2], [p1, LONG])
    time_box(slot, zero, [p2, LONG])
    time_box(slot, negative, [p3, LONG])
    for side in (tm, te, zero, negative):
        track(slot, side)
    snap(slot, tm, te, 5, slot.Result2)
    with Rung(slot.Scratch == 4):
        copy(zero.step, slot.Result3)
        copy(negative.step, slot.Result4)
    done(slot)


def prove_units(slot):
    r = rig(31)
    secs, mins = r.side(0), r.side(1)
    clock = ClickTimer(_TDS[-1])
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    count_scans(slot, 3)
    with Rung(slot.Scratch == 3):
        copy(secs.acc, slot.Result4)
    time_box(slot, secs, [LONG, LONG], unit="s")
    time_box(slot, mins, [LONG, LONG], unit="min")
    with Rung(slot.Cmd != 0, slot.Fired == 0, td[_TDS[-1]] >= 2100):
        copy(secs.acc, slot.Result2)
        copy(mins.acc, slot.Result3)
        copy(1, slot.Fired)


def prove_drum_in_loop(slot):
    r, q = rig(27), rig(28)
    inside, outside = r.side(0), r.side(1)
    jogged, evd = q.side(0), q.side(1)
    jog, e1, e2 = r.take(3)
    clock = ClickTimer(_TDS[-2])
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    count_scans(slot, 3)
    on_scans(slot, jog, 3)
    on_scans(slot, e1, 3)
    time_box(slot, outside, [LONG, LONG])
    # The For's own rung is always true, so its drums take the reset when Cmd drops.
    with Rung():
        with forloop(3):
            time_box(slot, inside, [LONG, LONG])
            time_box(slot, jogged, [LONG, LONG, LONG, LONG], jog=jog)
            event_box(slot, evd, [e1, e2, e2, e2])
    with Rung(slot.Cmd != 0, slot.Fired == 0, td[_TDS[-2]] >= 150):
        math(inside.acc * 10 / outside.acc, slot.Result2)
        copy(jogged.step, slot.Result3)
        copy(evd.step, slot.Result4)
        copy(1, slot.Fired)


def prove_single_step(slot):
    r = rig(32)
    tm, ev = r.side(0), r.side(1)
    q = rig(33)
    te = q.side(0)
    e1, jog = r.take(2)
    e2 = q.take(1)[0]
    count_scans(slot)
    put(slot, 3, LONG + 100, tm.acc)
    on_scans(slot, e1, 5)
    on_scans(slot, jog, 4)
    time_box(slot, tm, [LONG])
    event_box(slot, ev, [e1])
    te_box(slot, te, [e2], jog=jog)
    track(slot, tm)
    track(slot, ev)
    track(slot, te)
    snap(slot, tm, ev, 3, slot.Result2)
    snap(slot, tm, ev, 5, slot.Result3)
    with Rung(slot.Scratch == 4):
        copy(te.state, slot.Result4)
    done(slot)


SCANS = scans_for()
WIDE_SCANS = scans_for(WIDE)

group = Group(
    "drum_pins",
    [
        case(
            "Q1: step 1's event and a jog on one scan, an event drum over a time & event drum, "
            "3 steps, control 1111 (Result1): the jog takes the scan, 2222 (Result2, next scan "
            "Result3)",
            ladder=prove_event_and_jog,
            scans=SCANS,
            expect={"Result1": 1111, "Result2": 2222, "Result3": 2222},
        ),
        case(
            "Q1: Acc past step 1's preset and a jog on one scan, a time drum over a timed time "
            "& event drum with its event on, control 1111 (Result1): the jog takes the scan, "
            "2222 (Result2, Result3)",
            ladder=prove_time_and_jog,
            scans=SCANS,
            expect={"Result1": 1111, "Result2": 2222, "Result3": 2222},
        ),
        case(
            "Q1: on 2 steps own logic would land on the last and a jog there complete, 2424; "
            "the jog takes the scan, 2222 (Result2, Result3), control 1111 (Result1)",
            ladder=prove_end_and_jog_on_last,
            scans=SCANS,
            expect={"Result1": 1111, "Result2": 2222, "Result3": 2222},
        ),
        case(
            "Q1: on the last step, time up and a jump to step 1 on one scan, control 2222 "
            "(Result1): the jump takes the scan, 1111 (Result2, Result3), and clears Acc, 0 "
            "hundreds (Result4)",
            ladder=prove_last_time_and_jump,
            init=restoring(_EXTRA[0]),
            scans=SCANS,
            expect={"Result1": 2222, "Result2": 1111, "Result3": 1111, "Result4": 0},
        ),
        case(
            "Q1: on the last step, its event and a jump to step 1 on one scan, control 2222 "
            "(Result1): the jump takes the scan, 1111 (Result2, Result3)",
            ladder=prove_last_event_and_jump,
            init=restoring(_EXTRA[1]),
            scans=SCANS,
            expect={"Result1": 2222, "Result2": 1111, "Result3": 1111},
        ),
        case(
            "Q1: Acc 900 staged on the scan of a jog that completes the drum, the rung true: "
            "the jog takes the scan, Acc - 900 is 0 (Result2 time drum, Result4 time & event); "
            "a drum with no jog shows a scan's time, above 0 (Result3); 2422 (ErrorCode)",
            ladder=prove_acc_jog_completing,
            scans=SCANS,
            expect={
                "Result2": 0,
                "Result3": range(1, 901),
                "Result4": 0,
                "ErrorCode": 2422,
            },
        ),
        case(
            "Q2: a paused forward jog with the flag on, 2 steps, a time drum over a time & "
            "event drum jumped to step 1 with the flag on, control 1414 (Result1): the row is "
            "never written, 2424 at the jog (Result2), at the return (Result3), the scan after "
            "(Result4)",
            ladder=prove_paused_jog_with_flag,
            init=restoring(_EXTRA[2]),
            scans=WIDE_SCANS,
            expect={"Result1": 1414, "Result2": 2424, "Result3": 2424, "Result4": 2424},
        ),
        case(
            "Q2: the same to a step that is not the last, an event drum over a time drum on 3 "
            "steps, control 1414 (Result1): 2424 at the jog (Result2), at the return (Result3), "
            "the scan after (Result4)",
            ladder=prove_paused_jog_with_flag_mid,
            init=restoring(_EXTRA[3]),
            scans=WIDE_SCANS,
            expect={"Result1": 1414, "Result2": 2424, "Result3": 2424, "Result4": 2424},
        ),
        case(
            "A return writes no row a paused move left with the flag on: a jump, 1414 at the "
            "jump (Result2) and at the return (Result3); a jog on the last step, 2424 (Result4) "
            "and at the return (ErrorCode)",
            ladder=prove_paused_flag_moves,
            init=restoring(_EXTRA[4]),
            scans=WIDE_SCANS,
            expect={"Result2": 1414, "Result3": 1414, "Result4": 2424, "ErrorCode": 2424},
        ),
    ],
)


branch_group = Group(
    "drum_branch",
    [
        case(
            "A jump to a step the drum does not have from a DS is dropped: to 9 leaves 1111 "
            "(Result1), to 0 leaves 1111 (Result2), and to 4 with a jog on its scan leaves 1111 "
            "(Result3); the dropped jump keeps Acc, 9 hundreds (ErrorCode)",
            ladder=prove_jump_to_bad_step,
            init=restoring(_EXTRA[5]),
            scans=SCANS,
            expect={
                "Result1": 1111,
                "Result2": 1111,
                "Result3": 1111,
                "ErrorCode": 9,
            },
            probe="a jump past the drum's steps: dropped, standing or clamped",
        ),
        case(
            "A jump on a bad step: step 5 and a jump to 2 land with the row, 2222 (Result2, next "
            "scan Result1); with a jog as well the jump wins, 2222 (Result3, Result4); step 5 "
            "and a jump to 9 from step 2 leave step 5 standing, 5252 (ErrorCode)",
            ladder=prove_jump_on_bad_step,
            init=restoring(_EXTRA[9], _EXTRA[10]),
            scans=SCANS,
            expect={
                "Result1": 2222,
                "Result2": 2222,
                "Result3": 2222,
                "Result4": 2222,
                "ErrorCode": 5252,
            },
            probe="does a valid jump beat the correction and a jog's freeze",
        ),
        case(
            "A bad step in a completed drum corrects with the flag on, 1414 (Result2, Result3); "
            "staged while paused it stands, 5454 (Result4), and the return corrects it, 1414 "
            "(ErrorCode)",
            ladder=prove_correction_with_flag,
            scans=SCANS,
            expect={"Result2": 1414, "Result3": 1414, "Result4": 5454, "ErrorCode": 1414},
        ),
        case(
            "A flag cleared on a jumped drum writes no row, 1010 (Result2), then its own end "
            "moves it, 2222 (Result3); after a flag cleared while paused the return writes the "
            "row, 2222 (Result4)",
            ladder=prove_flag_cleared,
            init=restoring(_EXTRA[6]),
            scans=SCANS,
            expect={"Result2": 1010, "Result3": 2222, "Result4": 2222},
        ),
        case(
            "A reset while paused on a completed drum: 1010 (Result2), the return writes the row, "
            "1111 (Result3); a jump rising on a reset's scan and held is spent, 1111 (Result4, "
            "ErrorCode)",
            ladder=prove_reset_paths,
            init=restoring(_EXTRA[7]),
            scans=SCANS,
            expect={"Result2": 1010, "Result3": 1111, "Result4": 1111, "ErrorCode": 1111},
        ),
        case(
            "A step written with a jog on one scan moves from what was written, 3333 (Result2); "
            "Acc in a paused drum holds, 9 (Result3); a paused jog (Result4) and a paused jump "
            "(ErrorCode) clear it, 0",
            ladder=prove_written_and_paused_acc,
            init=restoring(_EXTRA[8]),
            scans=SCANS,
            expect={"Result2": 3333, "Result3": 9, "Result4": 0, "ErrorCode": 0},
        ),
        case(
            "Step 1's event rises during a pause and holds through the return: an event drum "
            "keeps tracking its event while paused and never moves, 1111 on the return "
            "(Result2), then 1122 (Result3, Result4) as the time & event drum below moves",
            ladder=prove_event_rises_in_pause,
            scans=SCANS,
            expect={"Result2": 1111, "Result3": 1122, "Result4": 1122},
        ),
        case(
            "A time & event step: an event pulse before the time is up is lost, and the time up "
            "later moves nothing, top digit 1 (Result2); time first, then a pulse, moves "
            "it (Result3); from Acc 32760 on a step waiting for its event, Acc stops at 32767, "
            "1 (Result4)",
            ladder=prove_te_event_before_time,
            scans=SCANS,
            expect={"Result2": 1111, "Result3": 1122, "Result4": 1},
        ),
        case(
            "A drum in a For of 3 beside the same drum outside: each pass adds a scan's time, so "
            "10 * inside Acc / outside Acc is about 30, not 10 (Result2); a jog bit up one scan "
            "moves it to step 2 only (Result3), an event likewise (Result4)",
            ladder=prove_drum_in_loop,
            scans=60,
            # Result2 moves with the scan time.  A CLICK reads 30, and pyrung's 10 ms scan 31.
            expect={"Result2": range(20, 61), "Result3": 2, "Result4": 2},
        ),
    ],
)


limits_group = Group(
    "drum_limits",
    [
        case(
            "A preset in a DS is read at step entry: lowered below Acc mid-step it does not "
            "end the step, 1111 (Result2); preset DS 0 (Result3) and -5 (Result4) never end "
            "step 1, 1 on scan 4",
            ladder=prove_preset_from_ds,
            init=restoring(_EXTRA[11], _EXTRA[12], _EXTRA[13]),
            scans=SCANS,
            expect={"Result2": 1111, "Result3": 1, "Result4": 1},
        ),
        case(
            "A time drum in seconds beside a free TON: Acc after 2100 ms is 2 in seconds "
            "(Result2); in minutes 0 (Result3); on scan 3 still 0 (Result4)",
            ladder=prove_units,
            scans=300,
            expect={"Result2": 2, "Result3": 0, "Result4": 0},
        ),
        case(
            "A 1-step drum: a time drum's end completes it, 1411 (Result2 on scan 3), an event "
            "drum's, 1414 (Result3 on scan 5), and a jog completes a time & event drum, 14 "
            "(Result4)",
            ladder=prove_single_step,
            scans=SCANS,
            expect={"Result2": 1411, "Result3": 1414, "Result4": 14},
        ),
    ],
)


def test_drum_pins():
    assert_all_passed(run(group))


def test_drum_branch():
    assert_all_passed(run(branch_group))


def test_drum_limits():
    assert_all_passed(run(limits_group))
