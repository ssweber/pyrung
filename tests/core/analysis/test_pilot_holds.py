"""Shared-gate hold pattern ported from test_walk_holds for PILOT.

Two stages sharing a Common gate; Target needs both. StageB seals on
a rising EnableB edge but its seal-in is gated by Common.
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Int, Or, Program, Rung, out, rise
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot._ops import OperationReceipt, PilotRung
from pyrung.core.analysis.pilot.recording import _build_plan_journal
from pyrung.core.analysis.pilot.types import (
    CorrectionStatus,
    _ConfirmedCorrection,
    _CorrectionReceipt,
    _HoldLogEntry,
    _Step,
)
from pyrung.core.runner import PLC


def _shared_gate_program() -> tuple[Program, Bool]:
    EnableA = Bool("EnableA", external=True)
    EnableB = Bool("EnableB", external=True)
    Common = Bool("Common", external=True)
    StageA = Bool("StageA")
    StageB = Bool("StageB")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(EnableA, Common):
            out(StageA)
        with Rung(Or(rise(EnableB), StageB), Common):
            out(StageB)
        with Rung(StageA, StageB):
            out(Target)

    return prog, Target


def _replay(prog: Program, path) -> PLC:
    return path.replay()


def test_shared_gate_premise() -> None:
    """Hold EnableA+Common, then pulse EnableB -> Target."""
    prog, _Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True, "Common": True})
    plc.step()
    assert plc.state.tags["StageA"] is True

    plc.patch({"EnableB": True})
    plc.step()
    assert plc.state.tags["StageB"] is True
    assert plc.state.tags["Target"] is True


def test_shared_gate_solves() -> None:
    """PILOT solves the shared-gate hold pattern."""
    prog, Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Target"] is True


def test_shared_gate_journal_retains_hold_values_and_guards() -> None:
    """Journal construction keeps the exact guarded rule, not just its tag."""
    State = Int("State")
    hold = PilotRung("DoorClosed", True, State != 6)
    state = SimpleNamespace(
        steps=[_Step(inputs={}, scan_before=0, scan_after=10)],
        step_contexts=[],
        lever_notes={},
        hold_log=[
            _HoldLogEntry(
                scan=2,
                source="investigation",
                rungs=(hold,),
            )
        ],
    )

    journal = _build_plan_journal(state, None, frozenset(), frozenset())
    hold_steps = [step for step in journal if step.kind == "force"]

    assert hold_steps
    assert hold_steps[0].rungs[0] is hold
    assert hold_steps[0].source == "investigation"


def test_journal_distinguishes_correction_operation_ownership() -> None:
    """A revoked operation cannot hide an active sibling with another lifetime."""
    State = Int("JournalState")
    guard = State != 6
    revoked = PilotRung(
        "DoorClosed",
        True,
        guard,
        OperationReceipt(State >= 10),
    )
    active = PilotRung(
        "DoorClosed",
        True,
        guard,
        OperationReceipt(State <= 0),
    )

    def _receipt(receipt_id: int, hold: PilotRung, status: CorrectionStatus) -> _CorrectionReceipt:
        correction = _ConfirmedCorrection(
            identity=((hold.dest, hold.value),),
            rungs=(hold,),
            sources=(hold.dest,),
            justification="test",
        )
        return _CorrectionReceipt(receipt_id, (), correction, status)

    state = SimpleNamespace(
        steps=[_Step(inputs={}, scan_before=0, scan_after=10)],
        step_contexts=[],
        lever_notes={},
        hold_log=[
            _HoldLogEntry(scan=2, source="investigation", rungs=(revoked,)),
            _HoldLogEntry(scan=3, source="investigation", rungs=(active,)),
        ],
        correction_receipts=[
            _receipt(1, revoked, CorrectionStatus.REVOKED),
            _receipt(2, active, CorrectionStatus.ACTIVE),
        ],
    )

    journal = _build_plan_journal(state, None, frozenset(), frozenset())
    force_steps = [step for step in journal if step.kind == "force"]

    assert [step.rungs for step in force_steps] == [(active,)]
