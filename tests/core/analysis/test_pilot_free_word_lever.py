"""Heuristic steerable values for free numeric words in ``how()`` — the fill gate.

The fill shape: a step register advances through an ``on_delay`` gated on
``PV < Lower`` where both compare operands are internal calc registers.  ``PV``'s
writer is a two-tag calc (``Raw - Level``) so the equality chase can't invert it;
``Lower = calc(SetPoint - Band)`` where ``Band`` is a steerable Real with **no
declared domain** — the "free word".  Statically every resolution punts, so
pre-fix the drive stops without finding the relational boundary for ``Band``.

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

from pyrung import PLC, Bool, Int, Or, Program, Real, Rung, Timer, calc, copy, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.sp_values import _writer_projection, _written_value_for_tag


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


def _transient_stepper_program() -> tuple[Program, object, dict[str, object]]:
    """Fill-shaped stepper with semantic odd states and transient even ticks."""
    Level = Real("Level", external=True, default=0.0, min=0.0, max=100.0)
    SetPoint = Real("SetPoint", external=True, default=100.0, min=0.0, max=100.0)
    Band = Real("Band", external=True, default=0.0)
    PV = Real("PV", min=0.0, max=100.0)
    Lower = Real("Lower")
    HMI_on = Bool("HMI_on", external=True)
    Reset = Bool("Reset", external=True)
    ManualFill = Bool("ManualFill", external=True)
    Status = Int("Status")
    StatusShot = Bool("StatusShot")
    IsEven = Int("IsEven")
    Step = Int("Step", default=1)
    SubOff = Bool("SubOff")
    SubOn = Bool("SubOn")
    SubFilling = Bool("SubFilling")
    Dwell = Timer.clone("Dwell")
    Filling = Bool("Filling")

    with Program() as prog:
        with Rung():
            calc(100.0 - Level, PV)
            calc(SetPoint - Band, Lower)
            calc(Step % 2, IsEven)
        with Rung(Status == 1):
            out(StatusShot, oneshot=True)
        with Rung(Or(StatusShot, IsEven != 1)):
            calc(Step + 1, Step)
        with Rung(StatusShot):
            copy(0, Status)
        with Rung(Step == 1):
            out(SubOff)
        with Rung(Step == 3):
            out(SubOn)
        with Rung(Step == 5):
            out(SubFilling)
        with Rung(Step == 5, Reset):
            calc(Step - 1, Step, oneshot=True)
        with Rung(SubOff, HMI_on):
            copy(1, Status, oneshot=True)
        with Rung(SubOn, PV < Lower):
            on_delay(Dwell, 10, "ms")
        with Rung(SubOn, Dwell.Done):
            copy(1, Status)
        with Rung(Or(SubFilling, ManualFill)):
            out(Filling)

    return prog, Filling, {"Reset": Reset, "ManualFill": ManualFill}


def test_transient_stepper_ranks_current_state_tools() -> None:
    """The reset/decrement writer is an edge from 5->4, not a tool at state 3."""
    from pyrung.core.analysis.pilot.trace import (
        _rank_writers,
        _writer_availability,
        _WriterAvailability,
    )
    from pyrung.core.analysis.steerable import compute_steerable

    prog, _target, _tags = _transient_stepper_program()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, PLC(prog)._known_tags_by_name, prog)
    snapshot = {
        "Step": 3,
        "IsEven": 1,
        "StatusShot": False,
        "Reset": False,
        "Status": 0,
    }

    writers = pdg.writers_of.get("Step", frozenset())
    inc_rung = next(
        i for i in writers if {"StatusShot", "IsEven"} <= pdg.rung_nodes[i].condition_reads
    )
    reset_rung = next(i for i in writers if {"Step", "Reset"} <= pdg.rung_nodes[i].condition_reads)

    ranked = _rank_writers(
        writers,
        pdg,
        prog,
        "Step",
        4,
        snapshot,
        steerable=steerable,
        ancestry=(("Step", 5),),
    )
    assert ranked[0] == inc_rung
    assert ranked.index(inc_rung) < ranked.index(reset_rung)

    reset_ro = resolve_rung(prog, pdg.rung_nodes[reset_rung])
    assert reset_ro is not None
    reset_wv = _written_value_for_tag(reset_ro, "Step")
    assert (
        _writer_availability(
            reset_ro,
            pdg.rung_nodes[reset_rung],
            reset_wv,
            "Step",
            4,
            snapshot,
            pdg,
            prog,
            steerable,
            frozenset(),
            False,
        )
        == _WriterAvailability.UNAVAILABLE_FROM_HERE
    )
    assert (
        _writer_availability(
            reset_ro,
            pdg.rung_nodes[reset_rung],
            reset_wv,
            "Step",
            4,
            {**snapshot, "Step": 5},
            pdg,
            prog,
            steerable,
            frozenset(),
            False,
        )
        == _WriterAvailability.AVAILABLE_NOW
    )

    inc_ro = resolve_rung(prog, pdg.rung_nodes[inc_rung])
    assert inc_ro is not None
    assert _writer_projection(inc_ro, "Step", 4, snapshot, pdg, prog, {}, frozenset()) == (
        False,
        ["StatusShot"],
    )


def test_transient_stepper_solves_through_available_dwell_route() -> None:
    """End to end: the fill-shaped stepper reaches Filling without reset."""
    prog, target, tags = _transient_stepper_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(target, avoid=tags["ManualFill"], max_scans=3000)
    assert path.reachable, path.reason

    replay = path.replay()
    assert replay.state.tags["Filling"] is True

    journal_inputs = [(t, v) for step in path.journal for t, v in step.inputs]
    assert all(t != "Reset" for t, _v in journal_inputs)
    notes = [note for step in path.journal for note in step.notes]
    assert any("PV" in n and "Lower" in n for n in notes)
