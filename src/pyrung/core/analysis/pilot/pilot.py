"""PILOT loop: trace backward, apply forward, learn from cause() chains.

Acceptance logic uses state-key-based layers (causal momentum) instead of
distance-gated branches.  The state key reuses the prover's projection
(stateful_names + done-bit abstraction + threshold vectors) so accumulator
ticks are absorbed and only structural transitions change the key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import Path, ReachabilityStep
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.steers import upstream_candidates
from pyrung.core.analysis.pilot.trace import (
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
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
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


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
    nogoods: dict[tuple[Any, ...], set[str]] = {}
    checkpoints: list[tuple[tuple[Any, ...], PLC, int]] = []
    forced_holds: dict[str, Any] = {}
    steps: list[_Step] = []
    work = plc
    watch_tags: list[str] = []
    best_trend: int | None = None
    last_wait_log: tuple[Any, ...] | None = None
    _key_cfg = key_config

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
            _dbg(f"# nogoods for key: {sorted(nogoods.get(key, set())) or '(none)'}")
            _dbg(f"# forced_holds: {dict(forced_holds) if forced_holds else '(none)'}")
            _dbg(f"# seen_keys: {len(seen_keys)}  checkpoints: {len(checkpoints)}")
            pivots = tree.pivot_tags()
            _dbg(f"# pivot_tags ({len(pivots)}): {sorted(pivots)}")
            _dbg(f"# trace ordered_actions (raw, {len(trace_actions)}):")
            for t, v in trace_actions:
                cur = snap.get(t)
                edge = " [EDGE]" if t in edge_tags else ""
                ng = " [NOGOOD]" if t in nogoods.get(key, ()) else ""
                already = " [ALREADY]" if _values_match(cur, v) and t not in edge_tags else ""
                _dbg(f"#   {t}={v!r}  (cur={cur!r}){edge}{ng}{already}")

        key_nogoods = nogoods.get(key, set())
        trace_actions = [
            (t, v)
            for t, v in trace_actions
            if (not _values_match(snap.get(t), v) or t in edge_tags) and t not in key_nogoods
        ]

        stuck_tags = {n.tag for n in tree.leaves() if not n.satisfied and not n.is_steerable}
        up_candidates = upstream_candidates(
            stuck_tags,
            steerable,
            key_nogoods,
            snap,
            pdg,
            nd_domains=nd_domains,
        )

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

        # ---- Batch acceptance (Layer 0-3 on batch) ----
        accepted = False
        if trace_actions:
            _dbg(f"# --- Batch try ({len(trace_actions)}) ---")
            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            scan_before = fork.state.scan_id
            _apply_pulse(fork, trace_actions, resting, edge_tags)
            fork_snap = dict(fork.state.tags)
            _settle_delayed_effects(
                fork, snap, _key_cfg, scan_budget=max_scans - fork.state.scan_id
            )
            fork_snap = dict(fork.state.tags)

            if _values_match(fork_snap.get(target_tag), target_value):
                _dbg_observe("batch-target", snap, fork)
                work = _commit_step(
                    work,
                    fork,
                    {t: v for t, v in trace_actions},
                    scan_before,
                    steps,
                    resting,
                    edge_tags,
                    live,
                )
                accepted = True
            else:
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
                    )
                    batch_trend = batch_tree.unsatisfied_count()
                    new_frontier = set(batch_tree.ordered_actions())
                    has_frontier = bool(new_frontier) or _has_pending_effects(fork)
                    if has_frontier and (
                        (new_frontier - set(tree.ordered_actions()))
                        or batch_trend < distance_before
                        or _has_pending_effects(fork)
                    ):
                        _dbg(f"# BATCH-ACCEPT: distance {distance_before} -> {batch_trend}")
                        _dbg_observe("batch", snap, fork)
                        seen_keys.add(new_key)
                        work = _commit_step(
                            work,
                            fork,
                            {t: v for t, v in trace_actions},
                            scan_before,
                            steps,
                            resting,
                            edge_tags,
                            live,
                        )
                        accepted = True
                        # Layer 4: checkpoint on trend improvement
                        if batch_trend < best_trend:
                            checkpoints.append((new_key, work.fork(), batch_trend))
                            best_trend = batch_trend
                    else:
                        _dbg("# BATCH-DEAD-END: no new frontier, no trend improvement")
                elif new_key == key:
                    _dbg("# BATCH-SPIN: no key change")
                else:
                    _dbg("# BATCH-CYCLE: key already seen")

        if accepted:
            last_wait_log = None
            continue

        # ---- Build single-candidate list ----
        seen_tags: set[str] = set()
        candidates: list[tuple[str, Any]] = []
        broad: list[tuple[str, Any]] = []
        for t, v in [*trace_actions, *up_candidates]:
            if t not in seen_tags:
                seen_tags.add(t)
                if len(pdg.downstream_slice(t)) > blast_cap:
                    broad.append((t, v))
                else:
                    candidates.append((t, v))
        candidates.extend(broad)

        if debug:
            _dbg(f"# --- Single-candidate fork-check ({len(candidates)}) ---")

        # ---- Per-candidate loop (Layer 0-3) ----
        for ci, (t, v) in enumerate(candidates):
            _dbg(f"#   [{ci + 1}/{len(candidates)}] try {t}={v!r}")
            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            scan_before = fork.state.scan_id
            _apply_pulse(fork, [(t, v)], resting, edge_tags)
            fork_snap = dict(fork.state.tags)
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

            pending = _has_pending_effects(fork)

            # Layer 0: Don't Spin (unless async effects are pending)
            if new_key == key and not pending:
                nogoods.setdefault(key, set()).add(t)
                _dbg(f"#     SPIN ({t}={v!r})")
                continue

            # Layer 1: Don't Cycle (unless async effects are pending)
            if new_key in seen_keys and not pending:
                nogoods.setdefault(key, set()).add(t)
                _dbg(f"#     CYCLE ({t}={v!r})")
                continue

            # Layer 3: Don't Dead-End (no actions and no async effects)
            new_tree = trace_back(
                target_tag,
                target_value,
                fork_snap,
                pdg,
                program,
                steerable,
            )
            new_trend = new_tree.unsatisfied_count()
            if not new_tree.ordered_actions() and not _has_pending_effects(fork):
                nogoods.setdefault(key, set()).add(t)
                _dbg(f"#     DEAD-END ({t}={v!r}): empty frontier, no pending effects")
                continue

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
                cause_holds: list[tuple[str, Any]] = []
                for wt in watch_tags:
                    if not _values_match(snap.get(wt), fork_snap.get(wt)):
                        _, hl = _chase_cause_roots(work, wt, steerable)
                        cause_holds.extend(hl)
                needed_tags = {a for a, _ in tree.ordered_actions()}
                useful_holds = [(ht, hv) for ht, hv in cause_holds if ht not in needed_tags]
                if useful_holds:
                    _install_holds(work, useful_holds, forced_holds)
                    for ht, hv in useful_holds:
                        _dbg(f"#     HOLD {ht}={hv!r} (from cause chain)")
                _, cp_fork, cp_trend = checkpoints[-1]
                work = cp_fork.fork()
                _install_holds(work, list(forced_holds.items()), {})
                best_trend = cp_trend

            break

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
        max_scans=max_scans,
        debug=debug,
    )

    return _build_path(reached, steps, target_tag, target_value)


def pilot_drive(
    plc: PLC,
    *conditions: Any,
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
        max_scans=max_scans,
        live=True,
        debug=debug,
    )

    return _build_path(reached, steps, target_tag, target_value)
