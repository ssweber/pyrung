"""Static priors and candidate generation for the corridor walker.

Everything here is a prior, never correctness-bearing: target extraction,
governing-tag selection (with the ``_probe_steps`` simulation probe as the
ground-truth fallback), steer-alphabet construction, writer-condition and
inequality prerequisite extraction, and the static coupling detection
behind the Tier-2 decomposition hint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _FLIP_FORM as _FLIP_FORM

# Moved to the neutral home (sp_values.py) so causal/ can import them without
# a causal -> walk layering inversion; re-exported here for compatibility.
from pyrung.core.analysis.sp_values import _chase_inequality_source as _chase_inequality_source
from pyrung.core.analysis.sp_values import _expr_tag_names as _expr_tag_names
from pyrung.core.analysis.sp_values import (
    _extract_inequality_prereqs as _extract_inequality_prereqs,
)
from pyrung.core.analysis.sp_values import _producible_values as _producible_values
from pyrung.core.analysis.sp_values import _satisfying_value as _satisfying_value
from pyrung.core.analysis.sp_values import _SnapshotView as _SnapshotView
from pyrung.core.analysis.sp_values import _sole_calc_expression as _sole_calc_expression
from pyrung.core.analysis.walk.base import (
    _CMP_OPS,
    _IDX_CHASE_CAP,
    _MAX_SET_VALUE_STEERS,
    _PULSE_REACT_CAP,
    NoGoodStore,
    _Steer,
    _values_match,
)
from pyrung.core.analysis.walk.fold import (
    _calc_self_referential,
    _collect_acc_sources,
    _is_clock_view,
    _is_free_running_selfcalc,
)
from pyrung.core.analysis.walk.steer import _steer_prefix

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC


@dataclass(frozen=True)
class _WriterCandidate:
    """One writer alternative with its full guard context preserved."""

    full_conditions: tuple[tuple[str, Any], ...]
    satisfied: tuple[tuple[str, Any], ...]
    unsatisfied: tuple[tuple[str, Any], ...]
    all_writes: frozenset[str]
    writer_index: int


# ---------------------------------------------------------------------------
# Target extraction
# ---------------------------------------------------------------------------


def _extract_goals(expr: Any, snapshot: dict[str, Any]) -> list[tuple[str, Any]] | None:
    """Reduce a target expression to a list of ``(tag, value)`` goals.

    Handles And (all terms), Or (cheapest branch), and single Atom.
    Returns ``None`` for expressions that can't be decomposed into
    concrete tag==value pairs (rise/fall/inequalities).
    """
    from pyrung.core.analysis.sp_values import _extract_required_values

    pairs = _extract_required_values(expr, snapshot)
    if not pairs:
        return None
    return pairs


# ---------------------------------------------------------------------------
# Static priors: governing tag, steer alphabet, horizon
# ---------------------------------------------------------------------------


def _copy_source(tag: str, pdg: ProgramGraph, program: Any) -> str | None:
    """Return ``U`` when *tag* is written ``copy(U, tag)`` (copy-from-tag)."""
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.sp_values import _named_copy_source, _writer_for_tag

    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        instr = _writer_for_tag(ro, tag)
        if instr is not None:
            src_name = _named_copy_source(instr)
            if src_name is not None:
                return src_name
    return None


def _indirect_copy_source(ro: Any, tag: str) -> Any | None:
    """The indirect source of a ``copy(block[...], tag)`` writer, if any.

    Returns the ``IndirectRef`` (``block[pointer]``) or ``IndirectExprRef``
    (``block[idx + k]``) feeding *tag*, or ``None`` when the writer is not
    an indirect copy.  These are exactly the sources
    :func:`~pyrung.core.analysis.sp_values._written_value_for_tag`
    classifies as statically unresolvable.
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    for instr in ro._instructions:
        if not isinstance(instr, CopyInstruction):
            continue
        if getattr(instr.dest, "name", None) != tag:
            continue
        src = instr.source
        if isinstance(src, (IndirectRef, IndirectExprRef)):
            return src
        return None
    return None


def _functional_deps(explore_context: Any) -> dict[str, tuple[str, int, Any]] | None:
    """The pre-BFS pipeline's affine projections, if present.

    ``detect_functional_dependencies`` records ``y = scale*x + offset``
    scratch registers as ``{y: (x, scale, offset)}`` (scale ∈ {1, -1}) —
    the chase follows these to land its sub-goal on the representative
    register the program actually drives.  Under ``walk_only`` the pass
    admits slice-elided scratch (the PackML jump-table pointers) and
    ND-input sources directly, so projections cover the affine shapes;
    :func:`_single_calc_definition` remains the fallback for non-affine
    single-source calc expressions the pass can't express.
    """
    fd = getattr(explore_context, "functional_dep_projections", None)
    return fd if fd else None


def _single_calc_definition(
    idx_tag: str, pdg: ProgramGraph, program: Any
) -> tuple[Any, str] | None:
    """``(expression, source_tag)`` when *idx_tag* is calc-defined scratch.

    The jump-table idiom computes the pointer into a scratch register
    (``calc(S_StateRequested + 150, sm__jump_target_ds_idx)``) one rung
    before the indirect copy reads it.  When *idx_tag*'s sole writer is a
    ``calc`` reading exactly one other tag, that expression defines the
    pointer — a per-writer lookup in the spirit of :func:`_copy_source`,
    not a program-wide analysis, and only a prior: a wrong hop yields
    candidates the interpreted trial rejects.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if len(writers) != 1:
        return None
    ro = _resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == idx_tag:
            names = _expr_tag_names(instr.expression)
            if names is not None and len(names) == 1:
                src = next(iter(names))
                if src != idx_tag:
                    return instr.expression, src
            return None
    return None


def _index_candidates(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    nd_domains: dict[str, tuple[Any, ...]] | None,
) -> list[int]:
    """Plausible values for an indirect-copy index register, current-first.

    Sources: the snapshot's current value (free — if it already lands the
    goal slot, the binding filters out as satisfied), every literal the
    program writes to the register, every copy-from-tag writer's *source
    snapshot value* (the data-flow half: the PackML template writes
    ``copy(sm__STATERESETTINGREF, S_StateRequested)``, so the REF's
    current value is exactly what the register can hold next), and the
    pipeline domain when the index is an ND input.  Bounded by
    ``_IDX_CHASE_CAP``.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.sp_values import (
        _named_copy_source,
        _writer_for_tag,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import Literal

    rest: set[int] = set()
    current = snapshot.get(idx_tag)
    for ri in sorted(pdg.writers_of.get(idx_tag, frozenset())):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, idx_tag)
        if isinstance(wv, Literal):
            v = wv.value
            if isinstance(v, int) and not isinstance(v, bool):
                rest.add(v)
        else:
            _instr = _writer_for_tag(ro, idx_tag)
            src_name = _named_copy_source(_instr) if _instr is not None else None
            if src_name is not None and src_name != idx_tag:
                v = snapshot.get(src_name)
                if isinstance(v, int) and not isinstance(v, bool):
                    rest.add(v)
    if nd_domains:
        for v in nd_domains.get(idx_tag, ()):
            if isinstance(v, int) and not isinstance(v, bool):
                rest.add(v)
    out: list[int] = []
    if isinstance(current, int) and not isinstance(current, bool):
        out.append(current)
        rest.discard(current)
    out.extend(sorted(rest))
    return out[:_IDX_CHASE_CAP]


def _invert_indirect_source(
    src: Any,
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
) -> tuple[str, list[int]] | None:
    """Idx-chasing: invert a jump table on the live snapshot.

    For a ``copy(block[idx + k], tag)`` writer with goal ``tag == value``,
    the source bank is fully readable in the snapshot — enumerate the index
    register's candidate values, evaluate the address each one yields, and
    keep the candidates whose table slot holds *value*.  The *index
    register* (never the source bank — ``ref_constant_order`` defers that)
    becomes the sub-goal.

    The pointer register may itself be calc-defined scratch (the template
    computes ``calc(S_StateRequested + 150, sm__jump_target_ds_idx)`` one
    rung before the copy): the chase hops to the register the program
    actually drives — the one with literal-write candidates — consulting
    the pipeline's functional-dep projections first (*func_deps*,
    ``{scratch: (representative, offset)}``) and falling back to the sole
    writer's calc expression (:func:`_single_calc_definition`), composing
    each hop into the address evaluation.

    Returns ``(index_tag, inverting_values)`` ordered current-value-first,
    or ``None`` when the index expression reads more than one tag, the
    index is the goal tag itself, or no candidate lands on *value* (the
    honest refusal — same verdict as before the chase existed).
    """
    from pyrung.core.memory_block import IndirectRef

    if isinstance(src, IndirectRef):
        idx_tag = src.pointer.name
        eval_addr: Any = lambda v: int(v)
    else:
        names = _expr_tag_names(src.expr)
        if names is None or len(names) != 1:
            return None
        idx_tag = next(iter(names))
        iexpr = src.expr
        itag = idx_tag
        eval_addr = lambda v: int(iexpr.evaluate(_SnapshotView(snapshot, {itag: v})))

    for _hop in range(3):
        dep = func_deps.get(idx_tag) if func_deps else None
        if dep is not None and dep[0] != idx_tag:
            hop_src, scale, offset = dep

            def _shifted(
                v: int, _prev: Any = eval_addr, _scale: Any = scale, _off: Any = offset
            ) -> int:
                return _prev(_scale * v + _off)

            eval_addr = _shifted
            idx_tag = hop_src
            continue
        defn = _single_calc_definition(idx_tag, pdg, program)
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
    if idx_tag == tag:
        return None

    block = src.block
    inverting: list[int] = []
    for v in _index_candidates(idx_tag, snapshot, pdg, program, nd_domains):
        try:
            addr = eval_addr(v)
            block._validate_address(addr)
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            continue
        slot_name = block._effective_slot_name(addr)
        if slot_name in snapshot:
            slot_val = snapshot[slot_name]
        else:
            _retentive, slot_val = block._effective_slot_policy(addr)
        if _values_match(slot_val, value):
            inverting.append(v)
    if not inverting:
        return None
    return idx_tag, inverting


def _value_richness(tag: str, pdg: ProgramGraph, program: Any) -> int:
    """How many distinct values *tag* plausibly steps through.

    Counts distinct literal write values of *tag* and (when copy-coupled) of
    its copy source; an arithmetic (counter) or self-updating ``calc`` writer
    counts as rich.  Used to decide whether *tag* is itself the governing
    corridor tag or merely a derived view of one.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.sp_values import (
        _has_arithmetic_writer,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import Literal

    if _has_arithmetic_writer(tag, pdg, program) or _calc_self_referential(tag, pdg, program):
        return 99
    values: set[Any] = set()
    sources = [tag]
    src = _copy_source(tag, pdg, program)
    if src is not None:
        sources.append(src)
        if _has_arithmetic_writer(src, pdg, program) or _calc_self_referential(src, pdg, program):
            return 99
    for s in sources:
        for ri in pdg.writers_of.get(s, frozenset()):
            ro = _resolve_rung(program, pdg.rung_nodes[ri])
            if ro is None:
                continue
            wv = _written_value_for_tag(ro, s)
            if isinstance(wv, Literal):
                values.add(wv.value)
    return len(values)


def _inequality_satisfying_value(form: str, operand: Any) -> Any | None:
    """Compute the nearest satisfying value for an inequality comparison."""
    if isinstance(operand, int):
        if form == "gt":
            return operand + 1
        if form == "ge":
            return operand
        if form == "lt":
            return operand - 1
        if form == "le":
            return operand
    if isinstance(operand, float):
        if form in ("gt", "ge"):
            return operand if form == "ge" else operand + 1.0
        if form in ("lt", "le"):
            return operand if form == "le" else operand - 1.0
    return None


def _extract_inequality_governing(
    expr: Any,
) -> dict[str, Any]:
    """Extract ``{tag: satisfying_value}`` from inequality atoms in *expr*."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    result: dict[str, Any] = {}

    def _visit(e: Any) -> None:
        if isinstance(e, Atom) and e.form in ("gt", "ge", "lt", "le"):
            if e.tag not in result:
                val = _inequality_satisfying_value(e.form, e.operand)
                if val is not None:
                    result[e.tag] = val
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                _visit(term)

    _visit(expr)
    return result


def _pipeline_richness(
    tag: str,
    explore_context: Any | None,
) -> int | None:
    """Value richness from the pipeline classification, or ``None`` if unknown.

    Uses ``stateful_dims``, ``nondeterministic_dims``, ``combinational_tags``,
    and ``elided_tags`` from the explore context.
    """
    if explore_context is None:
        return None
    sd = getattr(explore_context, "stateful_dims", None)
    if sd is not None and tag in sd:
        return len(sd[tag])
    nd = getattr(explore_context, "nondeterministic_dims", None)
    if nd is not None and tag in nd:
        return len(nd[tag])
    ct = getattr(explore_context, "combinational_tags", None)
    if ct is not None and tag in ct:
        return 99
    et = getattr(explore_context, "elided_tags", None)
    if et is not None and tag in et:
        reason = et[tag]
        if reason == "functional_dep":
            return 99
        # scan_local: input-derived, recomputed every scan → multi-valued
        return 99
    ic = getattr(explore_context, "init_constant_projections", None)
    if ic is not None and tag in ic:
        return 1
    return None


def _richness(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
    explore_context: Any | None,
) -> int:
    """Pipeline-aware value richness with static fallback."""
    pr = _pipeline_richness(tag, explore_context)
    if pr is not None:
        return pr
    return _value_richness(tag, pdg, program)


def _probe_steps(
    plc: PLC,
    tag: str,
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
    advice: Any = None,
) -> bool:
    """Fork, steer, observe: does *tag* actually visit multiple values?"""
    ext_inputs = _external_bool_inputs(pdg, known, program, advice=advice)
    edge_ext = _edge_tags(pdg, program) & set(ext_inputs)
    alphabet = _steer_alphabet(tag, pdg, known, program, advice=advice)
    start_val = plc.state.tags.get(tag)
    for steer in alphabet:
        trial = plc.fork()
        for action, scans in _steer_prefix(steer, dict(trial.state.tags), ext_inputs, edge_ext):
            if action:
                trial.patch(action)
            for _ in range(scans):
                trial.step()
            if trial.state.tags.get(tag) != start_val:
                return True
        for _ in range(_PULSE_REACT_CAP):
            trial.step()
            if trial.state.tags.get(tag) != start_val:
                return True
    return False


def _governing(
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    explore_context: Any | None = None,
    plc: PLC | None = None,
    probe_memo: dict[str, bool] | None = None,
    advice: Any = None,
) -> tuple[str, Any]:
    """Pick the governing tag/value for the corridor.

    If *target_tag* steps through multiple values, it governs itself.
    Otherwise it is a derived view (e.g. an ``out`` coil); delegate to the
    richest stateful tag that gates the writer producing *target_value*.

    When *plc* is provided, governance is decided by simulation probe —
    fork, steer, observe whether the tag actually visits multiple values.
    This is ground truth: immune to copy-chain, calc-wrapper, or any
    other write-mechanism blindness that defeats static classification.
    Static signals (``stepping_tags``, ``_value_richness``) are used only
    as a fast path before the probe.

    *probe_memo* caches the probe result per tag across one walk
    (``_WalkContext.probe_memo``); ``None`` probes fresh every call.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import Literal

    # Fast path: static signals say it steps — trust without probing.
    stepping = (
        getattr(explore_context, "stepping_tags", None) if explore_context is not None else None
    )
    if stepping is not None and target_tag in stepping:
        return target_tag, target_value
    if stepping is None and _value_richness(target_tag, pdg, program) >= 2:
        return target_tag, target_value

    # Simulation probe: the program is the model.
    if plc is not None:
        if probe_memo is not None and target_tag in probe_memo:
            probed = probe_memo[target_tag]
        else:
            probed = _probe_steps(
                plc, target_tag, pdg, plc._known_tags_by_name, program, advice=advice
            )
            if probe_memo is not None:
                probe_memo[target_tag] = probed
        if probed:
            return target_tag, target_value

    # A free-running self-calc — or a copy/offset view of one or of an
    # accumulator — is a clock, not a corridor: it advances by itself, so
    # delegating governance to it value-steps a hopeless graph.  Keep
    # governance downstream and let the fold ride the clock (its crossings
    # are in the jump context when affine/translated).
    skip_clocks = advice is None or advice.has("fold_modwrap_source")
    skip_views = advice is None or advice.has("fold_derived_crossings")
    skip_transients = advice is None or advice.has("transient_handshake")
    known = plc._known_tags_by_name if plc is not None else None
    acc_names: frozenset[str] | None = None

    def is_clock(gt: str) -> bool:
        nonlocal acc_names
        if skip_clocks and _is_free_running_selfcalc(gt, pdg, program):
            return True
        if not skip_views:
            return False
        if acc_names is None:
            acc_names = frozenset(s.acc_name for s in _collect_acc_sources(program))
        return _is_clock_view(gt, pdg, program, acc_names)

    def skip_candidate(gt: str) -> bool:
        if is_clock(gt):
            return True
        # A consumed-same-scan handshake tag has no boundary corridor —
        # delegating governance to it value-steps a hopeless graph
        # (findings §2a); the transient-handshake bundles fire it mid-scan.
        return skip_transients and _is_scan_transient(gt, pdg, program, known)

    best: tuple[str, Any] | None = None
    best_rich = 1
    for ri in pdg.writers_of.get(target_tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, target_tag)
        if isinstance(wv, Literal) and wv.value != target_value:
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        sp_expr = _sp_to_expr(sp)
        for gt, gvals in _extract_condition_values(sp_expr).items():
            if gt == target_tag or pdg.tag_roles.get(gt) == TagRole.INPUT:
                continue
            if skip_candidate(gt):
                continue
            rich = _richness(gt, pdg, program, explore_context)
            if rich > best_rich:
                best = (gt, next(iter(gvals)))
                best_rich = rich
        for gt, gval in _extract_inequality_governing(sp_expr).items():
            if gt == target_tag or pdg.tag_roles.get(gt) == TagRole.INPUT:
                continue
            if skip_candidate(gt):
                continue
            rich = _richness(gt, pdg, program, explore_context)
            if rich > best_rich:
                best = (gt, gval)
                best_rich = rich
    return best if best is not None else (target_tag, target_value)


def _latch_break_conditions(
    tag: str,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
) -> list[tuple[str, Any]]:
    """Conditions that would break a seal-in / OTE hold for *tag*.

    When a tag must reach its default (False for Bool) and no writer produces
    that value directly, the path is making the writer rung's condition
    evaluate to False.  For ``And`` nodes any single conjunct suffices;
    self-references (the tag itself in the condition) are skipped.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, _sp_to_expr

    result: list[tuple[str, Any]] = []
    seen: set[str] = set()

    def _break_candidates(e: Any) -> list[tuple[str, Any]]:
        if isinstance(e, Atom):
            if e.tag == tag:
                return []
            if e.form == "xio":
                return [(e.tag, True)]
            if e.form in ("xic", "truthy"):
                return [(e.tag, False)]
            return []
        if isinstance(e, And):
            candidates: list[tuple[str, Any]] = []
            for term in e.terms:
                candidates.extend(_break_candidates(term))
            return candidates
        return []

    for ri in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        for btag, bval in _break_candidates(_sp_to_expr(sp)):
            if btag in seen:
                continue
            current = snapshot.get(btag)
            if not _values_match(current, bval):
                seen.add(btag)
                result.append((btag, bval))

    return result


def _unsatisfied_conditions(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    known: dict[str, Any] | None = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
) -> list[tuple[str, Any]]:
    """Enabling conditions not yet met for *tag* to reach *value*.

    Inspects the writer rung(s) that produce *value* and returns the
    ``(tag, needed_value)`` pairs from their enabling conditions that
    differ from the current *snapshot*.  For subroutine writers the
    call-site condition is included.  When no writer produces *value*,
    falls back to :func:`_latch_break_conditions`.

    Scan-transient handshake tags (:func:`_is_scan_transient`) are skipped:
    they are provably at their default at every scan boundary, so a
    boundary goal for their set value is structurally unreachable and only
    poisons recovery — the transient-handshake steer bundles cover those
    gates mid-scan instead (findings §2a).

    This is the cross-writer UNION — the per-writer alternatives behind it
    come from :func:`_unsatisfied_condition_groups`.
    """
    return _unsatisfied_condition_groups(
        tag,
        value,
        snapshot,
        pdg,
        program,
        nd_domains=nd_domains,
        known=known,
        func_deps=func_deps,
    )[0]


def _predecessor_via_crossing(ro: Any, tag: str, value: Any) -> Any | None:
    """Derive the required predecessor via the crossings registry (CalcCrossing)."""
    if isinstance(value, bool):
        return None
    from pyrung.core.analysis import crossings
    from pyrung.core.analysis.sp_values import _writer_for_tag
    from pyrung.core.crossing import CrossingContext, Eq, eq_target

    instr = _writer_for_tag(ro, tag)
    if instr is None:
        return None
    result = crossings.reverse(instr, ro, eq_target(tag, value), CrossingContext())
    if result.fallthrough or len(result.branches) != 1:
        return None
    (branch,) = result.branches
    if len(branch) != 1:
        return None
    constraint = branch[0]
    if isinstance(constraint, Eq) and len(constraint.values) == 1 and constraint.tag == tag:
        return next(iter(constraint.values))
    return None


def _atom_holds_for_value(atom: Any, tag: str, value: Any, snapshot: dict[str, Any]) -> bool:
    if getattr(atom, "tag", None) != tag:
        return True
    form = getattr(atom, "form", None)
    operand = getattr(atom, "operand", None)
    if isinstance(operand, str):
        operand = value if operand == tag else snapshot.get(operand)
    try:
        if form in {"xic", "truthy", "rise"}:
            return bool(value)
        if form in {"xio", "fall"}:
            return not bool(value)
        if form == "eq":
            return value == operand
        if form == "ne":
            return value != operand
        if form == "lt":
            return value < operand
        if form == "le":
            return value <= operand
        if form == "gt":
            return value > operand
        if form == "ge":
            return value >= operand
    except TypeError:
        return False
    return True


def _arith_atom_holds_for_value(atom: Any, tag: str, value: Any, snapshot: dict[str, Any]) -> bool:
    if getattr(atom, "left", None) != tag and getattr(atom, "right", None) != tag:
        return True
    left = value if atom.left == tag else snapshot.get(atom.left)
    right = value if atom.right == tag else snapshot.get(atom.right)
    if (
        not isinstance(left, (int, float))
        or isinstance(left, bool)
        or not isinstance(right, (int, float))
        or isinstance(right, bool)
    ):
        return False
    try:
        if atom.arith_op == "+":
            actual = left + right
        elif atom.arith_op == "-":
            actual = left - right
        elif atom.arith_op == "*":
            actual = left * right
        else:
            return True
        if atom.form == "eq":
            return actual == atom.operand
        if atom.form == "ne":
            return actual != atom.operand
        if atom.form == "lt":
            return actual < atom.operand
        if atom.form == "le":
            return actual <= atom.operand
        if atom.form == "gt":
            return actual > atom.operand
        if atom.form == "ge":
            return actual >= atom.operand
    except TypeError:
        return False
    return True


def _self_conditions_allow_value(expr: Any, tag: str, value: Any, snapshot: dict[str, Any]) -> bool:
    """Whether *expr* can fire when *tag* holds *value* before the scan.

    Non-self conditions are treated as satisfiable prereqs; this helper only
    rejects writers whose own tag guards contradict the arithmetic predecessor
    (for example ``Step == 5`` on a ``Step - 1`` writer cannot produce
    ``Step == 5`` from predecessor ``6``).
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Const, Or

    if isinstance(expr, Const):
        return expr.value
    if isinstance(expr, Atom):
        return _atom_holds_for_value(expr, tag, value, snapshot)
    if isinstance(expr, ArithAtom):
        return _arith_atom_holds_for_value(expr, tag, value, snapshot)
    if isinstance(expr, And):
        return all(_self_conditions_allow_value(t, tag, value, snapshot) for t in expr.terms)
    if isinstance(expr, Or):
        return any(_self_conditions_allow_value(t, tag, value, snapshot) for t in expr.terms)
    return True


def _live_satisfied_conditions(expr: Any, snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    """Satisfied guard atoms from the currently-live path through *expr*.

    ``_extract_condition_values`` intentionally drops non-common ``Or``
    branches for prerequisites.  For candidate context we still need the
    branch that is already true in the snapshot, because it is exactly the
    state the child walk should preserve.
    """
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state
    from pyrung.core.analysis.simplified import And, Atom, Or
    from pyrung.core.analysis.sp_values import _extract_condition_values

    def _holds(e: Any) -> bool:
        return _eval_expr_from_state(e, snapshot) is True

    def _collect(e: Any) -> list[tuple[str, Any]]:
        if isinstance(e, Atom):
            out: list[tuple[str, Any]] = []
            for tag, value in _extract_condition_values(e).items():
                current = snapshot.get(tag)
                for needed in value:
                    if _values_match(current, needed):
                        out.append((tag, needed))
            return out
        if isinstance(e, And):
            out: list[tuple[str, Any]] = []
            for term in e.terms:
                out.extend(_collect(term))
            return out
        if isinstance(e, Or):
            out: list[tuple[str, Any]] = []
            for term in e.terms:
                if _holds(term):
                    out.extend(_collect(term))
            return out
        return []

    seen: set[tuple[str, Any]] = set()
    result: list[tuple[str, Any]] = []
    for pair in _collect(expr):
        if pair in seen:
            continue
        seen.add(pair)
        result.append(pair)
    return result


def _writer_candidates(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    known: dict[str, Any] | None = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
) -> tuple[list[tuple[str, Any]], list[_WriterCandidate]]:
    """Writer enabling conditions as ``(union, per-writer candidates)``.

    The union half is :func:`_unsatisfied_conditions`' historical output:
    conditions merged across every writer producing *value*, inequality
    prerequisites from every writer, and the latch-break fallback.  The
    candidates half splits the same extraction per matched writer while
    preserving each writer's full guard context, the satisfied/unsatisfied
    split against *snapshot*, and its static write footprint.  The
    unsatisfied projection is intentionally the same content the historical
    per-writer groups exposed.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _named_copy_source,
        _writer_for_tag,
        _written_value_for_tag,
        copy_source_binding,
    )
    from pyrung.core.crossing import Affine, Literal
    from pyrung.core.instruction.coils import OutInstruction

    merged: dict[str, set[Any]] = {}
    writer_rungs: list[int] = []
    writer_conds: list[dict[str, set[Any]]] = []
    writer_self_conds: list[dict[str, set[Any]]] = []
    any_writer_matched = False

    def _add(acc: dict[str, set[Any]], cvals: dict[str, frozenset[Any]]) -> None:
        for t, vs in cvals.items():
            acc.setdefault(t, set()).update(vs)

    def _temporal_done_writer_matches(ro: Any, tag_name: str, target_value: Any) -> bool:
        """True when a timer/counter can eventually produce done_bit=True."""
        if target_value is not True:
            return False
        return any(
            getattr(i, "done_bit", None) is not None
            and getattr(i.done_bit, "name", None) == tag_name
            for i in getattr(ro, "_instructions", ())
        )

    for ri in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        _instr = _writer_for_tag(ro, tag)
        _copy_src = _named_copy_source(_instr) if _instr is not None else None
        wv_classified = isinstance(wv, (Literal, Affine)) or _copy_src is not None
        temporal_done_writer = _temporal_done_writer_matches(ro, tag, value)
        is_ote = any(
            isinstance(i, OutInstruction) and getattr(i.target, "name", None) == tag
            for i in ro._instructions
        )
        idx_chase: tuple[str, list[int]] | None = None
        if wv_classified:
            if isinstance(wv, Literal) and not _values_match(wv.value, value):
                continue
            if isinstance(wv, Affine):
                predecessor = _predecessor_via_crossing(ro, tag, value)
                if predecessor is None:
                    continue
                sp = ro.sp_tree()
                if sp is not None and not _self_conditions_allow_value(
                    _sp_to_expr(sp), tag, predecessor, snapshot
                ):
                    continue
        elif not temporal_done_writer and (not is_ote or not value):
            # Indirect-copy writer (copy(block[idx], tag)): the source is
            # statically unresolvable, but the table is readable on the
            # live snapshot — chase the *index register* instead of the
            # source bank (idx-chasing, Open #11).  No inverting index
            # value means the old honest refusal.
            isrc = _indirect_copy_source(ro, tag) if value is not None else None
            if isrc is not None:
                idx_chase = _invert_indirect_source(
                    isrc, tag, value, snapshot, pdg, program, nd_domains, func_deps=func_deps
                )
            if idx_chase is None:
                continue
        any_writer_matched = True

        # Copy-from-tag writer: the source holding *value* is the data-flow
        # half of the regression — as much a prerequisite as the rung's
        # gate (the sm_copy_or_jump_state shape: a state register written
        # only by copy(Requested, Current) carries no literal to match).
        # The filter below applies the same snapshot and transient
        # filtering as condition-derived prerequisites.  An indirect-copy
        # writer binds the chased index register the same way, best
        # (current-first) inverting value in the union.
        own: dict[str, set[Any]] = {}
        own_self: dict[str, set[Any]] = {}
        copy_binding = copy_source_binding(ro, tag, value) if value is not None else None
        if copy_binding is not None:
            own.setdefault(copy_binding[0], set()).add(copy_binding[1])
        elif isinstance(wv, Affine):
            predecessor = _predecessor_via_crossing(ro, tag, value)
            if predecessor is not None:
                own_self.setdefault(tag, set()).add(predecessor)
        elif idx_chase is not None:
            own.setdefault(idx_chase[0], set()).add(idx_chase[1][0])

        sp = ro.sp_tree()
        if sp is not None:
            expr = _sp_to_expr(sp)
            _add(own, _extract_condition_values(expr))
            for live_tag, live_val in _live_satisfied_conditions(expr, snapshot):
                own.setdefault(live_tag, set()).add(live_val)

        if node.subroutine is not None:
            for caller in pdg.rung_nodes:
                if node.subroutine in caller.calls:
                    cro = _resolve_rung(program, caller)
                    if cro is None:
                        continue
                    csp = cro.sp_tree()
                    if csp is not None:
                        cexpr = _sp_to_expr(csp)
                        _add(own, _extract_condition_values(cexpr))
                        for live_tag, live_val in _live_satisfied_conditions(cexpr, snapshot):
                            own.setdefault(live_tag, set()).add(live_val)

        for t, vs in own.items():
            merged.setdefault(t, set()).update(vs)
        for t, vs in own_self.items():
            merged.setdefault(t, set()).update(vs)
        writer_rungs.append(ri)
        writer_conds.append(own)
        writer_self_conds.append(own_self)

        # Each further inverting index value is a genuine alternative (the
        # index can hold only one value at a time), so it gets its own
        # group — same gates, different binding — never a conjunct in the
        # primary group or the union.
        if idx_chase is not None:
            for extra in idx_chase[1][1:]:
                alt = {t: set(vs) for t, vs in own.items()}
                alt[idx_chase[0]] = {extra}
                writer_rungs.append(ri)
                writer_conds.append(alt)
                writer_self_conds.append({})

    trans_memo: dict[str, tuple[bool, Any]] = {}

    def _transient_rest(cond_tag: str) -> tuple[bool, Any]:
        got = trans_memo.get(cond_tag)
        if got is None:
            got = _scan_transient_rest(cond_tag, pdg, program, known)
            trans_memo[cond_tag] = got
        return got

    def _condition_pairs(
        conds: dict[str, set[Any]],
        self_conds: dict[str, set[Any]] | None = None,
        *,
        unsatisfied_only: bool,
        skip_nonpredecessor_self: bool,
    ) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for cond_tag, needed_vals in conds.items():
            current = snapshot.get(cond_tag)
            transient, rest = _transient_rest(cond_tag)
            for nv in needed_vals:
                if (
                    skip_nonpredecessor_self
                    and cond_tag == tag
                    and (
                        self_conds is None
                        or not any(_values_match(nv, v) for v in self_conds.get(cond_tag, ()))
                    )
                ):
                    continue
                if transient and not _values_match(nv, rest):
                    continue
                if not unsatisfied_only or not _values_match(current, nv):
                    out.append((cond_tag, nv))
        return out

    def _filter_unsatisfied(
        conds: dict[str, set[Any]],
        self_conds: dict[str, set[Any]] | None = None,
    ) -> list[tuple[str, Any]]:
        return _condition_pairs(
            conds,
            self_conds,
            unsatisfied_only=True,
            skip_nonpredecessor_self=True,
        )

    def _filter_full(
        conds: dict[str, set[Any]],
        self_conds: dict[str, set[Any]] | None = None,
    ) -> list[tuple[str, Any]]:
        return _condition_pairs(
            conds,
            self_conds,
            unsatisfied_only=False,
            skip_nonpredecessor_self=True,
        )

    merged_self: dict[str, set[Any]] = {}
    for own_self in writer_self_conds:
        for t, vs in own_self.items():
            merged_self.setdefault(t, set()).update(vs)

    result = _filter_unsatisfied(merged, merged_self)

    # Inequality prerequisites: resolve gt/ge/lt/le atoms from writer conditions
    # against pipeline domains.  These are typically INPUT tags (external ND
    # inputs the operator can set) — the equality path above skips INPUTs, but
    # for inequalities the walker needs to steer them to specific values.
    # The union dedupes by tag name across all writers (historical
    # behavior); each matched writer's group keeps its own full list.
    per_writer_ineq: dict[int, list[tuple[str, Any]]] = {}
    if nd_domains:
        equality_tags = {r[0] for r in result}
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            ro = _resolve_rung(program, node)
            if ro is None:
                continue
            sp = ro.sp_tree()
            if sp is None:
                continue
            collected = _extract_inequality_prereqs(
                _sp_to_expr(sp), snapshot, nd_domains, pdg, program, func_deps
            )
            if node.subroutine is not None:
                for caller in pdg.rung_nodes:
                    if node.subroutine in caller.calls:
                        cro = _resolve_rung(program, caller)
                        if cro is None:
                            continue
                        csp = cro.sp_tree()
                        if csp is None:
                            continue
                        collected.extend(
                            _extract_inequality_prereqs(
                                _sp_to_expr(csp), snapshot, nd_domains, pdg, program, func_deps
                            )
                        )
            per_writer_ineq[ri] = collected
            for itag, ival in collected:
                if itag != tag and itag not in equality_tags:
                    result.append((itag, ival))
                    equality_tags.add(itag)

    candidates: list[_WriterCandidate] = []
    for ri, own, own_self in zip(writer_rungs, writer_conds, writer_self_conds, strict=True):
        group_conds = {t: set(vs) for t, vs in own.items()}
        for t, vs in own_self.items():
            group_conds.setdefault(t, set()).update(vs)
        full = _filter_full(group_conds, own_self)
        group = _filter_unsatisfied(group_conds, own_self)
        gtags = {g[0] for g in group}
        for itag, ival in per_writer_ineq.get(ri, ()):
            if itag != tag and itag not in gtags:
                group.append((itag, ival))
                gtags.add(itag)
            if (itag, ival) not in full:
                full.append((itag, ival))
        satisfied = tuple(p for p in full if _values_match(snapshot.get(p[0]), p[1]))
        candidates.append(
            _WriterCandidate(
                full_conditions=tuple(full),
                satisfied=satisfied,
                unsatisfied=tuple(group),
                all_writes=pdg.rung_nodes[ri].all_writes,
                writer_index=ri,
            )
        )

    if not result and not any_writer_matched and value is False:
        result = _latch_break_conditions(tag, snapshot, pdg, program)

    return result, candidates


def _unsatisfied_condition_groups(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    known: dict[str, Any] | None = None,
    func_deps: dict[str, tuple[str, int, Any]] | None = None,
) -> tuple[list[tuple[str, Any]], list[list[tuple[str, Any]]]]:
    """Unsatisfied enabling conditions as ``(union, per-writer groups)``.

    Compatibility projection of :func:`_writer_candidates`.  Empty
    unsatisfied candidates are omitted here to preserve the historical group
    API; callers that need the already-armed writer case should use
    :func:`_writer_candidates`.
    """
    union, candidates = _writer_candidates(
        tag,
        value,
        snapshot,
        pdg,
        program,
        nd_domains=nd_domains,
        known=known,
        func_deps=func_deps,
    )
    groups = [list(c.unsatisfied) for c in candidates if c.unsatisfied]
    return union, groups


def _literal_write(ro: Any, tag: str) -> Any | None:
    """The literal value rung *ro* writes to *tag*, or ``None``.

    Range-aware sibling of ``_written_value_for_tag``: latch/reset/copy/fill
    instructions whose target is a block range (``reset(c.select(...))``)
    resolve for any covered tag.  Non-literal writes (copy-from-tag, calc)
    and unknown shapes return ``None``.
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in ro._instructions:
        target = getattr(instr, "target", None)
        if target is None:
            target = getattr(instr, "dest", None)
        if target is None:
            continue
        name = getattr(target, "name", None)
        if name is not None:
            names = {name}
        elif hasattr(target, "tags"):
            names = {getattr(t, "name", None) for t in target.tags()}
        else:
            continue
        if tag not in names:
            continue
        if isinstance(instr, ResetInstruction):
            return False
        if isinstance(instr, LatchInstruction):
            return True
        if isinstance(instr, CopyInstruction):
            src = instr.source
            if hasattr(src, "name"):
                return getattr(src, "default", None) if getattr(src, "readonly", False) else None
            # IndirectRef (block[pointer]) and other opaque sources are not
            # literals — their comparison operators build deferred Conditions.
            return src if isinstance(src, (bool, int, float, str)) else None
        if isinstance(instr, FillInstruction):
            val = instr.value
            if hasattr(val, "name"):
                return None
            return val if isinstance(val, (bool, int, float, str)) else None
        return None  # calc/out/other write shapes — not a known literal
    return None


def _ack_cleared_bool_inputs(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any,
) -> list[str]:
    """Operator-driven Bools the program only ever clears (acknowledge pattern).

    PackML HMI command bits — the mode-change request, the mode-select
    trio — are written by the program exclusively with ``reset()``/zero
    acknowledgements (often as a range reset), so their ``TagRole`` is not
    INPUT; yet only the operator can ever set them.  For steering purposes
    they are external inputs: a plan that pulses them is exactly what the
    HMI does.  Conservative: any writer whose write is not a provable
    literal default disqualifies the tag.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.tag import TagType

    result: list[str] = []
    for tag, t in known.items():
        if getattr(t, "type", None) is not TagType.BOOL:
            continue
        if pdg.tag_roles.get(tag) == TagRole.INPUT:
            continue  # already a steerable input
        writers = pdg.writers_of.get(tag, frozenset())
        if not writers or not pdg.readers_of.get(tag, frozenset()):
            continue
        default = t.default
        ok = True
        for ri in writers:
            node = pdg.rung_nodes[ri]
            if tag in node.ote_writes:
                ok = False
                break
            ro = _resolve_rung(program, node)
            lw = _literal_write(ro, tag) if ro is not None else None
            if lw is None or not _values_match(lw, default):
                ok = False
                break
        if ok:
            result.append(tag)
    return sorted(result)


def _reference_constants(pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Never-written reference constants the program reads as data sources.

    The PackML ``sm__STATE*REF`` shape: registers with no program writers
    (their values come from declared initial data — operator-writable in
    principle, configuration in practice) that feed ``copy``/``fill``
    sources.  Regression happily offers to rewrite one whenever the copy
    destination needs a value the constant doesn't hold — "make
    ``sm__STATEIDLEREF`` equal 3" — a sound but operator-meaningless detour,
    and a whole bank of them floods recovery (findings, Open Items #11).

    Used as ordering advice only (``ref_constant_order``): goals and writer
    groups that would *mutate* one of these are tried after every
    alternative.  Deliberately not membership-tested on read shape beyond
    the data-source requirement — a zero-writer tag read only in conditions
    (an ordinary setpoint) is not collected.
    """
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    sources: set[str] = set()

    def _scan(rungs: Any) -> None:
        for rung in rungs:
            for instr in getattr(rung, "_instructions", ()):
                if isinstance(instr, CopyInstruction):
                    src = instr.source
                    name = getattr(src, "name", None)
                    if name and not getattr(src, "readonly", False):
                        sources.add(name)
                elif isinstance(instr, FillInstruction):
                    name = getattr(instr.value, "name", None)
                    if name:
                        sources.add(name)
            _scan(getattr(rung, "_branches", ()))

    _scan(program.rungs)
    for sub_rungs in getattr(program, "subroutines", {}).values():
        _scan(sub_rungs)

    return frozenset(n for n in sources if not pdg.writers_of.get(n, frozenset()))


def _is_scan_transient(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any] | None = None,
) -> bool:
    """Whether *tag* is a consumed-same-scan handshake (see the rest form)."""
    transient, _rest = _scan_transient_rest(tag, pdg, program, known)
    return transient


def _scan_transient_rest(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    """Whether *tag* provably rests at one value at every scan boundary.

    The consumed-same-scan handshake shape (PackML command/mode protocols):
    producer rungs set the tag, consumers act on it, and a later rung
    clears it back to its *resting value* — all within one scan.  A
    boundary goal ``(tag, v)`` with ``v`` different from the resting value
    is structurally unreachable and poisons recovery (findings §2a).
    Returns ``(True, resting_value)`` or ``(False, None)``.

    The resting value is inferred from the clearers themselves, not the
    tag's declared default — a Click project may declare a nonzero initial
    value (the template's ``C_UnitMode`` initializes to 5) while the
    protocol rests at 0 from the first handshake on.

    Proof obligations, all static: every writer writes a provable literal
    (range targets resolved); for some candidate resting value R, every
    producer (write ≠ R) lives in one scope and some clearer (write == R)
    fires whenever the tag holds a produced value — a bare positive contact
    (every produced value truthy) or an ``==`` comparison matching every
    produced value, possibly inside an ``Or``, or no condition at all — and
    runs after every producer: a later rung in the producers' scope, or an
    unconditional(-when-set) rung inside a subroutine whose call gate
    fires-when-set at a later rung in the producers' scope (the PackML
    ``rung(ReqBool == 1): call(mode_change)`` shape).  Any shape this can't
    prove is not transient — the tag keeps its boundary goals
    (conservative direction).

    The claim covers *program* writes only: an operator patch outside the
    handshake cycle may survive a boundary.  That matches the walker's use —
    skipped boundary goals are exactly those it has no steer for, and the
    handshake bundles cover the program route.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import Atom, Or, _sp_to_expr

    del known  # the resting value is inferred from clearers, not defaults
    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return False, None

    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return False, None
    writes: list[tuple[Any, Any, Any]] = []  # (node, rung, literal value)
    for ri in writer_idxs:
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            return False, None
        if tag in node.ote_writes:
            return False, None  # OTE reflects its condition at the boundary
        lw = _literal_write(ro, tag)
        if lw is None:
            return False, None
        writes.append((node, ro, lw))

    candidate_rests: list[Any] = []
    for _n, _r, v in writes:
        if not any(_values_match(v, c) for c in candidate_rests):
            candidate_rests.append(v)

    for rest in candidate_rests:
        producers = [(n, v) for n, _r, v in writes if not _values_match(v, rest)]
        clearers = [(n, r) for n, r, v in writes if _values_match(v, rest)]
        if not producers or not clearers:
            continue
        prod_scopes = {n.subroutine for n, _v in producers}
        if len(prod_scopes) != 1:
            continue
        pscope = next(iter(prod_scopes))
        produced_vals = [v for _n, v in producers]
        last_producer = max(n.rung_index for n, _v in producers)

        def _fires_when_set(e: Any, produced: tuple[Any, ...] = tuple(produced_vals)) -> bool:
            if isinstance(e, Atom):
                if e.tag != tag:
                    return False
                if e.form in ("xic", "truthy"):
                    return all(bool(v) for v in produced)
                return e.form == "eq" and all(_values_match(e.operand, v) for v in produced)
            if isinstance(e, Or):
                return any(_fires_when_set(term, produced) for term in e.terms)
            return False

        for node, ro in clearers:
            sp = ro.sp_tree()
            if sp is not None and not _fires_when_set(_sp_to_expr(sp)):
                continue
            if node.subroutine == pscope:
                if node.rung_index > last_producer:
                    return True, rest
                continue
            if node.subroutine is None:
                continue
            # Cross-scope: clearer inside a subroutine whose call gate
            # fires whenever the tag is set, called after every producer.
            for cnode in pdg.rung_nodes:
                if (
                    node.subroutine in cnode.calls
                    and cnode.subroutine == pscope
                    and cnode.rung_index > last_producer
                ):
                    cro = _resolve_rung(program, cnode)
                    if cro is None:
                        continue
                    csp = cro.sp_tree()
                    if csp is not None and _fires_when_set(_sp_to_expr(csp)):
                        return True, rest
    return False, None


_MAX_HANDSHAKE_DEPTH = 4
_MAX_HANDSHAKE_STATES = 6
_MAX_HANDSHAKE_BUNDLES = 12


def _transient_handshake_bundles(
    governing: str,
    gov_value: Any,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any],
    ext_inputs: set[str],
    nd_domains: dict[str, tuple[Any, ...]] | None,
) -> list[dict[str, Any]]:
    """Simultaneous patches that fire consumed-same-scan handshake chains.

    When a writer of *governing* is gated on a scan-transient tag (see
    :func:`_is_scan_transient`) — directly, through a comparison, through
    its subroutine's call gate, or as its copy-source — no boundary goal
    can establish that gate: the whole chain must fire within one scan.
    Regress recursively (depth-bounded, cycle-guarded): each transient
    requirement expands through the producer rungs whose written literal
    satisfies it; each producer contributes its own external requirements
    (Bool polarities, satisfying values for comparisons on ND inputs) and,
    when it lives in a subroutine, its call-gate requirements.  A writer's
    ``copy(S, governing)`` with a concrete *gov_value* binds ``S =
    gov_value`` (external S) or regresses through S's producers (transient
    S).  The union of one consistent choice per requirement is emitted as
    one multi-input patch.  Candidates only — every bundle is validated by
    the interpreted trial like any other steer.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _named_copy_source,
        _writer_for_tag,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import Literal

    _POS = {"xic", "rise", "truthy"}
    _NEG = {"xio", "fall"}
    _INEQ = {"gt", "ge", "lt", "le"}

    def _cmp_ok(form: str, operand: Any, v: Any) -> bool:
        if form == "eq":
            return _values_match(v, operand)
        if form == "truthy":
            return bool(v)
        op = _CMP_OPS.get(form)
        if op is None:
            return False
        try:
            return bool(op(v, operand))
        except TypeError:
            return False

    def _merge(patch: dict[str, Any], key: str, val: Any) -> bool:
        prev = patch.get(key)
        if prev is not None and not _values_match(prev, val):
            return False
        patch[key] = val
        return True

    rest_memo: dict[str, tuple[bool, Any]] = {}

    def _rest_of(t: str) -> tuple[bool, Any]:
        if t not in rest_memo:
            rest_memo[t] = _scan_transient_rest(t, pdg, program, known)
        return rest_memo[t]

    # A state is (patch, cons): the simultaneous patch under construction
    # plus pending comparison constraints on external ND inputs.
    def _collect_reqs(
        e: Any,
        patch: dict[str, Any],
        cons: dict[str, list],
        trans: dict[str, list],
    ) -> bool:
        """Fold *e*'s atoms; transient internals land in *trans* as
        constraints to regress.  False = contradictory requirements."""
        if isinstance(e, And):
            return all(_collect_reqs(t, patch, cons, trans) for t in e.terms)
        if isinstance(e, Or):
            return True  # may already hold at runtime; the trial decides
        if isinstance(e, Atom):
            if e.tag in ext_inputs and (e.form in _POS or e.form in _NEG):
                return _merge(patch, e.tag, e.form in _POS)
            if pdg.tag_roles.get(e.tag) == TagRole.INPUT and (e.form in _INEQ or e.form == "eq"):
                cons.setdefault(e.tag, []).append((e.form, e.operand))
                return True
            if e.tag not in ext_inputs:
                transient, rest = _rest_of(e.tag)
                if transient:
                    if e.form in ("xic", "truthy"):
                        req = ("truthy", None)
                    elif e.form == "eq" or e.form in _INEQ:
                        req = (e.form, e.operand)
                    else:
                        return True
                    # A requirement the resting value already satisfies
                    # holds at every boundary — nothing to regress.
                    if not _cmp_ok(req[0], req[1], rest):
                        trans.setdefault(e.tag, []).append(req)
            return True
        return True

    def _expand_rung(
        node: Any,
        ro: Any,
        patch: dict[str, Any],
        cons: dict[str, list],
        depth: int,
        visited: frozenset[str],
        extra_trans: dict[str, list] | None = None,
    ) -> list[tuple[dict[str, Any], dict[str, list]]]:
        """All consistent (patch, cons) extensions that make *ro* fire."""
        if depth > _MAX_HANDSHAKE_DEPTH:
            return []
        patch = dict(patch)
        cons = {k: list(v) for k, v in cons.items()}
        trans: dict[str, list] = {k: list(v) for k, v in (extra_trans or {}).items()}
        sp = ro.sp_tree()
        if sp is not None and not _collect_reqs(_sp_to_expr(sp), patch, cons, trans):
            return []
        states = [(patch, cons)]
        # The rung fires only when its subroutine is called: fold call-gate
        # requirements (each call site is an alternative).
        if node.subroutine is not None:
            call_states: list[tuple[dict[str, Any], dict[str, list]]] = []
            for cnode in pdg.rung_nodes:
                if node.subroutine not in cnode.calls:
                    continue
                cro = _resolve_rung(program, cnode)
                if cro is None:
                    continue
                for p, c in states:
                    call_states.extend(_expand_rung(cnode, cro, p, c, depth + 1, visited))
                    if len(call_states) >= _MAX_HANDSHAKE_STATES:
                        break
            states = call_states[:_MAX_HANDSHAKE_STATES]
        # Transient requirements: regress through producers whose written
        # literal satisfies every constraint on the tag.
        for ttag, clist in trans.items():
            if ttag in visited:
                return []  # cycle — refuse
            _transient, ttag_rest = _rest_of(ttag)
            new_states: list[tuple[dict[str, Any], dict[str, list]]] = []
            for pi in pdg.writers_of.get(ttag, frozenset()):
                pnode = pdg.rung_nodes[pi]
                pro = _resolve_rung(program, pnode)
                if pro is None:
                    continue
                pval = _literal_write(pro, ttag)
                if pval is None or _values_match(pval, ttag_rest):
                    continue  # producers only
                if not all(_cmp_ok(f, o, pval) for f, o in clist):
                    continue
                for p, c in states:
                    new_states.extend(_expand_rung(pnode, pro, p, c, depth + 1, visited | {ttag}))
                    if len(new_states) >= _MAX_HANDSHAKE_STATES:
                        break
            states = new_states[:_MAX_HANDSHAKE_STATES]
            if not states:
                return []
        return states

    def _pick_satisfying(ctag: str, clist: list) -> Any | None:
        candidates: list[Any] = []
        if nd_domains and ctag in nd_domains:
            candidates.extend(nd_domains[ctag])
        for form, operand in clist:
            if form == "eq":
                candidates.append(operand)
            elif form in _INEQ:
                v = _inequality_satisfying_value(form, operand)
                if v is not None:
                    candidates.append(v)
        for cand in candidates:
            if all(_cmp_ok(f, o, cand) for f, o in clist):
                return cand
        return None

    bundles: list[dict[str, Any]] = []
    seen: set[frozenset] = set()
    for ri in pdg.writers_of.get(governing, frozenset()):
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, governing)
        if (
            isinstance(wv, Literal)
            and gov_value is not None
            and not _values_match(wv.value, gov_value)
        ):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        # Trigger only for writers with a transient element: a transient
        # gate in their own condition, or a transient copy-source.
        probe_trans: dict[str, list] = {}
        _collect_reqs(_sp_to_expr(sp), {}, {}, probe_trans)
        seed_patch: dict[str, Any] = {}
        extra_trans: dict[str, list] = {}
        _instr = _writer_for_tag(ro, governing)
        _copy_src = _named_copy_source(_instr) if _instr is not None else None
        if _copy_src is not None and gov_value is not None:
            src = _copy_src
            if src in ext_inputs or pdg.tag_roles.get(src) == TagRole.INPUT:
                seed_patch[src] = gov_value
            else:
                src_transient, src_rest = _rest_of(src)
                if src_transient and not _values_match(src_rest, gov_value):
                    extra_trans[src] = [("eq", gov_value)]
        if not probe_trans and not extra_trans:
            continue
        for patch, cons in _expand_rung(
            node, ro, seed_patch, {}, 0, frozenset(), extra_trans=extra_trans
        ):
            ok = True
            for ctag, clist in cons.items():
                bound = patch.get(ctag)
                if bound is not None and not isinstance(bound, bool):
                    if not all(_cmp_ok(f, o, bound) for f, o in clist):
                        ok = False
                        break
                    continue
                val = _pick_satisfying(ctag, clist)
                if val is None:
                    ok = False
                    break
                patch[ctag] = val
            if not ok or not patch:
                continue
            key = frozenset(patch.items())
            if key not in seen:
                seen.add(key)
                bundles.append(patch)
            if len(bundles) >= _MAX_HANDSHAKE_BUNDLES:
                return bundles
    return bundles


def _external_bool_inputs(
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any = None,
    *,
    advice: Any = None,
) -> list[str]:
    """External Bool inputs: never-written tags, plus — when *program* is
    given and the ``ack_cleared_inputs`` pass is enabled — operator-driven
    Bools the program only ever clears (:func:`_ack_cleared_bool_inputs`)."""
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.tag import TagType

    out: list[str] = []
    for tag, role in pdg.tag_roles.items():
        if role != TagRole.INPUT:
            continue
        t = known.get(tag)
        if t is not None and t.type is TagType.BOOL:
            out.append(tag)
    if program is not None and (advice is None or advice.has("ack_cleared_inputs")):
        out.extend(_ack_cleared_bool_inputs(pdg, known, program))
    return sorted(out)


def _edge_tags(pdg: ProgramGraph, program: Any) -> set[str]:
    """Tag names read through ``rise()``/``fall()`` anywhere in the program."""
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

    result: set[str] = set()

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            if e.form in ("rise", "fall"):
                result.add(e.tag)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    seen: set[int] = set()
    for node in pdg.rung_nodes:
        ro = _resolve_rung(program, node)
        if ro is None or id(ro) in seen:
            continue
        seen.add(id(ro))
        sp = ro.sp_tree()
        if sp is not None:
            visit(_sp_to_expr(sp))
    return result


def _steer_alphabet(
    governing: str,
    pdg: ProgramGraph,
    known: dict[str, Any],
    program: Any = None,
    gov_value: Any = None,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    *,
    advice: Any = None,
) -> list[_Steer]:
    """Empty plus pulse/low for each external Bool input in the governing cone,
    plus set-value steers for non-Bool ND inputs with known domains.

    The cone narrows branching; when static cone tracing finds nothing (e.g.
    indirect addressing) it falls back to every external Bool input.

    When *program* and *gov_value* are provided, inputs appearing in the
    simplified enabling condition for the target write-site are tried first,
    and negated inputs (``xio`` form) get a ``"low"`` steer.

    *advice* is the walk's frozen :class:`~.passes._WalkAdvice` — the pass
    registry's only door into alphabet construction.  ``None`` means
    all-enabled (the pre-registry behavior, bit-identical).  Gated advice:
    ``cone_filter`` (narrowing), ``steer_polarity`` (narrowing),
    ``helpful_order`` (ordering), ``set_value_relevance`` (narrowing —
    enabling-named ND inputs keep their full domains, the rest fill a
    bounded remainder).  Disabling a narrowing pass only widens the
    alphabet; disabling the ordering pass only reorders it.
    """

    def _has(pass_name: str) -> bool:
        return advice is None or advice.has(pass_name)

    ext = _external_bool_inputs(pdg, known, program, advice=advice)
    cone = pdg.upstream_slice(governing, follow_calls=False)
    cone_inputs = [c for c in ext if c in cone]
    candidates = cone_inputs if _has("cone_filter") and len(cone_inputs) >= 1 else ext

    # Determine polarity from enabling conditions.
    polarities: dict[str, set[str]] = {}
    if (
        program is not None
        and candidates
        and (_has("steer_polarity") or _has("helpful_order") or _has("set_value_relevance"))
    ):
        polarities = _enabling_inputs(governing, gov_value, pdg, program)
        if polarities and _has("helpful_order"):
            cset = set(candidates)
            relevant_in_cone = [r for r in polarities if r in cset]
            if relevant_in_cone:
                rest = [c for c in candidates if c not in polarities]
                candidates = relevant_in_cone + rest

    # Build steers: for each candidate, choose pulse/low/both based on polarity.
    _NEGATIVE_FORMS = {"xio", "fall"}
    _POSITIVE_FORMS = {"xic", "rise", "truthy"}
    steers: list[_Steer] = [_Steer("empty")]
    for c in candidates:
        forms = polarities.get(c)
        if not _has("steer_polarity"):
            # Ablated narrowing: the conservative direction is both forms.
            needs_high = True
            needs_low = True
        elif forms is not None:
            needs_high = bool(forms & _POSITIVE_FORMS)
            needs_low = bool(forms & _NEGATIVE_FORMS)
        else:
            needs_high = True
            needs_low = False
        if needs_high:
            steers.append(_Steer("pulse", c))
        if needs_low:
            steers.append(_Steer("low", c))
        if not needs_high and not needs_low:
            steers.append(_Steer("pulse", c))

    # Non-Bool ND inputs: add set-value steers from pipeline domains.
    if nd_domains:
        bool_set = set(ext)
        eligible: list[tuple[str, tuple[Any, ...]]] = []
        for nd_name, domain in nd_domains.items():
            if nd_name in bool_set:
                continue
            # Include ND inputs in the governing cone, plus the governing tag
            # itself (an external ND input being walked to a target value).
            if nd_name not in cone and nd_name != governing:
                continue
            eligible.append((nd_name, domain))
        if _has("set_value_relevance") and eligible:
            # Relevance-narrow the flood: inputs named by the governing
            # value's enabling conditions (comparison atoms carry the
            # compared tag) and the governing tag itself keep their full
            # domains; the rest fill a bounded remainder.  On wide programs
            # cones are program-wide, so the cone filter alone leaves
            # hundreds of set steers paid at every explore node.
            relevant = [(n, d) for n, d in eligible if n in polarities or n == governing]
            rest = [(n, d) for n, d in eligible if n not in polarities and n != governing]
            slots = _MAX_SET_VALUE_STEERS - sum(len(d) for _n, d in relevant)
            capped: list[tuple[str, tuple[Any, ...]]] = list(relevant)
            for nd_name, domain in rest:
                if slots <= 0:
                    break
                take = domain[:slots]
                capped.append((nd_name, take))
                slots -= len(take)
            eligible = capped
        for nd_name, domain in eligible:
            for v in domain:
                steers.append(_Steer("set", nd_name, v))

    # Multi-input steers: conjunctive groups from enabling conditions.
    if program is not None and gov_value is not None:
        ext_set = set(ext)
        for group in _conjunctive_input_groups(governing, gov_value, pdg, program, ext_set):
            steers.append(_Steer("multi", patch=group))
        # Consumed-same-scan handshake gates (findings §2a): bundle the
        # transient gate's producer inputs with the writer's own externals
        # so the whole chain fires within one scan.
        if _has("transient_handshake"):
            seen_multi = {
                frozenset(s.patch.items()) for s in steers if s.kind == "multi" and s.patch
            }
            for bundle in _transient_handshake_bundles(
                governing, gov_value, pdg, program, known, ext_set, nd_domains
            ):
                if frozenset(bundle.items()) not in seen_multi:
                    steers.append(_Steer("multi", patch=bundle))

    return steers


def _enabling_inputs(
    governing: str,
    gov_value: Any,
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, set[str]]:
    """Input tag names and their polarities from enabling conditions.

    Returns ``{tag_name: {forms...}}`` where forms are atom forms like
    ``"xic"``, ``"xio"``, ``"rise"``, ``"fall"``.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr
    from pyrung.core.analysis.sp_values import _written_value_for_tag
    from pyrung.core.crossing import Literal

    result: dict[str, set[str]] = {}

    def collect(e: Any) -> None:
        if isinstance(e, Atom):
            result.setdefault(e.tag, set()).add(e.form)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                collect(term)

    seen_rungs: set[int] = set()
    for ri in pdg.writers_of.get(governing, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None or id(ro) in seen_rungs:
            continue
        seen_rungs.add(id(ro))
        if gov_value is not None:
            wv = _written_value_for_tag(ro, governing)
            if isinstance(wv, Literal) and wv.value != gov_value:
                continue
        sp = ro.sp_tree()
        if sp is not None:
            collect(_sp_to_expr(sp))

    return result


def _conjunctive_input_groups(
    governing: str,
    gov_value: Any,
    pdg: ProgramGraph,
    program: Any,
    ext_inputs: set[str],
) -> list[dict[str, Any]]:
    """Extract multi-input patches from conjunctive enabling conditions.

    Walks the SP-tree of each writer rung producing *gov_value*.  For each
    top-level ``And`` node, collects the external-input atoms into a patch
    ``{input: True/False}``.  Returns only groups with ≥2 inputs — single
    inputs are already covered by per-input steers.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr
    from pyrung.core.analysis.sp_values import _written_value_for_tag
    from pyrung.core.crossing import Literal

    _POSITIVE_FORMS = {"xic", "rise", "truthy"}

    groups: list[dict[str, Any]] = []
    seen_patches: set[frozenset[tuple[str, Any]]] = set()

    def _extract_group(e: Any) -> dict[str, Any] | None:
        """Collect external-input atoms from a single conjunct."""
        if isinstance(e, And):
            patch: dict[str, Any] = {}
            for term in e.terms:
                sub = _extract_group(term)
                if sub:
                    patch.update(sub)
            return patch if patch else None
        if isinstance(e, Atom) and e.tag in ext_inputs:
            return {e.tag: e.form in _POSITIVE_FORMS}
        return None

    def _collect_groups(e: Any) -> None:
        """Walk the expression collecting conjunctive groups."""
        if isinstance(e, And):
            group = _extract_group(e)
            if group and len(group) >= 2:
                key = frozenset(group.items())
                if key not in seen_patches:
                    seen_patches.add(key)
                    groups.append(group)
        if isinstance(e, (And, Or)):
            for term in e.terms:
                _collect_groups(term)

    seen_rungs: set[int] = set()
    for ri in pdg.writers_of.get(governing, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None or id(ro) in seen_rungs:
            continue
        seen_rungs.add(id(ro))
        if gov_value is not None:
            wv = _written_value_for_tag(ro, governing)
            if isinstance(wv, Literal) and wv.value != gov_value:
                continue
        sp = ro.sp_tree()
        if sp is not None:
            _collect_groups(_sp_to_expr(sp))

    return groups


def _needs_decomposition(
    prereqs: list[tuple[str, Any]],
    target_tag: str,
    pdg: ProgramGraph,
    *,
    nogoods: NoGoodStore | None = None,
    transition: tuple[Any, Any] | None = None,
) -> tuple[bool, str | None]:
    """Detect whether two prerequisites of *target_tag* mutually interfere.

    Prerequisites that share an upstream cone tag or a common writer rung can
    clobber each other when walked serially.  This is the Tier 2
    (force-and-solve) insertion point: when detected, forking a fork per
    subsystem and solving them independently side-steps the interference.

    When *nogoods* and *transition* (``(from_value, to_value)``) are supplied,
    a recorded all-orderings-blocked nogood for that transition OR-s into the
    static overlap check — a learned dead ordering is direct evidence the
    prerequisites couple.  Defaults (no nogoods) keep existing call sites and
    the static-only behavior unchanged.

    Returns ``(needs_decomposition, detail)`` naming the first overlapping
    pair, or ``(False, None)``.
    """
    if nogoods is not None and transition is not None:
        from_value, to_value = transition
        if nogoods.all_orderings_blocked(target_tag, from_value, to_value, prereqs):
            return True, f"all orderings blocked for {target_tag} {from_value!r}->{to_value!r}"
    ptags = [t for t, _v in prereqs]
    for i in range(len(ptags)):
        for j in range(i + 1, len(ptags)):
            a, b = ptags[i], ptags[j]
            overlap = (pdg.upstream_slice(a) & pdg.upstream_slice(b)) - {target_tag}
            shared_writers = pdg.writers_of.get(a, frozenset()) & pdg.writers_of.get(b, frozenset())
            if overlap or shared_writers:
                bits: list[str] = []
                if overlap:
                    bits.append("shared cone {" + ", ".join(sorted(overlap)) + "}")
                if shared_writers:
                    bits.append(f"{len(shared_writers)} shared writer(s)")
                return True, f"{a}/{b}: " + "; ".join(bits)
    return False, None


def _log_decomposition_hint(
    target_tag: str,
    coupling: list[tuple[str, Any]],
    pdg: ProgramGraph,
    checkpoint: PLC | None = None,
    *,
    nogoods: NoGoodStore | None = None,
    transition: tuple[Any, Any] | None = None,
) -> None:
    """Log a Tier 2 (force-and-solve) hint when unresolved prereqs couple.

    Called before the walk gives up on *target_tag*: if two of its
    prerequisites share an upstream cone or writer, serial walking cannot
    avoid the clobber and forking a fork per subsystem would.  Observability
    only — the mechanism waits for a mutual-interference test case.

    *nogoods*/*transition* let a learned all-orderings-blocked nogood also
    trigger the hint (see :func:`_needs_decomposition`).
    """
    needs_decomp, detail = _needs_decomposition(
        coupling, target_tag, pdg, nogoods=nogoods, transition=transition
    )
    if not needs_decomp:
        return
    if checkpoint is not None:
        logger.info(
            "walk: %s unresolved after re-check; prerequisites couple (%s) — "
            "decomposition (force-and-solve) may help; checkpoint at scan %d",
            target_tag,
            detail,
            checkpoint.state.scan_id,
        )
    else:
        logger.info(
            "walk: %s unresolved after re-check; prerequisites couple (%s) — "
            "decomposition (force-and-solve) may help",
            target_tag,
            detail,
        )
