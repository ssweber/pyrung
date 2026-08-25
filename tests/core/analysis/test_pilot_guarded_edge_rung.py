"""Gates for repeated assertions of rung-managed simulated physical inputs."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, copy, rise, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.navigation_contracts import OrientationWorld, TargetSpec
from pyrung.core.analysis.pilot.options import read_candidates
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _set_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.program_facts import compute_edge_tags
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.steerable import compute_steerable


def _hold_for_operator_program():
    approach, hold_for_operator, ready_for_release, complete = range(4)
    door_closed = Bool("DoorClosed", external=True)
    unhold = Bool("Unhold", external=True)
    state = Int(
        "State",
        choices={
            approach: "Approach",
            hold_for_operator: "HoldForOperator",
            ready_for_release: "ReadyForRelease",
            complete: "Complete",
        },
    )

    with Program() as logic:
        # Evaluate the edge transition before Approach writes HoldForOperator;
        # the fixture must arrive with DoorClosed already high, not consume the
        # same scan's rise through read-after-write rung ordering.
        with rung(state == hold_for_operator, rise(door_closed)):
            copy(ready_for_release, state)
        with rung(state == approach):
            copy(hold_for_operator, state)
        with rung(state == ready_for_release, rise(unhold)):
            copy(complete, state)

    return (
        logic,
        door_closed,
        unhold,
        state,
        approach,
        hold_for_operator,
        ready_for_release,
        complete,
    )


def test_rung_managed_input_cycles_during_hold_for_operator() -> None:
    """The first guard yields open; a later local guard closes again.

    ``DoorClosed`` is not patched or driven False. Boolean input-image baseline
    supplies the open scan after the original guard expires; trace then appends
    another positive rung for the context that needs a fresh rising edge.
    """

    logic, door_closed, unhold, state_tag, approach, hold, ready, complete = (
        _hold_for_operator_program()
    )
    plc = PLC(logic, dt=0.010)
    earned = PilotRung(door_closed.name, True, state_tag == approach)
    starting_pilot_rungs = [earned]
    _set_pilot_rungs(plc, starting_pilot_rungs)

    # Approach enters HoldForOperator with DoorClosed already True, so the
    # transition requires a fresh off→on edge rather than the existing level.
    plc.step()
    snap = dict(plc.state.tags)
    assert snap[state_tag.name] == hold
    assert snap[door_closed.name] is True

    # The earned condition has ended. With no active Boolean rung, the input
    # image returns DoorClosed to False without release bookkeeping.
    plc.step()
    snap = dict(plc.state.tags)
    assert snap[state_tag.name] == hold
    assert snap[door_closed.name] is False

    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    tree = trace_back(state_tag.name, ready, snap, pdg, logic, steerable)
    details = tuple(tree.ordered_action_details())
    actions = tuple(detail.pair for detail in details)
    door_action = next(detail for detail in details if detail.tag == door_closed.name)
    assert Atom(state_tag.name, "eq", hold) in door_action.guard_atoms

    frame = SimpleNamespace(
        key=("hold",),
        snap=snap,
        tree=tree,
        raw_trace_actions=actions,
        raw_trace_action_details=details,
    )
    pilot_state = SimpleNamespace(work=plc, pilot_rungs=starting_pilot_rungs)
    ctx = SimpleNamespace(
        compass=Compass(),
        edge_tags=compute_edge_tags(pdg, logic),
        clear_only=frozenset(),
        steerable=steerable,
        pdg=pdg,
        program=logic,
        blocked_actions=frozenset(),
        opaque_loop=frozenset(),
        target=TargetSpec(state_tag.name, ready),
        resting={door_closed.name: False},
    )

    candidates = read_candidates(
        OrientationWorld(
            world_key=frame.key,
            snapshot=frame.snap,
            frame=frame,
            state=pilot_state,
            context=ctx,
        )
    )

    assert candidates.options == ()
    assert candidates.diagnosis is None
    assert len(candidates.prerequisites.pilot_rungs) == 1
    close_again = candidates.prerequisites.pilot_rungs[0]
    assert (close_again.dest, close_again.value) == (door_closed.name, True)

    pilot_rungs = (*starting_pilot_rungs, close_again)
    plc = fork_with_pilot_rungs(plc, pilot_rungs)
    plc.step()
    assert plc.state.tags[state_tag.name] == ready
    assert plc.state.tags[door_closed.name] is True

    # The operator command remains an edge pulse, not another PilotRung.
    plc.patch({unhold.name: True})
    plc.step()
    assert plc.state.tags[state_tag.name] == complete
    assert all(r.dest != unhold.name for r in pilot_rungs)
