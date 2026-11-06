"""Property tests: generated CLICK v4 programs agree across execution paths.

A random program mixes the v4 instructions (Out variants with Timer UDTs, For/Next
with Break and Continue, range aggregates, ``randint``) under random rung
conditions and is driven by a random input schedule.  Two properties:

- the interpreter and the compiled kernel hold the same tags and memory after
  every scan;
- a long ``run_for`` (the guarded scan loop, forced to fire on short stretches)
  lands on the state stepping reaches, tags, memory and clock.

Scan periods are binary fractions of a second, so timer remainders are exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyrung.core import (
    PLC,
    And,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    break_loop,
    compiled_run,
    continue_loop,
    debounce,
    flasher,
    forloop,
    math,
    randint,
    scan_trace,
    timed,
    toggle,
)

pytestmark = pytest.mark.hypothesis

SETTINGS = settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

INPUTS = tuple(Bool(f"AgIn{i}") for i in range(4))
LEVEL = Int("AgLevel")
RG = Block("AgRg", TagType.INT, 1, 6)
DTS = (0.0625, 0.125, 0.25, 0.5)


# --- generated program ----------------------------------------------------------


@st.composite
def _cond(draw):
    pick = draw(st.integers(0, 5))
    a, b = draw(st.integers(0, 3)), draw(st.integers(0, 3))
    if pick == 0:
        return ("in", a, False)
    if pick == 1:
        return ("in", a, True)
    if pick == 2:
        return ("and", a, b)
    if pick == 3:
        return ("or", a, b)
    if pick == 4:
        return ("gt", draw(st.integers(0, 8)), False)
    return ("lt", draw(st.integers(0, 8)), False)


def _build_cond(cond):
    kind, a, b = cond
    if kind == "in":
        return ~INPUTS[a] if b else INPUTS[a]
    if kind == "and":
        return And(INPUTS[a], ~INPUTS[b])
    if kind == "or":
        return Or(INPUTS[a], INPUTS[b])
    return LEVEL > a if kind == "gt" else LEVEL < a


@st.composite
def _instruction(draw):
    kind = draw(
        st.sampled_from(["toggle", "debounce", "flasher", "timed", "for", "agg", "rand", "randagg"])
    )
    unit = draw(st.sampled_from(["ms", "sec"]))
    cap = 500 if unit == "ms" else 3
    preset = lambda: draw(st.integers(1, cap))  # noqa: E731
    if kind == "toggle":
        return (kind,)
    if kind in ("debounce", "flasher"):
        return (kind, preset(), preset(), unit)
    if kind == "timed":
        return (kind, preset(), unit, draw(st.booleans()))
    if kind == "for":
        leaves = draw(
            st.lists(
                st.tuples(
                    st.sampled_from(["inc", "break", "continue"]),
                    st.integers(0, 4),
                ),
                min_size=1,
                max_size=3,
            )
        )
        return (kind, draw(st.integers(1, 6)), tuple(leaves))
    if kind == "agg":
        lo = draw(st.integers(1, 6))
        hi = draw(st.integers(lo, 6))
        return (
            kind,
            draw(st.sampled_from(["sum", "max", "min", "median", "mean"])),
            lo,
            hi,
        )
    lo, hi = draw(st.integers(-20, 20)), draw(st.integers(-20, 20))
    return (kind, lo if lo != hi else lo + 1, hi)


@dataclass(frozen=True)
class Spec:
    rungs: tuple  # ((cond, (instruction, ...)), ...)
    dt: float
    stretches: tuple  # ((inputs, level, rg, scans), ...)


@st.composite
def _spec(draw):
    rungs = tuple(
        (draw(_cond()), tuple(draw(st.lists(_instruction(), min_size=1, max_size=2))))
        for _ in range(draw(st.integers(1, 3)))
    )
    stretches = tuple(
        (
            tuple(draw(st.booleans()) for _ in range(4)),
            draw(st.integers(0, 8)),
            tuple(draw(st.integers(-5, 5)) for _ in range(6)),
            draw(st.integers(1, 60)),
        )
        for _ in range(draw(st.integers(1, 5)))
    )
    return Spec(rungs, draw(st.sampled_from(DTS)), stretches)


def _emit(instruction: tuple, k: int) -> None:
    kind = instruction[0]
    target = Bool(f"AgOut{k}")
    if kind == "toggle":
        toggle(target)
    elif kind == "debounce":
        _, on, off, unit = instruction
        debounce(
            target, on=(Timer.clone(f"AgTa{k}"), on), off=(Timer.clone(f"AgTb{k}"), off), unit=unit
        )
    elif kind == "flasher":
        _, on, off, unit = instruction
        flasher(
            target, on=(Timer.clone(f"AgTa{k}"), on), off=(Timer.clone(f"AgTb{k}"), off), unit=unit
        )
    elif kind == "timed":
        _, preset, unit, maintained = instruction
        timed(target, Timer.clone(f"AgTa{k}"), preset, unit, maintained=maintained)
    elif kind == "for":
        _, count, leaves = instruction
        total = Int(f"AgLoop{k}")
        with forloop(count) as loop:
            for leaf, at in leaves:
                if leaf == "inc":
                    math(total + 1, total)
                else:
                    with Rung(loop.idx == at):
                        break_loop() if leaf == "break" else continue_loop()
    elif kind == "agg":
        _, agg, lo, hi = instruction
        math(getattr(RG.select(lo, hi), agg)(), Int(f"AgAgg{k}"))
    elif kind == "rand":
        _, lo, hi = instruction
        math(randint(lo, hi), Dint(f"AgRand{k}"))
    else:  # randagg
        math(randint(1, 6) + RG.select(1, 6).sum(), Int(f"AgMix{k}"))


def _build(spec: Spec) -> Program:
    k = 0
    with Program(strict=False) as logic:
        for cond, instructions in spec.rungs:
            with Rung(_build_cond(cond)):
                for instruction in instructions:
                    k += 1
                    _emit(instruction, k)
    return logic


def _patch_for(stretch) -> dict:
    inputs, level, rg, _scans = stretch
    patch = {f"AgIn{i}": v for i, v in enumerate(inputs)}
    patch["AgLevel"] = level
    patch.update({RG[i].name: v for i, v in enumerate(rg, start=1)})
    return patch


def _same(left, right) -> None:
    assert left.scan_id == right.scan_id
    assert left.timestamp == pytest.approx(right.timestamp)
    assert dict(left.tags) == dict(right.tags)
    assert dict(left.memory) == dict(right.memory)


@given(spec=_spec())
@SETTINGS
def test_interpreter_and_compiled_kernel_agree_every_scan(spec):
    logic = _build(spec)
    plc, kernel = PLC(logic, dt=spec.dt), CompiledPLC(logic, dt=spec.dt)
    for stretch in spec.stretches:
        patch = _patch_for(stretch)
        plc.patch(patch)
        kernel.patch(patch)
        for _ in range(stretch[3]):
            plc.step()
            kernel.step()
            _same(plc.current_state, kernel.current_state)


@given(spec=_spec())
@SETTINGS
def test_a_long_run_lands_where_stepping_does(spec):
    logic = _build(spec)
    fast, stepped = PLC(logic, dt=spec.dt), PLC(logic, dt=spec.dt)
    # Let the guarded loop fire on stretches of a few dozen scans.
    with (
        mock.patch.object(compiled_run, "INTERPRETED_PREFIX", 1),
        mock.patch.object(scan_trace, "MIN_RUN_SCANS", 1),
        mock.patch.object(scan_trace, "BURST_SIZES", (1, 2, 3, 5, 8)),
        mock.patch.object(scan_trace, "STRICT_SELF_CHECK", True),
    ):
        for stretch in spec.stretches:
            patch = _patch_for(stretch)
            fast.patch(patch)
            stepped.patch(patch)
            fast.run_for(stretch[3] * spec.dt)
            for _ in range(stretch[3]):
                stepped.step()
            _same(fast.current_state, stepped.current_state)
