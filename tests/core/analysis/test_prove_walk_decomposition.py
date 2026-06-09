"""Multi-corridor decomposition: serial-clobber recovery in the walker.

The corridor walker (`walk.py`) discovers prerequisites and walks them
serially on a shared fork.  This fails when walking one prerequisite
creates a side effect that breaks a condition needed by another — the
"serial clobber."  These tests pin the premise (the target IS
forward-reachable) and the walker's expected recovery.

See ``scratchpad/corridor_walker_plan.md`` and the plan
``do-you-have-what-purring-cat.md``.
"""

from __future__ import annotations

from pyrung import (
    Bool,
    Or,
    Program,
    Rung,
    Timer,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.prove import walk
from pyrung.core.runner import PLC


def _clobber_program() -> tuple[Program, Bool]:
    """Two latches feeding a target, where sealing the second clobbers the first.

    - ``Latch_A`` seals on a rising ``Input_A`` edge, held by seal-in, but its
      rung is gated by ``~Blocker`` — so once ``Blocker`` seals, ``Latch_A`` drops.
    - ``Latch_B`` seals once a 0.100 s on-delay (driven by ``Input_B``) completes.
    - ``Blocker`` seals on a rising ``Input_B`` edge — a side effect of the very
      action that arms ``Latch_B`` — and is cleared only by ``Reset_Cmd``.
    - ``Target`` requires both latches True simultaneously.

    Walking ``Latch_B`` therefore clobbers ``Latch_A``: the serial walk lands in
    a state where ``Target`` is unreachable in a single further steer.
    """
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Blocker = Bool("Blocker")
    TimerB = Timer.clone("TimerB")
    Target = Bool("Target")

    with Program() as prog:
        # Latch A: seal-in, broken by Blocker.
        with Rung(Or(rise(Input_A), Latch_A), ~Blocker):
            out(Latch_A)
        # Latch B: timer-gated seal-in (multi-scan walk so Blocker takes effect).
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")  # 0.100 s = 10 scans at dt=0.010
        with Rung(Or(TimerB.Done, Latch_B)):
            out(Latch_B)
        # Blocker: latched side effect of Input_B, clearable via Reset_Cmd.
        with Rung(Or(rise(Input_B), Blocker), ~Reset_Cmd):
            out(Blocker)
        # Target.
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def test_clobber_is_forward_reachable() -> None:
    """Premise: the correct action sequence drives Target=True on a real PLC.

    1. Pulse Input_A   -> Latch_A seals
    2. Hold Input_B    -> Blocker seals (clobbers Latch_A), timer completes,
                          Latch_B seals
    3. Pulse Reset_Cmd -> Blocker drops
    4. Pulse Input_A   -> Latch_A re-seals
    5. Target = Latch_A AND Latch_B = True
    """
    prog, _Target = _clobber_program()
    plc = PLC(prog, dt=0.010)

    # 1. Pulse Input_A -> Latch_A seals.
    plc.patch({"Input_A": True})
    plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True
    assert plc.state.tags["Blocker"] is False

    # 2. Hold Input_B high until the on-delay completes -> Latch_B seals.
    #    The rising edge also seals Blocker, which clobbers Latch_A.
    plc.patch({"Input_B": True})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Latch_B"] is True
    assert plc.state.tags["Blocker"] is True
    assert plc.state.tags["Latch_A"] is False  # clobbered
    plc.patch({"Input_B": False})
    plc.step()

    # 3. Pulse Reset_Cmd -> Blocker drops.
    plc.patch({"Reset_Cmd": True})
    plc.step()
    plc.patch({"Reset_Cmd": False})
    plc.step()
    assert plc.state.tags["Blocker"] is False
    assert plc.state.tags["Latch_B"] is True  # self-sealed, unaffected

    # 4. Pulse Input_A -> Latch_A re-seals (Blocker now clear).
    plc.patch({"Input_A": True})
    plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True

    # 5. Target reached.
    assert plc.state.tags["Target"] is True


def test_serial_clobber_walker_recovers() -> None:
    """The walker recovers from the serial clobber and reaches Target.

    The oracle-driven re-check loop in ``_walk_to_goal`` walks ``Latch_B``
    (clobbering ``Latch_A``), then asks ``cause(Target, to=True)`` what still
    blocks the target and re-walks ``Latch_A`` (clearing ``Blocker`` first).
    """
    prog, Target = _clobber_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Target)
    assert path.reachable


def test_serial_clobber_replay() -> None:
    """The recovered plan replays to Target=True on a fresh PLC."""
    prog, Target = _clobber_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Target)
    assert path.reachable

    replay = PLC(prog, dt=0.010)
    for step in path.steps:
        replay.patch(step.action)
        for _ in range(step.scans):
            replay.step()
    assert replay.state.tags["Target"] is True


def test_needs_decomposition_detects_overlap() -> None:
    """The two latch prerequisites couple through a shared upstream cone.

    Both ``Latch_A`` (via ``Blocker``) and ``Latch_B`` (via its timer) depend on
    ``Input_B``, so walking them serially cannot avoid the clobber — exactly the
    case ``_needs_decomposition`` flags for Tier 2 force-and-solve.
    """
    prog, _Target = _clobber_program()
    pdg = build_program_graph(prog)

    prereqs = [("Latch_A", True), ("Latch_B", True)]
    needs_decomp, detail = walk._needs_decomposition(prereqs, "Target", pdg)
    assert needs_decomp
    assert detail is not None
    assert "Latch_A" in detail and "Latch_B" in detail

    # A single prerequisite cannot couple with anything.
    assert walk._needs_decomposition([("Latch_A", True)], "Target", pdg) == (False, None)
