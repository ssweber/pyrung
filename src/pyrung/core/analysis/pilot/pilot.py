"""PILOT loop: trace backward, apply forward, learn from cause() chains.

Acceptance logic uses state-key-based layers (causal momentum) instead of
distance-gated branches.  The state key reuses the prover's projection
(stateful_names + done-bit abstraction + threshold vectors) so accumulator
ticks are absorbed and only structural transitions change the key.

Layers 0-3 gate each candidate action:

  0. Don't Spin — state key must change (bypass if async effects pending:
     timers timing, harness feedback scheduled, profile couplings active).
  1. Don't Cycle — new key must not have been visited this episode
     (same bypass as Layer 0).
  2. Don't Hallucinate — captures post-pulse state key before settle to
     detect excursions (key changed then reverted).  On excursion: chase
     ``cause()`` on reverted dimensions, derive holds, retry with holds.
     Excursion-causing actions are NOT nogooded — the action works but
     needs a hold to stick.
  3. Don't Dead-End — trace frontier must be non-empty or async effects
     pending.  Empty frontier with no pending effects = pocket.

Layers 4-5 monitor the committed sequence:

  4. Don't Wander — checkpoint on ``unsatisfied_count`` improvement.
     The count is demoted from gatekeeper to trend indicator.
  5. Don't Repeat — on trend regression, chase ``cause()`` roots on
     regressed watch tags, install holds, revert to last checkpoint.

Layer 6 (future): Don't Rediscover — observed transitions become known
topology (slices / influence maps).  Replaces exploration with replay
for previously-seen state-machine corridors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    """Run the PILOT loop with layered acceptance (causal momentum).

    Layers 0-3 gate each candidate action:
      0. Don't Spin  — state key must change
      1. Don't Cycle — new key must be novel
      2. Don't Hallucinate — (settle window; caught by Layer 0 post-settle)
      3. Don't Dead-End — action frontier must grow or trend must improve

    Layers 4-5 monitor the committed sequence:
      4. Don't Wander — checkpoint on trend improvement
      5. Don't Repeat — cause-chain recovery on trend regression
    """
    # --- State ---
    seen_keys: set[tuple[Any, ...]] = set()
    nogoods: dict[tuple[Any, ...], set[tuple[str, Any]]] = {}
    checkpoints: list[tuple[tuple[Any, ...], PLC, int]] = []
    forced_holds: dict[str, Any] = {}
    steps: list[_Step] = []
    work = plc
    watch_tags: list[str] = []
    best_trend: int | None = None
    last_wait_log: tuple[Any, ...] | None = None
    _key_cfg = key_config
    _inf = influence or InfluenceMap()
    _inf_path: list[str] | None = None  # current BFS-prescribed path
    _inf_path_tag: str | None = None

    # Layer 6 probe set: a target's own Bool steerable command cone.  This
    # replaces the opaque-pipeline convergence union (which pulls in alarms,
    # IO faults, analog setpoints, and literal constants while missing real
    # buttons like C_UnitModeChgRequest).  Bool-typing drops INT noise
    # (limits, setpoints, the `True` literal); upstream-scoping drops the
    # alarm/IO inputs that don't actually drive the register.
    from pyrung.core.tag import TagType as _TagType

    _known_tags = plc._known_tags_by_name
    _bool_steerable = frozenset(
        t for t in steerable if getattr(_known_tags.get(t), "type", None) is _TagType.BOOL
    )
    _cmd_cone_cache: dict[str, frozenset[str]] = {}

    def _cmd_inputs(tag: str) -> frozenset[str]:
        c = _cmd_cone_cache.get(tag)
        if c is None:
            c = frozenset(pdg.upstream_slice(tag) & _bool_steerable)
            _cmd_cone_cache[tag] = c
        return c

    def _has_l6_frontier(tree: Any, snap: dict[str, Any]) -> bool:
        """True if *tree* has a dead-end leaf the opaque-loop guard handed
        to Layer 6.

        Those feedback-loop registers (``opaque_loop``) have an empty *trace*
        frontier by construction — the guard cut them to leaves — but L6 can
        still drive them via their command cone.  Layer 3 must count that as
        forward motion, not a pocket, or it rejects every probe into the
        state machine.  Scoped to ``opaque_loop`` so ordinary opaque
        pipelines (whose terminal outputs are not feedback registers) still
        dead-end normally.
        """
        if not opaque_loop:
            return False
        for n in _all_nodes(tree):
            if n.children or n.satisfied or n.is_steerable:
                continue
            if n.tag in opaque_loop and not _values_match(snap.get(n.tag), n.value):
                return True
        return False

    def _dbg(msg: str) -> None:
        if debug:
            print(msg, flush=True)

    def _dbg_observe(label: str, before: dict[str, Any], after: PLC) -> None:
        if not debug:
            return
        after_snap = dict(after.state.tags)
        changes = []
        for gt in watch_tags:
            ov, nv = before.get(gt), after_snap.get(gt)
            if not _values_match(ov, nv):
                changes.append(f"{gt}: {ov!r} -> {nv!r}")
        tv = after_snap.get(target_tag)
        if _values_match(tv, target_value):
            changes.append(f"{target_tag}={tv!r} OK")
        if changes:
            print(f"# {label}: {', '.join(changes)}", flush=True)

    _dbg(f"# pilot({target_tag}={target_value!r})")
    _dbg(f"# steerable: {len(steerable)} inputs")

    while work.state.scan_id < max_scans:
        snap = dict(work.state.tags)

        if _values_match(snap.get(target_tag), target_value):
            _dbg(f"# {target_tag}={target_value!r} OK  (scan {work.state.scan_id})")
            if steps:
                steps[-1] = _Step(
                    action=steps[-1].action,
                    scan_before=steps[-1].scan_before,
                    scan_after=work.state.scan_id,
                )
            return True, steps, work

        # --- Trace backward ---
        tree = trace_back(
            target_tag,
            target_value,
            snap,
            pdg,
            program,
            steerable,
            opaque_loop=opaque_loop,
            choice=choice,
        )

        # Initialize state key config from trace tree if prover unavailable.
        # Include all non-steerable tags (pivots + dead-end leaves like
        # harness-driven Physical tags) so the key captures delayed effects.
        if _key_cfg is None:
            tree_tags = tree.pivot_tags() | {target_tag}
            tree_tags.update(n.tag for n in tree.leaves() if not n.is_steerable)
            _key_cfg = _StateKeyConfig(
                stateful_names=tuple(sorted(tree_tags)),
                done_specs=(),
                threshold_vector_specs=(),
                acc_indices=frozenset(),
            )

        if not watch_tags:
            watch_tags.extend(sorted(tree.pivot_tags()))
            _dbg(f"# watch_tags ({len(watch_tags)}): {watch_tags[:8]}...")

        key = _pilot_state_key(snap, _key_cfg)
        if best_trend is None:
            best_trend = tree.unsatisfied_count()
            seen_keys.add(key)

        distance_before = tree.unsatisfied_count()

        # --- Build candidate list: trace actions first, then upstream cone ---
        trace_actions = tree.ordered_actions()

        if debug:
            _dbg(f"\n{'=' * 60}")
            _dbg(f"# ITERATION  scan={work.state.scan_id}  distance={distance_before}")
            # Show accomplished steps and key state
            if steps:
                _dbg(f"# accomplished ({len(steps)}):")
                for si, s in enumerate(steps):
                    _dbg(f"#   [{si}] {s.action}")
            # Show what the trace still needs (unsatisfied pivots with values)
            still_need = []
            for n in _all_nodes(tree):
                if not n.satisfied and not n.is_steerable and n.children:
                    cur = snap.get(n.tag)
                    if cur != n.value:
                        still_need.append(f"{n.tag}={n.value!r} (have {cur!r})")
            if still_need:
                _dbg(f"# still need ({len(still_need)}): {still_need[:10]}")
            _dbg(f"# nogoods for key: {sorted(nogoods.get(key, set())) or '(none)'}")
            _dbg(f"# forced_holds: {dict(forced_holds) if forced_holds else '(none)'}")
            _dbg(f"# seen_keys: {len(seen_keys)}  checkpoints: {len(checkpoints)}")
            _dbg(f"# trace ordered_actions (raw, {len(trace_actions)}):")
            for t, v in trace_actions:
                cur = snap.get(t)
                edge = " [EDGE]" if t in edge_tags else ""
                ng = " [NOGOOD]" if (t, v) in nogoods.get(key, ()) else ""
                already = " [ALREADY]" if _values_match(cur, v) and t not in edge_tags else ""
                _dbg(f"#   {t}={v!r}  (cur={cur!r}){edge}{ng}{already}")

        key_nogoods = nogoods.get(key, set())
        # Unsatisfied trace actions (for widening — nogoods don't apply to combinations)
        active_trace_actions = [
            (t, v)
            for t, v in trace_actions
            if (t, v) not in blocked_choice_actions
            and (not _values_match(snap.get(t), v) or t in edge_tags)
        ]
        # Individually-viable trace actions (nogood-filtered, for width-1 trial)
        trace_actions = [(t, v) for t, v in active_trace_actions if (t, v) not in key_nogoods]

        stuck_tags = {n.tag for n in tree.leaves() if not n.satisfied and not n.is_steerable}
        dead_parents = tree.dead_end_parent_tags()
        expanded_probe = stuck_tags | dead_parents
        needed_values: dict[str, Any] = {}
        for n in _all_nodes(tree):
            if n.is_steerable and not n.satisfied and n.tag not in needed_values:
                needed_values[n.tag] = n.value
        up_candidates = upstream_candidates(
            expanded_probe,
            steerable,
            key_nogoods,
            snap,
            pdg,
            nd_domains=nd_domains,
            needed_values=needed_values,
        )

        def _route_allowed(pair: tuple[str, Any]) -> bool:
            return pair not in blocked_choice_actions

        # --- Layer 6: influence-map candidates + harmful masking ---
        # Probe dead-end LEAVES — the opaque pipeline outputs the trace
        # can't reach through.  These are the tags (S_StateCurrent,
        # S_UnitModeCurrent, etc.) that L6 builds transition tables for.
        # Probing the leaf directly finds which command buttons change it;
        # probing the parent (o_BurnerLoop) is hopeless — no single input
        # toggles the final output.
        inf_candidates: list[tuple[str, Any]] = []
        _inf_path = None
        _inf_path_tag = None
        if _inf.free_args:
            l6_seen: set[tuple[str, Any]] = set()
            for n in _all_nodes(tree):
                if n.children or n.satisfied or n.is_steerable:
                    continue
                cur_val = snap.get(n.tag)
                if _values_match(cur_val, n.value):
                    continue
                l6_key = (n.tag, cur_val)
                if l6_key in l6_seen:
                    continue
                l6_seen.add(l6_key)

                harmful = _inf.harmful_inputs(n.tag, cur_val, n.value)
                if harmful:
                    route_harmful = {h for h in harmful if _route_allowed((h, True))}
                    nogoods.setdefault(key, set()).update((h, True) for h in route_harmful)
                    key_nogoods = nogoods.get(key, set())
                    if route_harmful:
                        _dbg(f"# L6 masking harmful for {n.tag}: {sorted(route_harmful)}")

                path = _inf.find_path(n.tag, cur_val, n.value)
                if path:
                    first_step = path[0]
                    if (first_step, True) not in key_nogoods and _route_allowed((first_step, True)):
                        inf_candidates.append((first_step, True))
                        _inf_path = path
                        _inf_path_tag = n.tag
                        _dbg(f"# L6 BFS path for {n.tag}: {cur_val!r}->{n.value!r} = {path}")
                        break
                else:
                    cand = _cmd_inputs(n.tag)
                    unprobed = sorted(cand - _inf.probed_inputs(n.tag, cur_val))
                    new_probes = [
                        inp
                        for inp in unprobed
                        if (inp, True) not in key_nogoods and _route_allowed((inp, True))
                    ]
                    if new_probes:
                        for inp in new_probes:
                            inf_candidates.append((inp, True))
                        _dbg(f"# L6 probing {n.tag} ({cur_val!r}->{n.value!r}): {new_probes}")
                        break

        # --- Blast-radius filter ---
        blast_cap = 20
        if len(trace_actions) > 1:
            radii = {t: len(pdg.downstream_slice(t)) for t, _v in trace_actions}
            median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
            blast_cap = max(median_r * 3, 20)
            trace_actions = [(t, v) for t, v in trace_actions if radii.get(t, 0) <= blast_cap]

        if debug:
            _dbg(f"# trace_actions (filtered, {len(trace_actions)}): {trace_actions}")
            _dbg(f"# upstream_candidates ({len(up_candidates)}): blast_cap={blast_cap}")
            if inf_candidates:
                _dbg(f"# influence_candidates ({len(inf_candidates)}): {inf_candidates}")

        # ---- Build candidate list: trace first, then influence + upstream ----
        accepted = False
        seen_cand: set[tuple[str, Any]] = set()
        candidates: list[tuple[str, Any]] = []
        broad: list[tuple[str, Any]] = []
        for t, v in trace_actions:
            pair = (t, v)
            if pair not in blocked_choice_actions and pair not in seen_cand:
                seen_cand.add(pair)
                candidates.append(pair)
        for t, v in [*inf_candidates, *up_candidates]:
            pair = (t, v)
            if _route_allowed(pair) and pair not in seen_cand:
                seen_cand.add(pair)
                if len(pdg.downstream_slice(t)) > blast_cap:
                    broad.append(pair)
                else:
                    candidates.append(pair)
        candidates.extend(broad)

        if debug:
            _dbg(f"# --- Single-candidate fork-check ({len(candidates)}) ---")

        # ---- Per-candidate loop (Layer 0-3) ----
        for ci, (t, v) in enumerate(candidates):
            _dbg(f"#   [{ci + 1}/{len(candidates)}] try {t}={v!r}")
            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            scan_before = fork.state.scan_id
            # L6: opaque pipelines often need trace-known inputs as prerequisites
            if t in _inf.free_args and trace_actions:
                pulse = [(t, v)] + [(ta, tv) for ta, tv in trace_actions if ta != t]
                _dbg(f"#     L6-CONTEXT: +{len(trace_actions)} trace actions")
            else:
                pulse = [(t, v)]
            _apply_pulse(fork, pulse, resting, edge_tags)
            post_pulse_snap = dict(fork.state.tags)
            post_pulse_key = _pilot_state_key(post_pulse_snap, _key_cfg)
            _settle_delayed_effects(
                fork, snap, _key_cfg, scan_budget=max_scans - fork.state.scan_id
            )
            fork_snap = dict(fork.state.tags)

            # Direct target
            if _values_match(fork_snap.get(target_tag), target_value):
                _dbg_observe("target", snap, fork)
                work = _commit_step(
                    work,
                    fork,
                    {t: v},
                    scan_before,
                    steps,
                    resting,
                    edge_tags,
                    live,
                )
                accepted = True
                break

            new_key = _pilot_state_key(fork_snap, _key_cfg)

            # Layer 6: record observations for the influence map.  Record for
            # any Bool steerable command probe (not just opaque-pipeline
            # free_args) so trace-driven button presses also populate the
            # transition table and L6 can BFS sooner.
            inf_prescribed = (
                _inf_path and _inf_path_tag and t == (_inf_path[0] if _inf_path else None)
            )
            if t in _bool_steerable:
                for n in _all_nodes(tree):
                    if n.satisfied or n.is_steerable:
                        continue
                    old_v = snap.get(n.tag)
                    new_v = fork_snap.get(n.tag)
                    if old_v != new_v and new_v is not None:
                        _inf.record(n.tag, t, old_v, new_v)
                    else:
                        _inf.record_no_change(n.tag, t, old_v)

            pending = _has_pending_effects(fork)

            # Layer 0 + Layer 2: Don't Spin / Don't Hallucinate
            if new_key == key and not pending:
                if post_pulse_key != key:
                    # Layer 2: excursion — key changed after pulse but
                    # reverted after settle.  Not a nogood.
                    reverted, exc_holds = _diagnose_excursion(
                        fork,
                        snap,
                        post_pulse_snap,
                        _key_cfg,
                        steerable,
                    )
                    action_tags = {t}
                    useful_holds = [(h, hv) for h, hv in exc_holds if h not in action_tags]
                    if useful_holds:
                        retry = _attempt_excursion_retry(
                            work,
                            [(t, v)],
                            snap,
                            key,
                            useful_holds,
                            forced_holds,
                            resting,
                            edge_tags,
                            _key_cfg,
                            max_scans - work.state.scan_id,
                        )
                        if retry is not None:
                            _install_holds(work, useful_holds, forced_holds)
                            fork = retry
                            fork_snap = dict(fork.state.tags)
                            new_key = _pilot_state_key(fork_snap, _key_cfg)
                            _dbg(
                                f"#     EXCURSION-RETRY-OK ({t}={v!r}): "
                                f"reverted={reverted}, holds={useful_holds}"
                            )
                            # Fall through to Layer 1/3 checks
                        else:
                            _dbg(f"#     EXCURSION-RETRY-FAIL ({t}={v!r})")
                            continue
                    else:
                        side_effects = _detect_latched_side_effects(
                            snap,
                            fork_snap,
                            _key_cfg,
                        )
                        if side_effects:
                            _dbg(
                                f"#     EXCURSION-SIDE-EFFECTS ({t}={v!r}): "
                                f"{list(side_effects)[:5]}"
                            )
                        _dbg(f"#     EXCURSION-NO-HOLDS ({t}={v!r})")
                        continue
                else:
                    nogoods.setdefault(key, set()).add((t, v))
                    _dbg(f"#     SPIN ({t}={v!r})")
                    continue

            # Layer 1: Don't Cycle (unless async effects are pending)
            if new_key in seen_keys and not pending:
                # Layer 6 override: influence-prescribed steps bypass cycle
                # detection — the transition table says this step is needed.
                if not inf_prescribed:
                    nogoods.setdefault(key, set()).add((t, v))
                    _dbg(f"#     CYCLE ({t}={v!r})")
                    continue
                _dbg(f"#     L6-OVERRIDE-CYCLE ({t}={v!r}): influence-prescribed")

            # Layer 3: Don't Dead-End (no actions and no async effects)
            new_tree = trace_back(
                target_tag,
                target_value,
                fork_snap,
                pdg,
                program,
                steerable,
                opaque_loop=opaque_loop,
                choice=choice,
            )
            new_trend = new_tree.unsatisfied_count()
            new_actions = set(new_tree.ordered_actions())
            old_actions = set(tree.ordered_actions())
            l6_frontier = _has_l6_frontier(new_tree, fork_snap)
            if not new_actions and not l6_frontier and not _has_pending_effects(fork):
                # Layer 6 override: influence-prescribed steps bypass dead-end
                if not inf_prescribed:
                    nogoods.setdefault(key, set()).add((t, v))
                    _dbg(f"#     DEAD-END ({t}={v!r}): empty frontier, no pending effects")
                    continue
                _dbg(f"#     L6-OVERRIDE-DEAD-END ({t}={v!r}): influence-prescribed")
            elif (
                new_actions
                and not (new_actions - {(t, v)} - old_actions)
                and new_trend >= distance_before
            ):
                if not inf_prescribed:
                    nogoods.setdefault(key, set()).add((t, v))
                    _dbg(f"#     LATERAL ({t}={v!r}): no new frontier, no trend improvement")
                    continue
                _dbg(f"#     L6-OVERRIDE-LATERAL ({t}={v!r}): influence-prescribed")

            # --- Commit ---
            _dbg(f"#     ACCEPT ({t}={v!r}): distance {distance_before} -> {new_trend}")
            _dbg_observe("accept", snap, fork)
            seen_keys.add(new_key)
            work = _commit_step(
                work,
                fork,
                {t: v},
                scan_before,
                steps,
                resting,
                edge_tags,
                live,
            )
            accepted = True

            # Layer 4: Trend monitoring
            assert best_trend is not None
            if new_trend < best_trend:
                checkpoints.append((new_key, work.fork(), new_trend))
                best_trend = new_trend
                _dbg(f"#     CHECKPOINT: trend {best_trend}")
            elif new_trend > best_trend and checkpoints:
                # Layer 5: Cause-chain regression recovery
                _dbg(
                    f"#     REGRESSION: trend {best_trend} -> {new_trend}, reverting to checkpoint"
                )
                cause_nogood_pairs: set[tuple[str, Any]] = set()
                cause_holds: list[tuple[str, Any]] = []
                for wt in watch_tags:
                    if not _values_match(snap.get(wt), fork_snap.get(wt)):
                        ng, hl = _chase_cause_roots(work, wt, steerable)
                        for ng_tag in ng:
                            cause_nogood_pairs.add((ng_tag, fork_snap.get(ng_tag, True)))
                        cause_holds.extend(hl)
                needed_tags = {a for a, _ in tree.ordered_actions()}
                useful_holds = [(ht, hv) for ht, hv in cause_holds if ht not in needed_tags]
                if useful_holds:
                    _install_holds(work, useful_holds, forced_holds)
                    for ht, hv in useful_holds:
                        _dbg(f"#     HOLD {ht}={hv!r} (from cause chain)")
                cp_key, cp_fork, cp_trend = checkpoints[-1]
                regression_nogoods = cause_nogood_pairs | {(t, v)}
                nogoods.setdefault(cp_key, set()).update(regression_nogoods)
                _dbg(f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods)}")
                work = cp_fork.fork()
                _install_holds(work, list(forced_holds.items()), {})
                best_trend = cp_trend

            break

        if accepted:
            last_wait_log = None
            continue

        # ---- Progressive widening of trace actions (width 2+) ----
        # Use active_trace_actions (not nogood-filtered) — individual nogoods
        # don't apply to combinations.  C_Start alone may regress, but
        # C_ProductionMode + C_Start together may succeed.
        if len(active_trace_actions) >= 2:
            for width in range(2, len(active_trace_actions) + 1):
                batch = active_trace_actions[:width]
                _dbg(f"# --- Width {width} ({len(batch)} actions) ---")
                fork = work.fork()
                _install_holds(fork, list(forced_holds.items()), {})
                scan_before = fork.state.scan_id
                _apply_pulse(fork, batch, resting, edge_tags)
                post_pulse_snap = dict(fork.state.tags)
                post_pulse_key = _pilot_state_key(post_pulse_snap, _key_cfg)
                _settle_delayed_effects(
                    fork, snap, _key_cfg, scan_budget=max_scans - fork.state.scan_id
                )
                fork_snap = dict(fork.state.tags)

                if _values_match(fork_snap.get(target_tag), target_value):
                    _dbg_observe("width-target", snap, fork)
                    work = _commit_step(
                        work,
                        fork,
                        {t: v for t, v in batch},
                        scan_before,
                        steps,
                        resting,
                        edge_tags,
                        live,
                    )
                    accepted = True
                    break

                new_key = _pilot_state_key(fork_snap, _key_cfg)
                key_changed = new_key != key or _has_pending_effects(fork)
                if key_changed and new_key not in seen_keys:
                    batch_tree = trace_back(
                        target_tag,
                        target_value,
                        fork_snap,
                        pdg,
                        program,
                        steerable,
                        opaque_loop=opaque_loop,
                        choice=choice,
                    )
                    batch_trend = batch_tree.unsatisfied_count()
                    new_frontier = set(batch_tree.ordered_actions())
                    batch_inputs = set(batch)
                    l6_frontier = _has_l6_frontier(batch_tree, fork_snap)
                    has_frontier = (
                        bool(new_frontier) or l6_frontier or _has_pending_effects(fork)
                    )
                    if has_frontier and (
                        (new_frontier - batch_inputs - set(tree.ordered_actions()))
                        or batch_trend < distance_before
                        or _has_pending_effects(fork)
                    ):
                        _dbg(f"# WIDTH-{width}-ACCEPT: distance {distance_before} -> {batch_trend}")
                        _dbg_observe("width", snap, fork)
                        seen_keys.add(new_key)
                        work = _commit_step(
                            work,
                            fork,
                            {t: v for t, v in batch},
                            scan_before,
                            steps,
                            resting,
                            edge_tags,
                            live,
                        )
                        accepted = True
                        # Layer 4+5: trend with regression check
                        assert best_trend is not None
                        if batch_trend < best_trend:
                            checkpoints.append((new_key, work.fork(), batch_trend))
                            best_trend = batch_trend
                            _dbg(f"#     CHECKPOINT: trend {best_trend}")
                        elif batch_trend > best_trend and checkpoints:
                            _dbg(
                                f"#     REGRESSION: trend {best_trend} -> {batch_trend},"
                                " reverting to checkpoint"
                            )
                            cp_key, cp_fork, cp_trend = checkpoints[-1]
                            nogoods.setdefault(cp_key, set()).update(batch)
                            _dbg(f"#     REGRESSION-NOGOOD at checkpoint: {sorted(batch)}")
                            work = cp_fork.fork()
                            _install_holds(work, list(forced_holds.items()), {})
                            best_trend = cp_trend
                        break
                    else:
                        _dbg(f"# WIDTH-{width}-DEAD-END")
                elif new_key == key:
                    if post_pulse_key != key:
                        batch_actions = list(batch)
                        reverted, exc_holds = _diagnose_excursion(
                            fork,
                            snap,
                            post_pulse_snap,
                            _key_cfg,
                            steerable,
                        )
                        action_tags = {t for t, _ in batch_actions}
                        useful_holds = [(h, hv) for h, hv in exc_holds if h not in action_tags]
                        if useful_holds:
                            retry = _attempt_excursion_retry(
                                work,
                                batch_actions,
                                snap,
                                key,
                                useful_holds,
                                forced_holds,
                                resting,
                                edge_tags,
                                _key_cfg,
                                max_scans - work.state.scan_id,
                            )
                            if retry is not None:
                                _install_holds(work, useful_holds, forced_holds)
                                retry_snap = dict(retry.state.tags)
                                retry_key = _pilot_state_key(retry_snap, _key_cfg)
                                _dbg(f"# WIDTH-{width}-EXCURSION-RETRY-OK: reverted={reverted}")
                                if _values_match(retry_snap.get(target_tag), target_value):
                                    work = _commit_step(
                                        work,
                                        retry,
                                        {t: v for t, v in batch_actions},
                                        scan_before,
                                        steps,
                                        resting,
                                        edge_tags,
                                        live,
                                    )
                                    accepted = True
                                elif retry_key not in seen_keys:
                                    seen_keys.add(retry_key)
                                    work = _commit_step(
                                        work,
                                        retry,
                                        {t: v for t, v in batch_actions},
                                        scan_before,
                                        steps,
                                        resting,
                                        edge_tags,
                                        live,
                                    )
                                    accepted = True
                                    retry_trend = trace_back(
                                        target_tag,
                                        target_value,
                                        retry_snap,
                                        pdg,
                                        program,
                                        steerable,
                                        opaque_loop=opaque_loop,
                                        choice=choice,
                                    ).unsatisfied_count()
                                    if best_trend is not None and retry_trend < best_trend:
                                        checkpoints.append((retry_key, work.fork(), retry_trend))
                                        best_trend = retry_trend
                                if accepted:
                                    break
                            else:
                                _dbg(f"# WIDTH-{width}-EXCURSION-RETRY-FAIL")
                        else:
                            _dbg(f"# WIDTH-{width}-EXCURSION-NO-HOLDS")
                    else:
                        _dbg(f"# WIDTH-{width}-SPIN")
                else:
                    _dbg(f"# WIDTH-{width}-CYCLE")

        if accepted:
            last_wait_log = None
            continue

        # --- Step forward (timers/SFCs) ---
        if debug:
            wait_key = tuple(snap.get(gt) for gt in watch_tags[:6])
            if wait_key != last_wait_log:
                vals = ", ".join(f"{gt}={snap.get(gt)!r}" for gt in watch_tags[:6])
                print(f"# waiting (scan {work.state.scan_id}) {vals}")
                last_wait_log = wait_key
        work.step()

    _dbg(f"# BUDGET EXHAUSTED at scan {work.state.scan_id}")
    return _values_match(work.state.tags.get(target_tag), target_value), steps, work


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
