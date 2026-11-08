"""Coast until a predicate holds, through the guarded scan loop.

Every scan runs, in order, so a coast cannot land anywhere stepping would not.
The coast takes an interpreted scan, and asks the loop to repeat what it has seen;
when no known path covers the next scan, that scan is traced for next time and the
coast takes it interpreted.  What must watch the scans is kept to the interpreted
ones, by exits rather than by checks:

* A stop predicate that declares its reads is asked after each loop scan, on the
  values the loop has just committed.  Its answer only ends the stretch: the coast
  judges the predicate again on the real state it lands on.
* An undeclared predicate has its reads traced (:func:`~pyrung.core.read_trace._traced_call`),
  and the loop leaves before the scan that would change one.  So it is asked only on
  scans a change of what it reads could matter, and it is asked on the real state.
  An opaque one, that reads the whole state, keeps the coast interpreted.
* A monitored tag exits before its scan, which runs interpreted and fires the monitor.
  An enabled breakpoint keeps the coast interpreted.

Paths
-----

A loop that follows a timer leaves when something else happens, and most of what
happens next has happened before: a program under a bang-bang hold spends its life
moving between a few paths, each a loop of :mod:`~pyrung.core.scan_trace`.  So a
kernel keeps its traces, and any whose constants hold can run from any state.  A
path usually hands over to one it handed over to before, and then only the cells the
last path moved and the cells it did not read can have broken the next one's
constants, which is cheaper than asking every path.

A coast's memo is bounded (kernels, paths per kernel, loops per path) and scoped to
what it serves.  A search gets one of its own, so what it costs does not depend on
what ran before it.

Records
-------

A stretch leaves the same index a stepped run does, so look-back needs no idea it was
there.  The guards held, so the rungs that fired are the ones that fired on the path's
own interpreted scan: the scan right after the trace, from the state it was traced on.
A path fires only once it has that capture.  What a rung attempted comes from there
too, unless it stored a moving value: a tag stored once moves as the tag does, and
otherwise that column says ``Unknown`` for that range, which readers treat as "ask a
replay".  Committed values come from the moves: a tag changes only by a checked delta,
an exact flip or a float ramp, and a ramp's values say ``Unknown``.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from pyrung.core import compiled_run, cycle_jump, jit_stats, scan_trace
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.compiled_run import _BoundsWatch, _install, _kernel_cannot_say_what_it_adds
from pyrung.core.read_trace import _FIELD_READ, _READ_ALL, _traced_call
from pyrung.core.rung_firings import _MISSING_WRITE, _UNKNOWN_VALUE, RepeatPlan
from pyrung.core.rung_firings import _equal as _same_value
from pyrung.core.scan_trace import (
    _CLASSIFIED_SYSTEM_TAGS,
    DECLINING_SYSTEM_TAGS,
    MOVING_SYSTEM_TAGS,
    CoastMoves,
    LoopShape,
    ScanLoop,
    ScanTrace,
    _record_block_writes,
    _source_of,
    attempted_stores,
    coast_loop,
    coast_moves,
    exit_of,
    moving_tags,
    same,
    start_counting_exit,
)
from pyrung.core.scan_trace import _current_value as _cell_value
from pyrung.core.system_points import (
    _SCAN_START_MEMORY_KEYS,
    _SD_DELETE_ALL_NAME,
    _SD_EJECT_NAME,
    HOOK_BOUND_TAG_NAMES,
    SYSTEM_TAGS_BY_NAME,
)
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.runner import PLC

# A stretch checkpoints this many intervals apart.  What a checkpoint saves is run-up through
# the compiled kernel, about 60 us a scan, and what it costs is a state and a force map kept.
STRETCH_CHECKPOINT_INTERVALS = 10
# Checkpoints a coasting runner keeps, thinned when it passes this.  The budgets that call a
# coast bound its scans; this bounds what they leave, however long the seek was allowed to be.
MAX_CHECKPOINTS = 512
# Record runs a runner holds before its stretches leave their scans to be settled when asked.
# A rung that fires every other scan costs a run each time, about 140 bytes.  The longest route
# in the tumbler reaches 78,000.
MAX_RECORD_RUNS = 1_000_000
# Kernels a memo keeps with their paths, least recently asked for out first.  Each is a
# compiled module and a parsed source, about 23 MB on a large program.  12 peaked at 1,000 MB
# on the tumbler and 2 at 677 MB, with no more time spent compiling than it saves.
MAX_KERNELS = 2
# Paths a kernel keeps, least recently fired out first.  A path holds its trace's
# operations and each loop written from them.
MAX_PATHS = 256
# Columns kept by content in one kernel, so two paths that record the same are seen to agree.
MAX_INTERNED = 4096
MAX_LOOPS_PER_PATH = 8
MAX_SUCCESSORS = 4
# Segments in the longest cycle a stretch's record looks for.  Under a bang-bang hold the paths
# take turns, and a slow timer can bring the same turns round again every hundred scans.  The
# first few cycles go in scan by scan, and the rest go in as one repeat.
MAX_CYCLE_SEGMENTS = 128
# The cycles that go in scan by scan first, which the timelines check for the same shape.
WARM_CYCLES = 3
# The fewest scans a repeat must save.  Each repeat splits the scan by scan record around it,
# and a split costs about as much as recording seventy scans.
MIN_REPEAT_SCANS = 256
# Interpreted scans between attempts at a stretch: at entry and after a good stretch,
# and how far apart they may get when stretches do not come or do not last.
ENTRY_WAIT = 2
MAX_WAIT_AFTER_SHORT = 64
MAX_WAIT_AFTER_NONE = 256
# A stretch this long paid for its entry into the kernel.
PAYING_STRETCH = 16
# What a loop run does with the cycle its paths repeat.  "off" ignores it.  "shadow" predicts
# how many cycles repeat exactly, and checks each scan the loop then runs against that.
# "jump" predicts too, and moves the state over the cycles predicted without running them.
CYCLE_PREDICTIONS = "jump"
# Whether a prediction the scans disagree with raises.  tests/fold_check.py sets it.
STRICT_CYCLE_CHECK = False
# The most cycles one prediction looks ahead, and the longest cycle in scans it is made for.
# Each scan of a cycle is run once on forms, and each clock or RTC value once per cycle.
MAX_PREDICTED_CYCLES = 4096
MAX_PREDICTED_PERIOD = 1024
# The most cycles a loop run waits to predict again after a prediction it could not make.
MAX_PREDICTION_WAIT = 64
# The most stop predicate answers a prediction keeps, so its memory stays small.
MAX_KEPT_ANSWERS = 1024


@dataclass(eq=False)
class _Loop:
    function: Callable[..., tuple[int, float]]
    moves: CoastMoves
    must_be_present: tuple[str, ...]
    shape: LoopShape
    # The iteration on forms, written when a prediction first needs it.  None if it cannot be.
    on_forms: cycle_jump.Iteration | None | bool = False


def _on_forms(loop: _Loop) -> cycle_jump.Iteration | None:
    if loop.on_forms is False:
        try:
            loop.on_forms = cycle_jump.iteration_of(loop.shape)
        except cycle_jump.Unsupported:
            loop.on_forms = None
    return cast("cycle_jump.Iteration | None", loop.on_forms)


@dataclass(eq=False)
class _Recording:
    """What a path's scans record in one rung's column."""

    # What every scan of the path says, a tag no one capture can speak for reading Unknown.
    settled: dict[str, Any]
    # A segment's end says the rest: (tag, how far it moves a scan or None if held, varied).
    from_ends: tuple[tuple[str, int | None, bool], ...]
    varied: frozenset[str]
    # What a rung's column has to change, coming here from another recording of it.
    changes: dict[_Recording, list[tuple[str, Any, bool]]] = field(default_factory=dict)


@dataclass(eq=False)
class _Capture:
    """What one interpreted scan of a path recorded, by rung and by subroutine rung."""

    columns: tuple[dict[Any, tuple[dict[str, Any], frozenset[str]]], ...]
    # Those with nothing moving in them, which every scan of the path repeats.
    settled: tuple[dict[Any, tuple[dict[str, Any], frozenset[str]]], ...]
    recordings: tuple[dict[Any, _Recording], ...]


@dataclass(eq=False)
class _Path:
    """A trace a coast may fire, what it has written of it, and what tends to follow."""

    trace: ScanTrace
    # Every tag its scans write, moving or not: a block cell rewritten with its own
    # value still appears in the state, the first time.
    written: frozenset[str]
    moves: CoastMoves
    # The moved and held tags with their cells, to read where a stretch left them.
    ending: tuple[tuple[str, tuple[Any, ...]], ...]
    stores: dict[str, tuple[int, int]]
    # rung and node -> (writes, varied), from the path's interpreted scan.  None until it ran.
    capture: _Capture | None = None
    loops: dict[tuple[frozenset[str], frozenset[str]], _Loop] = field(default_factory=dict)
    # Paths that followed this one, most recent first, each with the constants to check.
    successors: list[tuple[_Path, tuple[tuple[tuple[Any, ...], Any], ...]]] = field(
        default_factory=list
    )


@dataclass(eq=False)
class _KernelPaths:
    kernel: CompiledKernel
    paths: list[_Path] = field(default_factory=list)  # most recently fired last
    seeds: set[tuple[Any, ...]] = field(default_factory=set)
    off: str | None = None
    # One object per distinct column, so paths that agree share it and compare by identity.
    interned: dict[Any, tuple[dict[str, Any], frozenset[str]]] = field(default_factory=dict)
    # Likewise a column as a path records it, so scans in a row that say the same are seen to.
    recordings: dict[Any, _Recording] = field(default_factory=dict)
    # The columns a set of paths have in common, as a stretch moves between them.
    common: dict[frozenset[_Path], tuple[frozenset[Any], ...]] = field(default_factory=dict)


@dataclass(eq=False)
class CoastMemo:
    """What coasts keep for the coasts after them.  One a search, inherited by its forks."""

    # Keeps the program and the overlay rungs alive, so an id cannot be reused under it.
    kernels: dict[tuple[Any, ...], tuple[CompiledKernel, Any]] = field(default_factory=dict)
    paths: dict[int, _KernelPaths] = field(default_factory=dict)
    compiles: int = 0
    traces: int = 0
    counts: Counter[str] = field(default_factory=Counter)
    # Raises when the search has to stop.  Asked between scans and between fires.
    check_limits: Callable[[], None] | None = None
    # What the search's ledger has left, in interpreted scans.
    work_left: Callable[[], int] | None = None


def memo_of(plc: PLC) -> CoastMemo:
    memo = plc._coast_memo
    if memo is None:
        memo = plc._coast_memo = CoastMemo()
    return memo


def _kernel_for(plc: PLC, memo: CoastMemo) -> CompiledKernel | None:
    """The kernel of this runner's program and overlay.  A fork need not compile it again."""
    cached = plc._compiled_replay_kernel
    if cached is False:
        return None
    if cached is not None:
        return cast("CompiledKernel", cached)
    synthesis = plc._synthesis
    plant = () if synthesis is None else tuple(synthesis.plant)
    holds = () if synthesis is None else tuple(synthesis.holds)
    key = (id(plc._program), tuple(map(id, plant)), tuple(map(id, holds)))
    entry = memo.kernels.pop(key, None)
    if entry is not None:
        memo.kernels[key] = entry
        plc._compiled_replay_kernel = entry[0]
        return entry[0]
    kernel = plc._compiled_replay_supported_kernel()
    memo.compiles += 1
    if kernel is None:
        return None
    memo.kernels[key] = (kernel, (plc._program, plant, holds))
    if len(memo.kernels) > MAX_KERNELS:
        memo.kernels.pop(next(iter(memo.kernels)))
    return kernel


def _paths_for(memo: CoastMemo, kernel: CompiledKernel) -> _KernelPaths:
    paths = memo.paths.pop(id(kernel), None)
    if paths is None:
        paths = _KernelPaths(kernel)
        if len(memo.paths) >= MAX_KERNELS:
            memo.paths.pop(next(iter(memo.paths)))
    memo.paths[id(kernel)] = paths
    return paths


class _CoastLoop(ScanLoop):
    """One stretch's use of a kernel's paths: entry, tracing and the self check."""

    _keep_plans = True
    _real_scans_are = "self check"

    def __init__(
        self,
        replay: CompiledPLC,
        kernel: CompiledKernel,
        paths: _KernelPaths,
        constrained: Any,
        memo: CoastMemo,
    ) -> None:
        super().__init__(replay, kernel, 0, None, constrained)
        self._may_build = 1  # a stretch ends at the scan it traces
        self._kernel_paths = paths
        self._memo = memo
        self.learned: _Path | None = None
        paths.seeds |= self._seeds
        self._seeds = paths.seeds
        paths.off = self._off

    def _whole_run_blocker(self, kernel: CompiledKernel, budget: int, predicate: Any) -> str | None:
        return self._program_blocker(kernel)

    def learn(self, last: _Path | None) -> None:
        """Trace the state the kernel is in, and check the trace on one real scan."""
        self._traces = [] if last is None else [last.trace]
        known = len(self._traces)
        self._trace_this_scan()
        if self._pending is None:
            return
        self._memo.traces += 1
        replay = self._replay
        bounds = _BoundsWatch(replay, dict(self._constrained)) if self._constrained else None
        try:
            replay.step_replay(defer_flush=True)
        except Exception:
            self.real_scan_failed("raised")
        else:
            if bounds is not None and bounds.moved() and bounds.violated():
                self.real_scan_failed("left bounds")
            else:
                self.real_scan_ran()
        if self._off is not None:
            self._kernel_paths.off = self._off
        if len(self._traces) > known:
            self._keep(self._traces[-1])

    def _keep(self, trace: ScanTrace) -> None:
        kept = self._kernel_paths.paths
        names = self._replay._block_index_names
        plan = trace.plan
        assert plan is not None
        moves = coast_moves(plan, names)
        for path in kept:
            # The state was left out for a reason of this stretch: a read, a watch.
            if (
                path.trace.constants == trace.constants
                and path.trace.source == trace.source
                and path.moves == moves
            ):
                self._memo.counts["traces of a known path"] += 1
                self.learned = path
                return
        moving = moving_tags(plan, names)
        written = set(moving.values())
        for symbol, cells in trace.block_writes:
            written.update(
                names[symbol][index] for index in cells if index in names.get(symbol, {})
            )
        wanted = {name for name, _delta in moves.moved} | set(moves.held)
        ending = tuple((name, key) for key, name in moving.items() if name in wanted)
        stores = {
            # One attempt at a tag that does not move: what it computes is what the tag holds.
            tag: (total, 0 if total == 1 and tag not in written else symbolic)
            for tag, (total, symbolic) in attempted_stores(plan, names).items()
        }
        self.learned = _Path(trace, frozenset(written), moves, ending, stores)
        kept.append(self.learned)
        if len(kept) > MAX_PATHS:
            _forget(kept, kept[0], self._kernel_paths)


def _forget(paths: list[_Path], path: _Path, kernel_paths: _KernelPaths) -> None:
    paths.remove(path)
    for other in paths:
        other.successors = [entry for entry in other.successors if entry[0] is not path]
    kernel_paths.common.clear()


def _capture_of(plc: PLC, path: _Path, kernel_paths: _KernelPaths, scan_id: int) -> _Capture:
    """Take a path's columns from the interpreted scan just run on it."""
    moving = {tag for tag, (_total, symbolic) in path.stores.items() if symbolic}
    interned = kernel_paths.interned
    columns = []
    settled = []
    recordings = []
    for timelines in (plc._rung_firing_timelines, plc._node_firing_timelines):
        these: dict[Any, tuple[dict[str, Any], frozenset[str]]] = {}
        still: dict[Any, tuple[dict[str, Any], frozenset[str]]] = {}
        recorded: dict[Any, _Recording] = {}
        for key, fired in timelines.at(scan_id).items():
            writes = dict(fired)
            varied = frozenset(tag for tag in writes if timelines.varied_on(key, tag, scan_id))
            # By repr, or 1 and True would be one column.
            identity = (key, repr(sorted(writes.items())), varied)
            if identity not in interned and len(interned) >= MAX_INTERNED:
                interned.clear()  # Paths held from before no longer agree with new ones: a
                #                   record run breaks where it need not, and nothing else.
            column = interned.setdefault(identity, (writes, varied))
            these[key] = column
            recorded[key] = _recording_of(path, column, kernel_paths)
            if not (moving & writes.keys()):
                still[key] = column
        columns.append(these)
        settled.append(still)
        recordings.append(recorded)
    return _Capture(tuple(columns), tuple(settled), tuple(recordings))


def _recording_of(
    path: _Path, column: tuple[dict[str, Any], frozenset[str]], kernel_paths: _KernelPaths
) -> _Recording:
    """How one rung's capture speaks for every scan of its path."""
    writes, varied = column
    deltas = dict(path.moves.moved)
    held = set(path.moves.held)
    ending = {name for name, _key in path.ending}
    settled: dict[str, Any] = {}
    from_ends: list[tuple[str, int | None, bool]] = []
    for tag, value in writes.items():
        total, symbolic = path.stores.get(tag, (0, 0))
        if not symbolic:
            settled[tag] = value  # every attempt on this path is a constant
        elif total == 1 and tag in deltas and tag in ending:
            from_ends.append((tag, deltas[tag], tag in varied))
        elif total == 1 and tag in held and tag in ending:
            from_ends.append((tag, None, tag in varied))
        else:
            # Which attempt a rung made, or its value on a later scan, no one capture says.
            settled[tag] = _UNKNOWN_VALUE
            if total > 1:
                varied |= {tag}
    identity = (id(writes), varied, tuple(settled.items()), tuple(from_ends))
    found = kernel_paths.recordings.get(identity)
    if found is None:
        if len(kernel_paths.recordings) >= MAX_INTERNED:
            kernel_paths.recordings.clear()  # A run breaks where it need not, and nothing else.
        found = _Recording(settled, tuple(from_ends), varied)
        kernel_paths.recordings[identity] = found
    return found


def _common_columns(
    segments: list[_Segment], kernel_paths: _KernelPaths
) -> tuple[frozenset[Any], ...]:
    """The columns every path of these segments repeats unchanged, by rung and node."""
    paths = frozenset(segment.path for segment in segments)
    if len(paths) == 1:
        capture = segments[0].path.capture
        assert capture is not None
        return tuple(frozenset(still) for still in capture.settled)
    found = kernel_paths.common.get(paths)
    if found is None:
        result = []
        for level in range(2):
            ordered = iter(paths)
            first = next(ordered).capture
            assert first is not None
            common = dict(first.settled[level])
            for path in ordered:
                assert path.capture is not None
                other = path.capture.settled[level]
                common = {k: v for k, v in common.items() if other.get(k) is v}
            result.append(frozenset(common))
        found = tuple(result)
        if len(kernel_paths.common) >= 256:
            kernel_paths.common.clear()
        kernel_paths.common[paths] = found
    return found


def _record_segments(
    plc: PLC, segments: list[_Segment], kernel_paths: _KernelPaths
) -> list[tuple[int, int, int]]:
    """Write what a stretch's scans did into the three timelines a stepped run writes.

    Returns the runs of repeating cycles found, as :func:`_cycles_in` does.
    """
    if not segments:
        return []
    runs = _cycles_in(segments)
    start = segments[0].first
    total = sum(segment.count for segment in segments)
    timelines_held = (
        plc._rung_firing_timelines,
        plc._node_firing_timelines,
        plc._committed_tag_timelines,
    )
    if sum(timelines.run_count() for timelines in timelines_held) >= MAX_RECORD_RUNS:
        plc._note_unrecorded_scans(start, start + total - 1)
        return runs
    cursor = 0
    for lead, length, cycles in runs:
        period = _scans(segments, lead, length)
        end = lead + cycles * length
        at = lead + WARM_CYCLES * length
        misses = 0
        # A plan may stop short, when a column with steps would reach a hundred values.  One
        # cycle more goes in scan by scan before the next plan.
        while at + length <= end and misses < WARM_CYCLES:
            _record_scan_by_scan(plc, segments[cursor:at], kernel_paths)
            cursor = at
            last = segments[at - 1].first + segments[at - 1].count - 1
            plans = [timelines.plan_repeat(last, period) for timelines in timelines_held]
            more = (end - at) // length
            for plan in plans:
                more = 0 if plan is None else min(more, plan.limit)
            if more > 0:
                for plan in plans:
                    assert plan is not None
                    plan.apply(more)
                cursor = at = at + more * length
                misses = 0
            else:
                misses += 1
            at += length
    _record_scan_by_scan(plc, segments[cursor:], kernel_paths)
    return runs


def _cycles_in(segments: list[_Segment]) -> list[tuple[int, int, int]]:
    """Runs of segments that repeat a cycle, as (first, segments a cycle, cycles), in order.

    A segment's records follow from three things.  These are its path, its count and the values
    it left in the tags it moves.  Segments a cycle apart must have the same path and count.
    Each whole number they left must differ by the same amount on every cycle, and any other
    value must be the same.  Then the records of each cycle are those of the cycle before,
    with each whole number moved by that amount.  A run must save ``MIN_REPEAT_SCANS``,
    since each run splits the scan by scan record that batches the rest.
    """
    found: list[tuple[int, int, int]] = []
    count = len(segments)
    # Comparing paths and counts as lists is cheap, so it goes first.  The ends are compared
    # only for a run long enough to use.
    keys = [(id(segment.path), segment.count) for segment in segments]
    least = WARM_CYCLES + 2
    # Where the keys stop repeating every so many segments, found from some earlier first.
    # Keys that repeat from that first repeat from any later one too, up to the same end.
    # Without this, a long run of repeating keys is walked again from each first.
    known: dict[int, tuple[int, int]] = {}
    first = 0
    while first + least <= count:
        cover, length = 0, 0
        top = first + min(MAX_CYCLE_SEGMENTS, (count - first) // least)
        at = first
        while True:
            try:
                at = keys.index(keys[first], at + 1, top + 1)
            except ValueError:
                break
            candidate = at - first
            since, periodic = known.get(candidate, (count, 0))
            if not since <= first < periodic - candidate:
                periodic = _periodic_end(keys, first, candidate)
                known[candidate] = (first, periodic)
            reach = (periodic - first) // candidate * candidate
            if reach > cover:
                cover, length = reach, candidate
        if not length:
            first += 1
            continue
        if _worth_a_repeat(segments, first, length, cover // length):
            end = _stepping_end(segments, first, length, first + cover)
            cycles = (end - first) // length
            if _worth_a_repeat(segments, first, length, cycles):
                found.append((first, length, cycles))
                first += cycles * length
                continue
            cover = end - first
        first = max(first + 1, first + cover - length)
    return found


def _periodic_end(keys: list[Any], first: int, length: int) -> int:
    """Where the keys from *first* stop repeating every *length*."""
    count = len(keys)
    end = first + length
    stride = length
    while end < count:
        stop = min(count, end + stride)
        if keys[end:stop] == keys[end - length : stop - length]:
            end = stop
            stride *= 2
            continue
        low, high = end, stop  # keys match up to low, and differ somewhere before high
        while high - low > 1:
            middle = (low + high) // 2
            if keys[low:middle] == keys[low - length : middle - length]:
                low = middle
            else:
                high = middle
        return low
    return end


def _stepping_end(segments: list[_Segment], first: int, length: int, stop: int) -> int:
    """Where the segments from *first* stop leaving ends that step the same each cycle."""
    steps = []
    for position in range(first + length, stop):
        found = _end_steps(segments[position - length], segments[position])
        if found is None:
            return position
        if position < first + 2 * length:
            steps.append(found)
        elif found != steps[(position - first) % length]:
            return position
    return stop


def _end_steps(one: _Segment, other: _Segment) -> tuple[int, ...] | None:
    """How far each whole number *other* left moved from *one*, or None if another value moved."""
    if one.path is not other.path or one.count != other.count:
        return None
    steps = []
    others = other.ends
    for name, value in one.ends.items():
        later = others.get(name, _MISSING_WRITE)
        if type(value) is int and type(later) is int:
            steps.append(later - value)
        elif same(value, later):
            steps.append(0)
        else:
            return None
    return tuple(steps)


def _worth_a_repeat(segments: list[_Segment], first: int, length: int, cycles: int) -> bool:
    if cycles < WARM_CYCLES + 2:
        return False
    return _scans(segments, first, length) * (cycles - WARM_CYCLES) >= MIN_REPEAT_SCANS


def _scans(segments: list[_Segment], first: int, length: int) -> int:
    return sum(segment.count for segment in segments[first : first + length])


def _record_scan_by_scan(plc: PLC, segments: list[_Segment], kernel_paths: _KernelPaths) -> None:
    if not segments:
        return
    start = segments[0].first
    total = sum(segment.count for segment in segments)
    # A stretch that ran through several paths without a gap: a rung they all repeat
    # goes in once, which is most of them under a bang-bang hold.
    common = _common_columns(segments, kernel_paths)
    first_capture = segments[0].path.capture
    assert first_capture is not None
    for level, timelines in enumerate((plc._rung_firing_timelines, plc._node_firing_timelines)):
        here = common[level]
        for key in here:
            writes, varied = first_capture.columns[level][key]
            timelines.append_repeated(key, start, total, writes, varied)
        _record_columns(timelines, segments, level, here)
    _record_committed(plc._committed_tag_timelines, segments)


class _ColumnRuns:
    """One rung's column while a stretch goes in: when it has fired, and each tag's run."""

    __slots__ = ("first", "last", "recording", "runs")

    def __init__(self) -> None:
        self.first = 0
        self.last = 0
        self.recording: _Recording | None = None
        # tag -> [value on its first scan, delta, its first scan, varied].  A run goes on until
        # something says otherwise, so a tag no path moves is looked at only when it changes.
        self.runs: dict[str, list[Any]] = {}


def _record_columns(
    timelines: Any, segments: list[_Segment], level: int, common: frozenset[Any]
) -> None:
    """The rungs some paths of a stretch fire and others do not, or fire differently.

    Under a bang-bang hold a path changes nearly every scan while nearly every tag does not.
    Each tag of each column keeps a run, and a segment touches only the tags it moves or its
    recording changes.  A run is one :meth:`~RungFiringTimelines.append_tag_run`, which lands
    as appending its scans one by one would, and columns are independent of each other.
    """
    columns_of: dict[_Path, list[tuple[Any, _Recording]]] = {}
    columns: dict[Any, _ColumnRuns] = {}

    def finish(key: Any, column: _ColumnRuns) -> None:
        timelines.append_fired_run(key, column.first, column.last)
        for tag, (value, delta, first, varied) in column.runs.items():
            timelines.append_tag_run(key, tag, first, column.last, value, delta, varied)
        column.runs.clear()

    for segment in segments:
        path = segment.path
        fires = columns_of.get(path)
        if fires is None:
            capture = path.capture
            assert capture is not None
            fires = columns_of[path] = [
                (key, recording)
                for key, recording in capture.recordings[level].items()
                if key not in common
            ]
        first = segment.first
        count = segment.count
        last = first + count - 1
        ends = segment.ends
        for key, recording in fires:
            column = columns.get(key)
            if column is None:
                column = columns[key] = _ColumnRuns()
                column.first = first
            elif column.last != first - 1:
                finish(key, column)
                column.first = first
                column.recording = None
            before = column.recording
            runs = column.runs
            if before is not recording:
                if before is None:
                    changes = [
                        (tag, value, tag in recording.varied)
                        for tag, value in recording.settled.items()
                    ]
                    # Its next firing says a tag this rung once wrote and now does not.
                    written = {tag for tag, _delta, _varied in recording.from_ends}
                    changes += [
                        (tag, _MISSING_WRITE, False)
                        for tag in timelines.tags_of(key)
                        if tag not in recording.settled and tag not in written
                    ]
                else:
                    changes = recording.changes.get(before)
                    if changes is None:
                        changes = _changes_between(before, recording)
                        if len(recording.changes) >= MAX_PATHS:
                            recording.changes.clear()
                        recording.changes[before] = changes
                for tag, value, varied in changes:
                    run = runs.get(tag)
                    if run is not None:
                        timelines.append_tag_run(
                            key, tag, run[2], column.last, run[0], run[1], run[3]
                        )
                    runs[tag] = [value, None, first, varied]
                column.recording = recording
            for tag, delta, varied in recording.from_ends:
                value = ends[tag]
                if delta is not None:
                    value -= delta * (count - 1)
                run = runs.get(tag)
                if run is not None:
                    if run[1] == delta and run[3] == varied:
                        if delta is None:
                            if _same_value(run[0], value):
                                continue
                        elif type(value) is int and run[0] + delta * (first - run[2]) == value:
                            continue
                    timelines.append_tag_run(key, tag, run[2], column.last, run[0], run[1], run[3])
                runs[tag] = [value, delta, first, varied]
            column.last = last
    for key, column in columns.items():
        finish(key, column)


def _changes_between(before: _Recording, after: _Recording) -> list[tuple[str, Any, bool]]:
    """What a column's tags go to when it goes from *before* to *after*."""
    changes: list[tuple[str, Any, bool]] = []
    for tag, value in after.settled.items():
        varied = tag in after.varied
        if (
            tag not in before.settled
            or varied != (tag in before.varied)
            or not _same_value(before.settled[tag], value)
        ):
            changes.append((tag, value, varied))
    written = set(after.settled)
    written.update(tag for tag, _delta, _varied in after.from_ends)
    for tag in before.settled:
        if tag not in written:
            changes.append((tag, _MISSING_WRITE, False))
    for tag, _delta, _varied in before.from_ends:
        if tag not in written:
            changes.append((tag, _MISSING_WRITE, False))
    return changes


def _record_committed(committed: Any, segments: list[_Segment]) -> None:
    """What each tag committed.  A tag moving on one line goes in as one run."""
    # A run is [delta, base, first, last]; a delta of None is a run nothing can say.
    runs: dict[str, list[Any]] = {}

    def flush(name: str, run: list[Any]) -> None:
        delta, base, first, last = run
        if delta is None:
            committed.append_repeated(name, first, last - first + 1, {name: 0.0}, unknown={name})
        else:
            committed.append_repeated(
                name, first, last - first + 1, {name: base}, deltas={name: delta}
            )

    for segment in segments:
        moves = segment.path.moves
        first, count, ends = segment.first, segment.count, segment.ends
        last = first + count - 1
        for name, delta in moves.moved:
            base = ends[name] - delta * (count - 1)
            run = runs.get(name)
            if run is not None:
                if (
                    run[0] == delta
                    and run[3] == first - 1
                    and type(base) is int
                    and run[1] + delta * (first - run[2]) == base
                ):
                    run[3] = last
                    continue
                flush(name, run)
            runs[name] = [delta, base, first, last]
        for name in moves.free:
            run = runs.get(name)
            if run is not None:
                if run[0] is None and run[3] == first - 1:
                    run[3] = last
                    continue
                flush(name, run)
            runs[name] = [None, 0.0, first, last]
        for name, _before, after in moves.flips:
            run = runs.pop(name, None)
            if run is not None:
                flush(name, run)
            committed.append(name, first, {name: after})
    for name, run in runs.items():
        flush(name, run)


def _note_successor(first: _Path, then: _Path) -> None:
    successors = first.successors
    if successors and successors[0][0] is then:
        return
    for position, entry in enumerate(successors):
        if entry[0] is then:
            successors.insert(0, successors.pop(position))
            return
    # A constant of the first path stays put while it fires, and it held at the value
    # *then* wants the time this was noted, so only the others need asking.
    constants = {key for key, _value in first.trace.constants}
    check = tuple(
        (key, value)
        for key, value in then.trace.constants
        if key in first.trace.seeds or key not in constants
    )
    successors.insert(0, (then, check))
    del successors[MAX_SUCCESSORS:]


@dataclass(eq=False, slots=True)
class _Segment:
    """Scans of one path in a stretch, and where they left the tags it moves."""

    path: _Path
    first: int
    count: int
    ends: dict[str, Any]


# The cells the scan hooks read between scans.  A jump skips those reads, so none may move.
_HOOK_CELLS = frozenset(
    [("tags", name) for name in (*HOOK_BOUND_TAG_NAMES, _SD_EJECT_NAME, _SD_DELETE_ALL_NAME)]
    + [("memory", key) for key in _SCAN_START_MEMORY_KEYS]
)


def _set_cell(kernel: Any, key: tuple[Any, ...], value: Any) -> None:
    domain = key[0]
    if domain == "tags":
        kernel.tags[key[1]] = value
    elif domain == "memory":
        kernel.memory[key[1]] = value
    else:
        kernel.blocks[key[1]][key[2]] = value


class CycleMismatch(scan_trace.SelfCheckMismatch):
    """The scans the loop ran disagree with a prediction of their cycle."""


class _CycleWatch:
    """Follows the loop's segments through the cycle they repeat, and its prediction."""

    def __init__(self) -> None:
        self.keys: list[tuple[_Path, int]] = []
        self.cycle: list[tuple[_Path, int]] | None = None
        self.phase = -1  # segments of the cycle since it last began, or -1 when lost
        self.prediction: cycle_jump.Prediction | None = None
        self.start_scan = 0
        # The timestamp the scans since the prediction reached, and at which scan.
        self.time_mark = (0, 0.0)
        self.ends: dict[int, int] = {}  # scans into a cycle -> the segment that ends there
        self.wait_until = 0
        self.wait = 1
        self.names: dict[tuple[Any, ...], str] = {}  # the cells the stop predicate reads
        self.hooked = False  # whether a moving cell is one a scan hook reads
        self.cut = False  # whether something ended the prediction before its horizon

    def note(self, segment: _Segment) -> bool:
        """Follow one more segment.  True if it ends a cycle."""
        key = (segment.path, segment.count)
        keys = self.keys
        keys.append(key)
        if len(keys) > 4 * MAX_CYCLE_SEGMENTS:
            del keys[: -2 * MAX_CYCLE_SEGMENTS]
        cycle = self.cycle
        if cycle is None:
            return False
        if self.phase >= 0:
            if key == cycle[self.phase]:
                self.phase = (self.phase + 1) % len(cycle)
                return self.phase == 0
            self.phase = -1
            return False
        if key == cycle[-1] and keys[-len(cycle) :] == cycle:
            self.phase = 0
            return True
        return False

    def found(self, segments: list[_Segment], runs: list[tuple[int, int, int]]) -> bool:
        """Take the cycle of the last run if the segments end in it.  True if they end one."""
        if self.phase >= 0 or not runs:
            return False
        lead, length, cycles = runs[-1]
        end = lead + cycles * length
        cycle = [(segment.path, segment.count) for segment in segments[lead : lead + length]]
        tail = [(segment.path, segment.count) for segment in segments[end:]]
        if len(tail) >= length or tail != cycle[: len(tail)]:
            return False
        self.cycle = cycle
        self.phase = len(tail)
        self.prediction = None
        return self.phase == 0


class _NotDeclared(Exception):
    """A declared stop predicate read something it did not declare."""


class _Seen(dict[str, Any]):
    """The committed tags a declared predicate reads, as a loop stretch moves them.

    Reading anything else raises, which ends the stretch: the real state answers.
    """

    __slots__ = ("_reads",)

    def __init__(self, values: dict[str, Any], reads: frozenset[str]) -> None:
        super().__init__(values)
        self._reads = reads

    def _allowed(self, name: str) -> None:
        if name not in self._reads:
            raise _NotDeclared(name)

    def get(self, key: object, default: Any = None, /) -> Any:
        self._allowed(str(key))
        return super().get(key, default)

    def __getitem__(self, name: str) -> Any:
        self._allowed(name)
        return super().__getitem__(name)

    def __contains__(self, name: object) -> bool:
        self._allowed(str(name))
        return super().__contains__(name)

    def __iter__(self) -> Any:
        raise _NotDeclared("*")

    def keys(self) -> Any:
        raise _NotDeclared("*")

    def items(self) -> Any:
        raise _NotDeclared("*")

    def values(self) -> Any:
        raise _NotDeclared("*")


class _SeenState:
    """What a declared predicate is handed in a loop: the tags and nothing else."""

    __slots__ = ("tags",)

    def __init__(self, tags: _Seen) -> None:
        self.tags = tags


def _moves_a_watched_tag(path: _Path, watched: frozenset[str]) -> bool:
    """Whether this path's records say a scan moves a tag its watched loop holds still.

    A watched tag's loop leaves before the tag changes, so every scan it runs holds the tag,
    and the path's records (``path.moves``, the capture) are of a scan that moved it.  A path
    of a scan that holds the tag covers those scans, and records them as they ran.
    """
    if not watched:
        return False
    moves = path.moves
    return (
        any(name in watched for name, _delta in moves.moved)
        or any(name in watched for name in moves.free)
        or any(name in watched for name, _before, _after in moves.flips)
    )


def _loop_of(
    path: _Path,
    replay: CompiledPLC,
    watched: frozenset[str],
    reads: frozenset[str],
) -> _Loop:
    key = (watched & path.written, reads & path.written)
    loop = path.loops.pop(key, None)
    if loop is None:
        plan = path.trace.plan
        assert plan is not None
        started = time.perf_counter()
        function, moves, _source, shape = coast_loop(
            plan, replay._block_index_names, key[0], key[1]
        )
        if jit_stats.ACTIVE:
            jit_stats.add_cost("loop write", time.perf_counter() - started)
        loop = _Loop(function, moves, tuple(sorted(key[0] | key[1])), shape)
        if len(path.loops) >= MAX_LOOPS_PER_PATH:
            path.loops.pop(next(iter(path.loops)))
    path.loops[key] = loop
    return loop


def _try_stretch(
    plc: PLC,
    kernel: CompiledKernel,
    memo: CoastMemo,
    predicate: Callable[[Any], bool],
    reads: frozenset[str] | None,
    watched: frozenset[str],
    limit: int,
    run: jit_stats.Run | None = None,
) -> tuple[int, _Path | None]:
    """Run loop scans from the tip while a known path covers each.

    Returns how many, and the path of the scan to come if one wants its capture.  A scan
    no known path covers is traced here, so its next occurrence stays in the loop, and
    left for the coast to run interpreted.
    """
    overrides = plc._input_overrides
    if overrides.pending_patches or overrides.forces:
        if run is not None:
            jit_stats.add_decline("attempt: a pending patch or a force")
        return 0, None
    if _kernel_cannot_say_what_it_adds(plc, kernel):
        if run is not None:
            jit_stats.add_decline("attempt: a tag not in the state may get its default")
        return 0, None
    paths = _paths_for(memo, kernel)
    if paths.off is not None:
        if run is not None:
            jit_stats.add_decline(f"attempt: the kernel's paths are off ({paths.off})")
        return 0, None
    start = plc._state
    replay = CompiledPLC(
        plc._soft_exec_program(),
        initial_state=start,
        dt=plc._dt,
        compiled=kernel,
        exact_anchor=True,
    )
    replay._set_rtc_internal(plc._rtc_base, plc._rtc_base_sim_time)
    replay.rtc_applied = []
    constrained = plc._constrained_tags
    if constrained and _BoundsWatch(replay, constrained).violated():
        if run is not None:
            jit_stats.add_decline("attempt: a value already out of its bounds")
        return 0, None
    loop = _CoastLoop(replay, kernel, paths, constrained, memo)
    if loop._off is not None:
        if run is not None:
            jit_stats.add_decline(f"run declined: {loop._off}")
        return 0, None

    declared = reads if reads is not None else frozenset()
    seen = _Seen({name: start.tags[name] for name in declared if name in start.tags}, declared)
    seen_state = _SeenState(seen)
    asked_to_stop = False

    def stop() -> bool:
        nonlocal asked_to_stop
        try:
            asked_to_stop = bool(predicate(seen_state))
        except Exception:
            asked_to_stop = True
        return asked_to_stop

    state_kernel = replay._kernel
    interval = _stretch_checkpoint_interval(plc)
    loops: dict[int, _Loop | None] = {}
    total = 0
    previous: _Path | None = None
    burst = 0
    segments: list[_Segment] = []
    # What ended the last firing and whether a guard did, while the jit counters count.
    left_by: tuple[str, bool] | None = None
    watch = _CycleWatch() if CYCLE_PREDICTIONS != "off" else None
    checking = CYCLE_PREDICTIONS == "shadow" or STRICT_CYCLE_CHECK

    asked_on: dict[Any, bool] = {}
    # The tags a prediction shows the predicate.  One copy serves every ask, as ``seen`` does.
    # Each ask sets the same cells, so the tags it does not set stay as ``seen`` holds them.
    shown = _Seen({}, declared)
    shown_state = _SeenState(shown)

    def stops_on(values: Any, names: dict[tuple[Any, ...], str]) -> bool:
        # A prediction asks about the same values on many scans, so each answer is kept until
        # the next prediction.  The predicate's other tags may change before the next prediction.
        # Values that move with the cycle are seldom asked twice, so the answers kept are few.
        key = None
        if len(asked_on) < MAX_KEPT_ANSWERS:
            try:
                key = tuple((cell, type(value), value) for cell, value in values.items())
                answer = asked_on.get(key)
            except TypeError:
                key = answer = None
            if answer is not None:
                return answer
        for cell, value in values.items():
            dict.__setitem__(shown, names[cell], value)
        try:
            answer = bool(predicate(shown_state))
        except Exception:
            answer = True
        if key is not None:
            asked_on[key] = answer
        return answer

    def predict_cycle() -> None:
        """Predict the cycle that ends here, or wait a while before asking again."""
        assert watch is not None and watch.cycle is not None
        steps: list[cycle_jump.Step] = []
        names: dict[tuple[Any, ...], str] = {}
        why = None
        for path, count in watch.cycle:
            looped = loops.get(id(path))
            iteration = _on_forms(looped) if looped else None
            if looped is None or iteration is None:
                why = "a path's loop cannot run on forms"
                break
            steps.append(
                cycle_jump.Step(iteration, path.trace.constants, count, bool(looped.shape.seen))
            )
            for name, cell, _always in looped.shape.seen:
                names[cell] = name
        period = sum(count for _path, count in watch.cycle)
        horizon = min(MAX_PREDICTED_CYCLES, (limit - total) // period)
        if why is None and period > MAX_PREDICTED_PERIOD:
            why = "the cycle is too long"
        if why is None and horizon < 2:
            why = "the run ends within two cycles"
        found: cycle_jump.Prediction | str = why or ""
        if why is None:
            asked_on.clear()
            dict.clear(shown)
            dict.update(shown, dict.items(seen))
            saved = (replay._rtc_fields, replay._rtc_fields_from, replay._rtc_fields_until)
            called = time.perf_counter() if run is not None else 0.0
            try:
                found = cycle_jump.predict(
                    steps,
                    lambda cell: _cell_value(state_kernel, cell),
                    state_kernel.timestamp,
                    replay._dt,
                    loop._rtc_fields,
                    horizon,
                    lambda values: stops_on(values, names),
                    frozenset(names),
                    least=WARM_CYCLES + 2,
                )
            finally:
                replay._rtc_fields, replay._rtc_fields_from, replay._rtc_fields_until = saved
                if run is not None:
                    jit_stats.add_cost("cycle prediction", time.perf_counter() - called)
        if isinstance(found, str):
            memo.counts[f"cycle prediction declined: {found}"] += 1
            if run is not None:
                jit_stats.add_decline(f"cycle prediction: {found}")
            watch.wait_until = state_kernel.scan_id + watch.wait * period
            watch.wait = min(watch.wait * 2, MAX_PREDICTION_WAIT)
            return
        watch.wait = 1
        watch.prediction = found
        watch.names = names
        watch.cut = found.cycles < horizon
        watch.hooked = any(cell in _HOOK_CELLS for cell in found.steps)
        if watch.hooked and CYCLE_PREDICTIONS == "jump":
            memo.counts["cycle jump declined: a scan hook reads a cell that moves"] += 1
        watch.start_scan = state_kernel.scan_id
        watch.time_mark = (state_kernel.scan_id, state_kernel.timestamp)
        offset = 0
        watch.ends = {}
        for index, (_path, count) in enumerate(watch.cycle[:-1]):
            offset += count
            watch.ends[offset] = index
        memo.counts["cycle predictions"] += 1
        memo.counts["cycles predicted"] += found.cycles

    def check_prediction() -> None:
        """Compare the state a firing left with what the prediction says it is."""
        assert watch is not None and watch.prediction is not None
        prediction = watch.prediction
        scan_id = state_kernel.scan_id
        k, into = divmod(scan_id - watch.start_scan, prediction.period)
        if into == 0 and k > prediction.cycles:
            watch.prediction = None
            return
        if not checking:
            if into == 0 and k == prediction.cycles:
                used_up()
            return
        if into == 0:
            expected = {cell: prediction.cell_at(cell, k) for cell in prediction.start}
        else:
            index = watch.ends.get(into)
            if k >= prediction.cycles or index is None:
                return
            expected = {
                cell: cycle_jump._value_at(form, k)
                for cell, form in prediction.segment_ends[index].items()
            }
        wrong = [
            cell
            for cell, value in expected.items()
            if not same(_cell_value(state_kernel, cell), value)
        ]
        if not wrong and into == 0 and STRICT_CYCLE_CHECK:
            at, timestamp = watch.time_mark
            for _ in range(scan_id - at):
                timestamp += replay._dt
            watch.time_mark = (scan_id, timestamp)
            if timestamp != state_kernel.timestamp:
                wrong = [("timestamp",)]
        if wrong:
            missed(
                f"scan {scan_id}, {k} cycles of {prediction.cycles} in, disagrees on {wrong[:5]}"
            )
            return
        if into == 0 and k == prediction.cycles:
            used_up()

    def used_up() -> None:
        """Drop a prediction whose cycles have all run.

        A change that ended it before its horizon starts on this cycle, so a prediction here
        would decline.  The next try then waits for the cycle after.
        """
        assert watch is not None
        if watch.cut:
            watch.wait_until = max(watch.wait_until, state_kernel.scan_id + 1)
        watch.prediction = None

    def missed(what: str) -> None:
        assert watch is not None
        message = f"cycle prediction: {what}"
        for observer in scan_trace.SELF_CHECK_OBSERVERS:
            observer(message)
        if STRICT_CYCLE_CHECK:
            raise CycleMismatch(message)
        memo.counts["cycle prediction mismatches"] += 1
        watch.prediction = None

    def jump() -> None:
        """Move the state over the predicted cycles left, up to the next checkpoint.

        The records of the cycles skipped go in as a repeat of the last cycle.  That needs
        the last few cycles in the records to be cycles of this prediction.
        """
        assert watch is not None and watch.prediction is not None
        prediction = watch.prediction
        period = prediction.period
        scan_id = state_kernel.scan_id
        k, into = divmod(scan_id - watch.start_scan, period)
        if into or k < WARM_CYCLES:
            return
        room = min(limit - total, interval - scan_id % interval)
        cycles = min(prediction.cycles - k, room // period)
        if cycles < 1:
            return
        if watch.hooked:
            return
        called = time.perf_counter() if run is not None else 0.0
        try:
            jump_from(prediction, k, cycles)
        finally:
            if run is not None:
                jit_stats.add_cost("cycle jump", time.perf_counter() - called)

    def jump_from(prediction: cycle_jump.Prediction, k: int, cycles: int) -> None:
        nonlocal total, segments
        assert watch is not None
        period = prediction.period
        scan_id = state_kernel.scan_id
        # Stepping must have reached the state the prediction says, as a check on it.
        wrong = [
            cell
            for cell in prediction.start
            if not same(_cell_value(state_kernel, cell), prediction.cell_at(cell, k))
        ]
        if wrong:
            missed(f"scan {scan_id}, at a jump, disagrees on {wrong[:5]}")
            return
        _record_segments(plc, segments, paths)
        segments = []
        timelines_held = (
            plc._rung_firing_timelines,
            plc._node_firing_timelines,
            plc._committed_tag_timelines,
        )
        recorded = sum(timelines.run_count() for timelines in timelines_held) < MAX_RECORD_RUNS
        plans: list[RepeatPlan] = []
        if recorded:
            for timelines in timelines_held:
                plan = timelines.plan_repeat(scan_id, period)
                if plan is None:
                    memo.counts["cycle jump declined: the records do not repeat"] += 1
                    # Each try writes the records out first, so the next waits a while.
                    watch.prediction = None
                    watch.wait_until = scan_id + watch.wait * period
                    watch.wait = min(watch.wait * 2, MAX_PREDICTION_WAIT)
                    return
                cycles = min(cycles, plan.limit)
                plans.append(plan)
            if cycles < 1:
                return
        scans = cycles * period
        if recorded:
            for plan in plans:
                plan.apply(cycles)
        else:
            plc._note_unrecorded_scans(scan_id + 1, scan_id + scans)
        landing = k + cycles
        for cell in prediction.steps:
            _set_cell(state_kernel, cell, prediction.cell_at(cell, landing))
        for cell, name in watch.names.items():
            dict.__setitem__(seen, name, _cell_value(state_kernel, cell))
        # The time each scan adds, one scan at a time, as the loop adds it.
        timestamp = state_kernel.timestamp
        dt = replay._dt
        for _ in range(scans):
            timestamp += dt
        state_kernel.timestamp = timestamp
        state_kernel.scan_id += scans
        watch.time_mark = (state_kernel.scan_id, timestamp)
        total += scans
        scan_trace.TOTALS["loop scans"] += scans
        scan_trace.TOTALS["jumped scans"] += scans
        memo.counts["cycle jumps"] += 1
        memo.counts["cycles jumped"] += cycles
        if landing == prediction.cycles:
            used_up()
            if state_kernel.scan_id >= watch.wait_until:
                predict_cycle()

    def follow_cycle(segment: _Segment) -> None:
        assert watch is not None
        ends_a_cycle = watch.note(segment)
        if watch.prediction is not None:
            check_prediction()
        if watch.prediction is not None:
            if CYCLE_PREDICTIONS == "jump":
                jump()
        elif ends_a_cycle and state_kernel.scan_id >= watch.wait_until:
            predict_cycle()

    def fire(path: _Path) -> int:
        """Fire *path* for what its loop allows, or not at all."""
        nonlocal total, burst, left_by
        looped = loops.get(id(path), False)
        if looped is False:
            looped = (
                None
                if _moves_a_watched_tag(path, watched)
                else _loop_of(path, replay, watched, declared)
            )
            if looped is not None and any(
                name not in start.tags for name in looped.must_be_present
            ):
                # A tag becomes visible on this scan, and a reader sees that as a change.
                looped = None
            loops[id(path)] = looped
        if looped is None:
            return 0
        room = min(limit - total, interval - state_kernel.scan_id % interval)
        sizes = scan_trace.BURST_SIZES
        if sizes:
            room = min(room, sizes[burst % len(sizes)])
            burst += 1
        called = 0.0
        if run is not None:
            start_counting_exit(looped.function)
            called = time.perf_counter()
        used, timestamp = looped.function(
            state_kernel.tags,
            state_kernel.blocks,
            state_kernel.memory,
            state_kernel.timestamp,
            replay._dt,
            room,
            loop._rtc_fields,
            seen,
            stop,
        )
        if run is not None:
            jit_stats.add_cost("loop", time.perf_counter() - called)
        if used:
            if run is not None:
                if left_by is not None and left_by[1]:
                    jit_stats.add_handover(left_by[0])
                left_by = (
                    exit_of(looped.function, _source_of(kernel))
                    if used < room
                    else ("its scan limit", False)
                )
            segments.append(
                _Segment(
                    path,
                    state_kernel.scan_id + 1,
                    used,
                    {name: _cell_value(state_kernel, key) for name, key in path.ending},
                )
            )
            state_kernel.scan_id += used
            state_kernel.timestamp = timestamp
            _record_block_writes(path.trace, state_kernel.blocks)
            total += used
            scan_trace.TOTALS["loop scans"] += used
            if watch is not None:
                follow_cycle(segments[-1])
        return used

    kept = paths.paths
    blocked = None
    while total < limit and not asked_to_stop:
        if memo.check_limits is not None:
            memo.check_limits()
        blocked = loop._blocked()
        if blocked is not None:
            break
        fired: _Path | None = None
        if previous is not None:
            for candidate, check in previous.successors:
                if candidate.capture is None or any(
                    not same(_cell_value(state_kernel, key), value) for key, value in check
                ):
                    continue
                if fire(candidate):
                    fired = candidate
                    break
                memo.counts["successor misfires"] += 1
        if fired is None:
            for position in range(len(kept) - 1, -1, -1):
                candidate = kept[position]
                if candidate.capture is None or not candidate.trace.holds(replay, None):
                    continue
                if fire(candidate):
                    fired = candidate
                    kept.append(kept.pop(position))
                    break
                memo.counts["misfires"] += 1
        if fired is None:
            # What the state does next, no path knows.  Whatever has run is settled now,
            # since the trace's check steps the kernel past it.
            if total:
                _record_segments(plc, segments, paths)
                _land(plc, replay)
            if run is not None:
                if not total:
                    jit_stats.add_decline("attempt: no known path covers the scan")
                elif left_by is not None and left_by[1]:
                    run.stretch(start.scan_id + 1, total, left_by[0], guard_failed=True)
                else:
                    run.stretch(
                        start.scan_id + 1,
                        total,
                        "no known path covers the next scan",
                        guard_failed=True,
                    )
            # Traced even with little room left.  how() charges an interpreted scan a hundred
            # times a loop scan, so a skipped trace would spend its ledger and change its answer.
            loop.learn(previous or (kept[-1] if kept else None))
            loop.finish()
            return total, loop.learned
        if previous is not None:
            _note_successor(previous, fired)
        previous = fired
        memo.counts["fires"] += 1
        checkpoint = state_kernel.scan_id % interval == 0
        if checkpoint or compiled_run.LOOP_OBSERVERS:
            state = replay._materialize_replay_state()
            if checkpoint:
                runs = _record_segments(plc, segments, paths)
                if watch is not None and watch.found(segments, runs) and watch.prediction is None:
                    predict_cycle()
                segments = []
                _install(plc, replay, state)
                plc._thin_checkpoints(MAX_CHECKPOINTS)
            for observer in compiled_run.LOOP_OBSERVERS:
                observer(plc, state)
    loop.finish()
    if total:
        _record_segments(plc, segments, paths)
        _land(plc, replay)
    if run is not None:
        # A scan the loop cannot run is a real change, so no stretch could run past it.
        blocked_here = False
        if total >= limit:
            ended = "the run's end"
        elif asked_to_stop:
            ended = "the stop predicate asked"
        else:
            ended = f"a scan the loop cannot run ({blocked})"
            blocked_here = True
        if total:
            run.stretch(start.scan_id + 1, total, ended, guard_failed=blocked_here)
        else:
            jit_stats.add_decline(f"attempt: {ended}")
    return total, None


def _stretch_checkpoint_interval(plc: PLC) -> int:
    return plc._checkpoint_interval * STRETCH_CHECKPOINT_INTERVALS


def _land(plc: PLC, replay: CompiledPLC) -> None:
    if plc._state.scan_id != replay._kernel.scan_id:
        _install(plc, replay, replay._materialize_replay_state())
    plc._record_landing()
    plc._bounds_violations = {}
    plc._epoch_caches.on_tip_advanced()


def coast_until(
    plc: PLC,
    predicate: Callable[[Any], bool],
    *,
    budget: int,
    predicate_reads: frozenset[str] | None = None,
    stats: dict[str, int] | None = None,
    on_kernel_scan: Callable[[int], None] | None = None,
    loop_scans_per_unit: int = 1,
) -> bool:
    """Run *plc* up to *budget* scans, until *predicate* holds on a committed scan.

    Stops as stepping would: on the first scan where it holds, or at the budget, or
    after a scan that asks to pause, or after the program stops itself.  Returns whether it
    holds there.  *loop_scans_per_unit* is how many loop scans one unit of *budget* pays
    for.  An interpreted scan is always a unit.  *predicate_reads* is the exact set of tags
    *predicate* reads through ``state.tags``.  Without it the reads are traced.  The predicate
    must answer from those tags alone, with no counter, clock or global of its own.  A loop
    scan that changes none of them does not ask it again.  *stats* gets
    ``logical_scans``, ``kernel_scans``, ``stretches`` and ``stretched_scans`` (the logical
    scans ``kernel_scans`` leaves).  *on_kernel_scan* and ``kernel_scans`` have each
    interpreted scan and the landing, which a stretch may have run.
    """
    options = (predicate_reads, stats, on_kernel_scan, loop_scans_per_unit)
    if not jit_stats.ACTIVE:
        return _coast_until(plc, predicate, budget, *options, None)
    run = jit_stats.Run("coast_until()")
    start_scan = plc.state.scan_id
    try:
        return _coast_until(plc, predicate, budget, *options, run)
    finally:
        end = plc.state.scan_id
        run.end(end + 1)
        jit_stats.add_scans("interpreted", end - start_scan - run.loop_scans)


def _coast_until(
    plc: PLC,
    predicate: Callable[[Any], bool],
    budget: int,
    predicate_reads: frozenset[str] | None,
    stats: dict[str, int] | None,
    on_kernel_scan: Callable[[int], None] | None,
    loop_scans_per_unit: int,
    run: jit_stats.Run | None,
) -> bool:
    start_scan = plc.state.scan_id
    kernel_scans = 0
    stretches = 0
    last_noted = start_scan

    def note(scan: int) -> None:
        nonlocal kernel_scans, last_noted
        last_noted = scan
        kernel_scans += 1
        if on_kernel_scan is not None:
            on_kernel_scan(scan)

    def finish(reached: bool) -> bool:
        # A stretch may be what landed, and a caller reads its answer there.
        if plc.state.scan_id != last_noted:
            note(plc.state.scan_id)
        if stats is not None:
            logical = plc.state.scan_id - start_scan
            stats.update(
                logical_scans=logical,
                kernel_scans=kernel_scans,
                stretches=stretches,
                stretched_scans=logical - kernel_scans,
            )
        return reached

    memo = memo_of(plc)
    # Compiled at the first attempt at a stretch, so a call that ends before one compiles
    # nothing.  Which kernel a runner gets does not depend on its state.
    kernel: CompiledKernel | None = None
    can_loop = (
        plc._program is not None
        and not plc._replay_mode
        and plc._time_mode == TimeMode.FIXED_STEP
        and not any(r.enabled and not r.removed for r in plc._breakpoints_by_id.values())
    )
    monitored = (
        frozenset(r.tag_name for r in plc._monitors_by_id.values() if r.enabled and not r.removed)
        if can_loop
        else frozenset()
    )
    # Nothing in a loop scan writes these, and a reader must still see them change.
    if can_loop and _moves_without_a_write(monitored | (predicate_reads or frozenset())):
        can_loop = False
        if run is not None:
            jit_stats.add_decline("interpreted: a watched tag moves without a write")
    elif not can_loop and run is not None:
        jit_stats.add_decline(f"interpreted: {_why_interpreted(plc)}")
    wait = ENTRY_WAIT
    since = 0
    fails = 0
    learned: _Path | None = None
    # The ledger charges a seek when it returns, so one that cannot loop could spend it many
    # times over first.  A loop scan is a hundredth of one of these, and stays out of it.
    work = None if memo.work_left is None else max(1, memo.work_left())

    def charged() -> int:
        return work_charged(plc.state.scan_id - start_scan, kernel_scans, loop_scans_per_unit)

    while charged() < budget:
        if work is not None and kernel_scans >= work:
            if stats is not None:
                stats["out_of_work"] = 1
            break
        if memo.check_limits is not None:
            memo.check_limits()
        plc._consume_pause_request()
        plc._ensure_running()
        plc._run_single_scan(consume_pause_request=False)
        note(plc._state.scan_id)
        since += 1
        if not plc._running:
            # The program stopped itself.  The next scan would be a restart, a new recording
            # with scan ids from 0, and that is for a caller to choose.
            if stats is not None:
                stats["stopped"] = 1
            return finish(bool(predicate(plc.state)))
        if learned is not None:
            assert kernel is not None
            if learned.capture is None:
                learned.capture = _capture_of(
                    plc, learned, _paths_for(memo, kernel), plc._state.scan_id
                )
            learned = None
        paused = plc._consume_pause_request()
        hit = bool(predicate(plc.state))
        if hit or paused:
            return finish(hit)
        limit = (budget - charged()) * loop_scans_per_unit
        if not can_loop or since < wait or limit < 2:
            continue
        since = 0
        if kernel is None:
            kernel = _kernel_for(plc, memo)
            if kernel is None:
                can_loop = False
                if run is not None:
                    jit_stats.add_decline("interpreted: no kernel")
                continue
        reads = predicate_reads
        watched = monitored
        if reads is None:
            try:
                _hit, traced = _traced_call(predicate, plc)
            except Exception:
                can_loop = False
                if run is not None:
                    jit_stats.add_decline("interpreted: tracing the stop predicate's reads raised")
                continue
            if (
                _READ_ALL in traced
                or any(str(name).startswith(_FIELD_READ) for name in traced)
                or _moves_without_a_write(traced)
            ):
                wait = min(wait * 2, MAX_WAIT_AFTER_NONE)
                if run is not None:
                    jit_stats.add_decline(
                        "attempt: the stop predicate reads what a loop cannot watch"
                    )
                continue
            watched = monitored | {str(name) for name in traced}
        used, learned = _try_stretch(plc, kernel, memo, predicate, reads, watched, limit, run)
        if used:
            stretches += 1
            paused = plc._consume_pause_request()
            hit = bool(predicate(plc.state))
            if hit or paused:
                return finish(hit)
            if used >= PAYING_STRETCH or plc.state.scan_id % _stretch_checkpoint_interval(plc) == 0:
                fails = 0
                wait = ENTRY_WAIT
            else:
                fails += 1
                wait = min(ENTRY_WAIT * 2**fails, MAX_WAIT_AFTER_SHORT)
        else:
            fails += 1
            wait = min(ENTRY_WAIT * 2**fails, MAX_WAIT_AFTER_NONE)
    return finish(bool(predicate(plc.state)))


def _why_interpreted(plc: PLC) -> str:
    if plc._program is None:
        return "no program"
    if plc._replay_mode:
        return "replay mode"
    if plc._time_mode != TimeMode.FIXED_STEP:
        return "not fixed-step time"
    return "a breakpoint"


def work_charged(logical_scans: int, kernel_scans: int, loop_scans_per_unit: int) -> int:
    """What *logical_scans* cost, *kernel_scans* of them interpreted and the rest in the loop."""
    return kernel_scans + (logical_scans - kernel_scans) // loop_scans_per_unit


def _moves_without_a_write(names: Any) -> bool:
    return any(
        name in MOVING_SYSTEM_TAGS
        or name in DECLINING_SYSTEM_TAGS
        or (name in SYSTEM_TAGS_BY_NAME and name not in _CLASSIFIED_SYSTEM_TAGS)
        for name in names
    )
