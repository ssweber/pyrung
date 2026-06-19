"""Static value extraction from SP-trees and writer instructions.

Shared, dependency-light helpers for reading concrete tag values out of a
program's structure: what values a condition expression requires
(`_extract_required_values`, `_extract_condition_values`) and what value a
rung writes to a tag (`_written_value_for_tag`, `_has_arithmetic_writer`).

Consumed by the corridor walker (``analysis/walk``) as candidate-generation
priors, by the prover's heuristic seeding (``analysis/prove/seeding``), and
by causal analysis (``analysis/causal``) for projected-relation moves.
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


def _values_match(a: Any, b: Any) -> bool:
    """Loose equality for tag values (``1 == True``, ``0 == False``).

    Objects whose ``==`` builds a deferred Condition (an ``IndirectRef``
    leaking out of static extraction) raise on truth-testing; they cannot
    match a concrete value, so that is a non-match, never a crash.
    """
    if a is b:
        return True
    try:
        if a == b:
            return True
    except TypeError:
        return False
    return False


# Comparison operators shared by the inequality-resolution helpers.
_CMP_OPS: dict[str, Any] = {
    "gt": lambda v, o: v > o,
    "ge": lambda v, o: v >= o,
    "lt": lambda v, o: v < o,
    "le": lambda v, o: v <= o,
}

# Cap on index-register candidates enumerated when chasing an indirect copy
# source (idx-chasing): candidates come from the index's literal writes, its
# pipeline domain, and its current value, so the cap only guards degenerate
# programs that write hundreds of distinct literals to one register.
_IDX_CHASE_CAP = 64


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


def _written_value_for_tag(rung_obj: Any, tag_name: str) -> Any:
    """Forward-classify what *rung_obj* writes to *tag_name*.

    Returns ``Literal(value)``, ``Affine(source, scale, offset)``, or
    :data:`~pyrung.core.crossing.UNKNOWN`.
    """
    from pyrung.core.analysis import crossings as _crossings
    from pyrung.core.crossing import UNKNOWN, CrossingContext

    instr = _writer_for_tag(rung_obj, tag_name)
    if instr is None:
        return UNKNOWN
    return _crossings.forward(instr, CrossingContext())


def _writer_for_tag(rung_obj: Any, tag_name: str) -> Any | None:
    """The first instruction in *rung_obj* that writes *tag_name* (via ``_writes``)."""
    if rung_obj is None:
        return None
    for instr in getattr(rung_obj, "_instructions", ()):
        for field in getattr(instr, "_writes", ()):
            obj = getattr(instr, field, None)
            if getattr(obj, "name", None) == tag_name:
                return instr
            tags_fn = getattr(obj, "tags", None)
            if tags_fn is not None:
                try:
                    if any(getattr(t, "name", None) == tag_name for t in tags_fn()):
                        return instr
                except (TypeError, IndexError):
                    pass
            if isinstance(obj, (tuple, list)) and any(
                getattr(t, "name", None) == tag_name for t in obj
            ):
                return instr
    return None


def _copy_writer_for_tag(rung_obj: Any, tag_name: str) -> Any | None:
    """The first copy/fill instruction in the rung that writes *tag_name*."""
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in rung_obj._instructions:
        if isinstance(instr, CopyInstruction) and getattr(instr.dest, "name", None) == tag_name:
            return instr
        if isinstance(instr, FillInstruction):
            tags_fn = getattr(instr.dest, "tags", None)
            if tags_fn is not None and any(getattr(t, "name", None) == tag_name for t in tags_fn()):
                return instr
    return None


def copy_source_binding(rung_obj: Any, tag_name: str, value: Any) -> tuple[str, Any] | None:
    """The data-flow half of a copy writer of *tag_name*, via the crossings registry.

    When the rung writes *tag_name* via ``copy(src, tag_name)`` with *src* a
    distinct named tag, that source reaching *value* is a prerequisite — as much
    a part of the regression as the rung's gate (a state register written only
    by ``copy(Requested, Current)`` carries no literal to match).  Returns
    ``(src, value)``; or ``None`` for a literal / self / arithmetic / indirect
    source (those carry no distinct copy-source sub-goal).

    The copy-source reverse now lives in the projected registry
    (``crossings.reverse`` -> ``CopyCrossing``); this finds the copy/fill writer
    and extracts the single named-source ``Eq`` it implies.  A clamp-rail target
    (which the registry returns as a ``Cmp`` range, not a singleton) yields
    ``None`` — there is no single source value to bind.
    """
    from pyrung.core.analysis import crossings
    from pyrung.core.crossing import CrossingContext, Eq, eq_target

    instr = _copy_writer_for_tag(rung_obj, tag_name)
    if instr is None:
        return None
    result = crossings.reverse(instr, rung_obj, eq_target(tag_name, value), CrossingContext())
    if result.fallthrough or len(result.branches) != 1:
        return None
    (branch,) = result.branches
    if len(branch) != 1:
        return None
    constraint = branch[0]
    if isinstance(constraint, Eq) and len(constraint.values) == 1 and constraint.tag != tag_name:
        return (constraint.tag, next(iter(constraint.values)))
    return None


def _named_copy_source(instr: Any) -> str | None:
    """Source name when *instr* is a copy-from-named-non-readonly-tag, else ``None``."""
    from pyrung.core.instruction.data_transfer import CopyInstruction

    src = (
        getattr(instr, "source", None)
        if isinstance(instr, CopyInstruction)
        else getattr(instr, "value", None)
    )
    if src is None:
        return None
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    if isinstance(src, (IndirectRef, IndirectExprRef)):
        return None
    name = getattr(src, "name", None)
    if name is not None and not getattr(src, "readonly", False):
        return name
    return None


def _has_arithmetic_writer(tag_name: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when some writer increments/decrements *tag_name* (a ±1 counter).

    Marks tags whose value path can be walked one step at a time through their
    domain — the precondition for domain-stepping decomposition.
    """
    from pyrung.core.crossing import Affine

    for ri in pdg.writers_of.get(tag_name, frozenset()):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None:
            if isinstance(_written_value_for_tag(ro, tag_name), Affine):
                return True
    return False


class _SnapshotView:
    """Minimal ``ScanContext`` stand-in: evaluate an index ``Expression``
    against a tag snapshot with a candidate-index overlay."""

    __slots__ = ("_snapshot", "_overlay")

    def __init__(self, snapshot: dict[str, Any], overlay: dict[str, Any]):
        self._snapshot = snapshot
        self._overlay = overlay

    def get_tag(self, name: str, default: Any = None) -> Any:
        if name in self._overlay:
            return self._overlay[name]
        return self._snapshot.get(name, default)


def _expr_tag_names(expr: Any) -> set[str] | None:
    """Distinct tag names an index ``Expression`` reads.

    ``None`` for shapes the chase can't reason about (block sums, unknown
    nodes) — the conservative refusal, same direction as the pre-chase
    behavior.
    """
    from pyrung.core.expression import (
        BinaryExpr,
        LiteralExpr,
        MathFuncExpr,
        ShiftFuncExpr,
        TagExpr,
        UnaryExpr,
    )

    if isinstance(expr, TagExpr):
        return {expr.tag.name}
    if isinstance(expr, LiteralExpr):
        return set()
    if isinstance(expr, BinaryExpr):
        left = _expr_tag_names(expr.left)
        right = _expr_tag_names(expr.right)
        if left is None or right is None:
            return None
        return left | right
    if isinstance(expr, (UnaryExpr, MathFuncExpr)):
        return _expr_tag_names(expr.operand)
    if isinstance(expr, ShiftFuncExpr):
        value = _expr_tag_names(expr.value)
        count = _expr_tag_names(expr.count)
        if value is None or count is None:
            return None
        return value | count
    return None


def _satisfying_value(form: str, operand: Any, domain: tuple[Any, ...]) -> Any | None:
    """Pick the smallest domain value satisfying the comparison, or ``None``."""
    op = _CMP_OPS.get(form)
    if op is None:
        return None
    try:
        candidates = sorted(
            domain,
            key=lambda x: (
                abs(x - operand)
                if isinstance(x, (int, float)) and isinstance(operand, (int, float))
                else 0
            ),
        )
    except TypeError:
        candidates = list(domain)
    for v in candidates:
        try:
            if op(v, operand):
                return v
        except TypeError:
            continue
    return None


_FLIP_FORM = {"lt": "gt", "le": "ge", "gt": "lt", "ge": "le"}


def _chase_inequality_source(
    tag: str,
    form: str,
    threshold: Any,
    nd_domains: dict[str, tuple[Any, ...]],
    func_deps: dict[str, tuple[str, int, Any]] | None,
) -> tuple[str, Any] | None:
    """Resolve ``tag cmp threshold`` to a steerable source and value.

    Identity when *tag* has a pipeline domain with a satisfying value;
    otherwise hop through the affine functional-dep projections
    (``tag = scale*src + offset``), rewriting the comparison onto the
    source — flipping the form when scale is -1 (``pv = 100 - analog``
    turns ``pv < L`` into ``analog > 100 - L``) — until a tag with a
    domain is reached (3-hop bound, the idx-chase convention).
    """
    for _ in range(4):
        domain = nd_domains.get(tag)
        if domain is not None:
            val = _satisfying_value(form, threshold, domain)
            if val is None:
                return None
            return tag, val
        dep = func_deps.get(tag) if func_deps else None
        if dep is None or dep[0] == tag:
            return None
        src, scale, offset = dep
        try:
            if scale == 1:
                threshold = threshold - offset
            else:
                threshold = offset - threshold
                form = _FLIP_FORM[form]
        except TypeError:
            return None
        tag = src
    return None


def _sole_calc_expression(tag: str, pdg: ProgramGraph, program: Any) -> tuple[Any, set[str]] | None:
    """``(expression, source_names)`` when *tag*'s sole writer is a calc.

    Any-arity sibling of :func:`_single_calc_definition` — the inequality
    operand chase needs the full expression (``lower = setpoint - band``)
    so it can freeze all-but-one source at the snapshot.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag:
            names = _expr_tag_names(instr.expression)
            if names and tag not in names:
                return instr.expression, set(names)
            return None
    return None


def _producible_values(
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> list[Any]:
    """Numeric values the program's own writers can put into *tag*.

    Literal writes plus copy-from-tag writers' *source snapshot values*
    (the data-flow half — the analog sibling of :func:`_index_candidates`,
    floats included: ``copy(pv_LevelHt, sv_levelSetPoint)`` gated on a
    tare button can produce the snapshot's pv).
    """
    out: list[Any] = []
    seen_v: set[Any] = set()
    for ri in sorted(pdg.writers_of.get(tag, frozenset())):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if wv is None:
            continue
        if wv[0] == "literal":
            v = wv[1]
        elif wv[0] == "tag" and wv[1] != tag:
            v = snapshot.get(wv[1])
        else:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v not in seen_v:
            seen_v.add(v)
            out.append(v)
    return out[:_IDX_CHASE_CAP]


def _extract_inequality_prereqs(
    expr: Any,
    snapshot: dict[str, Any],
    nd_domains: dict[str, tuple[Any, ...]] | None,
    pdg: ProgramGraph,
    program: Any = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
) -> list[tuple[str, Any]]:
    """Extract ``(tag, satisfying_value)`` pairs from inequality atoms.

    Complements ``_extract_condition_values`` which handles eq/xic/xio but
    drops gt/ge/lt/le.  Uses pipeline domains to pick a concrete satisfying
    value for each inequality; chases domain-less compare tags through the
    affine functional-dep projections to their steerable source.
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Or

    if not nd_domains:
        return []

    result: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def _visit(e: Any) -> None:
        if isinstance(e, Atom) and e.form in ("gt", "ge", "lt", "le"):
            tag = e.tag
            if tag in seen:
                return
            # operand may be a literal (int/float) or a tag name (str reference)
            operand = e.operand
            operand_tag = operand if isinstance(operand, str) else None
            if operand_tag is not None:
                operand = snapshot.get(operand_tag, 0)
            current = snapshot.get(tag)
            op = _CMP_OPS[e.form]
            try:
                if current is not None and op(current, operand):
                    return
            except TypeError:
                pass
            hit = _chase_inequality_source(tag, e.form, operand, nd_domains, func_deps)
            if hit is not None:
                src, val = hit
                if src not in seen:
                    seen.add(src)
                    result.append((src, val))
                return
        elif isinstance(e, ArithAtom) and e.form in ("gt", "ge", "lt", "le"):
            for operand_tag in (e.left, e.right):
                if operand_tag in seen:
                    continue
                domain = nd_domains.get(operand_tag)
                if domain is None:
                    continue
                other = e.right if operand_tag == e.left else e.left
                other_val = snapshot.get(other)
                if other_val is None:
                    val = _satisfying_value(e.form, e.operand, domain)
                    if val is not None:
                        seen.add(operand_tag)
                        result.append((operand_tag, val))
                    continue
                # Solve for the operand: (operand_tag op other_val) cmp threshold
                try:
                    if e.arith_op == "+":
                        if operand_tag == e.left:
                            needed = e.operand - other_val
                        else:
                            needed = e.operand - other_val
                    elif e.arith_op == "-":
                        if operand_tag == e.left:
                            needed = e.operand + other_val
                        else:
                            needed = other_val - e.operand
                    elif e.arith_op == "*" and other_val != 0:
                        needed = e.operand / other_val
                    else:
                        needed = e.operand
                    val = _satisfying_value(e.form, needed, domain)
                except (TypeError, ZeroDivisionError):
                    val = _satisfying_value(e.form, e.operand, domain)
                if val is not None:
                    seen.add(operand_tag)
                    result.append((operand_tag, val))
        elif isinstance(e, And):
            for term in e.terms:
                _visit(term)
        elif isinstance(e, Or):
            for term in e.terms:
                _visit(term)

    _visit(expr)
    return result
