"""An ``avoid=`` check that raises did not say the state is clear.

A check that raises on a real state reaches the caller as a named error, whichever
gate met it: the route gate before the drive, the action gate, the scan gate, a
trial coast, a terminal reason.  It never reads as a pass and never as a verdict.
The resting baseline holds the steerable inputs and nothing else, so a check that
raises there is asked again with real values, and a raise there is the same error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import PLC, And, Bool, Int, Or, Program, Rung, Timer, copy, on_delay, out
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


REAL = {"Hold": True, "Danger": False, "Level": 5, "Limit": 10}


def test_a_raise_left_on_real_values_reaches_the_caller(raising_check) -> None:
    ctx = _hold_ctx(raising_check)

    with pytest.raises(_AvoidCheckRaised):
        _hold_allowed(ctx, ("Hold", True), REAL)
    with pytest.raises(_AvoidCheckRaised):
        _avoid_forces(ctx, [("Hold", True)], real=REAL)


def test_a_read_of_a_tag_no_real_state_has_reaches_the_caller() -> None:
    ctx = _hold_ctx(_compile_avoid(lambda s: s["Ghost"]))

    with pytest.raises(_AvoidCheckRaised):
        _hold_allowed(ctx, ("Hold", True), REAL)


def test_an_opaque_read_the_baseline_cannot_answer_is_asked_with_real_values() -> None:
    """The baseline has no Danger, and a real state does: the index read gets its answer."""
    opaque = _hold_ctx(_compile_avoid(lambda s: s["Danger"]))

    assert _hold_allowed(opaque, ("Hold", True), REAL) is True


def test_a_condition_the_baseline_trips_still_refuses_the_hold() -> None:
    """A condition reads the tag the baseline lacks as violated.  Lifting that is its own step."""
    condition = _hold_ctx(_compile_avoid(Bool("Danger")))

    assert _hold_allowed(condition, ("Hold", True), REAL) is False


def test_a_check_that_answers_on_the_resting_baseline_keeps_that_answer() -> None:
    reads_with_get = _hold_ctx(_compile_avoid(lambda s: bool(s.get("Danger"))))
    reads_the_input = _hold_ctx(_compile_avoid(lambda s: s["Hold"]))
    no_danger_on_it = _hold_ctx(_compile_avoid(lambda s: s.get("Danger") is None))

    assert _hold_allowed(reads_with_get, ("Hold", True), REAL) is True
    assert _hold_allowed(reads_the_input, ("Hold", False), REAL) is True
    assert _hold_allowed(reads_the_input, ("Hold", True), REAL) is False
    assert _hold_allowed(no_danger_on_it, ("Hold", True), REAL) is False


@pytest.mark.parametrize(
    "check",
    [lambda s: s["Hold"] and s["Level"] > 3, lambda s: s.get("Hold") and s.get("Level") + 0 > 3],
    ids=["index", "get"],
)
def test_real_values_say_what_the_hold_forces(check) -> None:
    ctx = _hold_ctx(_compile_avoid(check))

    assert _hold_allowed(ctx, ("Hold", True), {"Level": 5}) is False
    assert _hold_allowed(ctx, ("Hold", True), {"Level": 1}) is True
    assert _hold_allowed(ctx, ("Hold", False), {"Level": 5}) is True


def test_a_check_already_true_without_the_hold_is_not_forced_by_it() -> None:
    ctx = _hold_ctx(_compile_avoid(lambda s: s["Level"] > 3))

    assert _hold_allowed(ctx, ("Hold", True), {"Level": 5}) is True


def test_a_condition_with_a_program_owned_side_is_asked_with_real_values() -> None:
    """``Input > Limit``: the baseline has no Limit, and the compare raises on what it finds."""
    ctx = _hold_ctx(_compile_avoid(Int("Input") > Int("Limit")), {"Input": 0})
    with pytest.raises(_AvoidCheckRaised):
        _avoid_snap_names(ctx.avoid_pred, {"Input": 5})

    assert _hold_allowed(ctx, ("Input", 5), {"Limit": 10}) is True
    assert _hold_allowed(ctx, ("Input", 50), {"Limit": 10}) is False
    # True without the hold too, so the hold is not what forces it.
    assert _hold_allowed(ctx, ("Input", 5), {"Limit": -1}) is True


def test_rest_does_not_hide_a_real_input_the_check_reads() -> None:
    """Speed is 5 in the real state and 0 at rest; S sends the check to real values."""
    resting = {"Speed": 0, "Sensor": False}
    real = {"S": 6, "Speed": 5, "Sensor": False}
    ctx = _hold_ctx(
        _compile_avoid(lambda s: s["S"] == 6 and s["Speed"] > 3 and s["Sensor"]), resting
    )

    assert _hold_allowed(ctx, ("Sensor", True), real) is False
    assert _hold_allowed(ctx, ("Sensor", True), {**real, "Speed": 1}) is True


def test_a_condition_on_a_real_input_is_not_hidden_by_rest() -> None:
    sensor, speed, limit = Bool("Sensor"), Int("Speed"), Int("Lim")
    ctx = _hold_ctx(_compile_avoid(And(sensor, speed > limit)), {"Speed": 0, "Sensor": False})
    real = {"Speed": 5, "Sensor": False, "Lim": 0}

    assert _hold_allowed(ctx, ("Sensor", True), real) is False
    assert _hold_allowed(ctx, ("Sensor", True), {**real, "Speed": -1}) is True


def test_releasing_a_real_input_is_judged_from_it_being_on() -> None:
    resting = {"Speed": 0, "Sensor": False}
    ctx = _hold_ctx(_compile_avoid(lambda s: s["S"] == 6 and not s["Sensor"]), resting)

    assert _hold_allowed(ctx, ("Sensor", False), {"S": 6, "Speed": 0, "Sensor": True}) is False


def test_resting_fills_only_what_the_real_state_lacks() -> None:
    resting = {"Hold": False, "Spare": True}
    ctx = _hold_ctx(_compile_avoid(lambda s: s["S"] == 6 and s["Spare"] and s["Hold"]), resting)

    assert _hold_allowed(ctx, ("Hold", True), {"S": 6}) is False
    assert _hold_allowed(ctx, ("Hold", True), {"S": 6, "Spare": False}) is True


def test_each_member_of_a_union_is_judged_on_its_own() -> None:
    """A member the baseline trips stays tripped when another needs the real values."""
    avoid = _compile_avoid((Bool("Danger"), lambda s: s["Level"] > 100))
    ctx = _hold_ctx(avoid)

    assert _avoid_violations(ctx, [("Hold", True)], real=REAL) == ("Danger",)


def test_a_bare_callable_is_asked_with_real_values() -> None:
    ctx = _hold_ctx(lambda s: s["Level"] > 3)

    assert _hold_allowed(ctx, ("Hold", True), {"Level": 1}) is True
    assert _avoid_violations(ctx, [("Hold", True)], real={"Level": 1}) == ()


# ---------------------------------------------------------------------------
# A search that earns its route with corrective holds, through how()
# ---------------------------------------------------------------------------


def _dwell_program(limit_from=None, limit_to=None):
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
    return prog, state, sensor, limit


def _drive(prog, target, avoid):
    plc = PLC(prog, dt=0.010)
    plc.step()
    return plc.how(target, avoid=avoid, max_scans=2000)


def _holds(plan) -> int:
    return sum(len(step.rungs) for step in plan.journal)


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

    plan = _drive(prog, state == 7, sensor > limit)

    assert plan.status is PlanStatus.STOPPED
    assert _holds(plan) == 0


def test_a_condition_refused_on_the_baseline_stays_refused() -> None:
    """The baseline has no HoldState, which a condition reads as violated: as it was."""
    prog, state, *_ = _dwell_program()

    plan = _drive(prog, state == 7, state == 9999)

    assert plan.status is PlanStatus.STOPPED


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


def test_a_check_raising_only_with_a_hold_on_reaches_the_caller() -> None:
    prog, state, *_ = _dwell_program()

    def _raises_with_the_sensor_held(snapshot: dict) -> bool:
        if snapshot["HoldSensor"] and snapshot["HoldState"] == 6:
            raise ValueError("boom with the hold on")
        return False

    with pytest.raises(AvoidCheckError, match="ValueError"):
        _drive(prog, state == 7, _raises_with_the_sensor_held)
