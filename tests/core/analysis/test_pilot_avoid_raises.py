"""An ``avoid=`` check that raises did not say the state is clear.

A check that raises on a real state reaches the caller as a named error.  A real state is
the one how() starts from, or a scan a trial really runs, failed trials included.  Whichever
gate met it, it never reads as a pass and never as a verdict.

A raise on any other state says nothing about the route and denies nothing: the resting
baseline, a hold laid over a state, a route's demands, the state behind the start when a plan
rewinds and re-runs the entry scan.  That member is not violated, the others keep their answers,
and the gates judge the scans those choices lead to.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import (
    PLC,
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    copy,
    latch,
    on_delay,
    out,
    reset,
    system,
)
from pyrung.core import AvoidCheckError
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import api, budget, pilot_events, pilot_how
from pyrung.core.analysis.pilot import avoid as avoid_module
from pyrung.core.analysis.pilot.avoid import (
    _avoid_forces,
    _avoid_scan_names,
    _avoid_violations,
    _avoid_weighed_after_clear,
    _avoid_weighed_names,
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
from tests.fixtures import pilot_scan_zero_sequence_route as scan_zero_route
from tests.fixtures.pilot_alarm_presets import aborted_on_first_scan, alarmed_at_start


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
    assert "can be compared" in str(raised.value)


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
        _avoid_scan_names(raising_check, {"Danger": True})


def test_a_hold_laid_over_a_state_is_only_weighed(raising_check) -> None:
    ctx = SimpleNamespace(avoid_pred=raising_check, resting={})

    assert _avoid_violations(ctx, [("Fast", True)], {"Fast": False}) == ()
    assert _avoid_forces(ctx, [("Fast", True)], {"Fast": False}) is False


def test_a_routes_demands_are_only_weighed(raising_check) -> None:
    assert route_forces([], {"Danger": True}, raising_check) is False
    assert route_forced_names([], {"Danger": True}, raising_check) == ()


def test_a_weighed_member_that_raises_leaves_the_others_their_answers() -> None:
    avoid = _compile_avoid((_raises_at_once, Bool("Danger")))
    ctx = SimpleNamespace(avoid_pred=avoid, resting={})

    assert _avoid_weighed_names(avoid, {"Danger": True}) == ("Danger",)
    assert _avoid_weighed_names(avoid, {"Danger": False}) == ()
    assert _avoid_violations(ctx, [("Danger", True)], {}) == ("Danger",)
    assert route_forces([], {"Danger": True}, avoid) is True
    assert route_forced_names([], {"Danger": True}, avoid) == ("Danger",)


def test_a_real_state_is_not_weighed_when_another_member_answers() -> None:
    with pytest.raises(_AvoidCheckRaised):
        _avoid_scan_names(_compile_avoid((Bool("Danger"), _raises_at_once)), {"Danger": True})


def test_after_clear_gate_raises_on_the_scan(raising_check) -> None:
    with pytest.raises(_AvoidCheckRaised):
        _avoid_names_after_clear(raising_check, {"Danger": False}, {"Danger": True})


def test_after_clear_gate_only_weighs_the_start() -> None:
    def _raises_without_danger(snapshot: dict) -> bool:
        return snapshot["Danger"]

    for check in (_member_form(_raises_without_danger), _raises_without_danger):
        assert _avoid_names_after_clear(check, {}, {"Danger": False}) == ()
        assert len(_avoid_names_after_clear(check, {}, {"Danger": True})) == 1


def test_a_snapshot_no_scan_had_is_weighed_against_the_start() -> None:
    avoid = _compile_avoid((_raises_at_once, Bool("Danger")))

    assert _avoid_weighed_after_clear(avoid, {"Danger": False}, {"Danger": True}) == ("Danger",)
    assert _avoid_weighed_after_clear(avoid, {"Danger": True}, {"Danger": True}) == ()
    assert _avoid_weighed_after_clear(_raises_at_once, {}, {"Danger": True}) == ()


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
            _avoid_scan_names(_member_form(_raises_at_once), {})
        except Exception:
            pass


def _seen_coast_program():
    Tick = Bool("CoastTick", external=True)
    Out = Bool("CoastOut")
    with Program(strict=False) as prog:
        with Rung(Tick):
            out(Out)
    return prog


def test_arming_a_coast_only_weighs_where_it_stands() -> None:
    """It may stand behind how()'s start.  The member stays armed for the scans it runs."""
    session = CoastSession(PLC(_seen_coast_program(), dt=0.1))

    session.arm_avoid(_member_form(_raises_at_once))

    with pytest.raises(_AvoidCheckRaised):
        session._avoid_triggers[0].predicate(SimpleNamespace(tags={}))


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


def test_a_raise_on_the_baseline_denies_no_hold(raising_check) -> None:
    ctx = _hold_ctx(raising_check)

    assert _hold_allowed(ctx, ("Hold", True)) is True
    assert _avoid_forces(ctx, [("Hold", True)]) is False


def test_a_read_of_a_tag_the_baseline_lacks_denies_no_hold() -> None:
    """The baseline has no Danger, and a real state does: the scans get the answer."""
    assert _hold_allowed(_hold_ctx(_compile_avoid(lambda s: s["Danger"])), ("Hold", True)) is True
    assert _hold_allowed(_hold_ctx(_compile_avoid(lambda s: s["Ghost"])), ("Hold", True)) is True


def test_a_condition_on_a_tag_the_baseline_lacks_denies_no_hold() -> None:
    """The same check as the callable above, written as a condition, gets the same answer."""
    assert _hold_allowed(_hold_ctx(_compile_avoid(Bool("Danger"))), ("Hold", True)) is True


def test_a_condition_on_what_the_baseline_has_still_refuses_the_hold() -> None:
    assert _hold_allowed(_hold_ctx(_compile_avoid(Bool("Hold"))), ("Hold", True)) is False


def test_a_condition_on_the_hold_and_a_tag_the_baseline_lacks_is_left_to_the_scans() -> None:
    both = And(Bool("Hold"), Bool("Danger"))

    assert _hold_allowed(_hold_ctx(_compile_avoid(both)), ("Hold", True)) is True


def test_a_check_that_answers_on_the_resting_baseline_keeps_that_answer() -> None:
    reads_with_get = _hold_ctx(_compile_avoid(lambda s: bool(s.get("Danger"))))
    reads_the_input = _hold_ctx(_compile_avoid(lambda s: s["Hold"]))
    no_danger_on_it = _hold_ctx(_compile_avoid(lambda s: s.get("Danger") is None))

    assert _hold_allowed(reads_with_get, ("Hold", True)) is True
    assert _hold_allowed(reads_the_input, ("Hold", False)) is True
    assert _hold_allowed(reads_the_input, ("Hold", True)) is False
    assert _hold_allowed(no_danger_on_it, ("Hold", True)) is False


def test_a_condition_with_a_program_owned_side_denies_no_hold() -> None:
    """``Input > Limit``: the baseline has no Limit, and the compare raises on what it finds."""
    ctx = _hold_ctx(_compile_avoid(Int("Input") > Int("Limit")), {"Input": 0})
    with pytest.raises(_AvoidCheckRaised):
        _avoid_scan_names(ctx.avoid_pred, {"Input": 5})

    assert _hold_allowed(ctx, ("Input", 5)) is True


def test_a_hold_is_asked_about_on_the_baseline_alone() -> None:
    asked = []

    def check(snapshot: dict) -> bool:
        asked.append(dict(snapshot))
        return snapshot["S"] == 99

    ctx = _hold_ctx(_compile_avoid(check), {"Speed": 0, "Sensor": False})

    assert _hold_allowed(ctx, ("Sensor", True)) is True
    assert asked == [{"Speed": 0, "Sensor": True}]


def test_each_member_of_a_union_is_judged_on_its_own() -> None:
    """A member the baseline trips stays tripped when another raises on it."""
    avoid = _compile_avoid((Bool("Hold"), lambda s: s["Level"] > 100))
    ctx = _hold_ctx(avoid)

    assert _avoid_violations(ctx, [("Hold", True)]) == ("Hold",)


def test_a_bare_callable_that_raises_on_the_baseline_denies_no_hold() -> None:
    ctx = _hold_ctx(lambda s: s["Level"] > 3)

    assert _hold_allowed(ctx, ("Hold", True)) is True
    assert _avoid_violations(ctx, [("Hold", True)]) == ()


# ---------------------------------------------------------------------------
# A search that earns its route with corrective holds, through how()
# ---------------------------------------------------------------------------


def _dwell_program(limit_from=None, limit_to=None, watching=None):
    """A sensor must be held on, then off, or a watchdog takes state 6 to 8 instead of 7."""
    sensor = Bool("HoldSensor", external=True)
    stable_on = Timer.clone("HoldStableOn")
    stable_off = Timer.clone("HoldStableOff")
    on_wd = Timer.clone("HoldOnWD")
    off_wd = Timer.clone("HoldOffWD")
    run = Timer.clone("HoldRun")
    settle = Timer.clone("HoldSettle")
    state = Int("HoldState", default=6)
    limit = Int("HoldLimit", default=0 if limit_from is None else limit_from)
    with Program(strict=False) as prog:
        with Rung(state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with Rung(state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with Rung(state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with Rung(state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with Rung(state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with Rung(state == 6):
            on_delay(run, 250, "ms")
        with Rung(state == 6, run.Done):
            copy(7, state)
        with Rung(state == 6):
            on_delay(settle, 20, "ms")
        with Rung(settle.Done):
            copy(limit_to if limit_to is not None else limit_from or 0, limit)
        if watching is not None:
            with Rung(watching > 100):
                out(Bool("WatchingHigh"))
    return prog, state, sensor, limit


def _drive(prog, target, avoid):
    plc = PLC(prog, dt=0.010)
    plc.step()
    return plc.how(target, avoid=avoid, max_scans=2000)


def _holds(plan) -> int:
    return sum(len(step.rungs) for step in plan.journal)


def _avoided_scans(plan, avoided) -> list[int]:
    """Scans of the plan in the avoided state.  The fork has them all; a replay keeps its last."""
    return [
        state.scan_id for state in plan.fork.history.latest(100000) if avoided(dict(state.tags))
    ]


def test_the_route_needs_its_corrective_holds() -> None:
    prog, state, *_ = _dwell_program()

    plan = _drive(prog, state == 7, None)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 24
    assert _holds(plan) > 0


@pytest.mark.parametrize(
    "healthy",
    [
        lambda s: s.get("HoldState") == 9999,
        lambda s: s["HoldState"] == 9999,
        lambda s: s.get("HoldState") > 9999,
    ],
    ids=["get", "index", "get-compare"],
)
def test_a_healthy_check_keeps_a_route_with_holds(healthy) -> None:
    """Each reads a program tag, which every real state has and the baseline does not."""
    prog, state, *_ = _dwell_program()

    plan = _drive(prog, state == 7, healthy)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 24
    assert _holds(plan) > 0


@pytest.mark.parametrize("form", ["condition", "opaque"])
@pytest.mark.parametrize(
    ("limit_from", "limit_to"), [(9999, 9999), (0, 100)], ids=["high", "rises-before-the-hold"]
)
def test_input_over_a_program_owned_limit_keeps_a_route_with_holds(
    form, limit_from, limit_to
) -> None:
    """The limit is judged where the search is: 100 by the time a hold is asked for."""
    prog, state, sensor, limit = _dwell_program(limit_from, limit_to)
    avoid = sensor > limit if form == "condition" else (lambda s: s["HoldSensor"] > s["HoldLimit"])

    plan = _drive(prog, state == 7, avoid)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 24
    assert _holds(plan) > 0


def test_input_over_a_limit_the_hold_would_cross_goes_no_further() -> None:
    prog, state, sensor, limit = _dwell_program(0, 0)
    assert _drive(prog, state == 7, None).status is PlanStatus.REACHED

    plan = _drive(prog, state == 7, sensor > limit)

    assert plan.status is PlanStatus.STOPPED


def _armed_dwell(release_needed: bool):
    """Start latches Armed, and the dwell runs under Armed, or under Ready once Start is off."""
    start = Bool("Start", external=True)
    sensor = Bool("Sensor", external=True)
    armed = Bool("Armed")
    ready = Bool("Ready")
    lim = Int("Lim")
    stable_on = Timer.clone("StableOn")
    stable_off = Timer.clone("StableOff")
    on_wd = Timer.clone("OnWD")
    off_wd = Timer.clone("OffWD")
    run = Timer.clone("RunT")
    state = Int("St", default=6)
    gate = ready if release_needed else armed
    with Program(strict=False) as prog:
        with Rung():
            copy(5, lim)
        with Rung(start):
            latch(armed)
        with Rung(armed, ~start):
            latch(ready)
        with Rung(gate, state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with Rung(gate, state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with Rung(gate, state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with Rung(gate, state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with Rung(gate, state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with Rung(gate, state == 6):
            on_delay(run, 250, "ms")
        with Rung(gate, state == 6, run.Done):
            copy(7, state)
    return prog, state


@pytest.mark.parametrize(
    ("release_needed", "avoided"),
    [
        (False, lambda s: s["OffWD_Done"] and s["Start"] and s["Sensor"]),
        (False, lambda s: s["OffWD_Done"] and s["Sensor"]),
        (True, lambda s: s["OffWD_Done"] and s["Sensor"]),
    ],
    ids=["done-start-sensor", "done-sensor", "done-sensor-release"],
)
def test_a_check_true_only_at_the_failed_landing_keeps_the_route(release_needed, avoided) -> None:
    """A failed attempt ends with the watchdog done.  The corrected route never gets there."""
    prog, state = _armed_dwell(release_needed)

    plan = _drive(prog, state == 7, avoided)

    assert plan.status is PlanStatus.REACHED
    assert _holds(plan) > 0
    assert not _avoided_scans(plan, avoided)


def test_a_check_no_scan_of_the_route_can_clear_stops_without_spending_the_budget() -> None:
    """Armed needs Start held while the sensor is held: the action gate says so at once."""
    prog, state = _armed_dwell(False)
    assert _drive(prog, state == 7, None).status is PlanStatus.REACHED

    plan = _drive(prog, state == 7, lambda s: s["Lim"] > 0 and s["Start"] and s["Sensor"])

    assert plan.status is PlanStatus.STOPPED
    assert "budget" not in str(plan.reason).lower()


def _speed_dwell(start: dict):
    """The dwell on its own, with Speed an input the route does not need."""
    speed = Int("Speed", external=True)
    prog, state, sensor, limit = _dwell_program(0, 0, watching=speed)
    plc = PLC(prog, dt=0.010)
    plc.patch(start)
    plc.step()
    return plc, state, sensor, speed, limit


def test_a_hold_that_really_forces_the_check_stops() -> None:
    """Speed really is 5, so the sensor hold the route needs is the avoided state."""
    plc, state, *_ = _speed_dwell({"Speed": 5})
    avoid = lambda s: s["HoldState"] == 6 and s["Speed"] > 3 and s["HoldSensor"]  # noqa: E731
    assert plc.how(state == 7, max_scans=2000).status is PlanStatus.REACHED
    at_rest, *_ = _speed_dwell({"Speed": 0})
    assert at_rest.how(state == 7, avoid=avoid, max_scans=2000).status is PlanStatus.REACHED

    plan = plc.how(state == 7, avoid=avoid, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED


def test_a_condition_a_hold_really_forces_stops() -> None:
    plc, state, sensor, speed, limit = _speed_dwell({"Speed": 5})
    assert plc.how(state == 7, max_scans=2000).status is PlanStatus.REACHED

    plan = plc.how(state == 7, avoid=And(sensor, speed > limit), max_scans=2000)

    assert plan.status is PlanStatus.STOPPED


def test_releasing_an_input_that_really_is_on_stops() -> None:
    plc, state, *_ = _speed_dwell({"HoldSensor": True})
    assert plc.how(state == 7, max_scans=2000).status is PlanStatus.REACHED

    plan = plc.how(
        state == 7,
        avoid=lambda s: s["HoldState"] == 6 and not s["HoldSensor"],
        max_scans=2000,
    )

    assert plan.status is PlanStatus.STOPPED


def test_the_form_of_a_check_does_not_change_the_answer() -> None:
    """The baseline has no HoldState: a condition and a callable both have no answer for it."""
    prog, state, *_ = _dwell_program()

    assert _drive(prog, state == 7, None).status is PlanStatus.REACHED
    assert _drive(prog, state == 7, lambda s: s["HoldState"] == 9999).status is PlanStatus.REACHED
    assert _drive(prog, state == 7, state == 9999).status is PlanStatus.REACHED


def test_a_check_is_not_asked_about_a_real_input_put_back_at_rest() -> None:
    """RecipeShown copies Recipe every scan, so no real state has them apart.

    Putting Recipe back at 0 under a real RecipeShown of 5 would be a state that never
    happens, and raise for it.
    """
    sensor = Bool("HoldSensor", external=True)
    recipe = Int("Recipe", external=True)
    shown = Int("RecipeShown")
    stable_on = Timer.clone("HoldStableOn")
    stable_off = Timer.clone("HoldStableOff")
    on_wd = Timer.clone("HoldOnWD")
    off_wd = Timer.clone("HoldOffWD")
    run = Timer.clone("HoldRun")
    state = Int("HoldState", default=6)
    with Program(strict=False) as prog:
        with Rung():
            copy(recipe, shown)
        with Rung(state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with Rung(state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with Rung(state == 6):
            on_delay(on_wd, 70, "ms").reset(stable_off.Done)
        with Rung(state == 6):
            on_delay(off_wd, 70, "ms").reset(stable_on.Done)
        with Rung(state == 6, Or(on_wd.Done, off_wd.Done)):
            copy(8, state)
        with Rung(state == 6):
            on_delay(run, 250, "ms")
        with Rung(state == 6, run.Done):
            copy(7, state)

    def _in_step(snapshot: dict) -> bool:
        if snapshot["Recipe"] == snapshot["RecipeShown"]:
            return False
        raise ValueError("out of step")

    plc = PLC(prog, dt=0.010)
    plc.patch({"Recipe": 5})
    plc.step()

    plan = plc.how(state == 7, avoid=_in_step, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 24
    assert _holds(plan) > 0


@pytest.mark.parametrize(
    "only_with_the_hold_applied",
    [
        lambda s: s["HoldState"] == 8 and s["HoldSensor"] and s["HoldOffWD_Done"],
        lambda s: s["HoldState"] == 8 and not s["HoldSensor"] and s["HoldOnWD_Done"],
    ],
    ids=["hold-on", "hold-off"],
)
def test_a_check_raising_only_on_a_landing_with_a_hold_applied_keeps_the_route(
    only_with_the_hold_applied,
) -> None:
    """A failed attempt lands in state 8 on one watchdog, and no scan has it with the other side."""
    prog, state, *_ = _dwell_program()

    def _raises(snapshot: dict) -> bool:
        if only_with_the_hold_applied(snapshot):
            raise ValueError("only with the hold applied to the landing")
        return False

    plan = _drive(prog, state == 7, _raises)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 24
    assert _holds(plan) > 0
    assert not _avoided_scans(plan, only_with_the_hold_applied)


def test_a_check_raising_on_a_scan_of_a_failed_trial_reaches_the_caller() -> None:
    """The first try with the sensor held really scans state 8 with it on; the plan does not."""
    prog, state, *_ = _dwell_program()

    def _raises_in_eight_held(snapshot: dict) -> bool:
        if snapshot["HoldState"] == 8 and snapshot["HoldSensor"]:
            raise ValueError("on a scan the search ran")
        return False

    with pytest.raises(AvoidCheckError, match="ValueError"):
        _drive(prog, state == 7, _raises_in_eight_held)


_SYSTEM_TAGS = ("sys.", "rtc.", "fault.", "_")


def _scan_values(tags) -> tuple:
    return tuple(sorted((k, repr(v)) for k, v in tags.items() if not k.startswith(_SYSTEM_TAGS)))


def test_a_check_raising_on_any_one_scan_of_the_route_reaches_the_caller() -> None:
    """Every scan the holds take the plan through meets a gate, whichever scan it is."""
    prog, state, *_ = _dwell_program()
    route = _drive(prog, state == 7, None).fork.history.latest(100000)
    scans = {_scan_values(scan.tags) for scan in route}
    assert len(scans) == len(route) == 26

    start = PLC(prog, dt=0.010)
    start.step()
    for scan in route:
        if scan.scan_id < start.state.scan_id:
            continue
        values = _scan_values(scan.tags)

        def _raises_on_this_scan(snapshot: dict, _values: tuple = values) -> bool:
            if _scan_values(snapshot) == _values:
                raise ValueError("on this scan alone")
            return False

        with pytest.raises(AvoidCheckError, match="ValueError"):
            _drive(prog, state == 7, _raises_on_this_scan)


def test_a_check_raising_only_with_a_hold_on_reaches_the_caller() -> None:
    prog, state, *_ = _dwell_program()

    def _raises_with_the_sensor_held(snapshot: dict) -> bool:
        if snapshot["HoldSensor"] and snapshot["HoldState"] == 6:
            raise ValueError("boom with the hold on")
        return False

    with pytest.raises(AvoidCheckError, match="ValueError"):
        _drive(prog, state == 7, _raises_with_the_sensor_held)


# ---------------------------------------------------------------------------
# The scan before how() starts is history, not a state a plan enters
# ---------------------------------------------------------------------------


def _start_then_wait():
    """Start runs a timer that latches Target.  Phase is 0 before the first scan, then 1."""
    start = Bool("Start", external=True)
    delay = Timer.clone("Delay")
    target = Bool("Target")
    phase = Int("Phase")
    rate = Int("Rate", external=True)
    with Program(strict=False) as prog:
        with Rung():
            copy(1, phase)
        with Rung(rate > 100):
            out(Bool("RateHigh"))
        with Rung(start):
            on_delay(delay, 100, "ms")
        with Rung(delay.Done):
            latch(target)
    return prog, target


def _scanned(prog, scans: int, patch_before_last: dict | None = None) -> PLC:
    plc = PLC(prog, dt=0.010)
    for scan in range(scans):
        if patch_before_last and scan == scans - 1:
            plc.patch(patch_before_last)
        plc.step()
    return plc


@pytest.mark.parametrize(
    ("healthy", "scans", "patch"),
    [
        (lambda s: s["fault.out_of_range"], 1, None),
        (lambda s: 1 / s["Phase"] > 5, 1, None),
        (lambda s: 1 / s["Rate"] > 5, 5, {"Rate": 2}),
    ],
    ids=["system-tag-after-one-scan", "written-by-the-first-scan", "patched-before-the-last-scan"],
)
def test_a_check_raising_only_on_the_scan_before_the_start_keeps_the_route(
    healthy, scans, patch
) -> None:
    """Every state from where how() starts has what the check reads; the scan before does not."""
    prog, target = _start_then_wait()
    plc = _scanned(prog, scans, patch)
    healthy(dict(plc.state.tags))

    plan = plc.how(target, avoid=healthy, max_scans=2000)
    without = _scanned(prog, scans, patch).how(target, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert (plan.total_scans, _holds(plan)) == (without.total_scans, _holds(without))
    assert plan.total_scans == 10


def test_a_check_raising_before_the_start_and_on_the_route_reaches_the_caller() -> None:
    """The scan before the start excuses that scan only."""
    prog, target = _start_then_wait()

    def _raises_without_phase_or_at_done(snapshot: dict) -> bool:
        return 1 / snapshot["Phase"] > 5 or 1 / (1 - snapshot["Target"]) > 5

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError"):
        _scanned(prog, 1).how(target, avoid=_raises_without_phase_or_at_done, max_scans=2000)


def test_a_goal_already_true_keeps_a_check_raising_only_on_the_scan_before() -> None:
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung():
            out(target)
        with Rung(go):
            out(Bool("Other"))

    plan = _scanned(prog, 1).how(target, avoid=lambda s: s["fault.out_of_range"], max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 0


def test_a_typo_after_one_scan_still_reaches_the_caller() -> None:
    prog, target = _start_then_wait()

    with pytest.raises(AvoidCheckError, match="KeyError: 'Typo'"):
        _scanned(prog, 1).how(target, avoid=lambda s: s["Typo"], max_scans=2000)


# ---------------------------------------------------------------------------
# A check reading a tag that is not in the program
# ---------------------------------------------------------------------------


def _route_with_holds():
    prog, state, *_ = _dwell_program()
    return prog, state == 7, 0.010


def _route_with_no_hold():
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(go):
            out(target)
    return prog, target, 0.1


def _route_by_waiting():
    delay = Timer.clone("Delay")
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung():
            on_delay(delay, 100, "ms")
        with Rung(delay.Done):
            out(target)
    return prog, target, 0.010


def _goal_true_at_the_start():
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung():
            out(target)
        with Rung(go):
            out(Bool("Other"))
    return prog, target, 0.1


@pytest.mark.parametrize(
    "program",
    [_route_with_holds, _route_with_no_hold, _route_by_waiting, _goal_true_at_the_start],
    ids=["holds", "no-hold", "waiting", "goal-true"],
)
@pytest.mark.parametrize(
    "typo",
    [lambda s: s["Typo"], lambda s: s["Typo"] > 3, (lambda s: s["Typo"],)],
    ids=["index", "compare", "union"],
)
def test_a_tag_not_in_the_program_stops_with_the_error(program, typo) -> None:
    prog, target, dt = program()
    plc = PLC(prog, dt=dt)
    plc.step()
    before = plc.current_state

    with pytest.raises(AvoidCheckError, match="KeyError: 'Typo'") as raised:
        plc.how(target, avoid=typo, max_scans=2000)

    assert isinstance(raised.value.__cause__, KeyError)
    assert plc.current_state is before


# ---------------------------------------------------------------------------
# A hold laid over a state is no scan
# ---------------------------------------------------------------------------


def _same_plan(plan, without) -> None:
    assert plan.status is PlanStatus.REACHED
    assert [str(step) for step in plan.journal] == [str(step) for step in without.journal]
    assert plan.total_scans == without.total_scans


def _rate_on_speed():
    """Speed above 3 must be applied for a 1 s timer that drives Target."""
    speed = Int("Speed", external=True)
    run = Timer.clone("Rn")
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(speed > 3):
            on_delay(run, 1000, "ms")
        with Rung(run.Done):
            out(target)
    return prog, target


def test_a_rate_check_raising_only_on_the_hold_laid_over_the_state_keeps_the_route(
    monkeypatch,
) -> None:
    """Acc is 0 only until the first scan with Speed on has run its timer."""
    prog, target = _rate_on_speed()

    def rate(s):
        return 1000 / s["Rn_Acc"] > 1e9 if s["Speed"] > 3 else False

    # The drive, with the temporary logic it laid over the state.
    monkeypatch.setattr(api._trim, "trim_holds", lambda plan, *_args: plan)
    without = _scanned(prog, 1).how(target, max_scans=2000)
    assert (without.total_scans, _holds(without)) == (100, 2)
    plan = _scanned(prog, 1).how(target, avoid=rate, max_scans=2000)
    _same_plan(plan, without)
    monkeypatch.undo()

    # A Set in its place runs the same, with the check never raising on the way.
    without = _scanned(prog, 1).how(target, max_scans=2000)
    assert (without.total_scans, _holds(without)) == (100, 0)
    plan = _scanned(prog, 1).how(target, avoid=rate, max_scans=2000)
    _same_plan(plan, without)


def test_a_rate_check_raising_on_a_scan_with_the_speed_on_reaches_the_caller() -> None:
    prog, target = _rate_on_speed()

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError"):
        _scanned(prog, 1).how(
            target,
            avoid=lambda s: 1 / (s["Rn_Acc"] - 10) > 1e9 if s["Speed"] > 3 else False,
            max_scans=2000,
        )


def _armed_dwell_target():
    prog, state, *_ = _armed_dwell(True)
    return prog, state == 7


def _dwell_target():
    prog, state, *_ = _dwell_program()
    return prog, state == 7


def _entry_preset():
    """The first scan latches X, and a timer with no preset takes it away on the next."""
    x = Bool("HX")
    target = Bool("HT")
    wait = Timer.clone("HW")
    preset = Int("HP")
    first_scan = system.sys.first_scan
    with Program(strict=False) as prog:
        with Rung(first_scan):
            latch(x)
        with Rung():
            on_delay(wait, preset)
        with Rung(wait.Done):
            reset(x)
        with Rung(x):
            latch(target)
        with Rung(first_scan):
            latch(x)
    return prog, target


def _entry_step():
    """As above, with the entry scan's work on a step that a later rung puts back."""
    step = Int("HS")
    target = Bool("HT")
    wait = Timer.clone("HW")
    preset = Int("HP")
    first_scan = system.sys.first_scan
    with Program(strict=False) as prog:
        with Rung(first_scan):
            copy(10, step)
        with Rung():
            on_delay(wait, preset)
        with Rung(first_scan, wait.Done):
            copy(5, step)
        with Rung(step == 10, first_scan):
            latch(target)
        with Rung(first_scan, step == 5):
            copy(10, step)
    return prog, target


_SKIP_IN_KEY = ("sys.", "rtc.", "fault.", "_")


def _state_key(tags) -> tuple:
    return tuple(sorted((k, repr(v)) for k, v in tags.items() if not k.startswith(_SKIP_IN_KEY)))


@pytest.mark.parametrize(
    "program",
    [_rate_on_speed, _dwell_target, _armed_dwell_target],
    ids=["rate", "dwell", "armed"],
)
def test_a_check_raising_on_a_hold_laid_over_no_scan_keeps_the_route(program, monkeypatch) -> None:
    """Every overlay the action gates weigh that the plan's scans do not have, one at a time."""
    prog, target = program()
    without = _scanned(prog, 1).how(target, max_scans=2000)
    assert without.status is PlanStatus.REACHED
    on_the_plan = {_state_key(state.tags) for state in without.fork.history.latest(100000)}

    weighed = avoid_module._avoid_weighed_names
    overlays: dict = {}

    def _recording(avoid, snap):
        overlays.setdefault(_state_key(snap), None)
        return weighed(avoid, snap)

    with monkeypatch.context() as patched:
        patched.setattr(avoid_module, "_avoid_weighed_names", _recording)
        _scanned(prog, 1).how(target, avoid=lambda s: False, max_scans=2000)

    off_the_plan = [key for key in overlays if key not in on_the_plan]
    assert off_the_plan

    for key in off_the_plan:

        def _raises_on_this_overlay(snapshot: dict, _key: tuple = key) -> bool:
            if _state_key(snapshot) == _key:
                raise ValueError("on this overlay alone")
            return False

        plan = _scanned(prog, 1).how(target, avoid=_raises_on_this_overlay, max_scans=2000)

        _same_plan(plan, without)


# ---------------------------------------------------------------------------
# The state a restart lands on has run no scan
# ---------------------------------------------------------------------------

_FAULT = "fault.out_of_range"


def _same_answer(plan, without) -> None:
    assert plan.status is without.status
    assert [str(step) for step in plan.journal] == [str(step) for step in without.journal]
    assert plan.total_scans == without.total_scans
    assert plan.restart.lines() == without.restart.lines()


@pytest.mark.parametrize(
    ("program", "status"),
    [(_entry_preset, PlanStatus.REACHED), (_entry_step, PlanStatus.STOPPED)],
    ids=["preset", "step"],
)
@pytest.mark.parametrize(
    "check",
    [
        lambda s: s[_FAULT],
        lambda s: s.get(_FAULT, False),
        lambda s: s.get(_FAULT) is None and s["Typo"],
    ],
    ids=["system-tag", "with-default", "typo-only-before-the-first-scan"],
)
def test_a_raise_on_the_state_a_restart_lands_on_changes_no_answer(program, status, check) -> None:
    prog, target = program()
    without = _scanned(prog, 1).how(target, max_scans=2000)
    assert without.status is status
    assert without.restart is not None

    plan = _scanned(prog, 1).how(target, avoid=check, max_scans=2000)

    _same_answer(plan, without)


@pytest.mark.parametrize("program", [_entry_preset, _entry_step], ids=["preset", "step"])
def test_a_typo_on_an_entry_scan_route_reaches_the_caller(program) -> None:
    prog, target = program()

    with pytest.raises(AvoidCheckError, match="KeyError: 'Typo'"):
        _scanned(prog, 1).how(target, avoid=lambda s: s["Typo"], max_scans=2000)


def _route_fixture(logic):
    return lambda: _scanned(logic, 1)


def _aborted_at_start():
    return _scanned(aborted_on_first_scan.logic, 1)


def _alarmed_at_start():
    plc = PLC(alarmed_at_start.logic, dt=0.010)
    plc.force(alarmed_at_start.Reset, True)
    plc.force(alarmed_at_start.AtTarget, True)
    plc.step()
    return plc


_ENTRY_REQUIREMENTS = {
    "route-adjacent": (
        _route_fixture(scan_zero_route.adjacent_logic),
        scan_zero_route.SequenceStep == 81,
        scan_zero_route.SequenceStep.name,
        PlanStatus.STOPPED,
    ),
    "route-field": (
        _route_fixture(scan_zero_route.logic),
        scan_zero_route.SequenceStep == 81,
        scan_zero_route.SequenceStep.name,
        PlanStatus.STOPPED,
    ),
    "aborted": (
        _aborted_at_start,
        aborted_on_first_scan.ProcessStep == aborted_on_first_scan.AT_TARGET,
        aborted_on_first_scan.ProcessStep.name,
        PlanStatus.STOPPED,
    ),
    "alarmed": (
        _alarmed_at_start,
        alarmed_at_start.ProcessStep == alarmed_at_start.COMPLETE,
        alarmed_at_start.ProcessStep.name,
        PlanStatus.REACHED,
    ),
}


@pytest.mark.parametrize("row", sorted(_ENTRY_REQUIREMENTS))
def test_a_system_tag_a_restart_lacks_keeps_the_answer(row) -> None:
    make, target, _step, status = _ENTRY_REQUIREMENTS[row]
    without = make().how(target, max_scans=2000)
    assert without.status is status

    plan = make().how(target, avoid=lambda s: s[_FAULT], max_scans=2000)

    _same_answer(plan, without)


def test_a_division_by_a_step_cleared_for_the_restart_keeps_the_answer() -> None:
    """The step is 0 only on the state the restart lands on, and no scan has that."""
    make, target, step, _status = _ENTRY_REQUIREMENTS["aborted"]
    without = make().how(target, max_scans=2000)
    assert step in without.restart.clear

    plan = make().how(target, avoid=lambda s: 1 / s[step] > 5, max_scans=2000)

    _same_answer(plan, without)


def test_a_division_by_a_step_the_session_really_scans_reaches_the_caller() -> None:
    """A trial from the session itself puts the step at 0 on a real scan, before any restart."""
    make, target, step, _status = _ENTRY_REQUIREMENTS["route-field"]

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError"):
        make().how(target, avoid=lambda s: 1 / s[step] > 5, max_scans=2000)


@pytest.mark.parametrize("row", ["route-adjacent", "route-field"])
def test_a_raise_on_a_scan_of_a_clear_try_loses_the_advice_not_the_answer(row) -> None:
    """Only a try with tags cleared by hand gets to 81, and how() has its answer by then."""
    make, target, step, _answer = _ENTRY_REQUIREMENTS[row]

    plan = make().how(target, avoid=lambda s: 1 / (s[step] - 81) > 5, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart.clear is None


def test_a_raise_on_a_scan_after_the_restart_costs_the_restart_and_not_the_stop() -> None:
    prog, target = _entry_preset()

    plan = _scanned(prog, 1).how(target, avoid=lambda s: 1 / (s["HP"] - 11) > 5, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart.lines() == [
        "Restart: no answer, on a scan after it avoid check 'avoid' raised "
        "ZeroDivisionError: division by zero."
    ]


@pytest.mark.parametrize("row", sorted(_ENTRY_REQUIREMENTS))
def test_a_typo_on_an_entry_requirement_route_reaches_the_caller(row) -> None:
    make, target, _step, _answer = _ENTRY_REQUIREMENTS[row]

    with pytest.raises(AvoidCheckError, match="KeyError: 'Typo'"):
        make().how(target, avoid=lambda s: s["Typo"], max_scans=2000)


# ---------------------------------------------------------------------------
# A raising member does not silence the others
# ---------------------------------------------------------------------------


def _raises_on_fast_before_its_scan(snapshot: dict) -> bool:
    if snapshot["Fast"] and not snapshot["Danger"]:
        raise ValueError("Fast on with no Danger: no scan has that")
    return False


def test_a_member_raising_on_an_overlay_leaves_the_route_to_the_others() -> None:
    prog, target, *_ = _two_ways()
    plc = _scanned(prog, 1)

    plan = plc.how(target, avoid=_raises_on_fast_before_its_scan, max_scans=200)

    assert plan.status is PlanStatus.REACHED
    assert plan.fork.state.tags["Danger"] is True


def test_a_member_that_answers_keeps_its_answer_beside_one_that_raises() -> None:
    prog, target, danger, _level = _two_ways()
    plc = _scanned(prog, 1)

    plan = plc.how(target, avoid=(_raises_on_fast_before_its_scan, danger), max_scans=200)

    assert plan.status is PlanStatus.REACHED
    assert plan.fork.state.tags["Danger"] is False


# ---------------------------------------------------------------------------
# The settled gate on a trial with no pulse
# ---------------------------------------------------------------------------


def test_a_raise_on_the_landing_of_a_wait_reaches_the_caller() -> None:
    """True where the wait starts, so nothing else asks the member again: only the landing does."""
    prog, target, dt = _route_by_waiting()

    def _clear_on_landing(snapshot: dict) -> bool:
        return not snapshot["Delay_Done"]

    def _raises_on_landing(snapshot: dict) -> bool:
        return 1 / 0 > 0 if snapshot["Delay_Done"] else True

    def _run(avoid):
        plc = PLC(prog, dt=dt)
        plc.step()
        return plc.how(target, avoid=avoid, max_scans=2000)

    without = _run(None)
    assert without.status is PlanStatus.REACHED
    assert not any(step.inputs for step in without.journal)
    _same_plan(_run(_clear_on_landing), without)

    with pytest.raises(AvoidCheckError, match="ZeroDivisionError"):
        _run(_raises_on_landing)
