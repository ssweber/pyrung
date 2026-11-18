"""A ``return_early()`` anywhere in a subroutine rung guards what it can skip.

A return leaves the subroutine wherever it sits: in the rung's own items, in
a branch at any depth, or in a forloop body.  Everything after it is skipped,
the later rungs and the rest of its own rung, branches included, and an
``out()`` it skips holds its value instead of following its contacts.  The
program dependence graph used to see only a return in a rung's own items and
guard only top-level nodes of later rungs, so a return in a branch, or a
writer in a branch, dropped the guard's input from the writer's upstream
slice.  A coil no rung reads was taken for a function of its contacts however
often its rung was skipped (behind a return, in a subroutine not called every
scan, in a forloop body).
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import (
    And,
    Bool,
    Int,
    Program,
    branch,
    build_program_graph,
    call,
    copy,
    forloop,
    latch,
    out,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

Btn = Bool("Btn", external=True)
G = Bool("G", external=True)
C = Bool("C", external=True)
Y2 = Bool("Y2")
Y3 = Bool("Y3")
X = Bool("X")
Z = Bool("Z")
DQ = Int("DQ", external=True)

_BOOL = (False, True)


def _y3_without_btn(tags: Any) -> bool:
    return tags["Y3"] and not tags["Btn"]


def _y3_with_g(tags: Any) -> bool:
    return tags["Y3"] and tags["G"]


def _y3(tags: Any) -> bool:
    return tags["Y3"]


# --- shapes ----------------------------------------------------------------
# Each returns (program, input space, property, predicate).  The predicate is
# reachable on a concrete PLC in every shape here.


def _return_in_branch():
    """The guard is a branch of an otherwise unconditional rung."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung():
                with branch(G):
                    return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _return_in_branch_reader_apart():
    """As above, with no main rung sharing the writer's rung index."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Btn):
            out(Z)
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung():
                with branch(G):
                    return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _writer_in_branch():
    """A top-level guard skips a writer that sits in a branch."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung(G):
                return_early()
            with rung():
                with branch(Btn):
                    out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _top_level_guard_reader_apart():
    """A top-level guard and writer, no rung-index coincidence to hide behind."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Btn):
            out(Z)
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung(G):
                return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _return_in_nested_branch():
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung():
                with branch(Btn):
                    with branch(G):
                        return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, G), _y3_with_g


def _return_in_branch_of_conditioned_rung():
    """The return's path reads the rung's contact and the branch's."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung(C):
                with branch(G):
                    return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL, "C": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _return_before_writer_same_rung():
    """A branch return ahead of the rung's own out(): when it fires the coil holds."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung(Btn):
                with branch(G):
                    return_early()
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, G), _y3_with_g


def _return_before_branch_writer_same_rung():
    """A comparison guard in one branch skips the writer in the next."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with rung(DQ == 3):
            out(Z)
        with subroutine("work"):
            with rung():
                with branch(DQ > 5):
                    return_early()
                with branch(Btn):
                    out(Y2)
    return logic, {"Btn": _BOOL, "DQ": (0, 3, 6)}, And(Y3, ~Btn), _y3_without_btn


def _return_in_forloop_body():
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Y2):
            out(Y3)
        with subroutine("work"):
            with rung():
                with branch(G):
                    with forloop(1):
                        return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _return_in_nested_subroutine_branch():
    """A return in the inner subroutine's branch skips the inner writer."""
    with Program(strict=False) as logic:
        with rung():
            call("outer")
        with rung(Y2):
            out(Y3)
        with subroutine("outer"):
            with rung():
                call("inner")
        with subroutine("inner"):
            with rung():
                with branch(G):
                    return_early()
            with rung():
                with branch(Btn):
                    out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _guarded_call():
    """A return in the caller skips the call, so the callee's writer holds."""
    with Program(strict=False) as logic:
        with rung():
            call("outer")
        with rung(Y2):
            out(Y3)
        with subroutine("outer"):
            with rung():
                with branch(G):
                    return_early()
            with rung():
                call("inner")
        with subroutine("inner"):
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _guard_shares_writer_contact():
    """The guard and the writer read the same contact.

    Stripping the guard's reads from the writer's must not strip the
    writer's own: ``latch(X)`` is conditional, so X is no scan-local scratch.
    """
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(Btn):
            latch(X)
        with subroutine("work"):
            with rung(G):
                return_early()
            with rung(G):
                latch(X)
            with rung(X):
                out(Y3)
    return logic, {"Btn": _BOOL, "G": _BOOL}, Y3, _y3


def _guarded_write_read_after_call():
    """A guarded write in an always-run subroutine, read in main after the call."""
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(X):
            out(Y3)
        with subroutine("work"):
            with rung(G):
                return_early()
            with rung():
                copy(Btn, X)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y3, ~Btn), _y3_without_btn


def _y2_without_btn(tags: Any) -> bool:
    return tags["Y2"] and not tags["Btn"]


# Unread coils: nothing in the program reads Y2, only the property does, so
# the coil reaches the unread-tag rule rather than the OTE rule.


def _unread_coil_behind_branch_return():
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with subroutine("work"):
            with rung():
                with branch(G):
                    return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y2, ~Btn), _y2_without_btn


def _unread_coil_behind_return():
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with subroutine("work"):
            with rung(G):
                return_early()
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y2, ~Btn), _y2_without_btn


def _unread_branch_coil_after_branch_return():
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with subroutine("work"):
            with rung():
                with branch(G):
                    return_early()
                with branch(Btn):
                    out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y2, ~Btn), _y2_without_btn


def _unread_coil_in_conditional_call():
    """No return at all: the subroutine is not called on every scan."""
    with Program(strict=False) as logic:
        with rung(G):
            call("work")
        with subroutine("work"):
            with rung(Btn):
                out(Y2)
    return logic, {"Btn": _BOOL, "G": _BOOL}, And(Y2, ~Btn), _y2_without_btn


def _unread_coil_in_forloop_body():
    """A false For rung skips its body, so the coil holds."""
    with Program(strict=False) as logic:
        with rung(Btn):
            with forloop(1):
                out(Y2)
    return logic, {"Btn": _BOOL}, And(Y2, ~Btn), _y2_without_btn


_SHAPES = {
    "unread_coil_behind_branch_return": _unread_coil_behind_branch_return,
    "unread_coil_behind_return": _unread_coil_behind_return,
    "unread_branch_coil_after_branch_return": _unread_branch_coil_after_branch_return,
    "unread_coil_in_conditional_call": _unread_coil_in_conditional_call,
    "unread_coil_in_forloop_body": _unread_coil_in_forloop_body,
    "return_in_branch": _return_in_branch,
    "return_in_branch_reader_apart": _return_in_branch_reader_apart,
    "writer_in_branch": _writer_in_branch,
    "top_level_guard_reader_apart": _top_level_guard_reader_apart,
    "return_in_nested_branch": _return_in_nested_branch,
    "return_in_branch_of_conditioned_rung": _return_in_branch_of_conditioned_rung,
    "return_before_writer_same_rung": _return_before_writer_same_rung,
    "return_before_branch_writer_same_rung": _return_before_branch_writer_same_rung,
    "return_in_forloop_body": _return_in_forloop_body,
    "return_in_nested_subroutine_branch": _return_in_nested_subroutine_branch,
    "guarded_call": _guarded_call,
    "guard_shares_writer_contact": _guard_shares_writer_contact,
    "guarded_write_read_after_call": _guarded_write_read_after_call,
}


# --- graph -----------------------------------------------------------------


@pytest.mark.parametrize(
    "shape",
    [
        "return_in_branch",
        "writer_in_branch",
        "return_in_nested_branch",
        "return_before_writer_same_rung",
        "return_in_forloop_body",
        "return_in_nested_subroutine_branch",
        "guarded_call",
    ],
)
def test_upstream_slice_of_skipped_writer_reads_the_guard(shape: str) -> None:
    logic, *_ = _SHAPES[shape]()
    graph = build_program_graph(logic)
    assert "G" in graph.upstream_slice("Y2")
    assert "G" in graph.upstream_slice("Y3")


def test_writer_in_branch_carries_the_guard() -> None:
    logic, *_ = _writer_in_branch()
    graph = build_program_graph(logic)
    writer = next(n for n in graph.rung_nodes if "Y2" in n.writes)
    assert writer.branch_path == (0,)
    assert writer.guard_reads == frozenset({"G"})
    assert writer.condition_reads == frozenset({"Btn", "G"})
    assert writer.own_condition_reads == frozenset({"Btn"})


def test_call_after_a_return_carries_the_guard() -> None:
    logic, *_ = _guarded_call()
    graph = build_program_graph(logic)
    site = next(n for n in graph.rung_nodes if n.calls == ("inner",))
    assert site.guard_reads == frozenset({"G"})
    assert site.own_condition_reads == frozenset()


def test_out_before_a_return_in_its_rung_is_not_guarded() -> None:
    # out(X) runs before the return in its own rung, so it follows C as a
    # level and X stays combinational.
    with Program(strict=False) as logic:
        with rung():
            call("work")
        with rung(X):
            out(Y3)
        with subroutine("work"):
            with rung(C):
                out(X)
                return_early()
            with rung(Btn):
                out(Y2)

    graph = build_program_graph(logic)
    writer = next(n for n in graph.rung_nodes if "X" in n.writes)
    assert writer.guard_reads == frozenset()
    later = next(n for n in graph.rung_nodes if "Y2" in n.writes)
    assert later.guard_reads == frozenset({"C"})
    dims = _classify_dimensions(logic)
    assert not isinstance(dims, Intractable)
    _stateful, _nd, combinational, *_ = dims
    assert "X" in combinational


def test_guarded_write_is_not_scan_local_outside_its_subroutine() -> None:
    logic, *_ = _guarded_write_read_after_call()
    assert not build_program_graph(logic).unconditional_write_before_read("X")


def test_guard_reads_do_not_hide_the_writers_own_contact() -> None:
    logic, *_ = _guard_shares_writer_contact()
    graph = build_program_graph(logic)
    writer = next(n for n in graph.rung_nodes if n.subroutine == "work" and "X" in n.writes)
    assert writer.guard_reads == frozenset({"G"})
    assert writer.own_condition_reads == frozenset({"G"})
    assert not graph.unconditional_write_before_read("X")
