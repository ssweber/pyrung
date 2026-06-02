"""Tests for semantic path presentation in how() output."""

from __future__ import annotations

from pyrung.core.analysis.graph import (
    Path,
    ReachabilityStep,
    _classify_step_inputs,
    _enrich_atom_index,
    _render_step_inputs,
)
from pyrung.core.analysis.simplified import Atom

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    action: dict,
    constraints: dict[str, str] | None = None,
) -> ReachabilityStep:
    return ReachabilityStep(
        action=action,
        source_key=(0,),
        dest_key=(1,),
        scans=1,
        constraints=constraints,
    )


# ---------------------------------------------------------------------------
# _classify_step_inputs — unit tests
# ---------------------------------------------------------------------------


class TestClassifyStepInputs:
    """Unit tests for the three-tier classification logic."""

    def test_tier3_bool_unchanged(self):
        """Bool tags pass through as-is — no constraint annotation."""
        action = {"StartBtn": True, "StopBtn": False}
        atom_index: dict[str, list[Atom]] = {}
        domain_sources = {"StartBtn": "bool", "StopBtn": "bool"}
        result = _classify_step_inputs(action, atom_index, domain_sources)
        assert result == {}

    def test_tier3_choices_unchanged(self):
        action = {"Mode": 1}
        domain_sources = {"Mode": "choices"}
        result = _classify_step_inputs(action, {}, domain_sources)
        assert result == {}

    def test_tier2_paired_gt(self):
        """Two tags with a > comparison, both in the action → grouped constraint."""
        action = {"Pressure": 51.0, "Setpoint": 50.0}
        atom_index = {
            "Pressure": [Atom(tag="Pressure", form="gt", operand="Setpoint")],
            "Setpoint": [Atom(tag="Pressure", form="gt", operand="Setpoint")],
        }
        domain_sources = {
            "Pressure": "expression_partition",
            "Setpoint": "expression_partition",
        }
        dest_tags = {"Pressure": 51.0, "Setpoint": 50.0}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        group_keys = [k for k in result if k.startswith("_group:")]
        assert len(group_keys) == 1
        assert result[group_keys[0]] == "Pressure > Setpoint"
        assert "_suppress:Pressure" in result
        assert "_suppress:Setpoint" in result

    def test_tier2_paired_le(self):
        action = {"A": 5, "B": 10}
        atom_index = {
            "A": [Atom(tag="A", form="le", operand="B")],
            "B": [Atom(tag="A", form="le", operand="B")],
        }
        domain_sources = {"A": "expression_partition", "B": "expression_partition"}
        dest_tags = {"A": 5, "B": 10}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        group_keys = [k for k in result if k.startswith("_group:")]
        assert len(group_keys) == 1
        assert result[group_keys[0]] == "A <= B"

    def test_tier2_picks_satisfied_atom(self):
        """When both > and <= atoms exist, pick the one that's TRUE."""
        action = {"X": 10, "Y": 5}
        atom_index = {
            "X": [
                Atom(tag="X", form="gt", operand="Y"),
                Atom(tag="X", form="le", operand="Y"),
            ],
            "Y": [
                Atom(tag="X", form="gt", operand="Y"),
                Atom(tag="X", form="le", operand="Y"),
            ],
        }
        domain_sources = {"X": "expression_partition", "Y": "expression_partition"}
        dest_tags = {"X": 10, "Y": 5}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        group_keys = [k for k in result if k.startswith("_group:")]
        assert result[group_keys[0]] == "X > Y"

    def test_tier2_solo_tag_vs_tag(self):
        """One tag changes, partner doesn't — show the satisfied constraint."""
        action = {"Pressure": 51.0}
        atom_index = {
            "Pressure": [
                Atom(tag="Pressure", form="gt", operand="Setpoint"),
                Atom(tag="Pressure", form="le", operand="Setpoint"),
            ],
        }
        domain_sources = {"Pressure": "expression_partition"}
        dest_tags = {"Pressure": 51.0, "Setpoint": 50.0}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        assert result.get("Pressure") == "Pressure > Setpoint"

    def test_tier2_solo_unsatisfied_skipped(self):
        """Solo relational atom that is NOT satisfied in dest state → skip."""
        action = {"Pressure": 49.0}
        atom_index = {
            "Pressure": [Atom(tag="Pressure", form="gt", operand="Setpoint")],
        }
        domain_sources = {"Pressure": "expression_partition"}
        dest_tags = {"Pressure": 49.0, "Setpoint": 50.0}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        # gt is not satisfied; no other atoms → no constraint
        assert "Pressure" not in result

    def test_tier1_literal_threshold(self):
        """Tag with a literal comparison → annotated value."""
        action = {"Temp": 51}
        atom_index = {
            "Temp": [Atom(tag="Temp", form="gt", operand=50)],
        }
        domain_sources = {"Temp": "expression_partition"}
        result = _classify_step_inputs(action, atom_index, domain_sources)
        assert result["Temp"] == "Temp=51 (> 50)"

    def test_tier1_le_threshold(self):
        action = {"Level": 9}
        atom_index = {
            "Level": [Atom(tag="Level", form="le", operand=10)],
        }
        domain_sources = {"Level": "min_max"}
        result = _classify_step_inputs(action, atom_index, domain_sources)
        assert result["Level"] == "Level=9 (<= 10)"

    def test_tier1_closest_threshold(self):
        """When multiple literal thresholds exist, pick the closest."""
        action = {"Temp": 51}
        atom_index = {
            "Temp": [
                Atom(tag="Temp", form="gt", operand=50),
                Atom(tag="Temp", form="lt", operand=100),
            ],
        }
        domain_sources = {"Temp": "expression_partition"}
        result = _classify_step_inputs(action, atom_index, domain_sources)
        assert result["Temp"] == "Temp=51 (> 50)"

    def test_tier2_takes_priority_over_tier1(self):
        """If a tag is consumed by Tier 2 grouping, skip Tier 1."""
        action = {"Pressure": 51.0, "Setpoint": 50.0}
        atom_index = {
            "Pressure": [
                Atom(tag="Pressure", form="gt", operand="Setpoint"),
                Atom(tag="Pressure", form="gt", operand=10),
            ],
            "Setpoint": [
                Atom(tag="Pressure", form="gt", operand="Setpoint"),
            ],
        }
        domain_sources = {
            "Pressure": "expression_partition",
            "Setpoint": "expression_partition",
        }
        dest_tags = {"Pressure": 51.0, "Setpoint": 50.0}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        # Pressure suppressed by Tier 2 → no Tier 1 annotation
        assert "Pressure" not in result
        group_keys = [k for k in result if k.startswith("_group:")]
        assert len(group_keys) == 1

    def test_fallback_no_atoms(self):
        """Tag with no atoms → empty constraints (falls through to k=v)."""
        action = {"Unknown": 42}
        domain_sources = {"Unknown": "unknown"}
        result = _classify_step_inputs(action, {}, domain_sources)
        assert result == {}

    def test_mixed_tiers(self):
        """Step with Tier 1, Tier 2, and Tier 3 tags together."""
        action = {"Pressure": 51.0, "Setpoint": 50.0, "Temp": 75, "StartBtn": True}
        atom_index = {
            "Pressure": [Atom(tag="Pressure", form="gt", operand="Setpoint")],
            "Setpoint": [Atom(tag="Pressure", form="gt", operand="Setpoint")],
            "Temp": [Atom(tag="Temp", form="gt", operand=70)],
        }
        domain_sources = {
            "Pressure": "expression_partition",
            "Setpoint": "expression_partition",
            "Temp": "expression_partition",
            "StartBtn": "bool",
        }
        dest_tags = {"Pressure": 51.0, "Setpoint": 50.0, "Temp": 75, "StartBtn": True}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags)
        group_keys = [k for k in result if k.startswith("_group:")]
        assert len(group_keys) == 1
        assert "Temp" in result
        assert result["Temp"] == "Temp=75 (> 70)"
        assert "StartBtn" not in result  # Tier 3

    def test_no_dest_tags_falls_back(self):
        """Without dest_tags, truth evaluation is skipped — first atom wins."""
        action = {"X": 10, "Y": 5}
        atom_index = {
            "X": [Atom(tag="X", form="gt", operand="Y")],
            "Y": [Atom(tag="X", form="gt", operand="Y")],
        }
        domain_sources = {"X": "expression_partition", "Y": "expression_partition"}
        result = _classify_step_inputs(action, atom_index, domain_sources, dest_tags=None)
        group_keys = [k for k in result if k.startswith("_group:")]
        assert len(group_keys) == 1

    def test_atom_form_xic_ignored(self):
        """Non-comparison forms (xic, xio, rise, fall) are skipped."""
        action = {"Sensor": True}
        atom_index = {
            "Sensor": [Atom(tag="Sensor", form="xic")],
        }
        domain_sources = {"Sensor": "bool"}
        result = _classify_step_inputs(action, atom_index, domain_sources)
        assert result == {}


# ---------------------------------------------------------------------------
# _render_step_inputs — unit tests
# ---------------------------------------------------------------------------


class TestRenderStepInputs:
    def test_no_constraints_uses_kv(self):
        step = _step({"A": 1, "B": 2}, constraints=None)
        assert _render_step_inputs(step) == "A=1, B=2"

    def test_empty_constraints_uses_kv(self):
        step = _step({"A": 1}, constraints={})
        assert _render_step_inputs(step) == "A=1"

    def test_tier1_annotation(self):
        step = _step(
            {"Temp": 51, "Enable": True},
            constraints={"Temp": "Temp=51 (> 50)"},
        )
        result = _render_step_inputs(step)
        assert "Temp=51 (> 50)" in result
        assert "Enable=True" in result

    def test_tier2_grouped(self):
        step = _step(
            {"Pressure": 51.0, "Setpoint": 50.0, "Enable": True},
            constraints={
                "_group:Pressure,Setpoint": "Pressure > Setpoint",
                "_suppress:Pressure": "",
                "_suppress:Setpoint": "",
            },
        )
        result = _render_step_inputs(step)
        assert "Pressure > Setpoint" in result
        assert "Enable=True" in result
        assert "51.0" not in result
        assert "50.0" not in result

    def test_group_appears_before_individual_tags(self):
        step = _step(
            {"A": 1, "X": 10, "Y": 5},
            constraints={
                "_group:X,Y": "X > Y",
                "_suppress:X": "",
                "_suppress:Y": "",
            },
        )
        result = _render_step_inputs(step)
        parts = result.split(", ")
        group_idx = next(i for i, p in enumerate(parts) if "X > Y" in p)
        a_idx = next(i for i, p in enumerate(parts) if "A=" in p)
        assert group_idx < a_idx

    def test_empty_action_with_constraints(self):
        step = _step({}, constraints={})
        assert _render_step_inputs(step) == ""


# ---------------------------------------------------------------------------
# Path.__str__() integration
# ---------------------------------------------------------------------------


class TestPathStr:
    def test_path_uses_semantic_rendering(self):
        steps = (
            _step(
                {"Pressure": 51.0, "Setpoint": 50.0, "Enable": True},
                constraints={
                    "_group:Pressure,Setpoint": "Pressure > Setpoint",
                    "_suppress:Pressure": "",
                    "_suppress:Setpoint": "",
                },
            ),
        )
        path = Path(reachable=True, steps=steps, total_changes=3, total_scans=1)
        text = str(path)
        assert "Pressure > Setpoint" in text
        assert "Enable=True" in text
        assert "51.0" not in text

    def test_path_no_constraints_backward_compat(self):
        steps = (_step({"Start": True}),)
        path = Path(reachable=True, steps=steps, total_changes=1, total_scans=1)
        text = str(path)
        assert "Start=True" in text

    def test_path_wait_step(self):
        step = ReachabilityStep(
            action={}, source_key=(0,), dest_key=(1,), scans=50, constraints=None
        )
        path = Path(reachable=True, steps=(step,), total_changes=0, total_scans=50)
        assert "(wait)" in str(path)


# ---------------------------------------------------------------------------
# _enrich_atom_index — unit tests
# ---------------------------------------------------------------------------


class TestEnrichAtomIndex:
    def test_identity_copy_literal(self):
        """copy(Source, Target); Target > 50 → adds Source > 50."""
        from pyrung.core.analysis.reverse_edges import IDENTITY

        atom_index = {
            "Target": [Atom(tag="Target", form="gt", operand=50)],
        }
        reverse_edge_map = {"Source": [("Target", IDENTITY)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        source_atoms = enriched.get("Source", [])
        assert any(a.tag == "Source" and a.form == "gt" and a.operand == 50 for a in source_atoms)

    def test_identity_copy_tag_vs_tag(self):
        """copy(Source, Target); Target > Other → adds Source > Other."""
        from pyrung.core.analysis.reverse_edges import IDENTITY

        atom_index = {
            "Target": [Atom(tag="Target", form="gt", operand="Other")],
            "Other": [Atom(tag="Target", form="gt", operand="Other")],
        }
        reverse_edge_map = {"Source": [("Target", IDENTITY)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        source_atoms = enriched.get("Source", [])
        assert any(
            a.tag == "Source" and a.form == "gt" and a.operand == "Other" for a in source_atoms
        )
        # Also indexed under the operand
        other_atoms = enriched.get("Other", [])
        assert any(a.tag == "Source" and a.operand == "Other" for a in other_atoms)

    def test_calc_offset_literal(self):
        """calc(Source + 5, Target); Target > 10 → adds Source > 5."""
        atom_index = {
            "Target": [Atom(tag="Target", form="gt", operand=10)],
        }
        invert = lambda v, k=5: v - k  # noqa: E731
        reverse_edge_map = {"Source": [("Target", invert)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        source_atoms = enriched.get("Source", [])
        assert any(a.tag == "Source" and a.form == "gt" and a.operand == 5 for a in source_atoms)

    def test_calc_tag_vs_tag_skipped(self):
        """Non-identity transform with tag-vs-tag operand → not propagated."""
        atom_index = {
            "Target": [Atom(tag="Target", form="gt", operand="Other")],
            "Other": [Atom(tag="Target", form="gt", operand="Other")],
        }
        invert = lambda v: v - 5  # noqa: E731
        reverse_edge_map = {"Source": [("Target", invert)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        source_atoms = enriched.get("Source", [])
        assert not any(a.tag == "Source" for a in source_atoms)

    def test_chain_propagation(self):
        """copy(A, B); copy(B, C); C > 10 → both B and A get the atom."""
        from pyrung.core.analysis.reverse_edges import IDENTITY

        atom_index = {
            "C": [Atom(tag="C", form="gt", operand=10)],
        }
        reverse_edge_map = {
            "A": [("B", IDENTITY)],
            "B": [("C", IDENTITY)],
        }
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        a_atoms = enriched.get("A", [])
        b_atoms = enriched.get("B", [])
        assert any(a.tag == "B" and a.operand == 10 for a in b_atoms)
        assert any(a.tag == "A" and a.operand == 10 for a in a_atoms)

    def test_no_duplicate_atoms(self):
        """Enrichment doesn't add atoms that already exist on the source."""
        from pyrung.core.analysis.reverse_edges import IDENTITY

        existing_atom = Atom(tag="Source", form="gt", operand=50)
        atom_index = {
            "Source": [existing_atom],
            "Target": [Atom(tag="Target", form="gt", operand=50)],
        }
        reverse_edge_map = {"Source": [("Target", IDENTITY)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        source_atoms = [a for a in enriched["Source"] if a.form == "gt" and a.operand == 50]
        assert len(source_atoms) == 1

    def test_empty_reverse_edges(self):
        """No copy/calc → returns the original index unchanged."""
        atom_index = {"A": [Atom(tag="A", form="gt", operand=5)]}
        enriched = _enrich_atom_index(atom_index, {})
        assert enriched is atom_index

    def test_non_comparison_atoms_not_propagated(self):
        """xic/xio/truthy atoms are not propagated."""
        from pyrung.core.analysis.reverse_edges import IDENTITY

        atom_index = {
            "Target": [Atom(tag="Target", form="xic")],
        }
        reverse_edge_map = {"Source": [("Target", IDENTITY)]}
        enriched = _enrich_atom_index(atom_index, reverse_edge_map)
        assert "Source" not in enriched


# ---------------------------------------------------------------------------
# End-to-end integration: how()
# ---------------------------------------------------------------------------


class TestSemanticPathIntegration:
    """Integration tests that run how() and check the rendered path."""

    def test_bool_program_no_constraints(self):
        """Simple bool program — all Tier 3, no semantic annotations."""
        from pyrung.core import PLC, Bool, Program, Rung, latch, out

        Start = Bool("Start", external=True)
        Running = Bool("Running")
        Done = Bool("Done")
        with Program() as prog:
            with Rung(Start):
                latch(Running)
            with Rung(Running):
                out(Done)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        text = str(path)
        assert "Start=True" in text

    def test_literal_threshold_annotation(self):
        """Int tag with literal comparison → Tier 1 annotated output."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, latch, out

        Temp = Int("Temp", external=True, min=0, max=100)
        Hot = Bool("Hot")
        Alarm = Bool("Alarm")
        with Program() as prog:
            with Rung(Temp > 75):
                latch(Hot)
            with Rung(Hot):
                out(Alarm)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Alarm)
        assert path.reachable
        text = str(path)
        assert "(> 75)" in text

    def test_tag_vs_tag_constraint(self):
        """Direct tag-vs-tag comparison → Tier 2 constraint output."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Result = Bool("Result")
        with Program() as prog:
            with Rung(A > B):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A > B" in text
        # Raw values should NOT appear
        for step in path.steps:
            if step.constraints:
                assert "_suppress:A" in step.constraints or "A" in step.constraints

    def test_copy_chain_literal_threshold(self):
        """copy(Input, Copy); Copy > 50 → shows Input with threshold."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, copy, latch

        Temp = Int("Temp", external=True, min=0, max=100)
        TempCopy = Int("TempCopy")
        Hot = Bool("Hot")
        with Program() as prog:
            with Rung():
                copy(Temp, TempCopy)
            with Rung(TempCopy > 50):
                latch(Hot)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Hot)
        assert path.reachable
        text = str(path)
        assert "(> 50)" in text
        assert "Temp=" in text
        assert "TempCopy" not in text

    def test_calc_chain_threshold_inversion(self):
        """calc(Input + 10, Target); Target > 60 → Input shows (> 50)."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, latch

        Sensor = Int("Sensor", external=True, min=0, max=100)
        Adjusted = Int("Adjusted")
        Hot = Bool("Hot")
        with Program() as prog:
            with Rung():
                calc(Sensor + 10, Adjusted)
            with Rung(Adjusted > 60):
                latch(Hot)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Hot)
        assert path.reachable
        text = str(path)
        assert "(> 50)" in text
        assert "Sensor=" in text

    def test_path_replays_correctly_with_constraints(self):
        """Constraints are a rendering concern — replay still works."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Result = Bool("Result")
        with Program() as prog:
            with Rung(A > B):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable

        replay = PLC(prog, dt=0.010)
        for step in path.steps:
            replay.patch(step.action)
            for _ in range(step.scans):
                replay.step()
        assert replay.state.tags["Result"] is True

    def test_calc_subtraction_two_tags_threshold_zero(self):
        """calc(A - B, Diff); Diff > 0 → path shows A > B."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Diff = Int("Diff")
        Result = Bool("Result")
        with Program() as prog:
            with Rung():
                calc(A - B, Diff)
            with Rung(Diff > 0):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A > B" in text

    def test_calc_subtraction_reversed(self):
        """calc(B - A, Diff); Diff > 0 → path shows B > A."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Diff = Int("Diff")
        Result = Bool("Result")
        with Program() as prog:
            with Rung():
                calc(B - A, Diff)
            with Rung(Diff > 0):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A < B" in text or "B > A" in text

    def test_calc_subtraction_nonzero_threshold(self):
        """calc(A - B, Diff); Diff > 3 → path shows A - B > 3."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, latch

        A = Int("A", external=True, min=0, max=10)
        B = Int("B", external=True, min=0, max=10)
        Diff = Int("Diff")
        Result = Bool("Result")
        with Program() as prog:
            with Rung():
                calc(A - B, Diff)
            with Rung(Diff > 3):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A - B > 3" in text

    def test_calc_addition_two_tags(self):
        """calc(A + B, Sum); Sum > 8 → path shows A + B > 8."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Sum = Int("Sum")
        Result = Bool("Result")
        with Program() as prog:
            with Rung():
                calc(A + B, Sum)
            with Rung(Sum > 8):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A + B > 8" in text

    def test_calc_subtraction_chain_through_copy(self):
        """calc(A - B, Diff); copy(Diff, C); C > 0 → still shows A > B."""
        from pyrung.core import PLC, Bool, Int, Program, Rung, calc, copy, latch

        A = Int("A", external=True, min=0, max=5)
        B = Int("B", external=True, min=0, max=5)
        Diff = Int("Diff")
        C = Int("C")
        Result = Bool("Result")
        with Program() as prog:
            with Rung():
                calc(A - B, Diff)
            with Rung():
                copy(Diff, C)
            with Rung(C > 0):
                latch(Result)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Result)
        assert path.reachable
        text = str(path)
        assert "A > B" in text


# ---------------------------------------------------------------------------
# Default-value filtering in step 1
# ---------------------------------------------------------------------------


class TestDefaultFiltering:
    """_render_step_inputs filters out tags at their default values."""

    def test_defaults_filtered(self):
        step = _step({"Start": True, "Stop": False, "Speed": 0})
        defaults = {"Start": False, "Stop": False, "Speed": 0}
        result = _render_step_inputs(step, tag_defaults=defaults)
        assert result == "Start=True"

    def test_no_defaults_provided(self):
        step = _step({"Start": True, "Stop": False})
        result = _render_step_inputs(step, tag_defaults=None)
        assert "Stop=False" in result

    def test_all_at_default(self):
        step = _step({"Stop": False, "Speed": 0})
        defaults = {"Stop": False, "Speed": 0}
        result = _render_step_inputs(step, tag_defaults=defaults)
        assert result == ""

    def test_all_at_default_path_shows_wait(self):
        step = _step({"Stop": False, "Speed": 0})
        defaults = {"Stop": False, "Speed": 0}
        path = Path(
            reachable=True,
            steps=(step,),
            total_changes=2,
            total_scans=1,
            tag_defaults=defaults,
        )
        assert "(wait)" in str(path)

    def test_path_without_tag_defaults_shows_all(self):
        step = _step({"Start": True, "Stop": False})
        path = Path(
            reachable=True,
            steps=(step,),
            total_changes=2,
            total_scans=1,
        )
        text = str(path)
        assert "Start=True" in text
        assert "Stop=False" in text

    def test_constraints_path_filters_defaults(self):
        step = _step(
            action={"Pressure": 51.0, "Idle": False},
            constraints={"Pressure": "Pressure=51.0"},
        )
        defaults = {"Pressure": 0.0, "Idle": False}
        result = _render_step_inputs(step, tag_defaults=defaults)
        assert "Pressure=51.0" in result
        assert "Idle" not in result
