"""Helpful diagnostics for equality values absent from discrete stepper producers."""

from __future__ import annotations

from pyrung.core import Block, Bool, Int, Program, Rung, TagType, calc, copy
from pyrung.core.validation import CMP_STEPPER_VALUE_NOT_SET, validate


def _findings(program: Program):
    return list(validate(program, select={CMP_STEPPER_VALUE_NOT_SET}))


def _direct_stepper(compare_value: int) -> Program:
    choose_one = Bool("ChooseOne", external=True)
    choose_two = Bool("ChooseTwo", external=True)
    state = Int("State")
    result = Bool("Result", external=True)
    with Program(strict=False) as program:
        with Rung(choose_one):
            copy(1, state)
        with Rung(choose_two):
            copy(2, state)
        with Rung(state == compare_value):
            copy(1, result)
    return program


def test_reports_equality_value_absent_from_direct_producers() -> None:
    findings = _findings(_direct_stepper(300))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.target_name == "State"
    assert finding.severity == "warning"
    assert finding.display.frames[0].caret is not None
    assert finding.display.frames[0].caret[2] == len("300")
    assert finding.display.frames[0].caret_label == "State never set to 300"
    assert "^^^ State never set to 300" in finding.message
    assert "State is established as: 0, 1, 2" in finding.message
    assert "check the comparison or add the missing copy" in finding.message


def test_accepts_value_from_direct_producer() -> None:
    assert not _findings(_direct_stepper(2))


def test_follows_direct_copy_chain() -> None:
    choose_one = Bool("ChooseRequestOne", external=True)
    choose_two = Bool("ChooseRequestTwo", external=True)
    request = Int("DirectRequest")
    state = Int("DirectCopiedState")
    result = Bool("DirectCopiedResult", external=True)
    with Program(strict=False) as program:
        with Rung(choose_one):
            copy(10, request)
        with Rung(choose_two):
            copy(20, request)
        with Rung():
            copy(request, state)
        with Rung(state == 30):
            copy(1, result)

    findings = _findings(program)
    assert len(findings) == 1
    assert "DirectCopiedState is established as: 0, 10, 20" in findings[0].message


def _indirect_stepper(compare_value: int) -> Program:
    choose_one = Bool("ChooseIndirectOne", external=True)
    choose_two = Bool("ChooseIndirectTwo", external=True)
    pointer = Int("TablePointer")
    state = Int("IndirectState")
    result = Bool("IndirectResult", external=True)
    table = Block("StateTable", TagType.INT, 1, 2)
    table.slot(1, default=10)
    table.slot(2, default=20)

    with Program(strict=False) as program:
        with Rung(choose_one):
            copy(1, pointer)
        with Rung(choose_two):
            copy(2, pointer)
        with Rung():
            copy(table[pointer], state)
        with Rung(state == compare_value):
            copy(1, result)
    return program


def test_indirect_constant_table_values_are_producer_evidence() -> None:
    assert not _findings(_indirect_stepper(20))

    findings = _findings(_indirect_stepper(30))
    assert len(findings) == 1
    assert "IndirectState is established as: 0, 10, 20" in findings[0].message


def test_unknown_writer_path_punts_instead_of_guessing() -> None:
    choose_one = Bool("ChooseKnownOne", external=True)
    choose_two = Bool("ChooseKnownTwo", external=True)
    use_unknown = Bool("UseUnknown", external=True)
    unknown = Int("UnknownState", external=True)
    request = Int("RequestedState")
    state = Int("CopiedState")
    result = Bool("UnknownResult", external=True)
    with Program(strict=False) as program:
        with Rung(choose_one):
            copy(1, request)
        with Rung(choose_two):
            copy(2, request)
        with Rung(use_unknown):
            copy(unknown, state)
        with Rung():
            copy(request, state)
        with Rung(state == 99):
            copy(1, result)

    assert not _findings(program)


def test_externally_writable_stepper_punts() -> None:
    choose_one = Bool("ChooseExternalOne", external=True)
    choose_two = Bool("ChooseExternalTwo", external=True)
    state = Int("ExternallyWritableState", external=True)
    result = Bool("ExternalStateResult", external=True)
    with Program(strict=False) as program:
        with Rung(choose_one):
            copy(1, state)
        with Rung(choose_two):
            copy(2, state)
        with Rung(state == 99):
            copy(1, result)

    assert not _findings(program)


def test_dynamic_and_defensive_comparisons_stay_quiet() -> None:
    advance = Bool("Advance", external=True)
    step = Int("ArithmeticStep")
    result = Bool("DynamicResult", external=True)
    with Program(strict=False) as program:
        with Rung(advance):
            calc(step + 2, step)
        with Rung(step == 99):
            copy(1, result)
        with Rung(step != 101):
            copy(1, result)
        with Rung(step >= 103):
            copy(1, result)

    assert not _findings(program)
