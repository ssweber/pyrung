"""Readability advice for repeated discrete state-value comparisons."""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, branch, copy, subroutine
from pyrung.core.validation import CMP_REPEATED_STATE_VALUE, validate
from pyrung.core.validation.cmp_conditions import (
    _REPEATED_STATE_MIN_INTERVENING_RUNGS,
    _REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS,
)


def _findings(program: Program):
    return list(validate(program, select={CMP_REPEATED_STATE_VALUE}))


def _comparison_rung(state, value: int, result) -> None:
    with Rung(state == value):
        copy(1, result)


def test_single_value_volume_threshold() -> None:
    state = Int("VolumeState")
    result = Bool("VolumeResult", external=True)

    with Program(strict=False) as quiet:
        for _ in range(_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS - 1):
            _comparison_rung(state, 3, result)
    assert not _findings(quiet)

    with Program(strict=False) as noisy:
        for _ in range(_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS):
            _comparison_rung(state, 3, result)

    findings = _findings(noisy)
    assert len(findings) == 1
    assert findings[0].target_name == "VolumeState"
    assert findings[0].severity == "advisory"
    assert f"3 on {_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS} rungs" in findings[0].message
    assert (
        f"3 appears on {_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS} separate rungs "
        f"(limit: {_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS})" in findings[0].message
    )


def test_two_repeated_values_trigger_breadth_once_per_tag() -> None:
    state = Int("BreadthState")
    result = Bool("BreadthResult", external=True)
    with Program(strict=False) as program:
        _comparison_rung(state, 2, result)
        _comparison_rung(state, 2, result)
        _comparison_rung(state, 3, result)
        _comparison_rung(state, 3, result)

    findings = _findings(program)
    assert len(findings) == 1
    assert "2 on 2 rungs; 3 on 2 rungs" in findings[0].message
    assert "2 values are each compared on at least 2 separate rungs" in findings[0].message


def test_zero_one_only_convention_stays_quiet_at_high_volume() -> None:
    state = Int("BoolishState")
    result = Bool("BoolishResult", external=True)
    with Program(strict=False) as program:
        for value in (0, 1) * _REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS:
            _comparison_rung(state, value, result)

    assert not _findings(program)


def test_interleaved_value_is_dispersed() -> None:
    state = Int("InterleavedState")
    result = Bool("InterleavedResult", external=True)
    with Program(strict=False) as program:
        _comparison_rung(state, 2, result)
        _comparison_rung(state, 3, result)
        _comparison_rung(state, 2, result)

    findings = _findings(program)
    assert len(findings) == 1
    assert "2 on 2 rungs" in findings[0].message
    assert "3 on" not in findings[0].message
    assert "2 is compared on both sides of another value (3)" in findings[0].message


def test_far_apart_value_is_dispersed() -> None:
    state = Int("FarApartState")
    result = Bool("FarApartResult", external=True)
    filler = Int("Filler", external=True)
    with Program(strict=False) as program:
        _comparison_rung(state, 4, result)
        for _ in range(_REPEATED_STATE_MIN_INTERVENING_RUNGS):
            with Rung():
                copy(0, filler)
        _comparison_rung(state, 4, result)

    finding = _findings(program)[0]
    assert (
        f"4 is compared again after {_REPEATED_STATE_MIN_INTERVENING_RUNGS} intervening rungs"
        in finding.message
    )


def test_same_value_in_main_and_subroutine_is_dispersed() -> None:
    state = Int("ScopedState")
    result = Bool("ScopedResult", external=True)
    with Program(strict=False) as program:
        _comparison_rung(state, 5, result)
        with subroutine("check_state"):
            _comparison_rung(state, 5, result)

    finding = _findings(program)[0]
    assert len(finding.display.frames) == 2
    assert {frame.location for frame in finding.display.frames} == {
        "Main:R1",
        "check_state:R1",
    }
    assert "5 is compared in Main, check_state" in finding.message


def test_parallel_branches_count_as_one_top_level_rung() -> None:
    state = Int("BranchState")
    result = Bool("BranchResult", external=True)
    with Program(strict=False) as program:
        with Rung():
            with branch(state == 6):
                copy(1, result)
            with branch(state == 6):
                copy(1, result)

    assert not _findings(program)


def test_named_reference_comparisons_do_not_count_as_raw_literals() -> None:
    state = Int("ReferencedState")
    starting = Int("Ref_Sts_Starting", default=2, readonly=True)
    result = Bool("ReferencedResult", external=True)
    with Program(strict=False) as program:
        for _ in range(_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS):
            with Rung(state == starting):
                copy(1, result)

    assert not _findings(program)


def test_one_time_bool_decode_bank_stays_quiet() -> None:
    state = Int("DecodedState", choices={1: "IDLE", 2: "SHOW", 3: "INPUT"})
    decoded = [Bool("IsIdle"), Bool("IsShow"), Bool("IsInput")]
    with Program(strict=False) as program:
        for value, status in zip((1, 2, 3), decoded, strict=True):
            with Rung(state == state.choice({1: "IDLE", 2: "SHOW", 3: "INPUT"}[value])):
                copy(1, status)

    assert not _findings(program)


def test_choice_label_is_included_in_advice() -> None:
    state = Int("ChoiceState", choices={2: "STARTING", 3: "RUNNING"})
    result = Bool("ChoiceResult", external=True)
    with Program(strict=False) as program:
        for _ in range(_REPEATED_STATE_SINGLE_VALUE_MIN_RUNGS):
            _comparison_rung(state, state.choice("STARTING"), result)

    finding = _findings(program)[0]
    assert "2 ('STARTING')" in finding.message
    assert "read-only reference tag" in finding.message
    assert "Bool status tag" in finding.message
