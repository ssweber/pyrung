"""Low-level static-expression helpers shared by trace and tide readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.simplified import And, Atom, Or
from pyrung.core.analysis.sp_values import (
    _FLIP_FORM,
    _chase_inequality_source,
    _expr_tag_names,
    _required_from_atom,
    _satisfying_value,
    _values_match,
    _written_value_for_tag,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

_INDEX_CHASE_CAP = 32
_REAL_STRICT_EPSILON = 1e-6
_FORM_SYMBOL = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!="}


def simplified_expr_tags(expr: Any) -> set[str]:
    """Tag names referenced by a simplified expression."""

    if isinstance(expr, Atom):
        tags = {expr.tag}
        if expr.operand_is_tag:
            tags.add(expr.operand)
        return tags
    if isinstance(expr, (And, Or)):
        return set().union(*(simplified_expr_tags(term) for term in expr.terms))
    return set()


def _domain_granularity(domain: tuple[Any, ...]) -> Any:
    """Smallest positive spacing in a finite numeric domain."""

    nums = sorted(v for v in domain if isinstance(v, (int, float)) and not isinstance(v, bool))
    diffs = [b - a for a, b in zip(nums, nums[1:], strict=False) if b > a]
    return min(diffs) if diffs else None


def _strict_inequality_step(tag: str, prior: Any, pdg: ProgramGraph | None) -> Any:
    """Amount to step past a strict inequality threshold."""

    from pyrung.core.tag import TagType

    if pdg is not None:
        tag_ref = pdg.tags.get(tag)
        if tag_ref is not None and getattr(tag_ref, "type", None) is TagType.REAL:
            return _REAL_STRICT_EPSILON
    domain = None
    if prior is not None:
        domain = (prior.nd_domains or {}).get(tag)
        if domain is None:
            domain = (prior.stateful_domains or {}).get(tag)
    if domain:
        step = _domain_granularity(domain)
        if step is not None:
            return step
    return 1


def _resolve_inequality_target(
    atom: Atom,
    snapshot: dict[str, Any],
    prior: Any = None,
    pdg: ProgramGraph | None = None,
) -> tuple[str, Any] | None:
    """Resolve an inequality to a reachable or boundary satisfying value."""

    threshold = snapshot.get(atom.operand) if atom.operand_is_tag else atom.operand
    if atom.operand_is_tag and threshold is None:
        return None

    if prior is not None and prior.nd_domains:
        hit = _chase_inequality_source(
            atom.tag,
            atom.form,
            threshold,
            prior.nd_domains,
            prior.func_deps,
        )
        if hit is not None:
            return hit
        domain = prior.nd_domains.get(atom.tag)
        if domain and atom.form in _FLIP_FORM:
            try:
                extreme = max(domain) if atom.form in ("gt", "ge") else min(domain)
            except (TypeError, ValueError):
                extreme = None
            if extreme is not None and not _values_match(snapshot.get(atom.tag), extreme):
                return (atom.tag, extreme)

    stateful_domain = (
        prior.stateful_domains.get(atom.tag)
        if prior is not None and prior.stateful_domains
        else None
    )
    if stateful_domain and atom.form in {"lt", "le", "gt", "ge"}:
        value = _satisfying_value(atom.form, threshold, stateful_domain)
        if value is not None:
            if (
                isinstance(threshold, float)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                value = float(value)
            return (atom.tag, value)

    if atom.form == "ne":
        domain = prior.nd_domains.get(atom.tag) if prior is not None and prior.nd_domains else None
        if domain:
            current = snapshot.get(atom.tag)
            alternatives = [value for value in domain if not _values_match(value, threshold)]
            pick = next(
                (value for value in alternatives if not _values_match(value, current)),
                alternatives[0] if alternatives else None,
            )
            if pick is not None:
                return (atom.tag, pick)
        return None

    if not atom.operand_is_tag:
        return None
    if atom.form in ("ge", "le"):
        return (atom.tag, threshold)
    if (
        atom.form in ("gt", "lt")
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
    ):
        step = _strict_inequality_step(atom.tag, prior, pdg)
        return (atom.tag, threshold + step if atom.form == "gt" else threshold - step)
    return None


def _declared_float_bounds(tag: str, pdg: ProgramGraph | None) -> tuple[Any, Any]:
    """Declared numeric bounds used only to clamp a heuristic proposal."""

    if pdg is None:
        return (None, None)
    ref = pdg.tags.get(tag)
    if ref is None:
        return (None, None)
    return (getattr(ref, "min", None), getattr(ref, "max", None))


def _heuristic_inequality_target(
    atom: Atom,
    snapshot: dict[str, Any],
    steerable: frozenset[str],
    pdg: ProgramGraph | None,
) -> tuple[Any, str] | None:
    """Propose an exact boundary value for a steerable numeric word."""

    if atom.form not in ("lt", "le", "gt", "ge") or atom.tag not in steerable:
        return None
    threshold = snapshot.get(atom.operand) if atom.operand_is_tag else atom.operand
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return None
    if atom.form in ("ge", "le"):
        value = threshold
    else:
        step = _strict_inequality_step(atom.tag, None, pdg)
        value = threshold + step if atom.form == "gt" else threshold - step

    lo, hi = _declared_float_bounds(atom.tag, pdg)
    clamped = value
    if lo is not None and clamped < lo:
        clamped = lo
    if hi is not None and clamped > hi:
        clamped = hi
    if clamped != value:
        satisfies = {
            "lt": clamped < threshold,
            "le": clamped <= threshold,
            "gt": clamped > threshold,
            "ge": clamped >= threshold,
        }[atom.form]
        if not satisfies and _values_match(snapshot.get(atom.tag), clamped):
            return None
        value = clamped
    return (value, "heuristic value; relation is the requirement, not this number")


def _atom_text(atom: Atom) -> str:
    """Render an inequality atom for a lever note."""

    operand = atom.operand
    rhs = operand if isinstance(operand, str) else repr(operand)
    return f"{atom.tag} {_FORM_SYMBOL.get(atom.form, atom.form)} {rhs}"


def _channel_constraint(
    expr: Any,
    channel_tag: str,
    source_aliases: dict[tuple[str, Any], tuple[str, Any]],
) -> frozenset[Any] | None:
    """Channel values satisfying an expression, or ``None`` when unconstrained."""

    if isinstance(expr, Atom):
        pairs = _required_from_atom(expr)
        if not pairs:
            return None
        values: set[Any] = set()
        for tag, value in pairs:
            if tag == channel_tag:
                values.add(value)
            else:
                alias = source_aliases.get((tag, value))
                if alias is not None and alias[0] == channel_tag:
                    values.add(alias[1])
        return frozenset(values) if values else None
    if isinstance(expr, And):
        result: frozenset[Any] | None = None
        for term in expr.terms:
            constraint = _channel_constraint(term, channel_tag, source_aliases)
            if constraint is not None:
                result = constraint if result is None else result & constraint
        return result
    if isinstance(expr, Or):
        union: frozenset[Any] = frozenset()
        for term in expr.terms:
            constraint = _channel_constraint(term, channel_tag, source_aliases)
            if constraint is None:
                return None
            union |= constraint
        return union
    return None


def _channel_from_values(
    expr: Any,
    channel_tag: str,
    source_aliases: dict[tuple[str, Any], tuple[str, Any]] | None = None,
) -> tuple[Any, ...]:
    """Channel values a writer fires from, including disjunctions and aliases."""

    constraint = _channel_constraint(expr, channel_tag, source_aliases or {})
    if not constraint:
        return ()
    try:
        return tuple(sorted(constraint))
    except TypeError:
        return tuple(constraint)


def single_calc_source(idx_tag: str, pdg: Any, program: Any) -> tuple[Any, str] | None:
    """Return ``(expression, source_tag)`` for one calc-written index."""

    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == idx_tag:
            names = _expr_tag_names(instr.expression)
            if not names:
                return None
            mutable = {name for name in names if pdg.writers_of.get(name)} - {idx_tag}
            if len(mutable) != 1:
                return None
            return instr.expression, next(iter(mutable))
    return None


def index_values(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: Any,
    program: Any,
) -> list[int]:
    """Plausible values for an index register, current value first."""

    from pyrung.core.analysis.sp_values import _named_copy_source, _writer_for_tag
    from pyrung.core.crossing import Literal

    rest: set[int] = set()
    current = snapshot.get(idx_tag)
    for ri in sorted(pdg.writers_of.get(idx_tag, frozenset())):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        written = _written_value_for_tag(ro, idx_tag)
        if isinstance(written, Literal):
            value = written.value
            if isinstance(value, int) and not isinstance(value, bool):
                rest.add(value)
            continue
        instr = _writer_for_tag(ro, idx_tag)
        source = _named_copy_source(instr) if instr is not None else None
        if source is not None and source != idx_tag:
            value = snapshot.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                rest.add(value)
    out: list[int] = []
    if isinstance(current, int) and not isinstance(current, bool):
        out.append(current)
        rest.discard(current)
    out.extend(sorted(rest))
    return out[:_INDEX_CHASE_CAP]
