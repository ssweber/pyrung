"""Core types, constants, and per-walk state for the corridor walker.

The bottom of the package's dependency graph: tuning constants, the
learning/commitment stores (``NoGoodStore``, ``HoldStore``), the per-walk
``_WalkContext``/``_WalkBudget``, the steer/action value types, and the
loose tag-value equality every module shares (``_values_match``, re-exported
from its neutral home in ``analysis/sp_values.py`` alongside ``_CMP_OPS``
and ``_IDX_CHASE_CAP``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# Shared with prove/ and causal/ via the neutral home (sp_values.py);
# re-exported here so walk modules keep importing them from base.
from pyrung.core.analysis.sp_values import _CMP_OPS as _CMP_OPS
from pyrung.core.analysis.sp_values import _IDX_CHASE_CAP as _IDX_CHASE_CAP
from pyrung.core.analysis.sp_values import _values_match as _values_match

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
_DEPTH_PER_PROGRESS = 2
_MAX_PROGRESS_BONUS = 12
# Serial-clobber recovery: how many oracle-driven re-check rounds to attempt
# after the serial prerequisite walk leaves the governing tag unreachable.
_MAX_RECHECK_ITERS = 3
# Backjump (Stage D4): how many diverged-checkpoint re-entries may chain.
# Each segment re-enters the corridor search from the previous segment's
# deepest node with a fresh node/corridor budget, so long value corridors
# (beyond what one _explore can cover) are walked segment by segment.
_MAX_BACKJUMP_SEGMENTS = 8
# Global walk budget caps: generous enough that any solvable walk stays far
# below them; the agenda loop turns exhaustion into an honest "budget
# exhausted" result instead of an unbounded search.
_MAX_WALK_FORKS = 200_000
_MAX_WALK_SCANS = 5_000_000
# Cap on set-value steers per alphabet.  Wide programs surface dozens of
# in-cone non-Bool ND inputs whose domains multiply into hundreds of steers
# tried at every explore node; relevance-ordered survivors (enabling-named
# inputs and the governing tag keep their full domains) fill the cap first.
_MAX_SET_VALUE_STEERS = 24


def _progress_depth_limit(ctx: Any) -> int:
    """Effective prerequisite depth after committed progress credits."""
    bonus = len(getattr(ctx, "progress_goals", ())) * _DEPTH_PER_PROGRESS
    return _MAX_PREREQ_DEPTH + min(bonus, _MAX_PROGRESS_BONUS)


# ---------------------------------------------------------------------------
# Nogood learning (Phase 4: precondition accumulation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoGoodFact:
    """A scalar or relational blocking fact recorded by recovery."""

    kind: str
    tag: str | None = None
    value: Any = None
    lhs_tag: str | None = None
    operator: str | None = None
    rhs_repr: str | None = None
    rhs_value: Any = None
    tags: tuple[str, ...] = ()

    @classmethod
    def scalar(cls, tag: str, value: Any) -> NoGoodFact:
        return cls(kind="scalar", tag=tag, value=value, tags=(tag,))

    @classmethod
    def relation(
        cls,
        lhs_tag: str,
        operator: str,
        rhs_repr: str,
        rhs_value: Any,
        tags: tuple[str, ...],
    ) -> NoGoodFact:
        normalized = tuple(sorted(set(tags) | {lhs_tag}))
        return cls(
            kind="relation",
            lhs_tag=lhs_tag,
            operator=operator,
            rhs_repr=rhs_repr,
            rhs_value=rhs_value,
            tags=normalized,
        )

    def tag_names(self) -> frozenset[str]:
        if self.kind == "scalar" and self.tag is not None:
            return frozenset({self.tag})
        return frozenset(self.tags)

    def to_entry(self) -> tuple[str, Any] | str:
        if self.kind == "scalar" and self.tag is not None:
            return (self.tag, self.value)
        return f"{self.lhs_tag} {self.operator} {self.rhs_repr} (rhs={self.rhs_value!r})"


def _nogood_fact(item: Any) -> NoGoodFact:
    """Normalize legacy ``(tag, value)`` pairs and rich facts."""
    if isinstance(item, NoGoodFact):
        return item
    tag, value = item
    return NoGoodFact.scalar(tag, value)


@dataclass(frozen=True)
class _NoGood:
    """A recorded dead ordering: transition ``from_value -> to_value`` blocked.

    ``blocking`` is the precise set of cause-named facts at the time of
    failure.  Scalar facts keep the old ``(tag, needed_value)`` shape;
    relation facts preserve numeric comparisons such as ``PV < Lower``.
    """

    from_value: Any
    to_value: Any
    blocking: frozenset[NoGoodFact]


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
        blocking: frozenset[Any],
    ) -> bool:
        """Record a nogood; return whether the store grew (add-only)."""
        facts = frozenset(_nogood_fact(item) for item in blocking)
        ng = _NoGood(from_value, to_value, facts)
        if ng in self._nogoods:
            return False
        self._nogoods.add(ng)
        names: set[str] = set()
        for fact in facts:
            names.update(fact.tag_names())
        self._blocking_names = self._blocking_names | frozenset(names)
        return True

    def is_blocked(
        self,
        from_value: Any,
        to_value: Any,
        blocking: frozenset[Any],
    ) -> bool:
        """Exact-membership query for a proven-dead config."""
        facts = frozenset(_nogood_fact(item) for item in blocking)
        return _NoGood(from_value, to_value, facts) in self._nogoods

    def blocking_tag_names(self) -> frozenset[str]:
        """Union of tag names across all recorded nogoods (projection basis)."""
        return self._blocking_names

    def __len__(self) -> int:
        """Store generation: grows monotonically (add-only) — the spin
        guard's 'has anything been learned since' check."""
        return len(self._nogoods)

    def entries(self) -> tuple[tuple[Any, Any, tuple[Any, ...]], ...]:
        """Recorded nogoods as ``(from, to, sorted blocking)`` (diagnosis feed)."""
        return tuple(
            sorted(
                (
                    (
                        ng.from_value,
                        ng.to_value,
                        tuple(sorted((fact.to_entry() for fact in ng.blocking), key=repr)),
                    )
                    for ng in self._nogoods
                ),
                key=repr,
            )
        )

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
# Learned temporal rules (per-walk evidence and promotion)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """Compact pointer to evidence that produced a learned rule."""

    source: str
    tag: str
    scan: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class AvoidEvent:
    """Relational temporal constraint: avoid *event_tag* while a value holds."""

    event_tag: str
    while_tag: str
    while_value: Any
    stay_context: tuple[tuple[str, Any], ...] = ()
    max_scans: int | None = None
    source: str = "timer-enable"


@dataclass(frozen=True)
class LevelRule:
    """A learned level finding for one tag within a stay context."""

    tag: str
    value: Any
    kind: str  # "need" | "cannot_hold"
    stay_context: tuple[tuple[str, Any], ...] = ()
    avoid_event: AvoidEvent | None = None
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class TemporalRule:
    """A promoted temporal rule payload."""

    kind: str  # v1: "cycle"
    tag: str
    stay_context: tuple[tuple[str, Any], ...]
    constraints: tuple[AvoidEvent, ...]
    replaces: tuple[int, ...] = ()


@dataclass(frozen=True)
class LearnedRule:
    """Lifecycle wrapper for learned rule payloads."""

    id: int
    payload: LevelRule | TemporalRule
    status: str = "active"  # active | superseded
    superseded_by: int | None = None


class RuleStore:
    """Per-walk learned-rule store.

    The store is append-only for evidence and explicit about supersession:
    level findings remain journaled after promotion, but only active temporal
    rules generate recovery candidates.
    """

    def __init__(self) -> None:
        self._rules: list[LearnedRule] = []
        self._next_id = 1

    def add_level(self, rule: LevelRule) -> LearnedRule | None:
        """Record a level finding and promote conflicting Bool levels."""
        active_temporal = self._active_temporal_for(rule.tag, rule.stay_context)
        if active_temporal is not None:
            learned = LearnedRule(
                self._next_id,
                rule,
                status="superseded",
                superseded_by=active_temporal.id,
            )
            self._next_id += 1
            self._rules.append(learned)
            return learned
        for existing in self._rules:
            if (
                existing.status == "active"
                and isinstance(existing.payload, LevelRule)
                and existing.payload == rule
            ):
                return None
        learned = self._append(rule)
        self._promote_conflicts(rule.tag, rule.stay_context)
        return learned

    def active_temporal(self, *, tag: str | None = None) -> tuple[LearnedRule, ...]:
        """Active temporal rules, optionally narrowed to one subject tag."""
        return tuple(
            rule
            for rule in self._rules
            if rule.status == "active"
            and isinstance(rule.payload, TemporalRule)
            and (tag is None or rule.payload.tag == tag)
        )

    def entries(self) -> tuple[LearnedRule, ...]:
        """All learned rules, including superseded level rules."""
        return tuple(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def _active_temporal_for(
        self,
        tag: str,
        stay_context: tuple[tuple[str, Any], ...],
    ) -> LearnedRule | None:
        for rule in self._rules:
            if (
                rule.status == "active"
                and isinstance(rule.payload, TemporalRule)
                and rule.payload.tag == tag
                and rule.payload.stay_context == stay_context
            ):
                return rule
        return None

    def _append(self, payload: LevelRule | TemporalRule) -> LearnedRule:
        rule = LearnedRule(self._next_id, payload)
        self._next_id += 1
        self._rules.append(rule)
        return rule

    def _promote_conflicts(self, tag: str, stay_context: tuple[tuple[str, Any], ...]) -> None:
        active_levels: list[tuple[LearnedRule, LevelRule]] = []
        for rule in self._rules:
            payload = rule.payload
            if (
                rule.status == "active"
                and isinstance(payload, LevelRule)
                and payload.tag == tag
                and payload.stay_context == stay_context
                and isinstance(payload.value, bool)
            ):
                active_levels.append((rule, payload))
        values = {payload.value for _rule, payload in active_levels}
        if values != {False, True}:
            return
        constraints = tuple(
            ev
            for _rule, payload in active_levels
            if payload.avoid_event is not None
            for ev in (payload.avoid_event,)
        )
        if any(
            isinstance(rule.payload, TemporalRule)
            and rule.status == "active"
            and rule.payload.tag == tag
            and rule.payload.stay_context == stay_context
            and rule.payload.kind == "cycle"
            for rule in self._rules
        ):
            return
        temporal = self._append(
            TemporalRule(
                kind="cycle",
                tag=tag,
                stay_context=stay_context,
                constraints=constraints,
                replaces=tuple(rule.id for rule, _payload in active_levels),
            )
        )
        for i, rule in enumerate(self._rules):
            if any(rule == level_rule for level_rule, _payload in active_levels):
                self._rules[i] = LearnedRule(
                    rule.id,
                    rule.payload,
                    status="superseded",
                    superseded_by=temporal.id,
                )


# ---------------------------------------------------------------------------
# Stateful must-stay guards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MustStay:
    """Stateful context that must survive until an ancestor transition lands.

    Unlike :class:`HoldStore`, this is not an external-input causal link.
    It is a temporary state predicate carried by a prerequisite request:
    while solving the child, every ``must`` comparison must still hold
    unless any ``until`` comparison has already landed.  A violation only
    prunes a speculative branch, so the safe direction is a premature
    refusal rather than a wrong plan.
    """

    must: tuple[tuple[str, Any], ...]
    until: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class _StepMonitors:
    """Execution monitors threaded into the one fold seam.

    Today the only monitor is must-stay (ancestor transition context);
    future monitors (deadline race, path-sequence divergence, committed
    compound conjuncts) become additional fields composed here rather
    than new parameters threaded through the call graph.
    """

    must_stay: tuple[_MustStay, ...] = ()

    @property
    def active(self) -> bool:
        """Whether any monitor is engaged (cheap inactive-path short-circuit)."""
        return bool(self.must_stay)

    def landed(self, tags: dict[str, Any]) -> bool:
        """Whether any guarded ancestor transition has landed."""
        return any(
            _values_match(tags.get(tag), value)
            for guard in self.must_stay
            for tag, value in guard.until
        )

    def violation(self, tags: dict[str, Any]) -> tuple[str, Any] | None:
        """Return the first violated must-stay comparison, or ``None``."""
        for guard in self.must_stay:
            if any(_values_match(tags.get(tag), value) for tag, value in guard.until):
                continue
            for tag, value in guard.must:
                if not _values_match(tags.get(tag), value):
                    return (tag, value)
        return None

    def context_protected(
        self,
        ctx: _WalkContext,
        tags: dict[str, Any],
        steer: _Steer,
    ) -> frozenset[str]:
        """Inputs whose implicit release should be skipped under must-stay.

        A pulse normally drops every high external input to create a clean edge.
        While a stateful ancestor must stay true, that global release can break the
        ancestor even though the child steer does not intend to write that input
        (fill's ``HMI_tare`` pulse must keep ``HMI_on`` high).  Preserve current
        high external inputs from implicit releases, but leave intended writes to
        the normal guard path.
        """
        if not self.must_stay:
            return frozenset()
        intended = set(steer.patch) if steer.kind == "multi" and steer.patch else set()
        if steer.input is not None:
            intended.add(steer.input)
        return frozenset(
            name
            for name in set(ctx.ext_inputs) | ctx.edge_ext
            if name not in intended and bool(tags.get(name))
        )

    def with_guard(self, guard: _MustStay) -> _StepMonitors:
        """Add *guard* to the must-stay set; returns ``self`` when already present."""
        if guard in self.must_stay:
            return self
        return _StepMonitors(must_stay=self.must_stay + (guard,))

    def protected_tags(self) -> frozenset[str]:
        """Tags protected by active must-stay guards."""
        return frozenset(tag for guard in self.must_stay for tag, _value in guard.must)


_NO_MONITORS = _StepMonitors()


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
        # Chronological journal of released (divested) holds — the triangle
        # table's divest-point source.  Speculative sections roll it back via
        # snapshot/restore alongside the live holds.
        self._released: list[_Hold] = []

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
        """Divest: drop the hold for *name* (no-op when absent), journaling it."""
        hold = self._holds.pop(name, None)
        if hold is not None:
            self._released.append(hold)

    def protected(self) -> dict[str, Any]:
        """Protected ``{name: value}`` map."""
        return {h.name: h.value for h in self._holds.values()}

    def protected_names(self) -> frozenset[str]:
        return frozenset(self._holds)

    def goal_of(self, name: str) -> tuple[str, Any] | None:
        h = self._holds.get(name)
        return h.goal if h is not None else None

    def released(self) -> tuple[_Hold, ...]:
        """Divested holds in release order (the triangle table's divest rows)."""
        return tuple(self._released)

    def snapshot(self) -> tuple[dict[str, _Hold], int]:
        """Checkpoint for speculative sections (independent-fork trials)."""
        return dict(self._holds), len(self._released)

    def restore(self, snap: tuple[dict[str, _Hold], int]) -> None:
        """Roll back to *snap*, discarding speculative registrations and divests."""
        holds, released_len = snap
        self._holds = dict(holds)
        del self._released[released_len:]

    def filter_conflicting(self, goals: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Drop goals that would flip a protected hold."""
        if not self._holds:
            return goals
        protected = self.protected()
        return [(t, v) for t, v in goals if t not in protected or _values_match(protected[t], v)]

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

    ``max_wall_s`` is the wall-clock knob: ``None`` (default) means no time
    cap; a value makes :attr:`exhausted` trip once that many seconds have
    elapsed since construction — large programs pay real milliseconds per
    fork, so a fork cap alone can leave ``how()`` looking like a hang
    instead of returning an honest NotFound.
    """

    forks: int = 0
    scans: int = 0
    max_forks: int = _MAX_WALK_FORKS
    max_scans: int = _MAX_WALK_SCANS
    max_wall_s: float | None = None
    started: float = field(default_factory=time.monotonic)

    @property
    def exhausted(self) -> bool:
        if self.forks >= self.max_forks or self.scans >= self.max_scans:
            return True
        return self.max_wall_s is not None and time.monotonic() - self.started >= self.max_wall_s

    def describe_exhaustion(self) -> str:
        """Human-readable exhaustion summary for reasons/diagnosis."""
        parts = f"{self.forks} forks, {self.scans} scans"
        if self.max_wall_s is not None and time.monotonic() - self.started >= self.max_wall_s:
            parts += f", wall-clock {time.monotonic() - self.started:.1f}s >= {self.max_wall_s:g}s"
        return f"budget exhausted ({parts})"


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
    rules: RuleStore = field(default_factory=RuleStore)
    nd_domains: dict[str, tuple[Any, ...]] | None = None
    explore_context: Any = None
    atom_index: dict[str, list[Any]] | None = None
    domain_sources: dict[str, str] | None = None
    budget: _WalkBudget = field(default_factory=_WalkBudget)
    probe_memo: dict[str, bool] = field(default_factory=dict)
    # Distinct goals that resolved with committed non-empty work.  The
    # prerequisite depth cap grows from these credits, so productive walks can
    # continue through deep state-machine chains while stalled decompositions
    # keep the base cap.
    progress_goals: set[tuple[str, Any]] = field(default_factory=set)
    # Committed goal values re-checked while deeper agenda frames complete.
    # Unlike the debug diagnostic mirror below, this is always populated so
    # regressions can trigger protective holds in normal walks.
    committed_values: dict[tuple[str, Any], Any] = field(default_factory=dict)
    # Spin guard (findings §2c): goals that failed, keyed by
    # (goal, nogood-projected state), valued with the nogood-store
    # generation at failure.  A re-request of the same goal at the same
    # projected state with an unchanged store cannot succeed — recovery
    # rounds at every level recreate each other's goals (~3^depth re-walks
    # of the same failing subtree) without this.  A pruned re-walk is at
    # worst a premature None (state drift outside the blocking names is
    # not in the key) — the safe direction; never a wrong plan.
    failed_goals: dict[Any, int] = field(default_factory=dict)
    # Never-written reference constants (priors._reference_constants), built
    # once per walk when the ref_constant_order pass is enabled.  Empty set =
    # pass disabled or nothing detected — the consuming sort keys reduce to
    # their pre-pass form, so emptiness alone carries the ablation.
    ref_constants: frozenset[str] = frozenset()
    # Frozen pass-registry advice + per-walk journal (walk/passes.py); None
    # means all advice enabled with no journaling (pre-registry behavior).
    advice: Any = None
    journal: Any = None
    debug_sink: _DebugSink | None = None


# ---------------------------------------------------------------------------
# Debug trace — structured event collector for how(debug=True)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DebugEvent:
    kind: str
    tag: str | None = None
    value: Any = None
    depth: int = 0
    detail: str = ""
    chain_dump: str | None = None
    elapsed_s: float = 0.0


@dataclass
class _DiagnosticState:
    committed_values: dict[tuple[str, Any], Any] = field(default_factory=dict)
    recovery_snapshots: list[_DebugEvent] = field(default_factory=list)


class _DebugSink:
    __slots__ = ("_events", "_t0", "_diag")

    def __init__(self) -> None:
        self._events: list[_DebugEvent] = []
        self._t0: float = time.monotonic()
        self._diag: _DiagnosticState = _DiagnosticState()

    def emit(self, kind: str, **kwargs: Any) -> _DebugEvent:
        kwargs.setdefault("elapsed_s", time.monotonic() - self._t0)
        event = _DebugEvent(kind=kind, **kwargs)
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[_DebugEvent, ...]:
        return tuple(self._events)

    @property
    def diag(self) -> _DiagnosticState:
        return self._diag

    def __str__(self) -> str:
        if not self._events:
            return "(no debug events)"
        lines: list[str] = []
        for ev in self._events:
            ts = f"[{ev.elapsed_s:7.3f}s]"
            tag_part = f" {ev.tag}" if ev.tag else ""
            val_part = f"={ev.value!r}" if ev.value is not None else ""
            depth_part = f" d={ev.depth}" if ev.depth else ""
            lines.append(f"{ts} {ev.kind}{tag_part}{val_part}{depth_part}")
            if ev.detail:
                for dl in ev.detail.split("\n"):
                    lines.append(f"          {dl}")
            if ev.chain_dump:
                for cl in ev.chain_dump.split("\n"):
                    lines.append(f"          {cl}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._events)


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
