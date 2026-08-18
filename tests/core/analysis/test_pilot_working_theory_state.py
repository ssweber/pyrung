"""World/knowledge ownership tests for persistent WorkingTheory state."""

from __future__ import annotations

from pyrsistent import pvector

from pyrung import PLC, Bool, Program, out, rung
from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective, TargetSpec
from pyrung.core.analysis.pilot.requirement_evidence import _disposable_requirement_state
from pyrung.core.analysis.pilot.types import _CausalCheckpoint, _PilotState, _World
from pyrung.core.analysis.pilot.working_theory import (
    OpenTheory,
    RecordTheoryAttempt,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryObjectiveSnapshot,
    TheoryState,
    reduce_theory,
)
from pyrung.core.runner import EpochRef


def _execution_ref(label: str) -> EpochRef:
    return EpochRef(int.from_bytes(label.encode(), "big"))


def _boundary(label: str, scan: int = 0) -> TheoryBoundaryIdentity:
    return TheoryBoundaryIdentity(
        world_key=("world", label),
        scan_id=scan,
        owner_ref=_execution_ref(label),
        occurrence_identity=("occurrence", label),
    )


def _opened_theory() -> TheoryState:
    boundary = _boundary("source")
    claim = TheoryClaim(
        source=boundary,
        objective=TheoryObjectiveSnapshot("stepper_complete", True),
        obligations=(),
        selected_boundary=boundary,
    )
    return reduce_theory(
        TheoryState(),
        OpenTheory(claim=claim, opening_identity=("open",), remaining_budget=4),
    )


def _state() -> _PilotState:
    producer = Bool("producer", external=True)
    consumer = Bool("consumer")
    with Program() as logic:
        with rung(producer):
            out(consumer)
    return _PilotState(
        world=_World(
            work=PLC(logic),
            committed_acts=pvector(),
            best_trend=None,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        key_config=None,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )


def test_theory_knowledge_survives_world_restore_and_world_has_no_theory_fields() -> None:
    state = _state()
    theory = _opened_theory()
    state.theory_state = theory
    checkpoint_world = state.snapshot_world()

    state.best_trend = 7
    state.load_world(checkpoint_world)

    assert state.theory_state is theory
    assert state.best_trend is None
    assert not {name for name in _World._precord_fields if "theory" in name.lower()}


def test_disposable_requirement_clone_cannot_mutate_source_theory_state() -> None:
    source = _state()
    source.theory_state = _opened_theory()
    theory_id = source.theory_state.active_theory_id
    assert theory_id is not None
    version_id = source.theory_state.ledger.theories[theory_id].current_version_id
    checkpoint = _CausalCheckpoint(
        key=(),
        world=source.snapshot_world(),
        objective=BearingObjective(TargetSpec("consumer", True)),
    )

    clone = _disposable_requirement_state(source, checkpoint)
    assert clone.theory_state is source.theory_state

    clone.theory_state = reduce_theory(
        clone.theory_state,
        RecordTheoryAttempt(
            theory_id=theory_id,
            version_id=version_id,
            attempt_identity=("clone-attempt",),
            source=_boundary("source"),
            execution_ref=_execution_ref("clone"),
            occurrence_evidence=("occurrence", "clone"),
            act_identity=(("producer", True),),
            pilot_rung_identities=(),
            disposition=TheoryAttemptDisposition.REJECTED_EXACT,
        ),
    )

    assert len(clone.theory_state.ledger.attempts) == 1
    assert not source.theory_state.ledger.attempts
