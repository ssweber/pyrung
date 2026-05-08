"""Mode 1: Optimization soundness — optimized and unoptimized prove() must agree."""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, note, settings

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
