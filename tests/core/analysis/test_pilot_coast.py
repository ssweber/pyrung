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
from pyrung.core.analysis.pilot._ops import PilotRung, _coast_to_value, _set_rungs
from pyrung.core.analysis.pilot.coast import (
    DEPARTURE,
    TARGET,
    Bump,
    CoastSession,
    departure_bump,
    predicate_bump,
    value_bump,
)
from pyrung.core.condition import AllCondition, CompareEq, CompareGe
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
