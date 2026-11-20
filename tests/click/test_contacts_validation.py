"""CLICK validation of contacts and compares, which laddercodec's Contact and CompareContact check.

These tests cover how a refusal prints: the line that holds the contact, the caret under the
refused part, and the hint. A contact sits in a rung, a branch, a loop body rung or a pin such
as a counter's ``.reset()``. Each fault gets one finding, wherever the contact sits.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import (
    CLK_BANK_UNRESOLVED,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_STATUS_BIT_NOT_PORTABLE,
    validate_click_program,
)
from pyrung.core import (
    Bool,
    Counter,
    Int,
    Or,
    Timer,
    copy,
    count_up,
    fall,
    forloop,
    immediate,
    on_delay,
    out,
    rise,
)
from pyrung.core.program import Program, branch, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_COUNTER = Counter.clone("Cn")
_TIMER = Timer.clone("Tm")
_MAPS = [
    _COUNTER.Done.map_to(ct[1]),
    _COUNTER.Acc.map_to(ctd[1]),
    _TIMER.Done.map_to(t[1]),
    _TIMER.Acc.map_to(td[1]),
]


def _program(body) -> Program:
    prog = Program(strict=False)
    with prog:
        body()
    return prog


def _findings(body, mappings=()):
    tag_map = TagMap([*_MAPS, *mappings], include_system=False)
    report = validate_click_program(_program(body), tag_map, mode="strict")
    return [*report.errors, *report.warnings, *report.hints]


def _in_rung(condition):
    def body():
        with rung(x[1], condition()):
            out(y[1])

    return body


def _marked(finding) -> tuple[str, str]:
    """Return the line that the finding prints, and the text that its caret marks."""
    (frame,) = finding.display.frames
    _line, start, width = frame.caret
    return frame.lines[0], frame.lines[0][start : start + width]


# --- How a refusal prints ---------------------------------------------------------------


def test_a_refused_compare_prints_the_rung_with_a_caret_under_the_compare():
    (finding,) = _findings(_in_rung(lambda: txt[1] > txt[2]))
    assert finding.code == Code.TEXT_ORDER_COMPARE
    assert _marked(finding) == ("with rung(X001, TXT1 > TXT2):", "TXT1 > TXT2")
    assert finding.display.frames[0].caret_label == "'>' is not offered when Data A is text (TXT1)"
    assert finding.location == "main.rung[1].condition[1]"
    assert finding.message.startswith("CLICK refuses TXT1 > TXT2 at main.rung[1].condition[1].")
    assert finding.display.hint == (
        "A text compare takes == or !=. Compare text with == or !=, or copy the characters "
        "into Int tags (convert=to_ascii) and compare those."
    )
    assert finding.message.isascii() and finding.display.hint.isascii()


def test_the_label_gives_the_address_of_a_mapped_tag():
    sensor = Bool("Sensor")

    def body():
        with rung(immediate(sensor)):
            out(y[1])

    (finding,) = _findings(body, [sensor.map_to(c[1])])
    assert finding.code == Code.IMMEDIATE_CONTACT_NOT_X
    assert _marked(finding) == ("with rung(immediate(Sensor)):", "immediate(Sensor)")
    assert finding.display.frames[0].caret_label == "Sensor (C1) is not allowed"
    assert finding.suggestion == "Map it to an X input, or use a plain contact."


def test_the_hint_names_clicks_error():
    (finding,) = _findings(_in_rung(lambda: ds[1] == dh[1]))
    assert finding.code == Code.COMPARE_MIXED_TYPES
    assert _marked(finding) == ("with rung(X001, DS1 == DH1):", "DS1 == DH1")
    assert finding.display.hint.endswith("CLICK's error is OBJ-007.")
    assert "Copy the Word into a DS tag first" in finding.display.hint


def test_a_compare_in_an_or_marks_that_compare():
    def body():
        with rung(x[1], Or(x[2], txt[1] <= txt[2])):
            out(y[1])

    (finding,) = _findings(body)
    assert _marked(finding) == ("with rung(X001, Or(X002, TXT1 <= TXT2)):", "TXT1 <= TXT2")
    assert finding.location == "main.rung[1].condition[1].conditions[1]"


def test_a_compare_in_a_branch_prints_the_branch():
    def body():
        with rung(x[1]):
            out(y[1])
            with branch(ds[1] < txt[1]):
                out(y[2])

    (finding,) = _findings(body)
    assert finding.code == Code.COMPARE_MIXED_TYPES
    assert _marked(finding) == ("with branch(DS1 < TXT1):", "DS1 < TXT1")
    assert finding.display.frames[0].location == "Main:R1.0"


def test_a_compare_in_a_pin_prints_the_instruction():
    def body():
        with rung(x[1]):
            count_up(_COUNTER, preset=5).reset(txt[1] > txt[2])

    (finding,) = _findings(body)
    assert finding.code == Code.TEXT_ORDER_COMPARE
    assert _marked(finding) == ("count_up(Cn, preset=5).reset(TXT1 > TXT2)", "TXT1 > TXT2")
    assert finding.location == (
        "main.rung[1].instruction[0](CountUpInstruction).instruction.reset_condition"
    )


def test_a_bit_compare_in_a_pin_prints_as_written():
    # render_condition prints c[1] == True as C1, so the line must show the leaf as written.
    def body():
        with rung(x[1]):
            count_up(_COUNTER, preset=5).reset(c[1] == True)  # noqa: E712

    (finding,) = _findings(body)
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _marked(finding) == ("count_up(Cn, preset=5).reset(C1 == True)", "C1")
    assert finding.suggestion == "Write the contact C1 in place of the compare."


def test_a_compare_in_a_loop_body_prints_the_body_rung():
    def body():
        with rung(x[1]):
            with forloop(2):
                with rung(txt[1] >= txt[2]):
                    out(y[1])

    (finding,) = _findings(body)
    assert _marked(finding) == ("with rung(TXT1 >= TXT2):", "TXT1 >= TXT2")
    assert finding.location == "main.rung[1].body_rung[0].condition[0]"


# --- What CLICK accepts and refuses -----------------------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        lambda: x[1],
        lambda: ~c[2],
        lambda: rise(c[3]),
        lambda: fall(t[1]),
        lambda: ct[1],
        lambda: sc[1],
        lambda: immediate(x[2]),
        lambda: ~immediate(x[3]),
        lambda: ds[1] == 5,
        lambda: ds[1] < dd[1],
        lambda: df[1] > 1.5,
        lambda: dh[1] == dh[2],
        lambda: txt[1] == "A",
        lambda: txt[1] != txt[2],
        lambda: txt[1] == 'A"B',
    ],
    ids=[
        "x",
        "nc",
        "rise",
        "fall",
        "ct",
        "sc",
        "immediate",
        "immediate-nc",
        "ds-constant",
        "ds-dd",
        "df-real",
        "dh-dh",
        "txt-text",
        "txt-txt",
        "txt-quote",
    ],
)
def test_a_contact_that_click_takes_has_no_finding(condition):
    assert _findings(_in_rung(condition)) == []


@pytest.mark.parametrize(
    ("condition", "contact"),
    [
        (lambda: c[1] == True, "C1"),  # noqa: E712
        (lambda: c[2] == False, "~C2"),  # noqa: E712
        (lambda: c[3] != True, "~C3"),  # noqa: E712
    ],
    ids=["true", "false", "not-true"],
)
def test_a_compare_of_a_bit_is_refused_and_names_the_contact(condition, contact):
    # The export writes the compare as written, such as C1==1, and CLICK refuses a bit there.
    (finding,) = _findings(_in_rung(condition))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert finding.suggestion == f"Write the contact {contact} in place of the compare."
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(_in_rung(condition)), TagMap(_MAPS, include_system=False))


def test_a_text_constant_over_128_characters_is_refused():
    (finding,) = _findings(_in_rung(lambda: txt[1] == "A" * 129))
    assert finding.code == Code.CONSTANT_OUT_OF_RANGE


def test_an_unmapped_contact_has_one_finding():
    free = Bool("Free")
    (finding,) = _findings(_in_rung(lambda: free))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.location == "main.rung[1].condition[1].tag"


def test_an_immediate_edge_keeps_pyrungs_finding_alone():
    (finding,) = _findings(_in_rung(lambda: rise(immediate(x[1]))))
    assert finding.code == CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED


def test_the_export_refuses_a_refused_contact_until_it_is_allowed():
    prog = _program(_in_rung(lambda: txt[1] > txt[2]))
    tag_map = TagMap(_MAPS, include_system=False)
    with pytest.raises(LadderExportError, match=Code.TEXT_ORDER_COMPARE):
        pyrung_to_ladder(prog, tag_map)
    assert pyrung_to_ladder(prog, tag_map, allow=(Code.TEXT_ORDER_COMPARE,)).main_rows


@pytest.mark.parametrize(
    ("code", "replacement"),
    [
        ("CLK_IMMEDIATE_CONTACT_MUST_BE_X", "immediate-contact-not-x"),
        ("CLK_TEXT_ORDER_COMPARE", "text-order-compare"),
        ("CLK_COMPARE_HEX_MIX", "compare-mixed-types"),
    ],
)
def test_allow_names_the_code_that_replaces_an_old_one(code, replacement):
    prog = _program(_in_rung(lambda: txt[1] > txt[2]))
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass {replacement}"):
        pyrung_to_ladder(prog, TagMap(_MAPS, include_system=False), allow=(code,))


@pytest.mark.parametrize("code", [CLK_PTR_CONTEXT_ONLY_COPY, CLK_PTR_POINTER_MUST_BE_DS])
def test_an_old_pointer_code_no_longer_lets_a_pointer_compare_through(code):
    # Other checks still report the old codes, so allow= takes them without an error.
    prog = _program(_in_rung(lambda: ds[ds[5]] == 3))
    tag_map = TagMap(_MAPS, include_system=False)
    with pytest.raises(LadderExportError, match=Code.POINTER_OUTSIDE_COPY):
        pyrung_to_ladder(prog, tag_map, allow=(code,))
    assert pyrung_to_ladder(prog, tag_map, allow=(Code.POINTER_OUTSIDE_COPY,)).main_rows


# --- One finding for each fault ----------------------------------------------------------


def _pointer_in_rung():
    with rung(x[1], ds[ds[5]] == 3):
        out(y[1])


def _pointer_in_a_branch():
    with rung(x[1]):
        out(y[1])
        with branch(ds[ds[5]] > 1):
            out(y[2])


def _pointer_in_the_rung_of_a_branch():
    # The branch holds the rung's one condition as its first, so the walk of the branch
    # meets the pointer too.
    with rung(ds[ds[5]] == 3):
        out(y[1])
        with branch(x[2]):
            out(y[2])


def _pointer_in_a_pin():
    with rung(x[1]):
        count_up(_COUNTER, preset=5).reset(ds[ds[5]] == 2)


def _pointer_in_a_loop_body():
    with rung(x[1]):
        with forloop(2):
            with rung(ds[ds[5]] == 1):
                out(y[1])


@pytest.mark.parametrize(
    "body",
    [
        _pointer_in_rung,
        _pointer_in_a_branch,
        _pointer_in_the_rung_of_a_branch,
        _pointer_in_a_pin,
        _pointer_in_a_loop_body,
    ],
    ids=["rung", "branch", "rung-of-branch", "pin", "loop-body"],
)
def test_a_pointer_in_a_compare_has_one_finding(body):
    (finding,) = _findings(body)
    assert finding.code == Code.POINTER_OUTSIDE_COPY
    # pyrung's finding for the pointer is dropped, and its advice joins laddercodec's.
    assert finding.suggestion == (
        "Pointer DS[DS5] can only be used inside copy(). Use a direct tag reference here instead."
    )
    assert CLK_PTR_CONTEXT_ONLY_COPY not in finding.display.hint


def _status_bit_in_rung():
    with rung(_TIMER.TT):
        out(y[1])


def _status_bit_in_a_branch():
    with rung(x[1]):
        out(y[1])
        with branch(~_TIMER.EN):
            out(y[2])


def _status_bit_in_a_loop_body():
    with rung(x[1]):
        with forloop(2):
            with rung(Or(x[2], _TIMER.TT)):
                out(y[1])


@pytest.mark.parametrize(
    "body",
    [_status_bit_in_rung, _status_bit_in_a_branch, _status_bit_in_a_loop_body],
    ids=["rung", "branch", "loop-body"],
)
def test_a_status_bit_contact_has_one_finding(body):
    def program():
        with rung(x[9]):
            on_delay(_TIMER, preset=100)
        body()

    assert [f.code for f in _findings(program)] == [CLK_STATUS_BIT_NOT_PORTABLE]


def test_a_status_bit_in_a_pin_has_one_finding():
    # The counter's own check reports a pin's tag that has no CLICK address.
    def program():
        with rung(x[9]):
            on_delay(_TIMER, preset=100)
        with rung(x[1]):
            count_up(_COUNTER, preset=5).reset(_TIMER.EN)

    (finding,) = _findings(program)
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.location.endswith("instruction.reset_condition")


def test_an_immediate_contact_in_a_pin_has_one_finding():
    def body():
        with rung(x[1]):
            count_up(_COUNTER, preset=5).reset(~immediate(c[4]))

    (finding,) = _findings(body)
    assert finding.code == CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED


# --- Two instructions of one class in one loop ------------------------------------------


@pytest.mark.parametrize("bit_first", [True, False], ids=["bit-first", "register-first"])
def test_each_copy_in_a_loop_is_reported_once(bit_first):
    # Both copies sit at one location. Each copy's verdict is its own.
    def body():
        with rung(x[1]):
            with forloop(3):
                if bit_first:
                    copy(5, x[3])
                    copy(6, ds[4])
                else:
                    copy(6, ds[4])
                    copy(5, x[3])

    (finding,) = _findings(body)
    assert finding.code == Code.CONSTANT_INTO_BIT
    assert _marked(finding) == ("copy(5, X003)", "X003")


def test_two_refused_copies_in_a_loop_are_each_reported():
    def body():
        with rung(x[1]):
            with forloop(3):
                copy(5, x[3])
                copy(6, x[4])

    assert [_marked(f)[0] for f in _findings(body)] == ["copy(5, X003)", "copy(6, X004)"]


def test_a_pointer_fact_in_a_loop_keeps_pyrungs_finding():
    # The two copies share a location, so validation cannot tell which copy holds each
    # pointer. pyrung reports each pointer. laddercodec reports the copy that it refuses.
    def body():
        with rung(x[1]):
            with forloop(2):
                copy(ds[dd[1]], Int("FreeI"))
                copy(ds[dd[2]], ds[11])

    codes = sorted(f.code for f in _findings(body))
    assert codes == sorted(
        [
            CLK_BANK_UNRESOLVED,
            CLK_PTR_POINTER_MUST_BE_DS,
            CLK_PTR_POINTER_MUST_BE_DS,
            Code.POINTER_NOT_DS,
        ]
    )


def test_each_pointer_in_a_loop_keeps_its_own_advice():
    other = Counter.clone("Cm")

    def body():
        with rung(x[1]):
            with forloop(2):
                count_up(_COUNTER, preset=5).reset(ds[ds[5]] == 1)
                count_up(other, preset=5).reset(ds[ds[6]] == 2)

    findings = _findings(body, [other.Done.map_to(ct[2]), other.Acc.map_to(ctd[2])])
    pyrungs = [f.suggestion for f in findings if f.code == CLK_PTR_CONTEXT_ONLY_COPY]
    assert [s.split(" can ")[0] for s in pyrungs] == ["Pointer DS[DS5]", "Pointer DS[DS6]"]
    laddercodecs = [f for f in findings if f.code == Code.POINTER_OUTSIDE_COPY]
    assert len(laddercodecs) == 2
    assert all(f.suggestion is None for f in laddercodecs)
