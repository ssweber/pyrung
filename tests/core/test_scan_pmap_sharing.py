"""Regression tests: idle scans share state PMaps across commits.

Pyrsistent PMaps are rebuilt on every evolver mutation.  Unconditional
per-scan writes (system-point defaults, ``_dt``, etc.) used to churn the
tag and memory PMaps even when nothing observable changed, so long-running
debug sessions grew memory linearly.  These tests lock in structural
sharing for the idle case.
"""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, out


def _idle_runner() -> PLC:
    light = Bool("Light")
    with Program(strict=False) as logic:
        with Rung():
            out(light)
    return PLC(logic, dt=0.01)


def test_idle_scan_reuses_memory_pmap() -> None:
    runner = _idle_runner()

    # Prime: first two scans materialize `_dt`, scan-stat bindings,
    # `_prev:*` for every tag, and system-point defaults.  From scan 3+
    # the memory PMap should settle.
    for _ in range(3):
        runner.step()
    primed_memory = runner.current_state.memory

    for _ in range(20):
        runner.step()
        assert runner.current_state.memory is primed_memory


def test_idle_scan_reuses_tags_pmap() -> None:
    runner = _idle_runner()

    for _ in range(3):
        runner.step()
    primed_tags = runner.current_state.tags

    for _ in range(20):
        runner.step()
        assert runner.current_state.tags is primed_tags


def test_idle_scan_reuses_rung_firing_timeline_range() -> None:
    """A rung firing the same pattern every scan stays in a single range.

    Under the per-rung timeline storage, idle-scan memory reuse is no
    longer a PMap-identity property — it's a structural one.  A rung
    that produces the same canonical writes every scan extends its
    tail range's ``end_scan_id`` instead of allocating a new entry.
    """
    runner = _idle_runner()

    for _ in range(3):
        runner.step()
    timeline = runner._rung_firing_timelines._timelines.get(0, [])
    assert len(timeline) == 1

    for _ in range(20):
        runner.step()
        timeline = runner._rung_firing_timelines._timelines[0]
        # Still a single range, extended to cover every idle scan.
        assert len(timeline) == 1
        assert timeline[0].end_scan_id == runner.current_state.scan_id
