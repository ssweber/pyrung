"""Time folding: collapse provably-identical scans into one step.

Between accumulator crossings every rung emits identical output.  The fold
detects these plateaus, computes the nearest crossing in closed form, and
jumps the runner forward — dt knob for timers, acc-patch for per-scan
counters, modular arithmetic for wrapping self-calcs.  Soundness rests on
the plateau guard (only-accumulators-changed), not on the crossing set
being exhaustive.

Module structure
────────────────
1. Source types         — what the fold tracks (_AccSource, _ModWrap)
2. Fold context         — assembled static priors for the advance loop
3. Instruction registry — instruction type → fold-source mapping
4. Expression matching  — pattern detection on calc expressions
5. Comparison scanning  — rung reads relevant to crossings
6. Churn analysis       — identifying unobservable state changes
7. Self-calc sources    — calc-style counters promoted to fold sources
8. Mirror detection     — constant-offset views of tracked sources
9. Context assembly     — build the fold context from program structure
10. Crossing arithmetic — computing jump distances
11. State helpers       — accumulator totals, visible-items snapshot
12. Jump execution      — patching state forward in one step
13. The fold loop       — plateau detection, crossing, advance
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import _EMPTY_CAP, _EPS, _MAX_ADVANCE_ITERS
from pyrung.core.analysis.walk.physical import _harness_nearest_scan

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.walk.base import _DebugSink
    from pyrung.core.runner import PLC


# ── 1. Source types ──────────────────────────────────────────────────
#
# A "source" is anything whose accumulation the fold tracks: formal
# timers/counters, linear self-calcs, and modular self-calcs.


@dataclass(frozen=True)
class _AccSource:
    """A timer/counter whose accumulator a held wait advances.

    Also used for linear self-calc tags promoted to fold sources
    (with a sentinel ``done_bit`` that nothing reads).
    """

    acc_name: str
    done_bit: str
    preset: Any          # int literal or tag-name str (dynamic preset)
    kind: str            # "up" (on/off-delay, count-up) | "down" (count-down)
    timed: bool          # True: time-based (dt knob).  False: per-scan (acc patch).
    bidir: bool = False  # CountUp with down_condition — delta sign varies at runtime


@dataclass(frozen=True)
class _ModWrap:
    """An unconditional affine-mod self-calc: ``tag := (tag + c) % m``.

    A wrapping per-scan counter can't ride the monotone progress
    coordinates (its measured delta flips sign at the wrap), so it gets
    its own crossing arithmetic: the first truth-flip of any read
    comparison along the modular recurrence bounds the jump, and
    ``_do_jump`` patches the value forward in closed form so landings
    stay bit-equal to step-by-step execution.
    """

    name: str
    c: int
    m: int


# ── 2. Fold context ─────────────────────────────────────────────────


@dataclass(frozen=True)
class _FoldContext:
    """Static priors for the time-advance fold loop, built once per walk."""

    sources: tuple[_AccSource, ...]
    acc_names: frozenset[str]
    # tag_name -> tuple of (comparison form, operand) read by some rung.
    # Covers both accumulators and profile feedback tags.
    comparisons: dict[str, tuple[tuple[str, Any], ...]]
    # Done-bit tag names that some rung actually reads (actionable crossings).
    read_done: frozenset[str]
    normal_dt: float
    # Profile feedback tags — excluded from visible-items plateau check.
    profile_fb_names: frozenset[str] = frozenset()
    # Per-scan churn tags proven unobservable (unread self-updaters) —
    # excluded from the plateau check so their drift doesn't defeat folding.
    churn_excluded: frozenset[str] = frozenset()
    # Mod-wrap self-calc sources — tracked exactly, not invisible.
    modwrap: tuple[_ModWrap, ...] = ()
    modwrap_names: frozenset[str] = frozenset()
    # One full cycle of every mod-wrap source (lcm of periods, capped):
    # a plateau run this long with no accumulator progress is a limit
    # cycle — waiting cannot help, so the advance loop bails.
    mod_period: int = 0
    # Acc mirrors — copy/constant-offset views of a source whose
    # comparisons were translated onto the source; excluded from the
    # plateau guard (they churn with the source they track).
    mirror_names: frozenset[str] = frozenset()


# Backward compatibility.
_JumpContext = _FoldContext


# ── 3. Instruction registry ─────────────────────────────────────────
#
# Maps instruction types to fold-source parameters.  Adding a new
# accumulating instruction (e.g. a retentive timer) is one dict entry.
#
# Each entry: instruction_class → (kind, timed).
# ``bidir`` is instruction-specific (CountUp with down_condition) and
# handled in _collect_acc_sources.
#
# Built lazily to avoid circular imports — instruction modules import
# from core, and core may transitively reach this module.

_SOURCE_REGISTRY: dict[type, tuple[str, bool]] | None = None


def _ensure_registry() -> dict[type, tuple[str, bool]]:
    """Build the instruction→(kind, timed) dispatch table on first use."""
    global _SOURCE_REGISTRY
    if _SOURCE_REGISTRY is not None:
        return _SOURCE_REGISTRY

    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    _SOURCE_REGISTRY = {
        #
        # Timers: time-based accumulation (dt knob during jump).
        #
        OnDelayInstruction:  ("up",   True),
        OffDelayInstruction: ("up",   True),
        #
        # Counters: per-scan accumulation (acc patch during jump).
        #
        CountUpInstruction:   ("up",   False),
        CountDownInstruction: ("down", False),
    }
    return _SOURCE_REGISTRY


def _collect_acc_sources(program: Any) -> list[_AccSource]:
    """Introspect every timer/counter instruction (incl. subroutines).

    Uses the instruction registry to map each instruction to its fold
    source.  Instructions not in the registry are ignored.
    """
    from pyrung.core.instruction.counters import CountUpInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    registry = _ensure_registry()
    out: dict[str, _AccSource] = {}

    for instr in walk_instructions(program):
        params = registry.get(type(instr))
        if params is None:
            continue
        kind, timed = params
        bidir = (
            isinstance(instr, CountUpInstruction)
            and instr.down_condition is not None
        )
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


# ── 4. Expression matching ──────────────────────────────────────────
#
# Pattern detection on calc expressions: identify self-referential
# calcs, affine forms (tag ± c), and affine-mod forms ((tag ± c) % m).


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


def _is_free_running_selfcalc(tag: str, pdg: ProgramGraph, program: Any) -> bool:
    """A tag advanced by a single unconditional top-level self-calc.

    Such a tag is a clock: it moves on its own every scan, so value-stepping
    it as a governing corridor is futile — the fold tracks it (when affine)
    or excludes it (when unread / target-disjoint) instead.
    """
    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return False
    (ri,) = writers
    node = pdg.rung_nodes[ri]
    if node.scope != "main" or node.branch_path or node.condition_reads:
        return False
    return _calc_self_referential(tag, pdg, program)


def _match_affine_selfcalc(expr: Any, tag: str) -> tuple[int, int | None] | None:
    """Match ``(tag ± c) % m`` / ``tag ± c`` (commutative ``+``); return
    ``(c, m)`` with ``m=None`` for the linear form, or ``None``.

    Only literal int constants qualify (the fold patches in closed form, so
    the step delta must be static), ``c != 0`` (otherwise nothing churns),
    and ``0 < m <= 32767`` (the patch value must stay in Int range).
    """
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr

    def lit(e: Any) -> int | None:
        if isinstance(e, LiteralExpr):
            e = e.value
        if isinstance(e, bool) or not isinstance(e, int):
            return None
        return e

    def is_self(e: Any) -> bool:
        return isinstance(e, TagExpr) and getattr(e.tag, "name", None) == tag

    def linear(e: Any) -> int | None:
        if not isinstance(e, BinaryExpr):
            return None
        if e.symbol == "+":
            if is_self(e.left):
                return lit(e.right)
            if is_self(e.right):
                return lit(e.left)
        elif e.symbol == "-" and is_self(e.left):
            k = lit(e.right)
            return None if k is None else -k
        return None

    if isinstance(expr, BinaryExpr) and expr.symbol == "%":
        m = lit(expr.right)
        c = linear(expr.left)
        if m is not None and c is not None and c != 0 and 0 < m <= 32767:
            return (c, m)
        return None
    c = linear(expr)
    if c is not None and c != 0:
        return (c, None)
    return None


def _match_affine_of(expr: Any) -> tuple[str, int] | None:
    """Match ``tag ± c`` (commutative ``+``, literal int c); return ``(tag, c)``.

    Used to detect constant-offset mirrors of tracked sources.
    """
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr

    def lit(e: Any) -> int | None:
        if isinstance(e, LiteralExpr):
            e = e.value
        if isinstance(e, bool) or not isinstance(e, int):
            return None
        return e

    def name(e: Any) -> str | None:
        return getattr(e.tag, "name", None) if isinstance(e, TagExpr) else None

    if not isinstance(expr, BinaryExpr):
        return None
    if expr.symbol == "+":
        n, k = name(expr.left), lit(expr.right)
        if n is None or k is None:
            n, k = name(expr.right), lit(expr.left)
        if n is not None and k is not None:
            return (n, k)
    elif expr.symbol == "-":
        n, k = name(expr.left), lit(expr.right)
        if n is not None and k is not None:
            return (n, -k)
    return None


# ── 5. Comparison scanning ──────────────────────────────────────────


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


# ── 6. Churn analysis ───────────────────────────────────────────────
#
# Tags that change every scan but whose changes are unobservable.
# Excluding them from the plateau guard lets the fold proceed where
# it would otherwise refuse.
#
# Three tiers, independently ablatable:
#   - Unread churn:          self-updaters nothing else reads
#   - Target-disjoint churn: self-updaters whose downstream cone
#                            never reaches the walk targets
#   - Clock views:           copy/offset views of a source or
#                            free-running self-calc


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


def _node_reads(node: Any) -> frozenset[str]:
    """Every tag name *node* depends on (condition, data, exclusive reads)."""
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
    instruction can't smuggle the value into another dest).  Every reader
    node must be one of its writer nodes, and the tag must not be referenced
    by a Harness coupling.  Goal tags are subtracted by the caller — a walk
    targeting the churner itself reads it.
    """
    out: set[str] = set()
    harness_names = _harness_referenced_names(plc)
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names:
            continue
        readers = pdg.all_readers_of.get(tag, frozenset())
        if not readers <= writers:
            continue
        ok = True
        for ri in writers:
            extra = pdg.rung_nodes[ri].writes - {tag}
            if extra:
                ok = False
                break
        if not ok:
            continue
        if _calc_self_referential(tag, pdg, program):
            out.add(tag)
    return frozenset(out)


def _disjoint_churn_closures(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
    skip_roots: frozenset[str],
) -> frozenset[str]:
    """Read churn whose downstream cone never reaches the walk's targets.

    Roots are self-updating ``calc`` tags that DO have readers outside their
    writer rungs (the unread case is handled by ``_unread_churn_tags``,
    passed as *skip_roots* so the two passes stay independently ablatable).

    A root's downstream closure — everything its readers write, transitively
    — is excluded from the plateau guard only when it is fully disjoint from
    the union of the targets' upstream cones and from Harness coupling names.
    With no targets declared nothing is provably disjoint, so nothing is
    excluded.
    """
    if not target_names:
        return frozenset()
    harness_names = _harness_referenced_names(plc)
    cone: set[str] = set()
    for t in target_names:
        cone.add(t)
        cone |= pdg.upstream_slice(t)

    excluded: set[str] = set()
    for tag, writers in pdg.writers_of.items():
        if tag in skip_roots or tag in harness_names:
            continue
        readers = pdg.all_readers_of.get(tag, frozenset())
        if readers <= writers:
            continue  # unread churn — handled by _unread_churn_tags
        if not _calc_self_referential(tag, pdg, program):
            continue
        closure = _downstream_closure(pdg, tag)
        if closure & cone or closure & harness_names:
            continue
        excluded |= closure
    return frozenset(excluded - target_names)


# ── 7. Self-calc sources ────────────────────────────────────────────
#
# Calc-style counters promoted to fold sources.  Linear forms become
# _AccSource entries (same crossing arithmetic as formal counters).
# Modular forms become _ModWrap entries with dedicated crossing logic.


def _selfcalc_sources(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
) -> list[tuple[str, int, int | None]]:
    """Affine(-mod) self-calc churners eligible as exact fold sources.

    Returns ``(tag_name, c, m)`` where ``m=None`` for linear form.

    Eligibility mirrors the accumulator contract: exactly one writer rung,
    top-level main scope, unconditional (so the per-scan delta is
    unconditional too), writing nothing but the tag, not
    Harness-referenced, and not a goal tag.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction

    harness_names = _harness_referenced_names(plc)
    out: list[tuple[str, int, int | None]] = []
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names or tag in target_names:
            continue
        if pdg.all_readers_of.get(tag, frozenset()) <= writers:
            continue  # unread churn — kept independently ablatable
        if not _is_free_running_selfcalc(tag, pdg, program):
            continue
        (ri,) = writers
        node = pdg.rung_nodes[ri]
        if node.writes != frozenset({tag}):
            continue
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        calc_writers = [
            instr
            for instr in ro._instructions
            if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag
        ]
        if len(calc_writers) != 1:
            continue
        matched = _match_affine_selfcalc(calc_writers[0].expression, tag)
        if matched is not None:
            out.append((tag, matched[0], matched[1]))
    return out


# ── 8. Mirror detection ─────────────────────────────────────────────
#
# Mirrors are tags that track a source accumulator with a constant
# offset: ``calc(source + k, mirror)`` or ``copy(source, mirror)``.
# Their comparison thresholds are translated onto the source (shifted
# by k) and the mirror itself is excluded from the plateau guard.


def _is_clock_view(tag: str, pdg: ProgramGraph, program: Any, source_names: frozenset[str]) -> bool:
    """A copy / constant-offset calc view of a fold source or free-running
    self-calc.  Like the source, it advances on its own — value-stepping
    it as a governing corridor is futile."""
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag

    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return False
    (ri,) = writers
    node = pdg.rung_nodes[ri]
    if node.scope != "main" or node.branch_path or node.condition_reads:
        return False
    ro = _resolve_rung(program, node)
    if ro is None:
        return False
    src: str | None = None
    for instr in ro._instructions:
        if isinstance(instr, CopyInstruction) and getattr(instr.dest, "name", None) == tag:
            if isinstance(instr.source, Tag) and instr.convert is None and not instr.oneshot:
                src = instr.source.name
        elif isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag:
            matched = _match_affine_of(instr.expression)
            if matched is not None:
                src = matched[0]
    if src is None:
        return False
    return src in source_names or _is_free_running_selfcalc(src, pdg, program)


def _mirror_candidates(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
    source_names: frozenset[str],
) -> list[tuple[str, str, int]]:
    """Structurally eligible acc mirrors: ``(mirror, source, k)`` with
    ``mirror = source + k`` refreshed unconditionally every scan.

    Structural only — read-shape validation (every read a literal simple
    comparison) happens at translation time in ``_build_fold_context``.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag

    harness_names = _harness_referenced_names(plc)
    out: list[tuple[str, str, int]] = []
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names or tag in target_names or tag in source_names:
            continue
        if len(writers) != 1:
            continue
        (ri,) = writers
        node = pdg.rung_nodes[ri]
        if node.scope != "main" or node.branch_path or node.condition_reads:
            continue
        if node.writes != frozenset({tag}):
            continue
        ro = _resolve_rung(program, node)
        if ro is None:
            continue
        matched: tuple[str, int] | None = None
        n_writers = 0
        for instr in ro._instructions:
            if isinstance(instr, CopyInstruction) and getattr(instr.dest, "name", None) == tag:
                n_writers += 1
                if isinstance(instr.source, Tag) and instr.convert is None and not instr.oneshot:
                    matched = (instr.source.name, 0)
            elif isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag:
                n_writers += 1
                m = _match_affine_of(instr.expression)
                if m is not None:
                    matched = m
        if n_writers != 1 or matched is None or matched[0] not in source_names:
            continue
        out.append((tag, matched[0], matched[1]))
    return out


def _mirror_reads_are_simple(tag: str, pdg: ProgramGraph, program: Any, writer_ri: int) -> bool:
    """Every program read of *tag* is a simple literal comparison on *tag*.

    Data/exclusive reads, compound or opaque conditions (which hide the tag
    from atom scanning), comparisons where *tag* is the operand side, and
    non-literal thresholds all refuse — the conservative direction: the
    mirror stays visible and the fold refuses as today.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung

    cmp_classes = {"CompareEq", "CompareNe", "CompareLt", "CompareLe", "CompareGt", "CompareGe"}

    def leaves(cond: Any) -> Any:
        subs = getattr(cond, "conditions", None)
        if subs is not None:
            for c in subs:
                yield from leaves(c)
        else:
            yield cond

    for ri, node in enumerate(pdg.rung_nodes):
        if ri == writer_ri:
            continue
        if tag in (node.data_reads | node.exclusive_reads):
            return False
        if tag not in node.condition_reads:
            continue
        ro = _resolve_rung(program, node)
        if ro is None:
            return False
        accounted = False
        for leaf in (x for c in ro._conditions for x in leaves(c)):
            reads = _extract_reads_from_condition(leaf, dict(pdg.tags))
            if tag not in reads:
                continue
            if type(leaf).__name__ not in cmp_classes:
                return False
            if getattr(getattr(leaf, "tag", None), "name", None) != tag:
                return False
            v = leaf.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return False
            accounted = True
        if not accounted:
            return False  # read via a path the leaf walk can't see — refuse
    return True


# ── 9. Context assembly ─────────────────────────────────────────────


def _build_fold_context(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    *,
    target_names: frozenset[str] = frozenset(),
    advice: Any = None,
    journal: Any = None,
) -> _FoldContext:
    """Build the static fold priors.

    *target_names* are the walk's goal tags — never excluded from the
    plateau guard, since the verify replay (and ``done``/``monitor``
    predicates) read them.  *advice* gates the fold-kind passes
    (``None`` = all enabled); *journal* records applied exclusions.
    """
    # ── Formal timer/counter sources ─────────────────────────────
    sources = _collect_acc_sources(program)
    acc_names = frozenset(s.acc_name for s in sources)

    # ── Profile feedback names ───────────────────────────────────
    h = plc._harness
    profile_fb_names: frozenset[str] = (
        frozenset(c.fb_name for c in h._profile_couplings) if h is not None else frozenset()
    )

    # ── Churn exclusions ─────────────────────────────────────────
    churn_excluded: frozenset[str] = frozenset()

    # Tier 1: unread self-updaters.
    unread = _unread_churn_tags(plc, pdg, program) - target_names
    if advice is None or advice.has("fold_unread_churn"):
        churn_excluded |= unread
        if unread and journal is not None:
            journal.add_note(
                "fold: unread churn excluded from plateau guard: " + ", ".join(sorted(unread))
            )

    # Tier 2: target-disjoint downstream cones.
    if advice is None or advice.has("fold_disjoint_churn"):
        disjoint = _disjoint_churn_closures(plc, pdg, program, target_names, skip_roots=unread)
        churn_excluded |= disjoint
        if disjoint and journal is not None:
            journal.add_note(
                "fold: target-disjoint churn cone excluded from plateau guard: "
                + ", ".join(sorted(disjoint))
            )

    # ── Self-calc promotion ──────────────────────────────────────
    # Linear self-calcs → _AccSource (same crossing path as counters).
    # Modular self-calcs → _ModWrap (dedicated crossing arithmetic).
    modwrap: list[_ModWrap] = []
    if advice is None or advice.has("fold_modwrap_source"):
        tracked: list[str] = []
        for tag, c, m in _selfcalc_sources(plc, pdg, program, target_names):
            if tag in acc_names or tag in churn_excluded:
                continue  # real accumulators / already-excluded cones win
            if m is None:
                # Linear: promote to _AccSource with sentinel done_bit.
                sources.append(
                    _AccSource(
                        acc_name=tag,
                        done_bit=f"__selfcalc:{tag}",  # sentinel — never read
                        preset=0,
                        kind="up" if c > 0 else "down",
                        timed=False,
                    )
                )
                acc_names |= {tag}
            else:
                # Modular: track with dedicated wrap arithmetic.
                modwrap.append(_ModWrap(tag, c, m))
            tracked.append(tag)
        if tracked and journal is not None:
            journal.add_note(
                "fold: self-calc churn tracked as fold source(s): " + ", ".join(sorted(tracked))
            )

    modwrap_names = frozenset(mw.name for mw in modwrap)

    # ── Mod-wrap period (limit-cycle detection) ──────────────────
    mod_period = 0
    if modwrap:
        mod_period = 1
        for mw in modwrap:
            mod_period = math.lcm(mod_period, mw.m // math.gcd(abs(mw.c), mw.m))
            if mod_period > 4096:
                mod_period = 4096
                break

    # ── Mirror detection and threshold translation ───────────────
    mirror_cands: list[tuple[str, str, int]] = []
    if advice is None or advice.has("fold_derived_crossings"):
        mirror_cands = _mirror_candidates(
            plc, pdg, program, target_names, acc_names | modwrap_names
        )

    # ── Comparison scanning ──────────────────────────────────────
    watch = acc_names | profile_fb_names | modwrap_names
    comparisons, read_tags = _scan_rung_reads(
        pdg, program, watch | frozenset(m for m, _a, _k in mirror_cands)
    )

    # ── Mirror threshold translation ─────────────────────────────
    mirror_names: set[str] = set()
    if mirror_cands:
        merged = dict(comparisons)
        for m, a, k in mirror_cands:
            cmps = merged.get(m, ())
            (writer_ri,) = pdg.writers_of[m]
            if any(
                isinstance(t, bool) or not isinstance(t, (int, float)) for _f, t in cmps
            ) or not _mirror_reads_are_simple(m, pdg, program, writer_ri):
                merged.pop(m, None)  # refused — stays visible, fold refuses as today
                continue
            merged[a] = merged.get(a, ()) + tuple((f, t - k) for f, t in cmps)
            merged.pop(m, None)
            mirror_names.add(m)
        comparisons = merged
        if mirror_names and journal is not None:
            journal.add_note(
                "fold: acc-mirror thresholds translated onto their sources: "
                + ", ".join(sorted(mirror_names))
            )

    return _FoldContext(
        sources=tuple(sources),
        acc_names=acc_names,
        comparisons=comparisons,
        read_done=frozenset(s.done_bit for s in sources) & read_tags,
        normal_dt=float(getattr(plc, "_dt", 0.010) or 0.010),
        profile_fb_names=profile_fb_names,
        churn_excluded=churn_excluded,
        modwrap=tuple(modwrap),
        modwrap_names=modwrap_names,
        mod_period=mod_period,
        mirror_names=frozenset(mirror_names),
    )


# Backward compatibility.
_build_jump_context = _build_fold_context


# ── 10. Crossing arithmetic ─────────────────────────────────────────
#
# Given current accumulator values and per-scan deltas, compute how
# many scans until the nearest comparison flips truth value.


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

    Mirror of ``_scans_to_cross`` for bidir counters moving opposite to
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


def _modwrap_first_flip(v0: int, c: int, m: int, cmps: list[tuple[str, float]]) -> int | None:
    """Scans until any comparison's truth first differs from its truth now.

    The recurrence ``v := (v + c) % m`` is periodic with period <= m, so a
    full period with no flip means the comparisons are constant forever.
    """

    def truth(v: int, form: str, k: float) -> bool:
        if form == "eq":
            return v == k
        if form == "ne":
            return v != k
        if form == "lt":
            return v < k
        if form == "le":
            return v <= k
        if form == "gt":
            return v > k
        return v >= k  # ge

    base = [truth(v0, form, k) for form, k in cmps]
    v = v0
    for step in range(1, m + 1):
        v = (v + c) % m
        for (form, k), b in zip(cmps, base, strict=True):
            if truth(v, form, k) != b:
                return step
    return None


def _nearest_acc_crossing(
    ctx: _FoldContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
    state: Any,
) -> int | None:
    """Scans to the nearest actionable accumulator crossing.

    Returns ``None`` when no live accumulator has one — pure drift with
    nothing left to cross.
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
    return best


def _nearest_mod_flip(ctx: _FoldContext, state: Any) -> int | None:
    """Scans to the nearest comparison truth-flip among mod-wrap sources.

    Returns ``None`` when no flip can ever come from the wrap cycles.
    """
    best: int | None = None
    for mw in ctx.modwrap:
        cmps_raw = ctx.comparisons.get(mw.name, ())
        if not cmps_raw:
            continue
        resolved: list[tuple[str, float]] = []
        unresolved = False
        for form, operand in cmps_raw:
            kv = _resolve_num(operand, state)
            if kv is None:
                unresolved = True
                break
            resolved.append((form, kv))
        v0 = state.tags.get(mw.name, 0)
        if unresolved or isinstance(v0, bool) or not isinstance(v0, int):
            best = 1 if best is None else min(best, 1)
            continue
        flip = _modwrap_first_flip(v0, mw.c, mw.m, resolved)
        if flip is not None:
            best = flip if best is None else min(best, flip)
    return best


# ── 11. State helpers ────────────────────────────────────────────────


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
    """Tag snapshot minus accumulators and other excluded names.

    Only accumulator drift is permitted on a skippable plateau; everything
    else must be unchanged.
    """
    return {k: v for k, v in state.tags.items() if k not in exclude}


# ── 12. Jump execution ──────────────────────────────────────────────


def _do_jump(
    runner: PLC,
    skip: int,
    ctx: _FoldContext,
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

    # Per-scan counter patches.
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

    # Mod-wrap patches (closed-form modular advance).
    for mw in ctx.modwrap:
        raw = runner.state.tags.get(mw.name, 0)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        # The step's own calc supplies the final increment (and re-mods),
        # so the landing value is bit-equal to stepping `skip` scans.
        patches[mw.name] = (raw + (skip - 1) * mw.c) % mw.m

    if patches:
        runner.patch(patches)

    # Timer advance via dt knob.
    runner._dt_override_for_next_scan = skip * ctx.normal_dt
    runner.step()

    # Align scan_id with the equivalent scans that passed.
    runner._state = runner._state.set(scan_id=runner._state.scan_id + skip - 1)


# ── 13. The fold loop ───────────────────────────────────────────────


def _advance_time(
    runner: PLC,
    governing: str,
    from_value: Any,
    ctx: _FoldContext,
    react_cap: int,
    sink: _DebugSink | None = None,
) -> int | None:
    """Hold inputs and advance time until *governing* leaves *from_value*.

    Each iteration's normal scan doubles as the plateau probe: if it changed
    only accumulators (and profile feedback tags), fold the next
    pure-accumulation run to one-before the nearest actionable crossing via
    ``_do_jump``.  *react_cap* bounds consecutive *churn* scans (a visible
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
    jumps = 0
    mod_idle = 0
    reacted_first = False
    exclude = (
        ctx.acc_names
        | ctx.profile_fb_names
        | ctx.churn_excluded
        | ctx.modwrap_names
        | ctx.mirror_names
    )

    while used < _EMPTY_CAP and iters < _MAX_ADVANCE_ITERS:
        iters += 1

        # ── Probe: one normal scan ───────────────────────────────
        before_tot = _acc_totals(runner.state, ctx.sources)
        before_vis = _visible_items(runner.state, exclude)
        runner.step()
        used += 1

        # ── Check: did the governing tag flip? ───────────────────
        if runner.state.tags.get(governing) != from_value:
            if sink is not None:
                nv = runner.state.tags.get(governing)
                sink.emit(
                    "fold-done",
                    tag=governing,
                    detail=f"from={from_value!r} to={nv!r}, used={used}, jumps={jumps}",
                )
            return used

        # ── Plateau test: did anything visible change? ───────────
        after_vis = _visible_items(runner.state, exclude)
        if after_vis != before_vis:
            # Visible change — program is doing real work, can't fold.
            react += 1
            mod_idle = 0
            if not reacted_first:
                reacted_first = True
                if sink is not None and (jumps > 0 or used > 2):
                    changed = sorted(k for k in after_vis if before_vis.get(k) != after_vis[k])[:10]
                    sink.emit(
                        "fold-react",
                        tag=governing,
                        detail=f"visible change at scan {runner.state.scan_id}: {changed}, react={react}/{react_cap}",
                    )
            if react > react_cap:
                if sink is not None and used > 1:
                    sink.emit(
                        "fold-bail",
                        tag=governing,
                        detail=f"react-cap ({react}>{react_cap}), used={used}",
                    )
                return None
            continue

        # ── Plateau confirmed: compute jump distance ─────────────
        after_tot = _acc_totals(runner.state, ctx.sources)
        acc_scans = _nearest_acc_crossing(ctx, before_tot, after_tot, runner.state)
        mod_scans = _nearest_mod_flip(ctx, runner.state)
        cands = [s for s in (acc_scans, mod_scans) if s is not None]
        skip = min(cands) - 1 if cands else None

        # Constrain fold distance by pending harness feedback patches.
        harness_scan = _harness_nearest_scan(runner)
        if harness_scan is not None:
            gap = harness_scan - runner.state.scan_id - 1
            if gap >= 0:
                skip = min(skip, gap) if skip is not None else gap

        if skip is None:
            # No crossing reachable.
            if runner._harness is not None and any(
                c.active for c in runner._harness._profile_couplings
            ):
                continue
            if sink is not None:
                sink.emit(
                    "fold-bail",
                    tag=governing,
                    detail=f"no-crossing, used={used}",
                )
            return None

        react = 0
        skip = min(skip, _EMPTY_CAP - used)

        # ── Jump ─────────────────────────────────────────────────
        if skip >= 1:
            _do_jump(runner, skip, ctx, before_tot, after_tot)
            used += skip
            jumps += 1

            if runner.state.tags.get(governing) != from_value:
                if sink is not None:
                    nv = runner.state.tags.get(governing)
                    sink.emit(
                        "fold-done",
                        tag=governing,
                        detail=f"from={from_value!r} to={nv!r}, used={used}, jumps={jumps}",
                    )
                return used

        # ── Mod-wrap limit-cycle futility ────────────────────────
        # When no accumulator has an upcoming actionable crossing, the
        # only motion left is the wrap cycle.  One full period with no
        # visible change is a limit cycle — waiting cannot help.
        if ctx.mod_period:
            if acc_scans is not None:
                mod_idle = 0
            else:
                mod_idle += 1 + max(skip, 0)
                if mod_idle > ctx.mod_period and _harness_nearest_scan(runner) is None:
                    if sink is not None:
                        sink.emit(
                            "fold-bail",
                            tag=governing,
                            detail=f"mod-limit-cycle, used={used}",
                        )
                    return None

    if sink is not None:
        reason = "iter-cap" if iters >= _MAX_ADVANCE_ITERS else "empty-cap"
        sink.emit("fold-bail", tag=governing, detail=f"{reason}, used={used}")
    return None
