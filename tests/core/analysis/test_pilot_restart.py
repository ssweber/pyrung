"""What how() tries when it is about to stop: restarting the program, on a fork."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pyrung import PLC, Bool, Int, Program, Rung, copy, latch, out, reset, system
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import api, budget
from pyrung.core.analysis.pilot import restart as restart_module
from pyrung.core.analysis.pilot.avoid import _AvoidCheckRaised
from pyrung.core.analysis.pilot.restart import RestartReport


def _only_on_a_first_scan():
    """Target is on for the first scan alone, and the session is past it."""
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(system.sys.first_scan):
            out(target)
    return prog, target


def _spent_by_the_first_scan():
    """As above, on a retentive Step the first scan moves off the value that gives Target."""
    step = Int("Step")
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(system.sys.first_scan, step == 0):
            out(target)
        with Rung(system.sys.first_scan):
            copy(10, step)
    return prog, step, target


def _stuck():
    """The last rung takes Target back every scan, and no restart changes that."""
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(go):
            latch(target)
        with Rung():
            reset(target)
    return prog, target


def _scanned(prog) -> PLC:
    plc = PLC(prog, dt=0.010)
    plc.step()
    plc.step()
    return plc


def test_a_plan_that_needs_the_restart_says_so_as_its_first_step() -> None:
    prog, target = _only_on_a_first_scan()
    plc = _scanned(prog)
    before = plc.current_state

    plan = plc.how(target, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.restart.reached is True
    text = str(plan)
    assert "1. Stop the program and go back to RUN." in text
    assert "how() found no way from where the session is" in text
    assert "\n2. " in text
    assert plan.total_scans == 1
    # The session did not move, and the plan is one a real run gives.
    assert plc.current_state is before
    assert plan.replay().state.tags[target.name] is True


def test_a_plan_that_needs_no_restart_does_not_mention_one() -> None:
    go = Bool("Go", external=True)
    target = Bool("Target")
    with Program(strict=False) as prog:
        with Rung(go):
            latch(target)

    plan = _scanned(prog).how(target, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.restart is None
    assert "Stop the program" not in str(plan)


def test_a_restart_that_falls_short_says_what_it_needs() -> None:
    prog, step, target = _spent_by_the_first_scan()

    plan = _scanned(prog).how(target, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.fork is None
    report = plan.restart
    assert report.reached is False
    assert report.survivors == {step.name: (10, 0)}
    assert report.clear == {step.name: 0}
    assert report.clear_scans == 1
    assert report.written_by == {step.name: ("rung 2",)}
    assert report.lines() == [
        "Restart: alone it falls short, because Step=10 survives it.",
        "  With Step=0 written while stopped, a restart reaches it in 1 scan.",
        "  Write Step by hand while stopped, or make it non-retentive.",
        "  Step is written by rung 2.",
    ]
    assert "Restart: alone it falls short" in str(plan)


def test_what_a_clear_report_says_works() -> None:
    prog, step, target = _spent_by_the_first_scan()
    plc = _scanned(prog)

    report = plc.how(target, max_scans=2000).restart
    plc.stop()
    plc._state = plc.state.with_tags(dict(report.clear))
    plc.start()
    assert plc.state.tags[step.name] == 0
    plan = plc.how(target, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.restart is None
    assert plan.total_scans == report.clear_scans
    assert plan.replay().state.tags[target.name] is True


def test_a_goal_a_restart_cannot_help_is_reported() -> None:
    prog, target = _stuck()

    plan = _scanned(prog).how(target, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart.clear is None
    assert plan.restart.lines() == [
        "Restart: stopping the program and going back to RUN does not reach it either."
    ]


def test_a_session_that_has_not_scanned_has_nothing_to_restart() -> None:
    prog, target = _stuck()

    plan = PLC(prog, dt=0.010).how(target, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart is None


def test_a_search_cut_short_tries_no_restart(monkeypatch) -> None:
    monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)
    prog, target = _only_on_a_first_scan()

    plan = _scanned(prog).how(target, max_scans=2000, wall_clock_s=0.0)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart is None


def test_a_force_the_restart_releases_is_named() -> None:
    extra = Bool("Extra")
    prog, target = _only_on_a_first_scan()
    plc = PLC(prog, dt=0.010)
    plc.force(extra, True)
    plc.step()
    plc.step()

    plan = plc.how(target, max_scans=2000)

    assert plan.status is PlanStatus.REACHED
    assert plan.restart.released_forces == (extra.name,)
    assert f"This releases the force on {extra.name}." in str(plan)
    assert plc._forces == {extra.name: True}


def _trips_on_a_restart():
    """Running is on after every scan, so only the state before the first scan lacks it."""
    running = Bool("Running")
    target = Bool("Target")
    with Program(strict=False) as tripped:
        with Rung():
            out(running)
        with Rung(system.sys.first_scan):
            out(target)
    return tripped, target


@pytest.mark.parametrize(
    "avoid",
    [lambda s: not s["Running"], (lambda s: not s["Running"],)],
    ids=["callable", "union"],
)
def test_an_avoid_that_rules_out_the_state_after_a_restart_is_reported(avoid) -> None:
    prog, target = _trips_on_a_restart()

    plan = _scanned(prog).how(target, avoid=avoid, max_scans=2000)

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart.avoided
    assert plan.restart.lines()[0].startswith("Restart: not tried, the state after it is avoided")


def test_the_restart_shares_the_callers_time(monkeypatch) -> None:
    monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)
    prog, target = _only_on_a_first_scan()
    limits: list[float] = []
    original = api._how_from

    def recording(plc, conditions, max_scans, avoid_pred, unlink, on_event, wall_s, **kwargs):
        limits.append(wall_s)
        return original(plc, conditions, max_scans, avoid_pred, unlink, on_event, wall_s, **kwargs)

    monkeypatch.setattr(api, "_how_from", recording)

    _scanned(prog).how(target, max_scans=2000, wall_clock_s=5.0)

    assert len(limits) >= 2
    assert limits[0] == 5.0
    assert all(0 < limit <= 5.0 for limit in limits[1:])


def _fake(reachable: bool, scans: int = 0):
    return SimpleNamespace(reachable=reachable, total_scans=scans)


def test_a_restart_that_ran_out_of_time_claims_nothing() -> None:
    prog, _step, target = _spent_by_the_first_scan()
    plc = _scanned(prog)
    stopped = replace(plc.how(target, max_scans=2000), restart=None)
    setup = SimpleNamespace(steerable=frozenset())

    plan = restart_module.try_restart(
        stopped, plc, setup, None, lambda _fork, _wall_s: (stopped, True)
    )

    assert plan.status is PlanStatus.STOPPED
    assert plan.restart.lines() == ["Restart: how() ran out of time before it had an answer."]


def _clear_with(answers) -> tuple:
    prog, _step, _target = _spent_by_the_first_scan()
    survivors = {"A": (1, 0), "B": (2, 0), "C": (3, 0)}
    answer = iter(answers)

    def ask(_fork, _wall_s):
        next_answer = next(answer)
        if isinstance(next_answer, BaseException):
            raise next_answer
        return next_answer

    return survivors, restart_module._smallest_clear(_scanned(prog), survivors, ask)


def test_a_clear_cut_short_keeps_what_it_has_and_says_so() -> None:
    survivors, (clear, scans, untried) = _clear_with(
        [(_fake(True, 7), False), (_fake(True, 5), False), (_fake(False), True)]
    )

    # A went, and time ran out on B: nothing was learned about B or C.
    assert clear == {"B": 0, "C": 0}
    assert scans == 5
    assert untried is True
    report = RestartReport(
        reached=False,
        survivors=survivors,
        clear=clear,
        clear_scans=scans,
        clear_untried=untried,
    )
    assert "  how() ran out of time checking, so not all of them may be needed." in report.lines()


def test_a_needed_tag_stays_and_the_rest_go() -> None:
    _survivors, (clear, scans, untried) = _clear_with(
        [
            (_fake(True, 7), False),
            (_fake(True, 5), False),
            (_fake(False), False),
            (_fake(True, 4), False),
        ]
    )

    assert clear == {"B": 0}
    assert scans == 4
    assert untried is False


def test_a_check_raising_on_a_clear_try_loses_only_that_try() -> None:
    """The writes are advice.  how() has its answer, and that try just has no plan."""
    raised = _AvoidCheckRaised("avoid", ZeroDivisionError())
    _survivors, (clear, scans, untried) = _clear_with(
        [(_fake(True, 7), False), raised, (_fake(True, 5), False), (_fake(False), False)]
    )
    assert clear == {"A": 0, "C": 0}
    assert scans == 5
    assert untried is False

    _survivors, (clear, scans, untried) = _clear_with([raised])
    assert (clear, scans, untried) == (None, None, False)
