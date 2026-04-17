"""Tests for PDG-filtered rung-firing capture.

The firing log exists to serve the simulator's own analysis APIs
(``cause`` / ``effect`` / ``query``).  Writes to non-Bool tags that
no rung reads are dropped at capture time — by definition no analysis
question can depend on them.  **Bool tags are always kept**,
regardless of whether any rung reads them: they're low-cardinality,
usually represent user-facing state transitions, and are the common
target of ``cause()`` queries.  ``record_all_tags=True`` bypasses
the filter entirely for diagnostic sessions that suspect the PDG is
wrong.

Covers:
- Unconsumed non-Bool writes are dropped.
- Unconsumed Bool writes are kept (the refinement that keeps
  ``cause()`` on terminal Bool outputs on the direct-log path).
- Consumed writes of any type are kept.
- The escape-hatch flag bypasses the filter.
- Mixed rungs (Bool + non-Bool writes on the same rung): the Bool
  write survives, the non-Bool write drops.  This keeps the rung's
  intern pool small so causal chains on the Bool stay intact even
  when the non-Bool side is monotonic.
- Rung-fired status is preserved even when every write was filtered
  (the rung index still appears in the firing log with an empty
  inner map, so ``cause``'s PDG fallback and ``query.hot_rungs`` /
  ``query.cold_rungs`` still see the rung).
- Reboot doesn't invalidate the PDG consumed-tag set (rung list is
  immutable across reboots).
- Non-standard rungs (tests that implement only ``evaluate(ctx)``)
  silently bypass the filter.
"""

from __future__ import annotations

from pyrung.core import PLC, Bool, Int, Program, Rung, copy, out


def test_pdg_filter_drops_unconsumed_non_bool_writes() -> None:
    """A write to a non-Bool tag no rung reads is dropped from the log."""
    Enable = Bool("Enable")
    Source = Int("Source")
    Sink = Int("Sink")  # written but never read

    with Program() as logic:
        with Rung(Enable):
            copy(Source, Sink)

    runner = PLC(logic)
    runner.patch({"Enable": True, "Source": 42})
    runner.step()

    firings = runner.rung_firings()
    assert 0 in firings, "rung fired status should be preserved"
    assert "Sink" not in firings[0], "unconsumed non-Bool write should be filtered"
    # The tag still got its value — filter only affects the firing log.
    assert runner.current_state.tags["Sink"] == 42


def test_pdg_filter_keeps_unconsumed_bool_writes() -> None:
    """Bool tags stay in the firing log even when no rung reads them.

    The reverse of ``test_pdg_filter_drops_unconsumed_non_bool_writes``:
    same shape of program (rung writes a terminal tag that no other
    rung reads), but the tag type is Bool.  Bools are low-cardinality
    user-facing state; the filter preserves them so ``cause()`` can
    answer from the direct log path without falling back to SP-tree
    evaluation.

    Variable named ``Unused_But_Preserved`` to make the intent obvious:
    the tag is *unused* by the PDG's ``readers_of``, but the capture
    layer *preserves* it anyway because it's Bool-typed.
    """
    Enable = Bool("Enable")
    Unused_But_Preserved = Bool("Unused_But_Preserved")

    with Program() as logic:
        with Rung(Enable):
            out(Unused_But_Preserved)

    runner = PLC(logic)

    # Toggle the tag across scans so each scan actually writes a
    # distinct value — confirms the preservation isn't an artifact of
    # a single-scan diff.
    runner.patch({"Enable": True})
    runner.step()
    assert runner.rung_firings(scan_id=1)[0]["Unused_But_Preserved"] is True

    runner.patch({"Enable": False})
    runner.step()
    assert runner.rung_firings(scan_id=2)[0]["Unused_But_Preserved"] is False

    runner.patch({"Enable": True})
    runner.step()
    assert runner.rung_firings(scan_id=3)[0]["Unused_But_Preserved"] is True

    # Cross-check: no rung reads the tag.  It's genuinely terminal.
    pdg = runner._ensure_pdg()
    assert "Unused_But_Preserved" not in pdg.readers_of, (
        "test precondition: tag must be unread to exercise the Bool-keep branch of the filter"
    )


def test_pdg_filter_keeps_consumed_writes() -> None:
    """A write to a tag read by some rung stays in the firing log."""
    Enable = Bool("Enable")
    Signal = Bool("Signal")
    Readings = Int("Readings")  # consumed by Rung 1
    Unused = Int("Unused")  # not consumed, not Bool → filtered

    with Program() as logic:
        with Rung(Enable):
            out(Signal)
            copy(Readings, Unused)
        with Rung(Signal):
            pass  # makes Signal consumed

    runner = PLC(logic)
    runner.patch({"Enable": True, "Readings": 7})
    runner.step()

    firings = runner.rung_firings()
    assert firings[0]["Signal"] is True
    assert "Unused" not in firings[0]


def test_pdg_filter_mixed_rung_keeps_bool_drops_int() -> None:
    """A rung with mixed Bool + non-Bool writes keeps the Bool side only.

    This is the refinement's concrete payoff for the
    cycle→fired_only transition: a rung that writes a Bool flag plus a
    monotonic counter would otherwise churn the intern pool
    (counter value changes every scan) and get promoted to fired-only,
    losing the Bool causal chain in the process.  With Bools
    preserved and non-Bool unconsumed writes dropped, the rung's
    pattern stays stable on the Bool axis regardless of counter
    churn.
    """
    Enable = Bool("Enable")
    Flag = Bool("Flag")
    Source = Int("Source")
    Counter = Int("Counter")  # non-Bool, unconsumed

    with Program() as logic:
        with Rung(Enable):
            out(Flag)
            copy(Source, Counter)

    runner = PLC(logic)
    # Run for 150 scans with Counter changing every scan; under the
    # old "drop all unconsumed" filter the intern pool would hit the
    # 100-pattern threshold and the rung would transition to fired-only.
    for scan_id in range(150):
        runner.patch({"Enable": True, "Source": scan_id})
        runner.step()

    # Counter writes filtered out; Flag writes kept.
    firings = runner.rung_firings()
    assert firings[0]["Flag"] is True
    assert "Counter" not in firings[0]

    # Critical: intern pool stayed small (single Bool pattern),
    # so the rung never promoted to fired-only.
    assert runner._rung_firing_timelines.mode(0) == "cycle"
    assert runner._rung_firing_timelines.intern_size(0) == 1


def test_record_all_tags_bypasses_pdg_filter() -> None:
    """record_all_tags=True preserves every write including unconsumed non-Bools."""
    Enable = Bool("Enable")
    Source = Int("Source")
    Unused = Int("Unused")

    with Program() as logic:
        with Rung(Enable):
            copy(Source, Unused)

    runner = PLC(logic, record_all_tags=True)
    runner.patch({"Enable": True, "Source": 99})
    runner.step()

    firings = runner.rung_firings()
    assert firings[0]["Unused"] == 99


def test_pdg_filter_matches_unfiltered_on_consumed_writes() -> None:
    """Filtered and unfiltered runs agree on every kept write."""
    Enable = Bool("Enable")
    Flag = Bool("Flag")
    Source = Int("Source")
    Sink = Int("Sink")

    def build() -> object:
        with Program() as logic:
            with Rung(Enable):
                out(Flag)
                copy(Source, Sink)
            with Rung(Flag):
                pass
        return logic

    filtered = PLC(build())
    unfiltered = PLC(build(), record_all_tags=True)

    filtered.patch({"Enable": True, "Source": 5})
    filtered.step()
    unfiltered.patch({"Enable": True, "Source": 5})
    unfiltered.step()

    f_firings = filtered.rung_firings()
    u_firings = unfiltered.rung_firings()

    assert set(f_firings.keys()) == set(u_firings.keys())
    # Unconsumed non-Bool Sink lands in unfiltered but not filtered.
    assert "Sink" in u_firings[0]
    assert "Sink" not in f_firings[0]
    # The kept writes match exactly.
    for rung_idx in f_firings:
        for tag_name, value in f_firings[rung_idx].items():
            assert u_firings[rung_idx][tag_name] == value


def test_pdg_filter_preserves_fired_status_when_all_writes_filtered() -> None:
    """A rung whose only writes were filtered still registers as fired.

    To hit this path with the Bool-keep rule, every written tag must
    be a non-Bool that no rung reads.  ``query.cold_rungs`` /
    ``hot_rungs`` and ``cause()``'s PDG fallback both need to
    distinguish "didn't fire" from "fired but filtered."
    """
    Enable = Bool("Enable")
    SourceA = Int("SourceA")
    SourceB = Int("SourceB")
    TerminalA = Int("TerminalA")
    TerminalB = Int("TerminalB")

    with Program() as logic:
        with Rung(Enable):
            copy(SourceA, TerminalA)
            copy(SourceB, TerminalB)

    runner = PLC(logic)
    runner.patch({"Enable": True, "SourceA": 1, "SourceB": 2})
    runner.step()

    firings = runner.rung_firings()
    assert 0 in firings, "rung should still be listed as fired"
    assert len(firings[0]) == 0, "all writes were filtered out"

    # cause() still finds the writer via the PDG fallback.
    chain = runner.cause("TerminalA")
    assert chain is not None
    assert len(chain.steps) >= 1
    assert chain.steps[0].rung_index == 0


def test_pdg_filter_consumed_set_survives_reboot() -> None:
    """Reboot doesn't invalidate the consumed-tag cache (rung list is stable)."""
    Enable = Bool("Enable")
    Flag = Bool("Flag")
    Source = Int("Source")
    Unused = Int("Unused")

    with Program() as logic:
        with Rung(Enable):
            out(Flag)
            copy(Source, Unused)

    runner = PLC(logic)
    runner.patch({"Enable": True, "Source": 10})
    runner.step()
    firings_pre = runner.rung_firings()
    assert "Flag" in firings_pre[0]
    assert "Unused" not in firings_pre[0]

    runner.reboot()
    runner.patch({"Enable": True, "Source": 10})
    runner.step()
    firings_post = runner.rung_firings()
    assert "Flag" in firings_post[0]
    assert "Unused" not in firings_post[0]


def test_pdg_filter_bypassed_for_non_standard_rungs() -> None:
    """Programs with synthetic (non-Rung) entries bypass the filter cleanly."""

    class _RawRung:
        def evaluate(self, ctx) -> None:  # noqa: ANN001
            ctx.set_tag("Raw", 42)

    runner = PLC(logic=[_RawRung()])
    runner.step()
    firings = runner.rung_firings()
    assert firings[0]["Raw"] == 42


def test_pdg_filter_default_off_for_logic_less_plc() -> None:
    """PLC with no logic: filter returns None, capture proceeds unfiltered."""
    runner = PLC()
    runner.patch({"Some_Tag": True})
    runner.step()
    assert runner.rung_firings() == runner.rung_firings()
    assert len(runner.rung_firings()) == 0


def test_pdg_filter_preserved_across_fork() -> None:
    """fork() propagates the record_all_tags flag."""
    Enable = Bool("Enable")
    Source = Int("Source")
    Unused = Int("Unused")

    with Program() as logic:
        with Rung(Enable):
            copy(Source, Unused)

    parent = PLC(logic, record_all_tags=True)
    parent.patch({"Enable": True, "Source": 1})
    parent.step()
    child = parent.fork()
    child.patch({"Enable": True, "Source": 1})
    child.step()
    assert "Unused" in parent.rung_firings()[0]
    assert "Unused" in child.rung_firings()[0]

    parent_filtered = PLC(logic)
    parent_filtered.patch({"Enable": True, "Source": 1})
    parent_filtered.step()
    child_filtered = parent_filtered.fork()
    child_filtered.patch({"Enable": True, "Source": 1})
    child_filtered.step()
    assert "Unused" not in child_filtered.rung_firings()[0]


def test_pdg_filter_drops_counter_accumulator_pattern() -> None:
    """The canonical memory-win case: internal timer acc writes are dropped.

    A timer writes ``.Acc`` (Int) on every scan while enabled.  When
    no rung consumes the accumulator, the filter drops the write —
    the reason the filter exists (design doc §"PDG-filtered
    capture").  ``.Done`` (Bool) is kept even without a consumer
    because of the Bool-keep rule.
    """
    from pyrung.core import Timer, on_delay

    Trigger = Bool("Trigger")

    with Program() as logic:
        with Rung(Trigger):
            on_delay(Timer[1], preset=100)

    runner = PLC(logic, dt=0.010)
    runner.patch({"Trigger": True})
    runner.step()
    runner.step()

    firings_1 = runner.rung_firings(scan_id=1)
    firings_2 = runner.rung_firings(scan_id=2)
    assert 0 in firings_1
    assert 0 in firings_2
    assert "Timer_Acc" not in firings_1[0], "unconsumed timer acc should be filtered"
    assert "Timer_Acc" not in firings_2[0], "unconsumed timer acc should be filtered"
    # Timer_Done is a Bool → kept regardless of consumer status.
    assert "Timer_Done" in firings_1[0]

    # With record_all_tags=True, Timer_Acc writes are preserved.
    unfiltered = PLC(logic, dt=0.010, record_all_tags=True)
    unfiltered.patch({"Trigger": True})
    unfiltered.step()
    unfiltered.step()
    assert "Timer_Acc" in unfiltered.rung_firings(scan_id=1)[0]
    assert "Timer_Acc" in unfiltered.rung_firings(scan_id=2)[0]
