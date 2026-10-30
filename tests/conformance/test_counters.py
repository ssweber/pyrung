"""Counters: the ``counters`` group (subroutine ``Conf_Counters``).

``count_up`` (with and without ``.down()``) and ``count_down``.  The cases own
C1301..C1400 and counters CT1..CT150 (``memory.OWNED``), ten counters per case
(case N takes CT<10N-9>..); only ``level_vs_edge`` (C1301) and
``first_scan_input_already_on`` (C1391..) use C bits.

Each case counts its own scans (``scans.py``): the counting rung keys on the
scan number, observation rungs record what the counter did over the window, and
``Fired`` goes up once the record is final.  Every counting rung is false after
the window, so nothing moves while the harness reads.  A preset that changes
lives in the slot's ``Result4``, a DS register, as CLICK allows (DS or DD).

The reset is the counter's own: every counter's reset input is ``Cmd == 0``, so
it holds ``Acc = 0`` and ``Done`` off while the case is idle.  ``ClickCounter``
results are DINT; the slot copies them into DS, and the values are small, except
in ``acc_clamps_at_dint_range``, which compares against the limits in a contact.

The expectations are what a CLICK does (hardware round 1, 2026-10-29): a
counter counts the rising edges of its count inputs, with a memory of its own
updated on every scan, reset scans included, so an input held through a reset
does not count again; a down counter starts at 0 and is done at ``Acc <=
preset``, and pyrung does the same.  (pyrung used to count every scan an
input was on and had a down counter done at ``Acc <= -preset``; the cases that
showed it were pending S2.)  Most cases pulse their inputs for one scan at a
time, so each asks its own question under either counting rule.  What each
tells apart:

- ``level_vs_edge``: a bit on for scans 2..3 and 5..7 counts its 2 rising
  edges (Result1 = 2); counting every scan it is on reads 5.  The rise()
  counter reads 2 either way.
- ``survives_rung_false``: a counter that auto-resets when its rung goes false,
  as TON does, reads 0, 0, 1 instead of 3, 1, 4.
- ``preset_raised_resumes``: ``Done`` is ``Acc >= preset`` with the preset read
  each scan.  A sticky ``Done`` (on until reset) or a preset latched when the
  count started reads Result1 = 12, Result2 = 9; a counter that stops at its
  preset reads Result3 = 6.
- ``preset_lowered_while_idle``: ``Done`` is recomputed on every scan the
  instruction runs, counting or not.  A counter that only re-evaluates ``Done``
  when it counts never turns it on (Result1 = 0).
- ``reset_wins_over_count``: a reset on while a pulse arrives clears the
  counter and counts nothing that scan.  A reset scan that also counts reads
  Result1 = 1, Result2 = 3, Result3 = 4.
- ``reset_zero_preset``: reset writes ``Acc = 0, Done = False`` and returns, so
  with a preset of 0 ``Done`` is off on the reset scan though ``0 >= 0``, and
  back on the next scan, with the rung false.  Reset that recomputes ``Done``
  reads Result1 = 8, Result2 = 0, Result3 = 0; ``Done`` re-evaluated only on
  counting scans reads 3, 4, 8.
- ``count_down_negative``: pulses on scans 2, 4 and 6 take a down counter from
  0 to -3 (Result1), -1 after scan 2 (Result3), done from scan 1 (Result2), as
  0 <= 3.  pyrung's ``Acc <= -preset`` is done on scan 6; a down counter that
  a reset loads with its preset reads Result1 = 0, Result3 = 2.
- ``up_down_same_scan`` (answered as adopted): up pulses on 1, 3, 5, 7, down pulses on 5 and
  7, preset 2.  Both edges on one scan cancel (adopted): 2, 2, 6.  Up taking
  priority reads 3, 4, 6; down taking priority 1, 0, 2.
- ``acc_clamps_at_dint_range``: ``Acc`` is staged a few counts inside each
  limit and pulses into it.  A wrapping accumulator is at the limit for two
  scans only (Result1 = 2, Result3 = 2).
- ``held_input_not_recounted``: an input on from scan 1 counts once (Result1 =
  1 on scan 3); reset on scan 4 with the input still on, it does not count
  again (Result2 = 0).  Counting scans reads 3 and 4; an edge memory that a
  reset clears reads 1 and 1.
- ``count_down_done_at_preset`` (answered as adopted): staged at 5 and pulsed down on every
  even scan to -3, a down counter done at ``Acc <= preset`` (3) is first done
  on scan 4 (Result2, adopted); done at ``Acc <= 0`` on scan 10; at ``Acc <=
  -preset`` on scan 16.
- ``edge_on_reset_scan`` (answered as adopted): the input rises on scan 3, the reset's last
  scan, and stays on through 5; it rises again on 7.  Edge memory updated on
  reset scans (adopted): 0 after scan 5 (Result1), 1 at the end (Result2).
  Memory left alone while reset is on counts scan 4 too: 1, 2.
- ``first_scan_input_already_on`` (answered as adopted): an unconditional rung turns the
  count input on from the first scan, and the counter is held in reset on every
  scan but the first.  Edge memory that starts off at STOP->RUN counts on the
  first scan and latches a flag (Result1 = 1, adopted); memory seeded from the
  input, or carried over STOP, does not (Result1 = 0).  It reads the loaded
  program's first scan, whatever run reads it.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, count_down, count_up, latch, out, rise, system
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SCANS = scans_for()
DINT_MAX = 2147483647
DINT_MIN = -2147483648


def prove_level_vs_edge(slot):
    # C1301 is on for scans 2..3 and 5..7: five scans, two rising edges.
    level, edge = ClickCounter(1), ClickCounter(2)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= 7, slot.Scratch != 4):
        out(c[1301])
    with Rung(slot.Cmd != 0, c[1301]):
        count_up(level, 100).reset(slot.Cmd == 0)
    with Rung(rise(c[1301]), slot.Cmd != 0):
        count_up(edge, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(level.Acc, slot.Result1)
        copy(edge.Acc, slot.Result2)
    done(slot)


def on(slot, *scans):
    """True on exactly the given scans (a one-scan pulse on each)."""
    return Or(*(slot.Scratch == n for n in scans))


def prove_survives_rung_false(slot):
    # Pulses on scans 1, 3, 5 and 7, preset 3; nothing counts on scan 6.
    counter = ClickCounter(11)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 6, counter.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot)


#: ``preset_raised_resumes`` pulses on every odd scan of a longer window.
RAISED_WINDOW = 16


def prove_preset_raised_resumes(slot):
    # Pulses on scans 1, 3, ..., 15; the preset (Result4) is 3, raised to 6 on scan 9.
    w = RAISED_WINDOW
    counter = ClickCounter(21)
    count_scans(slot, w)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 9):
        copy(6, slot.Result4)
    with Rung(slot.Cmd != 0, on(slot, *range(1, w, 2))):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= w, counter.Done):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 9, slot.Scratch <= w, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot, w)


def prove_preset_lowered_while_idle(slot):
    # Pulses on scans 1, 3 and 5; the preset (Result4) is 10, lowered to 2 on scan 7.
    counter = ClickCounter(31)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(10, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 7):
        copy(2, slot.Result4)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5)):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)


def prove_reset_wins_over_count(slot):
    # Pulses on scans 1, 3, 5 and 7, preset 2; the reset input is on on scan 3 too.
    counter = ClickCounter(41)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 2).reset(Or(slot.Cmd == 0, slot.Scratch == 3))
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done):
        calc(slot.Result3 + 1, slot.Result3)
    done(slot)


def prove_reset_zero_preset(slot):
    # The preset is Result4, which stays 0.  The rung is true on scans 1..4,
    # the reset input on scan 4 too.
    counter = ClickCounter(51)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= 4):
        count_up(counter, slot.Result4).reset(Or(slot.Cmd == 0, slot.Scratch == 4))
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, ~counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, ~counter.Done):
        copy(slot.Scratch, slot.Result3)
    done(slot)


def prove_count_down_negative(slot):
    # Pulses down on scans 2, 4 and 6, preset 3.
    counter = ClickCounter(61)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 2, 4, 6)):
        count_down(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        copy(counter.Acc, slot.Result3)
    done(slot)


def prove_up_down_same_scan(slot):
    # Up pulses on scans 1, 3, 5 and 7, down pulses on 5 and 7, preset 2.
    counter = ClickCounter(71)
    count_scans(slot)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(counter, 2).down(on(slot, 5, 7)).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done):
        calc(slot.Result3 + 1, slot.Result3)
    done(slot)


def prove_acc_clamps_at_dint_range(slot):
    # Both accumulators are staged on scan 1, before their counters run;
    # both pulse on scans 1, 3, 5 and 7.
    up, down = ClickCounter(81), ClickCounter(82)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(DINT_MAX - 2, up.Acc)
        copy(DINT_MIN + 3, down.Acc)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_up(up, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, on(slot, 1, 3, 5, 7)):
        count_down(down, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, up.Acc == DINT_MAX):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, up.Acc == DINT_MAX, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, down.Acc == DINT_MIN):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, down.Acc == DINT_MIN, slot.Result4 == 0):
        copy(slot.Scratch, slot.Result4)
    done(slot)


def prove_held_input_not_recounted(slot):
    # The input is on for scans 1..8; the reset input is on on scan 4 too.
    counter = ClickCounter(91)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        count_up(counter, 100).reset(Or(slot.Cmd == 0, slot.Scratch == 4))
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)


#: ``count_down_done_at_preset`` pulses on every even scan of a longer window.
DOWN_WINDOW = 16


def prove_count_down_done_at_preset(slot):
    # Acc is staged at 5 on scan 1, before the counter runs; pulses down on
    # scans 2, 4, ..., 16 take it to 4, 3, ..., -3.  Preset 3.
    w = DOWN_WINDOW
    counter = ClickCounter(101)
    count_scans(slot, w)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(5, counter.Acc)
    with Rung(slot.Cmd != 0, on(slot, *range(2, w + 1, 2))):
        count_down(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= w, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        copy(counter.Acc, slot.Result3)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    done(slot, w)


def prove_edge_on_reset_scan(slot):
    # The input is on for scans 3..5 and 7..8; the reset input for scans 1..3.
    counter = ClickCounter(111)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= WINDOW, slot.Scratch != 6):
        count_up(counter, 100).reset(Or(slot.Cmd == 0, slot.Scratch <= 3))
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)


def prove_first_scan_input_already_on(slot):
    # Outside the Cmd gate: C1391 is on from the first scan, written above the
    # counter; the counter is in reset on every scan but the first; C1392
    # latches if it was done on the first scan.  The reset leaves both bits
    # alone, so only the first scan can latch C1392.
    counter = ClickCounter(121)
    first = system.sys.first_scan
    with Rung():
        out(c[1391])
    with Rung(c[1391]):
        count_up(counter, 1).reset(~first)
    with Rung(first, counter.Done):
        latch(c[1392])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1392]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, first):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


group = Group(
    "counters",
    [
        case(
            "A counter counts the rising edges of its rung, not the scans it is true: a "
            "bit on for scans 2..3 and 5..7 counts 2 (Result1), and through rise() "
            "counts the same 2 (Result2)",
            ladder=prove_level_vs_edge,
            expect={"Result1": 2, "Result2": 2},
            scans=SCANS,
        ),
        case(
            "A counter keeps its count and Done when its rung goes false: preset 3, "
            "pulsed on scans 1, 3 and 5, it still reads 3 (Result1) and Done (Result2 "
            "= 1) on scan 6 with the rung false, and a pulse on scan 7 takes it to 4 "
            "(Result3)",
            ladder=prove_survives_rung_false,
            expect={"Result1": 3, "Result2": 1, "Result3": 4},
            scans=SCANS,
        ),
        case(
            "Raising a counter's preset mid-count turns Done off until the count "
            "reaches it: pulsed on scans 1, 3, ..., 15 with preset 3 raised to 6 "
            "(Result4) on scan 9, Done is on for 10 scans (Result1), 5..8 and from "
            "scan 11 (Result2), and the count goes on to 8 (Result3)",
            ladder=prove_preset_raised_resumes,
            expect={"Result1": 10, "Result2": 11, "Result3": 8, "Result4": 6},
            scans=scans_for(RAISED_WINDOW),
        ),
        case(
            "A counter reads its preset every scan, counting or not: pulsed to 3 "
            "(Result2) on scans 1, 3 and 5 against preset 10, lowered to 2 (Result4) on "
            "scan 7 with no pulse, Done comes on on scan 7 (Result1)",
            ladder=prove_preset_lowered_while_idle,
            expect={"Result1": 7, "Result2": 3, "Result4": 2},
            scans=SCANS,
        ),
        case(
            "A counter's reset input beats its count input: pulsed on scans 1, 3, 5 "
            "and 7 with preset 2 and reset on scan 3, it reads 0 after scan 3 "
            "(Result1), ends at 2 (Result2), and is done for 2 scans (Result3), 7..8",
            ladder=prove_reset_wins_over_count,
            expect={"Result1": 0, "Result2": 2, "Result3": 2},
            scans=SCANS,
        ),
        case(
            "A counter's reset clears Done without comparing Acc to the preset: with "
            "a preset register holding 0, counting on scans 1..4 and reset on scan 4, "
            "Done is on for 7 scans (Result1) and off on scan 4 only (Result2 = "
            "Result3 = 4), back on with the rung false",
            ladder=prove_reset_zero_preset,
            expect={"Result1": 7, "Result2": 4, "Result3": 4},
            scans=SCANS,
        ),
        case(
            "count_down starts at 0, counts negative, and is done at Acc <= preset: "
            "preset 3, pulsed down on scans 2, 4 and 6, it reads -1 after scan 2 "
            "(Result3), is done from scan 1 (Result2), and ends at -3 (Result1)",
            ladder=prove_count_down_negative,
            expect={"Result1": -3, "Result2": 1, "Result3": -1},
            scans=SCANS,
        ),
        case(
            "An up/down counter whose up and down inputs rise on the same scan does not "
            "move: preset 2, up pulses on scans 1, 3, 5, 7 and down pulses on 5 and 7, it "
            "reads 2 after scan 5 (Result1), ends at 2 (Result2), and is done for 6 "
            "scans (Result3)",
            ladder=prove_up_down_same_scan,
            expect={"Result1": 2, "Result2": 2, "Result3": 6},
            scans=SCANS,
        ),
        case(
            "A counter's Acc clamps at the DINT range: staged at 2147483645 and pulsed "
            "up on scans 1, 3, 5, 7, it is at 2147483647 for 6 scans (Result1) from "
            "scan 3 (Result2); staged at -2147483645 and pulsed down, it is at "
            "-2147483648 for 4 scans (Result3) from scan 5 (Result4)",
            ladder=prove_acc_clamps_at_dint_range,
            expect={"Result1": 6, "Result2": 3, "Result3": 4, "Result4": 5},
            scans=SCANS,
        ),
        case(
            "A count input held on through a reset does not count again: on from scan "
            "1, it counts once (Result1 = 1 on scan 3), and reset on scan 4 the counter "
            "stays at 0 (Result2) with the input still on",
            ladder=prove_held_input_not_recounted,
            expect={"Result1": 1, "Result2": 0},
            scans=SCANS,
        ),
        case(
            "A down counter is done once Acc <= preset: staged at 5 with preset 3 and "
            "pulsed down on every even scan, it reads 4 after scan 2 (Result3), is "
            "first done on scan 4 at 3 (Result2), and ends at -3 (Result1)",
            ladder=prove_count_down_done_at_preset,
            expect={"Result1": -3, "Result2": 4, "Result3": 4},
            scans=scans_for(DOWN_WINDOW),
        ),
        case(
            "A count input that rises on the last scan of a reset is spent: reset on "
            "scans 1..3 and the input on for scans 3..5, the counter reads 0 after scan "
            "5 (Result1); the input's next rise, on scan 7, counts 1 (Result2)",
            ladder=prove_edge_on_reset_scan,
            expect={"Result1": 0, "Result2": 1},
            scans=SCANS,
        ),
        case(
            "A counter whose input is already on at the first scan after STOP->RUN "
            "counts it: held in reset on every other scan, it is done on the first "
            "and latches a flag the case reads (Result1 = 1); Result2 = 0 shows the "
            "case itself did not run on a first scan",
            ladder=prove_first_scan_input_already_on,
            # Done and Acc as the first scan left them differ from every later
            # scan's, so the state after the first reset never comes back.
            keep=[ClickCounter(121).Done, ClickCounter(121).Acc],
            expect={"Result1": 1, "Result2": 0},
        ),
    ],
)


def test_counters():
    assert_all_passed(run(group))
