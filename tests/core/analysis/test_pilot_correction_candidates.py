"""Focused contracts for correction candidate selection and materialization."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool
from pyrung.core.analysis.pilot import correction_candidates, investigate, retained
from pyrung.core.analysis.pilot.constrained_reachability import Unknown
from pyrung.core.analysis.pilot.corrections import CorrectionHypothesis
from pyrung.core.analysis.pilot.overlay import PilotRung


def test_candidate_identity_and_composition_have_one_owner() -> None:
    """Pair and executable proposal decisions live behind the investigate facade."""

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
    assert investigate._proposal_identity(first.holds[0]) == (
        correction_candidates._proposal_identity(first.holds[0])
    )
    assert investigate._hypothesis_identity(composite.holds) == (
        correction_candidates._hypothesis_identity(composite.holds)
    )


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
    assert (
        investigate._continuation_with_active_correction(
            (("Input", True),),
            {},
            SimpleNamespace(),
        )
        == status
    )


def test_retained_reads_candidate_decisions_without_investigate_indirection() -> None:
    assert retained._rank_hypotheses is correction_candidates._rank_hypotheses
    assert retained._exploratory_correction_rungs is (
        correction_candidates._exploratory_correction_rungs
    )
    assert retained._continuation_with_active_correction is (
        correction_candidates._continuation_with_active_correction
    )
    assert retained._active_pilot_rungs_defeat_needed is (
        correction_candidates._active_pilot_rungs_defeat_needed
    )
