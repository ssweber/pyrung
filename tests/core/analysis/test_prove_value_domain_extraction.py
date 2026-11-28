"""Test the static facts that how() steers with."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    TagType,
    Word,
    copy,
    math,
    out,
    run_function,
    rung,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions
from pyrung.core.expression import LiteralExpr

# ===================================================================
# Group 2: Value domain extraction
# ===================================================================


class TestValueDomainExtraction:
    def test_bool_domain(self):
        """Bool tags always have domain (False, True)."""
        flag = Bool("Flag", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(flag):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert nd["Flag"] == (False, True)

    def test_a_mod_of_a_whole_number_formula_lists_its_remainders(self):
        """List the remainders of (Step + 1) % 6, which is a whole number, as -5..5.

        An Int may be negative, and MOD takes the dividend's sign.
        """
        step = Int("Step")
        with Program(strict=False) as logic:
            with rung(step == 3):
                out(Bool("AtThree"))
            with rung():
                math((step + 1) % 6, step)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        assert result[0]["Step"] == tuple(range(-5, 6))

    def test_integer_comparison_literals(self):
        """Pure equality-only inputs collapse to {literals..., OTHER}."""
        state = Int("State", external=True)
        out_a = Bool("OutA")
        out_b = Bool("OutB")

        with Program(strict=False) as logic:
            with rung(state == 1):
                out(out_a)
            with rung(state == 2):
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
            with rung(mode == 1):
                out(light)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert set(nd["Mode"]) == {0, 1, 2}

    def test_eq_ne_only_input_gets_literal_plus_other_domain(self):
        """Collapse the domain of a pure-switch eq/ne input to {literals..., one other value}."""
        mode = Int("Mode", external=True)
        light = Bool("Light")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with rung(mode == 1):
                out(light)
            with rung(mode != 2):
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
        """Use the nearest unused value as the other value when the default is a literal."""
        mode = Int("Mode", external=True, min=0)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with rung(mode == 0):
                out(light)
            with rung(mode != 1):
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
            with rung():
                math(step % 2, odd)
            with rung(step == 5):
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
            with rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with rung(result_tag == 1):
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
            with rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with rung():
                copy(result_tag, Int("Stored"))
            with rung(result_tag > 3):
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
            with rung(level > 5):
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
            with rung(level):
                out(target)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

    def test_boundary_partition_includes_min_max_anchors(self):
        """Boundary partition always includes min and max values."""
        level = Int("Level", external=True, min=0, max=50)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with rung(level > 25):
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
            with rung(level > 0):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        domain = nd["Level"]
        assert all(0 <= v <= 100 for v in domain)

    def test_multiple_comparisons_boundary_partition(self):
        """Multiple comparison literals produce compact boundary domain."""
        level = Int("Level", external=True, min=0, max=100)
        low = Bool("Low")
        high = Bool("High")

        with Program(strict=False) as logic:
            with rung(level > 5):
                out(low)
            with rung(level > 20):
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
            with rung(temp > 5):
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
            with rung(temp > 50.0):
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
            with rung(pressure > 3000.0):
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
            with rung(State == "g"):
                on_delay(GreenTimer, 3000)
            with rung(GreenTimer.Done):
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
            with rung(State == "g"):
                out(green_out)
            with rung(State == "r"):
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
            with rung(State == "g"):
                on_delay(T1, 3000)
            with rung(T1.Done):
                copy("y", State)
            with rung(State == "y"):
                on_delay(T2, 1000)
            with rung(T2.Done):
                copy("r", State)
            with rung(State == "r"):
                on_delay(T3, 3000)
            with rung(T3.Done):
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
    """Count ``copy(Val, DS[WPtr])`` as a write that can land on DS7.

    DS7's writes are then not all literals.
    """
    from pyrung.core import Block, TagType
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import (
        _collect_all_exprs,
        _collect_structural_domains,
        collect_produced_value_domains,
    )

    go, put = Bool("Go", external=True), Bool("Put", external=True)
    sel = Int("Sel", external=True, choices={0: "a", 1: "b"})
    val = Int("Val", external=True, choices={0: "zero", 9: "nine"})
    wptr = Int("WPtr")
    ds = Block("DS", TagType.INT, 1, 4500)

    with Program(strict=False) as logic:
        with rung(go):
            copy(1, ds[7])
        with rung():
            math(sel + 6, wptr)
        with rung(put):
            copy(val, ds[wptr])

    graph = build_program_graph(logic)
    domains = _collect_structural_domains(logic, graph, _collect_all_exprs(logic, graph))

    assert "DS7" not in domains
    assert "DS7" not in collect_produced_value_domains(logic, graph)


# ===================================================================
# The values a Math formula can store
# ===================================================================


def _picked(go_a: Bool, go_b: Bool, tag: Int | Word, a: int, b: int, c: int) -> None:
    """Write *a* into *tag* when *go_a* is on, *b* when only *go_b* is on, and *c* otherwise."""
    with rung(go_a):
        copy(a, tag)
    with rung(~go_a, go_b):
        copy(b, tag)
    with rung(~go_a, ~go_b):
        copy(c, tag)


def _decimal_xor() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, d3 = Int("A"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, 2, 4, 5)
        with rung():
            math(a ^ 1, d3)
        with rung(d3 == 5):
            out(Bool("Seen"))
    return logic


def _hex_or_of_a_difference() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    d1, d2, d3 = Word("D1"), Word("D2"), Word("D3")
    with Program(strict=False) as logic:
        with rung(go_a):
            copy(65000, d1)
        with rung(~go_a):
            copy(0, d1)
        with rung(go_b):
            copy(2, d2)
        with rung(~go_b):
            copy(0, d2)
        with rung():
            math((d1 - d2) | 0, d3)
        with rung(d3 == 64998):
            out(Bool("Seen"))
    return logic


def _decimal_and_of_a_negative() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, d3 = Int("A"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, -3, 1, 2)
        with rung():
            math(a & 6, d3)
    return logic


def _shift_of_a_negative() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, n, d3 = Int("A"), Int("N"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, -7, 7, 0)
        _picked(go_b, go_a, n, 2, 0, 1)
        with rung():
            math((a << n) + (a >> n), d3)
    return logic


def _wide_shift() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    n, d3 = Int("N"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, n, 2000, 3, 0)
        with rung():
            math(1 << n, d3)
    return logic


def _wide_power() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    n, d3 = Int("N"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, n, 2000, 3, 0)
        with rung():
            math(2**n, d3)
    return logic


def _power_of_a_negative() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, d3 = Int("A"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, -2, 1, 2)
        with rung():
            math(a**2, d3)
    return logic


def _odd_power_of_a_negative() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, d3 = Int("A"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, -2, 1, 2)
        with rung():
            math(a**3, d3)
    return logic


def _power_past_2_53() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    n, d3 = Int("N"), Dint("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, n, 1, 2, 3)
        with rung():
            math(LiteralExpr(10) ** LiteralExpr(23), d3)
    return logic


def _floor_division_by_a_span_through_zero() -> Program:
    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    d, d3 = Int("D"), Int("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, d, -10, 1, 10)
        with rung():
            math(10 // d, d3)
    return logic


_STORED_VALUE_PROGRAMS = {
    "decimal_xor": _decimal_xor,
    "hex_or_of_a_difference": _hex_or_of_a_difference,
    "decimal_and_of_a_negative": _decimal_and_of_a_negative,
    "shift_of_a_negative": _shift_of_a_negative,
    "wide_shift": _wide_shift,
    "wide_power": _wide_power,
    "power_of_a_negative": _power_of_a_negative,
    "odd_power_of_a_negative": _odd_power_of_a_negative,
    "power_past_2_53": _power_past_2_53,
    "floor_division_by_a_span_through_zero": _floor_division_by_a_span_through_zero,
}


@pytest.mark.parametrize("build", _STORED_VALUE_PROGRAMS.values(), ids=_STORED_VALUE_PROGRAMS)
def test_each_math_domain_holds_every_value_a_scan_stores(build):
    """Step each pair of inputs and check that every domain of D3 holds what the scan stored.

    The validators read the produced domains, and how() reads the structural ones.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import (
        _collect_all_exprs,
        _collect_structural_domains,
        collect_produced_value_domains,
    )

    logic = build()
    graph = build_program_graph(logic)
    domains = (
        collect_produced_value_domains(logic, graph),
        _collect_structural_domains(logic, graph, _collect_all_exprs(logic, graph)),
    )
    for go_a in (False, True):
        for go_b in (False, True):
            plc = PLC(logic)
            plc.patch({"GoA": go_a, "GoB": go_b})
            plc.step()
            stored = plc.current_state.tags["D3"]
            for domain in domains:
                if "D3" in domain:
                    assert stored in domain["D3"], (go_a, go_b, stored, domain["D3"])


def test_a_power_past_2_53_has_the_low_bits_of_the_nearest_double():
    """List -167772160 for ``10 ^ 23`` into a Dint, and check that a scan stores it.

    The nearest double to 10 ** 23 has those low 32 bits.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import collect_produced_value_domains

    logic = _power_past_2_53()
    domains = collect_produced_value_domains(logic, build_program_graph(logic))
    assert -167772160 in domains["D3"]
    plc = PLC(logic)
    plc.step()
    assert plc.current_state.tags["D3"] == -167772160


def _one_value(tag_type: type, name: str, value: int) -> Any:
    return tag_type(name, choices={value: "a"}, default=value, external=True)


def _mean_of_ints(tag_type: TagType = TagType.DINT, value: int = 10) -> Any:
    from pyrung.core import Block

    block = Block("B", tag_type, 1, 2)
    for addr in (1, 2):
        block.slot(addr, default=value, choices={value: "a"}, external=True)
    return block.select(1, 2).mean()


_SINGLE_VALUE_POWERS = {
    "dint_10_to_23": (lambda: _one_value(Dint, "A", 10) ** 23, Dint),
    "real_10_to_23": (lambda: _one_value(Real, "R", 10) ** 23, Dint),
    "mean_of_10s_to_23": (lambda: _mean_of_ints() ** 23, Dint),
    "int_minus_1779_to_5": (lambda: _one_value(Int, "A", -1779) ** 5, Dint),
    "int_minus_1972_to_6_into_int": (lambda: _one_value(Int, "A", -1972) ** 6, Int),
}


@pytest.mark.parametrize("make, dest_type", _SINGLE_VALUE_POWERS.values(), ids=_SINGLE_VALUE_POWERS)
def test_a_power_of_single_values_holds_what_a_scan_stores(make, dest_type):
    """List what a scan stores for a power past 2 ** 53 of one base and one exponent.

    A scan rounds the exact power when both operands are ints.  It calls ``pow()`` when
    either is a float, as a Real or a ``mean()`` is, even when the value is 10.  On Windows
    ``pow()`` can be one double away from the exact power.  So the domain lists one or
    two values.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import (
        _collect_all_exprs,
        _collect_structural_domains,
        collect_produced_value_domains,
    )

    with Program(strict=False) as logic:
        power = make()
        with rung():
            math(power, dest_type("D3"))
    graph = build_program_graph(logic)
    plc = PLC(logic)
    plc.step()
    stored = plc.current_state.tags["D3"]
    produced = collect_produced_value_domains(logic, graph)
    structural = _collect_structural_domains(logic, graph, _collect_all_exprs(logic, graph))
    assert stored in structural["D3"]
    assert len(structural["D3"]) <= 2
    assert stored in produced.get("D3", (stored,))


_BIG = 2**20 + 1

#: Formulas with an int step past 2 ** 53, and the value a scan stores for each.  The
#: value is None where the scan calls ``pow()``, whose last bit depends on the platform.
_STEPS_PAST_2_53 = {
    "dint_product_of_three": (
        lambda: (
            _one_value(Dint, "A", _BIG) * _one_value(Dint, "B", _BIG) * _one_value(Dint, "C", _BIG)
        ),
        Dint,
        3145728,
    ),
    "dint_product_with_a_constant": (
        lambda: (
            LiteralExpr(2**31 - 1) * _one_value(Dint, "A", 2**31 - 1) * _one_value(Dint, "B", 3)
        ),
        Dint,
        0,
    ),
    "hex_word_3_to_35": (lambda: _one_value(Word, "A", 3) ** 35, Word, 38808),
    "hex_word_3_to_41": (lambda: _one_value(Word, "A", 3) ** 41, Word, 49152),
    "hex_mean_of_191s_to_7": (lambda: _mean_of_ints(TagType.WORD, 191) ** 7, Word, None),
}


@pytest.mark.parametrize(
    "make, dest_type, expected", _STEPS_PAST_2_53.values(), ids=_STEPS_PAST_2_53
)
def test_a_step_past_2_53_holds_the_rounded_value_a_scan_stores(make, dest_type, expected):
    """Check that each domain of D3 lists what a scan stores for a step past 2 ** 53.

    A Math rounds each int step past 2 ** 53 to the nearest double.  The exact step
    has other low bits, so a domain built from it leaves out the stored value.  A
    ``mean()`` returns a float, so a hex Math calls ``pow()`` on it.  On Windows
    ``pow()`` can be one double away from the rounded exact power.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import (
        _collect_all_exprs,
        _collect_structural_domains,
        collect_produced_value_domains,
    )
    from pyrung.core.compiled_plc import CompiledPLC

    with Program(strict=False) as logic:
        formula = make()
        with rung():
            math(formula, dest_type("D3"))
    graph = build_program_graph(logic)
    stored = set()
    for runner in (PLC(logic), CompiledPLC(logic)):
        runner.step()
        stored.add(runner.current_state.tags["D3"])
    assert len(stored) == 1
    if expected is not None:
        assert stored == {expected}
    produced = collect_produced_value_domains(logic, graph)
    structural = _collect_structural_domains(logic, graph, _collect_all_exprs(logic, graph))
    assert stored <= set(structural["D3"])
    if expected is None:
        # The validators list no domain for a mean(), and no domain leaves nothing out.
        assert stored <= set(produced.get("D3", stored))
    else:
        assert stored <= set(produced["D3"])


def test_check_keeps_a_comparison_with_a_value_an_xor_stores():
    """``math(A ^ 1, D3)`` with A = 4 stores 5, so ``D3 == 5`` is not always false.

    The corners of A in 2..5 give 3 and 4, and 5 lies past them.
    """
    logic = _decimal_xor()
    assert not logic.check(select={"CMP_ALWAYS_FALSE"})
    plc = PLC(logic)
    plc.patch({"GoA": False, "GoB": True})
    plc.step()
    assert plc.current_state.tags["D3"] == 5


def test_check_keeps_a_comparison_with_a_value_a_hex_or_stores():
    """A hex OR keeps 16 bits, so ``(65000 - 2) | 0`` stores 64998.

    The difference also reaches -2, whose 16 bits are 65534.  A bound taken from the
    corners was 65000..65534, and that left out 64998.
    """
    logic = _hex_or_of_a_difference()
    assert not logic.check(select={"CMP_ALWAYS_FALSE"})
    plc = PLC(logic)
    plc.patch({"GoA": True, "GoB": True})
    plc.step()
    assert plc.current_state.tags["D3"] == 64998


def test_a_hex_or_of_values_that_fit_keeps_a_small_domain():
    """List 0..3 for a hex ``A | B`` with A and B in 0..3.

    The hex Math cuts the OR to 16 bits, and that cut keeps every value that fits.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import collect_produced_value_domains

    go_a, go_b = Bool("GoA", external=True), Bool("GoB", external=True)
    a, b, d3 = Word("A"), Word("B"), Word("D3")
    with Program(strict=False) as logic:
        _picked(go_a, go_b, a, 1, 3, 0)
        _picked(go_b, go_a, b, 2, 0, 1)
        with rung():
            math(a | b, d3)

    domains = collect_produced_value_domains(logic, build_program_graph(logic))
    assert domains["D3"] == (0, 1, 2, 3)


def test_a_whole_power_of_a_negative_keeps_a_domain():
    """List -8..8 for ``A ** 3`` and 0..4 for ``A ** 2``, with A in -2..2."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.classify import collect_produced_value_domains

    for build, values in (
        (_odd_power_of_a_negative, range(-8, 9)),
        (_power_of_a_negative, range(5)),
    ):
        logic = build()
        domains = collect_produced_value_domains(logic, build_program_graph(logic))
        assert domains["D3"] == tuple(values)
