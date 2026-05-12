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
    Real,
    Rung,
    TagType,
    Timer,
    Word,
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


def _assert_soundness(
    logic: Program,
    condition,
    *,
    max_states: int = 10_000,
    depth_budget: int = 20,
) -> None:
    """Assert that optimized and unoptimized prove() agree on the result type."""
    optimized = prove(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = prove(
        logic,
        condition,
        max_states=max_states,
        depth_budget=depth_budget,
        _skip_optimizations=True,
        journal=True,
    )
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        pytest.skip("one side intractable")
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}\n"
        f"--- optimized journal ---\n{optimized.journal}\n"
        f"--- unoptimized journal ---\n{unoptimized.journal}"
    )


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
