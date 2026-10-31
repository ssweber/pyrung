"""Time folding: collapse provably-identical scans into one step.

Between accumulator crossings every rung emits identical output.  The fold
detects these plateaus, computes the nearest crossing in closed form, and
folds the runner forward — dt knob for timers, acc-patch for per-scan
self-calcs, modular arithmetic for wrapping self-calcs.  A counter counts
rising edges, so the ordinary fold never projects one: a probe scan that
counts declines the window (CycleFold still certifies a counter that rides
a whole input cycle, see ``pilot/cyclefold.py``).  Soundness rests on
the plateau guard for every value it checks, and on the crossing set for
every value it lets move: each read of a moving value is a comparison the
crossing arithmetic places, a read the landing scan reproduces, or it bounds
the window at that value's next change (see _audit_coordinate_reads).

Module structure
────────────────
1. Source types         — what the fold tracks (_AccSource, _ModWrap)
2. Fold context         — assembled static priors for the fold loop
3. Instruction registry — instruction type → fold-source mapping
4. Expression matching  — pattern detection on calc expressions
5. Comparison scanning  — rung reads relevant to crossings
6. Churn analysis       — identifying unobservable state changes
6a. Real-time clock     — rtc.* fields as clocks with known ticks
6b. Time mirrors        — clock copies the landing rebuilds; soft ticks
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
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from pyrsistent import PMap

from pyrung.core.storage import real_in_range

_PMAP: Any = PMap  # its bucket accessors are private API

if TYPE_CHECKING:
    from datetime import datetime

    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.executor import ConditionViewCapture
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState

# ── Constants ───────────────────────────────────────────────────────

_EMPTY_CAP = 20_000
_MAX_ADVANCE_ITERS = 4_000
_EPS = 1e-9

_CaptureDecision = bool | Callable[[], bool]


def _capture_requested(decision: _CaptureDecision) -> bool:
    return decision() if callable(decision) else decision


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
    timed: bool  # True: time-based (dt knob).  False: counter or self-calc (acc patch).
    bidir: bool = False  # CountUp with down_condition — delta sign varies at runtime
    unit: Any = None  # TimeUnit for timed sources; None for counters / synthetic sources.
    acc_max: int = 32767  # Timed sources: the accumulator's clamp (a time drum's DINT is wider).
    # Self-calc sources: the per-scan step (an int literal, or the name of a tag
    # no rung writes, ``-``-prefixed when subtracted) and the destination's wrap
    # range.  The probe must move the tag by exactly that step, and no skipped
    # scan may leave the range.
    step: int | str | None = None
    value_range: tuple[int, int] | None = None


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
    # Some rung reads ``sys.first_scan``: a probe scan run at scan 0 sees it True
    # while the window after it sees False, so such a probe cannot certify a fold.
    reads_first_scan: bool = False
    # ``rtc.*`` fields a rung or the run_until condition reads.  Each is a clock
    # whose next tick is known from the RTC base (see _scans_to_rtc_tick).
    rtc_fields: frozenset[str] = frozenset()
    # The subset the run_until condition reads: always bounded, never soft.
    rtc_condition_fields: frozenset[str] = frozenset()
    # Stored copies of the clock the plateau guard ignores, and when a window
    # may cross ticks (see _TimeMirrors).
    time_mirrors: _TimeMirrors | None = None
    # Coordinates some read reaches other than through a comparison the
    # crossing set models (an expression, an index, a moving operand or
    # preset, a stored copy, an edge): their next value change bounds every
    # window they move in (see :func:`_audit_coordinate_reads`).
    unmodeled: frozenset[str] = frozenset()
    # ``(view, source)`` per affine acc view in ``mirror_names``.
    mirror_sources: tuple[tuple[str, str], ...] = ()
    # Unread tags a copy/calc writes from a coordinate: stale inside a window,
    # rebuilt by its landing scan (see _audit_coordinate_reads).
    landing_rebuilt: frozenset[str] = frozenset()
    # Timed accumulators a scan may write more than once (see _timer_contests).
    contested_timers: frozenset[str] = frozenset()
    # Edge memory the plateau guard leaves to the clock handling (see
    # every_scan_clock_edge_keys).
    clock_edge_memory: frozenset[str] = frozenset()

    @cached_property
    def coordinates(self) -> frozenset[str]:
        """Every moving value the crossing set tracks for the plateau guard."""
        return self.acc_names | self.modwrap_names | self.mirror_names

    def unmodeled_coordinates(self, names: Collection[str]) -> frozenset[str]:
        """The tracked sources behind *names* (a view stands for its source)."""
        views = dict(self.mirror_sources)
        out = {views.get(name, name) for name in names}
        return frozenset(out) & (self.acc_names | self.modwrap_names)

    @cached_property
    def timed_sources(self) -> tuple[_AccSource, ...]:
        """The time-based sources, whose carry recurrence a fold replays exactly."""
        return tuple(src for src in self.sources if src.timed)

    @cached_property
    def timed_acc_names(self) -> frozenset[str]:
        return frozenset(src.acc_name for src in self.timed_sources)


# ── 3. Instruction registry ─────────────────────────────────────────

_SOURCE_REGISTRY: dict[type, tuple[str, bool]] | None = None


def _ensure_registry() -> dict[type, tuple[str, bool]]:
    """Build the instruction→(kind, timed) dispatch table on first use."""
    global _SOURCE_REGISTRY
    if _SOURCE_REGISTRY is not None:
        return _SOURCE_REGISTRY

    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction

    _SOURCE_REGISTRY = {
        OnDelayInstruction: ("up", True),
        OffDelayInstruction: ("up", True),
        CountUpInstruction: ("up", False),
        CountDownInstruction: ("down", False),
        TimeDrumInstruction: ("up", True),
        TimeEventDrumInstruction: ("up", True),
        DebounceInstruction: ("up", True),
        FlasherInstruction: ("up", True),
        TimedInstruction: ("up", True),
    }
    return _SOURCE_REGISTRY


def _instruction_phases(instr: Any) -> list[tuple[Any, Any, Any]]:
    """``(accumulator, done, preset)`` for each phase a source times.

    An Out coil variant times one or two phases, each like an on-delay timer.
    """
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )

    if isinstance(instr, (TimeDrumInstruction, TimeEventDrumInstruction)):
        preset = _StepPreset(instr.current_step.name, instr.presets)
        return [(instr.accumulator, instr.completion_flag, preset)]
    if isinstance(instr, (DebounceInstruction, FlasherInstruction)):
        return [
            (instr.on_acc, instr.on_done, instr.on_preset),
            (instr.off_acc, instr.off_done, instr.off_preset),
        ]
    if isinstance(instr, TimedInstruction):
        return [(instr.timer_acc, instr.timer_done, instr.on_preset)]
    return [(instr.accumulator, instr.done_bit, instr.preset)]


def _collect_acc_sources(program: Any) -> list[_AccSource]:
    """Introspect every registered accumulating instruction, including calls."""
    from pyrung.core.instruction.counters import CountUpInstruction
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
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
        drum = isinstance(instr, (TimeDrumInstruction, TimeEventDrumInstruction))
        for accumulator, done, preset in _instruction_phases(instr):
            out[accumulator.name] = _AccSource(
                acc_name=accumulator.name,
                done_bit=done.name,
                preset=preset.name if isinstance(preset, Tag) else preset,
                kind=kind,
                timed=timed,
                bidir=bidir,
                unit=getattr(instr, "unit", None) if timed else None,
                acc_max=instr._acc_max() if drum else 32767,
            )
    return list(out.values())


# ── 4. Expression matching ──────────────────────────────────────────


def _calc_self_referential(tag: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when *tag* is the dest of a ``calc`` that reads *tag* itself."""
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction

    def reads_tag(expr: Any) -> bool:
        # The PDG's own read walker, so every expression shape (sums, math and
        # shift functions, indirect refs) counts exactly as the PDG counts it.
        return tag in _extract_tag_names(expr, dict(pdg.tags))

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


def _match_affine_selfcalc(
    expr: Any,
    tag: str,
    constant_tag: Callable[[str], bool] | None = None,
) -> tuple[int | str, int | None] | None:
    """Match ``(tag ± c) % m`` / ``tag ± c``; return ``(c, m)`` or ``None``.

    ``c`` is an int literal.  Without ``% m`` it may also be a tag for which
    *constant_tag* holds; ``c`` is then that tag's name, prefixed with ``-``
    for ``tag - K``.
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

    def operand(e: Any, allow_tag: bool) -> int | str | None:
        k = lit(e)
        if k is not None:
            return k
        if allow_tag and constant_tag is not None and isinstance(e, TagExpr):
            name = getattr(e.tag, "name", None)
            if isinstance(name, str) and name != tag and constant_tag(name):
                return name
        return None

    def linear(e: Any, allow_tag: bool) -> int | str | None:
        if not isinstance(e, BinaryExpr):
            return None
        if e.symbol == "+":
            if is_self(e.left):
                return operand(e.right, allow_tag)
            if is_self(e.right):
                return operand(e.left, allow_tag)
        elif e.symbol == "-" and is_self(e.left):
            k = operand(e.right, allow_tag)
            if k is None:
                return None
            return f"-{k}" if isinstance(k, str) else -k
        return None

    if isinstance(expr, BinaryExpr) and expr.symbol == "%":
        m = lit(expr.right)
        c = linear(expr.left, allow_tag=False)
        if m is not None and isinstance(c, int) and c != 0 and 0 < m <= 32767:
            return (c, m)
        return None
    c = linear(expr, allow_tag=True)
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


_COMPARE_FORMS = {
    "CompareEq": "eq",
    "CompareNe": "ne",
    "CompareLt": "lt",
    "CompareLe": "le",
    "CompareGt": "gt",
    "CompareGe": "ge",
}


def _condition_leaves(condition: Any) -> list[Any]:
    """The leaf conditions of *condition* (``None``, a list, or a tree)."""
    from pyrung.core.condition import AllCondition, AnyCondition

    out: list[Any] = []
    stack = [condition]
    while stack:
        cond = stack.pop()
        if cond is None:
            continue
        if isinstance(cond, (list, tuple)):
            stack.extend(cond)
        elif isinstance(cond, (AllCondition, AnyCondition)):
            stack.extend(cond.conditions)
        else:
            out.append(cond)
    return out


def _audit_condition(
    condition: Any,
    coords: frozenset[str],
    tags: dict[str, Any],
    cmps: dict[str, list[tuple[str, Any]]],
    unmodeled: set[str],
) -> set[str]:
    """Classify every read of a coordinate in *condition*; return the ones seen.

    A leaf ``coord <cmp> operand`` (or an Int contact, ``coord != 0``) is a
    crossing the fold places; it is recorded in *cmps*.  Any other leaf that
    reads a coordinate, and a coordinate read as a comparison's operand, is
    added to *unmodeled*.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.analysis.simplified import _operand_label
    from pyrung.core.condition import IntTruthyCondition
    from pyrung.core.tag import Tag

    seen: set[str] = set()
    for leaf in _condition_leaves(condition):
        reads = _extract_tag_names(leaf, tags) & coords
        if not reads:
            continue
        seen |= reads
        tag = getattr(leaf, "tag", None)
        name = tag.name if isinstance(tag, Tag) else None
        form = _COMPARE_FORMS.get(type(leaf).__name__)
        operand: Any = None
        if form is not None:
            operand = _operand_label(leaf.value)
        elif isinstance(leaf, IntTruthyCondition):
            form, operand = "ne", 0
        if form is None or name not in reads:
            unmodeled |= reads
            continue
        cmps.setdefault(name, []).append((form, operand))
        unmodeled |= reads - {name}
    return seen


def _audit_coordinate_reads(
    pdg: ProgramGraph,
    program: Any,
    coords: frozenset[str],
    modeled_writes: dict[str, frozenset[str]],
    unread: Callable[[str], bool] = lambda _name: False,
    dormant: dict[str, list[frozenset[str] | None]] | None = None,
) -> tuple[
    frozenset[str],
    dict[str, list[tuple[str, Any]]],
    dict[str, list[tuple[str, Any]]],
    frozenset[str],
]:
    """Every read of a moving coordinate, sorted into modeled and unmodeled.

    Between crossings the plateau guard ignores a coordinate's motion, so the
    crossing set must cover each of its readers.  Covered are a comparison
    ``coord <cmp> operand`` in a rung's or an instruction's condition, the
    accumulating instruction's own read of its accumulator, the writer of
    *modeled_writes* (a self-calc reading itself, an affine view reading its
    source), and a plain ``copy``/``calc`` whose every write is *unread* (no
    rung reads it, nor a stop predicate): its rung's condition holds still
    through a window, so the landing scan rebuilds what stepping leaves there
    (a display such as ``calc(Preset - Tmr.Acc, HMI_Left)``).  Every other
    read, and any read the program graph records that this walk does not
    find, makes the coordinate *unmodeled*.

    A ``copy``/``calc`` read in a main rung gated by ``rise(X)`` / ``fall(X)``
    of a plain tag ``X`` the rung does not write, or by a one-shot, is
    *dormant* when *dormant* is given: the read is recorded there under the
    coordinate (with the gate tags, ``None`` for a one-shot) instead of
    counting as unmodeled.  Every scan of a window sees ``X`` as the probe
    did, so the edge, and the one-shot, never fire inside it; the caller
    confirms ``X`` is guarded by the plateau.

    Returns ``(unmodeled, rung_cmps, instruction_cmps, rebuilt)``: the
    comparisons found in rung conditions and in instruction conditions
    (resets, count-down legs, drum events), which the rung condition scan does
    not reach, and the unread tags whose value a window leaves stale until the
    landing scan.
    """
    from pyrung.core.analysis.pdg import _extract_instruction_writes, _extract_tag_names
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.condition import FallingEdgeCondition, RisingEdgeCondition
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.system_points import _DERIVED_EDGE_TAGS
    from pyrung.core.tag import Tag

    tags = dict(pdg.tags)
    registry = _ensure_registry()
    unmodeled: set[str] = set()
    rebuilt: set[str] = set()
    rung_cmps: dict[str, list[tuple[str, Any]]] = {}
    instr_cmps: dict[str, list[tuple[str, Any]]] = {}
    if not coords:
        return frozenset(), rung_cmps, instr_cmps, frozenset()
    for node in pdg.rung_nodes:
        reads = (node.own_condition_reads | node.data_reads | node.exclusive_reads) & coords
        if not reads:
            continue
        rung = _resolve_rung(program, node)
        if rung is None:
            unmodeled |= reads
            continue
        seen: set[str] = set()
        for cond in rung._conditions:
            seen |= _audit_condition(cond, coords, tags, rung_cmps, unmodeled)
        # forloop body-rung contacts are read inside the scan like any rung's.
        for loop in _nested_instructions(rung._instructions):
            for body_rung in getattr(loop, "body_rungs", ()):
                for cond in body_rung._conditions[body_rung._branch_condition_start :]:
                    seen |= _audit_condition(cond, coords, tags, rung_cmps, unmodeled)
        gates: frozenset[str] = frozenset()
        top_level = dormant is not None and node.scope == "main" and not node.branch_path
        if top_level:
            gates = (
                frozenset(
                    cond._resolved_tag.name
                    for cond in rung._conditions
                    if isinstance(cond, (RisingEdgeCondition, FallingEdgeCondition))
                    and cond._resolved_tag.name not in _DERIVED_EDGE_TAGS
                    and isinstance(cond.tag, Tag)
                )
                - node.all_writes
            )
        for instr in _nested_instructions(rung._instructions):
            cls = type(instr)
            for field_name in getattr(cls, "_conditions", ()):
                seen |= _audit_condition(
                    getattr(instr, field_name, None), coords, tags, instr_cmps, unmodeled
                )
            own = (
                {acc.name for acc, _done, _preset in _instruction_phases(instr)}
                if type(instr) in registry
                else set()
            )
            dest = getattr(getattr(instr, "dest", None), "name", None)
            allowed = modeled_writes.get(dest, frozenset()) if dest is not None else frozenset()
            writes, target_reads, implicit = _extract_instruction_writes(instr, tags)
            outputs = set(writes) | set(implicit)
            exclusive = getattr(cls, "_exclusive_fields", ())
            instr_reads: set[str] = set()
            for field_name in (*getattr(cls, "_reads", ()), *exclusive):
                field_reads = _extract_tag_names(getattr(instr, field_name, None), tags)
                instr_reads |= field_reads
                field_reads &= coords
                seen |= field_reads
                if field_name in exclusive:
                    field_reads = field_reads - own
                field_reads -= allowed
                if not field_reads:
                    continue
                plain = type(instr) in (CopyInstruction, CalcInstruction)
                oneshot = bool(getattr(instr, "oneshot", False))
                if (
                    plain
                    and not oneshot
                    and outputs
                    and all(unread(name) for name in outputs)
                    and not outputs & instr_reads
                ):
                    rebuilt |= outputs
                    continue
                if dormant is not None and plain and top_level and (oneshot or gates):
                    for name in field_reads:
                        dormant.setdefault(name, []).append(None if oneshot else gates)
                    continue
                unmodeled |= field_reads
            target_reads = set(target_reads) & coords
            seen |= target_reads
            unmodeled |= target_reads
        unmodeled |= reads - seen
    return frozenset(unmodeled), rung_cmps, instr_cmps, frozenset(rebuilt)


def _merge_comparisons(
    base: dict[str, tuple[tuple[str, Any], ...]],
    extra: Mapping[str, Sequence[tuple[str, Any]]],
) -> dict[str, tuple[tuple[str, Any], ...]]:
    """*base* with *extra*'s comparisons appended where not already present."""

    def key(item: tuple[str, Any]) -> tuple[str, Any]:
        form, operand = item
        if isinstance(operand, (bool, int, float, str)):
            return (form, (type(operand), operand))
        return (form, id(operand))

    out = dict(base)
    for name, items in extra.items():
        have = list(out.get(name, ()))
        keys = {key(item) for item in have}
        for item in items:
            k = key(item)
            if k not in keys:
                keys.add(k)
                have.append(item)
        out[name] = tuple(have)
    return out


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


# ── 6a. Real-time clock ─────────────────────────────────────────────


def _rtc_field_units() -> dict[str, str]:
    """``rtc.*`` field name -> the calendar unit whose boundary can change it.

    Day, weekday, month and both year fields change only at midnight, so the
    day boundary bounds all of them (exactly for day and weekday, early for the
    rest).
    """
    from pyrung.core.system_points import system as _system

    rtc = _system.rtc
    return {
        rtc.second.name: "second",
        rtc.minute.name: "minute",
        rtc.hour.name: "hour",
        rtc.day.name: "day",
        rtc.weekday.name: "day",
        rtc.month.name: "day",
        rtc.year4.name: "day",
        rtc.year2.name: "day",
    }


_RTC_UNIT_ORDER = ("second", "minute", "hour", "day")


def _rtc_apply_names() -> tuple[str, str]:
    from pyrung.core.system_points import system as _system

    return (_system.rtc.apply_date.name, _system.rtc.apply_time.name)


def _rtc_finest_unit(fields: frozenset[str]) -> str | None:
    units = _rtc_field_units()
    present = {units[f] for f in fields if f in units}
    for unit in _RTC_UNIT_ORDER:
        if unit in present:
            return unit
    return None


def _rtc_boundary(now: datetime, unit: str) -> datetime:
    """The first RTC instant after *now* at which a *unit*-grained field may change."""
    from datetime import timedelta

    if unit == "second":
        return now.replace(microsecond=0) + timedelta(seconds=1)
    if unit == "minute":
        return now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    if unit == "hour":
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _rtc_reads_before(
    rtc_at: Callable[[float], datetime],
    t0: float,
    dt: float,
    boundary: datetime,
) -> int:
    """How many consecutive scans reading at ``t0, t0 + dt, ...`` see the RTC before *boundary*.

    Scan ``j`` of a window reads the RTC at ``t0 + j * dt`` (the landing scan
    resolves it at exactly that expression).  The estimate from the remaining
    seconds is corrected against the runtime's own ``rtc_at`` so the count
    agrees with what each scan resolves, including its microsecond rounding.
    """
    remaining = (boundary - rtc_at(t0)).total_seconds()
    n = max(0, math.ceil(remaining / dt - _EPS))
    while n > 0 and rtc_at(t0 + (n - 1) * dt) >= boundary:
        n -= 1
    while rtc_at(t0 + n * dt) < boundary:
        n += 1
    return n


def _rtc_read_fields(pdg: ProgramGraph) -> frozenset[str]:
    """``rtc.*`` fields any rung reads (conditions, data, or exclusive reads)."""
    units = _rtc_field_units()
    out: set[str] = set()
    for node in pdg.rung_nodes:
        out |= _node_reads(node) & units.keys()
    return frozenset(out)


def _scans_to_rtc_tick(
    ctx: _FoldContext,
    state: Any,
    probe_timestamp: float,
    rtc_at: Callable[[float], datetime] | None,
    fields: frozenset[str],
) -> int | None:
    """Largest fold skip whose scans all read the RTC fields *fields* unchanged.

    Each ``rtc.*`` field is a clock: resolved on read from ``rtc_base +
    timestamp`` and never stored, so the plateau guard cannot see it move.
    The window after a probe is certified only by what the probe scan did, so

    * the probe must have read the same field values the window starts with.
      A probe that straddled a tick saw the previous value; return ``0``;
    * every window scan, the landing scan included, must read before the next
      tick of the finest read field.  The landing state itself may sit on or
      past the tick: the next genuine scan reads the new value.

    A program (or a patch) that leaves ``rtc.apply_date`` / ``apply_time`` set
    moves the RTC base at the start of the next scan, so nothing is certified
    then.  ``None`` means no field needs bounding.
    """
    if not fields:
        return None
    unit = _rtc_finest_unit(fields)
    if unit is None:
        return None
    dt = ctx.normal_dt
    if rtc_at is None or dt <= 0:
        return 0
    tags = state.tags
    if any(tags.get(name) for name in _rtc_apply_names()):
        return 0
    try:
        boundary = _rtc_boundary(rtc_at(probe_timestamp), unit)
        if rtc_at(state.timestamp) >= boundary:
            return 0
        return _rtc_reads_before(rtc_at, state.timestamp, dt, boundary)
    except OverflowError:
        return 0  # the next tick lies past datetime.max: decline, never raise


def _rtc_jump_room(
    ctx: _FoldContext,
    state: Any,
    observed_scans: int,
    rtc_at: Callable[[float], datetime] | None,
    fields: frozenset[str],
) -> int | None:
    """Most scans a cycle jump from *state* may skip without an RTC tick.

    A cycle jump extrapolates the last *observed_scans* genuine scans, so they
    and every skipped scan must read the same values of *fields*: the first
    observed scan read at ``state.timestamp - observed_scans * dt``.  Returns
    ``0`` when the observation already spans a tick, ``None`` when *fields*
    is empty.
    """
    if not fields:
        return None
    unit = _rtc_finest_unit(fields)
    if unit is None:
        return None
    dt = ctx.normal_dt
    if rtc_at is None or dt <= 0:
        return 0
    tags = state.tags
    if any(tags.get(name) for name in _rtc_apply_names()):
        return 0
    try:
        boundary = _rtc_boundary(rtc_at(state.timestamp - observed_scans * dt), unit)
        if rtc_at(state.timestamp) >= boundary:
            return 0
        return _rtc_reads_before(rtc_at, state.timestamp, dt, boundary)
    except OverflowError:
        return 0  # the next tick lies past datetime.max: decline, never raise


# ── 6b. Time mirrors (soft RTC ticks) ───────────────────────────────


@dataclass(frozen=True)
class _TimeMirrors:
    """When an RTC tick is *soft*: no rung but plain recomputes can act on it.

    A program commonly copies the clock into stored tags every scan
    (``copy(rtc.second, Sec)``) and reads those copies only in rungs that are
    normally off: an alarm history that stamps the time when an alarm edge
    fires, a stop-reason stamp, an acknowledgement.  Bounding every fold window
    at each second would cost such a program its folds, although nothing in it
    acts on the tick.  A window may cross a tick when all of these hold:

    * *names* (the clock mirrors) are written only by *recompute* rungs: reached
      on every scan (a main rung, or a rung of a subroutine called on every scan
      by an unconditional direct ``call()``, with no ``return_early()`` in the
      chain), with no condition, not continued, and holding only plain
      ``copy``/``calc`` with direct destinations whose result range provably
      fits the destination (so no clamp, wrap or fault flag depends on the
      clock).  A mirror's value after a scan depends only on that scan's clock
      and on guarded tags, never on the previous scan, so the landing scan
      (which reads the RTC at the last folded scan's time) rebuilds it exactly;
    * every reader of a mirror runs strictly after every writer of it;
    * no enable condition anywhere reads the clock or a mirror;
    * every other rung that reads one (*dead_nodes*) does nothing with it in
      the window.  Its instructions that read the clock do nothing while the
      rung is unpowered, so it is enough that the rung is off, judged at runtime
      from the window's states (:func:`_time_mirrors_off`).  A main rung whose
      clock reads are all one-shot copies/calcs is also inactive while its
      enable holds one judged value: powered, its one-shot already fired;
    * no mirror is a target, a harness name, a fold coordinate, an excluded
      tag, or a slot an unbounded pointer may reach.

    Then the window's scans differ from the probe scan only in the clock and the
    mirrors, so the plateau guard ignores the mirrors and the fold may cross
    ticks.  Anything not established here keeps every tick a hard bound.
    """

    names: frozenset[str]
    dead_nodes: tuple[int, ...]
    # Dead nodes whose clock reads are all one-shot copies/calcs of a main rung.
    oneshot_nodes: frozenset[int]
    # Per PDG node: the enable conditions, or ``None`` when they cannot be
    # judged at the node's own position (a ``continued()`` rung, or a branch of
    # one, evaluates them on an earlier rung's snapshot).
    conditions: tuple[tuple[Any, ...] | None, ...]
    # Per PDG node: (first, last) main-scan position, or ``None`` if never run.
    spans: tuple[tuple[int, int] | None, ...]
    # Per PDG node: conditions are evaluated at rung entry of a main rung, so
    # every write at the same position happens after them.
    main_entry: tuple[bool, ...]
    node_sub: tuple[str | None, ...]
    callers: dict[str, tuple[int, ...]]
    # Every placed PDG node that may write a tag (an indirect write counts for
    # every named slot of its block).
    writers: dict[str, tuple[int, ...]]
    # Per PDG node: tags it writes only through instructions that write nothing
    # while the rung is unpowered (so an off node is not a writer of them).
    silent_writes: tuple[frozenset[str], ...]
    # Names an enable condition may never read when judging a rung.
    unjudgeable: frozenset[str]


_ALWAYS_ON_NAME = "sys.always_on"
_FIRST_SCAN_NAME = "sys.first_scan"


class _Unjudgeable(Exception):
    """A condition read a value that is not fixed across the window."""


def _contains_indirect(obj: Any, seen: set[int] | None = None) -> bool:
    """Whether *obj* (an operand or expression tree) holds an indirect reference."""
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.tag import Tag

    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (bool, int, float, str, bytes, Tag)):
        return False
    if isinstance(obj, (IndirectRef, IndirectExprRef)):
        return True
    if id(obj) in seen:
        return False
    seen.add(id(obj))
    if isinstance(obj, (list, tuple, set, frozenset)):
        return any(_contains_indirect(item, seen) for item in obj)
    if isinstance(obj, dict):
        return any(_contains_indirect(item, seen) for item in obj.values())
    if hasattr(obj, "__dict__"):
        return any(_contains_indirect(item, seen) for item in vars(obj).values())
    return False


def _rtc_field_ranges() -> dict[str, tuple[int, int]]:
    from pyrung.core.system_points import system as _system

    rtc = _system.rtc
    return {
        rtc.year4.name: (1, 9999),
        rtc.year2.name: (0, 99),
        rtc.month.name: (1, 12),
        rtc.day.name: (1, 31),
        rtc.weekday.name: (1, 7),
        rtc.hour.name: (0, 23),
        rtc.minute.name: (0, 59),
        rtc.second.name: (0, 59),
    }


_TYPE_RANGES: dict[str, tuple[int, int]] = {
    "BOOL": (0, 1),
    "INT": (-32768, 32767),
    "DINT": (-2147483648, 2147483647),
    "WORD": (0, 0xFFFF),
}

_Span = tuple[float, float]


def _tag_range(tag: Any, ranges: dict[str, _Span]) -> _Span | None:
    known = ranges.get(tag.name)
    if known is not None:
        return known
    type_name = getattr(getattr(tag, "type", None), "name", None)
    found = _TYPE_RANGES.get(type_name or "")
    return (float(found[0]), float(found[1])) if found is not None else None


def _expr_range(expr: Any, ranges: dict[str, _Span]) -> _Span | None:
    """Interval of a ``calc`` expression built from ``+``, ``-`` and ``*``, else ``None``."""
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr
    from pyrung.core.tag import Tag

    if isinstance(expr, bool):
        return None
    if isinstance(expr, (int, float)):
        if not math.isfinite(expr):
            return None
        return (float(expr), float(expr))
    if isinstance(expr, Tag):
        return _tag_range(expr, ranges)
    if isinstance(expr, TagExpr):
        return _tag_range(expr.tag, ranges)
    if isinstance(expr, LiteralExpr):
        return _expr_range(expr.value, ranges)
    if isinstance(expr, UnaryExpr) and expr.symbol == "-":
        inner = _expr_range(expr.operand, ranges)
        return None if inner is None else (-inner[1], -inner[0])
    if isinstance(expr, BinaryExpr) and expr.symbol in ("+", "-", "*"):
        a = _expr_range(expr.left, ranges)
        b = _expr_range(expr.right, ranges)
        if a is None or b is None:
            return None
        if expr.symbol == "+":
            return (a[0] + b[0], a[1] + b[1])
        if expr.symbol == "-":
            return (a[0] - b[1], a[1] - b[0])
        products = (a[0] * b[0], a[0] * b[1], a[1] * b[0], a[1] * b[1])
        return (min(products), max(products))
    return None


def _fits_dest(span: _Span, dest: Any) -> bool:
    type_name = getattr(getattr(dest, "type", None), "name", None)
    if type_name == "REAL":
        # Past +/-REAL_MAX the store saturates and sets the out-of-range flag.
        return real_in_range(span[0]) and real_in_range(span[1])
    if type_name not in ("INT", "DINT", "WORD"):
        return False
    lo, hi = _TYPE_RANGES[type_name]
    return lo <= span[0] and span[1] <= hi


def _recompute_range(
    instr: Any, ranges: dict[str, _Span], tags: dict[str, Any]
) -> tuple[str, _Span] | None:
    """``(dest, value range)`` of a plain ``copy``/``calc`` that cannot fault.

    The stored result must depend only on this scan's reads (no one-shot, no
    conversion, no self-read, no indirect reference), and its value range must
    fit the destination, so no clamp, wrap or fault flag depends on the clock.
    ``None`` otherwise.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction
    from pyrung.core.tag import Tag

    if type(instr) is CopyInstruction:
        dest, source = instr.dest, instr.source
        if instr.convert is not None or instr.oneshot or not isinstance(dest, Tag):
            return None
        if isinstance(source, Tag):
            span = _tag_range(source, ranges)
        elif isinstance(source, (int, float)) and not isinstance(source, bool):
            span = _expr_range(source, ranges)
        else:
            return None
    elif type(instr) is CalcInstruction:
        dest = instr.dest
        if instr.oneshot or not isinstance(dest, Tag) or instr.mode != "decimal":
            return None
        if _contains_indirect(instr.expression):
            return None
        if dest.name in _extract_tag_names(instr.expression, tags):
            return None
        span = _expr_range(instr.expression, ranges)
    else:
        return None
    if span is None or not _fits_dest(span, dest):
        return None
    return dest.name, span


def _silent_when_off(instr: Any) -> bool:
    """Instructions that neither read an operand nor write while the rung is unpowered."""
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.control import CallInstruction

    if type(instr) in (LatchInstruction, ResetInstruction):
        return True
    return _inert_when_off(instr) and type(instr) is not CallInstruction


def _inert_when_off(instr: Any) -> bool:
    """Instructions whose execution with an unpowered rung reads no operand."""
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.control import CallInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.instruction.packing import (
        PackBitsInstruction,
        PackTextInstruction,
        PackWordsInstruction,
        UnpackToBitsInstruction,
        UnpackToWordsInstruction,
    )

    return type(instr) in (
        CopyInstruction,
        CalcInstruction,
        BlockCopyInstruction,
        FillInstruction,
        PackBitsInstruction,
        PackWordsInstruction,
        PackTextInstruction,
        UnpackToBitsInstruction,
        UnpackToWordsInstruction,
        CallInstruction,
    )


def _instruction_reads(instr: Any, tags: dict[str, Any]) -> frozenset[str]:
    """Every tag an instruction reads through its operand fields and targets."""
    from pyrung.core.analysis.pdg import _extract_instruction_writes, _extract_tag_names

    reads: set[str] = set()
    cls = type(instr)
    for field_name in (
        *getattr(cls, "_reads", ()),
        *getattr(cls, "_conditions", ()),
        *getattr(cls, "_exclusive_fields", ()),
    ):
        reads |= _extract_tag_names(getattr(instr, field_name), tags)
    _writes, target_reads, _implicit = _extract_instruction_writes(instr, tags)
    return frozenset(reads | target_reads)


def _nested_instructions(instructions: Sequence[Any]) -> list[Any]:
    """*instructions* with every for-loop body flattened in."""
    out: list[Any] = []
    stack = list(instructions)
    while stack:
        instr = stack.pop()
        out.append(instr)
        stack.extend(getattr(instr, "instructions", None) or ())
    return out


def _rung_instructions(rung: Any) -> list[Any]:
    """A rung's instructions, its branches' and loop bodies' included."""
    out = _nested_instructions(rung._instructions)
    for branch in getattr(rung, "_branches", None) or ():
        out.extend(_rung_instructions(branch))
    return out


def _always_reached_subroutines(pdg: ProgramGraph, program: Any) -> frozenset[str]:
    """Subroutines whose every rung runs on every scan.

    Every call site must be a direct ``call()`` in an unconditional rung (not a
    branch or a loop) that itself runs on every scan, and neither the callee
    nor a calling subroutine may hold a ``return_early()``.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.control import CallInstruction, ReturnInstruction

    def has_return(sub: str) -> bool:
        return any(
            isinstance(instr, ReturnInstruction)
            for rung in program.subroutines.get(sub, ())
            for instr in _rung_instructions(rung)
        )

    sites: dict[str, list[int]] = {}
    for site in pdg.call_sites():
        sites.setdefault(site.callee, []).append(site.node_index)

    always: set[str] = set()
    changed = True
    while changed:
        changed = False
        for sub, nodes in sites.items():
            if sub in always or has_return(sub):
                continue
            ok = bool(nodes)
            for ni in nodes:
                node = pdg.rung_nodes[ni]
                rung = resolve_rung(program, node)
                if (
                    rung is None
                    or node.branch_path
                    or node.condition_reads
                    or rung._conditions
                    or (node.subroutine is not None and node.subroutine not in always)
                    or not any(
                        isinstance(instr, CallInstruction) and instr.subroutine_name == sub
                        for instr in rung._instructions
                    )
                ):
                    ok = False
                    break
            if ok:
                always.add(sub)
                changed = True
    return frozenset(always)


def _top_rung(program: Any, node: Any) -> Any:
    """The top-level rung (not the branch) a PDG node belongs to."""
    from dataclasses import replace as _replace

    from pyrung.core.analysis.pdg import resolve_rung

    if not node.branch_path:
        return resolve_rung(program, node)
    return resolve_rung(program, _replace(node, branch_path=()))


def _time_mirror_analysis(
    pdg: ProgramGraph,
    program: Any,
    *,
    coordinates: frozenset[str],
    excluded: frozenset[str],
    target_names: frozenset[str],
    protected: frozenset[str],
    churn: frozenset[str] = frozenset(),
) -> _TimeMirrors | None:
    """Static half of :class:`_TimeMirrors`; ``None`` when the program does not qualify.

    *coordinates* are tags the fold projects on its own (accumulators, modular
    self-calcs, affine views, harness feedback); a mirror may not be one.
    *excluded* is every tag outside the plateau guard; a recompute rung may not
    read one (its pre-landing value can lag the stepped one), and an enable
    condition judged at runtime may not depend on one.  A mirror may still be
    in *churn* (the unread outputs the guard already ignores): a recompute
    rung's unread output is one.
    """
    from pyrung.core.analysis.pdg import _extract_instruction_writes, resolve_rung
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
    from pyrung.core.system_points import system as _system

    rtc_ranges = _rtc_field_ranges()
    rtc = frozenset(rtc_ranges)
    nodes = pdg.rung_nodes
    reads, pos = _node_scan_context(pdg)
    if not any(r & rtc for r in reads):
        return None
    # A rung that sets or applies the clock moves its base inside the window.
    if any(w.startswith("rtc.") for node in nodes for w in node.all_writes):
        return None

    tags = dict(pdg.tags)
    always = _always_reached_subroutines(pdg, program)
    rungs = [resolve_rung(program, node) for node in nodes]
    tops = [_top_rung(program, node) for node in nodes]

    def recompute_shape(i: int) -> bool:
        node, rung, top = nodes[i], rungs[i], tops[i]
        if rung is None or top is None or node.branch_path or node.calls or not pos[i]:
            return False
        if node.subroutine is not None and node.subroutine not in always:
            return False
        if top._use_prior_snapshot or node.condition_reads or rung._conditions:
            return False
        return bool(rung._instructions) and not rung._branches

    candidates = [i for i in range(len(nodes)) if recompute_shape(i)]
    # Grow the mirror set from the clock through recompute rungs, tracking each
    # mirror's value range so no recompute can clamp, wrap or fault.
    ranges: dict[str, _Span] = {
        name: (float(lo), float(hi)) for name, (lo, hi) in rtc_ranges.items()
    }
    theta: set[str] = set(rtc)
    recompute: set[int] = set()
    changed = True
    while changed:
        changed = False
        for i in candidates:
            rung = rungs[i]
            if i in recompute or rung is None or not reads[i] & theta:
                continue
            spans: dict[str, _Span] = {}
            for instr in rung._instructions:
                result = _recompute_range(instr, ranges, tags)
                if result is None:
                    spans = {}
                    break
                dest, span = result
                lo, hi = spans.get(dest, span)
                spans[dest] = (min(lo, span[0]), max(hi, span[1]))
            if not spans:
                continue
            recompute.add(i)
            for dest, span in spans.items():
                prior = ranges.get(dest)
                ranges[dest] = (
                    span if prior is None else (min(prior[0], span[0]), max(prior[1], span[1]))
                )
            theta |= nodes[i].writes
            changed = True
    names = frozenset(theta - rtc)
    if not names:
        return None
    opaque = _opaque_indirect_tags(pdg)
    if names & (target_names | protected | coordinates | (excluded - churn) | opaque):
        return None
    # Every writer of a mirror is a recompute rung, and every recompute rung
    # still fits its destination under the final (widest) mirror ranges.
    for name in names:
        if not pdg.writers_of.get(name, frozenset()) <= recompute:
            return None
    for i in recompute:
        rung = rungs[i]
        assert rung is not None
        if any(_recompute_range(instr, ranges, tags) is None for instr in rung._instructions):
            return None
    # A recompute rung reads only the clock, mirrors, guarded tags and fixed
    # configuration: those hold their stepped values entering the landing scan.
    lagging = (
        excluded | opaque | (frozenset(SYSTEM_TAGS_BY_NAME) - rtc - {_system.sys.always_on.name})
    ) - theta
    for i in recompute:
        if _node_reads(nodes[i]) & lagging:
            return None

    # Readers strictly after writers (effective reads: a subroutine rung counts
    # its callers' reads at its callers' positions).
    for tag in names:
        writer_last = max(
            (max(pos[i]) for i, node in enumerate(nodes) if tag in node.all_writes and pos[i]),
            default=None,
        )
        reader_first = min(
            (min(pos[i]) for i in range(len(nodes)) if tag in reads[i] and pos[i]),
            default=None,
        )
        if writer_last is not None and reader_first is not None and writer_last >= reader_first:
            return None

    dead: list[int] = []
    oneshot_nodes: set[int] = set()
    for i, node in enumerate(nodes):
        if node.condition_reads & theta:
            return None  # an enable reads the clock: any tick may matter
        if i in recompute or not (reads[i] & theta) or not pos[i]:
            continue
        rung, top = rungs[i], tops[i]
        if rung is None or top is None:
            return None
        all_oneshot = True
        stack: list[tuple[Any, bool]] = [(instr, False) for instr in rung._instructions]
        while stack:
            instr, in_loop = stack.pop()
            if isinstance(instr, ForLoopInstruction):
                if _instruction_reads(instr, tags) & theta:
                    return None
                stack.extend((child, True) for child in instr.instructions)
                continue
            if not _instruction_reads(instr, tags) & theta:
                continue
            if not _inert_when_off(instr):
                return None
            if in_loop or not getattr(instr, "oneshot", False):
                all_oneshot = False
        dead.append(i)
        if all_oneshot and node.scope == "main" and not top._use_prior_snapshot:
            oneshot_nodes.add(i)

    writers: dict[str, set[int]] = {}
    for i, node in enumerate(nodes):
        if pos[i]:
            for w in node.all_writes:
                writers.setdefault(w, set()).add(i)
    block_slots: dict[str, list[str]] = {}
    for name, tag in tags.items():
        block = getattr(getattr(tag, "_pyrung_block", None), "name", None)
        if block is not None:
            block_slots.setdefault(block, []).append(name)
    for ref in pdg.indirect_writes:
        if pos[ref.node_index]:
            for name in block_slots.get(ref.block.name, ()):
                writers.setdefault(name, set()).add(ref.node_index)

    indirect_slots: dict[int, set[str]] = {}
    for ref in pdg.indirect_writes:
        indirect_slots.setdefault(ref.node_index, set()).update(block_slots.get(ref.block.name, ()))

    silent: list[frozenset[str]] = []
    for i, rung in enumerate(rungs):
        loud: set[str] = set()
        quiet: set[str] = set()
        for instr in _nested_instructions(rung._instructions) if rung is not None else ():
            dests, _target_reads, implicit = _extract_instruction_writes(instr, tags)
            if _silent_when_off(instr):
                quiet |= dests | implicit
            else:
                # An instruction that acts while unpowered may own an indirect
                # write the PDG could not name, so it keeps every slot loud.
                loud |= dests | implicit | indirect_slots.get(i, set())
        if i in indirect_slots and not loud:
            quiet |= indirect_slots[i]
        silent.append(frozenset(quiet - loud))

    callers: dict[str, list[int]] = {}
    for site in pdg.call_sites():
        callers.setdefault(site.callee, []).append(site.node_index)

    judgeable_system = {_system.sys.always_on.name, _system.sys.first_scan.name}
    unjudgeable = theta | (frozenset(SYSTEM_TAGS_BY_NAME) - judgeable_system) | excluded | protected
    return _TimeMirrors(
        names=names,
        dead_nodes=tuple(dead),
        oneshot_nodes=frozenset(oneshot_nodes),
        conditions=tuple(
            None
            if rung is None or top is None or top._use_prior_snapshot
            else tuple(rung._conditions)
            for rung, top in zip(rungs, tops, strict=True)
        ),
        spans=tuple((min(p), max(p)) if p else None for p in pos),
        main_entry=tuple(node.scope == "main" for node in nodes),
        node_sub=tuple(node.subroutine for node in nodes),
        callers={sub: tuple(sites) for sub, sites in callers.items()},
        writers={tag: tuple(sorted(idx)) for tag, idx in writers.items()},
        silent_writes=tuple(silent),
        unjudgeable=frozenset(unjudgeable),
    )


def _timer_contests(
    pdg: ProgramGraph,
    program: Any,
    sources: Sequence[_AccSource],
) -> frozenset[str]:
    """Timed accumulators a scan may write other than by one timing instruction, once.

    The probe reads a timer's ``(acc, frac)`` only at scan boundaries, and the
    landing scan runs every timing instruction once at the aggregate dt.  Both
    are exact only while one execution of one timing instruction is the
    accumulator's only write in a scan.  Otherwise a scan can reset the timer
    and time it again (a second timer instruction on the same Timer, a
    ``copy(0, Tmr.Acc)``, a For loop, a second call into the timer's
    subroutine): the boundary readings stay put while the landing times the
    whole window in one step.  A live run counts each scan's writes instead
    (see :meth:`_OrdinaryFoldStrategy.capture`); this static answer is for
    executors that cannot count them.
    """
    from pyrung.core.analysis.pdg import _extract_instruction_writes, resolve_rung
    from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction

    registry = _ensure_registry()
    nodes = pdg.rung_nodes
    tags = dict(pdg.tags)
    block_of = {
        name: block.name
        for name, tag in tags.items()
        if (block := getattr(tag, "_pyrung_block", None)) is not None
    }
    sites_of: dict[str, list[Any]] = {}
    for site in pdg.call_sites():
        sites_of.setdefault(site.callee, []).append(site)

    def walk(instrs: Sequence[Any], in_loop: bool) -> Iterator[tuple[Any, bool]]:
        for instr in instrs:
            yield instr, in_loop
            children = getattr(instr, "instructions", None)
            if children:
                yield from walk(children, in_loop or isinstance(instr, ForLoopInstruction))

    def once_a_scan(i: int, seen: frozenset[int] = frozenset()) -> bool:
        node = nodes[i]
        if node.scope == "main":
            return True
        sites = sites_of.get(node.subroutine or "", [])
        if not sites:
            return True  # never called: never runs
        if len(sites) > 1 or i in seen:
            return False
        caller = sites[0].node_index
        rung = resolve_rung(program, nodes[caller])
        if rung is None or not any(
            isinstance(instr, CallInstruction) and instr.subroutine_name == node.subroutine
            for instr in rung._instructions
        ):
            return False  # the one call sits in a loop body
        return once_a_scan(caller, seen | {i})

    contested: set[str] = set()
    for src in sources:
        if not src.timed:
            continue
        acc = src.acc_name
        block = block_of.get(acc)
        writers = set(pdg.writers_of.get(acc, ()))
        writers |= {ref.node_index for ref in pdg.indirect_writes if ref.block.name == block}
        if not writers:
            continue  # a plant timer, or one no rung reaches
        timing = 0
        sole = True
        for i in writers:
            rung = resolve_rung(program, nodes[i])
            for instr, in_loop in walk(rung._instructions if rung is not None else (), False):
                if type(instr) in registry and any(
                    phase_acc.name == acc
                    for phase_acc, _done, _preset in _instruction_phases(instr)
                ):
                    timing += 1
                    sole = sole and not in_loop and once_a_scan(i)
        if timing != 1 or not sole or len(writers) != 1:
            contested.add(acc)
            continue
        (i,) = writers
        rung = resolve_rung(program, nodes[i])
        if any(ref.node_index == i for ref in pdg.indirect_writes):
            contested.add(acc)
            continue
        for instr, _in_loop in walk(rung._instructions if rung is not None else (), False):
            if type(instr) in registry:
                continue
            dests, _target_reads, implicit = _extract_instruction_writes(instr, tags)
            if acc in dests or acc in implicit:
                contested.add(acc)
                break
    return frozenset(contested)


class _OffJudge:
    """Decides, from scan-boundary tag maps, which rungs are unpowered in a window.

    A condition read is judged only when its value is fixed at the reading
    rung's position in every scan of the window: the tag is judgeable, holds
    one value in every supplied map, and its writers (other than writers that
    are themselves off and write nothing while off) all sit on one side of the
    rung.  With writers on both sides, the value seen mid-scan could differ
    from the stored one.
    """

    def __init__(
        self,
        tm: _TimeMirrors,
        tag_maps: Sequence[Any],
        forced: frozenset[str] = frozenset(),
    ) -> None:
        self.tm = tm
        self.maps = tag_maps
        # A forced tag is re-applied after logic, so its stored value is not
        # what a rung between a mid-scan writer and the force saw.
        self.forced = forced
        # Nodes proven off, and nodes some judgement asked about.  Proofs only
        # ever use nodes already proven, so every proof is well founded; the
        # loop in :meth:`all_inactive` repeats until no new proof appears.
        self.proven: set[int] = set()
        self.asked: set[int] = set()

    def _is_off(self, i: int) -> bool:
        if i in self.proven:
            return True
        self.asked.add(i)
        return False

    def _one_side(self, node: int, writers: Sequence[int]) -> bool:
        tm = self.tm
        here = tm.spans[node]
        if here is None:
            return False
        before = after = False
        for w in writers:
            span = tm.spans[w]
            if span is None:
                continue
            lo, hi = span
            if hi < here[0]:
                before = True
            elif lo > here[1] or (tm.main_entry[node] and lo >= here[0]):
                # Main-rung conditions are judged at rung entry: every write at
                # the same position (the rung, its branches, its callees) is later.
                after = True
            else:
                return False
            if before and after:
                return False
        return True

    def stable(self, name: str, node: int) -> bool:
        tm = self.tm
        if name in tm.unjudgeable or name in self.forced:
            return False
        writers = tm.writers.get(name, ())
        if self._one_side(node, writers):
            return True
        live = [w for w in writers if not (name in tm.silent_writes[w] and self._is_off(w))]
        return len(live) < len(writers) and self._one_side(node, live)

    def value(self, name: str, default: Any, node: int) -> Any:
        if name == _ALWAYS_ON_NAME:
            return True
        if name == _FIRST_SCAN_NAME:
            # Window scans follow a genuine probe scan, so scan_id >= 1.
            return False
        if not self.stable(name, node):
            raise _Unjudgeable(name)
        first = self.maps[0].get(name, default)
        for tags in self.maps[1:]:
            if tags.get(name, default) != first:
                raise _Unjudgeable(name)
        return first

    def enable(self, i: int) -> bool | None:
        """The node's own enable: ``True``/``False`` when judged, else ``None``."""
        conds = self.tm.conditions[i]
        if conds is None:
            return None
        view = _OffView(self, i)
        results = [_judge_condition(cond, view) for cond in conds]
        if any(r is False for r in results):
            return False
        return True if all(r is True for r in results) else None

    def _judge_off(self, i: int) -> bool:
        if self.enable(i) is False:
            return True
        sub = self.tm.node_sub[i]
        if sub is None:
            return False
        return all([self._is_off(s) for s in self.tm.callers.get(sub, ())])

    def all_inactive(self, nodes: Sequence[int]) -> bool:
        """Whether no node in *nodes* acts on the clock in any window scan."""
        oneshot = self.tm.oneshot_nodes
        pending = [i for i in nodes if i not in oneshot or self.enable(i) is None]
        self.asked.update(pending)
        while True:
            asked_before = len(self.asked)
            progress = False
            for i in sorted(self.asked - self.proven):
                if self._judge_off(i):
                    self.proven.add(i)
                    progress = True
            if all(i in self.proven for i in pending):
                return True
            if not progress and len(self.asked) == asked_before:
                return False


class _OffView:
    """Condition view for one rung position, backed by an :class:`_OffJudge`."""

    def __init__(self, judge: _OffJudge, node: int) -> None:
        self._judge = judge
        self._node = node

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._judge.value(name, default, self._node)

    def __getattr__(self, name: str) -> Any:
        raise _Unjudgeable(name)


def _judge_condition(cond: Any, view: _OffView) -> bool | None:
    """Three-valued evaluation of *cond* over *view*: ``None`` when not judgeable."""
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        FallingEdgeCondition,
        IntTruthyCondition,
        NormallyClosedCondition,
        RisingEdgeCondition,
    )
    from pyrung.core.system_points import _DERIVED_EDGE_TAGS
    from pyrung.core.tag import ImmediateRef

    if isinstance(cond, AllCondition):
        results = [_judge_condition(c, view) for c in cond.conditions]
        if any(r is False for r in results):
            return False
        return True if all(r is True for r in results) else None
    if isinstance(cond, AnyCondition):
        results = [_judge_condition(c, view) for c in cond.conditions]
        if any(r is True for r in results):
            return True
        return False if all(r is False for r in results) else None
    if isinstance(cond, (RisingEdgeCondition, FallingEdgeCondition)):
        # An edge of a value fixed across the probe and the window never fires
        # in a window scan: the probe left every edge site's memory as it found
        # it (``_OrdinaryFoldStrategy.plan`` declines otherwise), so each site saw the
        # value it remembered, and sees it again.
        if cond._resolved_tag.name in _DERIVED_EDGE_TAGS or isinstance(cond.tag, ImmediateRef):
            return None
        try:
            view.get_tag(cond._resolved_tag.name, False)
        except _Unjudgeable:
            return None
        return False
    if isinstance(cond, (BitCondition, NormallyClosedCondition)):
        if isinstance(cond.tag, ImmediateRef):
            return None
    elif not isinstance(
        cond,
        (
            IntTruthyCondition,
            CompareEq,
            CompareNe,
            CompareLt,
            CompareLe,
            CompareGt,
            CompareGe,
        ),
    ):
        return None
    try:
        return bool(cond.evaluate(view))  # type: ignore[arg-type]
    except _Unjudgeable:
        return None
    except Exception:  # noqa: BLE001 - an unexpected read shape is not judgeable
        return None


def _time_mirrors_off(
    tm: _TimeMirrors,
    tag_maps: Sequence[Any],
    forced: frozenset[str] = frozenset(),
) -> bool:
    """Whether no rung in ``tm.dead_nodes`` acts on the clock in the window.

    *tag_maps* are scan-boundary tag maps of the window (one plateau state, or
    every snapshot of an observed cycle).  A rung is off when one of its own
    conditions is definitely False, or when it is a subroutine rung and every
    call site is off.  A one-shot-only reader is inactive while its enable is
    judged either way.  Anything not judgeable counts as possibly active,
    including every tag in *forced* (forces re-apply after logic, so a forced
    tag's stored value is not what a mid-scan reader saw).
    """
    return _OffJudge(tm, tag_maps, forced).all_inactive(tm.dead_nodes)


def _rtc_hard_fields(
    ctx: _FoldContext,
    state: Any,
    tag_maps: Sequence[Any],
    forced: Collection[str] | None,
) -> frozenset[str]:
    """The RTC fields whose ticks bound this window.

    Every read field, unless the time-mirror proof holds over *tag_maps* (see
    :class:`_TimeMirrors`); then only the fields the run_until condition reads.
    *forced* names the executor's forced tags, which the proof may not read;
    ``None`` (unknown) keeps every field hard, as does a pending
    ``rtc.apply_*``.
    """
    tm = ctx.time_mirrors
    if tm is None or not ctx.rtc_fields or forced is None:
        return ctx.rtc_fields
    tags = state.tags
    if any(tags.get(name) for name in _rtc_apply_names()):
        return ctx.rtc_fields
    if not _time_mirrors_off(tm, tag_maps, frozenset(forced)):
        return ctx.rtc_fields
    return ctx.rtc_condition_fields


def _without_soft_rtc_ticks(ctx: _FoldContext) -> _FoldContext:
    """*ctx* with every RTC tick hard and every clock copy in the plateau guard.

    For a stop predicate whose reads are not declared (a callable): it may read
    a clock copy, which a soft tick would let change unobserved.
    """
    if ctx.time_mirrors is None:
        return ctx
    from dataclasses import replace

    return replace(ctx, time_mirrors=None)


def _window_constant_names() -> frozenset[str]:
    """Resolved-on-read system names that cannot move inside a fold window.

    Fixed configuration, plus the system clocks, whose edges the fold bounds
    on its own (``_scans_to_clock_edge``), plus ``first_scan``.  Every other
    derived name can change between two scans of a window: the scan-id
    signals, the scan-time and ``dt`` readouts (which the one landing scan
    inflates), the RTC fields and the SD status.

    ``first_scan`` resolves as ``scan_id == 0``.  Every fold is certified
    after a genuine probe scan (``_FoldPlan``), so its window starts at
    ``scan_id >= 1``; and :meth:`_OrdinaryFoldStrategy.plan` declines a probe
    that itself ran at scan 0 when the program reads ``first_scan``
    (``_FoldContext.reads_first_scan``).  The probe and every scan of the
    window therefore read ``first_scan`` as False.
    """
    from pyrung.core.system_points import _CLOCK_HALF_PERIODS
    from pyrung.core.system_points import system as _system

    sys_ = _system.sys
    firmware = _system.firmware
    return frozenset(_CLOCK_HALF_PERIODS) | {
        sys_.always_on.name,
        sys_.first_scan.name,
        sys_.mode_switch_run.name,
        sys_.mode_run.name,
        sys_.fixed_scan_mode.name,
        sys_.battery_present.name,
        sys_.scan_time_fixed_setup_ms.name,
        sys_.interrupt_scan_time_ms.name,
        firmware.main_ver_low.name,
        firmware.main_ver_high.name,
        firmware.sub_ver_low.name,
        firmware.sub_ver_high.name,
    }


def _opaque_indirect_tags(pdg: ProgramGraph) -> frozenset[str]:
    """Block slots some rung may read or write through an unbounded pointer.

    The PDG cannot name the element an unbounded ``block[ptr]`` touches, so its
    reader and writer sets for that block are incomplete.  Every known slot of
    such a block is reported here so the frozen-write pass can refuse it.
    """
    opaque_blocks: set[str] = set(pdg.opaque_index_blocks)
    for use in pdg.pointer_uses:
        ptr = pdg.tags.get(use.pointer)
        if ptr is None or (
            getattr(ptr, "choices", None) is None
            and (getattr(ptr, "min", None) is None or getattr(ptr, "max", None) is None)
        ):
            opaque_blocks.add(use.block)
    for ref in pdg.indirect_writes:
        opaque_blocks.add(ref.block.name)
    if not opaque_blocks:
        return frozenset()
    return frozenset(
        name
        for name, tag in pdg.tags.items()
        if getattr(getattr(tag, "_pyrung_block", None), "name", None) in opaque_blocks
    )


def _node_scan_context(
    pdg: ProgramGraph,
) -> tuple[list[frozenset[str]], list[frozenset[int]]]:
    """Per-node effective reads and main-scan positions.

    A subroutine rung runs only when its caller's rung calls it, so it inherits
    every read of its call sites (transitively), and it executes at those call
    sites' main rung positions.  A subroutine that no main rung reaches gets no
    position.
    """
    nodes = pdg.rung_nodes
    own = [_node_reads(node) for node in nodes]
    sub_members: dict[str, list[int]] = {}
    for i, node in enumerate(nodes):
        if node.subroutine is not None:
            sub_members.setdefault(node.subroutine, []).append(i)
    callers: dict[str, list[int]] = {}
    for site in pdg.call_sites():
        callers.setdefault(site.callee, []).append(site.node_index)

    reads = list(own)
    pos: list[frozenset[int]] = [
        frozenset({node.rung_index}) if node.scope == "main" else frozenset() for node in nodes
    ]
    changed = True
    while changed:
        changed = False
        for sub, members in sub_members.items():
            inherited_reads: set[str] = set()
            inherited_pos: set[int] = set()
            for ci in callers.get(sub, ()):
                inherited_reads |= reads[ci]
                inherited_pos |= pos[ci]
            for i in members:
                r = own[i] | inherited_reads
                p = frozenset(inherited_pos)
                if r != reads[i] or p != pos[i]:
                    reads[i] = r
                    pos[i] = p
                    changed = True
    return reads, pos


def _frozen_rung_writes(
    pdg: ProgramGraph,
    base_varying: frozenset[str],
    window_constant: frozenset[str],
    target_names: frozenset[str],
    protected: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Tags whose value is rebuilt, identically, inside every scan of a plateau.

    Excluding such a tag from the plateau guard lets the fold skip through
    clock-gated recomputations that would otherwise break the window.  The
    exclusion is sound only when the landing scan recomputes the tag's exact
    stepped value from window-constant inputs, so a tag is frozen only when:

    * no varying input reaches its writers.  *varying* is seeded with
      *base_varying* (accumulators, churn, mirrors, scan-varying system names)
      and propagated through rung read->write edges to a fixed point;
    * every rung that reads it runs strictly after every rung that writes it.
      A read of the scan-entry value (a self-read like ``calc(f(X), X)``, a
      seal-in, or a rung reading a tag written further down the scan) makes the
      value depend on the previous scan, which the one landing scan cannot
      replay;
    * it is not a target, not *protected*, and not a slot an unbounded pointer
      may reach.

    *window_constant* names are stripped from reads: they are resolved on read,
    never stored, and cannot move inside a fold window.  Anything uncertain
    keeps the tag in the guard, where a change it makes declines the fold.
    """
    reads, pos = _node_scan_context(pdg)
    reads = [r - window_constant for r in reads]
    nodes = pdg.rung_nodes

    varying: set[str] = set(base_varying)
    changed = True
    while changed:
        changed = False
        for i, node in enumerate(nodes):
            if not reads[i] & varying:
                continue
            for w in node.all_writes:
                if w not in varying:
                    varying.add(w)
                    changed = True

    writer_pos: dict[str, set[int]] = {}
    unplaced: set[str] = set()
    for i, node in enumerate(nodes):
        for w in node.all_writes:
            writer_pos.setdefault(w, set()).update(pos[i])
            if not pos[i]:
                unplaced.add(w)
    reader_min: dict[str, int] = {}
    for i in range(len(nodes)):
        if not pos[i]:
            continue
        first = min(pos[i])
        for r in reads[i]:
            if r not in reader_min or first < reader_min[r]:
                reader_min[r] = first

    excluded = set(varying) | target_names | protected | _opaque_indirect_tags(pdg) | unplaced
    frozen: set[str] = set()
    for node in nodes:
        for w in node.writes:
            if w in excluded or w in frozen:
                continue
            written_at = writer_pos.get(w)
            if not written_at:
                continue
            first_read = reader_min.get(w)
            if first_read is not None and max(written_at) >= first_read:
                continue
            frozen.add(w)
    return frozenset(frozen)


# ── 7. Self-calc sources ────────────────────────────────────────────


def _selfcalc_sources(
    plc: PLC,
    pdg: ProgramGraph,
    program: Any,
    target_names: frozenset[str],
) -> list[tuple[str, int | str, int | None, tuple[int, int]]]:
    """Affine(-mod) self-calc churners eligible as exact fold sources.

    Each is ``(tag, c, m, (lo, hi))``: one unconditional ``calc`` advances
    ``tag`` by ``c`` per scan (modulo ``m`` when given) into an integer
    destination that wraps at ``lo..hi``.  ``c`` is a literal, or, for a
    ``tag`` nothing else reads, the name of a tag no rung writes (see
    :func:`_match_affine_selfcalc`).
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.conversions import calc_store_transform
    from pyrung.core.system_points import _DERIVED_TAG_NAMES
    from pyrung.core.tag import TagType

    harness_names = _harness_referenced_names(plc)
    integer_types = (TagType.INT, TagType.DINT, TagType.WORD)

    def constant_tag(name: str) -> bool:
        # No rung writes it, so it holds still between patches, and no fold
        # window contains a patch.
        tag_obj = pdg.tags.get(name)
        return (
            not pdg.writers_of.get(name)
            and name not in harness_names
            and name not in _DERIVED_TAG_NAMES
            and not name.startswith(("sys.", "rtc.", "fault.", "_"))
            and getattr(tag_obj, "type", None) in integer_types
        )

    out: list[tuple[str, int | str, int | None, tuple[int, int]]] = []
    for tag, writers in pdg.writers_of.items():
        if tag in harness_names or tag in target_names:
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
        calc_instr = calc_writers[0]
        if getattr(calc_instr, "oneshot", False):
            continue
        dest_type = getattr(calc_instr.dest, "type", None)
        if dest_type not in integer_types:
            continue  # a REAL self-calc rounds per scan: no exact closed form
        storage = calc_store_transform(dest_type, calc_instr.mode)
        if storage is None or storage.kind != "wrap":
            continue
        if storage.lower is None or storage.upper is None:
            continue
        # A tag step only for a tag nothing else reads: the crossing set models
        # literal comparisons on a source, not a moving preset, a comparison
        # against another moving tag, an expression or an index, and a tag
        # step that dev left guarded must not start relying on it.
        unread = pdg.all_readers_of.get(tag, frozenset()) <= writers
        matched = _match_affine_selfcalc(
            calc_instr.expression, tag, constant_tag if unread else None
        )
        if matched is not None:
            out.append((tag, matched[0], matched[1], (storage.lower, storage.upper)))
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

    # Self-feeding churn (a calc reading its own destination) is never simply
    # left out of the plateau guard: the landing must still hold the value
    # stepping reaches.  An exact affine(-mod) self-calc becomes a fold source
    # below, patched forward in closed form; any other stays guarded, so a
    # window it moves in is not folded.  Only scan-local terminals, rebuilt
    # from the landing scan's own inputs, are excluded.
    churn_excluded: frozenset[str] = frozenset()

    if advice is None or advice.has("fold_unread_churn"):
        terminals = _scan_local_terminal_tags(plc, pdg, target_names) - acc_names
        churn_excluded |= terminals
        if terminals and journal is not None:
            journal.add_note(
                "fold: scan-local terminal outputs excluded from plateau guard: "
                + ", ".join(sorted(terminals))
            )

    modwrap: list[_ModWrap] = []
    if advice is None or advice.has("fold_modwrap_source"):
        tracked: list[str] = []
        for tag, c, m, value_range in _selfcalc_sources(plc, pdg, program, target_names):
            if tag in acc_names or tag in churn_excluded:
                continue
            if m is None:
                sources.append(
                    _AccSource(
                        acc_name=tag,
                        done_bit=f"__selfcalc:{tag}",
                        preset=0,
                        kind="down" if isinstance(c, int) and c < 0 else "up",
                        timed=False,
                        # A tag step's sign is only known at runtime.
                        bidir=not isinstance(c, int),
                        step=c,
                        value_range=value_range,
                    )
                )
                acc_names |= {tag}
            else:
                assert isinstance(c, int)
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
    from pyrung.core.system_points import scan_derived_names as _scan_derived
    from pyrung.core.system_points import system as _sys_points

    # Likewise ``sys.dt`` and ``sys.scan_time_current_ms`` read mid-scan: the
    # landing scan runs with the aggregate dt, so a rung reading either would
    # see it.  Plant rungs read ``sys.dt`` on purpose (a ramp advances by the
    # elapsed time); any other reader disables the fold.
    landing_inflated = frozenset(
        {_sys_points.sys.dt.name, _sys_points.sys.scan_time_current_ms.name}
    )
    syn_rungs = getattr(plc, "_synthesis", None)
    plant_ids = (
        {id(rung) for rung in syn_rungs.all_rungs()}
        if syn_rungs is not None and not syn_rungs.is_empty()
        else set()
    )
    node_derived: set[str] = set()
    for node in pdg.rung_nodes:
        node_reads = _node_reads(node)
        node_derived |= _scan_derived(node_reads)
        inflated = node_reads & landing_inflated
        if inflated:
            top = _top_rung(program, node)
            if top is None or id(top) not in plant_ids:
                node_derived |= inflated
    scan_derived_names = _scan_derived(read_tags) | frozenset(node_derived) | condition_scan_derived
    if scan_derived_names and journal is not None:
        journal.add_note(
            "fold: disabled by read scan-id-derived or per-scan dt signal(s): "
            + ", ".join(sorted(scan_derived_names))
        )

    mirror_names: set[str] = set()
    mirror_cmp_keys: dict[str, set[tuple[str, Any]]] = {}
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
            mirror_cmp_keys[m] = set(cmps)
        comparisons = merged
        if mirror_names and journal is not None:
            journal.add_note(
                "fold: affine acc-view thresholds translated onto their sources: "
                + ", ".join(sorted(mirror_names))
            )
    mirror_sources = tuple(
        sorted((m, a) for m, a, _scale, _offset in mirror_cands if m in mirror_names)
    )

    # Every read of a moving coordinate must be one the crossing set places.
    view_of = dict(mirror_sources)
    modeled_writes: dict[str, frozenset[str]] = {
        s.acc_name: frozenset({s.acc_name}) for s in sources if s.step is not None
    }
    modeled_writes.update({name: frozenset({name}) for name in modwrap_names})
    modeled_writes.update({m: frozenset({a}) for m, a in mirror_sources})
    protected_names = target_names | _harness_referenced_names(plc)

    def unread(name: str) -> bool:
        return name not in protected_names and not pdg.all_readers_of.get(name)

    dormant: dict[str, list[frozenset[str] | None]] = {}
    unmodeled_raw, rung_cmps, instr_cmps, landing_rebuilt = _audit_coordinate_reads(
        pdg,
        program,
        acc_names | modwrap_names | frozenset(mirror_names),
        modeled_writes,
        unread,
        dormant,
    )
    unmodeled_set = set(unmodeled_raw)
    found: dict[str, list[tuple[str, Any]]] = {}
    for table in (rung_cmps, instr_cmps):
        for name, items in table.items():
            if name in view_of:
                # A view's thresholds were projected from the rung scan; any
                # other comparison on it was not.
                if any(item not in mirror_cmp_keys.get(name, ()) for item in items):
                    unmodeled_set.add(name)
                continue
            found.setdefault(name, []).extend(items)
    comparisons = _merge_comparisons(comparisons, found)
    unmodeled = frozenset(view_of.get(name, name) for name in unmodeled_set)
    if unmodeled and journal is not None:
        journal.add_note(
            "fold: windows bounded at each value change of coordinates read "
            "outside a modeled comparison: " + ", ".join(sorted(unmodeled))
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
        pdg, clock_disqualifying, acc_names - unmodeled
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
        window_constant = _window_constant_names()
        base_varying = (
            acc_names
            | modwrap_names
            | frozenset(mirror_names)
            | profile_fb_names
            | churn_excluded
            | (frozenset(_DERIVED_TAG_NAMES) - window_constant)
        )
        frozen_writes = _frozen_rung_writes(
            pdg,
            base_varying,
            window_constant,
            target_names,
            protected=_harness_referenced_names(plc),
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
        syn_names: set[str] = set()
        for src in _collect_acc_sources(syn_program):
            if src.acc_name in acc_names:
                continue
            sources.append(src)
            acc_names = acc_names | {src.acc_name}
            syn_names.add(src.acc_name)
        if syn_names:
            syn_unmodeled, syn_rung, syn_instr, syn_rebuilt = _audit_coordinate_reads(
                pdg, program, frozenset(syn_names), {}, unread
            )
            unmodeled = unmodeled | syn_unmodeled
            landing_rebuilt = landing_rebuilt | syn_rebuilt
            comparisons = _merge_comparisons(comparisons, syn_rung)
            comparisons = _merge_comparisons(comparisons, syn_instr)

    first_scan_name = _system.sys.first_scan.name
    reads_first_scan = any(first_scan_name in _node_reads(node) for node in pdg.rung_nodes)

    rtc_condition_fields = target_names & frozenset(_rtc_field_units())
    rtc_fields = _rtc_read_fields(pdg) | rtc_condition_fields
    if rtc_fields and journal is not None:
        journal.add_note("fold: bounded by RTC field ticks: " + ", ".join(sorted(rtc_fields)))

    time_mirrors: _TimeMirrors | None = None
    if rtc_fields and (advice is None or advice.has("fold_frozen_writes")):
        time_mirrors = _time_mirror_analysis(
            pdg,
            program,
            coordinates=acc_names | modwrap_names | frozenset(mirror_names) | profile_fb_names,
            excluded=(
                acc_names
                | modwrap_names
                | frozenset(mirror_names)
                | profile_fb_names
                | churn_excluded
                | frozen_writes
            ),
            target_names=target_names,
            protected=_harness_referenced_names(plc),
            churn=churn_excluded,
        )
        if time_mirrors is not None and journal is not None:
            journal.add_note(
                "fold: RTC copies rebuilt by the landing scan, ticks soft while their "
                "readers are off: " + ", ".join(sorted(time_mirrors.names))
            )

    # A dormant read stays dormant only while each of its gates is a tag the
    # plateau guard checks (see _audit_coordinate_reads).
    unguarded = (
        acc_names
        | profile_fb_names
        | churn_excluded
        | modwrap_names
        | frozenset(mirror_names)
        | frozen_writes
        | landing_rebuilt
        | (time_mirrors.names if time_mirrors is not None else frozenset())
        | frozenset(_DERIVED_TAG_NAMES)
    )
    woken = {
        view_of.get(name, name)
        for name, sites in dormant.items()
        if any(gates is not None and gates <= unguarded for gates in sites)
    }
    unmodeled = unmodeled | frozenset(woken)

    contested_timers = _timer_contests(pdg, program, sources)
    if contested_timers and journal is not None:
        journal.add_note(
            "fold: timers another instruction also writes, folded only while a scan "
            "writes each once: " + ", ".join(sorted(contested_timers))
        )

    return _FoldContext(
        sources=tuple(sources),
        acc_names=acc_names,
        comparisons=comparisons,
        read_done=frozenset(
            s.done_bit
            for s in sources
            if s.done_bit in read_tags
            or s.done_bit in target_names
            or pdg.all_readers_of.get(s.done_bit)
        ),
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
        reads_first_scan=reads_first_scan,
        rtc_fields=rtc_fields,
        rtc_condition_fields=rtc_condition_fields,
        time_mirrors=time_mirrors,
        unmodeled=unmodeled,
        mirror_sources=mirror_sources,
        landing_rebuilt=landing_rebuilt,
        contested_timers=contested_timers,
        clock_edge_memory=every_scan_clock_edge_keys(getattr(plc, "_site_table", None)),
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


def _condition_read_audit(
    conditions: Collection[Any], ctx: _FoldContext
) -> tuple[frozenset[str], dict[str, tuple[tuple[str, Any], ...]]]:
    """How stop conditions read the fold's coordinates.

    Returns the sources some condition reads outside a comparison (see
    :func:`_audit_condition`), and the comparisons found, to merge into the
    caller's crossings.
    """
    if not ctx.coordinates:
        return frozenset(), {}
    tags: dict[str, Any] = {}
    cmps: dict[str, list[tuple[str, Any]]] = {}
    unmodeled: set[str] = set()
    for condition in conditions:
        _audit_condition(condition, ctx.coordinates, tags, cmps, unmodeled)
    # A comparison on a view was not projected onto its source: unmodeled.
    views = dict(ctx.mirror_sources)
    unmodeled |= {name for name in cmps if name in views}
    found = {name: items for name, items in cmps.items() if name not in views}
    return ctx.unmodeled_coordinates(unmodeled), _merge_comparisons({}, found)


def _step_without_preset(value: Any, state: Any) -> bool:
    """True on a time & event drum step that has no preset.

    Only its event ends such a step, and the event is a condition read whose
    change bounds the window like any other: the timer has no crossing to place.
    """
    if not isinstance(value, _StepPreset):
        return False
    step = state.tags.get(value.step_name)
    return (
        isinstance(step, int)
        and not isinstance(step, bool)
        and 1 <= step <= len(value.values)
        and value.values[step - 1] is None
    )


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


def _progress_bounds(kind: str, form: str, k: float) -> tuple[tuple[float, bool], ...]:
    """Every ``(target, strict)`` boundary where ``acc <form> k`` can flip.

    A monotone accumulator flips an ordering comparison once; ``eq``/``ne``
    flip twice, on reaching ``k`` and on passing it.  A step below one unit
    (a slow timer) sits at ``k`` for many scans, so the fall is a crossing of
    its own.
    """
    target, strict = _progress_bound(kind, form, k)
    if form in ("eq", "ne"):
        return ((target, False), (target, True))
    return ((target, strict),)


def _scans_to_cross(pa: float, delta: float, target: float, strict: bool) -> int | None:
    """Scans for progress (at ``pa``, +``delta``/scan) to cross ``target``."""
    if strict:
        if pa > target:
            return None
        return math.floor((target - pa) / delta) + 1
    if pa >= target:
        return None
    return max(1, math.ceil((target - pa) / delta))


def _crossed_between(before: float, after: float, target: float, strict: bool) -> bool:
    """Whether the probe scan, moving a coordinate *before* -> *after*, crossed ``target``.

    A rung above the moving instruction read *before* during the probe and
    reads *after* on the next scan, so a crossing inside the probe still
    flips that rung one scan later: no window may start there.
    """
    if strict:
        return (before > target) != (after > target)
    return (before >= target) != (after >= target)


def _comparisons_saturated(progress: float, kind: str, cmps: Sequence[tuple[str, float]]) -> bool:
    """True iff every comparison in *cmps* has made its final transition already.

    A monotone source at *progress* (the ``_acc_totals`` value — count-down
    accumulators negated, so the coordinate always increases) contributes a
    *frozen* boolean to a comparison once that comparison can no longer flip for
    the rest of the window.  When this holds for every comparison a rung reads on
    the source, the source's raw value keeps changing but its logical effect on
    that rung is constant — so a clock gating the rung need not stay bound to it.

    ``eq``/``ne`` are treated conservatively as *never* saturated (a value
    sitting at the threshold still has its fall ahead); excluding them keeps
    the predicate sound at the cost of a missed fold.

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


def _cmp_truth(v: float, form: str, k: float) -> bool:
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


def _modwrap_truths(v: Any, cmps: list[tuple[str, float]]) -> list[bool] | None:
    """Each comparison's truth at *v* (``None`` for a non-numeric *v*)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return [_cmp_truth(v, form, k) for form, k in cmps]


def _modwrap_first_flip(v0: int, c: int, m: int, cmps: list[tuple[str, float]]) -> int | None:
    """Scans until any comparison's truth first differs from its truth now."""
    base = [_cmp_truth(v0, form, k) for form, k in cmps]
    v = v0
    for step in range(1, m + 1):
        v = (v + c) % m
        for (form, k), b in zip(cmps, base, strict=True):
            if _cmp_truth(v, form, k) != b:
                return step
    return None


# Timer carry recurrence.  A timed instruction adds ``quantum + frac`` timer
# units each scan, keeps the integer part in its accumulator (clamped) and
# carries the float remainder in ``_frac:<acc>`` memory.  The fold must land
# every timer exactly where that per-scan recurrence would have put it, float
# rounding included: one aggregate ``skip * dt`` floors differently.


@dataclass(frozen=True)
class _TimerMove:
    """A timed source the probe scan advanced by one step of its recurrence."""

    src: _AccSource
    acc: int
    frac: float
    quantum: float
    delta: float  # the probe's progress, ``acc + frac`` after minus before
    acc_before: int | None = None  # the accumulator the probe scan started from


def _timer_step(acc: int, frac: float, quantum: float, acc_max: int) -> tuple[int, float]:
    """One scan of the timer recurrence, as the instructions compute it."""
    units = quantum + frac
    whole = int(units)
    return min(acc + whole, acc_max), units - whole


def _timer_grid(frac: float, quantum: float) -> tuple[int, int, int, float] | None:
    """``(Q, F, N, G)`` when the recurrence from *frac* is exact integer arithmetic.

    With ``G`` the unit in the last place of *quantum*, every sum
    ``quantum + frac`` stays in quantum's binade when ``quantum + 1`` does, so
    if *frac* lies on the ``G`` grid each float step is exact: in units of
    ``G`` the carry is ``F + k * Q`` and the timer gains ``(F + k * Q) // N``
    whole units in ``k`` scans (``N = 1 / G``).  ``None`` otherwise (the
    recurrence is then replayed scan by scan).
    """
    if not (quantum >= 1.0 and math.isfinite(quantum)) or not 0.0 <= frac < 1.0:
        return None
    grid = math.ulp(quantum)
    if grid > 1.0:
        return None
    _mant, exp = math.frexp(quantum)
    if quantum + 1.0 > math.ldexp(1.0, exp):
        return None
    f_units = frac / grid
    if f_units != int(f_units):
        return None
    return int(quantum / grid), int(f_units), int(1.0 / grid), grid


# Off-grid carries (a quantum below one unit, or a sum that straddles a power
# of two).  The recurrence is replayed, but exactly accelerated: IEEE rounding
# is translation invariant on a grid, so
#
# * while x stays strictly inside one binade (grid G) with the same integer
#   part, each step adds the same increment once f is on the grid (and even,
#   when that binade rounds ties): a regime is jumped in O(1);
# * when two consecutive groups of carry cycles did the same thing step for
#   step and their starts differ by D, an even multiple of every grid they
#   used, later groups are those cycles shifted by D for as long as every
#   step stays inside its binade.  That set of shifts is an interval, so a
#   doubling and bisection search on one shifted group finds its end.

# Most real float steps one carry replay may spend; a crossing search that
# runs out bounds the window where it stopped.
_CARRY_WORK_CAP = 200_000
_BIG = 1 << 62


def _carry_cycle(q, base, acc_max, target, acc, f, limit, budget):
    """Run until just after a carry step, *limit* steps, the target, or *budget* real steps.

    Returns ``(steps, acc, f, sig, gtop, status, work)``; *sig* is ``None`` when
    a step sat too close to a binade edge to promise translation.
    """
    steps = 0
    work = 0
    sig: list | None = []
    gtop = 0.0
    regime = None  # (i, e) of the current run of interior real steps
    run_len = 0
    while steps < limit:
        if work >= budget:
            return steps, acc, f, sig, gtop, "work", work
        x = q + f
        i = int(x)
        nf = x - i
        acc = acc + i if acc + i < acc_max else acc_max
        steps += 1
        work += 1
        _m, e = math.frexp(x)
        g = math.ldexp(1.0, e - 53)
        interior = math.ldexp(1.0, e - 1) + g <= x <= math.ldexp(1.0, e) - 2 * g and g <= 0.25
        if sig is not None:
            if interior:
                sig.append((i, e))
            else:
                # At a binade edge the rounding grid is ambiguous unless the
                # sum was exact (TwoSum error zero); an exact sum stays exact
                # under any shift by a multiple of this grid.
                back = x - f
                if (f - (x - back)) + (q - back) == 0.0:
                    sig.append((i, e, "x"))
                else:
                    sig = None
            if g > gtop:
                gtop = g
        prev = f
        f = nf
        if target is not None and acc >= target:
            return steps, acc, f, sig, gtop, "target", work
        if i > base:
            return steps, acc, f, sig, gtop, "carry", work
        if not interior:
            regime = None
            continue
        if regime != (i, e):
            regime = (i, e)
            run_len = 1
            continue
        run_len += 1
        if run_len < 3:
            continue
        # Three interior steps in one regime: the first left f on the grid, the
        # second (a tie, if this binade rounds ties) left it even, so the
        # third's increment f - prev repeats while x stays in the binade.
        d_units = int((f - prev) / g)
        f_units = int(f / g)
        i_units = int(i / g)
        if d_units * g != f - prev or f_units * g != f:
            regime = None
            continue
        hi = (1 << 53) - 2  # x <= 2**e - 2g, in units of g
        one = int(1.0 / g)  # f < 1
        room = limit - steps
        if d_units > 0:
            room = min(room, (hi - i_units - f_units) // d_units, (one - 1 - f_units) // d_units)
        if room <= 0:
            continue
        if target is not None and i > 0:
            need = -(-(target - acc) // i)
            if need <= room:
                room = need
        acc = acc + i * room if acc + i * room < acc_max else acc_max
        f = (f_units + d_units * room) * g
        steps += room
        if sig is not None:
            sig.append(("j", i, e, room))
        regime = None
        if target is not None and acc >= target:
            return steps, acc, f, sig, gtop, "target", work
    return steps, acc, f, sig, gtop, "limit", work


def _carry_translate(q, base, acc_max, target, acc, f, remaining, cycles, budget):
    """Jump whole groups of carry cycles that repeat as a translation, or ``None``.

    Every group size the history allows is a candidate when its two latest
    groups did the same thing step for step and their shift is an even
    multiple of the coarsest grid they used.  The smallest drift per scan
    survives longest, so those are searched first (three at most); the
    largest verified jump wins.
    """
    candidates = []
    for p in range(1, len(cycles) // 2 + 1):
        a = cycles[-2 * p : -p]
        b = cycles[-p:]
        steps_b = sum(c[2] for c in b)
        gain_b = sum(c[3] for c in b)
        if sum(c[2] for c in a) != steps_b or sum(c[3] for c in a) != gain_b:
            continue
        if any(ca[4] != cb[4] for ca, cb in zip(a, b, strict=True)):
            continue
        shift = b[0][1] - a[0][1]
        if f != b[0][1] + shift:
            continue
        gtop = max(c[5] for c in cycles[-2 * p :])
        units = shift / gtop
        if units != int(units) or int(units) % 2:
            continue
        m_cap = remaining // steps_b
        if gain_b > 0:
            m_cap = min(m_cap, (acc_max - acc) // gain_b)
            if target is not None:
                m_cap = min(m_cap, (target - 1 - acc) // gain_b)
        if m_cap >= 1:
            candidates.append((abs(shift) / steps_b, p, b, steps_b, gain_b, shift, m_cap))
    candidates.sort(key=lambda c: (c[0], c[1]))
    best = None
    used = 0
    for _drift, _p, b, steps_b, gain_b, shift, m_cap in candidates[:3]:

        def holds(m: int, b=b, shift=shift, gain_b=gain_b) -> bool:
            nonlocal used
            ca = acc + (m - 1) * gain_b
            cf = f + (m - 1) * shift
            if not 0.0 <= cf < 1.0:
                return False
            for c in b:
                s, ca2, cf2, sig, _g, status, w = _carry_cycle(
                    q, base, acc_max, None, ca, cf, c[2], budget - used
                )
                used += w
                if status != "carry" or s != c[2] or sig is None or tuple(sig) != c[4]:
                    return False
                if ca2 - ca != c[3]:
                    return False
                ca, cf = ca2, cf2
            return cf == f + m * shift

        good = 0
        m = 1
        while m <= m_cap and used < budget and holds(m):
            good = m
            m *= 2
        bad = min(m, m_cap + 1)
        while bad - good > 1 and used < budget:
            mid = (good + bad) // 2
            if holds(mid):
                good = mid
            else:
                bad = mid
        if good >= 1 and (best is None or good * steps_b > best[0]):
            best = (good * steps_b, good * gain_b, good * shift)
            if good == m_cap:
                break
        if used >= budget:
            break
    if best is None:
        return None
    return best[0], best[1], best[2], used


def _carry_run(
    acc: int,
    f: float,
    q: float,
    acc_max: int,
    n: int | None,
    target: int | None = None,
    work_cap: int | None = _CARRY_WORK_CAP,
) -> tuple[int, int, float, str]:
    """Advance the carry recurrence *n* scans (``None``: unbounded) or until ``acc >= target``.

    Exact: real float steps plus the regime jumps and cycle translations they
    certify (see the comment above).  Returns ``(steps, acc, f, status)`` with
    status ``"target"``, ``"limit"`` or ``"work"``: *work_cap* real steps were
    spent first, and *steps* scans were advanced exactly.  When the
    accelerations do not pay (a quantum whose cycles repeat only after
    hundreds of carries), the rest is plain stepping under the same cap.
    """
    cap = _BIG if work_cap is None else work_cap
    base = int(q)
    remaining = _BIG if n is None else n
    steps = 0
    work = 0
    cycles: list[tuple] = []  # (acc0, f0, steps, gain, sig, gtop) per completed carry cycle
    since = 0  # cycles recorded since the last translation attempt
    while remaining > 0:
        if work >= 4096 and steps < 8 * work:
            return _carry_plain(acc, f, q, acc_max, remaining, target, steps, cap - work)
        jumped = None
        if (len(cycles) in (2, 8, 32, 128, 512) and since == len(cycles)) or since >= 256:
            since = 0
            jumped = _carry_translate(
                q, base, acc_max, target, acc, f, remaining, cycles, cap - work
            )
        if jumped is not None:
            m_steps, m_gain, m_shift, used = jumped
            work += used
            steps += m_steps
            remaining -= m_steps
            acc += m_gain
            f = f + m_shift
            cycles.clear()
            if remaining <= 0:
                break
        s, acc2, f2, sig, gtop, status, w = _carry_cycle(
            q, base, acc_max, target, acc, f, remaining, cap - work
        )
        work += w
        if status == "carry" and sig is not None:
            cycles.append((acc, f, s, acc2 - acc, tuple(sig), gtop))
            since += 1
            if len(cycles) > 512:
                del cycles[0]
        elif status == "carry":
            cycles.clear()
        acc, f = acc2, f2
        steps += s
        remaining -= s
        if status == "target":
            return steps, acc, f, "target"
        if status == "work" or work >= cap:
            return steps, acc, f, "work"
    return steps, acc, f, "limit"


def _carry_plain(
    acc: int, f: float, q: float, acc_max: int, n: int, target: int | None, steps: int, budget: int
) -> tuple[int, int, float, str]:
    """Plain stepping of the carry recurrence (see :func:`_carry_run`)."""
    budget = min(n, max(0, budget))
    for _ in range(budget):
        x = q + f
        i = int(x)
        f = x - i
        acc = acc + i if acc + i < acc_max else acc_max
        steps += 1
        if target is not None and acc >= target:
            return steps, acc, f, "target"
    return steps, acc, f, ("limit" if budget == n else "work")


def _timer_after(move: _TimerMove, n: int) -> tuple[int, float]:
    """The timer's ``(acc, frac)`` after *n* more scans of its recurrence."""
    acc, frac, q, acc_max = move.acc, move.frac, move.quantum, move.src.acc_max
    if n <= 0:
        return acc, frac
    grid = _timer_grid(frac, q)
    if grid is not None:
        big_q, big_f, big_n, g = grid
        total = big_f + n * big_q
        return min(acc + total // big_n, acc_max), (total % big_n) * g
    steps, acc, frac, _status = _carry_run(acc, frac, q, acc_max, n, work_cap=None)
    assert steps == n
    return acc, frac


def _timer_scans_to_reach(move: _TimerMove, target: int, cap: int | None) -> int | None:
    """First scan count ``k >= 1`` after which the accumulator is ``>= target``.

    ``None`` when it never gets there.  An off-grid carry is searched no
    farther than ``cap`` scans, nor past :data:`_CARRY_WORK_CAP` real steps;
    not found by then returns the scans searched plus one, which bounds a
    window no farther than the search.
    """
    acc, frac, q, acc_max = move.acc, move.frac, move.quantum, move.src.acc_max
    if acc >= target or target > acc_max or not q > 0.0:
        return None
    grid = _timer_grid(frac, q)
    if grid is not None:
        big_q, big_f, big_n, _g = grid
        need = (target - acc) * big_n - big_f
        return max(1, -(-need // big_q))
    limit = None if cap is None else max(1, cap)
    steps, _acc, _frac, status = _carry_run(acc, frac, q, acc_max, limit, target)
    if status == "target":
        return steps
    if status == "limit" and limit is None:
        return None
    return steps + 1


def _timer_moves(
    ctx: _FoldContext,
    before: tuple[tuple[int, float], ...],
    after: tuple[tuple[int, float], ...],
) -> tuple[_TimerMove, ...] | None:
    """The timed sources advancing through the window, or ``None`` if unknown.

    *before* and *after* hold each of ``ctx.timed_sources``' ``(acc, frac)``
    before and after the probe scan (see :func:`_acc_readings`).  A timer the probe moved by exactly one step of the carry
    recurrence keeps doing so through the window, even when that step left
    it where it was; one otherwise left unchanged stays put.  Anything else
    (two steps in one scan, a patched accumulator, a scan ``dt`` other than
    the normal one, a reset) is not a recurrence the fold can extend, so the
    window is not certified.  A reset in particular may be a one-scan pulse
    the timer retimes from on the very next scan.
    """
    moves: list[_TimerMove] = []
    dt = ctx.normal_dt
    for src, (acc_b, frac_b), (acc, frac) in zip(ctx.timed_sources, before, after, strict=True):
        still = (acc, frac) == (acc_b, frac_b)
        if src.unit is None:
            if still:
                continue
            return None
        quantum = src.unit.dt_to_units(dt)
        if (acc, frac) != _timer_step(acc_b, frac_b, quantum, src.acc_max):
            if still:
                continue
            return None
        # One step of the recurrence, possibly a fixed point (an accumulator
        # held at its clamp with a whole-unit quantum): the landing still runs
        # the timer once with the aggregate dt, so it needs the edits too.
        delta = (float(acc) + frac) - (float(acc_b) + frac_b)
        moves.append(_TimerMove(src, acc, frac, quantum, delta, acc_b))
    return tuple(moves)


def _timer_integer_target(target: float, strict: bool) -> int | None:
    """The least integer accumulator value that crosses ``(target, strict)``."""
    if not math.isfinite(target):
        return None
    return math.floor(target) + 1 if strict else math.ceil(target)


def _nearest_timer_crossing(
    ctx: _FoldContext,
    moves: Sequence[_TimerMove],
    state: Any,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None,
    cap: int | None,
    unmodeled: frozenset[str] = frozenset(),
) -> int | None:
    """Scans to the nearest crossing of an advancing timer, on its exact recurrence.

    Same thresholds as :func:`_nearest_acc_crossing` (preset, read
    comparisons, run_until comparisons), resolved to the integer accumulator
    value the comparisons actually read.  A timer in *unmodeled* is bounded at
    its next integer step, and at one scan when the probe stepped it.  A
    search never needs to look past *cap* scans: the window is already
    bounded there.
    """
    best: int | None = None
    for move in moves:
        src = move.src
        targets: list[tuple[float, bool]] = []
        if src.acc_name in unmodeled:
            if move.acc_before is not None and move.acc_before != move.acc:
                return 1
            targets.append((float(move.acc + 1), False))
        preset = _resolve_num(src.preset, state)
        if preset is not None:
            targets.append((preset, False))
        elif src.done_bit in ctx.read_done and not _step_without_preset(src.preset, state):
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
            targets.extend(_progress_bounds(src.kind, form, kv))
        if unresolved:
            best = 1 if best is None else min(best, 1)
            continue
        nearest: int | None = None
        for target, strict in targets:
            t = _timer_integer_target(target, strict)
            if t is None:
                continue
            if move.acc_before is not None and move.acc_before < t <= move.acc:
                return 1  # crossed inside the probe (see _crossed_between)
            if t <= move.acc:
                continue
            if t > src.acc_max:
                # The accumulator clamps first, so this threshold never flips.
                # It still bounds the window, as a moving source's far threshold
                # always has: only the window's length depends on it.
                if move.delta > 0:
                    pace = _scans_to_cross(move.acc + move.frac, move.delta, target, strict)
                    if pace is not None:
                        best = pace if best is None else min(best, pace)
                continue
            if nearest is None or t < nearest:
                nearest = t
        if nearest is None:
            continue
        search = cap if best is None else (best if cap is None else min(cap, best))
        scans = _timer_scans_to_reach(move, nearest, search)
        if scans is not None:
            best = scans if best is None else min(best, scans)
    return best


def _timer_landing(
    moves: Sequence[_TimerMove], skip: int, normal_dt: float
) -> tuple[tuple[str, int, float, float], ...] | None:
    """Pre- and post-landing timer edits that make the macro scan exact.

    The landing scan executes each timer once with ``dt = skip * normal_dt``.
    Give it the accumulator the last folded scan starts from (what an earlier
    rung reading it sees when stepping) and a carry chosen so that its one
    aggregate step adds exactly that scan's increment; afterwards the carry is
    set to the recurrence's own.  Returns ``(acc_name, acc, frac_in,
    frac_out)`` per timer, or ``None`` if no such carry exists (it would take
    a window of about 2**51 timer units).
    """
    if skip <= 1 or not moves:
        return ()
    landing_dt = skip * normal_dt
    out: list[tuple[str, int, float, float]] = []
    for move in moves:
        acc_last, frac_last = _timer_after(move, skip - 1)
        units = move.quantum + frac_last
        increment = int(units)
        frac_out = units - increment
        landing_units = move.src.unit.dt_to_units(landing_dt)
        for bias in (0.5, 0.25, 0.75):
            frac_in = (increment + bias) - landing_units
            if int(landing_units + frac_in) == increment:
                break
        else:
            return None
        out.append((move.src.acc_name, acc_last, frac_in, frac_out))
    return tuple(out)


def _nearest_acc_crossing(
    ctx: _FoldContext,
    before_tot: dict[str, float],
    after_tot: dict[str, float],
    state: Any,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
    unmodeled: frozenset[str] = frozenset(),
) -> int | None:
    """Scans to the nearest actionable accumulator crossing.

    *extra_comparisons* carries thresholds read by the ``run_until`` predicate
    (e.g. ``Ctr_Acc > 500``); merging them in makes the fold land on the
    predicate's threshold instead of skipping to the preset and overshooting.
    A moving source in *unmodeled* changes every scan a reader may see, so it
    bounds the window at one scan.  Timed sources are left to
    :func:`_nearest_timer_crossing`.
    """
    best: int | None = None
    for src in ctx.sources:
        if src.timed:
            continue  # exact carry recurrence: see _nearest_timer_crossing
        pb = before_tot.get(src.acc_name)
        pa = after_tot.get(src.acc_name)
        if pb is None or pa is None:
            continue
        delta = pa - pb
        if abs(delta) <= _EPS:
            continue
        if src.step is None:
            # A counter counts the rising edges of its inputs: one that moved
            # on the probe scan saw an edge the next scan does not repeat (its
            # memory now holds the input), so the probe is no recurrence.  A
            # counter that held still is as guarded as any plateau tag.
            return 1
        if delta < 0 and not src.bidir:
            return 1
        if src.acc_name in unmodeled:
            return 1
        # What is left is a self-calc source: no Done bit, so its sentinel
        # preset is no crossing (its wrap is :func:`_selfcalc_wrap_bound`).
        bounds: list[tuple[float, bool]] = []
        cmps = ctx.comparisons.get(src.acc_name, ())
        if extra_comparisons:
            cmps = cmps + extra_comparisons.get(src.acc_name, ())
        unresolved = False
        for form, operand in cmps:
            kv = _resolve_num(operand, state)
            if kv is None:
                unresolved = True
                break
            bounds.extend(_progress_bounds(src.kind, form, kv))
        if unresolved:
            best = 1 if best is None else min(best, 1)
            continue
        for target, strict in bounds:
            if _crossed_between(pb, pa, target, strict):
                return 1
            if delta > 0:
                scans = _scans_to_cross(pa, delta, target, strict)
            else:
                scans = _scans_to_uncross(pa, delta, target, strict)
            if scans is None:
                continue
            best = scans if best is None else min(best, scans)
    return best


def _selfcalc_wrap_bound(
    ctx: _FoldContext, before_tot: dict[str, float], after_tot: dict[str, float], state: Any
) -> int | None:
    """The nearest :func:`_selfcalc_bound` over the self-calc sources."""
    best: int | None = None
    for src in ctx.sources:
        if src.step is None:
            continue
        pb = before_tot.get(src.acc_name)
        pa = after_tot.get(src.acc_name)
        if pb is None or pa is None:
            continue
        bound = _selfcalc_bound(src, pa - pb, state)
        if bound is not None:
            best = bound if best is None else min(best, bound)
    return best


def _selfcalc_bound(src: _AccSource, prog_delta: float, state: Any) -> int | None:
    """Crossing-style bound for a self-calc source: its step and its wrap.

    The probe must have moved the tag by exactly its step (else ``1``: no
    fold).  The patched value after ``skip - 1`` steps must stay inside the
    destination's range: an overflowing step wraps and raises the transient
    out-of-range fault, so only the genuine landing scan may take it.
    Returns scans in :func:`_nearest_acc_crossing` units (``skip = scans - 1``),
    or ``None`` when the source is not moving.
    """
    step = src.step
    if isinstance(step, str):
        negate = step.startswith("-")
        raw = state.tags.get(step[1:] if negate else step)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 1
        step = -raw if negate else raw
    assert isinstance(step, int)
    measured = prog_delta if src.kind == "up" else -prog_delta
    if measured != step:
        return 1
    if step == 0:
        return None
    value = state.tags.get(src.acc_name)
    if src.value_range is None or isinstance(value, bool) or not isinstance(value, int):
        return 1
    lo, hi = src.value_range
    if not lo <= value <= hi:
        return 1
    first_out = (hi - value) // step + 1 if step > 0 else (value - lo) // -step + 1
    return first_out + 1


def _nearest_mod_flip(
    ctx: _FoldContext,
    state: Any,
    extra_comparisons: dict[str, tuple[tuple[str, Any], ...]] | None = None,
    before: Sequence[Any] = (),
    unmodeled: frozenset[str] = frozenset(),
) -> int | None:
    """Scans to the nearest comparison truth-flip among mod-wrap sources.

    *before* holds each source's value before the probe scan: a flip between
    it and the current value happened inside the probe, and a rung above the
    self-calc reads it on the next scan (``1``).
    """
    best: int | None = None
    for i, mw in enumerate(ctx.modwrap):
        if mw.name in unmodeled:
            return 1  # it moves every scan, and some read of it is not a comparison
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
        if (
            i < len(before)
            and before[i] != v0
            and _modwrap_truths(before[i], resolved) != (_modwrap_truths(v0, resolved))
        ):
            return 1
        flip = _modwrap_first_flip(v0, mw.c, mw.m, resolved)
        if flip is not None:
            best = flip if best is None else min(best, flip)
    return best


# ── 11. State helpers ────────────────────────────────────────────────


def _acc_readings(
    state: Any, sources: tuple[_AccSource, ...]
) -> tuple[dict[str, float], tuple[tuple[int, float], ...]]:
    """:func:`_acc_totals` and each timed source's ``(acc, frac)``, in one pass."""
    tags = state.tags
    memory = state.memory
    totals: dict[str, float] = {}
    timers: list[tuple[int, float]] = []
    for src in sources:
        raw = tags.get(src.acc_name, 0)
        acc = float(raw or 0)
        if src.timed:
            frac = float(memory.get(f"_frac:{src.acc_name}", 0.0) or 0.0)
            acc += frac
            timers.append(
                (raw if isinstance(raw, int) and not isinstance(raw, bool) else int(raw or 0), frac)
            )
        totals[src.acc_name] = -acc if src.kind == "down" else acc
    return totals, tuple(timers)


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


EDGE_MEMORY_PREFIX = "_edge:"


def edge_memory_on(state: Any, exempt: frozenset[str] = frozenset()) -> frozenset[str]:
    """The edge sites whose memory (``_edge:e{N}``) holds on in *state*.

    An edge contact stores the bit it saw each time its row is scanned, so
    equal tags no longer imply equal edge memory: a scan that saw a bit change
    at a contact leaves the memory changed, and the next scan sees no edge
    there.  A window repeats its probe scan only if the probe left every edge
    memory as it found it; absent and off read the same.  Keys in *exempt*
    (see :func:`every_scan_clock_edge_keys`) are left out.
    """
    return frozenset(
        key
        for key, value in state.memory.items()
        if value
        and isinstance(key, str)
        and key.startswith(EDGE_MEMORY_PREFIX)
        and key not in exempt
    )


def every_scan_clock_edge_keys(site_table: Any) -> frozenset[str]:
    """Memory keys of the edge sites on a system clock that every scan scans.

    A contact on ``sys.clock_*`` in a main-program row (not a For body) is
    scanned on every scan, so its memory is the clock as the previous scan
    read it: a function of the timestamp, which the fold's clock handling
    already models (an edge-sensitive rung sees the pair of reads one ``dt``
    apart, see :func:`_mark_inert_soft`; windows end at a hard clock's edges).
    The plateau guard leaves those keys to it.  A clock contact in a
    subroutine, a For body or an overlay is scanned only when something lets
    it, so what it remembers depends on when it last ran: it stays guarded.
    """
    from pyrung.core.site_memory import SITE_EDGE
    from pyrung.core.system_points import _CLOCK_HALF_PERIODS

    if site_table is None:
        return frozenset()
    return frozenset(
        key
        for site in site_table.sites
        if site.site == SITE_EDGE
        and site.scope == "main"
        and site.leg != "body"
        and (_CLOCK_HALF_PERIODS.get(site.tag or "") or 0) > 0
        for key, _default in site.memory
    )


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


def _clock_reads_in_phase(t: float, dt: float, hp: float) -> int:
    """How many consecutive scans reading at ``t, t + dt, ...`` see the clock's phase at ``t``.

    Scan ``j`` of a window reads the clock at ``t + j * dt`` (the landing scan
    resolves it at exactly that expression).  The estimate is corrected
    against :func:`clock_phase` itself, so the count agrees with what each
    scan resolves.  Always at least ``1``: the scan reading at ``t`` does.
    """
    from pyrung.core.system_points import clock_phase

    phase = clock_phase(t, hp)
    n = max(1, math.ceil(((phase + 1) * hp - t) / dt - _EPS))
    while n > 1 and clock_phase(t + (n - 1) * dt, hp) != phase:
        n -= 1
    while clock_phase(t + n * dt, hp) == phase:
        n += 1
    return n


def _scans_to_clock_edge(
    ctx: _FoldContext,
    state: Any,
    inert_soft: frozenset[str] = frozenset(),
    promoted: frozenset[str] = frozenset(),
    probe_timestamp: float | None = None,
) -> int | None:
    """Largest fold skip whose scans all read a bounding clock in one phase.

    A clock toggles when ``int(timestamp / half_period)`` increments.  A scan
    reads the clock at its start, so the window after a probe reads at
    ``t, t + dt, ...`` from the post-probe timestamp ``t``.  The window is
    certified only by what the probe scan did, so

    * the probe must have read the same phase the window starts with.  A
      probe that read just before an edge and committed past it saw the old
      phase; return ``0`` (*probe_timestamp* is the probe's read time);
    * every window scan, the landing scan included, must read before the
      next edge.  The landing state itself sits on or past the edge, so the
      next genuine scan is the edge scan and runs as an ordinary probe (where
      a per-edge change breaks the plateau and is never wrongly marked inert).

    ``0`` means don't fold.  ``None`` means nothing is read that needs bounding.

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
        if probe_timestamp is not None and clock_phase(probe_timestamp, hp) != clock_phase(t, hp):
            return 0
        gap = _clock_reads_in_phase(t, ctx.normal_dt, hp)
        best = gap if best is None else min(best, gap)
    return best


def _mark_inert_soft(
    ctx: _FoldContext,
    inert_soft: set[str],
    inert_run: dict[str, int],
    read_ts: float,
    promoted: frozenset[str] = frozenset(),
) -> None:
    """Record the clock edges one genuine probe scan saw; confirm a clock inert once both did.

    The caller invokes this only after the visible state was observed
    unchanged across the probe scan, which read the clocks at *read_ts*; the
    scan before it read them one normal ``dt`` earlier.  An edge-sensitive
    rung (``rise(clock)`` / ``fall(clock)``) sees exactly that pair of reads,
    so the probe shows one polarity inert when the two reads differ.  A clock
    joins *inert_soft* only after both a rise and a fall were seen inert
    (``inert_run`` holds the polarities seen: 1 rise, 2 fall); the loop clears
    it whenever the plateau breaks.

    Edges the clock crossed between two reads are never counted: at a ``dt``
    of a half-period or more one scan can cross several, and stepping sees
    only the reads.  A clock whose reads never differ is never confirmed, and
    keeps bounding the fold.  Promoted saturation-rescuable clocks are
    inert-eligible on the same terms.
    """
    from pyrung.core.system_points import clock_high

    dt = ctx.normal_dt
    for name, hp in _window_soft_clocks(ctx, promoted):
        if name in inert_soft:
            continue
        before = clock_high(read_ts - dt, hp)
        now = clock_high(read_ts, hp)
        if before == now:
            continue
        seen = inert_run.get(name, 0) | (1 if now else 2)
        inert_run[name] = seen
        if seen == 3:
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
    *,
    on_kernel_scan: Callable[[int], None] | None = None,
    capture_kernel_scan: _CaptureDecision = False,
    capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
    timer_landing: Sequence[tuple[str, int, float, float]] = (),
) -> tuple[tuple[str, Any], ...]:
    """Fold ``skip`` pure-accumulation scans into one real step.

    Timers ride the dt knob, set up by *timer_landing* (see
    :func:`_timer_landing`) so that the one aggregate step lands each timer
    exactly where ``skip`` ordinary scans would; per-scan counters can't be
    moved by time, so their accumulators are patched forward by
    ``(skip-1)*delta`` and the step's own ``execute`` supplies the final
    increment, keeping every source in phase.

    The landing scan stands in for the last of the ``skip`` scans, so it
    starts at the timestamp that scan would have read, ``T0 + (skip - 1) *
    dt``: every time-derived read (``rtc.*`` fields, system clocks, edge
    memory) resolves as it would stepping.  It commits one normal step later,
    at ``T0 + skip * dt``.

    Uses ``_run_single_scan(consume_pause_request=False)`` so the caller
    retains control over pause consumption.
    """
    patches = _fold_patches(runner.state, skip, ctx, before_tot, after_tot) if skip > 1 else {}

    pre_landing = runner._state
    start_timestamp = pre_landing.timestamp
    positioned = pre_landing
    if skip > 1:
        positioned = pre_landing.set(timestamp=start_timestamp + (skip - 1) * ctx.normal_dt)
        # Direct tip edits, not patches: they are not inputs (a replay steps
        # the folded scans and re-derives them, so logging them would apply
        # them twice), and a timer in the synthesis plant runs before the
        # patch drain.
        edits: dict[str, Any] = dict(patches)
        if timer_landing:
            edits.update({name: acc for name, acc, _fi, _fo in timer_landing})
            positioned = positioned.set(
                memory=positioned.memory.update(
                    {f"_frac:{name}": frac_in for name, _acc, frac_in, _fo in timer_landing}
                ),
            )
        if edits:
            positioned = positioned.set(tags=positioned.tags.update(edits))
        _replace_tip_state(runner, positioned, edits or None)
        runner._landing_commit_dt = ctx.normal_dt
        runner._landing_scan_id = pre_landing.scan_id + skip
    runner._dt_override_for_next_scan = skip * ctx.normal_dt
    try:
        runner._run_single_scan(
            consume_pause_request=False,
            capture_execution=_capture_requested(capture_kernel_scan),
            capture_sink=capture_sink,
        )
    except BaseException:
        # The landing scan committed nothing: leave the tip as it was.
        runner._dt_override_for_next_scan = None
        if runner._state is positioned:
            _replace_tip_state(runner, pre_landing)
        raise
    finally:
        runner._landing_commit_dt = None
        runner._landing_scan_id = None
    if on_kernel_scan is not None:
        on_kernel_scan(runner._state.scan_id)

    landed = runner._state.set(
        scan_id=runner._state.scan_id + skip - 1,
        timestamp=start_timestamp + skip * ctx.normal_dt,
    )
    if timer_landing:
        landed = landed.set(
            memory=landed.memory.update(
                {f"_frac:{name}": frac_out for name, _acc, _fi, frac_out in timer_landing}
            )
        )
    _replace_tip_state(runner, landed)
    if skip > 1:
        runner._record_fold_landing(pre_landing.scan_id + 1)
    return tuple(patches.items())


def _scan_callbacks(runner: Any, moving: frozenset[str] | None = None) -> tuple[bool, bool]:
    """``(blocks_ordinary, blocks_jumps)`` for the active ``when()`` registrations and monitors.

    They are judged after every committed scan, on the state that scan left,
    so a fold that skips scans cannot honor them: the ordinary fold declines
    while any ``when()`` registration is active.  A monitor only acts when its
    tag changes, and on a plateau only the *moving* tags (the fold's excluded
    coordinates) change on a skipped scan, so with *moving* given only a
    monitor on one of those blocks the ordinary fold: any other monitored tag
    holds through the window and sees the changes it sees stepping (a Harness
    installs one on each bool feedback).  A ``when().do()`` callback acts every scan its
    condition holds, so a cycle jump that observed whole periods of it may
    still repeat it; a pause, snapshot or monitor blocks those jumps too.
    """
    active = [
        r for r in getattr(runner, "_breakpoints_by_id", {}).values() if r.enabled and not r.removed
    ]
    live_monitors = [
        r for r in getattr(runner, "_monitors_by_id", {}).values() if r.enabled and not r.removed
    ]
    monitors = bool(live_monitors)
    blocks_ordinary = bool(active) or (
        monitors if moving is None else any(r.tag_name in moving for r in live_monitors)
    )
    return blocks_ordinary, monitors or any(r.action != "do" for r in active)


def _replace_tip_state(
    runner: PLC, state: SystemState, tag_edits: dict[str, Any] | None = None
) -> None:
    """Install *state* as the tip without a scan.

    Keeps the runner's plain-dict read mirror of the tip tags valid.  *state*
    has the tip's tags, plus *tag_edits* when given; any other tag change
    drops the mirror (the next scan rebuilds it).
    """
    if runner._state_tags_read_cache_state is runner._state:
        if tag_edits:
            cache = runner._state_tags_read_cache
            if cache is not None:
                cache.update(tag_edits)
            runner._state_tags_read_cache_state = state
        elif state.tags is runner._state.tags:
            runner._state_tags_read_cache_state = state
        else:
            runner._state_tags_read_cache_state = None
    runner._state = state


# Reads of an opaque stop predicate.  A callable ``run_until`` predicate is
# judged on a state whose field and tag reads are recorded: a window may fold
# only if, at the probe, the predicate read no value the window moves (it then
# takes the same path and returns the same answer on every skipped scan).
_READ_TRACES: list[set[str]] = []
_READ_ALL = "*"  # iterated or compared the whole tag map
_FIELD_READ = ":"  # prefix of a state field read (scan_id, timestamp, memory)


class _TracedTags(PMap):
    """A tag map that records which keys are read while a trace is active."""

    __slots__ = ()

    def __getitem__(self, key: Any) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(key)
        return _PMAP._getitem(self._buckets, key)

    def __contains__(self, key: Any) -> bool:
        if _READ_TRACES:
            _READ_TRACES[-1].add(key)
        return _PMAP._contains(self._buckets, key)

    def iteritems(self) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_READ_ALL)
        return PMap.iteritems(self)

    def __eq__(self, other: object) -> bool:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_READ_ALL)
        return PMap.__eq__(self, other)

    __hash__ = PMap.__hash__


def _traced_state_class() -> type:
    global _TRACED_STATE
    if _TRACED_STATE is None:
        from pyrung.core.state import SystemState

        class _TracedState(SystemState):
            """A SystemState whose field reads (and tag reads) are recorded."""

            __slots__ = ()

            def __getitem__(self, key: Any) -> Any:
                value = _PMAP._getitem(self._buckets, key)
                if key == "tags":
                    return _PMAP.__new__(_TracedTags, value._size, value._buckets)
                if _READ_TRACES:
                    _READ_TRACES[-1].add(_FIELD_READ + str(key))
                return value

        _TRACED_STATE = _TracedState
    return _TRACED_STATE


_TRACED_STATE: type | None = None


@contextmanager
def _reads_untraced() -> Iterator[None]:
    """Keep the reads inside the block out of the active trace.

    For a predicate whose contract is to be judged only on the scans a coast
    really executes (PILOT's opaque ``avoid=`` callables): its reads must not
    bound the windows that skip the others.
    """
    _READ_TRACES.append(set())
    try:
        yield
    finally:
        _READ_TRACES.pop()


def _traced_call(
    predicate: Callable[[SystemState], bool], runner: PLC
) -> tuple[bool, frozenset[str]]:
    """``predicate(runner.state)`` and every tag (or state field) it read.

    The tip itself is swapped for the traced state during the call, so a
    predicate that reads ``plc.state`` instead of its argument is traced too.
    """
    state = runner._state
    traced = _PMAP.__new__(_traced_state_class(), state._size, state._buckets)
    reads: set[str] = set()
    _READ_TRACES.append(reads)
    runner._state = traced
    try:
        hit = bool(predicate(traced))
    finally:
        runner._state = state
        _READ_TRACES.pop()
    return hit, frozenset(reads)


@dataclass(frozen=True)
class _FoldProbe:
    """State captured immediately before one ordinary probe scan."""

    totals: dict[str, float]
    visible: Any
    timestamp: float
    scan_id: int
    # ``(acc, frac)`` of each of ``ctx.timed_sources`` before the probe scan.
    timers: tuple[tuple[int, float], ...] = ()
    # Each of ``ctx.modwrap``'s values before the probe scan.
    mods: tuple[Any, ...] = ()
    # The edge sites whose memory was on before the probe scan.
    edges: frozenset[str] = frozenset()


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
    # ``(acc_name, acc, frac_in, frac_out)`` timer edits around the landing
    # scan (see _timer_landing).
    timer_landing: tuple[tuple[str, int, float, float], ...] = ()


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
        extra_unmodeled: Collection[str] = (),
    ) -> None:
        self.ctx = ctx
        self.extra_comparisons = extra_comparisons
        # Coordinates read outside a modeled comparison by the program or by
        # the stop predicate (see _audit_coordinate_reads).
        self.unmodeled = ctx.unmodeled | ctx.unmodeled_coordinates(extra_unmodeled)
        # What an opaque stop predicate read at the latest probe (``None``: no
        # such predicate); the loop sets it (see _traced_call).
        self.predicate_reads: frozenset[str] | None = None
        self.exclude = (
            ctx.acc_names
            | ctx.profile_fb_names
            | ctx.churn_excluded
            | ctx.modwrap_names
            | ctx.mirror_names
            | ctx.frozen_writes
            | (ctx.time_mirrors.names if ctx.time_mirrors is not None else frozenset())
        )
        self.inert_soft: set[str] = set()
        self.inert_run: dict[str, int] = {}

    def predicate_unmodeled(self) -> frozenset[str] | None:
        """Sources the opaque predicate read, or ``None`` if it read the unknowable.

        A tag the plateau guard checks holds still through a window.  A source
        (or a view of one) moves: it is bounded at its next value change.  Any
        other moving read (another excluded tag, a value the landing rebuilds,
        ``scan_id``, ``timestamp``, memory, the whole map) declines the window.
        """
        reads = self.predicate_reads
        if reads is None:
            return frozenset()
        ctx = self.ctx
        moving = reads & (self.exclude | ctx.landing_rebuilt)
        if _READ_ALL in reads or any(
            isinstance(name, str) and name.startswith(_FIELD_READ) for name in reads
        ):
            return None
        if moving - ctx.coordinates:
            return None
        return ctx.unmodeled_coordinates(moving)

    def capture(self, runner: PLC) -> _FoldProbe:
        """Capture the entry side of one normal probe scan.

        ``visible`` omits only coordinates whose evolution has a separate,
        statically certified projection (accumulators, modular self-calcs,
        affine views, proven unobservable/frozen writes, and clock mirrors the
        landing scan rebuilds, see :class:`_TimeMirrors`). Everything else
        is the plateau guard: one changed value declines the fold.

        Also has *runner* count each timed accumulator's writes in the probe
        scan, for :meth:`plan` (see :func:`_timer_contests`).
        """
        names = self.ctx.timed_acc_names
        if names and not names <= runner._acc_write_watch:
            runner._acc_write_watch = runner._acc_write_watch | names
        return self.capture_state(runner._state)

    def capture_state(self, state: SystemState) -> _FoldProbe:
        """Capture a probe from a state-only executor such as compiled replay."""
        totals, timers = _acc_readings(state, self.ctx.sources)
        return _FoldProbe(
            totals=totals,
            visible=_visible_items(state, self.exclude),
            timestamp=state.timestamp,
            scan_id=state.scan_id,
            timers=timers,
            mods=tuple(state.tags.get(mw.name) for mw in self.ctx.modwrap),
            edges=edge_memory_on(state, self.ctx.clock_edge_memory),
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
        rtc_at: Callable[[float], datetime] | None = None,
        forced: Collection[str] | None = None,
        acc_writes: Mapping[str, int] | None = None,
    ) -> _FoldPlan | None:
        """Certify a macro landing after a completed probe scan.

        Replay may set ``endpoint_is_boundary`` when ``max_skip`` denotes one
        exact requested state or the scan immediately preceding a recorded
        input event.  Live run loops leave it false: their budget is not a
        semantic program boundary, and preserving their established cadence
        matters to PILOT's bounded correction lifecycle.

        *rtc_at* resolves the executor's RTC at a simulation time.  A program
        that reads an ``rtc.*`` field cannot fold without it.  *forced* names
        the executor's forced tags; without it every RTC tick stays a bound
        and no window with a timer is certified.  *acc_writes* counts each
        timed accumulator's writes in the probe scan; without it a program
        with a contested timer is not folded (see :func:`_timer_contests`).
        """
        if not _visible_items_match(state, probe.visible, self.exclude):
            self.inert_soft.clear()
            self.inert_run.clear()
            return None
        if edge_memory_on(state, self.ctx.clock_edge_memory) != probe.edges:
            # The probe saw an edge at some contact (or its memory moved off a
            # bit it last saw): the next scan is not a repeat of it.
            self.inert_soft.clear()
            self.inert_run.clear()
            return None
        if probe.scan_id == 0 and self.ctx.reads_first_scan:
            # The probe read first_scan True; the window would read it False.
            # Its unchanged state proves nothing about the window's scans.
            return None

        promoted = _runtime_soft_clocks(self.ctx, state)
        _mark_inert_soft(self.ctx, self.inert_soft, self.inert_run, probe.timestamp, promoted)
        after_totals, after_timers = _acc_readings(state, self.ctx.sources)
        timer_moves = _timer_moves(self.ctx, probe.timers, after_timers)
        if timer_moves is None:
            return None
        if not self._sole_timer_writes(forced, acc_writes):
            return None
        unmodeled = self.unmodeled
        if self.predicate_reads is not None:
            read_sources = self.predicate_unmodeled()
            if read_sources is None:
                return None
            unmodeled = unmodeled | read_sources
        acc_scans = _nearest_acc_crossing(
            self.ctx,
            probe.totals,
            after_totals,
            state,
            self.extra_comparisons,
            unmodeled,
        )
        if acc_scans == 1:
            return None
        mod_scans = _nearest_mod_flip(
            self.ctx,
            state,
            self.extra_comparisons,
            probe.mods,
            unmodeled,
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
            probe.timestamp,
        )
        if clock_gap is not None:
            skip = min(skip, clock_gap) if skip is not None else clock_gap
        if timer_moves:
            # Searched no farther than the window is already bounded.
            cap = skip if skip is not None else max_skip
            timer_scans = _nearest_timer_crossing(
                self.ctx, timer_moves, state, self.extra_comparisons, cap, unmodeled
            )
            if timer_scans is not None:
                skip = min(skip, timer_scans - 1) if skip is not None else timer_scans - 1
        if max_skip is not None:
            if skip is not None:
                skip = min(skip, max_skip)
            elif endpoint_is_boundary:
                skip = max_skip
        # An RTC tick or a self-calc's wrap only shortens a window something
        # else bounds; it never creates one.  A window with no other bound is
        # not folded at all, so a stop the fold cannot see (a callable
        # predicate) is still judged after every scan there.
        if skip is not None:
            wrap = _selfcalc_wrap_bound(self.ctx, probe.totals, after_totals, state)
            if wrap is not None:
                skip = min(skip, wrap - 1)
            rtc_gap = self._rtc_gap(state, probe, rtc_at, skip, forced)
            if rtc_gap is not None:
                skip = min(skip, rtc_gap)
        if skip is None or skip < min_skip:
            return None
        timer_landing = _timer_landing(timer_moves, skip, self.ctx.normal_dt)
        if timer_landing is None:
            return None
        return _FoldPlan(
            skip=skip,
            after_totals=after_totals,
            promoted=promoted,
            pre_fold_timestamp=state.timestamp,
            timer_landing=timer_landing,
        )

    def _sole_timer_writes(
        self, forced: Collection[str] | None, acc_writes: Mapping[str, int] | None
    ) -> bool:
        """Whether the probe wrote each timed accumulator at most once, unforced.

        A second write in one scan (a reset, then the timing step) leaves the
        boundary readings where they were while the landing scan would time the
        whole window at once.  Forces are writes around the scan too.  The
        window's scans run the same writers as the probe: which rungs act is
        fixed by the plateau guard and the crossings.
        """
        ctx = self.ctx
        names = ctx.timed_acc_names
        if not names:
            return True
        if forced is None or not names.isdisjoint(forced):
            return False
        if acc_writes is None or not names <= acc_writes.keys():
            return not ctx.contested_timers
        return all(acc_writes[name] <= 1 for name in names)

    def _rtc_gap(
        self,
        state: SystemState,
        probe: _FoldProbe,
        rtc_at: Callable[[float], datetime] | None,
        cap: int,
        forced: Collection[str] | None,
    ) -> int | None:
        """The RTC tick bound on a window already bounded at *cap* scans.

        Ticks of every read field bound the window (see
        :func:`_scans_to_rtc_tick`) unless the time-mirror proof holds
        (:func:`_rtc_hard_fields`).  That runtime judgement is only made when
        the next tick would actually shorten the window.
        """
        ctx = self.ctx
        if not ctx.rtc_fields:
            return None
        gap = _scans_to_rtc_tick(ctx, state, probe.timestamp, rtc_at, ctx.rtc_fields)
        if gap is None or ctx.time_mirrors is None or gap >= cap:
            return gap
        hard = _rtc_hard_fields(ctx, state, (state.tags,), forced)
        if hard == ctx.rtc_fields:
            return gap
        return _scans_to_rtc_tick(ctx, state, probe.timestamp, rtc_at, hard)

    def finish(
        self,
        state: SystemState,
        probe: _FoldProbe,
        plan: _FoldPlan,
        *,
        patches: tuple[tuple[str, Any], ...] = (),
    ) -> _FoldAdvance:
        """Update window-local clock evidence after an executor lands *plan*.

        A landing reads no clock edge that is not yet confirmed inert (the
        window stops before it), so it only ever clears the evidence.
        """
        if not _visible_items_match(state, probe.visible, self.exclude):
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
        on_kernel_scan: Callable[[int], None] | None = None,
        capture_kernel_scan: _CaptureDecision = False,
        capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
    ) -> _FoldAdvance | None:
        """Fold after a completed probe, or return ``None`` if proof declines."""
        if _scan_callbacks(runner, self.exclude)[0]:
            return None
        harness_scan = _harness_nearest_scan(runner)
        plan = self.plan(
            runner._state,
            probe,
            max_skip=max_skip,
            min_skip=min_skip,
            harness_scan=harness_scan,
            rtc_at=runner._rtc_at_sim_time,
            forced=runner._input_overrides.forces_mutable.keys(),
            acc_writes=runner._last_acc_writes,
        )
        if plan is None:
            return None
        patches = _do_fold(
            runner,
            plan.skip,
            self.ctx,
            probe.totals,
            plan.after_totals,
            on_kernel_scan=on_kernel_scan,
            capture_kernel_scan=capture_kernel_scan,
            capture_sink=capture_sink,
            timer_landing=plan.timer_landing,
        )
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
    on_kernel_scan: Callable[[int], None] | None = None,
    capture_kernel_scans: _CaptureDecision = False,
    capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
    extra_unmodeled: Collection[str] = (),
    trace_reads: bool = False,
) -> SystemState:
    """Fold-aware ``run_until`` loop.

    Steps scan-by-scan like the original, but when a plateau is detected
    (only accumulators changed), computes the nearest crossing and folds
    forward.  Respects ``when().pause()`` breakpoints and ``max_cycles``.

    *extra_comparisons* carries thresholds the predicate reads on excluded
    (accumulator / mod-wrap) tags so the fold lands on them exactly instead of
    overshooting to the preset.  *extra_unmodeled* names the coordinates it
    reads any other way.  With *trace_reads* (an opaque callable) every
    judgement records the predicate's reads, and only the values it read
    bound or decline the next window (see :func:`_traced_call`).
    """
    strategy = _OrdinaryFoldStrategy(fold_ctx, extra_comparisons, extra_unmodeled)

    def judge(state: SystemState) -> bool:
        if not trace_reads:
            return bool(predicate(state))
        hit, strategy.predicate_reads = _traced_call(predicate, runner)
        return hit

    used = 0
    kernel_scans = 0
    macro_folds = 0
    while used < max_cycles:
        # ── Probe: one normal scan ───────────────────────────────
        runner._consume_pause_request()
        probe = strategy.capture(runner)
        runner._run_single_scan(
            consume_pause_request=False,
            capture_execution=_capture_requested(capture_kernel_scans),
            capture_sink=capture_sink,
        )
        if on_kernel_scan is not None:
            on_kernel_scan(runner._state.scan_id)
        used += 1
        kernel_scans += 1

        pause_requested = runner._consume_pause_request()
        if judge(runner._state) or pause_requested:
            break

        if used >= max_cycles:
            break

        advance = strategy.try_fold(
            runner,
            probe,
            max_skip=max_cycles - used,
            on_kernel_scan=on_kernel_scan,
            capture_kernel_scan=capture_kernel_scans,
            capture_sink=capture_sink,
        )
        if advance is not None:
            used += advance.logical_scans
            kernel_scans += advance.kernel_scans
            macro_folds += 1
            if advances is not None:
                advances.extend(advance.patches)
            pause_requested = runner._consume_pause_request()
            if judge(runner._state) or pause_requested:
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


def _stepped_scans_until(start: float, target: float, dt: float) -> int | None:
    """Scans an unfolded ``run_for`` takes from *start* to reach *target*.

    Stepping advances the timestamp by repeated ``+ dt``; a fold lands at
    ``T0 + skip * dt``.  The two float paths differ in their last bits, so
    stopping when the folded timestamp reaches *target* can take one scan more
    or fewer than stepping.  Replaying the additions (cheap next to any scan)
    gives the exact scan count instead.  ``None`` for an unusable *dt* or an
    implausibly long span.
    """
    if dt <= 0 or not math.isfinite(dt) or (target - start) / dt > 5_000_000:
        return None
    t = start
    n = 0
    while t < target:
        t += dt
        n += 1
    return n


def fold_run_for(
    runner: PLC,
    seconds: float,
    *,
    fold_ctx: _FoldContext,
) -> SystemState:
    """Fold-aware ``run_for`` loop.

    Same plateau/fold logic as ``fold_run_until``.  In fixed-step mode it runs
    exactly the scans an unfolded ``run_for`` would (see
    :func:`_stepped_scans_until`); otherwise it terminates on
    ``state.timestamp >= target_time``.
    """
    from pyrung.core.time_mode import TimeMode

    strategy = _OrdinaryFoldStrategy(fold_ctx)
    target_time = runner._state.timestamp + seconds
    stop_scans: int | None = None
    if runner._time_mode == TimeMode.FIXED_STEP and runner._dt == fold_ctx.normal_dt:
        stop_scans = _stepped_scans_until(runner._state.timestamp, target_time, runner._dt)
    used = 0

    def reached() -> bool:
        if stop_scans is not None:
            return used >= stop_scans
        return runner._state.timestamp >= target_time

    while not reached():
        # ── Probe: one normal scan ───────────────────────────────
        runner._consume_pause_request()
        probe = strategy.capture(runner)
        runner._run_single_scan(consume_pause_request=False)
        used += 1

        pause_requested = runner._consume_pause_request()
        if reached() or pause_requested:
            break

        # Constrain so dt doesn't overshoot the time target.
        max_skip = None
        if stop_scans is not None:
            max_skip = stop_scans - used
        elif fold_ctx.normal_dt > 0:
            remaining_scans = int((target_time - runner._state.timestamp) / fold_ctx.normal_dt)
            if remaining_scans > 0:
                max_skip = remaining_scans

        advance = strategy.try_fold(runner, probe, max_skip=max_skip)
        if advance is not None:
            used += advance.logical_scans
            pause_requested = runner._consume_pause_request()
            if reached() or pause_requested:
                break

    return runner._state
