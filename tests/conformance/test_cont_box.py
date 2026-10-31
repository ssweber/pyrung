"""A box on a continued wire: the ``cont_box`` group (subroutine ``Conf_ContBox``).

S4-PLAN section 10, question 2: can a box sit on a continued wire of the rung
above, and do its enable and its pins see that wire's writes?  Each case is
one CLICK rung of two wires from the rail: the first latches a bit P, the
second is a counter whose enable or pin reads P, its pins drawn from the rail
under it.  pyrung's exporter writes that drawing, but no CLICK has taken a box
below another wire of its rung yet, so the group is part F, a project of its
own: a CLICK that refuses the rung at paste or transfer costs only this one,
and the refusal is the answer to "can a box sit there" (the cases then do not
run).

The models, as in ``test_solve_order.py``: **L** (live, adopted by S4) reads a
continued wire's contacts, and a box's enable and pins, when the solve reaches
them, so they see the wire above; **S** (pyrung today) reads the whole CLICK
rung, continued wires and box pins included, against the memory the rung was
entered with.  ``SOLVE_ORDER`` records what pyrung reads in-process
(measured), and ``test_solve_order.test_annotation_matches_readings`` checks
it.

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

The cases own C451..C500 and counters CT181..CT190 (``memory.OWNED``), ten C
bits per case (case N takes C<441+10N>..).
"""

from __future__ import annotations

from collections.abc import Mapping

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, count_up, latch, reset
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import count_scans, done, scans_for

GROUP = "cont_box"
SCANS = scans_for()

#: Case id -> (does the answer depend on solve order?, what pyrung reads in-process
#: today under S).  ``test_solve_order`` folds these into ``ORDER_DEPENDENT``.
SOLVE_ORDER: Mapping[str, tuple[bool, Mapping[str, int]]] = {
    "box_pin_on_continued_wire": (True, {"Result1": 1, "Result2": 1}),
    "box_enable_on_continued_wire": (True, {"Result1": 0}),
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
            pending="S4",
            probe="box on a continued wire: pins see the wire above",
        ),
        case(
            "A box on a continued wire reads its enable live: a counter whose enable "
            "needs the bit the wire above it latches on scan 2 counts it (Result1 = 1)",
            ladder=prove_box_enable_on_continued_wire,
            expect={"Result1": 1},
            scans=SCANS,
            pending="S4",
            probe="box on a continued wire: enable sees the wire above",
        ),
    ],
)


def test_cont_box():
    assert_all_passed(run(group))
