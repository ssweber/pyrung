"""Tests for the per-rung range-encoded firing timelines.

The previous ``scan_id -> PMap`` storage was pathological for any
rung that oscillated (scan-clock toggle, timer acc, cycle
oscillators).  The new timeline encoding stores one range per stable
run, collapses period-2 alternation into a single ``AlternatingRun``,
and falls back to ``FiredOnly`` for rungs whose pattern intern pool
explodes past ``_FIRED_ONLY_THRESHOLD``.

These tests exercise the module directly where possible (cheap,
focused) and through the PLC where end-to-end behavior matters
(lookup shape compatibility, reboot reset, the cycle→fired_only
threshold).  All PLC-driven programs use ``record_all_tags=True``
so the assertions pin down the timeline shape — not the interaction
between PDG filtering and timeline storage.
"""

from __future__ import annotations

from pyrsistent import pmap

from pyrung.core import PLC, Bool, Int, Program, Rung, out
from pyrung.core.rung_firings import (
    _FIRED_ONLY_THRESHOLD,
    AlternatingRun,
    FiredOnly,
    PatternRef,
    RungFiringTimelines,
)

# ---------------------------------------------------------------------------
# Direct exercises of the RungFiringTimelines surface
# ---------------------------------------------------------------------------


def test_stable_rung_single_range() -> None:
    """A rung firing the same pattern for 100 scans is one range, one pattern."""
    timelines = RungFiringTimelines()
    pattern = pmap({"Light": True})
    for scan_id in range(1, 101):
        timelines.append(rung_index=0, scan_id=scan_id, writes=pattern)

    assert timelines.mode(0) == "cycle"
    assert timelines.intern_size(0) == 1
    timeline = timelines._timelines[0]
    assert len(timeline) == 1
    (range_,) = timeline
    assert range_.start_scan_id == 1
    assert range_.end_scan_id == 100
    assert isinstance(range_.payload, PatternRef)
    assert range_.payload.pattern == pattern


def test_pattern_cycle_interning() -> None:
    """Three stable runs of A, B, A produce three ranges and two canonical PMaps."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"Signal": True})
    pat_b = pmap({"Signal": False})
    # Run A for scans 1-10, B for 11-20, A for 21-30.
    for scan_id in range(1, 11):
        timelines.append(0, scan_id, pat_a)
    for scan_id in range(11, 21):
        timelines.append(0, scan_id, pat_b)
    for scan_id in range(21, 31):
        timelines.append(0, scan_id, pat_a)

    timeline = timelines._timelines[0]
    assert len(timeline) == 3
    assert timelines.intern_size(0) == 2

    # Same PMap instance reused for both A ranges.
    a_payload_1 = timeline[0].payload
    a_payload_3 = timeline[2].payload
    assert isinstance(a_payload_1, PatternRef)
    assert isinstance(a_payload_3, PatternRef)
    assert a_payload_1.pattern is a_payload_3.pattern


def test_alternating_run_detection() -> None:
    """A,B,A,B,... for 1000 scans collapses to one AlternatingRun range."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"Clock": True})
    pat_b = pmap({"Clock": False})
    for scan_id in range(1, 1001):
        pattern = pat_a if scan_id % 2 == 1 else pat_b
        timelines.append(0, scan_id, pattern)

    timeline = timelines._timelines[0]
    assert len(timeline) == 1
    range_ = timeline[0]
    assert range_.start_scan_id == 1
    assert range_.end_scan_id == 1000
    assert isinstance(range_.payload, AlternatingRun)
    # Anchor at scan 1 (odd anchor).  Parity-0 is anchor's pattern (A),
    # parity-1 is the other (B).
    assert range_.payload.pattern_on_even == pat_a
    assert range_.payload.pattern_on_odd == pat_b


def test_alternating_run_parity_relative_to_start() -> None:
    """Parity is anchored at the run's start, not at scan_id itself.

    Guards the off-by-one trap from design doc §"Parity relative to
    start": a run that begins at an odd scan_id would invert the
    even/odd slots under a naive ``scan_id % 2`` lookup.
    """
    timelines = RungFiringTimelines()
    pat_a = pmap({"X": 1})
    pat_b = pmap({"X": 2})

    # Start the run at scan 7 (odd anchor).  Pattern A on the anchor,
    # B next, A, B, ...
    for offset in range(20):
        scan_id = 7 + offset
        pattern = pat_a if offset % 2 == 0 else pat_b
        timelines.append(0, scan_id, pattern)

    timeline = timelines._timelines[0]
    assert len(timeline) == 1
    range_ = timeline[0]
    assert isinstance(range_.payload, AlternatingRun)
    # Anchor at scan 7 holds pat_a in the "even" slot (parity relative
    # to start).  Under naive scan_id % 2 this would be swapped.
    assert range_.payload.pattern_on_even == pat_a
    assert range_.payload.pattern_on_odd == pat_b

    # Lookup uses the same parity rule.
    assert timelines.at(7) == pmap({0: pat_a})
    assert timelines.at(8) == pmap({0: pat_b})
    assert timelines.at(9) == pmap({0: pat_a})
    assert timelines.at(26) == pmap({0: pat_b})  # offset 19 -> odd -> pat_b


def test_alternating_run_breaks_on_third_pattern() -> None:
    """A,B,A,B,C closes the alternation at the last B; C starts a new range."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"X": "a"})
    pat_b = pmap({"X": "b"})
    pat_c = pmap({"X": "c"})

    timelines.append(0, 1, pat_a)
    timelines.append(0, 2, pat_b)
    timelines.append(0, 3, pat_a)
    timelines.append(0, 4, pat_b)
    timelines.append(0, 5, pat_c)

    timeline = timelines._timelines[0]
    # The A,B,A collapse at scan 3 produced one AlternatingRun covering
    # 1-3.  Scan 4 matched the expected parity slot (B) and extended
    # it to 1-4.  Scan 5 broke the pattern and started a PatternRef.
    assert len(timeline) == 2
    first, second = timeline
    assert isinstance(first.payload, AlternatingRun)
    assert first.start_scan_id == 1
    assert first.end_scan_id == 4
    assert isinstance(second.payload, PatternRef)
    assert second.start_scan_id == 5
    assert second.end_scan_id == 5
    assert second.payload.pattern == pat_c


def test_alternating_run_breaks_on_repeat() -> None:
    """A,B,A,A closes the alternation early when A repeats out of turn."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"X": "a"})
    pat_b = pmap({"X": "b"})

    timelines.append(0, 1, pat_a)
    timelines.append(0, 2, pat_b)
    timelines.append(0, 3, pat_a)
    # Expected at scan 4: pat_b (per alternation).  Instead we get
    # pat_a again — should close the run and start a new PatternRef.
    timelines.append(0, 4, pat_a)

    timeline = timelines._timelines[0]
    assert len(timeline) == 2
    first, second = timeline
    assert isinstance(first.payload, AlternatingRun)
    assert first.start_scan_id == 1
    assert first.end_scan_id == 3
    assert isinstance(second.payload, PatternRef)
    assert second.start_scan_id == 4
    assert second.end_scan_id == 4


def test_cycle_to_fired_only_transition() -> None:
    """Intern pool hitting the threshold flips the rung to fired-only permanently."""
    timelines = RungFiringTimelines()
    # Feed _FIRED_ONLY_THRESHOLD distinct patterns, one per scan.
    for scan_id in range(1, _FIRED_ONLY_THRESHOLD + 1):
        timelines.append(0, scan_id, pmap({"Counter": scan_id}))

    assert timelines.mode(0) == "fired_only"
    assert timelines.intern_size(0) == 0  # pool dropped at promotion

    # Next append lands as a FiredOnly range.
    next_scan = _FIRED_ONLY_THRESHOLD + 1
    timelines.append(0, next_scan, pmap({"Counter": next_scan}))
    timeline = timelines._timelines[0]
    last = timeline[-1]
    assert isinstance(last.payload, FiredOnly)
    assert last.start_scan_id == next_scan
    assert last.end_scan_id == next_scan


def test_fired_only_transition_is_one_way() -> None:
    """Once promoted, even 100 identical patterns don't revert to cycle mode."""
    timelines = RungFiringTimelines()
    for scan_id in range(1, _FIRED_ONLY_THRESHOLD + 1):
        timelines.append(0, scan_id, pmap({"C": scan_id}))
    assert timelines.mode(0) == "fired_only"

    stable = pmap({"C": 999})
    for offset in range(100):
        timelines.append(0, _FIRED_ONLY_THRESHOLD + 1 + offset, stable)

    assert timelines.mode(0) == "fired_only"
    assert timelines.intern_size(0) == 0
    # The 100 identical fired-only appends collapsed into one FiredOnly range.
    tail = timelines._timelines[0][-1]
    assert isinstance(tail.payload, FiredOnly)
    assert tail.end_scan_id == _FIRED_ONLY_THRESHOLD + 100


def test_fired_only_lookup_returns_sentinel_pmap() -> None:
    """FiredOnly lookup returns a PMap keyed by observed-tag names."""
    timelines = RungFiringTimelines()
    # Train with enough distinct patterns across varied tag sets to
    # ensure the observed-tag union at promotion includes A and B.
    for scan_id in range(1, _FIRED_ONLY_THRESHOLD + 1):
        # Alternate between two tag name shapes so both appear.
        if scan_id % 2 == 0:
            pattern = pmap({"A": scan_id})
        else:
            pattern = pmap({"B": scan_id})
        timelines.append(0, scan_id, pattern)
    assert timelines.mode(0) == "fired_only"

    # Append a fired-only scan and look it up.
    timelines.append(0, _FIRED_ONLY_THRESHOLD + 1, pmap({"A": 42}))
    firings = timelines.at(_FIRED_ONLY_THRESHOLD + 1)
    assert set(firings[0].keys()) == {"A", "B"}


def test_rung_firings_lookup_matches_prior_dict_shape() -> None:
    """The public API's shape is stable: PMap[int, PMap[str, Any]]."""
    Enable = Bool("Enable")
    Signal = Bool("Signal")

    with Program() as logic:
        with Rung(Enable):
            out(Signal)
        with Rung(Signal):
            out(Bool("Downstream"))

    runner = PLC(logic, record_all_tags=True)
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    # Outer PMap of rung_index -> inner PMap of tag_name -> value.
    assert isinstance(firings, type(pmap()))
    assert 0 in firings
    assert isinstance(firings[0], type(pmap()))
    assert firings[0]["Signal"] is True


def test_timelines_reset_clears_state() -> None:
    """reset() drops timelines, intern pools, mode, and fired-only caches."""
    timelines = RungFiringTimelines()
    timelines.append(0, 1, pmap({"X": 1}))
    for scan_id in range(2, _FIRED_ONLY_THRESHOLD + 2):
        timelines.append(1, scan_id, pmap({"Y": scan_id}))
    assert timelines.mode(1) == "fired_only"

    timelines.reset()

    assert timelines.mode(0) == "cycle"
    assert timelines.mode(1) == "cycle"
    assert timelines.intern_size(0) == 0
    assert timelines.intern_size(1) == 0
    assert timelines.ever_fired() == set()


def test_plc_reboot_resets_timelines() -> None:
    """PLC.reboot() flushes firing timelines together with the scan log."""
    Enable = Bool("Enable")
    Counter = Int("Counter")

    with Program() as logic:
        with Rung(Enable):
            out(Counter)

    runner = PLC(logic, record_all_tags=True)
    runner.patch({"Enable": True})
    runner.step()
    assert runner._rung_firing_timelines.ever_fired() == {0}

    runner.reboot()
    # Post-reboot: timelines cleared, nothing has fired yet.
    assert runner._rung_firing_timelines.ever_fired() == set()
    runner.patch({"Enable": True})
    runner.step()
    # Firing resumes in fresh storage.
    assert runner._rung_firing_timelines.ever_fired() == {0}


def test_gap_in_firings_starts_new_range() -> None:
    """Non-adjacent append of the same pattern starts a new range.

    A rung that fires on scans 1-5, skips 6, fires scan 7 with the
    same pattern should produce two ranges — ``end + 1 == scan_id``
    is the adjacency condition for range extension.
    """
    timelines = RungFiringTimelines()
    pat = pmap({"X": True})
    for scan_id in range(1, 6):
        timelines.append(0, scan_id, pat)
    # Skip scan 6.
    timelines.append(0, 7, pat)

    timeline = timelines._timelines[0]
    assert len(timeline) == 2
    assert timeline[0].start_scan_id == 1 and timeline[0].end_scan_id == 5
    assert timeline[1].start_scan_id == 7 and timeline[1].end_scan_id == 7
    # Same canonical pattern, though.
    assert timelines.intern_size(0) == 1
    assert (
        timeline[0].payload.pattern  # type: ignore[union-attr]
        is timeline[1].payload.pattern  # type: ignore[union-attr]
    )


def test_ever_fired_and_fired_on_queries() -> None:
    """Efficient ``ever_fired`` / ``fired_on`` for query.cold/hot_rungs."""
    timelines = RungFiringTimelines()
    timelines.append(0, 1, pmap({"A": 1}))
    timelines.append(0, 2, pmap({"A": 2}))
    timelines.append(2, 1, pmap({"B": 1}))

    assert timelines.ever_fired() == {0, 2}
    assert timelines.fired_on(1) == {0, 2}
    assert timelines.fired_on(2) == {0}
    assert timelines.fired_on(3) == set()


# ---------------------------------------------------------------------------
# Sweep-on-log-trim eviction
# ---------------------------------------------------------------------------


def test_trim_before_drops_ranges_entirely_past() -> None:
    """Ranges with end_scan_id < N are removed; later ranges stay."""
    timelines = RungFiringTimelines()
    pat = pmap({"X": True})
    for scan_id in range(1, 6):
        timelines.append(0, scan_id, pat)
    # Gap at scan 6 -> new range after trim horizon.
    for scan_id in range(7, 11):
        timelines.append(0, scan_id, pat)

    # Before trim: two ranges (1-5) and (7-10).
    assert len(timelines._timelines[0]) == 2

    timelines.trim_before(7)
    # First range (ends at 5) is fully past the horizon and dropped.
    remaining = timelines._timelines[0]
    assert len(remaining) == 1
    assert remaining[0].start_scan_id == 7
    assert remaining[0].end_scan_id == 10


def test_trim_before_advances_straddling_range_start() -> None:
    """A range straddling N has its start_scan_id pulled forward to N."""
    timelines = RungFiringTimelines()
    pat = pmap({"X": True})
    for scan_id in range(1, 11):
        timelines.append(0, scan_id, pat)

    timelines.trim_before(4)
    (range_,) = timelines._timelines[0]
    assert range_.start_scan_id == 4
    assert range_.end_scan_id == 10


def test_sweep_on_log_trim_preserves_alternating_parity() -> None:
    """Advancing AlternatingRun's start by an odd delta swaps even/odd slots.

    Guards design doc §"Eviction: sweep on log-trim" — parity is
    anchored at the current ``start_scan_id``, so trimming shifts the
    anchor and the per-slot patterns must swap when the delta is odd.
    """
    timelines = RungFiringTimelines()
    pat_a = pmap({"C": "a"})
    pat_b = pmap({"C": "b"})
    # Build an alternating run from scan 10 to scan 1000 with
    # pattern_on_even=A (anchor-parity 0 at scan 10) and
    # pattern_on_odd=B.
    for offset in range(10, 1001):
        scan_id = offset
        pattern = pat_a if (scan_id - 10) % 2 == 0 else pat_b
        timelines.append(0, scan_id, pattern)
    (before_trim,) = timelines._timelines[0]
    assert isinstance(before_trim.payload, AlternatingRun)
    assert before_trim.payload.pattern_on_even == pat_a
    assert before_trim.payload.pattern_on_odd == pat_b

    # Trim to N=17.  Delta = 17-10 = 7 (odd) -> slots must swap.
    timelines.trim_before(17)
    (after_trim,) = timelines._timelines[0]
    assert isinstance(after_trim.payload, AlternatingRun)
    assert after_trim.start_scan_id == 17
    assert after_trim.end_scan_id == 1000
    assert after_trim.payload.pattern_on_even == pat_b
    assert after_trim.payload.pattern_on_odd == pat_a
    # Lookup at scan 17 must return the same pattern as before the
    # trim.  Under the old anchor (scan 10), scan 17 had parity
    # (17-10)%2 = 1 → pat_b.  Under the new anchor (scan 17), parity
    # (17-17)%2 = 0 → pattern_on_even (which was swapped to pat_b).
    # The two answers agree — that's the whole point of the swap.
    assert timelines.at(17) == pmap({0: pat_b})


def test_sweep_on_log_trim_alternating_even_delta_preserves_slots() -> None:
    """Even-delta trim leaves pattern_on_even / pattern_on_odd as-is."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"C": 1})
    pat_b = pmap({"C": 2})
    for offset in range(10, 100):
        scan_id = offset
        pattern = pat_a if (scan_id - 10) % 2 == 0 else pat_b
        timelines.append(0, scan_id, pattern)

    # Delta = 18 - 10 = 8 (even) -> no swap.
    timelines.trim_before(18)
    (after_trim,) = timelines._timelines[0]
    assert isinstance(after_trim.payload, AlternatingRun)
    assert after_trim.payload.pattern_on_even == pat_a
    assert after_trim.payload.pattern_on_odd == pat_b


def test_sweep_drops_unreferenced_intern_patterns() -> None:
    """After trim, intern pool keeps only patterns still referenced."""
    timelines = RungFiringTimelines()
    pat_a = pmap({"T": "a"})
    pat_b = pmap({"T": "b"})
    # Two stable runs: A (scans 1-5), B (scans 6-10).  Intern pool
    # holds both.
    for scan_id in range(1, 6):
        timelines.append(0, scan_id, pat_a)
    for scan_id in range(6, 11):
        timelines.append(0, scan_id, pat_b)
    assert timelines.intern_size(0) == 2

    # Trim past the A range.  Only B survives; intern pool shrinks.
    timelines.trim_before(6)
    assert timelines.intern_size(0) == 1
    survivors = set(timelines._intern[0])
    assert pat_b in survivors
    assert pat_a not in survivors


def test_trim_fully_empty_rung_resets_rung_state() -> None:
    """A rung whose timeline is fully past N reverts to a fresh state."""
    timelines = RungFiringTimelines()
    pat = pmap({"X": 1})
    for scan_id in range(1, 11):
        timelines.append(0, scan_id, pat)

    # Trim past every range -> rung is effectively unseen again.
    timelines.trim_before(20)
    assert 0 not in timelines._timelines
    assert 0 not in timelines._intern
    assert timelines.mode(0) == "cycle"
    assert timelines.ever_fired() == set()


def test_trim_preserves_fired_only_sentinel() -> None:
    """Trimming a fired-only rung keeps the sentinel and FiredOnly ranges."""
    timelines = RungFiringTimelines()
    # Promote the rung to fired-only.
    for scan_id in range(1, _FIRED_ONLY_THRESHOLD + 1):
        timelines.append(0, scan_id, pmap({"N": scan_id}))
    for scan_id in range(_FIRED_ONLY_THRESHOLD + 1, _FIRED_ONLY_THRESHOLD + 50):
        timelines.append(0, scan_id, pmap({"N": scan_id}))
    assert timelines.mode(0) == "fired_only"
    pre_sentinel_keys = set(timelines._fired_only_writes[0].keys())

    # Trim in the middle of the fired-only tail range.
    trim_to = _FIRED_ONLY_THRESHOLD + 10
    timelines.trim_before(trim_to)
    assert timelines.mode(0) == "fired_only"
    # Sentinel PMap survives unchanged.
    assert set(timelines._fired_only_writes[0].keys()) == pre_sentinel_keys


def test_plc_trim_history_before_trims_firings() -> None:
    """_trim_history_before trims rung-firing timelines in lockstep."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            out(Light)

    runner = PLC(logic, record_all_tags=True, checkpoint_interval=10)
    runner.patch({"Enable": True})
    for _ in range(5):
        runner.step()
    # No-op: horizon below any recorded scan.
    runner._trim_history_before(0)
    assert runner._rung_firing_timelines.ever_fired() == {0}

    # Trim to midway — first half of the range drops.
    runner._trim_history_before(3)
    remaining = runner._rung_firing_timelines._timelines[0]
    assert remaining[0].start_scan_id == 3
