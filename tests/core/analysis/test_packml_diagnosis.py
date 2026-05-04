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
    prove,
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

        states = reachable_states(logic, project=["StateCurrent"], max_depth=50)
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
# Test 13: Dual-rise caveat contract
# ===================================================================


class TestDualRiseCaveat:
    def test_ungrouped_dual_rise_produces_caveat(self):
        """Two ungrouped rise() inputs in the same condition must produce a caveat on Proven."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(output)

        result = prove(logic, Or(output, ~output))
        assert isinstance(result, Proven)
        assert any("Simultaneous edge" in c for c in result.caveats), (
            f"Expected a 'Simultaneous edge combinations' caveat for ungrouped "
            f"rise(A), rise(B). Got caveats: {result.caveats}"
        )
