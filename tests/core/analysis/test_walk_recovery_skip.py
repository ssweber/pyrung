"""Same-depth writer-SP recovery skip guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung.core.analysis.walk import agenda as agenda_mod
from pyrung.core.analysis.walk.agenda import (
    _cacheable_writer_sp_failure,
    _last_child_node,
    _PlanNode,
    _Request,
)
from pyrung.core.analysis.walk.base import _NO_MONITORS, NoGoodStore, _DebugSink, _WalkBudget


class _DummyWork:
    def __init__(self) -> None:
        self.state = SimpleNamespace(tags={"Target": False, "Child": False})

    def fork(self) -> _DummyWork:
        return self


def _none_pipeline(*_args: object, **_kwargs: object):
    if False:
        yield None
    return None


def test_cacheable_writer_sp_failure_excludes_bounds_subtrees() -> None:
    hard = _PlanNode(goal=("Hard", True), provenance="writer-sp-tree", depth=1)
    hard.status = "failed"
    hard.failure = "explore-stuck"
    assert _cacheable_writer_sp_failure(hard)

    bounds = _PlanNode(goal=("Bounds", True), provenance="writer-sp-tree", depth=1)
    bounds.status = "failed"
    bounds.failure = "bounds"
    assert not _cacheable_writer_sp_failure(bounds)

    budget = _PlanNode(goal=("Budget", True), provenance="writer-sp-tree", depth=1)
    budget.status = "failed"
    budget.failure = "budget-exhausted"
    assert not _cacheable_writer_sp_failure(budget)

    parent = _PlanNode(goal=("Parent", True), provenance="writer-sp-tree", depth=1)
    parent.status = "failed"
    parent.failure = "explore-stuck"
    child = _PlanNode(goal=("Child", True), provenance="writer-sp-tree", depth=2)
    child.status = "failed"
    child.failure = "bounds"
    parent.segments.append(child)
    assert not _cacheable_writer_sp_failure(parent)


def test_last_child_node_finds_most_recent_matching_child() -> None:
    root = _PlanNode(goal=("Root", True), provenance="test", depth=0)
    older = _PlanNode(goal=("Child", True), provenance="writer-sp-tree", depth=1)
    newer = _PlanNode(goal=("Child", True), provenance="writer-sp-tree", depth=1)
    other = _PlanNode(goal=("Child", True), provenance="oracle-recheck", depth=1)
    root.segments.extend([older, other, newer])

    assert _last_child_node(root, ("Child", True), "writer-sp-tree") is newer
    assert _last_child_node(root, ("Child", True), "oracle-recheck") is other
    assert _last_child_node(root, ("Missing", True), "writer-sp-tree") is None


def test_establish_passes_hard_writer_sp_failures_to_recovery_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = SimpleNamespace(
        pdg=None,
        program=None,
        known={},
        nd_domains=None,
        explore_context=None,
        advice=None,
        holds=None,
        ref_constants=frozenset(),
        progress_goals=set(),
        probe_memo={},
        budget=_WalkBudget(),
        nogoods=NoGoodStore(),
    )
    work = _DummyWork()
    req = _Request(
        runner=work,
        goal=("Target", True),
        depth=0,
        visited=frozenset(),
        budget=8,
        provenance="test",
    )
    node = _PlanNode(goal=req.goal, provenance=req.provenance, depth=req.depth)

    monkeypatch.setattr(agenda_mod, "_governing", lambda *_args, **_kwargs: ("Target", True))
    monkeypatch.setattr(agenda_mod, "_steer_alphabet", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        agenda_mod,
        "_explore_corridor",
        lambda *_args, **_kwargs: SimpleNamespace(steps=None, outcome="stuck", best=None),
    )
    monkeypatch.setattr(
        agenda_mod,
        "_writer_candidates",
        lambda *_args, **_kwargs: (
            [("Child", True)],
            [
                agenda_mod._WriterCandidate(
                    full_conditions=(),
                    satisfied=(),
                    unsatisfied=(("Child", True),),
                    all_writes=frozenset(),
                    writer_index=0,
                )
            ],
        ),
    )
    monkeypatch.setattr(agenda_mod, "_apply_temporal_recovery", lambda *_args: None)
    monkeypatch.setattr(agenda_mod, "_why_regression", _none_pipeline)
    monkeypatch.setattr(agenda_mod, "_log_decomposition_hint", lambda *_args, **_kwargs: None)

    captured: list[frozenset[tuple[str, object]]] = []

    def fake_recover(*_args: object, skip_goals: frozenset[tuple[str, object]] = frozenset()):
        captured.append(skip_goals)
        if False:
            yield None
        return None

    monkeypatch.setattr(agenda_mod, "_recover", fake_recover)

    gen = agenda_mod._establish(ctx, req, node)
    child_req = next(gen)
    assert child_req.goal == ("Child", True)
    assert child_req.provenance == "writer-sp-tree"

    child = _PlanNode(goal=("Child", True), provenance="writer-sp-tree", depth=1)
    child.status = "failed"
    child.failure = "explore-stuck"
    node.segments.append(child)

    with pytest.raises(StopIteration) as stop:
        gen.send(None)

    assert stop.value.value is None
    assert captured == [frozenset({("Child", True)})]


def test_recover_filters_skipped_goals_without_yielding_oracle_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debug = _DebugSink()
    ctx = SimpleNamespace(
        nogoods=NoGoodStore(),
        holds=None,
        debug_sink=debug,
    )
    work = SimpleNamespace(state=SimpleNamespace(tags={"Target": False}))
    node = _PlanNode(goal=("Target", True), provenance="test", depth=0)

    monkeypatch.setattr(
        agenda_mod,
        "_recovery_goals",
        lambda *_args, **_kwargs: agenda_mod._RecoverySignal(
            [("Child", True)],
            frozenset(),
        ),
    )
    monkeypatch.setattr(agenda_mod, "_apply_temporal_recovery", lambda *_args: None)

    gen = agenda_mod._recover(
        ctx,
        node,
        work,
        "Target",
        True,
        8,
        0,
        frozenset(),
        _NO_MONITORS,
        skip_goals=frozenset({("Child", True)}),
    )

    with pytest.raises(StopIteration) as stop:
        next(gen)

    assert stop.value.value is None
    assert node.failure == "no-recovery-goals"
    assert [event.kind for event in debug.events] == ["recovery-skip"]
    assert "Child" in debug.events[0].detail
