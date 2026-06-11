"""Static priors and candidate generation for the corridor walker.

Everything here is a prior, never correctness-bearing: target extraction,
governing-tag selection (with the ``_probe_steps`` simulation probe as the
ground-truth fallback), steer-alphabet construction, writer-condition and
inequality prerequisite extraction, and the static coupling detection
behind the Tier-2 decomposition hint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _CMP_OPS,
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
    from pyrung.core.analysis.sp_values import _written_value_for_tag

    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if wv is not None and wv[0] == "tag":
            return wv[1]
    return None


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
            if wv is not None and wv[0] == "literal":
                values.add(wv[1])
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
) -> bool:
    """Fork, steer, observe: does *tag* actually visit multiple values?"""
    ext_inputs = _external_bool_inputs(pdg, known)
    edge_ext = _edge_tags(pdg, program) & set(ext_inputs)
    alphabet = _steer_alphabet(tag, pdg, known, program)
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
            probed = _probe_steps(plc, target_tag, pdg, plc._known_tags_by_name, program)
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
        if wv is not None and wv[0] == "literal" and wv[1] != target_value:
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


def _extract_inequality_prereqs(
    expr: Any,
    snapshot: dict[str, Any],
    nd_domains: dict[str, tuple[Any, ...]] | None,
    pdg: ProgramGraph,
) -> list[tuple[str, Any]]:
    """Extract ``(tag, satisfying_value)`` pairs from inequality atoms.

    Complements ``_extract_condition_values`` which handles eq/xic/xio but
    drops gt/ge/lt/le.  Uses pipeline domains to pick a concrete satisfying
    value for each inequality.
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
            if isinstance(operand, str):
                operand = snapshot.get(operand, 0)
            domain = nd_domains.get(tag)
            if domain is None:
                return
            current = snapshot.get(tag)
            op = _CMP_OPS[e.form]
            try:
                if current is not None and op(current, operand):
                    return
            except TypeError:
                pass
            val = _satisfying_value(e.form, operand, domain)
            if val is not None:
                seen.add(tag)
                result.append((tag, val))
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
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _written_value_for_tag,
    )
    from pyrung.core.instruction.coils import OutInstruction

    merged: dict[str, set[Any]] = {}
    any_writer_matched = False

    def _add(cvals: dict[str, frozenset[Any]]) -> None:
        for t, vs in cvals.items():
            merged.setdefault(t, set()).update(vs)

    for ri in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        is_ote = any(
            isinstance(i, OutInstruction) and getattr(i.target, "name", None) == tag
            for i in ro._instructions
        )
        if wv is not None:
            if wv[0] == "literal" and not _values_match(wv[1], value):
                continue
        elif not is_ote or not value:
            continue
        any_writer_matched = True

        sp = ro.sp_tree()
        if sp is not None:
            _add(_extract_condition_values(_sp_to_expr(sp)))

        if node.subroutine is not None:
            for caller in pdg.rung_nodes:
                if node.subroutine in caller.calls:
                    cro = _resolve_rung(program, caller)
                    if cro is None:
                        continue
                    csp = cro.sp_tree()
                    if csp is not None:
                        _add(_extract_condition_values(_sp_to_expr(csp)))

    result: list[tuple[str, Any]] = []
    for cond_tag, needed_vals in merged.items():
        if cond_tag == tag:
            continue
        current = snapshot.get(cond_tag)
        for nv in needed_vals:
            if not _values_match(current, nv):
                if nv and _is_scan_transient(cond_tag, pdg, program, known):
                    continue
                result.append((cond_tag, nv))

    # Inequality prerequisites: resolve gt/ge/lt/le atoms from writer conditions
    # against pipeline domains.  These are typically INPUT tags (external ND
    # inputs the operator can set) — the equality path above skips INPUTs, but
    # for inequalities the walker needs to steer them to specific values.
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
            ineq = _extract_inequality_prereqs(_sp_to_expr(sp), snapshot, nd_domains, pdg)
            for itag, ival in ineq:
                if itag != tag and itag not in equality_tags:
                    result.append((itag, ival))
                    equality_tags.add(itag)
            if node.subroutine is not None:
                for caller in pdg.rung_nodes:
                    if node.subroutine in caller.calls:
                        cro = _resolve_rung(program, caller)
                        if cro is None:
                            continue
                        csp = cro.sp_tree()
                        if csp is None:
                            continue
                        ineq2 = _extract_inequality_prereqs(
                            _sp_to_expr(csp), snapshot, nd_domains, pdg
                        )
                        for itag, ival in ineq2:
                            if itag != tag and itag not in equality_tags:
                                result.append((itag, ival))
                                equality_tags.add(itag)

    if not result and not any_writer_matched:
        result = _latch_break_conditions(tag, snapshot, pdg, program)

    return result


def _is_scan_transient(
    tag: str,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any] | None = None,
) -> bool:
    """Whether *tag* is provably back at its default at every scan boundary.

    The consumed-same-scan handshake shape (PackML command/mode protocols):
    a producer rung sets the tag, consumers act on it, and a later rung in
    the same scope clears it — all within one scan.  A boundary goal
    ``(tag, set_value)`` for such a tag is structurally unreachable and
    poisons recovery (findings §2a).

    Proof obligations, all static: every writer lives in one scope and
    writes a literal; at least one writes non-default (producer) and one
    writes the default back (clearer); some clearer sits at a later rung
    index than every producer and fires whenever the tag is set (bare
    positive contact on the tag, possibly inside an ``Or``, or no condition
    at all).  Any shape this can't prove returns ``False`` — the tag keeps
    its boundary goals (conservative direction).
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import Atom, Or, _sp_to_expr
    from pyrung.core.analysis.sp_values import _written_value_for_tag

    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return False
    t = known.get(tag) if known is not None else None
    default = t.default if t is not None else False

    writer_idxs = pdg.writers_of.get(tag, frozenset())
    if not writer_idxs:
        return False
    producers: list[int] = []
    clearers: list[tuple[int, Any]] = []
    scopes: set[str | None] = set()
    for ri in writer_idxs:
        node = pdg.rung_nodes[ri]
        ro = _resolve_rung(program, node)
        if ro is None:
            return False
        if tag in node.ote_writes:
            return False  # OTE reflects its condition at the boundary
        wv = _written_value_for_tag(ro, tag)
        if wv is None or wv[0] != "literal":
            return False
        scopes.add(node.subroutine)
        if _values_match(wv[1], default):
            clearers.append((node.rung_index, ro))
        else:
            producers.append(node.rung_index)
    if not producers or not clearers or len(scopes) != 1:
        return False

    def _fires_when_set(e: Any) -> bool:
        if isinstance(e, Atom):
            return e.tag == tag and e.form in ("xic", "truthy")
        if isinstance(e, Or):
            return any(_fires_when_set(term) for term in e.terms)
        return False

    last_producer = max(producers)
    for idx, ro in clearers:
        if idx <= last_producer:
            continue
        sp = ro.sp_tree()
        if sp is None:
            return True
        if _fires_when_set(_sp_to_expr(sp)):
            return True
    return False


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
    :func:`_is_scan_transient`), no boundary goal can establish that gate —
    the whole chain must fire within one scan.  Regress one level: bundle
    the transient's producer-rung external requirements (Bool polarities,
    satisfying values for comparisons on ND inputs) with the writer's own
    externals and its copy-source value binding (``copy(S, governing)`` with
    a concrete *gov_value* binds ``S = gov_value``), and emit the union as
    one multi-input patch.  Candidates only — every bundle is validated by
    the interpreted trial like any other steer.
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr
    from pyrung.core.analysis.sp_values import _written_value_for_tag

    _POS = {"xic", "rise", "truthy"}
    _NEG = {"xio", "fall"}
    _INEQ = {"gt", "ge", "lt", "le"}

    def _cmp_ok(form: str, operand: Any, v: Any) -> bool:
        if form == "eq":
            return _values_match(v, operand)
        op = _CMP_OPS.get(form)
        if op is None:
            return False
        try:
            return bool(op(v, operand))
        except TypeError:
            return False

    def _collect(e: Any, patch: dict[str, Any], cons: dict[str, list]) -> bool:
        """Gather external requirements from *e*; False = contradictory."""
        if isinstance(e, And):
            return all(_collect(t, patch, cons) for t in e.terms)
        if isinstance(e, Or):
            # An Or may already hold at runtime; the trial decides.
            return True
        if isinstance(e, Atom):
            if e.tag in ext_inputs and (e.form in _POS or e.form in _NEG):
                want = e.form in _POS
                prev = patch.get(e.tag)
                if prev is not None and prev != want:
                    return False
                patch[e.tag] = want
                return True
            if pdg.tag_roles.get(e.tag) == TagRole.INPUT and (e.form in _INEQ or e.form == "eq"):
                cons.setdefault(e.tag, []).append((e.form, e.operand))
                return True
        return True

    def _positive_internal_tags(e: Any, acc: set[str]) -> None:
        if isinstance(e, Atom):
            if e.form in _POS and e.tag not in ext_inputs:
                acc.add(e.tag)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                _positive_internal_tags(term, acc)

    def _pick_satisfying(ctag: str, cons: list) -> Any | None:
        candidates: list[Any] = []
        if nd_domains and ctag in nd_domains:
            candidates.extend(nd_domains[ctag])
        for form, operand in cons:
            if form == "eq":
                candidates.append(operand)
            else:
                v = _inequality_satisfying_value(form, operand)
                if v is not None:
                    candidates.append(v)
        for cand in candidates:
            if all(_cmp_ok(f, o, cand) for f, o in cons):
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
            wv is not None
            and wv[0] == "literal"
            and gov_value is not None
            and not _values_match(wv[1], gov_value)
        ):
            continue
        sp = ro.sp_tree()
        if sp is None:
            continue
        expr = _sp_to_expr(sp)
        gate_tags: set[str] = set()
        _positive_internal_tags(expr, gate_tags)
        transients = [t for t in sorted(gate_tags) if _is_scan_transient(t, pdg, program, known)]
        if not transients:
            continue
        base_patch: dict[str, Any] = {}
        base_cons: dict[str, list] = {}
        if not _collect(expr, base_patch, base_cons):
            continue
        # Copy-source value binding: copy(S, governing) with a concrete goal
        # value binds the external source to that value.
        if (
            wv is not None
            and wv[0] == "tag"
            and gov_value is not None
            and pdg.tag_roles.get(wv[1]) == TagRole.INPUT
            and wv[1] not in ext_inputs
        ):
            base_patch[wv[1]] = gov_value
        for trans_tag in transients:
            for pi in pdg.writers_of.get(trans_tag, frozenset()):
                pro = _resolve_rung(program, pdg.rung_nodes[pi])
                if pro is None:
                    continue
                pwv = _written_value_for_tag(pro, trans_tag)
                if pwv is None or pwv[0] != "literal" or not pwv[1]:
                    continue  # producers only
                psp = pro.sp_tree()
                if psp is None:
                    continue
                patch = dict(base_patch)
                cons = {k: list(v) for k, v in base_cons.items()}
                if not _collect(_sp_to_expr(psp), patch, cons):
                    continue
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
    return bundles


def _external_bool_inputs(pdg: ProgramGraph, known: dict[str, Any]) -> list[str]:
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.tag import TagType

    out: list[str] = []
    for tag, role in pdg.tag_roles.items():
        if role != TagRole.INPUT:
            continue
        t = known.get(tag)
        if t is not None and t.type is TagType.BOOL:
            out.append(tag)
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

    ext = _external_bool_inputs(pdg, known)
    cone = pdg.upstream_slice(governing)
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
            if wv is not None and wv[0] == "literal" and wv[1] != gov_value:
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
            if wv is not None and wv[0] == "literal" and wv[1] != gov_value:
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
        if nogoods.all_orderings_blocked(from_value, to_value, prereqs):
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
