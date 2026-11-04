"""``CANNOT_REACH`` is a proof: stepping the program must not find what it rules out.

Every false proof here reaches its goal under a plain stepped script, written out
in the test. Each real proof is one no input changes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyrung import (
    PLC,
    Bool,
    Harness,
    Int,
    Program,
    Pulse,
    Rung,
    Timer,
    branch,
    copy,
    latch,
    on_delay,
    reset,
    rise,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.physical import Physical

BOUND: dict[str, Any] = {"wall_clock_s": 30.0, "max_scans": 3000}


def _linked(physical: Physical, build: Callable[[Any, Any, Any], None]) -> tuple[Program, Any]:
    enable = Bool("Enable", external=True)
    feedback = Bool("Fb", physical=physical, link="Enable")
    hit = Bool("Hit")
    with Program(strict=False) as program:
        build(enable, feedback, hit)
    return program, hit


def _harnessed(program: Program, dt: float = 0.01) -> PLC:
    plc = PLC(program, dt=dt)
    Harness(plc).install()
    return plc


def _step_to(plc: PLC, tag: Any, inputs: dict[str, Any], scans: int) -> int | None:
    plc.patch(inputs)
    for scan in range(1, scans + 1):
        plc.patch(inputs)
        plc.step()
        if plc.state.tags.get(tag.name):
            return scan
    return None


def _two_contacts(enable: Any, feedback: Any, hit: Any) -> None:
    with Rung(enable, feedback):
        latch(hit)


def _pulse(dwell: str = "500ms") -> Physical:
    return Physical("Fb", profile=Pulse(on_dwell=dwell, off_dwell=dwell))


class TestLinkProofIsNotClaimedFalsely:
    def test_pulse_on_the_other_contact(self) -> None:
        """The link does not hold a pulse against its enable: it goes high while enabled."""
        program, hit = _linked(_pulse(), _two_contacts)
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 200) == 51

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_a_contact_that_follows_its_link(self) -> None:
        """``En, Fb`` is what the link gives a dwell after enabling."""
        program, hit = _linked(Physical("Fb", on_delay="100ms"), _two_contacts)
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 200) is not None

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_read_of_the_opposed_state_with_no_timer(self) -> None:
        """Fb stays low through its on-delay, so the instantaneous read is true."""

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="500ms"), build)
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 5) == 1

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_timer_shorter_than_the_dwell(self) -> None:
        """The watchdog shape, but its preset finishes while the feedback is still low."""
        Watch = Timer.clone("Watch")

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                on_delay(Watch, 100)
            with Rung(Watch.Done):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="1s"), build)
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 50) is not None

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_a_retentive_timer_keeps_what_the_dwell_gave_it(self) -> None:
        """Each short stretch adds up; enough of them finish past the dwell."""
        Watch = Timer.clone("Watch")
        clear = Bool("Clear", external=True)

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                on_delay(Watch, 300).reset(clear)
            with Rung(Watch.Done):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="100ms", off_delay="0ms"), build)
        stepped = _harnessed(program)
        for _cycle in range(8):
            if _step_to(stepped, hit, {"Enable": True}, 8) is not None:
                break
            if _step_to(stepped, hit, {"Enable": False}, 3) is not None:
                break
        assert stepped.state.tags[hit.name]

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_another_rung_reaches_it(self) -> None:
        """The watchdog holds, but a second writer needs no timer."""
        Watch = Timer.clone("Watch")
        manual = Bool("Manual", external=True)

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)
            with Rung(manual):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="100ms"), build)
        assert _step_to(_harnessed(program), hit, {"Manual": True}, 3) == 1

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_timer_already_most_of_the_way(self) -> None:
        """What the accumulator holds now counts against the preset."""
        Watch = Timer.clone("Watch")

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="100ms"), build)
        mid_count = {"Enable": True, Watch.Acc.name: 2950}
        stepped = _harnessed(program)
        stepped.patch(mid_count)
        stepped.step()
        assert _step_to(stepped, hit, {"Enable": True}, 8) is not None

        started = _harnessed(program)
        started.patch(mid_count)
        started.step()
        plan = started.how(hit, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason


class TestLinkProofStillProves:
    def test_watchdog_held_off_by_its_link(self) -> None:
        Watch = Timer.clone("Watch")

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(enable, ~feedback):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="200ms", off_delay="100ms"), build)
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 600) is None

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is PlanStatus.CANNOT_REACH, plan.reason
        assert "Fb<-Enable" in (plan.reason or "")
        assert "unlink=['Fb']" in (plan.reason or "")

        freed = _harnessed(program).how(hit, unlink=["Fb"], **BOUND)
        assert freed.status is PlanStatus.REACHED, freed.reason

    def test_watchdog_on_a_normally_closed_link(self) -> None:
        """A ``~`` link drives on the enable's low side, so that is the opposed side."""
        Watch = Timer.clone("Watch")
        enable = Bool("Enable", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="200ms"), link="~Enable")
        hit = Bool("Hit")
        with Program(strict=False) as program:
            with Rung(~enable, ~feedback):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Enable": False}, 600) is None

        plan = _harnessed(program).how(hit, **BOUND)
        assert plan.status is PlanStatus.CANNOT_REACH, plan.reason
        assert "Fb<-Enable" in (plan.reason or "")


def _state_machine() -> tuple[Program, Any, Any]:
    stage = Int("Stage")
    enter = Bool("Enter", external=True)
    home = Bool("Home", external=True)
    flag = Bool("Flag")
    with Program(strict=False) as program:
        with Rung(stage == 0, rise(enter)):
            copy(1, stage)
        with Rung(stage == 1):
            latch(flag)
        with Rung(home):
            reset(flag)
            copy(0, stage)
    return program, flag, stage


def _both(plc: PLC, script: list[tuple[dict[str, Any], int]], goal: dict[str, Any]) -> bool:
    for inputs, scans in script:
        plc.patch(inputs)
        for _ in range(scans):
            plc.step()
            if all(plc.state.tags.get(name) == value for name, value in goal.items()):
                return True
    return False


class TestExclusionProof:
    def test_real_exclusion_still_proves(self) -> None:
        program, flag, stage = _state_machine()
        plan = PLC(program, dt=0.01).how(flag, stage == 0, **BOUND)
        assert plan.status is PlanStatus.CANNOT_REACH, plan.reason
        assert "mutually exclusive" in (plan.reason or "")

    def test_a_second_route_that_clobbers_nothing(self) -> None:
        """One route clobbers the other target. The other route does not."""
        s1, s2, t1, t2 = (Bool(name, external=True) for name in ("S1", "S2", "T1", "T2"))
        m, n, x, y = Bool("M"), Bool("N"), Bool("X"), Bool("Y")
        with Program(strict=False) as program:
            with Rung(s1):
                latch(m)
                reset(y)
            with Rung(s2):
                latch(m)
            with Rung(m):
                latch(x)
            with Rung(t1):
                latch(n)
                reset(x)
            with Rung(t2):
                latch(n)
            with Rung(n):
                latch(y)
        goal = {"X": True, "Y": True}
        assert _both(PLC(program, dt=0.01), [({"S2": True, "T2": True}, 2)], goal)

        plan = PLC(program, dt=0.01).how(x, y, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_clobber_that_fires_once(self) -> None:
        """A one-shot clobber leaves the other target free to come back."""
        go = Bool("Go", external=True)
        again = Bool("Again", external=True)
        x, y = Int("X"), Int("Y")
        with Program(strict=False) as program:
            with Rung(go):
                copy(1, x)
                copy(0, y, oneshot=True)
            with Rung(again):
                copy(1, y)
                copy(0, x, oneshot=True)
        goal = {"X": 1, "Y": 1}
        assert _both(PLC(program, dt=0.01), [({"Go": True}, 2), ({"Again": True}, 3)], goal)

        plan = PLC(program, dt=0.01).how(x == 1, y == 1, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_both_already_hold(self) -> None:
        """Two targets true at the start need no write, whatever a writer would do."""
        go = Bool("Go", external=True)
        again = Bool("Again", external=True)
        x, y = Bool("X", default=True), Bool("Y", default=True)
        with Program(strict=False) as program:
            with Rung(go):
                latch(x)
                reset(y)
            with Rung(again):
                latch(y)
                reset(x)

        plan = PLC(program, dt=0.01).how(x, y, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_tag_the_outside_may_set(self) -> None:
        """A rung that clobbers an external input does not keep it down."""
        go = Bool("Go", external=True)
        ext_x = Bool("ExtX", external=True)
        ext_y = Bool("ExtY", external=True)
        with Program(strict=False) as program:
            with Rung(go):
                latch(ext_x)
                reset(ext_y)
            with Rung(ext_x, ext_y):
                reset(go)
        goal = {"ExtX": True, "ExtY": True}
        assert _both(PLC(program, dt=0.01), [({"ExtX": True, "ExtY": True}, 1)], goal)

        plan = PLC(program, dt=0.01).how(ext_x, ext_y, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_writers_two_rungs_upstream(self) -> None:
        """The state machine's shape, with the latch one rung away from the state read."""
        stage = Int("Stage")
        enter = Bool("Enter", external=True)
        home = Bool("Home", external=True)
        run = Bool("Run")
        flag = Bool("Flag")
        with Program(strict=False) as program:
            with Rung(stage == 0, rise(enter)):
                copy(1, stage)
                latch(run)
            with Rung(run):
                latch(flag)
            with Rung(home):
                copy(0, stage)
                reset(flag)
        goal = {"Flag": True, "Stage": 0}
        script = [({"Enter": True}, 1), ({"Home": True, "Enter": False}, 1), ({"Home": False}, 1)]
        assert _both(PLC(program, dt=0.01), script, goal)

        plan = PLC(program, dt=0.01).how(flag, stage == 0, **BOUND)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason

    def test_a_clobber_on_a_sibling_branch_is_no_evidence(self) -> None:
        """Only the firing node's own rung and ancestors are sure to have run."""
        go, side = Bool("Go", external=True), Bool("Side", external=True)
        again = Bool("Again", external=True)
        x, y = Bool("X"), Bool("Y")
        with Program(strict=False) as program:
            with Rung(go):
                with branch(side):
                    reset(y)
                latch(x)
            with Rung(again):
                reset(x)
                latch(y)
        goal = {"X": True, "Y": True}
        assert _both(
            PLC(program, dt=0.01),
            [({"Again": True}, 1), ({"Again": False, "Go": True}, 1)],
            goal,
        )

        plan = PLC(program, dt=0.01).how(x, y, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason


@pytest.mark.parametrize("value", [1, 2])
def test_one_register_still_proves(value: int) -> None:
    stage = Int("Stage")
    go = Bool("Go", external=True)
    with Program(strict=False) as program:
        with Rung(go):
            copy(value, stage)
    plan = PLC(program, dt=0.01).how(stage == 1, stage == 2, **BOUND)
    assert plan.status is PlanStatus.CANNOT_REACH, plan.reason
    assert "one register" in (plan.reason or "")
