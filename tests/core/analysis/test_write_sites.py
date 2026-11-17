"""Test the shared discovery of the destinations that an instruction writes."""

from __future__ import annotations

import math
from typing import Any

import pytest

from pyrung import (
    PLC,
    Block,
    Bool,
    Char,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    Word,
    copy,
    fill,
    immediate,
    on_delay,
    run_function,
    to_text,
)
from pyrung.click import ClickBlocks
from pyrung.core.analysis import build_program_graph
from pyrung.core.analysis.sp_values import _writer_for_tag
from pyrung.core.analysis.write_sites import (
    _to_text_length,
    instruction_partial_write_targets,
    instruction_write_targets,
    instruction_writes_tag,
    static_write_target_names,
)
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.memory_block import IndirectBlockRange
from pyrung.core.storage import BLANK_CHAR, REAL_MAX


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
    instr.primary = target  # ty: ignore[invalid-assignment]
    instr.grouped = []
    instr.outputs = {}
    instr.status = None  # ty: ignore[invalid-assignment]

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


# ---------------------------------------------------------------------------
# The characters a to_text() copy writes
# ---------------------------------------------------------------------------

#: The most characters a to_text() copy of each type writes, by (suppress_zero, exponential).
#: A termination code adds one.
_TEXT_BOUNDS = {
    TagType.BOOL: {(True, False): 1, (False, False): 5},
    TagType.INT: {(True, False): 6, (False, False): 6},
    TagType.DINT: {(True, False): 11, (False, False): 11},
    TagType.WORD: {(True, False): 4, (False, False): 4},
    # pyrung keeps a Real at 64-bit precision, so "-4.9406565E-324" is 15 characters.
    TagType.REAL: {(True, False): 48, (True, True): 15},
    TagType.CHAR: {(True, False): 1, (False, False): 5},
}
_TAG_OF = {
    TagType.BOOL: Bool,
    TagType.INT: Int,
    TagType.DINT: Dint,
    TagType.WORD: Word,
    TagType.REAL: Real,
    TagType.CHAR: Char,
}


@pytest.mark.parametrize(
    ("source_type", "suppress_zero", "exponential", "termination", "expected"),
    [
        (source_type, suppress_zero, exponential, termination, count + (termination is not None))
        for source_type, bounds in _TEXT_BOUNDS.items()
        for (suppress_zero, exponential), count in bounds.items()
        for termination in (None, 13)
    ],
)
def test_to_text_of_a_tag_counts_the_longest_text_of_its_type(
    source_type: TagType,
    suppress_zero: bool,
    exponential: bool,
    termination: int | None,
    expected: int,
) -> None:
    convert = to_text(
        suppress_zero=suppress_zero, exponential=exponential, termination_code=termination
    )
    instr = CopyInstruction(_TAG_OF[source_type]("TextSource"), Char("TextDest1"), convert=convert)
    assert _to_text_length(instr) == expected


#: Each type's extremes, with the options that write the longest text of each.
_EXTREMES: list[tuple[Any, Any, list[Any]]] = [
    (Bool, to_text(), [False, True]),
    (Bool, to_text(suppress_zero=False), [False, True]),
    (Char, to_text(), ["0", "9"]),
    (Char, to_text(suppress_zero=False), ["0", "9"]),
    (Int, to_text(), [-32768, 32767, 0]),
    (Int, to_text(suppress_zero=False), [-32768, 32767, 0]),
    (Dint, to_text(), [-2147483648, 2147483647, 0]),
    (Word, to_text(), [0xFFFF, 0]),
    (Word, to_text(suppress_zero=False), [0xFFFF, 0]),
    (Real, to_text(), [REAL_MAX, -REAL_MAX, math.nan, math.inf, -math.inf, 0.0]),
    (
        Real,
        to_text(exponential=True),
        [REAL_MAX, -REAL_MAX, math.nan, math.inf, -math.inf, -5e-324, 1e-45],
    ),
    (Real, to_text(termination_code=13), [-REAL_MAX, math.nan]),
]


@pytest.mark.parametrize(("make", "convert", "values"), _EXTREMES)
def test_to_text_writes_only_declared_characters_and_reaches_the_last(
    make: Any, convert: Any, values: list[Any]
) -> None:
    """Check that each value writes inside the declared characters and the longest fills them all."""
    msg = Block("BoundMsg", TagType.CHAR, 1, 100)
    source = make("BoundSource")
    with Program(strict=False) as logic:
        with Rung():
            fill(BLANK_CHAR, msg.select(1, 100))
            copy(source, msg[1], convert=convert)
    (_fill, instr) = logic.rungs[0]._instructions
    declared = {
        name
        for target in instruction_write_targets(instr)
        for name in static_write_target_names(target)
    }

    longest = 0
    for value in values:
        plc = PLC(logic)
        plc.patch({source.name: value})
        plc.step()
        tags = plc.current_state.tags
        written = {
            f"BoundMsg{a}"
            for a in range(1, 101)
            if tags.get(f"BoundMsg{a}", BLANK_CHAR) != BLANK_CHAR
        }
        assert written <= declared, (value, sorted(written - declared))
        longest = max(longest, len(written))
    assert longest == len(declared)


def test_to_text_stops_at_the_end_of_the_block() -> None:
    txt = ClickBlocks().txt
    with Program(strict=False) as logic:
        with Rung():
            copy(Real("EndSource"), txt[995], convert=to_text())
    (instr,) = logic.rungs[0]._instructions
    names = [getattr(target, "name", None) for target in instruction_write_targets(instr)]
    assert names == [f"TXT{a}" for a in range(995, 1001)]


def test_to_text_of_a_literal_writes_its_own_text_every_time() -> None:
    msg = Block("LiteralMsg", TagType.CHAR, 1, 20)
    with Program(strict=False) as logic:
        with Rung():
            copy(12345, msg[1], convert=to_text())
            copy(-0.25, msg[10], convert=to_text())
    first, second = logic.rungs[0]._instructions
    for instr, start, count in ((first, 1, 5), (second, 10, 10)):
        assert [target.name for target in instruction_write_targets(instr)] == [
            f"LiteralMsg{a}" for a in range(start, start + count)
        ]
        assert instruction_partial_write_targets(instr) == ()


def test_the_characters_past_the_first_are_writes_the_copy_may_not_make() -> None:
    """A short value may leave the later characters unwritten.

    So no read of them is write-before-read.
    """
    msg = Block("PartialMsg", TagType.CHAR, 1, 20)
    with Program(strict=False) as logic:
        with Rung():
            copy(Int("PartialSource"), msg[1], convert=to_text())
        with Rung(msg[1] == "1", msg[5] == "5"):
            copy(1, Int("PartialSeen"))
    (instr,) = logic.rungs[0]._instructions
    assert [target.name for target in instruction_partial_write_targets(instr)] == [
        f"PartialMsg{a}" for a in range(2, 7)
    ]
    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].writes >= {f"PartialMsg{a}" for a in range(1, 7)}
    assert graph.unconditional_write_before_read("PartialMsg1")
    assert not graph.unconditional_write_before_read("PartialMsg5")


def test_to_text_of_an_expression_runs_to_the_end_of_the_block() -> None:
    msg = Block("ExprMsg", TagType.CHAR, 1, 30)
    source = Dint("ExprSource")
    with Program(strict=False) as logic:
        with Rung():
            copy(source * source, msg[20], convert=to_text())
    (instr,) = logic.rungs[0]._instructions
    assert [target.name for target in instruction_write_targets(instr)] == [
        f"ExprMsg{a}" for a in range(20, 31)
    ]


def test_to_text_through_a_bounded_pointer_follows_the_pointer() -> None:
    txt = ClickBlocks().txt
    pointer = Int("TextPointer", choices={990: "near the end", 995: "nearer"})
    with Program(strict=False) as logic:
        with Rung():
            copy(Int("TextPointerSource"), txt[pointer], convert=to_text())
    (instr,) = logic.rungs[0]._instructions
    # Every call returns the same targets, so an analysis that keys a pointer by id finds it again.
    targets = instruction_write_targets(instr)
    assert all(a is b for a, b in zip(targets, instruction_write_targets(instr), strict=True))
    assert len(instruction_partial_write_targets(instr)) == 5
    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].writes == {f"TXT{a}" for a in range(990, 1001)}


def test_to_text_through_an_unbounded_pointer_writes_the_whole_bank() -> None:
    txt = ClickBlocks().txt
    with Program(strict=False) as logic:
        with Rung():
            copy(Real("BankSource"), txt[Int("BankPointer")], convert=to_text())
    graph = build_program_graph(logic)
    assert graph.rung_nodes[0].writes == {f"TXT{a}" for a in range(1, 1001)}


def test_a_destination_mapped_after_the_first_call_gets_its_new_bank() -> None:
    message = Char("LaterMsg")
    literal_dest = Char("LaterLiteral")
    with Program(strict=False) as logic:
        with Rung():
            copy(Int("LaterSource"), message, convert=to_text())
            copy("ABC", literal_dest)
    text, literal = logic.rungs[0]._instructions

    def names(instr: Any) -> list[str]:
        return [n for t in instruction_write_targets(instr) for n in static_write_target_names(t)]

    assert names(text) == ["LaterMsg"]
    assert names(literal) == ["LaterLiteral"]
    txt = ClickBlocks().txt
    message.map_to(txt[1])
    literal_dest.map_to(txt[10])
    assert names(text) == ["LaterMsg", *(f"TXT{a}" for a in range(2, 7))]
    assert names(literal) == ["LaterLiteral", "TXT11", "TXT12"]
    # A tag mapped onto an address that the text reaches takes the place of that address.
    Char("LaterThird").map_to(txt[3])
    assert names(text) == ["LaterMsg", "TXT2", "LaterThird", "TXT4", "TXT5", "TXT6"]


def test_the_analysis_does_not_keep_a_mapped_bank_alive() -> None:
    """The mapping holds the bank weakly. Once the bank is gone, the text has no block."""
    import gc
    import weakref

    message = Char("GoneMsg")
    with Program(strict=False) as logic:
        with Rung():
            copy("ABC", message)
    (instr,) = logic.rungs[0]._instructions
    txt = ClickBlocks().txt
    message.map_to(txt[1])
    assert len(instruction_write_targets(instr)) == 3
    bank = weakref.ref(txt)
    del txt
    gc.collect()
    assert bank() is None
    # A tag in no block counts on by name, and a plain name has no number to count from.
    assert [t.name for t in instruction_write_targets(instr)] == ["GoneMsg"]
