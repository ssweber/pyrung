"""Tests for the unified validation report and Program.validate()."""

import pytest

from pyrung.core import Bool, Program, Rung, latch, out
from pyrung.core.validation.registry import RULES, VALIDATOR_ORDER
from pyrung.core.validation.report import ALL_RULES, ValidationReport, check, validate


def _error_and_warning_program():
    """Produces a COIL/warning (STUCK_HIGH) and a TAG/error (READONLY_WRITE)."""
    go = Bool("Go")
    bit = Bool("Bit")
    ro = Bool("ReadonlyTag", readonly=True)
    with Program() as prog:
        with Rung(go):
            latch(bit)
        with Rung(go):
            out(ro)
    return prog


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
        assert "COIL_STUCK_HIGH" in codes

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
        assert "COIL_CONFLICTING_OUTPUT" in codes

    def test_readonly_write_detected(self):
        btn = Bool("Btn")
        ro = Bool("ReadonlyTag", readonly=True)
        with Program() as prog:
            with Rung(btn):
                out(ro)
        report = validate(prog)
        codes = {f.code for f in report}
        assert "TAG_READONLY_WRITE" in codes


class TestValidationContext:
    def test_shared_analyses_run_once_per_check(self, monkeypatch):
        import pyrung.core.analysis.pdg as pdg
        import pyrung.core.analysis.return_guards as return_guards
        import pyrung.core.analysis.value_domains as value_domains

        btn = Bool("Btn")
        motor = Bool("Motor")
        with Program() as prog:
            with Rung(btn):
                out(motor)

        calls = {"graph": 0, "produced": 0, "reach": 0}
        original_graph = pdg.build_program_graph
        original_produced = value_domains.produced_value_domains
        original_reach = return_guards.scope_reach_chains

        def counted_graph(program):
            calls["graph"] += 1
            return original_graph(program)

        def counted_produced(program, graph):
            calls["produced"] += 1
            return original_produced(program, graph)

        def counted_reach(program, graph):
            calls["reach"] += 1
            return original_reach(program, graph)

        monkeypatch.setattr(pdg, "build_program_graph", counted_graph)
        monkeypatch.setattr(value_domains, "produced_value_domains", counted_produced)
        monkeypatch.setattr(return_guards, "scope_reach_chains", counted_reach)

        validate(
            prog,
            select={
                "CALL_NEVER_CALLED",
                "CMP_ALWAYS_FALSE",
                "MATH_DIV_ZERO",
                "RUNG_REDUNDANT_TERM",
            },
        )

        assert calls == {"graph": 1, "produced": 1, "reach": 1}

    def test_unselected_analyses_stay_lazy(self, monkeypatch):
        import pyrung.core.analysis.pdg as pdg
        import pyrung.core.analysis.return_guards as return_guards
        import pyrung.core.analysis.value_domains as value_domains

        with Program() as prog:
            pass

        calls = {"graph": 0, "produced": 0, "reach": 0}
        original_graph = pdg.build_program_graph

        def counted_graph(program):
            calls["graph"] += 1
            return original_graph(program)

        def counted_produced(program, graph):
            calls["produced"] += 1
            return {}

        def counted_reach(program, graph):
            calls["reach"] += 1
            return {}

        monkeypatch.setattr(pdg, "build_program_graph", counted_graph)
        monkeypatch.setattr(value_domains, "produced_value_domains", counted_produced)
        monkeypatch.setattr(return_guards, "scope_reach_chains", counted_reach)

        validate(prog, select={"CALL_NEVER_CALLED"})

        assert calls == {"graph": 1, "produced": 0, "reach": 0}


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
        report = validate(prog, select={"COIL_CONFLICTING_OUTPUT"})
        assert len(report) == 0

    def test_select_includes_matching(self):
        prog = self._stuck_program()
        report = validate(prog, select={"COIL_STUCK_HIGH"})
        assert len(report) > 0
        assert all(f.code == "COIL_STUCK_HIGH" for f in report)

    def test_ignore_excludes_rules(self):
        prog = self._stuck_program()
        full = validate(prog)
        ignored = validate(prog, ignore={"COIL_STUCK_HIGH"})
        assert len(ignored) < len(full)
        assert "COIL_STUCK_HIGH" not in {f.code for f in ignored}

    def test_select_and_ignore_combined(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = validate(
            prog,
            select={"COIL_STUCK_HIGH", "COIL_STUCK_LOW"},
            ignore={"COIL_STUCK_LOW"},
        )
        assert all(f.code == "COIL_STUCK_HIGH" for f in report)

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
        report = validate(prog, select={"COIL_STUCK_HIGH"}, ignore={"COIL_STUCK_HIGH"})
        assert len(report) == 0


class TestProgramValidateMethod:
    def test_check_runs_core(self):
        report = _error_and_warning_program().check(select={"COIL_STUCK_HIGH"})
        assert report
        assert all(f.code == "COIL_STUCK_HIGH" for f in report)

    def test_no_args_runs_core(self):
        btn = Bool("Btn")
        motor = Bool("Motor")
        with Program() as prog:
            with Rung(btn):
                out(motor)
        report = prog.validate()
        assert isinstance(report, ValidationReport)
        assert not report

    def test_validate_without_dialect_is_check_alias(self):
        prog = _error_and_warning_program()
        assert prog.validate().findings == prog.check().findings
        assert validate(prog).findings == check(prog).findings

    def test_select_kwarg(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = prog.validate(select={"COIL_STUCK_HIGH"})
        assert report
        assert all(f.code == "COIL_STUCK_HIGH" for f in report)

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
        report = validate(prog, select={"COIL_STUCK_HIGH"})
        assert "COIL_STUCK_HIGH: 2" in report.summary()

    def test_iteration(self):
        go = Bool("Go")
        bit = Bool("Bit")
        with Program() as prog:
            with Rung(go):
                latch(bit)
        report = validate(prog, select={"COIL_STUCK_HIGH"})
        findings_list = list(report)
        assert len(findings_list) == len(report)

    def test_finding_str_renders_complete_diagnostic(self):
        report = validate(_error_and_warning_program(), select={"TAG_READONLY_WRITE"})
        (finding,) = report.findings
        expected = f"[{finding.code}] {finding.severity}\n{finding.message}"
        assert str(finding) == expected
        assert str(finding.display) == expected

    def test_all_rules_constant_complete(self):
        expected = {
            "PHYS_ANTITOGGLE",
            "CALL_NEVER_CALLED",
            "CALL_RECURSION",
            "CMP_ALWAYS_FALSE",
            "CMP_ALWAYS_TRUE",
            "TAG_CHOICES_VIOLATION",
            "COIL_CONFLICTING_OUTPUT",
            "TAG_FINAL_MULTIPLE_WRITERS",
            "PHYS_MISSING_PROFILE",
            "PTR_DEFAULT_BEFORE_BLOCK_START",
            "PTR_MAY_ESCAPE_BLOCK",
            "TAG_RANGE_VIOLATION",
            "TAG_READONLY_WRITE",
            "COIL_STUCK_HIGH",
            "COIL_STUCK_LOW",
            "RUNG_CONTRADICTION",
            "RUNG_REDUNDANT_TERM",
            "RUNG_TAUTOLOGY",
            "CMP_EQ_ON_MONOTONE",
            "CMP_OPERAND_STAYS_ZERO",
            "CMP_PRESET_STAYS_ZERO",
            "CMP_REPEATED_STATE_VALUE",
            "CMP_STEPPER_VALUE_NOT_SET",
            "CMP_TRUE_AT_RESET",
            "CMP_STATIC_ON_LEFT",
            "STEP_NO_ESCAPE",
            "MATH_DIV_ZERO",
            "TAG_DEAD_WRITE",
        }
        assert ALL_RULES == expected


class TestSeverity:
    _LEVELS = {"error", "warning", "info", "advisory"}

    def _mixed_program(self):
        go = Bool("Go")
        bit = Bool("Bit")
        ro = Bool("ReadonlyTag", readonly=True)
        with Program() as prog:
            with Rung(go):
                latch(bit)  # STUCK_HIGH -> warning
            with Rung(go):
                out(ro)  # READONLY_WRITE -> error
        return prog

    def test_every_finding_carries_a_known_severity(self):
        report = self._mixed_program().validate()
        assert report
        assert all(f.severity in self._LEVELS for f in report)

    def test_conflicting_output_is_error(self):
        a, b, motor = Bool("A"), Bool("B"), Bool("Motor")
        with Program() as prog:
            with Rung(a):
                out(motor)
            with Rung(b):
                out(motor)
        report = validate(prog, select={"COIL_CONFLICTING_OUTPUT"})
        assert report
        assert all(f.severity == "error" for f in report)

    def test_stuck_high_is_warning(self):
        report = validate(self._mixed_program(), select={"COIL_STUCK_HIGH"})
        assert report
        assert all(f.severity == "warning" for f in report)

    def test_errors_warnings_partition_the_report(self):
        report = self._mixed_program().validate()
        buckets = report.errors() + report.warnings() + report.infos() + report.advisories()
        assert len(buckets) == len(report)
        assert {f.code for f in report.errors()} == {"TAG_READONLY_WRITE"}
        assert "COIL_STUCK_HIGH" in {f.code for f in report.warnings()}

    def test_has_errors_reflects_error_findings(self):
        assert self._mixed_program().validate().has_errors() is True

    def test_clean_program_has_no_errors(self):
        btn, motor = Bool("Btn"), Bool("Motor")
        with Program() as prog:
            with Rung(btn):
                out(motor)
        report = validate(prog)
        assert not report.errors()  # the new recommended CI idiom
        assert report.has_errors() is False

    def test_warning_only_report_passes_errors_gate(self):
        # A stuck-high warning must not trip `assert not report.errors()`.
        report = validate(self._mixed_program(), select={"COIL_STUCK_HIGH"})
        assert report  # findings exist
        assert not report.errors()  # ...but none are errors

    def test_summary_breaks_down_by_severity(self):
        summary = self._mixed_program().validate().summary()
        assert "error: 1" in summary
        assert "warning: 1" in summary


class TestRegistry:
    def test_all_rules_is_registry_keys(self):
        assert ALL_RULES == frozenset(RULES)

    def test_every_rule_has_a_known_validator(self):
        assert all(spec.validator in VALIDATOR_ORDER for spec in RULES.values())

    def test_finding_severity_matches_registry(self):
        report = _error_and_warning_program().validate()
        assert report
        for f in report:
            assert f.severity == RULES[f.code].severity

    def test_select_by_category(self):
        report = validate(_error_and_warning_program(), select={"COIL"})
        assert report
        assert all(RULES[f.code].category == "COIL" for f in report)
        assert "TAG_READONLY_WRITE" not in {f.code for f in report}  # TAG bucket

    def test_ignore_by_category(self):
        report = validate(_error_and_warning_program(), ignore={"TAG"})
        codes = {f.code for f in report}
        assert "TAG_READONLY_WRITE" not in codes  # TAG, excluded
        assert "COIL_STUCK_HIGH" in codes  # COIL, kept

    def test_category_and_code_combine(self):
        report = validate(_error_and_warning_program(), select={"COIL", "TAG_READONLY_WRITE"})
        codes = {f.code for f in report}
        assert "COIL_STUCK_HIGH" in codes
        assert "TAG_READONLY_WRITE" in codes

    def test_unknown_category_or_code_raises(self):
        with pytest.raises(ValueError, match="Unknown rule code or category"):
            validate(_error_and_warning_program(), select={"NOPE"})

    def test_every_rule_has_a_nonempty_title(self):
        assert all(spec.title and isinstance(spec.title, str) for spec in RULES.values())

    def test_ordered_rules_is_severity_first_and_complete(self):
        from pyrung.core.validation import ordered_rules
        from pyrung.core.validation.severity import SEVERITY_ORDER

        ordered = ordered_rules()
        assert {s.code for s in ordered} == set(RULES)  # every rule, once
        ranks = [SEVERITY_ORDER[s.severity] for s in ordered]
        assert ranks == sorted(ranks, reverse=True)  # most severe first
