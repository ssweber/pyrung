"""Time folding: collapse provably-identical scans into one step.

Between accumulator crossings every rung emits identical output.  The fold
detects these plateaus, computes the nearest crossing in closed form, and
folds the runner forward — dt knob for timers, acc-patch for per-scan
counters, modular arithmetic for wrapping self-calcs.  Soundness rests on
the plateau guard (only-accumulators-changed), not on the crossing set
being exhaustive.

Module structure
────────────────
1. Source types         — what the fold tracks (_AccSource, _ModWrap)
2. Fold context         — assembled static priors for the fold loop
3. Instruction registry — instruction type → fold-source mapping
4. Expression matching  — pattern detection on calc expressions
5. Comparison scanning  — rung reads relevant to crossings
6. Churn analysis       — identifying unobservable state changes
7. Self-calc sources    — calc-style counters promoted to fold sources
8. Mirror detection     — constant-offset views of tracked sources
9. Context assembly     — build the fold context from program structure
10. Crossing arithmetic — computing jump distances
11. State helpers       — accumulator totals, visible-items snapshot
12. Fold execution      — patching state forward in one step
13. Shared strategy     — one ordinary-fold proof window for every caller
14. Runner integration  — fold-aware run_until / run_for loops
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState

# ── Constants ───────────────────────────────────────────────────────

_EMPTY_CAP = 20_000
_MAX_ADVANCE_ITERS = 4_000
_EPS = 1e-9


# ── 1. Source types ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _AccSource:
    """An accumulating instruction whose held operation advances its scalar.

    Also used for linear self-calc tags promoted to fold sources
    (with a sentinel ``done_bit`` that nothing reads).
    """

    acc_name: str
    done_bit: str
    preset: Any  # int literal or tag-name str (dynamic preset)
    kind: str  # "up" (on/off-delay, count-up) | "down" (count-down)
    timed: bool  # True: time-based (dt knob).  False: per-scan (acc patch).
    bidir: bool = False  # CountUp with down_condition — delta sign varies at runtime
    unit: Any = None  # TimeUnit for timed sources; None for counters / synthetic sources.


@dataclass(frozen=True)
class _StepPreset:
    """A staged instruction's preset selected by its current step."""

    step_name: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class _ModWrap:
    """An unconditional affine-mod self-calc: ``tag := (tag + c) % m``.

    A wrapping per-scan counter can't ride the monotone progress
    coordinates (its measured delta flips sign at the wrap), so it gets
    its own crossing arithmetic: the first truth-flip of any read
    comparison along the modular recurrence bounds the fold, and
    ``_do_fold`` patches the value forward in closed form so landings
    stay bit-equal to step-by-step execution.
    """

    name: str
    c: int
    m: int


# ── 2. Fold context ─────────────────────────────────────────────────


@dataclass(frozen=True)
class _FoldContext:
    """Static priors for the time-fold loop, built once per PLC."""

    sources: tuple[_AccSource, ...]
    acc_names: frozenset[str]
    comparisons: dict[str, tuple[tuple[str, Any], ...]]
    read_done: frozenset[str]
    normal_dt: float
    profile_fb_names: frozenset[str] = frozenset()
    churn_excluded: frozenset[str] = frozenset()
    modwrap: tuple[_ModWrap, ...] = ()
    modwrap_names: frozenset[str] = frozenset()
    mod_period: int = 0
    mirror_names: frozenset[str] = frozenset()
    clock_half_periods: tuple[float, ...] = ()
    scan_derived_names: frozenset[str] = frozenset()
    soft_clocks: tuple[tuple[str, float], ...] = ()
    # Clocks held hard *only* because a gated rung reads an accumulator through a
    # comparison.  Each entry pairs the clock with its half-period and the
    # accumulators whose comparisons must all be saturated before the clock may
    # be promoted soft at runtime (see _runtime_soft_clocks).
    sat_clocks: tuple[tuple[str, float, frozenset[str]], ...] = ()
    frozen_writes: frozenset[str] = frozenset()


# ── 3. Instruction registry ─────────────────────────────────────────

_SOURCE_REGISTRY: dict[type, tuple[str, bool]] | None = None


def _ensure_registry() -> dict[type, tuple[str, bool]]:
    """Build the instruction→(kind, timed) dispatch table on first use."""
    global _SOURCE_REGISTRY
    if _SOURCE_REGISTRY is not None:
        return _SOURCE_REGISTRY

    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    _SOURCE_REGISTRY = {
        OnDelayInstruction: ("up", True),
        OffDelayInstruction: ("up", True),
        CountUpInstruction: ("up", False),
        CountDownInstruction: ("down", False),
        TimeDrumInstruction: ("up", True),
    }
    return _SOURCE_REGISTRY


def _collect_acc_sources(program: Any) -> list[_AccSource]:
    """Introspect every registered accumulating instruction, including calls."""
    from pyrung.core.instruction.counters import CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    registry = _ensure_registry()
    out: dict[str, _AccSource] = {}

    for instr in walk_instructions(program):
        params = registry.get(type(instr))
        if params is None:
            continue
        kind, timed = params
        bidir = isinstance(instr, CountUpInstruction) and instr.down_condition is not None
        if isinstance(instr, TimeDrumInstruction):
            preset = _StepPreset(instr.current_step.name, instr.presets)
            done_bit = instr.completion_flag.name
        else:
            preset = instr.preset
            done_bit = instr.done_bit.name
        out[instr.accumulator.name] = _AccSource(
            acc_name=instr.accumulator.name,
            done_bit=done_bit,
            preset=preset.name if isinstance(preset, Tag) else preset,
            kind=kind,
            timed=timed,
            bidir=bidir,
            unit=getattr(instr, "unit", None) if timed else None,
        )
    return list(out.values())


# ── 4. Expression matching ──────────────────────────────────────────


def _calc_self_referential(tag: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when *tag* is the dest of a ``calc`` that reads *tag* itself."""
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
    """A tag advanced by a single unconditional top-level self-calc."""
    writers = pdg.writers_of.get(tag, frozenset())
    if len(writers) != 1:
        return False
    (ri,) = writers
    node = pdg.rung_nodes[ri]
    if node.scope != "main" or node.branch_path or node.condition_reads:
        return False
    return _calc_self_referential(tag, pdg, program)


def _match_affine_selfcalc(expr: Any, tag: str) -> tuple[int, int | None] | None:
    """Match ``(tag ± c) % m`` / ``tag ± c``; return ``(c, m)`` or ``None``."""
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


def _match_affine_of(expr: Any) -> tuple[str, int, int] | None:
    """Match ``scale * tag + offset`` for ``scale`` in ``{-1, +1}``.

    The supported surface is deliberately small and exact: a bare tag,
    ``tag ± literal``, ``literal + tag``, or ``literal - tag``.
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

    direct = name(expr)
    if direct is not None:
        return (direct, 1, 0)
    if not isinstance(expr, BinaryExpr):
        return None
    if expr.symbol == "+":
        n, k = name(expr.left), lit(expr.right)
        if n is None or k is None:
            n, k = name(expr.right), lit(expr.left)
        if n is not None and k is not None:
            return (n, 1, k)
    elif expr.symbol == "-":
        n, k = name(expr.left), lit(expr.right)
        if n is not None and k is not None:
            return (n, 1, -k)
        n, k = name(expr.right), lit(expr.left)
        if n is not None and k is not None:
            return (n, -1, k)
    return None


# ── 5. Comparison scanning ──────────────────────────────────────────


def _scan_rung_reads(
    pdg: ProgramGraph,
    program: Any,
    watch_names: frozenset[str],
) -> tuple[dict[str, tuple[tuple[str, Any], ...]], frozenset[str]]:
    """Collect comparison atoms for *watch_names* tags and all read tag names."""
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
    """All tags whose values can depend on *root*, transitively."""
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
    """Self-updating tags nothing else reads — unobservable per-scan churn."""
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


def _scan_local_terminal_tags(
    plc: PLC,
    pdg: ProgramGraph,
    target_names: frozenset[str],
) -> frozenset[str]:
    """Unread outputs whose entry value is killed on every scan.

    A merely unread tag is not automatically disposable: a conditional write
    can latch a value produced only inside a skipped interval.  This narrower
    class has no program readers and a proven unconditional first definition,
    so every real landing scan reconstructs its exact value from the current
    inputs.  Harness- and predicate-visible tags remain protected.
    """
    harness_names = _harness_referenced_names(plc)
    return frozenset(
        tag
        for tag in pdg.writers_of
        if tag not in harness_names
        and tag not in target_names
        and not pdg.all_readers_of.get(tag, frozenset())
        and pdg.unconditional_write_before_read(tag)
    )


def _disjoint_churn_closures(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
    skip_roots: frozenset[str],
) -> frozenset[str]:
    """Read churn whose downstream cone never reaches the targets."""
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
            continue
        if not _calc_self_referential(tag, pdg, program):
            continue
        closure = _downstream_closure(pdg, tag)
        if closure & cone or closure & harness_names:
            continue
        excluded |= closure
    return frozenset(excluded - target_names)


def _frozen_rung_writes(
    pdg: ProgramGraph,
    base_varying: frozenset[str],
    resolved_on_read: frozenset[str],
    target_names: frozenset[str],
) -> frozenset[str]:
    """Tags written exclusively by rungs whose inputs cannot vary in a plateau.

    Fixed-point: seed *varying* with *base_varying* (accumulators, churn, mirrors,
    etc.), then propagate through rung read→write edges until stable.  Tags written
    only by rungs whose reads sit entirely outside the reachable varying set are
    frozen — their values cannot change between accumulator crossings, so excluding
    them from the plateau visibility check lets the fold skip through clock-gated
    recomputations that would otherwise break the plateau with identical writes.

    *resolved_on_read* names (system clocks, always_on, scan-derived signals) are
    stripped from rung reads because they are never stored in ``state.tags`` and
    thus invisible to the plateau guard's dictionary comparison.
    """
    varying: set[str] = set(base_varying)
    changed = True
    while changed:
        changed = False
        for node in pdg.rung_nodes:
            reads = _node_reads(node) - resolved_on_read
            if not reads & varying:
                continue
            for w in node.writes:
                if w not in varying:
                    varying.add(w)
                    changed = True

    frozen: set[str] = set()
    for node in pdg.rung_nodes:
        reads = _node_reads(node) - resolved_on_read
        if not reads & varying:
            frozen |= node.writes

    return frozenset(frozen - varying - target_names)


# ── 7. Self-calc sources ────────────────────────────────────────────


def _selfcalc_sources(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
) -> list[tuple[str, int, int | None]]:
    """Affine(-mod) self-calc churners eligible as exact fold sources."""
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction

    harness_names = _harness_referenced_names(plc)
    out: list[tuple[str, int, int | None]] = []
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names or tag in target_names:
            continue
        if pdg.all_readers_of.get(tag, frozenset()) <= writers:
            continue
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


def _is_clock_view(tag: str, pdg: ProgramGraph, program: Any, source_names: frozenset[str]) -> bool:
    """A copy / constant-offset calc view of a fold source or free-running
    self-calc."""
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
) -> list[tuple[str, str, int, int]]:
    """Structurally eligible affine acc views: ``(view, source, scale, offset)``."""
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag

    harness_names = _harness_referenced_names(plc)
    out: list[tuple[str, str, int, int]] = []
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
        matched: tuple[str, int, int] | None = None
        n_writers = 0
        for instr in ro._instructions:
            if isinstance(instr, CopyInstruction) and getattr(instr.dest, "name", None) == tag:
                n_writers += 1
                if isinstance(instr.source, Tag) and instr.convert is None and not instr.oneshot:
                    matched = (instr.source.name, 1, 0)
            elif isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == tag:
                n_writers += 1
                m = _match_affine_of(instr.expression)
                if m is not None:
                    matched = m
        if n_writers != 1 or matched is None or matched[0] not in source_names:
            continue
        out.append((tag, matched[0], matched[1], matched[2]))
    return out


def _mirror_reads_are_simple(tag: str, pdg: ProgramGraph, program: Any, writer_ri: int) -> bool:
    """Every program read of *tag* is a simple literal comparison on *tag*."""
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
            return False
    return True


# ── 9. Context assembly ─────────────────────────────────────────────


def _partition_read_clocks(
    pdg: ProgramGraph,
    disqualifying: frozenset[str],
    acc_names: frozenset[str],
) -> tuple[
    tuple[float, ...], tuple[tuple[str, float], ...], tuple[tuple[str, float, frozenset[str]], ...]
]:
    """Split the system clocks the program reads into hard, soft, and rescuable.

    A clock is *soft* (inert-eligible) when every rung reading it uses it only
    as a gate and reads nothing else that varies within a plateau window — so
    the gated logic recomputes an identical result at every edge.  A clock read
    as data, or read alongside a window-varying tag (excluded churn, a mirror,
    or another resolved-on-read signal), is *hard*: bound on every edge.

    Between those two sits a *rescuable* clock: hard **only** because a gated
    rung reads an accumulator through a comparison (never as data, never beside a
    non-accumulator disqualifier).  Such a clock is value-blind-hard today, but
    the moment those comparisons saturate the gated logic recomputes identically
    — so it is returned separately, tagged with the accumulators that must
    saturate, for the loop to promote soft at runtime (see _runtime_soft_clocks).

    Returns ``(hard_half_periods, soft_clocks, sat_clocks)``.  Misclassifying a
    clock soft costs nothing for soundness — the runtime plateau guard only ever
    marks an edge inert after *observing* it leave the visible state unchanged.
    """
    from pyrung.core.system_points import _CLOCK_HALF_PERIODS

    clock_names = frozenset(_CLOCK_HALF_PERIODS)
    read_clocks: set[str] = set()
    unconditional: set[str] = set()
    req_accs: dict[str, set[str]] = {}
    for node in pdg.rung_nodes:
        reads = node.condition_reads | node.data_reads | node.exclusive_reads
        node_clocks = reads & clock_names
        if not node_clocks:
            continue
        read_clocks |= node_clocks
        body_reads = node.data_reads | node.exclusive_reads
        for c in node_clocks:
            if c in body_reads:
                unconditional.add(c)  # clock read as data — never rescuable
                continue
            dq = (reads - {c}) & disqualifying
            if not dq:
                continue  # soft contribution from this node
            # Rescuable only if every disqualifier here is an accumulator read
            # purely through a comparison.  A non-accumulator disqualifier, or an
            # accumulator read as data, makes this node's recompute genuinely
            # live — the clock is unconditionally hard.
            if (dq - acc_names) or (dq & acc_names & body_reads):
                unconditional.add(c)
                continue
            req_accs.setdefault(c, set()).update(dq & acc_names)

    hard = unconditional
    sat = {c: accs for c, accs in req_accs.items() if c not in hard and accs}
    soft = read_clocks - hard - set(sat)
    hard_half_periods = tuple(sorted({_CLOCK_HALF_PERIODS[c] for c in hard}))
    soft_clocks = tuple(sorted((c, _CLOCK_HALF_PERIODS[c]) for c in soft))
    sat_clocks = tuple(
        sorted((c, _CLOCK_HALF_PERIODS[c], frozenset(accs)) for c, accs in sat.items())
    )
    return hard_half_periods, soft_clocks, sat_clocks


def _build_fold_context(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    *,
    target_names: frozenset[str] = frozenset(),
    condition_clock_reads: frozenset[str] = frozenset(),
    condition_scan_derived: frozenset[str] = frozenset(),
    advice: Any = None,
    journal: Any = None,
) -> _FoldContext:
    """Build the static fold priors.

    *target_names* are the goal tags — never excluded from the plateau
    guard.  *condition_clock_reads* are system clocks a ``run_until``
    condition reads that the program's rungs may not: resolved on read and
    never stored, they are invisible to the plateau guard, so each is
    bounded as an unconditionally *hard* clock edge (never soft/inert).
    *condition_scan_derived* likewise disables folding entirely — a
    scan-id-derived signal read by the condition changes every scan.
    *advice* gates the fold-kind passes (``None`` = all enabled);
    *journal* records applied exclusions.
    """
    sources = _collect_acc_sources(program)
    acc_names = frozenset(s.acc_name for s in sources)

    h = plc._harness
    profile_fb_names: frozenset[str] = (
        frozenset(c.fb_name for c in h._profile_couplings) if h is not None else frozenset()
    )

    churn_excluded: frozenset[str] = frozenset()

    unread = _unread_churn_tags(plc, pdg, program) - target_names
    if advice is None or advice.has("fold_unread_churn"):
        churn_excluded |= unread
        if unread and journal is not None:
            journal.add_note(
                "fold: unread churn excluded from plateau guard: " + ", ".join(sorted(unread))
            )
        terminals = _scan_local_terminal_tags(plc, pdg, target_names) - acc_names
        churn_excluded |= terminals
        if terminals and journal is not None:
            journal.add_note(
                "fold: scan-local terminal outputs excluded from plateau guard: "
                + ", ".join(sorted(terminals))
            )

    if advice is None or advice.has("fold_disjoint_churn"):
        disjoint = _disjoint_churn_closures(plc, pdg, program, target_names, skip_roots=unread)
        churn_excluded |= disjoint
        if disjoint and journal is not None:
            journal.add_note(
                "fold: target-disjoint churn cone excluded from plateau guard: "
                + ", ".join(sorted(disjoint))
            )

    modwrap: list[_ModWrap] = []
    if advice is None or advice.has("fold_modwrap_source"):
        tracked: list[str] = []
        for tag, c, m in _selfcalc_sources(plc, pdg, program, target_names):
            if tag in acc_names or tag in churn_excluded:
                continue
            if m is None:
                sources.append(
                    _AccSource(
                        acc_name=tag,
                        done_bit=f"__selfcalc:{tag}",
                        preset=0,
                        kind="up" if c > 0 else "down",
                        timed=False,
                    )
                )
                acc_names |= {tag}
            else:
                modwrap.append(_ModWrap(tag, c, m))
            tracked.append(tag)
        if tracked and journal is not None:
            journal.add_note(
                "fold: self-calc churn tracked as fold source(s): " + ", ".join(sorted(tracked))
            )

    modwrap_names = frozenset(mw.name for mw in modwrap)

    mod_period = 0
    if modwrap:
        mod_period = 1
        for mw in modwrap:
            mod_period = math.lcm(mod_period, mw.m // math.gcd(abs(mw.c), mw.m))
            if mod_period > 4096:
                mod_period = 4096
                break

    mirror_cands: list[tuple[str, str, int, int]] = []
    if advice is None or advice.has("fold_derived_crossings"):
        mirror_cands = _mirror_candidates(
            plc, pdg, program, target_names, acc_names | modwrap_names
        )

    watch = acc_names | profile_fb_names | modwrap_names
    comparisons, read_tags = _scan_rung_reads(
        pdg, program, watch | frozenset(m for m, _a, _scale, _offset in mirror_cands)
    )

    # sys.scan_clock_toggle (scan_id % 2) and sys.scan_counter (scan_id %
    # 32768) are scan-id-derived — they change *every* scan and, like the
    # clocks, are resolved on read and never stored.  No periodic edge to land
    # on: if the program reads either, the fold cannot skip a single scan
    # without risking a dropped edge, so it degrades to scan-by-scan.
    from pyrung.core.system_points import _SCAN_DERIVED_NAMES

    scan_derived_names = frozenset(read_tags & _SCAN_DERIVED_NAMES) | condition_scan_derived
    if scan_derived_names and journal is not None:
        journal.add_note(
            "fold: disabled by read scan-id-derived signal(s): "
            + ", ".join(sorted(scan_derived_names))
        )

    mirror_names: set[str] = set()
    if mirror_cands:
        merged = dict(comparisons)
        reverse_form = {
            "lt": "gt",
            "le": "ge",
            "gt": "lt",
            "ge": "le",
            "eq": "eq",
            "ne": "ne",
        }
        for m, a, scale, offset in mirror_cands:
            cmps = merged.get(m, ())
            (writer_ri,) = pdg.writers_of[m]
            if any(
                isinstance(t, bool) or not isinstance(t, (int, float)) for _f, t in cmps
            ) or not _mirror_reads_are_simple(m, pdg, program, writer_ri):
                merged.pop(m, None)
                continue
            projected = (
                tuple((f, t - offset) for f, t in cmps)
                if scale == 1
                else tuple((reverse_form[f], offset - t) for f, t in cmps)
            )
            merged[a] = merged.get(a, ()) + projected
            merged.pop(m, None)
            mirror_names.add(m)
        comparisons = merged
        if mirror_names and journal is not None:
            journal.add_note(
                "fold: affine acc-view thresholds translated onto their sources: "
                + ", ".join(sorted(mirror_names))
            )

    # System clocks (sys.clock_1s, …) are pure functions of the timestamp,
    # resolved on read and never stored in state.tags — so the plateau guard
    # and the crossing arithmetic are both blind to them.  A rung that reads one
    # (level or via rise()/fall()) flips on the clock's edge; folding past that
    # edge would silently drop the firing.  Bound the fold to each read clock's
    # edges — but split them: a clock read *only* as a gate by rungs whose
    # bodies read nothing that varies within a plateau window recomputes the
    # same result at every edge, so once the runtime plateau confirms one edge
    # inert the rest can be skipped (see the inert_soft handling in the loops).
    # Anything that varies within a window — an accumulator, excluded churn, a
    # mirror, another resolved-on-read signal — keeps its clock hard.
    from pyrung.core.system_points import _DERIVED_TAG_NAMES
    from pyrung.core.system_points import system as _system

    clock_disqualifying = (
        acc_names
        | profile_fb_names
        | churn_excluded
        | modwrap_names
        | frozenset(mirror_names)
        | (frozenset(_DERIVED_TAG_NAMES) - {_system.sys.always_on.name})
    )
    clock_half_periods, soft_clocks, sat_clocks = _partition_read_clocks(
        pdg, clock_disqualifying, acc_names
    )
    if condition_clock_reads:
        # A clock read by the run_until condition has no rung body to prove it
        # inert — every edge is a potential landing, so it is unconditionally
        # hard, demoted out of soft/rescuable if the program also reads it.
        from pyrung.core.system_points import _CLOCK_HALF_PERIODS

        cond_clocks = condition_clock_reads & frozenset(_CLOCK_HALF_PERIODS)
        if cond_clocks:
            clock_half_periods = tuple(
                sorted(set(clock_half_periods) | {_CLOCK_HALF_PERIODS[c] for c in cond_clocks})
            )
            soft_clocks = tuple((n, hp) for n, hp in soft_clocks if n not in cond_clocks)
            sat_clocks = tuple((n, hp, accs) for n, hp, accs in sat_clocks if n not in cond_clocks)
            if journal is not None:
                journal.add_note(
                    "fold: bounded by run_until-condition clock edge(s): "
                    + ", ".join(sorted(cond_clocks))
                )
    if journal is not None:
        if clock_half_periods:
            journal.add_note(
                "fold: bounded by hard system-clock edges (half-periods s): "
                + ", ".join(str(hp) for hp in clock_half_periods)
            )
        if soft_clocks:
            journal.add_note(
                "fold: soft (inert-eligible) clocks tracked per window: "
                + ", ".join(name for name, _hp in soft_clocks)
            )
        if sat_clocks:
            journal.add_note(
                "fold: saturation-rescuable clocks (promoted soft once their "
                "accumulator comparisons settle): "
                + ", ".join(name for name, _hp, _accs in sat_clocks)
            )

    frozen_writes: frozenset[str] = frozenset()
    if advice is None or advice.has("fold_frozen_writes"):
        base_varying = (
            acc_names | modwrap_names | frozenset(mirror_names) | profile_fb_names | churn_excluded
        )
        frozen_writes = _frozen_rung_writes(
            pdg, base_varying, frozenset(_DERIVED_TAG_NAMES), target_names
        )
        if frozen_writes and journal is not None:
            journal.add_note(
                "fold: frozen-rung writes excluded from plateau guard: "
                + ", ".join(sorted(frozen_writes))
            )

    # The synthesis overlay's plant rungs are real on/off-delay timers (bool
    # feedback); register their accumulators as fold sources so a coupling's
    # dwell folds exactly like any program timer (preset-bounded, dt-knob
    # advanced, excluded from the plateau guard) rather than stepping scan-by-
    # scan.  Walked from the overlay, not the user program, keeping the brackets
    # off the deploy/prove roots.
    syn = getattr(plc, "_synthesis", None)
    if syn is not None and not syn.is_empty():
        from pyrung.core.program import Program

        syn_program = Program.__new__(Program)
        syn_program.rungs = list(syn.all_rungs())
        syn_program.subroutines = {}
        for src in _collect_acc_sources(syn_program):
            if src.acc_name in acc_names:
                continue
            sources.append(src)
            acc_names = acc_names | {src.acc_name}

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
        clock_half_periods=clock_half_periods,
        scan_derived_names=scan_derived_names,
        soft_clocks=soft_clocks,
        sat_clocks=sat_clocks,
        frozen_writes=frozen_writes,
    )


# ── 10. Crossing arithmetic ─────────────────────────────────────────


def _extract_condition_crossings(condition: Any) -> dict[str, tuple[tuple[str, Any], ...]]:
    """Pull tag comparisons out of a ``run_until`` predicate condition tree.

    Reuses ``simplified._condition_to_expr`` (the same ``Atom`` form vocabulary
    ``_scan_rung_reads`` walks) and returns ``{tag_name: ((form, operand), …)}``
    for every comparison ``Atom``.  The fold can then target a threshold the
    predicate reads on an *excluded* tag (an accumulator / mod-wrap): such a tag
    never breaks the plateau, so without this the fold skips straight to the
    preset and overshoots.  Landing on a candidate flip and re-checking the
    predicate is exact regardless of And/Or/negation structure — the predicate's
    truth can only change where a comparison flips.  Forms we don't model here
    (``ArithAtom`` compound thresholds) just fall back to prior behavior.
    """
    from pyrung.core.analysis.simplified import And, Atom, Or, _condition_to_expr

    cmp_forms = {"eq", "ne", "lt", "le", "gt", "ge"}
    out: dict[str, list[tuple[str, Any]]] = {}

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            if e.form in cmp_forms:
                out.setdefault(e.tag, []).append((e.form, e.operand))
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    visit(_condition_to_expr(condition))
    return {k: tuple(v) for k, v in out.items()}


def _extract_condition_reads(condition: Any) -> frozenset[str]:
    """Every tag name a ``run_until`` condition reads, regardless of form.

    Where :func:`_extract_condition_crossings` collects only comparison
    thresholds (for the crossing arithmetic), this collects *all* reads —
    bare contacts, ``rise``/``fall`` edges, comparison left sides, tag
    operands on comparison right sides, and both legs of ``ArithAtom``
    compounds.  The caller threads the set into the fold context as
    protected reads (``target_names``), so a condition tag classified as
    churn-excluded or frozen-write cannot be folded past.  A junk name
    (a Char *value* operand that happens to be a string) is harmless —
    protection only ever narrows folding, never unsounds it.
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Or, _condition_to_expr
    from pyrung.core.tag import Tag

    out: set[str] = set()

    def add_operand(operand: Any) -> None:
        if isinstance(operand, Tag):
            out.add(operand.name)
        elif isinstance(operand, str):
            out.add(operand)

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            out.add(e.tag)
            add_operand(e.operand)
        elif isinstance(e, ArithAtom):
            out.add(e.left)
            out.add(e.right)
            add_operand(e.operand)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    visit(_condition_to_expr(condition))
    return frozenset(out)


def _resolve_num(value: Any, state: Any) -> float | None:
    """Resolve a threshold operand (literal, tag name, or Tag) to a number."""
    from pyrung.core.tag import Tag

    if isinstance(value, _StepPreset):
        step = state.tags.get(value.step_name)
        if (
            isinstance(step, bool)
            or not isinstance(step, int)
            or not 1 <= step <= len(value.values)
        ):
            return None
        return _resolve_num(value.values[step - 1], state)
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
    in monotone-up progress coordinates."""
    if kind == "down":
        k = -k
        form = {"lt": "gt", "gt": "lt", "le": "ge", "ge": "le"}.get(form, form)
    return k, form in ("gt", "le")


def _scans_to_cross(pa: float, delta: float, target: float, strict: bool) -> int | None:
    """Scans for progress (at ``pa``, +``delta``/scan) to cross ``target``."""
    if strict:
        if pa > target:
            return None
        return math.floor((target - pa) / delta) + 1
    if pa >= target:
        return None
    return max(1, math.ceil((target - pa) / delta))


def _comparisons_saturated(progress: float, kind: str, cmps: Sequence[tuple[str, float]]) -> bool:
    """True iff every comparison in *cmps* has made its final transition already.

    A monotone source at *progress* (the ``_acc_totals`` value — count-down
    accumulators negated, so the coordinate always increases) contributes a
    *frozen* boolean to a comparison once that comparison can no longer flip for
    the rest of the window.  When this holds for every comparison a rung reads on
    the source, the source's raw value keeps changing but its logical effect on
    that rung is constant — so a clock gating the rung need not stay bound to it.

    ``eq``/``ne`` are treated conservatively as *never* saturated: ``_progress_bound``
    models only their rising crossing, not the fall one scan later, so a value
    sitting at or past the threshold could still re-flip under this arithmetic.
    Excluding them keeps the predicate sound at the cost of a missed fold.

    Empty *cmps* is vacuously saturated (the caller only passes comparison reads;
    a raw data read of the source is disqualifying upstream, never reaches here).
    """
    for form, k in cmps:
        if form in ("eq", "ne"):
            return False
        target, strict = _progress_bound(kind, form, k)
        if _scans_to_cross(progress, 1.0, target, strict) is not None:
            return False
    return True


def _scans_to_uncross(pa: float, delta: float, target: float, strict: bool) -> int | None:
    """Scans for progress to drop past ``target`` (bidir counter opposite direction)."""
    if strict:
        if pa <= target:
            return None
        return max(1, math.ceil((target - pa) / delta))
    if pa < target:
        return None
    return max(1, math.floor((target - pa) / delta) + 1)


def _modwrap_first_flip(v0: int, c: int, m: int, cmps: list[tuple[str, float]]) -> int | None:
    """Scans until any comparison's truth first differs from its truth now."""

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
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
) -> int | None:
    """Scans to the nearest actionable accumulator crossing.

    *extra_comparisons* carries thresholds read by the ``run_until`` predicate
    (e.g. ``Tmr_Acc > 500``); merging them in makes the fold land on the
    predicate's threshold instead of skipping to the preset and overshooting.
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
        bounds: list[tuple[float, bool]] = []
        preset = _resolve_num(src.preset, state)
        if preset is not None:
            # Crossing the preset flips the Done bit, which is a written, visible
            # tag (never excluded from the plateau guard) — so the preset is a
            # crossing whether or not any rung reads Done.  Targeting it lets an
            # unread, never-completing timer/counter fold its ramp instead of
            # stepping scan-by-scan to the time/cycle bound.
            bounds.append((preset, False))
        elif src.done_bit in ctx.read_done:
            # Read Done with an unresolvable dynamic preset: can't place the
            # crossing — step one scan to stay exact.  (Unread + unresolvable
            # leaves this source unbounded; other sources may still fold.)
            best = 1 if best is None else min(best, 1)
            continue
        cmps = ctx.comparisons.get(src.acc_name, ())
        if extra_comparisons:
            cmps = cmps + extra_comparisons.get(src.acc_name, ())
        unresolved = False
        for form, operand in cmps:
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


def _nearest_mod_flip(
    ctx: _FoldContext,
    state: Any,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
) -> int | None:
    """Scans to the nearest comparison truth-flip among mod-wrap sources."""
    best: int | None = None
    for mw in ctx.modwrap:
        cmps_raw = ctx.comparisons.get(mw.name, ())
        if extra_comparisons:
            cmps_raw = cmps_raw + extra_comparisons.get(mw.name, ())
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
    """Tag snapshot minus accumulators and other excluded names."""
    return {k: v for k, v in state.tags.items() if k not in exclude}


def _visible_items_match(
    state: Any,
    expected: dict[str, Any],
    exclude: frozenset[str],
) -> bool:
    """Whether *state* has exactly the visible items in *expected*.

    Fold probing needs one retained snapshot, then compares later states to it.
    Comparing in place avoids rebuilding a second (and, after a macro fold,
    third) large dictionary on every probe while still detecting added or
    removed visible tags.
    """
    visible_count = 0
    for name, value in state.tags.items():
        if name in exclude:
            continue
        visible_count += 1
        if name not in expected or expected[name] != value:
            return False
    return visible_count == len(expected)


# ── 12. Fold execution ──────────────────────────────────────────────


def _harness_nearest_scan(plc: PLC) -> int | None:
    """Nearest scan a harness feedback is scheduled to flip — ``None`` if none.

    The bool-coupling transport-delay heap this once peeked is gone: bool
    feedback is now a real on/off-delay timer registered as an ordinary fold
    source (see :func:`_build_fold_context`), so the standard accumulator
    crossing arithmetic bounds its dwell and the dt knob advances it — no
    separate schedule to look ahead to.  Retained as a no-op hook (and for
    ``cyclefold``'s jump bound).
    """
    return None


def _acc_comparisons_saturated_now(
    ctx: _FoldContext,
    state: Any,
    totals: dict[str, float],
    kinds: dict[str, str],
    acc: str,
) -> bool:
    """Resolve *acc*'s comparison thresholds against *state* and test saturation.

    Returns ``False`` when the accumulator has no recorded comparison (no
    evidence to promote on) or any threshold is currently unresolvable.
    """
    progress = totals.get(acc)
    if progress is None:
        return False
    cmps_raw = ctx.comparisons.get(acc, ())
    if not cmps_raw:
        return False
    resolved: list[tuple[str, float]] = []
    for form, operand in cmps_raw:
        kv = _resolve_num(operand, state)
        if kv is None:
            return False
        resolved.append((form, kv))
    return _comparisons_saturated(progress, kinds.get(acc, "up"), resolved)


def _runtime_soft_clocks(ctx: _FoldContext, state: Any) -> frozenset[str]:
    """Saturation-rescuable clocks (``ctx.sat_clocks``) safe to treat soft now.

    A rescuable clock is promoted only while *every* accumulator it requires has
    settled past all comparisons read on it — the same value-aware judgment the
    static partition is blind to.  The promotion is still backstopped by the
    loop's observe-before-skip guard, so a wrong promotion only costs a probe
    scan, never a dropped edge.
    """
    if not ctx.sat_clocks:
        return frozenset()
    totals = _acc_totals(state, ctx.sources)
    kinds = {s.acc_name: s.kind for s in ctx.sources}
    promoted: set[str] = set()
    for name, _hp, accs in ctx.sat_clocks:
        if all(_acc_comparisons_saturated_now(ctx, state, totals, kinds, acc) for acc in accs):
            promoted.add(name)
    return frozenset(promoted)


def _window_soft_clocks(
    ctx: _FoldContext, promoted: frozenset[str]
) -> tuple[tuple[str, float], ...]:
    """Soft (inert-eligible) clocks for this window: the always-soft set plus any
    saturation-rescuable clock currently promoted by ``_runtime_soft_clocks``."""
    if not ctx.sat_clocks:
        return ctx.soft_clocks
    return ctx.soft_clocks + tuple(
        (name, hp) for name, hp, _accs in ctx.sat_clocks if name in promoted
    )


def _scans_to_clock_edge(
    ctx: _FoldContext,
    state: Any,
    inert_soft: frozenset[str] = frozenset(),
    promoted: frozenset[str] = frozenset(),
) -> int | None:
    """Largest fold skip that won't cross a read system-clock's next edge.

    A clock toggles when ``int(timestamp / half_period)`` increments.  The
    next boundary after the current timestamp ``t`` is ``(phase + 1) *
    half_period``; flooring ``(t_edge - t) / dt`` gives the scans the fold may
    advance while staying inside the current half-period, so the edge scan
    runs normally on the following step (the same role the harness gap plays
    for scheduled feedback).  ``0`` means the edge is within one scan — don't
    fold.  ``None`` means nothing is read that needs bounding.

    Hard clocks always bound the fold.  Soft (inert-eligible) clocks bound it
    only until the loop confirms an edge inert and adds them to *inert_soft*;
    thereafter their edges are skipped for the rest of the plateau window.  A
    saturation-rescuable clock (``ctx.sat_clocks``) is hard while *not* in
    *promoted* — its accumulator comparison can still flip — and joins the soft
    pool once *promoted*, so it too needs one observed-inert edge before skipping.

    Scan-id-derived signals (``scan_clock_toggle``/``scan_counter``) change
    every scan, so a read of either forces ``0``: the fold may not skip.
    """
    if ctx.scan_derived_names:
        return 0
    half_periods = list(ctx.clock_half_periods)
    # Rescuable clocks that have *not* saturated yet are hard this window.
    half_periods.extend(hp for name, hp, _accs in ctx.sat_clocks if name not in promoted)
    half_periods.extend(
        hp for name, hp in _window_soft_clocks(ctx, promoted) if name not in inert_soft
    )
    if not half_periods or ctx.normal_dt <= 0:
        return None
    from pyrung.core.system_points import clock_phase

    t = state.timestamp
    best: int | None = None
    for hp in half_periods:
        phase = clock_phase(t, hp)
        # Largest whole scans that land *strictly before* the next edge, so the
        # edge itself runs as a single-dt probe (where a per-edge change breaks
        # the plateau and is never wrongly marked inert).  Using floor here lands
        # *on* the edge when the gap is an exact integer of scans, letting one
        # big fold step span the edge — which misses pulse outputs and leaves a
        # stale ``_prev`` for rise()/fall().  ``ceil(raw - eps) - 1`` is the
        # largest integer strictly below ``raw`` and is robust to float noise.
        raw = ((phase + 1) * hp - t) / ctx.normal_dt
        gap = math.ceil(raw - _EPS) - 1
        if gap < 0:
            gap = 0
        best = gap if best is None else min(best, gap)
    return best


def _mark_inert_soft(
    ctx: _FoldContext,
    inert_soft: set[str],
    inert_run: dict[str, int],
    before_ts: float,
    after_ts: float,
    promoted: frozenset[str] = frozenset(),
) -> None:
    """Count inert toggles per soft clock; confirm one inert after a full period.

    The caller invokes this only after the visible state was observed unchanged
    across the span, so every clock *toggle* the span crossed produced no visible
    change.  But a clock toggles every half-period — alternating rise, fall, rise
    … — and an edge-sensitive rung (``rise(clock)`` / ``fall(clock)``) responds to
    only *one* polarity: a ``rise()``-gated pulse leaves the state unchanged at
    every *fall*, so a single inert toggle would wrongly mark the whole clock
    skippable and drop the next rise.

    So require a **full period** — two consecutive inert toggles, covering both a
    rise and a fall — before adding the clock to *inert_soft*.  ``inert_run``
    accumulates consecutive inert toggles; the loop clears it (with *inert_soft*)
    whenever the plateau breaks, so a polarity that *does* change the state resets
    the run and the clock keeps bounding every edge.  Promoted saturation-
    rescuable clocks are inert-eligible on the same terms.
    """
    from pyrung.core.system_points import clock_phase

    for name, hp in _window_soft_clocks(ctx, promoted):
        if name in inert_soft:
            continue
        toggles = clock_phase(after_ts, hp) - clock_phase(before_ts, hp)
        if toggles <= 0:
            continue
        inert_run[name] = inert_run.get(name, 0) + toggles
        if inert_run[name] >= 2:
            inert_soft.add(name)


def _fold_patches(
    state: SystemState,
    skip: int,
    ctx: _FoldContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
) -> dict[str, int]:
    """Return the non-time projections needed before a macro landing scan."""
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
        raw_acc = int(state.tags.get(src.acc_name, 0) or 0)
        patches[src.acc_name] = raw_acc + (skip - 1) * raw_delta

    for mw in ctx.modwrap:
        raw = state.tags.get(mw.name, 0)
        if isinstance(raw, bool) or not isinstance(raw, int):
            continue
        patches[mw.name] = (raw + (skip - 1) * mw.c) % mw.m

    return patches


def _do_fold(
    runner: PLC,
    skip: int,
    ctx: _FoldContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
) -> tuple[tuple[str, Any], ...]:
    """Fold ``skip`` pure-accumulation scans into one real step.

    Timers ride the dt knob (the interpreter does ``skip`` scans of dt in the
    one step); per-scan counters can't be moved by time, so their accumulators
    are patched forward by ``(skip-1)*delta`` and the step's own ``execute``
    supplies the final increment, keeping every source in phase.

    Uses ``_run_single_scan(consume_pause_request=False)`` so the caller
    retains control over pause consumption.
    """
    patches = _fold_patches(runner.state, skip, ctx, before_tot, after_tot)
    if patches:
        runner.patch(patches)

    runner._dt_override_for_next_scan = skip * ctx.normal_dt
    runner._run_single_scan(consume_pause_request=False)

    runner._state = runner._state.set(scan_id=runner._state.scan_id + skip - 1)
    return tuple(patches.items())


@dataclass(frozen=True)
class _FoldProbe:
    """State captured immediately before one ordinary probe scan."""

    totals: dict[str, float]
    visible: Any
    timestamp: float


@dataclass(frozen=True)
class _FoldAdvance:
    """One certified macro step performed after a probe scan."""

    logical_scans: int
    kernel_scans: int = 1
    patches: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class _FoldPlan:
    """A certified ordinary-fold landing, independent of its executor.

    The proof needs the states on either side of one genuine probe scan.  It
    does not need to know whether the eventual macro step is interpreted or
    compiled.  Keeping the plan separate lets historical replay use the same
    crossing, clock, and plateau proof without constructing a second history.
    """

    skip: int
    after_totals: dict[str, float]
    promoted: frozenset[str]
    pre_fold_timestamp: float


class _OrdinaryFoldStrategy:
    """Reusable plateau/crossing proof shared by every folding loop.

    The caller owns scan cadence and stopping predicates. It captures a probe,
    executes one normal scan, judges its own stop condition, then offers the
    landing to :meth:`try_fold`. This keeps CycleFold's cycle observer and the
    public run loops in control without duplicating ordinary-fold soundness.
    """

    def __init__(
        self,
        ctx: _FoldContext,
        extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
    ) -> None:
        self.ctx = ctx
        self.extra_comparisons = extra_comparisons
        self.exclude = (
            ctx.acc_names
            | ctx.profile_fb_names
            | ctx.churn_excluded
            | ctx.modwrap_names
            | ctx.mirror_names
            | ctx.frozen_writes
        )
        self.inert_soft: set[str] = set()
        self.inert_run: dict[str, int] = {}

    def capture(self, runner: PLC) -> _FoldProbe:
        """Capture the entry side of one normal probe scan.

        ``visible`` omits only coordinates whose evolution has a separate,
        statically certified projection (accumulators, modular self-calcs,
        affine views, and proven unobservable/frozen writes). Everything else
        is the plateau guard: one changed value declines the fold.
        """
        return self.capture_state(runner._state)

    def capture_state(self, state: SystemState) -> _FoldProbe:
        """Capture a probe from a state-only executor such as compiled replay."""
        return _FoldProbe(
            totals=_acc_totals(state, self.ctx.sources),
            visible=_visible_items(state, self.exclude),
            timestamp=state.timestamp,
        )

    def plan(
        self,
        state: SystemState,
        probe: _FoldProbe,
        *,
        max_skip: int | None = None,
        min_skip: int = 1,
        harness_scan: int | None = None,
        endpoint_is_boundary: bool = False,
    ) -> _FoldPlan | None:
        """Certify a macro landing after a completed probe scan.

        Replay may set ``endpoint_is_boundary`` when ``max_skip`` denotes one
        exact requested state or the scan immediately preceding a recorded
        input event.  Live run loops leave it false: their budget is not a
        semantic program boundary, and preserving their established cadence
        matters to PILOT's bounded correction lifecycle.
        """
        if not _visible_items_match(state, probe.visible, self.exclude):
            self.inert_soft.clear()
            self.inert_run.clear()
            return None

        promoted = _runtime_soft_clocks(self.ctx, state)
        _mark_inert_soft(
            self.ctx,
            self.inert_soft,
            self.inert_run,
            probe.timestamp,
            state.timestamp,
            promoted,
        )
        after_totals = _acc_totals(state, self.ctx.sources)
        acc_scans = _nearest_acc_crossing(
            self.ctx,
            probe.totals,
            after_totals,
            state,
            self.extra_comparisons,
        )
        mod_scans = _nearest_mod_flip(
            self.ctx,
            state,
            self.extra_comparisons,
        )
        candidates = [n for n in (acc_scans, mod_scans) if n is not None]
        skip = min(candidates) - 1 if candidates else None

        if harness_scan is not None:
            gap = harness_scan - state.scan_id - 1
            if gap >= 0:
                skip = min(skip, gap) if skip is not None else gap

        clock_gap = _scans_to_clock_edge(
            self.ctx,
            state,
            frozenset(self.inert_soft),
            promoted,
        )
        if clock_gap is not None:
            skip = min(skip, clock_gap) if skip is not None else clock_gap
        if max_skip is not None:
            if skip is not None:
                skip = min(skip, max_skip)
            elif endpoint_is_boundary:
                skip = max_skip
        if skip is None or skip < min_skip:
            return None
        return _FoldPlan(
            skip=skip,
            after_totals=after_totals,
            promoted=promoted,
            pre_fold_timestamp=state.timestamp,
        )

    def finish(
        self,
        state: SystemState,
        probe: _FoldProbe,
        plan: _FoldPlan,
        *,
        patches: tuple[tuple[str, Any], ...] = (),
    ) -> _FoldAdvance:
        """Update window-local clock evidence after an executor lands *plan*."""
        if _visible_items_match(state, probe.visible, self.exclude):
            _mark_inert_soft(
                self.ctx,
                self.inert_soft,
                self.inert_run,
                plan.pre_fold_timestamp,
                state.timestamp,
                plan.promoted,
            )
        else:
            self.inert_soft.clear()
            self.inert_run.clear()
        return _FoldAdvance(logical_scans=plan.skip, patches=patches)

    def try_fold(
        self,
        runner: PLC,
        probe: _FoldProbe,
        *,
        max_skip: int | None = None,
        min_skip: int = 1,
    ) -> _FoldAdvance | None:
        """Fold after a completed probe, or return ``None`` if proof declines."""
        harness_scan = _harness_nearest_scan(runner)
        plan = self.plan(
            runner._state,
            probe,
            max_skip=max_skip,
            min_skip=min_skip,
            harness_scan=harness_scan,
        )
        if plan is None:
            return None
        patches = _do_fold(runner, plan.skip, self.ctx, probe.totals, plan.after_totals)
        return self.finish(runner._state, probe, plan, patches=patches)


# ── 13. Runner integration ──────────────────────────────────────────


def fold_run_until(
    runner: PLC,
    predicate: Callable[[SystemState], bool],
    *,
    max_cycles: int,
    fold_ctx: _FoldContext,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
    stats: dict[str, int] | None = None,
    advances: list[tuple[str, Any]] | None = None,
) -> SystemState:
    """Fold-aware ``run_until`` loop.

    Steps scan-by-scan like the original, but when a plateau is detected
    (only accumulators changed), computes the nearest crossing and folds
    forward.  Respects ``when().pause()`` breakpoints and ``max_cycles``.

    *extra_comparisons* carries thresholds the predicate reads on excluded
    (accumulator / mod-wrap) tags so the fold lands on them exactly instead of
    overshooting to the preset.
    """
    strategy = _OrdinaryFoldStrategy(fold_ctx, extra_comparisons)
    used = 0
    kernel_scans = 0
    macro_folds = 0
    while used < max_cycles:
        # ── Probe: one normal scan ───────────────────────────────
        runner._consume_pause_request()
        probe = strategy.capture(runner)
        runner._run_single_scan(consume_pause_request=False)
        used += 1
        kernel_scans += 1

        pause_requested = runner._consume_pause_request()
        if predicate(runner._state) or pause_requested:
            break

        if used >= max_cycles:
            break

        advance = strategy.try_fold(runner, probe, max_skip=max_cycles - used)
        if advance is not None:
            used += advance.logical_scans
            kernel_scans += advance.kernel_scans
            macro_folds += 1
            if advances is not None:
                advances.extend(advance.patches)
            pause_requested = runner._consume_pause_request()
            if predicate(runner._state) or pause_requested:
                break

    if stats is not None:
        ordinary_folded_scans = used - kernel_scans
        stats["logical_scans"] = used
        stats["kernel_scans"] = kernel_scans
        stats["macro_folds"] = macro_folds
        stats["skipped_scans"] = ordinary_folded_scans
        # Scalar work partition used by replay feasibility instrumentation.
        # These three values always sum to ``logical_scans`` and do not retain
        # any per-scan evidence.
        stats["ordinary_folded_scans"] = ordinary_folded_scans
        stats["cycle_folded_scans"] = 0
        stats["residual_scans"] = kernel_scans
    return runner._state


def fold_run_for(
    runner: PLC,
    seconds: float,
    *,
    fold_ctx: _FoldContext,
) -> SystemState:
    """Fold-aware ``run_for`` loop.

    Same plateau/fold logic as ``fold_run_until``, but terminates on
    ``state.timestamp >= target_time``.
    """
    strategy = _OrdinaryFoldStrategy(fold_ctx)
    target_time = runner._state.timestamp + seconds
    while runner._state.timestamp < target_time:
        # ── Probe: one normal scan ───────────────────────────────
        runner._consume_pause_request()
        probe = strategy.capture(runner)
        runner._run_single_scan(consume_pause_request=False)

        pause_requested = runner._consume_pause_request()
        if runner._state.timestamp >= target_time or pause_requested:
            break

        # Constrain so dt doesn't overshoot the time target.
        max_skip = None
        if fold_ctx.normal_dt > 0:
            remaining_scans = int((target_time - runner._state.timestamp) / fold_ctx.normal_dt)
            if remaining_scans > 0:
                max_skip = remaining_scans

        advance = strategy.try_fold(runner, probe, max_skip=max_skip)
        if advance is not None:
            pause_requested = runner._consume_pause_request()
            if runner._state.timestamp >= target_time or pause_requested:
                break

    return runner._state
