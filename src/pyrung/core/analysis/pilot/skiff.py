"""Probe unreadable transition frontiers with isolated forked scans.

The low-level scan helpers pin nonparticipating mutable tags, apply a bounded
action set, step a fork, and report the resulting changes. The frontier probe
logic selects only finite declared action domains, runs control and probe
experiments, and returns ``CompassObservation`` values.

Skiff probing does not update the compass itself or treat an observation as a
committed plan step.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.causal import empirical_program_writes
from pyrung.core.analysis.pilot.compass import (
    CompassObservation,
    NavigationObservation,
    _action_sort_key,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
    from pyrung.core.runner import PLC

ActionPair = tuple[str, Any]


@dataclass(frozen=True)
class SkiffResult:
    """Observed result from an isolated skiff pipeline scan."""

    allowed_tags: frozenset[str]
    forced_tags: frozenset[str]
    actions: tuple[ActionPair, ...]
    scan_before: int
    scan_after: int
    before: dict[str, Any]
    after: dict[str, Any]
    participating_changes: tuple[tuple[str, Any, Any], ...]
    suppressed_changes: tuple[tuple[str, Any, Any], ...]
    work: PLC


def participating_tags_for_skiff(
    role: PipelineRoles,
    *,
    routes: tuple[TransitionRoute, ...] = (),
    actions: tuple[ActionPair, ...] = (),
    extra_tags: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Tags allowed to change during an isolated skiff pipeline scan."""

    tags = set(role.participating_tags)
    tags.update(tag for tag, _value in actions)
    tags.update(extra_tags)
    for route in routes:
        if route.destination_tag == role.channel_tag:
            tags.update(tag for tag, _value in route.source_constraints)
            tags.update(tag for tag, _value in route.enablers)
            tags.update(route.action_tags)
            if route.request_tag is not None:
                tags.add(route.request_tag)
    return frozenset(tags)


def run_skiff_scan(
    plc: PLC,
    role: PipelineRoles,
    pdg: ProgramGraph,
    *,
    rungs: Sequence[Any],
    actions: tuple[ActionPair, ...] = (),
    routes: tuple[TransitionRoute, ...] = (),
    extra_tags: frozenset[str] = frozenset(),
    scans: int = 1,
) -> SkiffResult:
    """Run a real scan window while pinning non-participating tags.

    The scan uses a fork and does not mutate the caller's PLC. The program still
    executes normally; isolation is achieved by forcing every mutable tag outside
    the participating set to its pre-scan value for the scan window.
    """
    allowed = participating_tags_for_skiff(
        role,
        routes=routes,
        actions=actions,
        extra_tags=extra_tags,
    )
    return run_pinned_scan(
        plc,
        allowed,
        pdg,
        rungs=rungs,
        actions=actions,
        scans=scans,
    )


def run_pinned_scan(
    plc: PLC,
    allowed_tags: frozenset[str],
    pdg: ProgramGraph,
    *,
    rungs: Sequence[Any],
    actions: tuple[ActionPair, ...] = (),
    scans: int = 1,
) -> SkiffResult:
    """The skiff core: fork, pin every mutable tag outside *allowed_tags* to its
    pre-scan value, apply *actions*, step *scans*, observe.

    Role-less sibling of :func:`run_skiff_scan` — the caller supplies the
    participating set directly (e.g. the upstream cone of a live-guard
    frontier), so isolation works for programs with no detected pipeline role.
    """
    if scans < 1:
        raise ValueError("scans must be >= 1")

    from pyrung.core.analysis.pilot.overlay import fork_with_rungs

    fork = fork_with_rungs(plc, rungs)
    before = dict(fork.state.tags)
    force_map = _skiff_force_map(fork, before, allowed_tags, pdg)
    scan_before = fork.state.scan_id

    with fork.forced(force_map):
        if actions:
            fork.patch(dict(actions))
        for _ in range(scans):
            fork.step()

    after = dict(fork.state.tags)
    return SkiffResult(
        allowed_tags=allowed_tags,
        forced_tags=frozenset(force_map),
        actions=actions,
        scan_before=scan_before,
        scan_after=fork.state.scan_id,
        before=before,
        after=after,
        participating_changes=_diff(before, after, tags=allowed_tags),
        suppressed_changes=_diff(before, after, tags=frozenset(force_map)),
        work=fork,
    )


def _skiff_force_map(
    plc: PLC,
    snapshot: dict[str, Any],
    allowed_tags: frozenset[str],
    pdg: ProgramGraph,
) -> dict[str, Any]:
    forced: dict[str, Any] = {}
    for tag, value in snapshot.items():
        if tag in allowed_tags:
            continue
        tag_ref = pdg.tags.get(tag)
        if tag_ref is not None and tag_ref.readonly:
            continue
        if plc._system_runtime.is_read_only(tag):
            continue
        forced[tag] = value
    return forced


def _diff(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    tags: frozenset[str],
) -> tuple[tuple[str, Any, Any], ...]:
    changes: list[tuple[str, Any, Any]] = []
    for tag in sorted(tags):
        old = before.get(tag)
        new = after.get(tag)
        if not _values_match(old, new):
            changes.append((tag, old, new))
    return tuple(changes)


# ---------------------------------------------------------------------------
# The skiff driver — isolated probes on live-guard frontiers
# ---------------------------------------------------------------------------

_SKIFF_SCANS = 4  # enough for command -> staged register -> gated transition
_SKIFF_MAX_PROBES = 16  # per frontier per invocation — forks are cheap, not free
_SKIFF_MAX_DOMAIN = 8  # word probes only when the declared domain is this small


def probe_live_guard_frontiers(
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    scans: int = _SKIFF_SCANS,
    max_probes: int = _SKIFF_MAX_PROBES,
) -> tuple[NavigationObservation, ...]:
    """Probe each unreadable frontier in the current trace tree.

    A frontier is unreadable when the static walk punted on it: a
    ``live_guard`` node (writer guard over a genuinely-live word) or an
    opaque-cut leaf (the walk refused the tag as part of an opaque pipeline
    and no instrument produced a plan for it).  For each one, run isolated
    experiments: hold the tree's *readable* half (its steerable trace
    actions) as context, add unprobed candidate actions from the frontier's
    upstream cone — single actions first, then pairs, because a runtime-gated
    transition often needs a command AND an enablement select in the same
    window — pin everything else, step, and observe whether the frontier
    register moved.  An observed move is an ``"edge"`` observation (a pair
    carries a *composite* cause — a tuple of action pairs); a still stand is
    ``"no_change"`` so the same probe is never re-sent.

    Returns new observations without applying them. An empty result means the
    probes added no knowledge. A learned edge only surfaces a candidate on the
    next iteration and must still pass live trial verification.
    """
    frontiers = []
    seen_frontier: set[tuple[str, str]] = set()
    for n in frame.tree.iter_nodes():
        if n.satisfied or n.is_steerable:
            continue
        # An opaque-cut frontier: the walk refused the tag (opaque pipeline /
        # pipeline channel) and left it childless.  The skiff only runs from
        # the stuck exits, so a pipeline channel reaching here means every
        # static instrument (route plan, value graph) already came up empty.
        opaque_cut = not n.children and (
            n.tag in ctx.opaque_loop or getattr(n, "pipeline_internal", False)
        )
        if not (getattr(n, "live_guard", False) or opaque_cut):
            continue
        fkey = (n.tag, repr(frame.snap.get(n.tag)))
        if fkey in seen_frontier:
            continue
        seen_frontier.add(fkey)
        frontiers.append(n)
    if not frontiers:
        return ()

    # Empirical steerable veto (``empirical_program_writes``): a word that looks
    # steerable to the static classifier but that the recorded run shows the
    # PROGRAM wrote (at a scan the pilot neither held nor pulsed it) is not a
    # sound probe lever and must not enter the probe set. Restricted to
    # the frontier cones' steerable words (cheap) over the whole recorded run.
    cone_steerable: set[str] = set()
    for node in frontiers:
        cone_steerable.update(ctx.pdg.upstream_slice(node.tag, follow_calls=True) & ctx.steerable)
    empirical_writes = empirical_program_writes(
        state.work,
        frozenset(cone_steerable),
        start_scan=0,
        end_scan=getattr(getattr(state.work, "state", None), "scan_id", 0) or 0,
    )

    # Context: the readable half of the bearing.  A joint requirement
    # (command + config select) is only observable when the known-steerable
    # trace actions ride along with the probe.
    context: dict[str, Any] = {}
    for tag, value in frame.tree.ordered_actions():
        if tag in ctx.steerable and (tag, value) not in ctx.blocked_actions:
            context.setdefault(tag, value)

    observations: list[NavigationObservation] = []
    active_rungs = tuple(state.overlay_rules)
    for node in frontiers:
        cur_val = frame.snap.get(node.tag)
        # Canonical key: a frontier can surface probe pairs whose values mix types
        # (an Int word beside a Bool lever), which the default tuple order cannot
        # compare — ``_action_sort_key`` reprs each value (the same guard
        # ``unprobed_actions`` uses).
        singles = sorted(
            _frontier_probes(node.tag, frame.snap, context, ctx, empirical_writes),
            key=_action_sort_key,
        )
        if not singles:
            continue

        allowed = _frontier_participating(node.tag, context, singles, ctx)

        # Control run: context alone.  If the frontier moves without any probe,
        # the stall is not this frontier — attributing edges to probes would lie.
        control = run_pinned_scan(
            state.work,
            allowed,
            ctx.pdg,
            rungs=active_rungs,
            actions=tuple(context.items()),
            scans=scans,
        )
        if not _values_match(control.after.get(node.tag), cur_val):
            continue

        # Pass 1: single actions.
        edge_found = False
        budget = max_probes
        for probe in ctx.compass.knowledge.unprobed_actions(
            node.tag,
            cur_val,
            set(singles),
            world_key=frame.key,
            snapshot=frame.snap,
            applied_context=tuple(sorted(context.items())),
        )[:budget]:
            budget -= 1
            obs = _send_probe(
                node.tag,
                cur_val,
                (probe,),
                probe,
                context,
                allowed,
                state,
                ctx,
                active_rungs,
                scans,
                frame.key,
            )
            edge_found |= obs.kind == "edge"
            observations.append(obs)

        # Pass 2/3: small joint actions — only when no narrower action moved the
        # frontier.  The composite cause is the sorted tuple; Compass proposes
        # it as one atomic BatchPulse and live verification remains authoritative.
        if not edge_found and budget > 0:
            pairs = [
                tuple(sorted(pair))
                for pair in itertools.combinations(singles, 2)
                if pair[0][0] != pair[1][0]
            ]
            for composite in ctx.compass.knowledge.unprobed_actions(
                node.tag,
                cur_val,
                set(pairs),
                world_key=frame.key,
                snapshot=frame.snap,
                applied_context=tuple(sorted(context.items())),
            )[:budget]:
                observation = _send_probe(
                    node.tag,
                    cur_val,
                    tuple(composite),
                    composite,
                    context,
                    allowed,
                    state,
                    ctx,
                    active_rungs,
                    scans,
                    frame.key,
                )
                observations.append(observation)
                budget -= 1
                if observation.kind == "edge":
                    edge_found = True
                    break
        if not edge_found and budget > 0:
            triples = [
                tuple(sorted(group))
                for group in itertools.combinations(singles, 3)
                if len({pair[0] for pair in group}) == 3
            ]
            for composite in ctx.compass.knowledge.unprobed_actions(
                node.tag,
                cur_val,
                set(triples),
                world_key=frame.key,
                snapshot=frame.snap,
                applied_context=tuple(sorted(context.items())),
            )[:budget]:
                observation = _send_probe(
                    node.tag,
                    cur_val,
                    tuple(composite),
                    composite,
                    context,
                    allowed,
                    state,
                    ctx,
                    active_rungs,
                    scans,
                    frame.key,
                )
                observations.append(observation)
                if observation.kind == "edge":
                    break
    return tuple(observations)


def _send_probe(
    frontier_tag: str,
    cur_val: Any,
    probe_actions: tuple[ActionPair, ...],
    cause: Any,
    context: dict[str, Any],
    allowed: frozenset[str],
    state: Any,
    ctx: Any,
    rungs: Sequence[Any],
    scans: int,
    world_key: tuple[Any, ...],
) -> CompassObservation:
    """Run one isolated probe and return its unapplied observation."""
    actions = dict(context)
    actions.update(probe_actions)
    result = run_pinned_scan(
        state.work,
        allowed,
        ctx.pdg,
        rungs=rungs,
        actions=tuple(actions.items()),
        scans=scans,
    )
    new_val = result.after.get(frontier_tag)
    before = tuple(sorted(dict(state.work.state.tags).items()))
    applied = tuple(sorted(actions.items()))
    if not _values_match(new_val, cur_val):
        return CompassObservation(
            "edge", frontier_tag, cause, cur_val, new_val, world_key, before, applied
        )
    return CompassObservation(
        "no_change", frontier_tag, cause, cur_val, None, world_key, before, applied
    )


def _declared_domain(tag_ref: Any) -> tuple[Any, ...] | None:
    """The tag's **declared** complete finite domain, or ``None``.

    The single source of truth the prover / bounds / validators all read: an
    explicit ``choices=`` mapping, or an integer ``min=``/``max=`` range small
    enough to enumerate.  This is a *declaration*, not the prover's back-inferred
    representative ``nd_domains`` — so enumerating over it respects "Complete
    domains only": the skiff probes only values the engineer declared, never
    invented ones.
    """
    if tag_ref is None:
        return None
    choices = getattr(tag_ref, "choices", None)
    if choices:
        return tuple(sorted(choices))
    mn = getattr(tag_ref, "min", None)
    mx = getattr(tag_ref, "max", None)
    if mn is not None and mx is not None and int(mn) == mn and int(mx) == mx:
        span = int(mx) - int(mn) + 1
        if 0 < span <= _SKIFF_MAX_DOMAIN:
            return tuple(range(int(mn), int(mx) + 1))
    return None


def _frontier_probes(
    frontier_tag: str,
    snap: dict[str, Any],
    context: dict[str, Any],
    ctx: Any,
    empirical_writes: frozenset[str] = frozenset(),
) -> set[ActionPair]:
    """Candidate probe actions for one frontier: steerable tags in its upstream
    cone that the context does not already hold.

    *empirical_writes* (the empirical steerable veto) names cone words the
    recorded run shows the program wrote — not sound probe levers, so they are
    skipped (positive evidence only; empty = the prior behavior).

    Bools probe to their non-resting value (one rising edge inside the pinned
    window), and are restricted to tags some rung CONDITION reads: a lever the
    program decides on.  A *data-read* Bool (rare) is not an operator lever.

    Words probe each domain value other than the current one, only when the
    domain is small.  The sound domain is a **declared** one (``choices=`` /
    ``min=``/``max=``): a word carrying one is a finite operator lever — an
    external config word copied into an enablement mask is exactly this — so it
    is probeable even when only *data-read* (a copy source no condition reads).
    A word with only a back-inferred ``nd_domains`` representative is probed only
    when a condition reads it (the pre-existing lever case); a wide/undeclared
    data word offers no sound probe values and is left untouched.
    """
    cone = ctx.pdg.upstream_slice(frontier_tag, follow_calls=True)
    condition_read = {
        tag for node in ctx.pdg.rung_nodes for tag in getattr(node, "condition_reads", ())
    }
    probes: set[ActionPair] = set()
    for tag in sorted(cone & ctx.steerable):
        if tag in context or tag in empirical_writes:
            continue
        resting = ctx.resting.get(tag)
        if isinstance(resting, bool) or resting is None:
            if tag not in condition_read:
                continue
            probes.add((tag, not resting if resting is not None else True))
            continue
        # Word tag.  A declared complete domain makes it a probeable lever even
        # when only data-read; otherwise it must be condition-read for the softer
        # inferred-domain path.
        declared = _declared_domain(ctx.pdg.tags.get(tag))
        cur = snap.get(tag)
        if declared is not None and len(declared) <= _SKIFF_MAX_DOMAIN:
            probes.update((tag, v) for v in declared if not _values_match(v, cur))
            continue
        if tag in condition_read:
            domain = (ctx.nd_domains or {}).get(tag, ())
            if 0 < len(domain) <= _SKIFF_MAX_DOMAIN:
                probes.update((tag, v) for v in domain if not _values_match(v, cur))
    return probes


def _frontier_participating(
    frontier_tag: str,
    context: dict[str, Any],
    probes: list[ActionPair],
    ctx: Any,
) -> frozenset[str]:
    """The isolation set for a frontier probe: the frontier's upstream cone
    (the guard's whole causal territory), the frontier itself, and every tag
    the experiment drives."""
    allowed = set(ctx.pdg.upstream_slice(frontier_tag, follow_calls=True))
    allowed.add(frontier_tag)
    allowed.update(context)
    allowed.update(tag for tag, _v in probes)
    return frozenset(allowed)
