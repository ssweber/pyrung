"""A drum step's DS preset, and a bad jump on the rung's return: ``drum_ds`` and ``jump_return``.

Part ``follow2_drums``.

No reading has shown a DS preset ending a drum step.  ``drum_limits/preset_from_ds`` and
``drum_preset/entry_or_first_timing_scan`` both zeroed the preset DS on every reset scan and set
its value only from scan 1, and every one of their DS steps "did not end".  Two models fit all
five readings: (a) the preset is read while the drum is held in reset, so it read 0, and a DS 0
never ends; (b) a DS preset, as pasted, never ends.  pyrung reads the preset when a step starts
timing, which fails ``entry_or_first_timing_scan``'s Result2 (3 against CLICK's 2).  Here each DS
holds the value the case means from the reset on, as the init writes it.

``drum_ds/ds_preset_ends``: four time drums of three steps, each read on scan 20 (its step: 1 for
step 1 still timing, 2 when step 1 has ended), so that a 1 ms step has ended on any scan.  Every
step but the one asked about is a constant 30 s.

- Result1 (D1): step 1's preset a DS holding 1 ms from the reset on, never written again: 2 where
  a DS preset ends a step (pyrung), 1 where it never does (b).
- Result2 (D2): step 1's preset a DS holding 1 ms through the reset and 30 s from scan 1 (written
  on a rung above the drum): 2 where the preset is read while the drum is held in reset (a), 1
  where it is read when the step starts timing (pyrung) or never ends (b).
- Result3 (D3): step 1 a constant 5 ms, step 2 a DS holding 0, step 3 30 s.  Read as the step:
  2 where a DS of 0 never ends a step entered by a move (pyrung), 3 where it ends at once, as a
  constant 0 does (``drum_preset/literal_zero`` read 2).  1 where the constant 5 ms did not end.
- Result4, the control: step 1 a constant 1 ms, 2.

pyrung reads 2, 1, 2, 2.

``jump_return/bad_jump_on_return``: does a jump to a bad step on the scan the drum's rung comes
back keep the drum from writing its row?  ``bad_jump`` read on a CLICK that a jump to a bad step
takes its scan (a bad step 5 is corrected the scan after, 5252 then 1212), and the drum rule
writes a row on a move or on the rung's return.  Two pairs of a time drum and a time & event drum
(3 steps, 30 s, events never on), snapshots as in ``test_drum_pins`` (the step, then ``4 * flag +
2 * B + A``, the time drum above the other): jogged to step 2 on scan 2 (row 22), paused on scans
4 and 5, their outputs reset by hand on scan 4 (20), back on scan 6.  Pair Q has a jump to step 9
of 3 on scan 6; pair P none.

- Result1, P on scan 6: 2222 where the return writes its row, 2020 where it does not.
- Result2, Q on scan 5, paused: 2020 (a paused drum leaves its outputs).
- Result3, Q on scan 6: 2222 where the return writes its row anyway, 2020 where the bad jump takes
  the scan and nothing is written.
- Result4, Q on scan 7: 2020 where the row waits for a move, 2222 where it comes a scan late.
- ErrorCode, P on scan 7: as Result1, the row held.

pyrung reads 2222, 2020, 2222, 2222, 2222.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, reset
from tests.conformance.drum_rig import LONG, Pool, Side, on_scans, put, track
from tests.conformance.drum_shift_search.test_drum_pins import snap, te_box, time_box
from tests.conformance.memory import c, dd, ds, td
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

# --- drum_ds -------------------------------------------------------------------------

DS_WINDOW = 22
READ_SCAN = 20
#: ds_preset_ends: C611..C622, step registers DS4011..4014, presets DS4015..4017, TD261..264.
DS_BITS = range(611, 623)
DS_STEPS = range(4011, 4015)
DS_PRESETS = range(4015, 4018)
DS_TDS = range(261, 265)


def prove_ds_preset_ends(slot: Any) -> None:
    take = Pool(DS_BITS)
    sides = []
    for step, acc in zip(DS_STEPS, DS_TDS, strict=True):
        a, b, flag = take(3)
        sides.append(Side(ds[step], td[acc], a, b, flag, None))
    held, at_reset, by_move, control = sides
    p_held, p_reset, p_zero = (ds[n] for n in DS_PRESETS)
    count_scans(slot, DS_WINDOW)
    put(slot, 1, LONG, p_reset)
    time_box(slot, held, [p_held, LONG, LONG])
    time_box(slot, at_reset, [p_reset, LONG, LONG])
    time_box(slot, by_move, [5, p_zero, LONG])
    time_box(slot, control, [1, LONG, LONG])
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    with Rung(slot.Scratch == READ_SCAN):
        for side, field in zip(sides, fields, strict=True):
            copy(side.step, field)
    done(slot, DS_WINDOW)


def init_ds_preset_ends(slot: Any) -> None:
    # The drums' own reset pins (``Cmd == 0``) put their steps, TDs and bits back; the presets
    # hold what the case means from the reset on.
    p_held, p_reset, p_zero = (ds[n] for n in DS_PRESETS)
    copy(1, p_held)
    copy(1, p_reset)
    copy(0, p_zero)


drum_ds = Group(
    "drum_ds",
    [
        case(
            "A time drum ends a step on a DS preset of 1 ms held from the reset (Result1 = 2), "
            "not on one of 1 ms through the reset and 30 s from scan 1 (Result2 = 1); a DS of 0 "
            "entered by a move never ends (Result3 = 2, 3 it ends); a constant 1 ms ends "
            "(Result4 = 2)",
            ladder=prove_ds_preset_ends,
            init=init_ds_preset_ends,
            expect={
                "Result1": between(1, 2),
                "Result2": between(1, 2),
                "Result3": between(1, 3),
                "Result4": 2,
            },
            scans=scans_for(DS_WINDOW),
            probe="does a DS preset end a drum step, and when is it read (6)",
        ),
    ],
)


# --- jump_return ---------------------------------------------------------------------

#: bad_jump_on_return: C631..C651, step registers DS4021..4024, the target DS4025, TD265..268,
#: states DD121..124.
RET_BITS = range(631, 652)
RET_STEPS = range(4021, 4025)
RET_TARGET = 4025
RET_TDS = range(265, 269)
RET_STATES = range(121, 125)


def prove_bad_jump_on_return(slot: Any) -> None:
    take = Pool(RET_BITS)
    target = ds[RET_TARGET]

    def side(i: int) -> Side:
        a, b, flag = take(3)
        return Side(ds[RET_STEPS[i]], td[RET_TDS[i]], a, b, flag, dd[RET_STATES[i]])

    tm_p, te_p, tm_q, te_q = (side(i) for i in range(4))
    p1, p2, p3, q1, q2, q3 = take(6)
    jog, pause, jump = take(3)
    count_scans(slot)
    put(slot, 1, 9, target)
    on_scans(slot, jog, 2)
    on_scans(slot, pause, 4, 5)
    on_scans(slot, jump, 6)
    with Rung(slot.Scratch == 4):
        for s in (tm_p, te_p, tm_q, te_q):
            reset(s.a)
            reset(s.b)
    time_box(slot, tm_p, [LONG, LONG, LONG], pause=pause, jog=jog)
    te_box(slot, te_p, [p1, p2, p3], pause=pause, jog=jog)
    time_box(slot, tm_q, [LONG, LONG, LONG], pause=pause, jog=jog, jump=jump, step=target)
    te_box(slot, te_q, [q1, q2, q3], pause=pause, jog=jog, jump=jump, step=target)
    for s in (tm_p, te_p, tm_q, te_q):
        track(slot, s)
    snap(slot, tm_p, te_p, 6, slot.Result1)
    snap(slot, tm_q, te_q, 5, slot.Result2)
    snap(slot, tm_q, te_q, 6, slot.Result3)
    snap(slot, tm_q, te_q, 7, slot.Result4)
    snap(slot, tm_p, te_p, 7, slot.ErrorCode)
    done(slot)


def init_bad_jump_on_return(slot: Any) -> None:
    reset(c.select(RET_BITS.start, RET_BITS.stop - 1))
    fill(0, ds.select(RET_STEPS.start, RET_TARGET))
    for n in RET_TDS:
        copy(0, td[n])
    fill(0, dd.select(RET_STATES.start, RET_STATES.stop - 1))


jump_return = Group(
    "jump_return",
    [
        case(
            "A drum paused with its outputs reset by hand writes its row as its rung comes back "
            "(Result1 = 2222, ErrorCode = 2222), with a jump to step 9 of 3 on that scan too "
            "(Result3 = 2222, 2020 if the jump takes the scan; Result4 the scan after); paused, "
            "it leaves them off (Result2 = 2020)",
            ladder=prove_bad_jump_on_return,
            init=init_bad_jump_on_return,
            expect={
                "Result1": between(2020, 2222),
                "Result2": between(2020, 2222),
                "Result3": between(1111, 2222),
                "Result4": between(1111, 2222),
                "ErrorCode": between(2020, 2222),
            },
            scans=scans_for(),
            probe="a bad jump on the rung's return: is the return row written (6b)",
        ),
    ],
)


def test_drum_ds():
    assert_all_passed(run(drum_ds))


def test_jump_return():
    assert_all_passed(run(jump_return))
