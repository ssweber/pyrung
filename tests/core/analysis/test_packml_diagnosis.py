"""Regression tests for free-input × edge-flip cross-product in BFS.

The root cause was that free inputs (no rise/fall) and edge-bearing
inputs were enumerated as independent successor sets, so the combo
{FreeInput=True, EdgeInput flipped} was never explored. Encoder-group
canonicals had the same gap. Fixed in inputs.py by restructuring
_iter_input_assignments to cross-product edge bases × encoder combos
× free combos.
"""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    Bool,
    Int,
    Or,
    Program,
    Rung,
    call,
    copy,
    latch,
    rise,
    subroutine,
)
from pyrung.core.analysis.prove import (
    Intractable,
    Proven,
    _classify_dimensions,
    always,
    reachable_states,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


# ===================================================================
# Test 1: PackML baseline regression
# ===================================================================


class TestPackMLBaseline:
    def test_state_machine_reaches_beyond_stopped(self):
        """StateCurrent must reach at least STOPPED, RESETTING, IDLE, STARTING, EXECUTE."""
        from examples.packml_bench import logic

        states = reachable_states(logic, project=["StateCurrent"], depth_budget=50)
        assert not isinstance(states, Intractable), f"Intractable: {states}"
        values = {dict(s)["StateCurrent"] for s in states}
        expected = {"STOPPED", "RESETTING", "IDLE", "STARTING", "EXECUTE"}
        missing = expected - values
        assert not missing, (
            f"StateCurrent stuck ��� missing states: {', '.join(sorted(missing))}. "
            f"Reached: {', '.join(sorted(values))}"
        )


# ===================================================================
# Test 2: Classification of external+written tags (Theory 1)
# ===================================================================


class TestExternalWrittenClassification:
    def test_unconditionally_written_external_without_readers_is_combinational(self):
        """External+written tag with no readers is correctly combinational."""
        val = Int("Val", choices={0: "A", 1: "B", 2: "C"}, external=True)
        enable = Bool("Enable", external=True)

        with Program(strict=False) as logic:
            with Rung():
                copy(0, val)
            with Rung(enable):
                copy(1, val)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful_dims, nondeterministic_dims, combinational_tags, *_ = result
        assert "Val" in combinational_tags
        assert "Val" not in nondeterministic_dims

    def test_written_external_with_reader_is_stateful(self):
        """External+written tag WITH a downstream reader should be stateful."""
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"}, external=True)
        btn_a = Bool("BtnA", external=True)
        btn_b = Bool("BtnB", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(btn_a):
                copy(1, cmd)
            with Rung(btn_b):
                copy(2, cmd)
            with Rung(cmd == 1):
                latch(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful_dims, nondeterministic_dims, *_ = result
        assert "Cmd" in stateful_dims, (
            f"Cmd should be stateful when it has downstream readers. "
            f"stateful={list(stateful_dims)}, nd={list(nondeterministic_dims)}"
        )


# ===================================================================
# Test 3: Elision of external+written tags (Theory 2)
# ===================================================================


class TestExternalWrittenElision:
    def test_wbr_elision_correct_with_cross_product(self):
        """WBR tag elision is sound when BFS cross-products inputs with edge flips."""
        val = Int("Val", choices={0: "A", 1: "B", 2: "C"}, external=True)
        enable = Bool("Enable", external=True)
        trigger = Bool("Trigger", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, val)
            with Rung(enable):
                copy(1, val)
            with Rung(rise(trigger), val == 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable even when Val is WBR-elided, "
            "because the BFS cross-products Enable with rise(Trigger)"
        )


# ===================================================================
# Test 4: Free input + edge coincidence (Theory 4/5)
# ===================================================================


class TestFreeInputEdgeCoincidence:
    def test_free_input_and_edge_same_scan(self):
        """Output must be reachable when a free input and an edge are required together."""
        enable = Bool("Enable", external=True)
        trigger = Bool("Trigger", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(rise(trigger), enable):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, "Output=True should be reachable via Enable+rise(Trigger)"


# ===================================================================
# Test 5: Free input through combinational intermediary + edge
# ===================================================================


class TestFreeInputThroughIntermediary:
    def test_intermediary_preserves_free_input_effect(self):
        """Free input → copy → intermediary → comparison + edge → output must be reachable."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        trigger = Bool("Trigger", external=True)
        mid = Int("Mid", choices={0: "None", 1: "One", 2: "Two"})
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, mid)
            with Rung(a):
                copy(1, mid)
            with Rung(b):
                copy(2, mid)
            with Rung(rise(trigger), mid >= 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, "Output=True should be reachable via A/B → Mid >= 1 + rise(Trigger)"


# ===================================================================
# Test 6: Free input through external+written intermediary + edge
# ===================================================================


class TestExternalIntermediaryEdge:
    def test_external_intermediary_preserves_reachability(self):
        """Same as Test 5 but Mid is external=True (the CtrlCmd pattern)."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        trigger = Bool("Trigger", external=True)
        mid = Int("Mid", choices={0: "None", 1: "One", 2: "Two"}, external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, mid)
            with Rung(a):
                copy(1, mid)
            with Rung(b):
                copy(2, mid)
            with Rung(rise(trigger), mid >= 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via A/B → Mid >= 1 + rise(Trigger). "
            "If Test 5 passes but this fails, external= causes misclassification or improper elision."
        )


# ===================================================================
# Test 7: Live input pruning check
# ===================================================================


class TestLiveInputPruning:
    def test_free_inputs_through_intermediary_are_live(self):
        """Buttons feeding an unconditional copy should always be live."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        trigger = Bool("Trigger", external=True)
        mid = Int("Mid", choices={0: "None", 1: "One", 2: "Two"})
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, mid)
            with Rung(a):
                copy(1, mid)
            with Rung(b):
                copy(2, mid)
            with Rung(rise(trigger), mid >= 1):
                latch(output)

        context = prove_module._build_explore_context(logic)
        assert not isinstance(context, Intractable)
        assert "A" in context.free_input_names or "A" in context.nondeterministic_names, (
            "A should be enumerated (free or ND)"
        )
        assert "B" in context.free_input_names or "B" in context.nondeterministic_names, (
            "B should be enumerated (free or ND)"
        )


# ===================================================================
# Test 8: Joint enumeration of free inputs
# ===================================================================


class TestJointEnumeration:
    def test_two_free_inputs_jointly_reach_output(self):
        """Two free inputs both True on same scan must reach the output."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(a, b):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable when both A and B are True. "
            "If this fails, joint enumeration of free inputs is broken."
        )


# ===================================================================
# Test 9: ND tags overwritten by kernel
# ===================================================================


class TestOverwrittenNDEnumeration:
    def test_nd_enumeration_not_wasted_on_overwritten_tag(self):
        """External tag unconditionally written: BFS should track program-computed value."""
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"}, external=True)
        btn = Bool("Btn", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(btn):
                copy(1, cmd)
            with Rung(cmd == 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via Btn → Cmd=1 → latch. "
            "If Cmd is enumerated as ND input, the kernel overwrites it."
        )


# ===================================================================
# Test 10: Exit distinguishability under elision
# ===================================================================


class TestExitDistinguishabilityElision:
    def test_wbr_elidable_tag_exit_value_preserved(self):
        """WBR tag's exit value depends on free input; next scan needs to distinguish."""
        selector = Bool("Selector", external=True)
        trigger = Bool("Trigger", external=True)
        derived = Int("Derived", choices={0: "A", 1: "B"})
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, derived)
            with Rung(selector):
                copy(1, derived)
            with Rung(rise(trigger), derived == 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via Selector → Derived=1 + rise(Trigger). "
            "If this fails but Test 4 passes, Derived is being improperly elided."
        )


# ===================================================================
# Test 11: Subroutine intermediary (closer to PackML structure)
# ===================================================================


class TestSubroutineIntermediary:
    def test_subroutine_computed_intermediary_with_edge(self):
        """Subroutine maps buttons→intermediary; edge gate on intermediary must work."""
        btn_a = Bool("BtnA", external=True)
        btn_b = Bool("BtnB", external=True)
        trigger = Bool("Trigger", external=True)
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"})
        output = Bool("Output")

        @subroutine("MapButtons", strict=False)
        def map_buttons():
            with Rung():
                copy(0, cmd)
            with Rung(btn_a):
                copy(1, cmd)
            with Rung(btn_b):
                copy(2, cmd)

        with Program(strict=False) as logic:
            with Rung():
                call(map_buttons)
            with Rung(rise(trigger), cmd >= 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via subroutine-computed Cmd + rise(Trigger)"
        )

    def test_subroutine_external_intermediary_with_edge(self):
        """Same as above but Cmd is external=True (full CtrlCmd pattern)."""
        btn_a = Bool("BtnA", external=True)
        btn_b = Bool("BtnB", external=True)
        trigger = Bool("Trigger", external=True)
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"}, external=True)
        output = Bool("Output")

        @subroutine("MapButtonsExt", strict=False)
        def map_buttons_ext():
            with Rung():
                copy(0, cmd)
            with Rung(btn_a):
                copy(1, cmd)
            with Rung(btn_b):
                copy(2, cmd)

        with Program(strict=False) as logic:
            with Rung():
                call(map_buttons_ext)
            with Rung(rise(trigger), cmd >= 1):
                latch(output)

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via subroutine-computed external Cmd + rise(Trigger). "
            "This is the minimal reproduction of the PackML CtrlCmd pattern."
        )


# ===================================================================
# Test 12: Encoder group × edge cross-product
# ===================================================================


class TestEncoderGroupEdgeCrossProduct:
    def test_encoder_group_buttons_with_edge_trigger(self):
        """Mutually exclusive buttons (encoder group) combined with a separate edge trigger."""
        btn_a = Bool("BtnA", external=True)
        btn_b = Bool("BtnB", external=True)
        trigger = Bool("Trigger", external=True)
        cmd = Int("Cmd", choices={0: "None", 1: "A", 2: "B"})
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(0, cmd)
            with Rung(btn_a):
                copy(1, cmd)
            with Rung(btn_b):
                copy(2, cmd)
            with Rung(rise(trigger), cmd >= 1):
                latch(output)

        context = prove_module._build_reachable_context(
            logic, scope=["Output"], project=("Output",)
        )
        assert not isinstance(context, Intractable)
        assert context.exclusive_input_groups, (
            "BtnA/BtnB should form an encoder group for this test to exercise "
            "the encoder × edge cross-product path"
        )

        states = reachable_states(logic, project=["Output"])
        assert not isinstance(states, Intractable)
        values = {dict(s)["Output"] for s in states}
        assert True in values, (
            "Output=True should be reachable via encoder-group button + rise(Trigger)"
        )


# ===================================================================
# Test 13: Dual-rise auto-joint contract
# ===================================================================


class TestDualRiseAutoJoint:
    def test_ungrouped_dual_rise_is_handled_without_caveat(self):
        """Two ungrouped rise() inputs in the same condition should auto-joint cleanly."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(output)

        result = always(logic, Or(output, ~output))
        assert isinstance(result, Proven)
        assert result.caveats == ()


# ===================================================================
# Test 14: Hidden-event jump must fire only on a self-loop
# ===================================================================


class TestHiddenEventJumpSelfLoopOnly:
    """A hidden-event jump models time elapsing while the program stays on ONE
    plateau, so it must only fire as a self-loop.

    Root cause (distinct from the cross-product gap above): expanding STOPPED
    with an input assignment that *transitioned* to RESETTING — already visited,
    with a pending StateTimer — triggered a jump that fast-forwarded the timer
    and rode the unconditional RESETTING→IDLE auto-completion to IDLE. The
    jumped IDLE state was attributed to STOPPED, collapsing the intermediate
    transition out of the trace and clobbering the rising CmdChgRequest edge
    that drove it (the jump's edge-variant loop re-emits CmdChgRequest=False).
    ``how(IDLE)`` then returned a path that failed its own replay verification.
    """

    def test_how_idle_path_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        path = plc.how(StateCurrent == "IDLE")
        assert path.reachable, f"how(IDLE) should be reachable, got: {path.reason}"

        # Two-oracle check: the abstract BFS trace must replay to IDLE on a
        # concrete PLC.  A jump mis-attributed across a transition produces a
        # trace whose inputs no longer reproduce the path.
        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.IDLE.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected IDLE ({S.IDLE.default})"
        )


# ===================================================================
# Test 15: Multi-scan waypoint path from ABORTED to EXECUTE
# ===================================================================


class TestHowAbortedToExecute:
    """The waypoint planner must decompose a long multi-scan path through the
    PackML state machine: ABORTED → CLEARING → STOPPED → RESETTING → IDLE →
    STARTING → EXECUTE (7 steps).

    Regression for the hidden-event-jump bug (commit 8aa66f4) which caused
    ``how(EXECUTE)`` to return "path found but replay verification failed"
    because a jump across a state transition collapsed intermediate states
    out of the trace.
    """

    def test_how_execute_from_aborted_replays(self):
        from examples.packml_bench import (
            CmdAbort,
            CmdChgRequest,
            S,
            StateCurrent,
            logic,
        )
        from pyrung.core.runner import PLC

        # Seed PLC to ABORTED state
        plc = PLC(logic, dt=0.010)
        plc.step()  # init scan → STOPPED
        plc.patch({CmdAbort: True, CmdChgRequest: True})
        plc.step()  # ABORTING
        plc.step()  # ABORTED
        assert plc.state.tags["StateCurrent"] == S.ABORTED.default

        path = plc.how(StateCurrent == S.EXECUTE.default)
        assert path.reachable, f"how(EXECUTE) should be reachable, got: {path.reason}"
        assert path.total_changes >= 2, "ABORTED→EXECUTE requires multiple waypoints"

        # Two-oracle check: replay on a fresh PLC must reach EXECUTE
        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.EXECUTE.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected EXECUTE ({S.EXECUTE.default})"
        )


# ===================================================================
# Bench-fix regressions (spec §1.5) — pin the repaired packml_bench
# ===================================================================


def _seed_execute(plc):
    """Cold-start → EXECUTE via the edge-gated command path."""
    from examples.packml_bench import CmdChgRequest, CmdReset, CmdStart

    plc.step()  # init scan → STOPPED
    plc.patch({CmdReset: True, CmdChgRequest: True})
    plc.step()  # → RESETTING
    plc.patch({CmdReset: False, CmdChgRequest: False})
    plc.step()  # RESETTING → IDLE (auto-complete)
    plc.patch({CmdStart: True, CmdChgRequest: True})
    plc.step()  # → STARTING
    plc.patch({CmdStart: False, CmdChgRequest: False})
    plc.step()  # STARTING → EXECUTE (auto-complete)
    return plc


class TestHowHeldFromExecute:
    """§1.5: how(HELD) from EXECUTE — a coordinated level command (CmdHold)
    and a rising edge (rise(CmdChgRequest)) must land on the same scan, and
    CtrlCmd is both external and program-written, so ``how`` has two routes to
    ``CtrlCmd == 4``."""

    def test_how_held_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        _seed_execute(plc)
        assert plc.state.tags["StateCurrent"] == S.EXECUTE.default

        path = plc.how(StateCurrent == S.HELD.default)
        assert path.reachable, f"how(HELD) should be reachable, got: {path.reason}"

        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.HELD.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected HELD ({S.HELD.default})"
        )


class TestHowCompletedFromColdStart:
    """§1.5 + §1.3: with the EXECUTE→COMPLETING row wired, COMPLETED is now
    reachable through the *internal* cycle (STARTING auto-advance, EXECUTE task
    dwell, COMPLETING → COMPLETED) from a cold start — not only via the external
    Complete command."""

    def test_how_completed_replays(self):
        from examples.packml_bench import S, StateCurrent, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        path = plc.how(StateCurrent == S.COMPLETED.default)
        assert path.reachable, f"how(COMPLETED) should be reachable, got: {path.reason}"

        replay = path.replay()
        assert replay.state.tags["StateCurrent"] == S.COMPLETED.default, (
            f"replayed path ended at StateCurrent="
            f"{replay.state.tags['StateCurrent']}, expected COMPLETED ({S.COMPLETED.default})"
        )


@pytest.mark.xfail(
    strict=True,
    reason="Causal attribution of an overflow-driven ABORTED through the affine "
    "LoopIndex counter and the ds[150+StateRequested] jump table is not yet "
    "supported; why() reports 'no writer has fired' at the transition scan.",
)
class TestWhyAbortedViaOverflow:
    """§1.5: ``why StateCurrent == ABORTED`` must attribute the transition to the
    runaway guard (``LoopIndex > 10`` → copy(ABORTED, StateRequested)) and trace
    back through the affine counter and the jump table.  Uses the command path to
    reach ABORTED as a proxy for the attribution capability; the full overflow
    seeding additionally needs a never-resolving jump-table cycle that the current
    bench data does not produce."""

    def test_why_names_the_writer(self):
        from examples.packml_bench import CmdAbort, CmdChgRequest, logic
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        plc.step()
        plc.patch({CmdAbort: True, CmdChgRequest: True})
        plc.step()  # → ABORTING
        plc.patch({CmdAbort: False, CmdChgRequest: False})
        plc.step()  # → ABORTED

        chain = plc.why("StateCurrent")
        assert "no writer" not in str(chain), (
            "why(StateCurrent) at ABORTED must name the writer that set it, "
            "not report 'no writer has fired'"
        )


class TestHowIntoModeDisabledState:
    """§1.5: driven into Manual mode (DisabledStates=0x0224 blocks STARTING via
    the StateMask), ``how(STARTING)`` must either discover the mode-change route
    or fail loudly with a reason — never silently return an unreachable/wrong plan.
    """

    @staticmethod
    def _manual_idle_plc():
        from examples.packml_bench import (
            CmdChgRequest,
            CmdReset,
            ModeChgRequest,
            UnitModeCmd,
            logic,
        )
        from pyrung.core.runner import PLC

        plc = PLC(logic, dt=0.010)
        plc.step()  # init → STOPPED, Production
        # Switch to Manual mode via the external UnitModeCmd + request path.
        for _ in range(2):
            plc.patch({UnitModeCmd: 3, ModeChgRequest: True})
            plc.step()
        plc.patch({UnitModeCmd: 0, ModeChgRequest: False})
        plc.patch({CmdReset: True, CmdChgRequest: True})
        plc.step()  # → RESETTING
        plc.patch({CmdReset: False, CmdChgRequest: False})
        plc.step()  # → IDLE
        assert plc.state.tags["DisabledStates"] == 0x0224
        return plc

    def test_how_disabled_state_is_loud(self):
        """Never a silent unreachable: the loop names why it gave up.

        STARTING is blocked in Manual, so from Manual the transition needs a mode
        change first.  Even when the pilot can't complete that multi-phase plan it
        must surface a reason (``stuck: …`` / ``budget exhausted``), not
        ``reachable=False, reason=None``.
        """
        from examples.packml_bench import StateCurrent

        plc = self._manual_idle_plc()
        path = plc.how(StateCurrent == 3, max_scans=300)  # 3 == STARTING
        assert path.reachable or path.reason is not None, (
            "how() into a mode-disabled state returned a silent unreachable "
            "(reachable=False, reason=None) — the loop must report why it gave up"
        )

    def test_how_disabled_state_reaches(self):
        """The real goal: discover and drive the mode change, then reach STARTING.

        Staged bearings: ``how(STARTING)`` from Manual surfaces the mode change as a
        stage-0 ``establish`` prerequisite (the tide tables invert the disabled-
        state mask over the mode domain), drives ``UnitModeCmd``/``ModeChgRequest``
        to a mask-clearing mode and lets it settle, then — once the gate re-reads
        satisfied — pursues the deferred stage-1 command to land on STARTING.
        """
        from examples.packml_bench import StateCurrent

        plc = self._manual_idle_plc()
        path = plc.how(StateCurrent == 3, max_scans=400)
        assert path.reachable
        assert path.replay().state.tags["StateCurrent"] == 3
