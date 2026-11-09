"""Lower scalar constraints and synthesize actionable inequality levers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.trace_read as _trace_read
from pyrung.core.analysis.crossings._ranges import stored_value_possible
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.static_expressions import (
    _atom_text,
    _heuristic_inequality_target,
    _resolve_inequality_target,
    _resolved_atom_operand,
)
from pyrung.core.analysis.pilot.trace_tree import _FORM_TO_OP, _OP_TO_FORM
from pyrung.core.analysis.pilot.writer_selection import _sole_write_instr
from pyrung.core.analysis.reverse_semantics import normalize_reverse_result
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.sp_values import (
    _CMP_OPS,
    _FLIP_FORM,
    _satisfying_value,
    _values_match,
    _written_value_for_tag,
)
from pyrung.core.crossing import Affine, AffineCmp, Cmp, Constraint, Eq, Literal

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def _atom_target(
    atom: Atom,
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, Any] | None:
    """Convert an atom to the concrete tag/value needed to satisfy it."""

    form = atom.form
    if form == "xic":
        return (atom.tag, True)
    if form == "xio":
        return (atom.tag, False)
    if form == "eq":
        if atom.operand_is_tag:
            if snapshot is None or atom.operand not in snapshot:
                return None
            try:
                operand = atom.operand_scale * snapshot[atom.operand] + atom.operand_offset
            except TypeError:
                return None
            return (atom.tag, operand)
        return (atom.tag, atom.operand)
    if form == "rise":
        return (atom.tag, True)
    if form == "fall":
        return (atom.tag, False)
    if form == "truthy":
        return (atom.tag, True)
    return None


@dataclass(frozen=True)
class _Lever:
    """One actionable lever for an inequality atom, with provenance."""

    label: str
    tag: str
    value: Any
    heuristic: bool = False
    note: str = ""


def _rewrite_internal_compare(
    atom: Atom,
    steerable: frozenset[str],
    pdg: ProgramGraph,
    program: Any,
    snapshot: dict[str, Any],
    *,
    _depth: int = 0,
) -> list[Atom]:
    """Reverse one internal comparison through transparent copy/calc writers."""

    if _depth > 6 or atom.tag in steerable:
        return [atom]
    if atom.operand_is_tag and (atom.operand_scale != 1 or atom.operand_offset != 0):
        return [atom]
    op = _FORM_TO_OP.get(atom.form)
    if op is None:
        return [atom]
    instr = _sole_write_instr(atom.tag, pdg, program)
    if instr is None:
        return [atom]

    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import CrossingContext

    target = Cmp(atom.tag, op, atom.operand, bound_is_tag=atom.operand_is_tag)
    normalized = normalize_reverse_result(
        crossings.reverse(instr, None, target, CrossingContext(snapshot=snapshot))
    )
    if normalized.fallthrough or normalized.contradiction or normalized.trivial:
        return [atom]
    branches = normalized.branches
    if len(branches) != 1 or len(branches[0]) != 1 or not isinstance(branches[0][0], Cmp):
        return [atom]

    constraint = branches[0][0]
    form = _OP_TO_FORM.get(constraint.op)
    if form is None:
        return [atom]
    return _rewrite_internal_compare(
        Atom(
            tag=constraint.tag,
            form=form,
            operand=constraint.bound,
            operand_is_tag=constraint.bound_is_tag,
        ),
        steerable,
        pdg,
        program,
        snapshot,
        _depth=_depth + 1,
    )


def _lever_note(requirement: Atom, original: Atom, tag: str, value: Any, marker: str = "") -> str:
    requirement_text = _atom_text(requirement)
    original_text = _atom_text(original)
    body = (
        requirement_text
        if requirement._key() == original._key()
        else f"{requirement_text} to satisfy {original_text}"
    )
    note = f"held {body} (e.g., {tag} = {value!r}"
    if marker:
        note += f"; {marker}"
    return note + ")"


def _written_value_satisfying(
    atom: Atom,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> Any | None:
    """The value a writer of this tag writes that satisfies the comparison, if any.

    A writer that adds a constant to the tag itself counts too.  Its candidate is the first
    value on the count from the current one that meets the comparison.  Tracing that value
    back reaches the current one a write at a time.
    """

    if program is None:
        return None
    threshold = _resolved_atom_operand(atom, snapshot)
    if threshold is None:
        return None
    written = []
    for index in sorted(pdg.writers_of.get(atom.tag, ())):
        rung = resolve_rung(program, pdg.rung_nodes[index])
        value = _written_value_for_tag(rung, atom.tag) if rung is not None else None
        if isinstance(value, Literal):
            written.append(value.value)
        elif isinstance(value, Affine) and value.source == atom.tag and value.scale == 1:
            counted = _counted_value_satisfying(atom, threshold, snapshot, value.offset, pdg)
            if counted is not None:
                written.append(counted)
    if atom.form == "ne":
        return next((value for value in written if not _values_match(value, threshold)), None)
    return _satisfying_value(atom.form, threshold, tuple(written)) if written else None


_HOLDS = {**_CMP_OPS, "ne": lambda value, bound: value != bound}


def _counted_value_satisfying(
    atom: Atom, threshold: Any, snapshot: dict[str, Any], step: Any, pdg: ProgramGraph
) -> Any | None:
    """The first value of ``now + n * step`` (n >= 1) that satisfies *atom*, if one is in range."""

    now: Any = snapshot.get(atom.tag)
    numbers = (now, threshold, step)
    if any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in numbers) or not step:
        return None
    holds = _HOLDS.get(atom.form)
    if holds is None:
        return None
    if atom.form == "ne":
        n = 1
    elif (step > 0) != (atom.form in ("gt", "ge")):
        return None
    else:
        n = max(1, math.ceil((threshold - now) / step))
    # Float rounding can leave the first guess one step short.
    for count in (n, n + 1, n + 2):
        value = now + count * step
        if holds(value, threshold):
            tag = pdg.tags.get(atom.tag)
            return value if stored_value_possible(getattr(tag, "type", None), value) else None
    return None


def _inequality_levers(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph,
    prior: _trace_read.DomainPrior | None,
    program: Any = None,
) -> list[_Lever]:
    """Return actionable left/right levers for one scalar inequality."""

    levers: list[_Lever] = []
    seen: set[str] = set()

    def actionable(tag: str) -> bool:
        return tag in steerable or bool(pdg.writers_of.get(tag))

    def add(label: str, requirement: Atom) -> None:
        heuristic = False
        marker = ""
        target = _resolve_inequality_target(requirement, snapshot, prior, pdg)
        if target is None:
            hit = _heuristic_inequality_target(requirement, snapshot, steerable, pdg)
            if hit is not None:
                value, marker = hit
                target = (requirement.tag, value)
                heuristic = True
            else:
                # The domain prior only keeps what the program reads, and a goal can name a
                # tag nothing reads.  What its writers write says the same thing.
                value = _written_value_satisfying(requirement, snapshot, pdg, program)
                if value is None:
                    return
                target = (requirement.tag, value)
        tag, value = target
        if tag in seen or not actionable(tag):
            return
        seen.add(tag)
        levers.append(
            _Lever(
                label,
                tag,
                value,
                heuristic=heuristic,
                note=_lever_note(requirement, atom, tag, value, marker),
            )
        )

    def rewrite(requirement: Atom) -> list[Atom]:
        if program is None:
            return [requirement]
        return _rewrite_internal_compare(requirement, steerable, pdg, program, snapshot)

    base = rewrite(atom)
    operand = atom.operand
    if atom.operand_is_tag and program is not None:
        seen_atoms = {item._key() for item in base}
        frozen: list[Atom] = []
        raw_threshold = snapshot.get(operand)
        try:
            threshold = (
                atom.operand_scale * raw_threshold + atom.operand_offset
                if raw_threshold is not None
                else None
            )
        except TypeError:
            threshold = None
        if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
            frozen.extend(rewrite(Atom(tag=atom.tag, form=atom.form, operand=threshold)))
        lhs_now = snapshot.get(atom.tag)
        if (
            atom.form in _FLIP_FORM
            and atom.operand_scale != 0
            and isinstance(lhs_now, (int, float))
            and not isinstance(lhs_now, bool)
        ):
            right_form = _FLIP_FORM[atom.form] if atom.operand_scale > 0 else atom.form
            right_bound = (lhs_now - atom.operand_offset) / atom.operand_scale
            frozen.extend(rewrite(Atom(tag=operand, form=right_form, operand=right_bound)))
        for requirement in frozen:
            if requirement._key() not in seen_atoms:
                seen_atoms.add(requirement._key())
                base.append(requirement)

    for requirement in base:
        add("left", requirement)
        if (
            requirement.operand_is_tag
            and requirement.form in _FLIP_FORM
            and requirement.operand_scale != 0
        ):
            right_form = (
                _FLIP_FORM[requirement.form] if requirement.operand_scale > 0 else requirement.form
            )
            add(
                "right",
                Atom(
                    tag=requirement.operand,
                    form=right_form,
                    operand=requirement.tag,
                    operand_is_tag=True,
                    operand_scale=1 / requirement.operand_scale,
                    operand_offset=-requirement.operand_offset / requirement.operand_scale,
                ),
            )
    return levers


def _constraint_atom(constraint: Constraint) -> Atom | None:
    """Render an advance boundary in the trace predicate language."""

    if isinstance(constraint, Eq):
        if len(constraint.values) != 1:
            return None
        return Atom(constraint.tag, "eq", next(iter(constraint.values)))
    if isinstance(constraint, Cmp):
        form = {
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            "<=": "le",
            ">": "gt",
            ">=": "ge",
        }.get(constraint.op, constraint.op)
        return Atom(
            constraint.tag,
            form,
            constraint.bound,
            operand_is_tag=constraint.bound_is_tag,
        )
    if isinstance(constraint, AffineCmp):
        form = {
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            "<=": "le",
            ">": "gt",
            ">=": "ge",
        }.get(constraint.op, constraint.op)
        return Atom(
            constraint.tag,
            form,
            constraint.bound_tag,
            operand_is_tag=True,
            operand_scale=constraint.scale,
            operand_offset=constraint.offset,
        )
    return None
