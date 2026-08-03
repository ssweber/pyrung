"""Static value extraction from SP-trees and writer instructions.

Shared, dependency-light helpers for reading concrete tag values out of a
program's structure: what values a condition expression requires
(`_extract_required_values`, `_extract_condition_values`) and what value a
rung writes to a tag (`_written_value_for_tag`, `_has_arithmetic_writer`).

Consumed by PILOT's static readers, the prover's heuristic seeding, and causal
analysis for projected-relation moves. Everything here is static evidence used
as a prior; it is never correctness-bearing on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pdg import ProgramGraph, resolve_rung
from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or
from pyrung.core.analysis.sp_tree import SPLeaf, SPParallel, SPSeries, attribute
from pyrung.core.analysis.write_sites import instruction_writes_tag
from pyrung.core.crossing import (
    UNKNOWN,
    Affine,
    Literal,
    invert_affine_candidate,
)

if TYPE_CHECKING:
    from pyrung.core.condition import Condition


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


def _invert_affine(wv: Any, value: Any) -> Any | None:
    """Source value an ``Affine`` write needs to produce *value*, or ``None``.

    This is a candidate projection, not a complete preimage: a clamp rail or
    modular store can have multiple producers.  The central crossing helper
    accounts for destination storage and returns one representative that the
    interpreted fork must still verify.
    """
    if not isinstance(wv, Affine):
        return None
    candidate = invert_affine_candidate(wv, value)
    return None if candidate is UNKNOWN else candidate


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
        if atom.operand_is_tag:
            return None
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
    return _crossings.forward(instr, tag_name, CrossingContext())


def _writer_for_tag(rung_obj: Any, tag_name: str) -> Any | None:
    """The first instruction with an exact static write to *tag_name*."""
    if rung_obj is None:
        return None
    for instr in getattr(rung_obj, "_instructions", ()):
        if instruction_writes_tag(instr, tag_name):
            return instr
    return None


# ---------------------------------------------------------------------------
# Writer-value aliases (shared source-alias primitive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriterValueFact:
    """One combinational writer of *tag*: the static value it drives and its gate.

    A *combinational* writer drives a statically-known value into ``tag`` every
    scan its rung is true — either an OTE bit coil (``out(tag)`` → ``written_value``
    is ``True``, ``is_ote_bit`` True) or a constant move (``copy(5, tag)`` /
    ``fill(0, ...)`` → ``written_value`` is the literal, ``is_ote_bit`` False).
    Stateful writers (latch/reset held value, timer, counter, affine or opaque
    copy) are *not* facts: their tag value is not a static function of the rung
    condition, so they are omitted — except reset/latch, whose ``forward`` yields
    a held ``Literal`` and so appear as non-OTE facts (a consumer that needs pure
    combinational aliasing filters on ``is_ote_bit``).

    ``conditions`` is the writer rung's full condition chain (raw ``Condition``
    objects, parent branch guards included).  ``cond_values`` is the invertible
    ``tag → {values}`` projection of that chain (see ``_extract_condition_values``).

    The neutral primitive behind both PILOT's source-alias recognition (project by
    a channel register: ``S_ProductionMode=True`` *means* ``S_UnitModeCurrent==1``)
    and the conflicting-output validator's one-hot mutual-exclusivity reasoning.
    """

    tag: str
    node_index: int
    written_value: Any
    is_ote_bit: bool
    conditions: tuple[Condition, ...]
    cond_values: dict[str, frozenset[Any]]


def writer_value_facts(program: Any, pdg: ProgramGraph) -> dict[str, tuple[WriterValueFact, ...]]:
    """Map every tag to its combinational :class:`WriterValueFact` writers.

    Memoized on the program graph — pure in ``(program, pdg)``, both stable across
    a run.  Tags with no combinational writer are absent from the map.
    """
    cache = getattr(pdg, "_writer_value_facts_cache", None)
    if cache is not None:
        return cast("dict[str, tuple[WriterValueFact, ...]]", cache)

    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.instruction.coils import OutInstruction

    facts: dict[str, list[WriterValueFact]] = {}
    for tag_name, writers in pdg.writers_of.items():
        for node_idx in writers:
            rung_obj = resolve_rung(program, pdg.rung_nodes[node_idx])
            if rung_obj is None:
                continue
            written = _written_value_for_tag(rung_obj, tag_name)
            if isinstance(written, Literal):
                written_value: Any = written.value
                is_ote_bit = False
            else:
                instr = _writer_for_tag(rung_obj, tag_name)
                if not (
                    isinstance(instr, OutInstruction)
                    and getattr(instr.target, "name", None) == tag_name
                    and not getattr(instr, "_oneshot", False)
                ):
                    continue
                written_value = True
                is_ote_bit = True
            sp = rung_obj.sp_tree()
            cond_values = _extract_condition_values(_sp_to_expr(sp)) if sp is not None else {}
            facts.setdefault(tag_name, []).append(
                WriterValueFact(
                    tag=tag_name,
                    node_index=node_idx,
                    written_value=written_value,
                    is_ote_bit=is_ote_bit,
                    conditions=tuple(getattr(rung_obj, "_conditions", ())),
                    cond_values=cond_values,
                )
            )

    result = {tag: tuple(v) for tag, v in facts.items()}
    object.__setattr__(pdg, "_writer_value_facts_cache", result)
    return result


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
            operand_tag = operand if e.operand_is_tag else None
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


# ---------------------------------------------------------------------------
# Projected-oracle substrate (shared by pilot.trace, causal.projected, and
# prove.classify domain bounding).
#
# A tag is *pinned* when a consumer is committed to its value: a held one-hot
# state peer, or a self-referential affine source (``calc(CurStep+1, CurStep)``
# -> ``CurStep == 1`` to produce ``CurStep == 2``) plus its one-hop-derived
# tags (``valstepisodd = CurStep % 2 = 1``).  Evaluating a writer's guard in the
# pinned prerequisite state makes writer selection fall out natively: a FALSE
# guard leaf on a pinned tag means the writer is counterfactual — it can never
# fire from here.
#
# This is pure mechanism.  Each consumer owns the SOUNDNESS of the pins it
# passes: the pilot pins heuristically (fine — it replans); the verifier may
# pass only proven-invariant pins (the affine source + one-hop derive are exact
# prerequisites; a one-hot family needs a verified mutual-exclusion fact).
# ---------------------------------------------------------------------------


def _condition_tag_name(condition: Condition) -> str | None:
    """Extract the primary tag name from a leaf condition, or None."""
    tag = getattr(condition, "tag", None)
    if tag is None:
        return None
    # Handle ImmediateRef wrapping (check class name to avoid triggering
    # Tag.value property which requires an active runner)
    from pyrung.core.tag import ImmediateRef

    if isinstance(tag, ImmediateRef):
        inner = object.__getattribute__(tag, "value")
        return getattr(inner, "name", None)
    return getattr(tag, "name", None)


class _ProjectedView:
    """``ScanContext`` stand-in for ``cond.evaluate``: reads go overlay->snapshot."""

    __slots__ = ("_snap", "_overlay")

    def __init__(self, snap: dict[str, Any], overlay: dict[str, Any]) -> None:
        self._snap = snap
        self._overlay = overlay

    def get_tag(self, name: str, default: Any = None) -> Any:
        v = self._overlay[name] if name in self._overlay else self._snap.get(name, default)
        return v if v is not None else default

    def get_memory(self, key: str, default: Any = None) -> Any:
        return default


def _derive_one_hop(
    overlay: dict[str, Any],
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, Any]:
    """Partial-eval one hop: recompute calc tags that depend on the overlay.

    A writer guard may read a tag (``valstepisodd``) *derived* from a tag we
    are projecting (``CurStep``).  Recompute those derived values from the
    overlay so the guard is evaluated in the prerequisite state, not the
    current snapshot.  Exact partial evaluation — always sound.
    """
    from pyrung.core.instruction.calc import CalcInstruction

    out = dict(overlay)
    view = _ProjectedView(snapshot, out)
    for rn in pdg.rung_nodes:
        ro = resolve_rung(program, rn)
        if ro is None:
            continue
        for instr in ro._instructions:
            if not isinstance(instr, CalcInstruction):
                continue
            dest = getattr(instr.dest, "name", None)
            if dest is None or dest in out:
                continue
            names = _expr_tag_names(instr.expression)
            if not names or not (names & overlay.keys()):
                continue
            try:
                out[dest] = instr.expression.evaluate(cast(Any, view))
            except Exception:
                continue
    return out


def projected_writer_overlay(
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    pinned_overlay: dict[str, Any],
) -> tuple[dict[str, Any], set[str]] | None:
    """Projected overlay + pinned set for a candidate writer of ``(tag, value)``.

    Returns ``(overlay, local_pinned)``, or ``None`` when the writer cannot
    produce ``value`` at all.  For a self-referential affine write the overlay
    pins the source value and its one-hop-derived tags; otherwise it is just the
    held ``pinned_overlay``.
    """
    wv = _written_value_for_tag(ro, tag)
    overlay = dict(pinned_overlay)
    local_pinned = set(pinned_overlay)
    if isinstance(wv, Literal):
        if not _values_match(wv.value, value):
            return None
    elif isinstance(wv, Affine):
        src_val = _invert_affine(wv, value)
        if src_val is None:
            return None
        if wv.source == tag:
            overlay[tag] = src_val
            local_pinned.add(tag)
            overlay = _derive_one_hop(overlay, snapshot, pdg, program)
            local_pinned |= {k for k in overlay if k not in pinned_overlay}
    else:
        return None  # UNKNOWN write — no static projection
    return overlay, local_pinned


def _writer_projection(
    ro: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    pinned_overlay: dict[str, Any],
    pinned: frozenset[str],
) -> tuple[bool, list[str]] | None:
    """``(counterfactual, frontier)`` for a candidate writer (pilot ranking).

    ``counterfactual`` — a FALSE guard leaf reads a pinned tag.  ``frontier`` —
    the non-pinned FALSE guard leaves (the real prerequisites).  ``None`` when
    the writer cannot produce ``value``.

    Notion **#2** of three "what's still needed" — per-writer, evaluated in the
    PROJECTED fire-time overlay, answering *"dead branch? + what prereqs remain?"*.
    Its ``counterfactual`` feeds #3 ``_writer_availability``'s ``is_counterfactual``;
    its non-pinned frontier tags resurface in #1 ``frontier_pairs`` one recursion
    level down. See ``pilot/CLAUDE.md`` "Soundness and behavior invariants".
    """
    built = projected_writer_overlay(ro, tag, value, snapshot, pdg, program, dict(pinned_overlay))
    if built is None:
        return None
    overlay, local_pinned = built
    local_pinned |= set(pinned)
    sp = ro.sp_tree()
    if sp is None:
        return (False, [])
    view = _ProjectedView(snapshot, overlay)

    def _eval(cond: Condition) -> bool:
        return bool(cond.evaluate(cast(Any, view)))

    counterfactual, frontier = _projected_guard_frontier(sp, _eval, local_pinned)
    return (counterfactual, list(frontier))


def _projected_guard_frontier(
    node: Any,
    evaluate: Any,
    local_pinned: set[str],
) -> tuple[bool, tuple[str, ...]]:
    """Path-sensitive ``(counterfactual, frontier)`` for an SP guard.

    ``attribute()`` flattens the false leaves that mattered to the whole guard.
    That is correct for conjunctions, but too blunt for disjunctions: one
    pinned-false OR arm must not make a sibling arm counterfactual.  Walk the SP
    structure directly so ``Or(pinned_false, live_frontier)`` keeps the live arm.
    """
    if isinstance(node, SPLeaf):
        try:
            holds = bool(evaluate(node.condition))
        except Exception:
            holds = False
        if holds:
            return (False, ())
        tag = _condition_tag_name(node.condition)
        if tag is not None and tag in local_pinned:
            return (True, ())
        return (False, (tag,)) if tag is not None else (False, ())

    if isinstance(node, SPSeries):
        out: list[str] = []
        counterfactual = False
        for child in node.children:
            child_counterfactual, child_frontier = _projected_guard_frontier(
                child, evaluate, local_pinned
            )
            counterfactual = counterfactual or child_counterfactual
            out.extend(child_frontier)
        return (counterfactual, tuple(dict.fromkeys(out)))

    if isinstance(node, SPParallel):
        choices: list[tuple[bool, tuple[str, ...]]] = [
            _projected_guard_frontier(child, evaluate, local_pinned) for child in node.children
        ]
        live: list[tuple[str, ...]] = [
            frontier for counterfactual, frontier in choices if not counterfactual
        ]
        if not live:
            out: list[str] = []
            for _counterfactual, frontier in choices:
                out.extend(frontier)
            return (True, tuple(dict.fromkeys(out)))
        # An already-true arm needs no frontier.  Otherwise choose the narrowest
        # non-counterfactual arm; trace's OR scorer will expand that frontier.
        best = min(live, key=len)
        return (False, best)

    return (False, ())


# ---------------------------------------------------------------------------
# Backward-enabler projection (the narrowing primitive prove.classify consumes)
# ---------------------------------------------------------------------------

_NEEDED_UNKNOWN = object()


def _leaf_needed(cond: Any) -> Any:
    """Concrete value a false leaf needs, for literal/Bool cases; else sentinel.

    Returns :data:`_NEEDED_UNKNOWN` when the needed value can't be resolved
    statically — callers treat that frontier as reachable (over-approximate).
    """
    from pyrung.core.condition import (
        BitCondition,
        CompareEq,
        IntTruthyCondition,
        NormallyClosedCondition,
    )
    from pyrung.core.expression import Expression
    from pyrung.core.tag import Tag

    if isinstance(cond, NormallyClosedCondition):
        return False
    if isinstance(cond, BitCondition):
        return True
    if isinstance(cond, IntTruthyCondition):
        return 1
    if isinstance(cond, CompareEq):
        v = getattr(cond, "value", None)
        if not isinstance(v, (Tag, Expression)):
            return v
    return _NEEDED_UNKNOWN


def _false_leaves_under(sp: Any, snapshot: dict[str, Any], overlay: dict[str, Any]) -> list[Any]:
    """``(tag_name, condition)`` for each FALSE guard leaf under the projection."""
    view = _ProjectedView(snapshot, overlay)

    def _eval(cond: Any, _v: Any = view) -> bool:
        return bool(cond.evaluate(_v))

    return [
        (_condition_tag_name(a.condition), a.condition) for a in attribute(sp, _eval) if not a.value
    ]


def _enabler_reachable(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    overlay: dict[str, Any],
    pinned: set[str],
    *,
    depth: int,
    seen: frozenset | None = None,
) -> bool:
    """Can ``(tag, value)`` be produced without contradicting the given pins?

    Answers a pure reachability question — it does NOT decide to shrink any
    domain; the caller does, and the caller owns the soundness of *pinned*
    (see the substrate note above).

    Returns ``False`` (unreachable) ONLY when every producing writer is
    *provably* counterfactual: a writer counts toward unreachability only if its
    write is fully classified (Literal/Affine) AND its guard has a false leaf on
    a pinned tag.  Every uncertainty resolves toward reachable, so the narrowing
    a caller derives never removes a reachable value:

    * an UNKNOWN (unclassifiable) writer -> reachable,
    * a free input (no writers) -> reachable,
    * the depth / cycle cutoff -> reachable,
    * a frontier leaf whose needed value can't be resolved -> reachable.
    """
    key = (tag, value)
    seen = seen or frozenset()
    if key in seen or depth <= 0:
        return True
    seen = seen | {key}

    writers = pdg.writers_of.get(tag, frozenset())
    if not writers:
        return True  # free input — reachable

    for ni in writers:
        node = pdg.rung_nodes[ni]
        ro = resolve_rung(program, node)
        if ro is None:
            continue
        if _written_value_for_tag(ro, tag) is UNKNOWN:
            return True  # can't classify -> can't prove counterfactual -> reachable
        built = projected_writer_overlay(ro, tag, value, snapshot, pdg, program, dict(overlay))
        if built is None:
            continue  # classified write genuinely cannot produce value (literal mismatch)
        w_overlay, w_pinned = built
        w_pinned = set(w_pinned) | pinned
        sp = ro.sp_tree()
        if sp is None:
            return True  # unconditional producer
        false_leaves = _false_leaves_under(sp, snapshot, w_overlay)
        if any(t in w_pinned for t, _c in false_leaves if t is not None):
            continue  # counterfactual under pins
        blocked = False
        for t, cond in false_leaves:
            if t is None or t in w_pinned:
                continue
            needed = _leaf_needed(cond)
            if needed is _NEEDED_UNKNOWN:
                continue  # can't resolve frontier value -> assume reachable
            if not _enabler_reachable(
                t, needed, snapshot, pdg, program, w_overlay, w_pinned, depth=depth - 1, seen=seen
            ):
                blocked = True
                break
        if not blocked:
            return True  # found a viable producer
    return False  # all producers provably counterfactual under pins
