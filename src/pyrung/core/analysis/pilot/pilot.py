"""PILOT loop: trace backward, apply forward, learn from cause() chains.

Acceptance logic uses state-key-based layers (causal momentum) instead of
distance-gated branches.  The state key reuses the prover's projection
(stateful_names + done-bit abstraction + threshold vectors) so accumulator
ticks are absorbed and only structural transitions change the key.

Layers 0-2 gate each candidate action:

  0. Don't Spin — state key must change
     0a. Excursion — key changed then reverted; derive holds, retry
  1. Don't Cycle — new key must not have been visited
  2. Don't Dead-End — frontier must be non-empty or async pending

Layers 3-4 monitor the committed sequence:

  3. Don't Wander — checkpoint on trend improvement
  4. Don't Regress — cause-chain recovery on trend regression

Layer 5 (influence mapping):

  5. Don't Rediscover — observed transitions become known topology
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import Path, ReachabilityStep
from pyrung.core.analysis.pilot.influence import (
    InfluenceMap,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.steers import upstream_candidates
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    TraceChoice,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
    enumerate_trace_choices,
    trace_back,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_MAX_CAUSE_DEPTH = 32


# ---------------------------------------------------------------------------
# Recorded step — intermediate representation before Path construction
# ---------------------------------------------------------------------------


@dataclass
class _Step:
    action: dict[str, Any]
    scan_before: int
    scan_after: int

    @property
    def scans(self) -> int:
        return self.scan_after - self.scan_before


@dataclass(frozen=True)
class PilotGateEvent:
    """Structured result from one candidate acceptance gate."""

    event: str
    detail: str = ""


@dataclass(frozen=True)
class PilotEvent:
    """Structured diagnostic event emitted by :func:`pilot_events`.

    The payload intentionally carries Python objects where useful instead of a
    pre-rendered text log.  Callers can decide how much to display.
    """

    kind: str
    scan: int
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TagChange:
    """A single tag value transition between two snapshots."""

    tag: str
    before: Any
    after: Any


# ---------------------------------------------------------------------------
# Cause-chain walking — recursive root finding (from walk/rules.py pattern)
# ---------------------------------------------------------------------------


def _chase_cause_roots(
    plc: PLC,
    tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None = None,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Chase ``cause()`` chain to steerable-input roots.

    Returns ``(nogoods, holds)`` where:
    - *nogoods*: steerable inputs whose transition caused the regression
    - *holds*: ``(tag, value)`` pairs for inputs that must stay at their
      pre-transition value to prevent the regression
    """
    try:
        chain = plc.cause(tag, scan=scan) if scan is not None else plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot: cause(%s) raised", tag, exc_info=True)
        return set(), []
    if chain is None:
        return set(), []
    return _walk_cause_chain(plc, chain, steerable, set(), 0)


def _walk_cause_chain(
    plc: PLC,
    chain: Any,
    steerable: frozenset[str],
    seen: set[tuple[str, int | None]],
    depth: int,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Recursively walk a CausalChain to steerable-input roots."""
    if depth > _MAX_CAUSE_DEPTH:
        return set(), []

    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        return set(), []
    seen.add(key)

    nogoods: set[str] = set()
    holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()

    def _process_root(root: Any) -> None:
        if root.tag_name in steerable:
            nogoods.add(root.tag_name)
            if root.from_value is not None and not _values_match(root.from_value, root.to_value):
                hold = (root.tag_name, root.from_value)
                if hold not in seen_holds:
                    seen_holds.add(hold)
                    holds.append(hold)
        else:
            sub_ng, sub_holds = _expand_root(plc, root, steerable, seen, depth + 1)
            nogoods.update(sub_ng)
            for h in sub_holds:
                if h not in seen_holds:
                    seen_holds.add(h)
                    holds.append(h)

    for root in chain.conjunctive_roots:
        _process_root(root)
    for root in chain.ambiguous_roots:
        _process_root(root)

    for step in chain.steps:
        for trigger in step.triggers:
            _process_root(trigger)

    return nogoods, holds


def _expand_root(
    plc: PLC,
    transition: Any,
    steerable: frozenset[str],
    seen: set[tuple[str, int | None]],
    depth: int,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Expand a non-steerable root by chasing its cause chain."""
    try:
        sub = (
            plc.cause(transition.tag_name, scan=transition.scan_id)
            if transition.scan_id is not None
            else plc.cause(transition.tag_name)
        )
    except Exception:  # noqa: BLE001
        return set(), []
    if sub is None:
        return set(), []
    return _walk_cause_chain(plc, sub, steerable, seen, depth)


# ---------------------------------------------------------------------------
# Pulse helper — apply actions with edge semantics
# ---------------------------------------------------------------------------


def _apply_pulse(
    plc: PLC,
    actions: list[tuple[str, Any]],
    resting: dict[str, Any],
    edge_tags: set[str],
) -> int:
    """Apply *actions* with rising-edge semantics where needed.

    Returns the number of scans consumed.
    """
    patch = {t: v for t, v in actions}
    needs_edge = any(t in edge_tags for t in patch)

    if needs_edge:
        release = {t: resting.get(t, False) for t in patch if t in edge_tags}
        if release:
            plc.patch(release)
            plc.step()

    plc.patch(patch)
    plc.step()

    # Settle for a few scans so the program processes the input
    for _ in range(4):
        plc.step()

    return 6 if needs_edge else 5


# ---------------------------------------------------------------------------
# Hold installation
# ---------------------------------------------------------------------------


def _install_holds(
    plc: PLC,
    holds: list[tuple[str, Any]],
    forced_holds: dict[str, Any],
) -> None:
    """Force hold inputs on *plc*, skipping already-held ones."""
    for hold_tag, hold_val in holds:
        if hold_tag not in forced_holds:
            forced_holds[hold_tag] = hold_val
            plc.force(hold_tag, hold_val)
            logger.info("pilot: hold %s=%r", hold_tag, hold_val)


# ---------------------------------------------------------------------------
# State key — prover-derived projection for cycle/spin detection
# ---------------------------------------------------------------------------

_THRESHOLD_DOWN_KINDS = frozenset({"count_down", "int_down", "real_down"})
_THRESHOLD_FORM_GT = "gt"


@dataclass(frozen=True)
class _StateKeyConfig:
    """Projection dimensions for the pilot state key.

    When built from the prover's ``_ExploreContext``, ``stateful_names``
    contains every cross-scan tag, ``done_specs`` carries the Done-bit
    three-valued abstraction, ``threshold_vector_specs`` carries
    accumulator crossing vectors, and ``acc_indices`` marks raw
    accumulator positions to mask.

    When the prover pipeline is unavailable, the fallback uses
    ``pivot_tags`` from the trace tree with empty absorption specs.
    """

    stateful_names: tuple[str, ...]
    done_specs: tuple[Any, ...]
    threshold_vector_specs: tuple[Any, ...]
    acc_indices: frozenset[int]


def _threshold_crossed_snap(
    snap: dict[str, Any],
    kind: str,
    acc_name: str,
    threshold: int | float | str,
    form: str,
) -> bool:
    """Threshold-vector bit from a PLC snapshot (mirrors kernel._threshold_crossed)."""
    acc_value = snap.get(acc_name)
    threshold_value = snap.get(threshold) if isinstance(threshold, str) else threshold
    if (
        type(acc_value) is bool
        or type(threshold_value) is bool
        or not isinstance(acc_value, (int, float))
        or not isinstance(threshold_value, (int, float))
    ):
        return False
    if kind in _THRESHOLD_DOWN_KINDS:
        acc_value = -acc_value
        threshold_value = -threshold_value
    if form == _THRESHOLD_FORM_GT:
        return acc_value > threshold_value
    return acc_value >= threshold_value


def _has_pending_effects(fork: PLC) -> bool:
    """True if the fork has pending harness feedback or active analog profiles."""
    harness = getattr(fork, "_harness", None)
    if harness is None:
        return False
    if harness.pending_count > 0:
        return True
    for c in getattr(harness, "_profile_couplings", ()):
        if c.active:
            return True
    return False


def _settle_delayed_effects(
    fork: PLC,
    before_snap: dict[str, Any],
    cfg: _StateKeyConfig | None,
    *,
    scan_budget: int = 2000,
) -> None:
    """Fast-forward *fork* past pending timers and harness feedback.

    Phase 1 — harness feedback: if the harness has scheduled patches
    (Physical on_delay/off_delay), ``run_until(pending_count == 0)``.

    Phase 2 — timer accumulation: if any Timer/Counter done-bit moved
    ``False → PENDING``, ``run_until(~TT, fold=True)`` to skip ticks.
    """
    budget = scan_budget

    # Phase 1: drain pending harness feedback
    harness = getattr(fork, "_harness", None)
    if harness is not None and harness.pending_count > 0:
        scan_before = fork.state.scan_id
        fork.run_until(
            lambda s: harness.pending_count == 0,
            max_cycles=budget,
        )
        budget -= fork.state.scan_id - scan_before

    # Phase 2: fast-forward pending timers via TT bits
    if cfg is not None and cfg.done_specs and budget > 0:
        from pyrung.core.analysis.prove.absorb import _done_acc_state
        from pyrung.core.analysis.prove.results import PENDING

        cur_snap = dict(fork.state.tags)
        pending_tts: list[str] = []
        for spec in cfg.done_specs:
            done_name = cfg.stateful_names[spec.index]
            old = _done_acc_state(
                spec.kind, before_snap.get(done_name), before_snap.get(spec.acc_name)
            )
            new = _done_acc_state(spec.kind, cur_snap.get(done_name), cur_snap.get(spec.acc_name))
            if new == PENDING and old != PENDING:
                tt_name = done_name.rsplit("_Done", 1)[0] + "_TT"
                if cur_snap.get(tt_name) is True:
                    pending_tts.append(tt_name)

        if pending_tts:
            fork.run_until(
                lambda s: all(not s.tags.get(tt) for tt in pending_tts),
                max_cycles=budget,
                fold=True,
            )


def _pilot_state_key(snap: dict[str, Any], cfg: _StateKeyConfig) -> tuple[Any, ...]:
    """Project a PLC snapshot onto the state key dimensions."""
    parts: list[Any] = list(map(snap.get, cfg.stateful_names))
    if cfg.done_specs:
        from pyrung.core.analysis.prove.absorb import _done_acc_state

        for spec in cfg.done_specs:
            parts[spec.index] = _done_acc_state(
                spec.kind, parts[spec.index], snap.get(spec.acc_name)
            )
    for idx in cfg.acc_indices:
        parts[idx] = None
    for spec in cfg.threshold_vector_specs:
        parts.append(
            tuple(
                _threshold_crossed_snap(snap, spec.kind, spec.acc_name, atom.threshold, atom.form)
                for atom in spec.atoms
            )
        )
    return tuple(parts)


# ---------------------------------------------------------------------------
# Layer 2 — excursion detection, diagnosis, and recovery
# ---------------------------------------------------------------------------


def _diagnose_excursion(
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    cfg: _StateKeyConfig,
    steerable: frozenset[str],
) -> tuple[list[str], list[tuple[str, Any]]]:
    """Find reverted state-key dimensions and chase cause to derive holds.

    Called when the post-settle key matches the pre-action key but the
    post-pulse key was different (excursion detected).  The fork's most
    recent transition for each reverted tag IS the revert transition, so
    ``_chase_cause_roots`` traces the right chain.

    In addition to trigger-based holds (from ``_chase_cause_roots``), this
    scans the cause chain's step enablers for steerable inputs.  Holding a
    Bool enabler at its negated value prevents the clearing rung from
    firing on retry.

    Returns ``(reverted_tags, holds)``.
    """
    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        pre_val = pre_snap.get(name)
        pulse_val = post_pulse_snap.get(name)
        if not _values_match(pre_val, pulse_val):
            reverted.append(name)

    all_holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()
    for tag in reverted:
        _, holds = _chase_cause_roots(fork, tag, steerable)
        for h in holds:
            if h not in seen_holds:
                seen_holds.add(h)
                all_holds.append(h)

        # Enabler-based holds: steerable contacts that held the clearing
        # rung open.  Negating a Bool enabler prevents the rung from firing.
        try:
            chain = fork.cause(tag)
        except Exception:  # noqa: BLE001
            continue
        if chain is None:
            continue
        for step in chain.steps:
            for enabler in step.enablers:
                if enabler.tag_name not in steerable:
                    continue
                if not isinstance(enabler.value, bool):
                    continue
                hold = (enabler.tag_name, not enabler.value)
                if hold not in seen_holds:
                    seen_holds.add(hold)
                    all_holds.append(hold)

    return reverted, all_holds


def _attempt_excursion_retry(
    work: PLC,
    action: list[tuple[str, Any]],
    pre_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    excursion_holds: list[tuple[str, Any]],
    forced_holds: dict[str, Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    cfg: _StateKeyConfig,
    scan_budget: int,
) -> PLC | None:
    """Retry *action* with excursion-derived holds installed.

    Returns the retry fork if the state key held (differs from
    *pre_key*), otherwise ``None``.
    """
    retry = work.fork()
    combined: dict[str, Any] = {}
    _install_holds(retry, list(forced_holds.items()), combined)
    _install_holds(retry, excursion_holds, combined)
    _apply_pulse(retry, action, resting, edge_tags)
    _settle_delayed_effects(retry, pre_snap, cfg, scan_budget=scan_budget)
    retry_snap = dict(retry.state.tags)
    retry_key = _pilot_state_key(retry_snap, cfg)
    if retry_key != pre_key:
        return retry
    return None


def _detect_latched_side_effects(
    pre_snap: dict[str, Any],
    post_snap: dict[str, Any],
    cfg: _StateKeyConfig,
) -> dict[str, Any]:
    """Tags outside the state key that changed during an excursion and stuck."""
    key_tags = set(cfg.stateful_names)
    latched: dict[str, Any] = {}
    for tag, new_val in post_snap.items():
        if tag in key_tags:
            continue
        old_val = pre_snap.get(tag)
        if not _values_match(old_val, new_val):
            latched[tag] = new_val
    return latched


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _all_nodes(tree: Any) -> list[Any]:
    """Collect all nodes in a TraceNode tree (breadth-first)."""
    result = [tree]
    i = 0
    while i < len(result):
        result.extend(result[i].children)
        i += 1
    return result


def _commit_step(
    work: PLC,
    fork: PLC,
    action: dict[str, Any],
    scan_before: int,
    steps: list[_Step],
    resting: dict[str, Any],
    edge_tags: set[str],
    live: bool,
) -> PLC:
    """Record a step and swap the work fork (or apply live)."""
    steps.append(
        _Step(
            action=action,
            scan_before=scan_before,
            scan_after=fork.state.scan_id,
        )
    )
    if live:
        _apply_pulse(work, list(action.items()), resting, edge_tags)
        return work
    return fork


_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]
_Checkpoint = tuple[_StateKey, Any, int]
_DebugFn = Callable[[str], None]
_ObserveFn = Callable[[str, dict[str, Any], Any], None]


@dataclass
class _PilotContext:
    target_tag: str
    target_value: Any
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    influence: InfluenceMap
    opaque_loop: frozenset[str]
    choice: TraceChoice | None
    blocked_choice_actions: frozenset[_ActionPair]
    max_scans: int
    live: bool
    debug: bool
    bool_steerable: frozenset[str]
    cmd_cone_cache: dict[str, frozenset[str]]

    def route_allowed(self, pair: _ActionPair) -> bool:
        return pair not in self.blocked_choice_actions

    def cmd_inputs(self, tag: str) -> frozenset[str]:
        cached = self.cmd_cone_cache.get(tag)
        if cached is None:
            cached = frozenset(self.pdg.upstream_slice(tag) & self.bool_steerable)
            self.cmd_cone_cache[tag] = cached
        return cached


@dataclass
class _PilotState:
    work: PLC
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    nogoods: dict[_StateKey, set[_ActionPair]]
    checkpoints: list[_Checkpoint]
    forced_holds: dict[str, Any]
    steps: list[_Step]
    watch_tags: list[str]
    best_trend: int | None = None
    last_wait_log: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class _IterationFrame:
    snap: dict[str, Any]
    tree: Any
    key: _StateKey
    distance_before: int
    raw_trace_actions: tuple[_ActionPair, ...]
    raw_trace_action_details: tuple[TraceAction, ...]


@dataclass(frozen=True)
class _Candidate:
    tag: str
    value: Any
    influence_prescribed: bool = False
    provenance: tuple[str, ...] = ()
    blast_radius: int | None = None

    @property
    def pair(self) -> _ActionPair:
        return (self.tag, self.value)


@dataclass(frozen=True)
class _CandidateList:
    active_trace_actions: tuple[_ActionPair, ...]
    trace_actions: tuple[_ActionPair, ...]
    trace_action_details: tuple[TraceAction, ...]
    influence_candidates: tuple[_ActionPair, ...]
    upstream_candidates: tuple[_ActionPair, ...]
    candidates: tuple[_Candidate, ...]
    blast_cap: int


@dataclass
class _PulseState:
    fork: PLC
    scan_before: int
    post_pulse_snap: dict[str, Any]
    post_pulse_key: _StateKey
    snap: dict[str, Any]
    key: _StateKey


@dataclass(frozen=True)
class _DeadEndResult:
    tree: Any
    trend: int


@dataclass(frozen=True)
class _TrialResult:
    fork: PLC
    scan_before: int
    action: dict[str, Any]
    pulse_actions: tuple[_ActionPair, ...]
    before_snap: dict[str, Any]
    post_pulse_snap: dict[str, Any]
    fork_snap: dict[str, Any]
    observe_label: str
    new_key: _StateKey | None = None
    trend: int | None = None
    regression_nogoods: frozenset[_ActionPair] = frozenset()
    chase_regression_causes: bool = True
    gate_events: tuple[PilotGateEvent, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    trial: _TrialResult | None
    gate_events: tuple[PilotGateEvent, ...] = ()


def _make_pilot_context(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    influence: InfluenceMap | None,
    opaque_loop: frozenset[str],
    choice: TraceChoice | None,
    blocked_choice_actions: frozenset[_ActionPair],
    max_scans: int,
    live: bool,
    debug: bool,
) -> _PilotContext:
    from pyrung.core.tag import TagType as _TagType

    known_tags = plc._known_tags_by_name
    bool_steerable = frozenset(
        t for t in steerable if getattr(known_tags.get(t), "type", None) is _TagType.BOOL
    )
    return _PilotContext(
        target_tag=target_tag,
        target_value=target_value,
        pdg=pdg,
        program=program,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        nd_domains=nd_domains,
        influence=influence or InfluenceMap(),
        opaque_loop=opaque_loop,
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        bool_steerable=bool_steerable,
        cmd_cone_cache={},
    )


def _has_influence_frontier(
    tree: Any,
    snap: dict[str, Any],
    opaque_loop: frozenset[str],
) -> bool:
    """True if *tree* has a dead-end leaf that influence mapping can probe."""
    if not opaque_loop:
        return False
    for n in _all_nodes(tree):
        if n.children or n.satisfied or n.is_steerable:
            continue
        if n.tag in opaque_loop and not _values_match(snap.get(n.tag), n.value):
            return True
    return False


def _ensure_state_key_config(
    state: _PilotState,
    tree: Any,
    target_tag: str,
) -> _StateKeyConfig:
    """Install the trace-tree fallback key config when prover context is absent."""
    if state.key_config is None:
        tree_tags = tree.pivot_tags() | {target_tag}
        tree_tags.update(n.tag for n in tree.leaves() if not n.is_steerable)
        state.key_config = _StateKeyConfig(
            stateful_names=tuple(sorted(tree_tags)),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
    return state.key_config


def _prepare_iteration(
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _IterationFrame:
    snap = dict(state.work.state.tags)
    tree = trace_back(
        ctx.target_tag,
        ctx.target_value,
        snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        choice=ctx.choice,
    )
    key_config = _ensure_state_key_config(state, tree, ctx.target_tag)
    if not state.watch_tags:
        state.watch_tags.extend(sorted(tree.pivot_tags()))
        dbg(f"# watch_tags ({len(state.watch_tags)}): {state.watch_tags[:8]}...")

    key = _pilot_state_key(snap, key_config)
    distance_before = tree.unsatisfied_count()
    action_details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            blast_radius=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
        )
        for action in tree.ordered_action_details()
    )
    if state.best_trend is None:
        state.best_trend = distance_before
        state.seen_keys.add(key)

    return _IterationFrame(
        snap=snap,
        tree=tree,
        key=key,
        distance_before=distance_before,
        raw_trace_actions=tuple(action.pair for action in action_details),
        raw_trace_action_details=action_details,
    )


def _debug_iteration(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> None:
    if not ctx.debug:
        return

    dbg(f"\n{'=' * 60}")
    dbg(f"# ITERATION  scan={state.work.state.scan_id}  distance={frame.distance_before}")
    if state.steps:
        dbg(f"# accomplished ({len(state.steps)}):")
        for si, step in enumerate(state.steps):
            dbg(f"#   [{si}] {step.action}")

    still_need: list[str] = []
    seen_need: set[tuple[str, Any]] = set()
    for n in _all_nodes(frame.tree):
        if not n.satisfied and not n.is_steerable and n.children:
            cur = frame.snap.get(n.tag)
            if cur != n.value:
                nk = (n.tag, repr(n.value))
                if nk not in seen_need:
                    seen_need.add(nk)
                    still_need.append(f"{n.tag}={n.value!r} (have {cur!r})")
    if still_need:
        dbg(f"# still need ({len(still_need)}): {still_need[:10]}")

    dbg(f"# nogoods for key: {sorted(state.nogoods.get(frame.key, set())) or '(none)'}")
    dbg(f"# forced_holds: {dict(state.forced_holds) if state.forced_holds else '(none)'}")
    dbg(f"# seen_keys: {len(state.seen_keys)}  checkpoints: {len(state.checkpoints)}")
    dbg(f"# trace ordered_actions (raw, {len(frame.raw_trace_actions)}):")
    for t, v in frame.raw_trace_actions:
        cur = frame.snap.get(t)
        edge = " [EDGE]" if t in ctx.edge_tags else ""
        ng = " [NOGOOD]" if (t, v) in state.nogoods.get(frame.key, ()) else ""
        already = " [ALREADY]" if _values_match(cur, v) and t not in ctx.edge_tags else ""
        dbg(f"#   {t}={v!r}  (cur={cur!r}){edge}{ng}{already}")


def _build_candidates(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _CandidateList:
    key_nogoods = state.nogoods.get(frame.key, set())
    active_trace_actions = tuple(
        (t, v)
        for t, v in frame.raw_trace_actions
        if (t, v) not in ctx.blocked_choice_actions
        and (not _values_match(frame.snap.get(t), v) or t in ctx.edge_tags)
    )
    trace_actions = tuple(pair for pair in active_trace_actions if pair not in key_nogoods)
    detail_by_pair = {detail.pair: detail for detail in frame.raw_trace_action_details}
    trace_action_details = tuple(
        detail_by_pair[pair] for pair in trace_actions if pair in detail_by_pair
    )

    stuck_tags = {n.tag for n in frame.tree.leaves() if not n.satisfied and not n.is_steerable}
    expanded_probe = stuck_tags | frame.tree.dead_end_parent_tags()
    needed_values: dict[str, Any] = {}
    for n in _all_nodes(frame.tree):
        if n.is_steerable and not n.satisfied and n.tag not in needed_values:
            needed_values[n.tag] = n.value
    up_candidates = tuple(
        upstream_candidates(
            expanded_probe,
            ctx.steerable,
            key_nogoods,
            frame.snap,
            ctx.pdg,
            nd_domains=ctx.nd_domains,
            needed_values=needed_values,
        )
    )

    inf_candidates: list[_ActionPair] = []
    prescribed_input: str | None = None
    if ctx.influence.free_args:
        probed_leaf_states: set[tuple[str, Any]] = set()
        for n in _all_nodes(frame.tree):
            if n.children or n.satisfied or n.is_steerable:
                continue
            cur_val = frame.snap.get(n.tag)
            if _values_match(cur_val, n.value):
                continue
            leaf_state = (n.tag, cur_val)
            if leaf_state in probed_leaf_states:
                continue
            probed_leaf_states.add(leaf_state)

            harmful = ctx.influence.harmful_inputs(n.tag, cur_val, n.value)
            if harmful:
                route_harmful = {h for h in harmful if ctx.route_allowed((h, True))}
                state.nogoods.setdefault(frame.key, set()).update((h, True) for h in route_harmful)
                key_nogoods = state.nogoods.get(frame.key, set())
                if route_harmful:
                    dbg(f"# influence masking harmful for {n.tag}: {sorted(route_harmful)}")

            path = ctx.influence.find_path(n.tag, cur_val, n.value)
            if path:
                first_step = path[0]
                if (first_step, True) not in key_nogoods and ctx.route_allowed((first_step, True)):
                    inf_candidates.append((first_step, True))
                    prescribed_input = first_step
                    dbg(f"# influence path for {n.tag}: {cur_val!r}->{n.value!r} = {path}")
                    break
            else:
                unprobed = sorted(
                    ctx.cmd_inputs(n.tag) - ctx.influence.probed_inputs(n.tag, cur_val)
                )
                new_probes = [
                    inp
                    for inp in unprobed
                    if (inp, True) not in key_nogoods and ctx.route_allowed((inp, True))
                ]
                if new_probes:
                    inf_candidates.extend((inp, True) for inp in new_probes)
                    dbg(f"# influence probing {n.tag} ({cur_val!r}->{n.value!r}): {new_probes}")
                    break

    blast_cap = 20
    if len(trace_actions) > 1:
        radii = {t: len(ctx.pdg.downstream_slice(t, follow_calls=True)) for t, _v in trace_actions}
        median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
        blast_cap = max(median_r * 3, 20)
        trace_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) <= blast_cap)

    candidates: list[_Candidate] = []
    broad: list[_Candidate] = []
    seen_cand: set[_ActionPair] = set()

    def _candidate_for(pair: _ActionPair) -> _Candidate:
        detail = detail_by_pair.get(pair)
        return _Candidate(
            tag=pair[0],
            value=pair[1],
            influence_prescribed=prescribed_input is not None and pair[0] == prescribed_input,
            provenance=detail.provenance if detail is not None else (),
            blast_radius=(
                detail.blast_radius
                if detail is not None and detail.blast_radius is not None
                else len(ctx.pdg.downstream_slice(pair[0], follow_calls=True))
            ),
        )

    for pair in trace_actions:
        if pair not in ctx.blocked_choice_actions and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in [*inf_candidates, *up_candidates]:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidate = _candidate_for(pair)
            if len(ctx.pdg.downstream_slice(pair[0], follow_calls=True)) > blast_cap:
                broad.append(candidate)
            else:
                candidates.append(candidate)
    candidates.extend(broad)

    if ctx.debug:
        dbg(f"# trace_actions (filtered, {len(trace_actions)}): {list(trace_actions)}")
        dbg(f"# upstream_candidates ({len(up_candidates)}): blast_cap={blast_cap}")
        if inf_candidates:
            dbg(f"# influence_candidates ({len(inf_candidates)}): {inf_candidates}")

    return _CandidateList(
        active_trace_actions=active_trace_actions,
        trace_actions=trace_actions,
        trace_action_details=trace_action_details,
        influence_candidates=tuple(inf_candidates),
        upstream_candidates=up_candidates,
        candidates=tuple(candidates),
        blast_cap=blast_cap,
    )


def _pulse_actions(
    actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _PulseState:
    key_config = state.key_config
    assert key_config is not None

    fork = state.work.fork()
    _install_holds(fork, list(state.forced_holds.items()), {})
    scan_before = fork.state.scan_id
    _apply_pulse(fork, list(actions), ctx.resting, ctx.edge_tags)
    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_state_key(post_pulse_snap, key_config)
    _settle_delayed_effects(
        fork,
        frame.snap,
        key_config,
        scan_budget=ctx.max_scans - fork.state.scan_id,
    )
    fork_snap = dict(fork.state.tags)
    return _PulseState(
        fork=fork,
        scan_before=scan_before,
        post_pulse_snap=post_pulse_snap,
        post_pulse_key=post_pulse_key,
        snap=fork_snap,
        key=_pilot_state_key(fork_snap, key_config),
    )


def _record_influence_observations(
    input_tag: str,
    frame: _IterationFrame,
    trial: _PulseState,
    ctx: _PilotContext,
) -> None:
    if input_tag not in ctx.bool_steerable:
        return
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable:
            continue
        old_v = frame.snap.get(n.tag)
        new_v = trial.snap.get(n.tag)
        if old_v != new_v and new_v is not None:
            ctx.influence.record(n.tag, input_tag, old_v, new_v)
        else:
            ctx.influence.record_no_change(n.tag, input_tag, old_v)


def _label_action(action_pairs: tuple[_ActionPair, ...]) -> str:
    if len(action_pairs) == 1:
        t, v = action_pairs[0]
        return f"({t}={v!r})"
    return f"({', '.join(f'{t}={v!r}' for t, v in action_pairs)})"


def _gate_debug(
    dbg: _DebugFn,
    name: str,
    event: str,
    detail: str = "",
    gate_events: list[PilotGateEvent] | None = None,
) -> None:
    if gate_events is not None:
        gate_events.append(PilotGateEvent(event=event.lower(), detail=detail.lstrip(": ")))
    if name.startswith("WIDTH-"):
        dbg(f"# {name}-{event}{detail}")
    else:
        dbg(f"#     {event} {name}{detail}")


def _gate_spin(
    trial: _PulseState,
    action_pairs: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    *,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> _PulseState | None:
    key_config = state.key_config
    assert key_config is not None

    if trial.key != frame.key or _has_pending_effects(trial.fork):
        return trial

    if trial.post_pulse_key != frame.key:
        reverted, exc_holds = _diagnose_excursion(
            trial.fork,
            frame.snap,
            trial.post_pulse_snap,
            key_config,
            ctx.steerable,
        )
        action_tags = {t for t, _ in action_pairs}
        useful_holds = [(h, hv) for h, hv in exc_holds if h not in action_tags]
        if useful_holds:
            retry = _attempt_excursion_retry(
                state.work,
                list(action_pairs),
                frame.snap,
                frame.key,
                useful_holds,
                state.forced_holds,
                ctx.resting,
                ctx.edge_tags,
                key_config,
                ctx.max_scans - state.work.state.scan_id,
            )
            if retry is not None:
                _install_holds(state.work, useful_holds, state.forced_holds)
                retry_snap = dict(retry.state.tags)
                retry_key = _pilot_state_key(retry_snap, key_config)
                _gate_debug(
                    dbg,
                    debug_name,
                    "EXCURSION-RETRY-OK",
                    f": reverted={reverted}, holds={useful_holds}",
                    gate_events,
                )
                return _PulseState(
                    fork=retry,
                    scan_before=trial.scan_before,
                    post_pulse_snap=trial.post_pulse_snap,
                    post_pulse_key=trial.post_pulse_key,
                    snap=retry_snap,
                    key=retry_key,
                )
            _gate_debug(dbg, debug_name, "EXCURSION-RETRY-FAIL", gate_events=gate_events)
            return None

        side_effects = _detect_latched_side_effects(frame.snap, trial.snap, key_config)
        if side_effects:
            _gate_debug(
                dbg,
                debug_name,
                "EXCURSION-SIDE-EFFECTS",
                f": {list(side_effects)[:5]}",
                gate_events,
            )
        _gate_debug(dbg, debug_name, "EXCURSION-NO-HOLDS", gate_events=gate_events)
        return None

    if nogood_pair is not None:
        state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
    _gate_debug(dbg, debug_name, "SPIN", gate_events=gate_events)
    return None


def _gate_cycle(
    trial: _PulseState,
    frame: _IterationFrame,
    state: _PilotState,
    dbg: _DebugFn,
    *,
    pending: bool,
    influence_prescribed: bool,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> bool:
    if trial.key not in state.seen_keys or pending:
        return True
    if not influence_prescribed:
        if nogood_pair is not None:
            state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
        _gate_debug(dbg, debug_name, "CYCLE", gate_events=gate_events)
        return False
    _gate_debug(
        dbg,
        debug_name,
        "INFLUENCE-OVERRIDE-CYCLE",
        ": influence-prescribed",
        gate_events,
    )
    return True


def _gate_dead_end(
    trial: _PulseState,
    action_pairs: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    *,
    influence_prescribed: bool,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> _DeadEndResult | None:
    new_tree = trace_back(
        ctx.target_tag,
        ctx.target_value,
        trial.snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        choice=ctx.choice,
    )
    new_trend = new_tree.unsatisfied_count()
    new_actions = set(new_tree.ordered_actions())
    old_actions = set(frame.tree.ordered_actions())
    action_inputs = set(action_pairs)
    influence_frontier = _has_influence_frontier(new_tree, trial.snap, ctx.opaque_loop)
    pending = _has_pending_effects(trial.fork)

    if not new_actions and not influence_frontier and not pending:
        if not influence_prescribed:
            if nogood_pair is not None:
                state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
            _gate_debug(
                dbg,
                debug_name,
                "DEAD-END",
                ": empty frontier, no pending effects",
                gate_events,
            )
            return None
        _gate_debug(
            dbg,
            debug_name,
            "INFLUENCE-OVERRIDE-DEAD-END",
            ": influence-prescribed",
            gate_events,
        )
    elif (
        new_actions
        and not (new_actions - action_inputs - old_actions)
        and new_trend >= frame.distance_before
    ):
        if not influence_prescribed:
            if nogood_pair is not None:
                state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
            _gate_debug(
                dbg,
                debug_name,
                "LATERAL",
                ": no new frontier, no trend improvement",
                gate_events,
            )
            return None
        _gate_debug(
            dbg,
            debug_name,
            "INFLUENCE-OVERRIDE-LATERAL",
            ": influence-prescribed",
            gate_events,
        )

    return _DeadEndResult(tree=new_tree, trend=new_trend)


def _try_action_batch(
    action_pairs: tuple[_ActionPair, ...],
    pulse_actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    *,
    observe_label: str,
    target_observe_label: str,
    debug_name: str,
    influence_prescribed: bool,
    nogood_pair: _ActionPair | None,
    regression_nogoods: frozenset[_ActionPair],
    chase_regression_causes: bool,
    record_influence_tag: str | None = None,
) -> _AttemptResult:
    gate_events: list[PilotGateEvent] = []
    trial = _pulse_actions(pulse_actions, frame, state, ctx)

    if _values_match(trial.snap.get(ctx.target_tag), ctx.target_value):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_TrialResult(
                fork=trial.fork,
                scan_before=trial.scan_before,
                action=dict(action_pairs),
                pulse_actions=pulse_actions,
                before_snap=frame.snap,
                post_pulse_snap=trial.post_pulse_snap,
                fork_snap=trial.snap,
                observe_label=target_observe_label,
                regression_nogoods=regression_nogoods,
                chase_regression_causes=chase_regression_causes,
                gate_events=tuple(gate_events),
            ),
            gate_events=tuple(gate_events),
        )

    if record_influence_tag is not None:
        _record_influence_observations(record_influence_tag, frame, trial, ctx)

    trial = _gate_spin(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        dbg,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    )
    if trial is None:
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    if _values_match(trial.snap.get(ctx.target_tag), ctx.target_value):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_TrialResult(
                fork=trial.fork,
                scan_before=trial.scan_before,
                action=dict(action_pairs),
                pulse_actions=pulse_actions,
                before_snap=frame.snap,
                post_pulse_snap=trial.post_pulse_snap,
                fork_snap=trial.snap,
                observe_label=target_observe_label,
                regression_nogoods=regression_nogoods,
                chase_regression_causes=chase_regression_causes,
                gate_events=tuple(gate_events),
            ),
            gate_events=tuple(gate_events),
        )

    pending = _has_pending_effects(trial.fork)
    if not _gate_cycle(
        trial,
        frame,
        state,
        dbg,
        pending=pending,
        influence_prescribed=influence_prescribed,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    ):
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    dead_end = _gate_dead_end(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        dbg,
        influence_prescribed=influence_prescribed,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    )
    if dead_end is None:
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    if debug_name.startswith("WIDTH-"):
        dbg(f"# {debug_name}-ACCEPT: distance {frame.distance_before} -> {dead_end.trend}")
    else:
        dbg(f"#     ACCEPT {debug_name}: distance {frame.distance_before} -> {dead_end.trend}")
    gate_events.append(
        PilotGateEvent("accept", f"distance {frame.distance_before} -> {dead_end.trend}")
    )

    return _AttemptResult(
        trial=_TrialResult(
            fork=trial.fork,
            scan_before=trial.scan_before,
            action=dict(action_pairs),
            pulse_actions=pulse_actions,
            before_snap=frame.snap,
            post_pulse_snap=trial.post_pulse_snap,
            fork_snap=trial.snap,
            observe_label=observe_label,
            new_key=trial.key,
            trend=dead_end.trend,
            regression_nogoods=regression_nogoods,
            chase_regression_causes=chase_regression_causes,
            gate_events=tuple(gate_events),
        ),
        gate_events=tuple(gate_events),
    )


def _try_candidate(
    candidate: _Candidate,
    candidates: _CandidateList,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    pair = candidate.pair
    pulse_actions = _candidate_pulse_actions(candidate, candidates, ctx)
    if len(pulse_actions) > 1:
        dbg(f"#     INFLUENCE-CONTEXT: +{len(candidates.trace_actions)} trace actions")

    return _try_action_batch(
        (pair,),
        pulse_actions,
        frame,
        state,
        ctx,
        dbg,
        observe_label="accept",
        target_observe_label="target",
        debug_name=_label_action((pair,)),
        influence_prescribed=candidate.influence_prescribed,
        nogood_pair=pair,
        regression_nogoods=frozenset({pair}),
        chase_regression_causes=True,
        record_influence_tag=candidate.tag,
    )


def _try_widening(
    active_trace_actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    for width in range(2, len(active_trace_actions) + 1):
        batch = active_trace_actions[:width]
        dbg(f"# --- Width {width} ({len(batch)} actions) ---")
        attempt = _try_action_batch(
            batch,
            batch,
            frame,
            state,
            ctx,
            dbg,
            observe_label="width",
            target_observe_label="width-target",
            debug_name=f"WIDTH-{width}",
            influence_prescribed=False,
            nogood_pair=None,
            regression_nogoods=frozenset(batch),
            chase_regression_causes=False,
        )
        if attempt.trial is not None:
            return attempt
    return _AttemptResult(trial=None)


def _commit_trial(
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
    observe: _ObserveFn,
    before: dict[str, Any],
) -> None:
    observe(trial.observe_label, before, trial.fork)
    if trial.new_key is not None:
        state.seen_keys.add(trial.new_key)
    state.work = _commit_step(
        state.work,
        trial.fork,
        trial.action,
        trial.scan_before,
        state.steps,
        ctx.resting,
        ctx.edge_tags,
        ctx.live,
    )


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...]:
    if trial.new_key is None or trial.trend is None:
        return ()

    assert state.best_trend is not None
    if trial.trend < state.best_trend:
        state.checkpoints.append((trial.new_key, state.work.fork(), trial.trend))
        state.best_trend = trial.trend
        dbg(f"#     CHECKPOINT: trend {state.best_trend}")
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                },
            ),
        )

    if trial.trend <= state.best_trend or not state.checkpoints:
        return ()

    dbg(f"#     REGRESSION: trend {state.best_trend} -> {trial.trend}, reverting to checkpoint")
    cause_nogood_pairs: set[_ActionPair] = set()
    cause_holds: list[_ActionPair] = []
    if trial.chase_regression_causes:
        for wt in state.watch_tags:
            if not _values_match(frame.snap.get(wt), trial.fork_snap.get(wt)):
                ng, hl = _chase_cause_roots(state.work, wt, ctx.steerable)
                for ng_tag in ng:
                    cause_nogood_pairs.add((ng_tag, trial.fork_snap.get(ng_tag, True)))
                cause_holds.extend(hl)

        needed_tags = {a for a, _ in frame.tree.ordered_actions()}
        useful_holds = [(ht, hv) for ht, hv in cause_holds if ht not in needed_tags]
        if useful_holds:
            _install_holds(state.work, useful_holds, state.forced_holds)
            for ht, hv in useful_holds:
                dbg(f"#     HOLD {ht}={hv!r} (from cause chain)")

    cp_key, cp_fork, cp_trend = state.checkpoints[-1]
    regression_nogoods = cause_nogood_pairs | set(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods)}")
    state.work = cp_fork.fork()
    _install_holds(state.work, list(state.forced_holds.items()), {})
    state.best_trend = cp_trend
    return (
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": trial.trend,
                "to_trend": cp_trend,
                "checkpoint_key": cp_key,
                "regression_nogoods": frozenset(regression_nogoods),
                "forced_holds": dict(state.forced_holds),
            },
        ),
    )


def _iteration_payload(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> dict[str, Any]:
    still_need: list[str] = []
    seen_need: set[tuple[str, str]] = set()
    for n in _all_nodes(frame.tree):
        if not n.satisfied and not n.is_steerable and n.children:
            cur = frame.snap.get(n.tag)
            if cur != n.value:
                nk = (n.tag, repr(n.value))
                if nk not in seen_need:
                    seen_need.add(nk)
                    still_need.append(f"{n.tag}={n.value!r} (have {cur!r})")

    return {
        "target": (ctx.target_tag, ctx.target_value),
        "snapshot": frame.snap,
        "tree": frame.tree,
        "state_key": frame.key,
        "distance": frame.distance_before,
        "still_need": tuple(still_need),
        "raw_trace_actions": frame.raw_trace_actions,
        "raw_trace_action_details": frame.raw_trace_action_details,
        "nogoods": frozenset(state.nogoods.get(frame.key, set())),
        "forced_holds": dict(state.forced_holds),
        "seen_key_count": len(state.seen_keys),
        "checkpoint_count": len(state.checkpoints),
        "steps": tuple(state.steps),
        "watch_tags": tuple(state.watch_tags),
    }


def _candidate_payload(candidate: _Candidate) -> dict[str, Any]:
    return {
        "tag": candidate.tag,
        "value": candidate.value,
        "pair": candidate.pair,
        "influence_prescribed": candidate.influence_prescribed,
        "provenance": candidate.provenance,
        "blast_radius": candidate.blast_radius,
    }


def _candidate_pulse_actions(
    candidate: _Candidate,
    candidates: _CandidateList,
    ctx: _PilotContext,
) -> tuple[_ActionPair, ...]:
    pair = candidate.pair
    if candidate.tag in ctx.influence.free_args and candidates.trace_actions:
        return (
            pair,
            *((ta, tv) for ta, tv in candidates.trace_actions if ta != candidate.tag),
        )
    return (pair,)


def _context_actions(
    candidate: _Candidate,
    pulse_actions: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    return tuple(pair for pair in pulse_actions if pair != candidate.pair)


def _diff_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    tags: set[str] | frozenset[str] | None = None,
) -> tuple[TagChange, ...]:
    names = sorted(tags if tags is not None else (set(before) | set(after)))
    changes: list[TagChange] = []
    for tag in names:
        old = before.get(tag)
        new = after.get(tag)
        if not _values_match(old, new):
            changes.append(TagChange(tag=tag, before=old, after=new))
    return tuple(changes)


def _accepted_payload(
    candidate: _Candidate,
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
) -> dict[str, Any]:
    watched_tags = set(state.watch_tags)
    action_tags = {tag for tag, _value in trial.pulse_actions}
    target_relevant = set(frame.tree.pivot_tags()) | action_tags
    target_relevant.add(frame.tree.tag)
    changes = {
        "post_pulse": _diff_snapshots(trial.before_snap, trial.post_pulse_snap),
        "settle": _diff_snapshots(trial.post_pulse_snap, trial.fork_snap),
        "total": _diff_snapshots(trial.before_snap, trial.fork_snap),
        "watched": _diff_snapshots(trial.before_snap, trial.fork_snap, tags=watched_tags),
        "target_relevant": _diff_snapshots(
            trial.before_snap,
            trial.fork_snap,
            tags=target_relevant,
        ),
    }
    return {
        "index": None,
        "candidate": _candidate_payload(candidate),
        "action": trial.action,
        "pulse_actions": trial.pulse_actions,
        "context_actions": _context_actions(candidate, trial.pulse_actions),
        "gates": trial.gate_events,
        "accepted_because": {
            "gate_events": trial.gate_events,
            "trend_before": frame.distance_before,
            "trend_after": trial.trend,
            "state_key_changed": trial.new_key is not None and trial.new_key != frame.key,
            "novel_key": trial.new_key is not None and trial.new_key not in state.seen_keys,
            "target_reached": _values_match(
                trial.fork_snap.get(frame.tree.tag),
                frame.tree.value,
            ),
        },
        "changes": changes,
        "snapshots": {
            "before": trial.before_snap,
            "post_pulse": trial.post_pulse_snap,
            "after_settle": trial.fork_snap,
        },
        "new_key": trial.new_key,
        "trend": trial.trend,
        "snapshot": trial.fork_snap,
        "scan_before": trial.scan_before,
        "scan_after": trial.fork.state.scan_id,
    }


def _pilot_loop_events(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    key_config: _StateKeyConfig | None = None,
    influence: InfluenceMap | None = None,
    opaque_loop: frozenset[str] = frozenset(),
    choice: TraceChoice | None = None,
    blocked_choice_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
) -> Iterator[PilotEvent]:
    """Run the PILOT loop as a structured event stream."""
    ctx = _make_pilot_context(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        influence=influence,
        opaque_loop=opaque_loop,
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
    )
    state = _PilotState(
        work=plc,
        key_config=key_config,
        seen_keys=set(),
        nogoods={},
        checkpoints=[],
        forced_holds={},
        steps=[],
        watch_tags=[],
    )

    def _dbg(msg: str) -> None:
        return None

    def _dbg_observe(label: str, before: dict[str, Any], after: PLC) -> None:
        return None

    yield PilotEvent(
        "started",
        state.work.state.scan_id,
        {
            "target": (ctx.target_tag, ctx.target_value),
            "steerable_count": len(ctx.steerable),
            "opaque_loop": ctx.opaque_loop,
            "choice": ctx.choice,
            "blocked_choice_actions": ctx.blocked_choice_actions,
        },
    )

    while state.work.state.scan_id < ctx.max_scans:
        snap = dict(state.work.state.tags)
        if _values_match(snap.get(ctx.target_tag), ctx.target_value):
            if state.steps:
                state.steps[-1] = _Step(
                    action=state.steps[-1].action,
                    scan_before=state.steps[-1].scan_before,
                    scan_after=state.work.state.scan_id,
                )
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": True,
                    "steps": tuple(state.steps),
                    "work": state.work,
                    "reason": "target reached",
                },
            )
            return

        frame = _prepare_iteration(state, ctx, _dbg)
        _debug_iteration(frame, state, ctx, _dbg)
        yield PilotEvent("iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx))
        candidates = _build_candidates(frame, state, ctx, _dbg)
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            {
                "candidate_list": candidates,
                "candidates": tuple(_candidate_payload(c) for c in candidates.candidates),
                "trace_actions": candidates.trace_actions,
                "trace_action_details": candidates.trace_action_details,
                "active_trace_actions": candidates.active_trace_actions,
                "influence_candidates": candidates.influence_candidates,
                "upstream_candidate_count": len(candidates.upstream_candidates),
                "blast_cap": candidates.blast_cap,
            },
        )

        accepted = False
        for ci, candidate in enumerate(candidates.candidates):
            pulse_actions = _candidate_pulse_actions(candidate, candidates, ctx)
            yield PilotEvent(
                "candidate_try",
                state.work.state.scan_id,
                {
                    "index": ci,
                    "total": len(candidates.candidates),
                    "candidate": _candidate_payload(candidate),
                    "pulse_actions": pulse_actions,
                    "context_actions": _context_actions(candidate, pulse_actions),
                },
            )
            attempt = _try_candidate(candidate, candidates, frame, state, ctx, _dbg)
            if attempt.trial is None:
                yield PilotEvent(
                    "candidate_rejected",
                    state.work.state.scan_id,
                    {
                        "index": ci,
                        "candidate": _candidate_payload(candidate),
                        "pulse_actions": pulse_actions,
                        "context_actions": _context_actions(candidate, pulse_actions),
                        "gates": attempt.gate_events,
                    },
                )
                continue
            trial = attempt.trial
            accepted_payload = _accepted_payload(candidate, trial, frame, state)
            accepted_payload["index"] = ci
            yield PilotEvent(
                "candidate_accepted",
                trial.fork.state.scan_id,
                accepted_payload,
            )
            _commit_trial(trial, state, ctx, _dbg_observe, frame.snap)
            yield PilotEvent(
                "trial_committed",
                state.work.state.scan_id,
                {
                    "action": trial.action,
                    "pulse_actions": trial.pulse_actions,
                    "steps": tuple(state.steps),
                    "snapshot": dict(state.work.state.tags),
                },
            )
            yield from _monitor_trend(trial, frame, state, ctx, _dbg)
            accepted = True
            break

        if not accepted and len(candidates.active_trace_actions) >= 2:
            attempt = _try_widening(candidates.active_trace_actions, frame, state, ctx, _dbg)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "widening_accepted",
                    trial.fork.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "gates": trial.gate_events,
                        "new_key": trial.new_key,
                        "trend": trial.trend,
                        "snapshot": trial.fork_snap,
                        "scan_before": trial.scan_before,
                        "scan_after": trial.fork.state.scan_id,
                    },
                )
                _commit_trial(trial, state, ctx, _dbg_observe, frame.snap)
                yield PilotEvent(
                    "trial_committed",
                    state.work.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "steps": tuple(state.steps),
                        "snapshot": dict(state.work.state.tags),
                    },
                )
                yield from _monitor_trend(trial, frame, state, ctx, _dbg)
                accepted = True

        if accepted:
            state.last_wait_log = None
            continue

        before_wait = dict(state.work.state.tags)
        yield PilotEvent(
            "wait",
            state.work.state.scan_id,
            {"snapshot": before_wait, "watch_tags": tuple(state.watch_tags)},
        )
        state.work.step()
        yield PilotEvent(
            "waited",
            state.work.state.scan_id,
            {"before": before_wait, "after": dict(state.work.state.tags)},
        )

    yield PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": _values_match(state.work.state.tags.get(ctx.target_tag), ctx.target_value),
            "steps": tuple(state.steps),
            "work": state.work,
            "reason": "budget exhausted",
        },
    )


def _pilot_loop(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    key_config: _StateKeyConfig | None = None,
    influence: InfluenceMap | None = None,
    opaque_loop: frozenset[str] = frozenset(),
    choice: TraceChoice | None = None,
    blocked_choice_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
) -> tuple[bool, list[_Step], PLC]:
    """Run the PILOT loop and return the final result."""
    final: PilotEvent | None = None
    for event in _pilot_loop_events(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        key_config=key_config,
        influence=influence,
        opaque_loop=opaque_loop,
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
    ):
        if event.kind == "finished":
            final = event

    if final is None:
        return False, [], plc
    return bool(final.data["reached"]), list(final.data["steps"]), final.data["work"]


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


def _build_path(
    reached: bool,
    recorded_steps: list[_Step],
    target_tag: str,
    target_value: Any,
) -> Path:
    """Convert recorded PILOT steps into a ``Path``."""
    if not reached:
        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            reason=f"pilot: {target_tag}={target_value!r} not reached within budget",
        )

    path_steps: list[ReachabilityStep] = []
    for s in recorded_steps:
        path_steps.append(
            ReachabilityStep(
                action=s.action,
                source_key=(s.scan_before,),
                dest_key=(s.scan_after,),
                scans=s.scans,
            )
        )

    return Path(
        reachable=True,
        steps=tuple(path_steps),
        total_changes=sum(len(s.action) for s in recorded_steps),
        total_scans=sum(s.scans for s in recorded_steps),
    )


def _target_is_bool_true(plc: PLC, target_tag: str, target_value: Any) -> bool:
    from pyrung.core.tag import TagType

    tag_obj = plc._known_tags_by_name.get(target_tag)
    return getattr(tag_obj, "type", None) is TagType.BOOL and _values_match(target_value, True)


def _resolve_trace_choice(
    requested: int | str | TraceChoice | None,
    choices: tuple[TraceChoice, ...],
) -> TraceChoice | None:
    if requested is None:
        return None
    if isinstance(requested, TraceChoice):
        return requested
    if isinstance(requested, int):
        idx = requested - 1
        return choices[idx] if 0 <= idx < len(choices) else None
    requested_text = str(requested)
    for option in choices:
        if requested_text == option.id or requested_text == option.label:
            return option
    return None


def _ambiguous_path(
    target_tag: str,
    target_value: Any,
    choices: tuple[TraceChoice, ...],
) -> Path:
    return Path(
        reachable=False,
        steps=(),
        total_changes=0,
        total_scans=0,
        reason=f"pilot: {target_tag}={target_value!r} has multiple Bool output routes",
        choices=choices,
    )


def _exclusive_choice_actions(
    selected: TraceChoice | None,
    choices: tuple[TraceChoice, ...],
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
) -> frozenset[tuple[str, Any]]:
    if selected is None or not choices:
        return frozenset()
    selected_actions = set(
        trace_back(
            target_tag,
            target_value,
            snapshot,
            pdg,
            program,
            steerable,
            opaque_loop=opaque_loop,
            choice=selected,
        ).ordered_actions()
    )
    other_actions: set[tuple[str, Any]] = set()
    for option in choices:
        if option.id == selected.id:
            continue
        other_actions.update(
            trace_back(
                target_tag,
                target_value,
                snapshot,
                pdg,
                program,
                steerable,
                opaque_loop=opaque_loop,
                choice=option,
            ).ordered_actions()
        )
    return frozenset(other_actions - selected_actions)


def _prepare_trace_choice(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    choice: int | str | TraceChoice | None,
) -> tuple[Path | None, TraceChoice | None, frozenset[tuple[str, Any]]]:
    """Resolve an ambiguous Bool-output route choice for an entry point.

    Returns ``(early_path, trace_choice, blocked_choice_actions)``.  When
    *early_path* is not ``None`` the caller returns it immediately — the
    target has multiple output routes and no (or an invalid) choice was given.
    """
    snapshot = dict(plc.state.tags)
    trace_choice: TraceChoice | None = None
    choices: tuple[TraceChoice, ...] = ()
    if _target_is_bool_true(plc, target_tag, target_value) and not _values_match(
        snapshot.get(target_tag), target_value
    ):
        choices = enumerate_trace_choices(target_tag, target_value, snapshot, pdg, program)
        trace_choice = _resolve_trace_choice(choice, choices)
        if choices and choice is None:
            return _ambiguous_path(target_tag, target_value, choices), None, frozenset()
        if choice is not None and trace_choice is None:
            return (
                Path(
                    reachable=False,
                    steps=(),
                    total_changes=0,
                    total_scans=0,
                    reason=f"pilot: invalid choice {choice!r} for {target_tag}={target_value!r}",
                    choices=choices,
                ),
                None,
                frozenset(),
            )
    blocked = _exclusive_choice_actions(
        trace_choice,
        choices,
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        opaque_loop,
    )
    return None, trace_choice, blocked


# ---------------------------------------------------------------------------
# Prover context — nd_domains + state key config
# ---------------------------------------------------------------------------


def _build_pilot_context(
    program: Any,
    snapshot: dict[str, Any],
) -> tuple[dict[str, tuple[Any, ...]] | None, _StateKeyConfig | None]:
    """Build prover context for nd_domains and state key projection.

    Returns ``(nd_domains, key_config)``.  Both are ``None`` on failure —
    pilot falls back to Bool-only probing and pivot-tag state keys.
    """
    try:
        from dataclasses import replace as _replace

        from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.passes import _OptConfig
        from pyrung.core.analysis.prove.results import Intractable

        opt = _replace(_OptConfig(), domains_only=True)
        compiled = _compile_kernel(program, blockless=True, proof_metadata=True)
        ctx = _build_explore_context(
            program,
            _opt_config=opt,
            compiled=compiled,
            initial_state=snapshot,
            allow_partial=True,
        )
        if isinstance(ctx, Intractable):
            return None, None
        nd = getattr(ctx, "nondeterministic_dims", None)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

        # Build state key config from ExploreContext
        stateful_names = ctx.stateful_names
        done_specs = ctx.state_key_done_specs
        threshold_vector_specs = ctx.threshold_vector_specs

        acc_names: set[str] = set()
        for spec in done_specs:
            acc_names.add(spec.acc_name)
        for spec in threshold_vector_specs:
            acc_names.add(spec.acc_name)
        acc_indices = frozenset(i for i, name in enumerate(stateful_names) if name in acc_names)

        if not stateful_names:
            logger.info("pilot: stateful_names empty, falling back to pivot_tags")
            return nd, None

        key_config = _StateKeyConfig(
            stateful_names=stateful_names,
            done_specs=done_specs,
            threshold_vector_specs=threshold_vector_specs,
            acc_indices=acc_indices,
        )
        logger.info(
            "pilot: state key ready (%d dims, %d done, %d threshold, %d acc masked)",
            len(stateful_names),
            len(done_specs),
            len(threshold_vector_specs),
            len(acc_indices),
        )
        return nd, key_config
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return None, None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _parse_target(
    *conditions: Any,
) -> tuple[str, Any]:
    """Extract a single ``(tag_name, target_value)`` from conditions.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison condition (CompareEq)
    """
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")

    cond = conditions[0]

    if isinstance(cond, Tag):
        return cond.name, True

    if isinstance(cond, CompareEq):
        tag = cond.tag
        tag_name = tag.name if isinstance(tag, Tag) else str(tag)
        value = cond.value
        return tag_name, value

    raise ValueError(
        f"pilot: cannot extract (tag, value) from {cond!r}. "
        "Pass a Tag object (for Bool targets) or tag == value."
    )


def pilot_events(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork()
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config = _build_pilot_context(program, dict(fork.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = InfluenceMap(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        fork, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        yield PilotEvent(
            "finished",
            fork.state.scan_id,
            {
                "reached": False,
                "steps": (),
                "work": fork,
                "path": early,
                "reason": early.reason,
                "choices": early.choices,
            },
        )
        return

    yield from _pilot_loop_events(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=False,
        debug=False,
    )


def pilot_how(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
    debug: bool = False,
) -> Path:
    """PILOT on a fork — discover the path, return it. Nothing changes."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork()
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config = _build_pilot_context(program, dict(fork.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = InfluenceMap(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        fork, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        return early

    reached, steps, _work = _pilot_loop(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        debug=debug,
    )

    return _build_path(reached, steps, target_tag, target_value)


def pilot_drive(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
    debug: bool = False,
) -> Path:
    """PILOT on the live PLC — drive the state there."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    pdg = build_program_graph(program)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, plc._known_tags_by_name, pdg, program)
    nd_domains, key_config = _build_pilot_context(program, dict(plc.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = InfluenceMap(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        plc, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        return early

    reached, steps, _work = _pilot_loop(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=True,
        debug=debug,
    )

    return _build_path(reached, steps, target_tag, target_value)
