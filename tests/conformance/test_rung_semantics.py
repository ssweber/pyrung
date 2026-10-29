"""Rung semantics: the ``rungs`` group (subroutine ``Conf_Rungs``).

What a rung reads, when, and in what order.  What a CLICK does (hardware
round 1, 2026-10-29, part B, two identical passes; CLICK manual ch. 1,
"Program Execution"):

- A rung is solved row by row, top to bottom.  Each contact is read once,
  live, when the solve reaches it; the power at a shared node (a vertical
  wire) is computed once and held for every row below it; each output runs as
  soon as its row is solved, so a row sees the writes of the rows above it.
- A ``.continued()`` rung is one more row of the same CLICK rung, from the
  rail; it reads live like any row.
- When two rungs drive the same ``out`` coil, the lower one decides how the
  scan leaves it.

pyrung freezes a snapshot of memory on rung entry and reads every contact of
the rung, branches and continued wires included, against it.  The cases where
that shows are ``pending="S4"`` until the live row solve lands.

The bits are C bits; the cases own C1601..C1700 and counters CT151..CT160
(``memory.OWNED``), ten bits per case.  Latched bits are reset in ``init``.

A continued wire starts with a contact no other wire of its CLICK rung starts
with and carries one instruction.  Wires that start alike import back from
CLICK as branches of one wire, and a wire with two instructions as two wires;
either way the ladder means the same, but it would no longer export byte for
byte, and the ClickNick workspace round trip (``test_workspace``) needs it to.

Models the cases tell apart, and what each reads (L = live row by row, CLICK;
S = rung-entry snapshot, pyrung; W = every row's whole path re-solved live;
B = a branch snapshots where it leaves its parent):

- ``memory_is_immediate``: instruction operands read a rung-entry snapshot
  instead of live memory -> Result1 = 0.  This is also the check of source
  order within a rung (the copy that reads Scratch runs after the one that
  writes it).
- ``global_mutation``: writes are committed at the end of the scan -> Result1 = 0.
- ``branch_sees_rung_entry``: L {0, 1}; S {1, 0}; W {0, 0}.
- ``nested_branches_see_rung_entry``: L {0, 0}; S {1, 1}; W {0, 0}; B {0, 1}.
- ``branch_and_with_parent``: branches drawn from the rail (OR with their
  parent, not AND) -> Result1 = 1, Result2 = 1.  It also reads C bits nothing
  writes: were the Bool default True, Result3 = 1 and Result4 = 0.
- ``last_rung_wins_shared_coil``: duplicate coils resolved by OR at the end
  of the scan -> Result1 = 4; the first rung wins -> Result1 = 4, Result2 = 2;
  coil writes held back to the end of the scan -> Result3 = 2.
- ``continued_shares_snapshot``: L {0, 0}; S {1, 1}; only the first continued
  wire sharing the snapshot {1, 0}.
- ``continued_chain_breaks``: L {0, 0, 1}; S {1, 1, 1}; a chain that keeps its
  first snapshot through a normal rung reads Result3 = 0.
- ``box_leg_reads_rows_above`` (probe, round 2): a counter box sits below a
  latch row in one rung, and its reset leg reads the bit that row sets.  A leg
  read when the solve reaches the box's row (adopted, L) resets the counter
  on scan 4: Result1 = 0.  A leg read at rung entry (S) never sees the bit:
  Result1 = 1.  It also asks whether CLICK takes a box below another output
  in one rung at all: if the paste refuses the rung, that is the answer.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, branch, calc, copy, count_up, latch, out, reset
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import count_scans, done, scans_for


def prove_memory_is_immediate(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(42, slot.Scratch)
        copy(slot.Scratch, slot.Result1)
        copy(1, slot.Fired)


def prove_global_mutation(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(99, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(slot.Scratch, slot.Result1)
        copy(1, slot.Fired)


def prove_branch_sees_rung_entry(slot):
    # The rung's own first row sets the bit its parent contact reads (C1601)
    # and the bit its branches read (C1602).  Both are off on entry.
    with Rung(slot.Cmd != 0, slot.Fired == 0, ~c[1601]):
        latch(c[1601])
        latch(c[1602])
        with branch(~c[1602]):
            copy(1, slot.Result1)
        with branch(c[1602]):
            copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_nested_branches_see_rung_entry(slot):
    # C1611 is set above the outer branch, C1612 inside it above the inner
    # ones.  C1613 is never written: the outer branch's contact is always on.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1611])
        with branch(~c[1613]):
            latch(c[1612])
            with branch(~c[1611]):
                copy(1, slot.Result1)
            with branch(~c[1612]):
                copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_branch_and_with_parent(slot):
    # C1621 and C1622 are never written, so they are off.  Two branches per
    # rung: a rung with one branch and nothing else exports as plain series
    # contacts.
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1621]):
        with branch(~c[1621]):
            copy(1, slot.Result1)
        with branch(~c[1622]):
            copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with branch(c[1621]):
            copy(1, slot.Result3)
        with branch(~c[1621]):
            copy(1, slot.Result4)
        copy(1, slot.Fired)


LAST_RUNG_WINDOW = 5


def prove_last_rung_wins_shared_coil(slot):
    # Two rungs drive each coil.  C1631: the upper rung on every scan, the
    # lower from scan 3.  C1632: the upper from scan 3, the lower on every
    # scan.  Result1/Result2 count the scans each coil starts on (as the
    # previous scan left it); Result3/Result4 count the scans C1631 is on
    # between its two rungs and below them.
    w = LAST_RUNG_WINDOW
    count_scans(slot, w)
    with Rung(slot.Cmd != 0, slot.Scratch <= w, c[1631]):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= w, c[1632]):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0):
        out(c[1631])
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        out(c[1632])
    with Rung(slot.Cmd != 0, slot.Scratch <= w, c[1631]):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        out(c[1631])
    with Rung(slot.Cmd != 0):
        out(c[1632])
    with Rung(slot.Cmd != 0, slot.Scratch <= w, c[1631]):
        calc(slot.Result4 + 1, slot.Result4)
    done(slot, w)


def prove_continued_shares_snapshot(slot):
    # Four wires of one CLICK rung.  The first sets C1641 and the second needs
    # it off; the third sets C1642 and the fourth needs it off.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1641])
    with Rung(~c[1641], slot.Cmd != 0, slot.Fired == 0).continued():
        copy(1, slot.Result1)
    with Rung(slot.Fired == 0, slot.Cmd != 0).continued():
        latch(c[1642])
    with Rung(~c[1642], slot.Cmd != 0, slot.Fired == 0).continued():
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_continued_chain_breaks(slot):
    # A chain (C1651 set, then a continued wire), a normal rung that sets
    # C1652, and a chain continuing that normal rung.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1651])
    with Rung(~c[1651], slot.Cmd != 0, slot.Fired == 0).continued():
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[1652])
    with Rung(~c[1652], slot.Cmd != 0, slot.Fired == 0).continued():
        copy(1, slot.Result2)
    with Rung(c[1651], slot.Cmd != 0, slot.Fired == 0).continued():
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_box_leg_reads_rows_above(slot):
    # One rung, two branch rows: the upper latches C1661 on scan 4, the lower
    # holds a counter that counts once (its input is on for scan 2 only) and
    # whose reset leg reads C1661.  The next rung clears C1661 every scan, so
    # it is on only between the latch row and that rung, on scan 4.
    counter = ClickCounter(151)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= 8):
        with branch(slot.Scratch == 4):
            latch(c[1661])
        with branch(slot.Scratch == 2):
            count_up(counter, 100).reset(Or(slot.Cmd == 0, c[1661]))
    with Rung(slot.Cmd != 0):
        reset(c[1661])
        copy(counter.Acc, slot.Result1)
    done(slot)


def init_branch_sees_rung_entry(slot):
    reset(c[1601])
    reset(c[1602])


def init_nested_branches_see_rung_entry(slot):
    reset(c[1611])
    reset(c[1612])


def init_continued_shares_snapshot(slot):
    reset(c[1641])
    reset(c[1642])


def init_continued_chain_breaks(slot):
    reset(c[1651])
    reset(c[1652])


group = Group(
    "rungs",
    [
        case(
            "Memory writes are visible to the next instruction on the same scan",
            ladder=prove_memory_is_immediate,
            expect={"Result1": 42},
            confirmed=True,
        ),
        case(
            "Instructions mutate global memory",
            ladder=prove_global_mutation,
            expect={"Result1": 99},
            confirmed=True,
        ),
        case(
            "A branch reads its contacts live, when the solve reaches its row, after the "
            "rows above it ran; the parent's contacts are read once: the rung's first row "
            "sets the bit its parent contact needs off and the second row still runs "
            "(C1602 set); the branch that needs C1602 off does not run (Result1 = 0) and "
            "the one that needs it on does (Result2 = 1)",
            ladder=prove_branch_sees_rung_entry,
            init=init_branch_sees_rung_entry,
            expect={"Result1": 0, "Result2": 1},
            confirmed=True,
            pending="S4",
        ),
        case(
            "Nested branches read live too: inner branches that need off the bits set "
            "above the outer branch (Result1 = 0) and inside it (Result2 = 0) do not run",
            ladder=prove_nested_branches_see_rung_entry,
            init=init_nested_branches_see_rung_entry,
            expect={"Result1": 0, "Result2": 0},
            confirmed=True,
            pending="S4",
        ),
        case(
            "A branch runs only when its parent's contacts and its own pass, and a "
            "C bit nothing writes is off: true branches under a false parent stay "
            "off (Result1 = 0, Result2 = 0); under a true parent a false branch "
            "stays off (Result3 = 0) and a true one runs (Result4 = 1)",
            ladder=prove_branch_and_with_parent,
            expect={"Result1": 0, "Result2": 0, "Result3": 0, "Result4": 1},
            confirmed=True,
        ),
        case(
            "When two rungs drive one out coil, the lower rung decides how the scan "
            "leaves it, and each write is seen at once: over 5 scans a coil on above "
            "and on from scan 3 below starts 2 scans on (Result1 = 2); one on from "
            "scan 3 above and always below starts 4 on (Result2 = 4); the first is on "
            "between its rungs every scan (Result3 = 5) and below them 3 (Result4 = 3)",
            ladder=prove_last_rung_wins_shared_coil,
            expect={"Result1": 2, "Result2": 4, "Result3": 5, "Result4": 3},
            scans=scans_for(LAST_RUNG_WINDOW),
            confirmed=True,
        ),
        case(
            "A continued wire reads live, like a rung of its own: the second wire needs "
            "off a bit the first sets and does not run (Result1 = 0), nor the fourth, "
            "which needs off a bit the third sets (Result2 = 0)",
            ladder=prove_continued_shares_snapshot,
            init=init_continued_shares_snapshot,
            expect={"Result1": 0, "Result2": 0},
            confirmed=True,
            pending="S4",
        ),
        case(
            "Continued wires and normal rungs read alike: after a chain (Result1 = 0), a "
            "normal rung sets a bit; its continued wire that needs that bit off does not "
            "run (Result2 = 0), and one that needs on the bit set at the head of the "
            "earlier chain runs (Result3 = 1)",
            ladder=prove_continued_chain_breaks,
            init=init_continued_chain_breaks,
            expect={"Result1": 0, "Result2": 0, "Result3": 1},
            confirmed=True,
            pending="S4",
        ),
        case(
            "A box's legs are solved at the box's row: a counter below a row that "
            "latches a bit on scan 4, in the same rung, sees that bit on its reset leg "
            "the same scan, so its count from scan 2 is cleared (Result1 = 0)",
            ladder=prove_box_leg_reads_rows_above,
            expect={"Result1": 0},
            scans=scans_for(),
            pending="S4",
            probe="box legs solved at the box's row; a box below other outputs",
        ),
    ],
)


def test_rungs():
    assert_all_passed(run(group))
