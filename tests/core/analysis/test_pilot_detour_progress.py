"""PILOT gates for target-relative progress across channel detours."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Timer,
    calc,
    copy,
    on_delay,
    rise,
    rung,
)
from pyrung.core.analysis.pilot._ops import wait_edge_nogood
from pyrung.core.analysis.pilot.charts import StaticTransitionGraph
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    Compass,
    NavigationCatalog,
)
from pyrung.core.analysis.pilot.detour import (
    _continuation_safety,
    _current_action_allowed,
)
from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
from pyrung.core.analysis.pilot.navigation import pulse_identity


def _knock_three_times_program():
    """A channel revisit whose retained counter records real causal progress.

    Each knock bumps the machine away from the door.  It walks back by itself,
    and the third knock admits it.  Channel-only history sees the same
    ``AT_DOOR -> AWAY -> AT_DOOR`` loop twice; the meaningful states are the
    joint ``(channel, knock_count)`` visits.
    """

    at_door, away, inside = 0, 1, 2
    knock = Bool("Knock_Btn", external=True)
    channel = Int(
        "Knock_Channel",
        default=at_door,
        choices={at_door: "AtDoor", away: "Away", inside: "Inside"},
    )
    count = Int("Knock_Count")
    away_tmr = Timer.clone("Knock_AwayTmr")

    with Program() as logic:
        with rung(channel == at_door, rise(knock)):
            calc(count + 1, count)
            copy(away, channel)

        with rung(channel == away):
            on_delay(away_tmr, 30, "ms")

        with rung(channel == away, away_tmr.Done, count < 3):
            copy(at_door, channel)

        with rung(channel == away, away_tmr.Done, count >= 3):
            copy(inside, channel)

    return logic, knock, channel, count, at_door, away, inside


def _pulse_knock(plc: PLC, knock: Bool) -> None:
    plc.patch({knock.name: True})
    plc.step()
    plc.patch({knock.name: False})


def test_knock_three_times_premise() -> None:
    """The program itself reaches Inside after three separate knock edges."""

    logic, knock, channel, count, at_door, away, inside = _knock_three_times_program()
    plc = PLC(logic, dt=0.010)
    plc.step()

    for expected in (1, 2, 3):
        assert plc.state.tags[channel.name] == at_door
        _pulse_knock(plc, knock)
        assert plc.state.tags[channel.name] == away
        assert plc.state.tags[count.name] == expected
        plc.run_until(
            lambda state: state.tags[channel.name] != away,
            max_cycles=20,
        )
        # Give the off-rung scan a chance to clear the timer's Done state before
        # the next edge.  This is timer housekeeping, not a fourth action.
        if expected < 3:
            assert plc.state.tags[channel.name] == at_door
            plc.step()

    assert plc.state.tags[channel.name] == inside
    assert plc.state.tags[count.name] == 3


def test_pilot_reaches_counter_gated_target_across_channel_revisits() -> None:
    """PILOT knocks three times despite leaving and revisiting its channel.

    The search key threshold-abstracts ``Knock_Count`` — the joint visits
    ``(AtDoor, 1)`` and ``(AtDoor, 2)`` alias — so the SPIN / CYCLE / LATERAL
    gates consult the target-relative progress gauge (gauge.py): a
    knock that advanced the event-earned ordinal is real work, not a revisit.
    """

    logic, _knock, channel, count, _at_door, _away, inside = _knock_three_times_program()
    plc = PLC(logic, dt=0.010)

    path = plc.how(channel == inside, max_scans=3000)

    assert path.reachable, path.reason
    final = path.replay().state.tags
    assert final[channel.name] == inside
    assert final[count.name] == 3


def test_continuation_evidence_accepts_departure_already_at_goal() -> None:
    """A terminal departure has valid zero-edge continuation evidence."""
    from pyrung.core.analysis.pilot.navigation_evidence import (
        NavigationEvidence,
        Reachable,
    )

    status = NavigationEvidence.channel_continuation(
        (),
        "State",
        17,
        (6, 17),
        edge_allowed=lambda _edge: True,
    )
    assert isinstance(status, Reachable)


def _charted_edge(
    *,
    action: tuple[str, object] | None,
    co_actions: tuple[tuple[str, object], ...] = (),
):
    route = TransitionRoute(
        destination_tag="State",
        destination_value=2,
        request_tag=None,
        request_value=None,
        source_constraints=(("State", 0),),
        enablers=((action,) if action is not None else ()),
        action_tags=(frozenset({action[0]}) if action is not None else frozenset()),
        writer_node=0,
        writer_subroutine=None,
        call_site_gates=(),
        from_values=(0,),
    )
    graph = StaticTransitionGraph(PipelineRoles("State"), (route,))
    edge = graph.edges[0]
    return graph, replace(edge, co_actions=co_actions)


def _detour_edge_allowed(
    edge,
    compass: Compass,
    *,
    blocked_actions=frozenset(),
    avoid_pred=None,
    progress_erasing_values=frozenset(),
    completed_actions=None,
) -> bool:
    ctx = SimpleNamespace(
        compass=compass,
        avoid_pred=avoid_pred,
        blocked_route_actions=blocked_actions,
    )
    return _continuation_safety(
        edge,
        ctx,
        settled_key=("settled",),
        settled_snap={"State": 0, "Start": False, "Gate": False},
        blocked_actions=blocked_actions,
        progress_erasing_values=progress_erasing_values,
        completed_actions=completed_actions or set(),
    ).allowed


def test_detour_excludes_settled_world_pair_and_exact_pulse_nogoods() -> None:
    primary = ("Start", True)
    gate_a = ("Gate", True)
    gate_b = ("OtherGate", True)
    graph, edge_a = _charted_edge(action=primary, co_actions=(gate_a,))
    edge_b = replace(edge_a, co_actions=(gate_b,))
    graph.edges = (edge_a, edge_b)
    compass = Compass(NavigationCatalog(graphs=(graph,)))

    pair_rejected, _ = compass.apply((ActionNogoodObservation(("settled",), ("pair", primary)),))
    assert not _detour_edge_allowed(edge_a, pair_rejected)

    exact_rejected, _ = compass.apply(
        (ActionNogoodObservation(("settled",), pulse_identity((primary, gate_a))),)
    )
    assert not _detour_edge_allowed(edge_a, exact_rejected)
    assert _detour_edge_allowed(edge_b, exact_rejected)

    other_world_rejected, _ = compass.apply(
        (ActionNogoodObservation(("other-world",), pulse_identity((primary, gate_a))),)
    )
    assert _detour_edge_allowed(edge_a, other_world_rejected)


def test_detour_excludes_settled_world_wait_nogood() -> None:
    graph, edge = _charted_edge(action=None)
    compass, _ = Compass(NavigationCatalog(graphs=(graph,))).apply(
        (
            ActionNogoodObservation(
                ("settled",),
                ("pair", wait_edge_nogood("State", 0, 2)),
            ),
        )
    )

    assert not _detour_edge_allowed(edge, compass)


def test_detour_current_action_uses_the_same_settled_world_pair_scope() -> None:
    action = ("Start", True)
    compass, _ = Compass().apply(
        (ActionNogoodObservation(("settled",), pulse_identity((action,))),)
    )

    assert not _current_action_allowed(
        action,
        settled_key=("settled",),
        knowledge=compass.knowledge,
        blocked_actions=frozenset(),
    )
    assert _current_action_allowed(
        action,
        settled_key=("other-world",),
        knowledge=compass.knowledge,
        blocked_actions=frozenset(),
    )


def test_detour_checks_required_co_actions_and_recovery_only_exclusions() -> None:
    primary = ("Start", True)
    gate = ("Gate", True)
    graph, edge = _charted_edge(action=primary, co_actions=(gate,))
    compass = Compass(NavigationCatalog(graphs=(graph,)))

    assert not _detour_edge_allowed(
        edge,
        compass,
        blocked_actions=frozenset({gate}),
    )
    assert not _detour_edge_allowed(
        edge,
        compass,
        avoid_pred=lambda snap: snap.get("Gate") is True,
    )
    assert not _detour_edge_allowed(
        edge,
        compass,
        progress_erasing_values=frozenset({2}),
    )
    assert not _detour_edge_allowed(
        edge,
        compass,
        completed_actions={("Start", True, 0)},
    )
