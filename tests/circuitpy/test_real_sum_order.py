"""A Real range sum adds plain left to right in the generated code, as in the engines."""

from __future__ import annotations

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.core import PLC, Block, CompiledPLC, Program, Real, Rung, TagType, calc
from tests.circuitpy.test_codegen import _run_single_scan_source
from tests.core.test_indirect_expr_compare import _StubBase

VALUES = [1e16, 1.0, 1.0, -1e16]


def _program() -> Program:
    block = Block("RsB", TagType.REAL, 1, 4)
    for i, value in enumerate(VALUES, start=1):
        block.slot(i, default=value)
    with Program(strict=False) as logic:
        with Rung():
            calc(block.select(1, 4).sum(), Real("RsOut"))
    return logic


def test_real_sum_matches_across_core_kernel_and_generated_code(monkeypatch: pytest.MonkeyPatch):
    logic = _program()
    outs = []
    for runner in (PLC(logic), CompiledPLC(logic)):
        runner.step()
        outs.append(runner.current_state.tags["RsOut"])
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(logic, hw, target_scan_ms=10.0, runstop=None)
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )
    outs.append(namespace["_t_RsOut"])
    assert outs == [0.0, 0.0, 0.0]
