"""Multi-target ``how(A, B, …)`` — reachable composition + sound ME prunes.

Mirrors the four conveyor pairs from the design spike as small inline fixtures
(see ``scratchpad/multi_target/PLAN.md``): shared-prereq compose, same-tag ME,
cross-tag mutual retentive clobber, and the route-dodge case that separates the
∀ (sound) prune from a naive ∃ prune.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    copy,
    latch,
    out,
    reset,
    rise,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _two_step():
    """Ready latches on Start; Done = out(Ready & Confirm) — Done depends on Ready."""
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            out(Done)
    return prog, Ready, Done


def _shared_prereq():
    """Two latched outputs behind one Running seal-in — compose via shared enable."""
    Start = Bool("Start", external=True)
    A = Bool("A", external=True)
    B = Bool("B", external=True)
    Running = Bool("Running")
    Out1 = Bool("Out1")
    Out2 = Bool("Out2")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
        with Rung(Running, A):
            latch(Out1)
        with Rung(Running, B):
            latch(Out2)
    return prog, Out1, Out2


def _state_machine():
    """PARKED(0) -> RUNNING(1); Flag latched in RUNNING, reset on the way home."""
    Enter = Bool("Enter", external=True)
    Home = Bool("Home", external=True)
    Stage = Int("Stage")  # 0=PARKED, 1=RUNNING
    Flag = Bool("Flag")
    with Program() as prog:
        with Rung(Stage == 0, rise(Enter)):
            copy(1, Stage)
        with Rung(Stage == 1):
            latch(Flag)
        with Rung(Home):
            reset(Flag)
            copy(0, Stage)
    return prog, Stage, Flag


def _dodge():
    """Z reachable two ways: auto-latch in RUNNING (clobbers PARKED) OR manual (clean)."""
    ManualZ = Bool("ManualZ", external=True)
    EnterRun = Bool("EnterRun", external=True)
    Home = Bool("Home", external=True)
    Stage = Int("Stage")  # 0=PARKED, 1=RUNNING
    Z = Bool("Z")
    with Program() as prog:
        with Rung(Stage == 0, rise(EnterRun)):
            copy(1, Stage)
        with Rung(Stage == 1):
            latch(Z)  # route alpha — path drives Stage off PARKED
        with Rung(ManualZ):
            latch(Z)  # route beta — clean
        with Rung(Home):
            reset(Z)
            copy(0, Stage)
    return prog, Z, Stage


# --------------------------------------------------------------------------- #
# reachable composition
# --------------------------------------------------------------------------- #
def test_and_dependent_targets_reachable():
    prog, Ready, Done = _two_step()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Ready, Done)
    assert path.reachable
    result = path.replay()
    assert result.state.tags["Ready"] is True
    assert result.state.tags["Done"] is True


def test_and_shared_prereq_reachable():
    prog, Out1, Out2 = _shared_prereq()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Out1, Out2)
    assert path.reachable
    result = path.replay()
    assert result.state.tags["Out1"] is True
    assert result.state.tags["Out2"] is True


def test_and_route_dodge_reachable():
    """Z + hold Stage==PARKED: the ∀ prune must NOT fire (manual route dodges)."""
    prog, Z, Stage = _dodge()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Z, Stage == 0)
    assert path.reachable
    result = path.replay()
    assert result.state.tags["Z"] is True
    assert result.state.tags["Stage"] == 0


# --------------------------------------------------------------------------- #
# sound ME prunes
# --------------------------------------------------------------------------- #
def test_same_register_two_values_unreachable():
    prog, Stage, Flag = _state_machine()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Stage == 0, Stage == 1)
    assert not path.reachable
    assert "one register" in (path.reason or "")


def test_cross_tag_mutual_clobber_unreachable():
    """Flag lives only in RUNNING; PARKED's producer resets it -> mutual clobber."""
    prog, Stage, Flag = _state_machine()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Flag, Stage == 0)
    assert not path.reachable
    assert "mutually exclusive" in (path.reason or "")


@pytest.mark.parametrize("reverse", [False, True])
def test_joint_goal_chooses_compatible_input_route(reverse):
    """The OR choice for Y must account for X's opposite demand on B."""
    A = Bool("A", external=True)
    B = Bool("B", external=True)
    C = Bool("C", external=True)
    X = Bool("X")
    Y = Bool("Y")
    with Program() as prog:
        with Rung(A, ~B):
            out(X)
        with Rung(Or(B, C)):
            out(Y)
    plc = PLC(prog)
    events = []
    from pyrung.core.analysis.pilot import pilot_how

    goals = (Y, X) if reverse else (X, Y)
    path = pilot_how(plc, *goals, max_scans=30, on_event=events.append)
    assert path.reachable, path.reason
    result = path.replay()
    assert result.state.tags["X"] is True
    assert result.state.tags["Y"] is True
    assert sum(event.kind == "started" for event in events) == 1
    assert sum(event.kind == "finished" for event in events) == 1
    assert plc.state.scan_id == 0


def test_joint_relational_bounds_preserve_their_predicates():
    X = Int("X", external=True, default=5)
    Output = Int("Output")
    with Program() as prog:
        with Rung():
            copy(X, Output)
    path = PLC(prog).how(X > 0, X < 10)
    assert path.reachable, path.reason
    assert path.target_predicate is not None
    assert "X > 0" in str(path)
    assert "X < 10" in str(path)
    assert 0 < path.replay().state.tags["X"] < 10


def test_joint_relational_bounds_are_driven_together():
    X = Int("X", external=True, default=0)
    Output = Int("Output")
    with Program() as prog:
        with Rung():
            copy(X, Output)
    path = PLC(prog).how(X > 3, X < 10, max_scans=30)
    assert path.reachable, path.reason
    assert 3 < path.replay().state.tags["X"] < 10


def test_joint_goal_can_temporarily_displace_a_satisfied_member():
    """A satisfied terminal is not a waypoint that must stay permanently held."""
    B = Bool("B", external=True)
    X = Bool("X")
    Y = Bool("Y")
    with Program() as prog:
        with Rung(~B):
            out(X)
        with Rung(B):
            latch(Y)
    plc = PLC(prog)
    plc.step()
    assert plc.state.tags["X"] is True
    path = plc.how(X, Y, max_scans=30)
    assert path.reachable, path.reason
    result = path.replay()
    assert result.state.tags["X"] is True
    assert result.state.tags["Y"] is True


def test_joint_timer_goals_have_independent_producer_guards():
    from pyrung import Timer, on_delay

    A = Bool("A", external=True)
    B = Bool("B", external=True)
    First = Timer.clone("First")
    Second = Timer.clone("Second")
    with Program() as prog:
        with Rung(A):
            on_delay(First, 30, "ms")
        with Rung(B):
            on_delay(Second, 50, "ms")
    path = PLC(prog, dt=0.010).how(First.Done, Second.Done, max_scans=30)
    assert path.reachable, path.reason
    result = path.replay()
    assert result.state.tags[First.Done.name] is True
    assert result.state.tags[Second.Done.name] is True


@pytest.mark.parametrize("duplicate", [False, True])
def test_joint_goal_recovers_a_terminal_lost_inside_the_scan(duplicate):
    from tests.fixtures import pilot_transient_target_restore as fixture

    second = fixture.State == fixture.TARGET if duplicate else fixture.LaterPresetMs >= 0
    path = PLC(fixture.logic, dt=0.010).how(fixture.State == fixture.TARGET, second, max_scans=16)
    assert path.reachable, path.reason
    result = path.replay()
    assert result.state.tags[fixture.State.name] == fixture.TARGET
    assert result.state.tags[fixture.LaterPresetMs.name] > 10
