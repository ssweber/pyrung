"""Tests for pilot investigation — hypothesis generation and bounded replay.

Coverage targets:
- build_replay_fn: bounded vs unbounded judgment
- investigate_deviation: hypothesis generation pipeline
- _precise_causes, _latch_exposure_hypotheses, _done_boundary_hypotheses
- investigate_excursion: excursion diagnosis and retry
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, calc, copy, latch, on_delay, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import (
    OperationReceipt,
    PilotRung,
    _coast_holding_state,
    _pilot_state_key,
    _set_rungs,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.coast import CoastSession, CoastTriggerEvent
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.investigate import (
    CausalOccurrence,
    DeviationIncident,
    InvestigationHypothesis,
    InvestigationRejection,
    RegressionWitness,
    ReplacementEvidence,
    ReplayIncident,
    ReplayJustification,
    ReplayOutcome,
    ReplayStep,
    _dedupe_pairs,
    _first_timeline_departure,
    _hold_allowed,
    _hold_is_noop,
    _HypothesisExtended,
    _precise_causes,
    _regression_cause_replayed,
    _ReplayAccepted,
    _ReplayRejected,
    _resolve_replay_attempt,
    _shared_causal_suffix,
    build_deviation_incident,
    build_replay_fn,
    correction_identity,
    incident_regression_witness,
    investigate_deviation,
    investigate_excursion,
)
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.types import BearingDeparture
from pyrung.core.analysis.sp_values import _SnapshotView
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.condition import CompareEq, CompareNe
from pyrung.core.context import RungId
from pyrung.core.instruction.advance import ConditionDemand
from pyrung.core.runner import PLC

_DEFAULT_TARGET = TargetSpec("", None)


def _make_ctx(
    prog: Program,
    plc: PLC,
    *,
    target: TargetSpec = _DEFAULT_TARGET,
    **overrides: Any,
) -> SimpleNamespace:
    """Minimal duck-typed context for the hypothesis generators.

    The generators read ``pdg``, ``program``, ``steerable``, ``opaque_loop``,
    ``pipeline_internal_tags``, ``route`` and ``compass.action_tags`` off the
    context via ``getattr`` — a SimpleNamespace satisfies all of them.
    """
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    ns: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "route": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
        "target": target,
    }
    ns.update(overrides)
    return SimpleNamespace(**ns)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_replay_context(prog: Program, plc: PLC, target_tag: str, target_value: Any):
    """Build the minimal keyword context for build_replay_fn."""
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)
    return SimpleNamespace(
        resting={t: False for t in steerable if isinstance(plc.state.tags.get(t), bool)},
        edge_tags=set(),
        target=TargetSpec(target_tag, target_value),
        pdg=pdg,
        program=prog,
        steerable=steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        domain_prior=None,
        clear_only=frozenset(),
    )


def _ground_test_incident(plc: PLC) -> DeviationIncident:
    snap = dict(plc.state.tags)
    return DeviationIncident(
        anchor_scan=plc.state.scan_id,
        departure_scan=plc.state.scan_id,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(),
        before_snap=snap,
        after_snap=snap,
        changed_tags=(),
        departures=(),
    )


def test_investigation_rejections_carry_raw_and_guarded_replay_grounds(monkeypatch):
    """Replay reasons survive into the result instead of disappearing into DEBUG."""
    A = Bool("GroundA", external=True)
    B = Bool("GroundB", external=True)
    with Program(strict=False) as prog:
        with Rung(A):
            out(B)
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    raw_reject = InvestigationHypothesis("raw", (("GroundA", True),))
    guarded_reject = InvestigationHypothesis("guarded", (("GroundB", True),))

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([raw_reject, guarded_reject], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._scoped_correction_rungs",
        lambda _plc, holds, *_args: tuple(PilotRung(t, v, A == A) for t, v in holds),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )

    def replay(holds):
        first = holds[0]
        if isinstance(first, PilotRung):
            return ReplayOutcome(False, None, {}, "guard released before landing")
        if first[0] == "GroundA":
            return ReplayOutcome(False, None, {}, "watchdog still fired")
        return ReplayOutcome(True, None, {}, "incident silenced")

    result = investigate_deviation(plc, _ground_test_incident(plc), ctx, replay)

    assert result.rejected == (
        InvestigationRejection(
            raw_reject,
            "exploratory-replay-failed",
            "exploratory replay rejected: watchdog still fired",
        ),
        InvestigationRejection(
            guarded_reject,
            "guarded-replay-failed",
            "guarded replay rejected: guard released before landing",
        ),
    )


@pytest.mark.parametrize(
    ("phase", "slug", "ground"),
    (
        (
            "exploratory",
            "exploratory-replay-failed",
            "exploratory replay rejected: exploratory failed",
        ),
        (
            "guarded",
            "guarded-replay-failed",
            "guarded replay rejected: guarded failed",
        ),
    ),
)
def test_replay_resolution_keeps_phase_specific_rejection_ground(phase, slug, ground):
    hypothesis = InvestigationHypothesis("cause", (("Input", True),))

    resolved = _resolve_replay_attempt(
        phase=phase,
        current=hypothesis,
        outcome=ReplayOutcome(False, None, {}, f"{phase} failed"),
        seen_replacements=set(),
        extend=lambda *_args: pytest.fail("a failed replay cannot extend"),
    )

    assert resolved == _ReplayRejected(InvestigationRejection(hypothesis, slug, ground))


def test_replay_resolution_distinguishes_acceptance_extension_and_shared_cycle():
    hypothesis = InvestigationHypothesis("cause", (("A", False),))
    extended = InvestigationHypothesis("nested-cause", (("A", False), ("B", False)))
    occurrence = CausalOccurrence(RungId(None, 2), "State", 8)
    witness = RegressionWitness(
        channel_tag="State",
        source=3,
        departed=8,
        landing=8,
        departure_scan=2,
        cause=(occurrence,),
        causal_spine=frozenset({"State"}),
    )
    incident = DeviationIncident(
        anchor_scan=0,
        departure_scan=2,
        end_scan=2,
        action=(),
        bearing=(),
        before_snap={"State": 3},
        after_snap={"State": 8},
        changed_tags=("State",),
        departures=(),
    )
    replacement = ReplacementEvidence(
        plc=object(),
        incident=incident,
        witness=witness,
        shared_suffix=(occurrence,),
    )
    accepted = ReplayOutcome(True, None, {}, "accepted")
    replaced = ReplayOutcome(True, None, {}, "replacement", replacement=replacement)
    seen: set[tuple[Any, ...]] = set()
    extensions = 0

    def _extend(*_args):
        nonlocal extensions
        extensions += 1
        return extended

    assert _resolve_replay_attempt(
        phase="exploratory",
        current=hypothesis,
        outcome=accepted,
        seen_replacements=seen,
        extend=_extend,
    ) == _ReplayAccepted(accepted)
    assert _resolve_replay_attempt(
        phase="exploratory",
        current=hypothesis,
        outcome=replaced,
        seen_replacements=seen,
        extend=_extend,
    ) == _HypothesisExtended(extended)
    cycled = _resolve_replay_attempt(
        phase="guarded",
        current=extended,
        outcome=replaced,
        seen_replacements=seen,
        extend=_extend,
    )

    assert extensions == 1
    assert isinstance(cycled, _ReplayRejected)
    assert cycled.rejection.slug == "nested-cause-cycle"
    assert cycled.rejection.ground == ("guarded replay repeated a counterfactual replacement cause")


def test_investigation_static_rejections_carry_their_grounds(monkeypatch):
    A = Bool("StaticGround", external=True)
    with Program(strict=False) as prog:
        with Rung(A):
            out(Bool("StaticOut"))
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    empty = InvestigationHypothesis("empty", ())
    noop = InvestigationHypothesis("noop", (("StaticGround", False),))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([empty, noop], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: True,
    )

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        lambda _holds: pytest.fail("static rejection must not replay"),
    )

    assert result.rejected[0] == InvestigationRejection(empty, "no-holds", "no holds proposed")
    assert result.rejected[1].hypothesis == noop
    assert result.rejected[1].slug == "vacuous-hold"
    assert result.rejected[1].ground.startswith("vacuous no-op hold")


def test_revoked_correction_is_skipped_and_runner_up_is_replayed(monkeypatch):
    """An exact guarded correction nogood selects the next explanation."""
    Bad = Bool("Revoked_Bad", external=True)
    Good = Bool("Revoked_Good", external=True)
    with Program(strict=False) as prog:
        with Rung(Bad):
            out(Bool("Revoked_BadOut"))
        with Rung(Good):
            out(Bool("Revoked_GoodOut"))
    plc = PLC(prog)
    ctx = _make_ctx(
        prog,
        plc,
        target=TargetSpec("Revoked_GoodOut", True),
    )
    bad_rung = PilotRung(Bad.name, True, CompareEq(Bad, False))
    good_rung = PilotRung(Good.name, True, CompareEq(Good, False))
    bad = InvestigationHypothesis("bad", (bad_rung,), sources=(Bad.name,))
    good = InvestigationHypothesis("good", (good_rung,), sources=(Good.name,))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([bad, good], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )

    replayed = []

    def replay(holds):
        replayed.append(tuple((h.dest, h.value) if isinstance(h, PilotRung) else h for h in holds))
        return ReplayOutcome(True, None, dict(plc.state.tags), "incident solved")

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        replay,
        excluded_corrections=frozenset((correction_identity((bad_rung,)),)),
    )

    assert result.confirmed and result.confirmed[0].kind == "good"
    assert replayed
    assert all(Bad.name not in {tag for tag, _value in attempt} for attempt in replayed)
    assert tuple(rejection.slug for rejection in result.rejected) == ("correction-revoked",)


def test_revoked_broad_correction_does_not_exclude_new_safe_scope(monkeypatch):
    """Negative correction evidence names the guard that actually caused harm."""
    Held = Bool("ScopedNogoodHeld", external=True)
    Target = Bool("ScopedNogoodTarget")
    Broad = Bool("ScopedNogoodBroad", external=True)
    with Program(strict=False) as prog:
        with Rung(Held):
            out(Target)
    plc = PLC(prog)
    ctx = _make_ctx(
        prog,
        plc,
        target=TargetSpec(Target.name, True),
    )
    hypothesis = InvestigationHypothesis(
        "same-write-new-scope",
        ((Held.name, True),),
        sources=(Held.name,),
    )
    broad = PilotRung(Held.name, True, CompareEq(Broad, True))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([hypothesis], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )

    replayed: list[tuple[Any, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(True, None, dict(plc.state.tags), "incident solved")

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        replay,
        excluded_corrections=frozenset((correction_identity((broad,)),)),
    )

    assert result.correction is not None
    scoped = result.correction.rungs[0]
    assert scoped.dest == broad.dest and scoped.value == broad.value
    assert correction_identity((scoped,)) != correction_identity((broad,))
    assert result.rejected == ()
    assert len(replayed) == 2
    assert replayed[0] == hypothesis.holds
    assert replayed[1] == (scoped,)


def test_raw_hypothesis_is_rejected_after_it_acquires_revoked_scope(monkeypatch):
    """A raw pair is compared with negative evidence only in executable form."""
    Held = Bool("ScopedExactHeld", external=True)
    Target = Bool("ScopedExactTarget")
    with Program(strict=False) as prog:
        with Rung(Held):
            out(Target)
    plc = PLC(prog)
    ctx = _make_ctx(
        prog,
        plc,
        target=TargetSpec(Target.name, True),
    )
    hypothesis = InvestigationHypothesis(
        "raw-pair",
        ((Held.name, True),),
        sources=(Held.name,),
    )
    revoked = PilotRung(Held.name, True, CompareNe(Target, True))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([hypothesis], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )

    replayed: list[tuple[Any, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(True, None, dict(plc.state.tags), "incident solved")

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        replay,
        excluded_corrections=frozenset((correction_identity((revoked,)),)),
    )

    assert result.correction is None
    assert replayed == [hypothesis.holds]
    assert tuple(rejection.slug for rejection in result.rejected) == ("correction-revoked",)


def test_investigation_filters_corrections_after_observing_full_overlay(monkeypatch):
    """A correction shadowed by another continuing owner is not installed-active."""
    Held = Bool("FullOverlayHeld", external=True)
    Progress = Bool("FullOverlayProgress", external=True)
    Never = Bool("FullOverlayNever", external=True)
    with Program(strict=False) as prog:
        with Rung(Held, Progress, Never):
            out(Bool("FullOverlayOut"))
    plc = PLC(prog)
    ctx = _make_ctx(prog, plc)
    shadowed = PilotRung(
        Held.name,
        True,
        ~Never,
        OperationReceipt(Held, ConditionDemand(CompareEq(Never, True))),
    )
    winner = PilotRung(
        Held.name,
        False,
        ~Never,
        OperationReceipt(~Held, ConditionDemand(CompareEq(Progress, True))),
    )
    hypothesis = InvestigationHypothesis("shadowed", ((Held.name, True),))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([hypothesis], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._scoped_correction_rungs",
        lambda _plc, holds, *_args: tuple(PilotRung(tag, value, ~Never) for tag, value in holds),
    )
    incident = _ground_test_incident(plc)
    incident = DeviationIncident(
        **{
            **incident.__dict__,
            "before_snap": {**incident.before_snap, Progress.name: True, Never.name: False},
        }
    )
    replayed: list[tuple[Any, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(True, None, dict(incident.before_snap), "confirmed")

    result = investigate_deviation(
        plc,
        incident,
        ctx,
        replay,
        installed_rungs=(shadowed, winner),
        correction_rungs=(shadowed,),
    )

    assert replayed, "the shadowed correction must not skip its hypothesis"
    assert result.correction is not None


def test_investigation_reuses_exploratory_proof_for_identical_installed_rungs(monkeypatch):
    """An operation-owned correction is not replayed twice unchanged."""
    Held = Bool("ReplayOnceHeld", external=True)
    Progress = Bool("ReplayOnceProgress", external=True)
    Never = Bool("ReplayOnceNever", external=True)
    with Program(strict=False) as prog:
        with Rung(Held):
            out(Bool("ReplayOnceOut"))
    plc = PLC(prog)
    ctx = _make_ctx(prog, plc)
    proposal = PilotRung(
        Held.name,
        False,
        ~Never,
        OperationReceipt(~Held, ConditionDemand(CompareEq(Progress, True))),
    )
    hypothesis = InvestigationHypothesis("operation-owned", (proposal,))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._generate_deviation_hypotheses",
        lambda *_args, **_kwargs: ((hypothesis,), set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )
    replayed: list[tuple[Any, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(True, None, dict(plc.state.tags), "confirmed")

    result = investigate_deviation(plc, _ground_test_incident(plc), ctx, replay)

    assert result.correction is not None
    assert result.correction.rungs == (proposal,)
    assert replayed == [(proposal,)]


def test_investigation_nests_a_replacement_cut_without_proving_it_alone(monkeypatch):
    """A retained replay fork supplies B; only A and then A+B are replayed."""
    A = Bool("Nested_A", external=True)
    B = Bool("Nested_B", external=True)
    State = Int("Nested_State", default=3)
    with Program(strict=False) as prog:
        with Rung(A):
            copy(8, State)
        with Rung(B):
            copy(8, State)
    plc = PLC(prog)
    replacement_plc = plc.fork()
    ctx = _make_ctx(
        prog,
        plc,
        target=TargetSpec(State.name, 17),
    )
    incident = _ground_test_incident(plc)
    first = InvestigationHypothesis("precise-cause", ((A.name, False),), sources=(A.name,))
    second = InvestigationHypothesis("precise-cause", ((B.name, False),), sources=(B.name,))
    occurrence_a = CausalOccurrence(RungId(None, 1), "Nested_Request", 8)
    occurrence_b = CausalOccurrence(RungId(None, 2), State.name, 8)
    replacement_witness = RegressionWitness(
        channel_tag=State.name,
        source=3,
        departed=8,
        landing=8,
        departure_scan=2,
        cause=(occurrence_b, occurrence_a),
        causal_spine=frozenset({B.name, State.name, "Nested_Request"}),
    )
    replacement = ReplacementEvidence(
        plc=replacement_plc,
        incident=incident,
        witness=replacement_witness,
        shared_suffix=(occurrence_b, occurrence_a),
    )

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._generate_deviation_hypotheses",
        lambda source, *_args, **_kwargs: (
            ([second] if source is replacement_plc else [first]),
            frozenset(),
        ),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._scoped_correction_rungs",
        lambda _plc, holds, *_args: tuple(
            hold if isinstance(hold, PilotRung) else PilotRung(hold[0], hold[1], A == A)
            for hold in holds
        ),
    )

    attempts: list[tuple[str, ...]] = []

    def replay(holds):
        tags = tuple(hold.dest if isinstance(hold, PilotRung) else hold[0] for hold in holds)
        attempts.append(tags)
        if tags == (A.name,):
            return ReplayOutcome(
                True,
                None,
                dict(plc.state.tags),
                "A silenced before B reproduced the departure",
                ReplayJustification.NEUTRALIZED,
                replacement=replacement,
            )
        return ReplayOutcome(
            True,
            None,
            dict(plc.state.tags),
            "composite neutralized the incident",
            ReplayJustification.NEUTRALIZED,
        )

    result = investigate_deviation(plc, incident, ctx, replay)

    assert result.correction is not None
    assert {rung.dest for rung in result.correction.rungs} == {A.name, B.name}
    assert attempts == [
        (A.name,),
        (A.name, B.name),
        (A.name, B.name),
    ]
    assert all(attempt != (B.name,) for attempt in attempts)


def test_shared_pipeline_does_not_group_a_different_bounded_landing():
    """The same first hop and executor path need not be the same failed effect."""
    shared = (
        CausalOccurrence(RungId(None, 2), "State", 12),
        CausalOccurrence(RungId(None, 1), "Request", 12),
    )
    recorded = RegressionWitness(
        channel_tag="State",
        source=11,
        departed=12,
        landing=9,
        departure_scan=4,
        cause=shared,
        causal_spine=frozenset({"State", "Request", "DoorAlarm"}),
    )
    healthy_detour = RegressionWitness(
        channel_tag="State",
        source=11,
        departed=12,
        landing=6,
        departure_scan=4,
        cause=shared,
        causal_spine=frozenset({"State", "Request"}),
    )

    assert _shared_causal_suffix(recorded, healthy_detour) == ()


def test_noop_check_uses_recorded_incident_motion_not_pilot_ownership():
    Bool("RecordedMover", external=True)
    with Program(strict=False) as prog:
        with Rung():
            out(Bool("RecordedMoverReader"))
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    snap = {"RecordedMover": False}

    assert _hold_allowed(ctx, ("RecordedMover", False))
    assert _hold_is_noop("RecordedMover", False, snap, ctx.pdg, prog)
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        frozenset({"RecordedMover"}),
    )
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        after_snap={"RecordedMover": True},
    )
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        synthesis_rungs=(PilotRung("RecordedMover", True, Bool("SynthesisGuard")),),
    )


# ---------------------------------------------------------------------------
# Bounded replay — departure_scan / departure_bearing
# ---------------------------------------------------------------------------


def _watchdog_program() -> tuple[Program, Timer]:
    """Timer acts as a watchdog: Enable stays True, timer fires, Alarm goes True.

    Hold = True blocks the alarm output.  Use this to test bounded replay:
    without the hold, the bearing (Alarm=False) departs at the timer preset.
    """
    Enable = Bool("Enable", external=True)
    Hold = Bool("Hold", external=True)
    Tmr = Timer.clone("Tmr")
    Alarm = Bool("Alarm")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done, ~Hold):
            out(Alarm)
        with Rung(Enable, ~Alarm):
            out(Target)

    return prog, Tmr


class TestBoundedReplay:
    """build_replay_fn with departure_bearing judges a command incident by
    bearing rather than target-reached; the coast is the recorded dwell span."""

    def _setup(self):
        prog, tmr = _watchdog_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        cp = plc.fork()

        # Coast until the alarm fires
        for _ in range(20):
            plc.step()
        assert plc.state.tags["Alarm"] is True

        # The recorded coast span: a plain dwell reproduces the same scans the
        # live coast rode (the timer fires ~10 scans in, so 20 covers it).
        span = plc.state.scan_id - cp.state.scan_id
        ctx = _make_replay_context(prog, plc, "Target", True)
        cp_trend = 1
        steps = [ReplayStep(inputs=(), scans=span, kind="dwell")]
        return prog, plc, cp, cp_trend, steps, ctx

    def test_bounded_accepts_good_hold(self):
        """A hold that prevents the departure is accepted under bounded replay."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(departure_bearing=(("Alarm", False),)),
        )
        outcome = replay((("Hold", True),))
        assert outcome.accepted
        assert "held" in outcome.reason

    def test_bounded_rejects_bad_hold(self):
        """A no-op hold that doesn't prevent the departure is rejected."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(departure_bearing=(("Alarm", False),)),
        )
        outcome = replay(())
        assert not outcome.accepted
        assert "departed" in outcome.reason

    def test_unbounded_falls_through_to_trend_judgment(self):
        """Without departure info, replay uses the trace-back trend judgment."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            ctx=ctx,
        )
        outcome = replay((("Hold", True),))
        assert "trend" in outcome.reason


# ---------------------------------------------------------------------------
# Zoom incident — channel register reaches its requested value
# ---------------------------------------------------------------------------


def _zoom_transition_program() -> tuple[Program, Timer, Any]:
    """``State`` advances 3 -> 6 after a watchdog timer, but ejects to 8 (Aborting)
    if the door (``Guard``) is open at completion.  Holding the door closed lets
    the coast reach the requested value (6); leaving it open ejects (8).

    The timer is long (50 scans) on purpose: the requested value is reachable
    only by an *unbounded* coast, so a coast bounded to the departure window
    would never get there — that is the regression this guards.
    """
    Guard = Bool("Guard", external=True)
    Tmr = Timer.clone("Tmr")
    State = Int("State")
    with Program() as prog:
        with Rung(State == 3):
            on_delay(Tmr, 500, "ms")
        with Rung(Tmr.Done, ~Guard):
            copy(8, State)  # door open at completion -> eject to Aborting
        with Rung(Tmr.Done, Guard):
            copy(6, State)  # door closed -> advance to Execute
    return prog, Tmr, State


class TestZoomReplay:
    """build_replay_fn for a zoom incident.

    Judged by the channel register reaching its requested value over an
    *unbounded*, ejection-guarded coast — never by the bounded bearing-held test
    (the bearing carries the far-off requested value as a conjunct, which a
    bounded coast can never restore, so it would reject every hold).
    """

    def _setup(self):
        prog, _tmr, _state = _zoom_transition_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"State": 3})
        plc.step()
        assert plc.state.tags["State"] == 3
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "State", 6)
        # A recorded zoom step: the coast re-arms State -> 6 under the ejection
        # guard, unbounded — the requested value is a full coast away, so no
        # departure-window bound may cut it short.
        steps = [ReplayStep(inputs=(), scans=0, kind="zoom", channel_tag="State", channel_target=6)]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(channel_tag="State", channel_target=6),
        )

    def test_zoom_accepts_hold_that_reaches_corridor_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay((("Guard", True),))  # close the door
        assert outcome.accepted
        assert outcome.snapshot["State"] == 6
        assert "State -> 6" in outcome.reason

    def test_zoom_rejects_hold_that_ejects(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay(())  # door rests open -> ejects to 8
        assert not outcome.accepted
        assert outcome.snapshot["State"] == 8


def test_route_replay_accepts_local_neutralization_without_reaching_frontier():
    """A correction owns the recorded regression, not the whole remaining route."""
    Guard = Bool("Neutralize_Guard", external=True)
    Detour = Bool("Neutralize_Detour", external=True)
    Watchdog = Timer.clone("Neutralize_Watchdog")
    State = Int("Neutralize_State", default=6)

    with Program() as prog:
        with Rung(State == 6):
            on_delay(Watchdog, 100, "ms").reset(Guard)
        with Rung(State == 6, Detour):
            copy(13, State)
        with Rung(Watchdog.Done):
            copy(8, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    incident_session.dwell(20)
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None
    assert (witness.source, witness.departed) == (6, 8)
    assert witness.owner_snapshot is not None
    assert witness.owner_snapshot[State.name] == 6

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=20, kind="dwell"),),
        ctx=ctx,
        incident=ReplayIncident(
            channel_tag=State.name,
            channel_target=16,
            regression_witness=witness,
        ),
    )

    neutralized = replay(((Guard.name, True),))
    assert neutralized.accepted
    assert neutralized.justification is ReplayJustification.NEUTRALIZED
    assert neutralized.snapshot[State.name] == 6
    assert "recorded regression neutralized" in neutralized.reason

    # This proposal silences the recorded watchdog by creating a different
    # departure inside the same bounded replay. Its replacement cause owns the
    # result, so the hypothesis disproves itself before installation.
    destructive = replay(((Detour.name, True),))
    assert not destructive.accepted
    assert destructive.snapshot[State.name] == 13
    assert destructive.justification is None


def test_non_timer_regression_witness_distinguishes_suppression_from_masking():
    """Neutralization owns the recorded cause, not a behavior class."""
    Trip = Bool("Witness_Trip", external=True)
    Inhibit = Bool("Witness_Inhibit", external=True)
    Mask = Bool("Witness_Mask", external=True)
    State = Int("Witness_State", default=6)

    with Program() as prog:
        with Rung(Trip, ~Inhibit):
            copy(8, State)
        with Rung(Trip, Mask):
            copy(6, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    recorded.patch({Trip.name: True})
    recorded.step()
    incident_session.note_pens()
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=((Trip.name, True),),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=((Trip.name, True),), scans=1, kind="pulse"),),
        ctx=ctx,
        incident=ReplayIncident(
            channel_tag=State.name,
            channel_target=16,
            regression_witness=witness,
        ),
    )

    suppressed = replay(((Inhibit.name, True),))
    assert suppressed.snapshot[State.name] == 6
    assert suppressed.accepted
    assert suppressed.justification is ReplayJustification.NEUTRALIZED
    assert "suppressed its" in suppressed.reason

    masked_probe = cp.fork()
    _set_rungs(masked_probe, (PilotRung(Mask.name, True, State != 17),))
    masked_start = masked_probe.state.scan_id
    masked_probe.patch({Trip.name: True})
    masked_probe.step()
    assert masked_probe.state.tags[State.name] == 6
    assert _regression_cause_replayed(
        masked_probe,
        witness,
        start_scan=masked_start,
        end_scan=masked_probe.state.scan_id,
    ), masked_probe.rung_firings(masked_probe.state.scan_id)

    masked = replay(((Mask.name, True),))
    assert not masked.accepted
    assert masked.snapshot[State.name] == 6
    assert "cause replayed" in masked.reason


def test_regression_witness_does_not_confuse_a_shared_executor_with_its_owner():
    """A different cause may reuse the same response pipeline."""
    Fault = Bool("WitnessOwner_Fault", external=True)
    Alternate = Bool("WitnessOwner_Alternate", external=True)
    Request = Int("WitnessOwner_Request")
    State = Int("WitnessOwner_State", default=6)

    with Program() as prog:
        with Rung(Fault):
            copy(8, Request)
        with Rung(Alternate):
            copy(8, Request)
        with Rung(Request == 8):
            copy(Request, State)
            copy(0, Request)

    plc = PLC(prog)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    recorded.patch({Fault.name: True})
    recorded.step()
    incident_session.note_pens()
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=((Fault.name, True),),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None
    assert {item.rung.rung_index for item in witness.cause} == {0, 2}

    alternate = cp.fork()
    start_scan = alternate.state.scan_id
    alternate.patch({Alternate.name: True})
    alternate.step()
    assert alternate.state.tags[State.name] == 8
    assert not _regression_cause_replayed(
        alternate,
        witness,
        start_scan=start_scan,
        end_scan=alternate.state.scan_id,
    )


def test_replay_accepts_suppression_before_an_unrelated_executor_reuse():
    """A later fault is deferred to a new incident after local suppression."""
    Inhibit = Bool("ReplayOwner_Inhibit", external=True)
    Harmful = Bool("ReplayOwner_Harmful", external=True)
    Primary = Timer.clone("ReplayOwner_Primary")
    Alternate = Timer.clone("ReplayOwner_Alternate")
    Request = Int("ReplayOwner_Request")
    State = Int("ReplayOwner_State", default=6)

    with Program() as prog:
        with Rung(State == 6):
            on_delay(Primary, 100, "ms").reset(Inhibit)
        with Rung(State == 6):
            on_delay(Alternate, 150, "ms")
        with Rung(Primary.Done):
            copy(8, Request)
        with Rung(Alternate.Done):
            copy(8, Request)
        with Rung(Harmful):
            copy(8, Request)
        with Rung(Request == 8):
            copy(Request, State)
            copy(0, Request)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    session = CoastSession(recorded, kind="recorded-regression")
    session.arm_pens((State.name,))
    session.dwell(20)
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None

    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=20, kind="dwell"),),
        ctx=_make_replay_context(prog, plc, State.name, 17),
        incident=ReplayIncident(
            channel_tag=State.name,
            channel_target=16,
            regression_witness=witness,
        ),
    )

    unrelated = replay(((Inhibit.name, True),))
    assert unrelated.snapshot[State.name] == 8
    assert unrelated.accepted
    assert unrelated.justification is ReplayJustification.NEUTRALIZED
    assert "unrelated replacement departure" in unrelated.reason
    assert not unrelated.landed
    assert unrelated.replacement_cause

    proposal_owned = replay(((Harmful.name, True),))
    assert proposal_owned.snapshot[State.name] == 8
    assert not proposal_owned.accepted
    assert proposal_owned.justification is None


def test_replay_composes_owner_spines_when_all_changed_writes_are_reused():
    """A changed upstream owner is retained even when downstream writes match.

    The release timer and both executor rungs are identical in the recorded and
    replayed departures. The recorded branch is selected by ``PrimaryFault``;
    after that branch is corrected, a later timer selects the same writer for a
    different operation. The retained replacement witness separates the
    upstream owners and gives investigation the exact fork on which to derive
    the newly exposed correction.
    """
    PrimaryFault = Bool("ReplaySpine_PrimaryFault", external=True)
    Harmful = Bool("ReplaySpine_Harmful", external=True)
    Release = Timer.clone("ReplaySpine_Release")
    Alternate = Timer.clone("ReplaySpine_Alternate")
    Request = Int("ReplaySpine_Request")
    State = Int("ReplaySpine_State", default=6)

    with Program() as prog:
        with Rung(State == 6):
            on_delay(Release, 100, "ms")
            on_delay(Alternate, 150, "ms")
        with Rung(Release.Done, Or(PrimaryFault, Alternate.Done, Harmful)):
            copy(8, Request)
        with Rung(Request == 8):
            copy(Request, State)
            copy(0, Request)

    plc = PLC(prog, dt=0.010)
    plc.patch({PrimaryFault.name: True})
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    session = CoastSession(recorded, kind="recorded-regression")
    session.arm_pens((State.name,))
    session.dwell(12)
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None
    assert PrimaryFault.name in witness.causal_spine

    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=20, kind="dwell"),),
        ctx=_make_replay_context(prog, plc, State.name, 17),
        incident=ReplayIncident(
            channel_tag=State.name,
            channel_target=16,
            regression_witness=witness,
        ),
    )

    unrelated = replay(((PrimaryFault.name, False),))
    assert unrelated.snapshot[State.name] == 8
    assert unrelated.accepted
    assert unrelated.justification is ReplayJustification.NEUTRALIZED
    assert Alternate.Done.name in unrelated.replacement_cause
    assert unrelated.replacement is not None
    assert unrelated.replacement.plc.state.tags[State.name] == 8
    assert len(unrelated.replacement.shared_suffix) >= 2

    proposal_owned = replay(((Harmful.name, True),))
    assert not proposal_owned.accepted


def test_latch_silencing_replay_stops_at_the_incident_horizon():
    """Local proof does not coast forward to reconstruct a stable landing."""
    DoorA = Bool("Landing_DoorA", external=True)
    DoorB = Bool("Landing_DoorB", external=True)
    AlarmA = Bool("Landing_AlarmA")
    AlarmB = Bool("Landing_AlarmB")
    State = Int("Landing_State", default=3)
    AlarmTmr = Timer.clone("Landing_AlarmTmr")
    MotionTmr = Timer.clone("Landing_MotionTmr")

    with Program() as prog:
        with Rung(State == 3):
            on_delay(AlarmTmr, 100, "ms")
            on_delay(MotionTmr, 500, "ms")
        with Rung(AlarmTmr.Done, ~DoorA):
            latch(AlarmA)
        with Rung(AlarmTmr.Done, ~DoorB):
            latch(AlarmB)
        with Rung(Or(AlarmA, AlarmB)):
            copy(8, State)
        with Rung(MotionTmr.Done, DoorA, DoorB):
            copy(6, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    incident_session.dwell(12)
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 3),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=12, kind="letrun"),),
        ctx=ctx,
        incident=ReplayIncident(
            channel_tag=State.name,
            channel_target=3,
            terminal_role_tags=(State.name,),
            watch_roles=(State.name,),
            regression_witness=witness,
        ),
    )

    outcome = replay(((DoorA.name, True), (DoorB.name, True)))

    assert outcome.accepted
    assert outcome.snapshot[State.name] == 3

    guarded = replay(
        (
            PilotRung(DoorA.name, True, State != 6),
            PilotRung(DoorB.name, True, State != 6),
        )
    )
    assert guarded.accepted
    assert guarded.snapshot[State.name] == 3


# ---------------------------------------------------------------------------
# Terminal let-run incident — channel register *maintained* at its held value
# ---------------------------------------------------------------------------


def _letrun_hold_program() -> tuple[Program, Timer, Any]:
    """``Phase`` sits at 6 (Execute).  A watchdog ejects it to 8 (Aborting) at its
    preset unless ``Guard`` is held.  ``Goal`` (the global target) is never
    reached inside the window, so replay must prove both that ``Phase`` stayed
    at 6 and that the recorded 6 -> 8 cause stopped executing.
    """
    Guard = Bool("Guard", external=True)
    Tmr = Timer.clone("Tmr")
    Phase = Int("Phase")
    Goal = Bool("Goal")
    with Program() as prog:
        with Rung(Phase == 6):
            on_delay(Tmr, 200, "ms")
        with Rung(Tmr.Done, ~Guard):
            copy(8, Phase)  # eject Execute -> Aborting
        with Rung(Phase == 99):
            out(Goal)  # Phase never 99 -> Goal stays a known-but-unreached tag
    return prog, Tmr, Phase


class TestTerminalLetrunReplay:
    """build_replay_fn for a terminal let-run incident.

    The coast is *bounded* to the departure window (its global target is far
    off). Judgment requires source preservation plus suppression of the exact
    recorded cause, so a channel override cannot masquerade as maintenance.
    """

    def _setup(self):
        prog, _tmr, _phase = _letrun_hold_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Phase": 6})
        plc.step()
        assert plc.state.tags["Phase"] == 6
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        # A recorded let-run step whose span covers the watchdog eject (~20 scans)
        # so the bad hold ejects inside the bounded coast — the recorded coast
        # span replaces the old departure-window bound.
        steps = [ReplayStep(inputs=(), scans=25, kind="letrun")]
        recorded = cp.fork()
        incident_session = CoastSession(recorded, kind="recorded-regression")
        incident_session.arm_pens(("Phase",))
        incident_session.dwell(25)
        incident = build_deviation_incident(
            anchor_scan=cp.state.scan_id,
            end_scan=recorded.state.scan_id,
            action=(),
            bearing=(("Phase", 6),),
            before_snap=dict(cp.state.tags),
            after_snap=dict(recorded.state.tags),
            timeline=incident_session.events,
            channel_tag="Phase",
        )
        witness = incident_regression_witness(recorded, incident)
        assert witness is not None
        return cp, steps, ctx, witness

    def _build(self, cp, steps, ctx, witness):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(
                channel_tag="Phase",
                channel_target=6,
                terminal_role_tags=("Phase",),
                watch_roles=("Phase",),
                regression_witness=witness,
            ),
        )

    def test_letrun_accepts_hold_that_maintains_state(self):
        cp, steps, ctx, witness = self._setup()
        replay = self._build(cp, steps, ctx, witness)
        outcome = replay((("Guard", True),))  # keep the watchdog satisfied
        assert outcome.accepted
        assert outcome.snapshot["Phase"] == 6
        assert "suppressed its" in outcome.reason

    def test_letrun_rejects_hold_that_ejects(self):
        cp, steps, ctx, witness = self._setup()
        replay = self._build(cp, steps, ctx, witness)
        outcome = replay(())  # watchdog trips -> Phase ejects to 8
        assert not outcome.accepted
        assert outcome.snapshot["Phase"] == 8


def _letrun_global_program() -> tuple[Program, Timer]:
    """No macro-state register: ``Goal`` latches at the watchdog preset only if
    ``Hold`` keeps ``Alarm`` clear.  Exercises the let-run fallback that judges
    the *global* target when there is no channel register to maintain.
    """
    Enable = Bool("Enable", external=True)
    Hold = Bool("Hold", external=True)
    Tmr = Timer.clone("Tmr")
    Alarm = Bool("Alarm")
    Goal = Bool("Goal")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done, ~Hold):
            latch(Alarm)
        with Rung(Tmr.Done, ~Alarm):
            latch(Goal)
    return prog, Tmr


class TestTerminalLetrunNoChannelRegister:
    """A let-run with no recognized state machine (empty role tags, no channel
    register) falls back to judging the global target at the bounded point."""

    def _setup(self):
        prog, _tmr = _letrun_global_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        # A recorded let-run step; its span covers the watchdog eject (~10 scans)
        # so a missed global target is judged at the bounded point.
        steps = [ReplayStep(inputs=(), scans=15, kind="letrun")]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            ctx=ctx,
            incident=ReplayIncident(terminal_role_tags=()),  # no recognized state machine
        )

    def test_fallback_accepts_hold_that_reaches_global_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay((("Hold", True),))  # keep Alarm clear -> Goal latches
        assert outcome.accepted
        assert outcome.snapshot["Goal"] is True
        assert "Goal -> True" in outcome.reason

    def test_fallback_rejects_hold_that_misses_global_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay(())  # Alarm latches -> Goal never reached
        assert not outcome.accepted
        assert outcome.snapshot["Goal"] is not True


# ---------------------------------------------------------------------------
# _precise_causes — cause()-chain walks from departures
# ---------------------------------------------------------------------------


class TestPreciseCauses:
    """_precise_causes: cause walks to steerable inputs."""

    def test_guard_expiry_keeps_external_destinations_on_frontier(self):
        """Exact PILOT authorship must not erase the released field levers."""
        Door = Bool("Release_DoorClosed", external=True)
        LintDoor = Bool("Release_LintDoorClosed", external=True)
        DoorImage = Bool("Release_DoorImage")
        LintDoorImage = Bool("Release_LintDoorImage")
        Enter = Bool("Release_Enter", external=True)
        State = Int("Release_State", default=3)

        with Program() as prog:
            with Rung(Door):
                out(DoorImage)
            with Rung(LintDoor):
                out(LintDoorImage)
            with Rung(Enter):
                copy(6, State)
            with Rung(State == 6, Or(~DoorImage, ~LintDoorImage)):
                copy(10, State)

        plc = PLC(prog, dt=0.010)
        _set_rungs(
            plc,
            [
                PilotRung(Door.name, True, State != 6),
                PilotRung(LintDoor.name, True, State != 6),
            ],
        )
        plc.patch({Enter.name: True})
        plc.step()
        assert plc.state.tags[State.name] == 6
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id

        plc.patch({Enter.name: False})
        plc.step()
        departure_scan = plc.state.scan_id
        assert plc.state.tags[State.name] == 10

        incident = DeviationIncident(
            anchor_scan=anchor,
            departure_scan=departure_scan,
            end_scan=departure_scan,
            action=(),
            bearing=((State.name, 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=(Door.name, LintDoor.name, State.name),
            departures=(BearingDeparture(State.name, 6, departure_scan),),
            channel_tag=State.name,
        )

        hypotheses = _precise_causes(plc, incident, _make_ctx(prog, plc))

        assert hypotheses
        hypothesis = hypotheses[0]
        assert set(hypothesis.holds) == {
            (Door.name, True),
            (LintDoor.name, True),
        }

    def test_newly_conductive_enablers_break_actual_writer(self):
        Enter = Bool("Precise_EnterExecute", external=True)
        Door = Bool("Precise_DoorClosed", external=True)
        LintDoor = Bool("Precise_LintDoorClosed", external=True)
        FirstScan = Bool("Precise_FirstScan", external=True)
        Execute = Bool("Precise_StateExecute")
        State = Int("Precise_State")
        Cmd = Int("Precise_Command")
        Requested = Int("Precise_StateRequested")
        Unrelated = Int("Precise_Unrelated")

        with Program() as prog:
            with Rung(State == 6):
                out(Execute)
            with Rung(Enter):
                copy(6, State)
            with Rung(Execute, Or(~Door, ~LintDoor)):
                copy(4, Cmd)
            with Rung(Cmd == 4):
                copy(10, Requested)
            with Rung(Requested != 0):
                copy(Requested, State)
                copy(0, Requested)
                copy(0, Cmd)
            # A steady false default elsewhere in the program is support for
            # nothing on the fired causal path and must never become a hold.
            with Rung(~FirstScan):
                copy(0, Unrelated)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Precise_EnterExecute": True})
        plc.step()
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id
        plc.patch({"Precise_EnterExecute": False})
        plc.step()
        departure_scan = plc.state.scan_id

        incident = DeviationIncident(
            anchor_scan=anchor,
            departure_scan=departure_scan,
            end_scan=departure_scan,
            action=(),
            bearing=(("Precise_State", 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=("Precise_State",),
            departures=(BearingDeparture("Precise_State", 6, departure_scan),),
            channel_tag="Precise_State",
        )

        hypotheses = _precise_causes(plc, incident, _make_ctx(prog, plc))

        assert hypotheses
        hypothesis = hypotheses[0]
        assert hypothesis.kind == "precise-cause"
        assert {(hold.dest, hold.value) for hold in hypothesis.holds} == {
            ("Precise_DoorClosed", True),
            ("Precise_LintDoorClosed", True),
        }
        assert all(isinstance(hold, PilotRung) for hold in hypothesis.holds)
        assert all(hold.guard.tag.name == "Precise_State" for hold in hypothesis.holds)
        assert all(hold.guard.value == 6 for hold in hypothesis.holds)
        assert all(hold.dest != "Precise_FirstScan" for hold in hypothesis.holds)
        assert "R3 fired" in hypothesis.detail
        assert "minimal conductive cut" in hypothesis.detail

    def test_disabled_out_writer_is_not_treated_as_suppressed(self):
        """OUT writes False when its guard is false; false is not suppression."""
        Door = Bool("OutCut_Door", external=True)
        Init = Bool("OutCut_Init", external=True)
        Image = Bool("OutCut_Image")
        State = Int("OutCut_State")

        with Program() as prog:
            with Rung(Door):
                out(Image)
            with Rung(Init):
                copy(6, State)
            with Rung(~Image):
                copy(10, State)

        plc = PLC(prog, dt=0.010)
        plc.patch({"OutCut_Door": True})
        plc.step()
        plc.patch({"OutCut_Init": True})
        plc.step()
        before = dict(plc.state.tags)
        plc.patch({"OutCut_Door": False, "OutCut_Init": False})
        plc.step()
        scan = plc.state.scan_id
        incident = DeviationIncident(
            anchor_scan=scan - 1,
            departure_scan=scan,
            end_scan=scan,
            action=(),
            bearing=(("OutCut_State", 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=("OutCut_Door", "OutCut_Image", "OutCut_State"),
            departures=(BearingDeparture("OutCut_State", 6, scan),),
            channel_tag="OutCut_State",
        )

        hypotheses = _precise_causes(
            plc,
            incident,
            _make_ctx(prog, plc, steerable=frozenset({"OutCut_Door"})),
        )

        chain = plc.cause("OutCut_State", scan=scan)
        assert chain is not None
        assert any(
            step.transition.tag_name == "OutCut_Image" and step.transition.to_value is False
            for step in chain.steps
        )
        assert all(("OutCut_Door", False) not in h.holds for h in hypotheses)


# ---------------------------------------------------------------------------
# _latch_exposure_hypotheses — alarm latches that fired on state entry
# ---------------------------------------------------------------------------


class TestLatchExposureHypotheses:
    """_latch_exposure_hypotheses: alarm latches fired on state entry.

    A latch active after the move and gated by a state we were already in
    latched *because* of the move.  Its non-state guards are preconditions we
    failed to establish — flip each to the value that breaks the latch and
    resolve it to its steerable driver.
    """

    def test_latch_guard_resolved_to_steerable(self):
        Enter = Bool("Enter", external=True)
        Guard = Bool("Guard", external=True)
        State = Bool("State")
        Alarm = Bool("Alarm")
        with Program() as prog:
            with Rung(Enter):
                out(State)
            with Rung(State, ~Guard):
                latch(Alarm)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Enter", True),),
            bearing=(("Alarm", False),),
            before_snap={"State": True, "Guard": False},
            after_snap={"State": True, "Guard": False, "Alarm": True},
            changed_tags=("Alarm",),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        # The latch's non-state guard (Guard=False) flips to True to break it.
        # This hand-built incident carries no recorded activation chain, so no
        # channel scope is invented.
        assert len(hyps) == 1
        assert hyps[0].kind == "latch-exposure"
        assert hyps[0].holds == (("Guard", True),)
        assert "Alarm" in hyps[0].sources

    def test_conjunction_proposed_when_multiple_latches(self):
        Enter = Bool("Enter", external=True)
        G1 = Bool("G1", external=True)
        G2 = Bool("G2", external=True)
        State = Bool("State")
        A1 = Bool("A1")
        A2 = Bool("A2")
        with Program() as prog:
            with Rung(Enter):
                out(State)
            with Rung(State, ~G1):
                latch(A1)
            with Rung(State, ~G2):
                latch(A2)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Enter", True),),
            bearing=(("A1", False), ("A2", False)),
            before_snap={"State": True, "G1": False, "G2": False},
            # Deliberately reverse latch insertion order: hypothesis order is
            # semantic/deterministic, not an artifact of snapshot construction.
            after_snap={"State": True, "G1": False, "G2": False, "A2": True, "A1": True},
            changed_tags=("A1", "A2"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        # Two per-latch hypotheses plus one conjunction clearing both.
        assert len(hyps) == 3
        per_latch = [h for h in hyps if len(h.holds) == 1]
        conjunction = [h for h in hyps if len(h.holds) == 2]

        def _pairs(holds):
            return {(h.dest, h.value) if isinstance(h, PilotRung) else h for h in holds}

        assert {frozenset(_pairs(h.holds)) for h in per_latch} == {
            frozenset({("G1", True)}),
            frozenset({("G2", True)}),
        }
        assert [h.sources[0] for h in per_latch] == ["A1", "A2"]
        assert len(conjunction) == 1
        assert _pairs(conjunction[0].holds) == {("G1", True), ("G2", True)}


# ---------------------------------------------------------------------------
# _done_boundary_hypotheses — owner-declared watchdog reset operations
# ---------------------------------------------------------------------------


class TestLivenessHypotheses:
    """_done_boundary_hypotheses: watchdog reset operations.

    A resettable owner reports the operation that clears its recorded
    completion. Direct contacts complete in one scan; intermediate owners can
    retain longer boundaries and progress receipts.
    """

    def test_complement_reset_watchdog_produces_conditional_hold(self):
        # One watchdog resets on ~Sensor (counts while True): the only resetting
        # polarity is False, so the hold drives Sensor->False while it is != False.
        Sensor = Bool("Sensor", external=True)
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(~Sensor)
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Sensor": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True  # watchdog fired

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("Sensor", True),),
            bearing=(("Err", False),),
            before_snap={"Sensor": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        (proposal,) = hyps[0].holds
        assert isinstance(proposal, PilotRung)
        assert (proposal.dest, proposal.value) == ("Sensor", False)
        assert proposal.operation is not None

    def test_recorded_watchdog_yields_only_its_reset_operation(self):
        # The opposite watchdog is structurally present but did not complete in
        # this incident. Its remedy belongs to a later recorded occurrence, not
        # to a guessed complementary behavior category.
        Sensor = Bool("Sensor", external=True)
        OffWD = Timer.clone("OffWD")  # resets on Sensor -> counts while False
        OnWD = Timer.clone("OnWD")  # resets on ~Sensor -> counts while True
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(OffWD, 30, "ms").reset(Sensor)
            with Rung():
                on_delay(OnWD, 30, "ms").reset(~Sensor)
            with Rung(Or(OffWD.Done, OnWD.Done)):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Sensor": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["OnWD_Done"] is True  # the on-watchdog fired

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("Sensor", True),),
            bearing=(("Err", False),),
            before_snap={"Sensor": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("OnWD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        (proposal,) = hyps[0].holds
        assert isinstance(proposal, PilotRung)
        assert (proposal.dest, proposal.value) == ("Sensor", False)
        assert proposal.operation is not None

    def test_only_fired_watchdogs_proposed(self):
        S1 = Bool("S1", external=True)
        S2 = Bool("S2", external=True)
        W1 = Timer.clone("W1")
        W2 = Timer.clone("W2")
        E = Bool("E")
        with Program() as prog:
            with Rung():
                on_delay(W1, 30, "ms").reset(~S1)
            with Rung():
                on_delay(W2, 30, "ms").reset(~S2)
            with Rung(W1.Done):
                out(E)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(),
            bearing=(("E", False),),
            before_snap={},
            after_snap={"S1": True, "S2": True},
            changed_tags=("W1_Done",),  # only W1 fired; W2 did not
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        proposed = {h.holds[0].dest for h in hyps}
        assert proposed == {"S1"}


def _shaft_rotate_program() -> Program:
    """A shaft-rotate feedback that must keep *pulsing* while a delay counts up.

    The canonical liveness shape, self-contained: a rotation sensor guarded by
    two complement-reset watchdogs, and a run delay that only advances while no
    watchdog has faulted.

    - ``x_Rotate`` (external): the shaft-rotation feedback bit.
    - ``SensorOffWD``: resets on ``x_Rotate`` -> counts while the sensor is
      *False* -> trips if rotation stalls off.
    - ``SensorOnWD``: resets on ``~x_Rotate`` -> counts while the sensor is
      *True* -> trips if rotation sticks on.
    - either Done -> ``Fault`` latches -> ``RunDelay`` (gated by ``~Fault``)
      resets and can never complete.
    - ``RunDelay`` Done -> ``Running`` (the target).

    Steady at either polarity faults within 50 ms; only a sensor that oscillates
    faster than 50 ms keeps both watchdogs reset long enough for the 200 ms
    ``RunDelay`` to reach ``Running``.
    """
    Rotate = Bool("x_Rotate", external=True)
    SensorOffWD = Timer.clone("SensorOffWD")
    SensorOnWD = Timer.clone("SensorOnWD")
    RunDelay = Timer.clone("RunDelay")
    Fault = Bool("Fault")
    Running = Bool("Running")
    with Program() as prog:
        with Rung():
            on_delay(SensorOffWD, 50, "ms").reset(Rotate)
        with Rung():
            on_delay(SensorOnWD, 50, "ms").reset(~Rotate)
        with Rung(Or(SensorOffWD.Done, SensorOnWD.Done)):
            latch(Fault)
        with Rung(~Fault):
            on_delay(RunDelay, 200, "ms")
        with Rung(RunDelay.Done):
            out(Running)
    return prog


def _coast_holding_to_trip(plc: PLC, polarity: bool, limit: int = 60) -> DeviationIncident:
    """Hold ``x_Rotate`` steady at *polarity* and step until a sensor watchdog
    fires, then return the bounded incident over that coast span — the faithful
    analogue of a terminal let-run that ejects on a watchdog."""
    wd = ("SensorOffWD_Done", "SensorOnWD_Done")
    anchor = plc.state.scan_id
    before = dict(plc.state.tags)
    for _ in range(limit):
        plc.force("x_Rotate", polarity)
        plc.step()
        if any(plc.state.tags.get(n) for n in wd):
            break
    return build_deviation_incident(
        anchor_scan=anchor,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(("Running", True),),
        before_snap=before,
        after_snap=dict(plc.state.tags),
    )


class TestShaftRotateLiveness:
    """The shaft-rotate scenario end to end: a bit that must keep pulsing while a
    delay counts up, driven by a structurally-synthesized :class:`PilotRung`.
    """

    def test_delay_needs_pulsing(self):
        # The premise: a steady sensor faults and the delay never completes;
        # only an oscillating sensor lets RunDelay count up to Running.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.force("x_Rotate", False)
        for _ in range(30):
            plc.step()
        assert plc.state.tags["Fault"] is True
        assert plc.state.tags["Running"] is False

        prog2 = _shaft_rotate_program()
        plc2 = PLC(prog2, dt=0.010)
        val = True
        for i in range(40):
            if i % 3 == 0:  # flip every 3 scans (30 ms) — faster than the 50 ms WDs
                val = not val
            plc2.force("x_Rotate", val)
            plc2.step()
        assert plc2.state.tags["Fault"] is False
        assert plc2.state.tags["Running"] is True

    def test_ejection_synthesizes_the_recorded_owner_operation(self):
        # Park the sensor off and let SensorOffWD trip. The correction asks only
        # how that recorded owner resets and retains the resulting operation.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        ctx = _make_ctx(prog, plc)
        incident = _coast_holding_to_trip(plc, False)
        assert "SensorOffWD_Done" in incident.changed_tags

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        (proposal,) = hyps[0].holds
        assert isinstance(proposal, PilotRung)
        assert (proposal.dest, proposal.value) == ("x_Rotate", True)
        assert proposal.operation is not None

    def test_synthesized_hold_oscillates_to_target(self):
        # A direct assignment operation completes at x_Rotate=True and releases
        # to the Boolean resting value. Repeating that structural operation
        # naturally produces the required edges without an OSCILLATE category.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        ctx = _make_ctx(prog, plc)
        incident = _coast_holding_to_trip(plc, False)
        rungs = correct_enablers(plc, incident, ctx)[0].holds

        fresh = PLC(_shaft_rotate_program(), dt=0.010)
        fresh.step()
        _set_rungs(fresh, list(rungs))
        reached = _coast_holding_state(fresh, "Running", True, (), budget=200)
        assert reached.reached is True
        assert fresh.state.tags["Running"] is True
        assert fresh.state.tags["Fault"] is False

    def test_mapped_contact_keeps_its_trace_handoff_as_an_operation(self):
        """A plain input map must not erase the watchdog reset operation."""
        physical = Bool("MappedRotate", external=True)
        contact = Bool("MappedRotateContact")
        watchdog = Timer.clone("MappedRotateWD")
        with Program() as program:
            with Rung(physical):
                out(contact)
            with Rung():
                on_delay(watchdog, 50, "ms").reset(contact)

        plc = PLC(program, dt=0.010)
        plc.step()
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id
        for _ in range(6):
            plc.step()
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=((watchdog.Done.name, False),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
        )

        hypotheses = correct_enablers(plc, incident, _make_ctx(program, plc))

        assert len(hypotheses) == 1
        (operation,) = hypotheses[0].holds
        assert (operation.dest, operation.value) == (physical.name, True)
        assert operation.operation is not None
        assert operation.operation.until.tag == physical.name


# ---------------------------------------------------------------------------
# Multi-read reset/advance conditions — coordinated-hold generalization
#
# A reset/advance guard that is a *conjunction* of inputs used to be skipped
# ("no single unambiguous lever").  ``correct_enablers`` now evaluates the real
# Condition over its reads' value spaces to find the minimal lever assignment
# producing the needed polarity, and proposes coordinated holds.
# ---------------------------------------------------------------------------


def _conj_reset_target_program() -> Program:
    """A watchdog reset by ``And(A, B)`` gates a run-delay toward ``Running``.

    Only holding *both* A and B True keeps the watchdog reset long enough for the
    200 ms ``RunDelay`` to complete — the coordinated-conjunction analogue of the
    complement-reset shaft-rotate scenario.
    """
    A = Bool("A", external=True)
    B = Bool("B", external=True)
    WD = Timer.clone("WD")
    RunDelay = Timer.clone("RunDelay")
    Fault = Bool("Fault")
    Running = Bool("Running")
    with Program() as prog:
        with Rung():
            on_delay(WD, 50, "ms").reset(And(A, B))
        with Rung(WD.Done):
            latch(Fault)
        with Rung(~Fault):
            on_delay(RunDelay, 200, "ms")
        with Rung(RunDelay.Done):
            out(Running)
    return prog


class TestMultiReadCorrections:
    """Conjunctive reset/advance guards yield coordinated multi-tag corrections."""

    def test_conjunction_reset_yields_coordinated_oscillation(self):
        # (a) Two-Bool conjunction reset: only A AND B True resets the watchdog,
        # so a single lever can never satisfy it — the correction pairs both.
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(And(A, B))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"A": True, "B": False})  # reset never satisfied -> WD counts
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("A", True),),
            bearing=(("Err", False),),
            before_snap={"A": True, "B": False},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        held = {r.dest: r for r in hyps[0].holds}
        assert set(held) == {"A", "B"}
        for tag in ("A", "B"):
            assert isinstance(held[tag], PilotRung)
            assert held[tag].value is True

    def test_bool_int_conjunction_reset_resolved_via_choices(self):
        # (b) Bool+int conjunction: the int lever's domain comes from the tag's
        # declared choices, so ``Mode == 2`` resolves to the concrete value 2.
        Enable = Bool("Enable", external=True)
        Mode = Int("Mode", external=True, choices={1: "Idle", 2: "Run", 3: "Stop"})
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(And(Enable, Mode == 2))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True, "Mode": 1})  # Mode != 2 -> reset unsatisfied
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Err", False),),
            before_snap={"Enable": True, "Mode": 1},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        held = {r.dest: r for r in hyps[0].holds}
        assert set(held) == {"Enable", "Mode"}
        assert held["Enable"].value is True
        assert held["Mode"].value == 2

    def test_conjunction_with_unsteerable_read_declined(self):
        # (c) A conjunct that resolves to no steerable driver makes the whole
        # coordinated hold unsteerable — decline exactly as the single-read path did.
        A = Bool("A", external=True)
        Locked = Bool("Locked", readonly=True)  # a constant PILOT cannot steer
        Internal = Bool("Internal")
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung(Locked):
                out(Internal)  # Internal only rises via the unsteerable Locked
            with Rung():
                on_delay(WD, 30, "ms").reset(And(A, Internal))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"A": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Err", False),),
            before_snap={"A": True, "Internal": False},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        assert correct_enablers(plc, incident, ctx) == []

    def test_conjunction_advance_yields_single_lever_freeze(self):
        # (d) A conjunction *advance* stops as soon as ONE conjunct breaks, so the
        # cannot-hold correction is a single cheapest lever — not both.
        run1 = Bool("run1", external=True)
        run2 = Bool("run2", external=True)
        T = Timer.clone("T")
        Out = Bool("Out")
        with Program() as prog:
            with Rung(And(run1, run2)):
                on_delay(T, 50, "ms")
            with Rung(T.Done):
                out(Out)

        plc = PLC(prog, dt=0.010)
        plc.patch({"run1": True, "run2": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["T_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Out", False),),
            before_snap={"run1": True, "run2": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("T_Done", "Out"),
            departures=(),
        )

        done_boundary = [
            h for h in correct_enablers(plc, incident, ctx) if h.kind == "done-boundary"
        ]
        assert len(done_boundary) == 1
        ((tag, value),) = done_boundary[0].holds
        assert tag in {"run1", "run2"}
        assert value is False

    def test_coordinated_conjunction_hold_reaches_target(self):
        # End to end: the synthesized coordinated pair, installed together on a
        # coast, keeps the And(A, B) watchdog reset so RunDelay reaches Running.
        prog = _conj_reset_target_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        anchor = plc.state.scan_id
        before = dict(plc.state.tags)
        for _ in range(80):
            plc.force("A", True)
            plc.force("B", False)  # one conjunct low -> watchdog trips
            plc.step()
            if plc.state.tags.get("WD_Done"):
                break
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Running", True),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
        )
        assert "WD_Done" in incident.changed_tags

        ctx = _make_ctx(prog, plc)
        rungs = correct_enablers(plc, incident, ctx)[0].holds
        assert {r.dest for r in rungs} == {"A", "B"}

        fresh = PLC(_conj_reset_target_program(), dt=0.010)
        fresh.step()
        _set_rungs(fresh, list(rungs))
        reached = _coast_holding_state(fresh, "Running", True, (), budget=300)
        assert reached.reached is True
        assert fresh.state.tags["Running"] is True
        assert fresh.state.tags["Fault"] is False


# ---------------------------------------------------------------------------
# investigate_excursion — diagnose a state-key revert, replay-validate holds
# ---------------------------------------------------------------------------


def _seal_in_program() -> Program:
    """``Out`` fires on a rising ``Command`` edge but only *stays* up if sealed
    in by ``Hold``.  Pulsed (edge) without the hold, ``Out`` reverts — exactly
    the excursion shape verify detects.
    """
    Command = Bool("Command", external=True)
    Hold = Bool("Hold", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(Or(rise(Command), And(Out, Hold))):
            out(Out)
    return prog


def _excursion_inputs():
    """Build the (work, fork, snaps, key, cfg, steerable) for an excursion.

    ``work`` rests at Out=False; ``fork`` reproduces the pulse where Out went
    True then reverted to False after the edge released (no hold installed).
    """
    prog = _seal_in_program()
    work = PLC(prog, dt=0.010)
    work.patch({"Command": False, "Hold": False})
    work.step()

    cfg = _StateKeyConfig(
        stateful_names=("Out",),
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    pre_snap = dict(work.state.tags)
    pre_key = _pilot_state_key(pre_snap, cfg)

    fork = work.fork()
    fork.patch({"Command": False})
    fork.step()
    fork.patch({"Command": True})
    fork.step()
    post_pulse_snap = dict(fork.state.tags)
    for _ in range(4):
        fork.step()

    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    return work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable


class TestInvestigateExcursion:
    """investigate_excursion: state-key excursion diagnosis and hold-based retry."""

    def test_reverted_tags_diagnosed(self):
        work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable = _excursion_inputs()
        # Out was True at the end of the pulse but reverted to its pre value.
        assert post_pulse_snap["Out"] is True
        assert fork.state.tags["Out"] is False

        result = investigate_excursion(
            work,
            fork,
            pre_snap,
            post_pulse_snap,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        assert result.reverted == ["Out"]

    def test_confirmed_correction_fixes_revert(self):
        work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable = _excursion_inputs()

        result = investigate_excursion(
            work,
            fork,
            pre_snap,
            post_pulse_snap,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        # Sealing Hold=True keeps Out latched across the edge release — the
        # retry key differs from the (reverted) pre key, so the hold is kept.
        assert result.correction is not None
        assert ("Hold", True) in tuple((rung.dest, rung.value) for rung in result.correction.rungs)
        guard = result.correction.rungs[0].guard
        assert guard.evaluate(_SnapshotView({"Out": True}, {}))
        assert not guard.evaluate(_SnapshotView({"Out": False}, {}))
        assert result.retry_fork is not None


# ---------------------------------------------------------------------------
# Generalized antagonist dispatch — any causally-implicated clobbering writer
#
# The old excursion path only recognized ``ResetInstruction`` antagonists.  The
# dispatch is now by causal implication (``cause()``) + producibility, so a plain
# clobbering ``copy`` is suppressed by forcing its guard FALSE — and a live-word
# guard escalates to the skiff.  Both flow through the same replay-retry gate.
# ---------------------------------------------------------------------------


def _run_excursion(
    prog: Program,
    *,
    setup_patch: dict[str, Any],
    action_tag: str,
    stateful: tuple[str, ...],
    extra_resting: dict[str, Any] | None = None,
):
    """Drive one pulse-then-revert excursion and return investigate_excursion inputs.

    ``work`` rests at the pre-state; ``fork`` reproduces the pulse where the
    stateful register moved (post_pulse) then was clobbered back after settling.
    """
    work = PLC(prog, dt=0.010)
    work.patch(setup_patch)
    work.step()
    cfg = _StateKeyConfig(
        stateful_names=stateful,
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    pre_snap = dict(work.state.tags)
    pre_key = _pilot_state_key(pre_snap, cfg)

    fork = work.fork()
    fork.patch({action_tag: False})
    fork.step()
    fork.patch({action_tag: True})
    fork.step()
    post_pulse_snap = dict(fork.state.tags)
    for _ in range(4):
        fork.step()

    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    resting = {action_tag: False, **(extra_resting or {})}
    return work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable, pdg, resting


def _clobber_copy_program() -> Program:
    """A non-Reset clobbering copy with a compound int guard.

    ``State`` is set to 5 on a rising ``Command`` edge, but ``copy(0, State)``
    gated by ``And(Internal, Mode == 2)`` clobbers it back every scan.
    ``Internal`` rides an unsteerable ``readonly`` latch, so the only steerable
    lever is the int ``Mode``.  The old bool-only fallback cannot flip an int
    comparison (this excursion is *unresolved* today); the generalized dispatch
    suppresses the copy by forcing its guard FALSE via the int-domain forcing
    enumeration (``Mode -> 1``), a value the ``copy`` can never turn into a 5.
    """
    Command = Bool("Command", external=True)
    Locked = Bool("Locked", readonly=True)
    Internal = Bool("Internal")
    Mode = Int("Mode", external=True, choices={1: "Idle", 2: "Run", 3: "Stop"})
    State = Int("State")
    with Program() as prog:
        with Rung(Locked):
            out(Internal)
        with Rung(And(Internal, Mode == 2)):
            copy(0, State)
        with Rung(rise(Command)):
            copy(5, State)
    return prog


def _liveword_clobber_program() -> Program:
    """A clobbering copy gated by a genuinely-live (calc-computed) word.

    ``Sel`` selects a raw mask (4 or 0), ``Mask`` is ``RawMask & 4`` — a *calc*
    output, so its finite domain is unreadable and the guard-force enumeration
    **punts**.  The clobber ``copy(0, State)`` fires while ``Mask != 0``.  Only the
    skiff can find the suppressing lever: a bounded isolated probe holding the
    condition-read Bool ``Sel`` False clears the mask, so the antagonist stops
    firing — a nomination the replay-retry gate then confirms.
    """
    Command = Bool("Command", external=True)
    Sel = Bool("Sel", external=True)
    RawMask = Int("RawMask")
    Mask = Int("Mask")
    State = Int("State")
    with Program() as prog:
        with Rung(Sel):
            copy(4, RawMask)
        with Rung(~Sel):
            copy(0, RawMask)
        with Rung():
            calc(RawMask & 4, Mask)
        with Rung(Mask != 0):
            copy(0, State)
        with Rung(rise(Command)):
            copy(5, State)
    return prog


class TestGeneralizedAntagonistExcursion:
    """investigate_excursion suppresses any causally-implicated clobbering writer,
    not just ``ResetInstruction`` — via guard-force enumeration, with a skiff
    escalation for a live-word guard.  Every hold rides the existing retry gate."""

    def test_non_reset_copy_clobber_now_corrected(self):
        # Compound int guard: unresolved under the old ResetInstruction dispatch,
        # corrected by forcing the copy's guard FALSE (Mode -> 1).
        prog = _clobber_copy_program()
        (work, fork, pre, post, pre_key, cfg, steerable, pdg, resting) = _run_excursion(
            prog,
            setup_patch={"Command": False, "Mode": 2, "Locked": True},
            action_tag="Command",
            stateful=("State",),
        )
        assert post["State"] == 5  # pulse established the value
        assert fork.state.tags["State"] == 0  # then it was clobbered back

        result = investigate_excursion(
            work,
            fork,
            pre,
            post,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert result.correction is not None
        assert ("Mode", 1) in tuple((rung.dest, rung.value) for rung in result.correction.rungs)
        assert result.retry_fork is not None
        # The suppression preserved the pulse-established value across the settle.
        assert result.retry_fork.state.tags["State"] == 5

    def test_live_word_guard_uses_skiff_probe(self):
        # The clobber's guard reads a calc-computed word: guard-force enumeration
        # punts, and the skiff's isolated probe nominates the condition-read Bool
        # Sel=False, which the retry gate confirms.
        prog = _liveword_clobber_program()
        (work, fork, pre, post, pre_key, cfg, steerable, pdg, resting) = _run_excursion(
            prog,
            setup_patch={"Command": False, "Sel": True},
            action_tag="Command",
            stateful=("State",),
            extra_resting={"Sel": False},
        )
        assert post["State"] == 5
        assert fork.state.tags["State"] == 0

        result = investigate_excursion(
            work,
            fork,
            pre,
            post,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert result.correction is not None
        assert ("Sel", False) in tuple((rung.dest, rung.value) for rung in result.correction.rungs)
        assert result.retry_fork is not None
        assert result.retry_fork.state.tags["State"] == 5


# ---------------------------------------------------------------------------
# Incident construction + internal helpers
# ---------------------------------------------------------------------------


class TestDedupePairs:
    def test_preserves_first_occurrence_order(self):
        pairs = [("a", 1), ("b", 2), ("a", 1), ("c", 3)]
        assert _dedupe_pairs(pairs) == [("a", 1), ("b", 2), ("c", 3)]


class TestHoldAllowed:
    def test_rejects_action_tags(self):
        ctx = SimpleNamespace(compass=SimpleNamespace(action_tags=frozenset({"x"})))
        assert _hold_allowed(ctx, ("x", True)) is False
        assert _hold_allowed(ctx, ("y", True)) is True

    def test_rejects_blocked_action(self):
        ctx = SimpleNamespace(
            compass=SimpleNamespace(action_tags=frozenset()),
            blocked_actions=frozenset({("blocked", True)}),
        )
        assert _hold_allowed(ctx, ("blocked", True)) is False
        assert _hold_allowed(ctx, ("ok", True)) is True


def _change_program() -> Program:
    A = Bool("A", external=True)
    B = Bool("B")
    with Program() as prog:
        with Rung(A):
            out(B)
    return prog


class TestFirstTimelineDeparture:
    """``_first_timeline_departure`` reads the departure scan straight off the
    recorded receipt timeline — the pen mark IS the departure scan, never a
    history re-scan."""

    def test_finds_first_transition_off_value(self):
        timeline = (
            CoastTriggerEvent("pen", "pen", 5, (("B", False, True),)),
            CoastTriggerEvent("pen", "pen", 9, (("B", True, False),)),
        )
        assert _first_timeline_departure(timeline, "B", False) == 5

    def test_departure_is_relative_to_the_queried_value(self):
        # A single True -> False transition is a departure off True (scan 3),
        # not off False (which it lands on).
        timeline = (CoastTriggerEvent("pen", "pen", 3, (("B", True, False),)),)
        assert _first_timeline_departure(timeline, "B", True) == 3
        assert _first_timeline_departure(timeline, "B", False) is None

    def test_returns_the_first_of_several(self):
        timeline = (
            CoastTriggerEvent("pen", "pen", 4, (("B", False, True),)),
            CoastTriggerEvent("pen", "pen", 8, (("B", False, True),)),
        )
        assert _first_timeline_departure(timeline, "B", False) == 4

    def test_no_matching_tag_returns_none(self):
        timeline = (CoastTriggerEvent("pen", "pen", 7, (("A", False, True),)),)
        assert _first_timeline_departure(timeline, "B", False) is None

    def test_empty_timeline_returns_none(self):
        assert _first_timeline_departure((), "B", False) is None


def _oscillating_done_program() -> Program:
    """A complement-reset timer whose Done bit *pulses* — False -> True -> False
    each period — plus a latch that fires (and stays) the first time it does.
    The pens must record both Done transitions and the latch's single rise."""
    T = Timer.clone("T")
    Latched = Bool("Latched")
    with Program() as prog:
        with Rung(~T.Done):
            on_delay(T, 30, "ms")  # Done oscillates: ~3 scans off, 1 scan on
        with Rung(T.Done):
            latch(Latched)  # the first Done rise latches Latched permanently
    return prog


class TestPens:
    """CoastSession pens record mid-coast transitions onto the timeline so a
    fire-then-reset watchdog pulse is two recorded events, and incident
    construction reads changed tags + departure scans straight off them."""

    def test_pens_capture_fire_and_reset_onto_the_timeline(self):
        plc = PLC(_oscillating_done_program(), dt=0.010)
        session = CoastSession(plc, kind="test")
        session.arm_pens(("T_Done", "Latched"))
        session.dwell(20)

        pens = [e for e in session.events if e.kind == "pen"]
        rises = [
            e.scan
            for e in pens
            for t, b, a in e.transitions
            if t == "T_Done" and b is False and a is True
        ]
        falls = [
            e.scan
            for e in pens
            for t, b, a in e.transitions
            if t == "T_Done" and b is True and a is False
        ]
        # Both edges of the pulse landed as recorded pen marks with exact scans.
        assert rises and falls
        latched_scan = next(
            e.scan for e in pens for t, _b, a in e.transitions if t == "Latched" and a is True
        )

        # The Done bit fired and reset inside the window, so its endpoint diff is
        # a net no-op (before == after == False) — only the timeline carries it.
        incident = build_deviation_incident(
            anchor_scan=0,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("T_Done", False), ("Latched", False)),
            before_snap={"T_Done": False, "Latched": False},
            after_snap={"T_Done": False, "Latched": True},
            timeline=tuple(session.events),
        )
        assert "T_Done" in incident.changed_tags  # recovered from the timeline
        assert "Latched" in incident.changed_tags
        # The departure scan comes off the timeline, not a history re-diff.
        dep = {d.tag: d.scan for d in incident.departures}
        assert dep["Latched"] == latched_scan
        assert incident.departure_scan == latched_scan


class TestBuildDeviationIncident:
    def test_captures_changes_and_departures(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.patch({"A": True})
        plc.step()
        # The recorded evidence: B departed False -> True the scan A latched it.
        timeline = (CoastTriggerEvent("pen", "pen", plc.state.scan_id, (("B", False, True),)),)
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(("A", True),),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
            timeline=timeline,
        )
        assert "B" in incident.changed_tags
        # B departed from its bearing (False) inside the window.
        assert len(incident.departures) == 1
        assert incident.departures[0].tag == "B"
        assert incident.departure_scan == plc.state.scan_id

    def test_no_departure_when_bearing_held(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.step()  # B stays False — bearing held
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
        )
        assert incident.departures == ()
        assert incident.departure_scan is None


# ---------------------------------------------------------------------------
# Terminal coast receipts are ordinary durable Compass knowledge.
# ---------------------------------------------------------------------------


def test_terminal_coast_receipt_is_typed_navigation_knowledge() -> None:
    from pyrung.core.analysis.pilot.compass import CoastObservation, Compass

    key = ("world-key",)
    compass, changed = Compass().apply((CoastObservation(key, "quiescent"),))
    assert changed
    assert compass.knowledge.coast_receipt(key) == "quiescent"
