"""Immutable candidate readings produced for one Pilot orientation.

These records carry completed route, wait, prerequisite, continuation, and
action reads. Candidate construction belongs to options.py; Orientation owns
the declared precedence among the resulting proposals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeAlias

from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    ChannelHeading,
    CrossingFidelity,
    LandingReceiptAuthority,
    RouteEdgeContext,
    _ActionPair,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.pipeline_graph import StaticPath
    from pyrung.core.analysis.pilot.program_step import ProgramStep
    from pyrung.core.analysis.pilot.trace_tree import TraceAction

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One action option with exactly one provenance category."""

    tag: str
    value: Any
    source: ActSource
    provenance: tuple[str, ...] = ()
    downstream_reach: int | None = None
    # The first compass edge's executable promise. Trial verification uses this for every
    # route-prescribed action, not only a bearing coast: landing elsewhere means
    # the program displaced the route and must be investigated.
    bearing_channel_tag: str | None = None
    bearing_channel_value: Any = None
    bearing_boundary: Any = None
    route_context: RouteEdgeContext | None = None
    # A program-awaited action (awaited_actions.py): the one operator action the program
    # is dwelling on at the current state of an opaque-loop channel, surfaced when
    # the trace dead-ends and the compass route is the avoided command.  Ordered
    # like a prescribed edge (a recognized bearing), but below static-route and
    # learned-action evidence, so it never overrides an available route.
    awaited_action_note: str = ""
    # An external input required by the exact program producer selected for an
    # automatic route edge. It is a current-world bearing below an established
    # route/awaited-action evidence and above an unrelated trace action.
    program_note: str = ""
    program_context_actions: tuple[_ActionPair, ...] = ()
    # The exact consumer-relative reason this candidate was selected.  This is
    # minted once from the rich trace receipt and must survive every lowering.
    expectation: EffectExpectation | None = None

    @property
    def pair(self) -> _ActionPair:
        return (self.tag, self.value)

    @property
    def route_prescribed(self) -> bool:
        return self.source is ActSource.ROUTE

    @property
    def learned_prescribed(self) -> bool:
        return self.source is ActSource.LEARNED_ACTION

    @property
    def awaited_action_prescribed(self) -> bool:
        return self.source is ActSource.AWAITED_ACTION

    @property
    def program_prescribed(self) -> bool:
        return self.source is ActSource.PROGRAM


@dataclass(frozen=True)
class WaitPrescription:
    """One valid-by-construction wait bearing."""

    heading: ChannelHeading | None
    reason: str | None = None
    frontier: tuple[_ActionPair, ...] = ()
    expectation: EffectExpectation | None = None
    landing_receipt_authority: LandingReceiptAuthority = LandingReceiptAuthority.ORIENTATION


@dataclass(frozen=True)
class WaitRead:
    """One wait prescription together with every action its read discovered.

    The prescription cannot cross candidate construction on its own.  Its
    completion and exact-producer details travel with it into one ordinary
    admission pass.
    """

    prescription: WaitPrescription | None
    details: tuple[TraceAction, ...] = ()
    declined_reason: str | None = None
    program_step: ProgramStep | None = None
    declined_frontier: tuple[_ActionPair, ...] = ()

    @property
    def reason(self) -> str | None:
        return self.prescription.reason if self.prescription is not None else self.declined_reason

    @property
    def frontier(self) -> tuple[_ActionPair, ...]:
        return (
            self.prescription.frontier if self.prescription is not None else self.declined_frontier
        )

    def without_prescription(self) -> WaitRead:
        """Remove coast authorization without discarding the reading's evidence."""

        if self.prescription is None:
            return self
        return replace(
            self,
            prescription=None,
            declined_reason=self.reason,
            declined_frontier=self.frontier,
        )


@dataclass(frozen=True)
class _TraceAdmission:
    """One application of the candidate pool's ordinary admission rules."""

    active_actions: tuple[_ActionPair, ...]
    actions: tuple[_ActionPair, ...]
    details: tuple[TraceAction, ...]
    detail_by_pair: Mapping[_ActionPair, TraceAction]
    managed_boolean_rungs: tuple[PilotRung, ...]
    establish_pending: bool
    # All current-world trace readings before pair nogoods remove executable
    # singletons. Typed theory retries may re-resolve their exact rejected
    # trigger here while every companion still passes ordinary admission.
    read_details: tuple[TraceAction, ...] = ()


@dataclass(frozen=True)
class _AdmittedWait:
    """A complete wait read after the candidate pool admitted its details."""

    read: WaitRead
    admission: _TraceAdmission

    @property
    def admitted_pairs(self) -> frozenset[_ActionPair]:
        return frozenset(
            (
                *self.admission.actions,
                *((rung.dest, rung.value) for rung in self.admission.managed_boolean_rungs),
            )
        )

    @property
    def executable_pairs(self) -> frozenset[_ActionPair]:
        """Pairs whose ordinary Trace receipt permits use in this world.

        Admission answers whether a pair survived policy and empirical
        filters.  Availability is the separate present-tense receipt: a
        chart-selected future producer must not turn an
        ``UNAVAILABLE_FROM_HERE`` input into the current bearing merely by
        naming it as a supplement.
        """

        step = self.read.program_step
        handed_off = frozenset(step.handoff_by_action) if step is not None else frozenset()
        return frozenset(
            pair
            for pair in self.admitted_pairs
            for detail in (self.admission.detail_by_pair.get(pair),)
            if detail is None
            or detail.availability <= _WriterAvailability.AFTER_PREREQ
            or pair in handed_off
        )

    @property
    def admitted_supplement(self) -> bool:
        step = self.read.program_step
        if step is not None and step.required_pairs:
            return step.required_pairs <= self.executable_pairs
        return any(detail.pair in self.executable_pairs for detail in self.read.details)

    @property
    def viable(self) -> bool:
        """Whether every exact-producer input survived this admission.

        The program cannot be observed crossing an owned boundary unless every
        external input that exact producer currently requires will be applied
        by the same candidate result.
        """

        step = self.read.program_step
        required_pairs = step.required_pairs if step is not None else frozenset()
        return self.read.prescription is not None and (
            not required_pairs or required_pairs <= self.executable_pairs
        )

    @property
    def prescription(self) -> WaitPrescription | None:
        return self.candidate_read.prescription

    @property
    def candidate_read(self) -> WaitRead:
        """The evidence-preserving wait result candidate construction may use."""

        if self.viable and not self.admission.establish_pending:
            return self.read
        return self.read.without_prescription()


@dataclass(frozen=True)
class RouteRead:
    """The selected static route and its immediate executable action context."""

    plan: StaticPath
    candidates: tuple[_ActionPair, ...] = ()
    co_actions: tuple[_ActionPair, ...] = ()


@dataclass(frozen=True)
class PrerequisiteRead:
    """Executable prerequisites admitted by this read."""

    pilot_rungs: tuple[PilotRung, ...] = ()


@dataclass(frozen=True)
class LearnedBatchRead:
    """One learned joint action retained as a single executable artifact."""

    actions: tuple[_ActionPair, ...]
    expectation: EffectExpectation | None = None


@dataclass(frozen=True)
class CrossingBatchRead:
    """One retained crossing DNF branch as an atomic executable overlay."""

    actions: tuple[_ActionPair, ...]
    fidelity: CrossingFidelity
    expectation: EffectExpectation | None = None

    @property
    def constraints(self) -> tuple[Any, ...]:
        return self.fidelity.constraints

    @property
    def reason(self) -> str:
        return self.fidelity.reason

    @property
    def verify_required(self) -> bool:
        return self.fidelity.verify_required

    @property
    def exact(self) -> bool | None:
        return self.fidelity.exact

    @property
    def proposed(self) -> bool:
        return self.fidelity.proposed


@dataclass(frozen=True)
class CandidateDiagnosis:
    """Terminal diagnosis owned by candidate construction."""

    reason: str


class ContinuationKind(StrEnum):
    """Positive current-world evidence that authorizes program motion."""

    PREREQUISITE = "prerequisite"
    SELF_ADVANCING = "self_advancing"
    READY_WRITER = "ready_writer"
    TRACE_READY = "trace_ready"


@dataclass(frozen=True)
class ContinuationRead:
    """Why an actionless current world is expected to advance on its own.

    This is positive continuation evidence, not the absence of a stuck
    diagnosis. Orientation may lower it to a bounded ProgramContinuation while
    that evidence remains current.
    """

    kind: ContinuationKind
    reason: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateRead:
    """Owned current-world readings composed for Orientation."""

    trace: _TraceAdmission
    options: tuple[_Candidate, ...]
    downstream_reach_cap: int
    route: RouteRead | None = None
    wait: WaitRead | None = None
    prerequisites: PrerequisiteRead = PrerequisiteRead()
    learned_batch: LearnedBatchRead | None = None
    crossing_batches: tuple[CrossingBatchRead, ...] = ()
    continuation: ContinuationRead | None = None
    diagnosis: CandidateDiagnosis | None = None
    # Exact widening artifact -> the sole selected primary-path promise.
    # Missing entries are deliberately unresolved, never an invitation to
    # borrow another active action's path.
    widening_expectations: tuple[tuple[tuple[_ActionPair, ...], EffectExpectation], ...] = ()

    def __post_init__(self) -> None:
        if self.continuation is not None and self.diagnosis is not None:
            raise ValueError(
                "a candidate read cannot both authorize continuation and diagnose stuck"
            )


@dataclass(frozen=True)
class _RouteAndCompletionRead:
    """The admitted trace, static route, and charted-completion evidence."""

    trace: _TraceAdmission
    route: RouteRead | None
    charted_completion: WaitRead | None

    @property
    def charted_wait(self) -> WaitRead | None:
        """The charted completion that may participate in wait selection."""

        if self.trace.establish_pending:
            return None
        return self.charted_completion


@dataclass(frozen=True)
class _PrerequisiteSeparation:
    """Trace evidence after executable prerequisites have been separated."""

    trace: _TraceAdmission
    prerequisites: PrerequisiteRead
    instruction_boundary: ChannelHeading | None


@dataclass(frozen=True)
class _LearnedWait:
    """A learned transition whose next step is program-owned motion."""

    read: WaitRead


@dataclass(frozen=True)
class _LearnedAction:
    """A learned transition whose next step is one action."""

    action: _ActionPair
    expectation: EffectExpectation | None = None


@dataclass(frozen=True)
class _LearnedBatch:
    """A learned transition whose next step is one atomic action batch."""

    read: LearnedBatchRead


_LearnedFallback: TypeAlias = _LearnedWait | _LearnedAction | _LearnedBatch
