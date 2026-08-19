"""Focused contracts for correction candidate selection and materialization."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool
from pyrung.core.analysis.pilot import correction_candidates
from pyrung.core.analysis.pilot.constrained_reachability import Unknown
from pyrung.core.analysis.pilot.corrections import CorrectionHypothesis
from pyrung.core.analysis.pilot.overlay import PilotRung


def test_candidate_composition_combines_sources() -> None:

    active = Bool("CandidateActive", external=True)
    first = CorrectionHypothesis(
        "precise-cause",
        (PilotRung("CandidateInput", True, active),),
        sources=("FirstSource",),
    )
    second = CorrectionHypothesis(
        "absence-root",
        (("SecondInput", False),),
        sources=("SecondSource",),
    )

    composite = correction_candidates._compose_hypotheses(first, second)

    assert composite is not None
    assert composite.kind == "nested-cause"
    assert composite.sources == ("FirstSource", "SecondSource")


def test_candidate_composition_declines_conflicting_destinations() -> None:
    first = CorrectionHypothesis("precise-cause", (("SharedInput", True),))
    second = CorrectionHypothesis("precise-cause", (("SharedInput", False),))

    assert correction_candidates._compose_hypotheses(first, second) is None


def test_non_executable_candidate_continuation_is_honestly_unknown() -> None:
    status = correction_candidates._continuation_with_active_correction(
        (("Input", True),),
        {},
        SimpleNamespace(),
    )

    assert isinstance(status, Unknown)
    assert "no executable scope" in status.reason


def test_candidate_selection_exposes_no_historical_replay_entrypoint() -> None:
    assert not hasattr(correction_candidates, "replay_retained_prefix")
