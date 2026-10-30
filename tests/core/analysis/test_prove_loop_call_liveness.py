"""A call in a forloop body, or from another subroutine, is a call site.

Whether a subroutine's writes happen at all is decided by the guard of every
call on a path into it.  The caller map behind input-liveness pruning skipped
calls in forloop bodies, and gave a write in a subroutine called from another
subroutine only its own call site's conditions; the PDG's upstream slice
likewise stopped one caller up.  The guard's inputs were then pruned as dead
(or left out of scope), and ``never(And(Y3, ~Btn))`` came back Proven although
a concrete PLC reaches it: scan 1 runs the call with ``Btn`` on, scan 2 skips
it with ``Btn`` off, and ``Y2`` holds.

Each shape is checked under the default, the all-off and the sound-baseline
configurations against an exhaustive concrete search over its inputs, and
every Counterexample replays on a concrete PLC.  Every shape reads ``Y2`` in
a main rung so that it is state on its own account.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    And,
    Bool,
    Int,
    Program,
    Rung,
    branch,
    call,
    copy,
    forloop,
    out,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import Counterexample, Proven, never
from pyrung.core.analysis.prove.passes import _OptConfig
from pyrung.core.condition import CompareNe
from pyrung.core.validation._common import _build_caller_map, _scope_reach_chains

_CONFIGS = {
    "default": _OptConfig(),
    "all_off": _OptConfig.all_off(),
    "sound_baseline": _OptConfig.sound_baseline(),
}

Btn = Bool("Btn", external=True)
G = Bool("G", external=True)
DQ = Int("DQ", external=True, min=0, max=1)
Y2 = Bool("Y2")
Y3 = Bool("Y3")

_BOOL = (False, True)
_PROPERTY = And(Y3, ~Btn)


def _violates(tags: Any) -> bool:
    return tags["Y3"] and not tags["Btn"]


def _concrete_reaches(
    program: Program,
    space: dict[str, tuple[Any, ...]],
    predicate: Callable[[Any], bool],
    depth: int = 4,
) -> bool:
    """Exhaustive concrete search: every input combination on every scan."""
    plc = PLC(program, dt=0.010)

    def key(runner: PLC) -> tuple[frozenset, frozenset]:
        state = runner.current_state
        return (
            frozenset((k, v) for k, v in state.tags.items() if not k.startswith("sys.")),
            frozenset(
                (k, v) for k, v in state.memory.items() if not k.startswith("_sys") and k != "_dt"
            ),
        )

    names = list(space)
    combos = [
        dict(zip(names, values, strict=True)) for values in itertools.product(*space.values())
    ]
    frontier = [plc]
    seen = {key(plc)}
    for _ in range(depth):
        following = []
        for runner in frontier:
            for combo in combos:
                child = runner.fork()
                child.patch(combo)
                child.step()
                if predicate(child.current_state.tags):
                    return True
                child_key = key(child)
                if child_key not in seen:
                    seen.add(child_key)
                    following.append(child)
        frontier = following
    return False


def _replays(program: Program, result: Counterexample, predicate: Callable[[Any], bool]) -> bool:
    plc = PLC(program, dt=0.010)
    for step in result.trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return predicate(plc.current_state.tags)


def _coil_work() -> None:
    with subroutine("work"):
        with Rung(Btn):
            out(Y2)


def _copy_work() -> None:
    with subroutine("work"):
        with Rung():
            copy(Btn, Y2)


def _read_y2() -> None:
    with Rung(Y2):
        out(Y3)


def _loop_rung_guard():
    with Program(strict=False) as logic:
        with Rung(G):
            with forloop(1):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _loop_tag_count():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(DQ):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "DQ": (0, 1)}


def _nested_loop():
    with Program(strict=False) as logic:
        with Rung(G):
            with forloop(1):
                with forloop(1):
                    call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _nested_loop_tag_count():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                with forloop(DQ):
                    call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "DQ": (0, 1)}


def _branch_loop():
    with Program(strict=False) as logic:
        with Rung():
            with branch(G):
                with forloop(1):
                    call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _loop_in_subroutine():
    with Program(strict=False) as logic:
        with Rung():
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung(G):
                with forloop(1):
                    call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _guarded_outer_loop():
    with Program(strict=False) as logic:
        with Rung(G):
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung():
                with forloop(1):
                    call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _guarded_outer_copy():
    with Program(strict=False) as logic:
        with Rung(G):
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung():
                call("work")
        _copy_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _always_called_loop():
    # Called on every scan: Y2 follows Btn, so Y3 never shows without it.
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _zero_count_loop():
    # The count is read as unsigned 16-bit: 65536 runs no iteration, so work
    # is never called.
    with Program(strict=False) as logic:
        with Rung():
            with forloop(65536):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


_SHAPES = {
    "loop_rung_guard": _loop_rung_guard,
    "loop_tag_count": _loop_tag_count,
    "nested_loop": _nested_loop,
    "nested_loop_tag_count": _nested_loop_tag_count,
    "branch_loop": _branch_loop,
    "loop_in_subroutine": _loop_in_subroutine,
    "guarded_outer_loop": _guarded_outer_loop,
    "guarded_outer_copy": _guarded_outer_copy,
    "always_called_loop": _always_called_loop,
    "zero_count_loop": _zero_count_loop,
}


@pytest.mark.parametrize("config_name", sorted(_CONFIGS))
@pytest.mark.parametrize("shape_name", sorted(_SHAPES))
def test_verdict_agrees_with_concrete_stepping(shape_name: str, config_name: str) -> None:
    logic, space = _SHAPES[shape_name]()
    reachable = _concrete_reaches(logic, space, _violates)
    result = never(logic, _PROPERTY, _opt_config=_CONFIGS[config_name])
    if reachable:
        assert isinstance(result, Counterexample), (
            f"{shape_name} under {config_name}: concrete PLC reaches the state, got {result!r}"
        )
        assert _replays(logic, result, _violates), f"{shape_name}: trace does not replay"
    else:
        assert isinstance(result, Proven), (
            f"{shape_name} under {config_name}: unreachable concretely, got {result!r}"
        )


def test_expected_reachability() -> None:
    """The shapes mean what they say: only the two controls are safe."""
    safe = {"always_called_loop", "zero_count_loop"}
    for name, build in _SHAPES.items():
        logic, space = build()
        assert _concrete_reaches(logic, space, _violates) is (name not in safe), name


def test_caller_map_sees_calls_in_loop_bodies() -> None:
    logic, _space = _loop_tag_count()
    (entry,) = _build_caller_map(logic)["work"]
    scope, caller, rung_index, _branch_path, conditions = entry
    assert (scope, caller, rung_index) == ("main", None, 0)
    (count_condition,) = conditions
    assert isinstance(count_condition, CompareNe)
    assert count_condition.tag.name == "DQ" and count_condition.value == 0

    logic, _space = _loop_rung_guard()
    (entry,) = _build_caller_map(logic)["work"]
    assert [condition.tag.name for condition in entry[4]] == ["G"]


def test_constant_zero_count_loop_calls_nothing() -> None:
    logic, _space = _zero_count_loop()
    assert "work" not in _build_caller_map(logic)
    assert _scope_reach_chains(logic)["work"] == ()


def test_reach_chains_cross_loops_and_subroutines() -> None:
    logic, _space = _guarded_outer_loop()
    (chain,) = _scope_reach_chains(logic)["work"]
    assert [condition.tag.name for condition in chain] == ["G"]


@pytest.mark.parametrize("shape_name", ["guarded_outer_loop", "guarded_outer_copy"])
def test_upstream_slice_climbs_every_caller(shape_name: str) -> None:
    logic, _space = _SHAPES[shape_name]()
    graph = build_program_graph(logic)
    assert "G" in graph.upstream_slice("Y3")
    assert "G" not in graph.upstream_slice("Y3", follow_calls=False)
