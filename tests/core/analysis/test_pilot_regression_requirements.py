"""Bounded executor contracts for exact regression corrections."""

from __future__ import annotations

from types import SimpleNamespace

from pyrsistent import pvector

from pyrung import Bool, Program, latch, out, reset, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.correction_candidates import correction_identity
from pyrung.core.analysis.pilot.correction_records import _ConfirmedCorrection
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    Pulse,
    TargetSpec,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.regression_requirements import (
    _confirmed_correction_requirement_from_excursion,
    _ExactRegressionCorrection,
    _ordinary_correction_order,
    _tentative_rung_prevents_completion,
)
from pyrung.core.analysis.pilot.requirements import (
    OperandAuthority,
    classify_guard_operand_authority,
)
from pyrung.core.analysis.pilot.types import _ExecutedAttempt, _PulseState
from pyrung.core.analysis.pilot.working_theory import TheoryState
from pyrung.core.analysis.pilot.world import _CausalCheckpoint, _World
from pyrung.core.runner import PLC


def _correction(*holds: PilotRung) -> _ExactRegressionCorrection:
    return _ExactRegressionCorrection(
        holds=holds,
        done_tag="Done",
        obstruction=object(),
        execution_owner=object(),
        channel_tag="State",
        departure_scan=1,
        causal_spine=frozenset(),
        hypothesis_kind="latch-exposure",
    )


def test_ordinary_validation_prefers_an_existing_coordinated_cut() -> None:
    scope = Bool("CoordinatedCutScope", external=True)
    door = PilotRung("Door", True, scope)
    lint = PilotRung("Lint", True, scope)
    door_only = _correction(door)
    lint_only = _correction(lint)
    coordinated = _correction(door, lint)

    ordered = _ordinary_correction_order((door_only, lint_only, coordinated))

    assert ordered == (coordinated, door_only, lint_only)


def test_ordinary_validation_does_not_union_alternative_corrections() -> None:
    scope = Bool("AlternativeCutScope", external=True)
    door = _correction(PilotRung("Door", True, scope))
    bypass = _correction(PilotRung("Bypass", False, scope))

    assert _ordinary_correction_order((door, bypass)) == (door, bypass)


def test_exact_excursion_evidence_becomes_an_inert_working_theory_requirement() -> None:
    reset_request = Bool("ExcursionReset", external=True)
    preserved = Bool("ExcursionPreserved", external=True)
    with Program(strict=False) as program:
        with rung(reset_request):
            reset(preserved)

    source = PLC(program)
    work = source.fork()
    before = dict(work.state.tags)
    work.patch({preserved.name: True, reset_request.name: False})
    work.step()
    post_pulse = dict(work.state.tags)
    assert post_pulse[preserved.name] is True
    work.patch({reset_request.name: True})
    work.step()
    assert work.state.tags[preserved.name] is False

    policy = ActPolicy(
        source=ActSource.TRACE,
        action_pairs=((preserved.name, True),),
        applied=((preserved.name, True),),
    )
    bearing = Bearing(
        ("source",),
        Pulse(policy),
        BearingObjective(TargetSpec(preserved.name, True)),
    )
    pulse = _PulseState(
        fork=work,
        scan_before=0,
        action_scan=1,
        action_snap=post_pulse,
        wait_snaps=(),
        post_pulse_snap=post_pulse,
        post_pulse_key=("post-pulse",),
        snap=dict(work.state.tags),
        key=("landing",),
        kernel_scan_ids=(1, 2),
        source_snap=before,
    )
    executed = _ExecutedAttempt(pulse, bearing)
    corrective = PilotRung(reset_request.name, False, preserved)
    correction = _ConfirmedCorrection(
        identity=correction_identity((corrective,)),
        pilot_rungs=(corrective,),
        sources=(reset_request.name,),
        justification="exact excursion evidence",
    )
    checkpoint = _CausalCheckpoint(
        key=("source",),
        world=_World(
            work=source,
            committed_acts=pvector(),
            best_trend=0,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        objective=bearing.objective,
    )
    state = SimpleNamespace(theory_state=TheoryState())
    ctx = SimpleNamespace(
        steerable=frozenset((reset_request.name, preserved.name)),
        pdg=build_program_graph(program),
        configured_inputs=frozenset(),
    )
    projection = executed.projection_at(2)
    assert projection is not None
    assert pulse.projection_replay_count == 1
    assert work._causal_lineage.owner_at(2) is not None
    assert correction.identity == correction_identity((corrective,))
    assert (
        classify_guard_operand_authority(
            reset_request.name,
            steerable=ctx.steerable,
            program_written=frozenset(ctx.pdg.writers_of),
        )
        is OperandAuthority.ADJUSTABLE
    )
    assert tuple(
        (
            write.transition.tag_name,
            write.transition.from_value,
            write.transition.to_value,
        )
        for write in projection.writes
    ) == ((preserved.name, True, False),)

    requirement = _confirmed_correction_requirement_from_excursion(
        state,
        ctx,
        executed,
        correction,
        checkpoint,
        (preserved.name,),
    )

    assert requirement is not None
    assert requirement.provenance == "exact-excursion-legacy-receipt"
    assert requirement.obstruction_occurrence is not None
    assert requirement.obstruction_occurrence.scan_id == 2
    assert requirement.obstruction_occurrence.tag == preserved.name
    assert requirement.corrective_pilot_rungs == (corrective,)
    assert pulse.projection_replay_count == 1
    assert checkpoint.world.pilot_rungs == pvector()
    assert (
        _confirmed_correction_requirement_from_excursion(
            state,
            ctx,
            executed,
            correction,
            checkpoint,
            (preserved.name, reset_request.name),
        )
        is None
    )


def _two_scan_program(*, escaping_reader: bool) -> tuple[Program, dict[str, Bool]]:
    source = Bool("RegressionSource", external=True)
    mapped = Bool("RegressionMapped")
    armed = Bool("RegressionArmed")
    error = Bool("RegressionError")
    fault = Bool("RegressionFault")
    unrelated_gate = Bool("RegressionUnrelatedGate")
    unrelated_effect = Bool("RegressionUnrelatedEffect")

    with Program(strict=False) as program:
        # Error is a retained one-scan pipeline stage.  Source must therefore
        # be established early enough to clear Error before the next scan's
        # fault consumer, matching Sail's x_ -> i_ -> error -> timer pipeline.
        with rung(source):
            out(mapped)
        with rung(armed, error):
            out(fault)
        with rung(~mapped):
            out(error)
        with rung(~armed):
            latch(armed)
        if escaping_reader:
            with rung(source):
                out(unrelated_gate)
            with rung(unrelated_gate):
                out(unrelated_effect)

    return program, {
        "source": source,
        "mapped": mapped,
        "armed": armed,
        "error": error,
        "fault": fault,
    }


def _proof(*, escaping_reader: bool, source_scan: int):
    program, tags = _two_scan_program(escaping_reader=escaping_reader)
    baseline = PLC(program)
    baseline.step()
    baseline.step()
    projection = baseline._replay_pilot_rung_write_projection_at(2)
    assert projection is not None
    obstructions = tuple(
        write
        for write in projection.writes
        if write.transition.tag_name == tags["fault"].name and write.transition.to_value is True
    )
    assert len(obstructions) == 1

    source = PLC(program)
    for _ in range(source_scan):
        source.step()
    correction = PilotRung(tags["source"].name, True, ~tags["source"])
    evidence = _ExactRegressionCorrection(
        holds=(correction,),
        done_tag=tags["fault"].name,
        obstruction=obstructions[0],
        execution_owner=object(),
        channel_tag=None,
        departure_scan=2,
        causal_spine=frozenset(
            {
                tags["source"].name,
                tags["mapped"].name,
                tags["armed"].name,
                tags["error"].name,
                tags["fault"].name,
            }
        ),
        hypothesis_kind="absence-root",
    )
    return _tentative_rung_prevents_completion(
        SimpleNamespace(work=baseline),
        SimpleNamespace(
            program=program,
            pdg=build_program_graph(program),
        ),
        SimpleNamespace(),
        evidence,
        SimpleNamespace(
            world=SimpleNamespace(work=source, pilot_rungs=()),
        ),
    )


def test_tentative_rung_uses_the_minimum_two_scan_executor_pipeline() -> None:
    one_scan = _proof(escaping_reader=False, source_scan=1)
    two_scans = _proof(escaping_reader=False, source_scan=0)

    assert one_scan.admitted is False
    assert one_scan.reason == "the exact harmful occurrence still occurred"
    assert two_scans.admitted is True
    assert two_scans.scans == (1, 2)
    assert two_scans.reason == "tentative rung suppressed the exact harmful occurrence"


def test_tentative_rung_allows_other_changes_for_ordinary_execution_to_judge() -> None:
    proof = _proof(escaping_reader=True, source_scan=0)

    assert proof.admitted is True
    assert proof.reason == "tentative rung suppressed the exact harmful occurrence"
