"""Solve finite predicates over constant-backed lookup tables.

The solvers model indirect table operands, enumerate known finite index
domains, and return satisfying assignments or calculation preimages for the
backward trace. Unmodelled live operands produce no exact solution.

Some helper paths can return plausible values for non-rejecting reads.
``guard_verdict`` owns the complete-domain gate before it returns a permanent
guard rejection.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Collection, Iterable, Iterator
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


def bounded_product(
    domains: Iterable[Collection[Any]],
) -> Iterator[tuple[Any, ...]] | None:
    """Return the finite Cartesian product, or punt past enumeration guardrails."""

    finite_domains = tuple(domains)
    if len(finite_domains) > _MAX_FREE_INDICES:
        return None
    total = 1
    for domain in finite_domains:
        total *= len(domain)
    if total > _MAX_COMBOS:
        return None
    return itertools.product(*finite_domains)


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


@dataclass(frozen=True)
class _CalcSolutions:
    """Exact satisfying assignments produced by the shared finite solver."""

    free_tags: tuple[str, ...]
    assignments: tuple[dict[str, Any], ...]


def _solve_calc_assignments(
    result_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    fixed: dict[str, Any] | None,
    domains: dict[str, tuple[Any, ...]] | None,
    accept: Callable[[Any], bool],
    allow_free_sources: bool,
    require_complete_domains: bool,
) -> _CalcSolutions | None:
    """Model, enumerate, and evaluate one finite ``calc`` preimage."""

    fixed = dict(fixed or {})
    domains = domains or {}
    calc_expr = _sole_calc_expr(result_tag, pdg, program)
    if calc_expr is None:
        return None

    from pyrung.core.analysis.sp_values import _expr_tag_names, _SnapshotView

    operand_tags = _expr_tag_names(calc_expr)
    if not operand_tags:
        return None

    consts: dict[str, Any] = {}
    tables: dict[str, _TableOperand] = {}
    free_sources: list[str] = []
    for tag in operand_tags:
        if allow_free_sources and tag == result_tag:
            return None
        if tag in fixed:
            consts[tag] = fixed[tag]
            continue
        table = _model_table_operand(tag, snapshot, pdg, program)
        if table is not None:
            tables[tag] = table
            continue
        if allow_free_sources and _is_complete_domain(tag, pdg, domains):
            free_sources.append(tag)
            continue
        cval = _model_constant(tag, snapshot, pdg)
        if cval is not None:
            consts[tag] = cval
            continue
        return None

    free_tags: list[str] = list(free_sources)
    for table in tables.values():
        idx = table.index_tag
        if idx not in fixed and idx not in free_tags:
            free_tags.append(idx)

    free_domains: list[tuple[Any, ...]] = []
    for tag in free_tags:
        if require_complete_domains:
            if not _is_complete_domain(tag, pdg, domains):
                return None
            domain = _guard_operand_domain(tag, snapshot, pdg, program, domains)
        else:
            domain = _index_domain(tag, snapshot, pdg, program, domains)
        if domain is None or (require_complete_domains and not domain):
            return None
        free_domains.append(domain)

    combinations = bounded_product(free_domains)
    if combinations is None:
        return None

    satisfying: list[dict[str, Any]] = []
    for combo in combinations:
        free_asn = dict(zip(free_tags, combo, strict=True))
        overlay: dict[str, Any] = dict(consts)
        overlay.update((tag, free_asn[tag]) for tag in free_sources)
        for tag, table in tables.items():
            index_value = free_asn.get(table.index_tag, fixed.get(table.index_tag))
            value = _read_table(table, index_value, snapshot)
            if value is None:
                break
            overlay[tag] = value
        else:
            try:
                actual = calc_expr.evaluate(_SnapshotView(snapshot, overlay))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if accept(actual):
                satisfying.append(free_asn)

    return _CalcSolutions(tuple(free_tags), tuple(satisfying))


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
    predicate = _CMP[op]
    solved = _solve_calc_assignments(
        result_tag,
        snapshot,
        pdg,
        program,
        fixed=fixed,
        domains=domains,
        accept=lambda actual: predicate(actual, target_value),
        allow_free_sources=False,
        require_complete_domains=False,
    )
    if solved is None:
        return None
    if not solved.assignments:
        # A real, sound answer: the predicate is unsatisfiable over the domains
        # (the state is disabled in every mode).  Represent as empty assignments.
        return PredicateSolution(free_tags=solved.free_tags, assignments=())
    return PredicateSolution(
        free_tags=solved.free_tags,
        assignments=solved.assignments,
    )


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
    whose registered reverse cannot invert symbolically. Nothing here is
    guessed: it is exact enumerate-and-evaluate
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
    never pin, exactly as the completeness policy in :func:`guard_verdict`.

    Pin semantics — **FORCED values only**: a source is pinned to ``v`` iff *every*
    satisfying assignment projects that source to ``v`` (the shared per-source
    projection over all solutions).  A source that varies across solutions is not
    pinned.  Returns the dict of forced pins, which may be empty when nothing is
    forced.  Zero satisfying assignments over the domains also returns the
    empty-pin dict (no preimage ⇒ no data-flow pin) — the empty dict is a valid
    "no pin" result, distinct from ``None`` (punt); a rejection, if any, is the
    guard-verdict path's concern, not this pin derivation's.
    """
    solved = _solve_calc_assignments(
        result_tag,
        snapshot,
        pdg,
        program,
        fixed=fixed,
        domains=domains,
        accept=lambda actual: actual == target_value,
        allow_free_sources=True,
        require_complete_domains=True,
    )
    if solved is None:
        return None

    # FORCED pins: a free tag is pinned iff every satisfying assignment agrees on
    # its value.  No satisfying assignment ⇒ no preimage ⇒ no pin (never invent a
    # rejection here — that is the guard-verdict path's concern).
    forced: dict[str, Any] = {}
    for tag in solved.free_tags:
        vals = {assignment[tag] for assignment in solved.assignments}
        if len(vals) == 1:
            forced[tag] = next(iter(vals))
    return forced


def _is_complete_domain(tag: str, pdg: ProgramGraph, domains: dict[str, tuple[Any, ...]]) -> bool:
    """Whether *tag* has a **provably-complete** finite value domain.

    Only a Bool type (trivially ``(False, True)``) or an ``nd_domains`` entry
    (complete by the prover's construction) qualifies.  The tide tables' softer
    fallbacks (``_index_values`` / producible-literal chains) are only
    *plausible* value sets — enumerating a proof over one would fabricate it —
    so they are deliberately excluded here."""
    from pyrung.core.tag import TagType

    if domains and tag in domains:
        return True
    tag_ref = pdg.tags.get(tag)
    return tag_ref is not None and getattr(tag_ref, "type", None) is TagType.BOOL


# Three-valued guard verdicts (see :func:`guard_verdict`).  The ``PUNT``/``SAT``
# split lets callers distinguish "found a satisfying assignment" from "genuinely
# could not read the guard" (a live word / undecidable term).
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
    require_complete_domains: bool = True,
) -> str:
    """Whether a writer guard can fire under the pins the writer imposes.

    Generalizes the narrow copy-source producibility check
    (``trace._reduce_guard_by_pin``) from a source-only conjunct to the whole
    simplified ``And``/``Or``/``Atom`` guard. It enumerates complete finite
    domains for the remaining free operands and reports one of three cases:

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

    ``fixed`` pins what the writer *forces* — its copy/calc source and any
    context. By default, every free tag must pass :func:`_is_complete_domain`
    before enumeration: a Bool supplies ``(False, True)`` and ``domains`` carries
    prover-owned complete domains. Missing completeness returns
    :data:`GUARD_PUNT` even when a softer plausible domain can be inferred.

    ``require_complete_domains=False`` is an explicit escape for a caller that
    independently owns the completeness proof or needs a model-relative
    diagnostic. It preserves enumeration over plausible domains, but a
    :data:`GUARD_DEAD` result in that mode must not drive permanent rejection.
    Unknown domains, undecidable terms, and exceeded guardrails still punt.
    """
    from pyrung.core.analysis.pilot.static_expressions import simplified_expr_tags
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    domains = domains or {}
    overlay_base = {**snapshot, **fixed}
    free = sorted(simplified_expr_tags(expr) - set(fixed))

    if not free:
        # Fully pinned — ``False`` is a proof of unsat; ``True`` a proof of sat;
        # ``None`` (undecidable) punts.
        v = _eval_expr_from_state(expr, overlay_base)
        if v is True:
            return GUARD_SAT
        if v is False:
            return GUARD_DEAD
        return GUARD_PUNT

    free_domains: list[tuple[Any, ...]] = []
    for tag in free:
        if require_complete_domains and not _is_complete_domain(tag, pdg, domains):
            return GUARD_PUNT
        dom = _guard_operand_domain(tag, snapshot, pdg, program, domains)
        if dom is None:
            return GUARD_PUNT  # unknown/unbounded domain
        free_domains.append(dom)

    combinations = bounded_product(free_domains)
    if combinations is None:
        return GUARD_PUNT

    saw_unknown = False
    for combo in combinations:
        overlay = {**overlay_base, **dict(zip(free, combo, strict=True))}
        verdict = _eval_expr_from_state(expr, overlay)
        if verdict is True:
            return GUARD_SAT  # a satisfying assignment exists
        if verdict is None:
            saw_unknown = True
    # No assignment was definitely True: punt if any was undecidable, else the
    # guard is provably unsatisfiable over the domains.
    return GUARD_PUNT if saw_unknown else GUARD_DEAD


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

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    table = table_operand_from_copy(ro, tag, snapshot, pdg, program)
    if table is None or table.index_tag == tag:
        return None
    return table


def table_operand_from_copy(
    rung: Any,
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    **model_options: Any,
) -> _TableOperand | None:
    """Find and model the indirect-copy source in one exact writer."""

    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    for instruction in rung._instructions:
        if not isinstance(instruction, CopyInstruction):
            continue
        if getattr(instruction.dest, "name", None) != tag:
            continue
        if isinstance(instruction.source, (IndirectRef, IndirectExprRef)):
            return table_from_indirect_src(
                instruction.source,
                snapshot,
                pdg,
                program,
                **model_options,
            )
        return None
    return None


def table_from_indirect_src(
    src: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    *,
    evidence: Any = None,
    single_mutable_index: bool = True,
    live_snapshot: bool = True,
    strict_hop_budget: bool = True,
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
        candidates = (
            {name for name in names if pdg.writers_of.get(name)} if single_mutable_index else names
        )
        if len(candidates) != 1:
            return None
        idx_tag = next(iter(candidates))
        iexpr = src.expr
        itag = idx_tag
        address_snapshot = snapshot if live_snapshot else {}
        eval_addr = lambda v: int(  # noqa: E731
            iexpr.evaluate(_SnapshotView(address_snapshot, {itag: v}))
        )
    else:
        return None

    for _ in range(3):
        canonical = evidence.canonicalize(idx_tag) if evidence is not None else None
        if canonical is not None:
            previous = eval_addr
            scale = canonical.scale
            offset = canonical.offset
            eval_addr = lambda v, _prev=previous, _scale=scale, _offset=offset: _prev(
                _scale * v + _offset
            )
            idx_tag = canonical.representative
            continue

        defn = single_calc_source(idx_tag, pdg, program)
        if defn is None:
            break
        cexpr, hop_src = defn
        calc_snapshot = snapshot if live_snapshot else _constant_calc_env(cexpr, hop_src, pdg)

        def _hopped(
            v: int,
            _prev: Any = eval_addr,
            _cexpr: Any = cexpr,
            _src: str = hop_src,
            _snapshot: dict[str, Any] = calc_snapshot,
        ) -> int:
            mid = int(_cexpr.evaluate(_SnapshotView(_snapshot, {_src: v})))
            return _prev(mid)

        eval_addr = _hopped
        idx_tag = hop_src
    else:
        # Exhausted the 3-hop budget with the chain still going: the address
        # cannot be fully resolved within the supported hop count.  Punt cleanly
        # rather than model a table indexed by a still-computed pointer — a
        # partially-resolved ``eval_addr`` would fabricate a lookup.
        if strict_hop_budget and single_calc_source(idx_tag, pdg, program) is not None:
            return None

    return _TableOperand(index_tag=idx_tag, eval_addr=eval_addr, block=src.block)


def _constant_calc_env(expr: Any, source_tag: str, pdg: ProgramGraph) -> dict[str, Any]:
    """Declared defaults for immutable constants beside one mutable calc source."""

    from pyrung.core.analysis.sp_values import _expr_tag_names

    names = _expr_tag_names(expr) or set()
    return {
        name: pdg.tags[name].default for name in names if name != source_tag and name in pdg.tags
    }


def _read_table(
    table: _TableOperand,
    index_value: Any,
    snapshot: dict[str, Any],
    *,
    coerce_index: bool = False,
    invalid: Any = None,
) -> Any | None:
    """Value at ``table[index_value]`` — snapshot slot else declared default."""
    if coerce_index:
        try:
            index_value = int(index_value)
        except (TypeError, ValueError):
            return invalid
    elif not isinstance(index_value, int) or isinstance(index_value, bool):
        return invalid
    try:
        addr = table.eval_addr(index_value)
        table.block._validate_address(addr)
    except (IndexError, TypeError, ValueError, ZeroDivisionError):
        return invalid
    slot_name = table.block._effective_slot_name(addr)
    if slot_name in snapshot:
        return snapshot[slot_name]
    return table.block._effective_slot_policy(addr)[1]  # (retentive, default)


def invert_indirect_copy(
    rung: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> tuple[str, list[Any]] | None:
    """Invert the indirect copy in *rung* into matching index values."""

    from pyrung.core.analysis.pilot.static_expressions import index_values
    from pyrung.core.analysis.sp_values import _values_match

    table = table_operand_from_copy(rung, tag, snapshot, pdg, program)
    if table is None or table.index_tag == tag:
        return None
    matching = [
        index
        for index in index_values(table.index_tag, snapshot, pdg, program)
        if _values_match(_read_table(table, index, snapshot), value)
    ]
    return (table.index_tag, matching) if matching else None


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
    """Finite value domain for a free *guard* operand — :func:`guard_verdict`'s
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
    the command's *steerable* values rather than only the constants the program
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
