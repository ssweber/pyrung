"""The fold check's stop-predicate comparison can fail (tests/fold_check.py)."""

from __future__ import annotations

import pytest

from pyrung import PLC, Int, Program, Rung, calc
from pyrung.core import compiled_run, scan_trace
from tests import fold_check
from tests.fold_check import FoldMismatch


def _plc() -> PLC:
    n = Int("N")
    with Program(strict=False) as prog:
        with Rung():
            calc(n + 1, n)
    return PLC(prog)


def _stepper(plc: PLC, scans: int):
    def call() -> None:
        for _ in range(scans):
            plc.step()

    return call


def test_a_late_stop_is_caught() -> None:
    plc = _plc()
    with pytest.raises(
        FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 9"
    ):
        fold_check._checked("run_until", plc, _stepper(plc, 9), lambda s: s.tags["N"] >= 5)


def test_a_stop_on_the_first_true_scan_passes() -> None:
    plc = _plc()
    fold_check._checked("run_until", plc, _stepper(plc, 5), lambda s: s.tags["N"] >= 5)


def test_a_predicate_over_the_real_runner_is_not_asked() -> None:
    plc = _plc()
    before = fold_check.STATS["predicate skipped: reads the real runner"]
    fold_check._checked("run_until", plc, _stepper(plc, 9), lambda _s: plc.state.tags["N"] >= 5)
    assert fold_check.STATS["predicate skipped: reads the real runner"] == before + 1


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch):
    """The real wrappers, installed for this test only (everything is restored)."""
    import pyrung.core.analysis.pilot.cyclefold as cyclefold
    import pyrung.core.fold as fold

    for mod, name in (
        (fold, "fold_run_until"),
        (fold, "fold_run_for"),
        (cyclefold, "cycle_fold_until"),
        (compiled_run, "run_scans"),
        (compiled_run, "INTERPRETED_PREFIX"),
        (scan_trace, "MIN_RUN_SCANS"),
        (scan_trace, "BURST_SIZES"),
        (scan_trace, "STRICT_SELF_CHECK"),
    ):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    monkeypatch.setattr(fold, "_fold_check_installed", False, raising=False)
    return fold, cyclefold


def _overshooting(extra: int):
    def drive(plc, predicate, *a, **k):
        while not predicate(plc.state):
            plc.step()
        for _ in range(extra):
            plc.step()
        return plc.state

    return drive


def _overshooting_scans(extra: int):
    drive = _overshooting(extra)

    def run_scans(plc, _scans, predicate, **_kwargs):
        return drive(plc, predicate)

    return run_scans


def test_run_until_through_install_catches_a_late_stop(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "run_scans", _overshooting_scans(3))
    fold_check.install()
    plc = _plc()
    with pytest.raises(
        FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 8"
    ):
        plc.run_until(lambda s: s.tags["N"] >= 5, max_cycles=100)


def test_run_until_through_install_passes_an_exact_stop(installed, monkeypatch) -> None:
    monkeypatch.setattr(compiled_run, "run_scans", _overshooting_scans(0))
    fold_check.install()
    plc = _plc()
    plc.run_until(lambda s: s.tags["N"] >= 5, max_cycles=100)
    assert plc.state.scan_id == 5


def test_cycle_fold_through_install_catches_a_late_stop(installed, monkeypatch) -> None:
    _, cyclefold = installed
    monkeypatch.setattr(cyclefold, "cycle_fold_until", _overshooting(2))
    fold_check.install()
    plc = _plc()
    with pytest.raises(
        FoldMismatch, match="stop predicate true at scan 5, folded run stopped at 7"
    ):
        cyclefold.cycle_fold_until(plc, lambda s: s.tags["N"] >= 5, budget=100)


def test_a_stateful_predicate_is_not_advanced_by_the_check() -> None:
    plc = _plc()
    calls = []

    def predicate(s) -> bool:
        calls.append(s.scan_id)
        return s.tags["N"] >= 5

    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert calls == []


def test_a_predicate_that_swallows_exceptions_is_still_detected() -> None:
    plc = _plc()
    before = fold_check.STATS["predicate skipped: reads the real runner"]

    def predicate(_s) -> bool:
        try:
            return plc.state.tags["N"] >= 5
        except Exception:
            return False

    fold_check._checked("run_until", plc, _stepper(plc, 9), predicate)
    assert fold_check.STATS["predicate skipped: reads the real runner"] == before + 1


def test_a_counting_predicate_is_judged_from_its_own_copy() -> None:
    plc = _plc()
    k = [0]

    def predicate(_s) -> bool:
        k[0] += 1
        return k[0] >= 5

    # The folded run's own calls are not made here; the copy is taken first and
    # the real closure is untouched afterwards.
    fold_check._checked("run_until", plc, _stepper(plc, 5), predicate)
    assert k == [0]
