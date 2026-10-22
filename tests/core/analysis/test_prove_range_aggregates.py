"""Prover coverage for range aggregates (sum / max / min / median / mean) inside calc()."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, And, Block, Bool, Int, Program, Rung, TagType, calc, copy, out
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, TraceStep, always
from pyrung.core.analysis.prove.classify import _expr_reads_tag
from pyrung.core.expression import sqrt


def _replay(program: Program, trace: list[TraceStep]) -> PLC:
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


_TARGETS = {"max": 9, "min": 4, "median": 7, "mean": 7}
_HIGH = {"max": 9, "min": 5, "median": 7, "mean": 7}


def _program(kind: str):
    ext_a = Int("ExtA", external=True, choices={3: "low", 7: "high"})
    ext_b = Int("ExtB", external=True, choices={2: "low", 9: "high"})
    ext_c = Int("ExtC", external=True, choices={4: "low", 5: "high"})
    ds = Block("DS", TagType.INT, 1, 3)
    result = Int("Result")
    hit = Bool("Hit")
    with Program(strict=False) as logic:
        with Rung():
            copy(ext_a, ds[1])
            copy(ext_b, ds[2])
            copy(ext_c, ds[3])
        with Rung():
            calc(getattr(ds.select(1, 3), kind)(), result)
        with Rung(result == _TARGETS[kind]):
            out(hit)
    return logic, result, hit


class TestExprReadsTag:
    def test_sees_range_members(self):
        """The self-feed check must see members read through sum and aggregates."""
        ds = Block("DS", TagType.INT, 1, 4)
        assert _expr_reads_tag(ds.select(1, 3).sum() + 1, "DS2")
        assert _expr_reads_tag(ds.select(1, 3).max(), "DS3")
        assert not _expr_reads_tag(ds.select(1, 3).median(), "DS4")

    def test_sees_math_function_operands(self):
        x = Int("X")
        assert _expr_reads_tag(sqrt(x + 1), "X")


@pytest.mark.parametrize("kind", ["max", "min", "median", "mean"])
def test_aggregate_value_reached(kind):
    """Every aggregate value reachable on the concrete PLC is found by the prover."""
    logic, _result, hit = _program(kind)
    verdict = always(logic, ~hit, max_states=10_000, depth_budget=20)
    assert isinstance(verdict, Counterexample), verdict
    plc = _replay(logic, verdict.trace)
    assert plc.current_state.tags["Hit"] is True


@pytest.mark.parametrize("kind", ["max", "min", "median", "mean"])
def test_aggregate_stays_in_member_hull(kind):
    """The result never leaves what the members can produce (defaults included)."""
    logic, result, _hit = _program(kind)
    verdict = always(logic, And(result >= 0, result <= _HIGH[kind]), max_states=10_000)
    if isinstance(verdict, Intractable):
        pytest.skip("intractable")
    assert isinstance(verdict, Proven), verdict
    above = always(logic, result < _HIGH[kind], max_states=10_000)
    assert isinstance(above, Counterexample), above
