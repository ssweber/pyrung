"""Mode 3: Reachability cross-check — simulation-visited states must be in BFS set."""

from __future__ import annotations

import hypothesis.strategies as st
import pytest
from hypothesis import assume, given, note, settings

from pyrung.core import Bool, Int, PLC, Program, Rung, forloop, out, rise, time_drum
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
    # min_conditions=1: unconditional rungs expose known BFS completeness
    # gaps around input cross-products (see test_simultaneous_rise_cross_product).
    # Soundness and parity tests still exercise unconditional rungs.
    spec = data.draw(program_specs(min_conditions=1))
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

        state = _project_plc_state(plc, projection)
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


@pytest.mark.xfail(
    reason="BFS does not fully explore time_drum with zero presets — steps advance every scan",
    strict=True,
)
def test_time_drum_zero_preset_reachability():
    """time_drum with presets=[0,0,0,0] advances through all steps immediately.

    The drum reaches step 4 (pattern=[..., [False, True]]) within a few
    scans, setting B1=True.  BFS finds only {B0: False, B1: False}.
    """
    In0 = Bool("In0", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    N0 = Int("N0")
    DrumStep = Int("DrumStep")
    DrumAcc = Int("DrumAcc")
    DrumDone = Bool("DrumDone")

    with Program(strict=False) as logic:
        with Rung(In0):
            with forloop(N0):
                out(B0)
        with Rung(N0 == 0):
            time_drum(
                outputs=[B0, B1],
                presets=[0, 0, 0, 0],
                unit="ms",
                pattern=[
                    [False, False],
                    [False, False],
                    [False, False],
                    [False, True],
                ],
                current_step=DrumStep,
                accumulator=DrumAcc,
                completion_flag=DrumDone,
            ).reset(B0)

    projection = ["B0", "B1"]
    bfs_result = reachable_states(
        logic, project=projection, max_states=10_000, depth_budget=20
    )
    assert not isinstance(bfs_result, Intractable)

    plc = PLC(logic, dt=0.010)
    for _ in range(3):
        plc.patch({"In0": False})
        plc.step()

    tags = plc.current_state.tags
    state = frozenset((name, tags[name]) for name in projection)
    assert state in bfs_result
