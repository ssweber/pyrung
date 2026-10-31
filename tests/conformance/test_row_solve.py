"""Row solve, round 3: the ``row_solve`` group (subroutine ``Conf_RowSolve``).

Hardware questions the live row solve (S4, ``scratchpad/conformance_review/
S4-PLAN.md`` section 10) still has open after round 2, in drawings pyrung's
exporter writes and CLICK has taken before (branches off the trunk, a box with
a continued wire below it).  Part C, beside ``rungs`` and ``solve_order``.

The models, as in ``test_solve_order.py``:

- **L** (live, CLICK's model since round 2, adopted by S4): the trunk's power
  is computed once and held for every row under it; each row's own contacts,
  edge contacts included, are read when the solve reaches the row, so they see
  the writes of the rows above; a box reads its enable and pins before it
  writes; a continued wire is a wire of its own from the rail, read live.
  Each case's ``expect`` is L's reading.
- **S** (snapshot, pyrung today): every contact of the rung, its branches and
  its continued wires reads the memory the rung was entered with, and S3's edge
  frame samples every edge contact of the rung there too.  ``SOLVE_ORDER``
  records what pyrung reads in-process (measured, not derived), and
  ``test_solve_order.test_annotation_matches_readings`` checks it.

The cases, what each model reads, and the other models they tell apart:

- ``edge_on_branch_reads_row_above`` (+, probe): the trunk's first row writes
  ``out(X)`` for scans 3..5; the rows under it are a ``rise(X)`` and a
  ``fall(X)`` branch.  L: the rise fires on scan 3 and the fall on 6 (R1 = 3,
  R2 = 6), the scans X changes.  S, or any model that samples a rung's edge
  contacts when the rung is entered (an edge frame per rung, not per row): a
  scan late, 4 and 7.  An edge contact whose memory is the bit at the end of
  the previous scan never fires (0, 0): the rail rise and fall below the rung
  (R3 = 3, R4 = 6 in every model) rule that out on their own.
- ``edge_under_unpowered_branch`` (-, probe): the trunk is powered every scan;
  a parent branch whose only contact (the gate) is open while a bit rises
  (scan 3) and falls (scan 7) holds a ``rise`` and a ``fall`` branch; the gate
  is on for scans 5 and 8 only.  An edge contact under an unpowered parent is
  still solved and its memory updated ("scanned", adopted: S3 frames every
  contact of the rung, and S4 reaches every row, unpowered or not): R1 = 0, R2
  = 0.  Updated only when power reaches it, or a solve that skips the rows of
  an unpowered branch: the rise fires on scan 5 and the fall on 8 (5, 8).  The
  rail rise and fall below (R3 = 3, R4 = 7) are the bit's own edges under
  both.  ``coils/edge_in_false_rung_branch`` answered this for a rung whose
  rail contact is open; this one asks it under a powered trunk, one level down.
- ``continued_after_box_rereads_trunk`` (+, probe): a counter (preset 1) whose
  rung needs its own Done off counts on scan 2; the continued wire under the
  box repeats that rung's contacts (~Done first, so the importer does not fold
  the two wires into branches of one) and copies 1 into R1.  L: the continued
  wire reads Done after the box turned it on and does not run (R1 = 0).  S, or
  a CLICK that holds the box rung's power for its continued wires, or one whose
  box writes land when the CLICK rung's solve ends: R1 = 1.  The next rung
  reads Done on scan 2 in every model (R2 = 1): the box counted on that scan.

The cases own C401..C450 and counters CT191..CT200 (``memory.OWNED``), ten C
bits per case (case N takes C<391+10N>..).

Open (S4-PLAN section 10, question 6): whether a NOP or a blank row splits the
solve has no case.  Under L nothing is held from one wire to the next (each
continued wire starts at the rail, read live), so a split at a NOP or a blank
row reads the same as no split; only S or a fixpoint over a CLICK rung would
tell, and round 2 already ruled both out for continued wires
(``rungs/continued_shares_snapshot``, ``solve_order/continued_reads_later_wire``).
CLICK deletes a NOP row that shares its rung (``tests/click/
test_codegen_nop_continued.py``), so a NOP between two wires of one rung
cannot be drawn.
"""

from __future__ import annotations

from collections.abc import Mapping

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, branch, calc, copy, count_up, fall, out, rise
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import WINDOW, count_scans, done, firings, scans_for

GROUP = "row_solve"
SCANS = scans_for()

#: Case id -> (does the answer depend on solve order?, what pyrung reads in-process
#: today under S).  ``test_solve_order`` folds these into ``ORDER_DEPENDENT``.
SOLVE_ORDER: Mapping[str, tuple[bool, Mapping[str, int]]] = {
    "edge_on_branch_reads_row_above": (
        True,
        {"Result1": 4, "Result2": 7, "Result3": 3, "Result4": 6},
    ),
    "edge_under_unpowered_branch": (
        False,
        {"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 7},
    ),
    "continued_after_box_rereads_trunk": (True, {"Result1": 1, "Result2": 1}),
}


def prove_edge_on_branch_reads_row_above(slot):
    # X = C401, on for scans 3..5, written by the trunk's first row.
    bit = c[401]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        with branch(slot.Scratch >= 3, slot.Scratch <= 5):
            out(bit)
        with branch(rise(bit)):
            calc(slot.Result1 * 10 + slot.Scratch, slot.Result1)
        with branch(fall(bit)):
            calc(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    firings(rise(bit), slot, slot.Result3)
    firings(fall(bit), slot, slot.Result4)
    done(slot)


def prove_edge_under_unpowered_branch(slot):
    # The bit C411 is on for scans 3..6; the gate C412 on scans 5 and 8 only.
    # C413 is the trunk's own output, there so the gate stays a branch.
    bit, gate = c[411], c[412]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 6):
        out(bit)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        out(gate)
    with Rung(slot.Cmd != 0, slot.Scratch == 8):
        out(gate)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        out(c[413])
        with branch(gate):
            with branch(rise(bit)):
                calc(slot.Result1 * 10 + slot.Scratch, slot.Result1)
            with branch(fall(bit)):
                calc(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    firings(rise(bit), slot, slot.Result3)
    firings(fall(bit), slot, slot.Result4)
    done(slot)


def prove_continued_after_box_rereads_trunk(slot):
    # Counter CT191, preset 1, counts on scan 2.  The continued wire repeats the
    # box rung's contacts, ~Done first.
    counter = ClickCounter(191)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2, ~counter.Done):
        count_up(counter, 1).reset(slot.Cmd == 0)
    with Rung(~counter.Done, slot.Scratch == 2, slot.Cmd != 0).continued():
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 2, counter.Done):
        copy(1, slot.Result2)
    done(slot)


group = Group(
    GROUP,
    [
        case(
            "An edge contact on a branch reads the rows above it in the same scan: "
            "the trunk's first row turns a bit on for scans 3..5, and the rise branch "
            "under it fires on scan 3 (Result1) and the fall branch on scan 6 "
            "(Result2), as the rail rise (Result3) and fall (Result4) below the rung do",
            ladder=prove_edge_on_branch_reads_row_above,
            expect={"Result1": 3, "Result2": 6, "Result3": 3, "Result4": 6},
            scans=SCANS,
            pending="S4",
            probe="edge contact on a branch sees a row above",
        ),
        case(
            "An edge contact under an unpowered branch still tracks its bit: with the "
            "trunk powered and the parent branch's gate open while a bit rises (scan "
            "3) and falls (scan 7), the rise and fall branches under it do not fire "
            "when the gate closes on scans 5 and 8 (Result1 = 0, Result2 = 0); the "
            "rail rise and fall see scans 3 (Result3) and 7 (Result4)",
            ladder=prove_edge_under_unpowered_branch,
            expect={"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 7},
            scans=SCANS,
            probe="edge memory under an unpowered branch",
        ),
        case(
            "A continued wire below a box reads the box's writes: a counter whose rung "
            "needs its own Done off counts to its preset on scan 2, and the continued "
            "wire that repeats that rung's contacts does not run (Result1 = 0); the "
            "next rung sees Done on that scan (Result2 = 1)",
            ladder=prove_continued_after_box_rereads_trunk,
            expect={"Result1": 0, "Result2": 1},
            scans=SCANS,
            pending="S4",
            probe="continued wire re-reads the trunk after a box",
        ),
    ],
)


def test_row_solve():
    assert_all_passed(run(group))
