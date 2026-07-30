"""Crossings — recorded cause crosses a counter done-bit via the registry (Step 4b).

The footprint read-diff structurally misses a counter/timer accumulator (it is a
*write*, not a read footprint), so recorded cause used to dead-end at a done bit.
The registry fallback reverses ``done == True`` to ``acc >= preset`` and resolves
it against the observed scan, continuing the chase to the accumulator.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Dint
from pyrung.core.analysis import crossings
from pyrung.core.analysis.causal.recorded import _cross_via_registry, _registry_writer_for_tag
from pyrung.core.crossing import Eq, single
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


def test_done_bit_accumulator_is_co_write_filtered() -> None:
    """The accumulator is a co-write of the same instruction — internal state,
    not a user-visible cause.  The registry crossing reverses correctly (tested
    in test_crossings_accumulating.py), but _cross_via_registry filters it out
    so the recorded cause chain stops at the rung condition, not the mechanism."""
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
    assert crossed is None  # co-write filtered: accumulator is internal state


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


def test_tag_bound_done_crossing_retains_changed_preset_cause() -> None:
    preset = Dint("Preset")
    instr = CountUpInstruction(Bool("Done"), Dint("Acc"), preset, Bool("En"), Bool("Rst"))
    rung = SimpleNamespace(_instructions=[instr])
    history = _History(
        {
            1: {"Acc": 5, "Preset": 10, "Done": False},
            2: {"Acc": 5, "Preset": 5, "Done": True},
        }
    )

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
    triggers = crossed.triggers
    enablers = crossed.enablers
    assert [(trigger.tag_name, trigger.from_value, trigger.to_value) for trigger in triggers] == [
        ("Preset", 10, 5)
    ]
    assert enablers == ()


def test_registry_receipt_preserves_reverse_exactness(monkeypatch) -> None:
    rung, _ = _counter_rung()
    history = _History(
        {
            1: {"Source": 1, "Done": False},
            2: {"Source": 2, "Done": True},
        }
    )

    for exact in (True, False):
        monkeypatch.setattr(
            crossings,
            "reverse",
            lambda *_args, _exact=exact, **_kwargs: single(
                Eq("Source", frozenset({2})), exact=_exact
            ),
        )
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
        assert crossed.crossing_exact is exact
