"""Mode 3: Reachability cross-check — simulation-visited states must be in BFS set."""

from __future__ import annotations

import hypothesis.strategies as st
import pytest
from hypothesis import assume, given, note, settings

from pyrung.core import Bool, PLC, Program, Rung, out, rise
from pyrung.core.analysis.prove import Intractable, reachable_states

from .conftest import DEPTH_BUDGET, DT, MAX_STATES
from .pool import TagPool
from .reproducer import format_reachability_reproducer, write_reproducer
from .strategies import build_program, program_specs

REACHABILITY_SCANS = 100


def _projection_names(pool: TagPool, available: set[str]) -> list[str]:
    names: list[str] = []
    for t in pool.bool_internal:
        if t.name in available:
            names.append(t.name)
    for t in pool.timers:
        if t.Done.name in available:
            names.append(t.Done.name)
    for c in pool.counters:
        if c.Done.name in available:
            names.append(c.Done.name)
    return sorted(set(names))


def _project_plc_state(
    plc: PLC, names: list[str]
) -> frozenset[tuple[str, object]]:
    tags = plc.current_state.tags
    return frozenset((name, tags[name]) for name in names)


@given(data=st.data())
@settings(max_examples=200, deadline=None)
def test_reachability_crosscheck(data):
    spec = data.draw(program_specs())
    # Unconditional rungs expose known BFS completeness gaps (see
    # test_simultaneous_rise_cross_product); skip them here since
    # soundness and parity tests still exercise them.
    all_rungs = spec.rungs
    for s in spec.subroutines:
        all_rungs = all_rungs + s.rungs
    assume(all(len(r.conditions) > 0 for r in all_rungs))
    program = build_program(spec)

    plc = PLC(program, dt=DT)
    available = set(plc.current_state.tags.keys())
    projection = _projection_names(spec.pool, available)
    assume(len(projection) > 0)

    bfs_result = reachable_states(
        program,
        project=projection,
        max_states=MAX_STATES,
        depth_budget=DEPTH_BUDGET,
    )
    if isinstance(bfs_result, Intractable):
        return

    input_history: list[dict[str, bool | int]] = []
    strat_map = spec.pool.input_strategy_map()
    prev_bool_inputs: dict[str, bool] = {n: False for n, t in strat_map.items() if t == "bool"}

    for scan in range(REACHABILITY_SCANS):
        inputs: dict[str, bool | int] = {}
        for name in spec.pool.input_names():
            if strat_map[name] == "bool":
                inputs[name] = data.draw(st.booleans())
            else:
                inputs[name] = data.draw(st.sampled_from(spec.pool.int_input_domain(name)))
        input_history.append(inputs)
        plc.patch(inputs)
        plc.step()

        # Track bool input transitions — BFS doesn't enumerate cross-product
        # of simultaneous rise/fall (known limitation, see test_simultaneous_rise_cross_product)
        bool_flips = sum(
            1 for n in prev_bool_inputs if inputs.get(n) != prev_bool_inputs[n]
        )
        prev_bool_inputs = {n: inputs[n] for n in prev_bool_inputs}

        state = _project_plc_state(plc, projection)
        if state not in bfs_result and bool_flips > 1:
            continue
        if state not in bfs_result:
            code = format_reachability_reproducer(
                spec, scan, input_history, projection, dict(state), len(bfs_result)
            )
            note(f"\n--- Reproducer ---\n{code}")
            path = write_reproducer(code, "reachability")
            note(f"Written to {path}")
            raise AssertionError(
                f"Simulation reached state not in BFS set at scan {scan}:\n"
                f"  state: {dict(state)}\n"
                f"  BFS set size: {len(bfs_result)}\n"
                f"  Reproducer: {path}"
            )


@pytest.mark.xfail(
    reason="BFS input composition does not enumerate cross-product of simultaneous rise() transitions",
    strict=True,
)
def test_simultaneous_rise_cross_product():
    """Unconditional rung + two rise()-gated out() on separate inputs.

    BFS explores each input rising independently but misses both rising
    on the same scan, so {B0: True, B1: True} is unreachable in the
    BFS set despite being reachable via simulation.
    """
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")

    with Program(strict=False) as logic:
        with Rung():
            out(B0)
        with Rung(rise(In0)):
            out(B0)
        with Rung(rise(In1)):
            out(B1)

    projection = ["B0", "B1"]
    bfs_result = reachable_states(
        logic, project=projection, max_states=10_000, depth_budget=20
    )
    assert not isinstance(bfs_result, Intractable)

    plc = PLC(logic, dt=0.010)
    plc.patch({"In0": True, "In1": True})
    plc.step()

    tags = plc.current_state.tags
    state = frozenset((name, tags[name]) for name in projection)
    assert state in bfs_result
