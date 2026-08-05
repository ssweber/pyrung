"""Tests for the independent firing, final-value, and varied columns."""

from pyrsistent import pmap

from pyrung.core.context import RungId
from pyrung.core.rung_firings import (
    _VALUE_VARIETY_THRESHOLD,
    Alternating,
    Arithmetic,
    Constant,
    RungFiringTimelines,
    Unknown,
)


def test_stable_values_and_firing_ranges_are_independent() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 101):
        timelines.append(0, scan_id, {"A": True, "B": 7})

    assert len(timelines._fired_ranges[0]) == 1
    assert len(timelines._value_timelines[(0, "A")]) == 1
    assert len(timelines._value_timelines[(0, "B")]) == 1
    assert isinstance(timelines._value_timelines[(0, "A")][0].payload, Constant)
    assert timelines.at(50) == pmap({0: pmap({"A": True, "B": 7})})


def test_empty_write_firing_is_still_recorded() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(2, 4, {})

    assert timelines.fired_on(4) == {2}
    assert timelines.rung_writes_at(2, 4) == pmap()
    assert timelines.rung_writes_at(2, 3) is None


def test_columns_compress_different_shapes_independently() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 101):
        timelines.append(
            0,
            scan_id,
            {
                "constant": 9,
                "alternating": scan_id % 2,
                "arithmetic": 10 + 3 * scan_id,
            },
        )

    assert isinstance(timelines._value_timelines[(0, "constant")][0].payload, Constant)
    assert isinstance(timelines._value_timelines[(0, "alternating")][0].payload, Alternating)
    assert isinstance(timelines._value_timelines[(0, "arithmetic")][0].payload, Arithmetic)
    assert timelines.rung_writes_at(0, 80) == pmap(
        {"constant": 9, "alternating": 0, "arithmetic": 250}
    )


def test_firing_gap_breaks_value_range_without_inventing_a_write() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 1, {"A": True})
    timelines.append(0, 3, {"A": True})

    assert len(timelines._fired_ranges[0]) == 2
    assert len(timelines._value_timelines[(0, "A")]) == 2
    assert timelines.rung_writes_at(0, 2) is None


def test_alternating_write_and_missing_compresses_without_inventing_writes() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 101):
        timelines.append(0, scan_id, {"A": True} if scan_id % 2 else {})

    (range_,) = timelines._value_timelines[(0, "A")]
    assert isinstance(range_.payload, Alternating)
    assert timelines.rung_writes_at(0, 1) == pmap({"A": True})
    assert timelines.rung_writes_at(0, 2) == pmap()
    assert timelines.write_scans(frozenset({0}), "A", (1, 2, 3, 4)) == (1, 3)
    assert timelines.last_tag_write_before(frozenset({0}), "A", 5) == (3, True)


def test_missing_only_interval_is_not_an_observed_writer_interval() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 1, {"A": True})
    for scan_id in range(2, 6):
        timelines.append(0, scan_id, {})

    assert timelines.observed_writers_of_between("A", 1, 1) == frozenset({0})
    assert timelines.observed_writers_of_between("A", 2, 5) == frozenset()
    assert not timelines.any_wrote_on("A", 4)


def test_varied_is_a_sparse_sticky_true_fact_per_scope_and_tag() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 1, {"A": False, "B": 1}, {"A"})
    timelines.append(0, 2, {"A": False, "B": 1})
    timelines.append(0, 3, {"A": True, "B": 1}, {"A"})

    assert timelines.varied_on(0, "A", 1)
    assert not timelines.varied_on(0, "A", 2)
    assert timelines.varied_on(0, "A", 3)
    assert not timelines.varied_on(0, "B", 1)
    assert len(timelines._varied_ranges[(0, "A")]) == 2
    assert timelines.varied_scans(frozenset({0}), "A", (1, 2, 3)) == (1, 3)


def test_unknown_degrades_only_one_high_complexity_column() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, _VALUE_VARIETY_THRESHOLD + 2):
        timelines.append(0, scan_id, {"noisy": scan_id * scan_id, "stable": True})

    assert isinstance(timelines._value_timelines[(0, "noisy")][-1].payload, Unknown)
    assert isinstance(timelines._value_timelines[(0, "stable")][0].payload, Constant)
    assert timelines.value_at(0, "stable", _VALUE_VARIETY_THRESHOLD + 1) is True
    assert not timelines.value_is_known(0, "noisy")
    assert timelines.value_is_known(0, "stable")

    # Missing firings may compact with unknown values, but are not themselves
    # writes. The unknown side remains a conservative transition candidate.
    timelines.append(0, _VALUE_VARIETY_THRESHOLD + 2, {"stable": True})
    timelines.append(
        0,
        _VALUE_VARIETY_THRESHOLD + 3,
        {"noisy": -1, "stable": True},
    )
    assert timelines.latest_value_transition_scan_at_or_before(
        frozenset({0}),
        "noisy",
        "any possible lost value",
        _VALUE_VARIETY_THRESHOLD + 3,
    ) == (_VALUE_VARIETY_THRESHOLD + 3)


def test_fragmented_low_cardinality_column_stays_exact() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    # Deliberately aperiodic enough to fragment rather than form one
    # Alternating run, but it still has only two actual values.
    for scan_id in range(1, 401):
        value = (scan_id % 5) in {1, 2}
        timelines.append(0, scan_id, {"A": value})

    assert not any(
        isinstance(range_.payload, Unknown) for range_ in timelines._value_timelines[(0, "A")]
    )
    assert timelines.value_at(0, "A", 399) is False
    assert timelines.value_at(0, "A", 400) is False


def test_observed_writer_and_write_scan_indexes_are_columnar() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 1, {"A": True})
    timelines.append(1, 2, {"B": True})
    timelines.append(0, 3, {"A": False})

    assert timelines.observed_writers_of("A") == frozenset({0})
    assert timelines.observed_writers_of_between("A", 2, 3) == frozenset({0})
    assert timelines.observed_writers_of_between("A", 2, 2) == frozenset()
    assert timelines.write_scans(frozenset({0, 1}), "A", (1, 2, 3)) == (1, 3)
    assert timelines.any_wrote_on("B", 2)
    assert not timelines.any_wrote_on("B", 2, excluding=1)


def test_transition_queries_use_scalar_ranges() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id, value in enumerate((10, 12, 14, 16), start=1):
        timelines.append(0, scan_id, {"A": value})

    assert timelines.latest_firing_scan_at_or_before(frozenset({0}), 10) == 4
    assert timelines.latest_value_transition_scan_at_or_before(frozenset({0}), "A", 14, 10) == 3
    assert timelines.last_tag_write_before(frozenset({0}), "A", 4) == (3, 14)
    assert timelines.tag_transition_candidate_scans_before(frozenset({0}), "A", 5) == (4,)


def test_trim_before_rebases_alternating_and_arithmetic_columns() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 8):
        timelines.append(0, scan_id, {"A": scan_id % 2, "N": scan_id * 2})

    timelines.trim_before(4)

    assert timelines.rung_writes_at(0, 3) is None
    assert timelines.rung_writes_at(0, 4) == pmap({"A": 0, "N": 8})
    assert timelines.rung_writes_at(0, 7) == pmap({"A": 1, "N": 14})


def test_snapshot_and_trim_after_do_not_mutate_source() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 7):
        timelines.append(0, scan_id, {"A": scan_id}, {"A"} if scan_id == 4 else set())

    snapshot = timelines.snapshot(up_to=4)
    snapshot.trim_after(3)

    assert snapshot.rung_writes_at(0, 4) is None
    assert timelines.rung_writes_at(0, 6) == pmap({"A": 6})
    assert not snapshot.varied_on(0, "A", 4)
    assert timelines.varied_on(0, "A", 4)


def test_node_keys_share_the_same_index_implementation() -> None:
    node = RungId("Sub", 2)
    timelines: RungFiringTimelines[RungId] = RungFiringTimelines()
    timelines.append(node, 8, {"A": 1}, {"A"})

    assert timelines.fired_on(8) == {node}
    assert timelines.value_at(node, "A", 8) == 1
    assert timelines.varied_on(node, "A", 8)


def test_reset_clears_all_three_fact_families() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 1, {"A": True}, {"A"})

    timelines.reset()

    assert timelines.ever_fired() == set()
    assert timelines.observed_writers_of("A") == frozenset()
    assert not timelines.varied_on(0, "A", 1)
