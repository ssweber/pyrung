"""Comparisons against a block element indexed by an expression (``Tbl[Q + 0]``).

``Tbl[Q + 0] == S`` and ``S == Tbl[Q + 0]`` used to compare the tag value with
the unresolved reference object in the interpreted runtime (always unequal),
while the compiled kernel resolved it; ``Tbl[Q + 0] == 370`` built a plain
``bool``. Every form must now behave like ``Tbl[Q]`` in the interpreted
runtime, the compiled kernel (legacy and blockless) and the generated
CircuitPython code.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks, LadderExportError, TagMap, pyrung_to_ladder
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Rung,
    TagType,
    calc,
    copy,
    out,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.condition import (
    CompareEq,
    Condition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
)
from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ScanContext
from pyrung.core.memory_block import IndirectExprRef
from pyrung.core.state import SystemState
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source

_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}

_INDIRECT_CLASS = {
    "eq": IndirectCompareEq,
    "ne": IndirectCompareNe,
    "lt": IndirectCompareLt,
    "le": IndirectCompareLe,
    "gt": IndirectCompareGt,
    "ge": IndirectCompareGe,
}

# Python reflects ``a < b`` to ``b > a`` when ``a`` declines.
_REFLECTED = {"eq": "eq", "ne": "ne", "lt": "gt", "le": "ge", "gt": "lt", "ge": "le"}

# Table contents and pointers, written by the program's first rung.
_Q = 37  # Tbl[Q + 0] -> Tbl[37]
_R = 36  # Tbl[R] -> Tbl[36], Tbl[R + 1] -> Tbl[37]


def _tbl(i: int) -> int:
    return i * 10


def _build(s_value: int) -> tuple[Program, dict[str, bool], dict[str, Int]]:
    """One rung per (form, operator) driving its own coil, plus copy/calc reads."""
    S = Int("S")
    Q = Int("Q")
    R = Int("R")
    D = Int("D")
    E = Int("E")
    F = Int("F")
    Tbl = Block("Tbl", TagType.INT, 1, 50)

    ref = _tbl(_Q)  # value of Tbl[Q + 0]
    ptr = _tbl(_R)  # value of Tbl[R]
    forms: dict[str, tuple[Callable[[], Any], Callable[[], Any], int, int]] = {
        # name: (left, right, left value, right value)
        "ref_lit": (lambda: Tbl[Q + 0], lambda: s_value, ref, s_value),
        "lit_ref": (lambda: s_value, lambda: Tbl[Q + 0], s_value, ref),
        "ref_tag": (lambda: Tbl[Q + 0], lambda: S, ref, s_value),
        "tag_ref": (lambda: S, lambda: Tbl[Q + 0], s_value, ref),
        "ref_ref": (lambda: Tbl[Q + 0], lambda: Tbl[R + 1], ref, ref),
        "ref_ref_lower": (lambda: Tbl[Q + 0], lambda: Tbl[R + 0], ref, ptr),
        "ref_ptr": (lambda: Tbl[Q + 0], lambda: Tbl[R], ref, ptr),
        "ptr_ref": (lambda: Tbl[R], lambda: Tbl[Q + 0], ptr, ref),
        "expr_ref": (lambda: S + 1, lambda: Tbl[Q + 0], s_value + 1, ref),
        "ref_expr": (lambda: Tbl[Q + 0], lambda: S + 1, ref, s_value + 1),
        "expr_ptr": (lambda: S - 10, lambda: Tbl[R], s_value - 10, ptr),
    }

    expected: dict[str, bool] = {}
    coils: dict[str, tuple[Any, Any]] = {}
    for form, (left, right, left_value, right_value) in forms.items():
        for op_name, fn in _OPS.items():
            name = f"{form}_{op_name}"
            coils[name] = (fn(left(), right()), Bool(name))
            expected[name] = bool(fn(left_value, right_value))

    with Program(strict=False) as prog:
        with Rung():
            for i in range(1, 51):
                copy(_tbl(i), Tbl[i])
            copy(s_value, S)
            copy(_Q, Q)
            copy(_R, R)
            copy(Tbl[Q + 0], D)
            calc(Tbl[R + 1], E)
            copy(Tbl[Q + 0], Tbl[R + 2])
            copy(Tbl[R + 2], F)
        for condition, coil in coils.values():
            with Rung(condition):
                out(coil)

    return prog, expected, {"D": D, "E": E, "F": F}


_EXPECTED_READS = {"D": _tbl(_Q), "E": _tbl(_Q), "F": _tbl(_Q)}


@pytest.fixture(params=[_tbl(_Q) - 10, _tbl(_Q), _tbl(_Q) + 10], ids=["below", "equal", "above"])
def program_case(request) -> tuple[Program, dict[str, bool], dict[str, Int]]:
    return _build(request.param)


def _first_scan_tags(prog: Program, engine: str) -> dict[str, Any]:
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        runner.step()
        return dict(runner.current_state.tags)
    compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
    kernel = compiled.create_kernel()
    _step_compiled_kernel(compiled, kernel, dt=0.010)
    return dict(kernel.tags)


@pytest.mark.parametrize("engine", ["interpreted", "compiled", "kernel_legacy", "kernel_blockless"])
def test_expression_indexed_compares_match_python(program_case, engine: str) -> None:
    prog, expected, reads = program_case
    tags = _first_scan_tags(prog, engine)

    actual = {name: bool(tags[name]) for name in expected}
    assert actual == expected
    assert {name: tags[name] for name in reads} == _EXPECTED_READS


class _StubBase:
    def rollCall(self, modules):
        return None

    def readDiscrete(self, slot):
        return 0

    def writeDiscrete(self, value, slot):
        return None

    def readAnalog(self, slot, ch):
        return 0

    def writeAnalog(self, value, slot, ch):
        return None

    def readTemperature(self, slot, ch):
        return 0.0


def test_expression_indexed_compares_match_in_circuitpy(program_case, monkeypatch) -> None:
    prog, expected, reads = program_case
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None)

    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )
    ctx = _context_for_program(prog, hw)

    actual = {name: bool(namespace[ctx.symbol_for_tag(Bool(name))]) for name in expected}
    assert actual == expected
    assert {
        name: namespace[ctx.symbol_for_tag(tag)] for name, tag in reads.items()
    } == _EXPECTED_READS

    # The interpreted runtime agrees with the generated code coil for coil.
    interpreted = _first_scan_tags(prog, "interpreted")
    assert {name: bool(interpreted[name]) for name in expected} == actual


@pytest.mark.parametrize("op_name", list(_OPS))
def test_expression_indexed_comparisons_build_conditions(op_name: str) -> None:
    fn = _OPS[op_name]
    Tbl = Block("Tbl", TagType.INT, 1, 50)
    Q = Int("Q")
    S = Int("S")

    for condition in (fn(Tbl[Q + 0], 370), fn(Tbl[Q + 0], S), fn(Tbl[Q + 0], S + 1)):
        assert isinstance(condition, _INDIRECT_CLASS[op_name])
        assert isinstance(condition.indirect_ref, IndirectExprRef)

    # A literal or expression on the left declines, so Python reflects the
    # comparison onto the indexed element.
    for condition in (fn(370, Tbl[Q + 0]), fn(S + 1, Tbl[Q + 0]), fn(S + 1, Tbl[Q])):
        assert isinstance(condition, _INDIRECT_CLASS[_REFLECTED[op_name]])

    # A tag on the left keeps its own comparison and resolves the element as its value.
    assert isinstance(fn(S, Tbl[Q + 0]), Condition)


def test_original_repro_with_patched_pointer() -> None:
    Tbl = Block("Tbl", TagType.INT, 1, 50)
    S = Int("S")
    Q = Int("Q")
    D = Int("D")
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")

    with Program(strict=False) as prog:
        with Rung():
            for i in range(1, 51):
                copy(i * 10, Tbl[i])
            copy(370, S)
        with Rung(Tbl[Q + 0] == S):
            out(A)
        with Rung(S == Tbl[Q + 0]):
            out(B)
        with Rung(S == Tbl[Q]):
            out(C)
        with Rung():
            copy(Tbl[Q + 0], D)

    for runner_cls in (PLC, CompiledPLC):
        runner = runner_cls(prog, dt=0.010)
        runner.patch({"Q": 37})
        runner.step()
        tags = runner.current_state.tags
        assert (tags["A"], tags["B"], tags["C"], tags["D"]) == (True, True, True, 370)


def test_tag_compared_with_expression_index_resolves_the_element() -> None:
    Tbl = Block("Tbl", TagType.INT, 1, 50)
    S = Int("S")
    Q = Int("Q")
    condition = S == Tbl[Q + 1]
    assert isinstance(condition, CompareEq)

    state = SystemState().with_tags({"S": 370, "Q": 36, "Tbl37": 370})
    assert condition.evaluate(ScanContext(state)) is True
    state = state.with_tags({"Tbl37": 360})
    assert condition.evaluate(ScanContext(state)) is False


def test_condition_trace_names_the_expression_index() -> None:
    Tbl = Block(
        "Tbl",
        TagType.INT,
        1,
        50,
        address_formatter=lambda name, addr: f"{name}[{addr}]",
    )
    Q = Int("Q")
    S = Int("S")
    condition = Tbl[Q + 1] == S

    state = SystemState().with_tags({"Q": 36, "S": 370, "Tbl[37]": 370})
    engine = ConditionTraceEngine()
    value, details = engine.evaluate(condition, ScanContext(state))

    assert value is True
    detail_map = {item["name"]: item["value"] for item in details}
    assert detail_map["left"] == "Tbl[37]"
    assert detail_map["left_value"] == 370
    assert detail_map["left_pointer_expr"] == "Tbl[Q + 1]"
    assert "left_pointer" not in detail_map
    assert engine.expression(condition) == "Tbl[Q + 1] == S"


def test_click_export_rejects_expression_index_in_comparison() -> None:
    _x, y, *_rest = ClickBlocks()
    ds = ClickBlocks().ds
    idx = Int("idx")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(ds[idx + 1] == 5):
            out(Light)

    mapping = TagMap({idx: ds[100], Light: y[1]}, include_system=False)
    with pytest.raises(LadderExportError, match="CLK_PTR_EXPR_NOT_ALLOWED"):
        pyrung_to_ladder(logic, mapping)
