"""Mode 1: Optimization soundness — optimized and unoptimized prove() must agree."""

from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import given, settings

from pyrung.core.analysis.prove import Counterexample, Intractable, prove

from .conftest import DEPTH_BUDGET, MAX_STATES
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

    assert not isinstance(unoptimized, Counterexample), (
        f"Unsound optimization: optimized=Proven, unoptimized=Counterexample\n"
        f"Trace: {unoptimized.trace}"
    )
