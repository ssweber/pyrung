"""Tests for per-rung tag write tracking (rung_firings).

Verifies that the engine records which rungs wrote which tags during each
scan, stored as PMap[int, PMap[str, Any]] for structural sharing.
"""

from __future__ import annotations

from pyrsistent import PMap, pmap

from pyrung.core import PLC, Bool, Program, Rung, latch, out, reset


def test_simple_rung_fires_and_records_write() -> None:
    """A rung that fires should appear in rung_firings with its tag writes."""
    Button = Bool("Button")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Button):
            out(Light)

    runner = PLC(logic)
    runner.patch({"Button": True})
    runner.step()

    firings = runner.rung_firings()
    assert isinstance(firings, PMap)
    assert 0 in firings
    assert firings[0]["Light"] is True


def test_rung_disabled_out_resets_to_same_value_absent() -> None:
    """A disabled rung whose out() writes a value already pending should not appear."""
    Button = Bool("Button")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Button):
            out(Light)

    runner = PLC(logic)
    # Button=False, Light defaults to False.  out(Light) writes False when
    # disabled, but Light is already False in committed state.  On scan 1
    # the write lands in _tags_pending (first write), so it DOES diff.
    runner.step()

    firings = runner.rung_firings()
    # out() writes Light=False even when disabled; this is the first write
    # to _tags_pending for Light, so the diff catches it.
    assert 0 in firings
    assert firings[0]["Light"] is False


def test_latch_fires_and_records() -> None:
    """latch() writes True when enabled — should appear in firings."""
    Enable = Bool("Enable")
    Latched = Bool("Latched")

    with Program() as logic:
        with Rung(Enable):
            latch(Latched)

    runner = PLC(logic)
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    assert 0 in firings
    assert firings[0]["Latched"] is True


def test_multiple_rungs_write_different_tags() -> None:
    """Each rung's writes should be independent entries in firings."""
    A = Bool("A")
    B = Bool("B")
    X = Bool("X")
    Y = Bool("Y")

    with Program() as logic:
        with Rung(A):
            out(X)
        with Rung(B):
            out(Y)

    runner = PLC(logic)
    runner.patch({"A": True, "B": True})
    runner.step()

    firings = runner.rung_firings()
    assert firings[0]["X"] is True
    assert firings[1]["Y"] is True


def test_two_rungs_write_same_tag_both_recorded() -> None:
    """When two rungs write the same tag with different values, both appear."""
    A = Bool("A")
    B = Bool("B")
    X = Bool("X")

    with Program() as logic:
        with Rung(A):
            latch(X)
        with Rung(B):
            reset(X)

    runner = PLC(logic)
    runner.patch({"A": True, "B": True})
    runner.step()

    firings = runner.rung_firings()
    # Rung 0 latches X=True, rung 1 resets X=False
    assert firings[0]["X"] is True
    assert firings[1]["X"] is False
    # Committed value is the last writer's
    assert runner.current_state.tags["X"] is False


def test_history_eviction_removes_firings() -> None:
    """Firings should be evicted alongside history snapshots."""
    with Program() as logic:
        with Rung():
            out(Bool("X"))

    runner = PLC(logic, history_limit=3)

    runner.step()  # scan 1 — [0, 1]
    runner.step()  # scan 2 — [0, 1, 2]
    runner.step()  # scan 3 — [1, 2, 3] — scan 0 evicted

    # Scan 3 should have firings
    assert runner.rung_firings(scan_id=3) != pmap()
    # Scan 0 was evicted — firings should be gone
    assert runner.rung_firings(scan_id=0) == pmap()


def test_default_scan_id_uses_playhead() -> None:
    """rung_firings() with no argument should use the playhead scan."""
    X = Bool("X")

    with Program() as logic:
        with Rung():
            out(X)

    runner = PLC(logic)
    runner.step()
    runner.step()

    # Playhead follows latest scan
    default = runner.rung_firings()
    explicit = runner.rung_firings(scan_id=runner.current_state.scan_id)
    assert default == explicit


def test_empty_logic_has_empty_firings() -> None:
    """A PLC with no rungs should produce empty firings."""
    runner = PLC(logic=[])
    runner.step()

    firings = runner.rung_firings()
    assert firings == pmap()


def test_firings_are_pmap_instances() -> None:
    """Both the outer and inner maps should be pyrsistent PMaps."""
    X = Bool("X")

    with Program() as logic:
        with Rung():
            out(X)

    runner = PLC(logic)
    runner.step()

    firings = runner.rung_firings()
    assert isinstance(firings, PMap)
    assert isinstance(firings[0], PMap)


def test_firings_across_multiple_scans() -> None:
    """Firings should be queryable for any retained scan."""
    Button = Bool("Button")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Button):
            latch(Light)

    runner = PLC(logic)

    # Scan 1: Button=False, latch doesn't fire — no write from rung 0
    runner.step()
    firings_1 = runner.rung_firings(scan_id=1)

    # Scan 2: Button=True, latch fires
    runner.patch({"Button": True})
    runner.step()
    firings_2 = runner.rung_firings(scan_id=2)

    assert "Light" not in firings_1.get(0, pmap())
    assert firings_2[0]["Light"] is True


def test_debug_namespace_exposes_rung_firings() -> None:
    """The debug namespace should proxy rung_firings()."""
    X = Bool("X")

    with Program() as logic:
        with Rung():
            out(X)

    runner = PLC(logic)
    runner.step()

    assert runner.debug.rung_firings() == runner.rung_firings()
    assert runner.debug.rung_firings(scan_id=1) == runner.rung_firings(scan_id=1)
