"""Exact tag-access index over one observed interpreted scan.

The executor owns execution truth.  Its recursive :class:`RungRun` journal
retains every immediate read and write at the point it occurred, including
several writes to one tag inside one instruction and parent/child interleaving.
This module adds lookup indexes and causal ``Transition`` views without
reconstructing, grouping, or otherwise changing that execution order.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .models import Transition

if TYPE_CHECKING:
    from pyrung.core.context import RungId
    from pyrung.core.executor import (
        InstructionRun,
        ReadOccurrence,
        RungRun,
        WriteOccurrence,
    )
    from pyrung.core.history import History


_UNSET = object()


@dataclass(frozen=True)
class RungWrite:
    """One exact tag write owned by a dynamic rung/instruction run."""

    scan_id: int
    ordinal: int
    run_order: int
    call_invocation: int | None
    rung_id: RungId
    run: RungRun = field(compare=False, repr=False)
    instruction: InstructionRun | None = field(compare=False, repr=False)
    occurrence: WriteOccurrence = field(compare=False, repr=False)
    transition: Transition


@dataclass(frozen=True)
class RungRead:
    """One exact tag read owned by a dynamic rung/instruction run."""

    scan_id: int
    ordinal: int
    run_order: int
    call_invocation: int | None
    rung_id: RungId
    run: RungRun = field(compare=False, repr=False)
    instruction: InstructionRun | None = field(compare=False, repr=False)
    occurrence: ReadOccurrence = field(compare=False, repr=False)


EffectDisposition = Literal[
    "OVERWRITTEN",
    "STRANDED",
    "DISPLACED",
    "SURVIVED",
    "UNKNOWN",
]
StaticRungAddress = tuple[str | None, int, tuple[int, ...]]


@dataclass(frozen=True)
class OrderedEffectObservation:
    """Factual producer-to-consumer result for one exact appeared write.

    This record deliberately has no ``ABSENT`` arm. Callers receive no record
    for a designation whose selected producer did not write the designated
    value.

    ``consumer_read`` is the exact read which consumed the effect, when one
    exists. ``displacement`` is the first exact later write which defeated the
    effect or a required consumer-shape read. ``observed_reads`` retains the
    local occurrence-addressed reads relevant to the verdict, while
    ``displacement_enabling_reads`` carries the displacement's exact executed
    guard ancestry. Neither flattens repeated reads into a tag dictionary.
    """

    disposition: EffectDisposition
    appeared: RungWrite
    consumer_read: RungRead | None = None
    displacement: RungWrite | None = None
    displaced_read: RungRead | None = None
    observed_reads: tuple[RungRead, ...] = ()
    displacement_enabling_reads: tuple[RungRead, ...] = ()
    detail: str = ""


@dataclass
class ScanRungWriteProjection:
    """Indexed exact accesses and scan boundaries for one replay."""

    scan_id: int
    entry_tags: Mapping[str, Any]
    exit_tags: Mapping[str, Any]
    runs: tuple[RungRun, ...]
    writes: tuple[RungWrite, ...]
    reads: tuple[RungRead, ...]
    _writes_by_run: dict[int, tuple[RungWrite, ...]] = field(init=False, repr=False)
    _writes_by_tag: dict[str, tuple[RungWrite, ...]] = field(init=False, repr=False)
    _reads_by_run: dict[int, tuple[RungRead, ...]] = field(init=False, repr=False)
    _reads_by_tag: dict[str, tuple[RungRead, ...]] = field(init=False, repr=False)
    _call_invocation_by_run: dict[int, int | None] = field(init=False, repr=False)
    _run_order_by_identity: dict[int, int] = field(init=False, repr=False)
    _parent_order_by_run: dict[int, int | None] = field(init=False, repr=False)
    _write_identities: frozenset[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for kind, accesses in (("read", self.reads), ("write", self.writes)):
            for access in accesses:
                if access.scan_id != self.scan_id:
                    raise ValueError(f"{kind} belongs to a different projection scan")
                if not 0 <= access.run_order < len(self.runs):
                    raise ValueError(f"{kind} has no projection-owned run order")
                if self.runs[access.run_order] is not access.run:
                    raise ValueError(f"{kind} run identity does not match its projection order")

        by_run: dict[int, list[RungWrite]] = {}
        by_tag: dict[str, list[RungWrite]] = {}
        for occurrence in self.writes:
            by_run.setdefault(occurrence.run_order, []).append(occurrence)
            by_tag.setdefault(occurrence.transition.tag_name, []).append(occurrence)
        reads_by_run: dict[int, list[RungRead]] = {}
        reads_by_tag: dict[str, list[RungRead]] = {}
        for occurrence in self.reads:
            reads_by_run.setdefault(occurrence.run_order, []).append(occurrence)
            reads_by_tag.setdefault(occurrence.occurrence.name, []).append(occurrence)
        self._writes_by_run = {key: tuple(value) for key, value in by_run.items()}
        self._writes_by_tag = {key: tuple(value) for key, value in by_tag.items()}
        self._reads_by_run = {key: tuple(value) for key, value in reads_by_run.items()}
        self._reads_by_tag = {key: tuple(value) for key, value in reads_by_tag.items()}
        self._call_invocation_by_run = _dynamic_call_invocations(self.runs)
        self._run_order_by_identity = {id(run): order for order, run in enumerate(self.runs)}
        parent_by_run: dict[int, int | None] = {}
        ancestry: list[int] = []
        for order, run in enumerate(self.runs):
            while ancestry and self.runs[ancestry[-1]].depth >= run.depth:
                ancestry.pop()
            parent_by_run[order] = ancestry[-1] if ancestry else None
            ancestry.append(order)
        self._parent_order_by_run = parent_by_run
        self._write_identities = frozenset(id(write) for write in self.writes)

    def writes_for_run(self, run: RungRun) -> tuple[RungWrite, ...]:
        """Exact direct writes attributed to this dynamic run."""
        order = self._run_order_by_identity.get(id(run))
        return self._writes_by_run.get(order, ()) if order is not None else ()

    def reads_for_run(self, run: RungRun) -> tuple[RungRead, ...]:
        """Exact direct reads attributed to this dynamic run."""
        order = self._run_order_by_identity.get(id(run))
        return self._reads_by_run.get(order, ()) if order is not None else ()

    def reads_observed_by_write(self, write: RungWrite) -> tuple[RungRead, ...]:
        """Exact reads in the selected instruction before its selected write."""
        return tuple(
            read
            for read in self._reads_by_run.get(write.run_order, ())
            if read.instruction is write.instruction and read.ordinal < write.ordinal
        )

    def enabling_reads_observed_by_write(self, write: RungWrite) -> tuple[RungRead, ...]:
        """Exact direct rung reads observed before one selected write.

        Unlike :meth:`reads_observed_by_write`, this includes the enclosing
        rung's guard reads.  It is the factual surface needed to explain an
        overwriter (for example, the ``Done=True`` read which enabled an alarm
        copy) while still excluding reads owned by nested dynamic rungs.
        """

        return tuple(
            read
            for read in self._reads_by_run.get(write.run_order, ())
            if read.ordinal < write.ordinal
        )

    def enabling_read_closure_observed_by_write(
        self,
        write: RungWrite,
    ) -> tuple[RungRead, ...]:
        """Exact reads on the executed guard ancestry of one selected write.

        A write in a nested branch is enabled both by its direct branch reads
        and by each enclosing dynamic rung.  Walk only that recorded ancestry;
        sibling and unrelated runs remain outside the closure.  The returned
        occurrences retain their original execution order.
        """

        if id(write) not in self._write_identities:
            raise ValueError("write is not owned by this execution projection")

        runs = [write.run]
        enclosing = self.parent_run(write.run)
        while enclosing is not None:
            runs.append(enclosing)
            enclosing = self.parent_run(enclosing)
        direct_owner = id(write.run)
        ancestor_owners = {id(run) for run in runs[1:]}
        return tuple(
            read
            for read in self.reads
            if read.ordinal < write.ordinal
            and (
                id(read.run) == direct_owner
                or (id(read.run) in ancestor_owners and read.instruction is None)
            )
        )

    def observed_shape(self, consumer_read: RungRead) -> tuple[RungRead, ...]:
        """All exact direct reads of one selected dynamic consumer occurrence.

        This is factual projection only: it neither filters by nor reconstructs
        the adjustable required-shape policy. Repeated reads remain distinct
        occurrence records in ordinal order.
        """

        return self._reads_by_run.get(consumer_read.run_order, ())

    def observe_appeared_handoff(
        self,
        tag_name: str,
        expected_value: Any,
        *,
        producer_rung: object,
        consumer_rung: object | None,
        producer_address: StaticRungAddress | None = None,
        consumer_address: StaticRungAddress | None = None,
        required_shape: tuple[tuple[str, Any], ...] = (),
    ) -> tuple[OrderedEffectObservation, ...]:
        """Observe every exact occurrence of one static producer's value.

        ``producer_rung`` and ``consumer_rung`` are the immutable program rung
        objects selected before execution. Each matching producer write is
        judged separately. A consumer receives credit only when its recorded
        ``ReadOccurrence.source`` is that exact write occurrence; a later write
        of even the same value displaces the earlier producer's identity.

        A consumer-relative success ends at the consumer read: later program
        motion may legitimately advance the value.  A terminal designation
        (``consumer_rung is None``) instead requires survival to the scan exit.
        """

        appeared = tuple(
            write
            for write in self._writes_by_tag.get(tag_name, ())
            if write.run.rung is producer_rung and write.transition.to_value == expected_value
        )
        return tuple(
            self._observe_exact_handoff(
                write,
                consumer_rung=consumer_rung,
                producer_address=producer_address,
                consumer_address=consumer_address,
                required_shape=required_shape,
            )
            for write in appeared
        )

    def _observe_exact_handoff(
        self,
        effect_write: RungWrite,
        *,
        consumer_rung: object | None,
        producer_address: StaticRungAddress | None,
        consumer_address: StaticRungAddress | None,
        required_shape: tuple[tuple[str, Any], ...],
    ) -> OrderedEffectObservation:
        tag_name = effect_write.transition.tag_name
        if consumer_rung is None:
            displacement = self._first_intervening_write(
                tag_name,
                after=effect_write.ordinal,
            )
            if displacement is not None:
                return OrderedEffectObservation(
                    "OVERWRITTEN",
                    effect_write,
                    displacement=displacement,
                    observed_reads=self.enabling_reads_observed_by_write(displacement),
                    displacement_enabling_reads=(
                        self.enabling_read_closure_observed_by_write(displacement)
                    ),
                )
            return OrderedEffectObservation("SURVIVED", effect_write)

        consumer_runs = tuple(
            run
            for run in self.runs
            if run.rung is consumer_rung
            and any(read.ordinal > effect_write.ordinal for read in self.reads_for_run(run))
            and self._runs_share_selected_transaction(
                effect_write.run,
                run,
                producer_address,
                consumer_address,
            )
        )
        sourced_reads = tuple(
            read
            for run in consumer_runs
            for read in self.reads_for_run(run)
            if read.occurrence.name == tag_name
            and read.ordinal > effect_write.ordinal
            and self._read_observes_write(read, effect_write)
        )
        effect_read = sourced_reads[0] if sourced_reads else None
        consumer_run = (
            effect_read.run
            if effect_read is not None
            else consumer_runs[0]
            if consumer_runs
            else None
        )
        consumer_reads = (
            self.observed_shape(effect_read)
            if effect_read is not None
            else self.reads_for_run(consumer_run)
            if consumer_run is not None
            else ()
        )
        consumer_boundary = (
            effect_read.ordinal
            if effect_read is not None
            else max((read.ordinal for read in consumer_reads), default=-1) + 1
            if consumer_reads
            else None
        )
        displacement = self._first_intervening_write(
            tag_name,
            after=effect_write.ordinal,
            before=consumer_boundary,
        )
        if displacement is not None:
            return OrderedEffectObservation(
                "OVERWRITTEN",
                effect_write,
                consumer_read=effect_read,
                displacement=displacement,
                observed_reads=self.enabling_reads_observed_by_write(displacement),
                displacement_enabling_reads=(
                    self.enabling_read_closure_observed_by_write(displacement)
                ),
            )

        if effect_read is None:
            incomplete_source = any(
                read.occurrence.name == tag_name
                and read.occurrence.value == effect_write.transition.to_value
                and self.transition_observed_by_read(read) is None
                for read in consumer_reads
            )
            return OrderedEffectObservation(
                "UNKNOWN" if incomplete_source else "STRANDED",
                effect_write,
                observed_reads=consumer_reads,
                detail=(
                    "consumer effect read has incomplete source identity"
                    if incomplete_source
                    else "consumer did not observe the appeared write"
                ),
            )

        matched_shape: list[RungRead] = []
        next_ordinal = -1
        for required_tag, required_value in required_shape:
            if (
                required_tag == tag_name
                and required_value == effect_write.transition.to_value
                and effect_read.ordinal > next_ordinal
            ):
                observed = effect_read
            else:
                observed = next(
                    (
                        read
                        for read in consumer_reads
                        if read.occurrence.name == required_tag and read.ordinal > next_ordinal
                    ),
                    None,
                )
            if observed is None:
                return OrderedEffectObservation(
                    "UNKNOWN",
                    effect_write,
                    consumer_read=effect_read,
                    observed_reads=consumer_reads,
                    detail=f"required consumer read {required_tag!r} did not occur",
                )
            next_ordinal = observed.ordinal
            matched_shape.append(observed)
            if observed.occurrence.value == required_value:
                continue
            transition = self.transition_observed_by_read(observed)
            displaced_by = (
                self.write_at_ordinal(transition.occurrence_ordinal)
                if transition is not None and transition.occurrence_ordinal is not None
                else None
            )
            if displaced_by is None:
                if not effect_read.run.enabled:
                    return OrderedEffectObservation(
                        "STRANDED",
                        effect_write,
                        consumer_read=effect_read,
                        displaced_read=observed,
                        observed_reads=consumer_reads,
                        detail="consumer read the effect but its guard was false",
                    )
                return OrderedEffectObservation(
                    "UNKNOWN",
                    effect_write,
                    consumer_read=effect_read,
                    displaced_read=observed,
                    observed_reads=consumer_reads,
                    detail="required shape mismatch has no exact same-scan displacement",
                )
            return OrderedEffectObservation(
                "DISPLACED",
                effect_write,
                consumer_read=effect_read,
                displacement=displaced_by,
                displaced_read=observed,
                observed_reads=consumer_reads,
                displacement_enabling_reads=(
                    self.enabling_read_closure_observed_by_write(displaced_by)
                ),
            )

        if not effect_read.run.enabled:
            return OrderedEffectObservation(
                "STRANDED",
                effect_write,
                consumer_read=effect_read,
                observed_reads=consumer_reads,
                detail="consumer read the effect but its guard was false",
            )

        return OrderedEffectObservation(
            "SURVIVED",
            effect_write,
            consumer_read=effect_read,
            observed_reads=consumer_reads,
        )

    def _read_observes_write(self, read: RungRead, write: RungWrite) -> bool:
        """Whether one read carries this exact write occurrence as its source."""

        if read.occurrence.source is not write.occurrence:
            return False
        transition = self.transition_observed_by_read(read)
        return transition is not None and transition.occurrence_ordinal == write.ordinal

    def _runs_share_selected_transaction(
        self,
        producer: RungRun,
        consumer: RungRun,
        producer_address: StaticRungAddress | None,
        consumer_address: StaticRungAddress | None,
    ) -> bool:
        """Whether two dynamic runs belong to the selected static transaction.

        Repeated calls reuse the same immutable rung objects. When both sides
        of a handoff live in one subroutine, caller and call-stack identity must
        match; an exact source link in a later call is not the selected
        consumer occurrence. Sibling branches in one static rung additionally
        require the same enclosing dynamic rung.
        """

        if producer_address is None or consumer_address is None:
            return True
        producer_sub, producer_rung, producer_branch = producer_address
        consumer_sub, consumer_rung, consumer_branch = consumer_address
        same_subroutine = producer_sub is not None and producer_sub == consumer_sub
        same_branched_rung = (
            producer_sub == consumer_sub
            and producer_rung == consumer_rung
            and bool(producer_branch or consumer_branch)
        )
        if same_subroutine or same_branched_rung:
            if (
                producer.caller_rung != consumer.caller_rung
                or producer.call_stack != consumer.call_stack
            ):
                return False
        if same_subroutine:
            producer_invocation = self._call_invocation_by_run.get(id(producer))
            consumer_invocation = self._call_invocation_by_run.get(id(consumer))
            if (
                producer_invocation is None
                or consumer_invocation is None
                or producer_invocation != consumer_invocation
            ):
                return False
        if same_branched_rung:
            producer_parent = self.parent_run(producer)
            consumer_parent = self.parent_run(consumer)
            return producer_parent is consumer_parent
        return True

    def _first_intervening_write(
        self,
        tag_name: str,
        *,
        after: int,
        before: int | None = None,
    ) -> RungWrite | None:
        """First later write which replaces an appeared producer's identity."""

        return next(
            (
                write
                for write in self._writes_by_tag.get(tag_name, ())
                if write.ordinal > after and (before is None or write.ordinal < before)
            ),
            None,
        )

    def parent_run(self, run: RungRun) -> RungRun | None:
        """Nearest enclosing dynamic run, if this occurrence is nested."""
        run_order = self._run_order_by_identity.get(id(run))
        if run_order is None:
            return None
        parent_order = self._parent_order_by_run[run_order]
        return self.runs[parent_order] if parent_order is not None else None

    def write_at_ordinal(self, ordinal: int) -> RungWrite | None:
        """Resolve one retained rung-write identity."""
        for candidate in self.writes:
            if candidate.ordinal == ordinal:
                return candidate
        return None

    def final_write(self, tag_name: str, value: Any = _UNSET) -> RungWrite | None:
        """Last retained rung write consistent with the committed value."""
        candidates = self._writes_by_tag.get(tag_name, ())
        exit_value = self.exit_tags.get(tag_name) if value is _UNSET else value
        for candidate in reversed(candidates):
            if candidate.transition.to_value == exit_value:
                return candidate
        return None

    def boundary_transition(self, tag_name: str) -> Transition | None:
        """Committed transition attributed when a retained final write exists."""
        before = self.entry_tags.get(tag_name)
        after = self.exit_tags.get(tag_name)
        if before == after:
            return None
        winner = self.final_write(tag_name, after)
        return Transition(
            tag_name,
            self.scan_id,
            before,
            after,
            winner.ordinal if winner is not None else None,
        )

    def transition_observed_by(
        self,
        tag_name: str,
        run: RungRun,
        *,
        observed_value: Any,
    ) -> Transition | None:
        """Exact same-scan definition observed by a matching read in ``run``."""
        read = next(
            (
                candidate
                for candidate in self.reads_for_run(run)
                if candidate.occurrence.name == tag_name
                and candidate.occurrence.value == observed_value
            ),
            None,
        )
        if read is None:
            return None
        return self.transition_observed_by_read(read)

    def transition_observed_by_read(self, read: RungRead) -> Transition | None:
        """The exact dynamic definition carried by one observed tag read."""
        from pyrung.core.executor import WriteOccurrence

        tag_name = read.occurrence.name
        observed_value = read.occurrence.value
        source = read.occurrence.source
        if isinstance(source, WriteOccurrence):
            if source.domain != "tag" or source.name != tag_name or source.after != observed_value:
                raise RuntimeError("observed read carries an inconsistent write definition")
            return Transition(
                tag_name,
                self.scan_id,
                source.before,
                source.after,
                source.ordinal,
            )

        if source == "pending":
            # Compatibility for a context that attached its observer after a
            # pending value was established. Selected historical replay
            # attaches before all observable scan phases and never lands here.
            entry = self.entry_tags.get(tag_name)
            if entry != observed_value:
                return Transition(tag_name, self.scan_id, entry, observed_value)
        return None


def build_scan_rung_write_projection(
    history: History,
    scan_id: int,
    runs: tuple[RungRun, ...],
    *,
    entry_tags: Mapping[str, Any] | None = None,
    exit_tags: Mapping[str, Any] | None = None,
    include_memory_reads: bool = False,
) -> ScanRungWriteProjection | None:
    """Index exact replay occurrences against committed scan boundaries."""
    from pyrung.core.executor import ReadOccurrence

    if entry_tags is None or exit_tags is None:
        ids = history.scan_ids()
        try:
            scan_index = ids.index(scan_id)
        except ValueError:
            return None
        if scan_index == 0:
            return None
        entry = history.at(ids[scan_index - 1]).tags
        exit_boundary = history.at(scan_id).tags
    else:
        # Captured boundaries are immutable persistent maps. Retain their
        # structurally shared values instead of copying a full program-sized
        # dictionary for every selected scan.
        entry = entry_tags
        exit_boundary = exit_tags
    writes: list[RungWrite] = []
    reads: list[RungRead] = []
    call_invocations = _dynamic_call_invocations(runs)
    for run_order, run in enumerate(runs):
        for instruction, occurrence in _direct_accesses(run):
            if isinstance(occurrence, ReadOccurrence):
                if occurrence.domain != "tag" and not include_memory_reads:
                    continue
                reads.append(
                    RungRead(
                        scan_id=scan_id,
                        ordinal=occurrence.ordinal,
                        run_order=run_order,
                        call_invocation=call_invocations.get(id(run)),
                        rung_id=run.rung_id,
                        run=run,
                        instruction=instruction,
                        occurrence=occurrence,
                    )
                )
                continue
            if occurrence.domain != "tag":
                continue
            transition = Transition(
                occurrence.name,
                scan_id,
                occurrence.before,
                occurrence.after,
                occurrence_ordinal=occurrence.ordinal,
            )
            writes.append(
                RungWrite(
                    scan_id=scan_id,
                    ordinal=occurrence.ordinal,
                    run_order=run_order,
                    call_invocation=call_invocations.get(id(run)),
                    rung_id=run.rung_id,
                    run=run,
                    instruction=instruction,
                    occurrence=occurrence,
                    transition=transition,
                )
            )

    writes.sort(key=lambda occurrence: occurrence.ordinal)
    reads.sort(key=lambda occurrence: occurrence.ordinal)

    return ScanRungWriteProjection(
        scan_id=scan_id,
        entry_tags=entry,
        exit_tags=exit_boundary,
        runs=runs,
        writes=tuple(writes),
        reads=tuple(reads),
    )


@dataclass(frozen=True)
class _CompactConditionState:
    """Only the scan metadata a detached :class:`ConditionView` exposes."""

    scan_id: int
    timestamp: float


@dataclass(frozen=True)
class _RecordedResolver:
    """Replay only values whose captured read origin was ``resolved``."""

    values: Mapping[str, Any]

    def __call__(self, name: str, _view: Any) -> tuple[bool, Any]:
        try:
            return True, self.values[name]
        except KeyError:
            return False, None


def compact_projection_condition_views(projection: ScanRungWriteProjection) -> None:
    """Detach full scan state from every condition view in ``projection``.

    The projection's exact direct guard-read occurrences are authoritative.
    This function deliberately never rereads a live view's tag or memory maps:
    the runner may already have updated its fast-read cache during commit.
    Views shared by a parent/branch or continued rung are compacted once from
    the union of all their direct condition reads.

    Raises ``ValueError`` when one shared view cannot represent its recorded
    reads with a single frozen value/source surface. The session owner treats
    that as an all-or-nothing capture-stream failure and falls back to exact
    historical replay.
    """

    from pyrung.core.executor import WriteOccurrence

    views: dict[int, Any] = {}
    reads_by_view: dict[int, list[Any]] = {}
    for run in projection.runs:
        key = id(run.view)
        views[key] = run.view
        reads_by_view.setdefault(key, [])
    for read in projection.reads:
        if read.instruction is not None:
            continue
        key = id(read.run.view)
        if key not in views:
            raise ValueError("guard read refers to a view outside the projection")
        reads_by_view[key].append(read.occurrence)

    for key, view in views.items():
        entry_tags: dict[str, Any] = {}
        entry_memory: dict[str, Any] = {}
        pending_tags: dict[str, Any] = {}
        pending_memory: dict[str, Any] = {}
        tag_sources: dict[str, WriteOccurrence] = {}
        memory_sources: dict[str, WriteOccurrence] = {}
        resolved_tags: dict[str, Any] = {}
        definitions: dict[tuple[str, str], tuple[str, Any, Any]] = {}

        for occurrence in reads_by_view[key]:
            source = occurrence.source
            origin = "pending" if isinstance(source, WriteOccurrence) else source
            if origin not in {"entry", "pending", "default", "resolved"}:
                raise ValueError(f"unknown captured read origin: {origin!r}")
            if occurrence.domain not in {"tag", "memory"}:
                raise ValueError(f"unknown captured read domain: {occurrence.domain!r}")
            if occurrence.domain == "memory" and origin == "resolved":
                raise ValueError("memory reads cannot have resolved origin")

            address = (occurrence.domain, occurrence.name)
            definition_source = source if isinstance(source, WriteOccurrence) else origin
            previous = definitions.get(address)
            if previous is not None:
                previous_origin, previous_value, previous_source = previous
                same_value = previous_value is occurrence.value
                if not same_value:
                    try:
                        same_value = previous_value == occurrence.value
                    except (TypeError, ValueError):
                        same_value = False
                same_source = (
                    previous_source is definition_source
                    if isinstance(definition_source, WriteOccurrence)
                    else previous_source == definition_source
                )
                if previous_origin != origin or same_value is not True or not same_source:
                    raise ValueError(
                        "one captured condition view observed incompatible definitions "
                        f"for {occurrence.domain} {occurrence.name!r}"
                    )
            else:
                definitions[address] = (origin, occurrence.value, definition_source)

            if origin == "default":
                # Absence is the compact representation. Re-evaluating the
                # same condition supplies the same default argument.
                continue
            if origin == "resolved":
                resolved_tags[occurrence.name] = occurrence.value
                continue
            if occurrence.domain == "tag":
                target = pending_tags if origin == "pending" else entry_tags
                target[occurrence.name] = occurrence.value
                if isinstance(source, WriteOccurrence):
                    tag_sources[occurrence.name] = source
            else:
                target = pending_memory if origin == "pending" else entry_memory
                target[occurrence.name] = occurrence.value
                if isinstance(source, WriteOccurrence):
                    memory_sources[occurrence.name] = source

        # Preserve only public scan metadata and the opaque scope identity.
        # Every state/mapping/resolver reference below is replaced, so the
        # projection cannot pin the captured SystemState or its full maps.
        compact_state = _CompactConditionState(view.scan_id, view.timestamp)
        scope_token = view.scope_token
        view._state = compact_state
        view._tags = entry_tags
        view._memory = entry_memory
        view._tags_snapshot = pending_tags
        view._memory_snapshot = pending_memory
        view._resolver = _RecordedResolver(resolved_tags) if resolved_tags else None
        view._scope_token = scope_token
        view._read_sink = None
        view._tag_source_snapshot = tag_sources or None
        view._memory_source_snapshot = memory_sources or None


def _dynamic_call_invocations(runs: tuple[RungRun, ...]) -> dict[int, int | None]:
    """Map each dynamic rung run to its nearest exact CallInstruction run.

    ``caller_rung`` and ``call_stack`` identify a static call site and nested
    route, but one caller instruction body may call the same subroutine twice.
    The immutable recursive execution journal retains those as distinct
    ``InstructionRun`` objects; numbering them in execution order gives this
    projection an exact detached invocation identity.
    """

    from pyrung.core.executor import InstructionRun, LoopIterationRun, RungRun
    from pyrung.core.instruction.control import CallInstruction

    nested: set[int] = set()

    def _collect_nested(body: tuple[object, ...]) -> None:
        for item in body:
            if isinstance(item, RungRun):
                nested.add(id(item))
                _collect_nested(item.body)
            elif isinstance(item, (InstructionRun, LoopIterationRun)):
                _collect_nested(item.body)

    for run in runs:
        _collect_nested(run.body)

    result: dict[int, int | None] = {}
    invocation_ids: dict[int, int] = {}

    def _walk(body: tuple[object, ...], invocation: int | None) -> None:
        for item in body:
            if isinstance(item, RungRun):
                result[id(item)] = invocation
                _walk(item.body, invocation)
            elif isinstance(item, InstructionRun):
                child_invocation = invocation
                if isinstance(item.instruction, CallInstruction):
                    child_invocation = invocation_ids.setdefault(
                        id(item),
                        len(invocation_ids),
                    )
                _walk(item.body, child_invocation)
            elif isinstance(item, LoopIterationRun):
                _walk(item.body, invocation)

    for run in runs:
        if id(run) in nested:
            continue
        result[id(run)] = None
        _walk(run.body, None)
    return result


def _direct_accesses(
    run: RungRun,
) -> Iterator[tuple[InstructionRun | None, ReadOccurrence | WriteOccurrence]]:
    """Yield this rung's own accesses in execution order, stopping at child rungs."""
    from pyrung.core.executor import (
        InstructionRun,
        LoopIterationRun,
        ReadOccurrence,
        RungRun,
        WriteOccurrence,
    )

    def _walk(
        body: tuple[object, ...],
        instruction: InstructionRun | None,
    ) -> Iterator[tuple[InstructionRun | None, ReadOccurrence | WriteOccurrence]]:
        for item in body:
            if isinstance(item, (ReadOccurrence, WriteOccurrence)):
                yield instruction, item
            elif isinstance(item, RungRun):
                continue
            elif isinstance(item, InstructionRun):
                yield from _walk(item.body, item)
            elif isinstance(item, LoopIterationRun):
                yield from _walk(item.body, instruction)

    yield from _walk(run.body, None)
