"""Property tests: range aggregates and ``randint()``.

Aggregates (``sum``/``max``/``min``/``median``/``mean`` over a block range) are
checked against exact ``Fraction`` arithmetic, including how each destination
type stores the result (the docs' "calc wraps" and "an Int truncates toward
zero"), the CLICK rule for an even-count integer median (hardware-recorded in
``tests/conformance/math/test_math.py``) and the division rules (a zero divisor
stores 0 and sets ``division_error``).  Both backends must match the model.

``randint()`` draws stay inside the inclusive bounds (reversed bounds are
swapped), are the same for the same site and scan on any runner, whether
stepped, run in a long stretch or replayed from history, and a ``patch()``
of the site's tag picks the draw.
"""

from __future__ import annotations

import functools
import math as pymath
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    math,
    randint,
    system,
)

pytestmark = pytest.mark.hypothesis

SETTINGS = settings(max_examples=150, deadline=None, derandomize=True, database=None)

KINDS = ("sum", "max", "min", "median", "mean")
RANGES = ((1, 1), (2, 3), (3, 7), (1, 12))
SIZE = 12
Go = Bool("AgGo")


# --- stored value per destination -----------------------------------------------


def _trunc(value: Fraction) -> int:
    return pymath.trunc(value)


def _wrap(value: int, bits: int, signed: bool) -> int:
    value %= 1 << bits
    return value - (1 << bits) if signed and value >= 1 << (bits - 1) else value


def aggregate(kind: str, values: list, *, integer: bool) -> Fraction:
    """The exact value of an aggregate (calc stores it per destination)."""
    exact = [Fraction(v) for v in values]
    if kind == "sum":
        return sum(exact, Fraction(0))
    if kind == "max":
        return max(exact)
    if kind == "min":
        return min(exact)
    if kind == "mean":
        return sum(exact, Fraction(0)) / len(exact)
    ordered = sorted(exact)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    pair = ordered[mid - 1] + ordered[mid]
    # CLICK: MDN over integers is an integer, the middle two's mean truncated
    # toward zero; over Reals it keeps the fraction.
    return Fraction(_trunc(pair / 2)) if integer else pair / 2


# --- the fixed programs (built once; examples only vary the values) -------------

SOURCES = {
    "int": (Block("AgI", TagType.INT, 1, SIZE), True, -32768, 32767),
    "dint": (Block("AgD", TagType.DINT, 1, SIZE), True, -(2**31), 2**31 - 1),
    "word": (Block("AgW", TagType.WORD, 1, SIZE), True, 0, 65535),
    "real": (Block("AgR", TagType.REAL, 1, SIZE), False, -1e6, 1e6),
    "bool": (Block("AgB", TagType.BOOL, 1, SIZE), True, 0, 1),
}
DEST_TYPES = {"int": Int, "dint": Dint, "word": Word, "real": Real}


def _dest_names(src: str) -> list[tuple[str, str, tuple[int, int], str]]:
    """(source, kind, range, dest type) for every calc in the aggregate program."""
    out = []
    for kind in KINDS:
        if src == "bool" and kind != "sum":
            continue
        for rng in RANGES:
            dests = ("word",) if src == "word" else ("int", "dint", "real")
            for dest in dests:
                if src == "real" and dest != "real":
                    continue
                out.append((src, kind, rng, dest))
    return out


def _build_aggregate_program() -> tuple[Program, dict]:
    dests: dict[tuple, object] = {}
    with Program(strict=False) as logic:
        with Rung(Go):
            for src in SOURCES:
                block = SOURCES[src][0]
                for key in _dest_names(src):
                    _, kind, (lo, hi), dest_type = key
                    tag = DEST_TYPES[dest_type](f"AgOut_{src}_{kind}_{lo}_{hi}_{dest_type}")
                    dests[key] = tag
                    math(getattr(block.select(lo, hi), kind)(), tag)
    return logic, dests


AGG_PROGRAM, AGG_DESTS = _build_aggregate_program()


def _expected(key: tuple, values: dict[str, list]) -> object:
    src, kind, (lo, hi), dest = key
    integer = SOURCES[src][1]
    exact = aggregate(kind, values[src][lo - 1 : hi], integer=integer)
    if dest == "real":
        return float(exact)
    whole = _trunc(exact)
    return {
        "int": _wrap(whole, 16, True),
        "dint": _wrap(whole, 32, True),
        "word": _wrap(whole, 16, False),
    }[dest]


@st.composite
def _values(draw):
    result = {}
    for src, (_, _, lo, hi) in SOURCES.items():
        if src == "real":
            elems = st.floats(min_value=lo, max_value=hi, allow_nan=False, allow_infinity=False)
        elif src == "bool":
            elems = st.booleans()
        else:
            # Mostly small values, sometimes the extremes: sums must wrap.
            elems = st.one_of(
                st.integers(lo, hi),
                st.sampled_from([lo, hi, 0, 1, -1 if lo < 0 else 2]),
                st.integers(max(lo, -50), min(hi, 50)),
            )
        result[src] = draw(st.lists(elems, min_size=SIZE, max_size=SIZE))
    return result


@functools.cache
def _agg_runners() -> tuple[PLC, CompiledPLC]:
    # Built once: every example patches every source, so state left over is harmless.
    return PLC(AGG_PROGRAM), CompiledPLC(AGG_PROGRAM)


@given(values=_values())
@SETTINGS
def test_aggregates_match_exact_arithmetic(values):
    plc, kernel = _agg_runners()
    patch = {"AgGo": True}
    for src, (block, *_rest) in SOURCES.items():
        for i, value in enumerate(values[src], start=1):
            patch[block[i].name] = value
    for runner in (plc, kernel):
        runner.patch(patch)
        runner.step()
    assert dict(plc.current_state.tags) == dict(kernel.current_state.tags)
    tags = plc.current_state.tags
    for key, tag in AGG_DESTS.items():
        expected = _expected(key, values)
        got = tags[tag.name]
        if key[3] == "real":
            assert got == pytest.approx(expected, rel=1e-9, abs=1e-9), key
        else:
            assert got == expected, key


# --- division rules over an aggregate -------------------------------------------

Divisor = Int("AgDiv")
DIV_SRC = Block("AgDV", TagType.INT, 1, 5)
DIV_DESTS = {"int": Int, "dint": Dint}


def _build_division_programs() -> dict[tuple[str, str], tuple[Program, object]]:
    programs = {}
    for kind in KINDS:
        for dest_type, ctor in DIV_DESTS.items():
            dest = ctor(f"AgDivOut_{kind}_{dest_type}")
            with Program(strict=False) as logic:
                with Rung(Go):
                    math(getattr(DIV_SRC.select(1, 5), kind)() / Divisor, dest)
            programs[(kind, dest_type)] = (logic, dest)
    return programs


DIV_PROGRAMS = _build_division_programs()


@given(
    values=st.lists(st.integers(-32768, 32767), min_size=5, max_size=5),
    divisor=st.one_of(st.integers(-40, 40), st.integers(-32768, 32767)),
)
@SETTINGS
def test_aggregate_division_truncates_toward_zero_and_zero_divisor_faults(values, divisor):
    for (kind, dest_type), (logic, dest) in DIV_PROGRAMS.items():
        patch = {"AgGo": True, "AgDiv": divisor}
        patch.update({DIV_SRC[i].name: v for i, v in enumerate(values, start=1)})
        runners = (PLC(logic), CompiledPLC(logic))
        for runner in runners:
            runner.patch(patch)
            runner.step()
        assert dict(runners[0].current_state.tags) == dict(runners[1].current_state.tags)
        tags = runners[0].current_state.tags
        if divisor == 0:
            expected, fault = 0, True
        else:
            exact = aggregate(kind, values, integer=True) / divisor
            expected = _wrap(_trunc(exact), 16 if dest_type == "int" else 32, True)
            fault = False
        assert tags[dest.name] == expected, (kind, dest_type)
        assert tags[system.fault.division_error.name] is fault, (kind, dest_type)


# --- randint --------------------------------------------------------------------

Draw = Dint("RiDraw")
Draw2 = Dint("RiDraw2")
LoTag = Int("RiLo")
HiTag = Int("RiHi")


def _randint_program(lo, hi):
    expr = randint(lo, hi)
    with Program(strict=False) as logic:
        with Rung(Go):
            math(expr, Draw)
            math(expr, Draw2)
    return logic, expr


def _draws(runner, scans: int) -> list[int]:
    runner.patch({"AgGo": True})
    out = []
    for _ in range(scans):
        runner.step()
        out.append(runner.current_state.tags["RiDraw"])
        assert runner.current_state.tags["RiDraw2"] == out[-1]  # one site, one scan, one draw
    return out


bound_st = st.integers(-1000, 1000)


@st.composite
def bounds_st(draw):
    """(lo, hi), often within a few of each other and sometimes reversed."""
    lo = draw(bound_st)
    # Never equal: equal constants are a math error, tested on their own below.
    return lo, lo + draw(
        st.one_of(st.integers(-3, 3), st.integers(-1000, 1000)).filter(lambda d: d != 0)
    )


@given(bounds=bounds_st())
@SETTINGS
def test_randint_stays_in_range_inclusive_and_is_the_same_on_every_runner(bounds):
    lo, hi = bounds
    logic, _expr = _randint_program(lo, hi)
    low, high = min(lo, hi), max(lo, hi)
    stepped = _draws(PLC(logic), 60)
    assert all(low <= v <= high for v in stepped)
    assert _draws(CompiledPLC(logic), 60) == stepped
    if high - low <= 3:
        # Deterministic stream: 150 more draws reach every value of a tiny span.
        more = _draws(PLC(logic), 200)
        assert set(more) == set(range(low, high + 1))


@given(bounds=bounds_st())
@SETTINGS
def test_randint_long_run_and_history_replay_equal_stepping(bounds):
    lo, hi = bounds
    logic, _expr = _randint_program(lo, hi)
    stepped_plc = PLC(logic)
    stepped = _draws(stepped_plc, 120)
    long_plc = PLC(logic)
    long_plc.patch({"AgGo": True})
    long_plc.run(120)
    assert long_plc.current_state.tags["RiDraw"] == stepped[-1]
    for scan in (1, 17, 60, 119):
        assert long_plc.history.at(scan).tags["RiDraw"] == stepped[scan - 1]
        assert stepped_plc.history.at(scan).tags["RiDraw"] == stepped[scan - 1]


@given(
    lo=st.integers(-100, 100),
    hi=st.integers(-100, 100),
    scans=st.lists(
        st.tuples(st.integers(-100, 100), st.integers(-100, 100)), min_size=1, max_size=15
    ),
)
@SETTINGS
def test_randint_tag_bounds_are_read_each_scan(lo, hi, scans):
    expr = randint(LoTag, HiTag)
    with Program(strict=False) as logic:
        with Rung(Go):
            math(expr, Draw)
    plc, kernel = PLC(logic), CompiledPLC(logic)
    for low, high in scans:
        for runner in (plc, kernel):
            runner.patch({"AgGo": True, "RiLo": low, "RiHi": high})
            runner.step()
        got = plc.current_state.tags["RiDraw"]
        assert got == kernel.current_state.tags["RiDraw"]
        if low != high:
            assert min(low, high) <= got <= max(low, high)


@given(lo=bound_st)
@SETTINGS
def test_randint_equal_bounds_are_a_math_error(lo):
    """Equal integer constants: the draw is 0 and SC46 is set, on every runner."""
    logic, _expr = _randint_program(lo, lo)
    for runner in (PLC(logic), CompiledPLC(logic)):
        runner.patch({"AgGo": True})
        for _ in range(3):
            runner.step()
            tags = runner.current_state.tags
            assert tags["RiDraw"] == 0 and tags["RiDraw2"] == 0
            assert tags[system.fault.math_operation_error.name] is True


@given(lo=bound_st, hi=bound_st, pick=st.integers(0, 10**6))
@SETTINGS
def test_randint_patch_of_the_sites_tag_chooses_the_draw(lo, hi, pick):
    if lo == hi:
        hi = lo + 1
    logic, expr = _randint_program(lo, hi)
    low, high = min(lo, hi), max(lo, hi)
    chosen = low + pick % (high - low + 1)
    for runner in (PLC(logic), CompiledPLC(logic)):
        runner.patch({"AgGo": True, expr.tag.name: chosen})
        runner.step()
        assert runner.current_state.tags["RiDraw"] == chosen


def _real_sum_on_each_backend(values: list[float]) -> list[float]:
    block = Block("AgRS", TagType.REAL, 1, len(values))
    dest = Real("AgRSOut")
    with Program(strict=False) as logic:
        with Rung(Go):
            math(block.select(1, len(values)).sum(), dest)
    results = []
    for runner in (PLC(logic), CompiledPLC(logic)):
        runner.patch({"AgGo": True} | {block[i].name: v for i, v in enumerate(values, start=1)})
        runner.step()
        results.append(runner.current_state.tags["AgRSOut"])
    return results


def test_real_sum_is_the_same_on_both_backends():
    results = _real_sum_on_each_backend([0.1, 0.2, 0.3])
    assert results[0] == results[1] == (0.1 + 0.2) + 0.3


def test_real_sum_adds_plain_left_to_right_not_compensated():
    # Stored values are 64-bit floats here; 1e8, 1, -1e8 is exact at that width
    # (and loses the 1 at a CLICK's 32-bit Real), so the separating case is 1e16:
    # builtin sum() on Python 3.12+ compensates and gives 2.0, plain adds give 0.0.
    for values in ([1e16, 1.0, 1.0, -1e16], [1e8, 1.0, -1e8]):
        got = _real_sum_on_each_backend(values)
        plain = values[0]
        for value in values[1:]:
            plain += value
        assert got[0] == got[1] == plain, values
    assert _real_sum_on_each_backend([1e16, 1.0, 1.0, -1e16]) == [0.0, 0.0]
