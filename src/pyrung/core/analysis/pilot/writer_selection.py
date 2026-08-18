"""Resolve, classify, and rank program writers for backward navigation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.availability import (
    _writer_availability,
    _WriterAvailability,
)
from pyrung.core.analysis.reverse_semantics import normalize_reverse_result
from pyrung.core.analysis.sp_values import (
    _invert_affine,
    _values_match,
    _writer_for_tag,
    _writer_projection,
    _written_value_for_tag,
)
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    UNKNOWN,
    Affine,
    AffineCmp,
    Aggregate,
    Cmp,
    Constraint,
    CrossingContext,
    Eq,
    Literal,
    ReverseResult,
    eq_target,
    evaluate_forward,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def _sole_write_instr(tag: str, pdg: ProgramGraph, program: Any) -> Any:
    """The sole exact static instruction writing *tag*, or ``None``."""

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    rung = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if rung is None:
        return None
    return _writer_for_tag(rung, tag)


def _reverse_writer(
    rung: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
) -> ReverseResult:
    """Reverse the exact instruction selected inside a writer rung."""

    instruction = _writer_for_tag(rung, tag)
    if instruction is None:
        return REVERSE_FALLTHROUGH
    from pyrung.core.analysis import crossings

    return crossings.reverse(
        instruction,
        rung,
        eq_target(tag, value),
        CrossingContext(snapshot=snapshot, tags_by_name=pdg.tags),
    )


def _producer_constraints(
    result: ReverseResult,
    target: Constraint,
) -> tuple[Constraint, ...]:
    """Deterministic producer requirements carried by a reverse receipt."""

    normalized = normalize_reverse_result(result)
    if normalized.fallthrough or normalized.contradiction or len(normalized.branches) != 1:
        return ()
    (branch,) = normalized.branches
    requirements: list[Constraint] = []
    for constraint in branch:
        if not isinstance(constraint, (Eq, Cmp, AffineCmp)):
            return ()
        if constraint == target:
            continue
        if isinstance(constraint, Eq) and len(constraint.values) != 1:
            return ()
        requirements.append(constraint)
    return tuple(requirements)


def _producer_pins(result: ReverseResult, target: Constraint) -> dict[str, Any]:
    """Singleton equality pins from one deterministic producer receipt."""

    pins: dict[str, Any] = {}
    for constraint in _producer_constraints(result, target):
        if isinstance(constraint, Eq):
            pins[constraint.tag] = next(iter(constraint.values))
    return pins


def _can_produce(written: Any, value: Any) -> bool:
    if isinstance(written, Literal):
        return _values_match(written.value, value)
    if isinstance(written, (Affine, Aggregate)):
        return True
    return True


_UNRESOLVED = object()


def _concrete_written_value(written: Any, snapshot: dict[str, Any]) -> Any:
    """The concrete value *written* provably drives, or ``_UNRESOLVED``."""

    if not isinstance(written, (Literal, Affine, Aggregate)):
        return _UNRESOLVED
    produced = evaluate_forward(written, snapshot)
    return _UNRESOLVED if produced is UNKNOWN else produced


def _writer_clobbers_codemand(
    rung: Any,
    tag: str,
    codemands: tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any],
) -> bool:
    """Whether a writer's co-writes provably falsify a sibling demand."""

    for codemand_tag, codemand_value in codemands:
        if codemand_tag == tag:
            continue
        produced = _concrete_written_value(
            _written_value_for_tag(rung, codemand_tag), snapshot
        )
        if produced is _UNRESOLVED:
            continue
        if not _values_match(produced, codemand_value):
            return True
    return False


def _is_self_gated(rung_node: Any, pdg: ProgramGraph, tag: str) -> bool:
    """Whether a writer condition or call gate reads its own destination."""

    if tag in rung_node.condition_reads:
        return True
    if rung_node.subroutine:
        for caller in pdg.rung_nodes:
            if rung_node.subroutine in caller.calls and tag in caller.condition_reads:
                return True
    return False


@dataclass(frozen=True)
class _WriterRank:
    """One writer's place in the selected ordering and its sort dimensions."""

    ri: int
    availability: _WriterAvailability
    bucket: int
    clobber: int


def _rank_writers(
    writers: frozenset[int],
    pdg: ProgramGraph,
    program: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    opaque_loop: frozenset[str] = frozenset(),
    clear_only: frozenset[str] = frozenset(),
    *,
    steerable: frozenset[str] = frozenset(),
    ancestry: tuple[tuple[str, Any], ...] = (),
    codemands: tuple[tuple[str, Any], ...] = (),
    availability_out: dict[int, _WriterAvailability] | None = None,
    ranking_out: list[_WriterRank] | None = None,
    reverse_out: dict[int, ReverseResult] | None = None,
) -> list[int]:
    """Rank viable writers by current-state availability, then writer role."""

    pinned_overlay = {name: snapshot.get(name) for name in opaque_loop}
    pinned = frozenset(opaque_loop)
    ranked: list[tuple[_WriterAvailability, int, int, int]] = []
    prior_same_tag_values = tuple(item_value for item_tag, item_value in ancestry if item_tag == tag)
    ancestry_tags = frozenset(
        item_tag for item_tag, _item_value in ancestry if item_tag not in steerable
    )
    for rung_index in sorted(writers):
        rung_node = pdg.rung_nodes[rung_index]
        rung = resolve_rung(program, rung_node)
        if rung is None:
            continue
        written = _written_value_for_tag(rung, tag)
        if not _can_produce(written, value):
            continue
        reverse_result = _reverse_writer(rung, tag, value, snapshot, pdg)
        if reverse_out is not None:
            reverse_out[rung_index] = reverse_result
        if normalize_reverse_result(reverse_result).contradiction:
            continue
        projection = _writer_projection(
            rung,
            tag,
            value,
            snapshot,
            pdg,
            program,
            pinned_overlay,
            pinned,
        )
        is_counterfactual = projection is not None and projection[0]
        availability = _writer_availability(
            rung,
            rung_node,
            written,
            tag,
            value,
            snapshot,
            pdg,
            program,
            steerable,
            opaque_loop,
            is_counterfactual,
            ancestry_tags,
        )
        if (
            isinstance(written, Affine)
            and written.source == tag
            and written.storage.kind == "identity"
        ):
            source_value = _invert_affine(written, value)
            if source_value is not None and any(
                _values_match(source_value, prior) for prior in prior_same_tag_values
            ):
                availability = _WriterAvailability.UNAVAILABLE_FROM_HERE

        bucket = 1
        if isinstance(written, Literal) and _values_match(written.value, value):
            if _is_self_gated(rung_node, pdg, tag):
                bucket = 4
            elif is_counterfactual:
                bucket = 3
            elif projection is not None and any(name in clear_only for name in projection[1]):
                bucket = 2
            else:
                bucket = 0
        else:
            producer_pins = _producer_pins(reverse_result, eq_target(tag, value))
            if producer_pins and all(
                _values_match(snapshot.get(source_tag), source_value)
                for source_tag, source_value in producer_pins.items()
            ):
                bucket = 0
            if is_counterfactual:
                bucket = 3

        clobber = (
            1
            if codemands and _writer_clobbers_codemand(rung, tag, codemands, snapshot)
            else 0
        )
        ranked.append((availability, bucket, clobber, rung_index))
        if availability_out is not None:
            availability_out[rung_index] = availability

    ordered = sorted(ranked)
    if ranking_out is not None:
        ranking_out.extend(
            _WriterRank(
                ri=rung_index,
                availability=availability,
                bucket=bucket,
                clobber=clobber,
            )
            for availability, bucket, clobber, rung_index in ordered
        )
    return [rung_index for _availability, _bucket, _clobber, rung_index in ordered]
