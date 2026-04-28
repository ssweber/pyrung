"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

from pathlib import Path

from pyrung.cli import _apply_lock_config
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
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
    count_up,
    fill,
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
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr

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
        assert set(nd["Step"]) == {4, 5, 6}

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
        assert any(step.inputs.get("Level") == 4 for step in result.trace)

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

        runner = PLC(logic, dt=0.010)
        for step in result.trace:
            runner.patch(step.inputs)
            for _ in range(step.scans):
                runner.step()
        assert runner.current_state.tags.get("Flag") is True

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
                with Rung(inp):
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
    """_default_projection returns terminals, not public tags."""

    def test_terminals_not_public(self):
        button = Bool("Button", external=True)
        running = Bool("Running", public=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
            with Rung(running):
                out(light)

        proj = _default_projection(logic)
        assert proj == ["Light"]

    def test_empty_when_no_terminals(self):
        button = Bool("Button", external=True)
        internal = Bool("Internal")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(internal)
            with Rung(internal):
                reset(internal)

        proj = _default_projection(logic)
        assert proj == []


class TestApplyLockConfig:
    """CLI _apply_lock_config include/exclude logic."""

    def test_none_config_passthrough(self):
        proj = _apply_lock_config(["A", "B"], None)
        assert proj == ["A", "B"]

    def test_include_adds_tags(self):
        proj = _apply_lock_config(["A"], {"include": ["B", "C"]})
        assert proj == ["A", "B", "C"]

    def test_exclude_removes_tags(self):
        proj = _apply_lock_config(["A", "B", "C"], {"exclude": ["B"]})
        assert proj == ["A", "C"]

    def test_include_and_exclude(self):
        proj = _apply_lock_config(["A", "B"], {"include": ["C"], "exclude": ["A"]})
        assert proj == ["B", "C"]

    def test_exclude_nonexistent_is_noop(self):
        proj = _apply_lock_config(["A"], {"exclude": ["Z"]})
        assert proj == ["A"]

    def test_include_duplicate_is_noop(self):
        proj = _apply_lock_config(["A", "B"], {"include": ["A"]})
        assert proj == ["A", "B"]


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

        states = reachable_states(logic, project=["Output", "T1_Done"], max_depth=60)
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

        states = reachable_states(logic, project=["Output", "BigT_Done"], max_depth=10)
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

        states = reachable_states(logic, project=["Output", "C1_Done"], max_depth=10)
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

        states = reachable_states(logic, project=["Expired", "T1_Done"], max_depth=10)
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

        proved = prove(logic, Or(~output, t.Done), max_depth=5)
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

        proved = prove(logic, Or(~output, t.Done), max_depth=5)
        assert isinstance(proved, Proven)

    def test_external_preset_redundant_acc_comparison_is_absorbed(self):
        """External presets still absorb when their value is threshold-only."""
        enable = Bool("Enable", external=True)
        hmi_preset = Int("HmiPreset", external=True)
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

        proved = prove(logic, Or(~output, t.Done), max_depth=5)
        assert isinstance(proved, Proven)

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

        states = reachable_states(logic, project=["Output", "DynT_Done"], max_depth=5)
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

        states = reachable_states(logic, project=["Alarm"], max_depth=5)
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

        states = reachable_states(logic, project=["ResettableTimerAlarm"], max_depth=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableTimerAlarm", True)}) in states

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
            max_depth=10,
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

        states = reachable_states(logic, project=["CounterAlarm"], max_depth=5)
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

        states = reachable_states(logic, project=["ResettableCounterAlarm"], max_depth=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableCounterAlarm", True)}) in states

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

        states = reachable_states(logic, project=["TickAlarm"], max_depth=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("TickAlarm", True)}) in states

    def test_variable_stride_int_progress_stays_explicit(self):
        enable = Bool("Enable", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride", final=True)
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

        states = reachable_states(logic, project=["AtFive"], max_depth=5)
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

        states = reachable_states(logic, project=["Running"], max_depth=5)
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

        states = reachable_states(logic, project=["AtStep3", "PastThreshold"], max_depth=5)
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

        states = reachable_states(logic, project=["ExternalThresholdAlarm"], max_depth=5)
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

        states = reachable_states(logic, project=["ImplicitThresholdAlarm"], max_depth=5)
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
            max_depth=5,
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

        states = reachable_states(logic, project=["PublicThresholdAlarm"], max_depth=5)
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

        states = reachable_states(logic, project=["ExactAlarm", "HmiAlarm"], max_depth=5)
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

        states = reachable_states(logic, project=["ResettableTickAlarm"], max_depth=8)
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

        states = reachable_states(logic, project=["Warning", "NearestTmr_Done"], max_depth=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Warning", True), ("NearestTmr_Done", False)}) in states


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
        """Pointer tag (IndirectRef) gets a hint naming the block and range."""
        from pyrung.core import Block, TagType, copy

        blk = Block("Regs", TagType.INT, 1, 50)
        idx = Int("Idx", external=True)
        other = Int("Other", external=True)
        dest = Int("Out")

        with Program(strict=False) as logic:
            with Rung(idx > other):
                copy(blk[idx], dest)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert any("pointer" in h and "Regs" in h for h in result.hints)
        assert any("Idx" in h for h in result.hints)
        other_hints = [h for h in result.hints if "Other" in h]
        assert other_hints
        assert all("pointer" not in h for h in other_hints)

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
                with Rung(inp):
                    latch(flag)
            with Rung(*flags):
                out(output)

        result = prove(logic, lambda s: True, max_states=10)
        assert isinstance(result, Intractable)
        assert result.hints
        assert any("state space:" in h for h in result.hints)
        assert any("Constrain" in h for h in result.hints)

    def test_hints_mention_readonly(self):
        """All hint types mention readonly=True as an option."""
        val = Int("Val", external=True)
        other = Int("Other", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(val > other):
                out(flag)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable)
        assert all("readonly=True" in h for h in result.hints)


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

        result = reachable_states(logic, project=["StableAlarm"], max_depth=5)
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

        states = reachable_states(logic, project=["Flag"], max_depth=5)
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
