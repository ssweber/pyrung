"""Caches kept on a program or rung must not answer from a definition edited since."""

from __future__ import annotations

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    call,
    copy,
    forloop,
    on_delay,
    out,
    subroutine,
)
from pyrung.core.analysis.pilot.advance import _program_owners
from pyrung.core.analysis.pilot.static_expressions import caller_guard_context
from pyrung.core.analysis.pilot.trace import _loop_call_gates
from pyrung.core.analysis.sp_values import _written_value_for_tag
from pyrung.core.crossing import Literal


def _limit_block() -> Block:
    return Block("Lim", TagType.INT, 1, 4)


def test_written_value_follows_a_readonly_default_edit():
    limits = _limit_block()
    Dest = Int("Dest")
    Go = Bool("Go")
    with Program() as prog:
        with Rung(Go):
            copy(limits[1], Dest)
    rung = prog.rungs[0]
    assert not isinstance(_written_value_for_tag(rung, "Dest"), Literal)
    limits.slot(1, readonly=True, default=5)
    assert _written_value_for_tag(rung, "Dest") == Literal(5)


def test_caller_guard_follows_a_readonly_default_edit():
    limits = _limit_block()
    Count = Int("Count")
    Lamp = Bool("Lamp")
    with Program() as prog:
        with Rung(Count > limits[1]):
            call("sub")
        with subroutine("sub"):
            with Rung():
                out(Lamp)
    guard = caller_guard_context(prog).caller_guards["sub"]
    assert (guard.operand, guard.operand_is_tag) == ("Lim1", True)
    limits.slot(1, readonly=True, default=9)
    guard = caller_guard_context(prog).caller_guards["sub"]
    assert (guard.operand, guard.operand_is_tag) == (9, False)


def _preset_name(prog: Program) -> str:
    boundary = _program_owners(prog)[0].profile.completion_boundary
    assert boundary is not None
    return str(boundary({}).bound)


def test_advance_owners_follow_a_slot_rename():
    limits = _limit_block()
    T1 = Timer.clone("T1")
    Go = Bool("Go")
    with Program() as prog:
        with Rung(Go):
            on_delay(T1, limits[1])
    before = _program_owners(prog)
    assert _preset_name(prog) == "Lim1"
    limits.slot(1, name="Preset")
    assert _program_owners(prog) is not before
    assert _preset_name(prog) == "Preset"


def test_loop_call_gates_follow_a_readonly_default_edit():
    limits = _limit_block()
    Count = Int("Count")
    Lamp = Bool("Lamp")
    with Program() as prog:
        with Rung(Count > 0):
            with forloop(3):
                with Rung(Count > limits[1]):
                    call("sub")
        with subroutine("sub"):
            with Rung():
                out(Lamp)
    (gate,) = _loop_call_gates(prog).values()
    assert (gate.operand, gate.operand_is_tag) == ("Lim1", True)
    limits.slot(1, readonly=True, default=9)
    (gate,) = _loop_call_gates(prog).values()
    assert (gate.operand, gate.operand_is_tag) == (9, False)


def test_plc_graph_follows_a_slot_rename():
    limits = _limit_block()
    Dest = Int("Dest")
    Go = Bool("Go")
    with Program() as prog:
        with Rung(Go):
            copy(limits[2], Dest)
    plc = PLC(prog)
    assert plc.dataview.inputs().tags == {"Lim2", "Go"}
    limits.slot(2, name="Renamed")
    assert plc.dataview.inputs().tags == {"Renamed", "Go"}
