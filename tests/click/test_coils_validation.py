"""CLICK validation of out(), latch() and reset(), which laddercodec's Coil checks.

These tests cover how a refusal prints: the call, the caret under the target, and the hint.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Block, Bool, Int, TagType, immediate
from pyrung.core.program import Program, latch, out, reset, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=()):
    tag_map = TagMap(list(mappings), include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _frame(finding):
    (frame,) = finding.display.frames
    return frame.lines, frame.caret


def test_a_refused_target_marks_the_target():
    (finding,) = _errors(lambda: out(ds[1]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("out(DS1)",), (0, 4, 3))
    assert finding.display.frames[0].caret_label == "DS1 is not allowed"
    assert "This field takes Y, C, SC." in finding.display.hint
    assert "OBJ-004" in finding.display.hint
    assert finding.location == "main.rung[1].instruction[0](OutInstruction).instruction"


@pytest.mark.parametrize(
    "write, line",
    [
        (lambda: out(ds[1]), "out(DS1)"),
        (lambda: latch(ds[1]), "latch(DS1)"),
        (lambda: reset(ds[1]), "reset(DS1)"),
    ],
)
def test_a_register_target_gets_the_advice_to_copy_or_fill(write, line):
    (finding,) = _errors(write)
    assert _frame(finding)[0] == (line,)
    assert finding.suggestion == "Write a register with copy() or fill()."
    assert finding.suggestion in finding.display.hint


def test_a_bit_target_that_a_coil_cannot_write_has_no_register_advice():
    (finding,) = _errors(lambda: out(x[1]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert finding.suggestion is None


def test_the_label_gives_the_address_and_not_the_tags_name():
    lamp = Int("Lamp")
    (finding,) = _errors(lambda: out(lamp), [lamp.map_to(ds[5])])
    assert _frame(finding)[0] == ("out(Lamp)",)
    label = finding.display.frames[0].caret_label
    assert "DS5" in label
    assert "Lamp" not in label


def test_a_oneshot_out_shows_its_keyword():
    (finding,) = _errors(lambda: out(ds[1], oneshot=True))
    assert _frame(finding)[0] == ("out(DS1, oneshot=True)",)
    assert _frame(finding)[1] == (0, 4, 3)


def test_a_range_target_marks_the_range():
    findings = _errors(lambda: reset(ds.select(1, 3)))
    assert {f.code for f in findings} == {Code.BANK_NOT_ALLOWED}
    assert _frame(findings[0]) == (("reset(DS1..DS3)",), (0, 6, 8))


def test_an_immediate_coil_outside_y_marks_the_target():
    (finding,) = _errors(lambda: out(immediate(c[1])))
    assert finding.code == Code.IMMEDIATE_COIL_NOT_Y
    assert "out(" in _frame(finding)[0][0]
    assert finding.display.frames[0].caret is not None


def test_a_latch_has_no_oneshot_to_refuse():
    (finding,) = _errors(lambda: latch(ds[1]))
    assert finding.code == Code.BANK_NOT_ALLOWED


@pytest.mark.parametrize(
    "write",
    [
        lambda: out(y[1]),
        lambda: out(c[1], oneshot=True),
        lambda: latch(c[2]),
        lambda: reset(c.select(2, 4)),
        lambda: reset(sc[50]),
        lambda: out(sc[162]),
        lambda: latch(sc[162]),
        lambda: out(immediate(y[1])),
        lambda: out(immediate(y.select(1, 4))),
    ],
)
def test_a_coil_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_an_unmapped_target_names_the_target():
    target = Bool("Lamp")
    (finding,) = _errors(lambda: out(target))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith("pyrung cannot find one CLICK address for the target")


def test_a_range_of_scattered_addresses_is_refused():
    gap = Block("Gap", TagType.BOOL, 1, 3)
    (finding,) = _errors(
        lambda: out(gap.select(1, 3)),
        [gap[1].map_to(c[1]), gap[2].map_to(c[2]), gap[3].map_to(c[5])],
    )
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"


@pytest.mark.parametrize(
    "mapping",
    [
        [("y", 1), ("y", 3), ("y", 4)],
        [("y", 3), ("y", 2), ("y", 1)],
        [("y", 1), ("c", 2), ("y", 3)],
    ],
    ids=["gap", "descending", "mixed bank"],
)
def test_an_immediate_range_that_click_cannot_hold_is_refused_by_both(mapping):
    gap = Block("Gap", TagType.BOOL, 1, 3)
    banks = {"y": y, "c": c}
    maps = [gap[i + 1].map_to(banks[bank][n]) for i, (bank, n) in enumerate(mapping)]
    write = lambda: out(immediate(gap.select(1, 3)))  # noqa: E731
    (finding,) = _errors(write, maps)
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"
    assert finding.message.startswith("CLICK refuses out(")
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE"):
        pyrung_to_ladder(_program(write), TagMap(maps, include_system=False))


@pytest.mark.parametrize("write", [lambda: out(c.select(1, 1)), lambda: reset(c.select(7, 7))])
def test_a_range_of_one_address_is_refused(write):
    # CLICK refuses a range whose end is not above its start (OBJ-012).
    (finding,) = _errors(write)
    assert finding.code == Code.RANGE_NOT_ASCENDING


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_a_range_across_read_only_sc_bits_passes_and_exports_as_typed(coil):
    # CLICK v4.00's editor accepts SC50..SC60 in Out, Set and Reset and saves it as typed.
    write = lambda: coil(sc.select(50, 60))  # noqa: E731
    assert _errors(write) == ()
    assert pyrung_to_ladder(_program(write), TagMap()).main_rows


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_every_coil_takes_the_same_writable_sc_bits(coil):
    # Out, Set and Reset take SC162 and refuse SC49 and SC163 (OBJ-003).
    assert _errors(lambda: coil(sc[162])) == ()
    for address in (49, 163):
        (finding,) = _errors(lambda a=address: coil(sc[a]))
        assert finding.code == Code.NOT_WRITABLE


def test_the_export_refuses_a_coil_that_click_refuses():
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(lambda: out(ds[1])), TagMap())


def test_the_export_refuses_an_immediate_coil_outside_y():
    with pytest.raises(LadderExportError, match="Y"):
        pyrung_to_ladder(_program(lambda: latch(immediate(c[1]))), TagMap())


@pytest.mark.parametrize(
    ("code", "replacement"),
    [
        ("CLK_BANK_NOT_WRITABLE", "not-writable or bank-not-allowed"),
        ("CLK_BANK_WRONG_ROLE", "bank-not-allowed, invalid-modbus-target or not-an-address"),
    ],
)
def test_allow_names_what_replaced_a_removed_bank_code(code, replacement):
    write = lambda: out(ds[1])  # noqa: E731
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass {replacement} to"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(code,))
    assert pyrung_to_ladder(_program(write), TagMap(), allow=(Code.BANK_NOT_ALLOWED,)).main_rows


@pytest.mark.parametrize(
    ("code", "replacement"),
    [
        ("CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y", "immediate-coil-not-y"),
        ("CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS", "CLK_COPY_RANGE_NOT_CONSECUTIVE"),
    ],
)
def test_allow_names_what_replaced_a_removed_immediate_code(code, replacement):
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass {replacement}"):
        pyrung_to_ladder(_program(lambda: out(immediate(y[1]))), TagMap(), allow=(code,))


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_the_export_names_the_code_of_an_immediate_range_it_refuses_when_allowed(coil):
    gap = Block("Gap", TagType.BOOL, 1, 3)
    maps = [gap[1].map_to(y[1]), gap[2].map_to(y[3]), gap[3].map_to(y[4])]
    write = lambda: coil(immediate(gap.select(1, 3)))  # noqa: E731
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE: Gap1..Gap3"):
        pyrung_to_ladder(
            _program(write),
            TagMap(maps, include_system=False),
            allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",),
        )


@pytest.mark.parametrize("coil", [out, latch, reset])
@pytest.mark.parametrize(
    "mapping",
    [[("y", 1), ("y", 3), ("y", 4)], [("y", 1), ("c", 2), ("y", 3)]],
    ids=["gap", "mixed bank"],
)
def test_the_export_refuses_a_plain_coil_range_with_a_gap_even_when_allowed(coil, mapping):
    gap = Block("Gap", TagType.BOOL, 1, 3)
    banks = {"y": y, "c": c}
    maps = [gap[i + 1].map_to(banks[b][n]) for i, (b, n) in enumerate(mapping)]
    write = lambda: coil(gap.select(1, 3))  # noqa: E731
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE: Gap1..Gap3"):
        pyrung_to_ladder(
            _program(write),
            TagMap(maps, include_system=False),
            allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",),
        )


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_the_export_refuses_a_descending_coil_range_even_when_allowed(coil):
    desc = Block("Desc", TagType.BOOL, 1, 3)
    maps = [desc[1].map_to(y[3]), desc[2].map_to(y[2]), desc[3].map_to(y[1])]
    write = lambda: coil(desc.select(1, 3))  # noqa: E731
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE: Desc1..Desc3"):
        pyrung_to_ladder(
            _program(write),
            TagMap(maps, include_system=False),
            allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",),
        )


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_the_export_refuses_a_reversed_coil_range_even_when_allowed(coil):
    write = lambda: coil(c.select(1, 3).reverse())  # noqa: E731
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE: C1..C3"):
        pyrung_to_ladder(_program(write), TagMap(), allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",))


@pytest.mark.parametrize("coil", [out, latch, reset])
def test_the_export_writes_an_ascending_coil_range_as_a_range(coil):
    asc = Block("Asc", TagType.BOOL, 1, 3)
    maps = [asc[1].map_to(y[5]), asc[2].map_to(y[6]), asc[3].map_to(y[7])]
    for write, tag_map in (
        (lambda: coil(c.select(1, 3)), TagMap()),
        (lambda: coil(asc.select(1, 3)), TagMap(maps, include_system=False)),
    ):
        assert pyrung_to_ladder(_program(write), tag_map).main_rows


def test_the_export_names_the_code_of_an_immediate_target_outside_y_when_allowed():
    with pytest.raises(LadderExportError, match="immediate-coil-not-y: immediate"):
        pyrung_to_ladder(
            _program(lambda: out(immediate(c[1]))), TagMap(), allow=(Code.IMMEDIATE_COIL_NOT_Y,)
        )


def test_an_immediate_coil_prints_its_immediate_call():
    (finding,) = _errors(lambda: out(immediate(c[1]), oneshot=True))
    assert _frame(finding)[0] == ("out(immediate(C1), oneshot=True)",)
