"""A block element indexed outside its block (``Tbl[P]``, ``Tbl[Q // Z]``).

In a rung condition such an element reads as 0 (the type's zero), with no
fault flag and the scan carrying on: the same rule as any other expression
outside ``calc``. ``copy()`` and ``calc()`` with an out-of-range source set
``fault.address_error`` and write nothing. Every form must behave the same in
the interpreted runtime, the compiled kernel (legacy and blockless) and the
generated CircuitPython code.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.circuitpy import P1AM, generate_circuitpy
from pyrung.circuitpy.codegen import compile_kernel
from pyrung.click import ClickBlocks, TagMap
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    calc,
    copy,
    latch,
    out,
)
from pyrung.core.analysis.prove import Counterexample, Proven, never
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel
from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ScanContext
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.state import SystemState
from pyrung.core.storage import BLANK_CHAR
from tests.circuitpy.test_codegen import _context_for_program, _run_single_scan_source

_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}

# ``circuitpy_rt`` splits the generated code into ``code.py`` and ``pyrung_rt.py``,
# which ``code.py`` imports its helpers from.
_ENGINES = [
    "interpreted",
    "compiled",
    "kernel_legacy",
    "kernel_blockless",
    "circuitpy",
    "circuitpy_rt",
]

_IN_RANGE = 3  # Tbl[3] == 30


def _tbl(i: int) -> int:
    return i * 10


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


def _first_scan(prog: Program, engine: str, tags: dict[str, Any], monkeypatch) -> dict[str, Any]:
    """The named tags after one scan of *prog* on *engine*."""
    if engine in ("interpreted", "compiled"):
        runner = (PLC if engine == "interpreted" else CompiledPLC)(prog, dt=0.010)
        runner.step()
        state = runner.current_state.tags
        return {name: state[name] for name in tags}
    if engine in ("kernel_legacy", "kernel_blockless"):
        compiled = compile_kernel(prog, blockless=engine == "kernel_blockless")
        kernel = compiled.create_kernel()
        _step_compiled_kernel(compiled, kernel, dt=0.010)
        return {name: kernel.tags[name] for name in tags}
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    split = engine == "circuitpy_rt"
    result = generate_circuitpy(prog, hw, target_scan_ms=10.0, runstop=None, force_runtime=split)
    assert bool(result.runtime) is split
    namespace = _run_single_scan_source(
        result.code, monkeypatch, _StubBase(), runtime_source=result.runtime
    )
    ctx = _context_for_program(prog, hw)
    return {name: namespace[ctx.symbol_for_tag(tag)] for name, tag in tags.items()}


def _build_compares(s_value: int) -> tuple[Program, dict[str, bool], dict[str, Any]]:
    """One rung per (index, form, operator) driving its own coil."""
    Tbl = Block("Tbl", TagType.INT, 1, 10)
    P = Int("P")  # 50: past the end
    N = Int("N")  # 0: below the start
    Q = Int("Q")  # 5
    Z = Int("Z")  # 0: Q // Z divides by zero, reads address 0
    K = Int("K")  # _IN_RANGE
    S = Int("S")
    Last = Bool("Last")

    indexes: dict[str, Callable[[], Any]] = {
        "ptr_high": lambda: Tbl[P],
        "ptr_low": lambda: Tbl[N],
        "expr_high": lambda: Tbl[Q + 100],
        "expr_div0": lambda: Tbl[Q // Z],
    }
    expected: dict[str, bool] = {}
    coils: dict[str, tuple[Any, Any]] = {}
    for index_name, elem in indexes.items():
        forms: dict[str, tuple[Callable[[], Any], Callable[[], Any], int, int]] = {
            "ref_lit": (elem, lambda: s_value, 0, s_value),
            "lit_ref": (lambda: s_value, elem, s_value, 0),
            "ref_tag": (elem, lambda: S, 0, s_value),
            "tag_ref": (lambda: S, elem, s_value, 0),
            "ref_ref": (elem, elem, 0, 0),
            "ref_in": (elem, lambda: Tbl[K], 0, _tbl(_IN_RANGE)),
            "in_ref": (lambda: Tbl[K + 0], elem, _tbl(_IN_RANGE), 0),
        }
        for form, (left, right, left_value, right_value) in forms.items():
            for op_name, fn in _OPS.items():
                name = f"{index_name}_{form}_{op_name}"
                coils[name] = (fn(left(), right()), Bool(name))
                expected[name] = bool(fn(left_value, right_value))

    with Program(strict=False) as prog:
        with Rung():
            for i in range(1, 11):
                copy(_tbl(i), Tbl[i])
            copy(s_value, S)
            copy(50, P)
            copy(0, N)
            copy(5, Q)
            copy(0, Z)
            copy(_IN_RANGE, K)
        for condition, coil in coils.values():
            with Rung(condition):
                out(coil)
        with Rung():
            out(Last)

    tags: dict[str, Any] = {name: coil for name, (_cond, coil) in coils.items()}
    tags["Last"] = Last
    tags["fault.address_error"] = Bool("fault.address_error")
    return prog, expected, tags


@pytest.mark.parametrize("s_value", [-5, 0, 5], ids=["below", "zero", "above"])
@pytest.mark.parametrize("engine", _ENGINES)
def test_out_of_range_element_in_condition_reads_zero(
    engine: str, s_value: int, monkeypatch
) -> None:
    prog, expected, tags = _build_compares(s_value)
    actual = _first_scan(prog, engine, tags, monkeypatch)

    assert {name: bool(actual[name]) for name in expected} == expected
    # No fault flag, and the scan runs on to the last rung.
    assert bool(actual["fault.address_error"]) is False
    assert bool(actual["Last"]) is True


@pytest.mark.parametrize("engine", _ENGINES)
def test_sparse_gap_and_real_and_char_blocks_read_zero(engine: str, monkeypatch) -> None:
    Sparse = Block("Sp", TagType.INT, 1, 10, valid_ranges=((1, 3), (7, 10)))
    Reals = Block("Rl", TagType.REAL, 1, 4)
    Chars = Block("Ch", TagType.CHAR, 1, 4)
    G = Int("G")  # 5: a gap in Sp
    H = Int("H")  # 9: past the end of Rl and Ch
    InGap = Bool("InGap")
    GapNonzero = Bool("GapNonzero")
    RealZero = Bool("RealZero")
    RealBelow = Bool("RealBelow")
    CharBlank = Bool("CharBlank")
    Tail = Bool("Tail")

    with Program(strict=False) as prog:
        with Rung():
            copy(11, Sparse[1])
            copy(77, Sparse[7])
            copy(1.5, Reals[1])
            copy("x", Chars[1])
            copy(5, G)
            copy(9, H)
        with Rung(Sparse[G] == 0):
            out(InGap)
        with Rung(Sparse[G + 0] != 0):
            out(GapNonzero)
        with Rung(Reals[H] == 0.0):
            out(RealZero)
        with Rung(Reals[H + 0] < 0.5):
            out(RealBelow)
        with Rung(Chars[H] == BLANK_CHAR):  # a blank Char
            out(CharBlank)
        with Rung():
            out(Tail)

    tags = {
        tag.name: tag
        for tag in (
            InGap,
            GapNonzero,
            RealZero,
            RealBelow,
            CharBlank,
            Tail,
            Bool("fault.address_error"),
        )
    }
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert {name: bool(value) for name, value in actual.items()} == {
        "InGap": True,
        "GapNonzero": False,
        "RealZero": True,
        "RealBelow": True,
        "CharBlank": True,
        "Tail": True,
        "fault.address_error": False,
    }


@pytest.mark.parametrize("engine", _ENGINES)
def test_zero_based_block_reads_zero_below_its_start(engine: str, monkeypatch) -> None:
    Zb = Block("Zb", TagType.INT, 0, 4)
    Flags = Block("Fl", TagType.BOOL, 0, 3)
    M = Int("M")  # -1: below address 0
    K = Int("K")  # 0: the first element
    Below = Bool("Below")
    BelowExpr = Bool("BelowExpr")
    First = Bool("First")
    BoolBelow = Bool("BoolBelow")
    Tail = Bool("Tail")

    with Program(strict=False) as prog:
        with Rung():
            for i in range(0, 5):
                copy(i + 1, Zb[i])
            latch(Flags[0])
            copy(-1, M)
            copy(0, K)
        with Rung(Zb[M] == 0):
            out(Below)
        with Rung(Zb[K - 1] < Zb[K]):
            out(BelowExpr)
        with Rung(Zb[K] == 1):
            out(First)
        with Rung(Flags[M] == False):  # noqa: E712 - builds a condition
            out(BoolBelow)
        with Rung():
            out(Tail)

    tags = {
        tag.name: tag
        for tag in (Below, BelowExpr, First, BoolBelow, Tail, Bool("fault.address_error"))
    }
    actual = _first_scan(prog, engine, tags, monkeypatch)
    assert {name: bool(value) for name, value in actual.items()} == {
        "Below": True,
        "BelowExpr": True,
        "First": True,
        "BoolBelow": True,
        "Tail": True,
        "fault.address_error": False,
    }


@pytest.mark.parametrize("via", ["window", "bank"])
@pytest.mark.parametrize("engine", _ENGINES)
def test_bank_aliased_block_reads_zero_outside_range(
    engine: str, via: str, monkeypatch, request
) -> None:
    # Tl is a named window onto DS101..DS105, one register per tag. The
    # compiled code indexes one memory through one of the two, never both.
    ds = ClickBlocks().ds
    Tl = Block("Tl", TagType.INT, 1, 5)
    TagMap({Tl: ds.select(101, 105)}, include_system=False)
    P = Int("P")
    Q = Int("Q")
    coils = {name: Bool(name) for name in ("Past", "In", "ExprPast", "Tail")}

    with Program(strict=False) as prog:
        with Rung():
            for i in range(1, 6):
                copy(i * 10, Tl[i])
            copy(10, ds[106])
            if via == "window":
                copy(6, P)  # past Tl's end; DS106 is in DS, not in Tl
                copy(3, Q)  # Tl3 is DS103
            else:
                copy(4600, P)  # past DS's end
                copy(103, Q)
        if via == "window":
            past, inside, expr_past = Tl[P] == 0, Tl[Q] == 30, Tl[Q + 3] == 0
        else:
            past, inside, expr_past = ds[P] == 0, ds[Q] == 30, ds[P - 1] == 0
        with Rung(past):
            out(coils["Past"])
        with Rung(inside):
            out(coils["In"])
        with Rung(expr_past):
            out(coils["ExprPast"])
        with Rung():
            out(coils["Tail"])

    if engine == "kernel_blockless" and via == "window":
        # Pre-existing, on the base too: the blockless kernel refuses any
        # indirect access through a bank-aliased window (an instruction's as
        # well), as if the bank were indexed too.
        request.applymarker(
            pytest.mark.xfail(
                raises=ValueError,
                strict=True,
                reason="blockless kernel refuses indexing a bank-aliased window",
            )
        )
    tags: dict[str, Any] = dict(coils)
    tags["fault.address_error"] = Bool("fault.address_error")
    actual = _first_scan(prog, engine, tags, monkeypatch)
    expected = dict.fromkeys(coils, True)
    expected["fault.address_error"] = False
    assert {name: bool(value) for name, value in actual.items()} == expected


def _build_reads() -> tuple[Program, dict[str, Any]]:
    Tbl = Block("Tbl", TagType.INT, 1, 10)
    P = Int("P")
    Q = Int("Q")
    Z = Int("Z")
    names = ("D_copy_ptr", "D_copy_expr", "D_calc_ptr", "D_calc_expr", "D_calc_div0", "D_calc_sum")
    dests = {name: Int(name) for name in names}

    with Program(strict=False) as prog:
        with Rung():
            for i in range(1, 11):
                copy(_tbl(i), Tbl[i])
            copy(50, P)
            copy(5, Q)
            copy(0, Z)
            for dest in dests.values():
                copy(7, dest)
        with Rung():
            copy(Tbl[P], dests["D_copy_ptr"])
            copy(Tbl[Q + 100], dests["D_copy_expr"])
            calc(Tbl[P], dests["D_calc_ptr"])
            calc(Tbl[Q + 100], dests["D_calc_expr"])
            calc(Tbl[Q // Z], dests["D_calc_div0"])
            calc(Tbl[Q + 0], dests["D_calc_sum"])

    tags: dict[str, Any] = dict(dests)
    tags["fault.address_error"] = Bool("fault.address_error")
    tags["fault.division_error"] = Bool("fault.division_error")
    return prog, tags


@pytest.mark.parametrize("engine", _ENGINES)
def test_copy_and_calc_out_of_range_source_set_address_error(engine: str, monkeypatch) -> None:
    prog, tags = _build_reads()
    actual = _first_scan(prog, engine, tags, monkeypatch)

    # Out of range: nothing written; in range: the element. A division by zero
    # in calc's own index is calc's division error (0 and its flag), as any
    # division in calc is.
    assert {name: actual[name] for name in tags if name.startswith("D_")} == {
        "D_copy_ptr": 7,
        "D_copy_expr": 7,
        "D_calc_ptr": 7,
        "D_calc_expr": 7,
        "D_calc_div0": 0,
        "D_calc_sum": _tbl(5),
    }
    assert bool(actual["fault.address_error"]) is True
    assert bool(actual["fault.division_error"]) is True


def test_calc_fault_writes_name_address_error_only_for_indexed_sources() -> None:
    Tbl = Block("Tbl", TagType.INT, 1, 10)
    P = Int("P")
    Q = Int("Q")
    D = Int("D")
    R = Real("R")

    def fault_writes(instruction: CalcInstruction) -> tuple[str, ...]:
        return tuple(instruction._fault_writes)

    plain = fault_writes(CalcInstruction(Q + 1, D))
    assert "fault.address_error" not in plain
    for source in (Tbl[P], Tbl[Q + 100]):
        writes = fault_writes(CalcInstruction(source, D))
        assert "fault.address_error" in writes
        assert set(plain) <= set(writes)
    assert "fault.address_error" not in fault_writes(CalcInstruction(Q * 2, R))


def test_condition_trace_marks_out_of_range_element() -> None:
    Tbl = Block("Tbl", TagType.INT, 1, 10)
    P = Int("P")
    Q = Int("Q")
    S = Int("S")
    state = SystemState().with_tags({"P": 50, "Q": 5, "S": 0})
    engine = ConditionTraceEngine()

    value, details = engine.evaluate(Tbl[P] == S, ScanContext(state))
    assert value is True
    detail_map = {item["name"]: item["value"] for item in details}
    assert detail_map["left"] == "Tbl50 (out of range)"
    assert detail_map["left_value"] == 0

    value, details = engine.evaluate(S == Tbl[Q + 100], ScanContext(state))
    assert value is True
    detail_map = {item["name"]: item["value"] for item in details}
    assert detail_map["right"] == "Tbl105 (out of range)"
    assert detail_map["right_value"] == 0


def _prove_program(
    cond: Callable[[Block, Int, Int], Any], pointer_max: int
) -> tuple[Program, Bool]:
    Tbl = Block("KTbl", TagType.INT, 1, 10)
    P = Int("KP", external=True, min=1, max=pointer_max, default=1)
    S = Int("KS", external=True, min=0, max=5, default=1)
    A = Bool("KA")
    with Program(strict=False) as logic:
        with Rung():
            for i in range(1, 11):
                copy(_tbl(i), Tbl[i])
        with Rung(cond(Tbl, P, S)):
            latch(A)
    return logic, A


_PROVE_CONDS: dict[str, Callable[[Block, Int, Int], Any]] = {
    "ptr": lambda Tbl, P, S: Tbl[P] == 0,
    "expr": lambda Tbl, P, S: Tbl[P + 0] == 0,
    "tag_left": lambda Tbl, P, S: S == Tbl[P],
    "lt": lambda Tbl, P, S: Tbl[P] < 5,
}


@pytest.mark.parametrize("form", list(_PROVE_CONDS))
def test_never_finds_a_rung_true_only_out_of_range(form: str) -> None:
    # Every element in 1..10 is at least 10, so only an address past the end
    # (reading 0) makes the rung true.
    logic, A = _prove_program(_PROVE_CONDS[form], pointer_max=20)
    assert isinstance(never(logic, A), Counterexample)


@pytest.mark.parametrize("form", list(_PROVE_CONDS))
def test_never_proves_a_rung_false_over_an_in_range_pointer(form: str) -> None:
    logic, A = _prove_program(_PROVE_CONDS[form], pointer_max=10)
    assert isinstance(never(logic, A), Proven)
