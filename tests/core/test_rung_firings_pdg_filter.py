"""Tests for PDG-filtered rung-firing capture.

The firing log exists to serve the simulator's own analysis APIs
(``cause`` / ``effect`` / ``query``).  Writes to tags no rung reads
are dropped at capture time — by definition no analysis question can
depend on them.  ``record_all_tags=True`` bypasses the filter for
diagnostic sessions that suspect the PDG is wrong.

Covers:
- Unconsumed writes are dropped, consumed writes are kept.
- The escape-hatch flag bypasses the filter.
- Rung-fired status is preserved even when every write was filtered
  (the rung index still appears in the firing log with an empty
  inner map, so ``cause``'s PDG fallback and ``query.hot_rungs`` /
  ``query.cold_rungs`` still see the rung).
- Reboot doesn't invalidate the PDG consumed-tag set (rung list is
  immutable across reboots).
- Non-standard rungs (tests that implement only ``evaluate(ctx)``)
  silently bypass the filter — the PDG can't model them, and the
  escape hatch handles them gracefully.
"""

from __future__ import annotations

from pyrung.core import PLC, Bool, Program, Rung, out


def test_pdg_filter_drops_unconsumed_writes() -> None:
    """A write to a tag no rung reads is dropped from the firing log."""
    Enable = Bool("Enable")
    Unused = Bool("Unused")  # written but never read

    with Program() as logic:
        with Rung(Enable):
            out(Unused)

    runner = PLC(logic)  # record_all_tags=False by default
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    # Rung 0 fired (empty writes is fine), but Unused should NOT be recorded.
    assert 0 in firings, "rung fired status should be preserved"
    assert "Unused" not in firings[0], "unconsumed write should be filtered out"
    # The rung's inner map is empty under the filter.
    assert len(firings[0]) == 0
    # The tag still got its value — filter only affects the firing log.
    assert runner.current_state.tags["Unused"] is True


def test_pdg_filter_keeps_consumed_writes() -> None:
    """A write to a tag read by some rung stays in the firing log."""
    Enable = Bool("Enable")
    Signal = Bool("Signal")
    Unused = Bool("Unused")

    with Program() as logic:
        with Rung(Enable):
            out(Signal)
            out(Unused)
        with Rung(Signal):  # makes Signal consumed
            out(Bool("Downstream"))

    runner = PLC(logic)
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    # Rung 0: Signal is consumed (Rung 1 reads it), Unused is not.
    assert firings[0]["Signal"] is True
    assert "Unused" not in firings[0]


def test_record_all_tags_bypasses_pdg_filter() -> None:
    """record_all_tags=True preserves every write including unconsumed ones."""
    Enable = Bool("Enable")
    Unused = Bool("Unused")

    with Program() as logic:
        with Rung(Enable):
            out(Unused)

    runner = PLC(logic, record_all_tags=True)
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    # Filter bypassed — Unused must appear.
    assert firings[0]["Unused"] is True


def test_pdg_filter_matches_unfiltered_on_consumed_writes() -> None:
    """Filtered and unfiltered runs agree exactly on the consumed subset."""
    Enable = Bool("Enable")
    Signal = Bool("Signal")
    Unused = Bool("Unused")

    def build() -> object:
        with Program() as logic:
            with Rung(Enable):
                out(Signal)
                out(Unused)
            with Rung(Signal):
                out(Bool("Downstream"))
        return logic

    filtered = PLC(build())
    unfiltered = PLC(build(), record_all_tags=True)

    filtered.patch({"Enable": True})
    filtered.step()
    unfiltered.patch({"Enable": True})
    unfiltered.step()

    f_firings = filtered.rung_firings()
    u_firings = unfiltered.rung_firings()

    # Same rungs fired in both runs.
    assert set(f_firings.keys()) == set(u_firings.keys())
    # Unfiltered has the extra unconsumed write; filtered does not.
    assert "Unused" in u_firings[0]
    assert "Unused" not in f_firings[0]
    # Every other write matches exactly.
    for rung_idx in f_firings:
        for tag_name, value in f_firings[rung_idx].items():
            assert u_firings[rung_idx][tag_name] == value


def test_pdg_filter_preserves_fired_status_when_all_writes_filtered() -> None:
    """A rung whose only writes were filtered still registers as fired.

    ``query.cold_rungs`` / ``hot_rungs`` and ``cause()``'s PDG fallback
    both need to distinguish "didn't fire" from "fired but filtered."
    The rung index must remain in the firing log (with empty inner map)
    to carry that signal.
    """
    Enable = Bool("Enable")
    TerminalA = Bool("TerminalA")
    TerminalB = Bool("TerminalB")

    with Program() as logic:
        with Rung(Enable):
            out(TerminalA)
            out(TerminalB)

    runner = PLC(logic)
    runner.patch({"Enable": True})
    runner.step()

    firings = runner.rung_firings()
    assert 0 in firings, "rung should still be listed as fired"
    assert len(firings[0]) == 0, "all writes were filtered out"

    # cause() must still find the rung that wrote TerminalA via the PDG
    # fallback — this test pins the guarantee at the capture layer.
    chain = runner.cause("TerminalA")
    assert chain is not None
    assert len(chain.steps) >= 1
    assert chain.steps[0].rung_index == 0


def test_pdg_filter_consumed_set_survives_reboot() -> None:
    """Reboot doesn't invalidate the consumed-tag cache (rung list is stable)."""
    Enable = Bool("Enable")
    Signal = Bool("Signal")
    Unused = Bool("Unused")

    with Program() as logic:
        with Rung(Enable):
            out(Signal)
            out(Unused)
        with Rung(Signal):
            out(Bool("Downstream"))

    runner = PLC(logic)
    runner.patch({"Enable": True})
    runner.step()
    # Build PDG cache via the first capture.
    firings_pre = runner.rung_firings()
    assert "Signal" in firings_pre[0]
    assert "Unused" not in firings_pre[0]

    runner.reboot()
    runner.patch({"Enable": True})
    runner.step()
    firings_post = runner.rung_firings()
    # Filter still applied the same way after reboot.
    assert "Signal" in firings_post[0]
    assert "Unused" not in firings_post[0]


def test_pdg_filter_bypassed_for_non_standard_rungs() -> None:
    """Programs with synthetic (non-Rung) entries bypass the filter cleanly."""

    class _RawRung:
        def evaluate(self, ctx) -> None:  # noqa: ANN001
            ctx.set_tag("Raw", 42)

    runner = PLC(logic=[_RawRung()])
    runner.step()
    # The PDG can't model _RawRung; filter silently bypasses.  Capture
    # path still records the write — same behavior as record_all_tags.
    firings = runner.rung_firings()
    # Rung 0 fired: wrote Raw.  Since filter was bypassed, Raw is present.
    assert firings[0]["Raw"] == 42


def test_pdg_filter_default_off_for_logic_less_plc() -> None:
    """PLC with no logic: filter returns None, capture proceeds unfiltered."""
    runner = PLC()  # empty logic, record_all_tags=False (default)
    runner.patch({"Some_Tag": True})
    runner.step()
    # No rungs, no firings — regardless of filter state.
    assert runner.rung_firings() == runner.rung_firings()
    assert len(runner.rung_firings()) == 0


def test_pdg_filter_preserved_across_fork() -> None:
    """fork() propagates the record_all_tags flag."""
    Enable = Bool("Enable")
    Unused = Bool("Unused")

    with Program() as logic:
        with Rung(Enable):
            out(Unused)

    parent = PLC(logic, record_all_tags=True)
    parent.patch({"Enable": True})
    parent.step()
    child = parent.fork()
    child.patch({"Enable": True})
    child.step()

    # Both keep Unused because record_all_tags was inherited.
    assert "Unused" in parent.rung_firings()[0]
    assert "Unused" in child.rung_firings()[0]

    parent_filtered = PLC(logic)  # default: filter on
    parent_filtered.patch({"Enable": True})
    parent_filtered.step()
    child_filtered = parent_filtered.fork()
    child_filtered.patch({"Enable": True})
    child_filtered.step()
    assert "Unused" not in child_filtered.rung_firings()[0]


def test_pdg_filter_drops_counter_accumulator_pattern() -> None:
    """The canonical memory-win case: internal counter writes are dropped.

    A timer/counter instruction writes ``.acc`` on every scan while
    enabled.  When no rung consumes the accumulator, PDG filtering
    drops the write at capture time — this is the reason the filter
    exists (design doc §"PDG-filtered capture").
    """
    from pyrung.core import Timer, on_delay

    Trigger = Bool("Trigger")

    with Program() as logic:
        with Rung(Trigger):
            on_delay(Timer[1], preset=100)
        # Timer[1].Done is consumed here, Timer[1].Acc is not.
        with Rung(Timer[1].Done):
            out(Bool("Alert"))

    runner = PLC(logic, dt=0.010)
    runner.patch({"Trigger": True})
    runner.step()
    runner.step()

    firings_1 = runner.rung_firings(scan_id=1)
    firings_2 = runner.rung_firings(scan_id=2)
    # Rung 0 fired both scans.  Timer[1].Done is consumed → in firings.
    # Timer[1].Acc is NOT consumed → filtered out.
    assert 0 in firings_1
    assert 0 in firings_2
    assert "Timer_Acc" not in firings_1[0], "unconsumed timer acc should be filtered"
    assert "Timer_Acc" not in firings_2[0], "unconsumed timer acc should be filtered"
    assert "Timer_Done" in firings_1[0], "consumed timer done should be kept"

    # With record_all_tags=True, Timer_Acc writes are preserved.
    unfiltered = PLC(logic, dt=0.010, record_all_tags=True)
    unfiltered.patch({"Trigger": True})
    unfiltered.step()
    unfiltered.step()
    assert "Timer_Acc" in unfiltered.rung_firings(scan_id=1)[0]
    assert "Timer_Acc" in unfiltered.rung_firings(scan_id=2)[0]
