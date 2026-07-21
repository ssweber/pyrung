"""A held enabler that re-inits/pins a *needed* progress register is self-defeating.

The burner's rotate-liveness investigation confirmed a bundle of holds that each
individually kept Execute but, applied together, pinned progress forever:

  * ``Heat_xInit=1`` forces the shared-init rung (``Or(Heat_xInit==1, ...)``) that
    fills ``Heat_CurStep := 1`` every scan, while the target needs
    ``Heat_CurStep = 3`` — the burner can never advance.
  * ``Rotate_xPause=1`` forces the pause rung that copies ``Rotate_CurStep := 0``.

The confirmation window is too short to observe the lost progress (the payoff is a
~1000-scan coast away), so ``hold_defeats_needed`` catches it statically: a hold
whose steady value *forces* a rung writing a needed register to a contradicting
literal is self-defeating.  The correct lever (oscillate the rotate sensor) gates
no writer of a needed register, so it survives.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyrsistent import pvector

from pyrung import PLC, Bool, Int, Or, Program, copy, fill, out, rise, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import OperationReceipt, PilotRung
from pyrung.core.analysis.pilot.investigate import (
    DeviationIncident,
    InvestigationHypothesis,
    InvestigationResult,
    ReplayOutcome,
    _active_rungs_defeat_needed,
    _precise_causes,
    correction_identity,
    hold_defeats_needed,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.pilot import _record_attempt
from pyrung.core.analysis.pilot.progress import (
    _install_confirmed_correction,
    _investigate_and_revert,
    _monitor_trend,
)
from pyrung.core.analysis.pilot.steer import _install_prerequisites
from pyrung.core.analysis.pilot.trace import frontier_pairs, trace_back
from pyrung.core.analysis.pilot.types import (
    BearingDeparture,
    CorrectionStatus,
    _Checkpoint,
    _ConfirmedCorrection,
    _PilotState,
    _TrialResult,
    _World,
)
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.condition import AllCondition, CompareEq, CompareNe
from pyrung.core.crossing import Eq
from pyrung.core.memory_block import Block


def _pdg(prog: Program):
    return build_program_graph(prog)


def test_init_flag_pinning_needed_register_is_self_defeating():
    """Heat_xInit analog: hold InitFlag=1 -> re-init rung copies Counter:=1, but
    the target needs Counter=3."""
    InitFlag = Int("InitFlag")
    Counter = Int("Counter")
    Trigger = Bool("Trigger", external=True)

    with Program(strict=False) as prog:
        with rung(InitFlag == 1):
            copy(1, Counter)  # re-init: pins Counter at 1
        with rung(Trigger, Counter < 5):
            copy(Counter + 1, Counter)

    pdg = _pdg(prog)
    assert hold_defeats_needed("InitFlag", 1, [("Counter", 3)], pdg, prog) is True
    # A value that does NOT force the init rung is fine.
    assert hold_defeats_needed("InitFlag", 0, [("Counter", 3)], pdg, prog) is False


def test_or_disjunct_enabler_still_forces():
    """The shared-init rung fires on Or(InitFlag==1, ResetFlag==1): holding either
    disjunct alone forces it."""
    InitFlag = Int("InitFlag")
    ResetFlag = Int("ResetFlag")
    Counter = Int("Counter")

    with Program(strict=False) as prog:
        with rung(Or(InitFlag == 1, ResetFlag == 1)):
            copy(1, Counter)
        with rung(Counter >= 3):
            out(Bool("Done"))

    pdg = _pdg(prog)
    assert hold_defeats_needed("InitFlag", 1, [("Counter", 3)], pdg, prog) is True


def test_fill_target_block_is_detected():
    """Heat_CurStep is written by fill() over a block select — the literal-write
    reader must see it."""
    from pyrung.core.tag import TagType

    InitFlag = Int("InitFlag")
    ds = Block("ds", TagType.INT, 1, 20)
    Step = ds[10]

    with Program(strict=False) as prog:
        with rung(InitFlag == 1):
            fill(1, ds.select(10, 11))  # ds10..ds11 := 1
        with rung(Step >= 3):
            out(Bool("Fired"))

    pdg = _pdg(prog)
    assert hold_defeats_needed("InitFlag", 1, [(Step.name, 3)], pdg, prog) is True


def test_and_gated_enabler_not_forced_by_one_term():
    """A rung gated by And(Cmd==1, Other) is NOT forced by holding Cmd alone — the
    other conjunct is unknown, so the hold is not proven self-defeating."""
    Cmd = Int("Cmd")
    Other = Bool("Other", external=True)
    Counter = Int("Counter")

    with Program(strict=False) as prog:
        with rung(Cmd == 1, Other):
            copy(1, Counter)
        with rung(Counter >= 3):
            out(Bool("Done"))

    pdg = _pdg(prog)
    assert hold_defeats_needed("Cmd", 1, [("Counter", 3)], pdg, prog) is False


def test_benign_lever_not_self_defeating():
    """A hold that gates no writer of a needed register (the oscillate-the-sensor
    analog) is never self-defeating."""
    Sensor = Bool("Sensor", external=True)
    Counter = Int("Counter")

    with Program(strict=False) as prog:
        with rung(Sensor, Counter < 5):
            copy(Counter + 1, Counter)  # advances toward the goal, not away
        with rung(Counter >= 3):
            out(Bool("Done"))

    pdg = _pdg(prog)
    assert hold_defeats_needed("Sensor", True, [("Counter", 3)], pdg, prog) is False


def test_write_consistent_with_need_is_allowed():
    """A forced write that AGREES with the needed value is not self-defeating."""
    SetFlag = Int("SetFlag")
    Counter = Int("Counter")

    with Program(strict=False) as prog:
        with rung(SetFlag == 1):
            copy(3, Counter)  # forces Counter := 3, exactly what is needed
        with rung(Counter >= 3):
            out(Bool("Done"))

    pdg = _pdg(prog)
    assert hold_defeats_needed("SetFlag", 1, [("Counter", 3)], pdg, prog) is False


def test_direct_progress_cut_is_a_proven_self_defeat():
    """A causal cut may target the required register itself. It remains a
    hypothesis, but its contradiction of the requested frontier proves it
    harmful without a long replay."""
    State = Int("DirectCutState")
    with Program(strict=False) as prog:
        with rung(State == 6):
            out(Bool("DirectCutExecute"))

    pdg = _pdg(prog)
    assert hold_defeats_needed(State.name, 8, [(State.name, 6)], pdg, prog) is True
    assert hold_defeats_needed(State.name, 6, [(State.name, 6)], pdg, prog) is False


def test_exact_progress_cut_is_generated_then_rejected(monkeypatch):
    """Entering the requested state conducts a harmful mapping rung. Reverting
    that entry is a real exact-chain hypothesis; its contradiction of the
    requested state is recorded as self-defeat instead of being pruned during
    generation."""
    State = Int("GeneratedCutState", external=True)
    Channel = Int("GeneratedCutChannel")

    with Program(strict=False) as prog:
        with rung(State == 6):
            copy(8, Channel)

    plc = PLC(prog, dt=0.010)
    plc.patch({State.name: 0, Channel.name: 6})
    plc.step()
    before = dict(plc.state.tags)
    anchor = plc.state.scan_id
    plc.patch({State.name: 6})
    plc.step()
    scan = plc.state.scan_id

    pdg = build_program_graph(prog)
    ctx = SimpleNamespace(
        pdg=pdg,
        program=prog,
        steerable=frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog)),
        opaque_loop=frozenset({Channel.name}),
        pipeline_internal_tags=frozenset(),
        route=None,
        compass=SimpleNamespace(action_tags=frozenset()),
        target_tag=Channel.name,
        target_value=99,
    )
    incident = DeviationIncident(
        anchor_scan=anchor,
        departure_scan=scan,
        end_scan=scan,
        action=((State.name, 6),),
        bearing=((State.name, 6), (Channel.name, 6)),
        before_snap=before,
        after_snap=dict(plc.state.tags),
        changed_tags=(State.name, Channel.name),
        departures=(BearingDeparture(Channel.name, 6, scan),),
        channel_tag=Channel.name,
    )

    hypotheses = _precise_causes(plc, incident, ctx)
    progress_cut = next(h for h in hypotheses if (State.name, 0) in h.holds)

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    replayed: list[tuple[object, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(
            accepted=True,
            trend=None,
            snapshot={**before, Channel.name: 6},
            reason="short incident suppressed",
            landed=True,
        )

    result = investigate_deviation(
        plc,
        incident,
        ctx,
        replay,
    )

    assert replayed == [progress_cut.holds]
    assert result.correction is None
    assert result.rejected[0][0] == progress_cut
    assert result.rejection_slugs == ("self-defeat",)


def test_conditional_oscillating_hold_reaching_init_value_is_caught():
    """An oscillating hold (PilotRung) whose reachable values include the
    init-forcing one is self-defeating — the True phase re-inits every cycle."""
    InitFlag = Int("InitFlag")
    Counter = Int("Counter")

    with Program(strict=False) as prog:
        with rung(InitFlag == 1):
            copy(1, Counter)
        with rung(Counter >= 3):
            out(Bool("Done"))

    pdg = _pdg(prog)
    # Mimic a PilotRung oscillating InitFlag to 1.
    osc = SimpleNamespace(rules=(SimpleNamespace(value=1),))
    assert hold_defeats_needed("InitFlag", osc, [("Counter", 3)], pdg, prog) is True


def test_inactive_guard_does_not_prove_self_defeat():
    """A harmful value under a disjoint guard is not an executable pin here."""
    State = Int("GuardedState")
    InitFlag = Int("GuardedInitFlag")
    Counter = Int("GuardedCounter")

    with Program(strict=False) as prog:
        with rung(InitFlag == 1):
            copy(1, Counter)

    guarded = PilotRung(InitFlag.name, 1, CompareEq(State, 7))
    assert (
        _active_rungs_defeat_needed(
            (guarded,),
            ((Counter.name, 3),),
            {State.name: 6},
            _pdg(prog),
            prog,
        )
        is False
    )


def test_coordinated_guarded_holds_are_checked_as_one_world():
    """Two benign-alone holds can jointly force an And-gated reset."""
    State = Int("JointState")
    InitA = Bool("JointInitA", external=True)
    InitB = Bool("JointInitB", external=True)
    Counter = Int("JointCounter")

    with Program(strict=False) as prog:
        with rung(InitA, InitB):
            copy(1, Counter)

    scope = CompareEq(State, 6)
    rungs = (PilotRung(InitA.name, True, scope), PilotRung(InitB.name, True, scope))
    assert _active_rungs_defeat_needed(
        rungs,
        ((Counter.name, 3),),
        {State.name: 6},
        _pdg(prog),
        prog,
    )


# ---------------------------------------------------------------------------
# THE SEAM — the live regression path must feed the filter the checkpoint's
# non-steerable frontier, not steerable action leaves.
#
# The unit tests above hand-feed ``needed`` and prove the filter's semantics.
# This test drives the *real* feed: a terminal-let-run ejection reaches
# ``_investigate_and_revert`` with a coast frame (empty tree), and ``needed``
# is assembled from ``ordered_actions()`` — steerable leaves only.  The
# register that matters (``Step = 3``, the ``Heat_CurStep = 3`` analog) is a
# non-steerable interior node, so today the filter never sees it and the
# saboteur hold installs.  See
# scratchpad/burner/SELF_DEFEATING_HOLD_HANDOFF.md (AGREED DESIGN): the fix
# feeds the checkpoint frame's still_need-style frontier.
#
# Only the investigation *result* is stubbed (the burner establishes that the
# bounded replay confirms the saboteur — its window is too short to see the
# pinned progress).  The checkpoint fork, the tree derivation, the ``needed``
# assembly, the filter, and the install decision all run for real.
# ---------------------------------------------------------------------------


def _saboteur_scenario():
    """The burner rotate-liveness shape, minimal: a shared-init rung
    (``Or(State == 8, InitFlag == 1) -> Step := 1``) whose steerable disjunct,
    held steady, keeps the macro-state alive but pins the progress register the
    target still needs (``Step = 3``)."""
    Go = Bool("Go", external=True)
    InitFlag = Int("InitFlag")  # never written, condition-read -> steerable
    State = Int("State")
    Step = Int("Step")
    Out = Bool("Out")

    with Program(strict=False) as prog:
        with rung(Or(State == 8, InitFlag == 1)):  # shared init — the self-defeat
            copy(1, Step)
        with rung(State == 6, rise(Go), Step < 3):  # gated progress: Step has children
            copy(Step + 1, Step)
        # Equality need (the burner's Heat_CurStep = 3 shape): a relational gate
        # (Step >= 3) is not inverted to a concrete needed value by the walk.
        with rung(State == 6, Step == 3):
            out(Out)

    pdg = build_program_graph(prog)

    # Checkpoint: the Execute-analog frame the coast launched from.
    cp = PLC(prog, dt=0.010)
    cp.patch({"State": 6, "Step": 1})
    cp.step()
    cp_fork = cp.fork()
    anchor = cp_fork.state.scan_id

    # The coast: State ejects 6 -> 8 mid-coast (the watchdog-abort analog).
    work = cp.fork()
    work.step()
    work.patch({"State": 8})
    work.step()
    work.step()

    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    assert "InitFlag" in steerable  # the saboteur is a real lever

    # The checkpoint frontier, derived the way the system derives it at
    # checkpoint creation: the launching frame's tree, reduced to its
    # non-steerable outstanding needs.  Nothing hand-fed — the derivation must
    # surface ("Step", 3) itself or the test cannot pass.
    cp_snap = dict(cp_fork.state.tags)
    cp_tree = trace_back(
        "Out",
        True,
        cp_snap,
        pdg,
        prog,
        steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        prior=None,
    )
    cp_frontier = frontier_pairs(cp_tree, cp_snap)

    ctx = SimpleNamespace(
        resting={"Go": False},
        edge_tags={"Go"},
        target_tag="Out",
        target_value=True,
        pdg=pdg,
        program=prog,
        steerable=steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        pipeline_roles=(),
        compass=SimpleNamespace(action_tags=frozenset()),
    )
    # A coast frame: terminal let-run builds no trace tree.
    frame = SimpleNamespace(
        snap=dict(cp_fork.state.tags),
        tree=SimpleNamespace(ordered_actions=lambda: []),
        key=("coast",),
        distance_before=2,
    )
    state = _PilotState(
        world=_World(
            work=work,
            steps=pvector([]),
            step_contexts=pvector([]),
            best_trend=2,
            rungs=pvector([]),
            dwell_scans=0,
        ),
        key_config=None,
        seen_keys=set(),
        checkpoints=[
            _Checkpoint(
                ("cpk",),
                _World(
                    work=cp_fork,
                    steps=pvector([]),
                    step_contexts=pvector([]),
                    best_trend=2,
                    rungs=pvector([]),
                    dwell_scans=0,
                ),
                2,
                cp_frontier,
            )
        ],
        watch_tags=["State"],
    )
    trial = _TrialResult(
        fork=work,
        scan_before=anchor,
        candidate={},
        applied=(),
        before_snap=dict(cp_fork.state.tags),
        post_pulse_snap=dict(work.state.tags),
        fork_snap=dict(work.state.tags),
        observe_label="letrun",
        new_key=("ejected",),
        trend=1,  # misleadingly LOW — the ejection branch intercepts it
        outcome=Outcome.AMBIENT_DRIFT,
        chase_regression_causes=True,
        zoom_channel_tag="State",
        zoom_target_value=6,
    )
    return state, trial, frame, ctx


def _stub_investigation(confirmed_holds):
    def _investigate(_plc, _incident, _ctx, _replay, **_kwargs):
        rungs = tuple(confirmed_holds)
        return InvestigationResult(
            correction=(
                _ConfirmedCorrection(
                    identity=correction_identity(rungs),
                    rungs=rungs,
                    sources=tuple(dict.fromkeys(rung.dest for rung in rungs)),
                    justification="test replay confirmed",
                )
                if rungs
                else None
            ),
            regression_nogoods=frozenset(),
            hypotheses=(),
            confirmed=(),
            rejected=(),
            unresolved=(),
        )

    return _investigate


def test_investigation_rejects_guarded_self_defeating_correction(monkeypatch):
    """The scoped form is screened before its second replay and installation."""
    state, trial, _frame, ctx = _saboteur_scenario()
    hypothesis = InvestigationHypothesis(
        kind="saboteur",
        holds=(("InitFlag", 1),),
        sources=("InitFlag",),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [hypothesis],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )

    replayed: list[tuple[object, ...]] = []

    def replay(holds):
        replayed.append(tuple(holds))
        return ReplayOutcome(
            accepted=True,
            trend=1,
            snapshot={**trial.before_snap, "State": 6},
            reason="incident silenced",
        )

    incident = DeviationIncident(
        anchor_scan=trial.scan_before,
        departure_scan=trial.scan_before + 1,
        end_scan=trial.fork.state.scan_id,
        action=(),
        bearing=(("State", 6),),
        before_snap=trial.before_snap,
        after_snap=trial.fork_snap,
        changed_tags=("State",),
        departures=(),
        channel_tag="State",
    )
    result = investigate_deviation(
        state.work,
        incident,
        ctx,
        replay,
        needed=state.checkpoints[-1].frontier,
    )

    assert replayed == [(("InitFlag", 1),)]
    assert result.correction is None
    assert result.confirmed == ()
    assert len(result.rejected) == 1
    rejected, ground = result.rejected[0]
    assert rejected == hypothesis
    assert "defeats requested progress" in ground
    assert result.rejection_slugs == ("self-defeat",)


def test_letrun_regression_keeps_benign_hold(monkeypatch):
    """A benign correction installs, but only in its incident channel context."""
    state, trial, frame, ctx = _saboteur_scenario()
    scope = CompareEq(state.work._known_tags_by_name["State"], 6)
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.investigate_deviation",
        _stub_investigation([PilotRung("Go", True, scope)]),
    )

    _monitor_trend(trial, frame, state, ctx)

    installed = next(r for r in state.rungs if r.dest == "Go" and r.value is True)

    # The correction protected State=6. Leaving that context disables its guard,
    # and Boolean input-image baseline releases Go without a release registry.
    state.work.patch({"State": 8})
    state.work.step()
    assert state.work.state.tags["State"] == 8
    assert state.work.state.tags["Go"] is True  # guard saw the pre-scan State=6 image
    state.work.step()
    assert state.work.state.tags["Go"] is False
    assert installed in state.rungs  # benign scoped correction remains recorded


def test_excursion_correction_keeps_its_replayed_rung_and_receipt():
    """Verification hands off its exact correction instead of recompiling it."""
    state, _trial, frame, ctx = _saboteur_scenario()
    state_tag = state.work._known_tags_by_name["State"]
    replayed = PilotRung("Go", True, CompareEq(state_tag, 6))
    correction = _ConfirmedCorrection(
        identity=correction_identity((replayed,)),
        rungs=(replayed,),
        sources=("State", "Go"),
        justification="excursion replay preserved State=6",
    )

    class _Compass:
        action_tags = frozenset()

        def apply(self, observations):
            assert observations == []
            return self, ()

    ctx.compass = _Compass()
    attempt = SimpleNamespace(
        observations=(),
        nogood_pairs=(),
        confirmed_correction=correction,
        avoid_names=(),
    )
    frame.tree = SimpleNamespace(children=(), satisfied=True, is_steerable=False)

    _record_attempt(attempt, frame, state, ctx)

    assert tuple(state.rungs) == (replayed,)
    assert state.correction_receipts[0].correction is correction
    assert state.correction_receipts[0].rungs == (replayed,)
    assert state.correction_receipts[0].origin_key == frame.key
    assert state.correction_receipts[0].status is CorrectionStatus.ACTIVE
    assert state.hold_log[-1].source == "excursion"
    assert state.checkpoints[-1].world.rungs == state.world.rungs


def test_correction_installer_rejects_forged_identity():
    state, _trial, frame, _ctx = _saboteur_scenario()
    state_tag = state.work._known_tags_by_name["State"]
    rung = PilotRung("Go", True, CompareEq(state_tag, 6))
    correction = _ConfirmedCorrection(
        identity=(),
        rungs=(rung,),
        sources=("Go",),
        justification="forged proof",
    )

    with pytest.raises(ValueError, match="identity does not match"):
        _install_confirmed_correction(
            state,
            correction,
            origin_key=frame.key,
            scan=state.work.state.scan_id,
            source="test",
        )

    assert state.correction_receipts == []


def test_correction_installer_rejects_already_owned_rung():
    state, _trial, frame, _ctx = _saboteur_scenario()
    state_tag = state.work._known_tags_by_name["State"]
    rung = PilotRung("Go", True, CompareEq(state_tag, 6))
    _install_prerequisites(state, (rung,))
    correction = _ConfirmedCorrection(
        identity=correction_identity((rung,)),
        rungs=(rung,),
        sources=("Go",),
        justification="duplicate proof",
    )

    with pytest.raises(ValueError, match="already-owned rung"):
        _install_confirmed_correction(
            state,
            correction,
            origin_key=frame.key,
            scan=state.work.state.scan_id,
            source="test",
        )

    assert state.correction_receipts == []
    assert state.hold_log[-1].source == "prerequisite"


def test_prerequisite_reuses_correction_owned_rung_without_claiming_it():
    state, _trial, frame, _ctx = _saboteur_scenario()
    state_tag = state.work._known_tags_by_name["State"]
    rung = PilotRung("Go", True, CompareEq(state_tag, 6))
    correction = _ConfirmedCorrection(
        identity=correction_identity((rung,)),
        rungs=(rung,),
        sources=("Go",),
        justification="replay confirmed",
    )
    _install_confirmed_correction(
        state,
        correction,
        origin_key=frame.key,
        scan=state.work.state.scan_id,
        source="investigation",
    )

    _install_prerequisites(state, (rung,))

    assert tuple(state.rungs) == (rung,)
    assert len(state.correction_receipts) == 1
    assert [entry.source for entry in state.hold_log] == ["investigation"]


def test_correction_installer_banks_artifact_into_every_checkpoint():
    state, _trial, frame, _ctx = _saboteur_scenario()
    first = state.checkpoints[0]
    state.checkpoints.insert(
        0,
        _Checkpoint(("older",), first.world, first.trend, first.frontier),
    )
    state_tag = state.work._known_tags_by_name["State"]
    rung = PilotRung("Go", True, CompareEq(state_tag, 6))
    correction = _ConfirmedCorrection(
        identity=correction_identity((rung,)),
        rungs=(rung,),
        sources=("Go",),
        justification="replay confirmed",
    )

    _install_confirmed_correction(
        state,
        correction,
        origin_key=frame.key,
        scan=state.work.state.scan_id,
        source="investigation",
    )

    assert all(rung in checkpoint.world.rungs for checkpoint in state.checkpoints)
    state.load_world(state.checkpoints[0].world)
    assert rung in state.rungs
    assert state.correction_receipts[0].status is CorrectionStatus.ACTIVE


def test_causally_opposite_remedy_replaces_harmful_correction(monkeypatch):
    """A later exact contradiction hands ownership to its proved replacement."""
    state, trial, frame, ctx = _saboteur_scenario()
    scope = CompareEq(state.work._known_tags_by_name["State"], 6)
    harmful = PilotRung("Go", True, scope)
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.investigate_deviation",
        _stub_investigation([harmful]),
    )

    _monitor_trend(trial, frame, state, ctx)

    assert harmful in state.rungs
    assert len(state.correction_receipts) == 1
    receipt = state.correction_receipts[0]
    assert receipt.status is CorrectionStatus.ACTIVE

    opposite = PilotRung("Go", False, scope)
    remedy = InvestigationHypothesis(
        kind="precise-cause",
        holds=(opposite,),
        sources=("Go",),
        detail="installed Go=True caused the later departure",
    )

    def _opposite_investigation(_plc, _incident, _ctx, _replay, **_kwargs):
        return InvestigationResult(
            correction=_ConfirmedCorrection(
                identity=correction_identity((opposite,)),
                rungs=(opposite,),
                sources=remedy.sources,
                justification="later regression neutralized",
            ),
            hypotheses=(remedy,),
            confirmed=(remedy,),
        )

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.investigate_deviation",
        _opposite_investigation,
    )
    events = _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
        end_scan=trial.fork.state.scan_id,
    )

    assert harmful not in state.rungs
    assert opposite in state.rungs
    assert state.correction_receipts[0].status is CorrectionStatus.REVOKED
    replacement = state.correction_receipts[1]
    assert replacement.status is CorrectionStatus.ACTIVE
    assert replacement.rungs == (opposite,)
    assert receipt.identity in state.correction_nogoods[receipt.origin_key]
    assert all(harmful not in checkpoint.world.rungs for checkpoint in state.checkpoints)
    assert all(opposite in checkpoint.world.rungs for checkpoint in state.checkpoints)
    assert any(entry.source == "revocation" for entry in state.hold_log)
    assert events[-1].data["revoked_corrections"] == (receipt.receipt_id,)


def test_opposite_owner_operations_compose_as_temporal_phases(monkeypatch):
    """Different completion boundaries make opposite values compatible."""
    state, trial, frame, ctx = _saboteur_scenario()
    state_tag = state.work._known_tags_by_name["State"]
    go = state.work._known_tags_by_name["Go"]
    scope = CompareEq(state_tag, 6)
    high_boundary = Eq(go.name, frozenset((True,)))
    low_boundary = Eq(go.name, frozenset((False,)))
    high = PilotRung(
        go.name,
        True,
        AllCondition(scope, CompareNe(go, True)),
        OperationReceipt(high_boundary),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.investigate_deviation",
        _stub_investigation([high]),
    )
    _monitor_trend(trial, frame, state, ctx)

    low = PilotRung(
        go.name,
        False,
        AllCondition(scope, CompareNe(go, False)),
        OperationReceipt(low_boundary),
    )
    remedy = InvestigationHypothesis(
        kind="liveness",
        holds=(low,),
        sources=(go.name,),
        detail="the opposite owner operation has its own handoff boundary",
    )

    def _phase_investigation(_plc, _incident, _ctx, _replay, **_kwargs):
        return InvestigationResult(
            correction=_ConfirmedCorrection(
                identity=correction_identity((low,)),
                rungs=(low,),
                sources=remedy.sources,
                justification="phase neutralized",
            ),
            hypotheses=(remedy,),
            confirmed=(remedy,),
        )

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.investigate_deviation",
        _phase_investigation,
    )
    events = _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
        end_scan=trial.fork.state.scan_id,
    )

    assert high in state.rungs
    assert low in state.rungs
    assert all(receipt.status is CorrectionStatus.ACTIVE for receipt in state.correction_receipts)
    assert events[-1].data["revoked_corrections"] == ()
