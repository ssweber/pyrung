"""An ``avoid=`` check that raises did not say the state is clear.

A check that raises on a real state reaches the caller as a named error, whichever
gate met it: the route gate before the drive, the action gate, the scan gate, a
trial coast, a terminal reason.  It never reads as a pass and never as a verdict.
A raise on the resting baseline only denies the hold being judged, since that
baseline holds the steerable inputs and nothing else.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import PLC, Bool, Int, Or, Program, Rung, Timer, copy, on_delay, out
from pyrung.core import AvoidCheckError
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import budget, pilot_events, pilot_how
from pyrung.core.analysis.pilot.avoid import (
    _avoid_forces,
    _avoid_snap_names,
    _avoid_violations,
    _AvoidCheckRaised,
    _hold_allowed,
)
from pyrung.core.analysis.pilot.budget import SearchOutOfMemory, SearchOutOfTime
from pyrung.core.analysis.pilot.coast import CoastSession
from pyrung.core.analysis.pilot.route_judgment import route_forced_names, route_forces
from pyrung.core.analysis.pilot.trial_gates import _avoid_names_after_clear
from pyrung.core.analysis.pilot.types import _AvoidMember, _AvoidPredicate
from pyrung.core.coast_run import _Seen, _SeenState
from pyrung.core.rung_firings import SearchQuestionRefused, UnrecordedScansError
from pyrung.core.runner import _compile_avoid


def _two_ways():
    """Target has a route through Danger (Fast, tried first) and one clear of it (Slow)."""
    Fast = Bool("Fast", external=True)
    Slow = Bool("Slow", external=True)
    Danger = Bool("Danger")
    Level = Int("Level")
    Target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(Fast):
            out(Danger)
        with Rung(Fast):
            copy(5, Level)
        with Rung(Or(Fast, Slow)):
            out(Target)
    return prog, Target, Danger, Level


def _raises_in_danger(snapshot: dict) -> bool:
    if snapshot.get("Danger"):
        raise ZeroDivisionError("boom in the avoided state")
    return False


def _raises_at_once(snapshot: dict) -> bool:
    raise ZeroDivisionError("boom everywhere")


# ---------------------------------------------------------------------------
# Through the public entry points
# ---------------------------------------------------------------------------


def test_how_does_not_enter_a_state_whose_check_raises() -> None:
    """The check raises only in the avoided state, so only a route through it can meet it."""
    prog, target, *_ = _two_ways()

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError") as raised:
        PLC(prog, dt=0.1).how(target, avoid=_raises_in_danger, max_scans=200)

    assert isinstance(raised.value.__cause__, ZeroDivisionError)
    assert raised.value.check == "avoid"
    assert str(raised.value).isascii()


def test_bare_callable_does_not_enter_a_state_whose_check_raises() -> None:
    prog, target, *_ = _two_ways()

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError") as raised:
        pilot_how(PLC(prog, dt=0.1), target, avoid_pred=_raises_in_danger, max_scans=200)

    assert isinstance(raised.value.__cause__, ZeroDivisionError)


def test_pilot_events_raises_the_same_error() -> None:
    prog, target, *_ = _two_ways()
    plc = PLC(prog, dt=0.1)

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError"):
        list(pilot_events(plc, target, avoid_pred=_raises_in_danger, max_scans=200))


@pytest.mark.parametrize(
    ("predicate", "cause"),
    [
        (_raises_at_once, ZeroDivisionError),
        (lambda s: s["Ghost"], KeyError),
        (lambda s: s.get("Ghost") > 3, TypeError),
        (lambda s: s.tags["Danger"], AttributeError),
    ],
    ids=["division", "absent-index", "absent-compare", "wrong-shape"],
)
@pytest.mark.parametrize("entry", ["how", "bare"])
def test_a_broken_check_reaches_the_caller(predicate, cause, entry) -> None:
    prog, target, *_ = _two_ways()
    plc = PLC(prog, dt=0.1)

    with pytest.raises(AvoidCheckError) as raised:
        if entry == "how":
            plc.how(target, avoid=predicate, max_scans=200)
        else:
            pilot_how(plc, target, avoid_pred=predicate, max_scans=200)

    assert isinstance(raised.value.__cause__, cause)


def test_a_condition_that_cannot_compare_names_itself() -> None:
    prog, target, _danger, level = _two_ways()

    with pytest.raises(AvoidCheckError, match="TypeError") as raised:
        PLC(prog, dt=0.1).how(target, avoid=level > "x", max_scans=200)

    assert "Level" in raised.value.check
    assert isinstance(raised.value.__cause__, TypeError)


def test_a_tag_the_state_lacks_yet_is_read_with_a_default() -> None:
    """A system tag first appears after a scan, so a fresh state has no value to index."""
    prog, target, *_ = _two_ways()

    with pytest.raises(AvoidCheckError, match="snapshot.get") as raised:
        PLC(prog, dt=0.1).how(target, avoid=lambda s: s["fault.out_of_range"], max_scans=200)
    assert isinstance(raised.value.__cause__, KeyError)

    plan = PLC(prog, dt=0.1).how(
        target, avoid=lambda s: s.get("fault.out_of_range", False), max_scans=200
    )
    assert plan.status is PlanStatus.REACHED


def test_one_raising_member_of_a_union_is_enough() -> None:
    prog, target, danger, _level = _two_ways()

    with pytest.raises(AvoidCheckError):
        PLC(prog, dt=0.1).how(target, avoid=(danger, _raises_at_once), max_scans=200)


def test_a_check_raising_only_on_a_coasted_endpoint_reaches_the_caller() -> None:
    Start = Bool("Start", external=True)
    Run = Bool("Run")
    Tmr = Timer.clone("RaisesLateTmr")
    Target = Bool("RaisesLateTarget")
    with Program(strict=False) as prog:
        with Rung(Or(Start, Run)):
            out(Run)
        with Rung(Run):
            on_delay(Tmr, 1000, "ms")
        with Rung(Tmr.Done):
            out(Target)

    def _raises_once_timing(snapshot: dict) -> bool:
        if (snapshot.get(Tmr.Acc.name) or 0) >= 300:
            raise ValueError("boom while timing")
        return False

    with pytest.raises(AvoidCheckError, match="ValueError"):
        PLC(prog, dt=0.010).how(Target, avoid=_raises_once_timing, max_scans=2000)


def test_the_callers_plc_is_left_as_it_was() -> None:
    prog, target, *_ = _two_ways()
    plc = PLC(prog, dt=0.1)
    before = (plc.state.scan_id, dict(plc.state.tags))

    with pytest.raises(AvoidCheckError):
        plc.how(target, avoid=_raises_in_danger, max_scans=200)

    assert (plc.state.scan_id, dict(plc.state.tags)) == before
    assert plc.how(target, max_scans=200).status is PlanStatus.REACHED


# ---------------------------------------------------------------------------
# What ends a search keeps ending it
# ---------------------------------------------------------------------------


def _unrecorded() -> UnrecordedScansError:
    return UnrecordedScansError(1, 2, 2)


@pytest.mark.parametrize(
    ("stop", "said"),
    [
        (SearchOutOfTime, "wall-clock"),
        (SearchOutOfMemory, "memory"),
        (lambda: SearchQuestionRefused(_unrecorded()), "refused"),
    ],
    ids=["time", "memory", "refused"],
)
def test_search_control_passes_through_a_check(stop, said, monkeypatch) -> None:
    monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)
    prog, target, *_ = _two_ways()

    def _ends_the_search(snapshot: dict) -> bool:
        # A scanned state has the system tags: the search is under way.
        if "fault.out_of_range" in snapshot:
            raise stop()
        return False

    plc = PLC(prog, dt=0.1)
    plan = plc.how(target, avoid=_ends_the_search, max_scans=200, wall_clock_s=60)

    assert plan.status is PlanStatus.STOPPED
    assert said in (plan.reason or "")


# ---------------------------------------------------------------------------
# A healthy check is not affected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "healthy",
    [
        lambda s: bool(s.get("Danger")),
        lambda s: s["Danger"],
        lambda s: s["Level"] > 3,
    ],
    ids=["get", "index", "index-compare"],
)
def test_a_healthy_check_finds_the_clear_route(healthy) -> None:
    prog, target, *_ = _two_ways()

    plan = PLC(prog, dt=0.1).how(target, avoid=healthy, max_scans=200)

    assert plan.status is PlanStatus.REACHED
    assert plan.replay().state.tags["Danger"] is False


# ---------------------------------------------------------------------------
# Each boundary, with both kinds of check
# ---------------------------------------------------------------------------


def _member_form(pred) -> _AvoidPredicate:
    return _AvoidPredicate((_AvoidMember(name="the check", pred=pred),))


@pytest.fixture(params=["member", "bare"])
def raising_check(request):
    return _member_form(_raises_at_once) if request.param == "member" else _raises_at_once


def test_snapshot_check_raises(raising_check) -> None:
    with pytest.raises(_AvoidCheckRaised):
        _avoid_snap_names(raising_check, {"Danger": True})


def test_action_gate_on_a_real_snapshot_raises(raising_check) -> None:
    ctx = SimpleNamespace(avoid_pred=raising_check, resting={})

    with pytest.raises(_AvoidCheckRaised):
        _avoid_violations(ctx, [("Fast", True)], {"Fast": False})


def test_route_gate_raises(raising_check) -> None:
    with pytest.raises(_AvoidCheckRaised):
        route_forces([], {"Danger": True}, raising_check)
    with pytest.raises(_AvoidCheckRaised):
        route_forced_names([], {"Danger": True}, raising_check)


def test_after_clear_gate_raises_on_either_snapshot(raising_check) -> None:
    with pytest.raises(_AvoidCheckRaised):
        _avoid_names_after_clear(raising_check, {"Danger": False}, {"Danger": True})


def test_after_clear_gate_raises_on_the_later_snapshot_alone() -> None:
    for check in (_member_form(_raises_in_danger), _raises_in_danger):
        with pytest.raises(_AvoidCheckRaised):
            _avoid_names_after_clear(check, {"Danger": False}, {"Danger": True})


def test_the_transit_passes_a_search_that_swallows_exceptions() -> None:
    """Pilot reads causes under many ``except Exception`` that mean "no evidence"."""
    assert not issubclass(_AvoidCheckRaised, Exception)
    assert issubclass(AvoidCheckError, Exception)
    with pytest.raises(_AvoidCheckRaised):
        try:
            _avoid_snap_names(_member_form(_raises_at_once), {})
        except Exception:
            pass


def _seen_coast_program():
    Tick = Bool("CoastTick", external=True)
    Out = Bool("CoastOut")
    with Program(strict=False) as prog:
        with Rung(Tick):
            out(Out)
    return prog


def test_arming_a_coast_raises_for_a_broken_check() -> None:
    session = CoastSession(PLC(_seen_coast_program(), dt=0.1))

    with pytest.raises(_AvoidCheckRaised):
        session.arm_avoid(_member_form(_raises_at_once))


def test_a_coast_scan_raises_for_a_broken_check() -> None:
    session = CoastSession(PLC(_seen_coast_program(), dt=0.1))
    session.arm_avoid(_member_form(_raises_in_danger))
    trigger = session._avoid_triggers[0]

    assert trigger.predicate(SimpleNamespace(tags={"Danger": False})) is False
    with pytest.raises(_AvoidCheckRaised):
        trigger.predicate(SimpleNamespace(tags={"Danger": True}))


def test_a_loop_scan_asking_for_the_real_state_is_not_a_broken_check() -> None:
    """An opaque check cannot name its reads, so a loop scan hands it to the real state."""

    session = CoastSession(PLC(_seen_coast_program(), dt=0.1))
    session.arm_avoid(_member_form(_raises_in_danger))
    trigger = session._avoid_triggers[0]

    assert trigger.predicate(_SeenState(_Seen({"Danger": True}, frozenset()))) is False


# ---------------------------------------------------------------------------
# The resting baseline holds the steerable inputs and nothing else
# ---------------------------------------------------------------------------


def _hold_ctx(avoid, resting=None):
    return SimpleNamespace(
        avoid_pred=avoid,
        resting={"Hold": False} if resting is None else resting,
        compass=None,
    )


def test_a_raise_on_the_resting_baseline_denies_the_hold(raising_check) -> None:
    ctx = _hold_ctx(raising_check)

    assert _avoid_forces(ctx, [("Hold", True)])
    assert not _hold_allowed(ctx, ("Hold", True))


def test_an_opaque_read_of_an_absent_tag_judges_a_hold_as_its_condition_does() -> None:
    """The baseline has no Danger: a condition reads that as violated, an index read raises."""
    condition = _hold_ctx(_compile_avoid(Bool("Danger")))
    opaque = _hold_ctx(_compile_avoid(lambda s: s["Danger"]))

    assert _hold_allowed(condition, ("Hold", True)) is False
    assert _hold_allowed(opaque, ("Hold", True)) is False


def test_a_check_that_answers_on_the_resting_baseline_judges_the_hold() -> None:
    reads_with_get = _hold_ctx(_compile_avoid(lambda s: bool(s.get("Danger"))))
    reads_the_input = _hold_ctx(_compile_avoid(lambda s: s["Hold"]))

    assert _hold_allowed(reads_with_get, ("Hold", True)) is True
    assert _hold_allowed(reads_the_input, ("Hold", False)) is True
    assert _hold_allowed(reads_the_input, ("Hold", True)) is False
