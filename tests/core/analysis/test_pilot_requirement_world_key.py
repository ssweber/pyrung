"""Active requirements are part of PILOT's navigation identity."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, Rung, call, copy, subroutine
from pyrung.core.analysis.pilot import orientation_reading as orientation
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    TargetSpec,
)
from pyrung.core.analysis.pilot.world_key import (
    _physical_world_key,
    _pilot_world_key,
    _StateKeyConfig,
)


def _config() -> _StateKeyConfig:
    return _StateKeyConfig(
        stateful_names=("State",),
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )


def test_active_requirement_identity_distinguishes_otherwise_equal_worlds() -> None:
    snap = {"State": 7}
    config = _config()
    requirement_a = SimpleNamespace(identity=("preset", ">", 10))
    requirement_b = SimpleNamespace(identity=("preset", ">", 20))

    bare = _pilot_world_key(snap, config, ())
    constrained_a = _pilot_world_key(snap, config, (), (requirement_a,))
    constrained_b = _pilot_world_key(snap, config, (), (requirement_b,))

    assert bare == _pilot_world_key(snap, config, (), ())
    assert constrained_a != bare
    assert constrained_b != bare
    assert constrained_a != constrained_b
    assert _pilot_world_key(snap, config, (), (requirement_a, requirement_b)) != _pilot_world_key(
        snap, config, (), (requirement_b, requirement_a)
    )


def test_receipt_retry_identity_does_not_mint_a_new_navigation_world() -> None:
    snap = {"State": 7}
    config = _config()
    first = SimpleNamespace(
        identity=("exact", "epoch-1"),
        navigation_identity=("scheduled", "Preset", ">", 10),
    )
    retry = SimpleNamespace(
        identity=("exact", "epoch-2"),
        navigation_identity=("scheduled", "Preset", ">", 10),
    )

    assert first.identity != retry.identity
    assert _pilot_world_key(snap, config, (), (first,)) == _pilot_world_key(
        snap, config, (), (retry,)
    )


def test_physical_world_key_strips_only_requirement_identity() -> None:
    config = _config()
    requirement = SimpleNamespace(identity=("preset", ">", 10))
    rung = SimpleNamespace(dest="Command", value=True, guard=None, operation=None)

    bare = _pilot_world_key({"State": 7}, config, ())
    constrained = _pilot_world_key({"State": 7}, config, (), (requirement,))
    changed_state = _pilot_world_key({"State": 8}, config, ())
    changed_rungs = _pilot_world_key({"State": 7}, config, (rung,))

    assert constrained != bare
    assert _physical_world_key(constrained) == bare
    assert _physical_world_key(changed_state) != bare
    assert _physical_world_key(changed_rungs) != bare


def test_orientation_queries_nogoods_with_active_requirement_world_key(monkeypatch) -> None:
    config = _config()
    requirement = SimpleNamespace(identity=("preset", ">", 10))
    seen_keys: list[tuple[object, ...]] = []

    class Knowledge:
        def nogood_identities(self, world_key):
            seen_keys.append(world_key)
            return frozenset()

    state = SimpleNamespace(
        key_config=config,
        pilot_rungs=(),
        active_requirements=(requirement,),
    )
    world = SimpleNamespace(
        snapshot={"State": 7},
        state=state,
        context=SimpleNamespace(
            compass=SimpleNamespace(knowledge=Knowledge()),
        ),
    )
    selected_tree = object()
    monkeypatch.setattr(
        orientation,
        "_trace_for_route",
        lambda *_args, **_kwargs: selected_tree,
    )

    result = orientation._read_route_trees(
        world,
        TargetSpec("Target", True, predicate=object()),
        NavigationConstraints(active_requirements=(requirement,)),
    )

    assert result == ((None, selected_tree),)
    assert seen_keys == [
        _pilot_world_key(
            world.snapshot,
            config,
            (),
            (requirement,),
        )
    ]


def test_prior_scan_setup_is_retained_when_the_selected_producer_reaches_target() -> None:
    produce = Bool("RequirementKeyProduce", external=True)
    consume = Bool("RequirementKeyConsume", external=True)
    channel = Int("RequirementKeyChannel")
    step = Int("RequirementKeyStep")

    with Program(strict=False) as logic:
        with subroutine("RequirementKeyConsumer"):
            with Rung(consume):
                copy(1, step, oneshot=True)
            with Rung():
                copy(0, channel)

        with Rung(produce):
            copy(1, channel, oneshot=True)
        with Rung(channel == 1):
            call("RequirementKeyConsumer")

    events = tuple(pilot_events(PLC(logic), step == 1, max_scans=10))
    singleton_produce_tries = tuple(
        event
        for event in events
        if event.kind == "candidate_try" and tuple(event.data["applied"]) == ((produce.name, True),)
    )
    assert not any(event.kind == "requirement_activated" for event in events)
    producer_accept_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_accepted"
        and (produce.name, True) in tuple(event.data["applied"])
    )

    consume_try_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try" and event.data["applied"] == ((consume.name, True),)
    )
    produce_try_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try" and event.data["applied"] == ((produce.name, True),)
    )
    assert consume_try_index < produce_try_index < producer_accept_index
    assert len(singleton_produce_tries) == 1
    assert not any(
        event.kind == "candidate_try"
        and set(event.data["applied"]) == {(produce.name, True), (consume.name, True)}
        for event in events
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
