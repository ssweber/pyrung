"""CircuitPySim.stop() survives a temp directory Windows holds a moment."""

from __future__ import annotations

import tempfile

from pyrung.conformance import _circuitpy
from pyrung.conformance._circuitpy import CircuitPySim


def _sim(monkeypatch, cleanup):
    monkeypatch.setattr(_circuitpy.time, "sleep", lambda _s: None)
    sim = CircuitPySim.__new__(CircuitPySim)
    sim._proc = None
    sim._log = None
    sim._dir = tempfile.TemporaryDirectory(prefix="pyrung-cleanup-test-")
    monkeypatch.setattr(sim._dir, "cleanup", cleanup)
    return sim


def test_stop_retries_a_held_directory(monkeypatch):
    calls = []

    def cleanup():
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(32, "in use")

    sim = _sim(monkeypatch, cleanup)
    sim.stop()
    assert len(calls) == 2
    assert sim._dir is None


def test_stop_gives_up_quietly_on_a_directory_held_for_good(monkeypatch):
    def cleanup():
        raise PermissionError(32, "in use")

    sim = _sim(monkeypatch, cleanup)
    sim.stop()
    assert sim._dir is None
