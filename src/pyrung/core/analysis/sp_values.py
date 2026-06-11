"""Static value extraction from SP-trees and writer instructions.

Shared, dependency-light helpers for reading concrete tag values out of a
program's structure: what values a condition expression requires
(`_extract_required_values`, `_extract_condition_values`) and what value a
rung writes to a tag (`_written_value_for_tag`, `_has_arithmetic_writer`).

Consumed by the corridor walker (``analysis/walk``) as candidate-generation
priors and by the prover's heuristic seeding (``analysis/prove/seeding``).
Everything here is static analysis used as a *prior* — never
correctness-bearing on its own.

History: these began life in ``prove/waypoints.py`` (the legacy ``how()``
waypoint planner, deleted when the corridor walker became the sole path)
and moved here as the neutral home both subsystems can import.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import ProgramGraph, resolve_rung
from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or


def _scalar_eq(a: Any, b: Any) -> bool:
    """Equality check safe against Tag/IndirectRef overloaded __eq__."""
    result = a == b
    return result is True


def _extract_required_values(
    expr: Expr,
    snapshot: dict[str, Any],
) -> list[tuple[str, Any]] | None:
    """Extract ``(tag_name, required_value)`` pairs from *expr*.

    Returns ``None`` when the expression contains forms that cannot be
    inverted to a concrete required value (rise/fall/truthy, complex
    disjunctions where branch selection isn't obvious).
    """
    if isinstance(expr, Const):
        return []

    if isinstance(expr, Atom):
        return _required_from_atom(expr)

    if isinstance(expr, And):
        pairs: list[tuple[str, Any]] = []
        for term in expr.terms:
            sub = _extract_required_values(term, snapshot)
            if sub is None:
                return None
            pairs.extend(sub)
        return pairs

    if isinstance(expr, Or):
        best: list[tuple[str, Any]] | None = None
        best_cost = float("inf")
        for term in expr.terms:
            sub = _extract_required_values(term, snapshot)
            if sub is None:
                continue
            cost = sum(1 for tag, val in sub if not _scalar_eq(snapshot.get(tag), val))
            if cost < best_cost:
                best = sub
                best_cost = cost
        return best

    return None


def _required_from_atom(atom: Atom) -> list[tuple[str, Any]] | None:
    form = atom.form
    if form == "xic":
        return [(atom.tag, True)]
    if form == "xio":
        return [(atom.tag, False)]
    if form == "eq":
        return [(atom.tag, atom.operand)]
    if form in {"rise", "fall", "truthy"}:
        return None
    if form == "ne":
        return None
    if form in {"lt", "le", "gt", "ge"}:
        return None
    return None


def _extract_condition_values(expr: Expr) -> dict[str, frozenset[Any]]:
    """Extract invertible tag → {possible values} from *expr*.

    For And, each term contributes independently.  For Or, a tag is
    kept only when *every* branch constrains it — values are unioned.
    Rise/fall/truthy terms contribute nothing.
    """
    if isinstance(expr, Const):
        return {}
    if isinstance(expr, Atom):
        pairs = _required_from_atom(expr)
        return {t: frozenset([v]) for t, v in pairs} if pairs else {}
    if isinstance(expr, And):
        result: dict[str, frozenset[Any]] = {}
        for term in expr.terms:
            result.update(_extract_condition_values(term))
        return result
    if isinstance(expr, Or):
        per_branch: list[dict[str, frozenset[Any]]] = []
        for term in expr.terms:
            sub = _extract_condition_values(term)
            if not sub:
                return {}
            per_branch.append(sub)
        common = set(per_branch[0].keys())
        for b in per_branch[1:]:
            common &= b.keys()
        if not common:
            return {}
        result2: dict[str, frozenset[Any]] = {}
        for tag in common:
            vals: frozenset[Any] = frozenset()
            for b in per_branch:
                vals = vals | b[tag]
            result2[tag] = vals
        return result2
    return {}


def _written_value_for_tag(rung_obj: Any, tag_name: str) -> tuple[str, Any] | None:
    """Determine what a rung writes to *tag_name*.

    Returns ``("literal", value)``, ``("tag", source_name)``,
    ``("increment", step)`` for ``calc(tag + N, tag)``,
    ``("decrement", step)`` for ``calc(tag - N, tag)``,
    or ``None``.

    Sources that are neither named tags nor plain scalars — an
    ``IndirectRef`` (``block[pointer]``), whose comparison operators build
    deferred Conditions — are not statically resolvable and return
    ``None``; classifying one as a "literal" hands consumers a value that
    raises on any ``==``/``!=``.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in rung_obj._instructions:
        if isinstance(instr, CopyInstruction):
            dest = instr.dest
            if getattr(dest, "name", None) != tag_name:
                continue
            src = instr.source
            if hasattr(src, "name"):
                if getattr(src, "readonly", False):
                    return ("literal", src.default)
                return ("tag", src.name)
            if isinstance(src, (bool, int, float, str)):
                return ("literal", src)
            return None

        if isinstance(instr, CalcInstruction):
            if getattr(instr.dest, "name", None) != tag_name:
                continue
            result = _detect_arithmetic_pattern(instr.expression, tag_name)
            if result is not None:
                return result
            return None

        if isinstance(instr, FillInstruction):
            dest = instr.dest
            dest_names = set()
            if hasattr(dest, "tags"):
                dest_names = {getattr(t, "name", None) for t in dest.tags()}
            if tag_name in dest_names:
                val = instr.value
                if hasattr(val, "name"):
                    return ("tag", val.name)
                if isinstance(val, (bool, int, float, str)):
                    return ("literal", val)
                return None

        if isinstance(instr, LatchInstruction):
            if getattr(instr.target, "name", None) == tag_name:
                return ("literal", True)

        if isinstance(instr, ResetInstruction):
            if getattr(instr.target, "name", None) == tag_name:
                return ("literal", False)

    return None


def _detect_arithmetic_pattern(
    expression: Any,
    tag_name: str,
) -> tuple[str, Any] | None:
    """Detect ``tag + N`` or ``tag - N`` patterns in a calc expression."""
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr

    if not isinstance(expression, BinaryExpr):
        return None

    op_symbol = expression.symbol

    if op_symbol == "+":
        if (
            isinstance(expression.left, TagExpr)
            and getattr(expression.left.tag, "name", None) == tag_name
            and isinstance(expression.right, LiteralExpr)
        ):
            return ("increment", expression.right.value)
        if (
            isinstance(expression.right, TagExpr)
            and getattr(expression.right.tag, "name", None) == tag_name
            and isinstance(expression.left, LiteralExpr)
        ):
            return ("increment", expression.left.value)

    if op_symbol == "-":
        if (
            isinstance(expression.left, TagExpr)
            and getattr(expression.left.tag, "name", None) == tag_name
            and isinstance(expression.right, LiteralExpr)
        ):
            return ("decrement", expression.right.value)

    return None


def _has_arithmetic_writer(tag_name: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when some writer increments/decrements *tag_name* (a ±1 counter).

    Marks tags whose value path can be walked one step at a time through their
    domain — the precondition for domain-stepping decomposition.
    """
    for ri in pdg.writers_of.get(tag_name, frozenset()):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None:
            wv = _written_value_for_tag(ro, tag_name)
            if wv is not None and wv[0] in ("increment", "decrement"):
                return True
    return False
