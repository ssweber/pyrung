"""Mode 1: Optimization soundness — optimized and unoptimized prove() must agree."""

from __future__ import annotations

import hypothesis.strategies as st
import pytest
from hypothesis import given, note, settings

from pyrung.core import Bool, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.prove import Counterexample, Intractable, prove

from .conftest import DEPTH_BUDGET, MAX_STATES
from .reproducer import format_soundness_reproducer, write_reproducer
from .strategies import build_program, build_property, program_specs, property_specs


@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_optimization_soundness(data):
    spec = data.draw(program_specs())
    program = build_program(spec)
    prop_spec = data.draw(property_specs(spec.pool))
    prop = build_property(prop_spec)

    optimized = prove(program, prop, max_states=MAX_STATES, depth_budget=DEPTH_BUDGET)
    if isinstance(optimized, (Intractable, Counterexample)):
        return

    unoptimized = prove(
        program, prop, max_states=MAX_STATES, depth_budget=DEPTH_BUDGET, _skip_optimizations=True
    )
    if isinstance(unoptimized, Intractable):
        return

    if isinstance(unoptimized, Counterexample):
        code = format_soundness_reproducer(
            spec, prop_spec, type(optimized).__name__, type(unoptimized).__name__
        )
        note(f"\n--- Reproducer ---\n{code}")
        path = write_reproducer(code, "soundness")
        note(f"Written to {path}")
        raise AssertionError(
            f"Unsound optimization: optimized=Proven, unoptimized=Counterexample\n"
            f"Trace: {unoptimized.trace}\n"
            f"Reproducer: {path}"
        )


@pytest.mark.xfail(reason="timer absorption unsound when T.Acc used in downstream comparison")
def test_timer_acc_downstream_absorption():
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    T0 = Timer.clone("T0")

    with Program(strict=False) as logic:
        with Rung(In0):
            on_delay(T0, 50)
        with Rung(T0.Acc >= 10):
            out(B0)
        with Rung(In0):
            out(B0)
        with Rung(In0):
            out(B0)

    optimized = prove(logic, T0.Done == False, max_states=10_000, depth_budget=20)  # noqa: E712
    unoptimized = prove(
        logic,
        T0.Done == False,  # noqa: E712
        max_states=10_000,
        depth_budget=20,
        _skip_optimizations=True,
    )

    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        return
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}"
    )
