"""Tests for source-shaped validation and analysis rendering."""

from pyrung import Bool, Timer
from pyrung.core.condition import AllCondition
from pyrung.core.validation.render import operand_name, render_condition


def test_boolean_literal_comparisons_render_as_contacts() -> None:
    flag = Bool("RenderTest_Flag")

    assert render_condition(flag == False) == "~RenderTest_Flag"  # noqa: E712
    assert render_condition(flag != True) == "~RenderTest_Flag"  # noqa: E712
    assert render_condition(flag == True) == "RenderTest_Flag"  # noqa: E712
    assert render_condition(flag != False) == "RenderTest_Flag"  # noqa: E712


def test_structure_field_uses_its_source_access_path() -> None:
    timer = Timer.clone("RenderTest_Timer")
    sensor = Bool("RenderTest_Sensor")

    assert timer.Done.name == "RenderTest_Timer_Done"
    assert operand_name(timer.Done) == "RenderTest_Timer.Done"
    guard = AllCondition(timer.Done == False, sensor != True)  # noqa: E712
    assert render_condition(guard) == "And(~RenderTest_Timer.Done, ~RenderTest_Sensor)"


def test_render_expr_spells_functions_and_range_aggregates_as_source() -> None:
    from pyrung import Block, Int, TagType
    from pyrung.core.expression import lsh, sqrt
    from pyrung.core.validation.render import render_expr

    x = Int("RenderTest_X")
    ds = Block("DS", TagType.INT, 1, 10)

    assert render_expr(abs(x)) == "abs(RenderTest_X)"
    assert render_expr(sqrt(x)) == "sqrt(RenderTest_X)"
    assert render_expr(lsh(x, 2)) == "lsh(RenderTest_X, 2)"
    assert render_expr(ds.select(1, 5).sum()) == "DS.select(1, 5).sum()"
    assert render_expr(ds.select(1, 5).median()) == "DS.select(1, 5).median()"
    assert render_expr(ds.select(1, 5).max() + 1) == "(DS.select(1, 5).max() + 1)"


def test_argument_span_marks_an_argument_by_position_or_by_name() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation.render import argument_span

    count = Int("Count")
    copy = CopyInstruction(count, count)

    # A search for the text "Count" would mark the source.
    assert argument_span(copy, 1) == ("copy(Count, Count)", (12, 5))
    assert argument_span(copy, "target") == ("copy(Count, Count)", (12, 5))
    assert argument_span(copy, 0) == ("copy(Count, Count)", (5, 5))
    assert argument_span(copy, 2) == ("copy(Count, Count)", None)
    assert argument_span(copy, "convert") == ("copy(Count, Count)", None)


def test_argument_span_marks_a_name_that_holds_another_name() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation.render import argument_span

    copy = CopyInstruction(Int("DS10"), Int("DS1"))

    assert argument_span(copy, 1) == ("copy(DS10, DS1)", (11, 3))


def test_argument_span_marks_the_value_of_a_keyword_argument() -> None:
    from pyrung import Char, Int
    from pyrung.core.copy_converters import to_text
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation.render import argument_span

    copy = CopyInstruction(Int("Count"), Char("Shown"), convert=to_text(suppress_zero=False))

    code = "copy(Count, Shown, convert=to_text(suppress_zero=False))"
    assert argument_span(copy, "convert") == (code, (27, 28))


def test_a_copy_shows_only_the_converter_that_its_author_wrote() -> None:
    from pyrung import Char, Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation.render import render_instruction

    # A number into a Char runs to_text() without one.
    copy = CopyInstruction(Int("Count"), Char("Shown"))

    assert copy.convert is not None
    assert render_instruction(copy, "")[0] == "copy(Count, Shown)"


def test_pack_and_unpack_render_both_operands() -> None:
    from pyrung import Block, Int, TagType
    from pyrung.core.instruction import (
        PackBitsInstruction,
        PackTextInstruction,
        UnpackToWordsInstruction,
    )
    from pyrung.core.validation.render import argument_span

    bits = Block("Bits", TagType.BOOL, 1, 16)
    words = Block("Words", TagType.INT, 1, 2)
    text = Block("Text", TagType.CHAR, 1, 3)
    packed = Int("Packed")

    assert argument_span(PackBitsInstruction(bits.select(1, 16), packed), "dest") == (
        "pack_bits(Bits1..Bits16, Packed)",
        (25, 6),
    )
    assert argument_span(UnpackToWordsInstruction(packed, words.select(1, 2)), 1) == (
        "unpack_to_words(Packed, Words1..Words2)",
        (24, 14),
    )
    pack_text = PackTextInstruction(text.select(1, 3), packed, allow_whitespace=True)
    assert argument_span(pack_text, "allow_whitespace") == (
        "pack_text(Text1..Text3, Packed, allow_whitespace=True)",
        (49, 4),
    )


def test_location_frame_shows_the_call_with_a_caret_on_one_argument() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation._common import location_frame
    from pyrung.core.validation.display import Frame
    from pyrung.core.validation.walker import ProgramLocation

    count = Int("Count")
    copy = CopyInstruction(count, count)
    location = ProgramLocation(
        scope="subroutine",
        subroutine="Mixer",
        rung_index=2,
        branch_path=(1,),
        instruction_index=0,
        instruction_type="CopyInstruction",
        arg_path="instruction",
    )

    assert location_frame(location, copy, argument=1, caret_label="refused") == Frame(
        location="Mixer:R3.1",
        lines=("copy(Count, Count)",),
        caret=(0, 12, 5),
        caret_label="refused",
    )
    # With no argument, or an argument that the call does not show, there is no caret.
    for argument in (None, "convert"):
        frame = location_frame(location, copy, argument=argument, caret_label="refused")
        assert (frame.lines, frame.caret, frame.caret_label) == (("copy(Count, Count)",), None, "")
    # With no instruction, the frame holds the location alone.
    assert location_frame(location) == Frame(location="Mixer:R3.1")


def test_location_frame_leaves_out_a_call_that_it_cannot_write_in_full() -> None:
    from pyrung.core.validation._common import location_frame
    from pyrung.core.validation.display import Frame
    from pyrung.core.validation.walker import ProgramLocation

    class CustomInstruction:
        pass

    location = ProgramLocation("main", None, 0, (), 0, "CustomInstruction", "instruction")

    assert location_frame(location, CustomInstruction(), argument=0) == Frame(location="Main:R1")


def test_location_frame_names_the_source_line_of_the_instruction() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation._common import location_frame
    from pyrung.core.validation.walker import ProgramLocation

    copy = CopyInstruction(1, Int("Count"))
    copy.source_file, copy.source_line = "plant.py", 12
    location = ProgramLocation("main", None, 0, (), 0, "CopyInstruction", "instruction")

    frame = location_frame(location, copy)
    assert (frame.location, frame.source) == ("Main:R1", "plant.py:12")
