"""Row solve, round 3: the ``row_solve`` group (subroutine ``Conf_RowSolve``).

Hardware questions the live row solve (S4) still had open after round 2, in
drawings pyrung's exporter writes and CLICK has taken before (branches off the
trunk, a box with a continued wire below it).  Part C, beside ``rungs`` and ``solve_order``.

The models, as in ``test_solve_order.py``:

- **L** (live, CLICK's model since hardware round 1, adopted by S4): the
  trunk's power is computed once and held for every row under it; each row's
  own contacts, edge contacts included, are read when the solve reaches the
  row, so they see the writes of the rows above; a box reads its enable and
  pins before it writes; a continued wire is a wire of its own from the rail,
  read live.  Each case's ``expect`` is L's reading.
- **S** (snapshot, pyrung before S4): every contact of the rung, its branches
  and its continued wires reads the memory the rung was entered with, and S3's
  edge frame samples every edge contact of the rung there too.  ``SOLVE_ORDER``
  records what pyrung read in-process before S4 (measured, not derived);
  ``test_solve_order.test_annotation_matches_readings`` checks it against each
  case's ``expect``, which pyrung now reads.

The cases, what each model reads, and the other models they tell apart:

- ``edge_on_branch_reads_row_above`` (+, probe): the trunk's first row writes
  ``out(X)`` for scans 3..5; the rows under it are a ``rise(X)`` and a
  ``fall(X)`` branch.  L: the rise fires on scan 3 and the fall on 6 (R1 = 3,
  R2 = 6), the scans X changes.  S, or any model that samples a rung's edge
  contacts when the rung is entered (an edge frame per rung, not per row): a
  scan late, 4 and 7.  The rail rise and fall below the rung (R3 = 3, R4 = 6
  in every model) are controls: the bit moved on the scans it was meant to.
  An edge whose memory is the bit at the end of the previous scan, read live,
  also fires on 3 and 6, so this case does not tell it from L;
  ``coils/edge_sees_previous_scan_end`` rules that model out.
- ``edge_under_unpowered_branch`` (-, probe): the trunk is powered every scan;
  a parent branch whose only contact (the gate) is off while a bit rises (scan
  3) and falls (scan 7) holds a ``rise`` and a ``fall`` branch; the gate is on
  for scans 5 and 8 only, written by one rung (two ``out`` rungs would leave
  it as the lower one writes it, off on scan 5).  An edge contact under an
  unpowered parent is still solved and its memory updated ("scanned", adopted:
  S3 frames every contact of the rung, and S4 reaches every row, unpowered or
  not): R1 = 0, R2 = 0.  Updated only when power reaches it, or a solve that
  skips the rows of an unpowered branch: the rise fires on scan 5 and the fall
  on 8 (5, 8).  The rail rise and fall below (R3 = 3, R4 = 7) are the bit's own
  edges under both.  ``coils/edge_in_false_rung_branch`` answered this for a
  rung whose rail contact is open; this one asks it under a powered trunk, one
  level down.
- ``continued_after_box_rereads_trunk`` (+, probe): a counter (preset 1) whose
  rung needs its own Done off counts on scan 2; the continued wire under the
  box repeats that rung's contacts (~Done first, so the importer does not fold
  the two wires into branches of one) and copies 1 into R1.  L: the continued
  wire reads Done after the box turned it on and does not run (R1 = 0).  S, or
  a CLICK that holds the box rung's power for its continued wires, or one whose
  box writes land when the CLICK rung's solve ends: R1 = 1.  The next rung
  reads Done on scan 2 in every model (R2 = 1): the box counted on that scan.

S5 adds three probes (answers indexed in ``scratchpad/conformance_review/
S5-QUESTIONS.md``):

- ``edge_on_box_path_after_false_contact`` (probe, S5 #1): does a contact
  after a false one on an instruction's path still get solved, so an edge
  contact's memory updates while the path is dead?  A counter whose enable is
  ``gate, rise(bit)`` and one whose only live input is a down pin
  ``And(gate, rise(bit))`` (a box solves its enable and pins together, a path
  of their own).  The bit rises on scan 3 and stays on; the gate is open on
  scan 5 only.  Solved ("scanned", what ``coils/edge_behind_open_contact`` and
  ``edge_under_unpowered_branch`` answer for a coil and a branch, and what
  pyrung reads): the rise memory is already on, neither counts (R1 = 0, R2 =
  0).  A path that stops at its first false contact: the rise fires on scan 5,
  the up counter reads 1 and the down pin counts -1 (1, -1).
- ``edge_after_false_contact_in_branch`` (probe, S5 #1): the same question
  for a branch's own prefix, under a powered trunk: ``branch(gate, rise(bit))``
  and ``branch(gate, fall(bit))``, with a sibling output above them.  Scanned:
  0 and 0; the rail rise and fall below read 3 and 7 in every model.  Stopped
  at the false gate: 5 and 8.  (The trunk-off form is
  ``coils/edge_in_false_rung_branch``, answered on hardware.)
- ``correction_scan_with_free_enable`` (probe, S5 #6): the drum's enable and
  jog are not tied to its step register.  A 2-step event drum is on step 2
  (scan 2: the event advances it, output A off and B on); on scan 3 the step
  register is staged out of range (5) and a jog is on that scan.  Result3 is B
  and Result4 A as they stand at the end of scan 3, Result2 the completion
  flag then and ErrorCode the step then; Result1 is the step at the end.
  Hardware (2026-11-01) answers that the jog scan leaves the bad step and the
  outputs alone: step 5 at the end of scan 3 (ErrorCode = 5), flag off (Result2
  = 0), B still on (Result3 = 1, Result4 = 0), and the drum ends on step 1
  (Result1 = 1).  ``drum_trace/free_jog`` reads the scans after it: the next
  scan corrects, and B stays on.

The cases own C401..C450 and C1..C50 and counters CT191..CT195
(``memory.OWNED``): cases 1 to 3 take ten C bits each (case N takes
C<391+10N>..), cases 4 to 6 take C1.., C11.. and C21...

Open: whether a NOP or a blank row splits the
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
from pyrung.core import (
    And,
    Or,
    Rung,
    branch,
    calc,
    copy,
    count_up,
    event_drum,
    fall,
    out,
    rise,
)
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import WINDOW, count_scans, done, firings, scans_for

GROUP = "row_solve"
SCANS = scans_for()

#: Case id -> (does the answer depend on solve order?, what pyrung read in-process
#: under S, before S4).  ``test_solve_order`` folds these into ``ORDER_DEPENDENT``.
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
    # The S5 probes: every contact reads what an earlier rung wrote, so the model
    # does not change the reading.
    "edge_on_box_path_after_false_contact": (False, {"Result1": 0, "Result2": 0}),
    "edge_after_false_contact_in_branch": (
        False,
        {"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 7},
    ),
    "correction_scan_with_free_enable": (
        False,
        {"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0, "ErrorCode": 5},
    ),
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
    # The bit C411 is on for scans 3..6; the gate C412 on scans 5 and 8 only,
    # from one rung (a second out() rung would write it off on scan 5).  C413
    # is the trunk's own output, there so the gate stays a branch.
    bit, gate = c[411], c[412]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 6):
        out(bit)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 5, slot.Scratch == 8)):
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


def prove_edge_on_box_path_after_false_contact(slot):
    # The bit C1 is on from scan 3 on; the gate C2 only on scan 5.  C3 is never
    # on: it keeps the second counter's up input off, so only its down pin acts.
    bit, gate, never = c[1], c[2], c[3]
    up, down = ClickCounter(192), ClickCounter(193)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        out(bit)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        out(gate)
    with Rung(gate, rise(bit), slot.Cmd != 0):
        count_up(up, 100).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, never):
        count_up(down, 100).down(And(gate, rise(bit))).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0):
        copy(up.Acc, slot.Result1)
        copy(down.Acc, slot.Result2)
    done(slot)


def prove_edge_after_false_contact_in_branch(slot):
    # C11 on for scans 3..6; the gate C12 on scans 5 and 8; C13 the trunk's own
    # output, above the branches.
    bit, gate = c[11], c[12]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 6):
        out(bit)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 5, slot.Scratch == 8)):
        out(gate)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW):
        out(c[13])
        with branch(gate, rise(bit)):
            calc(slot.Result1 * 10 + slot.Scratch, slot.Result1)
        with branch(gate, fall(bit)):
            calc(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    firings(rise(bit), slot, slot.Result3)
    firings(fall(bit), slot, slot.Result4)
    done(slot)


def prove_correction_scan_with_free_enable(slot):
    # Outputs A = C21 and B = C22, events C23 and C24 (the first pulses on scan
    # 2), completion flag C25, jog C26 (scan 3).  The step register is Result1:
    # staged 1 on scan 1 and 5 (out of range) on scan 3, above the drum.  The
    # drum's enable is Cmd != 0 and its jog pin the jog bit alone.
    a, b, event, flag, jog = c[21], c[22], c[23], c[25], c[26]
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        out(event)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(5, slot.Result1)
        out(jog)
    with Rung(slot.Cmd != 0):
        event_drum(
            outputs=[a, b],
            events=[event, c[24]],
            pattern=[[1, 0], [0, 1]],
            current_step=slot.Result1,
            completion_flag=flag,
        ).reset(slot.Cmd == 0).jog(jog)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(slot.Result1, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, flag):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, b):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, a):
        copy(1, slot.Result4)
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
            probe="edge contact on a branch sees a row above",
        ),
        case(
            "An edge contact under an unpowered branch still tracks its bit: with the "
            "trunk powered and the parent branch's gate off while a bit rises (scan "
            "3) and falls (scan 7), the rise and fall branches under it do not fire "
            "when the gate comes on, on scans 5 and 8 (Result1 = 0, Result2 = 0); the "
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
            probe="continued wire re-reads the trunk after a box",
        ),
        case(
            "An edge contact after a false contact on a box's enable and on its down pin "
            "still tracks its bit: a bit that rises on scan 3 is already seen when the "
            "gate opens on scan 5, so neither the up counter (Result1 = 0) nor the down "
            "pin (Result2 = 0) counts",
            ladder=prove_edge_on_box_path_after_false_contact,
            expect={"Result1": 0, "Result2": 0},
            scans=SCANS,
            probe="S5 #1: edge memory on a box path after a false contact",
        ),
        case(
            "An edge contact after a false contact in a branch still tracks its bit: "
            "with the trunk powered and the branch's gate off while a bit rises (scan "
            "3) and falls (scan 7), the rise and fall behind the gate do not fire when "
            "it opens on scans 5 and 8 (Result1 = 0, Result2 = 0); the rail rise and "
            "fall see scans 3 (Result3) and 7 (Result4)",
            ladder=prove_edge_after_false_contact_in_branch,
            expect={"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 7},
            scans=SCANS,
            probe="S5 #1: edge memory after a false contact on a branch's prefix",
        ),
        case(
            "A drum whose enable and jog do not read its step register, with the "
            "register staged out of range on the scan of a jog: that scan leaves it "
            "alone, so the step reads 5 (ErrorCode) with the flag off (Result2 = 0), "
            "output B on (Result3 = 1) and A off (Result4 = 0), and the drum ends on "
            "step 1 (Result1)",
            ladder=prove_correction_scan_with_free_enable,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 0, "ErrorCode": 5},
            scans=SCANS,
            probe="S5 #6: the correction scan with an enable and a jog of its own",
        ),
    ],
)


def test_row_solve():
    assert_all_passed(run(group))
