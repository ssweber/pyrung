"""Selected effect obligations and exact execution observations.

Selection policy and execution fact capture deliberately meet only at the
immutable :class:`EffectObligation`.  Trace/navigation decide the required
shape; the causal projection reports every exact occurrence without rebuilding
that policy from the landing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

from pyrung.core.analysis.causal._rung_writes import RungRead, RungWrite
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_values import _SnapshotView, _values_match
from pyrung.core.analysis.write_sites import (
    instruction_write_targets,
    static_write_target_names,
)
from pyrung.core.context import RungId
from pyrung.core.executor import InstructionRun, LoopIterationRun, RungRun
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        OrderedEffectObservation,
        ScanRungWriteProjection,
    )
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.context import ConditionView

StaticRungAddress = tuple[str | None, int, tuple[int, ...]]
EffectDisposition = Literal[
    "ABSENT",
    "OVERWRITTEN",
    "STRANDED",
    "DISPLACED",
    "SURVIVED",
    "SUBSUMED",
    "PREVENTED",
    "FIRED",
    "UNKNOWN",
]


def _detached(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, tuple | list):
        return tuple(_detached(member) for member in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_detached(member) for member in value), key=repr))
    if isinstance(value, Mapping):
        return MappingProxyType(
            {_detached(key): _detached(member) for key, member in value.items()}
        )
    return repr(value)


@dataclass(frozen=True)
class EffectPathStep:
    """One writer on a selected requirement-tree path.

    ``local_requirements`` are policy input, not recorded execution facts.
    They retain the writer's direct child requirements until the final
    producer/consumer pair is selected.
    """

    node_index: int
    tag: str
    value: Any
    local_requirements: tuple[tuple[str, Any], ...] = ()


class EffectPolarity(StrEnum):
    """Whether one exact writer must produce or must not produce its effect."""

    PRODUCE = "produce"
    PREVENT = "prevent"


@dataclass(frozen=True)
class EffectOccurrenceSelector:
    """Relocatable identity of one exact access inside a dynamic rung owner.

    Absolute scan ordinals and run order are deliberately absent. Adding a
    top-of-scan synthetic rung shifts both without changing the selected user
    occurrence. The static branch, structural instruction path, dynamic call
    identity, and access index instead relocate that occurrence on an exact
    candidate projection.
    """

    kind: Literal["read", "write"]
    tag: str
    static_address: StaticRungAddress
    instruction_path: tuple[int, ...]
    execution_kind: str
    caller_rung: int
    call_stack: tuple[str, ...]
    depth: int
    call_invocation: int | None
    access_index: int


@dataclass(frozen=True)
class EffectObligation:
    """One selected producer/effect promised to one obliged consumer."""

    tag: str
    value: Any
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]
    boundary: tuple[str, Any] | None = None
    # True only for the user's exact global target after its selected producer
    # has actually appeared in an act-owned execution window. Ordinary
    # terminal effects remain endpoint evidence; this one may veto acceptance
    # when a later same-scan write displaces it before scan exit.
    terminal_target: bool = False
    # The selected trace contains a downstream consumer, but static analysis
    # cannot name it (for example, across an indirect data lookup).  Only one
    # exact producer-sourced read before displacement may complete this
    # obligation; ordinary ``consumer=None`` effects still require scan-exit
    # survival.
    projected_consumer: bool = False
    producer_rung: object = field(compare=False, repr=False, default=None)
    consumer_rung: object | None = field(compare=False, repr=False, default=None)
    polarity: EffectPolarity = field(default=EffectPolarity.PRODUCE, repr=False)
    occurrence_selector: EffectOccurrenceSelector | None = field(default=None, repr=False)


@dataclass(frozen=True)
class EffectExpectation:
    """Act-owned ordered conjunction of atomic obligations."""

    obligations: tuple[EffectObligation, ...]

    def __post_init__(self) -> None:
        if not self.obligations:
            raise ValueError("an effect expectation must contain an obligation")


@dataclass(frozen=True)
class EffectOccurrenceSnapshot:
    """Detached exact read/write occurrence retained by recording."""

    kind: Literal["read", "write"]
    ordinal: int
    scan_id: int
    run_order: int
    call_invocation: int | None
    rung: tuple[str | None, int]
    execution_kind: str
    caller_rung: int
    call_stack: tuple[str, ...]
    depth: int
    enabled: bool
    tag: str
    values: tuple[Any, ...]
    branch_path: tuple[int, ...] | None = None

    @property
    def dynamic_address(self) -> tuple[Any, ...]:
        """Stable full address within one exact executed scan projection."""

        return (
            self.rung,
            self.execution_kind,
            self.caller_rung,
            self.call_stack,
            self.depth,
            self.call_invocation,
            self.run_order,
            self.ordinal,
        )


@dataclass(frozen=True)
class EffectEpochSnapshot:
    """Detached interval metadata for the Epoch which owns an observation."""

    first_scan: int
    last_scan: int
    initial_scan_id: int


@dataclass(frozen=True)
class EffectObligationSnapshot:
    tag: str
    value: Any
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]
    boundary: tuple[str, Any] | None
    terminal_target: bool = False
    projected_consumer: bool = False


@dataclass(frozen=True)
class EffectObservationSnapshot:
    """PLC-free factual result for one obligation occurrence."""

    disposition: EffectDisposition
    obligation: EffectObligationSnapshot
    appeared: EffectOccurrenceSnapshot | None = None
    consumer_read: EffectOccurrenceSnapshot | None = None
    displacement: EffectOccurrenceSnapshot | None = None
    displaced_read: EffectOccurrenceSnapshot | None = None
    observed_reads: tuple[EffectOccurrenceSnapshot, ...] = ()
    displacement_enabling_reads: tuple[EffectOccurrenceSnapshot, ...] = ()
    detail: str = ""
    execution_epoch: EffectEpochSnapshot | None = None


@dataclass(frozen=True)
class ConsumerBoundary:
    """Exact historical handoff plus its relocatable replay selectors.

    The occurrence snapshots are immutable evidence of what the accepted
    transaction actually did. The selectors name the same accesses without
    depending on scan ordinals or synthetic-rung run order, while the offsets
    place a hard bound on a replay from that transaction's source.
    """

    produced_occurrence: EffectOccurrenceSnapshot
    consumer_occurrence: EffectOccurrenceSnapshot
    producer_selector: EffectOccurrenceSelector
    consumer_selector: EffectOccurrenceSelector
    producer_scan_offset: int
    consumer_scan_offset: int

    def __post_init__(self) -> None:
        if self.produced_occurrence.kind != "write" or self.producer_selector.kind != "write":
            raise ValueError("consumer boundary producer must be an exact write")
        if self.consumer_occurrence.kind != "read" or self.consumer_selector.kind != "read":
            raise ValueError("consumer boundary consumer must be an exact read")
        if (
            self.produced_occurrence.tag != self.consumer_occurrence.tag
            or self.producer_selector.tag != self.produced_occurrence.tag
            or self.consumer_selector.tag != self.consumer_occurrence.tag
        ):
            raise ValueError("consumer boundary must follow one produced tag")
        if not 1 <= self.producer_scan_offset <= self.consumer_scan_offset:
            raise ValueError("consumer boundary scan offsets are invalid")


@dataclass(frozen=True)
class EffectObservation:
    """Internal exact observation, retaining projection occurrence identity."""

    obligation: EffectObligation
    disposition: EffectDisposition
    appeared: RungWrite | None = field(default=None, compare=False, repr=False)
    consumer_read: RungRead | None = field(default=None, compare=False, repr=False)
    displacement: RungWrite | None = field(default=None, compare=False, repr=False)
    displaced_read: RungRead | None = field(default=None, compare=False, repr=False)
    observed_reads: tuple[RungRead, ...] = field(default=(), compare=False, repr=False)
    displacement_enabling_reads: tuple[RungRead, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )
    detail: str = ""
    execution_epoch: Any = field(default=None, compare=False, repr=False)
    execution_owner: Any = field(default=None, compare=False, repr=False)
    # Internal live proof surface.  A later replay query may reconstruct equal
    # occurrences as different objects; Phase-4/5 exact inversion must consume
    # the very projection which produced these observation members.
    execution_projection: ScanRungWriteProjection | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Freeze the exact displacement ancestry while its projection is owned."""

        if self.displacement is None:
            if self.displacement_enabling_reads:
                raise ValueError("displacement ancestry requires a displacement write")
            return
        if self.execution_projection is None:
            return
        exact = self.execution_projection.enabling_read_closure_observed_by_write(self.displacement)
        if self.displacement_enabling_reads and any(
            left is not right
            for left, right in zip(self.displacement_enabling_reads, exact, strict=False)
        ):
            raise ValueError("displacement ancestry is not owned by the exact projection")
        if self.displacement_enabling_reads and len(self.displacement_enabling_reads) != len(exact):
            raise ValueError("displacement ancestry is incomplete for the exact projection")
        if not self.displacement_enabling_reads:
            object.__setattr__(
                self,
                "displacement_enabling_reads",
                exact,
            )

    def diagnostic_snapshot(self) -> EffectObservationSnapshot:
        return EffectObservationSnapshot(
            disposition=self.disposition,
            obligation=obligation_snapshot(self.obligation),
            appeared=occurrence_snapshot(self.appeared) if self.appeared is not None else None,
            consumer_read=(
                occurrence_snapshot(self.consumer_read) if self.consumer_read is not None else None
            ),
            displacement=(
                occurrence_snapshot(self.displacement) if self.displacement is not None else None
            ),
            displaced_read=(
                occurrence_snapshot(self.displaced_read)
                if self.displaced_read is not None
                else None
            ),
            observed_reads=tuple(occurrence_snapshot(read) for read in self.observed_reads),
            displacement_enabling_reads=tuple(
                occurrence_snapshot(read) for read in self.displacement_enabling_reads
            ),
            detail=self.detail,
            execution_epoch=(
                EffectEpochSnapshot(
                    self.execution_epoch.first_scan,
                    self.execution_epoch.last_scan,
                    self.execution_epoch.initial_scan_id,
                )
                if self.execution_epoch is not None
                else None
            ),
        )


def _writer_address(pdg: ProgramGraph, node_index: int) -> StaticRungAddress:
    node = pdg.rung_nodes[node_index]
    return (node.subroutine, node.rung_index, node.branch_path)


def _run_static_address(
    projection: ScanRungWriteProjection,
    run: RungRun,
) -> StaticRungAddress | None:
    """Resolve one dynamic run to its immutable user/synthetic branch."""

    def find_branch(root: Any, selected: Any, path: tuple[int, ...] = ()) -> tuple[int, ...] | None:
        if root is selected:
            return path
        for index, branch in enumerate(getattr(root, "_branches", ())):
            found = find_branch(branch, selected, (*path, index))
            if found is not None:
                return found
        return None

    roots = tuple(
        candidate
        for candidate in projection.runs
        if candidate.rung_id == run.rung_id and candidate.kind != "branch"
    )
    paths = tuple(
        path for candidate in roots if (path := find_branch(candidate.rung, run.rung)) is not None
    )
    unique = tuple(dict.fromkeys(paths))
    if len(unique) != 1:
        return None
    return (run.rung_id.subroutine, run.rung_id.rung_index, unique[0])


def _instruction_path(
    run: RungRun,
    selected: InstructionRun,
) -> tuple[int, ...] | None:
    """Return a structural owner path unaffected by recorded read/write count."""

    def find(body: tuple[Any, ...], prefix: tuple[int, ...]) -> tuple[int, ...] | None:
        owners = tuple(item for item in body if isinstance(item, InstructionRun | LoopIterationRun))
        for index, item in enumerate(owners):
            path = (*prefix, index)
            if item is selected:
                return path
            nested = find(item.body, path)
            if nested is not None:
                return nested
        return None

    return find(run.body, ())


def _selector_accesses(
    projection: ScanRungWriteProjection,
    occurrence: RungRead | RungWrite,
) -> tuple[RungRead | RungWrite, ...]:
    if isinstance(occurrence, RungWrite):
        return tuple(
            candidate
            for candidate in projection.writes_for_run(occurrence.run)
            if candidate.instruction is occurrence.instruction
            and candidate.transition.tag_name == occurrence.transition.tag_name
        )
    return tuple(
        candidate
        for candidate in projection.reads_for_run(occurrence.run)
        if candidate.instruction is occurrence.instruction
        and candidate.occurrence.name == occurrence.occurrence.name
    )


def occurrence_selector(
    projection: ScanRungWriteProjection,
    occurrence: RungRead | RungWrite,
) -> EffectOccurrenceSelector | None:
    """Build a relocatable selector from one projection-owned exact access."""

    owned = projection.writes if isinstance(occurrence, RungWrite) else projection.reads
    if occurrence.scan_id != projection.scan_id or not any(
        candidate is occurrence for candidate in owned
    ):
        return None
    static_address = _run_static_address(projection, occurrence.run)
    if static_address is None:
        return None
    instruction_path = (
        ()
        if occurrence.instruction is None
        else _instruction_path(occurrence.run, occurrence.instruction)
    )
    if instruction_path is None:
        return None
    accesses = _selector_accesses(projection, occurrence)
    matches = tuple(index for index, candidate in enumerate(accesses) if candidate is occurrence)
    if len(matches) != 1:
        return None
    tag = (
        occurrence.transition.tag_name
        if isinstance(occurrence, RungWrite)
        else occurrence.occurrence.name
    )
    return EffectOccurrenceSelector(
        kind="write" if isinstance(occurrence, RungWrite) else "read",
        tag=tag,
        static_address=static_address,
        instruction_path=instruction_path,
        execution_kind=occurrence.run.kind,
        caller_rung=occurrence.run.caller_rung,
        call_stack=occurrence.run.call_stack,
        depth=occurrence.run.depth,
        call_invocation=occurrence.call_invocation,
        access_index=matches[0],
    )


def displacement_consumer_read(observation: EffectObservation) -> RungRead | None:
    """Return the one guard read which consumed an appeared value before displacing it.

    A cross-scan overwriter cannot point its entry read directly at the prior
    scan's ``WriteOccurrence``.  Its exact guard ancestry still records the
    handoff: one read of the appeared tag/value enabled the displacement.
    Ambiguous repeated reads fail closed instead of inventing a consumer.
    """

    appeared = observation.appeared
    if appeared is None or observation.displacement is None:
        return None
    matches = tuple(
        read
        for read in observation.displacement_enabling_reads
        if read.occurrence.name == appeared.transition.tag_name
        and _values_match(read.occurrence.value, appeared.transition.to_value)
    )
    return matches[0] if len(matches) == 1 else None


def resolve_occurrence_selector(
    projection: ScanRungWriteProjection,
    selector: EffectOccurrenceSelector,
) -> RungRead | RungWrite | None:
    """Resolve one relocatable access on an exact replay projection."""

    runs = tuple(
        run
        for run in projection.runs
        if _run_static_address(projection, run) == selector.static_address
        and run.kind == selector.execution_kind
        and run.caller_rung == selector.caller_rung
        and run.call_stack == selector.call_stack
        and run.depth == selector.depth
        and projection._call_invocation_by_run.get(id(run)) == selector.call_invocation
    )
    if len(runs) != 1:
        return None
    run = runs[0]

    def matches_instruction(access: RungRead | RungWrite) -> bool:
        return (
            selector.instruction_path == ()
            if access.instruction is None
            else _instruction_path(run, access.instruction) == selector.instruction_path
        )

    accesses: tuple[RungRead | RungWrite, ...]
    if selector.kind == "write":
        accesses = tuple(
            write
            for write in projection.writes_for_run(run)
            if matches_instruction(write) and write.transition.tag_name == selector.tag
        )
    else:
        accesses = tuple(
            read
            for read in projection.reads_for_run(run)
            if matches_instruction(read) and read.occurrence.name == selector.tag
        )
    if selector.access_index < 0 or selector.access_index >= len(accesses):
        return None
    return accesses[selector.access_index]


def consumer_boundary_reached(
    boundary: ConsumerBoundary,
    *,
    source_scan: int,
    projection_at: Callable[[int], ScanRungWriteProjection | None],
) -> bool:
    """Whether a replay carried the same produced value into the same consumer."""

    producer_scan = source_scan + boundary.producer_scan_offset
    consumer_scan = source_scan + boundary.consumer_scan_offset
    producer_projection = projection_at(producer_scan)
    consumer_projection = projection_at(consumer_scan)
    if producer_projection is None or consumer_projection is None:
        return False
    produced = resolve_occurrence_selector(producer_projection, boundary.producer_selector)
    consumed = resolve_occurrence_selector(consumer_projection, boundary.consumer_selector)
    if not isinstance(produced, RungWrite) or not isinstance(consumed, RungRead):
        return False
    historical_value = boundary.produced_occurrence.values[-1]
    consumed_value = boundary.consumer_occurrence.values[-1]
    if (
        not produced.run.enabled
        or not consumed.run.enabled
        or not _values_match(historical_value, consumed_value)
        or not _values_match(produced.transition.to_value, historical_value)
        or not _values_match(consumed.occurrence.value, consumed_value)
    ):
        return False
    tag = produced.transition.tag_name
    if producer_scan == consumer_scan:
        transition = consumer_projection.transition_observed_by_read(consumed)
        return bool(
            transition is not None
            and transition.tag_name == tag
            and transition.occurrence_ordinal == produced.ordinal
        )

    # An entry read intentionally has no same-scan WriteOccurrence source.
    # Prove the retained handoff from the complete ordered projections instead:
    # the selected producer must own scan exit, every intervening scan must
    # preserve that value without replacing its identity, and the selected
    # consumer must read it before any same-tag write in its own scan.
    if any(
        write.transition.tag_name == tag and write.ordinal > produced.ordinal
        for write in producer_projection.writes
    ) or not _values_match(producer_projection.exit_tags.get(tag), historical_value):
        return False
    for scan_id in range(producer_scan + 1, consumer_scan):
        projection = projection_at(scan_id)
        if (
            projection is None
            or not _values_match(projection.entry_tags.get(tag), historical_value)
            or not _values_match(projection.exit_tags.get(tag), historical_value)
            or any(write.transition.tag_name == tag for write in projection.writes)
        ):
            return False
    return bool(
        _values_match(consumer_projection.entry_tags.get(tag), historical_value)
        and consumer_projection.transition_observed_by_read(consumed) is None
        and not any(
            write.transition.tag_name == tag and write.ordinal < consumed.ordinal
            for write in consumer_projection.writes
        )
    )


def consumer_stop_reached(
    boundary: ConsumerBoundary,
    *,
    source_scan: int,
    projection_at: Callable[[int], ScanRungWriteProjection | None],
) -> bool:
    """Whether replay evaluated the exact consumer occurrence named by the stop.

    This is deliberately weaker than :func:`consumer_boundary_reached`. A
    correction may change or remove the historical producer value; that is an
    observed result of the retry, not evidence that execution stopped short.
    The relocatable consumer selector still fails closed when its call or rung
    was not executed.
    """

    consumer_scan = source_scan + boundary.consumer_scan_offset
    projection = projection_at(consumer_scan)
    if projection is None:
        return False
    consumed = resolve_occurrence_selector(projection, boundary.consumer_selector)
    return isinstance(consumed, RungRead)


def required_shape(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
) -> tuple[tuple[str, Any], ...]:
    """Adjustable policy for the selected producer's consumer-local shape."""

    if len(path) < 2:
        return ()
    producer = path[-1]
    consumer_node_index = _consumer_for_producer(path, pdg)
    if consumer_node_index is None:
        return ()
    node = pdg.rung_nodes[consumer_node_index]
    if hasattr(node, "subroutine") and hasattr(node, "rung_index"):
        consumer = next(
            step
            for step in reversed(path[:-1])
            if (
                getattr(pdg.rung_nodes[step.node_index], "subroutine", object()),
                getattr(pdg.rung_nodes[step.node_index], "rung_index", object()),
            )
            == (node.subroutine, node.rung_index)
        )
    else:
        consumer = next(
            step for step in reversed(path[:-1]) if step.node_index == consumer_node_index
        )
    read_tags = node.condition_reads | node.guard_reads | node.data_reads
    candidates = [pair for pair in consumer.local_requirements if pair[0] in read_tags]
    effect_pair = (producer.tag, producer.value)
    if producer.tag in read_tags and effect_pair not in candidates:
        candidates.insert(0, effect_pair)
    # Occurrence order is part of the contract.  In particular, two reads of
    # the same tag/value are two handoffs, not one set member.
    return tuple(candidates)


def _consumer_for_producer(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
) -> int | None:
    """Select the nearest path ancestor which actually reads the effect.

    A nested branch can contribute another writer step without consuming its
    child's produced tag.  Adjacency in the trace path is therefore not a
    producer/consumer proof; the PDG read footprint is.
    """

    if len(path) < 2:
        return None
    producer = path[-1]
    for candidate in reversed(path[:-1]):
        node = pdg.rung_nodes[candidate.node_index]
        reads = node.condition_reads | node.guard_reads | node.data_reads
        # A branch node's structural footprint can include conditions executed
        # by its parent run.  Prefer the outermost same-rung ancestor which
        # reads the effect; that is the dynamic occurrence which observes the
        # inherited parent condition.  The branch's own static read set need
        # not repeat that parent read, so resolve ancestors before deciding the
        # candidate does not consume the effect. Keep the branch only when no
        # ancestor owns that read.
        branch_path = getattr(node, "branch_path", ())
        ancestors = tuple(
            (index, owner)
            for index, owner in enumerate(pdg.rung_nodes)
            if hasattr(node, "subroutine")
            and hasattr(node, "rung_index")
            and getattr(owner, "subroutine", object()) == node.subroutine
            and getattr(owner, "rung_index", object()) == node.rung_index
            and len(getattr(owner, "branch_path", ())) < len(branch_path)
            and branch_path[: len(getattr(owner, "branch_path", ()))]
            == getattr(owner, "branch_path", ())
            and producer.tag in (owner.condition_reads | owner.guard_reads | owner.data_reads)
        )
        if ancestors:
            return min(ancestors, key=lambda item: len(item[1].branch_path))[0]
        if producer.tag not in reads:
            continue
        return candidate.node_index
    return None


def expectation_from_writer(
    pdg: ProgramGraph,
    program: Any,
    *,
    writer_node: int,
    tag: str,
    value: Any,
    consumer_node: int | None = None,
    required_shape: tuple[tuple[str, Any], ...] = (),
    boundary: tuple[str, Any] | None = None,
    terminal_target: bool = False,
) -> EffectExpectation | None:
    """Mint an expectation from an already-selected static writer receipt.

    This adapter is intentionally narrower than trace lowering: route,
    program-step, awaited, and learned-edge readers must name their own writer
    rather than borrowing the first trace detail with the same physical pair.
    """

    rung_nodes = getattr(pdg, "rung_nodes", ())
    if writer_node < 0 or writer_node >= len(rung_nodes):
        return None
    producer_node = rung_nodes[writer_node]
    if tag not in producer_node.writes:
        # One receipt binds an exact static producer to its own effect. Route
        # destinations and request writers are distinct pipeline producers.
        return None
    consumer = pdg.rung_nodes[consumer_node] if consumer_node is not None else None
    producer_rung = resolve_rung(program, producer_node)
    consumer_rung = resolve_rung(program, consumer) if consumer is not None else None
    if producer_rung is None or (consumer is not None and consumer_rung is None):
        return None
    return EffectExpectation(
        (
            EffectObligation(
                tag=tag,
                value=value,
                producer=_writer_address(pdg, writer_node),
                consumer=(
                    _writer_address(pdg, consumer_node) if consumer_node is not None else None
                ),
                required_shape=required_shape,
                boundary=boundary,
                terminal_target=terminal_target,
                producer_rung=producer_rung,
                consumer_rung=consumer_rung,
            ),
        )
    )


def exact_last_landing_write(
    projections: Iterable[ScanRungWriteProjection],
    *,
    after: RungRead | RungWrite | EffectOccurrenceSnapshot | None,
    tag: str,
    target_value: Any,
    landing_value: Any,
) -> tuple[ScanRungWriteProjection, RungWrite] | None:
    """Return the final exact later write which owns the window landing.

    Ordered handoff observation deliberately names the first displacement.
    Terminal recovery has a different question: which later occurrence left
    the tag at the scan-exit value? Peeling that final writer is exact and
    deterministic; missing projection identity fails closed. ``after=None``
    makes the supplied projection window itself the exclusive lower bound.
    """

    candidates = tuple(
        (projection, write)
        for projection in projections
        for write in projection.writes
        if (after is None or (write.scan_id, write.ordinal) > (after.scan_id, after.ordinal))
        and write.run.enabled
        and write.transition.tag_name == tag
        and not _values_match(write.transition.to_value, target_value)
        and _values_match(write.transition.to_value, landing_value)
    )
    return candidates[-1] if candidates else None


def exact_first_departure_write(
    projections: Iterable[ScanRungWriteProjection],
    *,
    after: RungRead | RungWrite | EffectOccurrenceSnapshot,
    tag: str,
    tenure_value: Any,
) -> tuple[ScanRungWriteProjection, RungWrite] | None:
    """Return the first exact write which ends a consumed value's tenure.

    Once an outer consumer has read the selected write, that handoff is
    fulfilled.  A later channel loss is a new occurrence-level problem whose
    causal boundary is the first transition away from the consumed value, not
    whichever later writer happens to own the final macro-state landing.
    """

    candidates = tuple(
        (projection, write)
        for projection in projections
        for write in projection.writes
        if (write.scan_id, write.ordinal) > (after.scan_id, after.ordinal)
        and write.run.enabled
        and write.transition.tag_name == tag
        and _values_match(write.transition.from_value, tenure_value)
        and not _values_match(write.transition.to_value, tenure_value)
    )
    return candidates[0] if candidates else None


def promote_terminal_target_observation(
    observations: Iterable[EffectObservation],
    *,
    window_entry_value: Any,
    final_landing_value: Any,
) -> EffectObservation | None:
    """Promote one appeared-and-displaced global target, never its absence.

    The current frontier selected the static producer before execution. This
    adapter grants it terminal authority only after exactly one dynamic target
    occurrence appears and an exact same-scan projection proves the final
    landing writer. Multiple appearances or a folded/missing projection are
    ambiguous and therefore produce no obligation failure.
    """

    if not _values_match(window_entry_value, final_landing_value):
        return None
    return _promote_displaced_terminal_target(
        observations,
        final_landing_value=final_landing_value,
    )


def promote_certified_prefix_target_observation(
    observations: Iterable[EffectObservation],
    *,
    final_landing_value: Any,
) -> EffectObservation | None:
    """Promote non-zero terminal loss after an adjacent certified checkpoint.

    The caller owns either the typed ProgramStep/checkpoint proof or an exact
    local-retry source and full execution window. This adapter owns exactly the
    same appeared occurrence, projection, and final landing-writer checks as
    zero-net promotion without pretending the execution window began and ended
    at the same value.
    """

    return _promote_displaced_terminal_target(
        observations,
        final_landing_value=final_landing_value,
    )


def promote_route_landing_observations(
    observations: Iterable[EffectObservation],
    projections: Iterable[ScanRungWriteProjection],
    *,
    final_landing: Mapping[str, Any],
) -> tuple[EffectObservation, ...]:
    """Prefer the exact off-route landing writer to an earlier stalled handoff.

    A charted producer can appear, fail to reach its selected consumer, and
    then be replaced later in the same retained window.  For an ordinary
    handoff receipt, ``STRANDED`` correctly explains the first local failure.
    A route-*landing* receipt has the stronger question: what left the channel
    off the selected route at the retained tip?  When an exact later writer
    owns that final value, report that displacement so intrascan can invert
    the writer which actually lost the route.

    Missing or ambiguous landing evidence leaves the original observation
    unchanged.  This adapter never promotes absence and never grants terminal
    target authority.
    """

    projection_tuple = tuple(projections)
    promoted: list[EffectObservation] = []
    for observation in observations:
        producer = observation.appeared
        obligation = observation.obligation
        if producer is None or observation.disposition in {
            "SURVIVED",
            "OVERWRITTEN",
            "DISPLACED",
        }:
            promoted.append(observation)
            continue
        landing_value = final_landing.get(obligation.tag)
        if _values_match(landing_value, obligation.value):
            promoted.append(observation)
            continue
        landing = exact_last_landing_write(
            projection_tuple,
            after=producer,
            tag=obligation.tag,
            target_value=obligation.value,
            landing_value=landing_value,
        )
        if landing is None:
            promoted.append(observation)
            continue
        landing_projection, displacement = landing
        promoted.append(
            replace(
                observation,
                disposition="OVERWRITTEN",
                displacement=displacement,
                observed_reads=landing_projection.enabling_reads_observed_by_write(displacement),
                displacement_enabling_reads=(
                    landing_projection.enabling_read_closure_observed_by_write(displacement)
                ),
                detail="exact later writer owns the off-route retained landing",
                execution_projection=landing_projection,
            )
        )
    return tuple(promoted)


def _promote_displaced_terminal_target(
    observations: Iterable[EffectObservation],
    *,
    final_landing_value: Any,
) -> EffectObservation | None:
    """Apply exact terminal occurrence/final-writer checks shared by promoters."""

    appeared = tuple(
        observation
        for observation in observations
        if observation.obligation.terminal_target and observation.appeared is not None
    )
    if len(appeared) != 1:
        return None
    observation = appeared[0]
    projection = observation.execution_projection
    producer = observation.appeared
    assert producer is not None
    if (
        projection is None
        or producer.scan_id != projection.scan_id
        or not any(write is producer for write in projection.writes)
    ):
        return None
    landing_value = projection.exit_tags.get(observation.obligation.tag)
    if not _values_match(landing_value, final_landing_value):
        return None
    if _values_match(landing_value, observation.obligation.value):
        return None
    landing = exact_last_landing_write(
        (projection,),
        after=producer,
        tag=observation.obligation.tag,
        target_value=observation.obligation.value,
        landing_value=landing_value,
    )
    if landing is None:
        return None
    landing_projection, displacement = landing
    return replace(
        observation,
        disposition="OVERWRITTEN",
        displacement=displacement,
        observed_reads=_terminal_landing_causal_reads(
            landing_projection,
            displacement,
            observation.obligation.tag,
        ),
        displacement_enabling_reads=(
            landing_projection.enabling_read_closure_observed_by_write(displacement)
        ),
        detail="global target appeared but the final same-scan writer replaced it",
        execution_projection=landing_projection,
    )


def _terminal_landing_causal_reads(
    projection: ScanRungWriteProjection,
    landing: RungWrite,
    tag: str,
) -> tuple[RungRead, ...]:
    """Expose the exact predecessor which enabled a nested final rollback.

    A subroutine copy can own the scan landing while its own run has no reads;
    the caller guard observes the earlier same-tag displacement. Follow only
    that exact dynamic source once, then include the predecessor writer's
    enabling reads so existing Advance inversion can reach its completion bit.
    """

    direct = list(projection.enabling_reads_observed_by_write(landing))
    parent = projection.parent_run(landing.run)
    has_displaced_tag_source = any(
        read.occurrence.name == tag and projection.transition_observed_by_read(read) is not None
        for read in direct
    )
    if not has_displaced_tag_source and parent is not None:
        direct.extend(
            read for read in projection.reads_for_run(parent) if read.ordinal < landing.ordinal
        )

    result = list(direct)
    predecessor_writes: list[RungWrite] = []
    for read in direct:
        if read.occurrence.name != tag:
            continue
        transition = projection.transition_observed_by_read(read)
        if transition is None or transition.occurrence_ordinal is None:
            continue
        matches = tuple(
            write
            for write in projection.writes
            if write.ordinal == transition.occurrence_ordinal and write.transition.tag_name == tag
        )
        if len(matches) != 1:
            return ()
        predecessor = matches[0]
        if not any(current is predecessor for current in predecessor_writes):
            predecessor_writes.append(predecessor)
    if len(predecessor_writes) > 1:
        return ()
    if predecessor_writes:
        result.extend(projection.enabling_reads_observed_by_write(predecessor_writes[0]))
    return tuple(dict.fromkeys(result))


def expectation_from_selected_path(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
    program: Any,
    *,
    boundary: tuple[str, Any] | None,
    selected_pairs: tuple[tuple[str, Any], ...] = (),
    snapshot: Mapping[str, Any] | None = None,
    steerable: frozenset[str] = frozenset(),
    projected_consumer_tags: frozenset[str] = frozenset(),
    require_ready: bool = True,
) -> EffectExpectation | None:
    """Mint one final obligation while the exact selected path is available.

    ``require_ready=False`` is reserved for detached candidate drafting.  The
    caller must still prove the missing prerequisites in one exact projected
    scan before the expectation can become an executable Bearing.
    """

    if not path:
        return None
    producer = path[-1]
    consumer_node_index = _consumer_for_producer(path, pdg)
    projected_consumer = (
        consumer_node_index is None and len(path) > 1 and producer.tag in projected_consumer_tags
    )
    shape = required_shape(path, pdg)
    if projected_consumer:
        # A statically opaque consumer is resolved from its exact projected
        # read rather than from a terminal scan-exit shape.
        shape = ()
    producer_node = pdg.rung_nodes[producer.node_index]
    if producer.tag not in producer_node.writes:
        return None
    consumer_node = pdg.rung_nodes[consumer_node_index] if consumer_node_index is not None else None
    producer_rung = resolve_rung(program, producer_node)
    consumer_rung = resolve_rung(program, consumer_node) if consumer_node is not None else None
    if producer_rung is None or (consumer_node is not None and consumer_rung is None):
        return None
    # Expectations are execution receipts, not forecasts for the rest of a
    # target trace.  Mint one only when this selected artifact makes the exact
    # producer runnable and its selected consumer is due now.  Otherwise a
    # fresh Orientation owns the later stage.  Returning a terminal producer
    # promise here would still reject ordinary prerequisite staging as ABSENT.
    if snapshot is not None and require_ready:
        selected_state = dict(snapshot)
        selected_state.update(selected_pairs)
        producer_ready = all(
            _values_match(selected_state.get(tag), value)
            for tag, value in producer.local_requirements
        )
        consumer_ready = consumer_node_index is None or all(
            tag == producer.tag
            and _values_match(value, producer.value)
            or _values_match(selected_state.get(tag), value)
            for tag, value in shape
        )
        if consumer_ready and consumer_rung is not None:
            consumer_state = dict(selected_state)
            consumer_state[producer.tag] = producer.value
            try:
                consumer_ready = bool(
                    consumer_rung._evaluate_conditions(
                        cast("ConditionView", _SnapshotView(consumer_state, {}))
                    )
                )
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
                # The retained scalar shape remains the conservative fallback
                # when a custom condition cannot be evaluated prospectively.
                pass
        if not producer_ready or not consumer_ready:
            return None
    obligation = EffectObligation(
        tag=producer.tag,
        value=producer.value,
        producer=_writer_address(pdg, producer.node_index),
        consumer=(
            _writer_address(pdg, consumer_node_index) if consumer_node_index is not None else None
        ),
        required_shape=shape,
        boundary=boundary,
        projected_consumer=projected_consumer,
        producer_rung=producer_rung,
        consumer_rung=consumer_rung,
    )
    return EffectExpectation((obligation,))


def observe_expectation(
    expectation: EffectExpectation,
    projections: Iterable[ScanRungWriteProjection],
) -> tuple[EffectObservation, ...]:
    """Observe every exact appeared occurrence, or one ordinary ``ABSENT``.

    Bootstrap intentionally does not call this adapter: its designations are
    not promises and continue to intersect with appeared writes only.
    """

    projection_tuple = tuple(projections)
    result: list[EffectObservation] = []
    for obligation in expectation.obligations:
        if obligation.polarity is EffectPolarity.PREVENT:
            result.append(
                EffectObservation(
                    obligation,
                    "UNKNOWN",
                    detail="prevention requires one complete intrascan projection",
                    execution_projection=(
                        projection_tuple[0] if len(projection_tuple) == 1 else None
                    ),
                )
            )
            continue
        appeared: list[tuple[ScanRungWriteProjection, OrderedEffectObservation]] = []
        for projection in projection_tuple:
            appeared.extend(
                (projection, observation)
                for observation in projection.observe_appeared_handoff(
                    obligation.tag,
                    obligation.value,
                    producer_rung=obligation.producer_rung,
                    consumer_rung=obligation.consumer_rung,
                    producer_address=obligation.producer,
                    consumer_address=obligation.consumer,
                    required_shape=obligation.required_shape,
                )
            )
        if not appeared:
            result.append(
                EffectObservation(
                    obligation,
                    "ABSENT",
                    detail="selected producer did not write the expected value",
                    execution_projection=(
                        projection_tuple[0] if len(projection_tuple) == 1 else None
                    ),
                )
            )
            continue
        for projection, observation in appeared:
            if obligation.projected_consumer:
                result.append(_observe_projected_consumer(obligation, observation, projection))
                continue
            later_writes = tuple(
                (later, write)
                for later in projection_tuple
                if later.scan_id > projection.scan_id
                for write in later.writes
                if write.run.enabled and write.transition.tag_name == obligation.tag
            )
            if (
                obligation.consumer_rung is None
                and observation.disposition == "SURVIVED"
                and later_writes
            ):
                later_projection, later_write = later_writes[0]
                result.append(
                    EffectObservation(
                        obligation=obligation,
                        disposition="OVERWRITTEN",
                        appeared=observation.appeared,
                        displacement=later_write,
                        observed_reads=later_projection.enabling_reads_observed_by_write(
                            later_write
                        ),
                        detail="a later corridor write replaced the terminal effect",
                        execution_projection=later_projection,
                    )
                )
                continue
            if observation.disposition == "STRANDED":
                preceding_reads = _consumer_reads_preceding_write(
                    obligation,
                    observation.appeared,
                    projection,
                )
                if len(preceding_reads) > 1:
                    result.append(
                        EffectObservation(
                            obligation,
                            "UNKNOWN",
                            appeared=observation.appeared,
                            observed_reads=preceding_reads,
                            detail="consumer occurrence before scan wrap is ambiguous",
                            execution_projection=projection,
                        )
                    )
                    continue
                adjacent_scan_is_observed = any(
                    candidate.scan_id == projection.scan_id + 1 for candidate in projection_tuple
                )
                if len(preceding_reads) == 1 or adjacent_scan_is_observed:
                    wrapped = _observe_wrapped_handoff(
                        obligation,
                        observation.appeared,
                        projection,
                        projection_tuple,
                    )
                    if wrapped is not None:
                        result.append(wrapped)
                        continue
            result.append(_from_ordered(obligation, observation, projection))
    return tuple(result)


def _observe_projected_consumer(
    obligation: EffectObligation,
    observation: OrderedEffectObservation,
    projection: ScanRungWriteProjection,
) -> EffectObservation:
    """Resolve one statically opaque consumer from exact occurrence sources.

    This is deliberately stricter than choosing the first same-tag read.  The
    read must carry the exact selected write as its source and occur before the
    first displacement.  More than one such read leaves the intended consumer
    ambiguous and therefore fails closed.
    """

    appeared = observation.appeared
    ceiling = observation.displacement.ordinal if observation.displacement is not None else None
    sourced = tuple(
        read
        for read in projection.reads
        if read.occurrence.name == obligation.tag
        and read.ordinal > appeared.ordinal
        and (ceiling is None or read.ordinal < ceiling)
        and (
            (transition := projection.transition_observed_by_read(read)) is not None
            and transition.occurrence_ordinal == appeared.ordinal
            and transition.tag_name == obligation.tag
        )
    )
    if len(sourced) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            displacement=observation.displacement,
            observed_reads=sourced,
            detail=(
                "projected consumer read is unavailable"
                if not sourced
                else "projected consumer read is ambiguous"
            ),
            execution_projection=projection,
        )
    consumer_read = sourced[0]
    consumer_shape = projection.observed_shape(consumer_read)
    if not consumer_read.run.enabled:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            consumer_read=consumer_read,
            observed_reads=consumer_shape,
            detail="projected consumer read the effect but its run was disabled",
            execution_projection=projection,
        )
    return EffectObservation(
        obligation,
        "SURVIVED",
        appeared=appeared,
        consumer_read=consumer_read,
        observed_reads=consumer_shape,
        detail="exact projection resolved the selected consumer",
        execution_projection=projection,
    )


def _selector_runs(
    projection: ScanRungWriteProjection,
    obligation: EffectObligation,
    selector: EffectOccurrenceSelector,
) -> tuple[RungRun, ...]:
    return tuple(
        run
        for run in projection.runs
        if _run_static_address(projection, run) == selector.static_address
        and run.kind == selector.execution_kind
        and run.caller_rung == selector.caller_rung
        and run.call_stack == selector.call_stack
        and run.depth == selector.depth
        and projection._call_invocation_by_run.get(id(run)) == selector.call_invocation
        and (obligation.producer_rung is None or run.rung is obligation.producer_rung)
    )


def _observe_prevention(
    obligation: EffectObligation,
    projection: ScanRungWriteProjection,
) -> EffectObservation:
    """Observe one forbidden exact write in one complete assertion projection."""

    selector = obligation.occurrence_selector
    if selector is None:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            detail="prevention obligation has no exact occurrence selector",
            execution_projection=projection,
        )
    if (
        selector.kind != "write"
        or selector.tag != obligation.tag
        or selector.static_address != obligation.producer
        or not selector.instruction_path
        or selector.access_index < 0
    ):
        return EffectObservation(
            obligation,
            "UNKNOWN",
            detail="prevention selector does not match the forbidden write contract",
            execution_projection=projection,
        )
    runs = _selector_runs(projection, obligation, selector)
    if len(runs) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            detail="prevention writer occurrence is unavailable or ambiguous",
            execution_projection=projection,
        )
    run = runs[0]
    writes = tuple(
        write
        for write in projection.writes_for_run(run)
        if write.instruction is not None
        and _instruction_path(run, write.instruction) == selector.instruction_path
        and write.transition.tag_name == selector.tag
    )
    if selector.access_index >= len(writes):
        return EffectObservation(
            obligation,
            "PREVENTED",
            detail="selected forbidden write did not occur",
            execution_projection=projection,
        )
    write = writes[selector.access_index]
    if not _values_match(write.transition.to_value, obligation.value):
        return EffectObservation(
            obligation,
            "PREVENTED",
            detail="selected instruction did not write the forbidden value",
            execution_projection=projection,
        )
    return EffectObservation(
        obligation,
        "FIRED",
        appeared=write,
        observed_reads=projection.enabling_reads_observed_by_write(write),
        detail="selected forbidden write occurred",
        execution_projection=projection,
    )


def observe_intrascan_expectation(
    expectation: EffectExpectation,
    projection: ScanRungWriteProjection,
) -> tuple[EffectObservation, ...]:
    """Observe a polarized conjunction on one complete exact projection.

    Positive obligations retain the established appeared-handoff semantics.
    Prevention is intentionally unavailable through ``observe_expectation``:
    absence over an arbitrary iterable of projections is not evidence that the
    execution window was complete.
    """

    result: list[EffectObservation] = []
    for obligation in expectation.obligations:
        if obligation.polarity is EffectPolarity.PRODUCE:
            result.extend(observe_expectation(EffectExpectation((obligation,)), (projection,)))
        else:
            result.append(_observe_prevention(obligation, projection))
    return tuple(result)


def fulfilled_expectation_observations(
    expectation: EffectExpectation,
    observations: Iterable[EffectObservation],
) -> tuple[EffectObservation, ...]:
    """One exact surviving occurrence for every selected obligation.

    A producer can execute again during pulse settlement. Once an occurrence
    has reached the selected consumer, a later repeat cannot retroactively
    invalidate that already fulfilled obligation; any later consequence is
    owned by the committed receipt and progress monitor.
    """

    observed = tuple(observations)
    fulfilled: list[EffectObservation] = []
    for obligation in expectation.obligations:
        survived = next(
            (
                item
                for item in observed
                if item.obligation is obligation and item.disposition == "SURVIVED"
            ),
            None,
        )
        if survived is None:
            return ()
        fulfilled.append(survived)
    return tuple(fulfilled)


def effect_reached_consumer(observation: EffectObservation) -> bool:
    """Whether one selected value completed its exact handoff.

    Program cleanup may replace a transient value after its selected consumer
    has read it.  The displacement remains useful execution evidence, but it
    cannot retroactively turn that completed handoff into a failed one.
    """

    if observation.disposition == "SURVIVED":
        return True
    appeared = observation.appeared
    consumer = observation.consumer_read
    displacement = observation.displacement
    return bool(
        observation.disposition == "OVERWRITTEN"
        and appeared is not None
        and consumer is not None
        and displacement is not None
        and appeared.scan_id == consumer.scan_id == displacement.scan_id
        and appeared.ordinal < consumer.ordinal < displacement.ordinal
    )


def _consumer_reads_preceding_write(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
) -> tuple[RungRead, ...]:
    """Exact consumer reads which make this handoff due after a cycle wrap."""

    if obligation.consumer_rung is None:
        return ()
    return tuple(
        read
        for run in projection.runs
        if run.rung is obligation.consumer_rung
        and projection._runs_share_selected_transaction(
            appeared.run,
            run,
            obligation.producer,
            obligation.consumer,
        )
        for read in projection.reads_for_run(run)
        if read.occurrence.name == obligation.tag and read.ordinal < appeared.ordinal
    )


def _observe_wrapped_handoff(
    obligation: EffectObligation,
    appeared: RungWrite,
    producer_projection: ScanRungWriteProjection,
    projections: tuple[ScanRungWriteProjection, ...],
) -> EffectObservation | None:
    """Observe the next exact consumer occurrence after scan order wraps.

    Cross-scan entry reads intentionally carry the executor's ``entry`` source
    marker rather than a previous scan's write object. Continuity is therefore
    proved from adjacent exact projections: the selected write wins the
    producer scan, the value is unchanged at the next entry, and no write
    intervenes before the selected consumer reads it.
    """

    next_projection = next(
        (
            candidate
            for candidate in projections
            if candidate.scan_id == producer_projection.scan_id + 1
        ),
        None,
    )
    if next_projection is None:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="consumer is due after scan wrap but the adjacent scan is unobserved",
            execution_projection=producer_projection,
        )
    if (
        producer_projection.final_write(obligation.tag, obligation.value) is not appeared
        or producer_projection.exit_tags.get(obligation.tag) != obligation.value
        or next_projection.entry_tags.get(obligation.tag) != obligation.value
    ):
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="cross-scan handoff continuity is not exact",
            execution_projection=next_projection,
        )

    consumer_runs = tuple(
        run
        for run in next_projection.runs
        if run.rung is obligation.consumer_rung
        and _wrapped_runs_share_selected_transaction(
            obligation,
            appeared,
            next_projection,
            run,
        )
    )
    if len(consumer_runs) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="selected adjacent-scan consumer occurrence is unavailable or ambiguous",
            execution_projection=next_projection,
        )

    consumer_run = consumer_runs[0]
    reads = next_projection.reads_for_run(consumer_run)
    effect_read = next(
        (
            read
            for read in reads
            if read.occurrence.name == obligation.tag and read.occurrence.value == obligation.value
        ),
        None,
    )
    consumer_boundary = (
        effect_read.ordinal
        if effect_read is not None
        else max((read.ordinal for read in reads), default=-1) + 1
    )
    displacement = next(
        (
            write
            for write in next_projection.writes
            if write.run.enabled
            and write.transition.tag_name == obligation.tag
            and write.ordinal < consumer_boundary
        ),
        None,
    )
    if displacement is not None:
        return EffectObservation(
            obligation,
            "OVERWRITTEN",
            appeared=appeared,
            consumer_read=effect_read,
            displacement=displacement,
            observed_reads=next_projection.enabling_reads_observed_by_write(displacement),
            detail="an adjacent-scan write replaced the pending handoff",
            execution_projection=next_projection,
        )
    if effect_read is not None and effect_read.occurrence.source != "entry":
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=next_projection.reads_for_run(consumer_run),
            detail="adjacent-scan consumer read lacks exact entry continuity",
            execution_projection=next_projection,
        )
    if effect_read is None:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            observed_reads=reads,
            detail="adjacent-scan consumer did not read the pending handoff",
            execution_projection=next_projection,
        )

    next_ordinal = -1
    matched: list[RungRead] = []
    for required_tag, required_value in obligation.required_shape:
        observed = next(
            (
                read
                for read in reads
                if read.occurrence.name == required_tag and read.ordinal > next_ordinal
            ),
            None,
        )
        if observed is None:
            return EffectObservation(
                obligation,
                "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                observed_reads=reads,
                detail=f"required consumer read {required_tag!r} did not occur",
                execution_projection=next_projection,
            )
        matched.append(observed)
        next_ordinal = observed.ordinal
        if observed.occurrence.value != required_value:
            return EffectObservation(
                obligation,
                "STRANDED" if not effect_read.run.enabled else "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                displaced_read=observed,
                observed_reads=reads,
                detail="adjacent-scan consumer required shape did not hold",
                execution_projection=next_projection,
            )
    if not effect_read.run.enabled:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=reads,
            detail="adjacent-scan consumer read the effect but its guard was false",
            execution_projection=next_projection,
        )
    return EffectObservation(
        obligation,
        "SURVIVED",
        appeared=appeared,
        consumer_read=effect_read,
        observed_reads=tuple(matched),
        execution_projection=next_projection,
    )


def _wrapped_runs_share_selected_transaction(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
    consumer_run: Any,
) -> bool:
    """Match dynamic call identity across adjacent scan invocations."""

    producer_sub, producer_rung, producer_branch = obligation.producer
    assert obligation.consumer is not None
    consumer_sub, consumer_rung, consumer_branch = obligation.consumer
    same_subroutine = producer_sub is not None and producer_sub == consumer_sub
    same_branched_rung = (
        producer_sub == consumer_sub
        and producer_rung == consumer_rung
        and bool(producer_branch or consumer_branch)
    )
    if same_subroutine or same_branched_rung:
        if (
            appeared.run.caller_rung != consumer_run.caller_rung
            or appeared.run.call_stack != consumer_run.call_stack
        ):
            return False
    if same_subroutine:
        return appeared.call_invocation == projection._call_invocation_by_run.get(id(consumer_run))
    return True


@dataclass(frozen=True)
class _CertifiedOutProducer:
    authoritative: int | RungId
    shadow_root: int | None


def _certified_out_producer(
    obligation: EffectObligation,
    fork: Any,
    owners: tuple[Any, ...],
) -> _CertifiedOutProducer | None:
    """Return one value-aware OUT producer only when call ownership is exact."""

    subroutine, rung_index, branch_path = obligation.producer
    if branch_path or obligation.value is not True:
        return None
    program = fork._program
    if program is None:
        return None

    if subroutine is None:
        if not 0 <= rung_index < len(program.rungs):
            return None
        rung = program.rungs[rung_index]
    else:
        subroutine_rungs = program.subroutines.get(subroutine)
        if subroutine_rungs is None or not 0 <= rung_index < len(subroutine_rungs):
            return None
        rung = subroutine_rungs[rung_index]
    if rung is not obligation.producer_rung or rung._branches:
        return None
    if any(isinstance(item, CallInstruction | ForLoopInstruction) for item in rung._instructions):
        return None

    matching_instructions: list[Any] = []
    for instruction in rung._instructions:
        for target in instruction_write_targets(instruction):
            names = static_write_target_names(target)
            if not names:
                return None
            if obligation.tag in names:
                matching_instructions.append(instruction)
    if len(matching_instructions) != 1:
        return None
    producer = matching_instructions[0]
    if not isinstance(producer, OutInstruction) or producer._oneshot:
        return None

    if subroutine is None:
        if any(
            not owner.rung_firing_timelines.value_is_known(rung_index, obligation.tag)
            for owner in owners
        ):
            return None
        return _CertifiedOutProducer(rung_index, None)

    pdg = fork._ensure_pdg()
    matching_nodes = tuple(
        (node_index, node)
        for node_index, node in enumerate(pdg.rung_nodes)
        if (node.subroutine, node.rung_index, node.branch_path) == obligation.producer
    )
    if len(matching_nodes) != 1:
        return None
    producer_node_index, producer_node = matching_nodes[0]
    if (
        producer_node_index not in pdg.writers_of.get(obligation.tag, frozenset())
        or obligation.tag not in producer_node.ote_writes
    ):
        return None

    call_nodes = tuple(
        node
        for node in pdg.rung_nodes
        for called_subroutine in node.calls
        if called_subroutine == subroutine
    )
    if len(call_nodes) != 1:
        return None
    caller_node = call_nodes[0]
    if caller_node.subroutine is not None or caller_node.branch_path:
        return None
    caller_index = caller_node.rung_index
    if not 0 <= caller_index < len(program.rungs):
        return None
    caller_rung = program.rungs[caller_index]
    if caller_rung._conditions or caller_rung._branches or len(caller_rung._instructions) != 1:
        return None
    call_instruction = caller_rung._instructions[0]
    if (
        not isinstance(call_instruction, CallInstruction)
        or call_instruction.subroutine_name != subroutine
    ):
        return None

    node_key = RungId(subroutine, rung_index)
    if any(
        not owner.node_firing_timelines.value_is_known(node_key, obligation.tag)
        or not owner.rung_firing_timelines.value_is_known(caller_index, obligation.tag)
        for owner in owners
    ):
        return None
    return _CertifiedOutProducer(node_key, caller_index)


def _effect_projection_scan_ids(
    expectation: EffectExpectation,
    fork: Any,
    exact_scan_ids: Iterable[int],
    *,
    mandatory_scan_ids: Iterable[int],
) -> tuple[int, ...]:
    """Prefilter reconstruction without weakening exact scan ownership.

    Every carried ID is checked. Any missing owner or incomplete historical
    retention certificate disables pruning for the whole window.
    """

    exact = tuple(sorted(set(exact_scan_ids)))
    if not exact:
        return ()
    owners = tuple(fork._causal_lineage.owner_at(scan_id) for scan_id in exact)
    if any(owner is None for owner in owners):
        return exact
    exact_owners = tuple(owner for owner in owners if owner is not None)
    exact_set = set(exact)
    selected = {scan_id for scan_id in mandatory_scan_ids if scan_id in exact}
    for obligation in expectation.obligations:
        retention_complete = all(
            owner.firing_retained_tags is None or obligation.tag in owner.firing_retained_tags
            for owner in exact_owners
        )
        if not retention_complete:
            selected.update(exact)
            continue
        certified = _certified_out_producer(
            obligation,
            fork,
            exact_owners,
        )
        if certified is None:
            obligation_scans = {
                scan_id
                for scan_id, owner in zip(exact, exact_owners, strict=True)
                if owner.rung_firing_timelines.any_wrote_on(obligation.tag, scan_id)
                or owner.node_firing_timelines.any_wrote_on(obligation.tag, scan_id)
            }
            selected.update(obligation_scans)
            if obligation.consumer is not None:
                selected.update(
                    scan_id + 1 for scan_id in obligation_scans if scan_id + 1 in exact_set
                )
            continue

        appeared = False
        producer_scans: set[int] = set()
        for scan_id, owner in zip(exact, exact_owners, strict=True):
            if isinstance(certified.authoritative, int):
                authoritative = owner.rung_firing_timelines
            else:
                authoritative = owner.node_firing_timelines
            produced = (
                authoritative.value_at(
                    certified.authoritative,
                    obligation.tag,
                    scan_id,
                )
                is True
            )
            varied = authoritative.varied_on(
                certified.authoritative,
                obligation.tag,
                scan_id,
            )
            if produced:
                appeared = True
                producer_scans.add(scan_id)
                selected.add(scan_id)
                continue
            if varied:
                # Final value alone cannot say whether the promised value
                # appeared transiently; selected replay resolves the order.
                selected.add(scan_id)
                continue
            if appeared and (
                owner.rung_firing_timelines.any_wrote_on(
                    obligation.tag,
                    scan_id,
                    excluding=(
                        certified.authoritative
                        if isinstance(certified.authoritative, int)
                        else certified.shadow_root
                    ),
                )
                or owner.node_firing_timelines.any_wrote_on(
                    obligation.tag,
                    scan_id,
                    excluding=(
                        certified.authoritative
                        if isinstance(certified.authoritative, RungId)
                        else None
                    ),
                )
            ):
                selected.add(scan_id)
        if obligation.consumer is not None:
            selected.update(scan_id + 1 for scan_id in producer_scans if scan_id + 1 in exact_set)
    return tuple(sorted(selected))


def terminal_target_replay_scan_ids(
    expectation: EffectExpectation,
    fork: Any,
    exact_scan_ids: Iterable[int],
) -> tuple[int, ...]:
    """Nominate exact scans that may contain a zero-net target appearance.

    Cross-scan target arrival is a coast trigger and needs no reconstruction.
    This sparse index serves only the remaining case: a writer attempted the
    target value during one kernel scan but the scan landed elsewhere.  A
    matching final attempted value nominates the scan directly; ``varied`` is
    the compact evidence for one writer attempting unequal values (including
    target-then-reset) in that same scan.

    If causal retention cannot answer the question, fail closed by returning
    the full exact set.  Soundness is preserved while the normal retained-tag
    path avoids replaying constant non-target writes on every timer scan.
    """

    exact = tuple(sorted(set(exact_scan_ids)))
    if not exact:
        return ()
    owners = tuple(fork._causal_lineage.owner_at(scan_id) for scan_id in exact)
    if any(owner is None for owner in owners):
        return exact
    exact_owners = tuple(owner for owner in owners if owner is not None)
    selected: set[int] = set()
    for obligation in expectation.obligations:
        if not obligation.terminal_target:
            continue
        retention_complete = all(
            owner.firing_retained_tags is None or obligation.tag in owner.firing_retained_tags
            for owner in exact_owners
        )
        if not retention_complete:
            selected.update(exact)
            continue
        subroutine, rung_index, _branch_path = obligation.producer
        if subroutine is None:
            timeline_name = "rung_firing_timelines"
            producer: int | RungId = rung_index
        else:
            timeline_name = "node_firing_timelines"
            producer = RungId(subroutine, rung_index)
        if any(
            not getattr(owner, timeline_name).value_is_known(producer, obligation.tag)
            for owner in exact_owners
        ):
            selected.update(exact)
            continue
        for scan_id, owner in zip(exact, exact_owners, strict=True):
            timelines = getattr(owner, timeline_name)
            if _values_match(
                timelines.value_at(producer, obligation.tag, scan_id),
                obligation.value,
            ) or timelines.varied_on(producer, obligation.tag, scan_id):
                selected.add(scan_id)
    return tuple(sorted(selected))


def observe_execution_window(
    expectation: EffectExpectation | None,
    fork: Any,
    *,
    scan_before: int,
    kernel_scan_ids: Iterable[int],
    action_scan: int | None = None,
    coast_receipt: Any = None,
    projection_at: Callable[[int], ScanRungWriteProjection | None] | None = None,
) -> tuple[EffectObservation, ...]:
    """Observe only exact scans owned by the executed act.

    Edge release is execution setup, not the selected producer occurrence. A
    matching release-scan write therefore cannot satisfy or erase the promise
    made by the assertion. Pulse/Batch observations begin at the assertion scan
    and include only later kernel scans explicitly retained by the execution
    session. Folded logical gaps remain unobserved rather than being replayed
    as a complete logical corridor.
    """

    if expectation is None:
        return ()
    project = projection_at or fork._replay_rung_write_projection_at
    if action_scan is not None:
        exact_scan_ids = {
            scan_id for scan_id in kernel_scan_ids if action_scan <= scan_id <= fork.state.scan_id
        }
        if action_scan not in exact_scan_ids:
            return _bind_execution_epoch(
                _unknown_observations(
                    expectation,
                    "assertion scan is absent from the exact kernel scan stream",
                ),
                fork,
                fallback_scan=action_scan,
            )
        selected_scan_ids = _effect_projection_scan_ids(
            expectation,
            fork,
            exact_scan_ids,
            mandatory_scan_ids=(action_scan,),
        )
        projections_by_scan = tuple((scan_id, project(scan_id)) for scan_id in selected_scan_ids)
        if any(projection is None for _scan_id, projection in projections_by_scan):
            return _bind_execution_epoch(
                _unknown_observations(
                    expectation,
                    "exact kernel scan projection is unavailable",
                ),
                fork,
                fallback_scan=action_scan,
            )
        projections = tuple(
            projection for _scan_id, projection in projections_by_scan if projection is not None
        )
        if projections:
            return _bind_execution_epoch(
                observe_expectation(expectation, projections),
                fork,
                fallback_scan=projections[-1].scan_id,
            )
        return _bind_execution_epoch(
            _unknown_observations(expectation, "assertion scan projection is unavailable"),
            fork,
            fallback_scan=action_scan,
        )

    landing_scan = coast_receipt.end_scan if coast_receipt is not None else fork.state.scan_id
    exact_scan_ids = {
        scan_id for scan_id in kernel_scan_ids if scan_before < scan_id <= fork.state.scan_id
    }
    mandatory_scan_ids = tuple(
        scan_id for scan_id in (landing_scan,) if scan_id in exact_scan_ids
    ) + ((min(exact_scan_ids),) if exact_scan_ids else ())
    selected_scan_ids = _effect_projection_scan_ids(
        expectation,
        fork,
        exact_scan_ids,
        mandatory_scan_ids=mandatory_scan_ids,
    )
    projections_by_scan = tuple((scan_id, project(scan_id)) for scan_id in selected_scan_ids)
    if any(projection is None for _scan_id, projection in projections_by_scan):
        return _bind_execution_epoch(
            _unknown_observations(
                expectation,
                "exact kernel scan projection is unavailable",
            ),
            fork,
            fallback_scan=landing_scan,
        )
    projections = tuple(
        projection for _scan_id, projection in projections_by_scan if projection is not None
    )
    complete_single_scan = (
        coast_receipt is not None
        and coast_receipt.logical_scans == 1
        and coast_receipt.skipped_scans == 0
        and len(projections) == 1
        and projections[0].scan_id == landing_scan
    )
    if complete_single_scan:
        return _bind_execution_epoch(
            observe_expectation(expectation, projections),
            fork,
            fallback_scan=landing_scan,
        )
    if not projections:
        return _bind_execution_epoch(
            _unknown_observations(
                expectation,
                "coast has no exact recorded effect scan",
            ),
            fork,
            fallback_scan=landing_scan,
        )

    results: list[EffectObservation] = []
    for obligation in expectation.obligations:
        local_expectation = EffectExpectation((obligation,))
        observations = observe_expectation(local_expectation, projections)
        for observation in observations:
            if observation.disposition in {"OVERWRITTEN", "DISPLACED"}:
                results.append(observation)
            elif observation.disposition == "SURVIVED" and (
                obligation.consumer is not None
                or (
                    observation.appeared is not None
                    and observation.appeared.scan_id == landing_scan
                )
            ):
                results.append(observation)
            else:
                results.append(
                    EffectObservation(
                        obligation,
                        "UNKNOWN",
                        appeared=observation.appeared,
                        consumer_read=observation.consumer_read,
                        displacement=observation.displacement,
                        displaced_read=observation.displaced_read,
                        observed_reads=observation.observed_reads,
                        displacement_enabling_reads=(observation.displacement_enabling_reads),
                        detail="coast corridor contains unobserved or folded scans",
                    )
                )
    return _bind_execution_epoch(tuple(results), fork, fallback_scan=landing_scan)


def _bind_execution_epoch(
    observations: tuple[EffectObservation, ...],
    fork: Any,
    *,
    fallback_scan: int,
) -> tuple[EffectObservation, ...]:
    """Attach the immutable Epoch owner of each exact observation.

    A scan number is not an execution identity after forks and replay.  Sealing
    through the observation gives receipts a stable epoch/query pair rather
    than the lineage's mutable live-query adapter.
    """

    observation_scans = tuple(
        tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        for observation in observations
    )
    latest_scan = max(
        (scan for scans in observation_scans for scan in scans),
        default=fallback_scan,
    )
    sealed = fork._causal_lineage.seal_through(latest_scan)

    result: list[EffectObservation] = []
    for observation, scans in zip(observations, observation_scans, strict=True):
        scan = max(scans, default=fallback_scan)
        owned = next(
            (
                (epoch, owner)
                for epoch, owner in sealed
                if epoch.first_scan <= scan <= epoch.last_scan
            ),
            None,
        )
        if owned is None:
            result.append(
                replace(
                    observation,
                    disposition="UNKNOWN",
                    detail=(f"{observation.detail}; " if observation.detail else "")
                    + "exact execution epoch is unavailable",
                )
            )
            continue
        epoch, owner = owned
        result.append(
            replace(
                observation,
                execution_epoch=epoch,
                execution_owner=owner,
            )
        )
    return tuple(result)


def _unknown_observations(
    expectation: EffectExpectation,
    detail: str,
) -> tuple[EffectObservation, ...]:
    return tuple(
        EffectObservation(obligation, "UNKNOWN", detail=detail)
        for obligation in expectation.obligations
    )


def _from_ordered(
    obligation: EffectObligation,
    observation: OrderedEffectObservation,
    projection: ScanRungWriteProjection,
) -> EffectObservation:
    return EffectObservation(
        obligation=obligation,
        disposition=observation.disposition,
        appeared=observation.appeared,
        consumer_read=observation.consumer_read,
        displacement=observation.displacement,
        displaced_read=observation.displaced_read,
        observed_reads=observation.observed_reads,
        displacement_enabling_reads=observation.displacement_enabling_reads,
        detail=observation.detail,
        execution_projection=projection,
    )


def obligation_snapshot(obligation: EffectObligation) -> EffectObligationSnapshot:
    return EffectObligationSnapshot(
        tag=obligation.tag,
        value=_detached(obligation.value),
        producer=obligation.producer,
        consumer=obligation.consumer,
        required_shape=tuple((tag, _detached(value)) for tag, value in obligation.required_shape),
        boundary=(
            (obligation.boundary[0], _detached(obligation.boundary[1]))
            if obligation.boundary is not None
            else None
        ),
        terminal_target=obligation.terminal_target,
        projected_consumer=obligation.projected_consumer,
    )


def expectation_snapshot(
    expectation: EffectExpectation | None,
) -> tuple[EffectObligationSnapshot, ...]:
    return (
        tuple(obligation_snapshot(obligation) for obligation in expectation.obligations)
        if expectation is not None
        else ()
    )


def occurrence_snapshot(read: RungRead | RungWrite) -> EffectOccurrenceSnapshot:
    """Detach one exact projection-owned occurrence for receipts/events."""

    if isinstance(read, RungWrite):
        return _write_snapshot(read)
    return EffectOccurrenceSnapshot(
        kind="read",
        ordinal=read.ordinal,
        scan_id=read.scan_id,
        run_order=read.run_order,
        call_invocation=read.call_invocation,
        rung=(read.rung_id.subroutine, read.rung_id.rung_index),
        execution_kind=read.run.kind,
        caller_rung=read.run.caller_rung,
        call_stack=read.run.call_stack,
        depth=read.run.depth,
        enabled=read.run.enabled,
        tag=read.occurrence.name,
        values=(_detached(read.occurrence.value),),
        branch_path=read.branch_path,
    )


def _write_snapshot(write: RungWrite) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind="write",
        ordinal=write.ordinal,
        scan_id=write.scan_id,
        run_order=write.run_order,
        call_invocation=write.call_invocation,
        rung=(write.rung_id.subroutine, write.rung_id.rung_index),
        execution_kind=write.run.kind,
        caller_rung=write.run.caller_rung,
        call_stack=write.run.call_stack,
        depth=write.run.depth,
        enabled=write.run.enabled,
        tag=write.transition.tag_name,
        values=(
            _detached(write.transition.from_value),
            _detached(write.transition.to_value),
        ),
        branch_path=write.branch_path,
    )
