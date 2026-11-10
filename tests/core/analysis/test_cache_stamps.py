"""Caches kept on a program or rung must not answer from a definition edited since."""

from __future__ import annotations

from pyrung import (
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
    before = repr(caller_guard_context(prog).caller_guards)
    limits.slot(1, readonly=True, default=9)
    assert repr(caller_guard_context(prog).caller_guards) != before


def test_advance_owners_are_rebuilt_after_a_slot_edit():
    limits = _limit_block()
    T1 = Timer.clone("T1")
    Go = Bool("Go")
    with Program() as prog:
        with Rung(Go):
            on_delay(T1, limits[1])
    before = _program_owners(prog)
    assert _program_owners(prog) is before
    limits.slot(1, readonly=True, default=9)
    assert _program_owners(prog) is not before


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
    before = repr(_loop_call_gates(prog))
    assert _loop_call_gates(prog) is _loop_call_gates(prog)
    limits.slot(1, readonly=True, default=9)
    assert repr(_loop_call_gates(prog)) != before
