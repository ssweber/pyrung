"""Numeric flags need stronger evidence than an unwritten zero initialization."""

import operator

import pytest

from pyrung import Bool, Dint, Int, Program, Real, Rung, Word, copy, out


@pytest.mark.parametrize("factory", [Int, Dint, Word, Real])
@pytest.mark.parametrize("compare", [operator.eq, operator.ne])
@pytest.mark.parametrize("value", [0, 1])
@pytest.mark.parametrize("explicit_default", [False, True])
def test_unwritten_zero_flag_is_not_reported_as_constant(factory, compare, value, explicit_default):
    flag = factory("Flag", **({"default": 0} if explicit_default else {}))
    with Program() as program:
        with Rung(compare(flag, value)):
            out(Bool("Result"))
    assert not program.check()
    assert not program.check(select={"CMP"})


def test_sfc_call_and_pause_flags_are_not_assumed_constant():
    call_flag, pause_flag = Int("SFCExample.xCall"), Int("SFCExample.xPause")
    with Program() as program:
        with Rung(call_flag == 1, pause_flag != 1):
            out(Bool("Result"))
    assert not program.check()
    assert not program.check(select={"CMP"})


@pytest.mark.parametrize(
    "contract", [{"readonly": True}, {"choices": {0: "Off"}}, {"min": -1, "max": 0}]
)
def test_declared_constraints_still_expose_impossible_boolean_comparison(contract):
    flag = Int("Flag", **contract)
    with Program() as program:
        with Rung(flag == 1):
            out(Bool("Result"))
    assert "CMP_ALWAYS_FALSE" in {finding.code for finding in program.check()}


def test_known_ladder_writes_still_expose_impossible_boolean_comparison():
    flag = Int("Flag")
    with Program() as program:
        with Rung():
            copy(0, flag)
        with Rung(flag == 1):
            out(Bool("Result"))
    assert "CMP_ALWAYS_FALSE" in {finding.code for finding in program.check()}


def test_boolean_flag_exemption_does_not_hide_contradictory_rung():
    flag = Int("Flag")
    with Program() as program:
        with Rung(flag == 0, flag == 1):
            out(Bool("Result"))
    assert "RUNG_CONTRADICTION" in {finding.code for finding in program.check()}


def test_no_writer_hints_share_one_concise_repair():
    unknown = Int("Limit")
    configured = Int("Limit", default=0)
    with Program() as missing_source:
        with Rung(unknown == 7):
            out(Bool("Result"))
    with Program() as constant:
        with Rung(configured == 7):
            out(Bool("Result"))
    (missing,) = missing_source.check(select={"CMP_OPERAND_NO_WRITER"})
    (impossible,) = constant.check(select={"CMP_ALWAYS_FALSE"})
    assert (
        missing.display.hint
        == impossible.display.hint
        == "set Limit in the ladder, or mark it external"
    )
