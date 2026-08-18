"""Agreement gate for the three "what's still needed" notions.

PILOT computes "what's still needed" three ways, at three points in the trace
pipeline (see ``pilot/CLAUDE.md`` "Soundness and behavior invariants"):

* **#1** ``frontier_pairs`` — whole-tree residual, AFTER writer selection;
* **#2** ``_projected_guard_frontier`` (via ``_writer_projection``) — per-writer
  ``(counterfactual, frontier)`` in the PROJECTED fire-time overlay;
* **#3** ``_expr_availability`` (via ``_writer_availability``) — per-writer 4-valued
  reachability tier in the LIVE snapshot.

They answer different questions but must never *contradict* each other about the same
writer in the same state.  These tests pin the documented relationships on one shared
burner-shaped SFC fixture, so a future edit that splits them fails here.

The fixture mirrors ``test_projected_backward_enabler.py``: a minimal step
sequencer where ``CurStep`` advances ``1 -> 2`` only via the transition rung (gated
``Trans == 1``), while the even-step bump (gated on parity) is *counterfactual* for
producing ``CurStep == 2`` from the odd step 1.
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, calc, copy
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.pilot.availability import _writer_availability, _WriterAvailability
from pyrung.core.analysis.pilot.trace import (
    _written_value_for_tag,
    trace_back,
)
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.sp_values import _writer_projection
from pyrung.core.analysis.steerable import compute_steerable


def _build_sfc() -> Program:
    CurStep = Int("CurStep")
    Trans = Int("Trans")
    valstepisodd = Int("valstepisodd")
    fb = Bool("fb", external=True)
    with Program(strict=False) as prog:
        with Rung(CurStep == 1, fb):  # rung 0: Trans set ONLY from CurStep==1
            copy(1, Trans)
        with Rung(Trans == 1):  # rung 1: transition increment
            calc(CurStep + 1, CurStep)
            copy(0, Trans)
        with Rung():  # rung 2: parity derive
            calc(CurStep % 2, valstepisodd)
        with Rung(valstepisodd != 1):  # rung 3: even-step neutral bump
            calc(CurStep + 1, CurStep)
    return prog


def _rung(prog, pdg, rung_index):
    for node in pdg.rung_nodes:
        if node.rung_index == rung_index and node.subroutine is None:
            return node, resolve_rung(prog, node)
    raise AssertionError(f"no main rung {rung_index}")


def _steerable(prog, pdg):
    plc = PLC(prog, dt=0.010)
    return frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))


def _trace(prog, pdg, snap, steerable):
    return trace_back(
        "CurStep",
        2,
        snap,
        pdg,
        prog,
        steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        prior=None,
    )


def test_live_prerequisite_agrees_across_all_three() -> None:
    """The transition writer's ``Trans`` prerequisite is the SAME need seen from all
    three altitudes: #2 returns it as a non-pinned frontier tag, #3 classifies its
    guard ``AFTER_PREREQ``, and #1 surfaces ``("Trans", 1)`` in the whole-tree
    frontier."""
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    steerable = _steerable(prog, pdg)
    snap = {"CurStep": 1, "Trans": 0, "valstepisodd": 1}

    trans_rn, trans_ro = _rung(prog, pdg, 1)
    wv = _written_value_for_tag(trans_ro, "CurStep")

    # #2 — projected, per-writer: not a dead branch, Trans is the open prerequisite.
    proj = _writer_projection(trans_ro, "CurStep", 2, snap, pdg, prog, {}, frozenset())
    assert proj == (False, ["Trans"])

    # #3 — live, per-writer: the guard Trans==1 is a real, non-steerable prerequisite.
    avail = _writer_availability(
        trans_ro, trans_rn, wv, "CurStep", 2, snap, pdg, prog, steerable, frozenset(), False
    )
    assert avail == _WriterAvailability.AFTER_PREREQ

    # #1 — whole tree: the same prerequisite one level down surfaces as a frontier pair.
    tree = _trace(prog, pdg, snap, steerable)
    pairs = frontier_pairs(tree, snap)
    assert ("Trans", 1) in pairs
    # And the chosen writer's tier is stamped worst-wins onto the tree node.
    assert tree.writer_availability == _WriterAvailability.AFTER_PREREQ


def test_counterfactual_writer_is_unavailable() -> None:
    """The composition pin: #2 ``counterfactual`` True MUST drive #3 to
    ``UNAVAILABLE_FROM_HERE`` — they are layered halves ("dead branch?" then "how
    far?"), never independent verdicts that could disagree."""
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    steerable = _steerable(prog, pdg)
    snap = {"CurStep": 1, "Trans": 0, "valstepisodd": 1}

    even_rn, even_ro = _rung(prog, pdg, 3)
    wv = _written_value_for_tag(even_ro, "CurStep")

    proj = _writer_projection(even_ro, "CurStep", 2, snap, pdg, prog, {}, frozenset())
    assert proj is not None
    is_counterfactual = proj[0]
    assert is_counterfactual is True  # parity: CurStep==1 is odd, can't fire the even bump

    avail = _writer_availability(
        even_ro,
        even_rn,
        wv,
        "CurStep",
        2,
        snap,
        pdg,
        prog,
        steerable,
        frozenset(),
        is_counterfactual,
    )
    assert avail == _WriterAvailability.UNAVAILABLE_FROM_HERE


def test_satisfied_guard_needs_nothing_across_all_three() -> None:
    """A writer whose guard is fully satisfied in the snapshot: #3 =
    ``AVAILABLE_NOW``, #2 = ``(False, [])`` (no dead branch, no open prereq), and its
    guard tag is ABSENT from #1 ``frontier_pairs``.  All three agree "nothing to do
    here"."""
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    steerable = _steerable(prog, pdg)
    # Trans already 1 — the transition guard holds outright.
    snap = {"CurStep": 1, "Trans": 1, "valstepisodd": 1}

    trans_rn, trans_ro = _rung(prog, pdg, 1)
    wv = _written_value_for_tag(trans_ro, "CurStep")

    proj = _writer_projection(trans_ro, "CurStep", 2, snap, pdg, prog, {}, frozenset())
    assert proj == (False, [])

    avail = _writer_availability(
        trans_ro, trans_rn, wv, "CurStep", 2, snap, pdg, prog, steerable, frozenset(), False
    )
    assert avail == _WriterAvailability.AVAILABLE_NOW

    tree = _trace(prog, pdg, snap, steerable)
    pairs = frontier_pairs(tree, snap)
    tags = {t for t, _v in pairs}
    assert "Trans" not in tags  # the satisfied prerequisite never surfaces as a need
