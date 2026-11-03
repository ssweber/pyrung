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

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core import compiled_run, scan_trace
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.compiled_run import _BoundsWatch, _install, _kernel_cannot_say_what_it_adds
from pyrung.core.read_trace import _FIELD_READ, _READ_ALL, _traced_call
from pyrung.core.scan_trace import (
    _CLASSIFIED_SYSTEM_TAGS,
    DECLINING_SYSTEM_TAGS,
    MOVING_SYSTEM_TAGS,
    CoastMoves,
    ScanLoop,
    ScanTrace,
    _record_block_writes,
    attempted_stores,
    coast_loop,
    coast_moves,
    moving_tags,
    same,
)
from pyrung.core.scan_trace import _current_value as _cell_value
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.time_mode import TimeMode

if TYPE_CHECKING:
    from pyrung.core.kernel import CompiledKernel
    from pyrung.core.runner import PLC

# Kernels a memo keeps with their paths.  Each is a compiled module and a parsed source.
MAX_KERNELS = 12
# Paths a kernel keeps, least recently fired out first.  A path holds its trace's
# operations and each loop written from them.
MAX_PATHS = 256
MAX_LOOPS_PER_PATH = 8
MAX_SUCCESSORS = 4
# Interpreted scans between attempts at a stretch: at entry and after a good stretch,
# and how far apart they may get when stretches do not come or do not last.
ENTRY_WAIT = 2
MAX_WAIT_AFTER_SHORT = 64
MAX_WAIT_AFTER_NONE = 256
# A stretch this long paid for its entry into the kernel.
PAYING_STRETCH = 16


@dataclass(eq=False)
class _Loop:
    function: Callable[..., tuple[int, float]]
    moves: CoastMoves
    must_be_present: tuple[str, ...]


@dataclass(eq=False)
class _Capture:
    """What one interpreted scan of a path recorded, by rung and by subroutine rung."""

    columns: tuple[dict[Any, tuple[dict[str, Any], frozenset[str]]], ...]
    # Those with nothing moving in them, which every scan of the path repeats.
    settled: tuple[dict[Any, tuple[dict[str, Any], frozenset[str]]], ...]


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
    check_time: Callable[[], None] | None = None


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
        return cached  # type: ignore[return-value]
    synthesis = plc._synthesis
    plant = () if synthesis is None else tuple(synthesis.plant)
    holds = () if synthesis is None else tuple(synthesis.holds)
    key = (id(plc._program), tuple(map(id, plant)), tuple(map(id, holds)))
    entry = memo.kernels.get(key)
    if entry is not None:
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
    for timelines in (plc._rung_firing_timelines, plc._node_firing_timelines):
        these: dict[Any, tuple[dict[str, Any], frozenset[str]]] = {}
        still: dict[Any, tuple[dict[str, Any], frozenset[str]]] = {}
        for key, recorded in timelines.at(scan_id).items():
            writes = dict(recorded)
            varied = frozenset(tag for tag in writes if timelines.varied_on(key, tag, scan_id))
            # By repr, or 1 and True would be one column.
            identity = (key, repr(sorted(writes.items())), varied)
            column = interned.setdefault(identity, (writes, varied))
            these[key] = column
            if not (moving & writes.keys()):
                still[key] = column
        columns.append(these)
        settled.append(still)
    return _Capture(tuple(columns), tuple(settled))


def _column(
    path: _Path, column: tuple[dict[str, Any], frozenset[str]], ends: dict[str, Any], count: int
) -> tuple[dict[str, Any], frozenset[str], dict[str, int], set[str]]:
    """What one rung's capture says over *count* scans: values, varied, deltas, unknowns."""
    writes, varied = column
    deltas = dict(path.moves.moved)
    held = set(path.moves.held)
    values = dict(writes)
    moving: dict[str, int] = {}
    unknown: set[str] = set()
    for tag in writes:
        total, symbolic = path.stores.get(tag, (0, 0))
        if not symbolic:
            continue  # every attempt on this path is a constant
        if total == 1 and tag in deltas and tag in ends:
            values[tag] = ends[tag] - deltas[tag] * (count - 1)
            moving[tag] = deltas[tag]
        elif total == 1 and tag in held and tag in ends:
            values[tag] = ends[tag]
        else:
            # Which attempt a rung made, or its value on a later scan, no one capture says.
            unknown.add(tag)
            if total > 1:
                varied |= {tag}
    return values, varied, moving, unknown


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


def _record_segments(plc: PLC, segments: list[_Segment], kernel_paths: _KernelPaths) -> None:
    """Write what a stretch's scans did into the three timelines a stepped run writes."""
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
        for segment in segments:
            capture = segment.path.capture
            assert capture is not None
            for key, column in capture.columns[level].items():
                if key in here:
                    continue
                values, varied, deltas, unknown = _column(
                    segment.path, column, segment.ends, segment.count
                )
                timelines.append_repeated(
                    key, segment.first, segment.count, values, varied, deltas, unknown
                )
    committed = plc._committed_tag_timelines
    for segment in segments:
        moves = segment.path.moves
        first, count, ends = segment.first, segment.count, segment.ends
        for name, delta in moves.moved:
            base = ends[name] - delta * (count - 1)
            committed.append_repeated(name, first, count, {name: base}, deltas={name: delta})
        for name in moves.free:
            committed.append_repeated(name, first, count, {name: 0.0}, unknown={name})
        for name, _before, after in moves.flips:
            committed.append(name, first, {name: after})


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

    def get(self, name: str, default: Any = None) -> Any:
        self._allowed(name)
        return super().get(name, default)

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
        function, moves, _source = coast_loop(plan, replay._block_index_names, key[0], key[1])
        loop = _Loop(function, moves, tuple(sorted(key[0] | key[1])))
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
) -> tuple[int, _Path | None]:
    """Run loop scans from the tip while a known path covers each.

    Returns how many, and the path of the scan to come if one wants its capture.  A scan
    no known path covers is traced here, so its next occurrence stays in the loop, and
    left for the coast to run interpreted.
    """
    overrides = plc._input_overrides
    if overrides.pending_patches or overrides.forces:
        return 0, None
    if _kernel_cannot_say_what_it_adds(plc, kernel):
        return 0, None
    paths = _paths_for(memo, kernel)
    if paths.off is not None:
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
        return 0, None
    loop = _CoastLoop(replay, kernel, paths, constrained, memo)
    if loop._off is not None:
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
    interval = plc._checkpoint_interval
    loops: dict[int, _Loop | None] = {}
    total = 0
    previous: _Path | None = None
    burst = 0
    segments: list[_Segment] = []

    def fire(path: _Path) -> int:
        """Fire *path* for what its loop allows, or not at all."""
        nonlocal total, burst
        looped = loops.get(id(path), False)
        if looped is False:
            looped = _loop_of(path, replay, watched, declared)
            if any(name not in start.tags for name in looped.must_be_present):
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
        if used:
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
        return used

    kept = paths.paths
    while total < limit and not asked_to_stop:
        if memo.check_time is not None:
            memo.check_time()
        if loop._blocked() is not None:
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
                _record_segments(plc, segments, paths)
                segments = []
                _install(plc, replay, state)
            for observer in compiled_run.LOOP_OBSERVERS:
                observer(plc, state)
    loop.finish()
    if total:
        _record_segments(plc, segments, paths)
        _land(plc, replay)
    return total, None


def _land(plc: PLC, replay: CompiledPLC) -> None:
    if plc._state.scan_id != replay._kernel.scan_id:
        _install(plc, replay, replay._materialize_replay_state())
    plc._record_fold_landing()
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
) -> bool:
    """Run *plc* up to *budget* scans, until *predicate* holds on a committed scan.

    Stops as stepping would: on the first scan where it holds, or at the budget, or
    after a scan that asks to pause, or after the program stops itself.  Returns whether it
    holds there.  *predicate_reads* is the exact set of tags *predicate* reads
    through ``state.tags``; without it the reads are traced.  *stats* gets
    ``logical_scans``, ``kernel_scans``, ``macro_folds`` (stretches) and
    ``ordinary_folded_scans`` (scans in them).  *on_kernel_scan* and ``kernel_scans``
    have each interpreted scan and the landing, which a stretch may have run.
    """
    start_scan = plc.state.scan_id
    kernel_scans = 0
    stretches = 0
    stretched = 0
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
                macro_folds=stretches,
                ordinary_folded_scans=stretched,
                skipped_scans=logical - kernel_scans,
            )
        return reached

    memo = memo_of(plc)
    kernel = None
    can_loop = (
        plc._program is not None
        and not plc._replay_mode
        and plc._time_mode == TimeMode.FIXED_STEP
        and not any(r.enabled and not r.removed for r in plc._breakpoints_by_id.values())
    )
    if can_loop:
        kernel = _kernel_for(plc, memo)
        can_loop = kernel is not None
    monitored = (
        frozenset(r.tag_name for r in plc._monitors_by_id.values() if r.enabled and not r.removed)
        if can_loop
        else frozenset()
    )
    # Nothing in a loop scan writes these, and a reader must still see them change.
    if can_loop and _moves_without_a_write(monitored | (predicate_reads or frozenset())):
        can_loop = False
    wait = ENTRY_WAIT
    since = 0
    fails = 0
    learned: _Path | None = None
    while plc.state.scan_id - start_scan < budget:
        if memo.check_time is not None:
            memo.check_time()
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
        limit = budget - (plc.state.scan_id - start_scan)
        if not can_loop or since < wait or limit < 2:
            continue
        since = 0
        reads = predicate_reads
        watched = monitored
        if reads is None:
            try:
                _hit, traced = _traced_call(predicate, plc)
            except Exception:
                can_loop = False
                continue
            if (
                _READ_ALL in traced
                or any(str(name).startswith(_FIELD_READ) for name in traced)
                or _moves_without_a_write(traced)
            ):
                wait = min(wait * 2, MAX_WAIT_AFTER_NONE)
                continue
            watched = monitored | {str(name) for name in traced}
        assert kernel is not None
        used, learned = _try_stretch(plc, kernel, memo, predicate, reads, watched, limit)
        if used:
            stretches += 1
            stretched += used
            paused = plc._consume_pause_request()
            hit = bool(predicate(plc.state))
            if hit or paused:
                return finish(hit)
            if used >= PAYING_STRETCH or plc.state.scan_id % plc._checkpoint_interval == 0:
                fails = 0
                wait = ENTRY_WAIT
            else:
                fails += 1
                wait = min(ENTRY_WAIT * 2**fails, MAX_WAIT_AFTER_SHORT)
        else:
            fails += 1
            wait = min(ENTRY_WAIT * 2**fails, MAX_WAIT_AFTER_NONE)
    return finish(bool(predicate(plc.state)))


def _moves_without_a_write(names: Any) -> bool:
    return any(
        name in MOVING_SYSTEM_TAGS
        or name in DECLINING_SYSTEM_TAGS
        or (name in SYSTEM_TAGS_BY_NAME and name not in _CLASSIFIED_SYSTEM_TAGS)
        for name in names
    )
