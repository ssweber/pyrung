"""Focused contracts for investigation's bounded refinement evidence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core.analysis.pilot import investigate, refinement
from pyrung.core.analysis.pilot.corrections import CorrectionHypothesis


def _identity(holds: Any) -> tuple[Any, ...]:
    return tuple(holds)


def test_relational_refinement_uses_caller_owned_identity_and_budget() -> None:
    """Refinement consumes novel counterexamples without owning hypothesis identity."""

    original = CorrectionHypothesis("relational", (("Limit", 1),))
    refined = CorrectionHypothesis("relational", (("Limit", 2),))
    outcome = SimpleNamespace(continuation_snapshot={"Limit": 2}, snapshot={})
    receipt = refinement._RelationalRefinementReceipt(budget=2)

    candidate, ground = refinement._refine_unknown_continuation(
        original,
        outcome,
        SimpleNamespace(),
        receipt,
        identity=_identity,
        refiner=lambda *_args: refined,
    )
    assert candidate is refined
    assert ground == ""

    candidate, ground = refinement._refine_unknown_continuation(
        original,
        outcome,
        SimpleNamespace(),
        receipt,
        identity=_identity,
        refiner=lambda *_args: refined,
    )
    assert candidate is None
    assert "repeated a prior counterexample" in ground


def test_investigate_refinement_facade_preserves_patchable_refiner(monkeypatch) -> None:
    """Legacy investigate imports still dispatch through its patched dependency."""

    original = CorrectionHypothesis("relational", (("Limit", 1),))
    refined = CorrectionHypothesis("relational", (("Limit", 2),))
    outcome = SimpleNamespace(continuation_snapshot={"Limit": 2}, snapshot={})
    monkeypatch.setattr(
        investigate,
        "refine_relational_hypothesis",
        lambda *_args: refined,
    )

    candidate, ground = investigate._refine_unknown_continuation(
        original,
        outcome,
        SimpleNamespace(),
        investigate._RelationalRefinementReceipt(),
    )

    assert investigate._RelationalRefinementReceipt is refinement._RelationalRefinementReceipt
    assert candidate is refined
    assert ground == ""


def test_pinned_suppression_nominations_are_bounded_evidence() -> None:
    """The focused module flips only a finite Bool condition-read lever."""

    node = SimpleNamespace(condition_reads=("Gate",))
    pdg = SimpleNamespace(
        rung_nodes=(node,),
        upstream_slice=lambda _tag, **_kwargs: frozenset({"Gate"}),
    )
    work = SimpleNamespace(state=SimpleNamespace(tags={"Gate": True, "Output": False}))
    calls: list[dict[str, Any]] = []

    def run_pinned(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(after={"Output": True})

    nominations = refinement._skiff_suppression_nominations(
        work,
        "Output",
        True,
        node,
        (("Command", True),),
        pdg,
        frozenset({"Gate"}),
        (),
        run_pinned=run_pinned,
    )

    assert nominations == [("Gate", False)]
    assert calls == [
        {
            "pilot_rungs": (),
            "actions": (("Command", True), ("Gate", False)),
            "scans": refinement._SKIFF_SCANS,
        }
    ]
