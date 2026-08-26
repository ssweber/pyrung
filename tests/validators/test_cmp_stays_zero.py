"""Specification tests for zero comparison operands and timer/counter presets."""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    Tag,
    TagType,
    Timer,
    copy,
    count_up,
    on_delay,
)
from pyrung.core.system_points import system
from pyrung.core.validation import (
    CMP_OPERAND_STAYS_ZERO,
    CMP_PRESET_STAYS_ZERO,
    validate,
)


def _codes(program: Program, *codes: str):
    return [finding for finding in validate(program, select=set(codes)) if finding.code in codes]


def _comparison_program(limit: Tag) -> Program:
    value = Int("Value", default=1, readonly=True)
    result = Bool("Result", external=True)
    with Program(strict=False) as program:
        with Rung(value >= limit):
            copy(1, result)
    return program


class TestOperandStaysZero:
    def test_unwritten_zero_operand_is_reported(self) -> None:
        limit = Int("Limit")
        findings = _codes(_comparison_program(limit), CMP_OPERAND_STAYS_ZERO)

        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert findings[0].target_name == "Limit"
        assert "Limit" in findings[0].message
        assert "stays 0" in findings[0].message
        assert "mark it external" in findings[0].message

    def test_nonzero_default_is_clean(self) -> None:
        limit = Int("Limit", default=5)
        assert not _codes(_comparison_program(limit), CMP_OPERAND_STAYS_ZERO)

    def test_explicit_zero_default_is_clean(self) -> None:
        zero_constant = Int("Idle", default=0)
        assert not _codes(_comparison_program(zero_constant), CMP_OPERAND_STAYS_ZERO)

    def test_plain_memory_slot_keeps_implicit_zero_provenance(self) -> None:
        data = Block("DS", TagType.INT, 1, 1)
        assert _codes(_comparison_program(data[1]), CMP_OPERAND_STAYS_ZERO)

    def test_explicit_zero_memory_slot_is_clean(self) -> None:
        data = Block("DS", TagType.INT, 1, 1)
        data.slot(1, default=0)
        assert not _codes(_comparison_program(data[1]), CMP_OPERAND_STAYS_ZERO)

    def test_first_scan_write_means_the_tag_does_not_stay_zero(self) -> None:
        limit = Int("Limit")
        value = Int("Value", default=1, readonly=True)
        result = Bool("Result", external=True)
        with Program(strict=False) as program:
            with Rung(system.sys.first_scan):
                copy(5, limit)
            with Rung(value >= limit):
                copy(1, result)

        assert not _codes(program, CMP_OPERAND_STAYS_ZERO)

    def test_external_and_readonly_zero_tags_are_explicit(self) -> None:
        external = Int("ExternalLimit", external=True)
        constant = Int("ZeroConstant", readonly=True)

        assert not _codes(_comparison_program(external), CMP_OPERAND_STAYS_ZERO)
        assert not _codes(_comparison_program(constant), CMP_OPERAND_STAYS_ZERO)

    def test_literal_zero_is_intentional(self) -> None:
        value = Int("Value", default=1, readonly=True)
        result = Bool("Result", external=True)
        with Program(strict=False) as program:
            with Rung(value >= 0):
                copy(1, result)

        assert not _codes(program, CMP_OPERAND_STAYS_ZERO)

    def test_boolish_numeric_equality_is_clean(self) -> None:
        flag = Int("NumericFlag")
        result = Bool("BoolishResult", external=True)
        with Program(strict=False) as program:
            with Rung(flag == 1):
                copy(1, result)
            with Rung(flag != 0):
                copy(1, result)

        assert not _codes(program, CMP_OPERAND_STAYS_ZERO)

    def test_non_boolish_numeric_equality_is_still_reported(self) -> None:
        state = Int("State")
        result = Bool("StateResult", external=True)
        with Program(strict=False) as program:
            with Rung(state == 7):
                copy(1, result)

        findings = _codes(program, CMP_OPERAND_STAYS_ZERO)
        assert [finding.target_name for finding in findings] == ["State"]


class TestPresetStaysZero:
    def test_timer_and_counter_tag_presets_are_reported(self) -> None:
        timer_preset = Int("TimerPreset")
        counter_preset = Dint("CounterPreset")
        timer = Timer.clone("Delay")
        counter = Counter.clone("Parts")
        enable = Bool("Enable", external=True)
        reset = Bool("Reset", external=True)
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(timer, timer_preset)
            with Rung(enable):
                count_up(counter, counter_preset).reset(reset)

        findings = _codes(program, CMP_PRESET_STAYS_ZERO)
        assert {finding.target_name for finding in findings} == {
            "TimerPreset",
            "CounterPreset",
        }
        assert all(finding.severity == "warning" for finding in findings)
        assert all("preset stays 0" in finding.message for finding in findings)
        assert "on_delay(Delay, preset=TimerPreset)" in findings[0].message

    def test_literal_zero_preset_is_intentional(self) -> None:
        timer = Timer.clone("Elapsed")
        enable = Bool("Enable", external=True)
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(timer, 0)

        assert not _codes(program, CMP_PRESET_STAYS_ZERO)

    def test_written_and_external_presets_are_clean(self) -> None:
        written = Int("WrittenPreset")
        external = Int("ExternalPreset", external=True)
        written_timer = Timer.clone("WrittenDelay")
        external_timer = Timer.clone("ExternalDelay")
        enable = Bool("Enable", external=True)
        with Program(strict=False) as program:
            with Rung(system.sys.first_scan):
                copy(50, written)
            with Rung(enable):
                on_delay(written_timer, written)
            with Rung(enable):
                on_delay(external_timer, external)

        assert not _codes(program, CMP_PRESET_STAYS_ZERO)

    def test_matching_accumulator_comparison_is_not_reported_twice(self) -> None:
        preset = Int("Preset")
        timer = Timer.clone("Delay")
        enable = Bool("Enable", external=True)
        result = Bool("Result", external=True)
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(timer, preset)
            with Rung(timer.Acc >= preset):
                copy(1, result)

        findings = _codes(
            program,
            CMP_OPERAND_STAYS_ZERO,
            CMP_PRESET_STAYS_ZERO,
        )
        assert [finding.code for finding in findings] == [CMP_PRESET_STAYS_ZERO]
