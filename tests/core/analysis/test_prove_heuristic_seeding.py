"""Tests for heuristic domain seeding in how()."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Real, Rung, calc, latch
from pyrung.core.analysis.prove import Intractable, _build_explore_context, _OptConfig
from pyrung.core.runner import PLC


class TestHeuristicSeedingBasic:
    """Heuristic seeding recovers otherwise-Intractable programs."""

    def test_tag_to_tag_real_without_heuristic_is_intractable(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = _build_explore_context(
            logic, _opt_config=_OptConfig(heuristic_domain_seeding=False)
        )
        assert isinstance(result, Intractable)

    def test_tag_to_tag_real_with_heuristic_is_reachable(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        path = PLC(logic).how(alarm)
        assert path.reachable

    def test_tag_to_tag_int_with_heuristic_is_reachable(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        path = PLC(logic).how(above)
        assert path.reachable


class TestBisectionFindsThresholds:
    """Behavioral bisection discovers comparison thresholds."""

    def test_tag_to_tag_real_finds_both_outcomes(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        plc = PLC(logic)
        assert plc.how(alarm).reachable
        assert plc.how(~alarm).reachable

    def test_tag_to_tag_int_finds_both_outcomes(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        plc = PLC(logic)
        assert plc.how(above).reachable
        assert plc.how(~above).reachable

    def test_calc_derived_comparison(self):
        """Tag-to-tag through a calc — no literal for expression stack."""
        raw = Int("RawInput", external=True, min=0, max=200)
        offset = Int("Offset", external=True, min=-50, max=50)
        scaled = Int("Scaled")
        high = Bool("High")
        with Program() as logic:
            with Rung():
                calc(raw + offset, scaled)
            with Rung(scaled > 100):
                latch(high)

        path = PLC(logic).how(high)
        assert path.reachable


class TestCrossInputBisection:
    """Bisection accounts for interactions between multiple ND inputs."""

    def test_two_infeasible_real_inputs(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        plc = PLC(logic)
        assert plc.how(alarm).reachable
        assert plc.how(~alarm).reachable

    def test_two_infeasible_int_inputs(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        plc = PLC(logic)
        assert plc.how(above).reachable
        assert plc.how(~above).reachable


class TestStatefulTraceObservation:
    """Trace observation discovers domains for stateful tags."""

    def test_stateful_calc_accumulator(self):
        enable = Bool("Enable", external=True)
        count = Int("Count")
        done = Bool("Done")
        with Program() as logic:
            with Rung(enable, count < 5):
                calc(count + 1, count)
            with Rung(count >= 5):
                latch(done)

        path = PLC(logic).how(done)
        assert path.reachable


class TestComparisonPartnerCrossSeeding:
    """Cross-seeding breaks the chicken-and-egg dependency for tag-vs-tag pairs."""

    def test_calc_chain_unbounded_intermediary(self):
        """Tag-vs-tag through calc — no literal, no bounds on either side."""
        pv = Real("PV", external=True)
        sp = Real("Setpoint", external=True)
        band = Real("Band", external=True)
        upper = Real("Upper")
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung():
                calc(sp + band, upper)
            with Rung(pv >= upper):
                latch(alarm)

        plc = PLC(logic)
        assert plc.how(alarm).reachable
        assert plc.how(~alarm).reachable

    def test_cascading_comparisons(self):
        """A > B > C — three unbounded Reals in a chain of comparisons."""
        a = Real("A", external=True)
        b = Real("B", external=True)
        c = Real("C", external=True)
        ab = Bool("A_gt_B")
        bc = Bool("B_gt_C")
        with Program() as logic:
            with Rung(a > b):
                latch(ab)
            with Rung(b > c):
                latch(bc)

        plc = PLC(logic)
        assert plc.how(ab).reachable
        assert plc.how(bc).reachable

    def test_unwritten_tag_in_comparison(self):
        """An unwritten, non-external tag used in a comparison gets cross-seeded."""
        pv = Real("PV", external=True)
        threshold = Real("Threshold")
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(pv > threshold):
                latch(alarm)

        plc = PLC(logic)
        assert plc.how(alarm).reachable
        assert plc.how(~alarm).reachable


class TestPostElisionSeeding:
    """Tags discovered infeasible during elision get seeded."""

    def test_observer_cone_tag_gets_seeded(self):
        """A tag in an observer influence cone that only becomes infeasible
        during elision should still get a heuristic domain."""
        trigger = Bool("Trigger", external=True)
        level = Real("Level", external=True)
        threshold = Real("Threshold", external=True)
        scan_local = Int("ScanLocal")
        output = Bool("Output")
        with Program() as logic:
            with Rung(trigger, level > threshold):
                calc(1, scan_local)
            with Rung(scan_local == 1):
                latch(output)

        path = PLC(logic).how(output)
        assert path.reachable


class TestSnapshotCenteredProbes:
    """Snapshot values (when non-default) inject neighbor probes for bisection."""

    def test_snapshot_probes_generate_neighbors_for_real(self):
        from pyrung.core.analysis.prove.seeding import _snapshot_probes

        tag = Real("Temp", external=True)
        probes = _snapshot_probes(tag, 5.0)
        assert 5.0 in probes
        assert any(p > 5.0 for p in probes)
        assert any(p < 5.0 for p in probes)

    def test_snapshot_probes_generate_neighbors_for_int(self):
        from pyrung.core.analysis.prove.seeding import _snapshot_probes

        tag = Int("Count", external=True)
        probes = _snapshot_probes(tag, 42)
        assert 42 in probes
        assert 43 in probes
        assert 41 in probes

    def test_bisection_skips_snapshot_probes_when_at_default(self):
        """_seed_nd_via_bisection only injects snapshot probes for non-default values."""
        from pyrung.core.analysis.prove.seeding import _initial_probes

        tag = Real("Temp", external=True)
        base_count = len(_initial_probes(tag))

        tag2 = Real("Temp2", external=True)
        base_count2 = len(_initial_probes(tag2))
        assert base_count == base_count2


class TestComparisonDomainExpansion:
    """After bisection, comparison-aware expansion adds partner-derived values."""

    def test_expand_adds_partner_values(self):
        from pyrung.core.analysis.prove.seeding import _expand_comparison_domains
        from pyrung.core.analysis.simplified import Atom

        pv = Real("PV", external=True)
        sp = Real("SP", external=True)
        exprs = [Atom(tag="PV", form="ge", operand="SP")]

        tags = {"PV": pv, "SP": sp}
        discovered = {"PV": (0.5,), "SP": (0.0,)}
        _expand_comparison_domains(tags, exprs, discovered)

        pv_domain = discovered["PV"]
        assert any(v < 0.0 for v in pv_domain), "PV should have values below SP=0.0"
        assert any(v > 0.0 for v in pv_domain), "PV should have values above SP=0.0"

        sp_domain = discovered["SP"]
        assert any(v < 0.5 for v in sp_domain), "SP should have values below PV=0.5"
        assert any(v > 0.5 for v in sp_domain), "SP should have values above PV=0.5"

    def test_expand_skips_non_comparison_atoms(self):
        from pyrung.core.analysis.prove.seeding import _expand_comparison_domains
        from pyrung.core.analysis.simplified import Atom

        pv = Real("PV", external=True)
        exprs = [Atom(tag="PV", form="gt", operand=10.0)]

        tags = {"PV": pv}
        discovered = {"PV": (5.0,)}
        _expand_comparison_domains(tags, exprs, discovered)
        assert discovered["PV"] == (5.0,), "tag-vs-literal should not trigger expansion"

    def test_int_comparison_expansion_clamps(self):
        from pyrung.core.analysis.prove.seeding import _expand_comparison_domains
        from pyrung.core.analysis.simplified import Atom

        a = Int("A", external=True)
        b = Int("B", external=True)
        exprs = [Atom(tag="A", form="gt", operand="B")]

        tags = {"A": a, "B": b}
        discovered = {"A": (0,), "B": (32767,)}
        _expand_comparison_domains(tags, exprs, discovered)

        a_domain = discovered["A"]
        assert all(-32768 <= v <= 32767 for v in a_domain), "values must stay in INT range"
        assert 32767 in a_domain, "A should include B's value 32767"
        assert 32766 in a_domain, "A should include 32767-1"


class TestProveNotAffected:
    """Heuristic seeding must not affect always/reachable_states."""

    def test_always_does_not_use_heuristic(self):
        from pyrung.core.analysis.prove import always

        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = always(logic, ~alarm)
        assert isinstance(result, Intractable)

    def test_reachable_states_does_not_use_heuristic(self):
        from pyrung.core.analysis.prove import reachable_states

        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = reachable_states(logic, project=["Alarm"])
        assert isinstance(result, Intractable)
