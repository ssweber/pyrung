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


def test_instruction_frame_shows_the_call_with_a_caret_on_one_argument() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation._common import instruction_frame
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

    assert instruction_frame(location, copy, argument=1, caret_label="refused") == Frame(
        location="Mixer:R3.1",
        lines=("copy(Count, Count)",),
        caret=(0, 12, 5),
        caret_label="refused",
    )
    # With no argument, or an argument that the call does not show, there is no caret.
    for argument in (None, "convert"):
        frame = instruction_frame(location, copy, argument=argument, caret_label="refused")
        assert (frame.lines, frame.caret, frame.caret_label) == (("copy(Count, Count)",), None, "")


def test_instruction_frame_leaves_out_a_call_that_it_cannot_write_in_full() -> None:
    from pyrung.core.validation._common import instruction_frame
    from pyrung.core.validation.display import Frame
    from pyrung.core.validation.walker import ProgramLocation

    class CustomInstruction:
        pass

    location = ProgramLocation("main", None, 0, (), 0, "CustomInstruction", "instruction")

    assert instruction_frame(location, CustomInstruction(), argument=0) == Frame(location="Main:R1")


def test_instruction_frame_names_the_source_line_of_the_instruction() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation._common import instruction_frame
    from pyrung.core.validation.walker import ProgramLocation

    copy = CopyInstruction(1, Int("Count"))
    copy.source_file, copy.source_line = "plant.py", 12
    location = ProgramLocation("main", None, 0, (), 0, "CopyInstruction", "instruction")

    frame = instruction_frame(location, copy)
    assert (frame.location, frame.source) == ("Main:R1", "plant.py:12")


def test_location_frame_marks_a_span_of_its_line() -> None:
    from pyrung.core.validation._common import location_frame
    from pyrung.core.validation.display import Frame
    from pyrung.core.validation.walker import ProgramLocation

    location = ProgramLocation("main", None, 4, (), 0, "CopyInstruction", "instruction")

    assert location_frame(location, "DS[Ptr]", (3, 3), caret_label="unguarded") == Frame(
        location="Main:R5", lines=("DS[Ptr]",), caret=(0, 3, 3), caret_label="unguarded"
    )
    assert location_frame(location, "DS[Ptr]", caret_label="unguarded") == Frame(
        location="Main:R5", lines=("DS[Ptr]",)
    )
    # With no line, the frame holds the location alone.
    assert location_frame(location) == Frame(location="Main:R5")


def test_a_call_shows_what_its_author_wrote() -> None:
    from pyrung.click import ClickBlocks
    from pyrung.core.copy_converters import to_text
    from pyrung.core.instruction import BlockCopyInstruction, CopyInstruction
    from pyrung.core.validation.render import argument_span, render_instruction

    blocks = ClickBlocks()
    ds, txt = blocks.ds, blocks.txt

    terminated = CopyInstruction(ds[1], txt[1], convert=to_text(termination_code="$0D"))
    assert render_instruction(terminated, "")[0] == (
        "copy(DS1, TXT1, convert=to_text(termination_code='$0D'))"
    )
    # blockcopy() stores a dest tag as the range it writes.
    into_tag = BlockCopyInstruction(ds.select(1, 3), ds[10])
    assert argument_span(into_tag, 1) == ("blockcopy(DS1..DS3, DS10)", (20, 4))
    assert argument_span(CopyInstruction(ds[1], ds[2], oneshot=True), "oneshot") == (
        "copy(DS1, DS2, oneshot=True)",
        (23, 4),
    )


def test_a_keyword_that_holds_an_operand_shows_its_text() -> None:
    from pyrung import Block, Bool, Int, TagType
    from pyrung.core.instruction import SearchInstruction
    from pyrung.core.validation.render import argument_span

    values = Block("Values", TagType.INT, 1, 10)
    search = SearchInstruction(
        values.select(1, 10) >= Int("Target"), result=Int("Res"), found=Bool("Hit"), oneshot=1
    )

    code = "search(Values1..Values10 >= Target, result=Res, found=Hit, oneshot=True)"
    assert argument_span(search, 0) == (code, (7, 27))
    assert argument_span(search, "result") == (code, (43, 3))
    assert argument_span(search, "found") == (code, (54, 3))


def _declared_timer(unit: str = "ms", reset=None):
    """Return an on_delay() whose class declares its call, and one whose class does not."""
    from pyrung.core.instruction import OnDelayInstruction

    declared = type(
        "OnDelayInstruction",
        (OnDelayInstruction,),
        {
            "_call_operands": (("timer", ("done_bit", "accumulator")),),
            "_call_keyword_operands": (("preset", "preset"),),
            "_call_keywords": ("unit",),
            "_call_chained": (("reset", "reset_condition"),),
        },
    )
    timer = Timer.clone("RenderTest_Delay")
    return (
        declared(timer.Done, timer.Acc, 100, None, reset, unit=unit),
        OnDelayInstruction(timer.Done, timer.Acc, 100, None, reset, unit=unit),
    )


def test_a_declared_call_shows_a_structure_and_a_number() -> None:
    from pyrung.core.validation.render import _declared_arguments, _special_arguments

    declared, plain = _declared_timer()
    # The timer's UDT holds done_bit and accumulator, so the call names the structure. The
    # preset shows its number. The call leaves out the unit, which is the default.
    assert _declared_arguments(declared) == (
        "on_delay",
        [("timer", "RenderTest_Delay", "positional"), ("preset", "100", "keyword")],
    )
    # At the default unit, the declared call matches the special case. Any other unit shows
    # in the declared call only, as the next two tests check.
    assert _declared_arguments(declared) == _special_arguments(plain)


def test_a_declared_option_shows_the_options_value() -> None:
    from pyrung import Bool
    from pyrung.core.validation.render import declared_call

    declared, _plain = _declared_timer(unit="s", reset=Bool("Rst"))
    call = declared_call(declared)
    assert call.text == "on_delay(RenderTest_Delay, preset=100, unit='seconds').reset(Rst)"
    assert call.span("unit") == (44, 9)
    assert call.span("reset") == (61, 3)


def test_a_special_case_runs_before_a_declared_call() -> None:
    from pyrung.core.validation.render import declared_call, render_instruction

    declared, plain = _declared_timer(unit="s")
    # A family that declares the call keeps the frame of every other finding. Its own
    # findings show the declared call.
    assert render_instruction(declared, "") == render_instruction(plain, "")
    assert declared_call(declared).text != render_instruction(plain, "")[0]


def test_a_chained_call_shows_its_condition() -> None:
    from pyrung import Block, Bool, TagType
    from pyrung.core.instruction import ShiftInstruction
    from pyrung.core.validation.render import argument_span

    declared = type(
        "ShiftInstruction",
        (ShiftInstruction,),
        {
            "_call_operands": (("bit_range", "bit_range"),),
            "_call_keywords": (),
            "_call_chained": (("clock", "clock_condition"), ("reset", "reset_condition")),
        },
    )
    bits = Block("Bits", TagType.BOOL, 1, 8)
    shift = declared(bits.select(1, 8), None, Bool("Clk"), ~Bool("Rst"))

    code = "shift(Bits1..Bits8).clock(Clk).reset(~Rst)"
    assert argument_span(shift, 0) == (code, (6, 12))
    assert argument_span(shift, "clock") == (code, (26, 3))
    assert argument_span(shift, "reset") == (code, (37, 4))


def test_a_list_argument_keeps_the_span_of_each_item() -> None:
    from pyrung.core.validation.render import call_of

    call = call_of("drum", [("outputs", ["Out1", "X009"], "keyword"), ("reset", "Rst", "chained")])
    assert call.text == "drum(outputs=[Out1, X009]).reset(Rst)"
    assert call.span("outputs") == (13, 12)
    assert call.span("outputs", 1) == (20, 4)
    # A call that has no such item marks the whole list.
    assert call.span("outputs", 2) == (13, 12)
    assert call.span("reset", 0) == (33, 3)


def test_a_declared_list_operand_shows_each_item() -> None:
    from pyrung import Int
    from pyrung.core.instruction import CopyInstruction
    from pyrung.core.validation.render import argument_span, declared_call

    # No builder accepts a list operand. copy(), fill(), out() and pack_bits() each refuse
    # one. A stand-in class with the name of a Copy holds one, to test the list branch of
    # declared_call().
    def hold(self, source, dest) -> None:
        vars(self).update(source=source, dest=dest, convert=None, oneshot=False)

    stand_in = type("CopyInstruction", (CopyInstruction,), {"__init__": hold})
    copy = stand_in(5, [Int("A"), Int("Bee")])
    assert declared_call(copy).text == "copy(5, [A, Bee])"
    assert argument_span(copy, 1) == ("copy(5, [A, Bee])", (8, 8))
    assert argument_span(copy, 1, 1) == ("copy(5, [A, Bee])", (12, 3))
