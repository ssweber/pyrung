"""Solve order: the ``solve_order`` group (subroutine ``Conf_SolveOrder``).

Every topology where an earlier part of a rung writes a tag a later part
reads, asked on a CLICK in hardware round 2.  Each case records whether its
answer depends on the order the rung is solved in.  The table is the spec of
the live row solve (``pending="S4"``) and of the check that will warn on
intra-rung order dependence, and it is that check's test data.

Two models of a rung:

- **L** (live, CLICK's model since hardware round 1): each contact and each
  box leg is read when the solve reaches its row.  The power at a shared node
  is computed once and held for every row below it.  Each case's ``expect`` is
  L's reading.
- **S** (snapshot, pyrung today): every contact and leg is read at rung entry;
  instructions still run in source order, with live operands.  Each case's
  ``snapshot`` is pyrung's in-process reading, measured, not derived.

``order_dependent`` is ``snapshot != expect``, declared on each case through
``solve_case()`` and checked by ``test_annotation_matches_readings``.  The four
live-read cases of the ``rungs`` group join the same map (``RUNGS_SNAPSHOT``),
and so do round 3's ``row_solve`` and ``cont_box`` groups, which annotate their
own cases (``SOLVE_ORDER`` in each module), so ``ORDER_DEPENDENT`` covers every
case the suite has on solve order.  If
round 2 reads S for some topology, its ``expect`` becomes S's reading and its
annotation flips to False.

Every case of the ``solve_order`` group itself keeps edge, counter and drum
semantics out of the answer (no S2, S3 or DRUM; the folded-in ``row_solve``
and ``cont_box`` cases ask about edges and boxes on purpose): the bit a later
part reads (P below) is latched by its writer row and cleared by the next rung
(or, in a one-shot case whose rung never runs again, by ``init``), so it is
never on when the rung that reads it is entered.  A part that reads P live
sees it; one that reads the snapshot never does.  A pulsed edge fires once
under L and never under S, and every counted or clocked input is a one-scan
pulse, so level and edge counting agree.  The drum cases read only the step,
never an output or the completion flag, and no scan has two drum controls on.
A box's reset leg is ``Cmd == 0`` (or that, or P).

A case where L and S agree (a control, -) must still tell some model apart,
as AGENTS.md asks.  The models named below, and what each reads:

- W: every row's whole path is re-solved live, rail contacts included.
- F (fixpoint): the rung, or a CLICK rung of continued wires, is solved again
  until nothing changes.
- E: coil writes land when the rung's solve ends.
- R: a box's legs are read once the rung's solve ends, after every row.
- O: an OR of coils on one bus, or set beating reset, at the end of the rung.
- V: every body rung of a For loop reads the For rung's snapshot.

The cases own C501..C700, timer T151..T160 and counters CT161..CT170
(``memory.OWNED``), ten C bits per case (case N takes C<491+10N>..).

+ = order-dependent (pending S4), - = a control.  R1..R4 = Result1..Result4.

- ``row_series_reads_rows_above`` (+): rows 1 and 2 latch A and B; row 3
  needs both on, row 4 both off.  L {1, 0}; S {0, 1}.
- ``row_reads_row_below`` (-): the upper branch needs on a bit the lower one
  latches; the next rung reads it.  L = S {0, 1}; F {1, 1}.
- ``sibling_branch_reads_sibling_above`` (+): inside one parent branch, the
  first inner branch latches A; the second needs A on, the third off.  L
  {1, 0, 1, 1}; S {0, 1, 1, 1}.  R3 is the parent branch's own output, R4 its
  sibling's.
- ``outer_row_reads_inner_branch_write`` (+): a write inside an inner branch,
  read by the rung's next branches.  L {1, 0, 1}; S {0, 1, 1}.
- ``input_or_legs_read_before_outputs`` (-): the rail ends in an OR of a bit
  nothing writes and ``~B``; output row 1 latches B and row 2 still runs.  L =
  S {1}; W {0}.
- ``branch_or_reads_row_above`` (+): a branch's input is an OR of a bit
  nothing writes and A, latched above; a second branch needs A off (an OR
  too).  L {1, 0}; S {0, 1}.
- ``bus_operand_sees_coil`` (-): ``out(X)``, then a pack of X..X+3 on the
  next bus row.  Operands are live in both: L = S {1}; E {0}.
- ``bus_coils_last_write_wins`` (-): on one bus, X1 latched then reset and X2
  reset then latched.  L = S {0, 1}; O (OR, set wins) {1, 1}; reset wins
  {0, 0}.
- ``continued_reads_later_wire`` (-): a wire needs on a bit its continued
  wire latches; the next rung reads it.  L = S {0, 1}; F {1, 1}.
- ``box_leg_reads_row_below`` (-, answered as adopted): a counter counts on scan 2, with
  reset leg ``Or(Cmd == 0, P)``; a continued wire below it latches P on scan
  4.  L = S {1}; R and F {0}.  A box's legs take the rows under it, so what
  sits below a box in one CLICK rung is a continued wire.
- ``for_body_rung_reads_body_rung_above`` (-, measured): body rung 1 latches
  A, body rungs 2 and 3 need it on and off.  pyrung takes a fresh snapshot
  per body rung, so it reads L {1, 0}; V {0, 1}.

CLICK does not allow a box below another output on one wire: all inputs
(pins) of a box must start from the same branch (error C000A), so a box's input wire is its own, from the rail.  No leg can read a
row above it in the same wire; the cases that asked (a box below a latch row)
are retired.  What sits below a box in one CLICK rung is a continued wire
(``box_leg_reads_row_below``).

Each For rung is unconditional, and its body rungs are gated instead, so no
case depends on what a false For rung does (F2, step S1).  Each loop runs
once, so no case asks how a body rung's contacts read across iterations (F5).

Open: a ``call()`` inside a rung, between rows.  CLICK has no nested calls and
every case already runs inside ``Conf_SolveOrder``; it waits for a subroutine
home called from main.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pyrung.conformance import Case, CaseResult, Group, assert_all_passed, case, run
from pyrung.core import (
    Or,
    Rung,
    branch,
    copy,
    count_up,
    forloop,
    latch,
    out,
    pack_bits,
    reset,
)
from tests.conformance import test_cont_box, test_row_solve, test_rung_semantics
from tests.conformance.memory import ClickCounter, c
from tests.conformance.scans import count_scans, done, scans_for

GROUP = "solve_order"
SCANS = scans_for()

#: ``"group/id"`` -> does the case's answer depend on solve order?
ORDER_DEPENDENT: dict[str, bool] = {}
#: ``"group/id"`` -> what the case reads when every contact and leg is read at
#: rung entry (S): pyrung's reading in-process, measured.
SNAPSHOT: dict[str, dict[str, int]] = {}

#: The ``rungs`` group's live-read cases, and what each reads under S.
RUNGS_SNAPSHOT: Mapping[str, Mapping[str, int]] = {
    "branch_sees_rung_entry": {"Result1": 1, "Result2": 0},
    "nested_branches_see_rung_entry": {"Result1": 1, "Result2": 1},
    "continued_shares_snapshot": {"Result1": 1, "Result2": 1},
    "continued_chain_breaks": {"Result1": 1, "Result2": 1, "Result3": 1},
}

for _id, _snapshot in RUNGS_SNAPSHOT.items():
    ORDER_DEPENDENT[f"rungs/{_id}"] = True
    SNAPSHOT[f"rungs/{_id}"] = dict(_snapshot)

#: Round 3's solve-order groups, which annotate their own cases (``SOLVE_ORDER``:
#: id -> (order dependent?, the S reading)).
ANNOTATED_MODULES = (test_row_solve, test_cont_box)

for _module in ANNOTATED_MODULES:
    for _id, (_dependent, _snapshot) in _module.SOLVE_ORDER.items():
        ORDER_DEPENDENT[f"{_module.group.name}/{_id}"] = _dependent
        SNAPSHOT[f"{_module.group.name}/{_id}"] = dict(_snapshot)


def solve_case(
    sentence: str, *, order_dependent: bool, snapshot: Mapping[str, int], **kw: Any
) -> Case:
    """``case()``, recording whether its answer depends on solve order and its S reading."""
    made = case(sentence, **kw)
    key = f"{GROUP}/{made.id}"
    ORDER_DEPENDENT[key] = order_dependent
    SNAPSHOT[key] = dict(snapshot)
    return made


def _fire(slot: Any) -> None:
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


# -- Rows of one rung ------------------------------------------------------


def prove_row_series_reads_rows_above(slot):
    # A = C501, B = C502.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[501])
        latch(c[502])
        with branch(c[501], c[502]):
            copy(1, slot.Result1)
        with branch(~c[501], ~c[502]):
            copy(1, slot.Result2)
    _fire(slot)


def init_row_series_reads_rows_above(slot):
    reset(c[501])
    reset(c[502])


def prove_row_reads_row_below(slot):
    # The lower branch latches C511; C519 is never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with branch(c[511]):
            copy(1, slot.Result1)
        with branch(~c[519]):
            latch(c[511])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[511]):
        copy(1, slot.Result2)
    _fire(slot)


def init_row_reads_row_below(slot):
    reset(c[511])


def prove_sibling_branch_reads_sibling_above(slot):
    # The parent branch (C529 off, never written) holds three inner branches;
    # the first latches C521.  C527 and C528 are never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with branch(~c[529]):
            copy(1, slot.Result3)
            with branch(~c[528]):
                latch(c[521])
            with branch(c[521]):
                copy(1, slot.Result1)
            with branch(~c[521]):
                copy(1, slot.Result2)
        with branch(~c[527]):
            copy(1, slot.Result4)
    _fire(slot)


def init_sibling_branch_reads_sibling_above(slot):
    reset(c[521])


def prove_outer_row_reads_inner_branch_write(slot):
    # The outer branch's inner branch latches C531; the rung's next branches
    # read it.  C538 and C539 are never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with branch(~c[539]):
            copy(1, slot.Result3)
            with branch(~c[538]):
                latch(c[531])
        with branch(c[531]):
            copy(1, slot.Result1)
        with branch(~c[531]):
            copy(1, slot.Result2)
    _fire(slot)


def init_outer_row_reads_inner_branch_write(slot):
    reset(c[531])


# -- OR on the input bus ---------------------------------------------------


def prove_input_or_legs_read_before_outputs(slot):
    # B = C541; C549 is never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0, Or(c[549], ~c[541])):
        latch(c[541])
        copy(1, slot.Result1)
    _fire(slot)


def init_input_or_legs_read_before_outputs(slot):
    reset(c[541])


def prove_branch_or_reads_row_above(slot):
    # A = C551; C558 and C559 are never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[551])
        with branch(Or(c[559], c[551])):
            copy(1, slot.Result1)
        with branch(Or(c[558], ~c[551])):
            copy(1, slot.Result2)
    _fire(slot)


def init_branch_or_reads_row_above(slot):
    reset(c[551])


# -- The output bus --------------------------------------------------------


def prove_bus_operand_sees_coil(slot):
    # X = C561; C562..C564 are never written.
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        out(c[561])
        pack_bits(c.select(561, 564), slot.Result1)
    _fire(slot)


def prove_bus_coils_last_write_wins(slot):
    # X1 = C571 (latched, then reset), X2 = C572 (reset, then latched).
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        latch(c[571])
        reset(c[571])
        reset(c[572])
        latch(c[572])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[571]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[572]):
        copy(1, slot.Result2)
    _fire(slot)


def init_bus_coils_last_write_wins(slot):
    reset(c[572])


# -- Continued wires -------------------------------------------------------


def prove_continued_reads_later_wire(slot):
    # The continued wire latches C581, which the wire above it needs on.
    with Rung(c[581], slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0).continued():
        latch(c[581])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[581]):
        copy(1, slot.Result2)
    _fire(slot)


def init_continued_reads_later_wire(slot):
    reset(c[581])


# -- A box and a continued wire below it ------------------------------------


def prove_box_leg_reads_row_below(slot):
    # P = C661, latched on scan 4 by a continued wire below the counter (what
    # sits below a box in one rung is a continued wire).  The counter counts on scan 2.
    counter = ClickCounter(162)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        count_up(counter, 100).reset(Or(slot.Cmd == 0, c[661]))
    with Rung(slot.Scratch == 4, slot.Cmd != 0).continued():
        latch(c[661])
    with Rung(slot.Cmd != 0):
        reset(c[661])
        copy(counter.Acc, slot.Result1)
    done(slot)


# -- For bodies ------------------------------------------------------------


def prove_for_body_rung_reads_body_rung_above(slot):
    # A = C681, latched by the first body rung.
    with Rung():
        with forloop(1):
            with Rung(slot.Cmd != 0, slot.Fired == 0):
                latch(c[681])
            with Rung(c[681], slot.Cmd != 0, slot.Fired == 0):
                copy(1, slot.Result1)
            with Rung(~c[681], slot.Cmd != 0, slot.Fired == 0):
                copy(1, slot.Result2)
    _fire(slot)


def init_for_body_rung_reads_body_rung_above(slot):
    reset(c[681])


group = Group(
    GROUP,
    [
        solve_case(
            "Each row reads its contacts when the solve reaches it: rows 1 and 2 latch "
            "two bits, a row needing both on runs (Result1 = 1) and one needing both "
            "off does not (Result2 = 0)",
            ladder=prove_row_series_reads_rows_above,
            init=init_row_series_reads_rows_above,
            expect={"Result1": 1, "Result2": 0},
            pending="S4",
            order_dependent=True,
            snapshot={"Result1": 0, "Result2": 1},
        ),
        solve_case(
            "A row does not see a write from a row below it, and the rung is solved "
            "once: a branch needing on the bit the next branch latches does not run "
            "(Result1 = 0), though the bit is on for the next rung (Result2 = 1)",
            ladder=prove_row_reads_row_below,
            init=init_row_reads_row_below,
            expect={"Result1": 0, "Result2": 1},
            order_dependent=False,
            snapshot={"Result1": 0, "Result2": 1},
        ),
        solve_case(
            "Sibling branches inside one branch read live: the first latches a bit, the "
            "next needing it on runs (Result1 = 1) and the one needing it off does not "
            "(Result2 = 0); the parent branch's own output runs (Result3 = 1), and so "
            "does its sibling (Result4 = 1)",
            ladder=prove_sibling_branch_reads_sibling_above,
            init=init_sibling_branch_reads_sibling_above,
            expect={"Result1": 1, "Result2": 0, "Result3": 1, "Result4": 1},
            pending="S4",
            order_dependent=True,
            snapshot={"Result1": 0, "Result2": 1, "Result3": 1, "Result4": 1},
        ),
        solve_case(
            "A write inside an inner branch is seen by the rung's later branches: one "
            "needing the bit on runs (Result1 = 1), one needing it off does not "
            "(Result2 = 0); the outer branch's own output runs (Result3 = 1)",
            ladder=prove_outer_row_reads_inner_branch_write,
            init=init_outer_row_reads_inner_branch_write,
            expect={"Result1": 1, "Result2": 0, "Result3": 1},
            pending="S4",
            order_dependent=True,
            snapshot={"Result1": 0, "Result2": 1, "Result3": 1},
        ),
        solve_case(
            "An OR on the rail is solved once, before the outputs: with an OR of an off "
            "bit and B off, output row 1 latches B and row 2 still runs (Result1 = 1)",
            ladder=prove_input_or_legs_read_before_outputs,
            init=init_input_or_legs_read_before_outputs,
            expect={"Result1": 1},
            order_dependent=False,
            snapshot={"Result1": 1},
        ),
        solve_case(
            "An OR on a branch reads live: a branch whose OR needs on a bit latched "
            "above runs (Result1 = 1), one whose OR needs it off does not (Result2 = 0)",
            ladder=prove_branch_or_reads_row_above,
            init=init_branch_or_reads_row_above,
            expect={"Result1": 1, "Result2": 0},
            pending="S4",
            order_dependent=True,
            snapshot={"Result1": 0, "Result2": 1},
        ),
        solve_case(
            "An instruction on the output bus reads the coil above it: out(X), then a "
            "pack of X..X+3 on the next row, reads X on (Result1 = 1)",
            ladder=prove_bus_operand_sees_coil,
            expect={"Result1": 1},
            order_dependent=False,
            snapshot={"Result1": 1},
        ),
        solve_case(
            "Coils on one output bus act in order, the last write wins: a bit latched "
            "then reset is off (Result1 = 0), one reset then latched is on (Result2 = 1)",
            ladder=prove_bus_coils_last_write_wins,
            init=init_bus_coils_last_write_wins,
            expect={"Result1": 0, "Result2": 1},
            order_dependent=False,
            snapshot={"Result1": 0, "Result2": 1},
        ),
        solve_case(
            "A wire does not see a write from a later continued wire: a wire needing on "
            "the bit its continued wire latches does not run (Result1 = 0); the next rung "
            "sees it on (Result2 = 1)",
            ladder=prove_continued_reads_later_wire,
            init=init_continued_reads_later_wire,
            expect={"Result1": 0, "Result2": 1},
            order_dependent=False,
            snapshot={"Result1": 0, "Result2": 1},
        ),
        solve_case(
            "A box's legs do not see a row below the box: a counter that counted on "
            "scan 2, whose reset leg reads a bit a continued wire below it latches on "
            "scan 4, still reads 1 (Result1 = 1)",
            ladder=prove_box_leg_reads_row_below,
            expect={"Result1": 1},
            scans=SCANS,
            order_dependent=False,
            snapshot={"Result1": 1},
        ),
        solve_case(
            "A For body rung sees the writes of the body rung above it in the same "
            "pass: one needing on the bit body rung 1 latches runs (Result1 = 1), one "
            "needing it off does not (Result2 = 0)",
            ladder=prove_for_body_rung_reads_body_rung_above,
            init=init_for_body_rung_reads_body_rung_above,
            expect={"Result1": 1, "Result2": 0},
            order_dependent=False,
            snapshot={"Result1": 1, "Result2": 0},
        ),
    ],
)


@pytest.fixture(scope="module")
def results() -> dict[str, CaseResult]:
    """Every annotated case's in-process result, keyed ``"group/id"``."""
    out: dict[str, CaseResult] = {}
    for g in (group, test_rung_semantics.group, *(m.group for m in ANNOTATED_MODULES)):
        for r in run(g):
            out[f"{g.name}/{r.case.id}"] = r
    return out


def test_solve_order(results: dict[str, CaseResult]):
    assert_all_passed([r for key, r in results.items() if key.startswith(f"{GROUP}/")])


def test_every_case_is_annotated():
    assert {f"{GROUP}/{x.id}" for x in group.cases} <= set(ORDER_DEPENDENT)
    assert set(ORDER_DEPENDENT) == set(SNAPSHOT)
    rungs_ids = {x.id for x in test_rung_semantics.group.cases}
    assert set(RUNGS_SNAPSHOT) <= rungs_ids
    for module in ANNOTATED_MODULES:
        assert set(module.SOLVE_ORDER) == {x.id for x in module.group.cases}


def test_only_s4_pending():
    for x in group.cases:
        assert x.pending in (None, "S4"), f"{GROUP}/{x.id} is pending {x.pending}"


def test_annotation_matches_readings(results: dict[str, CaseResult]):
    problems: list[str] = []
    for key, dependent in ORDER_DEPENDENT.items():
        r = results[key]
        snapshot = SNAPSHOT[key]
        if dependent != (snapshot != r.case.expect):
            problems.append(
                f"{key}: order_dependent={dependent}, but snapshot {snapshot} "
                f"{'==' if snapshot == r.case.expect else '!='} expect {r.case.expect}"
            )
        wanted = snapshot if r.case.pending == "S4" else r.case.expect
        if r.actual != wanted:
            problems.append(
                f"{key}: pyrung reads {r.actual}, expected {wanted} "
                f"({'snapshot, pending S4' if r.case.pending == 'S4' else 'expect'})"
            )
    assert not problems, "\n".join(problems)
