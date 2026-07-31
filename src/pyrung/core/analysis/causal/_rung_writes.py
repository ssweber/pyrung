"""Exact tag-access index over one observed interpreted scan.

The executor owns execution truth.  Its recursive :class:`RungRun` journal
retains every immediate read and write at the point it occurred, including
several writes to one tag inside one instruction and parent/child interleaving.
This module adds lookup indexes and causal ``Transition`` views without
reconstructing, grouping, or otherwise changing that execution order.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
    rung_id: RungId
    run: RungRun = field(compare=False, repr=False)
    instruction: InstructionRun | None = field(compare=False, repr=False)
    occurrence: ReadOccurrence = field(compare=False, repr=False)


@dataclass
class ScanRungWriteProjection:
    """Indexed exact accesses and scan boundaries for one replay."""

    scan_id: int
    entry_tags: dict[str, Any]
    exit_tags: dict[str, Any]
    runs: tuple[RungRun, ...]
    writes: tuple[RungWrite, ...]
    reads: tuple[RungRead, ...]
    _writes_by_run: dict[int, tuple[RungWrite, ...]] = field(init=False, repr=False)
    _writes_by_tag: dict[str, tuple[RungWrite, ...]] = field(init=False, repr=False)
    _reads_by_run: dict[int, tuple[RungRead, ...]] = field(init=False, repr=False)
    _reads_by_tag: dict[str, tuple[RungRead, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
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

    def writes_for_run(self, run: RungRun) -> tuple[RungWrite, ...]:
        """Exact direct writes attributed to this dynamic run."""
        for order, candidate in enumerate(self.runs):
            if candidate is run:
                return self._writes_by_run.get(order, ())
        return ()

    def reads_for_run(self, run: RungRun) -> tuple[RungRead, ...]:
        """Exact direct reads attributed to this dynamic run."""
        for order, candidate in enumerate(self.runs):
            if candidate is run:
                return self._reads_by_run.get(order, ())
        return ()

    def reads_observed_by_write(self, write: RungWrite) -> tuple[RungRead, ...]:
        """Exact reads in the selected instruction before its selected write."""
        return tuple(
            read
            for read in self._reads_by_run.get(write.run_order, ())
            if read.instruction is write.instruction and read.ordinal < write.ordinal
        )

    def parent_run(self, run: RungRun) -> RungRun | None:
        """Nearest enclosing dynamic run, if this occurrence is nested."""
        run_order = self._run_order(run)
        if run_order is None:
            return None
        for candidate in reversed(self.runs[:run_order]):
            if candidate.depth < run.depth:
                return candidate
        return None

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

    def _run_order(self, run: RungRun) -> int | None:
        for order, candidate in enumerate(self.runs):
            if candidate is run:
                return order
        return None


def build_scan_rung_write_projection(
    history: History,
    scan_id: int,
    runs: tuple[RungRun, ...],
) -> ScanRungWriteProjection | None:
    """Index exact replay occurrences against committed scan boundaries."""
    from pyrung.core.executor import ReadOccurrence

    ids = history.scan_ids()
    try:
        scan_index = ids.index(scan_id)
    except ValueError:
        return None
    if scan_index == 0:
        return None

    entry = dict(history.at(ids[scan_index - 1]).tags)
    exit_tags = dict(history.at(scan_id).tags)
    writes: list[RungWrite] = []
    reads: list[RungRead] = []
    for run_order, run in enumerate(runs):
        for instruction, occurrence in _direct_accesses(run):
            if occurrence.domain != "tag":
                continue
            if isinstance(occurrence, ReadOccurrence):
                reads.append(
                    RungRead(
                        scan_id=scan_id,
                        ordinal=occurrence.ordinal,
                        run_order=run_order,
                        rung_id=run.rung_id,
                        run=run,
                        instruction=instruction,
                        occurrence=occurrence,
                    )
                )
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
        exit_tags=exit_tags,
        runs=runs,
        writes=tuple(writes),
        reads=tuple(reads),
    )


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
