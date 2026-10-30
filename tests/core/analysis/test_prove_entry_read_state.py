"""A coil read above the rung that writes it is state, not combinational.

A read that comes before a tag's first write in scan order sees the value the
tag ended the previous scan with.  classify used to drop an unconditional OTE
coil from the state key without checking for such a read, so a chain of coils
written below their readers (``Rung(Y2): out(Y3)`` above ``Rung(Y1): out(Y2)``)
merged distinguishable states and ``never(Y3)`` came back Proven although a
concrete PLC reaches ``Y3``.

Each shape here is checked three ways: the verdict under the default, the
all-off and the sound-baseline configurations, against an exhaustive concrete
search over the inputs, and every Counterexample replays on a concrete PLC.
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
    Or,
    Program,
    Rung,
    branch,
    calc,
    call,
    copy,
    fall,
    forloop,
    out,
    return_early,
    rise,
    subroutine,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    _classify_dimensions,
    never,
)
from pyrung.core.analysis.prove.passes import _OptConfig

_CONFIGS = {
    "default": _OptConfig(),
    "all_off": _OptConfig.all_off(),
    "sound_baseline": _OptConfig.sound_baseline(),
}

Btn = Bool("Btn", external=True)
G = Bool("G", external=True)
Y1 = Bool("Y1")
Y2 = Bool("Y2")
Y3 = Bool("Y3")
D = Int("D")
E = Int("E")


def _concrete_reaches(
    program: Program,
    inputs: list[str],
    predicate: Callable[[Any], bool],
    depth: int = 6,
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

    combos = [
        dict(zip(inputs, values, strict=True))
        for values in itertools.product((False, True), repeat=len(inputs))
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


def _y3(tags: Any) -> bool:
    return tags["Y3"]


def _y3_without_btn(tags: Any) -> bool:
    return tags["Y3"] and not tags["Btn"]


def _y3_without_g(tags: Any) -> bool:
    return tags["Y3"] and not tags["G"]


def _y3_with_g(tags: Any) -> bool:
    return tags["Y3"] and tags["G"]


def _main_chain():
    with Program(strict=False) as logic:
        with Rung(Y2):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _subroutine_chain():
    with Program(strict=False) as logic:
        with Rung():
            call("chain")
        with subroutine("chain"):
            with Rung(Y2):
                out(Y3)
            with Rung(Y1):
                out(Y2)
            with Rung(rise(Btn)):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _main_reads_above_call():
    with Program(strict=False) as logic:
        with Rung(Y2):
            out(Y3)
        with Rung():
            call("chain")
        with subroutine("chain"):
            with Rung(Y1):
                out(Y2)
            with Rung(rise(Btn)):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _for_body_chain():
    with Program(strict=False) as logic:
        with Rung(Y2):
            with forloop(1):
                out(Y3)
        with Rung(Y1):
            with forloop(1):
                out(Y2)
        with Rung(rise(Btn)):
            with forloop(1):
                out(Y1)
    return logic, ["Btn"], Y3, _y3


def _for_body_skipped():
    with Program(strict=False) as logic:
        with Rung(Btn):
            with forloop(1):
                out(Y2)
        with Rung(Y2):
            out(Y3)
    return logic, ["Btn"], And(Y3, ~Btn), _y3_without_btn


def _for_zero_count():
    with Program(strict=False) as logic:
        with Rung(G):
            copy(0, D)
        with Rung(~G):
            copy(1, D)
        with Rung(Btn):
            with forloop(D):
                out(Y2)
        with Rung(Y2):
            out(Y3)
    return logic, ["Btn", "G"], And(Y3, ~Btn), _y3_without_btn


def _through_or():
    with Program(strict=False) as logic:
        with Rung(Or(Y2, G)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn", "G"], And(Y3, ~G), _y3_without_g


def _through_branch():
    with Program(strict=False) as logic:
        with Rung(G):
            with branch(Y2):
                out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn", "G"], And(Y3, G), _y3_with_g


def _rise_reader_pulse():
    # The pulse is in the entry value and the edge memory together: never seen.
    with Program(strict=False) as logic:
        with Rung(rise(Y2)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _fall_reader_pulse():
    with Program(strict=False) as logic:
        with Rung(fall(Y2)):
            out(Y3)
        with Rung(Y1):
            out(Y2)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _rise_reader_level():
    # rise() compares against the previous scan's end value, so a rise(Y2) above
    # Y2's only writer never fires: the property is unreachable, and the case
    # checks the prover gives no spurious counterexample.
    with Program(strict=False) as logic:
        with Rung(rise(Y2)):
            out(Y3)
        with Rung(Btn):
            out(Y2)
    return logic, ["Btn"], And(Y3, ~Btn), _y3_without_btn


def _copy_dest_above_writer():
    with Program(strict=False) as logic:
        with Rung(D == 1):
            out(Y3)
        with Rung(Y1):
            copy(1, D)
        with Rung(~Y1):
            copy(0, D)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _calc_dest_above_writer():
    with Program(strict=False) as logic:
        with Rung(D == 1):
            out(Y3)
        with Rung():
            calc(E * 1, D)
        with Rung(Y1):
            copy(1, E)
        with Rung(~Y1):
            copy(0, E)
        with Rung(rise(Btn)):
            out(Y1)
    return logic, ["Btn"], Y3, _y3


def _two_writers_read_between():
    with Program(strict=False) as logic:
        with Rung(Y1):
            out(Y2)
        with Rung(Y2):
            out(Y3)
        with Rung(rise(Btn)):
            out(Y1)
        with Rung(G):
            out(Y2)
    return logic, ["Btn", "G"], And(Y3, ~G), _y3_without_g


def _return_early_skips_writer():
    with Program(strict=False) as logic:
        with Rung():
            call("work")
        with Rung(Y2):
            out(Y3)
        with subroutine("work"):
            with Rung(G):
                return_early()
            with Rung(Btn):
                out(Y2)
    return logic, ["Btn", "G"], And(Y3, ~Btn), _y3_without_btn


_SHAPES = {
    "main_chain": _main_chain,
    "subroutine_chain": _subroutine_chain,
    "main_reads_above_call": _main_reads_above_call,
    "for_body_chain": _for_body_chain,
    "for_body_skipped": _for_body_skipped,
    "for_zero_count": _for_zero_count,
    "through_or": _through_or,
    "through_branch": _through_branch,
    "rise_reader_pulse": _rise_reader_pulse,
    "fall_reader_pulse": _fall_reader_pulse,
    "rise_reader_level": _rise_reader_level,
    "copy_dest_above_writer": _copy_dest_above_writer,
    "calc_dest_above_writer": _calc_dest_above_writer,
    "two_writers_read_between": _two_writers_read_between,
    "return_early_skips_writer": _return_early_skips_writer,
}


@pytest.mark.parametrize("config_name", sorted(_CONFIGS))
@pytest.mark.parametrize("shape_name", sorted(_SHAPES))
def test_verdict_agrees_with_concrete_stepping(shape_name: str, config_name: str) -> None:
    logic, inputs, prop, predicate = _SHAPES[shape_name]()
    reachable = _concrete_reaches(logic, inputs, predicate)

    result = never(logic, prop, _opt_config=_CONFIGS[config_name])

    if reachable:
        assert isinstance(result, Counterexample), (
            f"{shape_name}: a concrete PLC reaches the state, prove said {type(result).__name__}"
        )
        assert _replays(logic, result, predicate), f"{shape_name}: trace does not replay"
    else:
        assert isinstance(result, Proven), (
            f"{shape_name}: unreachable concretely, prove said {type(result).__name__}"
        )


@pytest.mark.parametrize("shape_name", ["main_chain", "subroutine_chain", "through_or"])
def test_coil_read_above_its_writer_is_stateful(shape_name: str) -> None:
    logic, *_ = _SHAPES[shape_name]()

    result = _classify_dimensions(logic)

    assert not isinstance(result, Intractable)
    stateful, _nd, combinational, _done_acc, _done_presets, _done_kinds = result
    assert {"Y1", "Y2"} <= set(stateful)
    assert "Y3" in combinational  # read by nothing: still left out of the key


def test_coil_read_only_below_its_writer_stays_combinational() -> None:
    with Program(strict=False) as logic:
        with Rung(rise(Btn)):
            out(Y1)
        with Rung(Y1):
            out(Y2)
        with Rung(Y2):
            out(Y3)

    result = _classify_dimensions(logic)

    assert not isinstance(result, Intractable)
    stateful, _nd, combinational, _done_acc, _done_presets, _done_kinds = result
    assert {"Y1", "Y2", "Y3"} <= combinational
    assert not {"Y1", "Y2"} & set(stateful)
