"""PILOT on programs that use range aggregates (max / min / median / mean) in calc()."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Block, Bool, Int, Program, Rung, TagType, calc, copy, out


def _program(kind: str):
    ext_a = Int("PilotAgg_ExtA", external=True, choices={0: "zero", 3: "low", 7: "high"})
    ext_b = Int("PilotAgg_ExtB", external=True, choices={0: "zero", 2: "low", 9: "high"})
    vals = Block("PilotAggVals", TagType.INT, 1, 2)
    result = Int("PilotAgg_Result")
    hit = Bool("PilotAgg_Hit")
    target = {"max": 9, "min": 3, "median": 8, "mean": 8}[kind]
    with Program(strict=False) as logic:
        with Rung():
            copy(ext_a, vals[1])
            copy(ext_b, vals[2])
        with Rung():
            calc(getattr(vals.select(1, 2), kind)(), result)
        with Rung(result == target):
            out(hit)
    return logic, hit


def test_prover_context_survives_aggregates() -> None:
    """An aggregate the prover cannot absorb would silently strip every how() of its context."""
    from pyrung.core.analysis.pilot.drive_setup import build_prover_context

    logic, _hit = _program("median")
    plc = PLC(logic, dt=0.010)
    plc.step()
    prover = build_prover_context(logic, dict(plc.state.tags))
    assert prover.key_config is not None
    assert prover.nd_domains is not None
    assert {"PilotAgg_ExtA", "PilotAgg_ExtB"} <= set(prover.nd_domains)


@pytest.mark.parametrize("kind", ["max", "min", "median", "mean"])
def test_how_reaches_through_aggregate(kind: str) -> None:
    logic, hit = _program(kind)
    path = PLC(logic, dt=0.010).how(hit, max_scans=50)
    assert path.reachable, str(path)
