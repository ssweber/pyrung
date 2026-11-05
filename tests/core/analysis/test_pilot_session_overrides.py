"""A plan runs under the session's forces and pending patches, and says what it does about them."""

from __future__ import annotations

from pyrung import PLC, Bool, Program, Rung, Timer, latch, on_delay, out, reset, system
from pyrung.core.analysis.graph import PlanStatus


def _follow(plc, plan):
    """Do on the real session what the plan says, and return it at the plan's last scan."""
    if plan.released is not None:
        for name in plan.released.forces:
            plc.unforce(name)
    steps = dict(plan.ordered_steps)
    for scan_id in range(plan.anchor_scan + 1, plan.anchor_scan + plan.total_scans + 1):
        if scan_id in steps:
            plc.patch(steps[scan_id])
        plc.step()
    return plc


def _latch_on_go():
    go, hit = Bool("Go"), Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(go):
            latch(hit)
    return logic, hit


def test_a_force_in_the_way_is_released_as_a_step_of_the_plan() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Go", False)

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.released.forces == ("Go",)
    assert plan.restart is None
    text = str(plan)
    assert "1. Release the force on Go." in text
    assert "2. Set Go=True." in text
    assert plan.replay().state.tags == plan.fork.state.tags
    assert _follow(plc, plan).state.tags["Hit"] is True


def test_following_a_plan_without_the_release_does_not_get_there() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Go", False)

    plan = plc.how(hit, max_scans=500)

    for scan_id, inputs in plan.ordered_steps:
        assert scan_id > plan.anchor_scan
        plc.patch(inputs)
        plc.step()
    assert plc.state.tags["Hit"] is False


def test_only_the_force_in_the_way_is_released() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.force(Bool("Unrelated"), True)
    plc.force("Go", False)
    plc.step()

    plan = plc.how(hit, max_scans=500)

    assert plan.released.forces == ("Go",)
    assert plan.changes == {"Go": True}
    assert _follow(plc, plan).state.tags["Hit"] is True
    assert plc.forces == {"Unrelated": True}


def test_a_force_that_keeps_resetting_the_goal_is_released() -> None:
    gate, stop, kept = Bool("Gate"), Bool("Stop"), Bool("Kept")
    with Program(strict=False) as logic:
        with Rung(gate):
            latch(kept)
        with Rung(stop):
            reset(kept)
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Stop", True)

    plan = plc.how(kept, max_scans=500)

    assert plan.released.forces == ("Stop",)
    assert _follow(plc, plan).state.tags["Kept"] is True


def test_a_force_on_the_goal_itself_is_released() -> None:
    go, lamp = Bool("Go"), Bool("Lamp")
    with Program(strict=False) as logic:
        with Rung(go):
            out(lamp)
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Lamp", False)

    plan = plc.how(lamp, max_scans=500)

    assert plan.released.forces == ("Lamp",)
    assert _follow(plc, plan).state.tags["Lamp"] is True


def test_a_force_out_of_the_way_is_no_step_and_no_change() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.force(Bool("Unrelated"), True)
    plc.step()

    plan = plc.how(hit, max_scans=500)

    assert plan.released is None
    assert plan.waiting_overrides == ()
    assert plan.changes == {"Go": True}
    assert "Unrelated" not in str(plan)
    assert _follow(plc, plan).state.tags["Hit"] is True


def test_a_force_that_helps_is_used() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Go", True)

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 1
    assert plan.changes == {}
    assert plan.waiting_overrides == ("Go",)
    assert "1. Let one scan run, so the session's own patch or force on Go" in str(plan)
    assert _follow(plc, plan).state.tags["Hit"] is True


def test_a_pending_patch_counts_toward_the_plan_and_is_not_its_step() -> None:
    enable, hit = Bool("En"), Bool("Hit")
    wait = Timer.clone("W")
    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(wait, 3000)
        with Rung(wait.Done):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.patch({"En": True, wait.Acc.name: 2995})

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 1
    assert plan.changes == {}
    assert plan.replay().state.tags == plan.fork.state.tags
    assert _follow(plc, plan).state.tags["Hit"] is True


def test_a_goal_a_pending_patch_is_about_to_undo_is_not_already_reached() -> None:
    go, stop, run = Bool("Go"), Bool("Stop"), Bool("Run")
    with Program(strict=False) as logic:
        with Rung(go):
            latch(run)
        with Rung(stop):
            reset(run)
    plc = PLC(logic, dt=0.01)
    plc.patch({"Go": True})
    plc.step()
    plc.step()
    plc.patch({"Stop": True})
    assert plc.state.tags["Run"] is True

    plan = plc.how(run, max_scans=500)

    assert plan.total_scans > 0
    assert _follow(plc, plan).state.tags["Run"] is True


def test_a_goal_true_under_a_force_a_scan_has_applied_takes_no_scan() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.force("Go", True)
    plc.step()

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.total_scans == 0
    assert plan.waiting_overrides == ()


def test_the_session_keeps_its_own_forces_and_patches() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Go", False)
    plc.patch({"Other": True})
    scan = plc.state.scan_id

    plc.how(hit, max_scans=500)

    assert plc.forces == {"Go": False}
    assert plc._pending_patches == {"Other": True}
    assert plc.state.scan_id == scan


def test_a_public_fork_still_starts_clean() -> None:
    logic, _hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.force("Go", False)
    plc.patch({"Other": True})

    fork = plc.fork()

    assert dict(fork.forces) == {}
    assert fork._pending_patches == {}


def test_a_raise_on_a_scan_of_the_release_try_loses_that_try_not_the_answer() -> None:
    logic, hit = _latch_on_go()
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("Go", False)

    def trips_on_the_goal(state) -> bool:
        if state["Hit"]:
            raise ZeroDivisionError("division by zero")
        return False

    plan = plc.how(hit, avoid=trips_on_the_goal, max_scans=500)

    assert plan.status is PlanStatus.STOPPED
    assert plan.released is None


def test_a_force_that_only_ran_the_budget_out_still_gets_the_try_without_it() -> None:
    """The guarded loop declines a forced session, so the wait costs what the budget has."""
    enable, hit = Bool("En", external=True), Bool("Hit")
    wait = Timer.clone("W")
    with Program(strict=False) as logic:
        with Rung(enable):
            on_delay(wait, 3000)
        with Rung(wait.Done):
            latch(hit)
    plc = PLC(logic, dt=0.01)
    plc.step()
    plc.force("En", True)

    plan = plc.how(hit, max_scans=100)

    assert plan.status is PlanStatus.REACHED
    assert plan.released.forces == ("En",)
    assert "budget exhausted" in str(plan)
    # This plan holds En with temporary logic, which _follow does not install.
    assert plan.fork.forces == {}
    assert plan.fork.state.tags["Hit"] is True
    assert plan.replay().state.tags == plan.fork.state.tags
    assert plc.forces == {"En": True}


def test_a_restart_still_releases_every_force_and_says_so() -> None:
    hit = Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(system.sys.first_scan):
            out(hit)
    plc = PLC(logic, dt=0.01)
    plc.force(Bool("Unrelated"), True)
    plc.step()
    plc.step()

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.released is None
    assert plan.restart.reached is True
    assert "This releases the force on Unrelated." in str(plan)
