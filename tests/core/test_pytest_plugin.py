"""Tests for pytest plugin and whitelist (Sections H3 + H4).

Covers:
- H3: CoverageCollector, session merge, JSON output
- H4: Whitelist loading (TOML), check_whitelist, CI gating
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyrung.core import PLC, And, Bool, Program, Rung, latch, out, reset
from pyrung.core.analysis.query import CoverageReport
from pyrung.pytest_plugin import (
    CoverageCollector,
    Whitelist,
    check_whitelist,
    load_whitelist,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_recoverable():
    """Latch with a reset rung — fault recovers."""
    Sensor = Bool("Sensor")
    Fault = Bool("Fault")
    Reset = Bool("Reset")
    Alarm = Bool("Alarm")

    with Program() as logic:
        with Rung(Sensor):
            latch(Fault)
        with Rung(And(Fault, Reset)):
            reset(Fault)
        with Rung(Fault):
            out(Alarm)

    return logic, Sensor, Fault, Reset, Alarm


def _build_stranded():
    """Latch with no reset rung — fault is stranded."""
    Sensor = Bool("Sensor")
    Fault = Bool("Fault")
    Alarm = Bool("Alarm")

    with Program() as logic:
        with Rung(Sensor):
            latch(Fault)
        with Rung(Fault):
            out(Alarm)

    return logic, Sensor, Fault, Alarm


# ---------------------------------------------------------------------------
# H3: CoverageCollector
# ---------------------------------------------------------------------------


class TestCoverageCollector:
    """Unit tests for CoverageCollector without pytest hooks."""

    def test_empty_collector_merge_returns_none(self) -> None:
        collector = CoverageCollector()
        assert collector.merge() is None

    def test_collect_from_plc(self) -> None:
        logic, *_ = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        collector = CoverageCollector()
        collector.collect(runner)
        assert len(collector.reports) == 1
        assert isinstance(collector.reports[0], CoverageReport)

    def test_collect_report_directly(self) -> None:
        report = CoverageReport(cold_rungs=frozenset({0}))
        collector = CoverageCollector()
        collector.collect_report(report)
        assert len(collector.reports) == 1

    def test_merge_single_report(self) -> None:
        logic, *_ = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        collector = CoverageCollector()
        collector.collect(runner)
        merged = collector.merge()
        assert merged is not None
        assert isinstance(merged, CoverageReport)

    def test_merge_two_plc_runs(self) -> None:
        """Two tests against the same program: merged cold shrinks."""
        # Test 1: trip fault, don't reset
        logic, *_ = _build_recoverable()
        runner1 = PLC(logic)
        runner1.patch({"Sensor": True})
        runner1.step()

        # Test 2: trip fault AND reset
        logic2, *_ = _build_recoverable()
        runner2 = PLC(logic2)
        runner2.patch({"Sensor": True})
        runner2.step()
        runner2.patch({"Reset": True})
        runner2.step()

        collector = CoverageCollector()
        collector.collect(runner1)
        collector.collect(runner2)
        merged = collector.merge()

        assert merged is not None
        # Test 2 exercised rung 1 → not cold in merged
        assert 1 not in merged.cold_rungs

    def test_merge_stranded_intersection(self) -> None:
        """Stranded bits disappear when one test shows recovery."""
        # Test 1: stranded program (no reset rung)
        logic1, *_ = _build_stranded()
        runner1 = PLC(logic1)
        runner1.patch({"Sensor": True})
        runner1.step()

        # Test 2: recoverable program (has reset rung)
        logic2, *_ = _build_recoverable()
        runner2 = PLC(logic2)
        runner2.patch({"Sensor": True, "Reset": True})
        runner2.step()

        collector = CoverageCollector()
        collector.collect(runner1)
        collector.collect(runner2)
        merged = collector.merge()

        assert merged is not None
        # Reports come from different programs, so stranded_chains
        # identities differ — intersection produces empty set
        stranded_tags = {tag for tag, _ in merged.stranded_chains}
        assert "Fault" not in stranded_tags


# ---------------------------------------------------------------------------
# H4: Whitelist
# ---------------------------------------------------------------------------


class TestWhitelist:
    """Whitelist loading and check_whitelist."""

    def test_load_missing_file(self, tmp_path: Path) -> None:
        wl = load_whitelist(tmp_path / "nonexistent.toml")
        assert wl == Whitelist()

    def test_load_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "whitelist.toml"
        p.write_text("", encoding="utf-8")
        wl = load_whitelist(p)
        assert wl == Whitelist()

    def test_load_cold_rungs(self, tmp_path: Path) -> None:
        p = tmp_path / "whitelist.toml"
        p.write_text(
            "[cold_rungs]\nallow = [22, 91, 104]\n",
            encoding="utf-8",
        )
        wl = load_whitelist(p)
        assert wl.cold_rungs == frozenset({22, 91, 104})
        assert wl.stranded_tags == frozenset()

    def test_load_stranded_chains(self, tmp_path: Path) -> None:
        p = tmp_path / "whitelist.toml"
        p.write_text(
            '[stranded_chains]\nallow = ["Sts_SpecialFault", "Sts_Other"]\n',
            encoding="utf-8",
        )
        wl = load_whitelist(p)
        assert wl.stranded_tags == frozenset({"Sts_SpecialFault", "Sts_Other"})

    def test_load_both_sections(self, tmp_path: Path) -> None:
        p = tmp_path / "whitelist.toml"
        p.write_text(
            '[cold_rungs]\nallow = [5]\n\n[stranded_chains]\nallow = ["Fault"]\n',
            encoding="utf-8",
        )
        wl = load_whitelist(p)
        assert wl.cold_rungs == frozenset({5})
        assert wl.stranded_tags == frozenset({"Fault"})

    def test_check_whitelist_all_covered(self) -> None:
        report = CoverageReport(
            cold_rungs=frozenset({22, 91}),
            stranded_chains=frozenset({("Fault", ())}),
        )
        wl = Whitelist(cold_rungs=frozenset({22, 91}), stranded_tags=frozenset({"Fault"}))
        new_cold, new_stranded = check_whitelist(report, wl)
        assert new_cold == set()
        assert new_stranded == set()

    def test_check_whitelist_new_findings(self) -> None:
        report = CoverageReport(
            cold_rungs=frozenset({22, 91, 200}),
            stranded_chains=frozenset({("Fault", ()), ("NewFault", ())}),
        )
        wl = Whitelist(cold_rungs=frozenset({22, 91}), stranded_tags=frozenset({"Fault"}))
        new_cold, new_stranded = check_whitelist(report, wl)
        assert new_cold == {200}
        assert new_stranded == {"NewFault"}

    def test_check_whitelist_empty(self) -> None:
        report = CoverageReport(
            cold_rungs=frozenset({1, 2}),
            stranded_chains=frozenset({("X", ())}),
        )
        wl = Whitelist()
        new_cold, new_stranded = check_whitelist(report, wl)
        assert new_cold == {1, 2}
        assert new_stranded == {"X"}

    def test_check_whitelist_no_findings(self) -> None:
        report = CoverageReport()
        wl = Whitelist(cold_rungs=frozenset({1}), stranded_tags=frozenset({"X"}))
        new_cold, new_stranded = check_whitelist(report, wl)
        assert new_cold == set()
        assert new_stranded == set()


# ---------------------------------------------------------------------------
# H3/H4: Integration with pytester
# ---------------------------------------------------------------------------


class TestPluginIntegration:
    """Integration tests using pytester to exercise the full plugin."""

    def test_json_output(self, pytester: pytest.Pytester) -> None:
        """Plugin writes pyrung_coverage.json with merged report."""
        pytester.makeconftest(
            """
            import pytest
            from pyrung.core import PLC, Bool, Program, Rung, latch, out
            from pyrung.pytest_plugin import CoverageCollector

            pytest_plugins = ["pyrung.pytest_plugin"]

            @pytest.fixture
            def plc(pyrung_coverage):
                Sensor = Bool("Sensor")
                Fault = Bool("Fault")
                with Program() as logic:
                    with Rung(Sensor):
                        latch(Fault)
                    with Rung(Fault):
                        out(Bool("Alarm"))
                runner = PLC(logic)
                yield runner
                pyrung_coverage.collect(runner)
            """
        )
        pytester.makepyfile(
            """
            def test_trip_fault(plc):
                plc.patch({"Sensor": True})
                plc.step()
                assert plc.current_state.tags["Fault"] is True
            """
        )
        result = pytester.runpytest("--pyrung-coverage-json=coverage.json")
        result.assert_outcomes(passed=1)

        import json

        coverage_path = pytester.path / "coverage.json"
        assert coverage_path.exists()
        data = json.loads(coverage_path.read_text(encoding="utf-8"))
        assert "cold_rungs" in data
        assert "hot_rungs" in data
        assert "stranded_chains" in data

    def test_json_output_disabled(self, pytester: pytest.Pytester) -> None:
        """Empty string disables JSON output."""
        pytester.makeconftest(
            """
            import pytest
            from pyrung.core import PLC, Bool, Program, Rung, out
            from pyrung.pytest_plugin import CoverageCollector

            pytest_plugins = ["pyrung.pytest_plugin"]

            @pytest.fixture
            def plc(pyrung_coverage):
                A = Bool("A")
                with Program() as logic:
                    with Rung(A):
                        out(Bool("X"))
                runner = PLC(logic)
                yield runner
                pyrung_coverage.collect(runner)
            """
        )
        pytester.makepyfile(
            """
            def test_noop(plc):
                plc.step()
            """
        )
        result = pytester.runpytest("--pyrung-coverage-json=")
        result.assert_outcomes(passed=1)
        # No coverage file should be written
        assert not (pytester.path / "pyrung_coverage.json").exists()

    def test_whitelist_pass(self, pytester: pytest.Pytester) -> None:
        """All findings whitelisted → no failure."""
        pytester.makeconftest(
            """
            import pytest
            from pyrung.core import PLC, Bool, Program, Rung, latch, out
            pytest_plugins = ["pyrung.pytest_plugin"]

            @pytest.fixture
            def plc(pyrung_coverage):
                Sensor = Bool("Sensor")
                Fault = Bool("Fault")
                with Program() as logic:
                    with Rung(Sensor):
                        latch(Fault)
                    with Rung(Fault):
                        out(Bool("Alarm"))
                runner = PLC(logic)
                yield runner
                pyrung_coverage.collect(runner)
            """
        )
        pytester.makepyfile(
            """
            def test_trip(plc):
                plc.patch({"Sensor": True})
                plc.step()
            """
        )
        # Whitelist covers everything — rung 0 fires (latch), rung 1 fires (out)
        # so no cold rungs. Stranded bit Fault is the only finding.
        wl = pytester.path / "whitelist.toml"
        wl.write_text(
            '[cold_rungs]\nallow = []\n\n[stranded_chains]\nallow = ["Fault"]\n',
            encoding="utf-8",
        )
        result = pytester.runpytest(
            f"--pyrung-whitelist={wl}",
            "--pyrung-coverage-json=",
        )
        result.assert_outcomes(passed=1)

    def test_whitelist_fail(self, pytester: pytest.Pytester) -> None:
        """New stranded bit not in whitelist → exitstatus 1."""
        pytester.makeconftest(
            """
            import pytest
            from pyrung.core import PLC, Bool, Program, Rung, latch, out
            pytest_plugins = ["pyrung.pytest_plugin"]

            @pytest.fixture
            def plc(pyrung_coverage):
                Sensor = Bool("Sensor")
                Fault = Bool("Fault")
                with Program() as logic:
                    with Rung(Sensor):
                        latch(Fault)
                    with Rung(Fault):
                        out(Bool("Alarm"))
                runner = PLC(logic)
                yield runner
                pyrung_coverage.collect(runner)
            """
        )
        pytester.makepyfile(
            """
            def test_trip(plc):
                plc.patch({"Sensor": True})
                plc.step()
            """
        )
        # Whitelist is empty — stranded Fault is a new finding
        wl = pytester.path / "whitelist.toml"
        wl.write_text("", encoding="utf-8")
        result = pytester.runpytest(
            f"--pyrung-whitelist={wl}",
            "--pyrung-coverage-json=",
        )
        assert result.ret != 0
        result.stdout.fnmatch_lines(["*stranded bits not in whitelist*"])

    def test_no_fixture_usage_no_crash(self, pytester: pytest.Pytester) -> None:
        """Plugin loaded but fixture never used → no crash, no output."""
        pytester.makeconftest(
            """
            pytest_plugins = ["pyrung.pytest_plugin"]
            """
        )
        pytester.makepyfile(
            """
            def test_plain():
                assert 1 + 1 == 2
            """
        )
        result = pytester.runpytest("--pyrung-coverage-json=coverage.json")
        result.assert_outcomes(passed=1)
        assert not (pytester.path / "coverage.json").exists()

    def test_multiple_tests_merge(self, pytester: pytest.Pytester) -> None:
        """Reports from multiple tests merge correctly."""
        pytester.makeconftest(
            """
            import pytest
            from pyrung.core import PLC, Bool, Program, Rung, And, latch, out, reset
            pytest_plugins = ["pyrung.pytest_plugin"]

            @pytest.fixture
            def plc(pyrung_coverage):
                Sensor = Bool("Sensor")
                Fault = Bool("Fault")
                ResetBtn = Bool("ResetBtn")
                Alarm = Bool("Alarm")
                with Program() as logic:
                    with Rung(Sensor):
                        latch(Fault)
                    with Rung(And(Fault, ResetBtn)):
                        reset(Fault)
                    with Rung(Fault):
                        out(Alarm)
                runner = PLC(logic)
                yield runner
                pyrung_coverage.collect(runner)
            """
        )
        pytester.makepyfile(
            """
            def test_trip_only(plc):
                plc.patch({"Sensor": True})
                plc.step()

            def test_trip_and_reset(plc):
                plc.patch({"Sensor": True})
                plc.step()
                plc.patch({"ResetBtn": True})
                plc.step()
            """
        )
        result = pytester.runpytest("--pyrung-coverage-json=coverage.json")
        result.assert_outcomes(passed=2)

        import json

        data = json.loads((pytester.path / "coverage.json").read_text(encoding="utf-8"))
        # test_trip_and_reset exercises rung 1 → no cold rungs
        assert 1 not in data["cold_rungs"]
