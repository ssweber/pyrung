"""Tests for PILOT's low-level PLC manipulation primitives.

Coverage targets:
- PilotRung: guarded-rule value_for selection
- rung list: steady vs conditional partition
- _coast_to_value: budget, ejection guard, target reached
- _coast_holding_state: role-tag ejection, conditional-hold animation
- _threshold_crossed_snap: up/down/tag-name/form/non-numeric
- _pilot_state_key: projection, done-bit abstraction, threshold vectors, masking
- _merged_pilot_rungs + fork installation: steady vs conditional hold semantics
- _apply_pulse: rising-edge vs plain scan count
- _settle_delayed_effects: harness feedback, timer accumulation
- _has_pending_effects: harness presence / pending count
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.coast import (
    _coast_holding_state,
    _coast_to_value,
    _has_pending_effects,
    _settle_delayed_effects,
)
from pyrung.core.analysis.pilot.overlay import (
    OperationReceipt,
    PilotRung,
    PilotRungExecutionState,
    _constraint_condition,
    _merged_pilot_rungs,
    _pilot_rung_execution_receipt,
    _set_pilot_rungs,
    _until_unresolved_condition,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.pulse import _apply_pulse
from pyrung.core.analysis.pilot.world_key import (
    _pilot_state_key,
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
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
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
)
from pyrung.core.context import ScanContext
from pyrung.core.crossing import AffineCmp, Cmp, Eq
from pyrung.core.harness import Harness
from pyrung.core.instruction.advance import ConditionDemand
from pyrung.core.instruction.timers import OnDelayInstruction
from pyrung.core.physical import Physical
from pyrung.core.program.context._state import _current_rung
from pyrung.core.runner import PLC

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


def _variant_named_timer_program():
    """On-delay timer whose Done/TT/Acc bits break the ``<base>_Done`` /
    ``<base>_TT`` naming convention.

    Fast-forwarding resolves the timing (TT) register through the instruction's
    ``advance_profile()``. Deriving ``TimerReady_TT`` from the Done name would
    miss the actual ``TimerActive`` register.
    """
    Enable = Bool("Enable", external=True)
    Ready = Bool("TimerReady")  # done bit — not ``*_Done``
    Active = Bool("TimerActive")  # timing/TT bit — not ``*_TT``
    Count = Int("TimerCount")  # accumulator
    Out = Bool("Out")
    # strict=False so the raw OnDelayInstruction can be added inside the rung
    # (the DSL builder always mints convention-named UDT bits).
    with Program(strict=False) as prog:
        with Rung(Enable):
            _current_rung()._rung.add_instruction(
                OnDelayInstruction(Ready, Count, 100, Enable, None, "ms", tt_bit=Active)
            )
        with Rung(Ready):
            out(Out)
    return prog


class TestCoastToValue:
    def test_reaches_target(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        receipt = _coast_to_value(plc, "Done", True, budget=50)
        assert receipt.stop_reason == "reached"
        assert receipt.fired == ("target",)
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
        """If the channel tag goes to an unexpected third value, coast stops."""
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        # Don't enable the timer — Done stays False, never reaches True
        receipt = _coast_to_value(plc, "Done", True, budget=20)
        assert receipt.stop_reason != "reached"
        assert receipt.stop_reason == "timeout"

    def test_none_channel_tag_returns_false(self):
        prog = _timer_program()
        plc = PLC(prog, dt=0.010)
        receipt = _coast_to_value(plc, None, True, budget=20)
        assert receipt.stop_reason != "reached"
        assert receipt.stop_reason == "skipped"


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
        receipt = _coast_holding_state(plc, "Target", True, role_tags=("State",), budget=200)
        # Target never reached; role State flipped 1 -> 2 so coast ejected early.
        assert receipt.stop_reason != "reached"
        assert receipt.stop_reason == "departed"
        assert receipt.fired == ("ejected",)
        assert plc.state.tags["State"] == 2
        assert plc.state.scan_id - scan_before < 200

    def test_conditional_holds_toggle_each_scan(self):
        from pyrung.core.condition import AllCondition

        prog = _free_timer_program()
        plc = PLC(prog, dt=0.010)
        plc.step()

        start_scan = plc.state.scan_id
        Input = plc._known_tags_by_name["Input"]
        Target = plc._known_tags_by_name["Target"]
        plc = fork_with_pilot_rungs(
            plc,
            [
                PilotRung("Input", True, AllCondition(~Target, ~Input)),
                PilotRung("Input", False, AllCondition(~Target, Input)),
            ],
        )
        receipt = _coast_holding_state(plc, "Target", True, role_tags=(), budget=200)
        assert receipt.stop_reason == "reached"
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

    def test_world_key_distinguishes_installed_rungs(self):
        cfg = _StateKeyConfig(
            stateful_names=("A",),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        _prog, _In, Scope = _scoped_input_program()
        snap = {"A": 1}
        bare = _pilot_world_key(snap, cfg, ())
        corrected = _pilot_world_key(
            snap,
            cfg,
            (PilotRung("In", True, ~Scope),),
        )
        rebuilt = _pilot_world_key(
            snap,
            cfg,
            (PilotRung("In", True, ~Scope),),
        )

        assert corrected != bare
        assert rebuilt == corrected


# ---------------------------------------------------------------------------
# _apply_pulse
# ---------------------------------------------------------------------------


def _scoped_input_program():
    In = Bool("In", external=True)
    Scope = Bool("Scope", external=True)
    Out = Bool("Out")
    ScopeSeen = Bool("ScopeSeen")
    with Program() as prog:
        with Rung(In):
            out(Out)
        with Rung(Scope):
            out(ScopeSeen)
    return prog, In, Scope


class TestPilotRungs:
    def test_guard_is_required(self):
        with pytest.raises(ValueError, match="guard is required"):
            PilotRung("In", True, None)

    def test_operation_receipt_has_overlay_semantic_rung_identity(self):
        _prog, _In, Scope = _scoped_input_program()
        receipt = OperationReceipt(Scope)
        receipt_key = (
            "pyrung.core.analysis.pilot.overlay",
            "OperationReceipt",
            (("progress", None), ("until", ("tag", Scope.name))),
        )
        rung = PilotRung("In", True, ~Scope, receipt)

        assert OperationReceipt.__module__ == "pyrung.core.analysis.pilot.overlay"
        assert _semantic_key(receipt) == receipt_key
        assert _rung_identity(rung) == (
            "In",
            True,
            _semantic_key(~Scope),
            receipt_key,
        )

    def test_all_guards_read_one_pre_overlay_snapshot(self):
        prog, In, _Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        _set_pilot_rungs(
            plc,
            [PilotRung("In", True, ~In), PilotRung("In", False, In)],
        )
        plc.step()
        assert plc.state.tags["In"] is True
        plc.step()
        assert plc.state.tags["In"] is False

    def test_last_active_rung_wins(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        pilot_rungs = _merged_pilot_rungs([PilotRung("In", True, ~Scope)], [])
        pilot_rungs = _merged_pilot_rungs(
            [PilotRung("In", False, ~Scope)], pilot_rungs
        )
        plc = fork_with_pilot_rungs(plc, pilot_rungs)
        plc.step()
        assert plc.state.tags["In"] is False

    def test_execution_receipt_matches_expanded_continuation_precedence(self):
        """Every installed rule receives one compiler-owned execution state."""
        In = Bool("ReceiptIn", external=True)
        Scope = Bool("ReceiptScope", external=True)
        ProgressA = Bool("ProgressA", external=True)
        ProgressB = Bool("ProgressB", external=True)
        Never = Bool("Never", external=True)
        with Program() as prog:
            with Rung(In):
                out(Bool("ReceiptOut"))
            with Rung(ProgressA, ProgressB, Never):
                out(Bool("ProgressSeen"))
        plc = PLC(prog, dt=0.010)
        continuing = PilotRung(
            In.name,
            True,
            ~Scope,
            OperationReceipt(In, ConditionDemand(CompareEq(ProgressA, True))),
        )
        eligible = PilotRung(In.name, False, ~Scope)
        effective = PilotRung(
            In.name,
            False,
            ~Scope,
            OperationReceipt(~In, ConditionDemand(CompareEq(ProgressB, True))),
        )
        shadowed = PilotRung(
            In.name,
            True,
            ~Scope,
            OperationReceipt(In, ConditionDemand(CompareEq(Never, True))),
        )
        dormant = PilotRung(In.name, True, Scope)
        pilot_rungs = (continuing, eligible, effective, shadowed, dormant)
        snapshot = dict(plc.state.tags)
        snapshot.update({ProgressA.name: True, ProgressB.name: True, Never.name: False})

        receipt = _pilot_rung_execution_receipt(pilot_rungs, snapshot)

        assert tuple(entry.state for entry in receipt.pilot_rungs) == (
            PilotRungExecutionState.CONTINUING,
            PilotRungExecutionState.ELIGIBLE,
            PilotRungExecutionState.EFFECTIVE,
            PilotRungExecutionState.SHADOWED,
            PilotRungExecutionState.DORMANT,
        )
        assert receipt.pilot_rungs[0].continuation
        assert receipt.pilot_rungs[2].continuation
        assert receipt.owner(In.name) is effective

        _set_pilot_rungs(plc, pilot_rungs)
        plc.patch({ProgressA.name: True, ProgressB.name: True, Never.name: False})
        plc.step()
        plc.step()
        assert plc.state.tags[In.name] is effective.value

    def test_operation_progress_continues_after_its_start_guard_closes(self):
        In = Bool("ProgressLifetime_In", external=True)
        Start = Bool("ProgressLifetime_Start", external=True)
        Progress = Bool("ProgressLifetime_Progress", external=True)
        with Program() as prog:
            with Rung(In, Start, Progress):
                out(Bool("ProgressLifetime_Seen"))
        plc = PLC(prog, dt=0.010)
        operation = PilotRung(
            In.name,
            True,
            Start,
            OperationReceipt(
                ~Progress,
                ConditionDemand(CompareEq(Progress, True)),
            ),
        )
        _set_pilot_rungs(plc, (operation,))

        # The operation has left the context that started it, but its owner
        # still reports affirmative in-flight progress.
        plc.patch({Start.name: False, Progress.name: True})
        plc.step()
        plc.step()

        receipt = _pilot_rung_execution_receipt((operation,), dict(plc.state.tags))
        assert plc.state.tags[In.name] is True
        assert receipt.pilot_rungs[0].continuation
        assert receipt.owner(In.name) is operation

    def test_semantically_duplicate_rung_is_not_another_world_change(self):
        _prog, _In, Scope = _scoped_input_program()
        pilot_rungs = _merged_pilot_rungs([PilotRung("In", True, ~Scope)], [])
        pilot_rungs = _merged_pilot_rungs(
            [PilotRung("In", True, ~Scope)], pilot_rungs
        )

        assert len(pilot_rungs) == 1

    def test_inactive_specialization_preserves_active_general_rung(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        _set_pilot_rungs(
            plc,
            [PilotRung("In", True, ~Scope), PilotRung("In", False, Scope)],
        )
        plc.step()
        assert plc.state.tags["In"] is True

    def test_no_active_rung_returns_input_to_default(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        _set_pilot_rungs(plc, [PilotRung("In", True, Scope)])
        plc.patch({"In": True})
        plc.step()
        assert plc.state.tags["In"] is True
        plc.step()
        assert plc.state.tags["In"] is False

    def test_patch_overrides_rung_for_one_scan(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        _set_pilot_rungs(plc, [PilotRung("In", True, ~Scope)])
        plc.patch({"In": False})
        plc.step()
        assert plc.state.tags["In"] is False
        plc.step()
        assert plc.state.tags["In"] is True

    def test_fork_rebuilds_ordered_rungs(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        fork = fork_with_pilot_rungs(plc, [PilotRung("In", True, ~Scope)])
        fork.step()
        assert fork.state.tags["In"] is True

    def test_trace_completion_lowers_to_unresolved_guard(self):
        from pyrung.core.analysis.simplified import Atom
        from pyrung.core.context import ScanContext

        prog, _In, _Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        guard = _until_unresolved_condition(plc, Atom("Scope", "eq", True))
        assert guard.evaluate(ScanContext(plc.state)) is True
        plc.patch({"Scope": True})
        plc.step()
        assert guard.evaluate(ScanContext(plc.state)) is False

    def test_route_constraint_lowers_to_runtime_comparison(self):
        Value = Int("ConstraintValue")
        with Program() as prog:
            with Rung():
                copy(0, Value)

        plc = PLC(prog, dt=0.010)
        condition = _constraint_condition(plc, Cmp(Value.name, ">=", 50))

        assert isinstance(condition, CompareGe)
        assert condition.tag is Value
        assert condition.value == 50

    @pytest.mark.parametrize(
        ("op", "condition_type"),
        [
            ("==", CompareEq),
            ("!=", CompareNe),
            ("<", CompareLt),
            ("<=", CompareLe),
            (">", CompareGt),
            (">=", CompareGe),
        ],
    )
    def test_route_constraint_supports_up_and_down_boundaries(self, op, condition_type):
        Value = Int(f"ConstraintValue_{condition_type.__name__}")
        with Program() as prog:
            with Rung():
                copy(0, Value)

        condition = _constraint_condition(PLC(prog), Cmp(Value.name, op, 50))

        assert isinstance(condition, condition_type)

    def test_affine_tag_bound_lowers_without_losing_offset(self):
        Value = Int("ConstraintValue")
        Preset = Int("ConstraintPreset")
        with Program() as prog:
            with Rung():
                copy(Preset, Value)

        plc = PLC(prog)
        condition = _constraint_condition(
            plc,
            AffineCmp(Value.name, ">=", Preset.name, scale=1, offset=-1),
        )

        assert condition.evaluate(
            ScanContext(plc.state.with_tags({Value.name: 9, Preset.name: 10}))
        )
        assert not condition.evaluate(
            ScanContext(plc.state.with_tags({Value.name: 8, Preset.name: 10}))
        )

    def test_multivalue_equality_and_its_inverse_preserve_set_semantics(self):
        prog, _In, Scope = _scoped_input_program()
        plc = PLC(prog, dt=0.010)
        constraint = Eq(Scope.name, frozenset({True, False}))

        direct = _constraint_condition(plc, constraint)
        inverse = _constraint_condition(plc, constraint, unresolved=True)

        assert isinstance(direct, AnyCondition)
        assert all(isinstance(term, CompareEq) for term in direct.conditions)
        assert isinstance(inverse, AllCondition)
        assert all(isinstance(term, CompareNe) for term in inverse.conditions)


def _single_input_program():
    In = Bool("In", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(In):
            out(Out)
    return prog


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

        _settle_delayed_effects(plc, scan_budget=200)
        assert harness.pending_count == 0
        # feedback resolved -> the gated copy fired
        assert plc.state.tags["Stage"] == 1

    def test_pending_timer_is_left_for_its_advance_owner(self):
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

        scan_before = plc.state.scan_id
        receipts = _settle_delayed_effects(plc, scan_budget=500)
        assert receipts == []
        assert plc.state.scan_id == scan_before
        assert plc.state.tags["Tmr_Done"] is False
        assert plc.state.tags["Tmr_TT"] is True

    def test_variant_named_timing_is_also_left_for_its_advance_owner(self):
        # A timer whose bits are NOT named ``<base>_Done`` / ``<base>_TT``: the
        # settle must still coast it by resolving the TT register off the
        # instruction's profile, not by deriving the absent ``TimerReady_TT``.
        prog = _variant_named_timer_program()
        plc = PLC(prog, dt=0.010)

        # The operation carries the timing receipt, resolved off its owner.
        owner = build_advance_index(prog).resolve("TimerReady")
        assert owner is not None
        operation = owner.profile.plan(
            Eq("TimerReady", frozenset((True,))),
            dict(plc.state.tags),
        )
        assert operation is not None
        assert operation.progress is not None
        assert operation.progress.condition.tag.name == "TimerActive"

        plc.patch({"Enable": True})
        plc.step()
        # PENDING: accumulator advancing, TT active, done still False.
        assert plc.state.tags["TimerActive"] is True
        assert plc.state.tags["TimerReady"] is False

        scan_before = plc.state.scan_id
        receipts = _settle_delayed_effects(plc, scan_budget=500)
        assert receipts == []
        assert plc.state.scan_id == scan_before
        assert plc.state.tags["TimerReady"] is False
        assert plc.state.tags["TimerActive"] is True


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
