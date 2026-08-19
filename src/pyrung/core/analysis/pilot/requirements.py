"""Inert requirement, receipt, and occurrence-ownership contracts.

Requirement derivation lives in :mod:`requirement_derivation`. This module
owns the immutable facts shared by evidence interpretation, WorkingTheory,
and repair policy; it does not inspect a trace or choose an action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.execution import ExecutionReceipt

from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    StaticRungAddress,
)
from pyrung.core.analysis.pilot.effects import (
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
    expectation_snapshot,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.navigation_contracts import act_identity
from pyrung.core.crossing import (
    Constraint,
)


class FailureExplanationKind(StrEnum):
    """Why a selected effect could not satisfy its obligation."""

    GUARD_FALSE = "guard_false"
    SPENT = "spent"
    NOT_EXECUTABLE = "not_executable"
    UNKNOWN = "unknown"
    OVERWRITTEN = "overwritten"
    STRANDED = "stranded"
    DISPLACED = "displaced"


class RequirementPhase(StrEnum):
    """Ordering phase retained by a requirement schedule."""

    STEADY = "steady"
    RELEASE = "release"
    ASSERT = "assert"


class RequirementStatus(StrEnum):
    """Requirement lifetime vocabulary; Phase 4 emits only ``ACTIVE``."""

    ACTIVE = "active"
    DISCHARGED = "discharged"
    INVALIDATED = "invalidated"
    AMBIGUOUS = "ambiguous"


class EffectReceiptRole(StrEnum):
    """Which part of a local transaction supplied a failed expectation."""

    IMMEDIATE = "immediate"
    ROUTE_LANDING = "route_landing"


class OperandAuthority(StrEnum):
    """Who owns the operand value; this is not assignment permission."""

    ADJUSTABLE = "adjustable"
    PROGRAM_WRITTEN = "program_written"
    CONFIGURED = "configured"
    UNKNOWN = "unknown"


class GuardLogic(StrEnum):
    """Boolean composition retained from one exact false guard frontier."""

    ALL = "all"
    ANY = "any"


class RequirementSourceWalkStatus(StrEnum):
    """Completeness of one exact same-scan occurrence-source walk."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class OccurrenceSourceLink:
    """Detached proof of one required-read -> exact earlier-writer hop."""

    required_read: EffectOccurrenceSnapshot
    source_write: EffectOccurrenceSnapshot
    enabling_reads: tuple[EffectOccurrenceSnapshot, ...]
    required_address: StaticRungAddress
    required_instruction_path: tuple[int, ...]
    source_address: StaticRungAddress
    instruction_path: tuple[int, ...]


@dataclass(frozen=True)
class RequirementSourceWalk:
    """Report-only result of following exact same-scan definition sources."""

    status: RequirementSourceWalkStatus
    condition: GuardRequirementCondition
    links: tuple[OccurrenceSourceLink, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class GuardRequirementAtom:
    """One scalar false guard term and the exact reads which demanded it."""

    condition: Constraint
    supporting_occurrences: tuple[EffectOccurrenceSnapshot, ...]
    deadline: EffectOccurrenceSnapshot
    source_path: tuple[int, ...]
    operand_authority: OperandAuthority = OperandAuthority.UNKNOWN
    source_links: tuple[OccurrenceSourceLink, ...] = field(default=(), repr=False)
    # Exact static rung whose dynamic guard read demanded this atom.  The
    # condition-tree ``source_path`` is not a PDG branch address.
    demanding_rung: Any = field(default=None, compare=False, repr=False)

    @property
    def permits_assignment(self) -> bool:
        """Whether recovery may directly steer this exact guard operand."""

        return self.operand_authority is OperandAuthority.ADJUSTABLE


@dataclass(frozen=True)
class GuardRequirementExpr:
    """A compound guard frontier with per-alternative occurrence deadlines.

    ``ANY`` is material: a false OR may be repaired through any exact branch,
    and flattening it into a conjunction would silently change the program's
    path semantics. ``ALL`` is retained for completeness even though the
    current false-frontier reducer normally selects the first false AND child.
    """

    logic: GuardLogic
    terms: tuple[GuardRequirementAtom | GuardRequirementExpr, ...]
    # False means the expression retains sound actionable alternatives but at
    # least one observed false arm had no representable inverse. Consumers may
    # choose a retained arm; they must not report the list as exhaustive.
    exhaustive: bool = True


GuardRequirementCondition = GuardRequirementAtom | GuardRequirementExpr
ActiveCondition = Constraint | GuardRequirementCondition


def _scheduled_condition_identity(condition: ActiveCondition) -> Any:
    """Constraint identity without receipt-local occurrence evidence."""

    if isinstance(condition, GuardRequirementAtom):
        return (
            "atom",
            condition.condition,
            condition.source_path,
            condition.operand_authority,
        )
    if isinstance(condition, GuardRequirementExpr):
        return (
            "expr",
            condition.logic,
            tuple(_scheduled_condition_identity(term) for term in condition.terms),
            condition.exhaustive,
        )
    return condition


def _scheduled_occurrence_identity(occurrence: EffectOccurrenceSnapshot) -> tuple[Any, ...]:
    """Dynamic program address, independent of the retry's absolute scan."""

    return (occurrence.kind, occurrence.tag, occurrence.dynamic_address)


def classify_bound_operand_authority(
    tag: str,
    *,
    source_value: Any,
    declared_default: Any,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
    provisional: frozenset[str] = frozenset(),
) -> OperandAuthority:
    """Classify one owner-declared parameter without granting a write.

    Program authorship and explicit external configuration are authoritative.
    A non-default/nonzero source value is otherwise treated as configured and
    preserved, unless an exact lifecycle receipt says that value came from a
    provisional Pilot setup. Only that narrow provenance permits a later
    requirement to refine Pilot's own earlier value. This policy is
    intentionally scoped to owner-declared boundary parameters, not arbitrary
    numeric process inputs.
    """

    if tag in program_written:
        return OperandAuthority.PROGRAM_WRITTEN
    if tag in configured:
        return OperandAuthority.CONFIGURED
    if tag in provisional and tag in steerable:
        return OperandAuthority.ADJUSTABLE
    if source_value != declared_default or bool(source_value):
        return OperandAuthority.CONFIGURED
    if tag in steerable:
        return OperandAuthority.ADJUSTABLE
    return OperandAuthority.UNKNOWN


def classify_guard_operand_authority(
    tag: str,
    *,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
) -> OperandAuthority:
    """Classify one arbitrary guard read without parameter-value heuristics.

    Guard values are program conditions, not owner-declared completion
    parameters. A true/nonzero external guard therefore remains an ordinary
    steerable input unless the program writes it or the caller explicitly
    patched/forced it at the causal source.
    """

    if tag in program_written:
        return OperandAuthority.PROGRAM_WRITTEN
    if tag in configured:
        return OperandAuthority.CONFIGURED
    if tag in steerable:
        return OperandAuthority.ADJUSTABLE
    return OperandAuthority.UNKNOWN


@dataclass(frozen=True)
class FailureExplanation:
    """Typed interpretation of one exact factual effect observation."""

    kind: FailureExplanationKind
    detail: str = ""
    supporting_occurrences: tuple[EffectOccurrenceSnapshot, ...] = ()


@dataclass(frozen=True)
class FailedEffectReceiptSnapshot:
    explanation: FailureExplanation
    observation: EffectObservationSnapshot
    selected_writer: Any
    source_world_key: Any
    source_scan: int | None
    causal_identity: tuple[int, int, int]
    act_identity: tuple[Any, ...]


@dataclass(frozen=True)
class FailedEffectReceipt:
    """Exact failed expectation and its typed interpretation."""

    explanation: FailureExplanation
    observation: EffectObservationSnapshot
    selected_writer: Any
    source_world_key: Any
    checkpoint_owner: Any
    execution_epoch: Any = field(compare=False, repr=False)
    execution_owner: Any = field(compare=False, repr=False)
    source_checkpoint: Any = field(compare=False, repr=False)
    # The exact local transaction which owned the failed expectation.  Phase 5
    # may re-execute this designation from its causal checkpoint; it must never
    # restore and ask Orientation to silently choose a different producer.
    act_identity: tuple[Any, ...] = ()
    local_act: Any = field(default=None, compare=False, repr=False)
    local_bearing: Any = field(default=None, compare=False, repr=False)
    expectation: Any = field(default=None, compare=False, repr=False)
    expectation_role: EffectReceiptRole = EffectReceiptRole.IMMEDIATE

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.explanation,
            self.observation,
            self.selected_writer,
            self.source_world_key,
            self.checkpoint_owner,
            id(self.execution_epoch),
            id(self.execution_owner),
            self.act_identity,
            self.expectation_role,
        )

    def diagnostic_snapshot(self) -> FailedEffectReceiptSnapshot:
        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        return FailedEffectReceiptSnapshot(
            explanation=self.explanation,
            observation=self.observation,
            selected_writer=self.selected_writer,
            source_world_key=self.source_world_key,
            source_scan=getattr(getattr(checkpoint_work, "state", None), "scan_id", None),
            causal_identity=(
                id(self.execution_epoch),
                id(self.execution_owner),
                id(self.checkpoint_owner),
            ),
            act_identity=self.act_identity,
        )


@dataclass(frozen=True)
class ActiveRequirementSnapshot:
    """Detached public diagnostic view of one active requirement."""

    condition: ActiveCondition
    demanding_occurrence: EffectOccurrenceSnapshot
    deadline: EffectOccurrenceSnapshot
    selected_writer: Any
    operand_authority: OperandAuthority
    source_world_key: Any
    source_scan: int | None
    causal_identity: tuple[int, int, int]
    phase: RequirementPhase
    status: RequirementStatus
    provenance: str
    scope: tuple[Any, ...]
    obstruction_occurrence: EffectOccurrenceSnapshot | None = None


@dataclass(frozen=True)
class ActiveRequirement:
    """An inert condition that must hold by an exact execution occurrence.

    Live epoch/query/checkpoint objects are retained only as internal proof
    handles. ``identity`` deliberately uses their stable owner identities plus
    immutable semantic fields; public recording consumes detached occurrences.
    """

    condition: ActiveCondition
    demanding_occurrence: EffectOccurrenceSnapshot
    deadline: EffectOccurrenceSnapshot
    selected_writer: Any
    operand_authority: OperandAuthority
    execution_epoch: Any = field(compare=False, repr=False)
    execution_owner: Any = field(compare=False, repr=False)
    source_world_key: Any
    checkpoint_owner: Any
    source_checkpoint: Any = field(compare=False, repr=False)
    phase: RequirementPhase = RequirementPhase.STEADY
    status: RequirementStatus = RequirementStatus.ACTIVE
    provenance: str = ""
    scope: tuple[Any, ...] = ()
    # Exact harmful write whose guard this requirement prevents. Keeping this
    # typed avoids rediscovering the physical obstruction from ``scope``.
    obstruction_occurrence: EffectOccurrenceSnapshot | None = None

    @property
    def permits_assignment(self) -> bool:
        """Whether Phase 5 may consider assigning this operand directly."""

        return self.operand_authority is OperandAuthority.ADJUSTABLE

    @property
    def navigation_identity(self) -> tuple[Any, ...]:
        """Semantic schedule identity used by current-world navigation.

        Exact epoch, checkpoint, scan, and supporting reads remain on
        ``identity`` and the diagnostic receipt. Re-observing the same active
        condition on a retry must not recursively mint a new Compass world.
        """

        return (
            _scheduled_condition_identity(self.condition),
            _scheduled_occurrence_identity(self.demanding_occurrence),
            _scheduled_occurrence_identity(self.deadline),
            self.selected_writer,
            self.operand_authority,
            self.phase,
            self.status,
            self.scope,
        )

    def diagnostic_snapshot(self) -> ActiveRequirementSnapshot:
        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        source_scan = getattr(getattr(checkpoint_work, "state", None), "scan_id", None)
        return ActiveRequirementSnapshot(
            condition=self.condition,
            demanding_occurrence=self.demanding_occurrence,
            deadline=self.deadline,
            selected_writer=self.selected_writer,
            operand_authority=self.operand_authority,
            source_world_key=self.source_world_key,
            source_scan=source_scan,
            causal_identity=(
                id(self.execution_epoch),
                id(self.execution_owner),
                id(self.checkpoint_owner),
            ),
            phase=self.phase,
            status=self.status,
            provenance=self.provenance,
            scope=self.scope,
            obstruction_occurrence=self.obstruction_occurrence,
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        """Canonical internal attempt/world identity for this requirement."""

        return (
            self.condition,
            self.demanding_occurrence,
            self.deadline,
            self.selected_writer,
            self.operand_authority,
            id(self.execution_epoch),
            id(self.execution_owner),
            self.source_world_key,
            self.checkpoint_owner,
            self.phase,
            self.status,
            self.provenance,
            self.scope,
            self.obstruction_occurrence,
        )


@dataclass(frozen=True)
class RequirementDerivation:
    """Fail-closed result of interpreting an exact owner boundary."""

    explanation: FailureExplanation
    requirement: ActiveRequirement | None = None
    source_walk: RequirementSourceWalk | None = None

    @property
    def exact(self) -> bool:
        return self.requirement is not None


@dataclass(frozen=True)
class ExpectationReceiptSnapshot:
    """Detached event view of one committed expectation receipt."""

    source_world_key: Any
    source_scan: int | None
    act_identity: tuple[Any, ...]
    active_rung_identities: tuple[Any, ...]
    obligations: tuple[Any, ...]
    producer_occurrences: tuple[EffectOccurrenceSnapshot, ...]
    consumer_occurrences: tuple[EffectOccurrenceSnapshot, ...]
    causal_identity: tuple[int, int, int]
    expectation_role: EffectReceiptRole = EffectReceiptRole.IMMEDIATE


@dataclass(frozen=True)
class ExpectationReceipt:
    """Exact committed satisfaction of an expectation-bearing operation."""

    source_world_key: Any
    checkpoint_owner: Any
    act_identity: tuple[Any, ...]
    active_rung_identities: tuple[Any, ...]
    obligations: tuple[Any, ...]
    producer_occurrences: tuple[EffectOccurrenceSnapshot, ...]
    consumer_occurrences: tuple[EffectOccurrenceSnapshot, ...]
    execution: ExecutionReceipt = field(compare=False, repr=False)
    source_checkpoint: Any = field(compare=False, repr=False)
    local_act: Any = field(default=None, compare=False, repr=False)
    local_bearing: Any = field(default=None, compare=False, repr=False)
    expectation: Any = field(default=None, compare=False, repr=False)
    expectation_role: EffectReceiptRole = EffectReceiptRole.IMMEDIATE

    @property
    def execution_owner(self) -> Any:
        """The one Epoch query owning every producer occurrence."""

        owners = tuple(
            dict.fromkeys(
                self.execution.owner_at(occurrence.scan_id)
                for occurrence in self.producer_occurrences
            )
        )
        if len(owners) != 1 or owners[0] is None:
            raise ValueError("expectation producers do not share one execution owner")
        return owners[0]

    @property
    def execution_epoch(self) -> Any:
        """The immutable Epoch owning this expectation's producer."""

        return self.execution_owner.epoch

    def diagnostic_snapshot(self) -> ExpectationReceiptSnapshot:
        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        return ExpectationReceiptSnapshot(
            source_world_key=self.source_world_key,
            source_scan=getattr(getattr(checkpoint_work, "state", None), "scan_id", None),
            act_identity=self.act_identity,
            active_rung_identities=self.active_rung_identities,
            obligations=self.obligations,
            producer_occurrences=self.producer_occurrences,
            consumer_occurrences=self.consumer_occurrences,
            causal_identity=(
                id(self.execution_epoch),
                id(self.execution_owner),
                id(self.checkpoint_owner),
            ),
            expectation_role=self.expectation_role,
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.source_world_key,
            self.checkpoint_owner,
            self.act_identity,
            self.active_rung_identities,
            self.obligations,
            self.producer_occurrences,
            self.consumer_occurrences,
            id(self.execution_epoch),
            id(self.execution_owner),
            self.expectation_role,
        )


@dataclass(frozen=True)
class ExpectationOccurrenceSupport:
    """One logical expectation supported by an exact committed write."""

    receipt: ExpectationReceipt
    obligation_index: int
    producer: RungWrite = field(compare=False, repr=False)


@dataclass(frozen=True)
class ExpectationOccurrenceOwnership:
    """All logical receipt aliases owned by one physical write occurrence.

    A producer may satisfy both an immediate handoff and a route-landing
    obligation.  Those are distinct logical supports, not ambiguous physical
    ownership, so consumers must retain the complete support set rather than
    selecting whichever receipt happens to be encountered last.
    """

    occurrence: EffectOccurrenceSnapshot
    supports: tuple[ExpectationOccurrenceSupport, ...]


def resolve_expectation_receipt_producer(
    receipt: ExpectationReceipt,
    index: int,
) -> RungWrite | None:
    """Rebuild one receipt producer from its owner-bound scan projection.

    Receipts retain detached addresses only.  A missing, ambiguous, or
    foreign reconstruction is not causal authority and therefore fails closed.
    """

    if index < 0 or index >= len(receipt.producer_occurrences):
        return None
    snapshot = receipt.producer_occurrences[index]
    if snapshot.kind != "write":
        return None
    owner = receipt.execution_owner
    if getattr(owner, "epoch", None) is not receipt.execution_epoch:
        return None
    runner_factory = getattr(owner, "_runner", None)
    if runner_factory is None:
        return None
    projection = runner_factory()._replay_rung_write_projection_at(snapshot.scan_id)
    if projection is None:
        return None
    matches = tuple(write for write in projection.writes if occurrence_snapshot(write) == snapshot)
    return matches[0] if len(matches) == 1 else None


def resolve_expectation_receipt_consumer(
    receipt: ExpectationReceipt,
    index: int,
) -> RungRead | None:
    """Rebuild the exact consumer read for one receipt obligation, if any."""

    if index < 0 or index >= len(receipt.obligations):
        return None
    obligation = receipt.expectation.obligations[index]
    if obligation.consumer_rung is None:
        return None
    snapshots = tuple(
        snapshot
        for snapshot in receipt.consumer_occurrences
        if snapshot.kind == "read"
        and snapshot.tag == obligation.tag
        and snapshot.rung[:2] == obligation.consumer[:2]
    )
    if len(snapshots) != 1:
        return None
    snapshot = snapshots[0]
    owner = receipt.execution_owner
    if getattr(owner, "epoch", None) is not receipt.execution_epoch:
        return None
    runner_factory = getattr(owner, "_runner", None)
    if runner_factory is None:
        return None
    projection = runner_factory()._replay_rung_write_projection_at(snapshot.scan_id)
    if projection is None:
        return None
    matches = tuple(
        read
        for read in projection.reads
        if occurrence_snapshot(read) == snapshot and read.run.rung is obligation.consumer_rung
    )
    return matches[0] if len(matches) == 1 else None


def expectation_occurrence_ownerships(
    receipts: tuple[ExpectationReceipt, ...] | list[ExpectationReceipt],
) -> tuple[ExpectationOccurrenceOwnership, ...]:
    """Group logical receipt aliases by immutable physical occurrence owner."""

    grouped: dict[
        tuple[EffectOccurrenceSnapshot, int, int],
        list[ExpectationOccurrenceSupport],
    ] = {}
    for receipt in receipts:
        for index, occurrence in enumerate(receipt.producer_occurrences):
            producer = resolve_expectation_receipt_producer(receipt, index)
            if producer is None:
                continue
            key = (occurrence, id(receipt.execution_epoch), id(receipt.execution_owner))
            grouped.setdefault(key, []).append(
                ExpectationOccurrenceSupport(receipt, index, producer)
            )
    return tuple(
        ExpectationOccurrenceOwnership(key[0], tuple(supports)) for key, supports in grouped.items()
    )


def match_expectation_receipt(
    receipts: tuple[ExpectationReceipt, ...] | list[ExpectationReceipt],
    *,
    occurrence: RungWrite,
    execution_epoch: Any,
    execution_owner: Any,
) -> ExpectationReceipt | None:
    """Return the unique accepted expectation owning one exact causal write.

    The detached receipt view is recording material, not causal authority.  A
    match therefore reconstructs the receipt's complete dynamic occurrence
    from its immutable Epoch/query owner, then requires the same owner and an
    intact local expectation source. Equal-looking evidence from another epoch
    is not authority.
    """

    observed = occurrence_snapshot(occurrence)
    matches: list[ExpectationReceipt] = []
    for receipt in receipts:
        if (
            receipt.execution_epoch is not execution_epoch
            or receipt.execution_owner is not execution_owner
            or receipt.local_act is None
            or receipt.local_bearing is None
            or receipt.expectation is None
            or len(receipt.producer_occurrences) != len(receipt.obligations)
            or receipt.act_identity != act_identity(receipt.local_act)
            or receipt.local_bearing.act is not receipt.local_act
            or (
                receipt.expectation_role is not EffectReceiptRole.ROUTE_LANDING
                and receipt.local_bearing.expectation is not receipt.expectation
            )
            or expectation_snapshot(receipt.expectation) != receipt.obligations
            or getattr(receipt.source_checkpoint, "owner", None) is not receipt.checkpoint_owner
            or getattr(receipt.source_checkpoint, "key", None) != receipt.source_world_key
        ):
            continue
        owned = tuple(
            index
            for index, producer in enumerate(receipt.producer_occurrences)
            if producer == observed
        )
        if len(owned) != 1:
            continue
        index = owned[0]
        producer = resolve_expectation_receipt_producer(receipt, index)
        if producer is None:
            continue
        obligation = receipt.obligations[index]
        local_obligation = receipt.expectation.obligations[index]
        if (
            obligation.tag != producer.transition.tag_name
            or obligation.value != producer.transition.to_value
            or obligation.producer[:2] != (producer.rung_id.subroutine, producer.rung_id.rung_index)
            or local_obligation.producer_rung is not producer.run.rung
        ):
            continue
        matches.append(receipt)
    return matches[0] if len(matches) == 1 else None


_SWAPPED_OPERATORS = {
    "==": "==",
    "!=": "!=",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}
