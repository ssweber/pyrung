"""Tests for pilot _ops — low-level PLC manipulation primitives.

Coverage targets:
- ConditionalHold: guarded-rule value_for selection
- _split_holds: steady vs conditional partition
- _coast_to_value: budget, ejection guard, target reached
- _coast_holding_state: role-tag ejection, conditional-hold animation
- _threshold_crossed_snap: up/down/tag-name/form/non-numeric
- _pilot_state_key: projection, done-bit abstraction, threshold vectors, masking
- _install_holds: steady vs conditional hold semantics
- _apply_pulse: rising-edge vs plain scan count
- _settle_delayed_effects: harness feedback, timer accumulation
- _has_pending_effects: harness presence / pending count
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out
from pyrung.core.analysis.pilot._ops import (
    ConditionalHold,
    _apply_pulse,
    _coast_holding_state,
    _coast_to_value,
    _has_pending_effects,
    _HoldRule,
    _install_holds,
    _pilot_state_key,
    _settle_delayed_effects,
    _split_holds,
    _StateKeyConfig,
    _threshold_crossed_snap,
)
from pyrung.core.analysis.prove.absorb import (
    _done_acc_state,
    _ThresholdAtomSpec,
    _ThresholdVectorSpec,
)
from pyrung.core.analysis.prove.events import _StateKeyDoneSpec
from pyrung.core.analysis.prove.results import PENDING
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC


def _oscillating_hold(tag: str) -> ConditionalHold:
    """The liveness shape: drive *tag* to each polarity while it is off that
    polarity, so the two mutually-exclusive guards alternate it every scan."""
    return ConditionalHold(
        rules=(
            _HoldRule(value=True, guard_tag=tag, guard_op="ne", guard_value=True),
            _HoldRule(value=False, guard_tag=tag, guard_op="ne", guard_value=False),
        )
    )


# ---------------------------------------------------------------------------
# ConditionalHold
# ---------------------------------------------------------------------------


class TestConditionalHold:
    def test_first_active_rule_wins(self):
        ch = _oscillating_hold("In")
        # value_for returns (active, value_to_force).
        # In == False -> "drive True while != True" rule is active.
        assert ch.value_for({"In": False}) == (True, True)
        # In == True -> "drive False while != False" rule is active.
        assert ch.value_for({"In": True}) == (True, False)

    def test_no_active_rule(self):
        # A guard that never matches the snapshot leaves the hold inactive.
        ch = ConditionalHold(
            rules=(_HoldRule(value=True, guard_tag="In", guard_op="eq", guard_value="X"),)
        )
        assert ch.value_for({"In": False}) == (False, None)

    def test_eq_guard(self):
        ch = ConditionalHold(
            rules=(_HoldRule(value=1, guard_tag="Mode", guard_op="eq", guard_value=2),)
        )
        assert ch.value_for({"Mode": 2}) == (True, 1)
        assert ch.value_for({"Mode": 3}) == (False, None)


# ---------------------------------------------------------------------------
# _split_holds
# ---------------------------------------------------------------------------


class TestSplitHolds:
    def test_partitions_steady_and_conditional(self):
        ch = _oscillating_hold("B")
        holds = [("A", 10), ("B", ch), ("C", True)]
        steady, conditional = _split_holds(holds)
        assert steady == [("A", 10), ("C", True)]
        assert conditional == {"B": ch}

    def test_empty(self):
        steady, conditional = _split_holds([])
        assert steady == []
        assert conditional == {}


# ---------------------------------------------------------------------------
# Coast to value
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


class TestCoastToValue:
    def test_reaches_target(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        reached = _coast_to_value(plc, "Done", True, budget=50)
        assert reached
        assert plc.state.tags["Done"] is True

    def test_budget_limits_scans(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        scan_before = plc.state.scan_id
        _coast_to_value(plc, "Done", True, budget=3)
        assert plc.state.scan_id - scan_before <= 3

    def test_ejection_stops_early(self):
        """If the governing tag goes to an unexpected third value, coast stops."""
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        # Don't enable the timer — Done stays False, never reaches True
        reached = _coast_to_value(plc, "Done", True, budget=20)
        assert not reached

    def test_none_governing_tag_returns_false(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        assert _coast_to_value(plc, None, True, budget=20) is False


# ---------------------------------------------------------------------------
# Coast holding state
# ---------------------------------------------------------------------------


def _role_program():
    """A role register (State) that flips on its own when a timer fires.

    Coast toward Target (never reachable here) while holding role State=1.
    The timer trips State 1 -> 2 on its own, which the coast must catch as an
    ejection.
    """
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    State = Int("State", default=1)
    Target = Bool("Target")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            copy(2, State)
        with Rung(State == 9):
            out(Target)
    return prog


def _free_timer_program():
    """A free-running timer reaches Target on its own; Input only mirrors.

    Used to confirm a liveness hold oscillates the input throughout the coast
    while the (independent) target still completes.
    """
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


class TestCoastHoldingState:
    def test_role_ejection_stops_coast(self):
        prog = _role_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        scan_before = plc.state.scan_id
        reached = _coast_holding_state(plc, "Target", True, role_tags=("State",), budget=200)
        # Target never reached; role State flipped 1 -> 2 so coast ejected early.
        assert reached is False
        assert plc.state.tags["State"] == 2
        assert plc.state.scan_id - scan_before < 200

    def test_conditional_holds_toggle_each_scan(self):
        prog = _free_timer_program()
        plc = PLC(prog, dt=0.010)
        plc.step()

        start_scan = plc.state.scan_id
        conditional = {"Input": _oscillating_hold("Input")}
        reached = _coast_holding_state(
            plc, "Target", True, role_tags=(), conditional=conditional, budget=200
        )
        assert reached is True
        assert plc.state.tags["Target"] is True

        # The held input must have actually oscillated (not pinned steady).
        seen = {
            plc.history.at(s).tags.get("Input")
            for s in range(start_scan + 1, plc.state.scan_id + 1)
        }
        assert True in seen and False in seen


# ---------------------------------------------------------------------------
# _threshold_crossed_snap
# ---------------------------------------------------------------------------


class TestThresholdCrossedSnap:
    def test_count_up_ge(self):
        snap = {"Acc": 75}
        assert _threshold_crossed_snap(snap, "count_up", "Acc", 50, "ge") is True
        assert _threshold_crossed_snap(snap, "count_up", "Acc", 100, "ge") is False

    def test_form_gt_vs_ge_at_boundary(self):
        snap = {"Acc": 100}
        assert _threshold_crossed_snap(snap, "count_up", "Acc", 100, "gt") is False
        assert _threshold_crossed_snap(snap, "count_up", "Acc", 100, "ge") is True

    def test_count_down_negates_both_sides(self):
        # down-counting: -Acc >= -Threshold  <=>  Acc <= Threshold
        snap = {"Acc": 75}
        assert _threshold_crossed_snap(snap, "count_down", "Acc", 100, "ge") is True
        assert _threshold_crossed_snap(snap, "count_down", "Acc", 50, "ge") is False

    def test_threshold_as_tag_name(self):
        snap = {"Acc": 50, "Preset": 80}
        assert _threshold_crossed_snap(snap, "count_up", "Acc", "Preset", "ge") is False
        snap["Preset"] = 40
        assert _threshold_crossed_snap(snap, "count_up", "Acc", "Preset", "ge") is True

    def test_bool_and_non_numeric_return_false(self):
        assert _threshold_crossed_snap({"F": True}, "count_up", "F", 1, "ge") is False
        assert _threshold_crossed_snap({"A": 10, "F": True}, "count_up", "A", "F", "ge") is False
        assert _threshold_crossed_snap({"S": "x"}, "count_up", "S", 1, "ge") is False
        # Missing acc -> None -> not numeric -> False
        assert _threshold_crossed_snap({}, "count_up", "Acc", 1, "ge") is False


# ---------------------------------------------------------------------------
# State key projection
# ---------------------------------------------------------------------------


class TestPilotStateKey:
    def test_basic_projection(self):
        cfg = _StateKeyConfig(
            stateful_names=("A", "B", "C"),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        snap = {"A": 10, "B": True, "C": "v", "D": 99}
        assert _pilot_state_key(snap, cfg) == (10, True, "v")

    def test_done_bit_abstraction(self):
        cfg = _StateKeyConfig(
            stateful_names=("Trigger", "Tmr_Done", "Out"),
            done_specs=(_StateKeyDoneSpec(index=1, acc_name="Tmr_Acc", kind="on_delay"),),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        # Done=False but Acc != 0 -> PENDING
        snap = {"Trigger": True, "Tmr_Done": False, "Tmr_Acc": 50, "Out": False}
        key = _pilot_state_key(snap, cfg)
        assert key[0] is True
        assert key[1] == PENDING
        assert key[1] == _done_acc_state("on_delay", False, 50)
        assert key[2] is False

        # Acc == 0 -> False
        snap_off = {"Trigger": True, "Tmr_Done": False, "Tmr_Acc": 0, "Out": False}
        assert _pilot_state_key(snap_off, cfg)[1] is False

        # Done True -> True
        snap_done = {"Trigger": True, "Tmr_Done": True, "Tmr_Acc": 100, "Out": True}
        assert _pilot_state_key(snap_done, cfg)[1] is True

    def test_acc_indices_masked(self):
        cfg = _StateKeyConfig(
            stateful_names=("A", "Acc", "C"),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset({1}),
        )
        snap = {"A": 10, "Acc": 999, "C": 7}
        key = _pilot_state_key(snap, cfg)
        assert key == (10, None, 7)

    def test_threshold_vector_appended(self):
        atoms = (
            _ThresholdAtomSpec(acc_name="Acc", threshold=10, form="ge", mode="x"),
            _ThresholdAtomSpec(acc_name="Acc", threshold=100, form="ge", mode="x"),
        )
        spec = _ThresholdVectorSpec(acc_name="Acc", kind="count_up", atoms=atoms)
        cfg = _StateKeyConfig(
            stateful_names=("A",),
            done_specs=(),
            threshold_vector_specs=(spec,),
            acc_indices=frozenset(),
        )
        snap = {"A": 1, "Acc": 50}
        key = _pilot_state_key(snap, cfg)
        # base projection + one appended bit-vector tuple
        assert key[0] == 1
        assert key[1] == (True, False)  # 50>=10 True, 50>=100 False


# ---------------------------------------------------------------------------
# _install_holds
# ---------------------------------------------------------------------------


def _single_input_program():
    In = Bool("In", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(In):
            out(Out)
    return prog


class TestInstallHolds:
    def test_steady_hold_forced(self):
        plc = PLC(_single_input_program(), dt=0.010)
        forced: dict = {}
        _install_holds(plc, [("In", True)], forced)
        assert forced["In"] is True
        plc.step()
        assert plc.state.tags["In"] is True
        assert plc.state.tags["Out"] is True

    def test_conditional_hold_recorded_not_forced(self):
        plc = PLC(_single_input_program(), dt=0.010)
        forced: dict = {}
        ch = _oscillating_hold("In")
        _install_holds(plc, [("In", ch)], forced)
        # recorded in the dict...
        assert forced["In"] is ch
        # ...but NOT forced onto the PLC (a steady force can't animate it)
        plc.step()
        assert plc.state.tags["In"] is not True

    def test_skip_already_held(self):
        plc = PLC(_single_input_program(), dt=0.010)
        forced = {"In": 99}
        _install_holds(plc, [("In", 55)], forced)
        assert forced["In"] == 99  # unchanged


# ---------------------------------------------------------------------------
# _apply_pulse
# ---------------------------------------------------------------------------


class TestApplyPulse:
    def test_no_edge_consumes_five_scans(self):
        plc = PLC(_single_input_program(), dt=0.010)
        before = plc.state.scan_id
        consumed = _apply_pulse(plc, [("In", True)], resting={}, edge_tags=set())
        assert consumed == 5
        assert plc.state.scan_id - before == 5

    def test_edge_consumes_six_scans(self):
        plc = PLC(_single_input_program(), dt=0.010)
        plc.patch({"In": False})
        plc.step()
        before = plc.state.scan_id
        consumed = _apply_pulse(plc, [("In", True)], resting={"In": False}, edge_tags={"In"})
        # one release scan + patch step + 4 settle steps
        assert consumed == 6
        assert plc.state.scan_id - before == 6


# ---------------------------------------------------------------------------
# Settle delayed effects
# ---------------------------------------------------------------------------


def _harness_feedback_program():
    FB = Physical("MotorFb", on_delay="200ms", off_delay="100ms")
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog


class TestSettleDelayedEffects:
    def test_harness_feedback_settled(self):
        plc = PLC(_harness_feedback_program(), dt=0.010)
        Harness(plc).install()
        plc.patch({"Enable": True})
        plc.step()
        harness = plc._harness
        assert harness.pending_count > 0

        before = dict(plc.state.tags)
        _settle_delayed_effects(plc, before, cfg=None, scan_budget=200)
        assert harness.pending_count == 0
        # feedback resolved -> the gated copy fired
        assert plc.state.tags["Stage"] == 1

    def test_pending_timer_fast_forwarded(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        # snapshot before the timer ever runs: Acc 0, Done False (not PENDING)
        before = dict(plc.state.tags)
        assert before["Tmr_Done"] is False
        assert before["Tmr_Acc"] == 0

        plc.patch({"Enable": True})
        plc.step()
        # now PENDING: Acc advancing, TT active, Done still False
        assert plc.state.tags["Tmr_TT"] is True
        assert plc.state.tags["Tmr_Done"] is False

        cfg = _StateKeyConfig(
            stateful_names=("Enable", "Tmr_Done", "Done"),
            done_specs=(_StateKeyDoneSpec(index=1, acc_name="Tmr_Acc", kind="on_delay"),),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        scan_before = plc.state.scan_id
        _settle_delayed_effects(plc, before, cfg=cfg, scan_budget=500)
        assert plc.state.tags["Tmr_Done"] is True
        assert plc.state.tags["Tmr_TT"] is False
        assert 0 < plc.state.scan_id - scan_before <= 500


# ---------------------------------------------------------------------------
# _has_pending_effects
# ---------------------------------------------------------------------------


class TestHasPendingEffects:
    def test_no_harness_returns_false(self):
        plc = PLC(_single_input_program(), dt=0.010)
        assert _has_pending_effects(plc) is False

    def test_pending_feedback_returns_true(self):
        plc = PLC(_harness_feedback_program(), dt=0.010)
        Harness(plc).install()
        plc.patch({"Enable": True})
        plc.step()
        assert plc._harness.pending_count > 0
        assert _has_pending_effects(plc) is True
