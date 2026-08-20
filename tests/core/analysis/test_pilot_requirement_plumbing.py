from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrsistent import pvector

from pyrung import PLC
from pyrung.core.analysis.pilot import orientation, theory_orientation
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _retain_active_requirement,
    _retain_failed_effect_receipt,
)
from pyrung.core.analysis.pilot.types import _PilotContext, _PilotState
from pyrung.core.analysis.pilot.world import _World
from tests.fixtures.pilot_alarm_presets import alarmed_at_start


def _state() -> _PilotState:
    plc = PLC(alarmed_at_start.logic, dt=0.010)
    return _PilotState(
        world=_World(
            work=plc,
            committed_acts=pvector(),
            best_trend=None,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        key_config=None,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )


def test_requirement_knowledge_survives_world_restore() -> None:
    state = _state()
    checkpoint_world = state.snapshot_world()
    requirement = object()
    receipt = object()

    state.active_requirements.append(requirement)  # type: ignore[arg-type]
    state.expectation_receipts.append(receipt)
    state.best_trend = 4
    state.load_world(checkpoint_world)

    assert state.active_requirements == [requirement]
    assert state.expectation_receipts == [receipt]
    assert state.best_trend is None


def test_active_requirement_retention_uses_navigation_identity() -> None:
    retained: Any = SimpleNamespace(
        navigation_identity=("scheduled",),
        identity=("scheduled", "first-exact-source"),
    )
    retry: Any = SimpleNamespace(
        navigation_identity=("scheduled",),
        identity=("scheduled", "second-exact-source"),
    )
    distinct: Any = SimpleNamespace(
        navigation_identity=("other-schedule",),
        identity=("other-schedule", "third-exact-source"),
    )
    state: Any = SimpleNamespace(active_requirements=[])

    assert _retain_active_requirement(state, retained)
    assert not _retain_active_requirement(state, retained)
    assert not _retain_active_requirement(state, retry)
    assert _retain_active_requirement(state, distinct)
    assert state.active_requirements == [retained, distinct]


def test_failed_effect_retention_uses_exact_identity_and_preserves_order() -> None:
    first: Any = SimpleNamespace(identity=("effect", "source-a"))
    equal: Any = SimpleNamespace(identity=("effect", "source-a"))
    distinct: Any = SimpleNamespace(identity=("effect", "source-b"))
    state: Any = SimpleNamespace(failed_effect_receipts=[])

    assert _retain_failed_effect_receipt(state, first)
    assert not _retain_failed_effect_receipt(state, first)
    assert not _retain_failed_effect_receipt(state, equal)
    assert _retain_failed_effect_receipt(state, distinct)
    assert state.failed_effect_receipts == [first, distinct]


def test_orientation_receives_active_requirement_views_unchanged(monkeypatch) -> None:
    compass = Compass()
    requirement = object()
    active = (requirement,)
    context = _PilotContext(
        target=TargetSpec("Target", True),
        pdg=SimpleNamespace(),
        program=SimpleNamespace(),
        steerable=frozenset(),
        edge_tags=set(),
        resting={},
        nd_domains=None,
        domain_prior=None,
        evidence=None,
        compass=compass,
        opaque_loop=frozenset(),
        pipeline_roles=(),
        pipeline_internal_tags=frozenset(),
        route=None,
        blocked_actions=frozenset(),
        max_scans=1,
    )
    world = OrientationWorld(
        world_key=(),
        snapshot={},
        frame=object(),
        state=SimpleNamespace(),
        context=context,
    )
    selected = object()
    seen: list[tuple[object, ...]] = []

    monkeypatch.setattr(theory_orientation, "_current_work_evidence", lambda *_args: ())

    def read_group(_compass, worlds, _target, **_kwargs):
        seen.append(worlds[0].context.active_requirements)
        return selected, ()

    monkeypatch.setattr(orientation, "_read_group", read_group)

    result = orientation.orient(
        compass,
        world,
        context.target,
        NavigationConstraints(active_requirements=active),  # type: ignore[arg-type]
    )

    assert result is selected
    assert seen == [active]
    assert seen[0] is active
