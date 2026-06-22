"""PILOT loop: trace backward, apply forward, learn from cause() chains."""

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
# Governing tags — auto-detect from PDG upstream cone
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Core PILOT loop
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
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
) -> tuple[bool, list[_Step], PLC]:
    """Run the PILOT loop: trace -> fork -> observe -> swap or learn.

    Returns ``(reached, steps, work)`` where *work* is the final PLC
    (may be a different fork than the original when ``live=False``).

    When ``live=True``, actions are applied directly — no lookahead.
    When ``debug=True``, prints PLC commands as they execute.
    """
    nogoods: set[str] = set()
    forced_holds: dict[str, Any] = {}
    chain_width: int = 1
    steps: list[_Step] = []
    work = plc
    watch_tags: list[str] = []
    last_wait_log: tuple[Any, ...] | None = None
    gate_moves_budget: int = 3
    damage_history: set[str] = set()

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
            # Extend the last step to cover Phase 3 waiting scans
            # (timers, Harness delays) so the path replays correctly.
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

        if not watch_tags:
            watch_tags.extend(sorted(tree.pivot_tags()))
            _dbg(f"# watch_tags ({len(watch_tags)}): {watch_tags[:8]}...")

        distance_before = tree.unsatisfied_count()

        # --- Build candidate list: trace actions first, then upstream cone ---
        trace_actions = tree.ordered_actions()
        # Same-tag chains encode sequential state-machine dependencies
        # (e.g. Cur must go 9→15→4). Truncate to chain_width actions;
        # width starts at 1 and escalates when batches are NEUTRAL.
        if trace_actions and tree.same_tag_chains():
            trace_actions = trace_actions[:chain_width]
        # Edge tags (rise/fall) need re-pulsing even when already at
        # the target value — _apply_pulse handles the release-and-reapply.
        trace_actions = [
            (t, v)
            for t, v in trace_actions
            if (not _values_match(snap.get(t), v) or t in edge_tags) and t not in nogoods
        ]

        stuck_tags = {n.tag for n in tree.leaves() if not n.satisfied and not n.is_steerable}
        up_candidates = upstream_candidates(
            stuck_tags,
            steerable,
            nogoods,
            snap,
            pdg,
            nd_domains=nd_domains,
        )

        # --- Try trace actions as a batch first ---
        accepted = False
        if trace_actions:
            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            scan_before = fork.state.scan_id
            _apply_pulse(fork, trace_actions, resting, edge_tags)
            fork_snap = dict(fork.state.tags)

            if _values_match(fork_snap.get(target_tag), target_value):
                patch_repr = ", ".join(f"{t}: {v!r}" for t, v in trace_actions)
                _dbg(f"plc.patch({{{patch_repr}}})")
                _dbg(f"plc.run({fork.state.scan_id - scan_before})")
                _dbg_observe("observe", snap, fork)
                steps.append(
                    _Step(
                        action={t: v for t, v in trace_actions},
                        scan_before=scan_before,
                        scan_after=fork.state.scan_id,
                    )
                )
                if live:
                    _apply_pulse(work, trace_actions, resting, edge_tags)
                else:
                    work = fork
                accepted = True

            if not accepted:
                batch_tree = trace_back(
                    target_tag,
                    target_value,
                    fork_snap,
                    pdg,
                    program,
                    steerable,
                )
                batch_distance = batch_tree.unsatisfied_count()
                # Accept NEUTRAL batches only if something actually changed
                # (e.g. Harness feedback is delayed). Ack-cleared inputs
                # that revert to default leave snap unchanged — reject those
                # to avoid spinning on the same action.
                snap_changed = any(not _values_match(snap.get(k), fork_snap.get(k)) for k in snap)
                if batch_distance < distance_before or (
                    batch_distance == distance_before and snap_changed
                ):
                    patch_repr = ", ".join(f"{t}: {v!r}" for t, v in trace_actions)
                    _dbg(f"plc.patch({{{patch_repr}}})")
                    _dbg(f"plc.run({fork.state.scan_id - scan_before})")
                    _dbg(f"# distance: {distance_before} -> {batch_distance}")
                    _dbg_observe("observe", snap, fork)
                    steps.append(
                        _Step(
                            action={t: v for t, v in trace_actions},
                            scan_before=scan_before,
                            scan_after=fork.state.scan_id,
                        )
                    )
                    if live:
                        _apply_pulse(work, trace_actions, resting, edge_tags)
                    else:
                        work = fork
                    accepted = True

        if accepted:
            chain_width = 1
            gate_moves_budget = 3
            # damage_history intentionally NOT cleared — once an input proved
            # harmful, don't retry it even after intervening progress.
            last_wait_log = None
            continue

        # Nothing worked at this width — widen the batch window.
        # An engineer who tries one button and nothing happens tries
        # two buttons next time (e.g. the handshake pair ChgReq+ProdMode).
        if tree.same_tag_chains():
            chain_width = min(chain_width + 1, len(tree.ordered_actions()))

        seen: set[str] = set()
        candidates: list[tuple[str, Any]] = []
        for t, v in [*trace_actions, *up_candidates]:
            if t not in seen:
                seen.add(t)
                candidates.append((t, v))

        # --- Fork-check each candidate one at a time ---
        for t, v in candidates:
            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            scan_before = fork.state.scan_id
            _apply_pulse(fork, [(t, v)], resting, edge_tags)

            fork_snap = dict(fork.state.tags)
            if _values_match(fork_snap.get(target_tag), target_value):
                _dbg(f"plc.patch({{{t}: {v!r}}})")
                _dbg(f"plc.run({fork.state.scan_id - scan_before})")
                _dbg_observe("observe", snap, fork)
                steps.append(
                    _Step(
                        action={t: v},
                        scan_before=scan_before,
                        scan_after=fork.state.scan_id,
                    )
                )
                if live:
                    _apply_pulse(work, [(t, v)], resting, edge_tags)
                else:
                    work = fork
                accepted = True
                break

            new_tree = trace_back(
                target_tag,
                target_value,
                fork_snap,
                pdg,
                program,
                steerable,
            )
            distance_after = new_tree.unsatisfied_count()

            if distance_after > distance_before:
                # Ask the fork *why* it regressed — chase cause chains
                # from each watch tag that worsened.
                cause_nogoods: set[str] = set()
                cause_holds: list[tuple[str, Any]] = []
                for wt in watch_tags:
                    if not _values_match(snap.get(wt), fork_snap.get(wt)):
                        ng, hl = _chase_cause_roots(fork, wt, steerable)
                        cause_nogoods.update(ng)
                        cause_holds.extend(hl)

                # Only hold inputs that aren't also needed by the trace —
                # holding a needed input blocks progress.
                needed_tags = {a for a, _ in tree.ordered_actions()}
                useful_holds = [(ht, hv) for ht, hv in cause_holds if ht not in needed_tags]
                if useful_holds:
                    _install_holds(work, useful_holds, forced_holds)
                    for ht, hv in useful_holds:
                        _dbg(f"# HOLD {ht}={hv!r} (from cause chain)")

                if t in needed_tags and t not in damage_history:
                    damage_history.add(t)
                    _dbg(f"# ACCEPT-WITH-DAMAGE ({t}={v!r}): {distance_before} -> {distance_after}")
                    steps.append(
                        _Step(
                            action={t: v},
                            scan_before=scan_before,
                            scan_after=fork.state.scan_id,
                        )
                    )
                    if live:
                        _apply_pulse(work, [(t, v)], resting, edge_tags)
                    else:
                        work = fork
                    accepted = True
                    last_wait_log = None
                    break

                nogoods.add(t)
                _dbg(f"# REGRESSED ({t}={v!r}): {distance_before} -> {distance_after}")
                continue
            if distance_after == distance_before:
                if gate_moves_budget > 0:
                    gate_moved = any(
                        not _values_match(snap.get(wt), fork_snap.get(wt))
                        for wt in watch_tags
                    )
                else:
                    gate_moved = False
                if gate_moved:
                    gate_moves_budget -= 1
                    _dbg(f"# GATE-MOVED ({t}={v!r}): distance={distance_before}")
                    _dbg_observe("observe", snap, fork)
                    steps.append(
                        _Step(
                            action={t: v},
                            scan_before=scan_before,
                            scan_after=fork.state.scan_id,
                        )
                    )
                    if live:
                        _apply_pulse(work, [(t, v)], resting, edge_tags)
                    else:
                        work = fork
                    accepted = True
                    last_wait_log = None
                    break
                _dbg(f"# NEUTRAL  ({t}={v!r}): {distance_before}")
                continue

            _dbg(f"plc.patch({{{t}: {v!r}}})")
            _dbg(f"plc.run({fork.state.scan_id - scan_before})")
            _dbg(f"# distance: {distance_before} -> {distance_after}")
            _dbg_observe("observe", snap, fork)
            steps.append(
                _Step(
                    action={t: v},
                    scan_before=scan_before,
                    scan_after=fork.state.scan_id,
                )
            )
            if live:
                _apply_pulse(work, [(t, v)], resting, edge_tags)
            else:
                work = fork
            accepted = True
            gate_moves_budget = 3
            # damage_history intentionally NOT cleared — once an input proved
            # harmful, don't retry it even after intervening progress.
            last_wait_log = None
            break

        if accepted:
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
# Domain inference — optional _ExploreContext from prove pipeline
# ---------------------------------------------------------------------------


def _build_nd_domains(
    program: Any,
    snapshot: dict[str, Any],
) -> dict[str, tuple[Any, ...]] | None:
    """Build nondeterministic-input domains via the prover pipeline.

    Returns ``None`` on any failure — pilot falls back to Bool-only probing.
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
            return None
        nd = getattr(ctx, "nondeterministic_dims", None)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))
        return nd
    except Exception:  # noqa: BLE001
        logger.debug("pilot: nd_domains build failed", exc_info=True)
        return None


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
    nd_domains = _build_nd_domains(program, dict(fork.state.tags))

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
    nd_domains = _build_nd_domains(program, dict(plc.state.tags))

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
        max_scans=max_scans,
        live=True,
        debug=debug,
    )

    return _build_path(reached, steps, target_tag, target_value)
