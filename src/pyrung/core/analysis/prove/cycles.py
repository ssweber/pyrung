"""Hidden progress made across multi-scan cycles.

Timer and counter accumulators, and integer counters absorbed into threshold
vectors, are hidden from the BFS state key: two states that differ only in a
hidden accumulator below its thresholds share one key.  Hidden-event jumps
(``events.py``) recover the crossings, but only from a *self-loop*: a state
whose stutter step returns to the same key while the accumulator advances.

Progress made on any other cycle is lost.  ``Rung(rise(X)): calc(N + 1, N)``
counts once per pulse, ``Rung(M != P): calc(N + 1, N); copy(M, P)`` once per
change, and a timer that runs while a free-running bit toggles advances on a
two-scan cycle.  Each loop returns to an already-visited key with more
progress than the state first explored there, the revisit is dropped, and a
reachable crossing (``N >= 3``, the timer's Done) is never seen.

``_CycleTracker`` keeps such revisits:

- The *hidden vector* of a state is the normalized progress of every hidden
  accumulator that still has an uncrossed threshold at its key.
- A revisit is *covered*, and dropped, when a node kept at its key has every
  counter equal and reaches the revisit's *time-like* accumulators by
  stuttering: one number of held-input scans, at each accumulator's per-scan
  advance (measured by the probe), takes the node's values to the revisit's.
  Stepping that node with its inputs held returns to the same key, so it
  reaches the revisit, and its self-loop jump schedules the crossing (the
  existing timer treatment).  Stuttering advances every time-like
  accumulator together: a revisit ahead on one timer and level with another
  (``B`` pressed partway through a timer started with ``A``) is not covered,
  since no kept node gets there.  Nor is one *behind* on a time-like
  accumulator: it has more time left before a threshold (a timer started
  later on another branch).
- When the revisit descends from a node with the same key, the path between
  them is a cycle.  If every link on it replays by holding its inputs for its
  scans (one scan, or a hidden-event jump with no caveat, as a self-resetting
  timer's lap jumps to its Done) and each accumulator it advances only
  advances along it, repeating the cycle adds the same progress again,
  because an absorbed accumulator is read only by the threshold comparisons
  (the absorption gate), which stay false below the thresholds.  An
  accumulator that falls back inside the cycle but returns to the same
  progress (that timer) is periodic and stays put.  The node is then
  *pumped*: advanced by as many further cycles as keep every threshold
  uncrossed, and its trace repeats the cycle that many times; traversing it
  once more from the revisit first confirms each accumulator repeats exactly
  (a timer on its carry recurrence).  From there the next traversal crosses.
- A pumped node's successors inherit its progress: a revisit below one is
  kept while it is not covered.
- Any other revisit whose counters (``count_up``/``count_down`` and absorbed
  integer or real counters) differ from the first node's is kept: its value
  was made on another branch, ahead (a pulse that came with the step that
  armed a timer) or behind (the first node took a shortcut that also
  counted), and neither is reachable from the first node.  Its descendants
  close any cycle.  A timer that advanced around a cycle the BFS tree does
  not show, not time-like at the key, is kept once per key, when its
  thresholds resolve.
- A counter with a dead guard (``deadness.py``) is left out of the hidden
  vector at a key where the guard is false, as if its thresholds were all
  crossed: nothing reads its value there before it is overwritten, so states
  that differ in it run alike.  A runaway count left over from the last
  request no longer duplicates every key until the next one.

What is left: a timer whose value at a key differs by the branch it came
from, and that does not advance while the key holds still, is represented by
the first node plus one seed per key.

A timer toward a tag preset (an abstract threshold) is tracked like any
other; it is pumped or seeded only at a key where every threshold still
ahead of it is exact (the preset itself crossed, ``T.Acc >= 40`` not yet).

Accumulators read outside threshold comparisons are never absorbed, so the
concrete value is in the key and no tracking is needed.  Comparison-only
vectors are not tracked either: they compare against literals or stable tags
only (see ``absorb._find_comparison_absorptions``), so the key already holds
everything their hidden value decides.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .absorb import (
    _DONE_KIND_COUNT_DOWN,
    _DONE_KIND_COUNT_UP,
    _DONE_KIND_OFF_DELAY,
    _DONE_KIND_ON_DELAY,
    _DONE_KIND_TIME_DRUM,
    _PROGRESS_KIND_INT_DOWN,
    _PROGRESS_KIND_INT_UP,
    _PROGRESS_KIND_REAL_DOWN,
    _PROGRESS_KIND_REAL_UP,
    _THRESHOLD_FORM_GT,
    _THRESHOLD_KIND_COMPARISON_ONLY,
    _THRESHOLD_MODE_EXACT,
)
from .results import PENDING

if TYPE_CHECKING:
    from pyrung.core.kernel import ReplayKernel

    from . import _ExploreContext
    from .deadness import DeadGuard

_DOWN_KINDS = frozenset((_DONE_KIND_COUNT_DOWN, _PROGRESS_KIND_INT_DOWN, _PROGRESS_KIND_REAL_DOWN))
_TIMER_KINDS = frozenset((_DONE_KIND_ON_DELAY, _DONE_KIND_OFF_DELAY, _DONE_KIND_TIME_DRUM))
# Kinds whose normalized progress is the raw accumulator value.
_PLAIN_KINDS = frozenset((_DONE_KIND_COUNT_UP, _PROGRESS_KIND_INT_UP, _PROGRESS_KIND_REAL_UP))

# Longest ancestor chain walked when looking for the start of a cycle, and
# most scans one traversal may span (it is replayed scan by scan).
_MAX_CYCLE_LENGTH = 64
_MAX_CYCLE_SCANS = 100_000


@dataclass(frozen=True, slots=True)
class _VectorAtom:
    threshold: int | float | str
    form: str
    exact: bool


@dataclass(frozen=True, slots=True)
class _ProgressSource:
    """One hidden accumulator and the key slots that show its thresholds."""

    acc_name: str
    kind: str
    # Timer units added per scan (timers only; see events._exact_timer_move).
    quantum: float | None
    # (state-key index of the Done slot, preset, preset memory key)
    done_slots: tuple[tuple[int, int | str, str | None], ...]
    # (state-key index of the threshold vector, its atoms)
    vector_slots: tuple[tuple[int, tuple[_VectorAtom, ...]], ...]


@dataclass(frozen=True, slots=True)
class _Pump:
    """A pumped revisit: advanced by ``count`` more traversals of a cycle."""

    count: int
    values: tuple[Any, ...]
    # Progress one traversal adds, per accumulator index.
    deltas: dict[int, float]
    # Timers: carry-recurrence steps one traversal runs, and the exact
    # ``(acc, frac)`` the pumped node lands on.
    timer_steps: dict[int, int]
    timer_landing: dict[int, tuple[int, float]]


def _hidden_progress_sources(context: _ExploreContext) -> tuple[_ProgressSource, ...]:
    exact = set(context.stateful_names)
    done: dict[str, list[tuple[int, int | str, str | None]]] = {}
    kinds: dict[str, str] = {}
    for spec in context.done_event_specs:
        if spec.acc_name in exact:
            continue
        done.setdefault(spec.acc_name, []).append(
            (spec.state_index, spec.preset, spec.preset_memory_key)
        )
        kinds.setdefault(spec.acc_name, spec.kind)
    vectors: dict[str, list[tuple[int, tuple[_VectorAtom, ...]]]] = {}
    offset = len(context.stateful_names)
    for index, vector in enumerate(context.threshold_vector_specs):
        if vector.acc_name in exact or vector.kind == _THRESHOLD_KIND_COMPARISON_ONLY:
            continue
        atoms = tuple(
            _VectorAtom(atom.threshold, atom.form, atom.mode == _THRESHOLD_MODE_EXACT)
            for atom in vector.atoms
        )
        vectors.setdefault(vector.acc_name, []).append((offset + index, atoms))
        kinds.setdefault(vector.acc_name, vector.kind)
    return tuple(
        _ProgressSource(
            acc_name=name,
            kind=kinds[name],
            quantum=context.timer_quanta.get(name) if kinds[name] in _TIMER_KINDS else None,
            done_slots=tuple(done.get(name, ())),
            vector_slots=tuple(vectors.get(name, ())),
        )
        for name in sorted(kinds)
    )


def _is_pending(source: _ProgressSource, key: tuple[Any, ...]) -> bool:
    for slot, _preset, _memory_key in source.done_slots:
        if key[slot] == PENDING:
            return True
    for slot, _atoms in source.vector_slots:
        if not all(key[slot]):
            return True
    return False


def _progress_value(kernel: ReplayKernel, source: _ProgressSource) -> float:
    """Normalized progress: grows toward every threshold for every kind."""
    raw = kernel.tags.get(source.acc_name, 0)
    value: float = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
    if source.kind in _TIMER_KINDS:
        frac = kernel.memory.get(f"_frac:{source.acc_name}", 0.0) or 0.0
        # Rounded so float drift in the fraction is not a new value.
        return round(value + float(frac), 9)
    if source.kind in _DOWN_KINDS:
        return -value
    return value


def _set_progress(kernel: ReplayKernel, source: _ProgressSource, value: float) -> None:
    if source.kind in _TIMER_KINDS:
        whole = math.floor(value)
        kernel.tags[source.acc_name] = int(whole)
        frac_key = f"_frac:{source.acc_name}"
        if frac_key in kernel.memory or value != whole:
            kernel.memory[frac_key] = value - whole
        return
    if source.kind in _DOWN_KINDS:
        value = -value
    current = kernel.tags.get(source.acc_name)
    kernel.tags[source.acc_name] = int(value) if isinstance(current, int) else value


def _uncrossed_limits(
    kernel: ReplayKernel,
    source: _ProgressSource,
    key: tuple[Any, ...],
) -> list[tuple[float, bool]] | None:
    """Normalized thresholds still ahead at *key*: ``(threshold, inclusive)``.

    ``inclusive`` means the value may reach the threshold and stay uncrossed
    (``gt``).  None when a threshold cannot be resolved here.
    """
    from .events import _resolve_done_preset

    limits: list[tuple[float, bool]] = []
    negate = source.kind in _DOWN_KINDS
    for slot, preset, memory_key in source.done_slots:
        if key[slot] != PENDING:
            continue
        if source.kind == _DONE_KIND_TIME_DRUM:
            return None
        resolved = _resolve_done_preset(preset, memory_key, kernel)
        if resolved is None:
            return None
        # A down counter is done at ``Acc <= preset``: progress ``-Acc`` reaching
        # ``-preset``.  Every other Done kind crosses at progress ``preset``.
        limit = -float(resolved) if source.kind == _DONE_KIND_COUNT_DOWN else float(resolved)
        limits.append((limit, False))
    for slot, atoms in source.vector_slots:
        for atom, crossed in zip(atoms, key[slot], strict=True):
            if crossed:
                continue
            if not atom.exact:
                return None
            threshold = (
                kernel.tags.get(atom.threshold)
                if isinstance(atom.threshold, str)
                else atom.threshold
            )
            if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
                return None
            limits.append((-threshold if negate else threshold, atom.form == _THRESHOLD_FORM_GT))
    return limits


def _max_repeats(value: float, delta: float, limits: list[tuple[float, bool]]) -> int:
    """Most extra traversals that keep ``value + j * delta`` below every limit."""
    best: int | None = None
    for threshold, inclusive in limits:
        room = threshold - value
        if inclusive:
            repeats = math.floor(room / delta)
            while repeats > 0 and value + repeats * delta > threshold:
                repeats -= 1
        else:
            repeats = math.ceil(room / delta) - 1
            while repeats > 0 and value + repeats * delta >= threshold:
                repeats -= 1
        repeats = max(repeats, 0)
        best = repeats if best is None else min(best, repeats)
    return best or 0


# Timer progress carries a float fraction: advances and scan counts are
# compared at this precision (``_progress_value`` rounds the same way).
_RATE_DIGITS = 9
_STEP_TOLERANCE = 1e-6


def _round_rate(delta: Any) -> float:
    return round(float(delta), _RATE_DIGITS)


def _stutters_to(
    kept: tuple[Any, ...],
    hidden: tuple[Any, ...],
    timelike: Any,
    rates: dict[int, float | None],
) -> bool:
    """Whether one number of held scans takes *kept* to *hidden* on every time-like slot."""
    steps: int | None = None
    for i in timelike:
        a, b = kept[i], hidden[i]
        if a is None or b is None:
            if a is not b:
                return False
            continue
        if a == b:
            needed = 0
        else:
            rate = rates.get(i)
            if rate is None or rate <= 0:
                return False
            exact = (b - a) / rate
            needed = round(exact)
            if needed < 0 or abs(exact - needed) > _STEP_TOLERANCE:
                return False
        if steps is None:
            steps = needed
        elif steps != needed:
            return False
    return True


def _max_timer_repeats(
    kernel: ReplayKernel,
    source: _ProgressSource,
    steps: int,
    limits: list[tuple[float, bool]],
) -> int | None:
    """Most extra traversals of *steps* timer steps each that keep every limit uncrossed.

    On the timer's exact carry recurrence: a timer threshold reads the
    integer accumulator, so ``(threshold, inclusive)`` is crossed from the
    least integer past it.  None when the recurrence cannot be searched.
    """
    from .events import _TIMER_ACC_MAX, _timer_reading, _timer_scans_to_reach

    if steps <= 0:
        return None
    assert source.quantum is not None
    acc, frac = _timer_reading(kernel, source.acc_name)
    best: int | None = None
    for threshold, inclusive in limits:
        target = math.floor(threshold) + 1 if inclusive else math.ceil(threshold)
        if target > _TIMER_ACC_MAX:
            continue
        scans = _timer_scans_to_reach(acc, frac, source.quantum, target)
        if scans is None:
            return None
        repeats = (scans - 1) // steps
        best = repeats if best is None else min(best, repeats)
    return 0 if best is None else best


class _CycleTracker:
    """Per-search bookkeeping for revisits that carry new hidden progress."""

    __slots__ = (
        "_hidden",
        "_parent",
        "_scans",
        "_seen",
        "_step",
        "_sources",
        "_timelike",
        "_rates",
        "_timer_seeded",
        "_pumped",
        "_probed",
        "_readers",
        "_dead",
        "refined_nodes",
    )

    def __init__(self, context: _ExploreContext) -> None:
        self._sources = _hidden_progress_sources(context)
        self._readers = tuple(
            (i, source.acc_name, source.kind in _PLAIN_KINDS)
            for i, source in enumerate(self._sources)
        )
        # (state-key index of the guard tag, guard) per source, for a counter
        # whose progress is dead where the guard is false (deadness.py).
        slot = {name: i for i, name in enumerate(context.stateful_names)}
        dead: list[tuple[int, DeadGuard] | None] = []
        for source in self._sources:
            guard = context.dead_guards.get(source.acc_name)
            dead.append(
                (slot[guard.tag], guard)
                if guard is not None and source.kind not in _TIMER_KINDS and guard.tag in slot
                else None
            )
        self._dead = tuple(dead) if any(dead) else None
        self._hidden: dict[Any, tuple[Any, ...] | None] = {}
        self._parent: dict[Any, Any] = {}
        self._scans: dict[Any, int] = {}
        self._step: dict[Any, tuple[dict[str, Any], dict[str, Any]]] = {}
        # Nodes kept at each key: the time-like set they are grouped under,
        # and the hidden vectors by projection (see ``covered``).
        self._seen: dict[
            Any, tuple[frozenset[int], dict[tuple[Any, ...], list[tuple[Any, ...]]]]
        ] = {}
        self._timelike: dict[Any, set[int]] = {}
        # Per-scan advance of each time-like accumulator at the key, or None
        # when two probes there measured different advances.
        self._rates: dict[Any, dict[int, float | None]] = {}
        self._probed: set[tuple[Any, ...]] = set()
        self._timer_seeded: set[Any] = set()
        self._pumped: set[Any] = set()
        self.refined_nodes = 0

    @property
    def active(self) -> bool:
        return bool(self._sources)

    def counters(self, indices: set[int]) -> set[int]:
        """The counters in *indices*: refined on a fresh pulse even unpumped."""
        return {i for i in indices if self._sources[i].kind not in _TIMER_KINDS}

    def is_pumped(self, tid: Any) -> bool:
        return tid in self._pumped

    def mark_pumped(self, tid: Any) -> None:
        self._pumped.add(tid)

    def timer_seeded(self, base_tid: Any) -> bool:
        return base_tid in self._timer_seeded

    def seed_timer(
        self,
        base_tid: Any,
        kernel: ReplayKernel,
        key: tuple[Any, ...],
        pulse: set[int],
    ) -> bool:
        """Whether a timer revisit at *base_tid* becomes the key's one timer seed.

        A timer advancing around a cycle the BFS tree does not show (the
        revisit does not descend from the key) is kept once per key, when its
        thresholds resolve, so its own descendants close the cycle and pump.
        """
        if base_tid in self._timer_seeded:
            return False
        if self.counters(pulse):
            return False
        for i in pulse:
            if not _uncrossed_limits(kernel, self._sources[i], key):
                return False
        self._timer_seeded.add(base_tid)
        return True

    def covered(self, base_tid: Any, base_hidden: tuple[Any, ...], hidden: tuple[Any, ...]) -> bool:
        """Whether a node kept at the key reaches *hidden* by stuttering there.

        Every counter equal, and one number of held-input scans taking every
        time-like accumulator from the node's value to *hidden*'s: held inputs
        keep the key and advance them all together, so the kept node gets
        there.  One *behind* on a time-like accumulator is not covered: it has
        more time left before a threshold.
        """
        timelike = self._timelike.get(base_tid, ())
        projection = self._projection(timelike, hidden)
        if base_tid in self._seen:
            candidates = self._kept(base_tid, base_hidden).get(projection, ())
        elif projection == self._projection(timelike, base_hidden):
            candidates = (base_hidden,)
        else:
            return False
        rates = self._rates.get(base_tid, {})
        for node in candidates:
            if _stutters_to(node, hidden, timelike, rates):
                return True
        return False

    def ahead(
        self, base_hidden: tuple[Any, ...], hidden: tuple[Any, ...], indices: set[int]
    ) -> bool:
        return all(base_hidden[i] is None or hidden[i] >= base_hidden[i] for i in indices)

    @staticmethod
    def _projection(timelike: Any, hidden: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(None if i in timelike else value for i, value in enumerate(hidden))

    def hidden_vector(self, kernel: ReplayKernel, key: tuple[Any, ...]) -> tuple[Any, ...] | None:
        values: list[Any] = []
        any_pending = False
        dead = self._dead
        for i, source in enumerate(self._sources):
            if dead is not None and (entry := dead[i]) is not None and entry[1].dead(key[entry[0]]):
                # Never read before it is overwritten from here: settled.
                values.append(None)
                continue
            if _is_pending(source, key):
                any_pending = True
                values.append(_progress_value(kernel, source))
            else:
                values.append(None)
        return tuple(values) if any_pending else None

    def changed_from(self, kernel: ReplayKernel, base: tuple[Any, ...]) -> tuple[Any, ...] | None:
        """Hidden vector at a revisit of *base*'s key, or None when it equals *base*.

        The pending pattern follows from the key, so only *base*'s pending
        accumulators are read.  This runs on every revisit: the common equal
        case reads each accumulator once and allocates nothing.
        """
        tags = kernel.tags
        for i, name, plain in self._readers:
            expected = base[i]
            if expected is None:
                continue
            if plain:
                if tags.get(name, 0) == expected:
                    continue
            elif _progress_value(kernel, self._sources[i]) == expected:
                continue
            return tuple(
                None if pattern is None else _progress_value(kernel, source)
                for source, pattern in zip(self._sources, base, strict=True)
            )
        return None

    def record_node(
        self,
        tid: Any,
        parent_tid: Any,
        scans: int,
        hidden: tuple[Any, ...] | None,
        step: tuple[dict[str, Any], dict[str, Any]] = ({}, {}),
    ) -> None:
        self._hidden[tid] = hidden
        self._parent[tid] = parent_tid
        self._scans[tid] = scans
        self._step[tid] = step

    def node_hidden(self, tid: Any) -> tuple[Any, ...] | None:
        return self._hidden.get(tid)

    @property
    def hidden_by_node(self) -> dict[Any, tuple[Any, ...] | None]:
        """The node -> hidden vector map, for the BFS's cheap revisit filter."""
        return self._hidden

    def note_timelike(
        self, base_tid: Any, probe_key: tuple[Any, ...], advanced: dict[int, float | None]
    ) -> None:
        """Record the accumulators one held scan advanced at the key, and by how much."""
        self._probed.add(probe_key)
        if advanced:
            self._timelike.setdefault(base_tid, set()).update(advanced)
            rates = self._rates.setdefault(base_tid, {})
            for i, rate in advanced.items():
                if i in rates and rates[i] != rate:
                    rates[i] = None
                else:
                    rates.setdefault(i, rate)

    def stutter_advance(
        self, before: tuple[Any, ...], after: tuple[Any, ...] | None
    ) -> dict[int, float | None]:
        """Accumulators that grew over one held scan, each with its advance.

        None for one that grew from nothing: its advance is unknown.
        """
        if after is None:
            return {}
        return {
            i: None if before[i] is None else _round_rate(after[i] - before[i])
            for i in self.advanced(before, after)
        }

    def probed(self, probe_key: tuple[Any, ...]) -> bool:
        return probe_key in self._probed

    def differing(self, base: tuple[Any, ...], hidden: tuple[Any, ...]) -> set[int]:
        return {
            i for i, (a, b) in enumerate(zip(base, hidden, strict=True)) if a != b and b is not None
        }

    def timelike(self, base_tid: Any) -> set[int]:
        return self._timelike.get(base_tid, set())

    def _kept(
        self, base_tid: Any, base_hidden: tuple[Any, ...]
    ) -> dict[tuple[Any, ...], list[tuple[Any, ...]]]:
        """Nodes kept at the key, by projection under its current time-like set."""
        timelike = frozenset(self._timelike.get(base_tid, ()))
        entry = self._seen.get(base_tid)
        if entry is None:
            kept = {self._projection(timelike, base_hidden): [base_hidden]}
            self._seen[base_tid] = (timelike, kept)
            return kept
        known, kept = entry
        if known != timelike:
            regrouped: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
            for nodes in kept.values():
                for node in nodes:
                    regrouped.setdefault(self._projection(timelike, node), []).append(node)
            self._seen[base_tid] = (timelike, regrouped)
            kept = regrouped
        return kept

    def claim(self, base_tid: Any, base_hidden: tuple[Any, ...], hidden: tuple[Any, ...]) -> bool:
        """Record a refined node with *hidden* at *base_tid*; False if covered."""
        if self.covered(base_tid, base_hidden, hidden):
            return False
        timelike = self._timelike.get(base_tid, ())
        self._kept(base_tid, base_hidden).setdefault(self._projection(timelike, hidden), []).append(
            hidden
        )
        return True

    def claim_stop(
        self, base_tid: Any, base_hidden: tuple[Any, ...], hidden: tuple[Any, ...]
    ) -> bool:
        """Record a stop-point plateau with *hidden* at *base_tid*; False if already kept.

        Not checked against ``covered``: stuttering reaches a plateau, but
        flips from it are what it is kept for (``events._stop_point_outcome``).
        """
        timelike = self._timelike.get(base_tid, ())
        nodes = self._kept(base_tid, base_hidden).setdefault(self._projection(timelike, hidden), [])
        if hidden in nodes:
            return False
        nodes.append(hidden)
        return True

    def cycle_start(
        self,
        parent_tid: Any,
        base_tid: Any,
        base_of: Any,
        edge_scans: int,
    ) -> list[Any] | None:
        """Ancestor chain from the parent back to a node with *base_tid*.

        Returns ``[parent, ..., start]`` (start last) when every link on the
        way replays by holding its inputs for its scans, else None.  A
        hidden-event jump is such a link: a self-resetting timer's lap
        (``Rung(~T.Done): on_delay(T, 50)``) jumps to its Done on the way.
        """
        if edge_scans < 1:
            return None
        chain: list[Any] = []
        current = parent_tid
        scans = edge_scans
        for _ in range(_MAX_CYCLE_LENGTH):
            if current is None:
                return None
            chain.append(current)
            if base_of(current) == base_tid:
                return chain
            link = self._scans.get(current, 0)
            scans += link
            if link < 1 or scans > _MAX_CYCLE_SCANS:
                return None
            current = self._parent.get(current)
        return None

    def pump(
        self,
        kernel: ReplayKernel,
        key: tuple[Any, ...],
        hidden: tuple[Any, ...],
        chain: list[Any],
        pulse: set[int],
    ) -> _Pump | None:
        """Extra cycle traversals that keep *key* uncrossed, if the cycle is linear.

        Every accumulator the cycle advances is pumped, time-like or not: the
        trace repeats the whole cycle, so a timer running beside the pulses
        advances with them and its thresholds bound the repeats too.
        """
        from .events import _timer_after, _timer_reading

        start = self._hidden.get(chain[-1])
        if start is None:
            return None
        deltas: dict[int, float] = {}
        timer_steps: dict[int, int] = {}
        for i, last in enumerate(hidden):
            if last is None:
                continue
            first = start[i]
            if first is None:
                return None
            delta = last - first
            if delta < 0:
                return None
            if delta == 0:
                continue
            # Linear only when the accumulator never falls back along the
            # cycle (no reset on the way).
            previous = last
            steps = 0
            for tid in chain[:-1]:
                values = self._hidden.get(tid)
                if values is None or values[i] is None or values[i] > previous:
                    return None
                steps += values[i] < previous
                previous = values[i]
            if first > previous:
                return None
            steps += first < previous
            deltas[i] = delta
            if self._sources[i].quantum is not None:
                # One scan runs a timer at most once: each link it grew on
                # is one step of its carry recurrence (checked exactly when
                # the cycle is traversed again).
                timer_steps[i] = steps
        if not (deltas.keys() & pulse):
            return None
        repeats: int | None = None
        for i, delta in deltas.items():
            limits = _uncrossed_limits(kernel, self._sources[i], key)
            if limits is None:
                return None
            if not limits:
                continue
            if i in timer_steps:
                count = _max_timer_repeats(kernel, self._sources[i], timer_steps[i], limits)
                if count is None:
                    return None
            else:
                count = _max_repeats(hidden[i], delta, limits)
            repeats = count if repeats is None else min(repeats, count)
        if repeats is None:
            return None
        if repeats == 0:
            # Already the last uncrossed point of the progression.
            return _Pump(
                count=0, values=hidden, deltas=deltas, timer_steps=timer_steps, timer_landing={}
            )
        values = list(hidden)
        landing: dict[int, tuple[int, float]] = {}
        for i, delta in deltas.items():
            if i in timer_steps:
                source = self._sources[i]
                assert source.quantum is not None
                acc, frac = _timer_reading(kernel, source.acc_name)
                landed = _timer_after(acc, frac, source.quantum, repeats * timer_steps[i])
                if landed is None:
                    return None
                landing[i] = landed
                values[i] = round(landed[0] + landed[1], 9)
            else:
                values[i] = hidden[i] + repeats * delta
        return _Pump(
            count=repeats,
            values=tuple(values),
            deltas=deltas,
            timer_steps=timer_steps,
            timer_landing=landing,
        )

    def repeats_linearly(
        self,
        before: tuple[Any, ...],
        after: tuple[Any, ...] | None,
        deltas: dict[int, float],
    ) -> bool:
        """Whether one more traversal from *before* added *deltas* again.

        A reset inside the cycle can look linear from the cycle's first
        state (it starts at the reset value); traversing once more from the
        revisit tells them apart.
        """
        if after is None:
            return False
        for i, value in enumerate(before):
            if value is None:
                continue
            if after[i] is None:
                return False
            if self._sources[i].quantum is not None:
                # Timers are compared exactly (timer_lap_matches).
                continue
            if abs(after[i] - (value + deltas.get(i, 0))) > 1e-9:
                return False
        return True

    def timer_readings(self, kernel: ReplayKernel) -> tuple[tuple[int, float] | None, ...]:
        from .events import _timer_reading

        return tuple(
            _timer_reading(kernel, source.acc_name) if source.quantum is not None else None
            for source in self._sources
        )

    def timer_lap_matches(
        self,
        before: tuple[tuple[int, float] | None, ...],
        kernel: ReplayKernel,
        timer_steps: dict[int, int],
    ) -> bool:
        """Whether one more traversal ran each timer exactly its steps again.

        A timer the cycle does not advance must be left where it was.
        """
        from .events import _timer_after, _timer_reading

        for i, source in enumerate(self._sources):
            start = before[i]
            if start is None:
                continue
            quantum = source.quantum
            assert quantum is not None
            expected = _timer_after(start[0], start[1], quantum, timer_steps.get(i, 0))
            if expected is None or _timer_reading(kernel, source.acc_name) != expected:
                return False
        return True

    def cycle_steps(self, chain: list[Any]) -> list[tuple[dict[str, Any], int]]:
        """Inputs and scans of the links from the cycle's start onward."""
        return [(self._step[tid][0], self._scans[tid]) for tid in reversed(chain[:-1])]

    def apply(self, kernel: ReplayKernel, pumped: _Pump) -> None:
        for i, (source, value) in enumerate(zip(self._sources, pumped.values, strict=True)):
            landed = pumped.timer_landing.get(i)
            if landed is not None:
                kernel.tags[source.acc_name] = landed[0]
                kernel.memory[f"_frac:{source.acc_name}"] = landed[1]
            elif value is not None:
                _set_progress(kernel, source, value)

    def advanced(
        self,
        before: tuple[Any, ...] | None,
        after: tuple[Any, ...] | None,
        among: set[int] | None = None,
    ) -> set[int]:
        """Accumulators that grew from *before* to *after* (from nothing counts)."""
        if after is None:
            return set()
        indices = range(len(after)) if among is None else among
        return {
            i
            for i in indices
            if after[i] is not None
            and (before is None or before[i] is None or after[i] > before[i])
        }
