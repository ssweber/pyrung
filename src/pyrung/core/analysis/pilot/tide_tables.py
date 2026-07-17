"""Solve finite predicates over constant-backed lookup tables.

The solvers model indirect table operands, enumerate known finite index
domains, and return satisfying assignments or calculation preimages for the
backward trace. Unmodelled live operands produce no exact solution.

Some helper paths can return plausible values for non-rejecting reads. A caller
that uses a result to prove a guard impossible must first establish complete
domains for every free tag; ``trace._writer_guard_verdict`` owns that gate.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph

logger = logging.getLogger(__name__)

# Guardrails: refuse to enumerate an unbounded/huge space (would be unsound to
# truncate).  A predicate over more free indices or a larger product than these
# is punted rather than silently sampled.
_MAX_FREE_INDICES = 3
_MAX_COMBOS = 4096

_CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class _TableOperand:
    """One expression tag whose value is ``table[eval_addr(index_tag)]``."""

    index_tag: str
    eval_addr: Any  # (index_value:int) -> address:int
    block: Any


@dataclass(frozen=True)
class PredicateSolution:
    """The satisfying assignments for the free index registers of a predicate.

    ``assignments`` is a DNF-ish list of full free-index assignments (each a
    ``{index_tag: value}`` dict).  ``per_tag`` projects them to the values each
    free index takes in *some* satisfying assignment — the shape trace surfaces
    as ``Eq(index_tag, values)`` prerequisite leaves.
    """

    free_tags: tuple[str, ...]
    assignments: tuple[dict[str, Any], ...]

    @property
    def per_tag(self) -> dict[str, list[Any]]:
        out: dict[str, set[Any]] = {t: set() for t in self.free_tags}
        for asn in self.assignments:
            for t, v in asn.items():
                out[t].add(v)
        return {t: sorted(vs) for t, vs in out.items()}


def solve_table_predicate(
    result_tag: str,
    target_value: Any,
    op: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    fixed: dict[str, Any] | None = None,
    domains: dict[str, tuple[Any, ...]] | None = None,
) -> PredicateSolution | None:
    """Solve ``expr(<operands>) <op> target_value`` for the free index registers.

    *result_tag* must be written by a single ``calc(expr, result_tag)`` whose
    expression tags each resolve to a constant, a ``fixed`` context value, or a
    constant-table lookup indexed by a register with a finite domain.  *fixed*
    pins context indices (e.g. ``S_StateRequested`` = the state being enabled);
    *domains* supplies finite value domains for the remaining free indices
    (usually the prover's ``nd_domains``).

    Returns the satisfying assignments, or ``None`` when the predicate is not a
    finite-domain constant-table predicate (the sound punt).
    """
    if op not in _CMP:
        return None
    fixed = dict(fixed or {})
    domains = domains or {}

    calc_expr = _sole_calc_expr(result_tag, pdg, program)
    if calc_expr is None:
        return None

    from pyrung.core.analysis.sp_values import _expr_tag_names

    operand_tags = _expr_tag_names(calc_expr)
    if not operand_tags:
        return None

    # Model every operand: constant value, or a constant-table lookup whose
    # index register is the free variable we enumerate.
    consts: dict[str, Any] = {}
    tables: dict[str, _TableOperand] = {}
    for tag in operand_tags:
        if tag in fixed:
            consts[tag] = fixed[tag]
            continue
        table = _model_table_operand(tag, snapshot, pdg, program)
        if table is not None:
            tables[tag] = table
            continue
        cval = _model_constant(tag, snapshot, pdg)
        if cval is not None:
            consts[tag] = cval
            continue
        return None  # a genuinely live operand — punt, do not fabricate

    # Free variables = the distinct index registers of the table operands, minus
    # any pinned by context.
    free_tags: list[str] = []
    for table in tables.values():
        idx = table.index_tag
        if idx not in fixed and idx not in free_tags:
            free_tags.append(idx)
    if len(free_tags) > _MAX_FREE_INDICES:
        return None

    free_domains: list[tuple[Any, ...]] = []
    for idx in free_tags:
        dom = _index_domain(idx, snapshot, pdg, program, domains)
        if dom is None:
            return None  # unknown/unbounded index domain — punt
        free_domains.append(dom)

    total = 1
    for dom in free_domains:
        total *= len(dom)
    if total > _MAX_COMBOS:
        return None

    from pyrung.core.analysis.sp_values import _SnapshotView

    predicate = _CMP[op]
    satisfying: list[dict[str, Any]] = []
    for combo in itertools.product(*free_domains):
        free_asn = dict(zip(free_tags, combo, strict=True))
        overlay: dict[str, Any] = dict(consts)
        ok = True
        for tag, table in tables.items():
            iv = free_asn.get(table.index_tag, fixed.get(table.index_tag))
            val = _read_table(table, iv, snapshot)
            if val is None:
                ok = False
                break
            overlay[tag] = val
        if not ok:
            continue
        try:
            actual = calc_expr.evaluate(_SnapshotView(snapshot, overlay))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if predicate(actual, target_value):
            satisfying.append(free_asn)

    if not satisfying:
        # A real, sound answer: the predicate is unsatisfiable over the domains
        # (the state is disabled in every mode).  Represent as empty assignments.
        return PredicateSolution(free_tags=tuple(free_tags), assignments=())
    return PredicateSolution(free_tags=tuple(free_tags), assignments=tuple(satisfying))


def solve_calc_preimage(
    result_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    fixed: dict[str, Any] | None = None,
    domains: dict[str, tuple[Any, ...]] | None = None,
) -> dict[str, Any] | None:
    """Forced source pins for a **non-affine** ``calc(expr, result_tag)`` decode.

    The value-side sibling of :func:`solve_table_predicate`: where that solver
    inverts a boolean *predicate* over constant-table operands, this one inverts
    a whole ``calc`` *expression* — ``A * B``, ``A & mask``, ``(A << 2) | B`` —
    that trace's affine crossing (``calc_source_binding``) cannot invert
    symbolically.  Nothing here is guessed: it is exact enumerate-and-evaluate
    over *complete finite* domains, punting on anything softer.

    Each operand tag is modeled as one of:

    - a ``fixed`` context value (or a genuinely-constant, never-written register);
    - a constant-table lookup ``table[index]`` indexed by a finite-domain register
      (as :func:`solve_table_predicate` does);
    - a **free source register** with a provably-complete finite domain (a Bool's
      ``(False, True)`` or an ``nd_domains`` entry).

    An operand that is none of these — a genuinely-live word with no complete
    domain — makes the whole solve return ``None`` (punt; never fabricate a pin).
    The free variables (free source operands plus the free table indices) are
    enumerated over the Cartesian product of their complete domains, honoring the
    same :data:`_MAX_FREE_INDICES` / :data:`_MAX_COMBOS` guardrails as the guard
    enumerators.  Any free variable lacking a complete domain ⇒ ``None``:
    plausible-value fallbacks (``_index_values`` / producible-literal chains) may
    never pin, exactly as the completeness gate in ``trace._writer_guard_verdict``.

    Pin semantics — **FORCED values only**: a source is pinned to ``v`` iff *every*
    satisfying assignment projects that source to ``v`` (the shared per-source
    projection over all solutions).  A source that varies across solutions is not
    pinned.  Returns the dict of forced pins, which may be empty when nothing is
    forced.  Zero satisfying assignments over the domains also returns the
    empty-pin dict (no preimage ⇒ no data-flow pin) — the empty dict is a valid
    "no pin" result, distinct from ``None`` (punt); a rejection, if any, is the
    guard-verdict path's concern, not this pin derivation's.
    """
    fixed = dict(fixed or {})
    domains = domains or {}

    calc_expr = _sole_calc_expr(result_tag, pdg, program)
    if calc_expr is None:
        return None

    from pyrung.core.analysis.sp_values import _expr_tag_names, _SnapshotView

    operand_tags = _expr_tag_names(calc_expr)
    if not operand_tags:
        return None

    # Model every operand: a fixed/constant value, a constant-table lookup whose
    # index is a free variable, or a free source register we enumerate directly.
    consts: dict[str, Any] = {}
    tables: dict[str, _TableOperand] = {}
    free_sources: list[str] = []
    for tag in operand_tags:
        if tag == result_tag:
            return None  # self-referential — cannot invert
        if tag in fixed:
            consts[tag] = fixed[tag]
            continue
        table = _model_table_operand(tag, snapshot, pdg, program)
        if table is not None:
            tables[tag] = table
            continue
        if _is_complete_domain(tag, pdg, domains):
            free_sources.append(tag)
            continue
        cval = _model_constant(tag, snapshot, pdg)
        if cval is not None:
            consts[tag] = cval
            continue
        return None  # a genuinely-live operand — punt, do not fabricate

    # Free variables: the free source operands plus the distinct index registers
    # of the table operands, minus anything pinned by context.
    free_tags: list[str] = list(free_sources)
    for table in tables.values():
        idx = table.index_tag
        if idx not in fixed and idx not in free_tags:
            free_tags.append(idx)
    if len(free_tags) > _MAX_FREE_INDICES:
        return None

    free_domains: list[tuple[Any, ...]] = []
    for t in free_tags:
        # Pin soundness: only a provably-complete finite domain may enumerate.
        if not _is_complete_domain(t, pdg, domains):
            return None
        dom = _guard_operand_domain(t, snapshot, pdg, program, domains)
        if not dom:
            return None
        free_domains.append(dom)

    total = 1
    for dom in free_domains:
        total *= len(dom)
    if total > _MAX_COMBOS:
        return None

    satisfying: list[dict[str, Any]] = []
    for combo in itertools.product(*free_domains):
        free_asn = dict(zip(free_tags, combo, strict=True))
        overlay: dict[str, Any] = dict(consts)
        for t in free_sources:
            overlay[t] = free_asn[t]
        ok = True
        for tag, table in tables.items():
            iv = free_asn.get(table.index_tag, fixed.get(table.index_tag))
            val = _read_table(table, iv, snapshot)
            if val is None:
                ok = False
                break
            overlay[tag] = val
        if not ok:
            continue
        try:
            actual = calc_expr.evaluate(_SnapshotView(snapshot, overlay))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if actual == target_value:
            satisfying.append(free_asn)

    # FORCED pins: a free tag is pinned iff every satisfying assignment agrees on
    # its value.  No satisfying assignment ⇒ no preimage ⇒ no pin (never invent a
    # rejection here — that is the guard-verdict path's concern).
    forced: dict[str, Any] = {}
    for t in free_tags:
        vals = {asn[t] for asn in satisfying}
        if len(vals) == 1:
            forced[t] = next(iter(vals))
    return forced


def _is_complete_domain(tag: str, pdg: ProgramGraph, domains: dict[str, tuple[Any, ...]]) -> bool:
    """Whether *tag* has a **provably-complete** finite value domain.

    Mirrors the completeness gate in ``trace._writer_guard_verdict``: only a
    Bool type (trivially ``(False, True)``) or an ``nd_domains`` entry (complete
    by the prover's construction) qualifies.  The tide tables' softer fallbacks
    (``_index_values`` / producible-literal chains) are only *plausible* value
    sets — enumerating a pin/rejection over one would fabricate it — so they are
    deliberately excluded here."""
    from pyrung.core.tag import TagType

    if domains and tag in domains:
        return True
    tag_ref = pdg.tags.get(tag)
    return tag_ref is not None and getattr(tag_ref, "type", None) is TagType.BOOL


# Three-valued guard verdicts (see :func:`guard_verdict`).  ``guard_satisfiable``
# is exactly ``guard_verdict(...) != GUARD_DEAD``; the extra ``PUNT``/``SAT`` split
# lets a caller distinguish "found a satisfying assignment" from "genuinely could
# not read the guard" (a live word / undecidable term) without changing the
# boolean's public contract.
GUARD_SAT = "sat"
GUARD_DEAD = "dead"
GUARD_PUNT = "punt"


def guard_verdict(
    expr: Any,
    *,
    fixed: dict[str, Any],
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    domains: dict[str, tuple[Any, ...]] | None = None,
) -> str:
    """Three-valued sibling of :func:`guard_satisfiable`: the *why* behind ``True``.

    Same enumerate-and-evaluate machinery, but reports which of three cases holds:

    - :data:`GUARD_DEAD` — *provably unsatisfiable*: every assignment over complete
      finite free-tag domains evaluates definitely ``False`` (or a fully-pinned
      guard is definitely ``False``).  Only here may a caller soundly reject the
      writer — it can never fire to produce the value.
    - :data:`GUARD_SAT` — a concrete satisfying assignment exists (or the
      fully-pinned guard is definitely ``True``).
    - :data:`GUARD_PUNT` — undecidable (a ``None`` term — ``rise``/``fall``, a
      stale calc-result), or a free tag has no known finite domain, or the
      enumeration guardrails are exceeded.  Never a rejection, but distinct from
      ``SAT`` so the caller can flag a frontier gated by a genuinely-unreadable
      guard (the skiff's escalation signal).

    ``fixed`` pins what the writer *forces* — its copy/calc source and any context.
    Free tags are the remaining guard operands; a Bool free operand resolves to the
    trivial ``(False, True)`` domain via ``_guard_operand_domain``, everything else
    to a finite integer domain (or ``None`` → punt).
    """
    from pyrung.core.analysis.pilot.availability import _simplified_expr_tags
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    domains = domains or {}
    overlay_base = {**snapshot, **fixed}
    free = sorted(_simplified_expr_tags(expr) - set(fixed))

    if not free:
        # Fully pinned — ``False`` is a proof of unsat; ``True`` a proof of sat;
        # ``None`` (undecidable) punts.
        v = _eval_expr_from_state(expr, overlay_base)
        if v is True:
            return GUARD_SAT
        if v is False:
            return GUARD_DEAD
        return GUARD_PUNT

    if len(free) > _MAX_FREE_INDICES:
        return GUARD_PUNT  # too wide to enumerate soundly

    free_domains: list[tuple[Any, ...]] = []
    for tag in free:
        dom = _guard_operand_domain(tag, snapshot, pdg, program, domains)
        if dom is None:
            return GUARD_PUNT  # unknown/unbounded domain
        free_domains.append(dom)

    total = 1
    for dom in free_domains:
        total *= len(dom)
    if total > _MAX_COMBOS:
        return GUARD_PUNT

    saw_unknown = False
    for combo in itertools.product(*free_domains):
        overlay = {**overlay_base, **dict(zip(free, combo, strict=True))}
        verdict = _eval_expr_from_state(expr, overlay)
        if verdict is True:
            return GUARD_SAT  # a satisfying assignment exists
        if verdict is None:
            saw_unknown = True
    # No assignment was definitely True: punt if any was undecidable, else the
    # guard is provably unsatisfiable over the domains.
    return GUARD_PUNT if saw_unknown else GUARD_DEAD


def guard_satisfiable(
    expr: Any,
    *,
    fixed: dict[str, Any],
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    domains: dict[str, tuple[Any, ...]] | None = None,
) -> bool:
    """Whether a writer guard *may* be satisfiable given the pins the writer imposes.

    Generalizes the narrow copy-source producibility check (``trace._reduce_guard_
    by_pin``) from "a source-only conjunct the pin settles" to "is the whole guard
    satisfiable over the free operands' finite domains".  Same enumerate-and-
    evaluate technique as :func:`solve_table_predicate`, but over an arbitrary
    simplified ``And``/``Or``/``Atom`` guard evaluated by the three-valued
    ``_eval_expr_from_state`` (not a single ``calc``-result comparison).

    ``fixed`` pins what the writer *forces* — its copy source (``src == src_val``)
    and any context (e.g. the transition's target state).  Free tags are the
    remaining guard operands; each is resolved to a finite domain and the guard is
    enumerated over their Cartesian product.

    Returns ``False`` **only** when the guard is *provably unsatisfiable* — every
    assignment over *complete finite* free-tag domains evaluates definitely
    ``False`` — so the caller may soundly reject the writer (it can never fire to
    produce the value).  Returns ``True`` in every other case: a satisfying
    assignment exists, or the guard is undecidable (a ``None`` term — ``rise``/
    ``fall``, a stale calc-result the tree can't resolve), or a free tag has no
    known finite domain, or the enumeration guardrails are exceeded.  ``True`` is
    the punt-biased default: it never rejects a writer the loop might still drive.

    Thin boolean over :func:`guard_verdict` — ``True`` for ``SAT``/``PUNT``,
    ``False`` for ``DEAD`` — preserved as the stable public contract; callers that
    need the ``PUNT`` vs ``SAT`` distinction (trace's rejection arm) call
    :func:`guard_verdict` directly.
    """
    return (
        guard_verdict(
            expr,
            fixed=fixed,
            snapshot=snapshot,
            pdg=pdg,
            program=program,
            domains=domains,
        )
        != GUARD_DEAD
    )


# ---------------------------------------------------------------------------
# Operand / index modeling (mirrors trace._invert_indirect, factored per-operand)
# ---------------------------------------------------------------------------


def _sole_calc_expr(tag: str, pdg: ProgramGraph, program: Any) -> Any | None:
    """The expression of the single ``calc(expr, tag)`` writer, else ``None``."""
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag:
            return instr.expression
    return None


def _model_table_operand(
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> _TableOperand | None:
    """Model *tag* as ``table[eval_addr(index_tag)]`` if its sole writer is an
    indirect copy out of a table."""
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    src = None
    for instr in ro._instructions:
        if not isinstance(instr, CopyInstruction):
            continue
        if getattr(instr.dest, "name", None) != tag:
            continue
        if isinstance(instr.source, (IndirectRef, IndirectExprRef)):
            src = instr.source
        break
    if src is None:
        return None
    table = table_from_indirect_src(src, snapshot, pdg, program)
    if table is None or table.index_tag == tag:
        return None
    return table


def table_from_indirect_src(
    src: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> _TableOperand | None:
    """Model an ``IndirectRef``/``IndirectExprRef`` copy source as a
    :class:`_TableOperand`: the index register plus an ``address(index)``
    evaluator, hopping through calc-defined scratch pointers (``calc(X+200, idx)``).

    Shared by :func:`_model_table_operand` (the tide tables' predicate solver) and
    ``trace._invert_indirect`` (the single-table value-jump inverter): the two are
    the same primitive at different arities, so the extraction lives in one place.
    """
    from pyrung.core.analysis.pilot.static_expressions import single_calc_source
    from pyrung.core.analysis.sp_values import _expr_tag_names, _SnapshotView
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    if isinstance(src, IndirectRef):
        idx_tag = src.pointer.name
        eval_addr: Any = lambda v: int(v)  # noqa: E731
    elif isinstance(src, IndirectExprRef):
        names = _expr_tag_names(src.expr)
        if not names:
            return None
        mutable = {n for n in names if pdg.writers_of.get(n)}
        if len(mutable) != 1:
            return None
        idx_tag = next(iter(mutable))
        iexpr = src.expr
        itag = idx_tag
        eval_addr = lambda v: int(iexpr.evaluate(_SnapshotView(snapshot, {itag: v})))  # noqa: E731
    else:
        return None

    for _ in range(3):
        defn = single_calc_source(idx_tag, pdg, program)
        if defn is None:
            break
        cexpr, hop_src = defn

        def _hopped(
            v: int, _prev: Any = eval_addr, _cexpr: Any = cexpr, _src: str = hop_src
        ) -> int:
            mid = int(_cexpr.evaluate(_SnapshotView(snapshot, {_src: v})))
            return _prev(mid)

        eval_addr = _hopped
        idx_tag = hop_src
    else:
        # Exhausted the 3-hop budget with the chain still going: the address
        # cannot be fully resolved within the supported hop count.  Punt cleanly
        # rather than model a table indexed by a still-computed pointer — a
        # partially-resolved ``eval_addr`` would fabricate a lookup.
        if single_calc_source(idx_tag, pdg, program) is not None:
            return None

    return _TableOperand(index_tag=idx_tag, eval_addr=eval_addr, block=src.block)


def _read_table(table: _TableOperand, index_value: Any, snapshot: dict[str, Any]) -> Any | None:
    """Value at ``table[index_value]`` — snapshot slot else declared default."""
    if not isinstance(index_value, int) or isinstance(index_value, bool):
        return None
    try:
        addr = table.eval_addr(index_value)
        table.block._validate_address(addr)
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return None
    slot_name = table.block._effective_slot_name(addr)
    if slot_name in snapshot:
        return snapshot[slot_name]
    return table.block._effective_slot_policy(addr)[1]  # (retentive, default)


def _model_constant(
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
) -> Any | None:
    """A constant operand value: a never-written / readonly tag read from the
    snapshot.  Returns ``None`` when *tag* is (re)written by the program and so
    is not a constant we can pin."""
    if pdg.writers_of.get(tag):
        return None
    return snapshot.get(tag)


def _guard_operand_domain(
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Finite value domain for a free *guard* operand — :func:`guard_satisfiable`'s
    resolver, distinct from :func:`_index_domain` (which is also used by
    :func:`solve_table_predicate` for table INDEX registers, where a Bool domain
    would be meaningless).

    A Bool-typed tag's value domain is trivially ``(False, True)`` — no table/copy
    modeling needed, just the tag's declared type.  Anything not genuinely
    Bool-typed (uncertain, indirect, or absent from ``pdg.tags``) falls through to
    :func:`_index_domain` unchanged."""
    from pyrung.core.tag import TagType

    tag_ref = pdg.tags.get(tag)
    if tag_ref is not None and getattr(tag_ref, "type", None) is TagType.BOOL:
        return (False, True)
    return _index_domain(tag, snapshot, pdg, program, domains)


def _index_domain(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    domains: dict[str, tuple[Any, ...]],
) -> tuple[Any, ...] | None:
    """Finite value domain for a free index register.

    Prefers the prover's ``nd_domains``; else the int values the program can
    actually *place* in the register (``_producible_int_domain`` — recursing copy
    hops so ``S_UnitModeCurrent <- C_UnitMode <- {1,2,3}`` resolves to the three
    modes, not just the current one); else the plausible index values trace
    enumerates (``_index_values``).  ``None`` means the domain is not known to be
    finite — the caller must punt."""
    if idx_tag in domains:
        dom = tuple(v for v in domains[idx_tag] if isinstance(v, int) and not isinstance(v, bool))
        return dom or None

    producible = _producible_int_domain(idx_tag, snapshot, pdg, program, domains)
    if producible:
        return tuple(sorted(producible))

    from pyrung.core.analysis.pilot.static_expressions import index_values

    vals = index_values(idx_tag, snapshot, pdg, program)
    return tuple(vals) or None


def _producible_int_domain(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    domains: dict[str, tuple[Any, ...]] | None = None,
    _hops: int = 3,
    _seen: frozenset[str] = frozenset(),
) -> set[int]:
    """Int values the program's writers can place in *idx_tag*, recursing
    identity copy-from-tag hops to collect the literals at the source
    (``copy(C_UnitMode, S_UnitModeCurrent)`` -> C_UnitMode's ``copy(1/2/3, ...)``).

    A chain that bottoms out at a **writer-less input** yields that input's
    declared finite domain (*domains* — the prover's ``nd_domains`` / a tag's
    ``choices``), so a channel staged by ``copy(ExternalCmd, Channel)`` inherits
    the command's *drivable* values rather than only the constants the program
    stamps directly.  Without this, an operator-selected channel whose write is a
    plain copy (not a literal decode) resolves to just its current value."""
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.sp_values import _written_value_for_tag
    from pyrung.core.crossing import Affine, Literal

    domains = domains or {}
    if idx_tag in _seen or _hops < 0:
        return set()
    seen = _seen | {idx_tag}

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if not writers:
        # Pure input: the operator/field chooses the value, so its producible set
        # is its declared finite domain (if known).
        dom = domains.get(idx_tag)
        return {v for v in dom if isinstance(v, int) and not isinstance(v, bool)} if dom else set()

    vals: set[int] = set()
    for ri in writers:
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, idx_tag)
        if isinstance(wv, Literal):
            if isinstance(wv.value, int) and not isinstance(wv.value, bool):
                vals.add(wv.value)
        elif isinstance(wv, Affine) and wv.source != idx_tag:
            # Propagate the source domain through the affine map ``y = scale*x +
            # offset``.  An identity copy (scale 1, offset 0) forwards the domain
            # unchanged; a computed pointer ``calc(200 + Cmd, idx)`` shifts it, so
            # ``idx`` inherits the command's domain offset by the constant rather
            # than resolving to just its current value.
            for v in _producible_int_domain(
                wv.source, snapshot, pdg, program, domains, _hops - 1, seen
            ):
                shifted = wv.scale * v + wv.offset
                if isinstance(shifted, int) and not isinstance(shifted, bool):
                    vals.add(shifted)
    return vals
