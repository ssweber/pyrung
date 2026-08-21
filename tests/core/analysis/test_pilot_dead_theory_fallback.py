"""A failed temporal hypothesis must not hide another current-world route."""

from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
from pyrung.core.analysis.pilot.pipeline_graph import (
    _best_static_path,
    build_static_transition_graphs,
    detect_opaque_loop,
    oneshot_rearm_edges,
)
from pyrung.core.analysis.steerable import compute_steerable
from tests.fixtures import pilot_indirect_sequence_controls as fixture


def test_fixture_preserves_exported_unnamed_indirect_table() -> None:
    loaded = fixture.watch_plc(sequence_state=95)

    assert loaded.current_state.tags[fixture.NextState.name] == fixture.TARGET


def test_manual_next_pulse_advances_exactly_one_table_entry() -> None:
    plc = fixture.watch_plc(sequence_state=25)

    assert plc.current_state.tags[fixture.NextState.name] == 30
    plc.force(fixture.NextStep.name, True)
    plc.step()

    assert plc.current_state.tags[fixture.SequenceState.name] == 30
    assert plc.current_state.tags[fixture.NextState.name] == 30


def test_manual_next_is_a_rearmable_oneshot_trigger() -> None:
    plc = fixture.watch_plc(sequence_state=25)
    pdg = build_program_graph(fixture.logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, fixture.logic)

    opaque = detect_opaque_loop(pdg, fixture.logic)
    role = infer_pipeline_roles(
        fixture.SequenceState.name,
        pdg,
        fixture.logic,
        steerable,
        opaque,
    )
    graphs = build_static_transition_graphs(
        (role,),
        pdg,
        fixture.logic,
        steerable,
        opaque,
        None,
    )

    rearm_edges = oneshot_rearm_edges(graphs, pdg, fixture.logic)
    assert any(
        edge.identity in rearm_edges and edge.action == (fixture.NextStep.name, True)
        for graph in graphs
        for edge in graph.edges
    )


def test_static_indirect_table_builds_complete_manual_route() -> None:
    plc = fixture.watch_plc(sequence_state=25)
    pdg = build_program_graph(fixture.logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, fixture.logic)
    opaque = detect_opaque_loop(pdg, fixture.logic)
    role = infer_pipeline_roles(
        fixture.SequenceState.name,
        pdg,
        fixture.logic,
        steerable,
        opaque,
    )
    graphs = build_static_transition_graphs(
        (role,),
        pdg,
        fixture.logic,
        steerable,
        opaque,
        None,
    )

    path = _best_static_path(
        fixture.SequenceState.name,
        fixture.TARGET,
        plc.current_state.tags,
        graphs,
        edge_allowed=lambda edge: edge.action == (fixture.NextStep.name, True),
    )

    assert path is not None
    assert tuple((edge.from_value, edge.to_value) for edge in path.edges) == tuple(
        (value, value + 5) for value in range(25, fixture.TARGET, 5)
    )


def test_pilot_rearms_oneshot_across_the_complete_indirect_route() -> None:
    events = []
    plan = fixture.watch_plc(sequence_state=25).how(
        fixture.SequenceState == fixture.TARGET,
        max_scans=100,
        on_event=events.append,
    )

    assert plan.status is PlanStatus.REACHED
    accepted_next = tuple(
        event
        for event in events
        if event.kind == "candidate_accepted"
        and event.data["applied"] == ((fixture.NextStep.name, True),)
    )
    assert len(accepted_next) == 14
    assert not any(
        event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.AdvancePermit.name, True),)
        for event in events
    )
    assert not any(
        event.kind == "bearing_coast"
        and event.data.get("channel_tag") == fixture.SequenceState.name
        for event in events
    )


def test_unready_actionless_startup_edge_cannot_authorize_a_coast() -> None:
    events = []
    plan = fixture.watch_plc(sequence_state=25, manual_mode=False).how(
        fixture.SequenceState == fixture.TARGET,
        max_scans=100,
        on_event=events.append,
    )

    assert plan.status is PlanStatus.REACHED
    assert any(
        event.kind == "candidate_accepted"
        and event.data["applied"]
        == (
            (fixture.NextStep.name, True),
            (fixture.ManualModeSwitch.name, True),
        )
        for event in events
    )
    assert not any(
        event.kind == "bearing_coast"
        and event.data.get("channel_tag") == fixture.SequenceState.name
        and event.data.get("before_value") == 25
        and event.data.get("target_value") == 35
        for event in events
    )


def test_unrealizable_reset_theory_falls_back_to_alternate_writer() -> None:
    events = []
    plan = fixture.watch_plc(sequence_state=0).how(
        fixture.SequenceState == fixture.TARGET,
        max_scans=100,
        on_event=events.append,
    )

    assert plan.status is PlanStatus.STOPPED
    assert "no ordinary scan-boundary realization" not in plan.reason
    rejected = tuple(
        event.data["applied"] for event in events if event.kind == "candidate_rejected"
    )
    assert rejected
    first_rejected = rejected[0]
    assert any(
        event.kind == "candidate_try"
        and event.data["applied"]
        and event.data["applied"] != first_rejected
        for event in events
    ), tuple((event.kind, event.data.get("applied")) for event in events)
