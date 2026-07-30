"""Shared instruction write-destination discovery."""

from __future__ import annotations

from pyrung import (
    Block,
    Bool,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    copy,
    fill,
    immediate,
    on_delay,
    run_function,
)
from pyrung.core.analysis import build_program_graph
from pyrung.core.analysis.sp_values import _writer_for_tag
from pyrung.core.analysis.write_sites import (
    instruction_write_targets,
    instruction_writes_tag,
    static_write_target_names,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.memory_block import IndirectBlockRange


class _MixedDestinations:
    _writes = ("primary", "grouped", "outputs")
    _status_fields = ("status",)

    def __init__(self) -> None:
        block = Block("MixedFlags", TagType.BOOL, 1, 4)
        self.primary = Bool("MixedPrimary")
        self.grouped = [immediate(block[1]), (block.select(2, 3),)]
        self.outputs = {"result": block[4]}
        self.status = Bool("MixedStatus")


def test_declared_destination_shapes_share_one_enumerator() -> None:
    instr = _MixedDestinations()

    assert {
        name
        for target in instruction_write_targets(instr)
        for name in static_write_target_names(target)
    } == {
        "MixedPrimary",
        "MixedFlags1",
        "MixedFlags2",
        "MixedFlags3",
        "MixedFlags4",
        "MixedStatus",
    }
    assert instruction_writes_tag(instr, "MixedFlags3")
    assert instruction_writes_tag(instr, "MixedStatus")


def test_static_range_lookup_does_not_mutate_the_program_object() -> None:
    target = Block("UncachedFlags", TagType.BOOL, 1, 3).select(1, 3)
    before = dict(target.__dict__)

    assert static_write_target_names(target) == frozenset(
        {"UncachedFlags1", "UncachedFlags2", "UncachedFlags3"}
    )
    assert target.__dict__ == before


def test_dynamic_range_stays_opaque_to_exact_writer_lookup() -> None:
    block = Block("DynamicValues", TagType.INT, 1, 10)
    target = block.select(Int("DynamicStart"), Int("DynamicEnd"))
    assert isinstance(target, IndirectBlockRange)

    instr = _MixedDestinations()
    instr.primary = target
    instr.grouped = []
    instr.outputs = {}
    instr.status = None

    assert instruction_write_targets(instr) == (target,)
    assert static_write_target_names(target) == frozenset()
    assert not instruction_writes_tag(instr, "DynamicValues5")


def test_pdg_retains_dynamic_range_region_and_address_reads() -> None:
    block = Block("WindowValues", TagType.INT, 1, 4)
    start = Int("WindowStart")
    end = Int("WindowEnd")
    with Program(strict=False) as logic:
        with Rung():
            fill(0, block.select(start, end))

    instr = logic.rungs[0]._instructions[0]
    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].data_reads == frozenset({"WindowStart", "WindowEnd"})
    assert graph.rung_nodes[0].writes == frozenset(
        {"WindowValues1", "WindowValues2", "WindowValues3", "WindowValues4"}
    )
    assert _writer_for_tag(logic.rungs[0], "WindowValues2") is None
    assert instruction_write_targets(instr) == (instr.dest,)


def test_static_copy_fanout_is_shared_by_pdg_and_writer_lookup() -> None:
    chars = Block("MessageChars", TagType.CHAR, 1, 4)
    with Program(strict=False) as logic:
        with Rung():
            copy("ABC", chars[1])

    instr = logic.rungs[0]._instructions[0]
    assert isinstance(instr, CopyInstruction)
    assert {
        name
        for target in instruction_write_targets(instr)
        for name in static_write_target_names(target)
    } == {"MessageChars1", "MessageChars2", "MessageChars3"}

    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].writes == frozenset(
        {"MessageChars1", "MessageChars2", "MessageChars3"}
    )
    for name in graph.rung_nodes[0].writes:
        assert _writer_for_tag(logic.rungs[0], name) is instr


def test_function_output_mapping_is_shared_by_pdg_and_writer_lookup() -> None:
    first = Int("FunctionFirst")
    second = Int("FunctionSecond")

    def values() -> dict[str, int]:
        return {"first": 1, "second": 2}

    with Program(strict=False) as logic:
        with Rung():
            run_function(values, outs={"first": first, "second": second})

    instr = logic.rungs[0]._instructions[0]
    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].writes == frozenset({"FunctionFirst", "FunctionSecond"})
    assert _writer_for_tag(logic.rungs[0], "FunctionFirst") is instr
    assert _writer_for_tag(logic.rungs[0], "FunctionSecond") is instr


def test_declared_status_fields_are_shared_by_pdg_and_writer_lookup() -> None:
    timer = Timer.clone("WriteSiteTimer")
    enable = Bool("WriteSiteEnable")
    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(timer, preset=5)

    instr = logic.rungs[0]._instructions[0]
    graph = build_program_graph(logic)
    expected = {
        "WriteSiteTimer_Done",
        "WriteSiteTimer_Acc",
        "WriteSiteTimer_EN",
        "WriteSiteTimer_TT",
    }
    assert graph.rung_nodes[0].writes == expected
    for name in expected:
        assert _writer_for_tag(logic.rungs[0], name) is instr
