"""Interpret projection-owned failed effects as inert active requirements.

This module is the Phase-4 seam between instruction-owned completion
semantics and recovery orchestration. It derives relational requirements,
their exact deadlines, and strictly decreasing same-scan occurrence-source
walks; it never chooses an assignment, installs an overlay, restores a
checkpoint, or executes a repair.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.execution import ExecutionReceipt

from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    ScanRungWriteProjection,
    StaticRungAddress,
)
from pyrung.core.analysis.pilot.advance import AdvanceIndex, AdvanceOwner
from pyrung.core.analysis.pilot.effects import (
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
    expectation_snapshot,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.navigation_contracts import act_identity
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import _condition_to_expr
from pyrung.core.analysis.write_sites import instruction_writes_tag
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    IntTruthyCondition,
    NormallyClosedCondition,
)
from pyrung.core.crossing import (
    AffineCmp,
    Cmp,
    Constraint,
    complement_scalar_constraint,
)
from pyrung.core.executor import InstructionRun, LoopIterationRun, RungRun
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.tag import ImmediateRef, Tag


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


def bind_guard_operand_authorities(
    requirement: ActiveRequirement | None,
    *,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
) -> ActiveRequirement | None:
    """Attach exact per-atom ownership to one derived guard requirement."""

    if requirement is None or not isinstance(
        requirement.condition,
        GuardRequirementAtom | GuardRequirementExpr,
    ):
        return requirement

    condition = bind_guard_condition_operand_authorities(
        requirement.condition,
        steerable=steerable,
        program_written=program_written,
        configured=configured,
    )
    authorities = {atom.operand_authority for atom in _guard_atoms(condition)}
    summary = authorities.pop() if len(authorities) == 1 else OperandAuthority.UNKNOWN
    return replace(
        requirement,
        condition=condition,
        operand_authority=summary,
    )


def bind_guard_condition_operand_authorities(
    condition: GuardRequirementCondition,
    *,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
) -> GuardRequirementCondition:
    """Bind every atom in detached transitive guard evidence."""

    if isinstance(condition, GuardRequirementExpr):
        return replace(
            condition,
            terms=tuple(
                bind_guard_condition_operand_authorities(
                    term,
                    steerable=steerable,
                    program_written=program_written,
                    configured=configured,
                )
                for term in condition.terms
            ),
        )
    tag = getattr(condition.condition, "tag", None)
    authority = (
        classify_guard_operand_authority(
            tag,
            steerable=steerable,
            program_written=program_written,
            configured=configured,
        )
        if isinstance(tag, str)
        else OperandAuthority.UNKNOWN
    )
    return replace(condition, operand_authority=authority)


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


def _unknown(detail: str) -> RequirementDerivation:
    return RequirementDerivation(FailureExplanation(FailureExplanationKind.UNKNOWN, detail=detail))


def _literal_requirement_for_operand(
    relation: Cmp | AffineCmp,
    operand: RungRead,
    observed_operands: Mapping[str, Any],
) -> Cmp | None:
    """Transpose one exact scalar relation onto the selected operand."""

    # Affine storage width, scale, and quantization require a separately proved
    # adapter. Do not erase that information into an apparently exact Cmp.
    if not isinstance(relation, Cmp):
        return None

    operand_tag = operand.occurrence.name
    # The relation's coordinate is the *post-advance* accumulator, while its
    # sibling read is the entry accumulator. Equating those without an exact
    # delta inverse moves the requirement across time and is unsound. Phase 4
    # therefore transposes only onto a distinct, exact bound-parameter read.
    if operand_tag == relation.tag:
        return None
    if not relation.bound_is_tag or operand_tag != str(relation.bound):
        return None
    op = _SWAPPED_OPERATORS.get(relation.op)
    other = observed_operands.get(relation.tag)
    if op is None or other is None:
        return None
    return Cmp(operand_tag, op, other)


def _exact_instruction_operands(
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    owner: AdvanceOwner | None = None,
) -> Mapping[str, Any] | None:
    """Return exact owner operands, including its post-advance accumulator.

    Timer/counter execution may journal the Done write before the accumulator
    write even though completion is calculated from that next accumulator.
    ``AdvanceProfile.accumulator`` declares that semantic coordinate; only the
    exact same instruction occurrence's unique write may replace its entry read.
    """

    result: dict[str, Any] = {}
    for read in projection.reads_observed_by_write(definition):
        tag = read.occurrence.name
        if tag in result:
            return None
        result[tag] = read.occurrence.value
    accumulator = owner.profile.accumulator if owner is not None else None
    if accumulator is not None:
        accumulator_writes = tuple(
            write
            for write in projection.writes_for_run(definition.run)
            if write.instruction is definition.instruction
            and write.transition.tag_name == accumulator.name
        )
        if len(accumulator_writes) != 1:
            return None
        result[accumulator.name] = accumulator_writes[0].transition.to_value
    return MappingProxyType(result)


def _post_advance_accumulator_write(
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    owner: AdvanceOwner,
) -> RungWrite | None:
    """The unique exact sibling write of the owner's resulting coordinate."""

    accumulator = owner.profile.accumulator
    if accumulator is None:
        return None
    writes = tuple(
        write
        for write in projection.writes_for_run(definition.run)
        if write.instruction is definition.instruction
        and write.transition.tag_name == accumulator.name
    )
    return writes[0] if len(writes) == 1 else None


def _completion_branch_error(
    owner: AdvanceOwner,
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    observed_operands: Mapping[str, Any],
    boundary: Cmp | AffineCmp,
) -> str | None:
    """Prove that an owned Boolean write came from its completion branch.

    An Advance owner may write the same Done channel while advancing, resetting,
    or executing disabled cleanup. A matching output value alone cannot select
    among those control paths. The owner therefore declares its ordered exact
    entry controls on :class:`AdvanceProfile`; this bridge merely evaluates
    them against the dynamic run and confirms the resulting comparison.
    """

    controls = owner.profile.completion_controls
    if controls is None:
        return "advance owner does not declare exact completion controls"

    # Completion controls use the rung-entry ConditionView. Build that factual
    # value surface from exact direct reads before the selected write, keeping
    # it separate from ``observed_operands`` where Acc is post-advance.
    entry_values: dict[str, Any] = {}
    for read in projection.reads_for_run(definition.run):
        if read.ordinal >= definition.ordinal:
            continue
        tag = read.occurrence.name
        prior = entry_values.get(tag, read.occurrence.value)
        if tag in entry_values and prior != read.occurrence.value:
            return "completion controls have conflicting exact entry reads"
        entry_values[tag] = read.occurrence.value
    for demand in controls:
        actual = (
            True
            if demand.condition is None
            else _eval_expr_from_state(
                _condition_to_expr(demand.condition),
                entry_values,
            )
        )
        expected = bool(demand.value)
        if actual is not expected:
            return (
                "declared completion control has the wrong truth value"
                if actual is not None
                else "declared completion control cannot be proved from exact entry reads"
            )

    accumulator_write = _post_advance_accumulator_write(projection, definition, owner)
    if accumulator_write is None:
        return "advance owner has no unique post-advance accumulator write"
    boundary_value = constraint_holds(boundary, observed_operands)
    written_value = definition.transition.to_value
    if not isinstance(written_value, bool) or boundary_value is None:
        return "advance completion comparison is not exactly Boolean"
    if written_value is not boundary_value:
        return "advance owner Boolean write did not come from its completion comparison"
    return None


def _contains_identity(values: tuple[Any, ...], selected: Any) -> bool:
    return any(value is selected for value in values)


def _instruction_runs(body: tuple[Any, ...]) -> tuple[InstructionRun, ...]:
    result: list[InstructionRun] = []
    for item in body:
        if isinstance(item, InstructionRun):
            result.append(item)
            result.extend(_instruction_runs(item.body))
        elif isinstance(item, LoopIterationRun):
            result.extend(_instruction_runs(item.body))
        elif isinstance(item, RungRun):
            continue
    return tuple(result)


@dataclass(frozen=True)
class _GuardEvaluation:
    value: bool
    supporting_reads: tuple[RungRead, ...]
    requirement: GuardRequirementCondition | None = None
    exact: bool = True


class _GuardReadCursor:
    """Match a replayed static guard evaluation to its exact journal reads."""

    def __init__(self, reads: tuple[RungRead, ...], view: Any) -> None:
        self._reads = reads
        self._view = view
        self._cursor = 0

    def evaluate_leaf(self, condition: Condition) -> tuple[bool, tuple[RungRead, ...]] | None:
        captured: list[tuple[str, str, Any, Any]] = []

        def _capture(domain: str, name: str, value: Any, origin: Any, source: Any) -> None:
            captured.append((domain, name, value, source if source is not None else origin))

        previous_sink = self._view._read_sink
        self._view._read_sink = _capture
        try:
            value = condition.evaluate(self._view)
        finally:
            self._view._read_sink = previous_sink

        matched: list[RungRead] = []
        for domain, name, value_read, source in captured:
            if self._cursor >= len(self._reads):
                return None
            exact = self._reads[self._cursor]
            occurrence = exact.occurrence
            same_value = occurrence.value is value_read
            if not same_value:
                try:
                    same_value = occurrence.value == value_read
                except (TypeError, ValueError):
                    same_value = False
            exact_source = occurrence.source
            same_source = (
                exact_source == source
                if isinstance(exact_source, str) and isinstance(source, str)
                else exact_source is source
            )
            if (
                occurrence.domain != domain
                or occurrence.name != name
                or same_value is not True
                or not same_source
            ):
                return None
            matched.append(exact)
            self._cursor += 1
        return bool(value), tuple(matched)


def _tag_name(value: Any) -> str | None:
    if isinstance(value, ImmediateRef):
        value = value.value
    return value.name if isinstance(value, Tag) else None


_GUARD_COMPARE_OPERATORS: dict[type[Condition], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
}


def _guard_leaf_constraint(condition: Condition) -> Constraint | None:
    """Translate only exact scalar guard leaves into neutral constraints."""

    if isinstance(condition, BitCondition):
        tag = _tag_name(condition.tag)
        return Cmp(tag, "==", True) if tag is not None else None
    if isinstance(condition, NormallyClosedCondition):
        tag = _tag_name(condition.tag)
        return Cmp(tag, "==", False) if tag is not None else None
    if isinstance(condition, IntTruthyCondition):
        return Cmp(condition.tag.name, "!=", 0)
    operator = _GUARD_COMPARE_OPERATORS.get(type(condition))
    if operator is None:
        return None
    tag = _tag_name(getattr(condition, "tag", None))
    if tag is None:
        return None
    bound = getattr(condition, "value", None)
    bound_tag = _tag_name(bound)
    if bound_tag is not None:
        return Cmp(tag, operator, bound_tag, bound_is_tag=True)
    if isinstance(bound, Tag | ImmediateRef) or hasattr(bound, "evaluate"):
        return None
    return Cmp(tag, operator, bound)


def _evaluate_guard_condition(
    condition: Condition,
    cursor: _GuardReadCursor,
    path: tuple[int, ...],
) -> _GuardEvaluation:
    """Reduce one observed false condition to its exact enabling frontier."""

    if isinstance(condition, AllCondition):
        supporting: list[RungRead] = []
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_condition(child, cursor, (*path, index))
            supporting.extend(result.supporting_reads)
            if not result.exact:
                return _GuardEvaluation(False, tuple(supporting), exact=False)
            if not result.value:
                # Short-circuit semantics make the first false AND child the
                # exact narrower condition. Unobserved suffixes are not minted.
                return _GuardEvaluation(
                    False,
                    tuple(supporting),
                    requirement=result.requirement,
                )
        return _GuardEvaluation(True, tuple(supporting))

    if isinstance(condition, AnyCondition):
        supporting = []
        alternatives: list[GuardRequirementCondition] = []
        exhaustive = True
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_condition(child, cursor, (*path, index))
            supporting.extend(result.supporting_reads)
            if result.value:
                return _GuardEvaluation(True, tuple(supporting))
            if not result.exact or result.requirement is None:
                # The dynamic rung proves the pure OR was false. An opaque arm
                # prevents an exhaustive inverse, but any separately exact arm
                # remains a sufficient, sound way to make the OR true.
                exhaustive = False
                continue
            alternatives.append(result.requirement)
        if not alternatives:
            return _GuardEvaluation(False, tuple(supporting), exact=False)
        requirement: GuardRequirementCondition
        if len(alternatives) == 1 and exhaustive:
            requirement = alternatives[0]
        else:
            requirement = GuardRequirementExpr(
                GuardLogic.ANY,
                tuple(alternatives),
                exhaustive=exhaustive,
            )
        return _GuardEvaluation(False, tuple(supporting), requirement=requirement)

    captured = cursor.evaluate_leaf(condition)
    if captured is None:
        return _GuardEvaluation(False, (), exact=False)
    value, reads = captured
    if value:
        return _GuardEvaluation(True, reads)
    constraint = _guard_leaf_constraint(condition)
    if constraint is None or not reads:
        return _GuardEvaluation(False, reads, exact=False)
    occurrences = tuple(occurrence_snapshot(read) for read in reads)
    return _GuardEvaluation(
        False,
        reads,
        requirement=GuardRequirementAtom(
            condition=constraint,
            supporting_occurrences=occurrences,
            deadline=occurrences[-1],
            source_path=path,
        ),
    )


def _evaluate_run_guard(
    run: RungRun,
    projection: ScanRungWriteProjection,
) -> _GuardEvaluation:
    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    cursor = _GuardReadCursor(projection.reads_for_run(run), run.view)
    supporting: list[RungRead] = []
    for index, condition in enumerate(conditions):
        result = _evaluate_guard_condition(condition, cursor, (index,))
        supporting.extend(result.supporting_reads)
        if not result.exact:
            return _GuardEvaluation(False, tuple(supporting), exact=False)
        if not result.value:
            return _GuardEvaluation(
                False,
                tuple(supporting),
                requirement=result.requirement,
            )
    return _GuardEvaluation(True, tuple(supporting))


def _evaluate_guard_complement(
    condition: Condition,
    cursor: _GuardReadCursor,
    path: tuple[int, ...],
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Invert one exactly observed true guard without guessing unread arms."""

    if isinstance(condition, AllCondition):
        supporting: list[RungRead] = []
        alternatives: list[GuardRequirementCondition] = []
        exhaustive = True
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_complement(
                child,
                cursor,
                (*path, index),
                preserve_nested_false=preserve_nested_false,
            )
            supporting.extend(result.supporting_reads)
            if not result.exact:
                exhaustive = False
                continue
            if not result.value:
                if not preserve_nested_false:
                    return _GuardEvaluation(False, tuple(supporting), exact=True)
                # A false nested AND already keeps an enclosing OR disabled.
                # Every earlier observed-true child can also be made false;
                # retain those alternatives as well as this exact false
                # frontier. An unread suffix makes the list non-exhaustive.
                if result.requirement is not None:
                    alternatives.append(result.requirement)
                exhaustive = exhaustive and index == len(condition.conditions) - 1
                if not alternatives:
                    return _GuardEvaluation(False, tuple(supporting), exact=False)
                requirement = (
                    alternatives[0]
                    if len(alternatives) == 1 and exhaustive
                    else GuardRequirementExpr(
                        GuardLogic.ANY,
                        tuple(alternatives),
                        exhaustive=exhaustive,
                    )
                )
                return _GuardEvaluation(
                    False,
                    tuple(supporting),
                    requirement=requirement,
                )
            if result.requirement is not None:
                alternatives.append(result.requirement)
        if not alternatives:
            return _GuardEvaluation(True, tuple(supporting), exact=False)
        requirement = (
            alternatives[0]
            if len(alternatives) == 1 and exhaustive
            else GuardRequirementExpr(
                GuardLogic.ANY,
                tuple(alternatives),
                exhaustive=exhaustive,
            )
        )
        return _GuardEvaluation(True, tuple(supporting), requirement=requirement)

    if isinstance(condition, AnyCondition):
        supporting: list[RungRead] = []
        conjuncts: list[GuardRequirementCondition] = []
        any_true = False
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_complement(
                child,
                cursor,
                (*path, index),
                preserve_nested_false=preserve_nested_false,
            )
            supporting.extend(result.supporting_reads)
            if not result.exact or result.requirement is None:
                # A true OR short-circuits.  Its unread suffix cannot be
                # asserted false merely because the first observed arm was
                # true, so the exact dual is unavailable.
                return _GuardEvaluation(any_true, tuple(supporting), exact=False)
            any_true = any_true or result.value
            conjuncts.append(result.requirement)
        if not any_true:
            if not preserve_nested_false:
                return _GuardEvaluation(False, tuple(supporting), exact=True)
            if not conjuncts:
                return _GuardEvaluation(False, tuple(supporting), exact=False)
            requirement = (
                conjuncts[0]
                if len(conjuncts) == 1
                else GuardRequirementExpr(GuardLogic.ALL, tuple(conjuncts))
            )
            return _GuardEvaluation(
                False,
                tuple(supporting),
                requirement=requirement,
            )
        requirement = (
            conjuncts[0]
            if len(conjuncts) == 1
            else GuardRequirementExpr(GuardLogic.ALL, tuple(conjuncts))
        )
        return _GuardEvaluation(True, tuple(supporting), requirement=requirement)

    captured = cursor.evaluate_leaf(condition)
    if captured is None:
        return _GuardEvaluation(False, (), exact=False)
    value, reads = captured
    constraint = _guard_leaf_constraint(condition)
    complement = complement_scalar_constraint(constraint) if constraint is not None else None
    if complement is None or not reads:
        return _GuardEvaluation(value, reads, exact=False)
    occurrences = tuple(occurrence_snapshot(read) for read in reads)
    return _GuardEvaluation(
        value,
        reads,
        requirement=GuardRequirementAtom(
            condition=complement,
            supporting_occurrences=occurrences,
            deadline=occurrences[-1],
            source_path=path,
        ),
    )


def _evaluate_run_guard_complement(
    run: RungRun,
    projection: ScanRungWriteProjection,
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Complement the implicit conjunction which enabled one exact run."""

    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    cursor = _GuardReadCursor(projection.reads_for_run(run), run.view)
    supporting: list[RungRead] = []
    alternatives: list[GuardRequirementCondition] = []
    exhaustive = True
    for index, condition in enumerate(conditions):
        result = _evaluate_guard_complement(
            condition,
            cursor,
            (index,),
            preserve_nested_false=preserve_nested_false,
        )
        supporting.extend(result.supporting_reads)
        if not result.exact:
            exhaustive = False
            continue
        if not result.value:
            return _GuardEvaluation(False, tuple(supporting), exact=True)
        if result.requirement is not None:
            alternatives.append(result.requirement)
    if not alternatives:
        return _GuardEvaluation(True, tuple(supporting), exact=False)
    requirement = (
        alternatives[0]
        if len(alternatives) == 1 and exhaustive
        else GuardRequirementExpr(
            GuardLogic.ANY,
            tuple(alternatives),
            exhaustive=exhaustive,
        )
    )
    return _GuardEvaluation(True, tuple(supporting), requirement=requirement)


def _evaluate_enabling_path_complement(
    selected: RungRun,
    projection: ScanRungWriteProjection,
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Find exact sufficient ways to disable an enabled nested writer path."""

    ancestors = tuple(
        sorted(
            (
                candidate
                for candidate in projection.runs
                if candidate is not selected
                and candidate.enabled
                and any(nested is selected for nested in candidate.rung_occurrences)
            ),
            key=lambda candidate: candidate.depth,
            reverse=True,
        )
    )
    alternatives: list[GuardRequirementCondition] = []
    supporting: list[RungRead] = []
    exhaustive = True
    for run in (selected, *ancestors):
        conditions = tuple(getattr(run.rung, "_conditions", ()))
        branch_start = getattr(run.rung, "_branch_condition_start", 0)
        if run.kind == "branch" or branch_start:
            conditions = conditions[branch_start:]
        if not conditions:
            continue
        evaluation = _evaluate_run_guard_complement(
            run,
            projection,
            preserve_nested_false=preserve_nested_false,
        )
        if not evaluation.exact or evaluation.requirement is None:
            exhaustive = False
            for requirement, reads in _unique_scalar_guard_complements(run, projection):
                alternatives.append(requirement)
                supporting.extend(reads)
            continue
        alternatives.append(evaluation.requirement)
        supporting.extend(evaluation.supporting_reads)
    if not alternatives or not supporting:
        return _GuardEvaluation(True, tuple(supporting), exact=False)
    requirement = (
        alternatives[0]
        if len(alternatives) == 1 and exhaustive
        else GuardRequirementExpr(
            GuardLogic.ANY,
            tuple(alternatives),
            exhaustive=exhaustive,
        )
    )
    return _GuardEvaluation(True, tuple(supporting), requirement=requirement)


def _unique_scalar_guard_complements(
    run: RungRun,
    projection: ScanRungWriteProjection,
) -> tuple[tuple[GuardRequirementAtom, tuple[RungRead, ...]], ...]:
    """Recover exact scalar alternatives beside an opaque true guard term.

    Re-evaluating a composite condition can fail source-token matching after a
    short-circuited arm.  A top-level scalar leaf remains independently exact
    when each of its operands has one projection-owned read in this run.
    """

    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    run_reads = projection.reads_for_run(run)
    result: list[tuple[GuardRequirementAtom, tuple[RungRead, ...]]] = []
    for index, condition in enumerate(conditions):
        constraint = _guard_leaf_constraint(condition)
        complement = complement_scalar_constraint(constraint) if constraint is not None else None
        if constraint is None or complement is None:
            continue
        tag = getattr(constraint, "tag", None)
        if not isinstance(tag, str):
            continue
        names = (tag,)
        if isinstance(constraint, Cmp) and constraint.bound_is_tag:
            if not isinstance(constraint.bound, str):
                continue
            names = (*names, constraint.bound)
        selected_reads: list[RungRead] = []
        snapshot: dict[str, Any] = {}
        exact = True
        for name in names:
            matches = tuple(read for read in run_reads if read.occurrence.name == name)
            if len(matches) != 1:
                exact = False
                break
            selected_reads.append(matches[0])
            snapshot[name] = matches[0].occurrence.value
        if not exact or constraint_holds(constraint, snapshot) is not True:
            continue
        selected_reads.sort(key=lambda read: read.ordinal)
        occurrences = tuple(occurrence_snapshot(read) for read in selected_reads)
        result.append(
            (
                GuardRequirementAtom(
                    condition=complement,
                    supporting_occurrences=occurrences,
                    deadline=occurrences[-1],
                    source_path=(index,),
                ),
                tuple(selected_reads),
            )
        )
    return tuple(result)


def _unique_static_run(
    projection: ScanRungWriteProjection,
    rung: Any,
) -> RungRun | None:
    runs = tuple(run for run in projection.runs if run.rung is rung)
    return runs[0] if len(runs) == 1 else None


def _exact_guard_cause_run(
    selected: RungRun,
    projection: ScanRungWriteProjection,
) -> RungRun:
    """Climb only exact dynamic parents when a selected branch inherited false."""

    ancestors = tuple(
        sorted(
            (
                candidate
                for candidate in projection.runs
                if candidate is not selected
                and any(nested is selected for nested in candidate.rung_occurrences)
            ),
            key=lambda candidate: candidate.depth,
            reverse=True,
        )
    )
    for candidate in (selected, *ancestors):
        evaluation = _evaluate_run_guard(candidate, projection)
        if (
            not candidate.enabled
            and not evaluation.value
            and evaluation.exact
            and evaluation.requirement is not None
        ):
            return candidate
    return selected


def _guard_explanation(run: RungRun, projection: ScanRungWriteProjection) -> FailureExplanation:
    run = _exact_guard_cause_run(run, projection)
    evaluation = _evaluate_run_guard(run, projection)
    supporting = tuple(occurrence_snapshot(read) for read in evaluation.supporting_reads)
    if run.enabled or evaluation.value:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected dynamic rung was not disabled by its local guard",
            supporting_occurrences=supporting,
        )
    if not evaluation.exact or evaluation.requirement is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected false guard frontier is not exactly representable",
            supporting_occurrences=supporting,
        )
    return FailureExplanation(
        FailureExplanationKind.GUARD_FALSE,
        detail="selected dynamic rung guard was false",
        supporting_occurrences=supporting,
    )


def explain_selected_absence(
    observation: Any,
    projection: ScanRungWriteProjection,
    source_checkpoint: Any,
) -> FailureExplanation:
    """Explain only the exact selected absent producer, failing closed.

    The obligation's retained producer rung is never replaced by another
    writer. Spentness is proved from the exact source checkpoint memory key,
    not inferred from the destination or failed landing.
    """

    if getattr(observation, "disposition", None) != "ABSENT":
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="effect observation is not an absent selected writer",
        )
    obligation = getattr(observation, "obligation", None)
    producer_rung = getattr(obligation, "producer_rung", None)
    tag = getattr(obligation, "tag", None)
    if producer_rung is None or tag is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer identity is incomplete",
        )
    runs = tuple(run for run in projection.runs if run.rung is producer_rung)
    if not runs:
        return FailureExplanation(
            FailureExplanationKind.NOT_EXECUTABLE,
            detail="selected producer has no dynamic occurrence in this execution",
        )
    if len(runs) != 1:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer has several dynamic occurrences",
        )
    run = runs[0]
    if not run.enabled:
        return _guard_explanation(run, projection)

    guard_evaluation = _evaluate_run_guard(run, projection)
    supporting = tuple(occurrence_snapshot(read) for read in guard_evaluation.supporting_reads)

    writers = tuple(
        item
        for item in _instruction_runs(run.body)
        if instruction_writes_tag(item.instruction, tag)
    )
    if len(writers) != 1:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer write-site identity is ambiguous",
            supporting_occurrences=supporting,
        )
    instruction = writers[0].instruction
    if not bool(getattr(instruction, "oneshot", False)):
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="powered selected writer produced no exact write",
            supporting_occurrences=supporting,
        )
    source_work = getattr(getattr(source_checkpoint, "world", None), "work", None)
    source_state = getattr(source_work, "state", None)
    if source_state is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected one-shot source checkpoint is unavailable",
            supporting_occurrences=supporting,
        )
    memory_key = instruction.memory_key("_oneshot")
    if source_state.memory.get(memory_key) is True:
        return FailureExplanation(
            FailureExplanationKind.SPENT,
            detail=f"selected one-shot was spent at source memory {memory_key!r}",
            supporting_occurrences=supporting,
        )
    return FailureExplanation(
        FailureExplanationKind.UNKNOWN,
        detail="selected one-shot was armed but produced no exact write",
        supporting_occurrences=supporting,
    )


def _validate_source_identity(
    *,
    execution_epoch: Any,
    execution_owner: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    selected_writer: Any,
    projection: ScanRungWriteProjection,
) -> str | None:
    if execution_epoch is None or execution_owner is None:
        return "exact execution epoch owner is required"
    if getattr(execution_owner, "epoch", None) is not execution_epoch:
        return "execution owner does not own the exact epoch"
    if not (
        getattr(execution_epoch, "first_scan", projection.scan_id + 1)
        <= projection.scan_id
        <= getattr(execution_epoch, "last_scan", projection.scan_id - 1)
    ):
        return "execution epoch does not cover the exact projection"
    if source_checkpoint is None or getattr(source_checkpoint, "owner", None) is None:
        return "source checkpoint identity is required"
    if selected_writer is None:
        return "selected writer identity is required"
    return None


def _guard_atoms(
    condition: GuardRequirementCondition,
) -> tuple[GuardRequirementAtom, ...]:
    if isinstance(condition, GuardRequirementAtom):
        return (condition,)
    return tuple(atom for term in condition.terms for atom in _guard_atoms(term))


def _bind_guard_demanding_rung(
    condition: GuardRequirementCondition,
    rung: Any,
) -> GuardRequirementCondition:
    if isinstance(condition, GuardRequirementAtom):
        return replace(condition, demanding_rung=rung)
    return replace(
        condition,
        terms=tuple(_bind_guard_demanding_rung(term, rung) for term in condition.terms),
    )


def derive_guard_requirement_from_effect(
    observation: Any,
    projection: ScanRungWriteProjection,
    *,
    execution_epoch: Any,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Derive the exact false frontier of the selected producer/consumer.

    ``ABSENT`` explains only the retained producer. ``STRANDED`` may explain
    only the retained obliged consumer.  A consumer that ran and read the
    effect names itself directly; an exactly unique disabled consumer run names
    its own false guard even though, by definition, it never reached the effect
    read. Neither arm searches for another producer or consumer.
    """

    source_error = _validate_source_identity(
        execution_epoch=execution_epoch,
        execution_owner=execution_owner,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)

    obligation = getattr(observation, "obligation", None)
    if obligation is None or selected_writer != getattr(obligation, "producer", None):
        return _unknown("selected writer does not match the retained obligation")
    disposition = getattr(observation, "disposition", None)
    run: RungRun | None = None
    explanation: FailureExplanation
    guard_scope: tuple[Any, ...]

    if disposition == "ABSENT":
        explanation = explain_selected_absence(observation, projection, source_checkpoint)
        if explanation.kind is not FailureExplanationKind.GUARD_FALSE:
            return RequirementDerivation(explanation)
        run = _unique_static_run(projection, getattr(obligation, "producer_rung", None))
        guard_scope = ("producer_guard", getattr(obligation, "producer", None))
    elif disposition == "STRANDED":
        consumer_read = getattr(observation, "consumer_read", None)
        appeared = getattr(observation, "appeared", None)
        if appeared is None or not any(write is appeared for write in projection.writes):
            return _unknown("stranded effect has no exact selected producer occurrence")
        consumer_rung = getattr(obligation, "consumer_rung", None)
        if consumer_read is not None and any(read is consumer_read for read in projection.reads):
            run = consumer_read.run
            if run.rung is not consumer_rung:
                return _unknown("stranded read does not belong to the obliged consumer")
        else:
            run = _unique_static_run(projection, consumer_rung)
            if run is None or run.enabled:
                return _unknown("stranded effect has no exact disabled consumer run")
        explanation = _guard_explanation(run, projection)
        if explanation.kind is not FailureExplanationKind.GUARD_FALSE:
            return RequirementDerivation(explanation)
        guard_scope = ("consumer_guard", getattr(obligation, "consumer", None))
    else:
        return _unknown("effect is neither absent nor stranded")

    if run is None:
        return _unknown("selected guard has no unique dynamic occurrence")
    run = _exact_guard_cause_run(run, projection)
    evaluation = _evaluate_run_guard(run, projection)
    if not evaluation.exact or evaluation.requirement is None:
        return RequirementDerivation(explanation)
    atoms = _guard_atoms(evaluation.requirement)
    if not atoms or not evaluation.supporting_reads:
        return RequirementDerivation(explanation)

    demanding = occurrence_snapshot(evaluation.supporting_reads[-1])
    condition = _bind_guard_demanding_rung(evaluation.requirement, run.rung)
    source_walk = derive_occurrence_source_requirement(condition, projection)
    return RequirementDerivation(
        explanation=explanation,
        requirement=ActiveRequirement(
            # The Stage-2 walk is report-only. Production retains the exact
            # one-hop requirement until bounded closure owns execution.
            condition=condition,
            demanding_occurrence=demanding,
            # A compound false guard is decided at its final observed read.
            # Each OR arm keeps its own (possibly earlier) actionable deadline;
            # Phase 5 must select an arm before applying that atom's deadline.
            deadline=demanding,
            selected_writer=selected_writer,
            operand_authority=OperandAuthority.UNKNOWN,
            execution_epoch=execution_epoch,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=(*scope, guard_scope),
        ),
        source_walk=source_walk,
    )


def derive_overwriter_guard_requirement_from_effect(
    observation: Any,
    projection: ScanRungWriteProjection,
    *,
    execution_epoch: Any,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementDerivation:
    """Prevent one exact harmful writer by complementing its observed guard."""

    return derive_overwriter_guard_requirement_from_write(
        getattr(observation, "displacement", None),
        projection,
        disposition=getattr(observation, "disposition", None),
        execution_epoch=execution_epoch,
        execution_owner=execution_owner,
        selected_writer=selected_writer,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        phase=phase,
        provenance=provenance,
        scope=scope,
        preserved_values=preserved_values,
    )


def derive_overwriter_guard_requirement_from_write(
    displacement: RungWrite | None,
    projection: ScanRungWriteProjection,
    *,
    disposition: str | None = "DISPLACED",
    execution_epoch: Any,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementDerivation:
    """Complement the guard of one projection-owned harmful write.

    Effect recovery normally reaches this proof through an observation.  A
    causal checkpoint rebase can already possess the exact harmful write, so
    this narrower seam avoids manufacturing an observation while retaining
    the same source-identity and guard-exactness checks.
    """

    source_error = _validate_source_identity(
        execution_epoch=execution_epoch,
        execution_owner=execution_owner,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)
    if disposition not in {"OVERWRITTEN", "DISPLACED"}:
        return _unknown("effect has no harmful overwriter guard to complement")
    if displacement is None or not _contains_identity(projection.writes, displacement):
        return _unknown("harmful overwriter is not owned by the exact projection")
    run = _exact_guard_cause_run(displacement.run, projection)
    if not run.enabled:
        return _unknown("harmful overwriter run was not enabled")
    evaluation = _evaluate_enabling_path_complement(run, projection)
    if (
        not evaluation.value
        or not evaluation.exact
        or evaluation.requirement is None
        or not evaluation.supporting_reads
    ):
        return _unknown("harmful overwriter guard has no exact scalar complement")
    filtered = _residualize_guard_requirement(
        evaluation.requirement,
        preserved_values,
    )
    if filtered is None:
        return _unknown("harmful overwriter guard depends only on excluded channel state")
    condition = _bind_guard_demanding_rung(filtered, run.rung)
    source_walk = derive_occurrence_source_requirement(
        condition,
        projection,
        preserved_values=preserved_values,
    )
    # Preserve the pre-Stage-2 production seam: only protected terminal tags
    # use the established deadline refinement. The complete transitive walk is
    # exposed separately as report evidence for later bounded closure.
    condition = _refine_preserved_tag_deadlines(condition, projection, preserved_values)
    demanding = occurrence_snapshot(evaluation.supporting_reads[-1])
    explanation_kind = (
        FailureExplanationKind.OVERWRITTEN
        if disposition == "OVERWRITTEN"
        else FailureExplanationKind.DISPLACED
    )
    refined_support: list[EffectOccurrenceSnapshot] = [
        occurrence_snapshot(read) for read in evaluation.supporting_reads
    ]
    for atom in _guard_atoms(condition):
        for occurrence in (*atom.supporting_occurrences, atom.deadline):
            if occurrence not in refined_support:
                refined_support.append(occurrence)
    explanation = FailureExplanation(
        explanation_kind,
        detail="exact harmful overwriter guard was complemented",
        # Source walking may replace the final overwriter guard with an exact
        # earlier predecessor guard. The failed receipt must carry that atom's
        # proof surface so exact source matching can recover the transaction.
        supporting_occurrences=tuple(refined_support),
    )
    return RequirementDerivation(
        explanation=explanation,
        requirement=ActiveRequirement(
            condition=condition,
            demanding_occurrence=demanding,
            deadline=demanding,
            selected_writer=selected_writer,
            operand_authority=OperandAuthority.UNKNOWN,
            execution_epoch=execution_epoch,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=(*scope, ("overwriter_guard", occurrence_snapshot(displacement))),
            obstruction_occurrence=occurrence_snapshot(displacement),
        ),
        source_walk=source_walk,
    )


def _residualize_guard_requirement(
    condition: GuardRequirementCondition,
    preserved_values: tuple[tuple[str, Any], ...],
    *,
    preserve_incomplete: bool = False,
) -> GuardRequirementCondition | None:
    """Remove only alternatives disproved by values the effect must preserve."""

    if isinstance(condition, GuardRequirementAtom):
        return (
            None
            if constraint_holds(condition.condition, dict(preserved_values)) is False
            else condition
        )
    filtered = tuple(
        member
        for term in condition.terms
        if (
            member := _residualize_guard_requirement(
                term,
                preserved_values,
                preserve_incomplete=preserve_incomplete,
            )
        )
        is not None
    )
    if len(filtered) == len(condition.terms):
        return condition
    if condition.logic is GuardLogic.ALL or not filtered:
        return None
    if len(filtered) == 1 and (condition.exhaustive or not preserve_incomplete):
        return filtered[0]
    return replace(condition, terms=filtered, exhaustive=False)


def _refine_preserved_tag_deadlines(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    preserved_values: tuple[tuple[str, Any], ...],
) -> GuardRequirementCondition:
    """Preserve the established protected-tag production refinement.

    The complete Stage-2 occurrence walk is exposed separately as report-only
    evidence. This compatibility seam deliberately retains the pre-Stage-2
    exact-run complement, fallback, and identity behavior used by production.
    """

    protected = dict(preserved_values)

    def refine(
        item: GuardRequirementCondition,
        *,
        remaining_hops: int,
        visited: frozenset[tuple[int, int, str, int]],
    ) -> GuardRequirementCondition:
        if isinstance(item, GuardRequirementExpr):
            terms = tuple(
                refine(term, remaining_hops=remaining_hops, visited=visited) for term in item.terms
            )
            return item if terms == item.terms else replace(item, terms=terms)
        if remaining_hops <= 0 or not isinstance(item.condition, Cmp):
            return item
        constraint = item.condition
        if constraint.bound_is_tag or constraint.tag not in protected:
            return item
        protected_view = dict(protected)
        if constraint_holds(constraint, protected_view) is not True:
            return item

        reads = tuple(
            read for read in projection.reads if occurrence_snapshot(read) == item.deadline
        )
        if len(reads) != 1:
            return item
        read = reads[0]
        observed_view = {**protected_view, constraint.tag: read.occurrence.value}
        if constraint_holds(constraint, observed_view) is not False:
            return item
        transition = projection.transition_observed_by_read(read)
        if transition is None or transition.occurrence_ordinal is None:
            return item
        definitions = tuple(
            write
            for write in projection.writes
            if write.scan_id == read.scan_id
            and write.ordinal == transition.occurrence_ordinal
            and write.transition.tag_name == constraint.tag
            and write.transition.from_value == transition.from_value
            and write.transition.to_value == transition.to_value
        )
        if len(definitions) != 1:
            return item
        definition = definitions[0]
        if not definition.run.enabled or not definition.ordinal < read.ordinal:
            return item
        before_view = {**protected_view, constraint.tag: definition.transition.from_value}
        after_view = {**protected_view, constraint.tag: definition.transition.to_value}
        if (
            constraint_holds(constraint, before_view) is not True
            or constraint_holds(constraint, after_view) is not False
        ):
            return item

        visit = (read.scan_id, read.ordinal, constraint.tag, definition.ordinal)
        if visit in visited:
            return item
        evaluation = _evaluate_run_guard_complement(definition.run, projection)
        if (
            not evaluation.value
            or not evaluation.exact
            or evaluation.requirement is None
            or not evaluation.supporting_reads
        ):
            return item
        substitute = _residualize_guard_requirement(
            evaluation.requirement,
            preserved_values,
        )
        if substitute is None:
            return item
        substitute = _bind_guard_demanding_rung(substitute, definition.run.rung)
        substitute_atoms = _guard_atoms(substitute)
        if not substitute_atoms or any(
            atom.deadline.scan_id != read.scan_id or atom.deadline.ordinal >= read.ordinal
            for atom in substitute_atoms
        ):
            return item
        return refine(
            substitute,
            remaining_hops=remaining_hops - 1,
            visited=visited | {visit},
        )

    return refine(
        condition,
        remaining_hops=len(projection.writes),
        visited=frozenset(),
    )


def derive_occurrence_source_requirement(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementSourceWalk:
    """Follow false reads through exact earlier same-scan program writes.

    The projection's live occurrence objects are the oracle.  The returned
    condition is inert evidence; this function never compiles or applies an
    assignment, chooses an alternative, or executes another scan.
    """

    return _derive_occurrence_source_requirement(
        condition,
        projection,
        preserved_values=preserved_values,
    )


def _derive_occurrence_source_requirement(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
) -> RequirementSourceWalk:
    result = _walk_requirement_sources(
        condition,
        projection,
        preserved_values=preserved_values,
        ceiling=None,
        remaining_hops=len(projection.writes),
        visited=frozenset(),
    )
    normalized = _normalize_guard_requirement(result.condition)
    return result if normalized is result.condition else replace(result, condition=normalized)


def _walk_requirement_sources(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
    ceiling: int | None,
    remaining_hops: int,
    visited: frozenset[tuple[int, int]],
) -> RequirementSourceWalk:
    if isinstance(condition, GuardRequirementExpr):
        terms: list[GuardRequirementCondition] = []
        links: list[OccurrenceSourceLink] = []
        details: list[str] = []
        complete = True
        for term in condition.terms:
            result = _walk_requirement_sources(
                term,
                projection,
                preserved_values=preserved_values,
                ceiling=ceiling,
                remaining_hops=remaining_hops,
                visited=visited,
            )
            terms.append(result.condition)
            _extend_unique(links, result.links)
            if result.status is RequirementSourceWalkStatus.INCOMPLETE:
                complete = False
                if result.detail:
                    details.append(result.detail)
        rewritten = (
            condition if tuple(terms) == condition.terms else replace(condition, terms=tuple(terms))
        )
        return RequirementSourceWalk(
            RequirementSourceWalkStatus.COMPLETE
            if complete
            else RequirementSourceWalkStatus.INCOMPLETE,
            rewritten,
            tuple(links),
            "; ".join(dict.fromkeys(details)),
        )
    return _walk_requirement_atom(
        condition,
        projection,
        preserved_values=preserved_values,
        ceiling=ceiling,
        remaining_hops=remaining_hops,
        visited=visited,
    )


def _walk_requirement_atom(
    atom: GuardRequirementAtom,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
    ceiling: int | None,
    remaining_hops: int,
    visited: frozenset[tuple[int, int]],
) -> RequirementSourceWalk:
    def incomplete(detail: str) -> RequirementSourceWalk:
        return RequirementSourceWalk(
            RequirementSourceWalkStatus.INCOMPLETE,
            atom,
            detail=detail,
        )

    if atom.deadline.scan_id != projection.scan_id:
        return incomplete("requirement deadline belongs to a different projection scan")
    reads = tuple(read for read in projection.reads if occurrence_snapshot(read) == atom.deadline)
    if len(reads) != 1:
        return incomplete("requirement deadline read is unavailable or ambiguous")
    read = reads[0]
    condition_tag = getattr(atom.condition, "tag", None)
    if (
        read.occurrence.domain != "tag"
        or not isinstance(condition_tag, str)
        or condition_tag != read.occurrence.name
    ):
        return incomplete("requirement constraint does not match its exact tag read")
    if ceiling is not None and read.ordinal >= ceiling:
        return incomplete("requirement deadline does not strictly precede its source ceiling")

    from pyrung.core.executor import WriteOccurrence

    source = read.occurrence.source
    if not isinstance(source, WriteOccurrence):
        if source == "pending":
            return incomplete("requirement read has an unjournaled pending source")
        return RequirementSourceWalk(RequirementSourceWalkStatus.COMPLETE, atom)
    if remaining_hops <= 0:
        return incomplete("occurrence-source walk exhausted its exact write bound")
    definitions = tuple(write for write in projection.writes if write.occurrence is source)
    if len(definitions) != 1:
        return incomplete("requirement read source write is unavailable or ambiguous")
    definition = definitions[0]
    if (
        source.domain != "tag"
        or source.name != read.occurrence.name
        or source.after != read.occurrence.value
        or definition.scan_id != read.scan_id
        or definition.transition.tag_name != read.occurrence.name
        or definition.transition.to_value != read.occurrence.value
    ):
        return incomplete("requirement read carries inconsistent source-write evidence")
    if not definition.run.enabled or definition.ordinal >= read.ordinal:
        return incomplete("requirement source write is disabled or not strictly earlier")
    if ceiling is not None and definition.ordinal >= ceiling:
        return incomplete("requirement source write does not strictly decrease")
    visit = (id(read.occurrence), id(source))
    if visit in visited:
        return incomplete("occurrence-source walk repeated an exact source link")
    if not isinstance(atom.condition, Cmp) or atom.condition.bound_is_tag:
        return incomplete("same-scan sourced requirement is not an exact scalar constraint")
    protected = dict(preserved_values)
    if atom.condition.tag in protected and constraint_holds(atom.condition, protected) is not True:
        # A terminal effect may require this channel to retain a value which
        # disproves the guard atom.  Preventing an earlier writer cannot make
        # that self-conflicting arm a compatible way to preserve the effect.
        return RequirementSourceWalk(RequirementSourceWalkStatus.COMPLETE, atom)
    before = {atom.condition.tag: definition.transition.from_value}
    after = {atom.condition.tag: definition.transition.to_value}
    if (
        constraint_holds(atom.condition, before) is not True
        or constraint_holds(atom.condition, after) is not False
    ):
        return incomplete("source write does not exactly cross the required scalar truth")

    evaluation = _evaluate_enabling_path_complement(
        definition.run,
        projection,
        preserve_nested_false=True,
    )
    if (
        not evaluation.value
        or not evaluation.exact
        or evaluation.requirement is None
        or not evaluation.supporting_reads
    ):
        return incomplete("source writer enabling path has no exact complement")
    substitute = _residualize_guard_requirement(
        evaluation.requirement,
        preserved_values,
        preserve_incomplete=True,
    )
    if substitute is None:
        return incomplete("source writer guard depends only on excluded preserved state")
    substitute = _bind_guard_demanding_rung(substitute, definition.run.rung)
    substitute_atoms = _guard_atoms(substitute)
    if not substitute_atoms:
        return incomplete("source writer complement has no exact scalar atom")
    if any(
        candidate.deadline.scan_id != read.scan_id
        or candidate.deadline.ordinal >= definition.ordinal
        for candidate in substitute_atoms
    ):
        return incomplete("source writer complement has a non-decreasing deadline")
    required_address = _static_run_address(projection, read.run)
    required_instruction_path = _read_instruction_path(read)
    source_address = _static_run_address(projection, definition.run)
    instruction_path = _instruction_occurrence_path(definition)
    if (
        required_address is None
        or required_instruction_path is None
        or source_address is None
        or instruction_path is None
    ):
        return incomplete("source writer dynamic branch or instruction identity is unavailable")
    link = OccurrenceSourceLink(
        required_read=occurrence_snapshot(read),
        source_write=occurrence_snapshot(definition),
        enabling_reads=tuple(occurrence_snapshot(item) for item in evaluation.supporting_reads),
        required_address=required_address,
        required_instruction_path=required_instruction_path,
        source_address=source_address,
        instruction_path=instruction_path,
    )
    substitute = _prepend_source_link(substitute, (*atom.source_links, link))
    nested = _walk_requirement_sources(
        substitute,
        projection,
        preserved_values=preserved_values,
        ceiling=definition.ordinal,
        remaining_hops=remaining_hops - 1,
        visited=visited | {visit},
    )
    links = [link]
    _extend_unique(links, nested.links)
    return replace(nested, links=tuple(links))


def _prepend_source_link(
    condition: GuardRequirementCondition,
    prefix: tuple[OccurrenceSourceLink, ...],
) -> GuardRequirementCondition:
    if isinstance(condition, GuardRequirementAtom):
        return replace(condition, source_links=(*prefix, *condition.source_links))
    return replace(
        condition,
        terms=tuple(_prepend_source_link(term, prefix) for term in condition.terms),
    )


def _static_run_address(
    projection: ScanRungWriteProjection,
    run: RungRun,
) -> StaticRungAddress | None:
    """Detach the static branch path of one exact dynamic run."""

    def find_branch(root: Any, selected: Any, path: tuple[int, ...] = ()) -> tuple[int, ...] | None:
        if root is selected:
            return path
        for index, branch in enumerate(getattr(root, "_branches", ())):
            found = find_branch(branch, selected, (*path, index))
            if found is not None:
                return found
        return None

    candidates = tuple(
        candidate
        for candidate in projection.runs
        if candidate.rung_id == run.rung_id and candidate.kind != "branch"
    )
    paths = tuple(
        path
        for candidate in candidates
        if (path := find_branch(candidate.rung, run.rung)) is not None
    )
    unique = tuple(dict.fromkeys(paths))
    if len(unique) != 1:
        return None
    return (run.rung_id.subroutine, run.rung_id.rung_index, unique[0])


def _instruction_run_path(
    run: RungRun,
    selected: InstructionRun,
) -> tuple[int, ...] | None:
    """Return the exact journal path to one projection-owned instruction."""

    def find(body: tuple[Any, ...], prefix: tuple[int, ...]) -> tuple[int, ...] | None:
        for index, item in enumerate(body):
            path = (*prefix, index)
            if item is selected:
                return path
            if isinstance(item, InstructionRun | LoopIterationRun):
                nested = find(item.body, path)
                if nested is not None:
                    return nested
        return None

    return find(run.body, ())


def _read_instruction_path(read: RungRead) -> tuple[int, ...] | None:
    """Detach a required read's exact instruction owner, or rung guard root."""

    if read.instruction is None:
        return ()
    return _instruction_run_path(read.run, read.instruction)


def _instruction_occurrence_path(write: RungWrite) -> tuple[int, ...] | None:
    """Return the exact journal path to a source write's InstructionRun."""

    if write.instruction is None:
        return None
    return _instruction_run_path(write.run, write.instruction)


def _normalize_guard_requirement(
    condition: GuardRequirementCondition,
) -> GuardRequirementCondition:
    """Flatten only identical Boolean structure without choosing an arm."""

    if isinstance(condition, GuardRequirementAtom):
        return condition
    terms: list[GuardRequirementCondition] = []
    exhaustive = condition.exhaustive
    for term in condition.terms:
        normalized = _normalize_guard_requirement(term)
        if isinstance(normalized, GuardRequirementExpr) and normalized.logic is condition.logic:
            terms.extend(normalized.terms)
            exhaustive = exhaustive and normalized.exhaustive
        else:
            terms.append(normalized)
    unique: list[GuardRequirementCondition] = []
    for term in terms:
        if not any(term == existing for existing in unique):
            unique.append(term)
    if len(unique) == 1 and exhaustive:
        return unique[0]
    rewritten = tuple(unique)
    if rewritten == condition.terms and exhaustive is condition.exhaustive:
        return condition
    return GuardRequirementExpr(condition.logic, rewritten, exhaustive=exhaustive)


def _extend_unique(
    target: list[OccurrenceSourceLink],
    additions: tuple[OccurrenceSourceLink, ...],
) -> None:
    for addition in additions:
        if addition not in target:
            target.append(addition)


def derive_advance_operand_requirement(
    index: AdvanceIndex,
    channel: str,
    *,
    desired_completion: bool,
    projection: ScanRungWriteProjection,
    definition_write: RungWrite,
    operand_read: RungRead,
    demanding_read: RungRead,
    operand_authority: OperandAuthority,
    execution_epoch: Any,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    explanation_kind: FailureExplanationKind,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Derive from one exact dynamic owner definition and causal read.

    Every proof input remains projection-owned until all dynamic identity and
    ordinal checks pass. Only then are occurrences detached into the retained
    requirement.
    """

    source_error = _validate_source_identity(
        execution_epoch=execution_epoch,
        execution_owner=execution_owner,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)

    conflicts = index.conflict(channel)
    if conflicts:
        return _unknown(index.conflict_message(channel) or "ambiguous advance owner")
    owner = index.resolve(channel)
    if owner is None:
        return _unknown(f"no advance owner for {channel!r}")

    occurrence_error = _validate_dynamic_occurrences(
        owner,
        channel,
        projection,
        definition_write,
        operand_read,
        demanding_read,
    )
    if occurrence_error is not None:
        return _unknown(occurrence_error)

    observed_operands = _exact_instruction_operands(projection, definition_write, owner)
    if observed_operands is None:
        return _unknown("owner occurrence has repeated operand reads")
    boundary_fn = owner.profile.completion_boundary
    if boundary_fn is None:
        return _unknown("advance owner does not declare a completion boundary")
    boundary = boundary_fn(observed_operands)
    if boundary is None:
        return _unknown("owner completion boundary is unavailable")
    branch_error = _completion_branch_error(
        owner,
        projection,
        definition_write,
        observed_operands,
        boundary,
    )
    if branch_error is not None:
        return _unknown(branch_error)
    relation = boundary if desired_completion else complement_scalar_constraint(boundary)
    if relation is None:
        return _unknown("owner completion boundary cannot be complemented exactly")

    condition = _literal_requirement_for_operand(
        relation,
        operand_read,
        observed_operands,
    )
    if condition is None:
        return _unknown("owner relation cannot be transposed onto the exact operand")

    operand_snapshot = occurrence_snapshot(operand_read)
    demanding_snapshot = occurrence_snapshot(demanding_read)
    accumulator_write = _post_advance_accumulator_write(projection, definition_write, owner)
    assert accumulator_write is not None  # proved by _completion_branch_error
    accumulator_snapshot = occurrence_snapshot(accumulator_write)
    return RequirementDerivation(
        explanation=FailureExplanation(
            explanation_kind,
            supporting_occurrences=(
                operand_snapshot,
                accumulator_snapshot,
                demanding_snapshot,
            ),
        ),
        requirement=ActiveRequirement(
            condition=condition,
            demanding_occurrence=demanding_snapshot,
            deadline=operand_snapshot,
            selected_writer=selected_writer,
            operand_authority=operand_authority,
            execution_epoch=execution_epoch,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=tuple(scope),
        ),
    )


def derive_advance_requirement_from_effect(
    index: AdvanceIndex,
    projection: ScanRungWriteProjection,
    observation: Any,
    *,
    operand_authorities: Mapping[str, OperandAuthority],
    execution_epoch: Any,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Follow one exact failed effect to an instruction-owned completion read.

    The effect observation already selected its overwriter/consumer and local
    enabling reads. This adapter considers only consequential Boolean reads
    with one unambiguous Advance owner, then follows their exact same-scan
    definition and chooses an explicitly classified owner operand. It never
    treats arbitrary scan reads as requirements.
    """

    disposition = getattr(observation, "disposition", None)
    if disposition not in {"OVERWRITTEN", "DISPLACED"}:
        return _unknown("effect disposition has no advance completion inversion")
    try:
        explanation_kind = FailureExplanationKind(disposition.lower())
    except ValueError:
        return _unknown("effect disposition has no typed requirement explanation")

    candidates: list[RequirementDerivation] = []
    # These surfaces are complementary.  The immutable displacement closure
    # owns the final writer's exact local/caller guards.  ``observed_reads``
    # may additionally own an exact predecessor chain (for example a nested
    # rollback whose caller consumed an intermediate value produced by an
    # Advance completion).  Replacing the latter with the former loses that
    # conductivity; append only identities not already present instead.
    displacement_reads = tuple(getattr(observation, "displacement_enabling_reads", ()))
    observed_reads = tuple(getattr(observation, "observed_reads", ()))
    demanding_reads = (
        *displacement_reads,
        *(
            read
            for read in observed_reads
            if not any(read is exact for exact in displacement_reads)
        ),
    )
    for demanding_read in demanding_reads:
        channel = demanding_read.occurrence.name
        observed_value = demanding_read.occurrence.value
        if not isinstance(observed_value, bool) or index.resolve(channel) is None:
            continue
        transition = projection.transition_observed_by_read(demanding_read)
        if transition is None or transition.occurrence_ordinal is None:
            continue
        definitions = tuple(
            write
            for write in projection.writes
            if write.ordinal == transition.occurrence_ordinal
            and write.transition.tag_name == channel
            and write.transition.to_value == observed_value
        )
        if len(definitions) != 1:
            continue
        definition = definitions[0]
        owner = index.resolve(channel)
        if owner is None or owner.profile.completion_boundary is None:
            continue
        exact_operands = _exact_instruction_operands(projection, definition, owner)
        if exact_operands is None:
            continue
        boundary = owner.profile.completion_boundary(exact_operands)
        operand_names: tuple[str, ...]
        if isinstance(boundary, Cmp) and boundary.bound_is_tag:
            # Only the distinct bound read is at the same semantic time as the
            # post-advance comparison. The boundary coordinate's sibling read
            # is its entry value and needs a delta inverse Phase 4 does not own.
            operand_names = (str(boundary.bound),)
        else:
            continue
        sibling_reads = projection.reads_observed_by_write(definition)
        for operand_name in operand_names:
            authority = operand_authorities.get(operand_name)
            matching_reads = tuple(
                read for read in sibling_reads if read.occurrence.name == operand_name
            )
            if authority is None or len(matching_reads) != 1:
                continue
            candidates.append(
                derive_advance_operand_requirement(
                    index,
                    channel,
                    desired_completion=not observed_value,
                    projection=projection,
                    definition_write=definition,
                    operand_read=matching_reads[0],
                    demanding_read=demanding_read,
                    operand_authority=authority,
                    execution_epoch=execution_epoch,
                    execution_owner=execution_owner,
                    selected_writer=selected_writer,
                    source_world_key=source_world_key,
                    source_checkpoint=source_checkpoint,
                    explanation_kind=explanation_kind,
                    phase=phase,
                    provenance=provenance,
                    scope=scope,
                )
            )
            break

    exact = tuple(result for result in candidates if result.requirement is not None)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _unknown("failed effect has several exact advance inversions")
    if len(candidates) == 1:
        # Preserve the exact owner's fail-closed reason.  The generic fallback
        # obscures which identity/timing check rejected an otherwise unique
        # consequential read and makes causal integration impossible to audit.
        return candidates[0]
    return _unknown("failed effect has no exact advance inversion")


def _validate_dynamic_occurrences(
    owner: AdvanceOwner,
    channel: str,
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    operand: RungRead,
    demand: RungRead,
) -> str | None:
    if not _contains_identity(projection.writes, definition):
        return "owner definition is not owned by the exact projection"
    if not _contains_identity(projection.reads, operand) or not _contains_identity(
        projection.reads, demand
    ):
        return "requirement reads are not owned by the exact projection"
    if not (definition.scan_id == operand.scan_id == demand.scan_id == projection.scan_id):
        return "owner definition and requirement reads cross scan boundaries"
    if definition.transition.tag_name != channel or demand.occurrence.name != channel:
        return "demand does not observe the selected owner channel"
    if (
        definition.instruction is None
        or definition.instruction.instruction is not owner.instruction
    ):
        return "channel definition does not belong to the resolved advance owner"
    sibling_reads = projection.reads_observed_by_write(definition)
    if not _contains_identity(sibling_reads, operand):
        return "selected operand is not an exact owner-instruction read"
    if operand.instruction is not definition.instruction:
        return "selected operand belongs to a different instruction occurrence"
    if operand.run is not definition.run:
        return "dynamic owner operand occurrence identity is inconsistent"
    if not operand.ordinal < definition.ordinal <= demand.ordinal:
        return "owner operand is not timely for the demanding read"

    observed = projection.transition_observed_by_read(demand)
    if observed is None or (
        observed.tag_name != definition.transition.tag_name
        or observed.occurrence_ordinal != definition.ordinal
        or observed.to_value != definition.transition.to_value
    ):
        return "demand did not observe the exact owner definition"
    return None
