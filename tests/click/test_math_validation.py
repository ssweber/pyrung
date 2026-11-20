"""CLICK validation of math(), which laddercodec's Math checks.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. The formula is one argument, so the caret marks the whole formula and the label
names the operand that CLICK refuses.
"""

from __future__ import annotations

import pytest
from laddercodec import Code

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_BANK_UNRESOLVED, validate_click_program
from pyrung.core import Block, Int, Tag, TagType, Word, randint
from pyrung.core.expression import RandIntExpr, lsh
from pyrung.core.program import (
    Program,
    branch,
    call,
    forloop,
    math,
    out,
    rung,
    subroutine,
)

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


def test_a_refused_operand_marks_the_formula():
    (finding,) = _errors(lambda: math(ds[1] + c[1], ds[100]))
    assert finding.code == Code.MATH_OPERAND_BANK
    assert _frame(finding) == (("math((DS1 + C1), DS100)",), (0, 5, 10))
    assert finding.display.frames[0].caret_label == "C1 is refused"
    assert "select(1, 1).sum()" in finding.suggestion
    assert finding.location == "main.rung[1].instruction[0](CalcInstruction).instruction"


def test_a_refused_destination_marks_the_destination():
    result = Word("Result")
    (finding,) = _errors(lambda: math(ds[1] + 1, result), [result.map_to(yd[1])])
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _frame(finding) == (("math((DS1 + 1), Result)",), (0, 16, 6))
    # The label gives the address, not the tag's name.
    label = finding.display.frames[0].caret_label
    assert label == "YD1 is not allowed"
    # A Word result beside other tags makes a decimal Math, so the advice is to split it.
    assert finding.suggestion.startswith("Split it into a math() of WORD tags")


def test_the_hint_gives_the_advice_and_clicks_error():
    (finding,) = _errors(lambda: math(lsh(ds[1], 2), ds[100]))
    assert finding.code == Code.MATH_FUNCTION_MODE
    assert finding.suggestion == "Move it into a separate math() of WORD tags in DH."
    assert finding.suggestion in finding.display.hint
    assert "OBJ-1006" in finding.display.hint
    assert finding.message.isascii()


def test_a_read_only_sd_result_is_not_writable():
    (finding,) = _errors(lambda: math(ds[1] + 1, sd[1]))
    assert finding.code == Code.NOT_WRITABLE
    assert "OBJ-003" in finding.display.hint


@pytest.mark.parametrize(
    "write",
    [
        lambda: math(ds[1] * 2 + dd[1] / 3 - df[1], df[100]),
        lambda: math(dh[1] & dh[2] | lsh(dh[3], 2), dh[100]),
        lambda: math(dh[1] + dh[2], yd[1]),
        lambda: math(ds[1] + 1, sd[29]),
        lambda: math(ds[1] + 1, td[1]),
        lambda: math(dd[1] + 1, ctd[1]),
        lambda: math(c.select(1, 8).sum() + 5, ds[100]),
        lambda: math(x.select(1, 16).sum(), ds[100]),
        lambda: math(xd.select(1, 4).sum(), dh[100]),
        lambda: math(ds.select(1, 512).median(), ds[100]),
        lambda: math(randint(ds[1], 10), ds[100]),
        lambda: math(randint(dh[1], dh[2]), dh[100]),
        lambda: math(randint(10, 1), ds[100]),
        lambda: math(dh[1] + -1, dh[100]),
        lambda: math(dd[1] * 2147483647, dd[100]),
        lambda: math(ds[1] + 1, ds[2], oneshot=True),
    ],
    ids=[
        "decimal",
        "hex",
        "hex-into-YD",
        "writable-SD",
        "into-TD",
        "into-CTD",
        "bit-sum",
        "X-sum",
        "XD-sum",
        "median-of-512",
        "rand-of-DS",
        "rand-of-DH",
        "rand-reversed",
        "hex-minus-one",
        "widest-constant",
        "oneshot",
    ],
)
def test_a_math_that_click_takes_passes(write):
    assert _errors(write) == ()


def test_each_unmapped_operand_names_its_argument():
    count, total = Int("Count"), Int("Total")
    findings = _errors(lambda: math(count + 1, total))
    assert [f.code for f in findings] == [CLK_BANK_UNRESOLVED] * 2
    assert [f.message.split(" at ")[0] for f in findings] == [
        "pyrung cannot find one CLICK address for the formula",
        "pyrung cannot find one CLICK address for the destination",
    ]
    assert [f.location.rpartition(".")[2] for f in findings] == ["source_expression", "dest"]


def test_an_unmapped_range_is_one_formula_finding():
    values = Block("Values", TagType.INT, 1, 3)
    (finding,) = _errors(lambda: math(values.select(1, 3).sum(), ds[100]))
    assert finding.code == CLK_BANK_UNRESOLVED
    # The export wrote the tag names in a SUM, which CLICK cannot load.
    with pytest.raises(LadderExportError, match=CLK_BANK_UNRESOLVED):
        pyrung_to_ladder(_program(lambda: math(values.select(1, 3).sum(), ds[100])), TagMap())


def test_an_unmapped_rand_bound_is_a_formula_finding():
    low = Int("Low")
    (finding,) = _errors(lambda: math(randint(low, 10), ds[100]))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.location.endswith("instruction.source_expression")


def test_a_pointer_has_one_finding_with_pyrungs_advice():
    pointer = Tag("P", TagType.INT)
    (finding,) = _errors(lambda: math(ds[pointer], ds[100]), [pointer.map_to(ds[50])])
    assert finding.code == Code.POINTER_OUTSIDE_COPY
    assert "can only be used inside copy()" in (finding.suggestion or "")


def test_a_range_of_scattered_addresses_is_refused():
    # laddercodec sees only the two ends that the export writes, so pyrung checks the rest.
    gap = Block("Gap", TagType.INT, 1, 3)
    (finding,) = _errors(
        lambda: math(gap.select(1, 3).sum(), ds[100]),
        [gap[1].map_to(ds[1]), gap[2].map_to(ds[2]), gap[3].map_to(ds[5])],
    )
    assert finding.code == "CLK_MATH_RANGE_NOT_CONTIGUOUS"


def test_a_bit_used_twice_is_named_once():
    findings = _errors(lambda: math(c[1] + c[2] * c[1], ds[100]))
    assert [f.display.frames[0].caret_label for f in findings] == [
        "C1 is refused",
        "C2 is refused",
    ]


def test_the_export_refuses_a_math_that_click_refuses():
    with pytest.raises(LadderExportError, match=Code.RANDOM_EQUAL_BOUNDS):
        pyrung_to_ladder(_program(lambda: math(randint(ds[1], ds[1]), ds[100])), TagMap())


def test_a_hex_constant_that_the_export_cannot_write_is_reported():
    # The export has no spelling for 70000 in a hex Math. Validation still reports it, and
    # the export refuses it even when the code is allowed.
    write = lambda: math(dh[1] + 70000, dh[100])  # noqa: E731
    (finding,) = _errors(write)
    assert finding.code == Code.MATH_CONSTANT_OUT_OF_RANGE
    assert finding.display.frames[0].caret_label == "11170h is outside 0h..FFFFh"
    with pytest.raises(LadderExportError, match="does not fit in 16 bits"):
        pyrung_to_ladder(_program(write), TagMap(), allow=(Code.MATH_CONSTANT_OUT_OF_RANGE,))


def test_a_decimal_wide_constant_exports_once_allowed():
    write = lambda: math(dd[1] / 3000000000, ds[1])  # noqa: E731
    (finding,) = _errors(write)
    assert finding.code == Code.MATH_CONSTANT_OUT_OF_RANGE
    rows = pyrung_to_ladder(
        _program(write), TagMap(), allow=(Code.MATH_CONSTANT_OUT_OF_RANGE,)
    ).main_rows
    assert any("DD1 / 3000000000" in cell for row in rows for cell in row)


def _math_cells(write) -> list[str]:
    rows = pyrung_to_ladder(_program(write), TagMap()).main_rows
    return [cell for row in rows for cell in row if cell.startswith("math(")]


@pytest.mark.parametrize(
    ("write", "cell"),
    [
        (lambda: math(dh[1] + True, dh[2]), "math(DH1 + 1h,DH2,mode=hex)"),
        (lambda: math(dh[1] & False, dh[2]), "math(DH1 AND 0h,DH2,mode=hex)"),
        (lambda: math(True + dh[1], dh[2]), "math(1h + DH1,DH2,mode=hex)"),
        (lambda: math(lsh(dh[1], True), dh[2]), "math(LSH ( DH1 , 1h ),DH2,mode=hex)"),
        (lambda: math(ds[1] + True, ds[2]), "math(DS1 + 1,DS2,mode=decimal)"),
    ],
)
def test_true_and_false_are_written_as_numbers_in_either_mode(write, cell):
    # A hex Math refuses a bare digit, so True goes out as 1h there.
    assert not _errors(write)
    assert _math_cells(write) == [cell]


def test_a_hex_constant_that_click_reads_as_a_real_is_refused():
    (finding,) = _errors(lambda: math(dh[1] + 0x1E, dh[2]))
    assert finding.code == Code.MATH_INVALID_TOKEN
    assert finding.display.frames[0].caret_label == "1Eh is refused"


def test_a_draws_site_number_is_not_read_as_a_constant():
    # Site 30 is 1Eh in hex, which CLICK refuses as a constant. A site is no constant.
    draw = RandIntExpr(dh[3], dh[4], site=30)
    (finding,) = _errors(lambda: math(dh[1] + 70000 + draw, dh[2]))
    assert finding.code == Code.MATH_CONSTANT_OUT_OF_RANGE


def test_a_math_is_checked_in_a_branch_a_loop_and_a_subroutine():
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            with branch(x[2]):
                math(ds[1] + c[1], ds[100])
        with rung(x[3]):
            with forloop(3):
                math(ds[1] + c[2], ds[101])
        with rung(x[4]):
            call("Sub")
        with subroutine("Sub"):
            with rung(x[5]):
                math(ds[1] + c[3], ds[102])
                out(y[1])
    report = validate_click_program(prog, TagMap(), mode="strict")
    labels = sorted(f.display.frames[0].caret_label for f in report.errors)
    assert [f.code for f in report.errors] == [Code.MATH_OPERAND_BANK] * 3
    assert labels == ["C1 is refused", "C2 is refused", "C3 is refused"]


@pytest.mark.parametrize(
    "code, new",
    [
        ("CLK_MATH_MODE_MIXED", "math-operand-bank or bank-not-allowed"),
        ("CLK_MATH_FUNC_MODE_MISMATCH", "math-function-mode"),
        ("CLK_MATH_NESTING_DEPTH", "math-nesting-depth"),
        ("CLK_MATH_MEDIAN_RANGE_TOO_LARGE", "median-range-too-large"),
        ("CLK_MATH_BIT_RANGE_AGGREGATE", "math-operand-bank"),
        ("CLK_MATH_SUM_BANK_NOT_AVAILABLE", "sum-bank-unavailable"),
        ("CLK_MATH_BIT_OPERAND", "math-operand-bank"),
        ("CLK_MATH_DEST_BANK", "bank-not-allowed"),
        ("CLK_MATH_ARG_NOT_PLAIN", "math-arg-expression"),
        ("CLK_MATH_WIDE_CONSTANT", "math-constant-out-of-range"),
        ("CLK_RAND_BOUND_TYPE", "random-bound-type"),
        ("CLK_RAND_EQUAL_BOUNDS", "random-equal-bounds"),
    ],
)
def test_allow_names_what_replaced_the_removed_code(code, new):
    write = lambda: math(ds[1] + 1, ds[2])  # noqa: E731
    with pytest.raises(ValueError, match=f"no longer reports {code}. Pass {new} to allow="):
        pyrung_to_ladder(_program(write), TagMap(), allow=(code,))


def test_allow_of_the_old_writable_code_names_what_replaced_it():
    write = lambda: math(ds[1] + 1, sd[1])  # noqa: E731
    with pytest.raises(
        ValueError, match="CLK_BANK_NOT_WRITABLE. Pass not-writable or bank-not-allowed"
    ):
        pyrung_to_ladder(_program(write), TagMap(), allow=("CLK_BANK_NOT_WRITABLE",))
    assert pyrung_to_ladder(_program(write), TagMap(), allow=(Code.NOT_WRITABLE,)).main_rows
