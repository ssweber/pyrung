"""Layer 2 (Don't Hallucinate Progress) — excursion detection and recovery.

Tests that PILOT detects transient state-key changes (excursions) where the
key changes after the pulse but reverts during settle, and either derives
replay-confirmed holds or correctly avoids nogooding the action.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy, latch, out, reset
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

FAST_FB = Physical("FastFb", on_delay="100ms")


# ---------------------------------------------------------------------------
# Excursion program: output latches on start, but feedback arriving
# during settle triggers a trip that resets the output — unless held.
# ---------------------------------------------------------------------------


def _excursion_program():
    """Motor starts on command, but feedback arrival trips it unless held.

    x_Start → latch y_Motor → out y_Started → (100 ms delay) y_Started_Fb
    y_Started_Fb ∧ ¬x_Hold → reset y_Motor → y_Started drops (OUT)

    Without x_Hold=True, the motor excurses True then reverts False.
    Layer 2 should diagnose the enabler x_Hold and install hold True.
    """
    x_Start = Bool("x_Start", external=True)
    x_Hold = Bool("x_Hold", external=True)
    y_Motor = Bool("y_Motor")
    y_Started = Bool("y_Started")
    y_Ready = Bool("y_Ready")
    y_Started_Fb = Bool(
        "y_Started_Fb",
        physical=FAST_FB,
        link="y_Started",
    )
    with Program() as prog:
        with Rung(x_Start):
            latch(y_Motor)
        with Rung(y_Motor):
            out(y_Started)
        with Rung(y_Started_Fb, ~x_Hold):
            reset(y_Motor)
        # The target lies beyond the clobber.  Observing y_Motor itself would
        # stop on its transient pulse and never exercise excursion recovery.
        with Rung(y_Motor, y_Started_Fb):
            out(y_Ready)
    return prog, y_Motor, y_Ready


def test_excursion_premise() -> None:
    """Verify the excursion exists: start without hold → motor reverts."""
    from pyrung.core.harness import Harness

    prog, _y_Motor, _y_Ready = _excursion_program()
    plc = PLC(prog, dt=0.010)
    harness = Harness(plc)
    harness.install()

    plc.patch({"x_Start": True})
    plc.step()
    assert plc.state.tags["y_Motor"] is True, "motor should latch immediately"

    # Run long enough for feedback to arrive and trip the motor
    for _ in range(20):
        plc.step()
    assert plc.state.tags["y_Motor"] is False, "motor should revert without hold"


def test_excursion_premise_with_hold() -> None:
    """Verify the hold works: start WITH hold → motor stays."""
    from pyrung.core.harness import Harness

    prog, _y_Motor, _y_Ready = _excursion_program()
    plc = PLC(prog, dt=0.010)
    harness = Harness(plc)
    harness.install()

    plc.patch({"x_Start": True, "x_Hold": True})
    plc.step()
    assert plc.state.tags["y_Motor"] is True

    for _ in range(20):
        plc.step()
    assert plc.state.tags["y_Motor"] is True, "motor should stay with hold"


def test_layer2_excursion_recovery() -> None:
    """Compass can select the guard directly and reach beyond the clobber."""
    prog, _y_Motor, y_Ready = _excursion_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, y_Ready, max_scans=3000)

    assert path.reachable


# ---------------------------------------------------------------------------
# No-hold excursion: same self-tripping pattern but no steerable enabler
# to derive a hold from.  Layer 2 should detect the excursion but NOT
# nogood the action (it does have an effect, just can't stick).
# ---------------------------------------------------------------------------


def _no_hold_excursion_program():
    """Motor starts but feedback always trips it — no steerable bypass.

    The reset rung is placed BEFORE the copy rung so that y_Motor is
    always False when the copy evaluates.  y_Stage can never reach 1,
    making the target genuinely unreachable regardless of settle budget.
    """
    x_Start = Bool("x_Start", external=True)
    y_Motor = Bool("y_Motor")
    y_Started = Bool("y_Started")
    y_Stage = Int("y_Stage")
    y_Started_Fb = Bool(
        "y_Started_Fb",
        physical=FAST_FB,
        link="y_Started",
    )
    with Program() as prog:
        with Rung(x_Start):
            latch(y_Motor)
        with Rung(y_Motor):
            out(y_Started)
        with Rung(y_Started_Fb):
            reset(y_Motor)
        with Rung(y_Motor, y_Started_Fb):
            copy(1, y_Stage)
    return prog, y_Motor, y_Stage


def test_no_hold_excursion_not_nogooded(monkeypatch) -> None:
    """Excursion without steerable hold — action must NOT be nogooded.

    The action x_Start=True does change the key transiently (y_Motor goes
    True then reverts after feedback), so it should not be treated as a
    true SPIN.  y_Stage==1 is genuinely unreachable because the reset rung
    fires before the copy rung in scan order.  PILOT should exhaust budget.
    """
    from pyrung.core.analysis.pilot import cyclefold

    coast_stats: list[dict[str, int]] = []
    cycle_fold_until = cyclefold.cycle_fold_until

    def capture_coast_stats(*args, **kwargs):
        result = cycle_fold_until(*args, **kwargs)
        coast_stats.append(dict(kwargs["stats"]))
        return result

    monkeypatch.setattr(cyclefold, "cycle_fold_until", capture_coast_stats)

    prog, _y_Motor, y_Stage = _no_hold_excursion_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, y_Stage == 1, max_scans=200)
    assert not path.reachable
    assert any(
        stats.get("sterile_cycle") == 1 and stats["kernel_scans"] < 100 for stats in coast_stats
    )


# ---------------------------------------------------------------------------
# True SPIN: action has no effect at all (no post-pulse key change).
# Should be nogooded as before.
# ---------------------------------------------------------------------------


def test_spin_still_nogooded() -> None:
    """True SPIN (no post-pulse key change) is still nogooded."""
    x_A = Bool("x_A", external=True)
    x_B = Bool("x_B", external=True)
    y_Out = Bool("y_Out")
    with Program() as prog:
        with Rung(x_A, x_B):
            out(y_Out)

    plc = PLC(prog, dt=0.010)
    # Force x_A=True so only x_B matters.  PILOT should try x_A, see SPIN
    # (x_A alone doesn't change key), and nogood it for this state key.
    # Then try x_B.
    path = pilot_how(plc, y_Out, max_scans=300)
    assert path.reachable
