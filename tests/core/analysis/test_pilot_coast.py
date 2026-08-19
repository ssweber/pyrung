"""Tests for the CoastSession spine (the technician's trend recorder).

These exercise ``CoastSession.seek`` and the trigger helpers
(``value_trigger`` / ``departure_trigger`` / ``predicate_trigger``) directly,
not through the higher-level adapters. Those adapters are covered in
``test_pilot_plc_primitives.py``.

Coverage targets (per the CoastSession v2 design):
- Receipt basics + fold/no-fold landing-scan parity (perfect reaction)
- Departure terminal + recorded (tag, before, after) transition
- Simultaneous terminal triggers (target wins classification)
- Timeout stop_reason
- Nonterminal re-arm timeline, one_shot vs re-arm (perfect recall)
- Accumulator-comparison crossing exactness through the session
- predicate_trigger (opaque callable) plateau-guard landing
- cyclefold dispatch under oscillating conditional holds
- seek([]) guard
- skipped receipt
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, calc, copy, count_up, on_delay, out
from pyrung.core import Counter
from pyrung.core.analysis.pilot.coast import (
    AVOID,
    DEPARTURE,
    LIMITS,
    QUIESCENT,
    TARGET,
    CoastReceipt,
    CoastSession,
    CoastTrigger,
    _coast_to_value,
    _settle_delayed_effects,
    departure_trigger,
    predicate_trigger,
    value_trigger,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _set_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.steer import _settle_watched_tags
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    CompareEq,
    CompareGe,
    CompareLt,
    CompareNe,
)
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Program fixtures
# ---------------------------------------------------------------------------


def _timer_program():
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog


def _role_program():
    """State starts at 1; a timer trips it 1 -> 2 on its own."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            copy(2, State)
    return prog


def _dual_output_program():
    """A timer whose Done drives two coils on the SAME scan."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    A = Bool("A")
    B = Bool("B")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(A)
        with Rung(Tmr.Done):
            out(B)
    return prog


def _counter_program():
    """A per-scan integer counter: Acc increments by 1 every enabled scan."""
    Enable = Bool("Enable", external=True)
    Reset = Bool("Reset", external=True)
    with Program() as prog:
        with Rung(Enable):
            count_up(Counter[1], preset=1000).reset(Reset)
    return prog


def _ramp_program():
    """An integer that ramps +1 per enabled scan (an opaque relational feed)."""
    Enable = Bool("Enable", external=True)
    Temp = Int("Temp")
    with Program() as prog:
        with Rung(Enable):
            calc(Temp + 1, Temp)
    return prog


def _blink_program():
    """A self-resetting timer whose Done pulses periodically, plus a longer
    timer that reaches ``Target`` well after several blink pulses."""
    Enable = Bool("Enable", external=True)
    Blink = Timer.clone("Blink")
    Long = Timer.clone("Long")
    Target = Bool("Target")
    with Program() as prog:
        # ~Blink.Done re-enables the timer the scan after it completes, so
        # Blink_Done is True for exactly one scan each period.
        with Rung(~Blink.Done):
            on_delay(Blink, 50, "ms")
        with Rung(Enable):
            on_delay(Long, 500, "ms")
        with Rung(Long.Done):
            out(Target)
    return prog


def _free_timer_program():
    """A free-running timer reaches Target on its own; Input only mirrors."""
    Input = Bool("Input", external=True)
    Tmr = Timer.clone("Tmr")
    Mirror = Bool("Mirror")
    Target = Bool("Target")
    with Program() as prog:
        with Rung():
            on_delay(Tmr, 80, "ms")
        with Rung(Input):
            out(Mirror)
        with Rung(Tmr.Done):
            out(Target)
    return prog


def _gated_counter_program():
    """A counter whose gate is its own ``~Done``: Acc climbs to preset, then
    the Done bit drops the gate and Acc plateaus — a settling watched set."""
    Reset = Bool("Reset", external=True)
    Ctr = Counter[1]
    with Program() as prog:
        with Rung(~Ctr.Done):
            count_up(Ctr, preset=3).reset(Reset)
    return prog


def _transient_program():
    """State passes through 2 for exactly one scan on its way to 3.

    The ``State == 2`` rung is placed *above* the ``State == 1`` rung so the
    two copies cannot cascade within one scan — State is 2 for one full scan
    before the next scan advances it to 3.
    """
    Enable = Bool("Enable", external=True)
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable, State == 2):
            copy(3, State)
        with Rung(Enable, State == 1):
            copy(2, State)
    return prog


def _harness_feedback_program():
    """Harness-fed program: Enable drives a Physical feedback that resolves
    after a plant delay, then a gated copy fires (mirrors the fixture in
    test_pilot_plc_primitives.py's TestSettleDelayedEffects)."""
    FB = Physical("MotorFb", on_delay="200ms", off_delay="100ms")
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog


def _two_stage_program():
    """State climbs 1 -> 2 (timer A) then 2 -> 3 (timer B, gated on State == 2).

    A two-hop transition chain: each stage is gated so the earlier copy cannot
    re-fire once its stage completes, and State settles at 3."""
    Enable = Bool("Enable", external=True)
    TmrA = Timer.clone("TmrA")
    TmrB = Timer.clone("TmrB")
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable):
            on_delay(TmrA, 100, "ms")
        with Rung(TmrA.Done, State == 1):
            copy(2, State)
        with Rung(State == 2):
            on_delay(TmrB, 100, "ms")
        with Rung(TmrB.Done):
            copy(3, State)
    return prog


def _runaway_channel_program():
    """A channel register that changes every enabled scan (never settles)."""
    Enable = Bool("Enable", external=True)
    Chan = Int("Chan")
    with Program() as prog:
        with Rung(Enable):
            calc(Chan + 1, Chan)
    return prog


def _static_channel_long_timer_program():
    """A channel that never moves while a long (foldable) timer runs."""
    Enable = Bool("Enable", external=True)
    Long = Timer.clone("Long")
    Chan = Int("Chan", default=5)
    with Program() as prog:
        # Written to a constant every scan so the tag materializes but never
        # departs its value (a static channel, not a pruned unused one).
        with Rung():
            copy(5, Chan)
        with Rung(Enable):
            on_delay(Long, 10_000, "ms")
    return prog


def _step_until(plc: PLC, pred, cap: int = 20_000) -> int:
    """Drive a fold=False fork scan-by-scan; return the first scan pred holds."""
    start = plc.state.scan_id
    while not pred(plc.state):
        plc.step()
        assert plc.state.scan_id - start <= cap, "manual fork never satisfied pred"
    return plc.state.scan_id


# ---------------------------------------------------------------------------
# 1. Receipt basics + fold/no-fold parity
# ---------------------------------------------------------------------------


class TestReceiptBasics:
    def test_target_reached_lands_on_exact_scan(self):
        plc = PLC(_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        manual = plc.fork()  # fold=False reference
        target = value_trigger(plc, "target", TARGET, "Done", True)
        receipt = CoastSession(plc, kind="bearing_coast").seek([target], budget=500)

        assert receipt.stop_reason == "reached"
        assert receipt.fired == ("target",)
        assert plc.state.tags["Done"] is True

        landing = _step_until(manual, lambda s: s.tags.get("Done") is True)
        # Perfect reaction: the folded seek lands on the identical scan the
        # scan-by-scan fork first sees the predicate hold.
        assert receipt.end_scan == landing
        # One recorded pen mark for the target's watched tag.
        assert len(receipt.events) == 1
        assert receipt.events[0].name == "target"
        assert receipt.events[0].scan == landing


# ---------------------------------------------------------------------------
# 2. Departure terminal
# ---------------------------------------------------------------------------


class TestDepartureTerminal:
    def test_departure_records_transition_at_exact_scan(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        manual = plc.fork()
        dep = departure_trigger(plc, "ejected", {"State": 1})
        receipt = CoastSession(plc, kind="bearing_coast").seek([dep], budget=500)

        assert receipt.stop_reason == "departed"
        assert receipt.stop_reason != "reached"
        assert receipt.fired == ("ejected",)

        assert len(receipt.events) == 1
        ev = receipt.events[0]
        assert ev.name == "ejected"
        assert ev.kind == DEPARTURE
        assert ev.transitions == (("State", 1, 2),)

        landing = _step_until(manual, lambda s: s.tags.get("State") != 1)
        assert ev.scan == landing
        assert receipt.end_scan == landing


# ---------------------------------------------------------------------------
# 3. Simultaneous terminal triggers
# ---------------------------------------------------------------------------


class TestSimultaneousTerminals:
    def test_target_and_departure_same_scan_both_fired_target_wins(self):
        plc = PLC(_dual_output_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        a = value_trigger(plc, "target", TARGET, "A", True)
        b = value_trigger(plc, "other", DEPARTURE, "B", True)
        receipt = CoastSession(plc).seek([a, b], budget=500)

        # Both coils flip on the one scan Tmr.Done goes true.
        assert plc.state.tags["A"] is True
        assert plc.state.tags["B"] is True
        # Both terminal triggers are recorded — never collapsed.
        assert set(receipt.fired) == {"target", "other"}
        assert receipt.fired == ("target", "other")
        # A target among the simultaneous firings wins classification.
        assert receipt.stop_reason == "reached"

    def test_target_and_avoid_same_scan_preserve_typed_avoid_evidence(self):
        from pyrung.core.analysis.pilot.execution import ChannelMotion
        from pyrung.core.analysis.pilot.navigation_contracts import (
            ActPolicy,
            ActSource,
            Bearing,
            BearingObjective,
            Pulse,
            TargetSpec,
        )
        from pyrung.core.analysis.pilot.types import (
            _AvoidMember,
            _AvoidPredicate,
            _ExecutedAttempt,
        )
        from pyrung.core.analysis.pilot.verify import verify_gates

        plc = PLC(_dual_output_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        before = dict(plc.state.tags)

        b_tag = plc._known_tags_by_name["B"]
        avoid = _AvoidPredicate(
            (
                _AvoidMember(
                    name="B",
                    pred=lambda snap: snap.get("B") is True,
                    tags=frozenset({"B"}),
                    condition=CompareEq(b_tag, True),
                ),
            )
        )
        session = CoastSession(plc)
        session.arm_avoid(avoid)

        receipt = session.seek(
            [value_trigger(plc, "target", TARGET, "A", True)],
            budget=500,
        )

        assert receipt.stop_reason == "reached"
        assert receipt.avoided == ("B",)
        assert set(receipt.fired) == {"target", "B"}

        target_spec = TargetSpec("A", True)
        result = verify_gates(
            _ExecutedAttempt(
                pulse=SimpleNamespace(
                    fork=plc,
                    snap=dict(plc.state.tags),
                    coast_receipt=receipt,
                    action_snap=before,
                    wait_snaps=(),
                    post_pulse_snap=before,
                    confirmed_correction=None,
                    channel_motion=ChannelMotion(),
                ),
                bearing=Bearing(
                    ("world",),
                    Pulse(
                        ActPolicy(
                            source=ActSource.TRACE,
                            nogood_pair=("Enable", True),
                        )
                    ),
                    BearingObjective(target_spec),
                ),
            ),
            SimpleNamespace(snap=before),
            SimpleNamespace(),
            SimpleNamespace(avoid_pred=avoid, target=target_spec),
        )
        assert result.trial is None
        assert result.avoid_names == ("B",)


# ---------------------------------------------------------------------------
# 4. Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_nothing_fires_within_budget(self):
        plc = PLC(_timer_program(), dt=0.010)  # Enable never asserted -> Done stays False
        target = value_trigger(plc, "target", TARGET, "Done", True)
        receipt = CoastSession(plc).seek([target], budget=20)

        assert receipt.stop_reason == "timeout"
        assert receipt.stop_reason != "reached"
        assert receipt.fired == ()
        assert receipt.events == ()
        assert receipt.end_scan - receipt.start_scan <= 20


# ---------------------------------------------------------------------------
# 5. Nonterminal re-arm timeline (perfect recall)
# ---------------------------------------------------------------------------


class TestReArmTimeline:
    def test_rearm_records_multiple_ordered_events_then_terminal(self):
        plc = PLC(_blink_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        blink = value_trigger(plc, "blink", DEPARTURE, "Blink_Done", True, terminal=False)
        target = value_trigger(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc).seek([blink, target], budget=5000)

        assert receipt.stop_reason == "reached"
        blink_events = [e for e in receipt.events if e.name == "blink"]
        # A re-arming nonterminal trigger fires more than once inside the window.
        assert len(blink_events) >= 2
        scans = [e.scan for e in blink_events]
        # Ordered, ascending, and distinct scans (perfect recall).
        assert scans == sorted(scans)
        assert len(set(scans)) == len(scans)
        # The terminal target closes the timeline.
        assert receipt.events[-1].name == "target"
        assert all(s < receipt.events[-1].scan for s in scans)

    def test_one_shot_records_exactly_one_event(self):
        plc = PLC(_blink_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        blink_tag = plc._known_tags_by_name["Blink_Done"]
        blink = CoastTrigger(
            name="blink",
            kind=DEPARTURE,
            predicate=lambda s: s.tags.get("Blink_Done") is True,
            condition=CompareEq(blink_tag, True),
            watched=("Blink_Done",),
            terminal=False,
            one_shot=True,
        )
        target = value_trigger(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc).seek([blink, target], budget=5000)

        assert receipt.stop_reason == "reached"
        blink_events = [e for e in receipt.events if e.name == "blink"]
        assert len(blink_events) == 1


# ---------------------------------------------------------------------------
# 6. Crossing exactness through the session
# ---------------------------------------------------------------------------


class TestCrossingExactness:
    def test_accumulator_compare_folds_to_exact_crossing_scan(self):
        plc = PLC(_counter_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Counter_Acc == 1

        manual = plc.fork()
        acc = plc._known_tags_by_name["Counter_Acc"]
        n = 7
        trigger = CoastTrigger(
            name="target",
            kind=TARGET,
            predicate=lambda s: (s.tags.get("Counter_Acc") or 0) >= n,
            condition=CompareGe(acc, n),
            watched=("Counter_Acc",),
        )
        receipt = CoastSession(plc).seek([trigger], budget=200)

        assert receipt.stop_reason == "reached"
        landing = _step_until(manual, lambda s: (s.tags.get("Counter_Acc") or 0) >= n)
        assert receipt.end_scan == landing
        assert plc.state.tags["Counter_Acc"] == n


# ---------------------------------------------------------------------------
# 7. predicate_trigger (opaque callable)
# ---------------------------------------------------------------------------


class TestPredicateBump:
    def test_relational_predicate_lands_via_plateau_guard(self):
        plc = PLC(_ramp_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Temp == 1

        manual = plc.fork()
        threshold = 5
        trigger = predicate_trigger(
            "target",
            TARGET,
            lambda s: (s.tags.get("Temp") or 0) >= threshold,
            watched=("Temp",),
        )
        receipt = CoastSession(plc).seek([trigger], budget=200)

        assert receipt.stop_reason == "reached"
        # The transition is recorded: before < threshold <= after.
        assert len(receipt.events) == 1
        ev = receipt.events[0]
        assert ev.name == "target"
        assert len(ev.transitions) == 1
        tag, before, after = ev.transitions[0]
        assert tag == "Temp"
        assert before < threshold <= after

        landing = _step_until(manual, lambda s: (s.tags.get("Temp") or 0) >= threshold)
        assert receipt.end_scan == landing

    def test_equivalent_condition_makes_relational_target_foldable(self):
        plc = PLC(_counter_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        acc = plc._known_tags_by_name["Counter_Acc"]
        threshold = 700
        trigger = predicate_trigger(
            "target",
            TARGET,
            lambda s: (s.tags.get(acc.name) or 0) >= threshold,
            condition=CompareGe(acc, threshold),
            watched=(acc.name,),
        )
        session = CoastSession(plc)

        receipt = session.seek([trigger], budget=1000)

        assert receipt.stop_reason == "reached"
        assert plc.state.tags[acc.name] == threshold
        assert session._last_cyclefold_stats["ordinary_folds"] >= 1
        assert receipt.kernel_scans <= 10


class TestAvoidBump:
    def test_condition_member_predicate_receives_only_declared_reads(self):
        from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

        plc = PLC(_ramp_program(), dt=0.010)
        temp = plc._known_tags_by_name["Temp"]
        observed: list[dict[str, object]] = []

        def _record(snapshot: dict[str, object]) -> bool:
            observed.append(snapshot)
            return snapshot.get("Temp") == 2

        avoid = _AvoidPredicate(
            (
                _AvoidMember(
                    name="Temp == 2",
                    pred=_record,
                    tags=frozenset({"Temp"}),
                    condition=CompareEq(temp, 2),
                ),
            )
        )
        session = CoastSession(plc)
        session.arm_avoid(avoid)
        observed.clear()  # Ignore the one full trial-start eligibility check.

        state = SimpleNamespace(tags={"Temp": 2, "Enable": True, "Unrelated": 99})

        assert session._avoid_triggers[0].predicate(state)
        assert observed == [{"Temp": 2}]
        assert type(observed[0]) is dict

    def test_compiled_union_and_composite_members_keep_their_read_sets(self):
        from pyrung.core.runner import _compile_avoid

        A = Bool("AvoidProjectionA")
        B = Bool("AvoidProjectionB")
        Noise = Int("AvoidProjectionNoise")
        with Program() as program:
            with Rung(A):
                out(B)
            with Rung(Noise == 1):
                out(B)
        plc = PLC(program)

        union = CoastSession(plc)
        union.arm_avoid(_compile_avoid((A, B)))

        assert [trigger.watched for trigger in union._avoid_triggers] == [
            ("AvoidProjectionA",),
            ("AvoidProjectionB",),
        ]
        state = SimpleNamespace(
            tags={
                "AvoidProjectionA": True,
                "AvoidProjectionB": False,
                "AvoidProjectionNoise": 1,
            }
        )
        assert union._avoid_triggers[0].predicate(state)
        assert not union._avoid_triggers[1].predicate(state)

        composite = CoastSession(plc)
        composite.arm_avoid(_compile_avoid(AllCondition(A, B)))

        assert len(composite._avoid_triggers) == 1
        assert composite._avoid_triggers[0].watched == (
            "AvoidProjectionA",
            "AvoidProjectionB",
        )
        assert not composite._avoid_triggers[0].predicate(state)
        state.tags["AvoidProjectionB"] = True
        assert composite._avoid_triggers[0].predicate(state)

    def test_condition_projection_preserves_missing_key_and_default_semantics(self):
        from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

        plc = PLC(_ramp_program(), dt=0.010)
        temp = plc._known_tags_by_name["Temp"]
        observed: list[dict[str, object]] = []

        def _uses_mapping_default(snapshot: dict[str, object]) -> bool:
            observed.append(snapshot)
            return snapshot.get("Absent", 42) == 42 and snapshot.get("Temp") == 7

        avoid = _AvoidPredicate(
            (
                _AvoidMember(
                    name="mapping default",
                    pred=_uses_mapping_default,
                    tags=frozenset({"Temp", "Absent"}),
                    condition=CompareEq(temp, 0),
                ),
            )
        )
        session = CoastSession(plc)
        session.arm_avoid(avoid)
        session._avoid_triggers[0].predicate(SimpleNamespace(tags={"Temp": 7, "Unrelated": 99}))

        assert observed[-1] == {"Temp": 7}
        assert "Absent" not in observed[-1]

    def test_member_true_at_trial_start_is_not_armed(self):
        from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

        plc = PLC(_ramp_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Temp == 1, already inside Temp < 3.
        temp = plc._known_tags_by_name["Temp"]
        avoid = _AvoidPredicate(
            (
                _AvoidMember(
                    name="Temp < 3",
                    pred=lambda snap: (snap.get("Temp") or 0) < 3,
                    tags=frozenset({"Temp"}),
                    condition=CompareLt(temp, 3),
                ),
            )
        )
        session = CoastSession(plc)
        session.arm_avoid(avoid)

        receipt = session.seek(
            [
                predicate_trigger(
                    "target",
                    TARGET,
                    lambda state: (state.tags.get("Temp") or 0) >= 5,
                    condition=CompareGe(temp, 5),
                    watched=("Temp",),
                )
            ],
            budget=20,
        )

        assert receipt.stop_reason == "reached"
        assert receipt.avoided == ()
        assert plc.state.tags["Temp"] == 5

    def test_opaque_member_firing_on_real_scan_stops_seek(self):
        from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

        plc = PLC(_ramp_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        temp = plc._known_tags_by_name["Temp"]
        opaque = _AvoidPredicate(
            (
                _AvoidMember(
                    name="opaque Temp == 2",
                    pred=lambda snap: snap.get("Temp") == 2,
                ),
            )
        )
        session = CoastSession(plc)
        session.arm_avoid(opaque)

        receipt = session.seek(
            [
                predicate_trigger(
                    "target",
                    TARGET,
                    lambda state: (state.tags.get("Temp") or 0) >= 5,
                    condition=CompareGe(temp, 5),
                    watched=("Temp",),
                )
            ],
            budget=20,
        )

        assert receipt.stop_reason == AVOID
        assert receipt.avoided == ("opaque Temp == 2",)
        assert receipt.kernel_scans == 1

    def test_opaque_member_predicate_retains_full_plain_snapshot(self):
        from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate

        plc = PLC(_ramp_program(), dt=0.010)
        observed: list[dict[str, object]] = []

        def _opaque(snapshot: dict[str, object]) -> bool:
            observed.append(snapshot)
            return False

        session = CoastSession(plc)
        session.arm_avoid(
            _AvoidPredicate(
                (
                    _AvoidMember(
                        name="opaque",
                        pred=_opaque,
                        tags=frozenset(),
                        condition=None,
                    ),
                )
            )
        )
        observed.clear()  # Ignore the one full trial-start eligibility check.
        state = SimpleNamespace(tags={"Temp": 2, "Unrelated": 99})

        assert not session._avoid_triggers[0].predicate(state)
        assert observed == [{"Temp": 2, "Unrelated": 99}]
        assert type(observed[0]) is dict


class TestPenCondition:
    def test_pen_compiles_current_baselines_as_any_tag_changed(self):
        plc = PLC(_role_program(), dt=0.010)
        session = CoastSession(plc)
        session.arm_pens(("State", "Enable"))

        trigger = session._pen_trigger()

        assert isinstance(trigger.condition, AnyCondition)
        assert all(isinstance(term, CompareNe) for term in trigger.condition.conditions)
        assert {(term.tag.name, term.value) for term in trigger.condition.conditions} == {
            ("State", plc.state.tags["State"]),
            ("Enable", plc.state.tags["Enable"]),
        }

    def test_pen_condition_rebuilds_after_rearm(self):
        plc = PLC(_role_program(), dt=0.010)
        session = CoastSession(plc)
        session.arm_pens(("State",))
        first = session._pen_trigger()

        plc.patch({"State": 9})
        plc.step()
        session.note_pens()
        second = session._pen_trigger()

        assert first.condition.conditions[0].value != second.condition.conditions[0].value
        assert second.condition.conditions[0].value == 9

    def test_rearmed_pen_keeps_exact_timeline_while_target_folds(self):
        plc = PLC(_blink_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        session = CoastSession(plc)
        session.arm_pens(("Blink_Done",))

        receipt = session.seek(
            [value_trigger(plc, "target", TARGET, "Target", True)],
            budget=5000,
        )

        pen_events = [event for event in receipt.events if event.kind == "pen"]
        assert receipt.stop_reason == "reached"
        assert len(pen_events) >= 2
        assert [event.scan for event in pen_events] == sorted({event.scan for event in pen_events})


# ---------------------------------------------------------------------------
# 8. cyclefold dispatch
# ---------------------------------------------------------------------------


class TestCyclefoldDispatch:
    def test_ordinary_fold_analyzes_pilot_rung_crossings(self):
        Input = Bool("HoldAwareInput", external=True)
        Tmr = Timer.clone("HoldAwareTmr")
        Output = Bool("HoldAwareOutput")
        with Program() as program:
            with Rung():
                on_delay(Tmr, 1000, "ms")
            with Rung(Input):
                out(Output)

        def install(plc: PLC) -> None:
            timer = plc._known_tags_by_name[Tmr.Acc.name]
            _set_pilot_rungs(
                plc,
                [
                    PilotRung(Input.name, True, timer < 500),
                    PilotRung(Input.name, False, timer >= 500),
                ],
            )

        reference = PLC(program, dt=0.010)
        install(reference)
        reference.run_until(Tmr.Done, max_cycles=500, fold=False)

        folded = PLC(program, dt=0.010)
        install(folded)
        session = CoastSession(folded, kind="letrun")
        receipt = session.seek(
            [value_trigger(folded, "target", TARGET, Tmr.Done.name, True)],
            budget=500,
        )

        assert receipt.stop_reason == "reached"
        assert folded.state.scan_id == reference.state.scan_id
        assert folded.state.tags[Input.name] == reference.state.tags[Input.name] is False
        assert folded.state.tags[Output.name] == reference.state.tags[Output.name] is False
        assert folded.state.tags[Tmr.Acc.name] == reference.state.tags[Tmr.Acc.name]
        assert session._last_cyclefold_stats["ordinary_folds"] >= 1
        assert session.kernel_scan_ids == tuple(sorted(set(session.kernel_scan_ids)))
        assert len(session.kernel_scan_ids) == receipt.kernel_scans
        assert all(
            folded._replay_rung_write_projection_at(scan_id) is not None
            for scan_id in session.kernel_scan_ids
        )
        folded_gap = set(range(receipt.start_scan + 1, receipt.end_scan + 1)).difference(
            session.kernel_scan_ids
        )
        assert folded_gap

    def test_oscillating_holds_dispatch_to_cyclefold(self):
        plc = PLC(_free_timer_program(), dt=0.010)
        plc.step()
        start_scan = plc.state.scan_id

        Input = plc._known_tags_by_name["Input"]
        Target = plc._known_tags_by_name["Target"]
        # Toggle Input every scan while the (independent) target still runs.
        plc = fork_with_pilot_rungs(
            plc,
            [
                PilotRung("Input", True, AllCondition(~Target, ~Input)),
                PilotRung("Input", False, AllCondition(~Target, Input)),
            ],
        )

        target = value_trigger(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc, kind="letrun").seek([target], budget=200)

        assert receipt.stop_reason == "reached"
        assert plc.state.tags["Target"] is True
        # Cyclefold ran kernel scans (it can't dt-jump past the pet oscillation).
        assert receipt.kernel_scans > 0
        # The held input actually oscillated during the coast.
        seen = {
            plc.history.at(s).tags.get("Input")
            for s in range(start_scan + 1, plc.state.scan_id + 1)
        }
        assert True in seen and False in seen


# ---------------------------------------------------------------------------
# 9. Empty coast triggers
# ---------------------------------------------------------------------------


class TestEmptyBumps:
    def test_seek_empty_raises(self):
        plc = PLC(_timer_program(), dt=0.010)
        with pytest.raises(ValueError, match="at least one coast trigger"):
            CoastSession(plc).seek([], budget=10)


# ---------------------------------------------------------------------------
# 10. skipped receipt (via the _coast_to_value builder)
# ---------------------------------------------------------------------------


class TestSkippedReceipt:
    def test_none_channel_tag_is_skipped(self):
        plc = PLC(_timer_program(), dt=0.010)
        receipt = _coast_to_value(plc, None, True, budget=20)
        assert receipt.stop_reason == "skipped"
        assert receipt.stop_reason != "reached"
        assert receipt.fired == ()
        assert receipt.events == ()


# ---------------------------------------------------------------------------
# 11. settle — watched-tag fixpoint (quiescence)
# ---------------------------------------------------------------------------


class TestSettleQuiescent:
    def test_watched_tags_stop_moving_reports_quiescent(self):
        plc = PLC(_gated_counter_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Counter_Acc"}))

        # The watched tags moved for a few scans (Acc climbing) then held — a fixpoint,
        # not a ceiling exit.
        assert receipt.stop_reason == "quiescent"
        assert receipt.kernel_scans < LIMITS.cone_ceiling
        # trajectory length matches the scans stepped; end_scan is exact.
        assert len(receipt.trajectory) == receipt.kernel_scans
        assert receipt.end_scan == start + receipt.kernel_scans
        assert receipt.end_scan - receipt.start_scan == receipt.kernel_scans
        # The last two snapshots are identical on the watched tag (what the
        # fixpoint observed).
        acc = [s.get("Counter_Acc") for s in receipt.trajectory]
        assert acc[-1] == acc[-2]


# ---------------------------------------------------------------------------
# 12. settle — reached_fn short-circuit (land a one-scan transient)
# ---------------------------------------------------------------------------


class TestSettleReached:
    def test_reached_fn_lands_one_scan_transient(self):
        plc = PLC(_transient_program(), dt=0.010)
        plc.patch({"Enable": True})
        start = plc.state.scan_id

        # State is 2 for exactly one scan; reached_fn must land on it and not
        # blow past to 3.  (Note: this fires below the floor — reached_fn is
        # judged every scan, floor gates only the fixpoint check.)
        receipt = CoastSession(plc, kind="settle").settle(
            frozenset({"State"}),
            reached_fn=lambda tags: tags.get("State") == 2,
        )

        assert receipt.stop_reason == "reached"
        assert receipt.kernel_scans == 1
        assert receipt.end_scan == start + 1
        assert len(receipt.trajectory) == 1
        assert receipt.trajectory[-1]["State"] == 2
        assert plc.state.tags["State"] == 2


# ---------------------------------------------------------------------------
# 13. settle — timeout honesty (the headline)
# ---------------------------------------------------------------------------


class TestSettleTimeout:
    def test_free_running_watched_tags_exhaust_ceiling_and_are_named(self):
        plc = PLC(_counter_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Counter_Acc == 1, climbing every scan hereafter
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Counter_Acc"}))

        # Watched tags that never quiesce exhaust the ceiling, and the receipt
        # names the result "timeout"; settlement is never passed off as reached.
        assert receipt.stop_reason == "timeout"
        assert receipt.stop_reason != "reached"
        assert receipt.kernel_scans == LIMITS.cone_ceiling
        assert len(receipt.trajectory) == LIMITS.cone_ceiling
        assert receipt.end_scan == start + LIMITS.cone_ceiling
        # Every snapshot genuinely moved (proving it was no false plateau).
        acc = [s.get("Counter_Acc") for s in receipt.trajectory]
        assert acc == sorted(acc)
        assert len(set(acc)) == len(acc)


# ---------------------------------------------------------------------------
# 14. settle — floor (fixpoint not judged before the floor)
# ---------------------------------------------------------------------------


class TestSettleFloor:
    def test_motionless_watched_tags_still_step_floor_scans(self):
        # Watched tags that are motionless from the very first scan (Enable never
        # asserted -> Tmr_Done stays False).  Without the floor, the fixpoint
        # would fire at scan 1; the floor forces at least `floor` scans first.
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Tmr_Done"}), floor=4)

        assert receipt.stop_reason == "quiescent"
        assert receipt.kernel_scans == 4
        assert receipt.end_scan == start + 4

    def test_floor_of_two_steps_two(self):
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Tmr_Done"}), floor=2)

        assert receipt.stop_reason == "quiescent"
        assert receipt.kernel_scans == 2
        assert receipt.end_scan == start + 2


# ---------------------------------------------------------------------------
# 15. dwell — fixed-window coast, no predicate
# ---------------------------------------------------------------------------


class TestDwell:
    def test_dwell_steps_exactly_n_scans(self):
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        session = CoastSession(plc, kind="pulse")
        receipt = session.dwell(4)

        assert receipt.stop_reason == "dwell"
        assert receipt.kernel_scans == 4
        assert receipt.end_scan - receipt.start_scan == 4
        assert receipt.end_scan == start + 4
        assert receipt.fired == ()
        assert session.kernel_scan_ids == tuple(range(start + 1, start + 5))
        # dwell is not a settle — it carries no per-scan trajectory.
        assert receipt.trajectory == ()

    def test_kernel_scan_stream_rejects_out_of_order_ids(self):
        plc = PLC(_timer_program(), dt=0.010)
        plc.step()
        plc.step()
        session = CoastSession(plc, kind="pulse")
        session.note_kernel_scan(2)

        with pytest.raises(ValueError, match="strictly increasing"):
            session.note_kernel_scan(1)


# ---------------------------------------------------------------------------
# 16. QUIESCENT stop_reason through seek (generalized classification)
# ---------------------------------------------------------------------------


class TestQuiescentThroughSeek:
    def test_quiescent_terminal_bump_names_stop_reason(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        # A terminal trigger whose kind is neither TARGET nor DEPARTURE — the
        # generalized classification falls through to the trigger's own kind.
        trigger = predicate_trigger(
            "quiesced",
            QUIESCENT,
            lambda s: s.tags.get("State") == 2,
            watched=("State",),
        )
        receipt = CoastSession(plc).seek([trigger], budget=500)

        assert receipt.stop_reason == "quiescent"
        assert receipt.stop_reason != "reached"
        assert receipt.fired == ("quiesced",)
        assert plc.state.tags["State"] == 2


# ---------------------------------------------------------------------------
# 17. _settle_delayed_effects returns CoastReceipts
# ---------------------------------------------------------------------------


class TestSettleDelayedEffectsReceipts:
    def test_harness_feedback_settle_returns_quiescent_receipt(self):
        plc = PLC(_harness_feedback_program(), dt=0.010)
        Harness(plc).install()
        plc.patch({"Enable": True})
        plc.step()
        assert plc._harness.pending_count > 0

        receipts = _settle_delayed_effects(plc, scan_budget=200)

        # Now a list of receipts (values that outlive the coast), not a bool.
        assert isinstance(receipts, list)
        assert len(receipts) == 1
        assert all(isinstance(r, CoastReceipt) for r in receipts)
        # The harness-quiescence seek lands on a QUIESCENT trigger.
        assert receipts[0].stop_reason == "quiescent"
        # And the effect actually settled: feedback resolved, gated copy fired.
        assert plc._harness.pending_count == 0
        assert plc.state.tags["Stage"] == 1

    def test_nothing_pending_returns_empty_list(self):
        # No harness installed and no done-specs -> nothing to settle.
        plc = PLC(_timer_program(), dt=0.010)
        assert getattr(plc, "_harness", None) is None
        receipts = _settle_delayed_effects(plc, scan_budget=200)
        assert receipts == []


# ---------------------------------------------------------------------------
# 18. _settle_watched_tags wrapper parity with CoastSession.settle
# ---------------------------------------------------------------------------


class TestSettleConeParity:
    def test_wrapper_and_session_produce_identical_trajectories(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        watched_tags = frozenset({"State"})
        # Two independent forks from the identical state, driven deterministically.
        fork_a = plc.fork()
        fork_b = plc.fork()

        via_steer = _settle_watched_tags(fork_a, watched_tags)
        via_session = CoastSession(fork_b, kind="settle").settle(watched_tags)

        # The thin wrapper returns exactly the receipt's trajectory as a list.
        assert via_steer == list(via_session.trajectory)
        assert via_session.stop_reason == "quiescent"


# ---------------------------------------------------------------------------
# 19. settle_landing — departure-then-quiescence (ride a transition chain home)
# ---------------------------------------------------------------------------


class TestSettleLandingSingleHop:
    def test_single_transition_then_quiescent(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        session = CoastSession(plc, kind="departure-settle")
        receipt = session.settle_landing("State", confirm_scans=50)

        assert receipt.stop_reason == "quiescent"
        assert plc.state.tags["State"] == 2
        # Exactly one hop, recording the 1 -> 2 transition on the channel.
        hops = [e for e in receipt.events if e.name == "hop"]
        assert len(hops) == 1
        assert hops[0].kind == DEPARTURE
        assert hops[0].transitions == (("State", 1, 2),)
        # The confirmation window folds, but its scan-ids still elapse — the
        # landing sits a full confirm window past the hop.
        assert receipt.end_scan >= hops[0].scan + 50


class TestSettleLandingMultiHop:
    def test_two_stage_chain_records_ordered_hops(self):
        plc = PLC(_two_stage_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "State", confirm_scans=50
        )

        # Perfect recall: the whole two-stage transition chain is evidence.
        assert receipt.stop_reason == "quiescent"
        assert plc.state.tags["State"] == 3
        hops = [e for e in receipt.events if e.name == "hop"]
        assert len(hops) == 2
        assert hops[0].transitions == (("State", 1, 2),)
        assert hops[1].transitions == (("State", 2, 3),)
        # Ordered and distinct in scan (rode each hop, re-armed, rode the next).
        assert hops[0].scan < hops[1].scan


class TestSettleLandingNeverMoves:
    def test_static_channel_quiescent_after_one_window(self):
        plc = PLC(_static_channel_long_timer_program(), dt=0.010)
        # Enable NOT asserted: the timer is idle and Chan never moves.
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=40
        )

        assert receipt.stop_reason == "quiescent"
        # A channel that never departs records no hop events.
        assert receipt.events == ()
        assert plc.state.tags["Chan"] == 5
        # One confirmation window, nothing more.
        assert receipt.end_scan - start == 40


class TestSettleLandingCapHonesty:
    def test_free_running_channel_times_out_within_cap(self):
        plc = PLC(_runaway_channel_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        start = plc.state.scan_id
        first = plc.state.tags["Chan"]

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=100, cap=50
        )

        # A channel that changes faster than the confirm window never quiesces;
        # the cap is respected and the landing is NAMED "timeout" — never
        # classified as settled.  (This is exactly the non-quiescent receipt
        # that classify_departure refuses, see TestClassifyDepartureRefusal.)
        assert receipt.stop_reason == "timeout"
        assert 0 < receipt.end_scan - start <= 50
        # It kept hopping the whole time (mid-transition at the cap).
        assert len(receipt.events) >= 2
        assert plc.state.tags["Chan"] > first


class TestSettleLandingFolds:
    def test_departure_settlement_receipt_owns_only_actual_kernel_scans(self):
        from pyrung.core.analysis.pilot.departure import _settle_departure

        plc = PLC(_static_channel_long_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        source_scan = plc.state.scan_id

        settled, execution = _settle_departure(
            SimpleNamespace(work=plc, pilot_rungs=()),
            "Chan",
        )

        receipt = execution.coast_receipt
        assert receipt is not None
        assert execution.source_scan == source_scan
        assert execution.after_snap == settled.state.tags
        assert len(execution.kernel_scan_ids) == receipt.kernel_scans
        assert receipt.kernel_scans < receipt.logical_scans
        skipped = next(
            scan
            for scan in range(receipt.start_scan + 1, receipt.end_scan + 1)
            if scan not in execution.kernel_scan_ids
        )
        assert execution.point_at(skipped) is None

    def test_confirmation_window_folds_scan_ids_still_elapse(self):
        plc = PLC(_static_channel_long_timer_program(), dt=0.010)
        plc.patch({"Enable": True})  # long timer runs -> a foldable window
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=200
        )

        assert receipt.stop_reason == "quiescent"
        # The confirmation window's scan-ids elapse in a single folded seek
        # (a quiet 200-scan second, not 200 stepped scans).
        assert receipt.end_scan - start == 200
        # The composite receipt retains the inner seek's actual work.
        assert 0 < receipt.kernel_scans < receipt.logical_scans
        assert receipt.macro_folds >= 1
        assert receipt.skipped_scans == receipt.logical_scans - receipt.kernel_scans

    def test_composite_receipt_aggregates_nested_ordinary_fold_edits(self):
        Enable = Bool("SettleAdvanceEnable", external=True)
        Reset = Bool("SettleAdvanceReset", external=True)
        Ctr = Counter.clone("SettleAdvance")
        Chan = Int("SettleAdvanceChan", default=5)
        with Program() as program:
            with Rung():
                copy(5, Chan)
            with Rung(Enable):
                count_up(Ctr, preset=1000).reset(Reset)

        plc = PLC(program, dt=0.010)
        plc.patch({Enable.name: True})
        plc.step()

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            Chan.name,
            confirm_scans=200,
        )

        assert receipt.stop_reason == "quiescent"
        assert receipt.advances
        assert receipt.advances == tuple(sorted(receipt.advances, key=lambda edit: edit[1]))
        assert {tag for tag, _value in receipt.advances} == {Ctr.Acc.name}


# ---------------------------------------------------------------------------
# 20. classify_departure refuses a non-quiescent (timeout) receipt
# ---------------------------------------------------------------------------


class TestClassifyDepartureRefusal:
    def test_non_quiescent_receipt_is_refused_as_unknown(self, monkeypatch):
        # Constructing a full _PilotState/_PilotContext is heavy, so exercise
        # the refusal at the seam: a timeout receipt from _settle_departure is
        # the cap-hit, possibly-mid-transition value the departure reader must NOT
        # trust as a settled landing.
        from types import SimpleNamespace

        from pyrung.core.analysis.pilot import departure
        from pyrung.core.analysis.pilot.execution import ChannelMotion, ExecutionReceipt
        from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective, TargetSpec

        timeout_receipt = CoastReceipt(
            kind="departure-settle",
            start_scan=0,
            end_scan=2000,
            stop_reason="timeout",
            fired=(),
            events=(),
            budget=2000,
        )
        fake_fork = SimpleNamespace(state=SimpleNamespace(tags={"Chan": 7}, scan_id=2000))
        monkeypatch.setattr(
            departure,
            "_settle_departure",
            lambda state, channel_tag: (
                fake_fork,
                ExecutionReceipt(
                    before_snap={"Chan": 1},
                    after_snap={"Chan": 7},
                    channel_motion=ChannelMotion("Chan", 7),
                    coast_receipt=timeout_receipt,
                    timeline=(),
                    source_scan=0,
                ),
            ),
        )

        observation, settled_work = departure.observe_departure(
            SimpleNamespace(),  # state — consumed only by the (mocked) settle
            SimpleNamespace(),  # ctx — untouched on the refusal arm
            BearingObjective(TargetSpec("Target", True)),
            "Chan",
            from_value=1,
            source_snap={"Chan": 1},
        )
        verdict = departure.classify_departure(observation)

        assert verdict.classification is departure.DepartureClassification.UNKNOWN
        assert "did not settle within cap" in verdict.reason
        assert "timeout" in verdict.reason
        # The receipt's landing value is still surfaced, just not trusted.
        assert verdict.observation.settled_value == 7
        assert verdict.observation.landing_receipt is timeout_receipt
        assert verdict.observation.landing_receipt.logical_scans == 2000
        assert settled_work is fake_fork
        assert isinstance(
            verdict.observation.continuation.channel_status,
            departure.Unknown,
        )
        assert verdict.observation.continuation.awaited_action_inspected is False
