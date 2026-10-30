"""Black-box timers: the prover's timing model, and replay of what it finds.

The search knows nothing about time.  The prover's kernel is compiled with
``abstract_timers`` (``circuitpy.codegen.compile_kernel``): each timed tick
(on-delay, off-delay, time drum step, the Out family's debounce, flasher and
timed internals) calls ``_bb_tick`` instead of adding the scan's dt.  The
instruction keeps its enable, reset and preset logic; only how far the
accumulator moves on this scan is a choice:

==============================  ===============================================
accumulator ``a``, limit ``P``  values the tick may take
==============================  ===============================================
``a < P``                       ``a`` (no visible progress) or ``P`` (done)
``a >= P``, ``a == 0``          ``0`` or ``1`` (progress past a preset <= 0)
``a >= P`` otherwise            ``a``
read by a plain comparison      also every value above ``a`` it splits at
read any other way              also every value up to the limit (a sweep)
==============================  ===============================================

Who reads an accumulator is decided by tag reference over every condition,
data read and search expression (``accumulator_reads``), not the atom index:
``T.Acc * 2 > 700`` is an opaque atom there.  The same reads decide a
counter's ``min(Acc, preset)`` key clamp (``count_clamps``).

So an enabled on-delay's Done may go False -> True on any scan, an off-delay's
may go True -> False on any scan while disabled, and a drum step may end on
any scan it runs.  Every tick moves forward (a real one adds ``dt >= 0``), and
every choice is a real scan of some length, so each abstract run is a real run
under some sequence of scan times: the search over-approximates every timing a
CLICK can show.  The limit is read at the tick, so a tag preset needs nothing
resolved.  Counters count edges, not time: they stay concrete.

The search enumerates the choices of one scan depth-first (``choice_runs``):
a run with every tick holding, then each tick moved in turn.  Ticks of timers
nothing reads never branch (``_TimerModel.branch``).

Replay
------
An abstract counterexample may need a timing the PLC cannot show (a 30 ms
timer done before a 1000 ms one started with it).  ``confirm`` replays every
trace on the concrete PLC at the search's fixed ``dt``: a step where no tick
moved is one scan; a step where one did runs, inputs held, until the state the
search compares (``_compared_names``) changes, folding long waits.  If the
first try lands elsewhere, the step's input change is moved to the scan the
state would change on, and then the wait is moved before the plain steps
leading up to it (``_slide_wait``: a timer that must finish two scans after
a button press starts earlier).  A replay that reaches a state violating the
property is a ``Counterexample`` with the concrete trace; one that does not is
an ``Intractable`` of kind ``"timing"`` carrying the abstract trace and the
step where replay diverged.

Under ``settled=True`` the replay accepts a violation only in a state no
timer was left running short of its limit (``_concretely_settled``), as the
search judges it.  A trace is replayed whenever the model has a timer that
can tick, even with no tick moved: holding a running timer is a timing
choice too.  A property's ``rise()``/``fall()`` the search leaves undecided is
decided on the PLC's scans; a counterexample standing only on one is an
``Intractable`` of kind ``"property"``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

from .results import Counterexample, Intractable, TraceStep

if TYPE_CHECKING:
    from pyrung.core.kernel import ReplayKernel
    from pyrung.core.program import Program
    from pyrung.core.time_mode import TimeUnit

    from . import _ExploreContext

# Memory key the kernel's ``_bb_tick`` looks up for its oracle.
ORACLE_KEY = "_bb"

# Choice runs of one input assignment at one state before the search refuses.
_CHOICE_RUN_CAP = 1 << 14

# Longest wait (scans) one replay step runs for a timer before giving up.
_REPLAY_SCAN_CAP = 200_000

# Widest jump an exactly-read accumulator's tick offers in one scan.
_EXACT_SPAN = 1000

# Scans a replay step waits for a free system input (a clock bit) to change.
_SYSTEM_WAIT = 1024

# Changed inputs of one replay step up to which every early/late split is tried.
_SPLIT_INPUT_CAP = 4


@dataclass(frozen=True)
class TimedSite:
    """Every timed tick of one accumulator."""

    acc_name: str
    owner: str
    verb: str
    unit: TimeUnit
    literal_presets: tuple[int, ...] = ()
    # A drum's completion flag: once on, its last step's tick is no timer
    # still running (``_concretely_settled``).
    completion: str | None = None


def timed_sites(program: Program) -> dict[str, TimedSite]:
    """The accumulators a timed instruction ticks, by name."""
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )
    from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
    from pyrung.core.validation._common import walk_instructions

    sites: dict[str, TimedSite] = {}

    def add(
        acc: Any,
        owner: Any,
        verb: str,
        unit: Any,
        presets: tuple[Any, ...],
        completion: str | None = None,
    ) -> None:
        literal = tuple(int(p) for p in presets if isinstance(p, int) and not isinstance(p, bool))
        name = acc.name
        prior = sites.get(name)
        if prior is not None:
            literal = tuple(sorted({*prior.literal_presets, *literal}))
            sites[name] = replace(prior, literal_presets=literal)
            return
        sites[name] = TimedSite(
            name, owner.name, verb, unit, tuple(sorted(set(literal))), completion
        )

    for instr in walk_instructions(program):
        if isinstance(instr, OnDelayInstruction):
            add(instr.accumulator, instr.done_bit, "on_delay", instr.unit, (instr.preset,))
        elif isinstance(instr, OffDelayInstruction):
            add(instr.accumulator, instr.done_bit, "off_delay", instr.unit, (instr.preset,))
        elif isinstance(instr, (TimeDrumInstruction, TimeEventDrumInstruction)):
            verb = "time_event_drum" if isinstance(instr, TimeEventDrumInstruction) else "time_drum"
            presets = tuple(p for p in instr.presets if p is not None)
            add(
                instr.accumulator,
                instr.current_step,
                verb,
                instr.unit,
                presets,
                instr.completion_flag.name,
            )
        elif isinstance(instr, DebounceInstruction):
            add(instr.on_acc, instr.target, "debounce", instr.unit, (instr.on_preset,))
            add(instr.off_acc, instr.target, "debounce", instr.unit, (instr.off_preset,))
        elif isinstance(instr, FlasherInstruction):
            add(instr.on_acc, instr.target, "flasher", instr.unit, (instr.on_preset,))
            add(instr.off_acc, instr.target, "flasher", instr.unit, (instr.off_preset,))
        elif isinstance(instr, TimedInstruction):
            add(instr.timer_acc, instr.target, "timed", instr.unit, (instr.on_preset,))
    return sites


def timer_held_tags(program: Program) -> frozenset[str]:
    """Bits a timed instruction sets and holds across scans.

    A drum's completion flag and an Out debounce/flasher/timed coil's target
    are written by the instruction alone, often read by nothing but the
    property, and keep their value until the instruction changes it: under
    black-box timers they are state, whatever their readers.
    """
    from pyrung.core.instruction.drums import TimeDrumInstruction, TimeEventDrumInstruction
    from pyrung.core.instruction.out_variants import (
        DebounceInstruction,
        FlasherInstruction,
        TimedInstruction,
    )
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import walk_instructions

    held: set[str] = set()
    for instr in walk_instructions(program):
        if isinstance(instr, (TimeDrumInstruction, TimeEventDrumInstruction)):
            held.add(instr.completion_flag.name)
        elif isinstance(instr, (DebounceInstruction, FlasherInstruction, TimedInstruction)):
            target = instr.target
            if isinstance(target, Tag):
                held.add(target.name)
    return frozenset(held)


@dataclass(frozen=True)
class AccumulatorReads:
    """How anything but its own instruction reads each accumulator.

    ``read``: some rung, instruction condition, data read, search expression
    (the property) or outside observer reads it.  ``swept``: some read is one
    the values it compares against cannot stand for (arithmetic, a data read,
    an observer, a comparison with a tag whose values the search does not
    fix): every value is told apart.  ``values``: for every other read
    accumulator, the values its plain comparisons split at.
    """

    read: frozenset[str] = frozenset()
    swept: frozenset[str] = frozenset()
    values: Mapping[str, tuple[int, ...]] = field(default_factory=dict)


_PLAIN_COMPARES: tuple[type, ...] = ()
_COMPARE_FORMS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


def _plain_compare_types() -> tuple[type, ...]:
    global _PLAIN_COMPARES
    if not _PLAIN_COMPARES:
        from pyrung.core.condition import (
            CompareEq,
            CompareGe,
            CompareGt,
            CompareLe,
            CompareLt,
            CompareNe,
        )

        _PLAIN_COMPARES = (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)
    return _PLAIN_COMPARES


def _split_points(value: Any) -> tuple[int, ...] | None:
    """Where an integer compared with *value* changes truth: ``floor(v)``, ``floor(v)+1``."""
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    low = math.floor(value)
    return (low, low + 1)


def accumulator_reads(
    program: Program,
    graph: Any,
    all_exprs: list[Any],
    names: frozenset[str],
    *,
    observed: frozenset[str],
    observe_all: bool,
    domains: Mapping[str, tuple[Any, ...]],
) -> AccumulatorReads:
    """Which of *names* are read other than by their own instruction, and how.

    By tag reference over every condition, data read and search expression,
    not the atom index: a comparison inside arithmetic (``T.Acc * 2 > 700``)
    is an opaque atom there, and a timer whose accumulator only such a
    comparison reads would otherwise get no in-between values.  The program
    graph's readers are the backstop: a read none of the walks below explains
    is swept.  *domains* are the complete value sets of the other tags a
    comparison may name (never a sizing domain); one without a set sweeps.
    """
    from pyrung.core.analysis.pdg import _extract_tag_names
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Or
    from pyrung.core.condition import AllCondition, AnyCondition
    from pyrung.core.instruction.control import ForLoopInstruction
    from pyrung.core.tag import Tag
    from pyrung.core.validation._common import iter_rungs, walk_instructions

    from .absorb import _has_forbidden_data_read

    if not names:
        return AccumulatorReads()
    plain = _plain_compare_types()
    read: set[str] = set()
    swept: set[str] = set()
    values: dict[str, set[int]] = {}

    def opaque_refs(obj: Any) -> None:
        hit = _extract_tag_names(obj, {}) & names
        read.update(hit)
        swept.update(hit)

    def compared(acc: str, other: Any) -> None:
        read.add(acc)
        if isinstance(other, Tag):
            domain = None if other.name in names else domains.get(other.name)
            points: set[int] = set()
            for value in domain or ():
                split = _split_points(value)
                if split is None:
                    domain = None
                    break
                points.update(split)
            if not domain:
                swept.add(acc)
                return
            values.setdefault(acc, set()).update(points)
            return
        split = _split_points(other)
        if split is None:
            swept.add(acc)
            return
        values.setdefault(acc, set()).update(split)

    def walk(obj: Any) -> None:
        if obj is None:
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item)
            return
        if isinstance(obj, (AllCondition, AnyCondition)):
            walk(obj.conditions)
            return
        if isinstance(obj, plain):
            tag, other = obj.tag, obj.value  # type: ignore[attr-defined]
            for side, against in ((tag, other), (other, tag)):
                if not isinstance(side, Tag):
                    # A literal, or an expression or reference: what it
                    # reads is read through arithmetic.
                    opaque_refs(side)
                elif side.name in names:
                    compared(side.name, against)
            return
        # Any other condition, a bare tag read as a truth value, an
        # expression: every tag in it is read in a way no value set covers.
        opaque_refs(obj)

    def walk_rung(rung: Any) -> None:
        walk(list(rung._conditions))
        for branch in rung._branches:
            walk_rung(branch)

    for _loc, rung in iter_rungs(program):
        walk(list(rung._conditions))
    for instr in walk_instructions(program):
        for field_name in getattr(type(instr), "_conditions", ()):
            walk(getattr(instr, field_name, None))
        if isinstance(instr, ForLoopInstruction):
            for body_rung in instr.body_rungs:
                walk_rung(body_rung)

    # Data reads (copies, math, indices, an unbounded index that can reach it).
    for node in graph.rung_nodes:
        hit = node.data_reads & names
        read.update(hit)
        swept.update(hit)
    for name in names:
        if _has_forbidden_data_read(program, graph, name):
            read.add(name)
            swept.add(name)

    # Search expressions: the property's atoms, and the rungs' as simplified.
    def walk_expr(expr: Any) -> None:
        if isinstance(expr, (And, Or)):
            for term in expr.terms:
                walk_expr(term)
        elif isinstance(expr, ArithAtom):
            hit = {expr.left, expr.right} & names
            read.update(hit)
            swept.update(hit)
        elif isinstance(expr, Atom):
            if expr.unsupported is not None:
                opaque_refs(expr.unsupported)
            plain_form = expr.form in _COMPARE_FORMS and (
                expr.operand_scale == 1 and expr.operand_offset == 0
            )
            other_name = expr.operand if expr.operand_is_tag else None
            for acc, other in ((expr.tag, other_name), (other_name, expr.tag)):
                if acc not in names:
                    continue
                if not plain_form:
                    read.add(acc)
                    swept.add(acc)
                elif other is None:
                    compared(acc, expr.operand)
                else:
                    other_tag = graph.tags.get(other)
                    if other_tag is None:
                        read.add(acc)
                        swept.add(acc)
                    else:
                        compared(acc, other_tag)

    for expr in all_exprs:
        walk_expr(expr)

    for name in names:
        if observe_all or name in observed:
            read.add(name)
            swept.add(name)
        elif graph.readers_of.get(name) and name not in read:
            # The graph sees a read none of the walks explains.
            read.add(name)
            swept.add(name)
    return AccumulatorReads(
        read=frozenset(read),
        swept=frozenset(swept),
        values={
            name: tuple(sorted(points))
            for name, points in sorted(values.items())
            if name not in swept
        },
    )


@dataclass(frozen=True)
class _TimerModel:
    """What the search's oracle needs: which ticks branch, and onto what."""

    sites: Mapping[str, TimedSite]
    # Ticks whose choice can matter: every one but an unread timer's.
    branch: frozenset[str]
    # Domain values of an accumulator something compares.
    reps: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    # Accumulators read as a value (copied, computed with, observed from
    # outside): every value is told apart, so a tick may land on any.
    exact: frozenset[str] = frozenset()


def build_timer_model(
    program: Program,
    *,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    combinational: frozenset[str],
    reads: AccumulatorReads,
) -> _TimerModel:
    """The oracle's model: *reads* (``accumulator_reads``) says which ticks land between.

    A swept accumulator is ``exact``; every read one gets classify's domain
    for it as ``reps`` (a swept one's sweep is short per scan, so the domain's
    values, back-propagated through arithmetic, are still worth a jump), and
    one read only by plain comparisons the values they split at too.
    """
    sites = timed_sites(program)
    names = frozenset(sites)
    exact = reads.swept & names
    # An unread timer (classify: its Done is combinational, its accumulator
    # in no key) changes no other tag, whatever its tick does.
    branch = frozenset(
        name
        for name, site in sites.items()
        if name in stateful_dims or site.owner not in combinational or name in reads.read
    )
    reps: dict[str, tuple[int, ...]] = {}
    for name in sorted(reads.read & names):
        domain = stateful_dims.get(name, ())
        values = {int(v) for v in domain if isinstance(v, (int, float)) and not isinstance(v, bool)}
        values.update(reads.values.get(name, ()))
        if values:
            reps[name] = tuple(sorted(values))
    return _TimerModel(sites=sites, branch=branch, reps=reps, exact=exact)


class _TimerOracle:
    """The ``_bb_tick`` choice maker for one scan's run (see module docstring).

    ``begin(forced)`` starts a run: the i-th choice point takes candidate
    ``forced[i]`` (0 = hold) and every later one holds.  After the run,
    ``taken``/``widths`` describe the choice points met, in order.
    """

    __slots__ = ("_branch", "_reps", "_exact", "forced", "taken", "widths", "moved", "open", "met")

    def __init__(self, model: _TimerModel) -> None:
        self._branch = model.branch
        self._reps = model.reps
        self._exact = model.exact
        self.begin(())

    def begin(self, forced: tuple[int, ...]) -> None:
        self.forced = forced
        self.taken: list[int] = []
        self.widths: list[int] = []
        self.moved: list[tuple[str, int]] = []
        # A tick this run left below its limit: a Done choice still open.
        self.open = False
        # Accumulators whose tick was a choice point this run.
        self.met: set[str] = set()

    def __call__(self, key: str, acc: int, limit: int | None) -> int:
        if key not in self._branch:
            return acc
        higher = {r for r in self._reps.get(key, ()) if r > acc}
        if limit is not None:
            if limit > acc:
                higher.add(limit)
            elif acc == 0:
                higher.add(1)
        if key in self._exact:
            # Any value: every one below the limit (a scan of that length),
            # one unit at a time past it (or past a long way to it).
            top = limit if limit is not None and acc < limit <= acc + _EXACT_SPAN else acc + 1
            higher.update(range(acc + 1, top + 1))
        if not higher:
            return acc
        candidates = sorted(higher)
        self.met.add(key)
        i = len(self.taken)
        choice = self.forced[i] if i < len(self.forced) else 0
        self.taken.append(choice)
        self.widths.append(len(candidates) + 1)
        value = acc if choice == 0 else candidates[choice - 1]
        if limit is not None and value < limit:
            self.open = True
        if choice:
            self.moved.append((key, value))
        return value

    def outcome(self) -> tuple[tuple[int, ...], tuple[tuple[str, int], ...], bool]:
        """``(choices, moved, open)`` of the run just made; choices trimmed."""
        taken = self.taken
        end = len(taken)
        while end and not taken[end - 1]:
            end -= 1
        return tuple(taken[:end]), tuple(self.moved), self.open


class ChoiceOverflow(Exception):
    def __init__(self, keys: tuple[str, ...]) -> None:
        super().__init__(keys)
        self.keys = keys


def choice_runs(oracle: _TimerOracle, run: Callable[[], None]) -> Iterator[None]:
    """Run one scan once per sequence of tick choices, depth first.

    *run* restores the pre-scan state and steps the kernel; each yield leaves
    the kernel in one successor, with ``oracle.outcome()`` describing it.
    """
    stack: list[tuple[int, ...]] = [()]
    runs = 0
    while stack:
        forced = stack.pop()
        oracle.begin(forced)
        run()
        taken = tuple(oracle.taken)
        widths = tuple(oracle.widths)
        for j in range(len(taken) - 1, len(forced) - 1, -1):
            for c in range(widths[j] - 1, 0, -1):
                stack.append((*taken[:j], c))
        runs += 1
        if runs > _CHOICE_RUN_CAP:
            raise ChoiceOverflow(tuple(sorted(oracle.met)))
        yield


# Key a counter no further than its preset (``count_clamps``).
# ``PYRUNG_PROVE_COUNT_CLAMPS=0`` turns it off, to measure what it saves.
_COUNT_CLAMPS = os.environ.get("PYRUNG_PROVE_COUNT_CLAMPS", "1") != "0"


def count_clamps(
    program: Program,
    graph: Any,
    stateful_names: tuple[str, ...],
    *,
    reads: AccumulatorReads,
) -> tuple[tuple[int, bool, int], ...]:
    """Counters whose key may stop at the preset: ``(index, counts up, preset)``.

    Counters count concretely.  Past its preset a counter that only counts
    one way stays done until a reset sets it to 0, so states that differ only
    in how far past the preset it is behave alike when nothing else reads the
    accumulator (*reads*, ``accumulator_reads``: no condition, arithmetic,
    data read, property or outside observer), its preset is a literal, and
    one counter instruction writes it.  Keying them as the preset keeps an
    unreset counter from walking the key up to its type's limit.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.validation._common import walk_instructions

    from .absorb import _all_write_targets

    if not _COUNT_CLAMPS:
        return ()
    writers: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for name, _kind in _all_write_targets(instr, graph.indirect_reach):
            writers.setdefault(name, []).append(instr)
    slots = {name: i for i, name in enumerate(stateful_names)}
    clamps: list[tuple[int, bool, int]] = []
    for instr in walk_instructions(program):
        if isinstance(instr, CountUpInstruction):
            if instr.down_condition is not None:
                continue
            up = True
        elif isinstance(instr, CountDownInstruction):
            up = False
        else:
            continue
        acc = instr.accumulator.name
        preset = instr.preset
        if (
            acc not in slots
            or not isinstance(preset, int)
            or isinstance(preset, bool)
            or [w for w in writers.get(acc, ()) if w is not instr]
            or acc in reads.read
        ):
            continue
        clamps.append((slots[acc], up, int(preset)))
    return tuple(sorted(set(clamps)))


def counter_accumulators(program: Program) -> frozenset[str]:
    """Every counter's accumulator (``count_clamps``' candidates)."""
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.validation._common import walk_instructions

    return frozenset(
        instr.accumulator.name
        for instr in walk_instructions(program)
        if isinstance(instr, (CountUpInstruction, CountDownInstruction))
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _compared_names(context: _ExploreContext) -> tuple[str, ...]:
    """State the replay matches step by step: the key's tags but timer ticks.

    A tick's value is a representative (the search moves it straight to a
    preset or a compared value), so the replay does not match it; the Done,
    step or output it decides is matched instead, even where the key leaves
    it out (a Done bit rewritten every scan is elided).  System tags and
    inputs are the PLC's and the trace's.
    """
    model = context.timer_model
    ticks = set(model.sites) if model is not None else set()
    owners = (
        {site.owner for name, site in model.sites.items() if name in model.branch}
        if model is not None
        else set()
    )
    names = {*context.stateful_names, *context.elided_tags, *owners}
    return tuple(
        sorted(
            name
            for name in names
            if name not in ticks
            and name not in SYSTEM_TAGS_BY_NAME
            and name not in context.nondeterministic_dims
            and name in context.graph.tags
        )
    )


def _classed_accumulators(
    context: _ExploreContext,
) -> tuple[tuple[str, tuple[int, ...] | None], ...]:
    """Ticked accumulators the replay matches, and how finely.

    One something reads as a value: exactly (``None``).  One compared: by the
    comparison's values.  Any other, by its literal presets only: its phase
    (an Out flasher's, a timed coil's) is in no tag, so the replay waits for
    the preset as the search crossed it, while a timer the search held at 0
    matches a running one.  A drum's accumulator is left out: its step is a
    tag, and it restarts on each.
    """
    model = context.timer_model
    if model is None:
        return ()
    classed: list[tuple[str, tuple[int, ...] | None]] = []
    for name in sorted(model.sites):
        site = model.sites[name]
        if name in model.exact:
            classed.append((name, None))
        elif name in model.reps:
            classed.append((name, model.reps[name]))
        elif site.literal_presets and site.verb not in ("time_drum", "time_event_drum"):
            classed.append((name, tuple(sorted({0, *site.literal_presets}))))
    return tuple(classed)


def _abstract_states(
    context: _ExploreContext, trace: list[TraceStep]
) -> tuple[dict[str, Any], list[tuple[dict[str, Any], tuple[tuple[str, int], ...]]]]:
    """Re-run the search's kernel along *trace*: the start state, the state after each step."""
    from .kernel import _step_kernel

    assert context.timer_model is not None
    kernel: ReplayKernel = context.compiled.create_kernel()
    oracle = _TimerOracle(context.timer_model)
    kernel.memory[ORACLE_KEY] = oracle
    seed_search_kernel(context, kernel)
    initial = dict(kernel.tags)
    states: list[tuple[dict[str, Any], tuple[tuple[str, int], ...]]] = []
    for step in trace:
        for name, value in step.prev.items():
            kernel.prev[name] = value
        for name, value in step.inputs.items():
            kernel.tags[name] = value
        moved: list[tuple[str, int]] = []
        for _ in range(step.scans):
            oracle.begin(step._choices)
            _step_kernel(context, kernel)
            moved.extend(oracle.moved)
        states.append((dict(kernel.tags), tuple(moved)))
    return initial, states


def seed_search_kernel(context: _ExploreContext, kernel: ReplayKernel) -> None:
    """Power-on seeding the search applies to a fresh kernel (bfs.py)."""
    from .kernel import _seed_synthetic_presets

    _seed_synthetic_presets(context, kernel)
    for name, value in context.outside_tag_defaults:
        kernel.tags[name] = value
    if context.system_points is not None:
        context.system_points.seed(kernel.tags)


@dataclass
class _Replay:
    """A concrete PLC following an abstract trace."""

    program: Program
    context: _ExploreContext
    predicate: Callable[[Any], bool]
    compared: tuple[str, ...]
    defaults: dict[str, Any]
    # System tags the property or the search reads: the PLC resolves them.
    system_names: tuple[str, ...] = ()
    # Timer accumulators something reads, with the values told apart
    # (``None``: every value).
    classed: tuple[tuple[str, tuple[int, ...] | None], ...] = ()
    plc: Any = None
    steps: list[TraceStep] = field(default_factory=list)
    # ``settled=True``: a violation counts only where no running timer is
    # still short of its limit (``accepts``).
    settled: bool = False
    # A condition property with rise()/fall() in it: the search leaves the
    # edges undecided (possibly true), so the replay decides them on the
    # PLC's scans (``violated``); ``negated`` for never().
    edge_expr: Any = None
    negated: bool = False

    def _view_of(self, plc: Any, state: Any) -> Mapping[str, Any]:
        return _StateView(state, self.defaults, frozenset(self.system_names), plc)

    def view(self, plc: Any) -> Mapping[str, Any]:
        return self._view_of(plc, plc.current_state)

    def key_of(self, tags: Mapping[str, Any]) -> tuple[Any, ...]:
        """The compared state: tags as they are, read accumulators by class."""
        defaults = self.defaults
        plain = tuple(tags.get(name, defaults.get(name)) for name in self.compared)
        if not self.classed:
            return plain
        return plain + tuple(
            _value_class(tags.get(name, defaults.get(name)), reps) for name, reps in self.classed
        )

    def key(self, plc: Any) -> tuple[Any, ...]:
        return self.key_of(plc.current_state.tags)

    def violated(self, plc: Any) -> bool:
        if self.edge_expr is None:
            return not self.predicate(self.view(plc))
        from .expr import _eval_expr_from_state

        state = plc.current_state
        before = state
        if state.scan_id > 0:
            try:
                before = plc.history.at(state.scan_id - 1)
            except Exception:  # noqa: BLE001 - no earlier state: no edge decided
                return False
        view = self._view_of(plc, state)
        resolved = _resolve_edges(self.edge_expr, self._view_of(plc, before), view)
        value = _eval_expr_from_state(resolved, view)
        # always(): violated when surely false; never(): when surely true.
        return value is True if self.negated else value is False

    def accepts(self, plc: Any) -> bool:
        """The PLC's state is a violation the search would judge."""
        if not self.violated(plc):
            return False
        return not self.settled or _concretely_settled(self.context, plc)

    def diff(self, plc: Any, target: dict[str, Any]) -> list[str]:
        names = [*self.compared, *(name for name, _ in self.classed)]
        mine = self.key(plc)
        goal = self.key_of(target)
        return [name for name, a, b in zip(names, mine, goal, strict=True) if a != b]

    def run_until_change(
        self, plc: Any, bound: int, target: dict[str, Any] | None = None
    ) -> tuple[int, bool]:
        """Run with inputs held until the compared state changes or the property fails.

        With *target*, until the compared state is the target's instead.
        Returns ``(scans, reached)``; folds the waits in between.
        """
        start_key = self.key(plc)
        goal = self.key_of(target) if target is not None else None
        start_scan = plc.current_state.scan_id

        def reached(key: tuple[Any, ...]) -> bool:
            return key == goal if goal is not None else key != start_key

        def stop(state: Any) -> bool:
            if reached(self.key_of(state.tags)):
                return True
            # Under ``settled`` a violation mid-wait is no stopping point:
            # it may be one a running timer leaves (``accepts``).  Nor is one
            # of a property with edges: the predicate cannot decide them.
            if self.settled or self.edge_expr is not None:
                return False
            return not self.predicate(self._view_of(plc, state))

        plc.run_until(stop, max_cycles=bound)
        scans = plc.current_state.scan_id - start_scan
        return scans, reached(self.key(plc)) or self.accepts(plc)


class _StateView(Mapping[str, Any]):
    """A PLC state as the property reads it, tag by tag.

    Lazy, so a wait reads only what the property reads and fold can still
    skip the scans in between.  Unwritten tags read as their defaults and
    system tags as the PLC resolves them.
    """

    __slots__ = ("_state", "_defaults", "_system", "_plc")

    def __init__(
        self, state: Any, defaults: Mapping[str, Any], system: frozenset[str], plc: Any
    ) -> None:
        self._state = state
        self._defaults = defaults
        self._system = system
        self._plc = plc

    def __getitem__(self, name: str) -> Any:
        if name in self._system:
            resolved, value = self._plc._system_runtime.resolve(name, self._state)
            if resolved:
                return value
        tags = self._state.tags
        if name in tags:
            return tags[name]
        return self._defaults[name]

    def __contains__(self, name: object) -> bool:
        return name in self._state.tags or name in self._defaults or name in self._system

    def __iter__(self) -> Iterator[str]:
        return iter({*self._defaults, *self._state.tags})

    def __len__(self) -> int:
        return len({*self._defaults, *self._state.tags})


def _edge_tags(expr: Any) -> frozenset[str]:
    """Tags a rise()/fall() atom of *expr* reads."""
    from pyrung.core.analysis.simplified import And, Atom, Or

    if isinstance(expr, Atom):
        return frozenset({expr.tag}) if expr.form in ("rise", "fall") else frozenset()
    if isinstance(expr, (And, Or)):
        return frozenset().union(*(_edge_tags(term) for term in expr.terms))
    return frozenset()


def _resolve_edges(expr: Any, before: Mapping[str, Any], after: Mapping[str, Any]) -> Any:
    """*expr* with each rise()/fall() decided from the scan *before* -> *after*."""
    from pyrung.core.analysis.simplified import And, Atom, Const, Or

    if isinstance(expr, Atom) and expr.form in ("rise", "fall"):
        try:
            was, now = bool(before[expr.tag]), bool(after[expr.tag])
        except KeyError:
            return expr
        return Const((not was and now) if expr.form == "rise" else (was and not now))
    if isinstance(expr, And):
        return And(tuple(_resolve_edges(term, before, after) for term in expr.terms))
    if isinstance(expr, Or):
        return Or(tuple(_resolve_edges(term, before, after) for term in expr.terms))
    return expr


def _value_class(value: Any, reps: tuple[int, ...] | None) -> Any:
    """*value* as the search tells it apart: itself, or the largest rep at or below it."""
    if reps is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    below = [r for r in reps if r <= value]
    return below[-1] if below else None


def _split_system(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(patchable, system)``: the PLC sets system tags itself (clocks, mode)."""
    patchable: dict[str, Any] = {}
    system: dict[str, Any] = {}
    for name, value in inputs.items():
        (system if name in SYSTEM_TAGS_BY_NAME else patchable)[name] = value
    return patchable, system


def _run_scans(plc: Any, scans: int) -> None:
    if scans > 0:
        plc.run_until(lambda _state: False, max_cycles=scans)


def _wait_bound(context: _ExploreContext, moved: tuple[tuple[str, int], ...]) -> int:
    """Scans a step's moved ticks can take to get where the search put them."""
    model = context.timer_model
    assert model is not None
    total = 2
    for key, value in moved:
        site = model.sites.get(key)
        rate = site.unit.dt_to_units(context.dt) if site is not None else 0.0
        if rate <= 0 or not math.isfinite(rate):
            return _REPLAY_SCAN_CAP
        total += math.ceil(max(value, 1) / rate) + 1
    # A timer enabled on some scans only (a duty cycle) takes longer.
    return min(4 * total + 16, _REPLAY_SCAN_CAP)


def _concretely_settled(context: _ExploreContext, plc: Any) -> bool:
    """No timer the search branches on was left running short of its limit.

    The search's ``settled`` (``_TimerOracle.open``): no tick of the scan that
    made the state ended below its limit.  On the PLC: a tick that moved its
    accumulator (with its ``_frac:`` carry) forward this scan without being
    done (an on-delay's Done still off, an off-delay's still on; any other
    timed instruction mid-step).  Unknown history counts as unsettled.
    """
    model = context.timer_model
    if model is None or not model.branch:
        return True
    state = plc.current_state
    if state.scan_id <= 0:
        return False
    try:
        before = plc.history.at(state.scan_id - 1)
    except Exception:  # noqa: BLE001 - no earlier state: cannot tell
        return False
    defaults = {name: tag.default for name, tag in context.graph.tags.items()}

    def progress(snapshot: Any, name: str) -> float:
        acc = snapshot.tags.get(name, defaults.get(name, 0)) or 0
        frac = snapshot.memory.get(f"_frac:{name}", 0.0) or 0.0
        return float(acc) + float(frac)

    for name in model.branch:
        site = model.sites[name]
        if progress(state, name) <= progress(before, name):
            continue
        if site.completion is not None and state.tags.get(
            site.completion, defaults.get(site.completion, False)
        ):
            # A completed drum: its last step's accumulator keeps counting.
            continue
        done = bool(state.tags.get(site.owner, defaults.get(site.owner, False)))
        if site.verb == "on_delay" and done:
            continue
        if site.verb == "off_delay" and not done:
            continue
        return False
    return True


def confirm(
    program: Program,
    context: _ExploreContext,
    predicate: Callable[[dict[str, Any]], bool],
    result: Counterexample,
    *,
    settled: bool = False,
) -> Counterexample | Intractable:
    """Replay an abstract counterexample on the concrete PLC (module docstring).

    With *settled* (``always(settled=True)``) a violation is accepted only in
    a state no running timer has left short of its limit
    (``_concretely_settled``), as the search judged it.
    """
    from pyrung.core import PLC

    # The search's root link is a zero-scan step with no inputs.
    trace = [step for step in result.trace if step.scans > 0 or step.inputs]
    defaults = {name: tag.default for name, tag in context.graph.tags.items()}
    replay = _Replay(
        program=program,
        context=context,
        predicate=predicate,
        compared=_compared_names(context),
        classed=_classed_accumulators(context),
        defaults=defaults,
        system_names=tuple(
            sorted(
                name
                for name in {*context.graph.tags, *context.nondeterministic_dims}
                if name in SYSTEM_TAGS_BY_NAME
            )
        ),
        plc=PLC(program, dt=context.dt),
        settled=settled,
    )
    expr = getattr(predicate, "_pyrung_expr", None)
    if expr is not None and _edge_tags(expr):
        replay.edge_expr = expr
        replay.negated = bool(getattr(predicate, "_pyrung_negated", False))
    model = context.timer_model
    if (model is None or not model.branch) and any(_split_system(step.inputs)[1] for step in trace):
        # No timer that could tick or hold, only free system inputs (clocks,
        # the scan counter, the RTC) the PLC sets itself: the search's
        # caveated counterexample as before (bfs._free_system_caveat).
        # Waiting for an RTC hour on the PLC is no check worth its scans.
        # A timer the trace holds is a timing choice even where no tick
        # moved, so with one in the model the trace is replayed.
        return result
    if not trace:
        # The power-on state itself.
        if replay.accepts(replay.plc):
            return replace(result, trace=[TraceStep(inputs={}, scans=0)])
        return _unconfirmed(
            context, result, trace, 0, ["power-on state"], edges=_edge_tags(replay.edge_expr)
        )

    initial, abstract = _abstract_states(context, trace)
    diverged_at: int | None = None
    diverged_tags: list[str] = []
    previous = initial
    # The PLC before each step (and the concrete steps taken by then), and
    # whether the step is plain scans (no tick moved, no system input).
    checkpoints: list[tuple[Any, int]] = []
    plain: list[bool] = []
    for index, (step, (target, moved)) in enumerate(zip(trace, abstract, strict=True)):
        inputs, system = _split_system(step.inputs)
        # A system input the search changed (a clock bit, the scan counter)
        # is time the PLC must let pass, like a timer's.
        system_moved = any(previous.get(name) != value for name, value in system.items())
        previous = target
        plc = replay.plc
        checkpoints.append((plc.fork(), len(replay.steps)))
        plain.append(not moved and not system_moved)
        if not moved and not system_moved:
            plc.patch(inputs)
            _run_scans(plc, step.scans)
            replay.steps.append(TraceStep(inputs=inputs, scans=step.scans))
            if replay.accepts(plc):
                return _confirmed(result, replay)
            mismatch = replay.diff(plc, target)
            if mismatch and diverged_at is None:
                diverged_at, diverged_tags = index, mismatch
            continue

        bound = _wait_bound(context, moved)
        if system_moved:
            bound = max(bound, _SYSTEM_WAIT)
        landed = _fire_step(replay, inputs, target, bound)
        if landed is None and not system_moved:
            slid = _slide_wait(replay, trace, index, target, moved, checkpoints, plain, bound)
            if slid is not None:
                replay.plc, kept, steps = slid
                del replay.steps[kept:]
                replay.steps.extend(steps)
                if replay.accepts(replay.plc):
                    return _confirmed(result, replay)
                continue
        if landed is None:
            if diverged_at is None:
                diverged_at = index
                diverged_tags = sorted({key for key, _ in moved} | set(system))
            # Carry on from the inputs-first attempt: a later step may still
            # reach the violation.
            plc.patch(inputs)
            scans, _changed = replay.run_until_change(plc, bound)
            replay.steps.append(TraceStep(inputs=inputs, scans=scans))
        else:
            replay.plc, steps = landed
            replay.steps.extend(steps)
        if replay.accepts(replay.plc):
            return _confirmed(result, replay)

    return _unconfirmed(
        context,
        result,
        trace,
        diverged_at if diverged_at is not None else len(trace) - 1,
        diverged_tags,
        edges=_edge_tags(replay.edge_expr),
    )


def _fire_step(
    replay: _Replay,
    inputs: dict[str, Any],
    target: dict[str, Any],
    bound: int,
) -> tuple[Any, list[TraceStep]] | None:
    """Reach *target* on a step where a tick moved.

    The search changed this step's inputs on the scan its timer finished; at
    a fixed scan time the timer finishes some scans later.  Each split of the
    changed inputs is tried: the *early* ones applied now, the state run
    (inputs held) until it changes, then the run redone one scan short and the
    *late* ones applied on the changing scan.  All early (the inputs, then the
    wait) comes first, all late (the wait on the old inputs) second.

    Returns the PLC that landed there and the concrete steps taken, or None.
    """
    base = replay.plc
    held = base.current_state.tags
    changed = sorted(
        name for name, value in inputs.items() if held.get(name, replay.defaults.get(name)) != value
    )
    splits: list[frozenset[str]] = [frozenset(changed)]
    if changed:
        splits.append(frozenset())
    if 1 < len(changed) <= _SPLIT_INPUT_CAP:
        from itertools import combinations

        for size in range(len(changed) - 1, 0, -1):
            splits.extend(frozenset(c) for c in combinations(changed, size))
    for early_names in splits:
        early = {name: value for name, value in inputs.items() if name not in changed}
        early.update((name, inputs[name]) for name in early_names)
        late = {name: inputs[name] for name in changed if name not in early_names}
        if not late:
            trial = base.fork()
            trial.patch(early)
            scans, reached = replay.run_until_change(trial, bound, target)
            if reached:
                return trial, [TraceStep(inputs=early, scans=scans)]
            continue
        probe = base.fork()
        probe.patch(early)
        wait, moved = replay.run_until_change(probe, bound)
        if not moved or wait < 1:
            continue
        trial = base.fork()
        if wait > 1:
            trial.patch(early)
            _run_scans(trial, wait - 1)
            last = late
        else:
            last = {**early, **late}
        trial.patch(last)
        trial.step()
        if replay.diff(trial, target) and not replay.accepts(trial):
            continue
        steps = [TraceStep(inputs=early, scans=wait - 1)] if wait > 1 else []
        steps.append(TraceStep(inputs=last, scans=1))
        return trial, steps
    return None


# Plain steps before a timer's step that ``_slide_wait`` moves its wait past.
_SLIDE_STEP_CAP = 8


def _slide_wait(
    replay: _Replay,
    trace: list[TraceStep],
    index: int,
    target: dict[str, Any],
    moved: tuple[tuple[str, int], ...],
    checkpoints: list[tuple[Any, int]],
    plain: list[bool],
    bound: int,
) -> tuple[Any, int, list[TraceStep]] | None:
    """Reach step *index*'s *target* by letting its timer run before earlier steps.

    The search may finish a timer a few plain scans after an input change
    (a button whose effect a chain of coils carries for two scans): at a
    fixed scan time the timer then has to start earlier.  For each plain
    step ``j`` before *index* (nearest first), the wait goes before it: the
    inputs steps ``j..index`` change (all, or a subset: the ones that start
    the timer), at their last value, are applied there and held; the PLC
    waits as long as the moved ticks' owners take to reach the target, less
    the scans of steps ``j..index``; then those steps run as recorded, with
    the early inputs kept.  Accepted when the PLC lands on *target* or on a
    violation the search would judge (any concrete run to one is a real
    counterexample, however it got there).

    Returns ``(plc, concrete steps kept, concrete steps after them)`` or None.
    """
    from itertools import combinations

    model = replay.context.timer_model
    assert model is not None
    owners = sorted({model.sites[key].owner for key, _ in moved if key in model.sites})
    if not owners or any(owner not in target for owner in owners):
        return None
    step = trace[index]
    inputs, _system = _split_system(step.inputs)

    def owners_reached(state: Any) -> bool:
        tags = state.tags
        return all(tags.get(owner, replay.defaults.get(owner)) == target[owner] for owner in owners)

    span = step.scans
    for j in range(index - 1, max(-1, index - 1 - _SLIDE_STEP_CAP), -1):
        if not plain[j]:
            break
        span += trace[j].scans
        base, kept = checkpoints[j]
        held = base.current_state.tags
        # The inputs steps ``j..index`` change, at their last value: the
        # ones that start the timer may need to come before the wait.
        last: dict[str, Any] = {}
        for k in range(j, index + 1):
            last.update(_split_system(trace[k].inputs)[0])
        changed = sorted(
            name
            for name in last
            if any(
                _split_system(trace[k].inputs)[0].get(name, last[name])
                != held.get(name, replay.defaults.get(name))
                for k in range(j, index + 1)
            )
        )
        subsets: list[tuple[str, ...]] = [tuple(changed)]
        if 0 < len(changed) <= _SPLIT_INPUT_CAP:
            for size in range(len(changed) - 1, -1, -1):
                subsets.extend(combinations(changed, size))
        for early_names in subsets:
            early = {name: last[name] for name in early_names}
            probe = base.fork()
            probe.patch(early)
            start = probe.current_state.scan_id
            probe.run_until(owners_reached, max_cycles=bound)
            if not owners_reached(probe.current_state):
                continue
            wait = probe.current_state.scan_id - start - span
            if wait < 0:
                continue
            trial = base.fork()
            steps: list[TraceStep] = []
            if wait:
                trial.patch(early)
                _run_scans(trial, wait)
                steps.append(TraceStep(inputs=dict(early), scans=wait))
            for k in range(j, index):
                k_inputs = {**_split_system(trace[k].inputs)[0], **early}
                trial.patch(k_inputs)
                _run_scans(trial, trace[k].scans)
                steps.append(TraceStep(inputs=k_inputs, scans=trace[k].scans))
            trial.patch(inputs)
            _run_scans(trial, step.scans)
            steps.append(TraceStep(inputs=inputs, scans=step.scans))
            if replay.diff(trial, target) and not replay.accepts(trial):
                continue
            return trial, kept, steps
    return None


def _confirmed(result: Counterexample, replay: _Replay) -> Counterexample:
    """The counterexample with the trace the PLC followed (fixed scan time)."""
    root = [step for step in result.trace[:1] if step.scans == 0 and not step.inputs]
    steps = [step for step in replay.steps if step.scans > 0 or step.inputs]
    return replace(result, trace=[*root, *steps])


def _unconfirmed(
    context: _ExploreContext,
    result: Counterexample,
    trace: list[TraceStep],
    diverged_at: int,
    tags: list[str],
    *,
    edges: frozenset[str] = frozenset(),
) -> Intractable:
    """The timing kind of Intractable: what the search let timers do, and where replay left it.

    With *edges* (the property's rise()/fall() tags) it is the ``"property"``
    kind: the search counts those edges as possibly true on every scan, so
    its counterexample may stand on an edge no scan makes.
    """
    model = context.timer_model
    sites = model.sites if model is not None else {}
    owners = {site.owner: name for name, site in sites.items()}
    moved = {sites[key].owner if key in sites else key for step in trace for key, _ in step.timers}
    # A timer the PLC finished while the search held it shows as its Done.
    held = {
        name if name in owners else sites[name].owner
        for name in tags
        if (name in owners or name in sites) and name not in moved
    }
    timers = sorted(moved | held)
    named = ", ".join(timers) if timers else "a timer's progress"
    where = ", ".join(tags) if tags else "the property"
    hints: list[str] = []
    if moved:
        hints.append(
            f"  the search finished {', '.join(sorted(moved))} on scans the PLC at"
            f" dt={context.dt} s does not"
        )
    if held:
        hints.append(
            f"  the search held {', '.join(sorted(held))} running longer than the PLC does"
        )
    hints.extend(
        [
            f"  replay left the search's trace at step {diverged_at + 1} of {len(trace)}"
            f" (on {where}); the trace is on this result",
            "  whether it can happen depends on timer timing: check it with a run_for() test",
        ]
    )
    reason = f"unconfirmed: the counterexample depends on timer timing ({named})"
    kind = "timing"
    if edges:
        edge_names = ", ".join(sorted(edges))
        reason = (
            f"unconfirmed: the property's rise()/fall() of [{edge_names}] is one the"
            f" search does not decide, and its counterexample did not replay"
        )
        kind = "property"
        if not (moved or held):
            # No timer in it: the edge alone is the open question.
            hints = hints[:-1]
        hints.append(
            "  the search counts a property's rise()/fall() as possibly true on every"
            " scan; latch or one-shot the edge in the program and name that coil in"
            " the property instead"
        )
    return Intractable(
        reason=reason,
        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
        estimated_space=0,
        tags=sorted({*timers, *edges}),
        hints=hints,
        journal=result.journal,
        kind=kind,
        trace=trace,
        diverged_at=diverged_at,
    )
