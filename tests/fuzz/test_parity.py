"""Mode 2: Engine parity — interpreted and compiled backends must agree."""

from __future__ import annotations

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

from pyrung.core import PLC, CompiledPLC

from .conftest import DT, PARITY_SCANS
from .strategies import build_program, program_specs


def _diff_dicts(left: dict, right: dict) -> str:
    diffs = []
    all_keys = sorted(set(left) | set(right))
    for key in all_keys:
        lv = left.get(key, "<missing>")
        rv = right.get(key, "<missing>")
        if lv != rv:
            diffs.append(f"  {key}: {lv!r} != {rv!r}")
    return "\n".join(diffs)


@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_engine_parity(data):
    spec = data.draw(program_specs())
    program = build_program(spec)

    interpreted = PLC(program, dt=DT)
    compiled = CompiledPLC(program, dt=DT)

    for scan in range(PARITY_SCANS):
        inputs = {name: data.draw(st.booleans()) for name in spec.pool.input_names()}
        interpreted.patch(inputs)
        compiled.patch(inputs)
        interpreted.step()
        compiled.step()

        i_state = interpreted.current_state
        c_state = compiled.current_state
        assert i_state.scan_id == c_state.scan_id
        assert i_state.timestamp == pytest.approx(c_state.timestamp)
        assert dict(i_state.tags) == dict(c_state.tags), (
            f"Tag mismatch at scan {scan}:\n{_diff_dicts(dict(i_state.tags), dict(c_state.tags))}"
        )
        assert dict(i_state.memory) == dict(c_state.memory), (
            f"Memory mismatch at scan {scan}:\n"
            f"{_diff_dicts(dict(i_state.memory), dict(c_state.memory))}"
        )
