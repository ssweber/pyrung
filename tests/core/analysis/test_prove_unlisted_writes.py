"""Writes through a pointer that list no targets, and the checks that read "no writer".

A write through a pointer with no declared bounds, into a block of more than 1000 elements
(``_INDIRECT_BLOCK_CAP`` in pdg.py), names none of its targets.  ``writers_of`` leaves it
out.  ``ProgramGraph.unlisted_writers_of`` names it for every element the pointer can
reach, so no check reads such an element as a constant or an input.  The pointer reaches
its power-on value and what its writers store, under their rungs' comparisons.
"""

from __future__ import annotations

from typing import Any

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    branch,
    copy,
    fill,
    latch,
    math,
    out,
    run_enabled_function,
)
from pyrung.click import ClickBlocks
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import passes
from pyrung.core.analysis.prove.classify import collect_produced_value_domains
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.tag import TagType
from pyrung.core.validation import CMP_OPERAND_NO_WRITER, validate
from pyrung.core.validation.stuck_bits import validate_stuck_bits

WRITE = Bool("Write", external=True)
AT = Int("At", external=True)
SOURCE = Int("Source", external=True, choices={0: "zero", 100: "hundred"})


def _program(dest: Any, element: Any) -> Program:
    with Program(strict=False) as prog:
        with Rung(WRITE):
            copy(SOURCE, dest)
        with Rung(element > 50):
            out(Bool("High"))
    return prog


def _writer_nodes(prog: Program, name: str) -> tuple[frozenset[int], frozenset[int]]:
    graph = build_program_graph(prog)
    return graph.writers_of.get(name, frozenset()), graph.unlisted_writers_of(name)


def test_a_pointer_with_no_bounds_lists_no_target_in_a_big_block() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    listed, unlisted = _writer_nodes(_program(big[AT], big[5]), "Big5")
    assert not listed
    assert unlisted
    assert build_program_graph(_program(big[AT], big[5])).may_be_written("Big5")


def test_a_window_and_an_index_sum_list_no_target_either() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    offset = Int("Offset", external=True, min=0, max=3)
    bounded = Int("Bounded", external=True, min=1, max=10)
    for dest in (big[bounded + offset], big.select(bounded, bounded + 1)):
        _listed, unlisted = _writer_nodes(_program(dest, big[5]), "Big5")
        assert unlisted


def test_declared_bounds_and_a_small_block_list_their_targets() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    small = Block("Small", TagType.INT, 1, 100)
    bounded = Int("Bounded", external=True, min=1, max=10)
    for dest, element, name in (
        (big[bounded], big[5], "Big5"),
        (small[AT], small[5], "Small5"),
    ):
        listed, unlisted = _writer_nodes(_program(dest, element), name)
        assert listed
        assert not unlisted


def test_a_click_bank_slot_is_reached_too() -> None:
    ds = ClickBlocks().ds
    _listed, unlisted = _writer_nodes(_program(ds[AT], ds[100]), "DS100")
    assert unlisted


def test_a_reached_element_has_no_domain_and_no_copy_takes_one() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    shown = Int("Shown")
    with Program(strict=False) as prog:
        with Rung(Bool("Set", external=True)):
            copy(3, big[5])
        with Rung(WRITE):
            copy(SOURCE, big[AT])
        with Rung(Bool("Show", external=True)):
            copy(big[5], shown)
        with Rung(shown > 50):
            out(Bool("ShownHigh"))
    ctx = passes._PassContext(program=prog, dt=0.01)
    prior = passes._run_prior_pipeline(ctx)
    assert "Big5" not in prior.stateful_dims
    assert "Big5" not in prior.nondeterministic_dims
    assert "Big5" in ctx._unclassified_written
    assert prior.stateful_dims.get("Shown") != (0, 3, 50)
    produced = collect_produced_value_domains(prog, build_program_graph(prog))
    assert "Big5" not in produced
    assert "Shown" not in produced


def test_a_reached_element_is_no_lever() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    prog = _program(big[AT], big[5])
    graph = build_program_graph(prog)
    assert "Big5" not in compute_steerable(graph, graph.tags, prog)


def test_check_does_not_call_a_reached_element_unwritten() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    limit = Int("Limit", external=True)
    with Program(strict=False) as prog:
        with Rung(WRITE):
            copy(SOURCE, big[AT])
        with Rung(limit >= big[5]):
            out(Bool("Reached"))
    findings = validate(prog, select={CMP_OPERAND_NO_WRITER})
    assert not [f for f in findings if f.code == CMP_OPERAND_NO_WRITER]


def test_a_latch_a_pointer_write_may_clear_is_not_stuck() -> None:
    bits = Block("Bits", TagType.BOOL, 1, 2000)
    with Program(strict=False) as prog:
        with Rung(Bool("Arm", external=True)):
            latch(bits[5])
        with Rung(WRITE):
            copy(Bool("Clear", external=True), bits[AT])
        with Rung(bits[5]):
            out(Bool("Armed"))
    assert not validate_stuck_bits(prog).findings


def test_a_fill_window_reaches_every_element() -> None:
    big = Block("Big", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with Rung(WRITE):
            fill(SOURCE, big.select(AT, AT + 1))
        with Rung(big[5] > 50):
            out(Bool("High"))
    graph = build_program_graph(prog)
    assert graph.unlisted_writers_of("Big5")
    assert "Big5" in graph.unlisted_written()


def _acknowledge(read_at: int, *conditions: Any, pointer: Any = None) -> Program:
    """The tumbler's write: ``math(Cmd * 10 + 2999, At)``, then a copy through ``At``."""
    big = Block("Ack", TagType.INT, 1, 4500)
    cmd = Int("Cmd", external=True)
    at = pointer if pointer is not None else Int("AckAt")
    shown = Int("Shown")
    with Program(strict=False) as prog:
        with Rung(Bool("Set", external=True)):
            copy(3, big[read_at])
        with Rung(*(conditions or (cmd >= 1, cmd <= 100))):
            math(cmd * 10 + 2999, at)
            copy(SOURCE, big[at])
        with Rung(Bool("Show", external=True)):
            copy(big[read_at], shown)
        with Rung(shown > 50):
            out(Bool("ShownHigh"))
    return prog


def _reach(prog: Program) -> frozenset[int] | None:
    (block_writes,) = build_program_graph(prog).unlisted_writes.values()
    ((_node, reach),) = block_writes
    return reach


def test_a_rung_comparison_bounds_what_a_pointer_reaches() -> None:
    assert _reach(_acknowledge(5)) == frozenset(range(3009, 4000, 10))


def test_an_element_out_of_reach_keeps_its_domain() -> None:
    def shown(read_at: int) -> Any:
        prog = _acknowledge(read_at)
        prior = passes._run_prior_pipeline(passes._PassContext(program=prog, dt=0.01))
        return prior.stateful_dims.get("Shown")

    assert shown(5) == (0, 3, 50)
    assert shown(3009) is None


def test_an_or_bounds_nothing() -> None:
    cmd = Int("Cmd", external=True)
    assert _reach(_acknowledge(5, Or(cmd == 1, cmd == 2))) is None


def test_a_write_earlier_in_the_rung_drops_the_bound() -> None:
    cmd = Int("Cmd", external=True)
    big = Block("Ack", TagType.INT, 1, 4500)
    at = Int("AckAt")
    with Program(strict=False) as prog:
        with Rung(cmd >= 1, cmd <= 100):
            copy(Int("Other", external=True), cmd)
            math(cmd * 10 + 2999, at)
            copy(SOURCE, big[at])
    assert _reach(prog) is None


def test_a_pointer_set_from_outside_is_not_narrowed() -> None:
    cmd = Int("Cmd", external=True)
    outside = Int("OutsideAt", external=True)
    assert _reach(_acknowledge(5, cmd >= 1, cmd <= 100, pointer=outside)) is None


def test_an_address_outside_the_block_writes_nothing() -> None:
    cmd = Int("Cmd", external=True)
    # Cmd 0 makes the pointer 2999.  Cmd 200 makes it 4999, past the block's 4500.
    assert _reach(_acknowledge(5, cmd >= 0, cmd <= 200)) == frozenset(range(2999, 4500, 10))


def test_a_branch_keeps_the_bounds_of_its_rung() -> None:
    cmd = Int("Cmd", external=True)
    big = Block("Ack", TagType.INT, 1, 4500)
    at = Int("AckAt")
    with Program(strict=False) as prog:
        with Rung(cmd >= 1):
            with branch(cmd <= 100):
                math(cmd * 10 + 2999, at)
                copy(SOURCE, big[at])
    assert _reach(prog) == frozenset(range(3009, 4000, 10))


def test_a_ladder_write_past_a_declared_bound_still_counts() -> None:
    # A CLICK stores 50 in At, past its max of 10.  pyrung stores it too and only reports it.
    # A plain big[At] lists 0..10 by the declared bounds (_indirect_ref_tags), so this
    # writes through an index expression, which lists no targets.
    big = Block("Big", TagType.INT, 1, 2000)
    at = Int("BoundedAt", min=0, max=10)
    with Program(strict=False) as prog:
        with Rung(Bool("SetAt", external=True)):
            math(SOURCE * 0 + 50, at)
        with Rung(WRITE):
            copy(SOURCE, big[at + 1])
        with Rung(big[51] > 50):
            out(Bool("High"))
    _listed, unlisted = _writer_nodes(prog, "Big51")
    assert unlisted


def test_a_pointer_listed_by_its_choices_reaches_only_those() -> None:
    # The graph lists Listed's two elements from its choices.  The narrowing reads the
    # same two, so the copy through Listed cannot widen AckAt, which lives in the block.
    big = Block("Ack", TagType.INT, 1, 4500)
    cmd = Int("Cmd", external=True)
    listed = Int("Listed", choices={10: "ten", 20: "twenty"})
    at = big[100]
    with Program(strict=False) as prog:
        with Rung():
            math(Int("Anything", external=True) * 1, listed)
            copy(SOURCE, big[listed])
        with Rung(cmd >= 1, cmd <= 100):
            math(cmd * 10 + 2999, at)
            copy(SOURCE, big[at])
    assert _reach(prog) == frozenset(range(3009, 4000, 10))


def _stamp(enabled: bool) -> dict[str, int]:
    return {"v": 77}


def test_a_write_on_a_false_rung_gets_no_bound() -> None:
    # run_enabled_function() writes its outs on every scan, its rung false or not.  So
    # P >= 1, P <= 10 does not bound P when it writes, and P 500 stores into Big500.
    big = Block("Big", TagType.INT, 1, 2000)
    p = Int("P", default=1)
    with Program(strict=False) as prog:
        with Rung(Bool("Go", external=True)):
            math(Int("Src", external=True) * 1, p)
        with Rung(p >= 1, p <= 10):
            run_enabled_function(_stamp, outs={"v": big[p]})
        with Rung(big[500] > 50):
            out(Bool("High"))
    _listed, unlisted = _writer_nodes(prog, "Big500")
    assert unlisted


def _mapped_onto_a_bank() -> Program:
    bank = Block("Bank", TagType.INT, 1, 2000)
    cmd = Int("MappedCmd")
    cmd.map_to(bank[100])
    logical = Block("Logi", TagType.INT, 1, 10)
    logical.map_to(bank.select(1001, 1010))
    with Program(strict=False) as prog:
        with Rung(WRITE):
            copy(SOURCE, bank[AT])
        with Rung(Bool("Set", external=True)):
            copy(3, cmd)
            copy(3, logical[5])
        with Rung(Bool("Show", external=True)):
            copy(cmd, Int("Shown"))
            copy(logical[5], Int("Shown2"))
        with Rung(Int("Shown") > 50):
            out(Bool("High"))
        with Rung(Int("Shown2") > 50):
            out(Bool("High2"))
    return prog


def test_a_tag_mapped_onto_a_bank_slot_is_reached() -> None:
    prog = _mapped_onto_a_bank()
    graph = build_program_graph(prog)
    assert graph.unlisted_writers_of("MappedCmd")
    assert graph.unlisted_writers_of("Logi5")

    def facts() -> tuple[Any, ...]:
        prog._cached_graph = None
        prior = passes._run_prior_pipeline(passes._PassContext(program=prog, dt=0.01))
        return tuple(prior.stateful_dims.get(name) for name in ("Shown", "Shown2"))

    before = facts()
    assert before == (None, None)
    plc = PLC(prog, dt=0.01)
    plc.patch({"Write": True, "At": 100, "Source": 100})
    plc.step()
    plc.patch({"At": 1005})
    plc.step()
    assert plc.current_state.tags["MappedCmd"] == 100
    assert plc.current_state.tags["Logi5"] == 100
    assert facts() == before


def test_a_rest_value_holds_no_claim_on_an_element_a_pointer_may_write() -> None:
    from pyrung.core.analysis.pilot import program_facts

    big = Block("Big", TagType.INT, 1, 2000)
    element = big[5]
    with Program(strict=False) as prog:
        with Rung(Bool("Go", external=True)):
            copy(7, element)
        with Rung(element == 7):
            out(Bool("Seven"))
        with Rung():
            copy(0, element)
        with Rung(WRITE):
            copy(SOURCE, big[AT])
        with Rung(element > 50):
            out(Bool("High"))
    graph = build_program_graph(prog)
    assert program_facts.scan_transient_rest("Big5", graph, prog) == (False, None)


def test_a_tag_mapped_onto_two_slots_is_reached_at_either() -> None:
    # map_to keeps the last slot on the tag, and the bank keeps both.  The write reaches
    # only Bank100, the first.
    bank = Block("Bank", TagType.INT, 1, 2000)
    twice = Int("Twice")
    twice.map_to(bank[100])
    twice.map_to(bank[200])
    at = Int("TwiceAt")
    with Program(strict=False) as prog:
        with Rung():
            math(Int("Ext", external=True) * 1, at)
        with Rung(WRITE, at >= 100, at <= 100):
            copy(SOURCE, bank[at])
        with Rung(twice > 50):
            out(Bool("High"))
    assert _reach(prog) == frozenset({100})
    _listed, unlisted = _writer_nodes(prog, "Twice")
    assert unlisted


def test_a_plain_tag_named_like_a_slot_is_reached() -> None:
    # The runtime stores bank[100] under the name Bank100, which this tag has too.
    bank = Block("Bank", TagType.INT, 1, 2000)
    same = Int("Bank100")
    with Program(strict=False) as prog:
        with Rung(WRITE):
            copy(SOURCE, bank[AT])
        with Rung(same > 50):
            out(Bool("High"))
    _listed, unlisted = _writer_nodes(prog, "Bank100")
    assert unlisted


def test_a_huge_block_finds_a_tag_by_its_slot_name_without_a_walk() -> None:
    huge = Block("Huge", TagType.INT, 1, 5_000_000)
    huge.slot(9, name="Renamed")
    plain = Int("Huge5")
    renamed = Int("Renamed")
    with Program(strict=False) as prog:
        with Rung(WRITE):
            copy(SOURCE, huge[AT])
            copy(1, plain)
            copy(1, renamed)
            copy(1, Int("Huge9"))
            copy(1, Int("Elsewhere"))
    graph = build_program_graph(prog)
    assert graph.tag_positions("Huge5", (huge,)) == {(huge, 5)}
    assert graph.tag_positions("Renamed", (huge,)) == {(huge, 9)}
    assert not graph.tag_positions("Huge9", (huge,))
    assert not graph.tag_positions("Elsewhere", (huge,))
    assert graph.unlisted_writers_of("Huge5")
    assert not graph.unlisted_writers_of("Elsewhere")


def test_a_huge_block_with_a_formatter_counts_every_tag_as_reached() -> None:
    from pyrung.core.analysis.value_sets import _MAX_BLOCK_SCAN

    huge = Block(
        "Huge", TagType.INT, 1, _MAX_BLOCK_SCAN + 1, address_formatter=lambda b, a: f"{b}_{a}"
    )
    graph = build_program_graph(_program(huge[AT], huge[5]))
    assert graph.unlisted_writers_of("Huge_5")
    assert graph.unlisted_writers_of("Source")
    edge = Block("Edge", TagType.INT, 1, _MAX_BLOCK_SCAN, address_formatter=lambda b, a: f"{b}_{a}")
    exact = build_program_graph(_program(edge[AT], edge[5]))
    assert exact.unlisted_writers_of("Edge_5")
    assert not exact.unlisted_writers_of("Source")


def _unbounded(prog: Program) -> Any:
    return build_program_graph(prog, unbounded_index_reads=True)


def test_an_unbounded_read_sees_a_plain_tag_named_like_a_slot() -> None:
    for pointer_bounded in (False, True):
        big = Block("Big", TagType.INT, 1, 2000)
        plain = Int("Big5")
        ptr = Int("Ptr") if pointer_bounded else Int("Ptr", external=True)
        with Program(strict=False) as prog:
            if pointer_bounded:
                with Rung():
                    copy(5, ptr)
            with Rung(WRITE):
                copy(42, plain)
            with Rung():
                copy(big[ptr], Int("Dest"))
        graph = _unbounded(prog)
        assert ("Big5",) in graph.indirect_read_reach.values()


def test_a_rename_after_the_first_lookup_gives_the_new_answer() -> None:
    big = Block("Big", TagType.INT, 1, 5000)
    big.slot(9, name="First")
    graph = build_program_graph(_program(big[AT], big[5]))
    assert graph.tag_positions("First", (big,)) == {(big, 9)}
    assert graph.tag_positions("Big9", (big,)) == frozenset()
    big.slot(9).reset()
    big.slot(9, name="Second")
    big.slot(12, name="First")
    assert graph.tag_positions("First", (big,)) == {(big, 12)}
    assert graph.tag_positions("Second", (big,)) == {(big, 9)}
