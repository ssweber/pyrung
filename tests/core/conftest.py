"""Fixtures shared by the core tests."""

from __future__ import annotations

import pytest

from pyrung.core import compiled_run, scan_trace


@pytest.fixture
def loop_from_first_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every long-run call through the kernel and the guarded loop, however short.

    At production settings a run's first 1000 scans are interpreted, which would hide
    the loop from any test shorter than that.
    """
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
