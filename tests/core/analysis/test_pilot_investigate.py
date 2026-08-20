"""Tests for exact correction hypotheses and excursion evidence."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import (
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    rise,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.avoid import _hold_allowed
from pyrung.core.analysis.pilot.coast import CoastSession, CoastTriggerEvent, _coast_holding_state
from pyrung.core.analysis.pilot.correction_candidates import (
    _compose_hypotheses,
    _reprove_composite_producer_envelope,
)
from pyrung.core.analysis.pilot.corrections import (
    CorrectionHypothesis,
    _dedupe_pairs,
    _precise_causes,
    _producer_envelope_guard,
    correct_enablers,
)
from pyrung.core.analysis.pilot.incidents import BearingDeparture, DeviationIncident
from pyrung.core.analysis.pilot.investigation_replay import (
    _first_timeline_departure,
    build_deviation_incident,
    investigate_excursion,
)
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _set_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.world_key import _pilot_state_key, _StateKeyConfig
from pyrung.core.analysis.sp_values import _SnapshotView
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.runner import PLC

_DEFAULT_TARGET = TargetSpec("", None)


def _make_ctx(
    prog: Program,
    plc: PLC,
    *,
    target: TargetSpec = _DEFAULT_TARGET,
    **overrides: Any,
) -> SimpleNamespace:
    """Minimal duck-typed context for the hypothesis generators."""
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    values: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "route": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
        "target": target,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ConditionSnapshot:
    def __init__(self, values: dict[str, Any]):
        self.values = values

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)


def test_nested_closure_reproves_individual_scopes_with_a_shared_joint_cut():
    """Composition coordinates its proof without merging runtime scopes."""

    Mode = Bool("CompositeEnvelope_Mode", external=True)
    Door = Bool("CompositeEnvelope_Door", external=True)
    Lint = Bool("CompositeEnvelope_Lint", external=True)
    ExactDoor = Bool("CompositeEnvelope_ExactDoor", external=True)
    ExactLint = Bool("CompositeEnvelope_ExactLint", external=True)
    State = Int("CompositeEnvelope_State")
    DoorAlarm = Bool("CompositeEnvelope_DoorAlarm")
    LintAlarm = Bool("CompositeEnvelope_LintAlarm")
    Command = Int("CompositeEnvelope_Command")

    with Program() as program:
        with Rung(Mode, Or(State == 3, State == 11), ~Door):
            latch(DoorAlarm)
        with Rung(Mode, Or(State == 3, State == 11), ~Lint):
            latch(LintAlarm)
        # Neither individual assignment disables this writer; the pair does.
        with Rung(Mode, State == 6, Or(~Door, ~Lint)):
            copy(4, Command)
        # A door-only sibling must not broaden the lint correction.
        with Rung(Mode, State == 17, ~Door):
            copy(4, Command)
        with Rung(DoorAlarm):
            copy(4, Command)
        with Rung(LintAlarm):
            copy(4, Command)
        with Rung(Command == 4):
            copy(8, State)

    ctx = SimpleNamespace(pdg=build_program_graph(program), program=program)
    spine = frozenset({DoorAlarm.name, LintAlarm.name, Command.name, State.name})
    first = CorrectionHypothesis(
        "latch-exposure",
        (PilotRung(Door.name, True, State == 3),),
        sources=(DoorAlarm.name, Door.name),
        producer_envelope=True,
        fallback_holds=(PilotRung(Door.name, True, ExactDoor),),
        producer_cuts=(((Door.name, True), Door.name, True),),
        producer_sources=(DoorAlarm.name,),
        producer_causal_spine=spine,
    )
    second = CorrectionHypothesis(
        "latch-exposure",
        (PilotRung(Lint.name, True, State == 3),),
        sources=(LintAlarm.name, Lint.name),
        producer_envelope=True,
        fallback_holds=(PilotRung(Lint.name, True, ExactLint),),
        producer_cuts=(((Lint.name, True), Lint.name, True),),
        producer_sources=(LintAlarm.name,),
        producer_causal_spine=spine,
    )

    composite = _compose_hypotheses(first, second)
    assert composite is not None
    assert tuple(rung.guard for rung in composite.holds) == (ExactDoor, ExactLint)

    reproved = _reprove_composite_producer_envelope(composite, ctx, State.name)

    assert reproved.producer_envelope
    assert reproved.fallback_holds == composite.holds
    by_dest = {rung.dest: rung.guard for rung in reproved.holds}

    def active(tag: str, state: int, *, mode: bool = True) -> bool:
        return by_dest[tag].evaluate(
            _ConditionSnapshot(
                {
                    Mode.name: mode,
                    Door.name: False,
                    Lint.name: False,
                    State.name: state,
                    DoorAlarm.name: False,
                    LintAlarm.name: False,
                    Command.name: 0,
                }
            )
        )

    assert active(Door.name, 3)
    assert active(Lint.name, 3)
    assert active(Door.name, 6)
    assert active(Lint.name, 6)
    assert active(Door.name, 17)
    assert not active(Lint.name, 17)
    assert not active(Door.name, 6, mode=False)


def test_producer_envelope_follows_recorded_cascade_and_retains_escape_conditions():
    """Sibling discovery stays on the causal skeleton, not a broad cone."""

    Mode = Bool("Envelope_Mode", external=True)
    Door = Bool("Envelope_Door", external=True)
    State = Int("Envelope_State")
    Step = Int("Envelope_Step")
    Alarm = Bool("Envelope_Alarm")
    Command = Int("Envelope_Command")
    Warning = Bool("Envelope_Warning")

    with Program() as program:
        with subroutine("EnvelopeFaults"):
            with Rung(Or(State == 3, State == 11), ~Door):
                latch(Alarm)
            with Rung(State == 6, ~Door):
                copy(4, Command)
            # This sibling really is step-dependent; projection must retain
            # that escape condition rather than generalizing over State=12.
            with Rung(State == 12, Step == 7, ~Door):
                copy(4, Command)
            # Door-dependent shared plumbing outside the recorded cascade.
            with Rung(State == 12, ~Door):
                out(Warning)
        with Rung(Mode):
            call("EnvelopeFaults")
        with Rung(Alarm):
            copy(4, Command)
        with Rung(Command == 4):
            copy(8, State)

    pdg = build_program_graph(program)
    guard = _producer_envelope_guard(
        SimpleNamespace(pdg=pdg, program=program),
        State.name,
        {Door.name: True},
        (Alarm.name,),
        frozenset({Alarm.name, Command.name, State.name}),
    )

    assert guard is not None

    def active(**values: Any) -> bool:
        snapshot = {
            Mode.name: False,
            Door.name: False,
            State.name: 0,
            Step.name: 0,
            Alarm.name: False,
            Command.name: 0,
            Warning.name: False,
            **values,
        }
        return guard.evaluate(_ConditionSnapshot(snapshot))

    assert active(**{Mode.name: True, State.name: 3})
    assert active(**{Mode.name: True, State.name: 6})
    assert active(**{Mode.name: True, State.name: 12, Step.name: 7})
    assert not active(**{Mode.name: False, State.name: 3})
    assert not active(**{Mode.name: True, State.name: 12, Step.name: 8})


def test_producer_envelope_punts_when_lever_is_mixed_with_context():
    Door = Bool("MixedEnvelope_Door", external=True)
    State = Int("MixedEnvelope_State")
    Alarm = Bool("MixedEnvelope_Alarm")

    with Program() as program:
        # One nested condition cannot be projected into independent lever and
        # context terms without Boolean reconstruction. The sound result is the
        # ordinary exact EarnedWork fallback.
        with Rung(And(State == 3, ~Door)):
            latch(Alarm)
        with Rung(Alarm):
            copy(8, State)

    pdg = build_program_graph(program)
    guard = _producer_envelope_guard(
        SimpleNamespace(pdg=pdg, program=program),
        State.name,
        {Door.name: True},
        (Alarm.name,),
        frozenset({Alarm.name, State.name}),
    )

    assert guard is None
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
        _set_pilot_rungs(
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
        pilot_rungs = correct_enablers(plc, incident, ctx)[0].holds

        fresh = PLC(_shaft_rotate_program(), dt=0.010)
        fresh.step()
        fresh = fork_with_pilot_rungs(fresh, pilot_rungs)
        reached = _coast_holding_state(fresh, "Running", True, (), budget=200)
        assert reached.stop_reason == "reached"
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
# Multi-read reset/advance conditions — coordinated holds
#
# ``correct_enablers`` evaluates a reset/advance guard's real Condition over its
# reads' value spaces to find the minimal lever assignment producing the needed
# polarity, and proposes coordinated holds.
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
        pilot_rungs = correct_enablers(plc, incident, ctx)[0].holds
        assert {r.dest for r in pilot_rungs} == {"A", "B"}

        fresh = PLC(_conj_reset_target_program(), dt=0.010)
        fresh.step()
        fresh = fork_with_pilot_rungs(fresh, pilot_rungs)
        reached = _coast_holding_state(fresh, "Running", True, (), budget=300)
        assert reached.stop_reason == "reached"
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
    """investigate_excursion: state-key diagnosis and hold-based replay."""

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
            pilot_rungs=[],
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
            pilot_rungs=[],
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        # Sealing Hold=True keeps Out latched across the edge release — the
        # replay key differs from the (reverted) pre key, so the hold is kept.
        assert result.correction is not None
        assert ("Hold", True) in tuple(
            (rung.dest, rung.value) for rung in result.correction.pilot_rungs
        )
        guard = result.correction.pilot_rungs[0].guard
        assert guard.evaluate(_SnapshotView({"Out": True}, {}))
        assert not guard.evaluate(_SnapshotView({"Out": False}, {}))
        assert result.replay_fork is not None
        assert result.replay_kernel_scan_ids
        assert (
            result.replay_fork._replay_pilot_rung_write_projection_at(
                result.replay_kernel_scan_ids[-1]
            )
            is not None
        )


# ---------------------------------------------------------------------------
# Antagonist dispatch — any causally implicated clobbering writer
#
# Dispatch is by causal implication (``cause()``) + producibility, so a plain
# clobbering ``copy`` is suppressed by forcing its guard FALSE — and a live-word
# guard escalates to the skiff. Both flow through the same replay verification.
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
    lever is the int ``Mode``. Antagonist dispatch suppresses the copy by forcing
    its guard FALSE via the int-domain forcing
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
    firing — a nomination replay verification then confirms.
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
    escalation for a live-word guard. Every hold passes replay verification."""

    def test_non_reset_copy_clobber_is_corrected(self):
        # The compound int guard is suppressed by forcing the copy's guard false.
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
            pilot_rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert result.correction is not None
        assert ("Mode", 1) in tuple(
            (rung.dest, rung.value) for rung in result.correction.pilot_rungs
        )
        assert result.replay_fork is not None
        # The suppression preserved the pulse-established value across the settle.
        assert result.replay_fork.state.tags["State"] == 5

    def test_live_word_guard_uses_skiff_probe(self):
        # The clobber's guard reads a calc-computed word: guard-force enumeration
        # punts, and the skiff's isolated probe nominates the condition-read Bool
        # Sel=False, which replay verification confirms.
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
            pilot_rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert result.correction is not None
        assert ("Sel", False) in tuple(
            (rung.dest, rung.value) for rung in result.correction.pilot_rungs
        )
        assert result.replay_fork is not None
        assert result.replay_fork.state.tags["State"] == 5


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
