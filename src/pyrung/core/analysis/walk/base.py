"""Core types, constants, and per-walk state for the corridor walker.

The bottom of the package's dependency graph: tuning constants, the
learning/commitment stores (``NoGoodStore``, ``HoldStore``), the per-walk
``_WalkContext``/``_WalkBudget``, the steer/action value types, and the
loose tag-value equality every module shares.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.walk.fold import _JumpContext

# The time-advance loop folds productive accumulation to _EMPTY_CAP equivalent
# normal-dt scans (a single jump covers many in one real step).  What differs
# per steer is the *reaction budget*: how long a steer may churn (a visible
# non-accumulator change every scan) before an accumulation plateau forms.  A
# pulse that merely *starts* a dwell settles within a few scans then folds, so
# it needs only a small budget; an inert or oscillating pulse exhausts it and
# bails.  The empty steer's long waits are plateaus, not churn, so it is
# effectively unbounded.  Crucially, once a plateau with an upcoming crossing is
# found the fold runs to _EMPTY_CAP regardless of the budget — productive
# waiting (the dwell a pulse started) is never cut short.
_PULSE_REACT_CAP = 6
_EMPTY_CAP = 20_000
# Guard on real loop iterations (plateaus + reaction scans); bounds oscillators
# that never settle and never cross an accumulator threshold.  Doubles as the
# empty steer's (effectively non-binding) reaction budget.
_MAX_ADVANCE_ITERS = 4_000
# Float tolerance for "is this accumulator advancing" (timers carry a fraction).
_EPS = 1e-9
# Caps on the interpreted value-graph search.
_MAX_NODES = 64
_MAX_CORRIDOR = 40
_MAX_PREREQ_DEPTH = 6
# Serial-clobber recovery: how many oracle-driven re-check rounds to attempt
# after the serial prerequisite walk leaves the governing tag unreachable.
_MAX_RECHECK_ITERS = 3
# Global walk budget caps: generous enough that any solvable walk stays far
# below them; the agenda loop turns exhaustion into an honest "budget
# exhausted" result instead of an unbounded search.
_MAX_WALK_FORKS = 200_000
_MAX_WALK_SCANS = 5_000_000
# Comparison operators shared by the inequality-resolution helpers.
_CMP_OPS: dict[str, Any] = {
    "gt": lambda v, o: v > o,
    "ge": lambda v, o: v >= o,
    "lt": lambda v, o: v < o,
    "le": lambda v, o: v <= o,
}


# ---------------------------------------------------------------------------
# Nogood learning (Phase 4: precondition accumulation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NoGood:
    """A recorded dead ordering: transition ``from_value -> to_value`` blocked.

    ``blocking`` is the *precise assignment* of cause()-named still-unsatisfied
    ``(tag, needed_value)`` pairs at the time of failure.  Precise pairs (not
    bare names) keep two failures of the same transition under different
    assignments distinct.
    """

    from_value: Any
    to_value: Any
    blocking: frozenset[tuple[str, Any]]


class NoGoodStore:
    """Accumulated nogoods for one ``plan_walk`` (precondition learning).

    Add-only over the finite product (gov value) x (gov value) x (powerset of
    finite tag x value pairs in the blocking cone), so growth terminates;
    ``_MAX_RECHECK_ITERS`` remains the hard cap on recovery rounds.

    Two surfaces:

    - The nogood *key* uses precise ``(tag, value)`` pairs (``is_blocked`` /
      ``add`` exact membership) so a proven-dead config bails immediately.
    - The ``_explore`` seen-key projection (:meth:`project`) uses **tag names
      only** (:meth:`blocking_tag_names`), so a re-walk re-enters a governing
      value under different learned constraints.

    Nogoods only prune re-attempts and refine seen-keys — they never assert
    reachability.  A wrong nogood yields at worst a premature ``None`` (the
    safe direction for ``how()``), and ``plan_walk`` independently re-validates
    any returned plan on a fresh fork.
    """

    def __init__(self) -> None:
        self._nogoods: set[_NoGood] = set()
        self._blocking_names: frozenset[str] = frozenset()
        # Telemetry: oracle-driven recovery rounds attempted across the walk.
        self.recovery_iters: int = 0

    def add(
        self,
        from_value: Any,
        to_value: Any,
        blocking: frozenset[tuple[str, Any]],
    ) -> bool:
        """Record a nogood; return whether the store grew (add-only)."""
        ng = _NoGood(from_value, to_value, frozenset(blocking))
        if ng in self._nogoods:
            return False
        self._nogoods.add(ng)
        self._blocking_names = self._blocking_names | {name for name, _v in blocking}
        return True

    def is_blocked(
        self,
        from_value: Any,
        to_value: Any,
        blocking: frozenset[tuple[str, Any]],
    ) -> bool:
        """Exact-membership query for a proven-dead config."""
        return _NoGood(from_value, to_value, frozenset(blocking)) in self._nogoods

    def blocking_tag_names(self) -> frozenset[str]:
        """Union of tag names across all recorded nogoods (projection basis)."""
        return self._blocking_names

    def project(self, snapshot: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
        """Project *snapshot* onto the learned blocking-tag names.

        Returns ``()`` when the store is empty, so an empty store keys
        ``_explore``'s ``seen`` exactly as the bare governing value did before
        (preserving compression / bit-identical behavior).
        """
        if not self._blocking_names:
            return ()
        return tuple(
            sorted((name, snapshot.get(name)) for name in self._blocking_names if name in snapshot)
        )

    def all_orderings_blocked(
        self,
        from_value: Any,
        to_value: Any,
        prereqs: list[tuple[str, Any]],
    ) -> bool:
        """Whether the ``from->to`` transition has any recorded dead ordering.

        Queryable-by-transition hook for :func:`_needs_decomposition` (and
        future Tier-2 force-and-solve).  Matches on the transition alone and
        ignores *prereqs*: the caller's prerequisites come from the static
        SP-tree while nogood keys are cause()-named assignments, so exact
        blocking-set equality would never fire.  Any nogood on the transition
        is direct evidence the prerequisites couple.  Method/query only — no
        force-and-solve.
        """
        del prereqs
        return any(ng.from_value == from_value and ng.to_value == to_value for ng in self._nogoods)


# ---------------------------------------------------------------------------
# Holds (protection intervals): the walker's own external-input commitments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Hold:
    """A protected external-input commitment: *name* must stay at *value*.

    ``goal`` is the committed ``(tag, value)`` goal that depends on the hold —
    the consumer of this causal link.  It labels conflicts/divests and is what
    the Phase-C divest probe re-checks before releasing.
    """

    name: str
    value: Any
    goal: tuple[str, Any]


class HoldStore:
    """Protected external-input holds for one ``plan_walk`` (causal links).

    A hold records the walker's own commitment: an external input that a
    committed sub-walk left at a value its achieved goal depends on.  Holds
    restrict *releases* only — the pulse steer's release prefix skips
    protected names, so a later sub-walk no longer breaks what an earlier one
    established (prevention, where the oracle recovery loop is repair).

    Safety contract (mirrors :class:`NoGoodStore`): holds never assert
    reachability.  An over-broad hold can at worst hide a corridor that
    needed the input released — the divest probe re-opens that corridor
    empirically — and ``plan_walk`` independently re-validates any returned
    plan on a fresh fork.  An empty store leaves every release untouched
    (bit-identical to the pre-holds walker).
    """

    def __init__(self) -> None:
        self._holds: dict[str, _Hold] = {}

    def protect(self, name: str, value: Any, goal: tuple[str, Any]) -> None:
        """Register a hold; the first registration for *name* wins.

        The earlier goal was committed first, so keeping its hold is the
        conservative choice.  A conflicting value is logged as a cross-goal
        coupling signal (future Tier-2 force-and-solve input).
        """
        existing = self._holds.get(name)
        if existing is not None:
            if not _values_match(existing.value, value):
                logger.info(
                    "walk: hold conflict on %s (%r for %s vs %r for %s) — keeping first",
                    name,
                    existing.value,
                    existing.goal[0],
                    value,
                    goal[0],
                )
            return
        self._holds[name] = _Hold(name, value, goal)

    def release(self, name: str) -> None:
        """Divest: drop the hold for *name* (no-op when absent)."""
        self._holds.pop(name, None)

    def protected(self) -> dict[str, Any]:
        """Protected ``{name: value}`` map."""
        return {h.name: h.value for h in self._holds.values()}

    def protected_names(self) -> frozenset[str]:
        return frozenset(self._holds)

    def goal_of(self, name: str) -> tuple[str, Any] | None:
        h = self._holds.get(name)
        return h.goal if h is not None else None

    def snapshot(self) -> dict[str, _Hold]:
        """Checkpoint for speculative sections (independent-fork trials)."""
        return dict(self._holds)

    def restore(self, snap: dict[str, _Hold]) -> None:
        """Roll back to *snap*, discarding speculative registrations."""
        self._holds = dict(snap)

    def __iter__(self) -> Any:
        return iter(self._holds.values())

    def __len__(self) -> int:
        return len(self._holds)


# ---------------------------------------------------------------------------
# Walk context: per-walk-immutable state, built once at walk entry
# ---------------------------------------------------------------------------


@dataclass
class _WalkBudget:
    """Global fork/scan counters for one walk, with caps.

    The agenda loop (:func:`_drive`) checks :attr:`exhausted` before every
    resolver step and unwinds to an honest "budget exhausted" result when a
    cap is hit.  Counts cover the search side (explore trials, divest and
    blocker-clearing probes, recovery, work-fork advancement); the entry,
    verify, and annotate forks in ``plan_walk`` and the ``_probe_steps``
    governance probe are uncounted.  ``scans`` counts folded-equivalent
    scans as recorded in realized actions, not interpreter iterations.
    """

    forks: int = 0
    scans: int = 0
    max_forks: int = _MAX_WALK_FORKS
    max_scans: int = _MAX_WALK_SCANS

    @property
    def exhausted(self) -> bool:
        return self.forks >= self.max_forks or self.scans >= self.max_scans


@dataclass
class _WalkContext:
    """Per-walk-immutable state, built once per walk and passed as one handle.

    Bundles what was previously threaded by hand through eight-plus
    parameters at every recursion site (the dropped-``nogoods`` bug, bitten
    twice, was that fragility's signature failure).  Genuinely per-call
    values — the work fork, the goal, depth, visited, remaining step
    budget — stay explicit parameters.

    ``jump_ctx`` and ``probe_memo`` are the build-once caches.  The jump
    context's whole-program SP-tree scan used to be rebuilt at every
    recursion level x recovery iteration x independent walk; everything in
    it except ``normal_dt``/``profile_fb_names`` is static per program, and
    those two are fixed once the work fork's harness is installed and
    unlinked — so one build at walk entry (after harness setup) is
    equivalent.  ``probe_memo`` caches ``_probe_steps`` per tag.
    """

    pdg: ProgramGraph
    program: Any
    known: dict[str, Any]
    ext_inputs: list[str]
    edge_ext: set[str]
    jump_ctx: _JumpContext
    nogoods: NoGoodStore
    holds: HoldStore | None
    nd_domains: dict[str, tuple[Any, ...]] | None = None
    explore_context: Any = None
    atom_index: dict[str, list[Any]] | None = None
    domain_sources: dict[str, str] | None = None
    budget: _WalkBudget = field(default_factory=_WalkBudget)
    probe_memo: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class _Steer:
    """A candidate move: empty, pulse high, drive low, set, or multi-input."""

    kind: str  # "empty" | "pulse" | "low" | "set" | "multi"
    input: str | None = None
    value: Any = None
    # For "multi" steers: dict of {input_name: value} to apply simultaneously.
    patch: dict[str, Any] | None = None


# A realized action step: ``patch(action)`` then ``scans`` steps.
_Action = tuple[dict[str, Any], int]


def _values_match(a: Any, b: Any) -> bool:
    """Loose equality for tag values (``1 == True``, ``0 == False``)."""
    if a is b:
        return True
    if a == b:
        return True
    return False
