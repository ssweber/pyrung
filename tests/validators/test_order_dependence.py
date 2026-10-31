"""RUNG_ORDER_DEPENDENT: rows that read a tag an earlier row of their rung wrote.

The solve-order table (``tests/conformance/test_solve_order.py``,
``ORDER_DEPENDENT``) is the check's ground truth: it records, for every
conformance case on solve order, whether its answer differs between the live
row solve (a CLICK) and pyrung's rung-entry snapshot.  The check must flag
exactly those cases.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import Case
from pyrung.conformance._ladder import emit_case
from pyrung.conformance._slot import make_slot
from pyrung.core import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    branch,
    call,
    copy,
    count_up,
    forloop,
    latch,
    on_delay,
    out,
    rise,
    subroutine,
)
from pyrung.core.tag import TagType
from pyrung.core.validation import (
    RUNG_ORDER_DEPENDENT,
    ReadAfterWrite,
    SolveRow,
    reads_after_write,
    validate_order_dependence,
)
from tests.conformance import test_rung_semantics, test_solve_order

Go, A, B, P, Q, Y1, Y2 = (Bool(n) for n in ("Go", "A", "B", "P", "Q", "Y1", "Y2"))
N = Int("N")
Ct = Counter.clone("Ct")
Tm = Timer.clone("Tm")


def _row(reads=(), writes=()) -> SolveRow:
    return SolveRow(reads=frozenset(reads), writes=frozenset(writes))


def _findings(program):
    return validate_order_dependence(program).findings


# -- the predicate ------------------------------------------------------------


def test_read_of_an_earlier_write_is_a_hit():
    rows = [_row(reads={"Go"}), _row(writes={"A"}), _row(reads={"A", "B"})]
    assert reads_after_write(rows) == (ReadAfterWrite(reader=2, tag="A", writer=1),)


def test_a_row_does_not_see_its_own_write_or_a_later_one():
    rows = [_row(reads={"A"}, writes={"A"}), _row(reads={"B"}), _row(writes={"B"})]
    assert reads_after_write(rows) == ()


def test_the_hit_names_the_latest_earlier_writer():
    rows = [_row(writes={"A"}), _row(writes={"A"}), _row(reads={"A"})]
    assert reads_after_write(rows) == (ReadAfterWrite(reader=2, tag="A", writer=1),)


def test_an_indexed_access_meets_any_access_to_its_bank():
    banks = {"DS3": "DS", "DS4": "DS"}
    named_write = [_row(writes={"DS3"}), SolveRow(indexed_reads=frozenset({"DS"}))]
    indexed_write = [SolveRow(indexed_writes=frozenset({"DS"})), _row(reads={"DS4"})]
    both = [SolveRow(indexed_writes=frozenset({"DS"})), SolveRow(indexed_reads=frozenset({"DS"}))]
    assert reads_after_write(named_write, bank_of=banks) == (
        ReadAfterWrite(reader=1, tag="DS", writer=0, indexed=True),
    )
    assert reads_after_write(indexed_write, bank_of=banks) == (
        ReadAfterWrite(reader=1, tag="DS4", writer=0),
    )
    assert reads_after_write(both) == (ReadAfterWrite(reader=1, tag="DS", writer=0, indexed=True),)
    assert reads_after_write([_row(writes={"DS3"}), _row(reads={"DS4"})], bank_of=banks) == ()


def test_shared_reads_are_re_read_by_every_row():
    rows = [_row(reads={"Go"}), _row(writes={"Go"}), _row()]
    assert reads_after_write(rows) == ()
    assert reads_after_write(rows, shared_reads={"Go"}) == (
        ReadAfterWrite(reader=2, tag="Go", writer=1),
    )


# -- the solve-order table ------------------------------------------------------


def _cases() -> list[tuple[str, Case]]:
    return [
        (group.name, c)
        for group in (test_solve_order.group, test_rung_semantics.group)
        for c in group.cases
    ]


@pytest.mark.parametrize(("group", "c"), _cases(), ids=lambda v: getattr(v, "id", v))
def test_flags_exactly_the_order_dependent_cases(group: str, c: Case):
    slots = make_slot(1, name="S")
    with Program() as program:
        emit_case(group, c, slots, 1)
    dependent = test_solve_order.ORDER_DEPENDENT.get(f"{group}/{c.id}", False)
    assert bool(_findings(program)) == dependent


def test_every_order_dependent_case_is_in_the_table():
    keys = {f"{group}/{c.id}" for group, c in _cases()}
    assert {key for key, dependent in test_solve_order.ORDER_DEPENDENT.items() if dependent} <= (
        keys
    )


# -- rows ---------------------------------------------------------------------


def test_branch_below_a_write_is_flagged():
    with Program() as program:
        with Rung(Go):
            latch(A)
            with branch(A):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.code == RUNG_ORDER_DEPENDENT
    assert finding.severity == "warning"
    assert finding.reader == "branch"
    assert finding.tags == ("A",)
    assert finding.target_name == "Main:R1.0"
    assert "with branch(A):" in finding.message
    assert "latch(A)" in finding.message
    assert finding.message.isascii()


def test_trunk_reads_before_its_rows_write():
    with Program() as program:
        with Rung(Go, ~A):
            latch(A)
            with branch(Go):
                out(Y1)
    assert _findings(program) == ()


def test_branch_above_its_writer_is_not_flagged():
    with Program() as program:
        with Rung(Go):
            with branch(A):
                out(Y1)
            with branch(Go):
                latch(A)
    assert _findings(program) == ()


def test_instruction_operands_do_not_count():
    with Program() as program:
        with Rung(Go):
            copy(1, N)
            copy(N, Int("M"))
    assert _findings(program) == ()


def test_edge_contact_below_its_tag_writer_is_flagged():
    with Program() as program:
        with Rung(Go):
            out(A)
            with branch(rise(A)):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("A",)


def test_one_finding_per_reading_row_naming_every_tag():
    with Program() as program:
        with Rung(Go):
            latch(A)
            latch(B)
            with branch(B, A):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("A", "B")


def test_continued_rung_reads_its_chain():
    with Program() as program:
        with Rung(Go):
            latch(A)
        with Rung(A).continued():
            out(Y1)
        with Rung(A):
            out(Y2)
    (finding,) = _findings(program)
    assert finding.reader == "continued"
    assert finding.target_name == "Main:R2"
    assert ".continued():" in finding.message


def test_box_leg_below_a_write_is_flagged():
    with Program() as program:
        with Rung(Go):
            latch(P)
            count_up(Ct, preset=10).reset(P)
    (finding,) = _findings(program)
    assert finding.reader == "box"
    assert finding.tags == ("P",)
    assert ".reset(P)" in finding.message


def test_box_enable_is_the_rung_power_not_a_read():
    with Program() as program:
        with Rung(Go):
            out(A)
            on_delay(Tm, 10)
    assert _findings(program) == ()


def test_status_writes_count():
    with Program() as program:
        with Rung(Go):
            on_delay(Tm, 10)
            with branch(Tm.Done):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == (Tm.Done.name,)


def test_call_writes_what_the_subroutine_writes():
    @subroutine("set_q")
    def set_q():
        with Rung(Go):
            latch(Q)

    with Program() as program:
        with Rung(Go):
            call(set_q)
            with branch(Q):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("Q",)
    assert 'call("set_q")' in finding.message


def test_subroutine_rungs_are_checked():
    @subroutine("sub")
    def sub():
        with Rung(Go):
            latch(A)
            with branch(A):
                out(Y1)

    with Program() as program:
        with Rung():
            call(sub)
    (finding,) = _findings(program)
    assert finding.target_name == "sub:R1.0"


def test_for_body_rungs_read_their_own_contacts_live():
    with Program() as program:
        with Rung():
            with forloop(1):
                with Rung(Go):
                    latch(A)
                with Rung(A):
                    out(Y1)
    assert _findings(program) == ()


def test_box_leg_in_a_for_body_rung_below_a_write_is_flagged():
    with Program() as program:
        with Rung():
            with forloop(1):
                with Rung(Go):
                    latch(P)
                    count_up(Ct, preset=10).reset(P)
    (finding,) = _findings(program)
    assert finding.reader == "box"
    assert "For body rung 1" in finding.message


def test_branch_below_a_for_loop_sees_its_body_writes():
    with Program() as program:
        with Rung(Go):
            with forloop(1):
                latch(A)
            with branch(A):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("A",)
    assert "forloop(1)" in finding.message


# -- indexed rows -----------------------------------------------------------------
#
# A CLICK-sized bank is too large for the program graph to list, so an index no
# declared range bounds stands for any element of its bank.


def _bank() -> Block:
    return Block("DS", TagType.INT, 1, 4500)


def test_indexed_contact_below_a_named_write_is_flagged():
    ds, p = _bank(), Int("P")
    with Program() as program:
        with Rung(Go):
            copy(1, ds[3])
            with branch(ds[p] == 1):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("DS[...]",)
    assert "with branch(DS[P] == 1):" in finding.message
    assert finding.message.isascii()


def test_indexed_contact_below_an_indexed_write_is_flagged():
    ds, p, q = _bank(), Int("P"), Int("Q")
    with Program() as program:
        with Rung(Go):
            copy(1, ds[q])
            with branch(ds[p] == 1):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("DS[...]",)
    assert "copy(1, DS[Q])" in finding.message


def test_named_contact_below_an_indexed_write_is_flagged():
    ds, q = _bank(), Int("Q")
    with Program() as program:
        with Rung(Go):
            copy(1, ds[q])
            with branch(ds[3] == 1):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("DS3",)


def test_named_tags_of_one_bank_stay_apart():
    ds = _bank()
    with Program() as program:
        with Rung(Go):
            copy(1, ds[3])
            with branch(ds[4] == 1):
                out(Y1)
    assert _findings(program) == ()


def test_a_bounded_index_reads_only_its_range():
    ds, p = _bank(), Int("P", min=10, max=12)
    with Program() as program:
        with Rung(Go):
            copy(1, ds[3])
            with branch(ds[p] == 1):
                out(Y1)
        with Rung(Go):
            copy(1, ds[11])
            with branch(ds[p] == 1):
                out(Y2)
    (finding,) = _findings(program)
    assert finding.target_name == "Main:R2.0"
    assert finding.tags == ("DS11",)


def test_indexed_read_inside_an_expression_or_a_leg_is_flagged():
    ds, p = _bank(), Int("P")
    with Program() as program:
        with Rung(Go):
            copy(1, ds[3])
            with branch(ds[p + 1] > 1):
                out(Y1)
        with Rung(Go):
            copy(1, ds[3])
            count_up(Ct, preset=10).reset(ds[p] == 2)
    branch_finding, box_finding = _findings(program)
    assert branch_finding.tags == box_finding.tags == ("DS[...]",)
    assert box_finding.reader == "box"
    assert ".reset(DS[P] == 2)" in box_finding.message


def test_a_mapped_tag_is_an_element_of_its_bank():
    ds, p, speed = _bank(), Int("P"), Int("Speed")
    speed.map_to(ds[5])
    with Program() as program:
        with Rung(Go):
            copy(1, speed)
            with branch(ds[p] == 1):
                out(Y1)
    (finding,) = _findings(program)
    assert finding.tags == ("DS[...]",)


def test_default_check_reports_it():
    with Program() as program:
        with Rung(Go):
            latch(A)
            with branch(A):
                out(Y1)
    assert [f.code for f in program.check().findings] == [RUNG_ORDER_DEPENDENT]
    assert program.check(ignore={RUNG_ORDER_DEPENDENT}).findings == ()
