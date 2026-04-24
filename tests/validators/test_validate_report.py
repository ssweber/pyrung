"""Tests for the unified validation report and Program.validate()."""

import pytest

from pyrung.core import Bool, Program, Rung, latch, out
from pyrung.core.validation.report import ALL_RULES, ValidationReport, validate


class TestValidateAllRuns:
    def test_clean_program_no_findings(self):
        btn = Bool("Btn")
        motor = Bool("Motor")
        with Program() as prog:
            with Rung(btn):
                out(motor)
        report = validate(prog)
        assert isinstance(report, ValidationReport)
        assert len(report) == 0
        assert not report
        assert report.summary() == "No findings."

    def test_stuck_high_detected(self):
        go = Bool("Go")
        latch_bit = Bool("LatchBit")
        with Program() as prog:
            with Rung(go):
                latch(latch_bit)
        report = validate(prog)
        assert report
        codes = {f.code for f in report}
        assert "CORE_STUCK_HIGH" in codes

    def test_conflicting_output_detected(self):
        a = Bool("A")
        b = Bool("B")
        motor = Bool("Motor")
        with Program() as prog:
            with Rung(a):
                out(motor)
            with Rung(b):
                out(motor)
        report = validate(prog)
        codes = {f.code for f in report}
        assert "CORE_CONFLICTING_OUTPUT" in codes

    def test_readonly_write_detected(self):
        btn = Bool("Btn")
        ro = Bool("ReadonlyTag", readonly=True)
        with Program() as prog:
            with Rung(btn):
                out(ro)
        report = validate(prog)
        codes = {f.code for f in report}
        assert "CORE_READONLY_WRITE" in codes


class TestSelectIgnore:
    def _stuck_program(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        return prog

    def test_select_limits_rules(self):
        prog = self._stuck_program()
        report = validate(prog, select={"CORE_CONFLICTING_OUTPUT"})
        assert len(report) == 0

    def test_select_includes_matching(self):
        prog = self._stuck_program()
        report = validate(prog, select={"CORE_STUCK_HIGH"})
        assert len(report) > 0
        assert all(f.code == "CORE_STUCK_HIGH" for f in report)

    def test_ignore_excludes_rules(self):
        prog = self._stuck_program()
        full = validate(prog)
        ignored = validate(prog, ignore={"CORE_STUCK_HIGH"})
        assert len(ignored) < len(full)
        assert "CORE_STUCK_HIGH" not in {f.code for f in ignored}

    def test_select_and_ignore_combined(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = validate(
            prog,
            select={"CORE_STUCK_HIGH", "CORE_STUCK_LOW"},
            ignore={"CORE_STUCK_LOW"},
        )
        assert all(f.code == "CORE_STUCK_HIGH" for f in report)

    def test_unknown_rule_raises(self):
        btn = Bool("Btn")
        with Program() as prog:
            with Rung(btn):
                out(Bool("X"))
        with pytest.raises(ValueError, match="Unknown rule code"):
            validate(prog, select={"NOT_A_RULE"})

    def test_empty_active_returns_empty(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = validate(prog, select={"CORE_STUCK_HIGH"}, ignore={"CORE_STUCK_HIGH"})
        assert len(report) == 0


class TestProgramValidateMethod:
    def test_no_args_runs_core(self):
        btn = Bool("Btn")
        motor = Bool("Motor")
        with Program() as prog:
            with Rung(btn):
                out(motor)
        report = prog.validate()
        assert isinstance(report, ValidationReport)
        assert not report

    def test_select_kwarg(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = prog.validate(select={"CORE_STUCK_HIGH"})
        assert report
        assert all(f.code == "CORE_STUCK_HIGH" for f in report)

    def test_dialect_still_works(self):
        btn = Bool("Btn")
        with Program() as prog:
            with Rung(btn):
                out(Bool("X"))
        with pytest.raises(KeyError, match="Unknown validation dialect"):
            prog.validate("nonexistent_dialect")


class TestValidationReport:
    def test_summary_groups_by_code(self):
        go = Bool("Go")
        bit_a = Bool("BitA")
        bit_b = Bool("BitB")
        with Program() as prog:
            with Rung(go):
                latch(bit_a)
                latch(bit_b)
        report = validate(prog, select={"CORE_STUCK_HIGH"})
        assert "CORE_STUCK_HIGH: 2" in report.summary()

    def test_iteration(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = validate(prog, select={"CORE_STUCK_HIGH"})
        findings_list = list(report)
        assert len(findings_list) == len(report)

    def test_all_rules_constant_complete(self):
        expected = {
            "CORE_ANTITOGGLE",
            "CORE_CHOICES_VIOLATION",
            "CORE_CONFLICTING_OUTPUT",
            "CORE_FINAL_MULTIPLE_WRITERS",
            "CORE_MISSING_PROFILE",
            "CORE_RANGE_VIOLATION",
            "CORE_READONLY_WRITE",
            "CORE_STUCK_HIGH",
            "CORE_STUCK_LOW",
        }
        assert ALL_RULES == expected
