"""Writes a rung makes only for some values, and the analyses that ask what a rung always writes.

A copy through a pointer writes one of the elements the pointer can reach.  A ``to_text()``
copy of a tag writes as many characters as its value has.  The program graph records these
once (``RungNode.may_writes``, and a read of the value before each in the def-use chains).
Each analysis below must read that record, or it takes a tag the copy left alone for one it
wrote this scan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    call,
    copy,
    subroutine,
    to_text,
)
from pyrung.click import ClickBlocks
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove.passes import (
    _pass_build_graph,
    _pass_classify_dimensions,
    _pass_detect_functional_dependencies,
    _pass_elide_scan_local_state,
    _PassContext,
)


def _through_funcdep(program: Program) -> _PassContext:
    ctx = _PassContext(program=program, dt=0.01)
    _pass_build_graph(ctx)
    _pass_classify_dimensions(ctx)
    _pass_elide_scan_local_state(ctx)
    _pass_detect_functional_dependencies(ctx)
    return ctx


def _two_way_pointer(name: str, first: int = 1) -> Int:
    return Int(name, choices={first: "first", first + 1: "second"})


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_the_graph_records_which_writes_a_rung_may_not_make() -> None:
    regs = Block("RecReg", TagType.INT, 1, 4)
    msg = Block("RecMsg", TagType.CHAR, 1, 10)
    with Program(strict=False) as logic:
        with Rung():
            copy(5, regs[_two_way_pointer("RecPtr")])
        with Rung():
            copy(5, regs[Int("RecOne", choices={3: "only"})])
        with Rung():
            copy(Int("RecSource"), msg[1], convert=to_text())
        with Rung():
            copy(-12, msg[8], convert=to_text())
    pointer, one_way, text, literal = build_program_graph(logic).rung_nodes[:4]

    assert pointer.may_writes == {"RecReg1", "RecReg2"}
    assert pointer.sure_writes == frozenset()
    # A pointer with one value writes its element every time.
    assert one_way.may_writes == frozenset()
    assert one_way.sure_writes == {"RecReg3"}
    assert text.sure_writes == {"RecMsg1"}
    assert text.may_writes == {f"RecMsg{a}" for a in range(2, 7)}
    # A literal's text is the same every scan.
    assert literal.may_writes == frozenset()
    assert literal.sure_writes == {"RecMsg8", "RecMsg9", "RecMsg10"}


def test_a_write_the_rung_also_makes_for_sure_is_sure() -> None:
    regs = Block("BothReg", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung():
            copy(5, regs[_two_way_pointer("BothPtr")])
            copy(6, regs[1])
    (node,) = build_program_graph(logic).rung_nodes
    assert node.may_writes == {"BothReg2"}
    assert node.sure_writes == {"BothReg1"}


# ---------------------------------------------------------------------------
# The def-use chains: unconditional_write_before_read
# ---------------------------------------------------------------------------


def test_a_pointer_write_is_not_a_write_before_read() -> None:
    """``copy(5, ds[Ptr])`` with Ptr 1 or 2 leaves DS1 alone half the time."""
    regs = Block("WbrReg", TagType.INT, 1, 4)
    with Program(strict=False) as logic:
        with Rung():
            copy(5, regs[_two_way_pointer("WbrPtr")])
        with Rung(regs[1] == 5):
            copy(1, Int("WbrSeen"))
    graph = build_program_graph(logic)
    assert not graph.unconditional_write_before_read("WbrReg1")
    # The write may leave the value DS1 came into the scan with, so the write reads it.
    assert graph.def_use_chains["WbrReg1"][0].read_by == {0}


def test_a_text_through_a_pointer_is_not_a_write_before_read() -> None:
    txt = ClickBlocks().txt
    pointer = Int("TextWbrPtr", choices={990: "near the end", 995: "nearer"})
    with Program(strict=False) as logic:
        with Rung():
            copy(Int("TextWbrSource"), txt[pointer], convert=to_text())
        with Rung(txt[990] == "1"):
            copy(1, Int("TextWbrSeen"))
    assert not build_program_graph(logic).unconditional_write_before_read("TXT990")


# ---------------------------------------------------------------------------
# Slice elision: a subroutine's writes before its reads
# ---------------------------------------------------------------------------


def _subroutine_reads_a_kept_element(write: Callable[[], object], kept: Any, mark: Any) -> Program:
    """A rung above the call writes *kept*.  The subroutine's first rung may overwrite it,
    and X follows whether it still holds that value."""
    with Program() as logic:
        with Rung(Bool("SliceCmd", external=True)):
            copy(mark, kept)
        with Rung():
            call("Slice")
        with subroutine("Slice"):
            with Rung():
                write()
            with Rung(kept != mark):
                copy(7, Int("SliceX"))
            with Rung():
                copy(Int("SliceX"), Int("SliceShown"))
    return logic


def test_slice_keeps_a_tag_decided_by_a_character_a_short_text_leaves() -> None:
    txt = ClickBlocks().txt
    source = Int("SliceSource", external=True, choices={1: "one", 2: "two"})
    logic = _subroutine_reads_a_kept_element(
        lambda: copy(source, txt[1], convert=to_text()), txt[5], "4"
    )
    ctx = _through_funcdep(logic)
    assert "SliceX" not in (ctx._elided_tags or {})
    assert "SliceX" in (ctx.stateful_dims or {})


def test_slice_keeps_a_tag_decided_by_an_element_a_pointer_leaves() -> None:
    regs = Block("SliceReg", TagType.INT, 1, 10)
    pointer = Int("SlicePtr", choices={5: "five", 6: "six"})
    logic = _subroutine_reads_a_kept_element(lambda: copy(9, regs[pointer]), regs[5], 4)
    ctx = _through_funcdep(logic)
    assert "SliceX" not in (ctx._elided_tags or {})
    assert "SliceX" in (ctx.stateful_dims or {})


# ---------------------------------------------------------------------------
# Functional dependencies: Y recomputed from X on every scan
# ---------------------------------------------------------------------------


def test_a_copy_through_a_pointer_is_no_functional_dependency() -> None:
    """DS1 follows the source only while the pointer points at it."""
    regs = Block("DepReg", TagType.INT, 1, 4)
    source = Int("DepSource")
    with Program() as logic:
        with Rung(regs[1] == 2):
            copy(1, Int("DepSeen"))
        with Rung(Bool("DepLoad", external=True)):
            copy(Int("DepInput", external=True, min=0, max=3), source)
        with Rung():
            copy(source, regs[_two_way_pointer("DepPtr")])
    ctx = _through_funcdep(logic)
    assert (ctx._elided_tags or {}).get("DepReg1") != "functional_dep"
    assert "DepReg1" in (ctx.stateful_dims or {})


# ---------------------------------------------------------------------------
# Dead writes
# ---------------------------------------------------------------------------


def test_a_text_whose_first_character_is_overwritten_is_not_a_dead_write() -> None:
    """The copy's other characters last, and a later rung reads one of them."""
    txt = ClickBlocks().txt
    with Program(strict=False) as logic:
        with Rung():
            copy(Int("DeadSource"), txt[1], convert=to_text())
        with Rung():
            copy("A", txt[1])
        with Rung(txt[1] == "A", txt[3] == "B"):
            copy(1, Int("DeadSeen"))
    codes = [finding.code for finding in logic.check().findings]
    assert "TAG_DEAD_WRITE" not in codes
