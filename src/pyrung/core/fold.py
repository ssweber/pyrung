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
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
        return math.isfinite(span[0]) and math.isfinite(span[1])
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
        # in a window scan.
        if cond._edge_fn is not None or isinstance(cond.tag, ImmediateRef):
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
    opaque_blocks: set[str] = set()
    for pointer, (block_name, _start, _end) in pdg.pointer_tags.items():
        ptr = pdg.tags.get(pointer)
        if ptr is None or (
            getattr(ptr, "choices", None) is None
            and (getattr(ptr, "min", None) is None or getattr(ptr, "max", None) is None)
        ):
            opaque_blocks.add(block_name)
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
        for src in _collect_acc_sources(syn_program):
            if src.acc_name in acc_names:
                continue
            sources.append(src)
            acc_names = acc_names | {src.acc_name}

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
        reads_first_scan=reads_first_scan,
        rtc_fields=rtc_fields,
        rtc_condition_fields=rtc_condition_fields,
        time_mirrors=time_mirrors,
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

    *before_ts* and *after_ts* are the timestamps of the states on either side
    of the span.  A scan reads the clock at its *start*, one normal ``dt``
    before the state it commits (a fold landing likewise reads at the last
    folded scan's start), so the toggles a span's scans actually executed lie
    between ``before_ts - dt`` and ``after_ts - dt``.  A toggle between the
    last read and the committed timestamp has not run yet and is not counted.
    """
    from pyrung.core.system_points import clock_phase

    dt = ctx.normal_dt
    for name, hp in _window_soft_clocks(ctx, promoted):
        if name in inert_soft:
            continue
        toggles = clock_phase(after_ts - dt, hp) - clock_phase(before_ts - dt, hp)
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
    *,
    on_kernel_scan: Callable[[int], None] | None = None,
    capture_kernel_scan: _CaptureDecision = False,
    capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
) -> tuple[tuple[str, Any], ...]:
    """Fold ``skip`` pure-accumulation scans into one real step.

    Timers ride the dt knob (the interpreter does ``skip`` scans of dt in the
    one step); per-scan counters can't be moved by time, so their accumulators
    are patched forward by ``(skip-1)*delta`` and the step's own ``execute``
    supplies the final increment, keeping every source in phase.

    The landing scan stands in for the last of the ``skip`` scans, so it
    starts at the timestamp that scan would have read, ``T0 + (skip - 1) *
    dt``: every time-derived read (``rtc.*`` fields, system clocks, edge
    memory) resolves as it would stepping.  It commits one normal step later,
    at ``T0 + skip * dt``.

    Uses ``_run_single_scan(consume_pause_request=False)`` so the caller
    retains control over pause consumption.
    """
    patches = _fold_patches(runner.state, skip, ctx, before_tot, after_tot)
    if patches:
        runner.patch(patches)

    pre_landing = runner._state
    start_timestamp = pre_landing.timestamp
    positioned = pre_landing
    if skip > 1:
        positioned = pre_landing.set(timestamp=start_timestamp + (skip - 1) * ctx.normal_dt)
        _replace_tip_state(runner, positioned)
        runner._landing_commit_dt = ctx.normal_dt
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
    if on_kernel_scan is not None:
        on_kernel_scan(runner._state.scan_id)

    _replace_tip_state(
        runner,
        runner._state.set(
            scan_id=runner._state.scan_id + skip - 1,
            timestamp=start_timestamp + skip * ctx.normal_dt,
        ),
    )
    if skip > 1:
        runner._checkpoint_fold_landing()
    return tuple(patches.items())


def _replace_tip_state(runner: PLC, state: SystemState) -> None:
    """Install *state* (same tags as the tip) as the tip without a scan.

    Keeps the runner's plain-dict read mirror of the tip tags valid, since only
    the scan id or timestamp changed.
    """
    if runner._state_tags_read_cache_state is runner._state:
        runner._state_tags_read_cache_state = state
    runner._state = state


@dataclass(frozen=True)
class _FoldProbe:
    """State captured immediately before one ordinary probe scan."""

    totals: dict[str, float]
    visible: Any
    timestamp: float
    scan_id: int


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
            | (ctx.time_mirrors.names if ctx.time_mirrors is not None else frozenset())
        )
        self.inert_soft: set[str] = set()
        self.inert_run: dict[str, int] = {}

    def capture(self, runner: PLC) -> _FoldProbe:
        """Capture the entry side of one normal probe scan.

        ``visible`` omits only coordinates whose evolution has a separate,
        statically certified projection (accumulators, modular self-calcs,
        affine views, proven unobservable/frozen writes, and clock mirrors the
        landing scan rebuilds, see :class:`_TimeMirrors`). Everything else
        is the plateau guard: one changed value declines the fold.
        """
        return self.capture_state(runner._state)

    def capture_state(self, state: SystemState) -> _FoldProbe:
        """Capture a probe from a state-only executor such as compiled replay."""
        return _FoldProbe(
            totals=_acc_totals(state, self.ctx.sources),
            visible=_visible_items(state, self.exclude),
            timestamp=state.timestamp,
            scan_id=state.scan_id,
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
    ) -> _FoldPlan | None:
        """Certify a macro landing after a completed probe scan.

        Replay may set ``endpoint_is_boundary`` when ``max_skip`` denotes one
        exact requested state or the scan immediately preceding a recorded
        input event.  Live run loops leave it false: their budget is not a
        semantic program boundary, and preserving their established cadence
        matters to PILOT's bounded correction lifecycle.

        *rtc_at* resolves the executor's RTC at a simulation time.  A program
        that reads an ``rtc.*`` field cannot fold without it.  *forced* names
        the executor's forced tags; without it every RTC tick stays a bound.
        """
        if not _visible_items_match(state, probe.visible, self.exclude):
            self.inert_soft.clear()
            self.inert_run.clear()
            return None
        if probe.scan_id == 0 and self.ctx.reads_first_scan:
            # The probe read first_scan True; the window would read it False.
            # Its unchanged state proves nothing about the window's scans.
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
            probe.timestamp,
        )
        if clock_gap is not None:
            skip = min(skip, clock_gap) if skip is not None else clock_gap
        if max_skip is not None:
            if skip is not None:
                skip = min(skip, max_skip)
            elif endpoint_is_boundary:
                skip = max_skip
        # An RTC tick only shortens a window something else bounds; it never
        # creates one.  A window with no other bound is not folded at all, as
        # without an RTC reader, so a stop predicate the fold cannot see (a
        # callable) is still judged after every scan there.
        if skip is not None:
            rtc_gap = self._rtc_gap(state, probe, rtc_at, skip, forced)
            if rtc_gap is not None:
                skip = min(skip, rtc_gap)
        if skip is None or skip < min_skip:
            return None
        return _FoldPlan(
            skip=skip,
            after_totals=after_totals,
            promoted=promoted,
            pre_fold_timestamp=state.timestamp,
        )

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
        on_kernel_scan: Callable[[int], None] | None = None,
        capture_kernel_scan: _CaptureDecision = False,
        capture_sink: Callable[[int, ConditionViewCapture], None] | None = None,
    ) -> _FoldAdvance | None:
        """Fold after a completed probe, or return ``None`` if proof declines."""
        harness_scan = _harness_nearest_scan(runner)
        plan = self.plan(
            runner._state,
            probe,
            max_skip=max_skip,
            min_skip=min_skip,
            harness_scan=harness_scan,
            rtc_at=runner._rtc_at_sim_time,
            forced=runner._input_overrides.forces_mutable.keys(),
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
        if predicate(runner._state) or pause_requested:
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
