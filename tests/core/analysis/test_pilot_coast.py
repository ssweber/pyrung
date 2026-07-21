"""Tests for the CoastSession spine (the technician's trend recorder).

These exercise ``CoastSession.seek`` and the bump helpers (``value_bump`` /
``departure_bump`` / ``predicate_bump``) directly, not through the ``_ops``
wrappers — the wrappers are covered in ``test_pilot_ops.py``.

Coverage targets (per the CoastSession v2 design):
- Receipt basics + fold/no-fold landing-scan parity (perfect reaction)
- Departure terminal + recorded (tag, before, after) transition
- Simultaneous terminal bumps (target wins classification)
- Timeout stop_reason
- Nonterminal re-arm timeline, one_shot vs re-arm (perfect recall)
- Accumulator-comparison crossing exactness through the session
- predicate_bump (opaque callable) plateau-guard landing
- cyclefold dispatch under oscillating conditional holds
- seek([]) guard
- skipped receipt
"""

from __future__ import annotations

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, calc, copy, count_up, on_delay, out
from pyrung.core import Counter
from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _coast_to_value,
    _set_rungs,
    _settle_delayed_effects,
)
from pyrung.core.analysis.pilot.coast import (
    DEPARTURE,
    LIMITS,
    QUIESCENT,
    TARGET,
    Bump,
    CoastReceipt,
    CoastSession,
    departure_bump,
    predicate_bump,
    value_bump,
)
from pyrung.core.analysis.pilot.steer import _settle_cone
from pyrung.core.condition import AllCondition, CompareEq, CompareGe
from pyrung.core.harness import Harness
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Program fixtures
# ---------------------------------------------------------------------------


def _timer_program():
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog


def _role_program():
    """State starts at 1; a timer trips it 1 -> 2 on its own."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            copy(2, State)
    return prog


def _dual_output_program():
    """A timer whose Done drives two coils on the SAME scan."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    A = Bool("A")
    B = Bool("B")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(A)
        with Rung(Tmr.Done):
            out(B)
    return prog


def _counter_program():
    """A per-scan integer counter: Acc increments by 1 every enabled scan."""
    Enable = Bool("Enable", external=True)
    Reset = Bool("Reset", external=True)
    with Program() as prog:
        with Rung(Enable):
            count_up(Counter[1], preset=1000).reset(Reset)
    return prog


def _ramp_program():
    """An integer that ramps +1 per enabled scan (an opaque relational feed)."""
    Enable = Bool("Enable", external=True)
    Temp = Int("Temp")
    with Program() as prog:
        with Rung(Enable):
            calc(Temp + 1, Temp)
    return prog


def _blink_program():
    """A self-resetting timer whose Done pulses periodically, plus a longer
    timer that reaches ``Target`` well after several blink pulses."""
    Enable = Bool("Enable", external=True)
    Blink = Timer.clone("Blink")
    Long = Timer.clone("Long")
    Target = Bool("Target")
    with Program() as prog:
        # ~Blink.Done re-enables the timer the scan after it completes, so
        # Blink_Done is True for exactly one scan each period.
        with Rung(~Blink.Done):
            on_delay(Blink, 50, "ms")
        with Rung(Enable):
            on_delay(Long, 500, "ms")
        with Rung(Long.Done):
            out(Target)
    return prog


def _free_timer_program():
    """A free-running timer reaches Target on its own; Input only mirrors."""
    Input = Bool("Input", external=True)
    Tmr = Timer.clone("Tmr")
    Mirror = Bool("Mirror")
    Target = Bool("Target")
    with Program() as prog:
        with Rung():
            on_delay(Tmr, 80, "ms")
        with Rung(Input):
            out(Mirror)
        with Rung(Tmr.Done):
            out(Target)
    return prog


def _gated_counter_program():
    """A counter whose gate is its own ``~Done``: Acc climbs to preset, then
    the Done bit drops the gate and Acc plateaus — a settling cone."""
    Reset = Bool("Reset", external=True)
    Ctr = Counter[1]
    with Program() as prog:
        with Rung(~Ctr.Done):
            count_up(Ctr, preset=3).reset(Reset)
    return prog


def _transient_program():
    """State passes through 2 for exactly one scan on its way to 3.

    The ``State == 2`` rung is placed *above* the ``State == 1`` rung so the
    two copies cannot cascade within one scan — State is 2 for one full scan
    before the next scan advances it to 3.
    """
    Enable = Bool("Enable", external=True)
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable, State == 2):
            copy(3, State)
        with Rung(Enable, State == 1):
            copy(2, State)
    return prog


def _harness_feedback_program():
    """Harness-fed program: Enable drives a Physical feedback that resolves
    after a plant delay, then a gated copy fires (mirrors the fixture in
    test_pilot_ops.py's TestSettleDelayedEffects)."""
    FB = Physical("MotorFb", on_delay="200ms", off_delay="100ms")
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog


def _two_stage_program():
    """State climbs 1 -> 2 (timer A) then 2 -> 3 (timer B, gated on State == 2).

    A two-hop transition chain: each stage is gated so the earlier copy cannot
    re-fire once its stage completes, and State settles at 3."""
    Enable = Bool("Enable", external=True)
    TmrA = Timer.clone("TmrA")
    TmrB = Timer.clone("TmrB")
    State = Int("State", default=1)
    with Program() as prog:
        with Rung(Enable):
            on_delay(TmrA, 100, "ms")
        with Rung(TmrA.Done, State == 1):
            copy(2, State)
        with Rung(State == 2):
            on_delay(TmrB, 100, "ms")
        with Rung(TmrB.Done):
            copy(3, State)
    return prog


def _runaway_channel_program():
    """A channel register that changes every enabled scan (never settles)."""
    Enable = Bool("Enable", external=True)
    Chan = Int("Chan")
    with Program() as prog:
        with Rung(Enable):
            calc(Chan + 1, Chan)
    return prog


def _static_channel_long_timer_program():
    """A channel that never moves while a long (foldable) timer runs."""
    Enable = Bool("Enable", external=True)
    Long = Timer.clone("Long")
    Chan = Int("Chan", default=5)
    with Program() as prog:
        # Written to a constant every scan so the tag materializes but never
        # departs its value (a static channel, not a pruned unused one).
        with Rung():
            copy(5, Chan)
        with Rung(Enable):
            on_delay(Long, 10_000, "ms")
    return prog


def _step_until(plc: PLC, pred, cap: int = 20_000) -> int:
    """Drive a fold=False fork scan-by-scan; return the first scan pred holds."""
    start = plc.state.scan_id
    while not pred(plc.state):
        plc.step()
        assert plc.state.scan_id - start <= cap, "manual fork never satisfied pred"
    return plc.state.scan_id


# ---------------------------------------------------------------------------
# 1. Receipt basics + fold/no-fold parity
# ---------------------------------------------------------------------------


class TestReceiptBasics:
    def test_target_reached_lands_on_exact_scan(self):
        plc = PLC(_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        manual = plc.fork()  # fold=False reference
        target = value_bump(plc, "target", TARGET, "Done", True)
        receipt = CoastSession(plc, kind="zoom").seek([target], budget=500)

        assert receipt.reached
        assert receipt.stop_reason == "reached"
        assert receipt.fired == ("target",)
        assert plc.state.tags["Done"] is True

        landing = _step_until(manual, lambda s: s.tags.get("Done") is True)
        # Perfect reaction: the folded seek lands on the identical scan the
        # scan-by-scan fork first sees the predicate hold.
        assert receipt.end_scan == landing
        # One recorded pen mark for the target's watched tag.
        assert len(receipt.events) == 1
        assert receipt.events[0].name == "target"
        assert receipt.events[0].scan == landing


# ---------------------------------------------------------------------------
# 2. Departure terminal
# ---------------------------------------------------------------------------


class TestDepartureTerminal:
    def test_departure_records_transition_at_exact_scan(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        manual = plc.fork()
        dep = departure_bump(plc, "ejected", {"State": 1})
        receipt = CoastSession(plc, kind="zoom").seek([dep], budget=500)

        assert receipt.stop_reason == "departed"
        assert receipt.reached is False
        assert receipt.fired == ("ejected",)

        assert len(receipt.events) == 1
        ev = receipt.events[0]
        assert ev.name == "ejected"
        assert ev.kind == DEPARTURE
        assert ev.transitions == (("State", 1, 2),)

        landing = _step_until(manual, lambda s: s.tags.get("State") != 1)
        assert ev.scan == landing
        assert receipt.end_scan == landing


# ---------------------------------------------------------------------------
# 3. Simultaneous terminal bumps
# ---------------------------------------------------------------------------


class TestSimultaneousTerminals:
    def test_target_and_departure_same_scan_both_fired_target_wins(self):
        plc = PLC(_dual_output_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        a = value_bump(plc, "target", TARGET, "A", True)
        b = value_bump(plc, "other", DEPARTURE, "B", True)
        receipt = CoastSession(plc).seek([a, b], budget=500)

        # Both coils flip on the one scan Tmr.Done goes true.
        assert plc.state.tags["A"] is True
        assert plc.state.tags["B"] is True
        # Both terminal bumps are recorded — never collapsed.
        assert set(receipt.fired) == {"target", "other"}
        assert receipt.fired == ("target", "other")
        # A target among the simultaneous firings wins classification.
        assert receipt.stop_reason == "reached"
        assert receipt.reached


# ---------------------------------------------------------------------------
# 4. Timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_nothing_fires_within_budget(self):
        plc = PLC(_timer_program(), dt=0.010)  # Enable never asserted -> Done stays False
        target = value_bump(plc, "target", TARGET, "Done", True)
        receipt = CoastSession(plc).seek([target], budget=20)

        assert receipt.stop_reason == "timeout"
        assert receipt.reached is False
        assert receipt.fired == ()
        assert receipt.events == ()
        assert receipt.end_scan - receipt.start_scan <= 20


# ---------------------------------------------------------------------------
# 5. Nonterminal re-arm timeline (perfect recall)
# ---------------------------------------------------------------------------


class TestReArmTimeline:
    def test_rearm_records_multiple_ordered_events_then_terminal(self):
        plc = PLC(_blink_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        blink = value_bump(plc, "blink", DEPARTURE, "Blink_Done", True, terminal=False)
        target = value_bump(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc).seek([blink, target], budget=5000)

        assert receipt.stop_reason == "reached"
        blink_events = [e for e in receipt.events if e.name == "blink"]
        # A re-arming nonterminal bump fires more than once inside the window.
        assert len(blink_events) >= 2
        scans = [e.scan for e in blink_events]
        # Ordered, ascending, and distinct scans (perfect recall).
        assert scans == sorted(scans)
        assert len(set(scans)) == len(scans)
        # The terminal target closes the timeline.
        assert receipt.events[-1].name == "target"
        assert all(s < receipt.events[-1].scan for s in scans)

    def test_one_shot_records_exactly_one_event(self):
        plc = PLC(_blink_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        blink_tag = plc._known_tags_by_name["Blink_Done"]
        blink = Bump(
            name="blink",
            kind=DEPARTURE,
            predicate=lambda s: s.tags.get("Blink_Done") is True,
            condition=CompareEq(blink_tag, True),
            watched=("Blink_Done",),
            terminal=False,
            one_shot=True,
        )
        target = value_bump(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc).seek([blink, target], budget=5000)

        assert receipt.stop_reason == "reached"
        blink_events = [e for e in receipt.events if e.name == "blink"]
        assert len(blink_events) == 1


# ---------------------------------------------------------------------------
# 6. Crossing exactness through the session
# ---------------------------------------------------------------------------


class TestCrossingExactness:
    def test_accumulator_compare_folds_to_exact_crossing_scan(self):
        plc = PLC(_counter_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Counter_Acc == 1

        manual = plc.fork()
        acc = plc._known_tags_by_name["Counter_Acc"]
        n = 7
        bump = Bump(
            name="target",
            kind=TARGET,
            predicate=lambda s: (s.tags.get("Counter_Acc") or 0) >= n,
            condition=CompareGe(acc, n),
            watched=("Counter_Acc",),
        )
        receipt = CoastSession(plc).seek([bump], budget=200)

        assert receipt.stop_reason == "reached"
        landing = _step_until(manual, lambda s: (s.tags.get("Counter_Acc") or 0) >= n)
        assert receipt.end_scan == landing
        assert plc.state.tags["Counter_Acc"] == n


# ---------------------------------------------------------------------------
# 7. predicate_bump (opaque callable)
# ---------------------------------------------------------------------------


class TestPredicateBump:
    def test_relational_predicate_lands_via_plateau_guard(self):
        plc = PLC(_ramp_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Temp == 1

        manual = plc.fork()
        threshold = 5
        bump = predicate_bump(
            "target",
            TARGET,
            lambda s: (s.tags.get("Temp") or 0) >= threshold,
            watched=("Temp",),
        )
        receipt = CoastSession(plc).seek([bump], budget=200)

        assert receipt.reached
        assert receipt.stop_reason == "reached"
        # The transition is recorded: before < threshold <= after.
        assert len(receipt.events) == 1
        ev = receipt.events[0]
        assert ev.name == "target"
        assert len(ev.transitions) == 1
        tag, before, after = ev.transitions[0]
        assert tag == "Temp"
        assert before < threshold <= after

        landing = _step_until(manual, lambda s: (s.tags.get("Temp") or 0) >= threshold)
        assert receipt.end_scan == landing


# ---------------------------------------------------------------------------
# 8. cyclefold dispatch
# ---------------------------------------------------------------------------


class TestCyclefoldDispatch:
    def test_oscillating_holds_dispatch_to_cyclefold(self):
        plc = PLC(_free_timer_program(), dt=0.010)
        plc.step()
        start_scan = plc.state.scan_id

        Input = plc._known_tags_by_name["Input"]
        Target = plc._known_tags_by_name["Target"]
        # Toggle Input every scan while the (independent) target still runs.
        _set_rungs(
            plc,
            [
                PilotRung("Input", True, AllCondition(~Target, ~Input)),
                PilotRung("Input", False, AllCondition(~Target, Input)),
            ],
        )

        target = value_bump(plc, "target", TARGET, "Target", True)
        receipt = CoastSession(plc, kind="letrun").seek([target], budget=200)

        assert receipt.reached
        assert plc.state.tags["Target"] is True
        # cyclefold ran real scans (it can't dt-jump past the pet oscillation).
        assert receipt.real_scans > 0
        # The held input actually oscillated during the coast.
        seen = {
            plc.history.at(s).tags.get("Input")
            for s in range(start_scan + 1, plc.state.scan_id + 1)
        }
        assert True in seen and False in seen


# ---------------------------------------------------------------------------
# 9. Empty bumps
# ---------------------------------------------------------------------------


class TestEmptyBumps:
    def test_seek_empty_raises(self):
        plc = PLC(_timer_program(), dt=0.010)
        with pytest.raises(ValueError, match="at least one bump"):
            CoastSession(plc).seek([], budget=10)


# ---------------------------------------------------------------------------
# 10. skipped receipt (via the _coast_to_value builder)
# ---------------------------------------------------------------------------


class TestSkippedReceipt:
    def test_none_channel_tag_is_skipped(self):
        plc = PLC(_timer_program(), dt=0.010)
        receipt = _coast_to_value(plc, None, True, budget=20)
        assert receipt.stop_reason == "skipped"
        assert receipt.reached is False
        assert receipt.fired == ()
        assert receipt.events == ()


# ---------------------------------------------------------------------------
# 11. settle — cone fixpoint (quiescence)
# ---------------------------------------------------------------------------


class TestSettleQuiescent:
    def test_cone_stops_moving_reports_quiescent(self):
        plc = PLC(_gated_counter_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Counter_Acc"}))

        # The cone moved for a few scans (Acc climbing) then held — a fixpoint,
        # not a ceiling exit.
        assert receipt.stop_reason == "quiescent"
        assert receipt.real_scans < LIMITS.cone_ceiling
        # trajectory length matches the scans stepped; end_scan is exact.
        assert len(receipt.trajectory) == receipt.real_scans
        assert receipt.end_scan == start + receipt.real_scans
        assert receipt.end_scan - receipt.start_scan == receipt.real_scans
        # The last two snapshots are identical on the watched tag (what the
        # fixpoint observed).
        acc = [s.get("Counter_Acc") for s in receipt.trajectory]
        assert acc[-1] == acc[-2]


# ---------------------------------------------------------------------------
# 12. settle — reached_fn short-circuit (land a one-scan transient)
# ---------------------------------------------------------------------------


class TestSettleReached:
    def test_reached_fn_lands_one_scan_transient(self):
        plc = PLC(_transient_program(), dt=0.010)
        plc.patch({"Enable": True})
        start = plc.state.scan_id

        # State is 2 for exactly one scan; reached_fn must land on it and not
        # blow past to 3.  (Note: this fires below the floor — reached_fn is
        # judged every scan, floor gates only the fixpoint check.)
        receipt = CoastSession(plc, kind="settle").settle(
            frozenset({"State"}),
            reached_fn=lambda tags: tags.get("State") == 2,
        )

        assert receipt.stop_reason == "reached"
        assert receipt.reached
        assert receipt.real_scans == 1
        assert receipt.end_scan == start + 1
        assert len(receipt.trajectory) == 1
        assert receipt.trajectory[-1]["State"] == 2
        assert plc.state.tags["State"] == 2


# ---------------------------------------------------------------------------
# 13. settle — timeout honesty (the headline)
# ---------------------------------------------------------------------------


class TestSettleTimeout:
    def test_free_running_cone_exhausts_ceiling_and_is_named(self):
        plc = PLC(_counter_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()  # Counter_Acc == 1, climbing every scan hereafter
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Counter_Acc"}))

        # A cone that never quiesces exhausts the ceiling — and the receipt
        # NAMES it "timeout" (the old _settle_cone returned a bare trajectory
        # with no such flag; settlement is never passed off as reached here).
        assert receipt.stop_reason == "timeout"
        assert receipt.reached is False
        assert receipt.real_scans == LIMITS.cone_ceiling
        assert len(receipt.trajectory) == LIMITS.cone_ceiling
        assert receipt.end_scan == start + LIMITS.cone_ceiling
        # Every snapshot genuinely moved (proving it was no false plateau).
        acc = [s.get("Counter_Acc") for s in receipt.trajectory]
        assert acc == sorted(acc)
        assert len(set(acc)) == len(acc)


# ---------------------------------------------------------------------------
# 14. settle — floor (fixpoint not judged before the floor)
# ---------------------------------------------------------------------------


class TestSettleFloor:
    def test_motionless_cone_still_steps_floor_scans(self):
        # A cone that is motionless from the very first scan (Enable never
        # asserted -> Tmr_Done stays False).  Without the floor, the fixpoint
        # would fire at scan 1; the floor forces at least `floor` scans first.
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Tmr_Done"}), floor=4)

        assert receipt.stop_reason == "quiescent"
        assert receipt.real_scans == 4
        assert receipt.end_scan == start + 4

    def test_floor_of_two_steps_two(self):
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="settle").settle(frozenset({"Tmr_Done"}), floor=2)

        assert receipt.stop_reason == "quiescent"
        assert receipt.real_scans == 2
        assert receipt.end_scan == start + 2


# ---------------------------------------------------------------------------
# 15. dwell — fixed-window coast, no predicate
# ---------------------------------------------------------------------------


class TestDwell:
    def test_dwell_steps_exactly_n_scans(self):
        plc = PLC(_timer_program(), dt=0.010)
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="pulse").dwell(4)

        assert receipt.stop_reason == "dwell"
        assert receipt.real_scans == 4
        assert receipt.end_scan - receipt.start_scan == 4
        assert receipt.end_scan == start + 4
        assert receipt.fired == ()
        # dwell is not a settle — it carries no per-scan trajectory.
        assert receipt.trajectory == ()


# ---------------------------------------------------------------------------
# 16. QUIESCENT stop_reason through seek (generalized classification)
# ---------------------------------------------------------------------------


class TestQuiescentThroughSeek:
    def test_quiescent_terminal_bump_names_stop_reason(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        # A terminal bump whose kind is neither TARGET nor DEPARTURE — the
        # generalized classification falls through to the bump's own kind.
        bump = predicate_bump(
            "quiesced",
            QUIESCENT,
            lambda s: s.tags.get("State") == 2,
            watched=("State",),
        )
        receipt = CoastSession(plc).seek([bump], budget=500)

        assert receipt.stop_reason == "quiescent"
        assert receipt.reached is False
        assert receipt.fired == ("quiesced",)
        assert plc.state.tags["State"] == 2


# ---------------------------------------------------------------------------
# 17. _settle_delayed_effects returns CoastReceipts
# ---------------------------------------------------------------------------


class TestSettleDelayedEffectsReceipts:
    def test_harness_feedback_settle_returns_quiescent_receipt(self):
        plc = PLC(_harness_feedback_program(), dt=0.010)
        Harness(plc).install()
        plc.patch({"Enable": True})
        plc.step()
        assert plc._harness.pending_count > 0

        before = dict(plc.state.tags)
        receipts = _settle_delayed_effects(plc, before, cfg=None, scan_budget=200)

        # Now a list of receipts (values that outlive the coast), not a bool.
        assert isinstance(receipts, list)
        assert len(receipts) == 1
        assert all(isinstance(r, CoastReceipt) for r in receipts)
        # The harness-quiescence seek lands on a QUIESCENT bump.
        assert receipts[0].stop_reason == "quiescent"
        # And the effect actually settled: feedback resolved, gated copy fired.
        assert plc._harness.pending_count == 0
        assert plc.state.tags["Stage"] == 1

    def test_nothing_pending_returns_empty_list(self):
        # No harness installed and no done-specs -> nothing to settle.
        plc = PLC(_timer_program(), dt=0.010)
        assert getattr(plc, "_harness", None) is None
        receipts = _settle_delayed_effects(plc, dict(plc.state.tags), cfg=None, scan_budget=200)
        assert receipts == []


# ---------------------------------------------------------------------------
# 18. _settle_cone wrapper parity with CoastSession.settle
# ---------------------------------------------------------------------------


class TestSettleConeParity:
    def test_wrapper_and_session_produce_identical_trajectories(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()

        cone = frozenset({"State"})
        # Two independent forks from the identical state, driven deterministically.
        fork_a = plc.fork()
        fork_b = plc.fork()

        via_steer = _settle_cone(fork_a, cone)
        via_session = CoastSession(fork_b, kind="settle").settle(cone)

        # The thin wrapper returns exactly the receipt's trajectory as a list.
        assert via_steer == list(via_session.trajectory)
        assert via_session.stop_reason == "quiescent"


# ---------------------------------------------------------------------------
# 19. settle_landing — departure-then-quiescence (ride a transition chain home)
# ---------------------------------------------------------------------------


class TestSettleLandingSingleHop:
    def test_single_transition_then_quiescent(self):
        plc = PLC(_role_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        session = CoastSession(plc, kind="departure-settle")
        receipt = session.settle_landing("State", confirm_scans=50)

        assert receipt.stop_reason == "quiescent"
        assert plc.state.tags["State"] == 2
        # Exactly one hop, recording the 1 -> 2 transition on the channel.
        hops = [e for e in receipt.events if e.name == "hop"]
        assert len(hops) == 1
        assert hops[0].kind == DEPARTURE
        assert hops[0].transitions == (("State", 1, 2),)
        # The confirmation window folds, but its scan-ids still elapse — the
        # landing sits a full confirm window past the hop.
        assert receipt.end_scan >= hops[0].scan + 50


class TestSettleLandingMultiHop:
    def test_two_stage_chain_records_ordered_hops(self):
        plc = PLC(_two_stage_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["State"] == 1

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "State", confirm_scans=50
        )

        # Perfect recall: the whole two-stage transition chain is evidence.
        assert receipt.stop_reason == "quiescent"
        assert plc.state.tags["State"] == 3
        hops = [e for e in receipt.events if e.name == "hop"]
        assert len(hops) == 2
        assert hops[0].transitions == (("State", 1, 2),)
        assert hops[1].transitions == (("State", 2, 3),)
        # Ordered and distinct in scan (rode each hop, re-armed, rode the next).
        assert hops[0].scan < hops[1].scan


class TestSettleLandingNeverMoves:
    def test_static_channel_quiescent_after_one_window(self):
        plc = PLC(_static_channel_long_timer_program(), dt=0.010)
        # Enable NOT asserted: the timer is idle and Chan never moves.
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=40
        )

        assert receipt.stop_reason == "quiescent"
        # A channel that never departs records no hop events.
        assert receipt.events == ()
        assert plc.state.tags["Chan"] == 5
        # One confirmation window, nothing more.
        assert receipt.end_scan - start == 40


class TestSettleLandingCapHonesty:
    def test_free_running_channel_times_out_within_cap(self):
        plc = PLC(_runaway_channel_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        start = plc.state.scan_id
        first = plc.state.tags["Chan"]

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=100, cap=50
        )

        # A channel that changes faster than the confirm window never quiesces;
        # the cap is respected and the landing is NAMED "timeout" — never
        # classified as settled.  (This is exactly the non-quiescent receipt
        # that classify_departure refuses, see TestClassifyDepartureRefusal.)
        assert receipt.stop_reason == "timeout"
        assert 0 < receipt.end_scan - start <= 50
        # It kept hopping the whole time (mid-transition at the cap).
        assert len(receipt.events) >= 2
        assert plc.state.tags["Chan"] > first


class TestSettleLandingFolds:
    def test_confirmation_window_folds_scan_ids_still_elapse(self):
        plc = PLC(_static_channel_long_timer_program(), dt=0.010)
        plc.patch({"Enable": True})  # long timer runs -> a foldable window
        start = plc.state.scan_id

        receipt = CoastSession(plc, kind="departure-settle").settle_landing(
            "Chan", confirm_scans=200
        )

        assert receipt.stop_reason == "quiescent"
        # The confirmation window's scan-ids elapse in a single folded seek
        # (a quiet 200-scan second, not 200 stepped scans).
        assert receipt.end_scan - start == 200
        # The composite receipt retains the inner seek's actual work.
        assert 0 < receipt.kernel_scans < receipt.logical_scans
        assert receipt.macro_folds >= 1
        assert receipt.skipped_scans == receipt.logical_scans - receipt.kernel_scans


# ---------------------------------------------------------------------------
# 20. classify_departure refuses a non-quiescent (timeout) receipt
# ---------------------------------------------------------------------------


class TestClassifyDepartureRefusal:
    def test_non_quiescent_receipt_is_refused_as_unknown(self, monkeypatch):
        # Constructing a full _PilotState/_PilotContext is heavy, so exercise
        # the refusal at the seam: a timeout receipt from _settle_departure is
        # the cap-hit, possibly-mid-transition value the detour reader must NOT
        # trust as a settled landing.
        from types import SimpleNamespace

        from pyrung.core.analysis.pilot import detour

        timeout_receipt = CoastReceipt(
            kind="departure-settle",
            start_scan=0,
            end_scan=2000,
            stop_reason="timeout",
            fired=(),
            events=(),
            budget=2000,
        )
        fake_fork = SimpleNamespace(state=SimpleNamespace(tags={"Chan": 7}, scan_id=2000))
        monkeypatch.setattr(
            detour,
            "_settle_departure",
            lambda state, channel_tag: (fake_fork, timeout_receipt),
        )

        verdict = detour.classify_departure(
            SimpleNamespace(),  # state — consumed only by the (mocked) settle
            SimpleNamespace(),  # ctx — untouched on the refusal arm
            "Chan",
            from_value=1,
            source_snap={"Chan": 1},
        )

        assert verdict.decision == "unknown"
        assert "did not settle within cap" in verdict.reason
        assert "timeout" in verdict.reason
        # The receipt's landing value is still surfaced, just not trusted.
        assert verdict.settled_value == 7
        assert verdict.settle_scans == 2000
