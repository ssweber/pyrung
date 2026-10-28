"""Counters: the ``counters`` group (subroutine ``Conf_Counters``).

``count_up`` (with and without ``.down()``) and ``count_down``.  The cases own
C1301..C1400 and counters CT1..CT100 (``memory.OWNED``), ten of each per case.

Each case counts its own scans (``scans.py``): the counting rung keys on the
scan number, observation rungs record what the counter did over scans 1..8, and
``Fired`` goes up once the record is final.  Every counting rung is false after
scan 8, so nothing moves while the harness reads.  A preset that changes lives
in the slot's ``Result4``, a DS register, as CLICK allows (DS or DD).

The reset is the counter's own: every counter's reset input is ``Cmd == 0``, so
it holds ``Acc = 0`` and ``Done`` off while the case is idle.  ``ClickCounter``
results are DINT; the slot copies them into DS, and the values are small, except
in ``acc_clamps_at_dint_range``, which compares against the limits in a contact.

The expectations are pyrung's answers, unconfirmed on a CLICK.  What each case
tells apart, and what the other model would read:

- ``level_vs_edge``: pyrung counts every scan the rung is true.  A counter that
  counts OFF->ON transitions of its input, as many PLCs' count-up instructions
  do, reads ``Result1 = 2`` instead of 5; both read ``Result2 = 2`` for the
  ``rise()`` counter.
- ``survives_rung_false``: a counter that auto-resets when its rung goes false,
  as TON does, reads 0, 0, 2 instead of 3, 1, 5.
- ``preset_raised_resumes``: ``Done`` is ``Acc >= preset`` with the preset read
  each scan.  A sticky ``Done`` (on until reset) or a preset latched when the
  count started reads ``Result1 = 6, Result2 = 5``; a counter that stops at its
  preset and resumes when it is raised reads 4, 7, 6.
- ``preset_lowered_while_idle``: ``Done`` is recomputed on every scan the
  instruction runs, counting or not.  A counter that only re-evaluates ``Done``
  when it counts never turns it on (``Result1 = 0``).
- ``reset_wins_over_count``: a reset input on while the rung is true clears the
  counter and counts nothing that scan.  A reset scan that also counts reads
  ``Result1 = 1, Result2 = 5, Result3 = 4``.
- ``reset_zero_preset``: reset writes ``Acc = 0, Done = False`` and returns, so
  with a preset of 0 ``Done`` is off on the reset scan though ``0 >= 0``, and
  back on the next scan, with the rung false.  Reset that recomputes ``Done``
  reads ``Result1 = 8, Result2 = 0, Result3 = 0``; ``Done`` re-evaluated only on
  counting scans reads 3, 4, 8.
- ``count_down_negative``: ``count_down`` starts at 0, goes negative, and is
  done at ``Acc <= -preset``.  A down counter that a reset loads with its preset
  and that is done at 0 reads ``Result1 = -2, Result3 = 2``; one that starts at
  0 and is done at ``Acc <= 0`` reads ``Result2 = 1``.
- ``up_down_same_scan``: with both inputs on, the net change is 0.  Up taking
  priority reads 6, 4, 8; down taking priority reads 0, -2, 3.
- ``acc_clamps_at_dint_range``: ``Acc`` is staged a few counts inside each
  limit and counts into it.  A wrapping accumulator is at the limit for one
  scan only (``Result1 = 1, Result3 = 1``).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, count_down, count_up, out, rise
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


def prove_survives_rung_false(slot):
    # Counts on scans 2..4 and 7..8, preset 3; the rung is false on scans 5..6.
    counter = ClickCounter(11)
    count_scans(slot)
    with Rung(
        slot.Cmd != 0,
        slot.Scratch >= 2,
        slot.Scratch <= WINDOW,
        slot.Scratch != 5,
        slot.Scratch != 6,
    ):
        count_up(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 6, counter.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot)


def prove_preset_raised_resumes(slot):
    # Counts on scans 1..8; the preset (Result4) is 3, raised to 6 on scan 5.
    counter = ClickCounter(21)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        copy(6, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(
        slot.Cmd != 0,
        slot.Scratch >= 5,
        slot.Scratch <= WINDOW,
        counter.Done,
        slot.Result2 == 0,
    ):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result3)
    done(slot)


def prove_preset_lowered_while_idle(slot):
    # Counts on scans 1..3 only; the preset (Result4) is 10, lowered to 2 on scan 6.
    counter = ClickCounter(31)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(10, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        copy(2, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch <= 3):
        count_up(counter, slot.Result4).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done, slot.Result1 == 0):
        copy(slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    done(slot)


def prove_reset_wins_over_count(slot):
    # Counts on scans 1..8, preset 3; the reset input is on on scan 4 too.
    counter = ClickCounter(41)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        count_up(counter, 3).reset(Or(slot.Cmd == 0, slot.Scratch == 4))
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
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
    # Counts down on scans 2..6, preset 3.
    counter = ClickCounter(61)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= 6):
        count_down(counter, 3).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done, slot.Result2 == 0):
        copy(slot.Scratch, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        copy(counter.Acc, slot.Result3)
    done(slot)


def prove_up_down_same_scan(slot):
    # Up on scans 1..6, down on scans 4..8, preset 3.
    counter = ClickCounter(71)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= 6):
        count_up(counter, 3).down(slot.Scratch >= 4, slot.Scratch <= WINDOW).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, counter.Done):
        copy(slot.Scratch, slot.Result3)
    done(slot)


def prove_acc_clamps_at_dint_range(slot):
    # Both accumulators are staged on scan 1, before their counters run;
    # both count on scans 1..8.
    up, down = ClickCounter(81), ClickCounter(82)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(DINT_MAX - 2, up.Acc)
        copy(DINT_MIN + 3, down.Acc)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        count_up(up, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
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


group = Group(
    "counters",
    [
        case(
            "A counter counts every scan its rung is true, not every edge: a bit on "
            "for scans 2..3 and 5..7 counts 5 (Result1), and through rise() counts "
            "its 2 rising edges (Result2)",
            ladder=prove_level_vs_edge,
            expect={"Result1": 5, "Result2": 2},
            scans=SCANS,
        ),
        case(
            "A counter keeps its count and Done when its rung goes false: preset 3, "
            "counting on scans 2..4, it still reads 3 (Result1) and Done (Result2 = 1) "
            "on scan 6 with the rung false, and counting on scans 7..8 ends at 5 "
            "(Result3)",
            ladder=prove_survives_rung_false,
            expect={"Result1": 3, "Result2": 1, "Result3": 5},
            scans=SCANS,
        ),
        case(
            "Raising a counter's preset mid-count turns Done off until the count "
            "reaches it: counting on scans 1..8 with preset 3 raised to 6 (Result4) "
            "on scan 5, Done is on for 5 scans (Result1), 3..4 and from scan 6 "
            "(Result2), and the count goes on to 8 (Result3)",
            ladder=prove_preset_raised_resumes,
            expect={"Result1": 5, "Result2": 6, "Result3": 8, "Result4": 6},
            scans=SCANS,
        ),
        case(
            "A counter reads its preset every scan, counting or not: counted to 3 "
            "(Result2) on scans 1..3 against preset 10, lowered to 2 (Result4) on "
            "scan 6 with the rung false, Done comes on on scan 6 (Result1)",
            ladder=prove_preset_lowered_while_idle,
            expect={"Result1": 6, "Result2": 3, "Result4": 2},
            scans=SCANS,
        ),
        case(
            "A counter's reset input beats its count input: counting on scans 1..8 "
            "with preset 3 and reset on scan 4, it reads 0 after scan 4 (Result1), "
            "ends at 4 (Result2), and is done for 3 scans (Result3), 3 and 7..8",
            ladder=prove_reset_wins_over_count,
            expect={"Result1": 0, "Result2": 4, "Result3": 3},
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
            "count_down starts at 0 and counts negative, and is done at -preset: "
            "preset 3, counting on scans 2..6, it reads -1 after scan 2 (Result3), "
            "is done from scan 4 (Result2), and ends at -5 (Result1)",
            ladder=prove_count_down_negative,
            expect={"Result1": -5, "Result2": 4, "Result3": -1},
            scans=SCANS,
        ),
        case(
            "An up/down counter with both inputs on does not move: preset 3, up on "
            "scans 1..6 and down on scans 4..8, it holds 3 through scan 6 (Result1), "
            "ends at 1 (Result2), and Done is last on on scan 6 (Result3)",
            ladder=prove_up_down_same_scan,
            expect={"Result1": 3, "Result2": 1, "Result3": 6},
            scans=SCANS,
        ),
        case(
            "A counter's Acc clamps at the DINT range: staged at 2147483645 and "
            "counting up on scans 1..8, it is at 2147483647 for 7 scans (Result1) "
            "from scan 2 (Result2); staged at -2147483645 and counting down, it is at "
            "-2147483648 for 6 scans (Result3) from scan 3 (Result4)",
            ladder=prove_acc_clamps_at_dint_range,
            expect={"Result1": 7, "Result2": 2, "Result3": 6, "Result4": 3},
            scans=SCANS,
        ),
    ],
)


def test_counters():
    assert_all_passed(run(group))
