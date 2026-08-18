"""PILOT wait-edge arc, Part 1: ``StaticTransitionEdge.completion`` records the wait's
bearing — the route's charted gate pairs, verbatim.

The tumbler's Starting(3)→Execute(6) transition is a wait edge: Starting stands
until ``State Complete``.  The channel route's recorded condition names the
``Sts_StateCompleteFlag`` coil (main ``R26`` gates the request writer on it);
that pair IS the completion — Part 2's sibling trace descends the coil to its
driving predicate (``Sts_StateCompleteBool == 1``, main ``R25``) as ordinary
transparent ladder, so no producer analysis happens at record time.  No
consumer yet — the field is behavior-inert until Part 2.
"""

from __future__ import annotations

import importlib

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.drive_setup import (
    build_prover_context,
    infer_opaque_pipeline_roles,
)
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.pipeline_graph import (
    build_static_transition_graphs,
    detect_opaque_loop,
)
from pyrung.core.analysis.pilot.program_facts import compute_reference_constants
from pyrung.core.analysis.steerable import compute_steerable


def _compass_graphs(logic, plc):
    """Build the tumbler's compass value-graphs the way PILOT setup does."""
    pdg = build_program_graph(logic)
    harness_fb = install_harness(plc)
    ref = compute_reference_constants(pdg, logic, plc._known_tags_by_name)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic) - harness_fb - ref
    opaque_loop = detect_opaque_loop(pdg, logic)
    prover = build_prover_context(logic, dict(plc.state.tags))
    roles = infer_opaque_pipeline_roles(
        pdg,
        logic,
        steerable,
        opaque_loop,
        prover.evidence,
    )
    return build_static_transition_graphs(
        roles,
        pdg,
        logic,
        steerable,
        opaque_loop,
        prover.evidence,
    )


def test_tumbler_starting_to_execute_records_state_complete_bearing() -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    plc = PLC(logic, dt=0.010)
    graphs = _compass_graphs(logic, plc)

    edge = next(
        edge
        for graph in graphs
        if graph.role.channel_tag == "Sts_StateCurrent"
        for edge in graph.edges
        if edge.action is None and edge.from_value == 3 and edge.to_value == 6
    )
    assert edge.completion == (("Sts_StateCompleteFlag", True),)
