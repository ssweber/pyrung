"""Corridor walker for how().

A sequential-simulation planner that runs on the PLC runner instead of the
BFS infrastructure.

The principle: the target reduces to driving one **governing** stateful tag
to a value.  The walker discovers that tag's value-transition graph by
*interpreted simulation* — from a state at value ``from``, it applies a
candidate *steer* on a forked runner and observes the value ``to`` that
results.  Every edge is therefore something the real interpreter produced
(sound by construction; immune to copy/calc/indirect-addressing blindness
that defeats static writer inversion).

Static analysis is only a **prior**, never correctness-bearing:
  1. it picks the governing tag (a derived coil delegates to the state tag
     that gates it),
  2. it narrows the steer alphabet to the governing tag's input cone, and
  3. it sets the search horizon (short for command machines, long when a
     timer/counter gates the tag — a held wait advances time to the crossing,
     so "advance time" is just an empty steer with a longer horizon).

Best-first search runs over the governing tag's value space (tiny — mode
values or a counter's range), not the full state space, so it stays "mostly
no search".  Anything the walker cannot reach returns ``None`` and the caller
falls back to the existing waypoint/BFS planner.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.graph import Path
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC

# The time-advance loop folds productive accumulation to _EMPTY_CAP equivalent
# normal-dt scans (a single jump covers many in one real step).  What differs
# per steer is the *reaction budget*: how long a steer may churn (a visible
# non-accumulator change every scan) before an accumulation plateau forms.  A
# pulse that merely *starts* a dwell settles within a few scans then folds, so
# it needs only a small budget; an inert or oscillating pulse exhausts it and
# bails.  The empty steer's long waits are plateaus, not churn, so it is
# effectively unbounded.  Crucially, once a plateau with an upcoming crossing is
# found the fold runs to _EMPTY_CAP regardless of the budget — productive
# waiting (the dwell a pulse started) is never cut short.
_PULSE_REACT_CAP = 6
_EMPTY_CAP = 20_000
# Guard on real loop iterations (plateaus + reaction scans); bounds oscillators
# that never settle and never cross an accumulator threshold.  Doubles as the
# empty steer's (effectively non-binding) reaction budget.
_MAX_ADVANCE_ITERS = 4_000
# Float tolerance for "is this accumulator advancing" (timers carry a fraction).
_EPS = 1e-9
# Caps on the interpreted value-graph search.
_MAX_NODES = 64
_MAX_CORRIDOR = 40
_MAX_PREREQ_DEPTH = 6
# Serial-clobber recovery: how many oracle-driven re-check rounds to attempt
# after the serial prerequisite walk leaves the governing tag unreachable.
_MAX_RECHECK_ITERS = 3


@dataclass(frozen=True)
class _Steer:
    """A candidate move: empty, pulse high, drive low, set, or multi-input."""

    kind: str  # "empty" | "pulse" | "low" | "set" | "multi"
    input: str | None = None
    value: Any = None
    # For "multi" steers: dict of {input_name: value} to apply simultaneously.
    patch: dict[str, Any] | None = None


# A realized action step: ``patch(action)`` then ``scans`` steps.
_Action = tuple[dict[str, Any], int]


# ---------------------------------------------------------------------------
# Physical feedback: Harness on walk forks
# ---------------------------------------------------------------------------
# ``_do_jump`` bumps scan_id by *skip* (not 1), so the Harness's
# scan-indexed heap drains at the right time during folds.  Profile
# feedback tags are excluded from the plateau guard so their per-scan
# drift doesn't break fold detection.  ``PLC.fork()`` propagates the
# installed Harness, so every trial fork in ``_explore`` inherits it.


def _install_walk_harness(plc: PLC) -> frozenset[str]:
    """Install a :class:`~pyrung.core.harness.Harness` on *plc* if it has
    physical couplings, and return profile-feedback tag names.

    Profile-feedback names are excluded from the plateau guard so their
    per-scan drift doesn't break fold detection.  The installed Harness
    propagates to forks via ``PLC.fork()``, so every trial runner in
    ``_explore`` inherits it automatically.
    """
    from pyrung.core.harness import Harness

    harness = Harness(plc)
    harness.install()
    profile_fb_names: set[str] = set()
    has_couplings = False
    for coupling in harness.couplings():
        has_couplings = True
        if coupling.physical.feedback_type == "analog":
            profile_fb_names.add(coupling.fb_name)
    if not has_couplings:
        harness.uninstall()
    return frozenset(profile_fb_names)


def _harness_nearest_scan(plc: PLC) -> int | None:
    """Peek the installed Harness's heap for the nearest scheduled scan."""
    h = plc._harness
    if h is not None and h._heap:
        return h._heap[0].target_scan
    return None


# ---------------------------------------------------------------------------
# Target extraction
# ---------------------------------------------------------------------------


def _extract_goals(expr: Any, snapshot: dict[str, Any]) -> list[tuple[str, Any]] | None:
    """Reduce a target expression to a list of ``(tag, value)`` goals.

    Handles And (all terms), Or (cheapest branch), and single Atom.
    Returns ``None`` for expressions that can't be decomposed into
    concrete tag==value pairs (rise/fall/inequalities).
    """
    from pyrung.core.analysis.prove.waypoints import _extract_required_values

    pairs = _extract_required_values(expr, snapshot)
    if not pairs:
        return None
    return pairs


# ---------------------------------------------------------------------------
# Static priors: governing tag, steer alphabet, horizon
# ---------------------------------------------------------------------------


def _copy_source(tag: str, pdg: ProgramGraph, program: Any) -> str | None:
    """Return ``U`` when *tag* is written ``copy(U, tag)`` (copy-from-tag)."""
    from pyrung.core.analysis.prove.waypoints import _resolve_rung, _written_value_for_tag

    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        wv = _written_value_for_tag(ro, tag)
        if wv is not None and wv[0] == "tag":
            return wv[1]
    return None


def _calc_self_referential(tag: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when *tag* is the dest of a ``calc`` that reads *tag* itself.

    A self-updating calc — ``calc(tag + 1, tag)``, ``calc((tag + 1) % 6, tag)``,
    etc. — is a stateful multi-value tag (counter-like) even when the wrapper
    op (modulo, mask) hides the ±1 from the shared monotone-stepping detector
    ``_has_arithmetic_writer``.  Used only to decide governance, so it may be
    liberal: the corridor is confirmed by replay regardless.
    """
    from pyrung.core.analysis.prove.waypoints import _resolve_rung
    from pyrung.core.expression import BinaryExpr, TagExpr, UnaryExpr
    from pyrung.core.instruction.calc import CalcInstruction

    def reads_tag(expr: Any) -> bool:
        if isinstance(expr, TagExpr):
            return getattr(expr.tag, "name", None) == tag
        if isinstance(expr, BinaryExpr):
            return reads_tag(expr.left) or reads_tag(expr.right)
        if isinstance(expr, UnaryExpr):
            return reads_tag(expr.operand)
        return False

    for ri in pdg.writers_of.get(tag, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        for instr in ro._instructions:
            if (
                isinstance(instr, CalcInstruction)
                and getattr(instr.dest, "name", None) == tag
                and reads_tag(instr.expression)
            ):
                return True
    return False


def _value_richness(tag: str, pdg: ProgramGraph, program: Any) -> int:
    """How many distinct values *tag* plausibly steps through.

    Counts distinct literal write values of *tag* and (when copy-coupled) of
    its copy source; an arithmetic (counter) or self-updating ``calc`` writer
    counts as rich.  Used to decide whether *tag* is itself the governing
    corridor tag or merely a derived view of one.
    """
    from pyrung.core.analysis.prove.waypoints import (
        _has_arithmetic_writer,
        _resolve_rung,
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
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.prove.waypoints import (
        _extract_condition_values,
        _resolve_rung,
        _written_value_for_tag,
    )
    from pyrung.core.analysis.simplified import _sp_to_expr

    # Fast path: static signals say it steps — trust without probing.
    stepping = (
        getattr(explore_context, "stepping_tags", None) if explore_context is not None else None
    )
    if stepping is not None and target_tag in stepping:
        return target_tag, target_value
    if stepping is None and _value_richness(target_tag, pdg, program) >= 2:
        return target_tag, target_value

    # Simulation probe: the program is the model.
    if plc is not None and _probe_steps(plc, target_tag, pdg, plc._known_tags_by_name, program):
        return target_tag, target_value

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
            rich = _richness(gt, pdg, program, explore_context)
            if rich > best_rich:
                best = (gt, next(iter(gvals)))
                best_rich = rich
        for gt, gval in _extract_inequality_governing(sp_expr).items():
            if gt == target_tag or pdg.tag_roles.get(gt) == TagRole.INPUT:
                continue
            rich = _richness(gt, pdg, program, explore_context)
            if rich > best_rich:
                best = (gt, gval)
                best_rich = rich
    return best if best is not None else (target_tag, target_value)


def _satisfying_value(form: str, operand: Any, domain: tuple[Any, ...]) -> Any | None:
    """Pick the smallest domain value satisfying the comparison, or ``None``."""
    _OPS: dict[str, Any] = {
        "gt": lambda v, o: v > o,
        "ge": lambda v, o: v >= o,
        "lt": lambda v, o: v < o,
        "le": lambda v, o: v <= o,
    }
    op = _OPS.get(form)
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
            if isinstance(operand, str) and operand in (nd_domains or {}):
                operand = snapshot.get(operand, 0)
            elif isinstance(operand, str):
                operand = snapshot.get(operand, 0)
            domain = nd_domains.get(tag)
            if domain is None:
                return
            current = snapshot.get(tag)
            _OPS = {
                "gt": lambda v, o: v > o,
                "ge": lambda v, o: v >= o,
                "lt": lambda v, o: v < o,
                "le": lambda v, o: v <= o,
            }
            op = _OPS[e.form]
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
    from pyrung.core.analysis.prove.waypoints import _resolve_rung
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
) -> list[tuple[str, Any]]:
    """Enabling conditions not yet met for *tag* to reach *value*.

    Inspects the writer rung(s) that produce *value* and returns the
    ``(tag, needed_value)`` pairs from their enabling conditions that
    differ from the current *snapshot*.  For subroutine writers the
    call-site condition is included.  When no writer produces *value*,
    falls back to :func:`_latch_break_conditions`.
    """
    from pyrung.core.analysis.prove.waypoints import (
        _extract_condition_values,
        _resolve_rung,
        _written_value_for_tag,
    )
    from pyrung.core.analysis.simplified import _sp_to_expr
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


def _values_match(a: Any, b: Any) -> bool:
    """Loose equality for tag values (``1 == True``, ``0 == False``)."""
    if a is b:
        return True
    if a == b:
        return True
    return False


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
    from pyrung.core.analysis.prove.waypoints import _resolve_rung
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
) -> list[_Steer]:
    """Empty plus pulse/low for each external Bool input in the governing cone,
    plus set-value steers for non-Bool ND inputs with known domains.

    The cone narrows branching; when static cone tracing finds nothing (e.g.
    indirect addressing) it falls back to every external Bool input.

    When *program* and *gov_value* are provided, inputs appearing in the
    simplified enabling condition for the target write-site are tried first,
    and negated inputs (``xio`` form) get a ``"low"`` steer.
    """
    ext = _external_bool_inputs(pdg, known)
    cone = pdg.upstream_slice(governing)
    cone_inputs = [c for c in ext if c in cone]
    candidates = cone_inputs if len(cone_inputs) >= 1 else ext

    # Determine polarity from enabling conditions.
    polarities: dict[str, set[str]] = {}
    if program is not None and candidates:
        polarities = _enabling_inputs(governing, gov_value, pdg, program)
        if polarities:
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
        if forms is not None:
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
        for nd_name, domain in nd_domains.items():
            if nd_name in bool_set:
                continue
            # Include ND inputs in the governing cone, plus the governing tag
            # itself (an external ND input being walked to a target value).
            if nd_name not in cone and nd_name != governing:
                continue
            for v in domain:
                steers.append(_Steer("set", nd_name, v))

    # Multi-input steers: conjunctive groups from enabling conditions.
    if program is not None and gov_value is not None:
        ext_set = set(ext)
        for group in _conjunctive_input_groups(governing, gov_value, pdg, program, ext_set):
            steers.append(_Steer("multi", patch=group))

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
    from pyrung.core.analysis.prove.waypoints import _resolve_rung, _written_value_for_tag
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

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
    from pyrung.core.analysis.prove.waypoints import _resolve_rung, _written_value_for_tag
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

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


# ---------------------------------------------------------------------------
# Time-jump priors: timer/counter introspection and the actionable-crossing set
# ---------------------------------------------------------------------------
#
# A held wait only changes the program when a comparison the program *reads*
# against some accumulator flips — the implicit ``Acc >= preset`` done bit, or
# an explicit ``Acc <op> threshold`` in a rung.  Between flips every rung emits
# identical output, so those scans are skippable: jump to one-before the nearest
# flip, then settle the crossing and its reaction at normal dt.
#
# Soundness rests on the *plateau guard* (only-accumulators-changed), not on
# this set being exhaustive: within a plateau no value a rung reads can change
# except through one of these comparisons (a direct data-copy of an accumulator
# would move a non-accumulator tag every scan, so no plateau would form).  The
# set is for efficiency — how far to jump once a plateau is detected.


@dataclass(frozen=True)
class _AccSource:
    """A timer/counter whose accumulator a held wait advances."""

    acc_name: str
    done_bit: str
    preset: Any  # int literal or tag-name str (dynamic preset)
    kind: str  # "up" (on/off-delay, count-up) | "down" (count-down)
    timed: bool  # True: time-based (dt knob).  False: per-scan (acc patch).


@dataclass(frozen=True)
class _JumpContext:
    """Static priors for the time-advance jump loop, built once per walk."""

    sources: tuple[_AccSource, ...]
    acc_names: frozenset[str]
    # tag_name -> tuple of (comparison form, operand) read by some rung
    # Covers both accumulators and profile feedback tags.
    comparisons: dict[str, tuple[tuple[str, Any], ...]]
    # done-bit tag names that some rung actually reads (actionable crossings)
    read_done: frozenset[str]
    normal_dt: float
    # Profile feedback tags — excluded from visible-items plateau check.
    profile_fb_names: frozenset[str] = frozenset()


def _collect_acc_sources(program: Any) -> list[_AccSource]:
    """Introspect every timer/counter instruction (incl. subroutines)."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    out: dict[str, _AccSource] = {}
    for instr in walk_instructions(program):
        if isinstance(instr, (OnDelayInstruction, OffDelayInstruction)):
            kind, timed = "up", True
        elif isinstance(instr, CountUpInstruction):
            kind, timed = "up", False
        elif isinstance(instr, CountDownInstruction):
            kind, timed = "down", False
        else:
            continue
        preset = instr.preset
        out[instr.accumulator.name] = _AccSource(
            acc_name=instr.accumulator.name,
            done_bit=instr.done_bit.name,
            preset=preset.name if isinstance(preset, Tag) else preset,
            kind=kind,
            timed=timed,
        )
    return list(out.values())


def _scan_rung_reads(
    pdg: ProgramGraph,
    program: Any,
    watch_names: frozenset[str],
) -> tuple[dict[str, tuple[tuple[str, Any], ...]], frozenset[str]]:
    """Collect comparison atoms for *watch_names* tags and all read tag names.

    *watch_names* is typically ``acc_names | profile_fb_names`` — any tag whose
    comparison crossings the fold arithmetic needs.
    """
    from pyrung.core.analysis.prove.waypoints import _resolve_rung
    from pyrung.core.analysis.simplified import And, Atom, Or, _sp_to_expr

    cmp_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    comparisons: dict[str, list[tuple[str, Any]]] = {}
    read_tags: set[str] = set()

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            read_tags.add(e.tag)
            if e.form in cmp_forms and e.tag in watch_names:
                comparisons.setdefault(e.tag, []).append((e.form, e.operand))
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

    frozen = {k: tuple(v) for k, v in comparisons.items()}
    return frozen, frozenset(read_tags)


def _build_jump_context(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
) -> _JumpContext:
    sources = _collect_acc_sources(program)
    acc_names = frozenset(s.acc_name for s in sources)
    h = plc._harness
    profile_fb_names: frozenset[str] = (
        frozenset(c.fb_name for c in h._profile_couplings) if h is not None else frozenset()
    )
    comparisons, read_tags = _scan_rung_reads(pdg, program, acc_names | profile_fb_names)
    return _JumpContext(
        sources=tuple(sources),
        acc_names=acc_names,
        comparisons=comparisons,
        read_done=frozenset(s.done_bit for s in sources) & read_tags,
        normal_dt=float(getattr(plc, "_dt", 0.010) or 0.010),
        profile_fb_names=profile_fb_names,
    )


def _resolve_num(value: Any, state: Any) -> float | None:
    """Resolve a threshold operand (literal, tag name, or Tag) to a number."""
    from pyrung.core.tag import Tag

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    name = value.name if isinstance(value, Tag) else value if isinstance(value, str) else None
    if name is None:
        return None
    resolved = state.tags.get(name)
    if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
        return None
    return float(resolved)


def _acc_totals(state: Any, sources: tuple[_AccSource, ...]) -> dict[str, float]:
    """Per-source progress in monotone-up coordinates (count-down negated)."""
    totals: dict[str, float] = {}
    for src in sources:
        acc = float(state.tags.get(src.acc_name, 0) or 0)
        if src.timed:
            acc += float(state.memory.get(f"_frac:{src.acc_name}", 0.0) or 0.0)
        totals[src.acc_name] = -acc if src.kind == "down" else acc
    return totals


def _visible_items(state: Any, exclude: frozenset[str]) -> dict[str, Any]:
    """Tag snapshot excluding accumulators and profile feedback tags (whose
    monotone drift is the only state change permitted on a skippable plateau)."""
    return {k: v for k, v in state.tags.items() if k not in exclude}


def _nearest_skip(
    ctx: _JumpContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
    state: Any,
) -> int | None:
    """Skippable scans before the nearest actionable crossing on this plateau.

    Returns ``None`` when no live accumulator has an upcoming flip (waiting is
    futile), ``0`` when the crossing is the very next scan, else the number of
    pure-accumulation scans that can be folded over.
    """
    best: int | None = None
    for src in ctx.sources:
        pb = before_tot.get(src.acc_name)
        pa = after_tot.get(src.acc_name)
        if pb is None or pa is None:
            continue
        delta = pa - pb
        if delta <= _EPS:  # not advancing this scan
            continue
        # Actionable boundaries in progress coordinates: (target, strict).
        bounds: list[tuple[float, bool]] = []
        if src.done_bit in ctx.read_done:
            preset = _resolve_num(src.preset, state)
            if preset is None:
                # A read done bit with an unresolvable preset could flip next
                # scan — stay conservative and never skip past it.
                best = 1 if best is None else min(best, 1)
                continue
            bounds.append((preset, False))  # done := progress >= preset
        unresolved = False
        for form, operand in ctx.comparisons.get(src.acc_name, ()):
            kv = _resolve_num(operand, state)
            if kv is None:
                unresolved = True
                break
            bounds.append(_progress_bound(src.kind, form, kv))
        if unresolved:
            best = 1 if best is None else min(best, 1)
            continue
        for target, strict in bounds:
            scans = _scans_to_cross(pa, delta, target, strict)
            if scans is None:  # already past; monotone progress won't reflip it
                continue
            best = scans if best is None else min(best, scans)
    return None if best is None else best - 1


def _progress_bound(kind: str, form: str, k: float) -> tuple[float, bool]:
    """Map a comparison ``acc <form> k`` to a ``(target, strict)`` flip boundary
    in monotone-up progress coordinates (``progress`` = ``acc``, or ``-acc`` for
    count-down).  ``strict`` selects ``progress > target`` over ``>= target``.
    """
    if kind == "down":
        k = -k
        form = {"lt": "gt", "gt": "lt", "le": "ge", "ge": "le"}.get(form, form)
    # ge/lt flip as progress reaches k; gt/le flip as progress exceeds it.
    # eq/ne are treated conservatively as reach-k (lands at or before the flip).
    return k, form in ("gt", "le")


def _scans_to_cross(pa: float, delta: float, target: float, strict: bool) -> int | None:
    """Scans for progress (at ``pa``, +``delta``/scan) to cross ``target``.

    ``None`` when already crossed.  ``strict`` => first scan ``progress > target``
    (``gt``/``le`` boundary); otherwise first scan ``progress >= target``.
    """

    if strict:
        if pa > target:
            return None
        return math.floor((target - pa) / delta) + 1
    if pa >= target:
        return None
    return max(1, math.ceil((target - pa) / delta))


def _do_jump(
    runner: PLC,
    skip: int,
    ctx: _JumpContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
) -> None:
    """Fold ``skip`` pure-accumulation scans into one real step.

    Timers ride the dt knob (the interpreter does ``skip`` scans of dt in the
    one step); per-scan counters can't be moved by time, so their accumulators
    are patched forward by ``(skip-1)*delta`` and the step's own ``execute``
    supplies the final increment, keeping every source in phase.

    After the step, scan_id is bumped by ``skip - 1`` so that scan-indexed
    mechanisms (Harness feedback heap, ScanLog) see the equivalent elapsed
    scans.  This keeps the scan_id coordinate consistent with the time that
    passed, which is essential for installed Harness couplings.
    """
    patches: dict[str, int] = {}
    for src in ctx.sources:
        if src.timed:
            continue
        pb = before_tot.get(src.acc_name)
        pa = after_tot.get(src.acc_name)
        if pb is None or pa is None or pa - pb <= _EPS:
            continue
        raw_delta = int(round((pa - pb) if src.kind == "up" else -(pa - pb)))
        if raw_delta == 0:
            continue
        raw_acc = int(runner.state.tags.get(src.acc_name, 0) or 0)
        patches[src.acc_name] = raw_acc + (skip - 1) * raw_delta
    if patches:
        runner.patch(patches)
    runner._dt_override_for_next_scan = skip * ctx.normal_dt
    runner.step()
    # Align scan_id with the equivalent scans that passed.
    runner._state = runner._state.set(scan_id=runner._state.scan_id + skip - 1)


def _advance_time(
    runner: PLC,
    governing: str,
    from_value: Any,
    ctx: _JumpContext,
    react_cap: int,
) -> int | None:
    """Hold inputs and advance time until *governing* leaves *from_value*.

    Each iteration's normal scan doubles as the plateau probe: if it changed
    only accumulators (and profile feedback tags), fold the next
    pure-accumulation run to one-before the nearest actionable crossing via
    :func:`_do_jump`.  *react_cap* bounds consecutive *churn* scans (a visible
    non-accumulator change every scan) before a plateau forms, so an inert or
    oscillating steer bails; productive folding always runs to ``_EMPTY_CAP``
    regardless, so a dwell a pulse merely started is never cut short.  Returns
    the equivalent normal-dt scan count advanced, or ``None`` (fixpoint, churn
    budget exhausted, or iteration guard).

    When the runner has an installed Harness, ``_do_jump`` bumps scan_id by
    *skip* so that scan-indexed feedback patches drain at the correct time.
    Pending harness patches constrain the fold distance.
    """
    used = 0
    iters = 0
    react = 0
    exclude = ctx.acc_names | ctx.profile_fb_names
    while used < _EMPTY_CAP and iters < _MAX_ADVANCE_ITERS:
        iters += 1
        before_tot = _acc_totals(runner.state, ctx.sources)
        before_vis = _visible_items(runner.state, exclude)
        runner.step()
        used += 1
        if runner.state.tags.get(governing) != from_value:
            return used
        if _visible_items(runner.state, exclude) != before_vis:
            react += 1
            if react > react_cap:
                return None  # churning without reaching a plateau — bail
            continue  # reaction / settling in progress — not a plateau
        after_tot = _acc_totals(runner.state, ctx.sources)
        skip = _nearest_skip(ctx, before_tot, after_tot, runner.state)
        # Constrain fold distance by pending harness feedback patches.
        harness_scan = _harness_nearest_scan(runner)
        if harness_scan is not None:
            gap = harness_scan - runner.state.scan_id - 1
            if gap >= 0:
                skip = min(skip, gap) if skip is not None else gap
        if skip is None:
            if runner._harness is not None and any(
                c.active for c in runner._harness._profile_couplings
            ):
                continue  # profile is ramping — keep stepping
            return None  # nothing a held wait can change
        react = 0  # a productive plateau resets the churn budget
        skip = min(skip, _EMPTY_CAP - used)
        if skip >= 1:
            _do_jump(runner, skip, ctx, before_tot, after_tot)
            used += skip
            if runner.state.tags.get(governing) != from_value:
                return used
    return None


# ---------------------------------------------------------------------------
# Interpreted steer application
# ---------------------------------------------------------------------------


def _steer_prefix(
    steer: _Steer,
    work_tags: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
) -> list[_Action]:
    """Action prefix for *steer*: empty → none; pulse → release then high; low → drive low; set → patch value; multi → simultaneous patch."""
    if steer.kind == "empty" or (steer.input is None and steer.kind != "multi"):
        return []
    if steer.kind == "multi" and steer.patch:
        release: dict[str, Any] = {}
        pulse: dict[str, Any] = {}
        for inp, val in steer.patch.items():
            if val:
                if work_tags.get(inp):
                    release[inp] = False
                pulse[inp] = True
            else:
                if inp in edge_ext and not work_tags.get(inp):
                    release[inp] = True
                pulse[inp] = False
        prefix: list[_Action] = []
        if release:
            prefix.append((release, 1))
        prefix.append((pulse, 1))
        return prefix
    # set / low / pulse all require a concrete input tag; the empty/None guard
    # above guarantees steer.input is set for these kinds.
    assert steer.input is not None
    inp = steer.input
    if steer.kind == "set":
        return [({inp: steer.value}, 1)]
    if steer.kind == "low":
        prefix: list[_Action] = []
        if inp in edge_ext and not work_tags.get(inp):
            prefix.append(({inp: True}, 1))
        prefix.append(({inp: False}, 1))
        return prefix
    # pulse: release all highs for a clean rising edge, then drive high.
    release: dict[str, Any] = {c: False for c in ext_inputs if work_tags.get(c)}
    for e in edge_ext:
        if work_tags.get(e):
            release[e] = False
    pulse: dict[str, Any] = {inp: True}
    for e in edge_ext:
        pulse[e] = True
    prefix: list[_Action] = []
    if release:
        prefix.append((release, 1))
    prefix.append((pulse, 1))
    return prefix


def _apply_steer(
    runner: PLC,
    steer: _Steer,
    governing: str,
    from_value: Any,
    ext_inputs: list[str],
    edge_ext: set[str],
    ctx: _JumpContext,
    react_cap: int,
) -> list[_Action] | None:
    """Apply *steer* on *runner* and step until the governing value changes.

    Returns the realized ``(action, scans)`` list (leaving *runner* at the new
    value), or ``None`` if the governing value does not change.  The trailing
    held wait is advanced by :func:`_advance_time`, which folds timer dwells via
    dt jumps; the folded run is emitted as one ``({}, scans)`` entry so a plain
    normal-dt replay reproduces it.
    """
    realized: list[_Action] = []
    for action, scans in _steer_prefix(steer, dict(runner.state.tags), ext_inputs, edge_ext):
        if action:
            runner.patch(action)
        for _ in range(scans):
            runner.step()
        realized.append((action, scans))
        if runner.state.tags.get(governing) != from_value:
            return realized

    auto = _advance_time(runner, governing, from_value, ctx, react_cap)
    if auto is None:
        return None
    if auto:
        realized.append(({}, auto))
    return realized


# ---------------------------------------------------------------------------
# Best-first interpreted search over the governing value graph
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    value: Any
    plc: PLC
    path: list[_Action]


def _explore(
    start_plc: PLC,
    governing: str,
    target_value: Any,
    alphabet: list[_Steer],
    ext_inputs: list[str],
    edge_ext: set[str],
    jump_ctx: _JumpContext,
) -> list[_Action] | None:
    """BFS over governing values; edges discovered by interpreted stepping.

    Returns the realized action sequence reaching *target_value*, or ``None``.
    """
    start_val = start_plc.state.tags.get(governing)
    if start_val == target_value:
        return []
    seen: set[Any] = {start_val}
    frontier: deque[_Node] = deque([_Node(start_val, start_plc.fork(), [])])
    nodes = 0

    while frontier and nodes < _MAX_NODES:
        node = frontier.popleft()
        nodes += 1
        for steer in alphabet:
            trial = node.plc.fork()
            # Both steers fold productive dwells to _EMPTY_CAP; what is passed
            # here is a *reaction* budget that bails a steer only while it churns
            # without reaching an accumulation plateau.  A pulse that merely
            # starts a dwell settles within a few scans then folds, so it needs
            # only a small budget; the empty steer's long waits are plateaus,
            # not churn, so it is effectively unbounded.
            react_cap = _MAX_ADVANCE_ITERS if steer.kind == "empty" else _PULSE_REACT_CAP
            realized = _apply_steer(
                trial,
                steer,
                governing,
                node.value,
                ext_inputs,
                edge_ext,
                jump_ctx,
                react_cap,
            )
            if realized is None:
                continue
            nv = trial.state.tags.get(governing)
            if nv in seen:
                continue
            new_path = node.path + realized
            if nv == target_value:
                return new_path
            if len(new_path) > _MAX_CORRIDOR:
                continue
            seen.add(nv)
            frontier.append(_Node(nv, trial, new_path))
    return None


# ---------------------------------------------------------------------------
# Recursive corridor walk with prerequisite discovery
# ---------------------------------------------------------------------------


def _advance_work(work: PLC, steps: list[_Action]) -> None:
    """Replay *steps* on the work fork so it reaches the post-corridor state."""
    for action, scans in steps:
        if action:
            work.patch(action)
        for _ in range(scans):
            work.step()


def _recheck_prereqs(
    work: PLC,
    target_tag: str,
    target_value: Any,
) -> list[tuple[str, Any]]:
    """Ask the projected causal oracle what still blocks *target_tag*.

    Used after the serial prerequisite walk leaves the governing tag stuck:
    walking one prerequisite may have clobbered an earlier one (a side effect
    that broke a condition the governing tag needs).  ``cause(tag, to=value)``
    returns either a projected chain (proximate-cause ``triggers``) or, when it
    cannot find a single-step path, an ``unreachable`` chain whose ``blockers``
    name the load-bearing condition.  Both are mined for actionable
    ``(tag, value)`` sub-walk goals, skipping any already satisfied.
    """
    try:
        chain = work.cause(target_tag, to=target_value)
    except Exception:  # noqa: BLE001 - oracle is best-effort; never break the walk
        return []
    if chain is None:
        return []

    tags = work.state.tags
    goals: list[tuple[str, Any]] = []
    seen: set[tuple[str, Any]] = set()

    def _add(name: str, value: Any) -> None:
        key = (name, value)
        if key in seen or name == target_tag:
            return
        if _values_match(tags.get(name), value):
            return
        seen.add(key)
        goals.append(key)

    for step in chain.steps:
        for trig in step.triggers:
            _add(trig.tag_name, trig.to_value)
    for blocker in getattr(chain, "blockers", ()):  # unreachable mode
        _add(blocker.blocked_tag, blocker.needed_value)
        for sub in getattr(blocker, "sub_blockers", ()):
            _add(sub.blocked_tag, sub.needed_value)
    return goals


def _needs_decomposition(
    prereqs: list[tuple[str, Any]],
    target_tag: str,
    pdg: ProgramGraph,
) -> tuple[bool, str | None]:
    """Detect whether two prerequisites of *target_tag* mutually interfere.

    Prerequisites that share an upstream cone tag or a common writer rung can
    clobber each other when walked serially.  This is the Tier 2
    (force-and-solve) insertion point: when detected, forking a fork per
    subsystem and solving them independently side-steps the interference.

    Returns ``(needs_decomposition, detail)`` naming the first overlapping
    pair, or ``(False, None)``.
    """
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
) -> None:
    """Log a Tier 2 (force-and-solve) hint when unresolved prereqs couple.

    Called before the walk gives up on *target_tag*: if two of its
    prerequisites share an upstream cone or writer, serial walking cannot
    avoid the clobber and forking a fork per subsystem would.  Observability
    only — the mechanism waits for a mutual-interference test case.
    """
    needs_decomp, detail = _needs_decomposition(coupling, target_tag, pdg)
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


def _recover_via_oracle(
    work: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    explore_context: Any = None,
) -> list[_Action] | None:
    """Oracle-driven serial-clobber recovery for *target_tag* -> *target_value*.

    When the normal corridor walk leaves the target short of its value — a later
    sub-walk clobbered an earlier prerequisite (a side effect that broke a
    condition the target needs) — ask the projected causal oracle what still
    blocks it (:func:`_recheck_prereqs`) and walk those goals.  Bounded by
    ``_MAX_RECHECK_ITERS`` rounds.

    Applies the recovery steps to *work* in place and returns them, or ``None``
    if the target cannot be recovered.  On ``None`` the caller returns
    ``None`` too, so any partial mutation of *work* is discarded with it.
    """
    recovered: list[_Action] = []
    for _ in range(_MAX_RECHECK_ITERS):
        if _values_match(work.state.tags.get(target_tag), target_value):
            return recovered
        goals = _recheck_prereqs(work, target_tag, target_value)
        if not goals:
            return None
        for rtag, rval in goals:
            sub = _walk_to_goal(
                work,
                rtag,
                rval,
                pdg,
                program,
                known,
                ext_inputs,
                edge_ext,
                budget - len(recovered),
                depth + 1,
                visited,
                nd_domains=nd_domains,
                explore_context=explore_context,
            )
            if sub is None:
                return None
            recovered.extend(sub)
            if len(recovered) > budget:
                return None
    return recovered if _values_match(work.state.tags.get(target_tag), target_value) else None


def _walk_to_goal(
    work: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
    budget: int,
    depth: int = 0,
    visited: frozenset[tuple[str, Any]] = frozenset(),
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    explore_context: Any = None,
) -> list[_Action] | None:
    """Drive *target_tag* to *target_value* on *work*, discovering prerequisites.

    Picks a governing tag, tries ``_explore``.  On failure, extracts
    unsatisfied enabling conditions from the target-value writer rung(s)
    and recursively walks those first.  On success but target still
    unsatisfied, walks residual conditions from the target's own writer.

    *work* is modified in place (fork advanced through every successful
    sub-corridor).  Returns the accumulated action list, or ``None``.
    """
    if _values_match(work.state.tags.get(target_tag), target_value):
        return []
    goal_key = (target_tag, target_value)
    if goal_key in visited or depth > _MAX_PREREQ_DEPTH or budget <= 0:
        return None
    visited = visited | {goal_key}

    governing, gov_value = _governing(
        target_tag, target_value, pdg, program, explore_context=explore_context, plc=work
    )
    alphabet = _steer_alphabet(governing, pdg, known, program, gov_value, nd_domains=nd_domains)
    jump_ctx = _build_jump_context(work, pdg, program)

    steps = _explore(work, governing, gov_value, alphabet, ext_inputs, edge_ext, jump_ctx)

    if steps is None:
        prereqs = _unsatisfied_conditions(
            governing,
            gov_value,
            dict(work.state.tags),
            pdg,
            program,
            nd_domains=nd_domains,
        )
        if not prereqs:
            return None
        # Snapshot the pre-clobber state before walking prerequisites serially.
        # Tier 2 (force-and-solve) will fork from here to solve interfering
        # subsystems independently; for now it anchors the diagnostic below.
        checkpoint = work.fork()
        all_steps: list[_Action] = []
        for ptag, pval in prereqs:
            sub = _walk_to_goal(
                work,
                ptag,
                pval,
                pdg,
                program,
                known,
                ext_inputs,
                edge_ext,
                budget - len(all_steps),
                depth + 1,
                visited,
                nd_domains=nd_domains,
                explore_context=explore_context,
            )
            if sub is None:
                return None
            all_steps.extend(sub)

        alphabet = _steer_alphabet(governing, pdg, known, program, gov_value, nd_domains=nd_domains)
        jump_ctx = _build_jump_context(work, pdg, program)
        steps = _explore(work, governing, gov_value, alphabet, ext_inputs, edge_ext, jump_ctx)

        if steps is None:
            # Serial-clobber recovery: walking a later prerequisite may have
            # broken an earlier one.  Ask the oracle what still blocks the
            # governing value and walk those, then proceed as if _explore had
            # found a zero-action corridor (recovery already advanced *work*).
            rec = _recover_via_oracle(
                work,
                governing,
                gov_value,
                pdg,
                program,
                known,
                ext_inputs,
                edge_ext,
                budget - len(all_steps),
                depth,
                visited,
                nd_domains=nd_domains,
                explore_context=explore_context,
            )
            if rec is None:
                _log_decomposition_hint(target_tag, prereqs, pdg, checkpoint)
                return None
            logger.info(
                "walk: recovered %s -> %s via oracle re-check in %d action(s)",
                governing,
                gov_value,
                len(rec),
            )
            all_steps.extend(rec)
            steps = []  # recovery already advanced *work*

        logger.info(
            "walk: corridor on %s reached %s in %d action(s)",
            governing,
            gov_value,
            len(steps),
        )
        _advance_work(work, steps)
        all_steps.extend(steps)
        if len(all_steps) > budget:
            return None
        return _check_residuals(
            work,
            target_tag,
            target_value,
            governing,
            pdg,
            program,
            known,
            ext_inputs,
            edge_ext,
            budget - len(all_steps),
            depth,
            visited,
            all_steps,
            nd_domains=nd_domains,
            explore_context=explore_context,
        )

    logger.info(
        "walk: corridor on %s reached %s in %d action(s)",
        governing,
        gov_value,
        len(steps),
    )
    _advance_work(work, steps)
    all_steps = list(steps)
    if len(all_steps) > budget:
        return None
    return _check_residuals(
        work,
        target_tag,
        target_value,
        governing,
        pdg,
        program,
        known,
        ext_inputs,
        edge_ext,
        budget - len(all_steps),
        depth,
        visited,
        all_steps,
        nd_domains=nd_domains,
        explore_context=explore_context,
    )


def _check_residuals(
    work: PLC,
    target_tag: str,
    target_value: Any,
    governing: str,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    all_steps: list[_Action],
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    explore_context: Any = None,
) -> list[_Action] | None:
    """After driving the governing tag, walk any residual conditions.

    Uses the oracle-driven re-check loop (:func:`_recover_via_oracle`) to walk
    the target's still-unsatisfied conditions.  This subsumes the older static
    ``_unsatisfied_conditions`` residual sweep: walking residuals serially can
    clobber the governing corridor (a side effect of a later condition breaking
    an earlier one), and the oracle loop both walks the residuals and recovers
    from such clobbers in a single bounded loop.
    """
    if _values_match(work.state.tags.get(target_tag), target_value):
        return all_steps

    if target_tag != governing:
        rec = _recover_via_oracle(
            work,
            target_tag,
            target_value,
            pdg,
            program,
            known,
            ext_inputs,
            edge_ext,
            budget - len(all_steps),
            depth,
            visited,
            nd_domains=nd_domains,
            explore_context=explore_context,
        )
        if rec is not None:
            all_steps.extend(rec)

    if _values_match(work.state.tags.get(target_tag), target_value):
        return all_steps

    # Unrecoverable: log a Tier 2 hint if the target's conditions couple.
    coupling = _unsatisfied_conditions(
        target_tag, target_value, dict(work.state.tags), pdg, program, nd_domains=nd_domains
    )
    _log_decomposition_hint(target_tag, [(governing, True), *coupling], pdg)
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def plan_walk(
    plc: PLC,
    snapshot: dict[str, Any],
    expr: Any,
    max_steps: int,
    avoid_pred: Any = None,
    *,
    explore_context: Any = None,
    atom_index: dict[str, list[Any]] | None = None,
    domain_sources: dict[str, str] | None = None,
    unlink: list[str] | None = None,
) -> Path | None:
    """Try to reach the target by walking a governing-tag value corridor.

    Returns a :class:`~pyrung.core.analysis.graph.Path` on success, or
    ``None`` when the walker cannot solve it.

    When *explore_context* (an ``_ExploreContext`` from the prover pipeline)
    is provided, the walker uses its ``nondeterministic_dims`` for non-Bool
    input steers and inequality prerequisite resolution.
    """
    from pyrung.core.analysis.graph import Path, ReachabilityStep
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    program = getattr(plc, "_program", None)
    if program is None:
        return None

    goals = _extract_goals(expr, snapshot)
    if goals is None:
        return None

    known = plc._known_tags_by_name
    tag_defaults = {t.name: t.default for t in known.values()}

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    if explore_context is not None:
        nd_domains = getattr(explore_context, "nondeterministic_dims", None)

    # Resolve choice names ("IDLE") to underlying values.
    resolved_goals: list[tuple[str, Any]] = []
    for target_tag, target_value in goals:
        if isinstance(target_value, str):
            t = known.get(target_tag)
            choices = getattr(t, "choices", None) if t is not None else None
            if choices:
                inv = {name: val for val, name in choices.items()}
                if target_value in inv:
                    target_value = inv[target_value]
        resolved_goals.append((target_tag, target_value))

    # Already satisfied?
    if all(snapshot.get(tag) == val for tag, val in resolved_goals):
        return Path(
            reachable=True, steps=(), total_changes=0, total_scans=0, tag_defaults=tag_defaults
        )

    pdg = build_program_graph(program)
    ext_inputs = _external_bool_inputs(pdg, known)
    edge_ext = _edge_tags(pdg, program) & set(ext_inputs)

    # Walk each goal, discovering prerequisites recursively.
    # Install harness on the work fork so feedback timing is respected
    # during folded jumps.  fork() propagates the harness to trial forks.
    all_steps: list[_Action] = []
    work = plc.fork()
    _install_walk_harness(work)

    # Linked Fb tags are driven by the Harness, not steered directly.
    if work._harness is not None:
        if unlink:
            work._harness.unlink(unlink)
        linked_fbs = {c.fb_name for c in work._harness.couplings()}
        ext_inputs = [i for i in ext_inputs if i not in linked_fbs]
        edge_ext -= linked_fbs
    for target_tag, target_value in resolved_goals:
        if _values_match(work.state.tags.get(target_tag), target_value):
            continue

        steps = _walk_to_goal(
            work,
            target_tag,
            target_value,
            pdg,
            program,
            known,
            ext_inputs,
            edge_ext,
            max_steps - len(all_steps),
            nd_domains=nd_domains,
            explore_context=explore_context,
        )
        if steps is None or not steps:
            return None
        all_steps.extend(steps)
        if len(all_steps) > max_steps:
            return None

    if not all_steps:
        return None

    # Verify on a fresh fork against the *full* original expression.
    # The verify fork uses the real Harness (step-by-step, no folding) so
    # that feedback timing is validated at full fidelity.
    verify = plc.fork()
    if work._harness is not None:
        from pyrung.core.harness import Harness

        Harness(verify).install()
    for action, scans in all_steps:
        if action:
            verify.patch(action)
        for _ in range(scans):
            verify.step()
            if avoid_pred is not None and avoid_pred(dict(verify.state.tags)):
                logger.info("walk: path passes through avoided state")
                return None
    if _eval_expr_from_state(expr, dict(verify.state.tags)) is not True:
        logger.info("walk: replay verification failed for compound target")
        return None

    # Build annotated steps: replay on a second fork to collect per-step state
    # for semantic constraint annotations.
    rsteps: list[ReachabilityStep] = []
    if atom_index is not None and domain_sources is not None:
        from pyrung.core.analysis.graph import _classify_step_inputs

        annotate_fork = plc.fork()
        for action, scans in all_steps:
            if action:
                annotate_fork.patch(action)
            for _ in range(scans):
                annotate_fork.step()
            constraints = (
                _classify_step_inputs(
                    action, atom_index, domain_sources, dict(annotate_fork.state.tags)
                )
                if action
                else None
            ) or None
            rsteps.append(
                ReachabilityStep(
                    action=action,
                    source_key=(),
                    dest_key=(),
                    scans=scans,
                    constraints=constraints,
                )
            )
    else:
        rsteps = [
            ReachabilityStep(action=action, source_key=(), dest_key=(), scans=scans)
            for action, scans in all_steps
        ]
    from pyrung.core.runner import _count_visible_changes

    total_changes = _count_visible_changes(rsteps, tag_defaults)
    total_scans = sum(scans for _action, scans in all_steps)
    logger.info("walk: reached compound target in %d step(s)", len(rsteps))
    return Path(
        reachable=True,
        steps=tuple(rsteps),
        total_changes=total_changes,
        total_scans=total_scans,
        tag_defaults=tag_defaults,
    )
