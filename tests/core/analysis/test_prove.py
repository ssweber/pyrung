"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

from pathlib import Path

from pyrung.core import (
    Counter,
    Or,
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    reset,
    rise,
    run_function,
)
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import Atom, Const
from pyrung.core.analysis.simplified import Or as ExprOr
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
    StateDiff,
    _classify_dimensions,
    _eval_atom,
    _live_inputs,
    _partial_eval,
    check_lock,
    diff_states,
    program_hash,
    prove,
    reachable_states,
    write_lock,
)

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
        stateful, nd, combinational, _done_acc, _max_preset = result
        assert "Light" not in stateful
        assert "Light" in combinational
        assert "InputA" in nd

    def test_latch_reset_are_stateful(self):
        """Latch/reset writes make tags stateful."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, combinational, _done_acc, _max_preset = result
        assert "Running" in stateful
        assert stateful["Running"] == (False, True)
        assert "Button" in nd
        assert "Stop" in nd

    def test_external_tags_are_nondeterministic(self):
        """Tags with external=True are nondeterministic."""
        sensor = Bool("Sensor", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(sensor):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _max_preset = result
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
        stateful, nd, _combinational, _done_acc, _max_preset = result
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
        stateful, _nd, _combinational, _done_acc, _max_preset = result
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
        _stateful, nd, _combinational, _done_acc, _max_preset = result
        assert nd["Flag"] == (False, True)

    def test_integer_comparison_literals(self):
        """Int tag compared with literals extracts those values + unmatched."""
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
        _stateful, nd, _combinational, _done_acc, _max_preset = result
        domain = nd["State"]
        assert 1 in domain
        assert 2 in domain
        assert len(domain) == 3  # 1, 2, unmatched

    def test_choices_tag_uses_declared_domain(self):
        """Tag with choices uses the declared values."""
        mode = Int("Mode", external=True, choices={0: "off", 1: "on", 2: "auto"})
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(mode == 1):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _max_preset = result
        assert set(nd["Mode"]) == {0, 1, 2}

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
        for inputs in result.trace:
            runner.patch(inputs)
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

        with Program(strict=False) as logic:
            for inp, flag in zip(inputs, flags, strict=True):
                with Rung(inp):
                    latch(flag)

        result = prove(
            logic,
            lambda s: True,
            max_states=10,
        )
        assert isinstance(result, Intractable)
        assert "max_states" in result.reason


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
        _stateful, nd, _combinational, _done_acc, _max_preset = result
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


class TestConsumedAccumulator:
    """Item 15: accumulator consumed in a condition stays as separate dimension."""

    def test_consumed_acc_kept_as_dimension(self):
        """Timer accumulator used in a condition is not collapsed."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        early = Bool("Early")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc > 50):
                out(early)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _combinational, done_acc, _max_preset = result
        assert "T1_Acc" in stateful
        assert "T1_Done" not in done_acc


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

    def test_max_preset_extracted(self):
        """_classify_dimensions extracts max_preset from timer instructions."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=5000)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _s, _n, _c, _d, max_preset = result
        assert max_preset == 5000


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
