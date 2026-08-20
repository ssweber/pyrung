from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core.analysis.pilot.candidate_read import (
    PrerequisiteRead,
    _LearnedAction,
    _LearnedBatch,
    _LearnedWait,
    _PrerequisiteSeparation,
    _RouteAndCompletionRead,
    _TraceAdmission,
)
from pyrung.core.analysis.pilot.compass import (
    WAIT,
    ActionNogoodObservation,
    Compass,
    CompassObservation,
)
from pyrung.core.analysis.pilot.constrained_reachability import (
    NavigationEvidence,
    Reachable,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
    is_composite_action,
    pulse_identity,
)
from pyrung.core.analysis.pilot.options import _read_learned_fallback
from pyrung.core.analysis.pilot.trace_tree import TraceNode
from pyrung.core.analysis.pilot.world_key import wait_edge_nogood

_WORLD = ("learned-world",)
_TAG = "LearnedState"


def _observation(cause: Any, source: int, destination: int) -> CompassObservation:
    applied = cause if is_composite_action(cause) else (cause,) if isinstance(cause, tuple) else ()
    return CompassObservation(
        "edge",
        _TAG,
        cause,
        source,
        destination,
        applied=applied,
    )


def _read_and_reach(
    compass: Compass,
    *,
    target: int,
    blocked: frozenset[tuple[str, Any]] = frozenset(),
    avoid: Any = None,
) -> tuple[Any, bool]:
    snapshot = {_TAG: 0, "ActionA": False, "ActionB": False, "Alternate": False}
    frame = SimpleNamespace(
        key=_WORLD,
        snap=snapshot,
        tree=TraceNode(_TAG, target, satisfied=False),
    )
    context = SimpleNamespace(
        compass=compass,
        blocked_actions=blocked,
        avoid_pred=avoid,
    )
    admission = _TraceAdmission((), (), (), {}, (), False)
    learned = _read_learned_fallback(
        _RouteAndCompletionRead(admission, None, None),
        _PrerequisiteSeparation(admission, PrerequisiteRead(), None),
        frame,
        SimpleNamespace(),
        context,
        set(compass.knowledge.nogood_pairs(_WORLD)),
    )
    world = OrientationWorld(
        world_key=_WORLD,
        snapshot=snapshot,
        frame=frame,
        state=SimpleNamespace(),
        context=context,
    )
    reachable = isinstance(
        NavigationEvidence.frontier_status(
            world,
            TargetSpec(_TAG, target),
            NavigationConstraints(blocked_actions=blocked, avoid_predicate=avoid),
            compass.knowledge,
        ),
        Reachable,
    )
    return learned, reachable


def test_learned_composite_rejects_a_blocked_member_in_both_readers() -> None:
    cause = (("ActionA", True), ("ActionB", True))
    compass, _ = Compass().apply((_observation(cause, 0, 1),))

    learned, reachable = _read_and_reach(
        compass,
        target=1,
        blocked=frozenset({("ActionB", True)}),
    )

    assert learned is None
    assert not reachable


def test_learned_composite_rejects_a_collectively_avoided_overlay() -> None:
    cause = (("ActionA", True), ("ActionB", True))
    compass, _ = Compass().apply((_observation(cause, 0, 1),))

    def avoid(snapshot: dict[str, Any]) -> bool:
        return snapshot.get("ActionA") is True and snapshot.get("ActionB") is True

    learned, reachable = _read_and_reach(compass, target=1, avoid=avoid)

    assert learned is None
    assert not reachable


def test_whole_act_nogood_selects_an_alternate_learned_path() -> None:
    rejected = (("ActionA", True), ("ActionB", True))
    alternate = ("Alternate", True)
    compass, _ = Compass().apply(
        (
            _observation(rejected, 0, 1),
            _observation(alternate, 0, 2),
            _observation(WAIT, 2, 1),
            ActionNogoodObservation(_WORLD, pulse_identity(rejected)),
        )
    )

    learned, reachable = _read_and_reach(compass, target=1)

    assert isinstance(learned, _LearnedAction)
    assert learned.action == alternate
    assert reachable


def test_wait_nogood_has_identical_candidate_and_reachability_behavior() -> None:
    fresh, _ = Compass().apply((_observation(WAIT, 0, 1),))
    learned, reachable = _read_and_reach(fresh, target=1)
    assert isinstance(learned, _LearnedWait)
    assert reachable

    rejected, _ = fresh.apply(
        (
            ActionNogoodObservation(
                _WORLD,
                ("pair", wait_edge_nogood(_TAG, 0, 1)),
            ),
        )
    )
    learned, reachable = _read_and_reach(rejected, target=1)
    assert learned is None
    assert not reachable


def test_unblocked_learned_composite_remains_one_batch() -> None:
    cause = (("ActionA", True), ("ActionB", True))
    compass, _ = Compass().apply((_observation(cause, 0, 1),))

    learned, reachable = _read_and_reach(compass, target=1)

    assert isinstance(learned, _LearnedBatch)
    assert learned.read.actions == cause
    assert reachable
