"""``CANNOT_REACH`` is a proof: stepping the program must not find what it rules out.

Every false proof here reaches its goal under a plain stepped script, written out
in the test. Each real proof is one no input changes.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import (
    PLC,
    And,
    Block,
    Bool,
    Harness,
    InputBlock,
    Int,
    Or,
    Program,
    Pulse,
    Rung,
    TagType,
    Timer,
    branch,
    call,
    copy,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    return_early,
    rise,
    run_function,
    subroutine,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot.drive_setup import prepare_drive
from pyrung.core.analysis.pilot.static_proofs import (
    exclusion_evidence,
    facts_for,
    linked_feedback_block,
)
from pyrung.core.harness import Coupling
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


def _link_proof(program: Program, tag: Any) -> str | None:
    """What the proof says on its own: a search that reaches the target would hide it."""
    setup = prepare_drive(_harnessed(program), unlink=None)
    return linked_feedback_block(tag.name, True, facts_for(setup))


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
        assert _step_to(_harnessed(program), hit, {"Enable": True}, 200) == 2

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

    def test_a_program_that_keeps_both_arms_going(self) -> None:
        """Each arm ends within its delay, but this program flips into the other arm unbroken.

        The plant reads the enable the program left last scan, so the feedback's change and
        the program's answer to it land in one scan and the rung never goes false.
        """
        Watch = Timer.clone("Watch")
        go = Bool("Go", external=True)
        enable = Bool("Enable")
        feedback = Bool(
            "Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="Enable"
        )
        hit = Bool("Hit")
        with Program(strict=False) as program:
            with Rung(go, ~feedback):
                out(enable)
            with Rung(Or(And(enable, ~feedback), And(~enable, feedback))):
                on_delay(Watch, 1000)
            with Rung(Watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 200) is not None
        assert _link_proof(program, hit) is None

        plan = _harnessed(program).how(hit, **BOUND)
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

        reason = _proved_on(_harnessed(program), hit)
        assert reason is not None
        assert "Fb<-Enable" in reason
        assert "unlink=['Fb']" in reason

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

        reason = _proved_on(_harnessed(program), hit)
        assert reason is not None
        assert "Fb<-Enable" in reason


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
        again = Bool("Again", external=True)
        ext_x = Bool("ExtX", external=True)
        ext_y = Bool("ExtY", external=True)
        with Program(strict=False) as program:
            with Rung(go):
                latch(ext_x)
                reset(ext_y)
            with Rung(again):
                latch(ext_y)
                reset(ext_x)
        goal = {"ExtX": True, "ExtY": True}
        assert _both(PLC(program, dt=0.01), [({"ExtX": True, "ExtY": True}, 1)], goal)

        plan = PLC(program, dt=0.01).how(ext_x, ext_y, max_scans=300)
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


# ---------------------------------------------------------------------------
# What the proof cannot see: it gives up
# ---------------------------------------------------------------------------


def _watchdog_chain() -> tuple[Program, Any, Any]:
    enable = Bool("En", external=True)
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
    hit = Bool("Hit")
    middle = Bool("M")
    watch = Timer.clone("W")
    with Program(strict=False) as program:
        with Rung(enable, ~feedback):
            on_delay(watch, 3000)
        with Rung(watch.Done):
            latch(middle)
        with Rung(middle):
            latch(hit)
    return program, hit, watch


def _proved_on(plc: PLC, tag: Any, value: Any = True) -> str | None:
    return linked_feedback_block(tag.name, value, facts_for(prepare_drive(plc, unlink=None)))


class TestForcesAndPatchesOfTheSession:
    """how() searches a fork, which drops both: the session itself is what the proof is about."""

    def test_the_watchdog_proves_with_nothing_forced(self) -> None:
        program, hit, _watch = _watchdog_chain()
        assert _proved_on(_harnessed(program), hit) is not None

    @pytest.mark.parametrize(
        "forced",
        [
            lambda watch: ("Fb", False),
            lambda watch: ("M", True),
            lambda watch: (watch.Acc.name, 3100),
        ],
        ids=["feedback", "a tag on the way", "the count"],
    )
    def test_a_force_on_the_way_is_no_proof(self, forced: Callable[[Any], Any]) -> None:
        program, hit, watch = _watchdog_chain()
        name, value = forced(watch)

        stepped = _harnessed(program)
        stepped.force(name, value)
        assert _step_to(stepped, hit, {"En": True}, 400) is not None

        session = _harnessed(program)
        session.force(name, value)
        assert _proved_on(session, hit) is None
        assert session.how(hit, **BOUND).status is not PlanStatus.CANNOT_REACH

    def test_a_force_holds_both_of_an_exclusion(self) -> None:
        go, again = Bool("Go", external=True), Bool("Ag", external=True)
        x, y = Bool("X"), Bool("Y")
        with Program(strict=False) as program:
            with Rung(go):
                latch(x)
                reset(y)
            with Rung(again):
                latch(y)
                reset(x)
        plc = PLC(program, dt=0.01)
        plc.force("Y", True)
        assert plc.how(x, y, max_scans=300).status is not PlanStatus.CANNOT_REACH

    def test_a_pending_patch_is_no_proof(self) -> None:
        program, hit, watch = _watchdog_chain()

        stepped = _harnessed(program)
        stepped.patch({"En": True, watch.Acc.name: 2995})
        assert _step_to(stepped, hit, {"En": True}, 10) is not None

        session = _harnessed(program)
        session.patch({"En": True, watch.Acc.name: 2995})
        assert _proved_on(session, hit) is None
        assert session.how(hit, **BOUND).status is not PlanStatus.CANNOT_REACH


def test_a_write_after_a_possible_return_is_no_clobber() -> None:
    go, again, skip = (Bool(name, external=True) for name in ("Go", "Ag", "Sw"))
    x, y = Bool("X"), Bool("Y")
    with Program(strict=False) as program:
        with Rung():
            call("S")
        with subroutine("S"):
            with Rung(go):
                latch(x)
                with branch(skip):
                    return_early()
                reset(y)
            with Rung(again):
                latch(y)
                reset(x)

    stepped = PLC(program, dt=0.01)
    stepped.patch({"Ag": True})
    stepped.step()
    stepped.patch({"Ag": False, "Go": True, "Sw": True})
    stepped.step()
    assert stepped.state.tags["X"] and stepped.state.tags["Y"]

    plan = PLC(program, dt=0.01).how(x, y, max_scans=300)
    assert plan.status is not PlanStatus.CANNOT_REACH


class TestPointerWritesOverTheCap:
    """A pointer into a block too big to expand leaves no writer for the slot in the graph."""

    def test_the_timers_done(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit, command = Bool("Hit"), Bool("Cmd", external=True)
        bits = Block("C", TagType.BOOL, 1, 2000)
        pointer = Int("Idx")
        timer = SimpleNamespace(Done=bits[1], Acc=Int("Acc"))
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(timer, 3000)
            with Rung(command):
                copy(1, bits[pointer])
            with Rung(bits[1]):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Cmd": True, "Idx": 1}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_the_timers_count(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit, command, done = Bool("Hit"), Bool("Cmd", external=True), Bool("Done")
        words = Block("D", TagType.INT, 1, 2000)
        pointer = Int("Idx")
        timer = SimpleNamespace(Done=done, Acc=words[1])
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(timer, 3000)
            with Rung(command):
                copy(2995, words[pointer])
            with Rung(done):
                latch(hit)
        assert (
            _step_to(_harnessed(program), hit, {"Cmd": True, "Idx": 1, "En": True}, 5) is not None
        )
        assert _proved_on(_harnessed(program), hit) is None

    def test_the_feedback(self) -> None:
        enable, command = Bool("En", external=True), Bool("Cmd", external=True)
        bits = Block("C", TagType.BOOL, 1, 2000)
        pointer = Int("Idx")
        watch = Timer.clone("W")
        hit = Bool("Hit")
        feedback = bits[1]
        with Program(strict=False) as program:
            with Rung(command):
                copy(0, bits[pointer])
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(hit)

        def session() -> PLC:
            plc = PLC(program, dt=0.01)
            harness = Harness(plc)
            harness.install()
            harness.set_overrides(
                {
                    feedback.name: Coupling(
                        "En", feedback.name, Physical(feedback.name, on_delay="100ms")
                    )
                }
            )
            return plc

        assert _step_to(session(), hit, {"Cmd": True, "En": True, "Idx": 1}, 400) is not None
        assert _proved_on(session(), hit) is None


def test_an_input_block_slot_is_the_outsides_to_set() -> None:
    go, again = Bool("Go", external=True), Bool("Ag", external=True)
    inputs = InputBlock("I", TagType.BOOL, 1, 5)
    with Program(strict=False) as program:
        with Rung(go):
            latch(inputs[1])
            reset(inputs[2])
        with Rung(again):
            latch(inputs[2])
            reset(inputs[1])

    stepped = PLC(program, dt=0.01)
    stepped.patch({inputs[1].name: True, inputs[2].name: True})
    stepped.step()
    assert stepped.state.tags[inputs[1].name] and stepped.state.tags[inputs[2].name]

    plan = PLC(program, dt=0.01).how(inputs[1], inputs[2], max_scans=300)
    assert plan.status is not PlanStatus.CANNOT_REACH


class TestThePlantsOwnDelay:
    """Past 32.767 s the plant counts whole seconds, rounded up, so it runs longer than declared."""

    def test_on_delay(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="40500ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(watch, 41, "s")
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program, dt=0.1), hit, {"En": True}, 800) is not None
        assert _proved_on(_harnessed(program, dt=0.1), hit) is None

    def test_off_delay(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", off_delay="40500ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(~enable, feedback):
                on_delay(watch, 41, "s")
            with Rung(watch.Done):
                latch(hit)

        stepped = _harnessed(program, dt=0.1)
        _step_to(stepped, hit, {"En": True}, 5)
        assert _step_to(stepped, hit, {"En": False}, 800) is not None
        assert _proved_on(_harnessed(program, dt=0.1), hit) is None

    def test_a_delay_the_timer_clearly_outlasts_still_proves(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="40500ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(watch, 60, "s")
            with Rung(watch.Done):
                latch(hit)
        assert _proved_on(_harnessed(program, dt=0.1), hit) is not None


# ---------------------------------------------------------------------------
# The enable a rung reads and the enable the plant reads
# ---------------------------------------------------------------------------


def _mid_scan(
    build: Callable[[Any, Any, Any, Any, Any], None],
    *,
    enable: Any = None,
    feedback: Any = None,
) -> tuple[Program, Any]:
    go = Bool("Go", external=True)
    enable = Bool("En") if enable is None else enable
    feedback = (
        Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        if feedback is None
        else feedback
    )
    hit = Bool("Hit")
    watch = Timer.clone("W")
    with Program(strict=False) as program:
        build(go, enable, feedback, watch, hit)
        with Rung(watch.Done):
            latch(hit)
    return program, hit


def _cleared_after_the_read(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with Rung(go):
        latch(enable)
    with Rung(enable, ~feedback):
        on_delay(watch, 3000)
    with Rung(go):
        reset(enable)


def _cleared_at_the_top(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with Rung():
        reset(enable)
    with Rung(~enable, feedback):
        on_delay(watch, 3000)
    with Rung(go):
        latch(enable)


def _clears_itself(_go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with Rung(enable, ~feedback):
        on_delay(watch, 3000)
    with Rung(enable):
        reset(enable)


def _latched(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with Rung(go):
        latch(enable)
    with Rung(enable, ~feedback):
        on_delay(watch, 3000)


def _reset_only(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with Rung(go):
        reset(enable)
    with Rung(~enable, feedback):
        on_delay(watch, 3000)


def _cleared_by_a_subroutine(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
    with subroutine("S"):
        with Rung():
            reset(enable)
    with Rung(go):
        latch(enable)
    with Rung(enable, ~feedback):
        on_delay(watch, 3000)
    with Rung(go):
        call("S")


class TestAnEnableThatMovesMidScan:
    """Each holds the opposed state for good while the plant, reading at the boundary, sees none."""

    @pytest.mark.parametrize(
        ("build", "enable", "feedback", "forced", "inputs"),
        [
            (_cleared_after_the_read, None, None, {}, {"Go": True}),
            (
                _cleared_at_the_top,
                None,
                Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En"),
                {},
                {"Go": True},
            ),
            (_clears_itself, Bool("En", external=True), None, {}, {"En": True}),
            (_latched, None, None, {"En": False}, {"Go": True}),
            (_reset_only, None, None, {"En": True}, {"Go": True}),
            (_cleared_by_a_subroutine, None, None, {}, {"Go": True}),
        ],
        ids=[
            "set, read, cleared",
            "cleared, read, set",
            "external, clears itself",
            "forced off over a latch",
            "forced on over a reset",
            "a subroutine clears it",
        ],
    )
    def test_no_proof(
        self, build: Any, enable: Any, feedback: Any, forced: dict[str, Any], inputs: dict[str, Any]
    ) -> None:
        program, hit = _mid_scan(build, enable=enable, feedback=feedback)

        def session() -> PLC:
            plc = _harnessed(program)
            for name, value in forced.items():
                plc.force(name, value)
            return plc

        assert _step_to(session(), hit, inputs, 500) is not None
        assert session().how(hit, max_scans=1000).status is not PlanStatus.CANNOT_REACH

    def test_a_pointer_clears_it(self) -> None:
        go = Bool("Go", external=True)
        bits = Block("B", TagType.BOOL, 1, 3)
        pointer = Int("Idx", default=1)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="B1")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                latch(bits[1])
            with Rung(bits[1], ~feedback):
                on_delay(watch, 3000)
            with Rung(go):
                copy(0, bits[pointer])
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is not None
        assert _harnessed(program).how(hit, max_scans=1000).status is not PlanStatus.CANNOT_REACH

    def test_a_coil_in_a_subroutine_called_twice(self) -> None:
        """One out coil, two positions a scan: on for the rung, off for the plant."""
        source = Bool("Src")
        enable = Bool("En")
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with subroutine("S"):
                with Rung(source):
                    out(enable)
            with Rung():
                latch(source)
            with Rung():
                call("S")
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung():
                reset(source)
            with Rung():
                call("S")
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {}, 500) is not None
        assert _proved_on(_harnessed(program), hit) is None


class TestWiderFormsClaimNothing:
    """True on these programs, but the proof no longer argues over the shape: STOPPED, not false."""

    def test_the_same_opposition_on_every_arm(self) -> None:
        extra = Bool("Extra", external=True)
        Watch = Timer.clone("Watch")

        def build(enable: Any, feedback: Any, hit: Any) -> None:
            with Rung(Or(And(enable, ~feedback), And(enable, ~feedback, extra))):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)

        program, hit = _linked(Physical("Fb", on_delay="200ms"), build)
        assert _proved_on(_harnessed(program), hit) is None

    def test_a_value_trigger(self) -> None:
        mode = Int("Mode", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="200ms"), link="Mode:3")
        hit = Bool("Hit")
        Watch = Timer.clone("Watch")
        with Program(strict=False) as program:
            with Rung(mode == 3, ~feedback):
                on_delay(Watch, 3000)
            with Rung(Watch.Done):
                latch(hit)
        assert _proved_on(_harnessed(program), hit) is None


class TestAnEnableThatKeepsTheProof:
    def test_no_writer_and_a_force(self) -> None:
        program, hit = _mid_scan(
            lambda _go, enable, feedback, watch, _hit: _plain_rung(enable, feedback, watch),
            enable=Bool("En", external=True),
        )
        session = _harnessed(program)
        session.force("En", True)
        assert session.how(hit, max_scans=1000).status is PlanStatus.CANNOT_REACH

    @pytest.mark.parametrize("coil_first", [True, False], ids=["coil first", "coil after"])
    def test_one_out_coil(self, coil_first: bool) -> None:
        def build(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
            if coil_first:
                with Rung(go):
                    out(enable)
            _plain_rung(enable, feedback, watch)
            if not coil_first:
                with Rung(go):
                    out(enable)

        program, hit = _mid_scan(build)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is None
        assert _harnessed(program).how(hit, max_scans=1000).status is PlanStatus.CANNOT_REACH

    def test_one_out_coil_that_toggles(self) -> None:
        def build(go: Any, enable: Any, feedback: Any, watch: Any, _hit: Any) -> None:
            with Rung(go, ~enable):
                out(enable)
            _plain_rung(enable, feedback, watch)

        program, hit = _mid_scan(build)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is None
        assert _proved_on(_harnessed(program), hit) is not None


def _plain_rung(enable: Any, feedback: Any, watch: Any) -> None:
    with Rung(enable, ~feedback):
        on_delay(watch, 3000)


# ---------------------------------------------------------------------------
# Writes the graph has no element list for
# ---------------------------------------------------------------------------

_INDEX_EXPRESSIONS = [
    pytest.param(lambda index, offset, plus=0: index * 2 - 1 + plus, id="Idx*2-1"),
    pytest.param(lambda index, offset, plus=0: index + offset + plus, id="Idx+Off"),
]


def _big_block(kind: TagType = TagType.BOOL) -> tuple[Any, Any, Any]:
    name = "C" if kind is TagType.BOOL else "D"
    return Block(name, kind, 1, 2000), Int("Idx", default=1), Int("Off", default=0)


@pytest.mark.parametrize("index_of", _INDEX_EXPRESSIONS)
class TestIndexExpressionsOverTheCap:
    """Past the cap ``C[Idx*2-1]`` is listed nowhere, and may be any slot of the block."""

    def test_the_timers_done(self, index_of: Any) -> None:
        go, enable = Bool("Go", external=True), Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        bits, index, offset = _big_block()
        timer = SimpleNamespace(Done=bits[1], Acc=Int("Acc"))
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(timer, 3000)
            with Rung(go):
                copy(1, bits[index_of(index, offset)])
            with Rung(bits[1]):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_the_timers_count(self, index_of: Any) -> None:
        go, enable = Bool("Go", external=True), Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit, done = Bool("Hit"), Bool("Done")
        words, index, offset = _big_block(TagType.INT)
        timer = SimpleNamespace(Done=done, Acc=words[1])
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(timer, 3000)
            with Rung(go):
                copy(2995, words[index_of(index, offset)])
            with Rung(done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True, "En": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_the_enable(self, index_of: Any) -> None:
        go = Bool("Go", external=True)
        hit = Bool("Hit")
        bits, index, offset = _big_block()
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link=bits[1].name)
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                copy(1, bits[index_of(index, offset)])
            with Rung(bits[1], ~feedback):
                on_delay(watch, 3000)
            with Rung(go):
                copy(0, bits[index_of(index, offset)])
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_the_feedback(self, index_of: Any) -> None:
        go, enable = Bool("Go", external=True), Bool("En", external=True)
        hit = Bool("Hit")
        bits, index, offset = _big_block()
        feedback = bits[1]
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                copy(0, bits[index_of(index, offset)])
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(hit)

        def session() -> PLC:
            plc = PLC(program, dt=0.01)
            harness = Harness(plc)
            harness.install()
            harness.set_overrides(
                {
                    feedback.name: Coupling(
                        "En", feedback.name, Physical(feedback.name, on_delay="100ms")
                    )
                }
            )
            return plc

        assert _step_to(session(), hit, {"Go": True, "En": True}, 500) is not None
        assert _proved_on(session(), hit) is None

    @pytest.mark.parametrize("through_a_function", [False, True], ids=["copy", "run_function"])
    def test_a_second_route(self, index_of: Any, through_a_function: bool) -> None:
        go, enable = Bool("Go", external=True), Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        bits, index, offset = _big_block()
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                if through_a_function:
                    run_function(lambda: {"v": True}, outs={"v": bits[index_of(index, offset)]})
                else:
                    copy(1, bits[index_of(index, offset)])
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(bits[1])
            with Rung(bits[1]):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None
        assert _harnessed(program).how(hit, max_scans=100).status is not PlanStatus.CANNOT_REACH

    def test_exclusion(self, index_of: Any) -> None:
        a, b, go = (Bool(name, external=True) for name in ("A", "Bx", "Go"))
        bits, index, offset = _big_block()
        with Program(strict=False) as program:
            with Rung(a):
                latch(bits[1])
                reset(bits[2])
            with Rung(b):
                latch(bits[2])
                reset(bits[1])
            with Rung(go):
                copy(1, bits[index_of(index, offset)])
                copy(1, bits[index_of(index, offset, 1)])

        def stepped() -> bool:
            return _both(
                PLC(program, dt=0.01),
                [({"Go": True}, 2)],
                {bits[1].name: True, bits[2].name: True},
            )

        assert stepped()
        facts = facts_for(prepare_drive(PLC(program, dt=0.01), unlink=None))
        assert exclusion_evidence(facts, (bits[1].name, True), (bits[2].name, True)) is None
        plan = PLC(program, dt=0.01).how(bits[1], bits[2], max_scans=100)
        assert plan.status is not PlanStatus.CANNOT_REACH, plan.reason


def test_a_read_at_an_index_expression_keeps_the_proof() -> None:
    """Only a write goes opaque: reading ``C[Idx*2]`` moves no slot."""
    enable = Bool("En", external=True)
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
    hit, other = Bool("Hit"), Bool("Other")
    bits, index, _offset = _big_block()
    done = bits[1]
    timer = SimpleNamespace(Done=done, Acc=Int("Acc"))
    with Program(strict=False) as program:
        with Rung():
            copy(bits[index * 2], other)
        with Rung(enable, ~feedback):
            on_delay(timer, 3000)
        with Rung(done):
            latch(hit)
    assert _proved_on(_harnessed(program), hit) is not None


# ---------------------------------------------------------------------------
# Other writes on the one rung
# ---------------------------------------------------------------------------


def _load_by_copy(watch: Any, saved: Any) -> None:
    copy(saved, watch.Acc)


def _load_by_literal(watch: Any, _saved: Any) -> None:
    copy(3000, watch.Acc)


def _load_by_function(watch: Any, saved: Any) -> None:
    run_function(lambda s: {"v": s}, ins={"s": saved}, outs={"v": watch.Acc})


def _done_by_function(watch: Any, _saved: Any) -> None:
    run_function(lambda: {"v": True}, outs={"v": watch.Done})


def _load_in_a_loop(watch: Any, saved: Any) -> None:
    with forloop(1):
        copy(saved, watch.Acc)


class TestOtherWritesInTheTimersRung:
    @pytest.mark.parametrize(
        "extra",
        [_load_by_copy, _load_by_literal, _load_by_function, _done_by_function, _load_in_a_loop],
        ids=["copy a tag", "copy a literal", "function to Acc", "function to Done", "forloop"],
    )
    def test_with_an_external_enable(self, extra: Any) -> None:
        enable = Bool("En", external=True)
        saved = Int("Saved", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
                extra(watch, saved)
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"En": True, "Saved": 3000}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None
        assert _harnessed(program).how(hit, max_scans=1).status is not PlanStatus.CANNOT_REACH

    def test_with_one_coil(self) -> None:
        go, switch = Bool("Go", external=True), Bool("Sw", external=True)
        enable, boost = Bool("En"), Int("Boost")
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                out(enable)
            with Rung(switch):
                copy(3000, boost)
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
                copy(boost, watch.Acc)
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True, "Sw": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    @pytest.mark.parametrize("slot", ["Acc", "Done"])
    def test_through_a_pointer_into_a_small_block(self, slot: str) -> None:
        """A small block lists the slot on this very rung, so the writer's node looks like the timer's."""
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        index = Int("Idx", default=1)
        kind = TagType.INT if slot == "Acc" else TagType.BOOL
        small = Block("S", kind, 1, 10)
        timer = SimpleNamespace(
            Done=small[1] if slot == "Done" else Bool("TD"),
            Acc=small[1] if slot == "Acc" else Int("TA"),
        )
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(timer, 3000)
                copy(3000 if slot == "Acc" else 1, small[index])
            with Rung(timer.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"En": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_a_harmless_write_keeps_the_proof(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit, other = Bool("Hit"), Int("Other")
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
                copy(5, other)
            with Rung(watch.Done):
                latch(hit)
        assert _proved_on(_harnessed(program), hit) is not None


class TestAWriteTheRungHides:
    """One instruction on the rung cannot make the value, and a second one it cannot see does."""

    def _watchdog(self) -> tuple[Any, Any, Any]:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        watch = Timer.clone("W")
        with Rung(enable, ~feedback):
            on_delay(watch, 3000)
        return watch.Done, Bool("Go", external=True), Bool("Hit")

    def test_through_a_pointer(self) -> None:
        bits = Block("B", TagType.BOOL, 1, 5)
        index = Int("Idx", default=1)
        with Program(strict=False) as program:
            done, go, hit = self._watchdog()
            with Rung(done):
                latch(bits[1])
            with Rung(go):
                reset(bits[1])
                copy(1, bits[index])
            with Rung(bits[1]):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_in_a_loop(self) -> None:
        target = Bool("Y")
        with Program(strict=False) as program:
            done, go, hit = self._watchdog()
            with Rung(done):
                latch(target)
            with Rung(go):
                reset(target)
                with forloop(1):
                    latch(target)
            with Rung(target):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_exclusion_in_a_loop(self) -> None:
        a, b = Bool("A", external=True), Bool("Bx", external=True)
        x, y = Bool("X"), Bool("Y")
        with Program(strict=False) as program:
            with Rung(a):
                latch(x)
                reset(y)
                with forloop(1):
                    latch(y)
            with Rung(b):
                latch(y)
                reset(x)
        assert _both(PLC(program, dt=0.01), [({"A": True}, 1)], {"X": True, "Y": True})
        facts = facts_for(prepare_drive(PLC(program, dt=0.01), unlink=None))
        assert exclusion_evidence(facts, ("X", True), ("Y", True)) is None


# ---------------------------------------------------------------------------
# The timer in a branch of the coil's own rung
# ---------------------------------------------------------------------------


def _both_ways_link() -> Any:
    return Bool("Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="En")


class TestABranchOfTheCoilsRung:
    """A branch reads its contacts when execution reaches it, so the rung moves the enable first."""

    def test_a_loop_clears_it_after_the_branch(self) -> None:
        go, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
        feedback = _both_ways_link()
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                out(enable)
                with branch(enable, ~feedback):
                    on_delay(watch, 3000)
                with forloop(1):
                    reset(enable)
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_a_loop_sets_it_after_the_branch(self) -> None:
        go, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
        feedback = _both_ways_link()
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                out(enable)
                with forloop(1):
                    reset(enable)
                with branch(~enable, feedback):
                    on_delay(watch, 3000)
                with forloop(1):
                    latch(enable)
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_a_pointer_clears_it_after_the_branch(self) -> None:
        go, hit = Bool("Go", external=True), Bool("Hit")
        bits = Block("B", TagType.BOOL, 1, 3)
        index = Int("Idx", default=1)
        feedback = Bool(
            "Fb", physical=Physical("Fb", on_delay="100ms", off_delay="100ms"), link="B1"
        )
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                out(bits[1])
                with branch(bits[1], ~feedback):
                    on_delay(watch, 3000)
                copy(0, bits[index])
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is not None
        assert _proved_on(_harnessed(program), hit) is None

    def test_a_separate_timer_rung_keeps_the_proof(self) -> None:
        go, enable, hit = Bool("Go", external=True), Bool("En"), Bool("Hit")
        feedback = _both_ways_link()
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                out(enable)
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 500) is None
        assert _proved_on(_harnessed(program), hit) is not None


# ---------------------------------------------------------------------------
# A pointer's declared range
# ---------------------------------------------------------------------------


_DECLARED_RANGES = [
    pytest.param({"min": 2, "max": 3}, id="min and max"),
    pytest.param({"choices": {2: "two", 3: "three"}}, id="choices"),
]


class TestAPointersDeclaredRangeIsOnlyAWarning:
    """``C[P]`` with P declared 2..3 writes C1 when P holds 1, so C1 has a writer after all."""

    @pytest.mark.parametrize("size", [8, 2000])
    @pytest.mark.parametrize("declared", _DECLARED_RANGES)
    def test_the_program_steps_it_out_of_range(self, declared: dict[str, Any], size: int) -> None:
        go, enable = Bool("Go", external=True), Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        bits = Block("C", TagType.BOOL, 1, size)
        pointer = Int("P", default=2, **declared)
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung(go):
                copy(1, pointer)
                copy(1, bits[pointer])
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(bits[1])
            with Rung(bits[1]):
                latch(hit)
        assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
        assert _proved_on(_harnessed(program), hit) is None
        assert _harnessed(program).how(hit, max_scans=100).status is not PlanStatus.CANNOT_REACH

    def test_a_read_through_the_same_pointer_keeps_the_proof(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit, other = Bool("Hit"), Bool("Other")
        bits = Block("C", TagType.BOOL, 1, 8)
        pointer = Int("P", default=2, min=2, max=3)
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung():
                copy(bits[pointer], other)
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(hit)
        assert _proved_on(_harnessed(program), hit) is not None

    def test_a_pointer_into_another_block_keeps_the_proof(self) -> None:
        enable = Bool("En", external=True)
        feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
        hit = Bool("Hit")
        words = Block("D", TagType.INT, 1, 8)
        pointer = Int("P", default=2, min=2, max=3)
        watch = Timer.clone("W")
        with Program(strict=False) as program:
            with Rung():
                copy(1, words[pointer])
            with Rung(enable, ~feedback):
                on_delay(watch, 3000)
            with Rung(watch.Done):
                latch(hit)
        assert _proved_on(_harnessed(program), hit) is not None


def test_a_loop_body_at_an_index_expression_over_the_cap() -> None:
    go, enable = Bool("Go", external=True), Bool("En", external=True)
    feedback = Bool("Fb", physical=Physical("Fb", on_delay="100ms"), link="En")
    hit = Bool("Hit")
    bits, index, offset = _big_block()
    watch = Timer.clone("W")
    with Program(strict=False) as program:
        with Rung(go), forloop(1):
            copy(1, bits[index + offset])
        with Rung(enable, ~feedback):
            on_delay(watch, 3000)
        with Rung(watch.Done):
            latch(bits[1])
        with Rung(bits[1]):
            latch(hit)
    assert _step_to(_harnessed(program), hit, {"Go": True}, 5) is not None
    assert _proved_on(_harnessed(program), hit) is None


# ---------------------------------------------------------------------------
# A search that ran out of budget
# ---------------------------------------------------------------------------


def test_a_search_cut_short_makes_no_claim() -> None:
    """The proof holds, but with the search unfinished how() says it stopped, as it always has."""
    program, hit, _watch = _watchdog_chain()
    assert _proved_on(_harnessed(program), hit) is not None
    plan = _harnessed(program).how(hit, max_scans=1)
    assert plan.status is PlanStatus.STOPPED, plan.reason
