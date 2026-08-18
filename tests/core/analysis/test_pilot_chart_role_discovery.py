"""Static chart discovery remains separate from opaque Trace ownership."""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Bool, Int, Program, call, copy, rung, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.compass import NavigationCatalog
from pyrung.core.analysis.pilot.evidence import (
    TransitionEvidence,
    discover_chart_roles,
    selected_chart_producer_guard_rungs,
)
from pyrung.core.analysis.pilot.pipeline_graph import build_static_transition_graphs
from pyrung.core.analysis.pilot.program_facts import compute_reference_constants
from pyrung.core.analysis.steerable import compute_steerable

ChartForward = Bool("ChartDiscoveryForward", external=True)
ChartReverse = Bool("ChartDiscoveryReverse", external=True)
ChartUnused = Bool("ChartDiscoveryUnused", external=True)
ChartStepper = Int("ChartDiscoveryStepper")
ChartReported = Int("ChartDiscoveryReported")

with Program() as chart_program:
    with rung(ChartStepper == 0, ChartForward):
        copy(1, ChartStepper)
    with rung(ChartStepper == 1, ChartReverse):
        copy(0, ChartStepper)
    with rung():
        copy(ChartStepper, ChartReported)


def _chart_parts() -> tuple[Any, Any, frozenset[str], TransitionEvidence]:
    plc = PLC(chart_program)
    pdg = build_program_graph(chart_program)
    constants = compute_reference_constants(
        pdg,
        chart_program,
        plc._known_tags_by_name,
    )
    steerable = compute_steerable(pdg, plc._known_tags_by_name, chart_program) - constants
    evidence = TransitionEvidence(
        functional_deps={},
        elided=frozenset(),
        stepping=frozenset((ChartReported.name, ChartUnused.name, ChartStepper.name)),
    )
    return plc, pdg, steerable, evidence


def test_discovers_direct_requestless_stepper_from_prover_evidence() -> None:
    _plc, pdg, steerable, evidence = _chart_parts()

    roles = discover_chart_roles(
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )

    assert [role.channel_tag for role in roles] == [ChartStepper.name, ChartUnused.name]
    assert roles[0].request_tags == frozenset()
    assert roles[0].observation_tags == frozenset((ChartReported.name,))
    graphs = build_static_transition_graphs(
        roles,
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )
    assert len(graphs) == 1
    assert {edge.to_value for edge in graphs[0].edges} == {0, 1}
    catalog = NavigationCatalog(chart_graphs=graphs)
    assert catalog.graphs == ()
    assert catalog.action_tags == frozenset()


def test_chart_discovery_fails_closed_without_prover_evidence() -> None:
    _plc, pdg, steerable, _evidence = _chart_parts()

    assert (
        discover_chart_roles(
            pdg,
            chart_program,
            steerable,
            frozenset(),
            None,
        )
        == ()
    )


def test_chart_discovery_and_graph_build_are_deterministic() -> None:
    _plc, pdg, steerable, evidence = _chart_parts()

    first_roles = discover_chart_roles(
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )
    second_roles = discover_chart_roles(
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )
    assert first_roles == second_roles
    first_graphs = build_static_transition_graphs(
        first_roles,
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )
    second_graphs = build_static_transition_graphs(
        second_roles,
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )

    def identities(graphs: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(
            (
                graph.role.channel_tag,
                graph.role.request_tags,
                tuple(edge.identity for edge in graph.edges),
            )
            for graph in graphs
        )

    assert identities(first_graphs) == identities(second_graphs)


def test_graph_dedupe_preserves_distinct_role_evidence() -> None:
    _plc, pdg, steerable, evidence = _chart_parts()

    graphs = build_static_transition_graphs(
        discover_chart_roles(pdg, chart_program, steerable, frozenset(), evidence),
        pdg,
        chart_program,
        steerable,
        frozenset(),
        evidence,
    )
    assert len(graphs) == 1
    original = graphs[0]
    distinct_role = type(original)(
        type(original.role)(
            original.role.channel_tag,
            request_tags=frozenset({"Request"}),
        ),
        original.routes,
    )

    assert original != distinct_role


IndependentPermit = Bool("ChartIndependentPermit", external=True)
IndependentStepper = Int("ChartIndependentStepper")

with Program() as independent_chart_program:
    with rung(IndependentStepper == 0, IndependentPermit):
        copy(1, IndependentStepper)


def test_direct_and_opaque_chart_edges_select_the_same_exact_guard_owner() -> None:
    plc = PLC(independent_chart_program)
    pdg = build_program_graph(independent_chart_program)
    constants = compute_reference_constants(pdg, independent_chart_program, plc._known_tags_by_name)
    steerable = (
        compute_steerable(pdg, plc._known_tags_by_name, independent_chart_program) - constants
    )
    evidence = TransitionEvidence(
        functional_deps={},
        elided=frozenset(),
        stepping=frozenset((IndependentStepper.name,)),
    )

    def edge(opaque_loop: frozenset[str]):
        roles = discover_chart_roles(
            pdg, independent_chart_program, steerable, opaque_loop, evidence
        )
        graphs = build_static_transition_graphs(
            roles,
            pdg,
            independent_chart_program,
            steerable,
            opaque_loop,
            evidence,
        )
        return graphs[0].edges[0]

    direct = selected_chart_producer_guard_rungs(edge(frozenset()), pdg, independent_chart_program)
    opaque = selected_chart_producer_guard_rungs(
        edge(frozenset((IndependentStepper.name,))), pdg, independent_chart_program
    )

    assert direct == opaque == (independent_chart_program.rungs[0],)


AmbiguousCallerPermit = Bool("ChartAmbiguousCallerPermit", external=True)
AmbiguousCallerStepper = Int("ChartAmbiguousCallerStepper")

with Program() as ambiguous_caller_program:
    with subroutine("ChartAmbiguousProducer"):
        with rung(AmbiguousCallerPermit):
            copy(1, AmbiguousCallerStepper)
    with rung():
        call("ChartAmbiguousProducer")
    with rung():
        call("ChartAmbiguousProducer")


def test_chart_guard_fails_closed_when_the_dynamic_caller_is_ambiguous() -> None:
    plc = PLC(ambiguous_caller_program)
    pdg = build_program_graph(ambiguous_caller_program)
    constants = compute_reference_constants(pdg, ambiguous_caller_program, plc._known_tags_by_name)
    steerable = (
        compute_steerable(pdg, plc._known_tags_by_name, ambiguous_caller_program) - constants
    )
    evidence = TransitionEvidence(
        functional_deps={},
        elided=frozenset(),
        stepping=frozenset((AmbiguousCallerStepper.name,)),
    )
    roles = discover_chart_roles(pdg, ambiguous_caller_program, steerable, frozenset(), evidence)
    graphs = build_static_transition_graphs(
        roles, pdg, ambiguous_caller_program, steerable, frozenset(), evidence
    )

    assert len(graphs) == 1
    assert (
        selected_chart_producer_guard_rungs(graphs[0].edges[0], pdg, ambiguous_caller_program)
        is None
    )
