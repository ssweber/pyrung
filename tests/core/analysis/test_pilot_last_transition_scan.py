"""The last scan a tag changed in a window, read from the records, is the one a replay finds.

how() ranks its corrections by how near each one's tags changed.  The records answer that
without replaying the window.  These tests replay the window as the old code did, and
compare the two answers on these records:

- a counter, a bit that flips every scan, an input patched by hand and a float ramp
- a column with too many changes to keep its values
- a bounded history
- scans run after the records filled
- a compiled run
- a fork
- a NaN that stays, stepped and run by coast_until()
- a NaN that history replays from a checkpoint
- a Real that turns NaN inside the loop
- tags set between scans by hand, by a harness install, and on a fork before its first scan
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, Real, Rung, Timer, copy, math, on_delay, out
from pyrung.core import coast_run, scan_trace
from pyrung.core.analysis.pilot.correction_candidates import _last_transition_scan
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.state import value_changed

Go = Bool("Go", external=True)
Flip = Bool("Flip")
Count = Int("Count")
Noisy = Int("Noisy")
Ramp = Real("Ramp")
Delay = Timer.clone("Delay")
TAGS = ("Go", "Flip", "Count", "Noisy", "Delay_Done", "Delay_Acc", "Ramp", "NeverWritten")


def _program() -> Program:
    with Program(strict=False) as program:
        with Rung(~Flip):
            out(Flip)
        with Rung(Go):
            math(Count + 1, Count)
        with Rung():
            # It changes on every scan, so the records stop keeping its values after 1,000
            # changes.
            math((Noisy * 75 + 74) % 30011, Noisy)
        with Rung(Go):
            math(Ramp + 0.1, Ramp)
        with Rung(Go):
            on_delay(Delay, preset=50)
    return program


def _replayed(plc: PLC, tag: str, start: int, end: int) -> int | None:
    """The old answer.  It replays the window and compares each state with the one before."""
    states = plc.history.range(start, end + 1)
    last = None
    for previous, current in zip(states, states[1:], strict=False):
        if value_changed(previous.tags.get(tag), current.tags.get(tag)):
            last = current.scan_id
    return last


def _stepped(plc: PLC, scans: int) -> None:
    for scan in range(scans):
        if scan % 97 in (0, 40):
            plc.patch({"Go": scan % 97 == 0})
        plc.step()


def _windows(plc: PLC) -> list[tuple[int, int]]:
    tip = plc.history.newest_scan_id
    edges = {0, 1, 2, 40, 41, 97, 98, 137, 500, 999, 1000, tip - 1, tip, tip + 5}
    edges |= {49, 50, 51, 52}
    edges = sorted(edges)
    return [(start, end) for start in edges for end in edges if end >= start - 1]


def _assert_agree(plc: PLC, tags: tuple[str, ...] = TAGS) -> None:
    for tag in tags:
        for start, end in _windows(plc):
            assert _last_transition_scan(plc, tag, start, end) == _replayed(plc, tag, start, end), (
                tag,
                start,
                end,
            )


def _recorded_window(plc: PLC) -> bool:
    return not plc._unrecorded_in(plc.history.oldest_scan_id, plc.history.newest_scan_id)


def test_a_stepped_run_agrees() -> None:
    plc = PLC(_program(), dt=0.01)
    _stepped(plc, 1_200)
    assert _recorded_window(plc)
    assert ("Noisy", "Noisy") in plc._committed_tag_timelines._unknown_keys
    _assert_agree(plc)


def test_a_bounded_history_agrees() -> None:
    plc = PLC(_program(), dt=0.01, history=300)
    _stepped(plc, 1_200)
    assert plc.history.oldest_scan_id > 0
    _assert_agree(plc)


@pytest.mark.parametrize("settled", [False, True], ids=["unrecorded", "settled"])
def test_scans_run_after_the_records_filled_agree(
    settled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 300)
    plc = PLC(_program(), dt=0.01)
    _stepped(plc, 1_200)
    assert not _recorded_window(plc)
    if settled:
        plc._settle_compiled_ranges(at_any_cost=True)
        assert _recorded_window(plc)
    _assert_agree(plc)


def test_a_compiled_run_agrees() -> None:
    plc = PLC(_program(), dt=0.01)
    _stepped(plc, 300)
    plc.patch({"Go": True})
    plc.run_for(9.0)
    _assert_agree(plc)


def test_a_fork_agrees() -> None:
    parent = PLC(_program(), dt=0.01)
    _stepped(parent, 600)
    child: Any = parent.fork(450)
    _stepped(child, 700)
    assert _recorded_window(child)
    _assert_agree(child)


Source = Real("Source", external=True)
Copied = Real("Copied")
Scaled = Real("Scaled")
Steps = Int("Steps")
NAN_TAGS = ("Source", "Copied", "Scaled", "Steps")


def _nan_program() -> Program:
    with Program(strict=False) as program:
        with Rung():
            # copy() keeps the NaN it copies.  math() makes a new one each scan, which is no
            # change.
            copy(Source, Copied)
            math(Source * 1.0, Scaled)
            math(Steps + 1, Steps)
    return program


def _run(plc: PLC, scans: int, coast: bool) -> None:
    end = plc.state.scan_id + scans
    while plc.state.scan_id < end:
        if coast:
            budget = end - plc.state.scan_id
            coast_run.coast_until(
                plc, lambda _state: False, budget=budget, predicate_reads=frozenset()
            )
        else:
            plc.step()


def _changes(plc: PLC, tag: str) -> list[int]:
    states = plc.history.range(0, plc.history.newest_scan_id + 1)
    return [
        current.scan_id
        for previous, current in zip(states, states[1:], strict=False)
        if value_changed(previous.tags.get(tag), current.tags.get(tag))
    ]


@pytest.mark.parametrize(
    ("first", "copied_changed"), [(1.5, 602), (float("nan"), 1)], ids=["nan-later", "nan-first"]
)
@pytest.mark.parametrize("coast", [False, True], ids=["stepped", "coast"])
def test_a_nan_that_stays_agrees(coast: bool, first: float, copied_changed: int) -> None:
    plc = PLC(_nan_program(), dt=0.01)
    plc.patch({"Source": first})
    plc.step()
    _run(plc, 600, coast)
    # A new NaN reaches Copied on scan 602.  Over a NaN it is no change.
    plc.patch({"Source": float("nan")})
    _run(plc, 600, coast)
    assert _recorded_window(plc)
    assert _last_transition_scan(plc, "Copied", 0, plc.history.newest_scan_id) == copied_changed
    _assert_agree(plc, NAN_TAGS)


def _assert_agree_against_stepping(runs: dict[bool, PLC], edges: tuple[int, ...]) -> None:
    for tag in NAN_TAGS:
        assert _changes(runs[True], tag) == _changes(runs[False], tag), tag
    for plc in runs.values():
        for tag in NAN_TAGS:
            for start in edges:
                for end in edges:
                    got = _last_transition_scan(plc, tag, start, end)
                    assert got == _replayed(plc, tag, start, end), (tag, start, end)


def test_a_nan_computed_until_its_rung_stops_agrees() -> None:
    with Program(strict=False) as program:
        with Rung():
            math(Steps + 1, Steps)
        with Rung(Steps < 300):
            math(Source * 1.0, Scaled)
    runs = {}
    for coast in (False, True):
        plc = runs[coast] = PLC(program, dt=0.01)
        plc.patch({"Source": float("nan")})
        _run(plc, 300, coast)
        plc.patch({"Source": 1.0})
        _run(plc, 300, coast)
    # History serves some scans from its cache and replays others from a checkpoint.  The two
    # hold different NaN objects, which is no change.
    assert _changes(runs[True], "Scaled") == [1]
    edges = (0, 1, 2, 199, 200, 201, 298, 299, 300, 301, 302, 306, 307, 308, 599, 600)
    _assert_agree_against_stepping(runs, edges)


def test_a_real_that_turns_nan_in_the_loop_agrees() -> None:
    with Program(strict=False) as program:
        with Rung():
            math(Steps + 1, Steps)
            math(Scaled + 0.5, Scaled)
        with Rung(Scaled > 100.0):
            math(Scaled + Source, Copied)
    runs = {}
    for coast in (False, True):
        plc = runs[coast] = PLC(program, dt=0.01)
        plc.patch({"Source": float("nan")})
        before = scan_trace.TOTALS["loop scans"]
        _run(plc, 700, coast)
    # The loop keeps running once Copied holds a NaN that math() computes again each scan.
    assert scan_trace.TOTALS["loop scans"] - before > 600
    # Copied goes from 0.0 to NaN on scan 201, and stays NaN.
    assert _changes(runs[True], "Copied") == [201]
    edges = (0, 1, 199, 200, 201, 202, 203, 299, 300, 301, 699, 700, 701)
    _assert_agree_against_stepping(runs, edges)


def test_an_edit_between_scans_agrees() -> None:
    plc = PLC(_program(), dt=0.01)
    _stepped(plc, 50)
    plc._edit_tip_tags({"Count": 7, "Noisy": 3})
    _stepped(plc, 300)
    plc._edit_tip_tags({"Count": 9})
    _assert_agree(plc)
    _stepped(plc, 5)
    _assert_agree(plc)


Enable = Bool("Enable", external=True)
Feedback = Bool("Feedback", physical=Physical("MotorFb", on_delay="200ms"), link="Enable")
Stage = Int("Stage")


def test_a_harness_installed_mid_run_agrees() -> None:
    with Program() as program:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    plc = PLC(program, dt=0.01)
    plc.patch({"Enable": True})
    for _ in range(50):
        plc.step()
    # The install seeds Feedback True, since its link has been on for longer than the delay.
    Harness(plc).install()
    for _ in range(20):
        plc.step()
    assert _last_transition_scan(plc, "Feedback", 0, 70) == 50
    _assert_agree(plc, ("Enable", "Feedback", "Stage"))


def test_a_fork_edited_before_its_first_scan_agrees() -> None:
    parent = PLC(_program(), dt=0.01)
    _stepped(parent, 50)
    child: Any = parent.fork()
    child._edit_tip_tags({"Count": 4, "NeverWritten": 4})
    _stepped(child, 100)
    # History shows the parent's state at scan 50, so the edit shows on the fork's first scan.
    assert _last_transition_scan(child, "NeverWritten", 40, 60) == 51
    _assert_agree(child)
    grandchild: Any = child.fork()
    _stepped(grandchild, 100)
    _assert_agree(grandchild)
