"""Tests for the static program dependence graph."""

from __future__ import annotations

import importlib
import sys

from pyrung.core import (
    Block,
    Bool,
    InputBlock,
    Int,
    OutputBlock,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    return_early,
    subroutine,
)
from pyrung.core.analysis import TagRole, TagVersion, build_program_graph
from pyrung.core.validation.walker import walk_program


def test_build_program_graph_extracts_simple_roles() -> None:
    start_button = Bool("StartButton")
    run_mode = Bool("RunMode")
    conveyor = Bool("Conveyor")

    with Program() as prog:
        with Rung(start_button):
            copy(True, run_mode)
        with Rung(run_mode):
            out(conveyor)

    graph = build_program_graph(prog)

    assert len(graph.rung_nodes) == 2
    assert graph.rung_nodes[0].condition_reads == frozenset({"StartButton"})
    assert graph.rung_nodes[0].data_reads == frozenset()
    assert graph.rung_nodes[0].writes == frozenset({"RunMode"})
    assert graph.rung_nodes[1].condition_reads == frozenset({"RunMode"})
    assert graph.rung_nodes[1].writes == frozenset({"Conveyor"})

    assert graph.tag_roles["StartButton"] == TagRole.INPUT
    assert graph.tag_roles["RunMode"] == TagRole.PIVOT
    assert graph.tag_roles["Conveyor"] == TagRole.TERMINAL


def test_embedded_timer_conditions_and_calc_reads_are_extracted() -> None:
    pdg_timer = Timer.clone("PdgTimer")
    enable = Bool("Enable")
    reset = Bool("Reset")
    preset = Int("Preset")
    scale = Int("Scale")
    result = Int("Result")

    with Program() as prog:
        with Rung(enable):
            calc(scale + 1, result)
            on_delay(pdg_timer, preset=preset, unit="Ts").reset(reset)

    graph = build_program_graph(prog)
    node = graph.rung_nodes[0]

    assert node.condition_reads == frozenset({"Enable", "Reset"})
    assert node.data_reads == frozenset({"Preset", "Scale"})
    assert node.writes == frozenset({"PdgTimer_Acc", "PdgTimer_Done", "Result"})


def test_indirect_refs_keep_pointer_as_read_and_block_as_conservative_target() -> None:
    ds = Block("DS", TagType.INT, 1, 3)
    index = Int("Index")
    result = Int("Result")

    with Program() as prog:
        with Rung():
            copy(ds[index], result)
            copy(1, ds[index])

    graph = build_program_graph(prog)
    node = graph.rung_nodes[0]

    assert {"DS1", "DS2", "DS3", "Index"} <= node.data_reads
    assert {"DS1", "DS2", "DS3", "Result"} <= node.writes
    assert "Index" not in node.writes


def test_def_use_chain_tracks_write_then_next_rung_read() -> None:
    request = Bool("Request")
    active = Bool("Active")
    output = Bool("Output")

    with Program() as prog:
        with Rung(request):
            latch(active)
        with Rung(active):
            out(output)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Active"] == (
        TagVersion(tag="Active", defined_at=None, read_by=frozenset()),
        TagVersion(tag="Active", defined_at=0, read_by=frozenset({1})),
    )


def test_def_use_chain_tracks_chained_writes() -> None:
    step = Int("Step")

    with Program() as prog:
        with Rung(step == 0):
            copy(1, step)
        with Rung(step == 1):
            copy(2, step)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Step"] == (
        TagVersion(tag="Step", defined_at=None, read_by=frozenset({0})),
        TagVersion(tag="Step", defined_at=0, read_by=frozenset({1})),
        TagVersion(tag="Step", defined_at=1, read_by=frozenset()),
    )


def test_entry_only_reads_share_one_entry_version() -> None:
    sensor = Bool("Sensor")
    light_a = Bool("LightA")
    light_b = Bool("LightB")

    with Program() as prog:
        with Rung(sensor):
            out(light_a)
        with Rung(~sensor):
            out(light_b)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Sensor"] == (
        TagVersion(tag="Sensor", defined_at=None, read_by=frozenset({0, 1})),
    )


def test_execution_items_preserve_instruction_branch_interleaving() -> None:
    source = Int("Source")
    capture = Int("Capture")

    with Program() as prog:
        with Rung():
            copy(1, source)
            with branch():
                copy(source, capture)
            copy(2, source)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Source"] == (
        TagVersion(tag="Source", defined_at=None, read_by=frozenset()),
        TagVersion(tag="Source", defined_at=0, read_by=frozenset({1})),
        TagVersion(tag="Source", defined_at=0, read_by=frozenset()),
    )


def test_branch_local_conditions_are_precomputed_before_sibling_instructions() -> None:
    gate = Bool("Gate")
    light = Bool("Light")

    with Program() as prog:
        with Rung():
            copy(True, gate)
            with branch(gate):
                out(light)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Gate"] == (
        TagVersion(tag="Gate", defined_at=None, read_by=frozenset({1})),
        TagVersion(tag="Gate", defined_at=0, read_by=frozenset()),
    )


def test_nested_branch_conditions_also_read_preinstruction_snapshot() -> None:
    gate = Bool("Gate")
    middle = Bool("Middle")
    light = Bool("Light")

    with Program() as prog:
        with Rung():
            copy(True, gate)
            with branch():
                copy(True, middle)
                with branch(gate):
                    out(light)

    graph = build_program_graph(prog)

    assert graph.def_use_chains["Gate"] == (
        TagVersion(tag="Gate", defined_at=None, read_by=frozenset({2})),
        TagVersion(tag="Gate", defined_at=0, read_by=frozenset()),
    )


def test_isolated_role_prefers_same_rung_read_write_cycles() -> None:
    temp = Int("Temp")
    dest = Int("Dest")

    with Program() as prog:
        with Rung():
            copy(1, temp)
            calc(temp + 1, dest)

    graph = build_program_graph(prog)

    assert graph.tag_roles["Temp"] == TagRole.ISOLATED


def test_graph_tracks_physical_io_types() -> None:
    x = InputBlock("X", TagType.BOOL, 1, 1)
    y = OutputBlock("Y", TagType.BOOL, 1, 1)

    with Program() as prog:
        with Rung(x[1]):
            out(y[1])

    graph = build_program_graph(prog)

    assert graph.is_physical_input("X1")
    assert graph.is_physical_output("Y1")


def test_def_use_chains_inline_subroutines_at_call_site() -> None:
    trigger = Bool("Trigger")
    flag = Bool("Flag")
    result = Bool("Result")

    with Program() as prog:
        with Rung(trigger):
            call("Worker")
        with Rung(flag):
            out(result)
        with subroutine("Worker"):
            with Rung():
                latch(flag)

    graph = build_program_graph(prog)

    # The subroutine writes Flag, then the main rung after the call reads it.
    # Inlining at the call site means main rung 1 reads the subroutine's
    # version, not the scan-entry version.
    sub_node = next(i for i, n in enumerate(graph.rung_nodes) if n.scope == "subroutine")
    main_reader = next(
        i for i, n in enumerate(graph.rung_nodes) if "Flag" in n.condition_reads and n.scope == "main"
    )

    chain = graph.def_use_chains["Flag"]
    assert chain == (
        TagVersion(tag="Flag", defined_at=None, read_by=frozenset()),
        TagVersion(tag="Flag", defined_at=sub_node, read_by=frozenset({main_reader})),
    )


def test_example_program_builds_graph_without_crashing(monkeypatch) -> None:
    module_name = "examples.click_conveyor"
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    sys.modules.pop(module_name, None)
    module = importlib.import_module(module_name)

    graph = build_program_graph(module.logic)

    assert graph.rung_nodes
    assert graph.tag_roles["StartBtn"] == TagRole.INPUT
    assert graph.tag_roles["Running"] == TagRole.PIVOT


def test_walker_uses_declared_instruction_fields_without_unknowns() -> None:
    with Program() as prog:
        with subroutine("worker"):
            with Rung():
                return_early()
        with Rung():
            call("worker")

    facts = walk_program(prog)

    assert not [fact for fact in facts.operands if fact.value_kind == "unknown"]
    assert any(fact.location.arg_path == "instruction.subroutine_name" for fact in facts.operands)
