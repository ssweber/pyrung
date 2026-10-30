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
read by a comparison            also every value of its domain above ``a``
==============================  ===============================================

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
state would change on.  A replay that reaches a state violating the property
is a ``Counterexample`` with the concrete trace; one that does not is an
``Intractable`` of kind ``"timing"`` carrying the abstract trace and the step
where replay diverged.
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

    def add(acc: Any, owner: Any, verb: str, unit: Any, presets: tuple[Any, ...]) -> None:
        literal = tuple(int(p) for p in presets if isinstance(p, int) and not isinstance(p, bool))
        name = acc.name
        prior = sites.get(name)
        if prior is not None:
            literal = tuple(sorted({*prior.literal_presets, *literal}))
            sites[name] = replace(prior, literal_presets=literal)
            return
        sites[name] = TimedSite(name, owner.name, verb, unit, tuple(sorted(set(literal))))

    for instr in walk_instructions(program):
        if isinstance(instr, OnDelayInstruction):
            add(instr.accumulator, instr.done_bit, "on_delay", instr.unit, (instr.preset,))
        elif isinstance(instr, OffDelayInstruction):
            add(instr.accumulator, instr.done_bit, "off_delay", instr.unit, (instr.preset,))
        elif isinstance(instr, (TimeDrumInstruction, TimeEventDrumInstruction)):
            verb = "time_event_drum" if isinstance(instr, TimeEventDrumInstruction) else "time_drum"
            presets = tuple(p for p in instr.presets if p is not None)
            add(instr.accumulator, instr.current_step, verb, instr.unit, presets)
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
    consumed: frozenset[str],
    exact: frozenset[str] = frozenset(),
) -> _TimerModel:
    sites = timed_sites(program)
    # An unread timer (classify: its Done is combinational, its accumulator
    # in no key) changes no other tag, whatever its tick does.
    branch = frozenset(
        name
        for name, site in sites.items()
        if name in stateful_dims or site.owner not in combinational
    )
    reps: dict[str, tuple[int, ...]] = {}
    for name in sorted(consumed & set(sites)):
        domain = stateful_dims.get(name, ())
        values = sorted(
            {int(v) for v in domain if isinstance(v, (int, float)) and not isinstance(v, bool)}
        )
        if values:
            reps[name] = tuple(values)
    return _TimerModel(sites=sites, branch=branch, reps=reps, exact=exact & frozenset(sites))


class _TimerOracle:
    """The ``_bb_tick`` choice maker for one scan's run (see module docstring).

    ``begin(forced)`` starts a run: the i-th choice point takes candidate
    ``forced[i]`` (0 = hold) and every later one holds.  After the run,
    ``taken``/``widths`` describe the choice points met, in order.
    """

    __slots__ = ("_branch", "_reps", "_exact", "forced", "taken", "widths", "moved", "open")

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
            raise ChoiceOverflow(tuple(sorted({key for key, _ in oracle.moved})))
        yield


# Key a counter no further than its preset (``count_clamps``).
# ``PYRUNG_PROVE_COUNT_CLAMPS=0`` turns it off, to measure what it saves.
_COUNT_CLAMPS = os.environ.get("PYRUNG_PROVE_COUNT_CLAMPS", "1") != "0"


def count_clamps(
    program: Program,
    graph: Any,
    stateful_names: tuple[str, ...],
    *,
    observed: frozenset[str],
    observe_all: bool,
    atoms: Mapping[str, Any],
) -> tuple[tuple[int, bool, int], ...]:
    """Counters whose key may stop at the preset: ``(index, counts up, preset)``.

    Counters count concretely.  Past its preset a counter that only counts
    one way stays done until a reset sets it to 0, so states that differ only
    in how far past the preset it is behave alike when nothing else reads the
    accumulator: no comparison, data read or outside observer, a literal
    preset, one counter instruction writing it and no other writer.  Keying
    them as the preset keeps an unreset counter from walking the key up to
    its type's limit.
    """
    from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
    from pyrung.core.validation._common import walk_instructions

    from .absorb import _all_write_targets, _has_forbidden_data_read

    if not _COUNT_CLAMPS or observe_all:
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
            or acc in observed
            or atoms.get(acc)
            or _has_forbidden_data_read(program, graph, acc)
        ):
            continue
        clamps.append((slots[acc], up, int(preset)))
    return tuple(sorted(set(clamps)))


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
        return not self.predicate(self.view(plc))

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
            return not self.predicate(self._view_of(plc, state))

        plc.run_until(stop, max_cycles=bound)
        scans = plc.current_state.scan_id - start_scan
        return scans, reached(self.key(plc)) or self.violated(plc)


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


def confirm(
    program: Program,
    context: _ExploreContext,
    predicate: Callable[[dict[str, Any]], bool],
    result: Counterexample,
) -> Counterexample | Intractable:
    """Replay an abstract counterexample on the concrete PLC (module docstring)."""
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
    )
    if not any(step.timers for step in trace) and any(
        _split_system(step.inputs)[1] for step in trace
    ):
        # No timer choice in it, only free system inputs (clocks, the scan
        # counter, the RTC) the PLC sets itself: the search's caveated
        # counterexample as before (bfs._free_system_caveat).  Waiting for an
        # RTC hour on the PLC is no check worth its scans.
        return result
    if not trace:
        # The power-on state itself.
        if replay.violated(replay.plc):
            return replace(result, trace=[TraceStep(inputs={}, scans=0)])
        return _unconfirmed(context, result, trace, 0, ["power-on state"])

    initial, abstract = _abstract_states(context, trace)
    diverged_at: int | None = None
    diverged_tags: list[str] = []
    previous = initial
    for index, (step, (target, moved)) in enumerate(zip(trace, abstract, strict=True)):
        inputs, system = _split_system(step.inputs)
        # A system input the search changed (a clock bit, the scan counter)
        # is time the PLC must let pass, like a timer's.
        system_moved = any(previous.get(name) != value for name, value in system.items())
        previous = target
        plc = replay.plc
        if not moved and not system_moved:
            plc.patch(inputs)
            _run_scans(plc, step.scans)
            replay.steps.append(TraceStep(inputs=inputs, scans=step.scans))
            if replay.violated(plc):
                return _confirmed(result, replay)
            mismatch = replay.diff(plc, target)
            if mismatch and diverged_at is None:
                diverged_at, diverged_tags = index, mismatch
            continue

        bound = _wait_bound(context, moved)
        if system_moved:
            bound = max(bound, _SYSTEM_WAIT)
        landed = _fire_step(replay, inputs, target, bound)
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
        if replay.violated(replay.plc):
            return _confirmed(result, replay)

    return _unconfirmed(
        context,
        result,
        trace,
        diverged_at if diverged_at is not None else len(trace) - 1,
        diverged_tags,
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
        if replay.diff(trial, target) and not replay.violated(trial):
            continue
        steps = [TraceStep(inputs=early, scans=wait - 1)] if wait > 1 else []
        steps.append(TraceStep(inputs=last, scans=1))
        return trial, steps
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
) -> Intractable:
    """The timing kind of Intractable: what the search let timers do, and where replay left it."""
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
    return Intractable(
        reason=f"unconfirmed: the counterexample depends on timer timing ({named})",
        dimensions=len(context.stateful_dims) + len(context.nondeterministic_dims),
        estimated_space=0,
        tags=timers,
        hints=hints,
        journal=result.journal,
        kind="timing",
        trace=trace,
        diverged_at=diverged_at,
    )
