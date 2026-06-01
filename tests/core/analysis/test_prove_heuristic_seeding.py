"""Tests for heuristic domain seeding in explore()."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Real, Rung, calc, latch
from pyrung.core.analysis.graph import TransitionGraph
from pyrung.core.analysis.prove import Intractable, _OptConfig, explore


class TestHeuristicSeedingBasic:
    """Heuristic seeding recovers otherwise-Intractable programs."""

    def test_tag_to_tag_real_without_heuristic_is_intractable(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = explore(logic, _opt_config=_OptConfig(heuristic_domain_seeding=False))
        assert isinstance(result, Intractable)

    def test_tag_to_tag_real_with_heuristic_produces_graph(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = explore(logic)
        assert isinstance(result, TransitionGraph)
        assert result.state_count >= 2

    def test_explore_enables_heuristic_by_default(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        graph = explore(logic)
        assert isinstance(graph, TransitionGraph)

        intractable = explore(logic, _opt_config=_OptConfig(heuristic_domain_seeding=False))
        assert isinstance(intractable, Intractable)

    def test_tag_to_tag_int_with_heuristic_produces_graph(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        result = explore(logic)
        assert isinstance(result, TransitionGraph)
        assert result.state_count >= 2


class TestBisectionFindsThresholds:
    """Behavioral bisection discovers comparison thresholds."""

    def test_tag_to_tag_real_finds_both_outcomes(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        graph = explore(logic)
        assert isinstance(graph, TransitionGraph)

        alarm_values = {graph.state_tags(k).get("Alarm") for k in graph._state_tags}
        assert True in alarm_values, "bisection should discover temp > setpoint case"
        assert False in alarm_values, "bisection should discover temp <= setpoint case"

    def test_tag_to_tag_int_finds_both_outcomes(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        graph = explore(logic)
        assert isinstance(graph, TransitionGraph)

        above_values = {graph.state_tags(k).get("Above") for k in graph._state_tags}
        assert True in above_values
        assert False in above_values

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

        result = explore(logic)
        assert isinstance(result, TransitionGraph)


class TestCrossInputBisection:
    """Bisection accounts for interactions between multiple ND inputs."""

    def test_two_infeasible_real_inputs(self):
        temp = Real("Temperature", external=True)
        setpoint = Real("Setpoint", external=True)
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(temp > setpoint):
                latch(alarm)

        result = explore(logic)
        assert isinstance(result, TransitionGraph)

        alarm_values = {result.state_tags(k).get("Alarm") for k in result._state_tags}
        assert True in alarm_values
        assert False in alarm_values

    def test_two_infeasible_int_inputs(self):
        actual = Int("Actual", external=True)
        target = Int("Target", external=True)
        above = Bool("Above")
        with Program() as logic:
            with Rung(actual > target):
                latch(above)

        result = explore(logic)
        assert isinstance(result, TransitionGraph)

        above_values = {result.state_tags(k).get("Above") for k in result._state_tags}
        assert True in above_values
        assert False in above_values


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

        result = explore(logic)
        assert isinstance(result, TransitionGraph)

        done_seen = any(result.state_tags(k).get("Done") is True for k in result._state_tags)
        assert done_seen, "trace should observe count reaching 5"


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

        result = explore(logic)
        assert isinstance(result, TransitionGraph)

        alarm_values = {result.state_tags(k).get("Alarm") for k in result._state_tags}
        assert True in alarm_values
        assert False in alarm_values

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

        result = explore(logic)
        assert isinstance(result, TransitionGraph)
        assert result.state_count >= 3

    def test_unwritten_tag_in_comparison(self):
        """An unwritten, non-external tag used in a comparison gets cross-seeded."""
        pv = Real("PV", external=True)
        threshold = Real("Threshold")
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(pv > threshold):
                latch(alarm)

        result = explore(logic)
        assert isinstance(result, TransitionGraph)

        alarm_values = {result.state_tags(k).get("Alarm") for k in result._state_tags}
        assert True in alarm_values
        assert False in alarm_values


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

        result = explore(logic)
        assert isinstance(result, TransitionGraph)


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
