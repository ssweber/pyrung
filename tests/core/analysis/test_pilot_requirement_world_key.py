"""Active requirements are part of PILOT's navigation identity."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung.core.analysis.pilot import orientation
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    TargetSpec,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig


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
