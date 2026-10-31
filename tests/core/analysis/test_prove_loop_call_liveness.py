"""A call in a forloop body, or from another subroutine, is a call site.

Whether a subroutine's writes happen at all is decided by the guard of every
call on a path into it.  The caller map behind input-liveness pruning skipped
calls in forloop bodies, and gave a write in a subroutine called from another
subroutine only its own call site's conditions; the PDG's upstream slice
likewise stopped one caller up.  An indirect comparison (``DS[P] != 0``, or a
loop counting ``DS[P]``) lowers to an atom that names neither the pointer nor
the elements.  The guard's inputs were then pruned as dead (or left out of
scope), and ``never(And(Y3, ~Btn))`` came back Proven although a concrete PLC
reaches it: scan 1 runs the call with ``Btn`` on, scan 2 skips it with ``Btn``
off, and ``Y2`` holds.

The shared caller map keeps only conditions that hold on every scan that runs
the call: a body rung's contacts count only where no iteration can change them.
Where the conditions do not decide the call (a body-rung contact an iteration
can change, a oneshot loop, a ``break_loop()``) the subroutine is marked
inexact and the validators and pilot guards do not claim anything from them.

A condition the lowering does not understand (``(DQ + 0) != 0``) becomes an
opaque atom too, and the inputs it reads stay live the same way.

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
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    branch,
    break_loop,
    call,
    continue_loop,
    copy,
    forloop,
    latch,
    out,
    reset,
    return_early,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.corrections import _producer_caller_scope
from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, never
from pyrung.core.analysis.prove.passes import _OptConfig
from pyrung.core.analysis.simplified import _build_guard_ctx
from pyrung.core.condition import CompareNe
from pyrung.core.instruction.control import ForLoopInstruction
from pyrung.core.tag import TagType
from pyrung.core.validation._common import (
    _build_caller_map,
    _build_tag_map,
    _inexact_entry_subroutines,
    _loop_count_conditions,
    _scope_reach_chains,
)
from pyrung.core.validation.duplicate_out import validate_conflicting_outputs
from pyrung.core.validation.stuck_bits import COIL_STUCK_HIGH, validate_stuck_bits

_CONFIGS = {
    "default": _OptConfig(),
    "all_off": _OptConfig.all_off(),
    "sound_baseline": _OptConfig.sound_baseline(),
}

Btn = Bool("Btn", external=True)
G = Bool("G", external=True)
R = Bool("R", external=True)
DQ = Int("DQ", external=True, min=0, max=1)
P = Int("P", external=True, min=1, max=2, default=1)
N = Int("N")
Y2 = Bool("Y2")
Y3 = Bool("Y3")
LDS = Block("LDS", TagType.INT, 1, 2)

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


def _return_in_outer():
    # A return_early() ahead of the call in the calling subroutine skips it.
    with Program(strict=False) as logic:
        with Rung():
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung(R):
                return_early()
            with Rung():
                call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "R": _BOOL}


def _return_in_outer_loop():
    with Program(strict=False) as logic:
        with Rung():
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung(R):
                return_early()
            with Rung():
                with forloop(1):
                    call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "R": _BOOL}


def _oneshot_loop():
    # A oneshot loop runs on its rung's rising edge only.
    with Program(strict=False) as logic:
        with Rung(G):
            with forloop(1, oneshot=True):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _indirect_count():
    with Program(strict=False) as logic:
        with Rung():
            copy(DQ, LDS[1])
        with Rung():
            with forloop(LDS[P]):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "DQ": (0, 1), "P": (1, 2)}


def _indirect_count_pointer_only():
    # LDS[1] = 1 and LDS[2] = 0: only the pointer decides the call.
    with Program(strict=False) as logic:
        with Rung():
            copy(1, LDS[1])
        with Rung():
            with forloop(LDS[P]):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "P": (1, 2)}


def _indirect_expr_count():
    with Program(strict=False) as logic:
        with Rung():
            copy(1, LDS[1])
        with Rung():
            with forloop(LDS[P - 0]):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "P": (1, 2)}


def _indirect_call_guard():
    with Program(strict=False) as logic:
        with Rung():
            copy(1, LDS[1])
        with Rung(LDS[P] != 0):
            call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "P": (1, 2)}


def _indirect_contact():
    # No call at all: the pointer reaches liveness only through the atom.
    with Program(strict=False) as logic:
        with Rung():
            copy(1, LDS[2])
        with Rung(LDS[P] != 0, ~Btn):
            out(Y3)
    return logic, {"Btn": _BOOL, "P": (1, 2)}


def _internal_count():
    with Program(strict=False) as logic:
        with Rung():
            copy(DQ, N)
        with Rung():
            with forloop(N):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "DQ": (0, 1)}


def _two_sites():
    with Program(strict=False) as logic:
        with Rung(G):
            call("work")
        with Rung(R):
            with forloop(1):
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL, "R": _BOOL}


def _branch_in_outer():
    with Program(strict=False) as logic:
        with Rung():
            call("outer")
        _read_y2()
        with subroutine("outer"):
            with Rung():
                with branch(G):
                    call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _three_levels():
    with Program(strict=False) as logic:
        with Rung(G):
            call("a")
        _read_y2()
        with subroutine("a"):
            with Rung():
                call("b")
        with subroutine("b"):
            with Rung():
                with forloop(1):
                    call("work")
        _coil_work()
    return logic, {"Btn": _BOOL, "G": _BOOL}


def _break_before_call():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                with Rung(R):
                    break_loop()
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "R": _BOOL}


def _continue_before_call():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                with Rung(R):
                    continue_loop()
                call("work")
        _read_y2()
        _coil_work()
    return logic, {"Btn": _BOOL, "R": _BOOL}


def _body_rung_call():
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                with Rung(G):
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
    "return_in_outer": _return_in_outer,
    "return_in_outer_loop": _return_in_outer_loop,
    "oneshot_loop": _oneshot_loop,
    "indirect_count": _indirect_count,
    "indirect_count_pointer_only": _indirect_count_pointer_only,
    "indirect_expr_count": _indirect_expr_count,
    "indirect_call_guard": _indirect_call_guard,
    "indirect_contact": _indirect_contact,
    "internal_count": _internal_count,
    "two_sites": _two_sites,
    "branch_in_outer": _branch_in_outer,
    "three_levels": _three_levels,
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


# The prover does not model forloop body rungs, break_loop() or
# continue_loop() yet: it must say so, never Proven.
_UNMODELED_SHAPES = {
    "break_before_call": _break_before_call,
    "continue_before_call": _continue_before_call,
    "body_rung_call": _body_rung_call,
}


@pytest.mark.parametrize("config_name", sorted(_CONFIGS))
@pytest.mark.parametrize("shape_name", sorted(_UNMODELED_SHAPES))
def test_unmodeled_loop_exits_are_never_proven(shape_name: str, config_name: str) -> None:
    logic, space = _UNMODELED_SHAPES[shape_name]()
    assert _concrete_reaches(logic, space, _violates)
    result = never(logic, _PROPERTY, _opt_config=_CONFIGS[config_name])
    assert isinstance(result, Intractable | Counterexample), (shape_name, result)
    if isinstance(result, Counterexample):
        assert _replays(logic, result, _violates)


# -- The shared caller map: what holds on every scan that runs the call --------

Z = Bool("Z")
A1 = Bool("A1", external=True)
B1 = Bool("B1", external=True)
C = Int("C", external=True, min=1, max=5)
Wide = Dint("Wide", external=True)


def _out_z_work() -> None:
    with subroutine("work"):
        with Rung():
            out(Z)


def _idx_body_rung_call():
    # Iteration 0 always runs, so work runs on every scan.
    with Program(strict=False) as logic:
        with Rung():
            with forloop(2) as loop:
                with Rung(loop.idx == 0):
                    call("work")
        _out_z_work()
    return logic


def test_body_rung_contacts_are_no_call_condition() -> None:
    logic = _idx_body_rung_call()
    (entry,) = _build_caller_map(logic)["work"]
    assert entry[4] == ()
    assert _inexact_entry_subroutines(logic) == {"work"}


@pytest.mark.parametrize(
    "shape_name", ["oneshot_loop", "break_before_call", "continue_before_call"]
)
def test_undecided_loop_calls_are_inexact(shape_name: str) -> None:
    shapes = {**_SHAPES, **_UNMODELED_SHAPES}
    logic, _space = shapes[shape_name]()
    assert _inexact_entry_subroutines(logic) == {"work"}
    assert "work" not in _build_guard_ctx(logic).caller_guards
    assert _producer_caller_scope(logic, "work") == (None, False, False)


def test_inexact_entry_reaches_nested_callees() -> None:
    with Program(strict=False) as logic:
        with Rung(G):
            with forloop(1, oneshot=True):
                call("outer")
        with subroutine("outer"):
            with Rung():
                call("work")
        _out_z_work()
    assert _inexact_entry_subroutines(logic) == {"outer", "work"}


def test_exact_loop_call_guard() -> None:
    logic, _space = _loop_rung_guard()
    assert not _inexact_entry_subroutines(logic)
    assert repr(_build_guard_ctx(logic).caller_guards["work"]) == repr(
        _build_guard_ctx(_plain_guarded_call()).caller_guards["work"]
    )


def _plain_guarded_call():
    with Program(strict=False) as logic:
        with Rung(G):
            call("work")
        _coil_work()
    return logic


def test_wide_count_is_necessary_but_not_exact() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with forloop(Wide):
                call("work")
        _out_z_work()
    (entry,) = _build_caller_map(logic)["work"]
    assert [condition.tag.name for condition in entry[4]] == ["Wide"]
    assert _inexact_entry_subroutines(logic) == {"work"}


def test_count_condition_follows_zero_count_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyrung.core.instruction.control as control

    loop = ForLoopInstruction(count=C, idx_tag=N, instructions=[])
    conditions, exact = _loop_count_conditions(loop) or ((), False)
    assert exact and len(conditions) == 1
    monkeypatch.setattr(control, "FOR_ZERO_COUNT_ITERATIONS", 65536)
    assert _loop_count_conditions(loop) == ((), True)


def test_stuck_bits_body_rung_call_is_not_reported() -> None:
    findings = validate_stuck_bits(_idx_body_rung_call()).findings
    assert not [f for f in findings if f.code == COIL_STUCK_HIGH]


def test_stuck_bits_count_tag_domain_covers_the_call() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with forloop(C):
                call("work")
        _out_z_work()
    assert "C" in _build_tag_map(logic)
    assert not [f for f in validate_stuck_bits(logic).findings if f.code == COIL_STUCK_HIGH]


def test_duplicate_out_body_rung_calls_are_not_exclusive() -> None:
    # Both iterations run on every scan: b's out() overwrites a's.
    with Program(strict=False) as logic:
        with Rung():
            with forloop(2) as loop:
                with Rung(loop.idx == 0):
                    call("a")
                with Rung(loop.idx == 1):
                    call("b")
        with subroutine("a"):
            with Rung(A1):
                out(Y2)
        with subroutine("b"):
            with Rung(B1):
                out(Y2)
    findings = validate_conflicting_outputs(logic).findings
    assert [f.target_name for f in findings] == ["Y2"]


# -- Body-rung contacts no iteration can change --------------------------------

M = Bool("M")
S = Bool("S", external=True)


def _a_b_out_y2(*, a_resets_m: bool = False) -> None:
    with subroutine("a"):
        with Rung(A1):
            out(Y2)
        if a_resets_m:
            with Rung():
                reset(M)
    with subroutine("b"):
        with Rung(B1):
            out(Y2)


def _split_on_m(*, a_resets_m: bool = False) -> Program:
    with Program(strict=False) as logic:
        with Rung(S):
            latch(M)
        with Rung(~S):
            reset(M)
        with Rung():
            with forloop(1):
                with Rung(M):
                    call("a")
                with Rung(~M):
                    call("b")
        _a_b_out_y2(a_resets_m=a_resets_m)
    return logic


def _plain_split_on_m() -> Program:
    with Program(strict=False) as logic:
        with Rung(S):
            latch(M)
        with Rung(~S):
            reset(M)
        with Rung(M):
            call("a")
        with Rung(~M):
            call("b")
        _a_b_out_y2()
    return logic


def _y2_after_scan(logic: Program, **inputs: bool) -> bool:
    plc = PLC(logic, dt=0.010)
    plc.patch(inputs)
    plc.step()
    return plc.current_state.tags["Y2"]


@pytest.mark.parametrize("build", [_split_on_m, _plain_split_on_m])
def test_duplicate_out_invariant_body_contacts_stay_exclusive(build: Callable[[], Program]) -> None:
    # M holds through the loop: a and b never both run in one scan.
    logic = build()
    assert _y2_after_scan(logic, S=True, A1=True, B1=False)
    assert not _y2_after_scan(logic, S=False, A1=True, B1=False)
    assert not validate_conflicting_outputs(logic).findings
    assert not validate_stuck_bits(logic).findings
    assert [c.tag.name for c in _build_caller_map(logic)["a"][0][4]] == ["M"]
    assert not _inexact_entry_subroutines(logic)


def test_duplicate_out_body_contact_a_callee_writes_is_no_condition() -> None:
    # a resets M: ~M then holds on the next body rung, and b runs after a.
    logic = _split_on_m(a_resets_m=True)
    assert not _y2_after_scan(logic, S=True, A1=True, B1=False)
    findings = validate_conflicting_outputs(logic).findings
    assert [f.target_name for f in findings] == ["Y2"]
    assert _build_caller_map(logic)["a"][0][4] == ()
    assert _inexact_entry_subroutines(logic) == {"a", "b"}


def test_invariant_body_contact_call_is_exact() -> None:
    logic, _space = _body_rung_call()
    (entry,) = _build_caller_map(logic)["work"]
    assert [condition.tag.name for condition in entry[4]] == ["G"]
    assert not _inexact_entry_subroutines(logic)
    plain = _plain_guarded_call()
    assert repr(_build_guard_ctx(logic).caller_guards["work"]) == repr(
        _build_guard_ctx(plain).caller_guards["work"]
    )
    assert repr(_producer_caller_scope(logic, "work")) == repr(
        _producer_caller_scope(plain, "work")
    )
    (chain,) = _scope_reach_chains(logic)["work"]
    assert [condition.tag.name for condition in chain] == ["G"]


def test_body_contact_written_earlier_in_the_rung_is_kept_but_inexact() -> None:
    # The body rung reads M after the copy, not with the rung's own contacts.
    with Program(strict=False) as logic:
        with Rung():
            copy(G, M)
            with forloop(1):
                with Rung(M):
                    call("work")
        _out_z_work()
    (entry,) = _build_caller_map(logic)["work"]
    assert [condition.tag.name for condition in entry[4]] == ["M"]
    assert _inexact_entry_subroutines(logic) == {"work"}


DSB = Block("DSB", TagType.INT, 1, 3)


def test_body_contact_on_an_element_the_body_writes_indirectly_is_no_condition() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with forloop(2) as loop:
                copy(1, DSB[loop.idx + 1])
                with Rung(DSB[1] == 0):
                    call("work")
        _out_z_work()
    (entry,) = _build_caller_map(logic)["work"]
    assert entry[4] == ()
    assert _inexact_entry_subroutines(logic) == {"work"}


def test_indirect_body_contact_on_a_block_the_body_writes_is_no_condition() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                copy(1, DSB[2])
                with Rung(DSB[P] == 0):
                    call("work")
        _out_z_work()
    (entry,) = _build_caller_map(logic)["work"]
    assert entry[4] == ()
    assert _inexact_entry_subroutines(logic) == {"work"}


def test_body_contact_on_an_unwritten_element_is_kept() -> None:
    with Program(strict=False) as logic:
        with Rung():
            with forloop(1):
                copy(1, DSB[2])
                with Rung(DSB[1] == 0):
                    call("work")
        _out_z_work()
    (entry,) = _build_caller_map(logic)["work"]
    assert [condition.tag.name for condition in entry[4]] == ["DSB1"]
    assert not _inexact_entry_subroutines(logic)


# -- Unsupported conditions keep their inputs live -----------------------------

B = Bool("B", external=True)
Y = Bool("Y")
DIn = Int("DIn", external=True, min=0, max=1)
Mid = Int("Mid")


def _expr_compare_coil():
    with Program(strict=False) as logic:
        with Rung((DQ + 0) != 0, B):
            out(Y)
    return logic, {"B": _BOOL, "DQ": (0, 1)}


def _expr_compare_internal():
    with Program(strict=False) as logic:
        with Rung():
            copy(DIn, Mid)
        with Rung((Mid + 0) != 0, B):
            out(Y)
    return logic, {"B": _BOOL, "DIn": (0, 1)}


def _expr_compare_call():
    with Program(strict=False) as logic:
        with Rung((DQ + 0) != 0, B):
            call("set_y")
        with subroutine("set_y"):
            with Rung():
                out(Y)
    return logic, {"B": _BOOL, "DQ": (0, 1)}


_EXPR_SHAPES = {
    "expr_compare_coil": _expr_compare_coil,
    "expr_compare_internal": _expr_compare_internal,
    "expr_compare_call": _expr_compare_call,
}


def _y_on(tags: Any) -> bool:
    return bool(tags["Y"])


@pytest.mark.parametrize("config_name", sorted(_CONFIGS))
@pytest.mark.parametrize("shape_name", sorted(_EXPR_SHAPES))
def test_unsupported_condition_inputs_stay_live(shape_name: str, config_name: str) -> None:
    logic, space = _EXPR_SHAPES[shape_name]()
    assert _concrete_reaches(logic, space, _y_on)
    result = never(logic, Y, _opt_config=_CONFIGS[config_name])
    assert isinstance(result, Counterexample), (shape_name, config_name, result)
    assert _replays(logic, result, _y_on)
