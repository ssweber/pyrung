"""The fold check's stop-predicate comparison can fail (tests/fold_check.py)."""

from __future__ import annotations

import pytest

from pyrung import PLC, Int, Program, Rung, calc
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
