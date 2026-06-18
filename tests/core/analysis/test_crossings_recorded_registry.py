"""Crossings — recorded cause crosses a counter done-bit via the registry (Step 4b).

The footprint read-diff structurally misses a counter/timer accumulator (it is a
*write*, not a read footprint), so recorded cause used to dead-end at a done bit.
The registry fallback reverses ``done == True`` to ``acc >= preset`` and resolves
it against the observed scan, continuing the chase to the accumulator.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Dint
from pyrung.core.analysis.causal.recorded import _cross_via_registry, _registry_writer_for_tag
from pyrung.core.instruction.counters import CountUpInstruction


class _State:
    def __init__(self, tags):
        self.tags = tags


class _History:
    def __init__(self, states):
        self._states = states

    def scan_ids(self):
        return sorted(self._states)

    def at(self, scan_id):
        return _State(self._states[scan_id])


def _counter_rung():
    instr = CountUpInstruction(Bool("Done"), Dint("Acc"), 10, Bool("En"), Bool("Rst"))
    return SimpleNamespace(_instructions=[instr]), instr


def test_registry_writer_finder_matches_both_writes() -> None:
    rung, instr = _counter_rung()
    assert _registry_writer_for_tag(rung, "Done") is instr  # done_bit write
    assert _registry_writer_for_tag(rung, "Acc") is instr  # accumulator write
    assert _registry_writer_for_tag(rung, "Nope") is None


def test_done_bit_crosses_to_accumulator_trigger() -> None:
    rung, _ = _counter_rung()
    history = _History({1: {"Acc": 9, "Done": False}, 2: {"Acc": 10, "Done": True}})
    crossed = _cross_via_registry(
        rung=rung,
        tag_name="Done",
        scan_id=2,
        history=history,
        timelines=None,
        pdg=None,
        scan_log=None,
        initial_tags=None,
    )
    assert crossed is not None
    triggers, enablers = crossed
    assert [t.tag_name for t in triggers] == ["Acc"]  # done crossed to its accumulator
    assert (triggers[0].from_value, triggers[0].to_value) == (9, 10)


def test_no_registry_writer_returns_none() -> None:
    rung, _ = _counter_rung()
    history = _History({1: {}, 2: {}})
    crossed = _cross_via_registry(
        rung=rung,
        tag_name="Unwritten",
        scan_id=2,
        history=history,
        timelines=None,
        pdg=None,
        scan_log=None,
        initial_tags=None,
    )
    assert crossed is None
