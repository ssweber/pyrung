"""Site identity table: keys, edge sites, overlay namespaces, reload remap."""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    branch,
    calc,
    call,
    count_down,
    count_up,
    fall,
    out,
    rise,
    subroutine,
)
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.rung import Rung as RungLogic
from pyrung.core.site_memory import (
    LOGIC_NAMESPACE,
    PILOT_NAMESPACE,
    PLANT_NAMESPACE,
    SITE_EDGE,
    bind_sites,
    remap_site_memory,
    site_table_of,
)


def _program() -> tuple[Program, object]:
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    D = Bool("D")
    N = Int("N")
    cnt = Counter.clone("Cnt")
    shared = rise(A)
    with Program(strict=False) as logic:
        with Rung(shared, B):
            out(C, oneshot=True)
            with branch(fall(D)):
                out(D)
        with Rung(shared):
            count_up(cnt, preset=3).reset(shared)
        with Rung(A):
            call("sub")
        with subroutine("sub"):
            with Rung(rise(B)):
                calc(N + 1, N, oneshot=True)
    return logic, shared


class TestBindSites:
    def test_instruction_keys_keep_walk_order(self):
        logic, _shared = _program()
        table = site_table_of(logic)
        assert table is not None
        keys = [s.key for s in table.instruction_sites]
        assert keys == ["i1", "i2", "i3", "i4", "i5"]
        assert logic.rungs[0]._instructions[0].memory_key("_oneshot") == "_oneshot:i1"
        assert logic.subroutines["sub"][0]._instructions[0].state_key == "i5"

    def test_edge_sites_in_solve_order(self):
        logic, _shared = _program()
        table = site_table_of(logic)
        assert table is not None
        edges = [(s.key, s.kind, s.tag, s.scope, s.row, s.leg) for s in table.edge_sites]
        assert edges == [
            ("e1", "rise", "A", "main", 0, "rung"),
            ("e2", "fall", "D", "main", 0, "branch"),
            ("e3", "rise", "A", "main", 1, "rung"),
            ("e4", "rise", "A", "main", 1, "i3.reset_condition"),
            ("e5", "rise", "B", "sub", 0, "rung"),
        ]
        assert [s.key for s in table.sites_reading("A")] == ["e1", "e3", "e4"]

    def test_shared_edge_object_is_copied_per_site(self):
        logic, shared = _program()
        first, second = logic.rungs[0], logic.rungs[1]
        assert first._conditions[0] is shared
        assert first._branches[0]._conditions[0] is shared  # inherited, same contact
        copy_rung = second._conditions[0]
        assert copy_rung is not shared
        assert copy_rung.tag is shared.tag
        counter = second._instructions[0]
        # The counter's power is the rung's own contact; its reset leg is a site.
        assert counter.up_condition is copy_rung
        assert counter.reset_condition is not copy_rung
        assert counter.reset_condition is not shared
        keys = {shared._site_key, copy_rung._site_key, counter.reset_condition._site_key}
        assert keys == {"e1", "e3", "e4"}

    def test_power_fields_are_the_rung_contact(self):
        A = Bool("A")
        B = Bool("B")
        down = Counter.clone("Down")
        edge = rise(A)
        with Program(strict=False) as logic:
            with Rung(edge):
                count_down(down, preset=3).reset(B)
        table = site_table_of(logic)
        assert table is not None
        assert [(s.key, s.leg) for s in table.edge_sites] == [("e1", "rung")]
        assert logic.rungs[0]._instructions[0].down_condition is edge

    def test_memory_defaults(self):
        logic, _shared = _program()
        table = site_table_of(logic)
        assert table is not None
        assert table.memory_defaults() == {"_oneshot:i1": False, "_oneshot:i5": False}

    def test_rebinding_reproduces_keys(self):
        logic, _shared = _program()
        before = site_table_of(logic)
        logic._assign_state_keys()
        after = site_table_of(logic)
        assert before is not None and after is not None
        assert [s.key for s in before.sites] == [s.key for s in after.sites]
        assert [s.fingerprint for s in before.sites] == [s.fingerprint for s in after.sites]


class TestNamespacesAndUnbound:
    def test_overlay_namespaces(self):
        A = Bool("A")
        X = Bool("X")
        for namespace in (PLANT_NAMESPACE, PILOT_NAMESPACE):
            rung = RungLogic(rise(A))
            rung.add_instruction(OutInstruction(X, oneshot=True))
            table = bind_sites([rung], namespace=namespace)
            assert [s.key for s in table.sites] == [f"{namespace}:i1", f"{namespace}:e1"]
            assert rung._instructions[0].memory_key("_oneshot") == f"_oneshot:{namespace}:i1"

    def test_overlay_copies_an_edge_the_program_owns(self):
        logic, shared = _program()
        hold = RungLogic(shared)
        bind_sites([hold], namespace=PILOT_NAMESPACE)
        assert hold._conditions[0] is not shared
        assert shared._site_key == "e1"
        assert hold._conditions[0]._site_key == "pilot:e1"

    def test_unbound_instruction_has_no_key(self):
        instr = OutInstruction(Bool("X"), oneshot=True)
        with pytest.raises(RuntimeError, match="no site key"):
            instr.memory_key("_oneshot")

    def test_codegen_refuses_unbound_instruction(self):
        from pyrung.circuitpy.codegen.context import CodegenContext

        logic, _shared = _program()
        ctx = CodegenContext.for_kernel(logic)
        assert ctx.state_key_for(logic.rungs[0]._instructions[0]) == "i1"
        with pytest.raises(RuntimeError, match="no site key"):
            ctx.state_key_for(OutInstruction(Bool("Y")))

    def test_bare_rung_list_binds(self):
        X = Bool("X")
        rung = RungLogic(Bool("A"))
        rung.add_instruction(OutInstruction(X, oneshot=True))
        plc = PLC([rung])
        assert rung._instructions[0].state_key == "i1"
        assert plc._site_table is not None
        plc.step()

    def test_list_of_bound_rungs_keeps_their_keys(self):
        logic, _shared = _program()
        extra = RungLogic(Bool("A"))
        extra.add_instruction(OutInstruction(Bool("Z"), oneshot=True))
        plc = PLC([*logic.rungs, extra])
        assert logic.rungs[0]._instructions[0].state_key == "i1"
        assert extra._instructions[0].state_key == f"{LOGIC_NAMESPACE}:i5"
        assert plc._site_table is not None
        plc.step()


def _counter_program(*, insert_rung: bool) -> Program:
    A = Bool("A")
    P = Bool("P")
    Q = Bool("Q")
    E = Bool("E")
    with Program(strict=False) as logic:
        if insert_rung:
            with Rung(A):
                out(E, oneshot=True)
        with Rung(A):
            out(P, oneshot=True)
        with Rung(A):
            out(Q, oneshot=True)
    return logic


class TestRemap:
    def test_memory_follows_the_site_after_an_insert(self):
        old = _counter_program(insert_rung=False)
        new = _counter_program(insert_rung=True)
        memory = {"_oneshot:i1": True, "_oneshot:i2": False, "_prev:A": True, "_dt": 0.01}
        remapped, dropped = remap_site_memory(memory, site_table_of(old), site_table_of(new))
        # P's one-shot was i1 and is now i2; Q's was i2, now i3.  The new
        # rung's one-shot (i1) starts unwritten.  Non-site memory is kept.
        assert remapped == {
            "_oneshot:i2": True,
            "_oneshot:i3": False,
            "_prev:A": True,
            "_dt": 0.01,
        }
        assert dropped == ()

    def test_unmatched_site_memory_is_dropped(self):
        old = _counter_program(insert_rung=True)
        A = Bool("A")
        R = Bool("R")
        with Program(strict=False) as new:
            with Rung(A):
                out(R, oneshot=True)
        memory = {"_oneshot:i1": True, "_oneshot:i2": True, "_oneshot:i3": True}
        remapped, dropped = remap_site_memory(memory, site_table_of(old), site_table_of(new))
        assert remapped == {}
        assert sorted(dropped) == ["_oneshot:i1", "_oneshot:i2", "_oneshot:i3"]

    def test_edge_sites_are_recorded_for_remap(self):
        logic, _shared = _program()
        table = site_table_of(logic)
        assert table is not None
        assert all(s.site == SITE_EDGE for s in table.edge_sites)
        assert all(s.memory == () for s in table.edge_sites)
