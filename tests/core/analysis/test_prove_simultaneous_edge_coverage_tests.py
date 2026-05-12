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

    def test_forloop_minimum_one_count_executes_body(self):
        """A forloop count that resolves false/zero still executes one body iteration."""
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
        plc.patch({"Start": False})
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
