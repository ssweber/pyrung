"""Tests for CircuitPython deployment validation."""

from __future__ import annotations

import pytest

from pyrung.circuitpy import P1AM, board, validate_circuitpy_program
from pyrung.circuitpy.validation import (
    CPY_FUNCTION_CALL_VERIFY,
    CPY_IO_BLOCK_UNTRACKED,
    CPY_TIMER_RESOLUTION,
    CircuitPyValidationReport,
)
from pyrung.core import (
    Bool,
    InputBlock,
    Int,
    OutputBlock,
    Program,
    Rung,
    TagType,
    Tms,
    Ts,
    branch,
    off_delay,
    on_delay,
    out,
    run_enabled_function,
    run_function,
    subroutine,
)


def _build_program(fn):
    prog = Program(strict=False)
    with prog:
        fn()
    return prog


def _all_findings(report: CircuitPyValidationReport):
    return [*report.errors, *report.warnings, *report.hints]


def _finding_codes(report: CircuitPyValidationReport) -> list[str]:
    return [finding.code for finding in _all_findings(report)]


class TestCleanProgram:
    def test_simple_p1am_program_has_no_findings(self):
        hw = P1AM()
        inputs = hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")

        button = inputs[1]
        light = outputs[1]

        def logic():
            with Rung(button):
                out(light)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert _finding_codes(report) == []
        assert report.summary() == "No findings."


class TestFunctionCallVerify:
    def test_run_function_warn_and_strict(self):
        source = Int("Source")
        dest = Int("Dest")

        def fn(value):
            return {"result": value}

        def logic():
            with Rung():
                run_function(fn, ins={"value": source}, outs={"result": dest})

        prog = _build_program(logic)

        warn_report = validate_circuitpy_program(prog, mode="warn")
        strict_report = validate_circuitpy_program(prog, mode="strict")

        assert any(f.code == CPY_FUNCTION_CALL_VERIFY for f in warn_report.hints)
        assert any(f.code == CPY_FUNCTION_CALL_VERIFY for f in strict_report.hints)


class TestEnabledFunctionCallVerify:
    def test_run_enabled_function_warn_and_strict(self):
        enable = Bool("Enable")
        source = Int("Source")
        dest = Int("Dest")

        def fn(enabled, value):
            _ = enabled
            return {"result": value}

        def logic():
            with Rung(enable):
                run_enabled_function(fn, ins={"value": source}, outs={"result": dest})

        prog = _build_program(logic)

        warn_report = validate_circuitpy_program(prog, mode="warn")
        strict_report = validate_circuitpy_program(prog, mode="strict")

        assert any(f.code == CPY_FUNCTION_CALL_VERIFY for f in warn_report.hints)
        assert any(f.code == CPY_FUNCTION_CALL_VERIFY for f in strict_report.hints)


class TestIOBlockUntracked:
    def test_input_tag_not_from_p1am_emits_finding(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]

        remote_inputs = InputBlock("Remote", TagType.BOOL, 1, 8)
        remote_button = remote_inputs[1]

        def logic():
            with Rung(remote_button):
                out(light)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert CPY_IO_BLOCK_UNTRACKED in _finding_codes(report)


class TestIOBlockTracked:
    def test_input_and_output_from_p1am_have_no_io_findings(self):
        hw = P1AM()
        inputs = hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")
        button = inputs[1]
        light = outputs[1]

        def logic():
            with Rung(button):
                out(light)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert CPY_IO_BLOCK_UNTRACKED not in _finding_codes(report)

    def test_board_io_tags_are_treated_as_tracked_hardware(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")

        def logic():
            with Rung(board.switch):
                out(board.led)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")
        assert CPY_IO_BLOCK_UNTRACKED not in _finding_codes(report)


class TestTimerMillisecond:
    def test_on_delay_tms_emits_resolution_finding(self):
        hw = P1AM()
        done = Bool("Done")
        acc = Int("Acc")
        reset_tag = Bool("Reset")

        def logic():
            with Rung(Bool("Enable")):
                on_delay(done, acc, preset=10, unit=Tms).reset(reset_tag)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert CPY_TIMER_RESOLUTION in _finding_codes(report)


class TestTimerSeconds:
    def test_on_delay_seconds_has_no_timer_resolution_finding(self):
        hw = P1AM()
        done = Bool("Done")
        acc = Int("Acc")
        reset_tag = Bool("Reset")

        def logic():
            with Rung(Bool("Enable")):
                on_delay(done, acc, preset=10, unit=Ts).reset(reset_tag)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert CPY_TIMER_RESOLUTION not in _finding_codes(report)


class TestOffDelayTimer:
    def test_off_delay_tms_emits_resolution_finding(self):
        hw = P1AM()
        done = Bool("Done")
        acc = Int("Acc")

        def logic():
            with Rung(Bool("Enable")):
                off_delay(done, acc, preset=10, unit=Tms)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=hw, mode="warn")

        assert CPY_TIMER_RESOLUTION in _finding_codes(report)


class TestNoHardware:
    def test_hw_none_runs_stage2_only(self):
        untracked_outputs = OutputBlock("RemoteOut", TagType.BOOL, 1, 8)
        remote_light = untracked_outputs[1]
        done = Bool("Done")
        acc = Int("Acc")

        def fn():
            return {}

        def logic():
            with Rung(Bool("Enable")):
                out(remote_light)
                run_function(fn)
                on_delay(done, acc, preset=10, unit=Tms).reset(Bool("Reset"))

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, hw=None, mode="warn")
        codes = _finding_codes(report)

        assert CPY_FUNCTION_CALL_VERIFY in codes
        assert CPY_IO_BLOCK_UNTRACKED not in codes
        assert CPY_TIMER_RESOLUTION not in codes


class TestDialectRegistration:
    def test_program_validate_dispatches_circuitpy(self):
        hw = P1AM()

        def fn():
            return {}

        def logic():
            with Rung():
                run_function(fn)

        prog = _build_program(logic)
        direct = validate_circuitpy_program(prog, hw=hw, mode="warn")
        via_program = prog.validate("circuitpy", hw=hw, mode="warn")

        assert _finding_codes(direct) == _finding_codes(via_program)

    def test_program_validate_rejects_bad_hw_type(self):
        prog = Program(strict=False)
        with pytest.raises(TypeError, match="hw=P1AM"):
            prog.validate("circuitpy", hw=object())

    def test_program_validate_rejects_bad_mode(self):
        prog = Program(strict=False)
        with pytest.raises(ValueError, match="mode"):
            prog.validate("circuitpy", mode="nope")


class TestReportSummary:
    def test_summary_formats_like_click_report(self):
        empty = CircuitPyValidationReport()
        assert empty.summary() == "No findings."

        def fn():
            return {}

        with Program(strict=False) as prog:
            with Rung():
                run_function(fn)

        warn_report = validate_circuitpy_program(prog, mode="warn")
        strict_report = validate_circuitpy_program(prog, mode="strict")

        assert "hint(s)" in warn_report.summary()
        assert "hint(s)" in strict_report.summary()


class TestLocationFormatting:
    def test_location_is_deterministic_and_human_readable(self):
        def fn():
            return {}

        def logic():
            with Rung():
                out(Bool("Filler"))
                run_function(fn)

        prog = _build_program(logic)
        report = validate_circuitpy_program(prog, mode="warn")
        finding = [f for f in report.hints if f.code == CPY_FUNCTION_CALL_VERIFY][0]

        assert finding.location.startswith("main.rung[0].")
        assert "instruction[1](FunctionCallInstruction)" in finding.location

    def test_location_is_stable_across_runs(self):
        def fn():
            return {}

        def logic():
            with Rung():
                run_function(fn)

        prog = _build_program(logic)
        r1 = validate_circuitpy_program(prog, mode="warn")
        r2 = validate_circuitpy_program(prog, mode="warn")

        locs1 = [f.location for f in _all_findings(r1)]
        locs2 = [f.location for f in _all_findings(r2)]
        assert locs1 == locs2


class TestSuggestionContent:
    def test_function_call_suggestion_is_actionable(self):
        def fn():
            return {}

        def logic():
            with Rung():
                run_function(fn)

        report = validate_circuitpy_program(_build_program(logic), mode="warn")
        finding = [f for f in report.hints if f.code == CPY_FUNCTION_CALL_VERIFY][0]
        assert finding.suggestion is not None
        assert "CircuitPython" in finding.suggestion

    def test_io_suggestion_mentions_hw_slot_usage(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]
        external = InputBlock("External", TagType.BOOL, 1, 8)[1]

        def logic():
            with Rung(external):
                out(light)

        report = validate_circuitpy_program(_build_program(logic), hw=hw, mode="warn")
        finding = [f for f in report.hints if f.code == CPY_IO_BLOCK_UNTRACKED][0]
        assert finding.suggestion is not None
        assert "hw.slot" in finding.suggestion

    def test_timer_suggestion_mentions_scan_time(self):
        hw = P1AM()
        done = Bool("Done")
        acc = Int("Acc")

        def logic():
            with Rung(Bool("Enable")):
                on_delay(done, acc, preset=5, unit=Tms).reset(Bool("Reset"))

        report = validate_circuitpy_program(_build_program(logic), hw=hw, mode="warn")
        finding = [f for f in report.hints if f.code == CPY_TIMER_RESOLUTION][0]
        assert finding.suggestion is not None
        assert "scan" in finding.suggestion.lower()


class TestSubroutines:
    def test_findings_include_subroutine_location(self):
        def fn():
            return {}

        with Program(strict=False) as prog:
            with subroutine("worker"):
                with Rung():
                    run_function(fn)

        report = validate_circuitpy_program(prog, mode="warn")
        finding = [f for f in report.hints if f.code == CPY_FUNCTION_CALL_VERIFY][0]
        assert finding.location.startswith("subroutine[worker].rung[0].")


class TestBranches:
    def test_findings_include_branch_path(self):
        def fn():
            return {}

        with Program(strict=False) as prog:
            with Rung(Bool("Enable")):
                with branch(Bool("BranchEnable")):
                    run_function(fn)

        report = validate_circuitpy_program(prog, mode="warn")
        finding = [f for f in report.hints if f.code == CPY_FUNCTION_CALL_VERIFY][0]
        assert ".branch[0]." in finding.location


class TestStrictMode:
    def test_strict_mode_keeps_non_blocking_advisories_as_hints(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]
        external_input = InputBlock("External", TagType.BOOL, 1, 8)[1]
        done = Bool("Done")
        acc = Int("Acc")

        def fn():
            return {}

        def logic():
            with Rung(external_input):
                out(light)
                run_function(fn)
                on_delay(done, acc, preset=5, unit=Tms).reset(Bool("Reset"))

        report = validate_circuitpy_program(_build_program(logic), hw=hw, mode="strict")
        codes = set(_finding_codes(report))

        assert {CPY_FUNCTION_CALL_VERIFY, CPY_IO_BLOCK_UNTRACKED, CPY_TIMER_RESOLUTION} <= codes
        assert any(f.code == CPY_IO_BLOCK_UNTRACKED for f in report.errors)
        assert any(f.code == CPY_FUNCTION_CALL_VERIFY for f in report.hints)
        assert any(f.code == CPY_TIMER_RESOLUTION for f in report.hints)


class TestWarnMode:
    def test_all_findings_are_hints(self):
        hw = P1AM()
        outputs = hw.slot(1, "P1-08TRS")
        light = outputs[1]
        external_input = InputBlock("External", TagType.BOOL, 1, 8)[1]
        done = Bool("Done")
        acc = Int("Acc")

        def fn():
            return {}

        def logic():
            with Rung(external_input):
                out(light)
                run_function(fn)
                on_delay(done, acc, preset=5, unit=Tms).reset(Bool("Reset"))

        report = validate_circuitpy_program(_build_program(logic), hw=hw, mode="warn")
        codes = set(_finding_codes(report))

        assert {CPY_FUNCTION_CALL_VERIFY, CPY_IO_BLOCK_UNTRACKED, CPY_TIMER_RESOLUTION} <= codes
        assert report.errors == ()


class TestComboModuleIO:
    def test_combo_module_io_tags_are_tracked(self):
        hw = P1AM()
        combo_in, combo_out = hw.slot(1, "P1-16CDR")
        sensor = combo_in[1]
        relay = combo_out[1]

        def logic():
            with Rung(sensor):
                out(relay)

        report = validate_circuitpy_program(_build_program(logic), hw=hw, mode="warn")

        assert CPY_IO_BLOCK_UNTRACKED not in _finding_codes(report)
