"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    Word,
    copy,
    latch,
    math,
    out,
    run_function,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    _classify_dimensions,
    always,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a always() counterexample trace on the concrete PLC."""
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
    """Assert that optimized and unoptimized always() agree on the result type."""
    optimized = always(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = always(
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

    def test_a_mod_of_a_whole_number_formula_lists_its_remainders(self):
        """(Step + 1) % 6 is a whole number: its remainders are -5..5 (an Int may be
        negative, and MOD takes the dividend's sign)."""
        step = Int("Step")
        with Program(strict=False) as logic:
            with Rung(step == 3):
                out(Bool("AtThree"))
            with Rung():
                math((step + 1) % 6, step)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        assert result[0]["Step"] == tuple(range(-5, 6))

    @pytest.mark.parametrize("low, domain", [(-5, (-2, -1, 0, 1, 2)), (0, (0, 1, 2))])
    def test_a_mod_takes_the_dividends_sign(self, low, domain):
        """MOD truncates (a CLICK's Math): -5 MOD 3 is -2, so a dividend that may be
        negative gives a domain below 0 too."""
        n = Int("N", external=True, min=low, max=5)
        d = Int("D")
        neg = Bool("Neg")
        with Program(strict=False) as logic:
            with Rung(d < 0):
                latch(neg)
            with Rung():
                math(n % 3, d)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful = result[0]
        assert stateful["D"] == domain
        expected = Counterexample if low < 0 else prove_module.Proven
        assert isinstance(always(logic, ~neg, max_states=5000), expected)

    def test_a_mod_into_a_word_stores_a_negative_remainder_wrapped(self):
        """-2 MOD 3 into a Word (a decimal Math, an Int operand) is stored as 65534."""
        n = Int("N", external=True, min=-5, max=5)
        w = Word("W")
        high = Bool("High")
        with Program(strict=False) as logic:
            with Rung(w > 2):
                latch(high)
            with Rung():
                math(n % 3, w)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        assert result[0]["W"] == (0, 1, 2, 65534, 65535)
        assert isinstance(always(logic, ~high, max_states=5000), Counterexample)

    def test_a_power_that_may_be_zero_to_a_negative_is_not_bounded_by_its_corners(self):
        """0 ** -1 is +Inf, which a Dint stores as 0.  So the corners of N ** E run from 0 to
        +Inf, and the domain of D must still hold 2 (2 ** 1)."""
        two_base, plus_one = Bool("TwoBase", external=True), Bool("PlusOne", external=True)
        n, e, d = Int("N"), Int("E"), Dint("D")
        big = Bool("Big")
        with Program(strict=False) as logic:
            with Rung(two_base):
                copy(2, n)
            with Rung(~two_base):
                copy(0, n)
            with Rung(plus_one):
                copy(1, e)
            with Rung(~plus_one):
                copy(-1, e)
            with Rung():
                math(n**e, d)
            with Rung(d > 1):
                latch(big)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        assert 2 in result[0]["D"]
        assert isinstance(always(logic, ~big, max_states=5000), Counterexample)

    @staticmethod
    def _real_mod_program():
        r = Real("R", external=True, min=-5.0, max=5.0)
        x = Real("X")
        hi = Bool("Hi")
        with Program(strict=False) as logic:
            with Rung():
                math(r % 3, x)
            with Rung(x > 2.2):
                latch(hi)
        return logic, hi

    def test_a_mod_of_a_real_is_not_listed_as_whole_remainders(self):
        """2.5 MOD 3 is 2.5: a Real dividend's remainder is not one of -2..2, so
        X keeps a value on each side of its compare with 2.2."""
        logic, _hi = self._real_mod_program()
        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        assert any(value > 2.2 for value in result[0]["X"])

    @pytest.mark.xfail(
        strict=True,
        reason="a Real input with whole-number min/max is searched at its whole numbers "
        "(_declared_domain), so R = 2.5 is never tried: the search answers Intractable, and "
        "a Counterexample needs a Real's fractions modelled through %",
    )
    def test_a_mod_of_a_real_reaches_its_fraction(self):
        logic, hi = self._real_mod_program()
        assert isinstance(always(logic, ~hi, max_states=5000), Counterexample)

    def test_a_mod_of_a_real_is_not_proven(self):
        """R = 2.5 latches Hi, so whatever the search tries, it must not answer Proven."""
        logic, hi = self._real_mod_program()
        plc = PLC(logic, dt=0.010)
        plc.patch({"R": 2.5})
        plc.step()
        assert plc.current_state.tags["Hi"] is True
        assert not isinstance(always(logic, ~hi, max_states=5000), Proven)

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
        """Pure-switch eq/ne inputs collapse to {literals..., one other value}."""
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
        # The other value is the power-on default, a value the PLC can hold.
        assert 0 in nd["Mode"]

    def test_eq_ne_other_value_skips_literals_and_bounds(self):
        """When the default is a literal, the other value is the nearest unused one."""
        mode = Int("Mode", external=True, min=0)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(mode == 0):
                out(light)
            with Rung(mode != 1):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert nd["Mode"] == (0, 1, 2)

    def test_eq_ne_only_closure_rejects_data_flow_usage(self):
        """Arithmetic/data-flow usage keeps eq/ne-only closure from applying."""
        step = Int("Step", external=True)
        odd = Int("Odd")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                math(step % 2, odd)
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

        result = always(logic, ~alarm)
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

    def test_real_whole_number_bounds_uses_integer_range(self):
        """Real with whole-number bounds still gets integer range (no regression)."""
        temp = Real("Temp", external=True, min=0.0, max=10.0)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(temp > 5):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Temp"]
        assert 0 in domain
        assert 10 in domain
        assert 5 in domain
        assert 6 in domain

    def test_real_fractional_bounds_with_comparison(self):
        """Real with fractional bounds and comparison gets partition domain."""
        temp = Real("Temp", external=True, min=0.5, max=99.5)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(temp > 50.0):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Temp"]
        assert 0.5 in domain
        assert 99.5 in domain
        assert 50.0 in domain
        assert all(0.5 <= v <= 99.5 for v in domain)

    def test_real_fractional_bounds_no_comparison(self):
        """Real with fractional bounds and no comparison seeds min/max, not None."""
        from pyrung.core.analysis.prove.classify import _extract_value_domain
        from pyrung.core.analysis.simplified import Atom

        tag = Real("Temp", external=True, min=0.5, max=99.5)
        atoms = [Atom(tag="Temp", form="truthy")]
        domain = _extract_value_domain("Temp", tag, all_exprs=[], atom_index={"Temp": atoms})
        assert domain is not None
        assert len(domain) >= 2
        assert 0.5 in domain
        assert 99.5 in domain

    def test_real_large_fractional_range_with_comparison(self):
        """Real with large fractional range + comparison gets small partition domain."""
        pressure = Real("Pressure", external=True, min=0.5, max=5000.5)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(pressure > 3000.0):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Pressure"]
        assert len(domain) < 20
        assert 0.5 in domain
        assert 5000.5 in domain

    def test_char_literal_write_domain(self):
        """Char tag written by string-literal copies merges write + comparison values."""
        from pyrung.core import Char, Timer, on_delay

        State = Char("State")
        GreenTimer = Timer.clone("GreenTimer")

        with Program(strict=False) as logic:
            with Rung(State == "g"):
                on_delay(GreenTimer, 3000)
            with Rung(GreenTimer.Done):
                copy("y", State)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), f"got: {result.reason}"
        stateful, _nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "State" in stateful
        # "y" from the copy, "g" from the comparison, "\x00" from the default
        assert "y" in stateful["State"]
        assert "g" in stateful["State"]
        assert "\x00" in stateful["State"]

    def test_char_eq_ne_domain_extraction(self):
        """Char tag compared with string literals uses eq/ne domain closure."""
        from pyrung.core import Char

        State = Char("State", external=True)
        green_out = Bool("GreenOut")
        red_out = Bool("RedOut")

        with Program(strict=False) as logic:
            with Rung(State == "g"):
                out(green_out)
            with Rung(State == "r"):
                out(red_out)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), f"got: {result.reason}"
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "State" in nd
        domain = nd["State"]
        assert "g" in domain
        assert "r" in domain
        assert len(domain) == 3  # "g", "r", plus OTHER sentinel

    def test_char_state_machine_full_domain(self):
        """Char-based state machine with multiple copy targets has full domain."""
        from pyrung.core import Char, Timer, on_delay

        State = Char("State")
        T1 = Timer.clone("T1")
        T2 = Timer.clone("T2")
        T3 = Timer.clone("T3")

        with Program(strict=False) as logic:
            with Rung(State == "g"):
                on_delay(T1, 3000)
            with Rung(T1.Done):
                copy("y", State)
            with Rung(State == "y"):
                on_delay(T2, 1000)
            with Rung(T2.Done):
                copy("r", State)
            with Rung(State == "r"):
                on_delay(T3, 3000)
            with Rung(T3.Done):
                copy("g", State)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), f"got: {result.reason}"
        stateful, _nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "State" in stateful
        domain = stateful["State"]
        assert set(domain) >= {"g", "y", "r", "\x00"}

    def test_char_literal_not_confused_with_tag_name(self):
        """String literal 'g' in comparison is not confused with a tag named 'g'."""
        from pyrung.core import Char
        from pyrung.core.analysis.prove.classify import _extract_value_domain
        from pyrung.core.analysis.simplified import Atom

        tag = Char("Mode", external=True)
        atoms = [
            Atom(tag="Mode", form="eq", operand="a"),
            Atom(tag="Mode", form="eq", operand="b"),
        ]
        domain = _extract_value_domain(
            "Mode", tag, all_exprs=[], atom_index={"Mode": atoms}, all_tags={"Mode": tag}
        )
        assert domain is not None
        assert "a" in domain
        assert "b" in domain


def test_literal_domain_counts_a_write_past_the_block_cap():
    """``copy(Val, DS[WPtr])`` can land on DS7, so DS7's writes are not all literals."""
    from pyrung.core import Block, TagType
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import _collect_literal_write_domains

    go, put = Bool("Go", external=True), Bool("Put", external=True)
    sel = Int("Sel", external=True, choices={0: "a", 1: "b"})
    val = Int("Val", external=True, choices={0: "zero", 9: "nine"})
    wptr = Int("WPtr")
    ds = Block("DS", TagType.INT, 1, 4500)

    with Program(strict=False) as logic:
        with Rung(go):
            copy(1, ds[7])
        with Rung():
            math(sel + 6, wptr)
        with Rung(put):
            copy(val, ds[wptr])

    graph = build_program_graph(logic, unbounded_index_reads=True)
    domains = _collect_literal_write_domains(logic, graph.tags, graph.indirect_reach)

    assert "DS7" not in domains


# A REAL past 2**53 has no value one away: ``1e20 + 1 == 1e20``.
LARGE_REALS = [
    pytest.param(lambda r: r > 1e20, id="above 1e20"),
    pytest.param(lambda r: r > 1e16, id="above 1e16"),
    pytest.param(lambda r: r < -1e30, id="below -1e30"),
    pytest.param(lambda r: r > 3.4e38, id="above 3.4e38"),
]


def _fires(logic: Program, trace: list[TraceStep], name: str) -> bool:
    return bool(_replay_trace(logic, trace).current_state.tags[name])


@pytest.mark.parametrize("compare", LARGE_REALS)
def test_a_large_real_compared_with_a_literal_has_values_past_it(compare) -> None:
    level, hit = Real("BigR", external=True), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(compare(level)):
            out(hit)
    result = always(logic, ~hit)
    assert isinstance(result, Counterexample), result
    assert _fires(logic, result.trace, "Hit")


@pytest.mark.parametrize("compare", LARGE_REALS)
def test_a_large_real_carried_back_through_a_copy_has_values_past_it(compare) -> None:
    level, copied, hit = Real("BigR", external=True), Real("BigCopy"), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung():
            copy(level, copied)
        with Rung(compare(copied)):
            out(hit)
    result = always(logic, ~hit)
    assert isinstance(result, Counterexample), result
    assert _fires(logic, result.trace, "Hit")


def test_two_large_reals_compared_with_each_other_past_a_literal() -> None:
    """Both above 1e20 and one above the other: two floats past the point."""
    pv, sp, hit = Real("BigPV", external=True), Real("BigSP", external=True), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(sp > 1e20, pv > 1e20, pv > sp):
            out(hit)
    result = always(logic, ~hit)
    assert isinstance(result, Counterexample), result
    assert _fires(logic, result.trace, "Hit")
