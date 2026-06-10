"""Time folding: jump context, accumulator crossings, and the advance loop.

A held wait only changes the program when a comparison the program reads
against some accumulator flips; between flips every rung emits identical
output, so those scans fold into one real step (dt knob for timers,
acc-patch for per-scan counters), guarded by the plateau check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import _EMPTY_CAP, _EPS, _MAX_ADVANCE_ITERS
from pyrung.core.analysis.walk.physical import _harness_nearest_scan

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC

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
    bidir: bool = False  # CountUp with down_condition — delta sign varies at runtime


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
    # Per-scan churn tags proven unobservable (unread self-updaters) —
    # excluded from the plateau check so their drift doesn't defeat folding.
    churn_excluded: frozenset[str] = frozenset()


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
        bidir = isinstance(instr, CountUpInstruction) and instr.down_condition is not None
        preset = instr.preset
        out[instr.accumulator.name] = _AccSource(
            acc_name=instr.accumulator.name,
            done_bit=instr.done_bit.name,
            preset=preset.name if isinstance(preset, Tag) else preset,
            kind=kind,
            timed=timed,
            bidir=bidir,
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
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
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


def _calc_self_referential(tag: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when *tag* is the dest of a ``calc`` that reads *tag* itself.

    A self-updating calc — ``calc(tag + 1, tag)``, ``calc((tag + 1) % 6, tag)``,
    etc. — is a stateful multi-value tag (counter-like) even when the wrapper
    op (modulo, mask) hides the ±1 from the shared monotone-stepping detector
    ``_has_arithmetic_writer``.  Used to decide governance (liberally — the
    corridor is confirmed by replay regardless) and to identify per-scan churn
    candidates for the plateau-guard exclusions.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
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


def _harness_referenced_names(plc: PLC) -> frozenset[str]:
    """Tag names the installed Harness reads or writes (couplings)."""
    h = plc._harness
    if h is None:
        return frozenset()
    names: set[str] = set()
    for c in h.couplings():
        names.add(c.en_name)
        names.add(c.fb_name)
    return frozenset(names)


def _unread_churn_tags(plc: PLC, pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Self-updating tags nothing else reads — unobservable per-scan churn.

    The plateau guard refuses to fold while any visible (non-accumulator)
    tag changes, so one unconditional ``calc((T + 1) % 2, T)`` rung makes
    folding unavailable program-wide.  When such a tag has **no readers
    outside its own writer rungs** its value is unobservable: no rung
    condition, data operand, or Harness coupling depends on it, so its drift
    on a plateau cannot change anything the walk steers or the step-by-step
    verify replay checks.  Excluding it from the visible-items set is exact,
    not heuristic.

    Conservatism: every writer node must write only this tag (a same-rung
    instruction can't smuggle the value into another dest) — implicit system
    fault flags are tolerated only when nothing reads them, since a flag flip
    nothing conditions on is as unobservable as the churner itself.  Every
    reader node must be one of its writer nodes, and the tag must not be
    referenced by a Harness coupling.  Goal tags are subtracted by the
    caller — a walk targeting the churner itself reads it.
    """
    from pyrung.core.system_points import system

    fault_names = frozenset(
        {
            system.fault.division_error.name,
            system.fault.out_of_range.name,
            system.fault.address_error.name,
        }
    )
    out: set[str] = set()
    harness_names = _harness_referenced_names(plc)
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names or tag in fault_names:
            continue
        readers = pdg.all_readers_of.get(tag, frozenset())
        if not readers <= writers:
            continue
        ok = True
        for ri in writers:
            extra = pdg.rung_nodes[ri].writes - {tag}
            if not extra <= fault_names or any(
                pdg.all_readers_of.get(f, frozenset()) for f in extra
            ):
                ok = False
                break
        if not ok:
            continue
        if _calc_self_referential(tag, pdg, program):
            out.add(tag)
    return frozenset(out)


def _node_reads(node: Any) -> frozenset[str]:
    """Every tag name *node* depends on (condition, data, exclusive reads;
    return-early guard reads are already folded into condition_reads)."""
    return node.condition_reads | node.data_reads | node.exclusive_reads


def _downstream_closure(pdg: ProgramGraph, root: str) -> frozenset[str]:
    """All tags whose values can depend on *root*, transitively.

    Walks reader rungs to their writes; a reader rung that calls subroutines
    gates every rung of those subroutines, so their writes (and transitive
    calls) join the closure too.
    """
    sub_nodes: dict[str, list[Any]] = {}
    for node in pdg.rung_nodes:
        if node.subroutine is not None:
            sub_nodes.setdefault(node.subroutine, []).append(node)

    closure: set[str] = {root}
    called: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in pdg.rung_nodes:
            if not (_node_reads(node) & closure):
                continue
            if not node.writes <= closure:
                closure |= node.writes
                changed = True
            for sub in node.calls:
                if sub in called:
                    continue
                called.add(sub)
                stack = [sub]
                while stack:
                    name = stack.pop()
                    for sn in sub_nodes.get(name, ()):
                        if not sn.writes <= closure:
                            closure |= sn.writes
                            changed = True
                        for nested in sn.calls:
                            if nested not in called:
                                called.add(nested)
                                stack.append(nested)
    return frozenset(closure)


def _disjoint_churn_closures(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
    skip_roots: frozenset[str],
) -> frozenset[str]:
    """Read churn whose downstream cone never reaches the walk's targets.

    Roots are self-updating ``calc`` tags that DO have readers outside their
    writer rungs (the unread case is rung 1's, passed as *skip_roots* so the
    two passes stay independently ablatable).  A root's downstream closure —
    everything its readers write, transitively — is excluded from the
    plateau guard only when it is fully disjoint from the union of the
    targets' upstream cones and from Harness coupling names: then nothing
    the walk steers toward, the verify replay's target check reads, or the
    Harness synthesizes can depend on any closure tag, so folding past
    closure flips is unobservable where it matters.  Divergence (a stateful
    closure tag landing at a different phase than true stepping) stays
    confined to the disjoint cone; the step-by-step verify replay backstops
    the boundary.  With no targets declared nothing is provably disjoint,
    so nothing is excluded.
    """
    if not target_names:
        return frozenset()
    harness_names = _harness_referenced_names(plc)
    cone: set[str] = set()
    for t in target_names:
        cone.add(t)
        cone |= pdg.upstream_slice_with_calls(t)

    excluded: set[str] = set()
    for tag, writers in pdg.writers_of.items():
        if tag in skip_roots or tag in harness_names:
            continue
        readers = pdg.all_readers_of.get(tag, frozenset())
        if readers <= writers:
            continue  # unread churn — rung 1's case
        if not _calc_self_referential(tag, pdg, program):
            continue
        closure = _downstream_closure(pdg, tag)
        if closure & cone or closure & harness_names:
            continue
        excluded |= closure
    return frozenset(excluded - target_names)


def _build_jump_context(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    *,
    target_names: frozenset[str] = frozenset(),
    advice: Any = None,
    journal: Any = None,
) -> _JumpContext:
    """Build the static fold priors.  *target_names* are the walk's goal
    tags — never excluded from the plateau guard, since the verify replay
    (and ``done``/``monitor`` predicates) read them.  *advice* gates the
    fold-kind passes (``None`` = all enabled); *journal* records applied
    exclusions.
    """
    sources = _collect_acc_sources(program)
    acc_names = frozenset(s.acc_name for s in sources)
    h = plc._harness
    profile_fb_names: frozenset[str] = (
        frozenset(c.fb_name for c in h._profile_couplings) if h is not None else frozenset()
    )
    comparisons, read_tags = _scan_rung_reads(pdg, program, acc_names | profile_fb_names)
    churn_excluded: frozenset[str] = frozenset()
    unread = _unread_churn_tags(plc, pdg, program) - target_names
    if advice is None or advice.has("fold_unread_churn"):
        churn_excluded |= unread
        if unread and journal is not None:
            journal.add_note(
                "fold: unread churn excluded from plateau guard: " + ", ".join(sorted(unread))
            )
    if advice is None or advice.has("fold_disjoint_churn"):
        disjoint = _disjoint_churn_closures(plc, pdg, program, target_names, skip_roots=unread)
        churn_excluded |= disjoint
        if disjoint and journal is not None:
            journal.add_note(
                "fold: target-disjoint churn cone excluded from plateau guard: "
                + ", ".join(sorted(disjoint))
            )
    return _JumpContext(
        sources=tuple(sources),
        acc_names=acc_names,
        comparisons=comparisons,
        read_done=frozenset(s.done_bit for s in sources) & read_tags,
        normal_dt=float(getattr(plc, "_dt", 0.010) or 0.010),
        profile_fb_names=profile_fb_names,
        churn_excluded=churn_excluded,
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
        if abs(delta) <= _EPS:
            continue
        if delta < 0 and not src.bidir:
            continue
        # Actionable boundaries in progress coordinates: (target, strict).
        bounds: list[tuple[float, bool]] = []
        if src.done_bit in ctx.read_done:
            preset = _resolve_num(src.preset, state)
            if preset is None:
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
            if delta > 0:
                scans = _scans_to_cross(pa, delta, target, strict)
            else:
                scans = _scans_to_uncross(pa, delta, target, strict)
            if scans is None:
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


def _scans_to_uncross(pa: float, delta: float, target: float, strict: bool) -> int | None:
    """Scans for progress (at ``pa``, negative ``delta``/scan) to drop past ``target``.

    Mirror of :func:`_scans_to_cross` for bidir counters moving opposite to
    their declared kind.  ``strict`` (``gt``/``le`` boundary): first scan where
    ``progress <= target``; non-strict (``ge``/``lt``): first scan where
    ``progress < target``.  ``None`` when already below.
    """
    if strict:
        if pa <= target:
            return None
        return max(1, math.ceil((target - pa) / delta))
    if pa < target:
        return None
    return max(1, math.floor((target - pa) / delta) + 1)


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
        if pb is None or pa is None:
            continue
        prog_delta = pa - pb
        if abs(prog_delta) <= _EPS:
            continue
        if prog_delta < 0 and not src.bidir:
            continue
        raw_delta = int(round(prog_delta if src.kind == "up" else -prog_delta))
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
    exclude = ctx.acc_names | ctx.profile_fb_names | ctx.churn_excluded
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
