"""A box on a continued wire: the ``cont_box`` group (subroutine ``Conf_ContBox``).

Can a box sit on a continued wire of the rung above, and do its enable and its
pins see that wire's writes?  Each case is
one CLICK rung of two wires from the rail: the first latches a bit P, the
second is a counter whose enable or pin reads P, its pins drawn from the rail
under it.  pyrung's exporter writes that drawing, and a CLICK takes it.  The
group is in part ``control``, near the end with the other paste probes.

The models, as in ``test_solve_order.py``:

- **L** (live, CLICK's model and pyrung's) reads a continued wire's contacts,
  and a box's enable and pins, when the solve reaches them, so they see the
  wire above.
- **S** (snapshot) reads the whole CLICK rung, continued wires and box pins
  included, against the memory the rung was entered with.

``SOLVE_ORDER`` holds what S reads in each case.  pyrung solves by L, so a new
case works out its S reading by hand.
``test_solve_order.test_annotation_matches_readings`` checks those readings
against each case's ``expect``, which pyrung reads.

- ``box_pin_on_continued_wire`` (+, probe): the counter counts on scan 2; its
  reset pin is ``Or(Cmd == 0, P)`` and the wire above latches P on scan 4 (the
  next rung resets it on the same scan).  L: the pin sees P and the counter
  reads 0 at the end (R1 = 0).  S, or pins read when the CLICK rung was
  entered: P is never seen, R1 = 1.  R2 = the count on scan 3, 1 in every
  model: the box on the continued wire counts at all.
- ``box_enable_on_continued_wire`` (+, probe): the counter's enable needs P,
  which the wire above latches on scan 2 only.  L: it counts once (R1 = 1).
  S, or the enable read when the CLICK rung was entered: it never counts (R1 =
  0).

- ``box_pin_on_own_rung`` (probe): the control of
  ``box_pin_on_continued_wire``.  The same counter, pin and latching rung, but
  the counter stands on a rung of its own (an ``R`` marker, its contacts from
  the rail) instead of below a wire.  A CLICK takes the box first on a
  continued wire and reads it as it reads the box on its own rung (R1 = 0,
  R2 = 1), since every row of a rung and every rung reads live.
- ``box_tee_without_trunk`` (probe): a box rung with no contact in
  front of its own.  The counter (preset 2) counts up on scans 1 and 3, so Acc
  is 2 on scan 4 (R2) with Done on (R3 = 1), and a down pin on scan 5 takes it
  to 1 (R1).  CLICK's syntax check redraws pins teed off a bare wire
  (``R,-,T`` over ``,,-``) as pins from the rail, so the export draws them
  from the rail (``bare=True``).

The cases own C451..C500 and counters CT181..CT190 (``memory.OWNED``), ten C
bits per case (case N takes C<441+10N>..).
"""

from __future__ import annotations

from collections.abc import Mapping

from pyrung.conformance import Group, TeeSpec, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, count_up, latch, out, reset
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import count_scans, done, scans_for

GROUP = "cont_box"
SCANS = scans_for()

#: Case id -> (does the answer depend on solve order?, what S reads).
#: ``test_solve_order`` folds these into ``ORDER_DEPENDENT``.
SOLVE_ORDER: Mapping[str, tuple[bool, Mapping[str, int]]] = {
    "box_pin_on_continued_wire": (True, {"Result1": 1, "Result2": 1}),
    "box_enable_on_continued_wire": (True, {"Result1": 0}),
    # Both models read the same in the last two cases.
    "box_pin_on_own_rung": (False, {"Result1": 0, "Result2": 1}),
    "box_tee_without_trunk": (False, {"Result1": 1, "Result2": 2, "Result3": 1}),
}


def prove_box_pin_on_continued_wire(slot):
    # P = C451, latched on scan 4 by the first wire and reset by the next rung.
    # Counter CT181 counts on scan 2.
    p, counter = c[451], ClickCounter(181)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        latch(p)
    with Rung(slot.Scratch == 2, slot.Cmd != 0).continued():
        count_up(counter, 100).reset(Or(slot.Cmd == 0, p))
    with Rung(slot.Cmd != 0):
        reset(p)
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result2)
    done(slot)


def prove_box_enable_on_continued_wire(slot):
    # P = C461, latched on scan 2 by the first wire and reset by the next rung.
    p, counter = c[461], ClickCounter(182)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        latch(p)
    with Rung(p, slot.Cmd != 0).continued():
        count_up(counter, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        reset(p)
        copy(counter.Acc, slot.Result1)
    done(slot)


def prove_box_pin_on_own_rung(slot):
    # The control.  P = C471 and counter CT183: the ladder of
    # box_pin_on_continued_wire, with the counter on a rung of its own.
    p, counter = c[471], ClickCounter(183)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        latch(p)
    with Rung(slot.Scratch == 2, slot.Cmd != 0):
        count_up(counter, 100).reset(Or(slot.Cmd == 0, p))
    with Rung(slot.Cmd != 0):
        reset(p)
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(counter.Acc, slot.Result2)
    done(slot)


def prove_box_tee_without_trunk(slot):
    # Counter CT184, preset 2: up pulses (C481) on scans 1 and 3, a down
    # pulse (C482) on scan 5.  The box rung has no contact before its own and
    # both pins have only their own, so CLICK draws the pins from the rail.
    counter = ClickCounter(184)
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 1, slot.Scratch == 3)):
        out(c[481])
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        out(c[482])
    with Rung(c[481]):
        count_up(counter, 2).down(c[482]).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(counter.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 4):
        copy(counter.Acc, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 4, counter.Done):
        copy(1, slot.Result3)
    done(slot)


group = Group(
    GROUP,
    [
        case(
            "A box on a continued wire reads its pins live: a counter that counted on "
            "scan 2 (Result2 = 1 on scan 3) is reset on scan 4 by a pin that reads the "
            "bit the wire above it latches that scan (Result1 = 0)",
            ladder=prove_box_pin_on_continued_wire,
            expect={"Result1": 0, "Result2": 1},
            scans=SCANS,
            probe="box on a continued wire: pins see the wire above",
        ),
        case(
            "A box on a continued wire reads its enable live: a counter whose enable "
            "needs the bit the wire above it latches on scan 2 counts it (Result1 = 1)",
            ladder=prove_box_enable_on_continued_wire,
            expect={"Result1": 1},
            scans=SCANS,
            probe="box on a continued wire: enable sees the wire above",
        ),
        case(
            "The box of the continued-wire case on a rung of its own reads the same: a "
            "counter that counted on scan 2 (Result2 = 1 on scan 3) is reset on scan 4 "
            "by a pin that reads the bit the rung above it latches that scan "
            "(Result1 = 0)",
            ladder=prove_box_pin_on_own_rung,
            expect={"Result1": 0, "Result2": 1},
            scans=SCANS,
            probe="the box on its own rung, the control for the continued wire",
        ),
        case(
            "A box rung with no contact in front of its own, its pins drawn from the "
            "rail, counts like any other: a counter of preset "
            "2 counts up on scans 1 and 3 (Result2 = 2 on scan 4, Done on, Result3 = 1) "
            "and a down pin on scan 5 takes it to 1 (Result1)",
            ladder=prove_box_tee_without_trunk,
            expect={"Result1": 1, "Result2": 2, "Result3": 1},
            scans=SCANS,
            tee=TeeSpec("count_up(", (".down()", ".reset()"), after=0, bare=True),
            probe="a box rung with no trunk contact, tee-drawn",
        ),
    ],
)


def test_cont_box():
    assert_all_passed(run(group))
