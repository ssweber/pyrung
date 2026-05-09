"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib
from dataclasses import replace
from pathlib import Path

import pytest

from pyrung.cli import _apply_lock_config
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    InputBlock,
    Int,
    Or,
    OutputBlock,
    Program,
    Rung,
    TagType,
    Timer,
    calc,
    copy,
    count_down,
    count_up,
    fall,
    fill,
    forloop,
    latch,
    named_array,
    off_delay,
    on_delay,
    out,
    reset,
    rise,
    run_function,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
    StateDiff,
    TraceStep,
    _bfs_explore,
    _classify_dimensions,
    _default_projection,
    _eval_atom,
    _has_data_feedback,
    _live_inputs,
    _partial_eval,
    _pilot_sweep_domains,
    check_lock,
    diff_states,
    program_hash,
    prove,
    reachable_states,
    write_lock,
)
from pyrung.core.analysis.prove.passes import _BFSConfig
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr

from .conftest import no_agreement

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a prove() counterexample trace on the concrete PLC."""
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


# ===================================================================
# Group 1: Dimension classification
# ===================================================================


class TestDimensionClassification:
    def test_ote_only_all_combinational(self):
        """Program with only OTE writes — all tags combinational, zero state dims."""
        input_a = Bool("InputA", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(input_a):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Light" not in stateful
        assert "Light" in combinational
        assert "InputA" in nd

    def test_latch_reset_are_stateful(self):
        """Latch/reset writes make tags stateful when referenced."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung(running):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Running" in stateful
        assert stateful["Running"] == (False, True)
        assert "Button" in nd
        assert "Stop" in nd

    def test_unreferenced_bool_excluded(self):
        """Written Bool not referenced in any expression is excluded."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Flag" not in stateful
        assert "Flag" in combinational

    def test_write_only_terminal_is_combinational(self):
        """Tag written by copy but never read is combinational (dead output)."""
        sensor = Bool("Sensor", external=True)
        level = Int("Level", external=True, min=0, max=10)
        output = Int("Output")

        with Program(strict=False) as logic:
            with Rung(sensor):
                copy(level, output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Output" not in stateful
        assert "Output" in combinational

    def test_external_tags_are_nondeterministic(self):
        """Tags with external=True are nondeterministic."""
        sensor = Bool("Sensor", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(sensor):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Sensor" in nd

    def test_readonly_tags_excluded(self):
        """Readonly tags are excluded from enumeration."""
        config = Bool("Config", readonly=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(config):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Config" not in nd
        assert "Config" not in stateful

    def test_timer_done_is_bool_state_dim(self):
        """Timer.Done is a Bool state dimension."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "T1_Done" in stateful
        assert stateful["T1_Done"] == (False, PENDING, True)


# ===================================================================
# Group 2: Value domain extraction
# ===================================================================


class TestValueDomainExtraction:
    def test_bool_domain(self):
        """Bool tags always have domain (False, True)."""
        flag = Bool("Flag", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(flag):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert nd["Flag"] == (False, True)

    def test_integer_comparison_literals(self):
        """Pure equality-only inputs collapse to {literals..., OTHER}."""
        state = Int("State", external=True)
        out_a = Bool("OutA")
        out_b = Bool("OutB")

        with Program(strict=False) as logic:
            with Rung(state == 1):
                out(out_a)
            with Rung(state == 2):
                out(out_b)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["State"]
        assert 1 in domain
        assert 2 in domain
        assert len(domain) == 3

    def test_choices_tag_uses_declared_domain(self):
        """Tag with choices uses the declared values."""
        mode = Int("Mode", external=True, choices={0: "off", 1: "on", 2: "auto"})
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(mode == 1):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert set(nd["Mode"]) == {0, 1, 2}

    def test_eq_ne_only_input_gets_literal_plus_other_domain(self):
        """Pure-switch eq/ne inputs collapse to {literals..., OTHER}."""
        mode = Int("Mode", external=True)
        light = Bool("Light")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(mode == 1):
                out(light)
            with Rung(mode != 2):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert 1 in nd["Mode"]
        assert 2 in nd["Mode"]
        assert len(nd["Mode"]) == 3

    def test_eq_ne_only_closure_rejects_data_flow_usage(self):
        """Arithmetic/data-flow usage keeps eq/ne-only closure from applying."""
        step = Int("Step", external=True)
        odd = Int("Odd")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                calc(step % 2, odd)
            with Rung(step == 5):
                out(flag)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert set(nd["Step"]) == {0, 4, 5, 6}

    def test_unannotated_function_output_is_intractable(self):
        """run_function output without choices/min/max returns Intractable."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Result" in result.reason

    def test_stateful_tag_with_min_max_written_by_unsupported_instruction(self):
        """Stateful tag written by run_function but carrying min/max uses declared domain."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result", min=0, max=5)
        alarm = Bool("Alarm")

        def compute() -> dict:
            return {"result": 3}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung():
                copy(result_tag, Int("Stored"))
            with Rung(result_tag > 3):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), (
            f"Expected domain from min/max, got: {result.reason}"
        )
        stateful, _nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "Result" in stateful
        assert set(stateful["Result"]) == set(range(0, 6))

    def test_bounded_integer_with_comparison_uses_boundary_partition(self):
        """Int with min/max and comparison literals uses boundary partitioning."""
        level = Int("Level", external=True, min=0, max=100)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level > 5):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Level"]
        assert len(domain) < 20
        assert 0 in domain
        assert 100 in domain
        assert 5 in domain
        assert 6 in domain

    def test_bounded_integer_without_comparison_uses_full_range(self):
        """Int with small min/max and no comparisons uses full range."""
        level = Int("Level", external=True, min=0, max=10)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(level):
                out(target)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

    def test_boundary_partition_includes_min_max_anchors(self):
        """Boundary partition always includes min and max values."""
        level = Int("Level", external=True, min=0, max=50)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level > 25):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Level"]
        assert 0 in domain
        assert 50 in domain

    def test_boundary_values_clamped_to_min_max(self):
        """Boundary partition does not include values outside min/max."""
        level = Int("Level", external=True, min=0, max=100)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level > 0):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Level"]
        assert all(0 <= v <= 100 for v in domain)

    def test_boundary_partition_proves_correctly(self):
        """End-to-end prove with boundary-partitioned integer domain."""
        level = Int("Level", external=True, min=0, max=100)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level > 50):
                latch(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("Level", 0) > 50 for step in result.trace)

    def test_multiple_comparisons_boundary_partition(self):
        """Multiple comparison literals produce compact boundary domain."""
        level = Int("Level", external=True, min=0, max=100)
        low = Bool("Low")
        high = Bool("High")

        with Program(strict=False) as logic:
            with Rung(level > 5):
                out(low)
            with Rung(level > 20):
                out(high)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Level"]
        assert 0 in domain
        assert 5 in domain
        assert 6 in domain
        assert 20 in domain
        assert 21 in domain
        assert 100 in domain
        assert len(domain) < 20


# ===================================================================
# Group 3: Don't-care pruning
# ===================================================================


class TestDontCarePruning:
    def test_masked_input_not_live(self):
        """And(StateBit, Input) with StateBit=False → Input not live."""
        state_expr = ExprAnd((Atom("StateBit", "xic"), Atom("Input", "xic")))
        result = _partial_eval(state_expr, {"StateBit": False})
        assert isinstance(result, Const)
        assert result.value is False

    def test_unmasked_input_is_live(self):
        """And(StateBit, Input) with StateBit=True → Input remains."""
        state_expr = ExprAnd((Atom("StateBit", "xic"), Atom("Input", "xic")))
        result = _partial_eval(state_expr, {"StateBit": True})
        assert not isinstance(result, Const)

    def test_live_inputs_partial_masking(self):
        """Some inputs masked, others live — correct subset."""
        exprs = [
            ExprAnd((Atom("StateBit", "xic"), Atom("InputA", "xic"))),
            Atom("InputB", "xic"),
        ]
        nd_dims = {
            "InputA": (False, True),
            "InputB": (False, True),
        }
        state = {"StateBit": False, "InputA": False, "InputB": False}
        live = _live_inputs(state, nd_dims, exprs)
        assert "InputB" in live
        assert "InputA" not in live

    def test_all_inputs_live_in_or(self):
        """All inputs in top-level Or are live."""
        exprs = [ExprOr((Atom("InputA", "xic"), Atom("InputB", "xic")))]
        nd_dims = {"InputA": (False, True), "InputB": (False, True)}
        state = {"InputA": False, "InputB": False}
        live = _live_inputs(state, nd_dims, exprs)
        assert live == {"InputA", "InputB"}

    def test_hidden_residual_tag_keeps_upstream_input_live(self):
        """Residual hidden tags propagate liveness through their ND upstream deps."""
        exprs = [Atom("Stored", "gt", 150)]
        nd_dims = {"Source": tuple(range(3))}
        live = _live_inputs({}, nd_dims, exprs, {"Stored": frozenset({"Source"})})
        assert live == {"Source"}

    def test_eval_atom_xic(self):
        assert _eval_atom(Atom("X", "xic"), True) is True
        assert _eval_atom(Atom("X", "xic"), False) is False

    def test_eval_atom_xio(self):
        assert _eval_atom(Atom("X", "xio"), True) is False
        assert _eval_atom(Atom("X", "xio"), False) is True

    def test_eval_atom_eq(self):
        assert _eval_atom(Atom("X", "eq", 5), 5) is True
        assert _eval_atom(Atom("X", "eq", 5), 3) is False

    def test_eval_atom_rise_returns_none(self):
        assert _eval_atom(Atom("X", "rise"), True) is None


# ===================================================================
# Group 4: BFS & public API
# ===================================================================


class TestProve:
    def test_basic_property_holds(self):
        """Conveyor-style: Running implies EstopOK → Proven."""
        estop = Bool("EstopOK", external=True)
        start = Bool("Start", external=True)
        running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(start, estop):
                latch(running)
            with Rung(~estop):
                reset(running)

        result = prove(logic, Or(~running, estop))
        assert isinstance(result, Proven)
        assert result.states_explored > 0

    def test_property_violation(self):
        """Property that doesn't hold returns Counterexample."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        result = prove(logic, ~flag)
        assert isinstance(result, Counterexample)
        assert len(result.trace) > 0
        assert isinstance(result.trace[0], TraceStep)

    def test_less_than_partition_explores_below_literal(self):
        """Level < 5 includes values on both sides of the comparison."""
        level = Int("Level", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level < 5):
                latch(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample)
        trace_levels = {v for step in result.trace if (v := step.inputs.get("Level")) is not None}
        assert any(v < 5 for v in trace_levels) or result.trace[0].scans == 0

    def test_property_expression_contributes_input_domain(self):
        """Property-only comparison literals are included in exploration domains."""
        level = Int("Level", external=True)
        seen_zero = Bool("SeenZero")

        with Program(strict=False) as logic:
            with Rung(level == 0):
                out(seen_zero)

        result = prove(logic, level < 5)
        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("Level") in {5, 6} for step in result.trace)

    def test_tag_comparison_explores_operand_tag_domain(self):
        """A > B keeps B live and explores B's finite domain."""
        a = Int("A", external=True, min=0, max=1)
        b = Int("B", external=True, default=1, min=0, max=1)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(a > b):
                latch(target)

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("B") == 0 for step in result.trace)

    def test_oneshot_memory_state_allows_rearming_after_false_scan(self):
        """One-shot runtime memory is part of the visited-state key."""
        gate = Bool("Gate", external=True)
        fired_before = Bool("FiredBefore")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(gate):
                out(target, oneshot=True)
            with Rung(target, ~fired_before):
                latch(fired_before)
                reset(target)

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample)
        gate_values = [step.inputs.get("Gate") for step in result.trace if "Gate" in step.inputs]
        assert gate_values == [True, False, True]

    def test_counterexample_trace_is_replayable(self):
        """Counterexample trace reproduces the violation on a real PLC."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        result = prove(logic, ~flag)
        assert isinstance(result, Counterexample)

        runner = _replay_trace(logic, result.trace)
        assert runner.current_state.tags.get("Flag") is True

    def test_exact_hidden_event_counterexample_trace_replays_with_full_scan_count(self):
        """Exact accelerated traces should replay concretely with their reported scans."""
        enable = Bool("Enable", external=True)
        threshold = Int("ExactThreshold", final=True)
        t = Timer.clone("ExactTraceTmr")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = prove(logic, ~alarm, depth_budget=5)
        assert isinstance(result, Counterexample)
        assert not result.caveats
        assert any(step.scans > 1 for step in result.trace)
        assert sum(step.scans for step in result.trace) == 51

        runner = _replay_trace(logic, result.trace)
        assert runner.current_state.tags.get("Alarm") is True

    def test_abstract_threshold_counterexample_carries_nonreplayable_trace_caveat(self):
        """Abstract threshold witnesses should be called out explicitly on counterexamples."""
        enable = Bool("Enable", external=True)
        hmi_threshold = Int(
            "HmiThreshold",
            external=True,
            choices={500: "Near", 1000: "Far"},
            default=1000,
        )
        t = Timer.clone("AbstractTraceTmr")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > hmi_threshold):
                out(alarm)

        result = prove(logic, ~alarm, depth_budget=5)
        assert isinstance(result, Counterexample)
        assert any("abstract threshold witness" in caveat for caveat in result.caveats)

        runner = _replay_trace(logic, result.trace)
        assert runner.current_state.tags.get("Alarm") is not True

    def test_callable_predicate_fallback(self):
        """Callable predicate still works for complex properties."""
        estop = Bool("EstopOK", external=True)
        start = Bool("Start", external=True)
        running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(start, estop):
                latch(running)
            with Rung(~estop):
                reset(running)

        result = prove(
            logic,
            lambda s: not s.get("Running") or s.get("EstopOK"),
        )
        assert isinstance(result, Proven)

    def test_max_states_cap(self):
        """Large state space hits max_states cap → Intractable."""
        inputs = [Bool(f"In{i}", external=True) for i in range(20)]
        flags = [Bool(f"Flag{i}") for i in range(20)]
        output = Bool("Output")

        with Program(strict=False) as logic:
            for inp, flag in zip(inputs, flags, strict=True):
                with Rung(rise(inp)):
                    latch(flag)
            with Rung(*flags):
                out(output)

        result = prove(
            logic,
            lambda s: True,
            max_states=10,
        )
        assert isinstance(result, Intractable)
        assert "max_states" in result.reason

    def test_property_list_batches_results(self):
        """A sole list argument batch-proves properties in one pass."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        result = prove(logic, [~flag, Or(flag, ~flag)])
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], Counterexample)
        assert isinstance(result[1], Proven)

    def test_tuple_property_keeps_grouped_and_semantics(self):
        """Tuple input still means one property with implicit AND semantics."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(a, b):
                latch(light)

        result = prove(logic, (~light, ~a))
        assert isinstance(result, Counterexample)

    def test_readonly_named_array_symbol_is_treated_as_literal_constant(self):
        """Readonly named-array symbols in comparisons should prove like literals."""

        @named_array(Int, stride=2, readonly=True)
        class SortState:
            IDLE = 0
            RUNNING = 1

        state = Int("State", choices=SortState, default=SortState.IDLE)

        with Program(strict=False) as logic:
            with Rung(state == SortState.IDLE):
                out(Bool("AtIdle"))

        result = prove(
            logic,
            Or(state == SortState.IDLE, state == SortState.RUNNING),
        )
        assert isinstance(result, Proven)


class TestReachableStates:
    def test_reachable_states_basic(self):
        """Simple latch program — known reachable set."""
        button = Bool("Button", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        light_values = {dict(s)["Light"] for s in states}
        assert False in light_values
        assert True in light_values

    def test_combinational_projection_collapses_duplicate_input_outcomes(self):
        """Irrelevant extra inputs do not create duplicate projected states."""
        a = Bool("A", external=True)
        _b = Bool("B", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(a):
                out(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Light", False)}),
                frozenset({("Light", True)}),
            }
        )

    def test_projection_preserves_distinct_states_with_same_abstract_key(self):
        """Projected public/input state is preserved even when the abstract key is identical."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        light = Bool("Light")
        other = Bool("Other")

        with Program(strict=False) as logic:
            with Rung(a):
                out(light)
            with Rung(b):
                out(other)

        states = reachable_states(logic, project=["Light", "Other"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Light", False), ("Other", False)}),
                frozenset({("Light", False), ("Other", True)}),
                frozenset({("Light", True), ("Other", False)}),
                frozenset({("Light", True), ("Other", True)}),
            }
        )

    def test_equivalent_refactors_produce_same_states(self):
        """Two equivalent programs produce identical projected state sets."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")

        with Program(strict=False) as logic_a:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)

        running2 = Bool("Running")
        with Program(strict=False) as logic_b:
            with Rung(button):
                latch(running2)
            with Rung(stop):
                reset(running2)

        states_a = reachable_states(logic_a, project=["Running"])
        states_b = reachable_states(logic_b, project=["Running"])
        assert not isinstance(states_a, Intractable)
        assert not isinstance(states_b, Intractable)
        assert states_a == states_b

    def test_behavioral_change_detected(self):
        """Behavioral change produces non-empty StateDiff."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")

        with Program(strict=False) as original:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)

        with Program(strict=False) as modified:
            with Rung(button):
                latch(running)
            # removed reset — Running can never turn off

        before = reachable_states(original, project=["Running"])
        after = reachable_states(modified, project=["Running"])
        assert not isinstance(before, Intractable)
        assert not isinstance(after, Intractable)
        d = diff_states(before, after)
        # both have Running=True and Running=False initially reachable,
        # but the modified version may differ in other projected tags
        assert isinstance(d, StateDiff)

    def test_public_projection(self):
        """Project to public tags — internal changes don't affect result."""
        button = Bool("Button", external=True)
        running = Bool("Running", public=True)
        internal = Bool("Internal")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
                out(internal)

        states = reachable_states(logic, project=["Running"])
        assert not isinstance(states, Intractable)
        for s in states:
            keys = {k for k, _v in s}
            assert "Running" in keys
            assert "Internal" not in keys

    def test_projected_raw_inputs_disable_exclusive_grouping(self):
        cmd_a = Bool("CmdA", external=True)
        cmd_b = Bool("CmdB", external=True)
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"})

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(cmd_a):
                copy(1, cmd)
            with Rung(cmd_b):
                copy(2, cmd)

        states = reachable_states(logic, project=["CmdA", "CmdB", "Cmd"])
        assert isinstance(states, frozenset)
        assert frozenset({("CmdA", True), ("CmdB", True), ("Cmd", "B")}) in states

    def test_exclusive_input_grouping_preserves_reachable_states(self):
        cmd_a = Bool("CmdA", external=True)
        cmd_b = Bool("CmdB", external=True)
        cmd_c = Bool("CmdC", external=True)
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B", 3: "C"}, lock=True)
        flag = Bool("Flag", lock=True)

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(cmd_a):
                copy(1, cmd)
            with Rung(cmd_b):
                copy(2, cmd)
            with Rung(cmd_c):
                copy(3, cmd)
            with Rung(cmd == 1):
                out(flag)

        context = prove_module._build_reachable_context(
            logic,
            scope=["Flag", "Cmd"],
            project=("Flag", "Cmd"),
        )
        assert not isinstance(context, Intractable)

        grouped = _bfs_explore(
            context,
            project=("Flag", "Cmd"),
            bfs_config=_BFSConfig(),
        )
        ungrouped = _bfs_explore(
            context,
            project=("Flag", "Cmd"),
            bfs_config=_BFSConfig(exclusive_input_grouping=False),
        )

        assert grouped == ungrouped

    def test_reaction_time_window_all_outcomes_reachable(self):
        """Three-outcome timing window: TooSoon / Perfect / TooLate are each reachable."""
        button = Bool("Button", external=True)
        running = Bool("Running")
        too_soon = Bool("TooSoon", lock=True)
        perfect = Bool("Perfect", lock=True)
        too_late = Bool("TooLate", lock=True)
        done_tmr = Timer.clone("DoneTmr")
        watchdog_tmr = Timer.clone("WatchdogTmr")

        with Program(strict=False) as logic:
            with Rung(~too_soon, ~perfect, ~too_late):
                out(running)
                on_delay(done_tmr, preset=1000)
                on_delay(watchdog_tmr, preset=2000)
            with Rung(running, ~done_tmr.Done, button):
                latch(too_soon)
            with Rung(running, done_tmr.Done, ~watchdog_tmr.Done, button):
                latch(perfect)
            with Rung(running, watchdog_tmr.Done, button):
                latch(too_late)

        states = reachable_states(logic, project=["TooSoon", "Perfect", "TooLate"], depth_budget=60)
        assert not isinstance(states, Intractable)
        # Exactly four states: idle + one of three mutually exclusive outcomes
        assert states == frozenset(
            {
                frozenset({("TooSoon", False), ("Perfect", False), ("TooLate", False)}),
                frozenset({("TooSoon", True), ("Perfect", False), ("TooLate", False)}),
                frozenset({("TooSoon", False), ("Perfect", True), ("TooLate", False)}),
                frozenset({("TooSoon", False), ("Perfect", False), ("TooLate", True)}),
            }
        )

    def test_reaction_time_window_counters(self):
        """Counter variant: three mutually exclusive count thresholds."""
        trigger = Bool("Trigger", external=True)
        running = Bool("Running")
        too_few = Bool("TooFew", lock=True)
        just_right = Bool("JustRight", lock=True)
        too_many = Bool("TooMany", lock=True)
        ready_ctr = Counter.clone("ReadyCtr")
        limit_ctr = Counter.clone("LimitCtr")
        never = Bool("Never")

        with Program(strict=False) as logic:
            with Rung(~too_few, ~just_right, ~too_many):
                out(running)
            with Rung(running):
                count_up(ready_ctr, preset=3).reset(never)
            with Rung(running):
                count_up(limit_ctr, preset=5).reset(never)
            with Rung(running, ~ready_ctr.Done, trigger):
                latch(too_few)
            with Rung(running, ready_ctr.Done, ~limit_ctr.Done, trigger):
                latch(just_right)
            with Rung(running, limit_ctr.Done, trigger):
                latch(too_many)

        states = reachable_states(
            logic, project=["TooFew", "JustRight", "TooMany"], depth_budget=60
        )
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("TooFew", False), ("JustRight", False), ("TooMany", False)}),
                frozenset({("TooFew", True), ("JustRight", False), ("TooMany", False)}),
                frozenset({("TooFew", False), ("JustRight", True), ("TooMany", False)}),
                frozenset({("TooFew", False), ("JustRight", False), ("TooMany", True)}),
            }
        )

    def test_reaction_time_window_acc_threshold(self):
        """Same pattern using Acc < preset instead of ~Done — currently lost by absorption."""
        button = Bool("Button", external=True)
        running = Bool("Running")
        too_soon = Bool("TooSoon", lock=True)
        perfect = Bool("Perfect", lock=True)
        too_late = Bool("TooLate", lock=True)
        done_tmr = Timer.clone("DoneTmr")
        watchdog_tmr = Timer.clone("WatchdogTmr")

        with Program(strict=False) as logic:
            with Rung(~too_soon, ~perfect, ~too_late):
                out(running)
                on_delay(done_tmr, preset=1000)
                on_delay(watchdog_tmr, preset=2000)
            with Rung(running, done_tmr.Acc < 1000, button):
                latch(too_soon)
            with Rung(running, done_tmr.Done, ~watchdog_tmr.Done, button):
                latch(perfect)
            with Rung(running, watchdog_tmr.Done, button):
                latch(too_late)

        states = reachable_states(logic, project=["TooSoon", "Perfect", "TooLate"], depth_budget=60)
        assert not isinstance(states, Intractable)
        expected = frozenset(
            {
                frozenset({("TooSoon", False), ("Perfect", False), ("TooLate", False)}),
                frozenset({("TooSoon", True), ("Perfect", False), ("TooLate", False)}),
                frozenset({("TooSoon", False), ("Perfect", True), ("TooLate", False)}),
                frozenset({("TooSoon", False), ("Perfect", False), ("TooLate", True)}),
            }
        )
        assert expected <= states


class TestDiffStates:
    def test_empty_diff(self):
        s = frozenset({frozenset({("A", True)})})
        d = diff_states(s, s)
        assert not d.added
        assert not d.removed

    def test_added_state(self):
        before = frozenset({frozenset({("A", False)})})
        after = frozenset({frozenset({("A", False)}), frozenset({("A", True)})})
        d = diff_states(before, after)
        assert frozenset({("A", True)}) in d.added
        assert not d.removed

    def test_removed_state(self):
        before = frozenset({frozenset({("A", False)}), frozenset({("A", True)})})
        after = frozenset({frozenset({("A", False)})})
        d = diff_states(before, after)
        assert not d.added
        assert frozenset({("A", True)}) in d.removed


class TestDefaultProjection:
    """_default_projection returns tags with lock=True."""

    def test_lock_flag_selects_projection(self):
        button = Bool("Button", external=True)
        running = Bool("Running", public=True)
        light = Bool("Light", lock=True)

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
            with Rung(running):
                out(light)

        proj = _default_projection(logic)
        assert proj == ["Light"]

    def test_empty_when_no_lock_tags(self):
        button = Bool("Button", external=True)
        internal = Bool("Internal")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(internal)
            with Rung(internal):
                reset(internal)

        proj = _default_projection(logic)
        assert proj == []

    def test_non_bool_lock_included(self):
        """Non-Bool tags with lock=True are included in projection."""
        button = Bool("Button", external=True)
        light = Bool("Light", lock=True)
        counter_val = Int("CounterVal", lock=True)

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)
                copy(1, counter_val)

        proj = _default_projection(logic)
        assert "Light" in proj
        assert "CounterVal" in proj

    def test_unlocked_terminals_excluded(self):
        """Terminal tags without lock=True are not in the default projection."""
        button = Bool("Button", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        proj = _default_projection(logic)
        assert proj == []


class TestApplyLockConfig:
    """CLI _apply_lock_config include/exclude logic."""

    def test_none_config_passthrough(self):
        proj, joint, exclusive = _apply_lock_config(["A", "B"], None)
        assert proj == ["A", "B"]
        assert joint == ()
        assert exclusive == ()

    def test_include_adds_tags(self):
        proj, _joint, _exclusive = _apply_lock_config(["A"], {"include": ["B", "C"]})
        assert proj == ["A", "B", "C"]

    def test_exclude_removes_tags(self):
        proj, _joint, _exclusive = _apply_lock_config(["A", "B", "C"], {"exclude": ["B"]})
        assert proj == ["A", "C"]

    def test_include_and_exclude(self):
        proj, _joint, _exclusive = _apply_lock_config(
            ["A", "B"], {"include": ["C"], "exclude": ["A"]}
        )
        assert proj == ["B", "C"]

    def test_exclude_nonexistent_is_noop(self):
        proj, _joint, _exclusive = _apply_lock_config(["A"], {"exclude": ["Z"]})
        assert proj == ["A"]

    def test_include_duplicate_is_noop(self):
        proj, _joint, _exclusive = _apply_lock_config(["A", "B"], {"include": ["A"]})
        assert proj == ["A", "B"]

    def test_joint_parses_named_groups(self):
        proj, joint, _exclusive = _apply_lock_config(
            ["A"],
            {"joint": {"faults": ["Estop", "CommFault"]}},
        )
        assert proj == ["A"]
        assert joint == (("Estop", "CommFault"),)

    def test_exclusive_parses_named_groups(self):
        proj, _joint, exclusive = _apply_lock_config(
            ["A"],
            {"exclusive": {"mode": ["Manual", "Auto", "Step"]}},
        )
        assert proj == ["A"]
        assert exclusive == (("Manual", "Auto", "Step"),)


# ===================================================================
# Group 5: Kernel oracle (soundness)
# ===================================================================


class TestKernelOracle:
    def test_timer_accumulation(self):
        """Timer Done bit flips after sufficient BFS depth."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=50)
            with Rung(t.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "T1_Done"], depth_budget=60)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("T1_Done") for s in states}
        assert True in done_values
        assert False in done_values


# ===================================================================
# Group 6: Lock file
# ===================================================================


class TestLockFile:
    def test_round_trip(self, tmp_path: Path):
        """Write and read back a lock file."""
        states = frozenset(
            {
                frozenset({("Running", False), ("Light", False)}),
                frozenset({("Running", True), ("Light", True)}),
            }
        )
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Light", "Running"], "abc123")

        data = __import__("json").loads(lock_path.read_text())
        assert data["version"] == 1
        assert data["program_hash"] == "abc123"
        assert len(data["reachable"]) == 2

    def test_check_detects_change(self, tmp_path: Path):
        """Lock check detects behavioral change."""
        button = Bool("Button", external=True)
        running = Bool("Running", public=True)

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)

        states = reachable_states(logic, project=["Running"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Running"], program_hash(logic))

        d = check_lock(logic, lock_path)
        assert d is None  # should match

    def test_program_hash_changes(self):
        """Different programs produce different hashes."""
        a_in = Bool("A", external=True)
        b = Bool("B")

        with Program(strict=False) as logic_a:
            with Rung(a_in):
                out(b)

        with Program(strict=False) as logic_b:
            with Rung(a_in):
                latch(b)

        assert program_hash(logic_a) != program_hash(logic_b)

    def test_false_omitted_in_json(self, tmp_path: Path):
        """Lock file omits False values — states read as 'what is ON'."""
        import json

        states = frozenset(
            {
                frozenset({("A", False), ("B", False)}),
                frozenset({("A", True), ("B", False)}),
                frozenset({("A", True), ("B", True)}),
            }
        )
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["A", "B"], "hash1")

        data = json.loads(lock_path.read_text())
        reachable = data["reachable"]
        assert len(reachable) == 3
        assert {} in reachable
        assert {"A": True} in reachable
        assert {"A": True, "B": True} in reachable

    def test_false_omission_roundtrips_via_check(self, tmp_path: Path):
        """check_lock round-trips correctly with False-omitted lock files."""
        button = Bool("Button", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Light"], program_hash(logic))

        d = check_lock(logic, lock_path)
        assert d is None

    def test_choice_labels_in_lock(self, tmp_path: Path):
        """Projected tags with choices= serialize labels, not raw ints."""
        import json

        from pyrung.core.analysis.prove import (
            _build_choice_labels,
            _resolve_choice_labels,
        )
        from pyrung.core.tag import Tag, TagType

        mode_tag = Tag(
            name="Mode",
            type=TagType.INT,
            choices={0: "OFF", 1: "SLOW", 2: "FAST"},
        )
        states = frozenset(
            {
                frozenset({("Mode", 0), ("Active", True)}),
                frozenset({("Mode", 1), ("Active", True)}),
                frozenset({("Mode", 2), ("Active", False)}),
            }
        )
        tags = {"Mode": mode_tag}
        choice_labels = _build_choice_labels(["Active", "Mode"], tags)
        resolved = _resolve_choice_labels(states, choice_labels)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, resolved, ["Active", "Mode"], "hash2")

        data = json.loads(lock_path.read_text())
        mode_values = {row.get("Mode") for row in data["reachable"]}
        assert "OFF" in mode_values
        assert "SLOW" in mode_values
        assert "FAST" in mode_values
        assert 0 not in mode_values

    def test_band_labels_collapse_states(self, tmp_path: Path):
        """Tags with band= collapse multiple values into labeled bands."""
        import json

        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        extent_tag = Tag(
            name="Extent",
            type=TagType.INT,
            band={"ZERO": 0, "POSITIVE": ">0"},
        )
        states = frozenset(
            {
                frozenset({("Extent", 0), ("Active", True)}),
                frozenset({("Extent", 1), ("Active", True)}),
                frozenset({("Extent", 2), ("Active", True)}),
                frozenset({("Extent", 3), ("Active", True)}),
            }
        )
        tags = {"Extent": extent_tag}
        band_maps = _build_band_maps(["Active", "Extent"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, resolved, ["Active", "Extent"], "hash3")

        data = json.loads(lock_path.read_text())
        extent_values = {row.get("Extent") for row in data["reachable"]}
        assert extent_values == {"ZERO", "POSITIVE"}
        assert len(data["reachable"]) == 2

    def test_band_range_predicate(self):
        """Band with range predicates (a..b) works."""
        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        level_tag = Tag(
            name="Level",
            type=TagType.INT,
            band={"LOW": "0..2", "HIGH": "3..5"},
        )
        states = frozenset(
            {
                frozenset({("Level", 0)}),
                frozenset({("Level", 1)}),
                frozenset({("Level", 2)}),
                frozenset({("Level", 3)}),
                frozenset({("Level", 4)}),
                frozenset({("Level", 5)}),
            }
        )
        tags = {"Level": level_tag}
        band_maps = _build_band_maps(["Level"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        level_values = {dict(s)["Level"] for s in resolved}
        assert level_values == {"LOW", "HIGH"}

    def test_band_wildcard_catchall(self):
        """Band with '*' catch-all matches unmatched values."""
        from pyrung.core.analysis.prove import _build_band_maps, _resolve_band_labels
        from pyrung.core.tag import Tag, TagType

        tag = Tag(
            name="Score",
            type=TagType.INT,
            band={"ZERO": 0, "OTHER": "*"},
        )
        states = frozenset(
            {
                frozenset({("Score", 0)}),
                frozenset({("Score", 7)}),
                frozenset({("Score", 99)}),
            }
        )
        tags = {"Score": tag}
        band_maps = _build_band_maps(["Score"], tags)
        resolved = _resolve_band_labels(states, band_maps)

        score_values = {dict(s)["Score"] for s in resolved}
        assert score_values == {"ZERO", "OTHER"}


# ===================================================================
# Group 7: Missing coverage items
# ===================================================================


class TestEdgeConditions:
    """Item 13: rise()/fall() in verified programs."""

    def test_rise_in_condition_explores_edge(self):
        """Program with rise() — prev tracking produces correct transitions."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(rise(button)):
                latch(flag)

        result = prove(logic, ~flag)
        assert isinstance(result, Counterexample)
        assert len(result.trace) >= 2

    def test_rise_guard_prevents_relatch(self):
        """rise() fires only on 0→1 edge — holding True doesn't re-trigger."""
        trigger = Bool("Trigger", external=True)
        flag = Bool("Flag")
        second = Bool("Second")

        with Program(strict=False) as logic:
            with Rung(rise(trigger)):
                latch(flag)
            with Rung(flag):
                out(second)

        states = reachable_states(logic, project=["Flag", "Second"])
        assert not isinstance(states, Intractable)


class TestScopeParameter:
    """Item 14: scope= parameter on prove and _classify_dimensions."""

    def test_scope_restricts_inputs(self):
        """Scoped verification only enumerates upstream inputs."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        result = _classify_dimensions(logic, scope=["X"])
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "A" in nd
        assert "B" not in nd

    def test_prove_with_scope(self):
        """prove() with explicit scope restricts exploration."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        result = prove(logic, ~x, scope=["X"])
        assert isinstance(result, Counterexample)


class TestBatchPartitioning:
    """Auto-partition independent batch properties into separate BFS passes."""

    def test_batch_partitions_independent_subsystems(self):
        """Two independent subsystems are proved separately."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        results = prove(logic, [~x, ~y])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)

    def test_batch_overlapping_properties_share_bfs(self):
        """Properties referencing the same tags are grouped together."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)

        results = prove(logic, [~flag, Or(flag, ~flag)])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Proven)

    def test_batch_lambda_falls_back_to_full_scope(self):
        """Lambda properties use full scope."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        results = prove(logic, [lambda s: not s.get("X"), ~y])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)

    def test_batch_partition_preserves_result_order(self):
        """Results are returned in original property order, not partition order."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        results = prove(logic, [~x, ~y, ~z])
        assert isinstance(results, list)
        assert len(results) == 3
        assert all(isinstance(r, Counterexample) for r in results)

    def test_batch_single_group_degenerates_to_current(self):
        """All-overlapping properties produce one group."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(flag)
            with Rung(flag):
                out(output)

        results = prove(logic, [~flag, ~output])
        assert len(results) == 2
        assert isinstance(results[0], Counterexample)
        assert isinstance(results[1], Counterexample)


class TestReachablePartitioning:
    """Reachable-state partitioning: independent clusters explored separately."""

    def test_independent_ote_outputs_partitioned(self):
        """Two OTE outputs with separate inputs are explored independently."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", False), ("Y", True)}),
                frozenset({("X", True), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_independent_latches_partitioned(self):
        """Two independent latch subsystems produce Cartesian product."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert len(states) == 4
        x_vals = {dict(s)["X"] for s in states}
        y_vals = {dict(s)["Y"] for s in states}
        assert x_vals == {True, False}
        assert y_vals == {True, False}

    def test_coupled_tags_stay_together(self):
        """Tags sharing upstream input are not falsely split."""
        btn = Bool("Btn", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(btn):
                out(x)
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_three_cluster_partition(self):
        """Three independent subsystems each explored separately."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        states = reachable_states(logic, project=["X", "Y", "Z"])
        assert not isinstance(states, Intractable)
        assert len(states) == 8

    def test_explicit_scope_disables_partitioning(self):
        """Explicit scope= bypasses automatic partitioning."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        states = reachable_states(logic, scope=["X", "Y"], project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert len(states) == 4

    def test_simplified_form_refines_partition(self):
        """Pivot resolution via simplified forms enables finer splitting.

        X and Y share a pivot tag P in the PDG, but P is OTE-resolvable
        and resolves to different inputs — so simplified forms show they
        are truly independent.
        """
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        p = Bool("P")
        q = Bool("Q")
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(p)
            with Rung(b):
                out(q)
            with Rung(p):
                out(x)
            with Rung(q):
                out(y)

        states = reachable_states(logic, project=["X", "Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("X", False), ("Y", False)}),
                frozenset({("X", False), ("Y", True)}),
                frozenset({("X", True), ("Y", False)}),
                frozenset({("X", True), ("Y", True)}),
            }
        )

    def test_lock_roundtrip_with_partitioned_states(self, tmp_path: Path):
        """Lock write/check works with partitioned reachable-state computation."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        proj = ["X", "Y"]
        states = reachable_states(logic, project=proj)
        assert not isinstance(states, Intractable)

        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, proj, program_hash(logic))

        diff = check_lock(logic, lock_path)
        assert diff is None


class TestReachableStateSlicing:
    """Whole-rung sliced kernels preserve reachable-state behavior."""

    def test_explicit_scope_slice_seeds_scope_and_projection(self):
        """Explicit scope= keeps projected writers even when they sit outside scope."""
        a = Bool("A", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(x):
                out(y)

        states = reachable_states(logic, scope=["X"], project=["Y"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Y", False)}),
                frozenset({("Y", True)}),
            }
        )

    def test_reachable_states_tracks_continued_snapshot_across_scans(self):
        """continued() readers make their source tag part of the reachable state."""
        a = Bool("A", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(x).continued():
                out(y)

        states = reachable_states(logic, project=["Y"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Y", False)}),
                frozenset({("Y", True)}),
            }
        )

    def test_reachable_states_subroutine_slice_keeps_callers(self):
        """Selecting a subroutine writer also keeps the caller path that invokes it."""
        from pyrung.core import call, subroutine

        go = Bool("Go", external=True)
        step = Int("Step", external=True, choices={0: "Idle", 1: "Run"})
        active = Bool("Active")

        @subroutine("Worker", strict=False)
        def worker():
            with Rung(step == 1):
                out(active)

        with Program(strict=False) as logic:
            with Rung(go):
                call(worker)

        states = reachable_states(logic, project=["Active"])
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Active", False)}),
                frozenset({("Active", True)}),
            }
        )

    def test_reachable_states_fault_projection_keeps_implicit_fault_writers(self):
        """Implicit fault-tag writers stay in the slice when a projection depends on them."""
        from pyrung.core import system

        enable = Bool("Enable", external=True)
        divisor = Int("Divisor", external=True, min=0, max=1)
        result = Int("Result")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(100 / divisor, result)
            with Rung(system.fault.division_error):
                out(alarm)

        states = reachable_states(logic, project=["Alarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert states == frozenset(
            {
                frozenset({("Alarm", False)}),
                frozenset({("Alarm", True)}),
            }
        )

    def test_reachable_context_keeps_init_backed_indirect_lookup_writers(self):
        """Reachable-state contexts retain init-backed lookup writers for elision."""
        init_done = Bool("InitDone")
        selector = Int("Selector", external=True, choices={1: "A", 2: "B"})
        idx = Int("Idx")
        tmp = Int("Tmp")
        outv = Int("OutV")
        table = Block("Table", TagType.INT, 1, 2)

        with Program(strict=False) as logic:
            with Rung(~init_done):
                copy(10, table[1])
                copy(20, table[2])
                copy(1, init_done)
            with Rung():
                calc(selector, idx)
            with Rung():
                copy(table[idx], tmp)
            with Rung():
                copy(tmp, outv)

        context = prove_module._build_reachable_context(
            logic,
            scope=["OutV"],
            project=("OutV",),
        )
        assert not isinstance(context, Intractable)
        assert "Idx" not in context.stateful_dims
        assert "Tmp" not in context.stateful_dims


class TestConsumedAccumulator:
    """Item 15: accumulator consumed in a condition stays as separate dimension."""

    def test_consumed_acc_kept_as_dimension(self):
        """Timer accumulator used as data flow is not threshold-abstracted."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        saved = Int("SavedAcc")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung():
                copy(t.Acc, saved)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "T1_Acc" in result.tags


class TestAnnotatedFunctionOutput:
    """Item 16: annotated function outputs succeed verification."""

    def test_choices_annotated_function_is_verifiable(self):
        """run_function output with choices is not Intractable."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result", choices={0: "off", 1: "on"})

        def compute() -> dict:
            return {"result": 1}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

    def test_min_max_annotated_function_is_verifiable(self):
        """run_function output with min/max is not Intractable."""
        trigger = Bool("Trigger", external=True)
        level = Int("Level", min=0, max=3)

        def read_level() -> dict:
            return {"level": 2}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(read_level, outs={"level": level})
            with Rung(level == 2):
                out(Bool("High"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)


class TestCheckLockChange:
    """Item 17: check_lock detects an actual behavioral change."""

    def test_check_lock_detects_behavioral_change(self, tmp_path):
        """Lock check returns StateDiff when program changes."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running", public=True)
        light = Bool("Light", public=True)

        with Program(strict=False) as original:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung(running):
                out(light)

        states = reachable_states(original, project=["Running", "Light"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Running", "Light"], program_hash(original))

        with Program(strict=False) as modified:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung():
                out(light)

        d = check_lock(modified, lock_path)
        assert d is not None
        assert d.added or d.removed


class TestTimerFastForward:
    """Item 18: timer with large preset exercises fast-forward budget."""

    def test_large_preset_reaches_done(self):
        """Timer with preset=500 (50K steps at dt=0.010) still reaches Done=True."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("BigT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=500)
            with Rung(t.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "BigT_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("BigT_Done") for s in states}
        assert True in done_values

    def test_done_preset_extracted(self):
        """_classify_dimensions extracts constant done presets by tag name."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=5000)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _s, _n, _c, _d, done_presets, _done_kinds = result
        assert done_presets["T1_Done"] == 5000

    def test_large_counter_reaches_done(self):
        """Count-up with a large preset still reaches Done=True via event jump."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        counter = Counter.clone("C1")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(counter, preset=500).reset(reset_btn)
            with Rung(counter.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "C1_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("C1_Done") for s in states}
        assert True in done_values

    def test_large_off_delay_reaches_expired(self):
        """Off-delay pending state eventually reaches Done=False via event jump."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        expired = Bool("Expired")

        with Program(strict=False) as logic:
            with Rung(enable):
                off_delay(t, preset=500)
            with Rung(~t.Done):
                out(expired)

        states = reachable_states(logic, project=["Expired", "T1_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("T1_Done") for s in states}
        assert False in done_values
        assert True in done_values


class TestRedundantTimerAccumulatorAbstraction:
    """Dynamic timer presets whose Acc comparisons collapse to Done state."""

    def test_final_preset_redundant_acc_comparison_is_absorbed(self):
        """A ladder-owned dynamic preset can be absorbed with its redundant Acc check."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_literal_write_preset_redundant_acc_comparison_is_absorbed(self):
        """Literal-written presets absorb without readonly/final annotations."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset")
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_external_preset_redundant_acc_comparison_is_absorbed(self):
        """External presets still absorb when their value is threshold-only."""
        enable = Bool("Enable", external=True)
        hmi_preset = Int("HmiPreset", external=True, default=1)
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=hmi_preset)
            with Rung(t.Acc >= hmi_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "HmiPreset" not in stateful
        assert "HmiPreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_zero_default_preset_blocks_absorption(self):
        """External preset with default=0 must not absorb — the comparison
        Acc >= 0 is trivially true at initialization."""
        enable = Bool("Enable", external=True)
        hmi_preset = Int("HmiPreset", external=True, min=0, max=3)
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=hmi_preset)
            with Rung(t.Acc >= hmi_preset):
                out(output)
            with Rung(t.Done):
                out(Bool("DoneOutput"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), result
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DynT_Acc" in stateful, "Acc must not be absorbed with preset default=0"

        result = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(result, Counterexample)

    def test_non_redundant_acc_comparison_is_not_absorbed(self):
        """Strictly greater-than is absorbed by Layer 2 threshold events."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc > active_preset):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd

        states = reachable_states(logic, project=["Output", "DynT_Done"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Output", True), ("DynT_Done", True)}) in states

    def test_preset_data_use_elsewhere_is_bounded_but_not_absorbed(self):
        """A preset copied elsewhere stays explicit instead of being absorbed."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        copied_preset = Int("CopiedPreset")
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
                copy(active_preset, copied_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, done_presets, _done_kinds = result
        assert "ActivePreset" in stateful
        assert "DynT_Acc" in stateful
        assert done_acc == {}
        assert done_presets == {}


class TestThresholdEventAbstraction:
    """Layer 2: progress threshold events for hidden accumulators."""

    def test_timer_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        active_threshold = Int("ActiveThreshold", final=True)
        t = Timer.clone("StepTmr")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["Alarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Alarm", True)}) in states

    def test_timer_threshold_event_allows_direct_zero_acc_reset(self):
        enable = Bool("ResettableTimerEnable", external=True)
        reset_btn = Bool("ResettableTimerReset", external=True)
        active_threshold = Int("ResettableTimerThreshold", final=True)
        t = Timer.clone("ResettableThresholdTmr")
        alarm = Bool("ResettableTimerAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(reset_btn):
                copy(0, t.Acc)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableTimerAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableTimerAlarm", True)}) in states

    def test_timer_acc_zero_copy_with_owner_is_valid_proof_input(self):
        """A data write to Timer.Acc is valid even when one timer owns the UDT."""
        enable = Bool("TimerAccCopyEnable", external=True)
        clear_acc = Bool("TimerAccCopyClear", external=True)
        t = Timer.clone("TimerAccCopyTmr")
        alarm = Bool("TimerAccCopyAlarm")

        with Program(strict=False) as logic:
            with Rung(clear_acc):
                copy(0, t.Acc)
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc >= 10):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=10_000,
            depth_budget=20,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_timer_threshold_event_nonzero_acc_assignment_stays_explicit(self):
        enable = Bool("AssignedTimerEnable", external=True)
        force = Bool("AssignedTimerForce", external=True)
        active_threshold = Int("AssignedTimerThreshold", final=True)
        t = Timer.clone("AssignedThresholdTmr")
        alarm = Bool("AssignedTimerAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(force):
                copy(7, t.Acc)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "AssignedThresholdTmr_Acc" in stateful
        assert "AssignedTimerThreshold" in stateful
        assert done_acc == {}

    def test_multiple_timer_threshold_events_become_tractable(self):
        enable = Bool("Enable", external=True)
        pan = Int("PanThreshold", final=True)
        shaft = Int("ShaftThreshold", final=True)
        drip = Int("DripThreshold", final=True)
        t = Timer.clone("StepTmrMany")
        pan_alarm = Bool("PanAlarm")
        shaft_alarm = Bool("ShaftAlarm")
        drip_alarm = Bool("DripAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(200, pan)
                copy(400, shaft)
                copy(600, drip)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > pan):
                out(pan_alarm)
            with Rung(t.Acc > shaft):
                out(shaft_alarm)
            with Rung(t.Acc > drip):
                out(drip_alarm)

        states = reachable_states(
            logic,
            project=["PanAlarm", "ShaftAlarm", "DripAlarm"],
            depth_budget=10,
        )
        assert not isinstance(states, Intractable)
        assert (
            frozenset(
                {
                    ("PanAlarm", True),
                    ("ShaftAlarm", True),
                    ("DripAlarm", True),
                }
            )
            in states
        )

    def test_count_up_counter_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        active_threshold = Int("CounterThreshold", final=True)
        counter = Counter.clone("StepCounter")
        alarm = Bool("CounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                count_up(counter, preset=1000).reset(reset_btn)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["CounterAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("CounterAlarm", True)}) in states

    def test_count_up_counter_threshold_event_allows_direct_zero_acc_reset(self):
        enable = Bool("ResettableCounterEnable", external=True)
        reset_btn = Bool("ResettableCounterReset", external=True)
        counter_reset = Bool("ResettableCounterOwnerReset", external=True)
        active_threshold = Int("ResettableCounterThreshold", final=True)
        counter = Counter.clone("ResettableThresholdCounter")
        alarm = Bool("ResettableCounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(reset_btn):
                copy(0, counter.Acc)
            with Rung(enable):
                count_up(counter, preset=1000).reset(counter_reset)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableCounterAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableCounterAlarm", True)}) in states

    def test_counter_acc_calc_with_owner_is_valid_proof_input(self):
        """A data write to Counter.Acc is valid even when one counter owns the UDT."""
        boost = Bool("CounterAccCalcBoost", external=True)
        enable = Bool("CounterAccCalcEnable", external=True)
        reset_btn = Bool("CounterAccCalcReset", external=True)
        counter = Counter.clone("CounterAccCalc")
        alarm = Bool("CounterAccCalcAlarm")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter.Acc + 5, counter.Acc)
            with Rung(enable):
                count_up(counter, preset=10).reset(reset_btn)
            with Rung(counter.Acc >= 5):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=10_000,
            depth_budget=20,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_internal_int_step_ticks_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("StepTicks")
        threshold = Int("TickThreshold", final=True)
        alarm = Bool("TickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["TickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("TickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
                copy(500, threshold)
            with Rung(enable):
                calc(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["VariableTickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("VariableTickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_down_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        ticks = Int("DescendingStepTicks", default=1000)
        stride = Int("DescendingStride")
        alarm = Bool("DescendingTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
            with Rung(enable):
                calc(ticks - stride, ticks)
            with Rung(ticks <= 500):
                out(alarm)

        states = reachable_states(logic, project=["DescendingTickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("DescendingTickAlarm", True)}) in states

    def test_nonconstant_stride_tag_int_progress_stays_explicit(self):
        enable = Bool("Enable", external=True)
        sel = Bool("StrideSelect", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, stride)
            with Rung(~sel):
                copy(2, stride)
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                calc(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "VariableStepTicks" in stateful
        assert "Stride" in stateful
        assert "VariableTickThreshold" in stateful

    def test_int_progress_eq_comparison_becomes_tractable(self):
        """calc(ticks + 1, ticks) with Rung(ticks == k) decomposes into ge/gt boundary atoms."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("EqTicks")
        at_five = Bool("AtFive")

        with Program(strict=False) as logic:
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks == 5):
                out(at_five)

        states = reachable_states(logic, project=["AtFive"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("AtFive", True)}) in states
        assert frozenset({("AtFive", False)}) in states

    def test_int_progress_ne_comparison_becomes_tractable(self):
        """calc(ticks + 1, ticks) with Rung(ticks != 0) decomposes into ge/gt boundary atoms."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("NeTicks")
        running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks != 0):
                out(running)

        states = reachable_states(logic, project=["Running"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Running", True)}) in states
        assert frozenset({("Running", False)}) in states

    def test_int_progress_eq_and_gt_mixed_becomes_tractable(self):
        """Progress tag with both == and > comparisons on the same accumulator."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        step = Int("MixedStep")
        threshold = Int("MixedThreshold", final=True)
        at_step = Bool("AtStep3")
        past_threshold = Bool("PastThreshold")

        with Program(strict=False) as logic:
            with Rung():
                copy(10, threshold)
            with Rung(reset_btn):
                copy(0, step)
            with Rung(enable):
                calc(step + 1, step)
            with Rung(step == 3):
                out(at_step)
            with Rung(step > threshold):
                out(past_threshold)

        states = reachable_states(logic, project=["AtStep3", "PastThreshold"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("AtStep3", True), ("PastThreshold", False)}) in states
        assert frozenset({("AtStep3", False), ("PastThreshold", True)}) in states
        assert frozenset({("AtStep3", True), ("PastThreshold", True)}) not in states

    def test_raw_external_threshold_is_absorbed_when_threshold_only(self):
        enable = Bool("Enable", external=True)
        hmi_threshold = Int("HmiThreshold", external=True, default=1000)
        t = Timer.clone("ExternalThresholdTmr")
        alarm = Bool("ExternalThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > hmi_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ExternalThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ExternalThresholdAlarm", False)}) in states
        assert frozenset({("ExternalThresholdAlarm", True)}) in states

    def test_unwritten_internal_threshold_is_absorbed_as_constant(self):
        enable = Bool("Enable", external=True)
        threshold = Int("ImplicitThreshold")
        t = Timer.clone("ImplicitThresholdTmr")
        alarm = Bool("ImplicitThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(alarm)

        states = reachable_states(logic, project=["ImplicitThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ImplicitThresholdAlarm", True)}) in states

    def test_projected_public_threshold_is_discovered(self):
        enable = Bool("Enable", external=True)
        active_threshold = Int("ProjectedThreshold", final=True, public=True)
        t = Timer.clone("ProjectedThresholdTmr")
        alarm = Bool("ProjectedThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(
            logic,
            project=["ProjectedThresholdAlarm", "ProjectedThreshold"],
            depth_budget=5,
        )
        assert not isinstance(states, Intractable)
        threshold_vals = {dict(row)["ProjectedThreshold"] for row in states}
        assert 500 in threshold_vals

    def test_public_threshold_is_absorbed_when_not_projected(self):
        enable = Bool("Enable", external=True)
        public_threshold = Int("PublicThreshold", public=True, default=1000)
        t = Timer.clone("PublicThresholdTmr")
        alarm = Bool("PublicThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > public_threshold):
                out(alarm)

        states = reachable_states(logic, project=["PublicThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("PublicThresholdAlarm", False)}) in states
        assert frozenset({("PublicThresholdAlarm", True)}) in states

    def test_exact_and_abstract_threshold_events_both_branch(self):
        enable = Bool("Enable", external=True)
        hmi_threshold = Int("HmiThreshold", external=True, default=1000)
        t = Timer.clone("MixedThresholdTmr")
        exact_alarm = Bool("ExactAlarm")
        hmi_alarm = Bool("HmiAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > 500):
                out(exact_alarm)
            with Rung(t.Acc > hmi_threshold):
                out(hmi_alarm)

        states = reachable_states(logic, project=["ExactAlarm", "HmiAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ExactAlarm", True), ("HmiAlarm", False)}) in states
        assert frozenset({("ExactAlarm", False), ("HmiAlarm", True)}) in states

    def test_non_threshold_accumulator_read_stays_explicit(self):
        enable = Bool("Enable", external=True)
        threshold = Int("SavedAccThreshold", final=True)
        t = Timer.clone("SavedAccTmr")
        saved = Int("SavedAccValue")
        alarm = Bool("SavedAccAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung():
                copy(t.Acc, saved)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SavedAccTmr_Acc" in stateful
        assert "SavedAccThreshold" in stateful
        assert done_acc == {}

    def test_reset_recomputes_threshold_vector_false(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("ResetTicks", external=True)
        ticks = Int("ResettableStepTicks")
        threshold = Int("ResettableTickThreshold", final=True)
        alarm = Bool("ResettableTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(3, threshold)
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableTickAlarm"], depth_budget=8)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableTickAlarm", True)}) in states
        assert frozenset({("ResettableTickAlarm", False)}) in states

    def test_threshold_event_before_done_uses_nearest_event(self):
        enable = Bool("Enable", external=True)
        threshold = Int("NearestThreshold", final=True)
        t = Timer.clone("NearestTmr")
        warning = Bool("Warning")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(warning)

        states = reachable_states(logic, project=["Warning", "NearestTmr_Done"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Warning", True), ("NearestTmr_Done", False)}) in states


class TestPendingSettlementChains:
    """Pending settlement should fully resolve chained hidden-event work."""

    def test_prove_settles_chained_exact_timers_before_reporting_failure(self):
        """A false pending plateau should settle through both exact timers first."""
        cmd = Bool("Cmd", external=True)
        fb = Bool("Fb", external=True)
        t1 = Timer.clone("ChainT1")
        t2 = Timer.clone("ChainT2")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(cmd, ~fb):
                on_delay(t1, preset=30)
            with Rung(t1.Done):
                on_delay(t2, preset=30)
            with Rung(t2.Done):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Cmd": True, "Fb": False})
        for _ in range(5):
            plc.step()
        assert plc.current_state.tags.get("Alarm") is True

        result = prove(logic, Or(~cmd, fb, alarm), depth_budget=5)
        assert isinstance(result, Proven)

    @no_agreement
    def test_prove_settles_exact_timer_started_by_abstract_threshold_branch(self):
        """Abstract threshold branches should keep settling exact work they enable."""
        enable = Bool("Enable", external=True)
        hidden_threshold = Int(
            "HiddenThreshold",
            external=True,
            choices={10: "Trip"},
            default=10,
        )
        t1 = Timer.clone("AbstractChainT1")
        t2 = Timer.clone("AbstractChainT2")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t1, preset=30)
            with Rung(t1.Acc > hidden_threshold):
                on_delay(t2, preset=30)
            with Rung(t2.Done):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True})
        for _ in range(4):
            plc.step()
        assert plc.current_state.tags.get("Alarm") is True

        result = prove(logic, Or(~enable, alarm), depth_budget=5)
        assert isinstance(result, Proven)


class TestIntractableTags:
    """Item 3 follow-up: Intractable.tags field is populated."""

    def test_unbounded_domain_tags(self):
        """Intractable from unbounded domain carries tag names."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Result" in result.tags


class TestIntractableHints:
    """Intractable results carry actionable hints for the user."""

    def test_pointer_tag_hint(self):
        """Pointer into a block > 1000 elements gets a hint naming the block."""
        from pyrung.core import Block, TagType, copy

        blk = Block("Regs", TagType.INT, 1, 1500)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("pointer" in h and "Regs" in h for h in result.hints)
        assert any("Idx" in h for h in result.hints)

    def test_wide_range_hint(self):
        """Tag with min/max range > 1000 gets a 'too wide' hint."""
        level = Int("Level", external=True, min=0, max=5000)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level):
                out(alarm)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("too wide" in h and "Level" in h for h in result.hints)

    def test_no_constraint_hint(self):
        """Tag with no choices/min/max gets a generic hint."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("no domain constraint" in h for h in result.hints)

    def test_function_output_hint(self):
        """Unannotated function output gets a hint."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result")

        def compute() -> dict:
            return {"result": 42}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("function output" in h and "Result" in h for h in result.hints)

    def test_max_states_dimension_breakdown(self):
        """max_states exceeded carries dimension breakdown hints."""
        inputs = [Bool(f"In{i}", external=True) for i in range(20)]
        flags = [Bool(f"Flag{i}") for i in range(20)]
        output = Bool("Output")

        with Program(strict=False) as logic:
            for inp, flag in zip(inputs, flags, strict=True):
                with Rung(rise(inp)):
                    latch(flag)
            with Rung(*flags):
                out(output)

        result = prove(logic, lambda s: True, max_states=10)
        assert isinstance(result, Intractable)
        assert result.hints
        assert any("state space:" in h for h in result.hints)
        assert any("Constrain" in h for h in result.hints)

    def test_hints_suggest_choices_or_testing(self):
        """Unbounded hints suggest choices= or dt= testing."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert all("choices=" in h or "dt= testing" in h for h in result.hints)


class TestThresholdBlockerHints:
    """Intractable hints explain why threshold abstraction was blocked."""

    def test_literal_thresholds_are_structurally_bounded(self):
        """Literal-written thresholds no longer need readonly/final hints."""
        enable = Bool("Enable", external=True)
        pan_ts = Int("PanWatchdog_Ts")
        shaft_ts = Int("ShaftWatchdog_Ts")
        t = Timer.clone("CurStep_tmr")
        pan_alarm = Bool("PanAlarm")
        shaft_alarm = Bool("ShaftAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, pan_ts)
                copy(600, shaft_ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > pan_ts):
                out(pan_alarm)
            with Rung(t.Acc > shaft_ts):
                out(shaft_alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "CurStep_tmr_Acc" not in stateful
        assert "PanWatchdog_Ts" not in stateful
        assert "ShaftWatchdog_Ts" not in stateful

    def test_literal_threshold_no_longer_suggests_readonly(self):
        """Literal-written thresholds are bounded without readonly hints."""
        enable = Bool("Enable", external=True)
        ts = Int("WatchdogTs")
        t = Timer.clone("WdTmr")
        alarm = Bool("WdAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "WdTmr_Acc" not in stateful
        assert "WatchdogTs" not in stateful

    def test_shared_threshold_blocks_absorption(self):
        """One threshold shared across progress sources stays explicit."""
        enable_a = Bool("EnableA", external=True)
        enable_b = Bool("EnableB", external=True)
        shared_ts = Int("SharedWatchdogTs")
        t_a = Timer.clone("SharedA")
        t_b = Timer.clone("SharedB")
        alarm_a = Bool("SharedAlarmA")
        alarm_b = Bool("SharedAlarmB")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, shared_ts)
            with Rung(enable_a):
                on_delay(t_a, preset=1000)
            with Rung(enable_b):
                on_delay(t_b, preset=1000)
            with Rung(t_a.Acc > shared_ts):
                out(alarm_a)
            with Rung(t_b.Acc > shared_ts):
                out(alarm_b)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SharedWatchdogTs" in stateful
        assert "SharedA_Acc" in stateful
        assert "SharedB_Acc" in stateful
        assert done_acc == {}

    def test_threshold_tag_non_threshold_comparison_blocks_absorption(self):
        """Threshold tags used in other comparisons stay explicit."""
        enable = Bool("Enable", external=True)
        ts = Int("ComparedThreshold")
        t = Timer.clone("ComparedTmr")
        alarm = Bool("ComparedAlarm")
        mode = Bool("ThresholdMode")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, ts)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)
            with Rung(ts == 500):
                out(mode)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "ComparedThreshold" in stateful
        assert "ComparedTmr_Acc" in stateful
        assert done_acc == {}

    def test_data_read_blocker_falls_back_to_structural_bounding(self):
        """Data-flow reads can block absorption without forcing Intractable."""
        enable = Bool("Enable", external=True)
        threshold = Int("DataReadThreshold", final=True)
        t = Timer.clone("DataReadTmr")
        saved = Int("SavedValue")
        alarm = Bool("DataReadAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung():
                copy(t.Acc, saved)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "DataReadTmr_Acc" in stateful
        assert "DataReadThreshold" in stateful
        assert done_acc == {}

    def test_stable_threshold_no_blocker(self):
        """Stable thresholds produce no blocker — absorption succeeds."""
        enable = Bool("Enable", external=True)
        ts = Int("StableTs", readonly=True)
        t = Timer.clone("StableTmr")
        alarm = Bool("StableAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > ts):
                out(alarm)

        result = reachable_states(logic, project=["StableAlarm"], depth_budget=5)
        assert not isinstance(result, Intractable)


class TestKernelDomainDiscovery:
    """Pilot sweep discovers finite domains for program-derived tags."""

    def test_copy_choices_inherits_domain(self):
        """copy(Step, StoredStep) where Step has choices= becomes tractable."""
        Step = Int("Step", external=True, choices={0: "Idle", 1: "Fill", 2: "Dump"})
        StoredStep = Int("StoredStep")
        DumpMode = Bool("DumpMode")

        with Program(strict=False) as logic:
            with Rung():
                copy(Step, StoredStep)
            with Rung(StoredStep == 2):
                out(DumpMode)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_calc_identity_inherits_domain(self):
        """calc(Step, StoredStep) identity-style assignment becomes tractable."""
        Step = Int("Step", external=True, choices={0: "Off", 1: "On"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung():
                calc(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_large_comparison_only_calc_tag_is_absorbed(self):
        """Large written comparison-only tags use vector keying instead of exact state."""
        source = Int("Source", external=True, min=0, max=300)
        stored = Int("Stored")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(source, stored)
            with Rung(stored > 150):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "Stored" not in stateful

        states = reachable_states(logic, project=["Alarm"], depth_budget=2)
        assert not isinstance(states, Intractable)
        assert frozenset({("Alarm", False)}) in states
        assert frozenset({("Alarm", True)}) in states

    def test_timer_preset_source_excluded_from_comparison_only(self):
        """Tags used as timer preset sources are excluded from comparison-only absorption."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        enable = Bool("Enable", external=True)
        h1 = Int("H1", min=0, max=100)
        h2 = Int("H2", min=0, max=100)
        t1 = Timer.clone("T1")
        t2 = Timer.clone("T2")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(enable):
                copy(50, h1)
                copy(75, h2)
            with Rung(enable):
                on_delay(t1, preset=h1)
                on_delay(t2, preset=h2)
            with Rung(h1 > 30, h2 > 60):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "H1" not in comparison.comparison_tags
        assert "H2" not in comparison.comparison_tags

    def test_self_feeding_calc_is_excluded_from_comparison_only(self):
        """calc(X + N, X) reads X via _reads — _has_forbidden_data_read excludes it."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("SelfFeedBoost", external=True)
        counter = Int("SelfFeedCounter", min=0, max=100)
        flag = Bool("SelfFeedFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 2, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "SelfFeedCounter" not in comparison.comparison_tags

    def test_mixed_self_feed_and_overwrite_writers_are_excluded(self):
        """A self-feeding calc next to an overwrite copy still reads the tag — excluded."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("MixBoost", external=True)
        reset_btn = Bool("MixReset", external=True)
        counter = Int("MixCounter", min=0, max=100)
        flag = Bool("MixFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 3, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(reset_btn):
                copy(0, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "MixCounter" not in comparison.comparison_tags

    def test_overwrite_only_tag_source_still_absorbs(self):
        """copy(Source, Stored) is overwrite-only — comparison-only absorption still applies."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        source = Int("OverwriteSource", external=True, min=0, max=300)
        stored = Int("OverwriteStored")
        flag = Bool("OverwriteFlag")

        with Program(strict=False) as logic:
            with Rung():
                copy(source, stored)
            with Rung(stored > 150):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "OverwriteStored" in comparison.comparison_tags

    def test_self_feeding_calc_prove_matches_unoptimized(self):
        """End-to-end soundness oracle: variable-stride self-feed prove() agrees with unoptimized.

        Locks in that the existing data-flow read check keeps comparison-only
        absorption from collapsing a self-feeding accumulator into a vector.
        """
        boost = Bool("SelfFeedProveBoost", external=True)
        counter = Int("SelfFeedProveCounter", min=0, max=30)
        alarm = Bool("SelfFeedProveAlarm")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter + 2, counter)
            with Rung(~boost):
                calc(counter + 1, counter)
            with Rung(counter > 20):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=100_000, depth_budget=60)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=100_000,
            depth_budget=60,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_real_operand_side_boundary_uses_comparison_partner(self):
        """Projected REAL tags resolve operand-side comparison boundaries from the partner tag."""
        from pyrung.core import Real
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.classify import (
            _classify_dimensions_from_graph,
            _collect_all_exprs,
        )

        source = Int("Source", external=True, min=0, max=300)
        stored = Real("Stored")
        limit = Real("Limit", readonly=True, default=15.0)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(source / 10, stored)
            with Rung(limit > stored):
                out(alarm)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        result = _classify_dimensions_from_graph(
            logic,
            graph,
            all_exprs,
            project=("Stored",),
        )
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful["Stored"] == (0.0, 14.0, 15.0, 16.0)

    def test_literal_writes_discover_domain(self):
        """Literal writes discover {default, 5, 10}."""
        trigger_a = Bool("TrigA", external=True)
        trigger_b = Bool("TrigB", external=True)
        dest = Int("Dest")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger_a):
                copy(5, dest)
            with Rung(trigger_b):
                copy(10, dest)
            with Rung(dest == 5):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_fill_literal_writes_discover_domain(self):
        """fill(constant, block.select(...)) counts as literal writes per target."""
        enable = Bool("Enable", external=True)
        ds = Block("DS", TagType.INT, 1, 3)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(enable):
                fill(7, ds.select(1, 3))
            with Rung(ds[1] == 7):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_direct_self_feed_threshold_only_progress_becomes_tractable(self):
        """Monotone self-feed is tractable when the only consumer is a threshold."""
        from pyrung.core.analysis.pdg import build_program_graph

        trigger = Bool("Trigger", external=True)
        count = Int("Count")
        threshold = Int("Threshold", external=True, default=1000)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                calc(count + 1, count)
            with Rung(count > threshold):
                out(flag)

        graph = build_program_graph(logic)
        assert _has_data_feedback("Count", graph)

        states = reachable_states(logic, project=["Flag"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Flag", False)}) in states
        assert frozenset({("Flag", True)}) in states

    def test_transitive_feedback_remains_intractable(self):
        """Transitive feedback A→B→A remains intractable."""
        from pyrung.core.analysis.pdg import build_program_graph

        trigger = Bool("Trigger", external=True)
        a = Int("A")
        b = Int("B")
        threshold = Int("Threshold", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                calc(a + 1, b)
            with Rung(trigger):
                copy(b, a)
            with Rung(a > threshold):
                out(flag)

        graph = build_program_graph(logic)
        assert _has_data_feedback("A", graph)
        assert _has_data_feedback("B", graph)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Intractable)

    def test_condition_only_not_data_feedback(self):
        """Condition-only reference is not treated as data feedback."""
        from pyrung.core.analysis.pdg import build_program_graph

        Step = Int("Step", external=True, choices={0: "Idle", 1: "Run"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung(StoredStep > 0):
                copy(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        graph = build_program_graph(logic)
        assert not _has_data_feedback("StoredStep", graph)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_raw_external_no_writer_remains_intractable(self):
        """Raw external tag with no writer remains intractable."""
        ext = Int("ExtVal", external=True)
        other = Int("OtherVal", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(ext > other):
                latch(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Intractable)

    def test_external_final_with_writer_discoverable(self):
        """external=True, final=True with an in-ladder writer is discovered."""
        trigger = Bool("Trigger", external=True)
        ext_final = Int("ExtFinal", external=True, final=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(trigger):
                copy(42, ext_final)
            with Rung(ext_final == 42):
                out(flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_huge_input_product_skips_discovery(self):
        """Huge relevant input product over max_combos skips discovery."""
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core.analysis.pdg import build_program_graph

        inputs = [Int(f"In{i}", external=True, min=0, max=200) for i in range(5)]
        dest = Int("Dest")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            for i, inp in enumerate(inputs):
                with Rung():
                    calc(inp + i, dest)
            with Rung(dest > 0):
                out(flag)

        graph = build_program_graph(logic)
        compiled = compile_kernel(logic)
        nd = {inp.name: tuple(range(201)) for inp in inputs}
        result = _pilot_sweep_domains(
            compiled,
            ["Dest"],
            nd,
            graph,
            max_combos=100_000,
        )
        assert "Dest" not in result

    def test_copy_from_min_max_source_inherits_domain(self):
        """copy(Source, Dest) where Source has min=/max= but no comparison atoms."""
        Source = Int("Source", external=True, min=0, max=5)
        Stored = Int("Stored")
        Other = Int("Other", external=True, default=5)
        Flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                copy(Source, Stored)
            with Rung(Stored > Other):
                out(Flag)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_copy_with_reset_from_bounded_source(self):
        """copy(CurStep, StoredStep) plus copy(0, StoredStep) reset path."""
        CurStep = Int("CurStep", external=True, min=0, max=3)
        StoredStep = Int("StoredStep")
        ResetBtn = Bool("ResetBtn", external=True)
        Active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung():
                copy(CurStep, StoredStep)
            with Rung(ResetBtn):
                copy(0, StoredStep)
            with Rung(StoredStep == 2):
                out(Active)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)

    def test_subroutine_gated_tag_discovered_via_multiscan(self):
        """Tag written inside a subroutine behind a call gate needs multi-scan."""
        from pyrung.core import call, subroutine

        xCall = Bool("xCall", external=True)
        running = Bool("Running")
        Step = Int("Step", external=True, choices={0: "Idle", 1: "Run", 2: "Done"})
        StoredStep = Int("StoredStep")
        Active = Bool("Active")

        @subroutine("Worker", strict=False)
        def worker():
            with Rung():
                copy(Step, StoredStep)
            with Rung(StoredStep == 1):
                out(Active)

        with Program(strict=False) as logic:
            with Rung(xCall):
                out(running)
            with Rung(running):
                call(worker)

        result = prove(logic, lambda s: True)
        assert isinstance(result, Proven)


class TestSettlePending:
    """prove() settles pending timers before reporting counterexamples."""

    def test_timer_gated_alarm_proves_with_settle(self):
        """A property guarded by a timer-gated alarm should prove, not produce
        a spurious counterexample from the PENDING state."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("Fault")
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Alarm)

        result = prove(logic, Or(~Cmd, Fb, Alarm))
        assert isinstance(result, Proven), (
            f"Expected Proven but got {type(result).__name__}: "
            f"settle-pending should resolve the timer-gated alarm"
        )

    def test_genuinely_missing_alarm_still_counterexample(self):
        """A feedback fault with no alarm should produce a Counterexample.
        Uses the same timer pattern but proves a property that is NOT
        reachable — Running latches but the property demands ~Running."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("NoAlarm")
        Running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Running)

        result = prove(logic, ~Running)
        assert isinstance(result, Counterexample)

    def test_batch_prove_settles_pending(self):
        """Batch mode also settles pending timers."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("Fault")
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Alarm)

        results = prove(logic, [Or(~Cmd, Fb, Alarm)])
        assert isinstance(results, list)
        assert isinstance(results[0], Proven)


# ===================================================================
# InputBlock / TagMap input inference
# ===================================================================


class TestInputBlockNondeterministic:
    def test_input_block_tags_classified_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert nd["X1"] == (False, True)
        assert "X1" not in sd

    def test_output_block_tags_not_nondeterministic(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        y = OutputBlock("Y", TagType.BOOL, 1, 4)

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(y[1])

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        sd, nd, combinational, _, _, _ = result
        assert "X1" in nd
        assert "Y1" not in nd
        assert "Y1" in combinational

    def test_reachable_states_with_input_block(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert len(states) == 2

    def test_prove_with_input_block(self):
        x = InputBlock("X", TagType.BOOL, 1, 4)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(x[1]):
                out(light)

        result = prove(logic, light)
        assert isinstance(result, Counterexample)

    def test_tagmap_stamps_external_on_input_mapped_tags(self):
        from pyrung.click import TagMap, x, y

        button = Bool("Button")
        motor = Bool("Motor")
        assert not button.external
        assert not motor.external

        TagMap({button: x[1], motor: y[1]}, include_system=False)

        assert button.external
        assert not motor.external

    def test_tagmap_stamped_tag_becomes_nondeterministic(self):
        from pyrung.click import TagMap, x

        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        TagMap({button: x[1]}, include_system=False)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _, nd, _, _, _, _ = result
        assert "Button" in nd

    def test_tagmap_stamped_reachable_states(self):
        from pyrung.click import TagMap, x

        button = Bool("Button")
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                out(light)

        TagMap({button: x[1]}, include_system=False)

        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert len(states) == 2


class TestPointerDomainInference:
    """Pointer tags into blocks get auto-bounded from block address range."""

    def test_external_pointer_auto_bounded(self):
        """External pointer with no annotation gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(0, 11))

    def test_explicit_choices_not_overridden(self):
        """Pointer with explicit choices= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True, choices={1: "first", 5: "fifth"})
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == (1, 5)

    def test_explicit_min_max_not_overridden(self):
        """Pointer with explicit min=/max= keeps its annotated domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx", external=True, min=1, max=5)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nondeterministic, *_ = result
        assert "Idx" in nondeterministic
        assert nondeterministic["Idx"] == tuple(range(1, 6))

    def test_literal_copy_domain_not_overridden(self):
        """Pointer already inferred by literal copies keeps that tighter domain."""
        blk = Block("DS", TagType.INT, 1, 50)
        idx = Int("Idx")
        dest = Int("Out")
        sel = Bool("Sel", external=True)

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, idx)
            with Rung(~sel):
                copy(3, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nondeterministic, *_ = result
        assert "Idx" in stateful
        assert set(stateful["Idx"]) == {0, 1, 3}

    def test_wide_block_still_intractable(self):
        """Block with > 1000 addresses leaves the pointer intractable."""
        blk = Block("Big", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags

    def test_internal_pointer_auto_bounded(self):
        """Internal pointer written by calc gets domain from block bounds."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx")
        dest = Int("Out")
        step = Bool("Step", external=True)

        with Program(strict=False) as logic:
            with Rung(step):
                calc(idx + 1, idx)
            with Rung():
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, *_ = result
        assert "Idx" in stateful
        assert stateful["Idx"] == tuple(range(0, 11))

    def test_unconditioned_pointer_not_silently_dropped(self):
        """Pointer used only in instructions (no conditions) must still
        surface as intractable — not be silently dropped because it has
        no comparison atoms."""
        blk = Block("DS", TagType.INT, 1, 2000)
        idx = Int("Idx", external=True)
        dest = Int("Out")
        flag = Bool("Flag", external=True)

        with Program(strict=False) as logic:
            with Rung(flag):
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert "Idx" in result.tags
        assert any("pointer" in h and "Idx" in h for h in result.hints)


class TestBlocklessProveKernel:
    """prove defaults to blockless compiled kernels."""

    def test_build_explore_context_uses_blockless_kernel(self):
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=5)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        context = prove_module._build_explore_context(logic)

        assert not isinstance(context, Intractable)
        assert context.compiled.blockless is True

    def test_public_compile_sites_use_blockless_kernel(self, monkeypatch):
        from pyrung.circuitpy import codegen as codegen_module

        cmd = Bool("Cmd", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(cmd):
                out(light)

        real_compile = codegen_module.compile_kernel
        calls: list[dict[str, object]] = []

        def _record(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_compile(*args, **kwargs)

        monkeypatch.setattr(codegen_module, "compile_kernel", _record)

        assert isinstance(prove(logic, lambda _state: True), Proven)
        states = reachable_states(logic, project=["Light"])
        assert not isinstance(states, Intractable)
        assert isinstance(program_hash(logic), str)
        assert calls
        assert all(call.get("blockless") is True for call in calls)

    def test_reachable_states_still_work_for_full_pointer_domain(self):
        """Full pointer domains still prove and enumerate correctly."""
        blk = Block("DS", TagType.INT, 1, 10)
        idx = Int("Idx", external=True, min=1, max=10)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        states = reachable_states(logic, project=["Out"])
        assert not isinstance(states, Intractable)

    def test_prove_correct_with_narrowed_block(self):
        """Prove still produces correct results with narrowed indirect blocks."""
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=3)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)

        result = prove(logic, dest >= 0)
        assert isinstance(result, Proven)

    def test_mixed_access_prove_correct(self):
        """Prove works with mixed static + indirect access on same block."""
        blk = Block("DS", TagType.INT, 1, 100)
        idx = Int("Idx", external=True, min=1, max=3)
        dest = Int("Out")
        dest2 = Int("Out2")

        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)
            with Rung():
                copy(blk[50], dest2)

        result = prove(logic, dest >= 0)
        assert isinstance(result, Proven)


# ===================================================================
# Group: Free input elision
# ===================================================================


class TestFreeInputElision:
    """Verify the free-input state key elision optimization."""

    def test_free_input_reduces_states(self):
        """Free input (xic only) is not in nondeterministic_names; state count halved."""
        free = Bool("Free", external=True)
        edge = Bool("Edge", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(free):
                latch(x)
            with Rung(rise(edge)):
                reset(x)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Free" in context.free_input_names
        assert "Free" not in context.nondeterministic_names
        assert "Edge" not in context.free_input_names
        assert "Edge" in context.nondeterministic_names

    def test_shift_clock_is_edge_bearing(self):
        """ShiftInstruction clock ND input stays in nondeterministic_names."""
        from pyrung.core import Block, TagType, shift

        clk = Bool("Clk", external=True)
        data = Bool("Data", external=True)
        rst = Bool("Rst", external=True)
        bits = Block("SR", TagType.BOOL, 1, 4)

        with Program(strict=False) as logic:
            with Rung(data):
                shift(bits.select(1, 4)).clock(clk).reset(rst)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Clk" not in context.free_input_names
        assert "Clk" in context.nondeterministic_names

    def test_drum_jog_is_edge_bearing(self):
        """Drum jog ND input stays in nondeterministic_names."""
        from pyrung.core import event_drum

        enable = Bool("Enable", external=True)
        jog = Bool("Jog", external=True)
        reset_sig = Bool("Rst", external=True)
        e1 = Bool("E1", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1 = Bool("Y1")

        with Program(strict=False) as logic:
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset_sig).jog(jog)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "Jog" not in context.free_input_names
        assert "Jog" in context.nondeterministic_names

    def test_drum_event_is_edge_bearing(self):
        """EventDrum per-step event ND input stays in nondeterministic_names."""
        from pyrung.core import event_drum

        enable = Bool("Enable", external=True)
        reset_sig = Bool("Rst", external=True)
        e1 = Bool("E1", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1 = Bool("Y1")

        with Program(strict=False) as logic:
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset_sig)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "E1" not in context.free_input_names
        assert "E1" in context.nondeterministic_names

    def test_projected_free_input_kept(self):
        """Free input in project stays in nondeterministic_names."""
        a = Bool("A", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)

        context = prove_module._build_explore_context(logic, project=("A",))
        assert not isinstance(context, Intractable)
        assert "A" in context.nondeterministic_names

    def test_all_edge_bearing_no_reduction(self):
        """All ND inputs use rise() — no free inputs, names unchanged."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert context.free_input_names == frozenset()
        assert "A" in context.nondeterministic_names
        assert "B" in context.nondeterministic_names

    def test_soundness_preserved(self):
        """Latch controlled by free input: prove() result is sound."""
        free = Bool("Free", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(free):
                latch(x)

        result = prove(logic, ~x)
        assert isinstance(result, Counterexample)

        result2 = prove(logic, Or(x, ~x))
        assert isinstance(result2, Proven)


# ===================================================================
# Absorption gap regression tests
# ===================================================================


class TestThresholdProgressAbsorptionGaps:
    def test_threshold_only_progress_keeps_immediate_counterexample(self):
        """Threshold jumps must not hide the concrete post-scan state."""
        in0 = Bool("In0", external=True)
        d0 = Dint("D0")

        with Program(strict=False) as logic:
            with Rung(in0):
                calc(d0 + 1, d0)
            with Rung(in0):
                copy(d0, d0)
            with Rung(in0):
                copy(d0, d0)

        result = prove(logic, d0 < 1, max_states=10_000, depth_budget=20)

        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("In0") is True for step in result.trace)


class TestCountDownAbsorptionGaps:
    """CountDown counters are excluded from all absorption paths.

    Verify that prove() still reaches Done=True states and that
    intermediate-Acc-dependent outputs are reachable.
    """

    def test_count_down_done_is_reachable(self):
        """Basic count_down: prove() should find the Done=True state."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDReset", external=True)
        counter = Counter.clone("CTD")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(rst)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), "count_down Done=True should be reachable"

    def test_count_down_consumed_acc_done_still_reachable(self):
        """count_down with Acc comparison — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDAccReset", external=True)
        counter = Counter.clone("CTDAcc")
        warning = Bool("Warning")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(rst)
            with Rung(counter.Acc <= -3):
                out(warning)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), (
            "count_down with consumed Acc: Done=True should be reachable"
        )

    def test_count_down_intermediate_state_reachable(self):
        """count_down intermediate output (Acc-dependent) must be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDMidReset", external=True)
        counter = Counter.clone("CTDMid")
        midway = Bool("Midway")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=10).reset(rst)
            with Rung(counter.Acc < -3):
                out(midway)

        states = reachable_states(logic, project=["Midway"])
        assert not isinstance(states, Intractable), (
            "count_down with Acc comparison should be tractable"
        )
        assert frozenset({("Midway", True)}) in states, "Midway should be reachable when Acc < -3"


class TestBidirectionalCounterGaps:
    """CountUp with down_condition is excluded from progress source detection."""

    def test_bidirectional_counter_done_is_reachable(self):
        """count_up with .down() — Done should still be reachable."""
        up_btn = Bool("UpBtn", external=True)
        down_btn = Bool("DownBtn", external=True)
        rst = Bool("BiDirReset", external=True)
        counter = Counter.clone("BiDir")
        alarm = Bool("BiDirAlarm")

        with Program(strict=False) as logic:
            with Rung(up_btn):
                count_up(counter, preset=5).down(down_btn).reset(rst)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), (
            "bidirectional counter Done=True should be reachable"
        )

    def test_bidirectional_counter_threshold_reachable(self):
        """count_up with .down() and threshold comparison."""
        up_btn = Bool("UpBtn", external=True)
        down_btn = Bool("DownBtn", external=True)
        rst = Bool("BiDirThrReset", external=True)
        counter = Counter.clone("BiDirThr")
        threshold = Int("BiDirThreshold", final=True)
        halfway = Bool("BiDirHalfway")

        with Program(strict=False) as logic:
            with Rung():
                copy(3, threshold)
            with Rung(up_btn):
                count_up(counter, preset=5).down(down_btn).reset(rst)
            with Rung(counter.Acc >= threshold):
                out(halfway)

        states = reachable_states(logic, project=["BiDirHalfway"])
        assert not isinstance(states, Intractable), (
            "bidirectional counter with threshold should be tractable"
        )
        assert frozenset({("BiDirHalfway", True)}) in states


class TestTruthyAccAbsorptionGaps:
    """Timer/Counter Acc used as a boolean condition (truthy/xic form).

    _threshold_atom_for_progress and _atom_matches_acc_preset_boundary
    don't handle truthy atoms, blocking both absorption paths.  The Acc
    gets an empty domain and drops out of the state key entirely.
    """

    def test_timer_acc_truthy_done_still_reachable(self):
        """Timer Acc as truthy condition — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("TruthyTmr")
        timing = Bool("Timing")
        complete = Bool("Complete")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc):
                out(timing)
            with Rung(t.Done):
                out(complete)

        result = prove(logic, ~complete)
        assert isinstance(result, Counterexample), (
            "timer with truthy Acc: Done=True should be reachable"
        )

    def test_counter_acc_nonzero_done_still_reachable(self):
        """Counter Acc > 0 condition — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("TruthyCtrReset", external=True)
        c = Counter.clone("TruthyCtr")
        counting = Bool("Counting")
        complete = Bool("CountComplete")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc > 0):
                out(counting)
            with Rung(c.Done):
                out(complete)

        result = prove(logic, ~complete)
        assert isinstance(result, Counterexample), (
            "counter with Acc > 0: Done=True should be reachable"
        )

    def test_timer_acc_truthy_timing_state_reachable(self):
        """Truthy Acc guard: the 'timing active' output must be reachable."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("TruthyTmr2")
        active = Bool("TimerActive")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc):
                latch(active)

        result = prove(logic, ~active)
        assert isinstance(result, Counterexample), (
            "timer Acc truthy: 'active' should be reachable via latched output"
        )


class TestCounterResetReachability:
    """Verify that reset-during-counting states are reachable."""

    def test_count_up_reset_during_count_is_reachable(self):
        """Reset mid-count: prove should find the state where reset fires while counting."""
        enable = Bool("Enable", external=True)
        rst = Bool("ResetBtn", external=True)
        c = Counter.clone("RstCtr")
        counting = Bool("Counting")
        was_reset = Bool("WasReset")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc > 0):
                latch(counting)
            with Rung(counting, c.Acc == 0):
                latch(was_reset)

        result = prove(logic, ~was_reset)
        assert isinstance(result, Counterexample), (
            "reset-during-count should be reachable: counting=True then Acc=0 after reset"
        )


class TestConstantPresetCounterAbsorption:
    """Verify the e6a0d4f fix also applies to count_up counters."""

    def test_count_up_constant_preset_acc_lt_absorbed(self):
        """count_up with constant preset and Acc < Preset should absorb."""
        enable = Bool("Enable", external=True)
        rst = Bool("ConstCtrReset", external=True)
        c = Counter.clone("ConstCtr")
        almost = Bool("AlmostDone")
        done_out = Bool("DoneOut")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=1000).reset(rst)
            with Rung(c.Acc < 1000):
                out(almost)
            with Rung(c.Done):
                out(done_out)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful.get("ConstCtr_Done") == (False, PENDING, True), (
            "count_up with constant preset and Acc < Preset: Done should get 3-valued domain"
        )
        assert "ConstCtr_Acc" not in stateful, (
            "count_up Acc should be absorbed (redundant with Done boundary)"
        )

    def test_count_up_constant_preset_acc_le_not_same_boundary(self):
        """Acc <= Preset is NOT the same boundary as Acc < Preset; threshold absorption handles it."""
        enable = Bool("Enable", external=True)
        rst = Bool("LeCtrReset", external=True)
        c = Counter.clone("LeCtr")
        output = Bool("LeOutput")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=1000).reset(rst)
            with Rung(c.Acc <= 1000):
                out(output)

        states = reachable_states(logic, project=["LeOutput", "LeCtr_Done"])
        assert not isinstance(states, Intractable)
        assert any(("LeCtr_Done", True) in s for s in states), (
            "Done=True should be reachable even with Acc <= Preset comparison"
        )


# ===================================================================
# Simultaneous edge coverage tests
# ===================================================================


class TestSimultaneousEdgeCoverage:
    """Auto-jointed dual edges and explicit joint inputs cover simultaneous patterns."""

    def test_dual_rise_auto_jointed_without_user_declaration(self):
        """rise(A) AND rise(B) should be handled automatically as a joint input."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(target)

        # Concrete PLC reaches Target=True.
        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True, "B": True})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_rise_fall_pair_auto_jointed_without_user_declaration(self):
        """rise(A) AND fall(B) should be handled automatically as a joint input."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), fall(b)):
                latch(target)

        # Concrete PLC reaches Target=True.
        plc = PLC(logic, dt=0.010)
        plc.patch({"B": True})
        plc.step()
        plc.patch({"A": True, "B": False})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_triple_rise_partial_joint_plus_single_flip_composes(self):
        """Explicit A+B joint input should compose with an independent C flip."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b), rise(c)):
                latch(target)

        # The explicit A+B joint move should compose with the single C flip.
        states = reachable_states(logic, project=["Target"], joint_inputs=(("A", "B"),))
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_joint_inputs_dual_rise_workaround(self):
        """joint_inputs=(("A","B"),) recovers the simultaneous pair."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(target)

        states = reachable_states(logic, project=["Target"], joint_inputs=(("A", "B"),))
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_joint_inputs_rise_fall_workaround(self):
        """joint_inputs=(("A","B"),) recovers the cross-edge pair."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), fall(b)):
                latch(target)

        states = reachable_states(logic, project=["Target"], joint_inputs=(("A", "B"),))
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states

    def test_caveat_emitted_for_uncovered_triple_edge_set(self):
        """prove() should still emit a caveat for uncovered larger edge sets."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b), rise(c)):
                latch(target)

        result = prove(logic, ~target)
        assert isinstance(result, Proven)
        assert result.caveats, "should emit edge caveat for uncovered inputs"
        assert any("A" in caveat and "B" in caveat and "C" in caveat for caveat in result.caveats)


class TestSequentialAndSimultaneousEdgeCoverage:
    """Sequential and auto-jointed simultaneous edge paths should both be reachable."""

    def test_sequential_and_simultaneous_targets_reachable(self):
        """Both sequential and simultaneous targets should be reachable."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        phase = Bool("Phase")
        seq_target = Bool("SeqTarget")
        sim_target = Bool("SimTarget")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(phase)
            with Rung(phase, rise(b)):
                latch(seq_target)
            with Rung(rise(a), rise(b)):
                latch(sim_target)

        states = reachable_states(logic, project=["SeqTarget", "SimTarget"])
        assert not isinstance(states, Intractable)
        assert any(("SeqTarget", True) in s for s in states)
        assert any(("SimTarget", True) in s for s in states)


class TestAutoJointDetectionLimits:
    """Current auto-joint detection is limited to edge pairs in one conjunction tree."""

    @pytest.mark.xfail(
        reason="auto-joint detection does not yet infer simultaneous edge pairs spread across multiple rungs"
    )
    def test_split_dual_edges_across_rungs_not_auto_jointed(self):
        """Two edge pulses materialized on separate rungs are still missed without explicit joint_inputs."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        a_edge = Bool("AEdge")
        b_edge = Bool("BEdge")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                out(a_edge)
            with Rung(rise(b)):
                out(b_edge)
            with Rung(a_edge, b_edge):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True, "B": True})
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"])
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states


class TestAdversarialElisionSoundness:
    """calc(C + Step, C) where Step is a tag — the elision pass
    incorrectly classifies C as scan-local (write-before-read) without
    tracking that the calc source expression reads C from the previous scan.

    FIX DIRECTION: the abstract provenance analysis in elision/abstract.py
    must treat CalcInstruction source reads as cross-scan dependencies when
    the source expression references the target tag itself.
    """

    def test_self_referencing_calc_not_elided(self):
        """calc(C + Step, C) reads C cross-scan — C must stay in state key."""
        step = Int("Step")
        c = Int("C")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, step)
            with Rung():
                calc(c + step, c)
            with Rung(c >= 5):
                latch(target)

        # Concrete PLC reaches Target=True after 5 scans.
        plc = PLC(logic, dt=0.010)
        for _ in range(6):
            plc.step()
        assert plc.current_state.tags["Target"] is True

        # prove() must find the counterexample.
        result = prove(logic, ~target, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"C is reachable at >=5 but prove returned {type(result).__name__} "
            f"with states_explored={getattr(result, 'states_explored', '?')}"
        )


class TestAdversarialConcreteElisionCombinationalObserver:
    """Concrete elision must observe combinational tags downstream of a candidate.

    When B0 has a self-referencing OTE (Rung(B0): out(B0)) followed by another
    OTE that overwrites it (Rung(In0): out(B0)), B0's end-of-scan value is
    always In0 — so the concrete proof sees no sensitivity to B0's entry value
    when only observing B0.  But the mid-scan value of B0 (between Rung 1 and
    Rung 3) propagates to combinational tag B1, making B1=True reachable.
    """

    def test_self_latch_ote_mid_scan_affects_combinational(self):
        In0 = Bool("In0", external=True)
        B0 = Bool("B0")
        B1 = Bool("B1")

        with Program(strict=False) as logic:
            with Rung(B0):
                out(B0)
            with Rung(B0):
                out(B1)
            with Rung(In0):
                out(B0)

        plc = PLC(logic, dt=0.010)
        plc.patch({"In0": True})
        plc.step()
        plc.step()
        assert plc.current_state.tags["B1"] is True

        result = prove(logic, B1 == False, max_states=10_000, depth_budget=20)  # noqa: E712
        assert isinstance(result, Counterexample), (
            f"B1=True is reachable via mid-scan B0 propagation but prove returned "
            f"{type(result).__name__}"
        )
        plc2 = _replay_trace(logic, result.trace)
        assert plc2.current_state.tags["B1"] is True


class TestAdversarialAbstractElisionRetainedSummary:
    """Abstract elision must not accept tags whose entry value lags retained state by one scan."""

    def test_retained_seeded_one_scan_late_not_elided(self):
        """C is read before a write sourced from Mode, so C must remain in the state key."""
        start = Bool("Start", external=True)
        mode = Bool("Mode")
        c = Bool("C")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(c):
                latch(target)
            with Rung(mode):
                copy(True, c)
            with Rung(start):
                latch(mode)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        plc.patch({"Start": False})
        plc.step()
        plc.step()
        assert plc.current_state.tags["Target"] is True

        result = prove(logic, ~target, depth_budget=5)
        assert isinstance(result, Counterexample), (
            "C keeps one-scan-delayed memory relative to Mode, so prove() should find "
            "the Target counterexample instead of merging the distinct Mode=True states"
        )

    def test_forloop_zero_iteration_delay_not_elided(self):
        """A forloop body gated by the previous C value makes Target=True reachable on scan 3."""
        start = Bool("Start", external=True)
        mode = Bool("Mode")
        c = Bool("C")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung():
                with forloop(c):
                    latch(target)
            with Rung(mode):
                copy(True, c)
            with Rung(start):
                latch(mode)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        plc.patch({"Start": False})
        plc.step()
        plc.step()
        assert plc.current_state.tags["Target"] is True

        states = reachable_states(logic, project=["Target"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Target", True)}) in states


class TestAdversarialDepthTruncation:
    """depth_budget truncation silently returns Proven with no caveat.

    FIX DIRECTION: emit a caveat when any BFS frontier state is discarded
    due to depth — at minimum ``"BFS reached depth_budget={n}; deeper states
    were not explored"``.
    """

    def test_constant_stride_threshold_counterexample_not_lost_to_depth(self):
        enable = Bool("Enable", external=True)
        step = Int("Step")
        c = Int("C")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, step)
            with Rung(enable):
                calc(c + step, c)
            with Rung(c >= 55):
                latch(target)

        result = prove(logic, ~target, depth_budget=50)
        assert isinstance(result, Counterexample), (
            f"C should reach the threshold with Enable=True, but prove returned "
            f"{type(result).__name__} with caveats={getattr(result, 'caveats', '?')}"
        )

    def test_depth_truncation_emits_caveat(self):
        """When the BFS hits depth_budget, Proven.caveats should warn the user."""
        enable = Bool("Enable", external=True)
        stage_a = Bool("StageA")
        stage_b = Bool("StageB")
        stage_c = Bool("StageC")
        stage_d = Bool("StageD")
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung():
                copy(stage_d, target)
            with Rung():
                copy(stage_c, stage_d)
            with Rung():
                copy(stage_b, stage_c)
            with Rung():
                copy(stage_a, stage_b)
            with Rung(enable):
                latch(stage_a)

        predicate, auto_scope, expr = prove_module._compile_property_spec(~target)
        extra_exprs = [expr] if expr is not None else []
        context = prove_module._build_explore_context(
            logic,
            scope=auto_scope,
            extra_exprs=extra_exprs,
        )

        assert not isinstance(context, Intractable)
        context = replace(
            context,
            stateful_dims={
                "StageA": (False, True),
                "StageB": (False, True),
                "StageC": (False, True),
                "StageD": (False, True),
                "Target": (False, True),
            },
            stateful_names=("StageA", "StageB", "StageC", "StageD", "Target"),
        )
        result = _bfs_explore(
            context,
            predicates=[predicate],
            depth_budget=3,
            bfs_config=_BFSConfig(
                live_input_pruning=False,
                edge_compression=False,
                hidden_event_jumping=False,
                pending_settlement=False,
            ),
        )[0]
        assert isinstance(result, Proven)
        assert any("depth_budget=3" in caveat for caveat in result.caveats), (
            "prove() should emit a caveat when depth_budget truncates exploration"
        )

    def test_no_depth_caveat_when_exhaustive(self):
        """Fully explored finite state spaces should not report depth truncation."""
        enable = Bool("Enable", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(enable):
                latch(target)

        result = prove(logic, lambda s: True, depth_budget=3)
        assert isinstance(result, Proven)
        assert not any("depth_budget" in caveat for caveat in result.caveats), (
            "prove() should not emit a depth caveat when exploration is exhaustive"
        )


class TestAdversarialNDDomainCompleteness:
    """Backward propagation of comparison boundaries through copy chains.

    When ``copy(nd_input, stateful_tag)`` feeds a downstream comparison on the
    stateful tag, the ND domain must include the downstream boundary values —
    otherwise the BFS never enumerates them.

    Fixed by ``_backward_propagate_comparison_boundaries`` in classify.py,
    which runs after the forward structural-domain fixed-point and unions
    target comparison atoms back into source domains through copy edges.
    """

    def test_copy_to_stateful_tag_with_different_comparison(self):
        """copy(Level, Stored) + Stored == 75 requires Level=75 in the ND domain."""
        level = Int("Level", external=True)
        stored = Int("Stored")
        alarm_a = Bool("AlarmA")
        alarm_b = Bool("AlarmB")

        with Program(strict=False) as logic:
            with Rung(level > 100):
                latch(alarm_a)
            with Rung():
                copy(level, stored)
            with Rung(stored == 75):
                latch(alarm_b)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["AlarmB"] is True

        result = prove(logic, ~alarm_b)
        assert isinstance(result, Counterexample), (
            f"Level=75 flows through copy to Stored=75 which latches AlarmB, "
            f"but prove returned {type(result).__name__} "
            f"(ND domain for Level likely missing 75)"
        )

    def test_calc_offset_to_stateful_tag_with_comparison(self):
        """calc(Level + 10, Stored) + Stored == 85 requires Level=75 in the ND domain."""
        level = Int("Level", external=True)
        stored = Int("Stored")
        alarm_a = Bool("AlarmA")
        alarm_b = Bool("AlarmB")

        with Program(strict=False) as logic:
            with Rung(level > 100):
                latch(alarm_a)
            with Rung():
                calc(level + 10, stored)
            with Rung(stored == 85):
                latch(alarm_b)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["AlarmB"] is True

        result = prove(logic, ~alarm_b)
        assert isinstance(result, Counterexample), (
            f"Level=75 flows through calc(level+10) to Stored=85 which latches AlarmB, "
            f"but prove returned {type(result).__name__} "
            f"(ND domain for Level likely missing 75)"
        )


class TestReversePropagationFallbacks:
    """Unsupported reverse shapes must widen to declared domains or become Intractable."""

    def test_bounded_unsupported_reverse_widens_to_declared_domain(self):
        """calc(source % 10, stored) with stored == 5 and source min=0/max=20:
        backward propagation cannot invert %, but source's declared domain
        covers the critical value (source=5), so prove finds the counterexample."""
        source = Int("Source", external=True, min=0, max=20)
        stored = Int("Stored")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(source > 100):
                latch(alarm)
            with Rung():
                calc(source % 10, stored)
            with Rung(stored == 5):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Source": 5})
        plc.step()
        assert plc.current_state.tags["Stored"] == 5
        assert plc.current_state.tags["Alarm"] is True

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Source=5 should produce Stored=5 via %, but prove returned {type(result).__name__}"
        )

    def test_unbounded_unsupported_reverse_is_intractable(self):
        """calc(source % 10, stored) with stored == 5 and no source metadata:
        backward propagation cannot invert % and source has no safe finite
        fallback, so the result must be Intractable (not a false Proven)."""
        source = Int("Source", external=True)
        stored = Int("Stored")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(source > 100):
                latch(alarm)
            with Rung():
                calc(source % 10, stored)
            with Rung(stored == 5):
                latch(alarm)

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Intractable), (
            "Source has no metadata and % is non-invertible, "
            f"expected Intractable but got {type(result).__name__}"
        )


class TestAdversarialOTECombinational:
    """OTE combinational classification guards.

    OTE is combinational only when every writer rung evaluates every scan.
    Two cases break that invariant: conditionally-called subroutines (the OTE
    doesn't fire when the sub isn't called) and self-referencing conditions
    (the output depends on the previous-scan value).

    Fixed by ``_is_ote_unconditionally_reachable`` and
    ``_is_self_referencing_ote`` in classify.py, plus a concrete-elision
    guard in ``_can_elide`` that observes the candidate's own exit value
    when it has downstream readers but no retained stateful frontier.
    """

    def test_subroutine_ote_retains_value_when_sub_not_called(self):
        """Flag is OTE in a subroutine only called when Mode=True. When Mode goes
        False the sub is not called and Flag retains its previous value."""
        from pyrung.core import call, subroutine

        mode = Bool("Mode")
        enable = Bool("Enable", external=True)
        trigger = Bool("Trigger", external=True)
        flag = Bool("Flag")
        target = Bool("Target")

        @subroutine("SubOteWorker", strict=False)
        def sub_worker():
            with Rung(trigger):
                out(flag)

        with Program(strict=False) as logic:
            with Rung(enable):
                out(mode)
            with Rung(mode):
                call(sub_worker)
            with Rung(flag, ~mode):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True, "Trigger": True})
        plc.step()
        assert plc.current_state.tags.get("Flag") is True
        plc.patch({"Enable": False, "Trigger": False})
        plc.step()
        assert plc.current_state.tags.get("Target") is True

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample), (
            f"Flag=True retained from sub call + ~Mode=True should latch Target, "
            f"but prove returned {type(result).__name__}"
        )

    def test_self_referencing_ote_cross_scan_state(self):
        """Toggle = enable AND ~toggle_prev alternates each scan. With default=True
        the initial phase is toggle=True→False, and target requires the second phase
        (toggle=True on scan 1)."""
        toggle = Bool("Toggle", default=True)
        enable = Bool("Enable", external=True)
        sensor = Bool("Sensor", external=True)
        target = Bool("Target")

        with Program(strict=False) as logic:
            with Rung(enable, ~toggle):
                out(toggle)
            with Rung(toggle, sensor):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True, "Sensor": True})
        plc.step()
        assert plc.current_state.tags.get("Toggle") is False
        plc.step()
        assert plc.current_state.tags.get("Target") is True

        result = prove(logic, ~target)
        assert isinstance(result, Counterexample), (
            f"Toggle alternates True→False→True; target reachable on scan 1, "
            f"but prove returned {type(result).__name__}"
        )


class TestReceiveDestAutoND:
    """Receive() destination tags are nondeterministic regardless of external annotation."""

    def test_receive_dest_classified_as_nd_without_external(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        Enable = Bool("Enable", external=True)
        Dest = Int("Dest", choices={0: "OFF", 1: "ON", 2: "FAULT"})
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Alarm = Bool("Alarm")

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )
            with Rung(Dest == 2):
                out(Alarm)

        assert not Dest.external

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _sd, nd, _comb, _da, _dp, _dk = result
        assert "Dest" in nd, "Dest should be nondeterministic without external=True"

    def test_receive_dest_matches_explicit_external(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        Enable = Bool("Enable", external=True)
        Dest = Int("Dest", choices={0: "OFF", 1: "ON", 2: "FAULT"}, external=True)
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Alarm = Bool("Alarm")

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )
            with Rung(Dest == 2):
                out(Alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _sd, nd, _comb, _da, _dp, _dk = result
        assert "Dest" in nd, "Dest should be nondeterministic with explicit external=True"

    def test_receive_dest_reachable_states_explores_values(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        Enable = Bool("Enable", external=True)
        Dest = Int("Dest", choices={0: "OFF", 1: "ON", 2: "FAULT"})
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")
        Alarm = Bool("Alarm", lock=True)

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )
            with Rung(Dest == 2):
                latch(Alarm)

        states = reachable_states(logic)
        assert not isinstance(states, Intractable)
        alarm_values = {dict(s).get("Alarm", False) for s in states}
        assert True in alarm_values, "Alarm=True should be reachable when Dest==2"
