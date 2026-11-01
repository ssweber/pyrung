"""A box on a continued wire: the ``cont_box`` group (subroutine ``Conf_ContBox``).

S4-PLAN section 10, question 2: can a box sit on a continued wire of the rung
above, and do its enable and its pins see that wire's writes?  Each case is
one CLICK rung of two wires from the rail: the first latches a bit P, the
second is a counter whose enable or pin reads P, its pins drawn from the rail
under it.  pyrung's exporter writes that drawing, but no CLICK has taken a box
below another wire of its rung yet, so the group is in part C, last with the
other paste probes: a CLICK that refuses the rung at paste or transfer costs
only this group's subroutine and call rung,
and the refusal is the answer to "can a box sit there" (the cases then do not
run).

The models, as in ``test_solve_order.py``: **L** (live, adopted by S4) reads a
continued wire's contacts, and a box's enable and pins, when the solve reaches
them, so they see the wire above; **S** (pyrung before S4) reads the whole
CLICK rung, continued wires and box pins included, against the memory the rung
was entered with.  ``SOLVE_ORDER`` records what pyrung read in-process before
S4 (measured), and ``test_solve_order.test_annotation_matches_readings``
checks it against each case's ``expect``, which pyrung now reads.

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

- ``box_pin_on_own_rung`` (probe, S5 #2): the control of ``box_pin_on_continued_wire``.
  The same counter, pin and latching rung, but the counter stands on a rung
  of its own (an ``R`` marker, its contacts from the rail) instead of below a
  wire.  S5 asks whether a CLICK accepts the box first on a continued wire and
  reads it the same as the box on its own rung: adopted, the same (R1 = 0, R2 =
  1), as in L every row of a rung and every rung reads live.  A CLICK that
  reads the continued box differently from this control answers question 2 with
  the difference.
- ``box_tee_without_trunk`` (probe, S5 #3): a box rung with no contact in
  front of its own, its pins drawn as tees (``R,-,T`` over ``,,-``: a wire in
  column A and the tee in column B, which CLICK already takes for wires).  The
  counter (preset 2) counts up on scans 1 and 3, so Acc is 2 on scan 4 (R2)
  with Done on (R3 = 1), and a down pin on scan 5 takes it to 1 (R1).  The
  tee redraws only the CLICK export (``tee=``, ``bare=True``);
  ``test_tee_twins.py`` imports the drawing and compares it with the rail
  drawing.  A CLICK that refuses the split with a box never fires the case;
  that refusal is the answer.

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

#: Case id -> (does the answer depend on solve order?, what pyrung read in-process
#: under S, before S4).  ``test_solve_order`` folds these into ``ORDER_DEPENDENT``.
SOLVE_ORDER: Mapping[str, tuple[bool, Mapping[str, int]]] = {
    "box_pin_on_continued_wire": (True, {"Result1": 1, "Result2": 1}),
    "box_enable_on_continued_wire": (True, {"Result1": 0}),
    # The S5 probes: the control reads what a rung above wrote in either model.
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
    # S5 #2 control.  P = C471 and counter CT183: the ladder of
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
    # S5 #3.  Counter CT184, preset 2: up pulses (C481) on scans 1 and 3, a down
    # pulse (C482) on scan 5.  The box rung has no contact before its own and
    # both pins have only their own, so the tee drawing hangs off a bare wire.
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
            probe="S5 #2: the box on its own rung, the control for the continued wire",
        ),
        case(
            "A box rung with no contact in front of its own, its pins drawn as tees off "
            "a bare wire (R,-,T over ,,-), counts like any other: a counter of preset "
            "2 counts up on scans 1 and 3 (Result2 = 2 on scan 4, Done on, Result3 = 1) "
            "and a down pin on scan 5 takes it to 1 (Result1)",
            ladder=prove_box_tee_without_trunk,
            expect={"Result1": 1, "Result2": 2, "Result3": 1},
            scans=SCANS,
            tee=TeeSpec("count_up(", (".down()", ".reset()"), after=0, bare=True),
            probe="S5 #3: a box rung with no trunk contact, tee-drawn",
        ),
    ],
)


def test_cont_box():
    assert_all_passed(run(group))
