"""Multi-target ``how(A, B, …)`` — reachable composition + sound ME prunes.

Mirrors the four conveyor pairs from the design spike as small inline fixtures
(see ``scratchpad/multi_target/PLAN.md``): shared-prereq compose, same-tag ME,
cross-tag mutual retentive clobber, and the route-dodge case that separates the
∀ (sound) prune from a naive ∃ prune.
"""

from __future__ import annotations

from pyrung.core import (
    PLC,
    Bool,
    Int,
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
