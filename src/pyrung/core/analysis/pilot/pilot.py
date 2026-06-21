"""PILOT loop: trace backward, apply forward, learn from cause() chains."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import Path, ReachabilityStep
from pyrung.core.analysis.pilot.trace import (
    compute_edge_tags,
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
            if (
                root.from_value is not None
                and not _values_match(root.from_value, root.to_value)
            ):
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


def _compute_gov_tags(
    target_tag: str,
    pdg: ProgramGraph,
) -> list[str]:
    """Tags in the transitive write-cone of *target_tag*.

    These are the tags PILOT monitors for progress/regression.
    Only PIVOT/non-INPUT tags with writers are interesting.
    """
    from pyrung.core.analysis.pdg import TagRole

    cone: set[str] = set()
    frontier = {target_tag}

    while frontier:
        tag = frontier.pop()
        if tag in cone:
            continue
        cone.add(tag)
        for ri in pdg.writers_of.get(tag, frozenset()):
            node = pdg.rung_nodes[ri]
            for read_tag in node.condition_reads | node.data_reads:
                if read_tag not in cone and pdg.tag_roles.get(read_tag) != TagRole.INPUT:
                    frontier.add(read_tag)

    cone.discard(target_tag)
    return sorted(cone)


# ---------------------------------------------------------------------------
# Core PILOT loop
# ---------------------------------------------------------------------------


def _check_regression(
    fork: PLC,
    snap: dict[str, Any],
    committed: dict[str, Any],
    gov_tags: list[str],
    steerable: frozenset[str],
    target_tag: str,
    target_value: Any,
) -> tuple[set[str], list[tuple[str, Any]]]:
    """Check a fork for regressions on *committed* governing-tag values.

    A regression is a gov tag that was previously at a committed value
    (one we achieved and want to keep) and has now changed away from it.
    Tags that weren't committed yet can change freely — that's progress.

    Returns ``(nogoods, holds)`` accumulated from cause() chains.
    """
    fork_snap = dict(fork.state.tags)

    if _values_match(fork_snap.get(target_tag), target_value):
        return set(), []

    nogoods: set[str] = set()
    holds: list[tuple[str, Any]] = []
    for gt in gov_tags:
        if gt not in committed:
            continue
        committed_val = committed[gt]
        new_val = fork_snap.get(gt)
        if not _values_match(committed_val, new_val):
            ng, hs = _chase_cause_roots(fork, gt, steerable)
            nogoods.update(ng)
            holds.extend(hs)
    return nogoods, holds


def _update_committed(
    committed: dict[str, Any],
    work: PLC,
    gov_tags: list[str],
) -> None:
    """Snapshot current gov-tag values as committed (must-stay)."""
    tags = work.state.tags
    for gt in gov_tags:
        val = tags.get(gt)
        if val is not None:
            committed[gt] = val


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
    max_scans: int = 3000,
    live: bool = False,
) -> tuple[bool, list[_Step], PLC]:
    """Run the PILOT loop: trace → fork → observe → swap or learn.

    Returns ``(reached, steps, work)`` where *work* is the final PLC
    (may be a different fork than the original when ``live=False``).

    When ``live=True``, actions are applied directly — no lookahead.
    """
    gov_tags = _compute_gov_tags(target_tag, pdg)
    nogoods: set[str] = set()
    forced_holds: dict[str, Any] = {}
    committed: dict[str, Any] = {}
    steps: list[_Step] = []
    steerable_list = sorted(steerable)
    work = plc

    while work.state.scan_id < max_scans:
        snap = dict(work.state.tags)

        if _values_match(snap.get(target_tag), target_value):
            return True, steps, work

        # --- Phase 1: Trace backward ---
        tree = trace_back(
            target_tag, target_value, snap, pdg, program, steerable,
        )
        actions = tree.ordered_actions()
        actions = [
            (t, v) for t, v in actions
            if not _values_match(snap.get(t), v) and t not in nogoods
        ]

        if actions:
            if live:
                scan_before = work.state.scan_id
                _apply_pulse(work, actions, resting, edge_tags)
                steps.append(_Step(
                    action={t: v for t, v in actions},
                    scan_before=scan_before,
                    scan_after=work.state.scan_id,
                ))
                ng, holds = _check_regression(
                    work, snap, committed, gov_tags, steerable,
                    target_tag, target_value,
                )
                nogoods.update(ng)
                _install_holds(work, holds, forced_holds)
                _update_committed(committed, work, gov_tags)
            else:
                fork = work.fork()
                _install_holds(fork, list(forced_holds.items()), {})
                scan_before = fork.state.scan_id
                _apply_pulse(fork, actions, resting, edge_tags)

                ng, holds = _check_regression(
                    fork, snap, committed, gov_tags, steerable,
                    target_tag, target_value,
                )

                if ng:
                    nogoods.update(ng)
                    logger.info("pilot: trace nogoods %s, discarding fork", ng)
                else:
                    steps.append(_Step(
                        action={t: v for t, v in actions},
                        scan_before=scan_before,
                        scan_after=fork.state.scan_id,
                    ))
                    _install_holds(fork, holds, forced_holds)
                    work = fork
                    _update_committed(committed, work, gov_tags)
            continue

        # --- Phase 2: Probe one input at a time (on a fork) ---
        probed = False
        for inp in steerable_list:
            if inp in nogoods or _values_match(snap.get(inp), True):
                continue

            fork = work.fork()
            _install_holds(fork, list(forced_holds.items()), {})
            fork.patch({inp: True})
            fork.step()
            for _ in range(4):
                fork.step()

            fork_snap = dict(fork.state.tags)
            changed = any(
                not _values_match(snap.get(gt), fork_snap.get(gt))
                for gt in gov_tags
            )
            if not changed:
                continue

            ng, holds = _check_regression(
                fork, snap, committed, gov_tags, steerable,
                target_tag, target_value,
            )
            if ng:
                nogoods.update(ng)
            else:
                steps.append(_Step(
                    action={inp: True},
                    scan_before=work.state.scan_id,
                    scan_after=fork.state.scan_id,
                ))
                _install_holds(fork, holds, forced_holds)
                if live:
                    _apply_pulse(work, [(inp, True)], resting, edge_tags)
                    _update_committed(committed, work, gov_tags)
                else:
                    work = fork
                    _update_committed(committed, work, gov_tags)
                probed = True
                break

        if probed:
            continue

        # --- Phase 3: Step forward (timers/SFCs) ---
        work.step()

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
) -> Path:
    """PILOT on a fork — discover the path, return it. Nothing changes."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork()
    pdg = build_program_graph(program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program)
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)

    reached, steps, _work = _pilot_loop(
        fork, target_tag, target_value, pdg, program,
        steerable, edge_tags, resting,
        max_scans=max_scans,
    )

    return _build_path(reached, steps, target_tag, target_value)


def pilot_drive(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
) -> Path:
    """PILOT on the live PLC — drive the state there."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    pdg = build_program_graph(program)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program)
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, plc._known_tags_by_name, pdg, program)

    reached, steps, _work = _pilot_loop(
        plc, target_tag, target_value, pdg, program,
        steerable, edge_tags, resting,
        max_scans=max_scans,
        live=True,
    )

    return _build_path(reached, steps, target_tag, target_value)
