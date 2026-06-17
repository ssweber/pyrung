"""Tests for query namespace and coverage report (Sections G + H).

Covers:
- G1: plc.recovers(tag) convenience predicate
- G2: plc.query.cold_rungs() and plc.query.hot_rungs()
- G3: plc.query.stranded_bits()
- G7: Survey methods against known programs
- H1: CoverageReport dataclass with merge()
"""

from __future__ import annotations

from pyrung.core import PLC, And, Bool, Program, Rung, call, latch, out, reset, subroutine
from pyrung.core.program import rung

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_recoverable():
    """Latch with a reset rung — fault recovers.

    Rung 0: Sensor → latch(Fault)
    Rung 1: And(Fault, Reset) → reset(Fault)
    Rung 2: Fault → out(Alarm)
    """
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
    """Latch with no reset rung — fault is stranded.

    Rung 0: Sensor → latch(Fault)
    Rung 1: Fault → out(Alarm)
    """
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
# G1: recovers()
# ---------------------------------------------------------------------------


class TestRecovers:
    """plc.recovers(tag) — convenience predicate over projected cause."""

    def test_recoverable_fault(self) -> None:
        logic, Sensor, Fault, Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True, "Reset": True})
        runner.step()

        assert runner.recovers(Fault) is True

    def test_stranded_fault(self) -> None:
        logic, Sensor, Fault, _Alarm = _build_stranded()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        assert runner.recovers(Fault) is False

    def test_recovers_with_string(self) -> None:
        logic, Sensor, Fault, Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True, "Reset": True})
        runner.step()

        assert runner.recovers("Fault") is True

    def test_recovers_already_at_resting(self) -> None:
        """Tag already at default → projected mode, empty steps → recovers."""
        logic, _Sensor, Fault, _Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.step()

        # Fault is False (default), so cause(Fault, to=False) = projected, empty
        assert runner.recovers(Fault) is True


# ---------------------------------------------------------------------------
# G2: cold_rungs / hot_rungs
# ---------------------------------------------------------------------------


class TestColdRungs:
    """plc.query.cold_rungs() — rungs that never fired."""

    def test_latch_and_reset_cold_before_input(self) -> None:
        logic, _Sensor, _Fault, _Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.step()

        # latch/reset rungs (1, 2) are no-ops when disabled → cold.
        # out() rung (3) writes False even when disabled → not cold.
        cold = runner.query.cold_rungs()
        assert cold == ["1", "2"]

    def test_some_cold_after_input(self) -> None:
        logic, _Sensor, _Fault, _Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        # Rung 1 fired (latched Fault), Rung 3 fired (out Alarm)
        # Rung 2 is cold (needs Fault AND Reset, Reset is False)
        cold = runner.query.cold_rungs()
        assert "2" in cold
        assert "1" not in cold
        assert "3" not in cold

    def test_no_cold_all_exercised(self) -> None:
        logic, _Sensor, _Fault, _Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()  # Rung 0, 2 fire
        runner.patch({"Reset": True})
        runner.step()  # Rung 1 fires (Fault AND Reset)

        cold = runner.query.cold_rungs()
        assert cold == []


class TestHotRungs:
    """plc.query.hot_rungs() — rungs that fired every scan."""

    def test_out_rungs_are_always_hot(self) -> None:
        """out() writes both True and False, so out-rungs fire every scan."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(X)
            with Rung(A):
                out(Y)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()
        runner.step()

        hot = runner.query.hot_rungs()
        assert "1" in hot
        assert "2" in hot

    def test_latch_rung_not_hot_when_intermittent(self) -> None:
        """latch() is no-op when disabled, so it only fires some scans."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # fires
        runner.patch({"A": False})
        runner.step()  # doesn't fire

        assert "1" not in runner.query.hot_rungs()

    def test_no_hot_when_only_latch_reset(self) -> None:
        """Programs with only latch/reset have no hot rungs when nothing enables."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)
            with Rung(B):
                reset(X)

        runner = PLC(logic)
        runner.step()

        assert runner.query.hot_rungs() == []

    def test_hot_across_all_scans(self) -> None:
        """A rung must fire every scan to be hot."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # fires
        runner.patch({"A": False})
        runner.step()  # doesn't fire (latch is no-op when disabled)

        # Rung 1 only fired on scan 1, not scan 2
        hot = runner.query.hot_rungs()
        assert "1" not in hot


class TestSubroutineColdHot:
    """cold_rungs / hot_rungs see subroutine rungs (labelled ``Sub:N``)."""

    def test_uncalled_subroutine_rungs_are_cold(self) -> None:
        """A subroutine never called shows its rungs as cold ``Sub:N`` labels."""
        Enable = Bool("Enable")
        Trigger = Bool("Trigger")
        Out = Bool("Out")

        @subroutine("Sub")
        def sub():
            with rung(Trigger):
                out(Out)

        with Program() as prog:
            with Rung(Enable):
                call(sub)

        runner = PLC(prog)
        runner.step()  # Enable False → subroutine never called

        # Pre-fix this was invisible: subroutine rungs weren't in the universe.
        assert "Sub:1" in runner.query.cold_rungs()

    def test_called_subroutine_rung_is_hot(self) -> None:
        """A subroutine rung that fires every scan is hot, labelled ``Sub:N``."""
        Enable = Bool("Enable")
        Out = Bool("Out")

        @subroutine("Sub")
        def sub():
            with rung(Enable):
                out(Out)

        with Program() as prog:
            with Rung(Enable):
                call(sub)

        runner = PLC(prog)
        runner.patch({"Enable": True})
        runner.step()
        runner.step()

        assert "Sub:1" in runner.query.hot_rungs()
        # Not cold once it has fired.
        assert "Sub:1" not in runner.query.cold_rungs()

    def test_main_and_subroutine_same_index_are_distinct(self) -> None:
        """A main rung and a subroutine rung sharing index 0 get distinct labels."""
        Enable = Bool("Enable")
        Out = Bool("Out")

        @subroutine("Sub")
        def sub():
            with rung(Enable):  # subroutine rung index 0
                out(Out)

        with Program() as prog:
            with Rung(Enable):  # main rung index 0
                call(sub)

        runner = PLC(prog)
        runner.step()  # nothing enabled → subroutine never runs

        cold = runner.query.cold_rungs()
        # The subroutine rung (index 0) is reported separately from main rungs.
        assert "Sub:1" in cold

    def test_subroutine_called_twice_per_scan_dedups(self) -> None:
        """A subroutine called from two sites in one scan records one entry per rung."""
        A = Bool("A")
        B = Bool("B")
        Out = Bool("Out")

        @subroutine("Sub")
        def sub():
            with rung(A):
                out(Out)

        with Program() as prog:
            with Rung(A):
                call(sub)
            with Rung(B):
                call(sub)

        runner = PLC(prog)
        runner.patch({"A": True, "B": True})
        # Two calls per scan must not violate the timeline's one-entry-per-scan
        # invariant — the per-scan node firing dict dedups by RungId.
        runner.step()
        runner.step()

        assert "Sub:1" in runner.query.hot_rungs()


# ---------------------------------------------------------------------------
# G3: stranded_bits
# ---------------------------------------------------------------------------


class TestStrandedBits:
    """plc.query.stranded_bits() — persistent bits with no clear path."""

    def test_no_stranded_when_recoverable(self) -> None:
        logic, _Sensor, _Fault, _Reset, _Alarm = _build_recoverable()
        runner = PLC(logic)
        runner.patch({"Sensor": True, "Reset": True})
        runner.step()

        stranded = runner.query.stranded_bits()
        assert len(stranded) == 0

    def test_stranded_when_no_reset(self) -> None:
        logic, _Sensor, Fault, _Alarm = _build_stranded()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        stranded = runner.query.stranded_bits()
        assert len(stranded) == 1
        assert stranded[0].effect.tag_name == "Fault"
        assert stranded[0].mode == "unreachable"

    def test_stranded_carries_blockers(self) -> None:
        logic, _Sensor, _Fault, _Alarm = _build_stranded()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        chain = runner.query.stranded_bits()[0]
        assert len(chain.blockers) > 0

    def test_only_latch_tags_considered(self) -> None:
        """out()-driven tags are self-clearing and not in stranded_bits."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                out(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        # X is driven by out(), not latch(), so not considered persistent
        stranded = runner.query.stranded_bits()
        assert len(stranded) == 0

    def test_multiple_stranded_bits(self) -> None:
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                latch(X)
                latch(Y)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        stranded = runner.query.stranded_bits()
        tags = {c.effect.tag_name for c in stranded}
        assert tags == {"X", "Y"}


# ---------------------------------------------------------------------------
# H1: CoverageReport merge
# ---------------------------------------------------------------------------


class TestCoverageReport:
    """CoverageReport with merge() — set algebra across tests."""

    def test_report_from_query(self) -> None:
        logic, _Sensor, _Fault, _Alarm = _build_stranded()
        runner = PLC(logic)
        runner.patch({"Sensor": True})
        runner.step()

        report = runner.query.report()
        assert isinstance(report.cold_rungs, frozenset)
        assert isinstance(report.stranded_chains, frozenset)

    def test_merge_cold_rungs_intersection(self) -> None:
        """Cold rungs merge by intersection — only cold if no test fired it."""
        from pyrung.core.analysis.query import CoverageReport

        r1 = CoverageReport(cold_rungs=frozenset({"0", "1", "2"}))
        r2 = CoverageReport(cold_rungs=frozenset({"1", "2", "3"}))
        merged = r1.merge(r2)
        assert merged.cold_rungs == frozenset({"1", "2"})

    def test_merge_hot_rungs_intersection(self) -> None:
        """Hot rungs merge by intersection — only hot if hot in every test."""
        from pyrung.core.analysis.query import CoverageReport

        r1 = CoverageReport(hot_rungs=frozenset({"0", "1"}))
        r2 = CoverageReport(hot_rungs=frozenset({"1", "2"}))
        merged = r1.merge(r2)
        assert merged.hot_rungs == frozenset({"1"})

    def test_merge_stranded_intersection(self) -> None:
        """Stranded chains merge by intersection on chain identity."""
        from pyrung.core.analysis.query import CoverageReport

        chain_a = ("Fault", ((-1, "Fault", False, "NO_OBSERVED_TRANSITION"),))
        chain_b = ("Other", ((-1, "Other", False, "NO_OBSERVED_TRANSITION"),))

        r1 = CoverageReport(stranded_chains=frozenset({chain_a, chain_b}))
        r2 = CoverageReport(stranded_chains=frozenset({chain_a}))
        merged = r1.merge(r2)
        assert merged.stranded_chains == frozenset({chain_a})

    def test_merge_monotonic_shrinkage(self) -> None:
        """Each merge can only shrink residuals, never grow them."""
        from pyrung.core.analysis.query import CoverageReport

        r1 = CoverageReport(
            cold_rungs=frozenset({"0", "1", "2", "3"}),
            stranded_chains=frozenset({("X", ()), ("Y", ())}),
        )
        r2 = CoverageReport(
            cold_rungs=frozenset({"2", "3", "4"}),
            stranded_chains=frozenset({("Y", ()), ("Z", ())}),
        )
        merged = r1.merge(r2)
        assert len(merged.cold_rungs) <= min(len(r1.cold_rungs), len(r2.cold_rungs))
        assert len(merged.stranded_chains) <= min(len(r1.stranded_chains), len(r2.stranded_chains))

    def test_to_dict(self) -> None:
        from pyrung.core.analysis.query import CoverageReport

        r = CoverageReport(
            cold_rungs=frozenset({"2", "0"}),
            hot_rungs=frozenset({"1"}),
            stranded_chains=frozenset(),
        )
        d = r.to_dict()
        assert d["cold_rungs"] == ["0", "2"]
        assert d["hot_rungs"] == ["1"]
        assert d["stranded_chains"] == []

    def test_end_to_end_two_tests_merge(self) -> None:
        """Simulate two tests against the same program, merge reports."""
        # Test 1: trip the fault but don't reset
        logic, _Sensor, _Fault, _Reset, _Alarm = _build_recoverable()
        runner1 = PLC(logic)
        runner1.patch({"Sensor": True})
        runner1.step()
        report1 = runner1.query.report()

        # Test 2: trip the fault AND reset
        logic2, _, _, _, _ = _build_recoverable()
        runner2 = PLC(logic2)
        runner2.patch({"Sensor": True})
        runner2.step()
        runner2.patch({"Reset": True})
        runner2.step()
        report2 = runner2.query.report()

        merged = report1.merge(report2)

        # Test 2 exercised the reset rung (rung 2) → it is not cold in merged
        assert "2" not in merged.cold_rungs
        # Test 2 showed recovery path → no stranded bits in merged
        assert len(merged.stranded_chains) == 0
