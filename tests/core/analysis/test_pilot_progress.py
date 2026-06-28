"""Tests for pilot progress — trend monitoring, checkpoints, regression recovery.

Coverage targets:
- _monitor_trend: checkpoint creation, regression detection, investigation trigger
- _investigate_and_revert: incident construction, replay-fn creation, revert mechanics
- Frontier outcome handling (trend vs flat checkpoint)
- Terminal let-run ejection path (LETRUN-EJECTION)
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Trend monitoring
# ---------------------------------------------------------------------------


class TestCheckpoints:
    """Trend improvement creates checkpoints; flat CONFIRMED does too."""

    @pytest.mark.skip(reason="stub")
    def test_trend_improvement_creates_checkpoint(self): ...

    @pytest.mark.skip(reason="stub")
    def test_flat_confirmed_creates_checkpoint(self): ...

    @pytest.mark.skip(reason="stub")
    def test_frontier_preserves_baseline(self): ...


class TestRegression:
    """Trend regression triggers investigation and revert."""

    @pytest.mark.skip(reason="stub")
    def test_regression_triggers_investigation(self): ...

    @pytest.mark.skip(reason="stub")
    def test_regression_reverts_to_checkpoint(self): ...

    @pytest.mark.skip(reason="stub")
    def test_investigation_holds_installed_before_revert(self): ...

    @pytest.mark.skip(reason="stub")
    def test_regression_nogoods_recorded(self): ...


class TestLetrunEjection:
    """Terminal let-run ejection investigates over the coast-span window."""

    @pytest.mark.skip(reason="stub")
    def test_ejection_anchors_at_coast_start(self): ...

    @pytest.mark.skip(reason="stub")
    def test_ejection_without_checkpoints_is_noop(self): ...
