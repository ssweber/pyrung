"""Heuristic steerable values for free numeric words in ``how()`` — the fill gate.

The fill shape: a step register advances through an ``on_delay`` gated on
``PV < Lower`` where both compare operands are internal calc registers.  ``PV``'s
writer is a two-tag calc (``Raw - Level``) so the equality chase can't invert it;
``Lower = calc(SetPoint - Band)`` where ``Band`` is a steerable Real with **no
declared domain** — the "free word".  Statically every resolution punts, so
pre-fix the drive dies with the skiff's free-word decline naming ``Band``.

The target is hand-driveable (ground truth below), so ``how()`` must not fail and
blame the missing domain: for an *ordered* comparison the trace may propose a
heuristic boundary value on a steerable free word — replay-verified through the
normal Act→Verify pipeline, reported relationally (the relation is the
requirement; the value is an example).  Equality/mask-shaped free-word gates keep
the honest ``choices=`` decline (``test_pilot_sandbox_gate.py``).

The machine-local fill project is the live tier of this gate
(``scratchpad/burner/repro_fill_free_word.py``, not CI).
"""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Real, Rung, Timer, calc, copy, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.simplified import Atom


def _fill_band_program() -> tuple[Program, object]:
    """Synthetic replica of the live fill-station free-word shape.

    ``PV = calc(Raw - Level)`` (``Raw`` pinned to 100 by logic, ``Level`` a
    bounded external Real) mirrors ``pv_LevelHt = calc(100 - systemLevel)``;
    the two-tag writer keeps the equality chase honest (no single-source affine
    inversion escape).  ``Lower = calc(SetPoint - Band)`` with ``Band``
    undeclared is the free word.  The dwell + step register is the fill stepper
    in miniature.
    """
    Level = Real("Level", external=True, default=0.0, min=0.0, max=100.0)
    SetPoint = Real("SetPoint", external=True, default=0.0, min=0.0, max=100.0)
    Band = Real("Band", external=True, default=0.0)  # the free word: NO domain
    Raw = Real("Raw")
    PV = Real("PV", min=0.0, max=100.0)
    Lower = Real("Lower")
    HMI_on = Bool("HMI_on", external=True)
    Step = Int("Step", default=1)
    Dwell = Timer.clone("Dwell")
    Filling = Bool("Filling")

    with Program() as prog:
        with Rung():
            copy(100.0, Raw)
            calc(Raw - Level, PV)
            calc(SetPoint - Band, Lower)
        with Rung(HMI_on, Step == 1, PV < Lower):
            on_delay(Dwell, 50, "ms")
        with Rung(Dwell.Done):
            copy(2, Step)
        with Rung(Step == 2):
            out(Filling)

    return prog, Filling


def test_fill_band_hand_driveable() -> None:
    """Ground truth: the target is reachable by hand, so declining is a miss."""
    prog, _ = _fill_band_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"HMI_on": True, "Level": 100.0, "SetPoint": 100.0})
    for _ in range(12):
        plc.step()
    assert plc.state.tags["Filling"] is True


def test_fill_band_static_punts() -> None:
    """The sound resolver stays sound: a literal-operand inequality on the
    domain-less free word has no derivable value — ``None`` (the escalation
    trigger for the heuristic stage, which must NOT live in this function)."""
    from pyrung.core.analysis.pilot.trace import _resolve_inequality_target

    prog, _ = _fill_band_program()
    pdg = build_program_graph(prog)

    atom = Atom(tag="Band", form="lt", operand=-100.0)
    snapshot = {"Band": 0.0, "SetPoint": 0.0, "PV": 100.0}
    assert _resolve_inequality_target(atom, snapshot, None, pdg) is None


def test_fill_band_solves_with_heuristic_lever() -> None:
    """``how(Filling)`` proposes a heuristic boundary value on a steerable free
    word instead of declining, and the replay confirms the drive."""
    prog, target = _fill_band_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, target, max_scans=2000)
    assert path.reachable, path.reason

    replay = path.replay()
    assert replay.state.tags["Filling"] is True


def test_fill_band_reports_relational_hold() -> None:
    """The plan reports the heuristic hold *relationally*: the journal step
    carries the relation and an "e.g." example value — the relation is the
    requirement, the number is an example."""
    prog, target = _fill_band_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, target, max_scans=2000)
    assert path.reachable, path.reason

    notes = [note for step in path.journal for note in step.notes]
    heuristic_notes = [n for n in notes if "e.g." in n]
    assert heuristic_notes, f"no relational note in journal: {path.journal!r}"
    assert any("relation is the requirement" in n for n in heuristic_notes)
    # The note names the relation it satisfies (the live compare's operands).
    assert any("PV" in n and "Lower" in n for n in heuristic_notes)
