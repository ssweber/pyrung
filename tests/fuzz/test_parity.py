"""Mode 2: Engine parity — interpreted and compiled backends must agree."""

from __future__ import annotations

import gc

import hypothesis.strategies as st
import pytest
from hypothesis import Phase, given, note, settings

from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Program,
    Rung,
    TagType,
    Word,
    copy,
    math,
    search,
)

from .conftest import DT, PARITY_EXAMPLES, PARITY_SCANS, PROFILE
from .minimize import minimize
from .reproducer import format_parity_reproducer, write_reproducer
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


@pytest.mark.fuzz
@given(data=st.data())
@settings(
    settings.get_profile(PROFILE),
    max_examples=PARITY_EXAMPLES,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
)
def test_engine_parity(data):
    try:
        spec = data.draw(program_specs())
        program = build_program(spec)

        interpreted = PLC(program, dt=DT)
        compiled = CompiledPLC(program, dt=DT)

        input_history: list[dict[str, bool | int]] = []
        strat_map = spec.pool.input_strategy_map()

        for scan in range(PARITY_SCANS):
            inputs: dict[str, bool | int] = {}
            for name in spec.pool.input_names():
                if strat_map[name] == "bool":
                    inputs[name] = data.draw(st.booleans())
                else:
                    inputs[name] = data.draw(st.sampled_from(spec.pool.int_input_domain(name)))
            input_history.append(inputs)
            interpreted.patch(inputs)
            compiled.patch(inputs)
            interpreted.step()
            compiled.step()

            i_state = interpreted.current_state
            c_state = compiled.current_state

            tag_diff = _diff_dicts(dict(i_state.tags), dict(c_state.tags))
            mem_diff = _diff_dicts(dict(i_state.memory), dict(c_state.memory))
            diff = tag_diff or mem_diff

            if i_state.timestamp != pytest.approx(c_state.timestamp) or diff:

                def _check_parity(candidate, _scan=scan, _hist=input_history):
                    try:
                        p = build_program(candidate)
                        interp = PLC(p, dt=DT)
                        comp = CompiledPLC(p, dt=DT)
                        for step_inputs in _hist[: _scan + 1]:
                            interp.patch(step_inputs)
                            comp.patch(step_inputs)
                            interp.step()
                            comp.step()
                        i_s = interp.current_state
                        c_s = comp.current_state
                        return dict(i_s.tags) != dict(c_s.tags) or dict(i_s.memory) != dict(
                            c_s.memory
                        )
                    except Exception:
                        return False

                note(f"scan={scan} timestamps={i_state.timestamp!r} {c_state.timestamp!r}")
                note(f"diff: {diff}")
                spec = minimize(spec, _check_parity)
                code = format_parity_reproducer(spec, scan, input_history, diff)
                note(f"\n--- Reproducer ---\n{code}")
                path = write_reproducer(code, "parity")
                note(f"Written to {path}")
                raise AssertionError(f"Found a parity bug, see the reproducer:\n  {path}")
    finally:
        gc.collect()


def test_indirect_copy_tag_materialization():
    In0 = Bool("In0", external=True)
    W0 = Word("W0")
    DS = Block("DS", TagType.INT, 1, 3)

    with Program(strict=False) as logic:
        with Rung(In0):
            math(W0 + 1, W0)
        with Rung(In0):
            copy(W0, W0)
        with Rung(In0):
            copy(W0, W0)
        with Rung(In0):
            copy(W0, W0)
        with Rung(In0):
            copy(0, DS[DS[1]])

    interpreted = PLC(logic, dt=0.010)
    compiled = CompiledPLC(logic, dt=0.010)

    interpreted.patch({"In0": False})
    compiled.patch({"In0": False})
    interpreted.step()
    compiled.step()

    i_state = interpreted.current_state
    c_state = compiled.current_state
    assert dict(i_state.tags) == dict(c_state.tags)
    assert dict(i_state.memory) == dict(c_state.memory)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PLC holds a slot the program writes as DS[1] but no rung uses, and CompiledPLC does not "
        "(tests/fuzz/reproducers/parity_20261117_indexed_unused_slot.py)"
    ),
)
def test_unused_indexed_slot_parity():
    DS = Block("DS", TagType.INT, 1, 7)
    D0 = Dint("D0")
    B1 = Bool("B1")
    DS[1]  # noqa: B018  (the touch is the case)

    with Program(strict=False) as logic:
        with Rung():
            search(DS.select(6, 7) != -66, result=D0, found=B1)

    interpreted = PLC(logic, dt=DT)
    compiled = CompiledPLC(logic, dt=DT)
    interpreted.step()
    compiled.step()

    assert dict(interpreted.current_state.tags) == dict(compiled.current_state.tags)
