"""Input-group detection and combo specialization for prove BFS."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import Atom, Expr, _condition_to_expr
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


@dataclass(frozen=True, slots=True)
class _ExclusiveInputGroup:
    """A Bool input family whose multi-hot combinations are observationally redundant."""

    target_name: str
    members: tuple[str, ...]
    canonical_assignments: tuple[tuple[tuple[str, bool], ...], ...]


@dataclass(frozen=True, slots=True)
class _ScopedInstruction:
    instr: Any
    scope: str
    subroutine: str | None
    rung_index: int
    branch_path: tuple[int, ...]
    conditions: tuple[Any, ...]
    rung_instruction_count: int
    has_branches: bool
    instruction_depth: int


@dataclass(frozen=True, slots=True)
class _EncoderCandidate:
    target_name: str
    input_name: str
    node_index: int


def _walk_scoped_instructions(program: Program) -> list[_ScopedInstruction]:
    from pyrung.core.instruction.control import ForLoopInstruction

    scoped: list[_ScopedInstruction] = []

    def _walk_instruction_list(
        instructions: list[Any],
        *,
        scope: str,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        conditions: tuple[Any, ...],
        rung_instruction_count: int,
        has_branches: bool,
        instruction_depth: int,
    ) -> None:
        for instr in instructions:
            scoped.append(
                _ScopedInstruction(
                    instr=instr,
                    scope=scope,
                    subroutine=subroutine,
                    rung_index=rung_index,
                    branch_path=branch_path,
                    conditions=conditions,
                    rung_instruction_count=rung_instruction_count,
                    has_branches=has_branches,
                    instruction_depth=instruction_depth,
                )
            )
            if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
                _walk_instruction_list(
                    instr.instructions,
                    scope=scope,
                    subroutine=subroutine,
                    rung_index=rung_index,
                    branch_path=branch_path,
                    conditions=conditions,
                    rung_instruction_count=rung_instruction_count,
                    has_branches=has_branches,
                    instruction_depth=instruction_depth + 1,
                )

    def _walk_rung(
        rung: Rung,
        *,
        scope: str,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        conditions = tuple(rung._conditions)
        _walk_instruction_list(
            rung._instructions,
            scope=scope,
            subroutine=subroutine,
            rung_index=rung_index,
            branch_path=branch_path,
            conditions=conditions,
            rung_instruction_count=len(rung._instructions),
            has_branches=bool(rung._branches),
            instruction_depth=0,
        )
        for branch_idx, branch in enumerate(rung._branches):
            _walk_rung(
                branch,
                scope=scope,
                subroutine=subroutine,
                rung_index=rung_index,
                branch_path=branch_path + (branch_idx,),
            )

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, scope="main", subroutine=None, rung_index=rung_index, branch_path=())

    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(
                rung,
                scope="subroutine",
                subroutine=sub_name,
                rung_index=rung_index,
                branch_path=(),
            )

    return scoped


def _literal_copy_value(raw_value: Any) -> Any | None:
    from pyrung.core.expression import LiteralExpr
    from pyrung.core.tag import ImmediateRef

    raw = raw_value.value if isinstance(raw_value, ImmediateRef) else raw_value
    if isinstance(raw, LiteralExpr):
        return raw.value
    if isinstance(raw, (bool, int, float)):
        return raw
    return None


def _observed_tags(
    *,
    project: tuple[str, ...] | None,
    extra_exprs: list[Expr] | None,
) -> frozenset[str]:
    from .expr import _referenced_tags

    observed = set(project or ())
    for expr in extra_exprs or ():
        observed.update(_referenced_tags(expr))
    return frozenset(observed)


def _canonical_assignments_for_members(
    members: tuple[str, ...],
) -> tuple[tuple[tuple[str, bool], ...], ...]:
    assignments: list[tuple[tuple[str, bool], ...]] = []
    assignments.append(tuple((name, False) for name in members))
    for chosen in members:
        assignments.append(tuple((name, name == chosen) for name in members))
    return tuple(assignments)


def _encoder_candidate(
    scoped: _ScopedInstruction,
    *,
    graph: ProgramGraph,
    nd_dims: dict[str, tuple[Any, ...]],
    observed_tags: frozenset[str],
    node_index_by_key: dict[tuple[str, str | None, int, tuple[int, ...]], int],
) -> _EncoderCandidate | None:
    from pyrung.core.instruction.data_transfer import CopyInstruction

    if scoped.instruction_depth != 0:
        return None
    if scoped.rung_instruction_count != 1 or scoped.has_branches:
        return None
    if not isinstance(scoped.instr, CopyInstruction) or scoped.instr.convert is not None:
        return None

    dest = scoped.instr.dest
    if not isinstance(dest, Tag):
        return None
    if _literal_copy_value(scoped.instr.source) is None:
        return None

    if len(scoped.conditions) != 1:
        return None
    expr = _condition_to_expr(scoped.conditions[0])
    if not isinstance(expr, Atom) or expr.form != "xic" or expr.operand is not None:
        return None

    input_name = expr.tag
    tag = graph.tags.get(input_name)
    if tag is None or tag.type != TagType.BOOL:
        return None
    if nd_dims.get(input_name) != (False, True):
        return None
    if input_name in observed_tags:
        return None

    node_key = (scoped.scope, scoped.subroutine, scoped.rung_index, scoped.branch_path)
    node_index = node_index_by_key.get(node_key)
    if node_index is None:
        return None
    node = graph.rung_nodes[node_index]
    if node.condition_reads != frozenset({input_name}):
        return None
    if node.data_reads or node.calls:
        return None
    if dest.name not in node.writes:
        return None
    if not all(name == dest.name or name.startswith("fault.") for name in node.writes):
        return None

    return _EncoderCandidate(dest.name, input_name, node_index)


def _detect_exclusive_input_groups(
    program: Program,
    graph: ProgramGraph,
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    *,
    project: tuple[str, ...] | None,
    extra_exprs: list[Expr] | None,
) -> tuple[_ExclusiveInputGroup, ...]:
    """Find Bool input families that are only observed through a shared encoder tag."""
    if project is None and not extra_exprs:
        return ()

    observed_tags = _observed_tags(project=project, extra_exprs=extra_exprs)
    node_index_by_key: dict[tuple[str, str | None, int, tuple[int, ...]], int] = {
        (node.scope, node.subroutine, node.rung_index, node.branch_path): idx
        for idx, node in enumerate(graph.rung_nodes)
    }

    candidates_by_target: dict[str, list[_EncoderCandidate]] = {}
    for scoped in _walk_scoped_instructions(program):
        candidate = _encoder_candidate(
            scoped,
            graph=graph,
            nd_dims=nondeterministic_dims,
            observed_tags=observed_tags,
            node_index_by_key=node_index_by_key,
        )
        if candidate is None:
            continue
        candidates_by_target.setdefault(candidate.target_name, []).append(candidate)

    groups: list[_ExclusiveInputGroup] = []
    used_inputs: set[str] = set()
    for target_name in sorted(candidates_by_target):
        candidates = candidates_by_target[target_name]
        members = tuple(sorted({candidate.input_name for candidate in candidates}))
        if len(members) < 2:
            continue
        if any(member in used_inputs for member in members):
            continue

        candidate_nodes = frozenset(candidate.node_index for candidate in candidates)
        if any(
            not graph.readers_of.get(member, frozenset()).issubset(candidate_nodes)
            for member in members
        ):
            continue

        groups.append(
            _ExclusiveInputGroup(
                target_name=target_name,
                members=members,
                canonical_assignments=_canonical_assignments_for_members(members),
            )
        )
        used_inputs.update(members)

    return tuple(groups)


def _exclusive_input_group_membership(
    groups: tuple[_ExclusiveInputGroup, ...],
) -> dict[str, int]:
    membership: dict[str, int] = {}
    for index, group in enumerate(groups):
        for member in group.members:
            membership[member] = index
    return membership


def _merge_assignment_diffs(
    base: dict[str, Any],
    diffs: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    """Merge assignment diffs, rejecting conflicting writes to one input."""
    merged = dict(base)
    written: dict[str, Any] = {}
    for diff in diffs:
        for name, value in diff.items():
            if name in written and written[name] != value:
                return None
            written[name] = value
            merged[name] = value
    return merged


def _iter_input_assignments(
    live_inputs: frozenset[str],
    nondeterministic_dims: dict[str, tuple[Any, ...]],
    groups: tuple[_ExclusiveInputGroup, ...],
    group_by_member: dict[str, int],
    current_values: dict[str, Any] | None = None,
    input_groups: tuple[tuple[str, ...], ...] = (),
    free_inputs: frozenset[str] = frozenset(),
) -> Any:
    """Yield single-dimension interleaved input assignments for one BFS state.

    Generates stutter (hold all inputs) plus single-input-change successors.
    Encoder families produce one-hot canonical changes.  User-declared
    ``input_groups`` add joint-product successors for grouped inputs.

    *free_inputs* are ND inputs elided from the state key.  Because their
    intermediate states are merged, single-dimension flips cannot chain
    through intermediate baselines.  Free inputs are therefore enumerated
    jointly (Cartesian product of all domain values) so that every
    combination is explored from the current BFS state.
    """
    if not live_inputs:
        return [()]

    if current_values is None:
        current_values = {}

    stutter: tuple[tuple[str, Any], ...] = tuple(
        (name, current_values.get(name, nondeterministic_dims[name][0]))
        for name in sorted(live_inputs)
    )

    stutter_dict = dict(stutter)

    seen_encoder_members: set[str] = set()
    seen_encoder_groups: set[int] = set()
    encoder_diffs: list[dict[str, Any]] = []
    edge_diffs: list[dict[str, Any]] = []
    for name in sorted(live_inputs):
        group_index = group_by_member.get(name)
        if group_index is not None:
            if group_index in seen_encoder_groups:
                continue
            seen_encoder_groups.add(group_index)
            group = groups[group_index]
            seen_encoder_members.update(group.members)
            current_canonical = tuple((m, stutter_dict.get(m, False)) for m in group.members)
            for canonical in group.canonical_assignments:
                if canonical != current_canonical:
                    encoder_diffs.append(dict(canonical))
        elif name in free_inputs:
            pass
        else:
            cur = stutter_dict[name]
            for value in nondeterministic_dims[name]:
                if value != cur:
                    edge_diffs.append({name: value})

    live_free = sorted(n for n in free_inputs if n in live_inputs and n not in seen_encoder_members)
    free_combos: list[dict[str, Any]] = [{}]
    if live_free:
        free_domains = [[(n, v) for v in nondeterministic_dims[n]] for n in live_free]
        for combo in itertools.product(*free_domains):
            d = dict(combo)
            if any(d[n] != stutter_dict.get(n) for n in d):
                free_combos.append(d)

    encoder_combos: list[dict[str, Any]] = [{}] + encoder_diffs

    user_group_option_lists: list[list[dict[str, Any]]] = []
    live_set = set(live_inputs)
    for ig in input_groups:
        live_members = [m for m in ig if m in live_set and m not in seen_encoder_members]
        if len(live_members) < 2:
            continue
        member_alternatives: list[list[tuple[str, Any]]] = []
        for m in live_members:
            cur = stutter_dict.get(m, nondeterministic_dims[m][0])
            alts = [(m, v) for v in nondeterministic_dims[m] if v != cur]
            if not alts:
                continue
            member_alternatives.append(alts)
        if len(member_alternatives) < 2:
            continue
        group_options: list[dict[str, Any]] = [{}]
        for combo in itertools.product(*member_alternatives):
            group_options.append(dict(combo))
        user_group_option_lists.append(group_options)

    seen: set[tuple[tuple[str, Any], ...]] = set()
    assignments: list[tuple[tuple[str, Any], ...]] = []
    option_dimensions: list[list[dict[str, Any]]] = [
        [{}] + edge_diffs,
        encoder_combos,
        free_combos,
        *user_group_option_lists,
    ]
    for diffs in itertools.product(*option_dimensions):
        merged = _merge_assignment_diffs(stutter_dict, diffs)
        if merged is None:
            continue
        entry = tuple(sorted(merged.items()))
        if entry not in seen:
            seen.add(entry)
            assignments.append(entry)

    return assignments
