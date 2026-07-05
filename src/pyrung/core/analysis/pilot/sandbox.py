"""Sandbox scans for opaque transition pipelines.

The *scout* instrument of the compass: when ``trace`` cannot statically read an
edge (a runtime-computed table), run an isolated experiment — fork, pin every
non-participating mutable tag to its pre-scan value, step, and observe the
isolated edge. See ``pilot/CLAUDE.md`` for where this sits in the compass.

Purely the fork-pin-step instrument: this module executes. The static
need→pipeline-route bridging (``PipelineNeedExpansion``,
``roles_for_needed_tag``, ``expand_pipeline_need``) lives in ``evidence.py``
alongside the ``PipelineRoles``/``TransitionRoute`` types it operates on.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionRoute
    from pyrung.core.runner import PLC

ActionPair = tuple[str, Any]


@dataclass(frozen=True)
class SandboxResult:
    """Observed result from an isolated (sandboxed) pipeline scan."""

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


def participating_tags_for_sandbox(
    role: PipelineRoles,
    *,
    routes: tuple[TransitionRoute, ...] = (),
    actions: tuple[ActionPair, ...] = (),
    extra_tags: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Tags allowed to change during a sandboxed pipeline scan."""

    tags = set(role.participating_tags)
    tags.update(tag for tag, _value in actions)
    tags.update(extra_tags)
    for route in routes:
        if route.destination_tag == role.governing_tag:
            tags.update(tag for tag, _value in route.source_constraints)
            tags.update(tag for tag, _value in route.enablers)
            tags.update(route.action_tags)
            if route.request_tag is not None:
                tags.add(route.request_tag)
    return frozenset(tags)


def run_sandbox_scan(
    plc: PLC,
    role: PipelineRoles,
    pdg: ProgramGraph,
    *,
    actions: tuple[ActionPair, ...] = (),
    routes: tuple[TransitionRoute, ...] = (),
    extra_tags: frozenset[str] = frozenset(),
    scans: int = 1,
) -> SandboxResult:
    """Run a real scan window while pinning non-participating tags.

    The scan uses a fork and does not mutate the caller's PLC. The program still
    executes normally; isolation is achieved by forcing every mutable tag outside
    the participating set to its pre-scan value for the scan window.
    """
    allowed = participating_tags_for_sandbox(
        role,
        routes=routes,
        actions=actions,
        extra_tags=extra_tags,
    )
    return run_pinned_scan(plc, allowed, pdg, actions=actions, scans=scans)


def run_pinned_scan(
    plc: PLC,
    allowed_tags: frozenset[str],
    pdg: ProgramGraph,
    *,
    actions: tuple[ActionPair, ...] = (),
    scans: int = 1,
) -> SandboxResult:
    """The skiff core: fork, pin every mutable tag outside *allowed_tags* to its
    pre-scan value, apply *actions*, step *scans*, observe.

    Role-less sibling of :func:`run_sandbox_scan` — the caller supplies the
    participating set directly (e.g. the upstream cone of a live-guard
    frontier), so isolation works for programs with no detected pipeline role.
    """
    if scans < 1:
        raise ValueError("scans must be >= 1")

    fork = plc.fork()
    before = dict(fork.state.tags)
    force_map = _sandbox_force_map(fork, before, allowed_tags, pdg)
    scan_before = fork.state.scan_id

    with fork.forced(force_map):
        if actions:
            fork.patch(dict(actions))
        for _ in range(scans):
            fork.step()

    after = dict(fork.state.tags)
    return SandboxResult(
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


def _sandbox_force_map(
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
) -> int:
    """Send the skiff at every unreadable frontier in the current tree.

    A frontier is unreadable when the static walk punted on it: a
    ``live_guard`` node (writer guard over a genuinely-live word) or an
    opaque-cut leaf (the walk refused the tag as part of an opaque pipeline
    and no instrument produced a plan for it).  For each one, run isolated
    experiments: hold the tree's *readable* half (its steerable trace
    actions) as context, add unprobed candidate actions from the frontier's
    upstream cone — single actions first, then pairs, because a runtime-gated
    transition often needs a command AND an enablement select in the same
    window — pin everything else, step, and observe whether the frontier
    register moved.  Observed moves are recorded into the compass as learned
    edges (a pair records a *composite* cause — a tuple of action pairs);
    still stands are recorded as probed-no-change so the same probe is never
    re-sent.

    Returns the number of NEW observations recorded.  Honesty invariant: a
    learned edge is a *bearing* only — it surfaces as a prescribed candidate
    (or prescribed batch, for a composite) on the next iteration and must be
    confirmed live through the verify pipeline.  Nothing here commits a plan
    step.
    """
    from pyrung.core.analysis.pilot.trace import _all_nodes

    frontiers = []
    seen_frontier: set[tuple[str, str]] = set()
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable:
            continue
        # An opaque-cut frontier: the walk refused the tag (opaque pipeline /
        # pipeline governor) and left it childless.  The skiff only runs from
        # the stuck exits, so a pipeline governor reaching here means every
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
        return 0

    # Context: the readable half of the bearing.  A joint requirement
    # (command + config select) is only observable when the known-steerable
    # trace actions ride along with the probe.
    context: dict[str, Any] = {}
    for tag, value in frame.tree.ordered_actions():
        if tag in ctx.steerable and ctx.route_allowed((tag, value)):
            context.setdefault(tag, value)

    recorded = 0
    for node in frontiers:
        cur_val = frame.snap.get(node.tag)
        singles = sorted(_frontier_probes(node.tag, frame.snap, context, ctx))
        if not singles:
            continue

        allowed = _frontier_participating(node.tag, context, singles, ctx)

        # Control run: context alone.  If the frontier moves without any probe,
        # the stall is not this frontier — attributing edges to probes would lie.
        control = run_pinned_scan(
            state.work, allowed, ctx.pdg, actions=tuple(context.items()), scans=scans
        )
        if not _values_match(control.after.get(node.tag), cur_val):
            continue

        # Pass 1: single actions.
        edge_found = False
        budget = max_probes
        for probe in ctx.compass.unprobed_actions(node.tag, cur_val, set(singles))[:budget]:
            budget -= 1
            edge_found |= _send_probe(
                node.tag, cur_val, (probe,), probe, context, allowed, state, ctx, scans
            )
            recorded += 1

        # Pass 2: pairs — only when no single action moved the frontier.  The
        # composite cause is the sorted pair tuple; candidates propose it as a
        # batch and the verify pipeline confirms it live like any other trial.
        if not edge_found and budget > 0:
            pairs = [
                tuple(sorted(pair))
                for pair in itertools.combinations(singles, 2)
                if pair[0][0] != pair[1][0]
            ]
            for composite in ctx.compass.unprobed_actions(node.tag, cur_val, set(pairs))[:budget]:
                _send_probe(
                    node.tag,
                    cur_val,
                    tuple(composite),
                    composite,
                    context,
                    allowed,
                    state,
                    ctx,
                    scans,
                )
                recorded += 1
    return recorded


def _send_probe(
    frontier_tag: str,
    cur_val: Any,
    probe_actions: tuple[ActionPair, ...],
    cause: Any,
    context: dict[str, Any],
    allowed: frozenset[str],
    state: Any,
    ctx: Any,
    scans: int,
) -> bool:
    """Run one isolated probe and record the observation; True when an edge
    was learned."""
    actions = dict(context)
    actions.update(probe_actions)
    result = run_pinned_scan(
        state.work, allowed, ctx.pdg, actions=tuple(actions.items()), scans=scans
    )
    new_val = result.after.get(frontier_tag)
    if not _values_match(new_val, cur_val):
        ctx.compass.record(frontier_tag, cause, cur_val, new_val)
        return True
    ctx.compass.record_no_change(frontier_tag, cause, cur_val)
    return False


def _frontier_probes(
    frontier_tag: str,
    snap: dict[str, Any],
    context: dict[str, Any],
    ctx: Any,
) -> set[ActionPair]:
    """Candidate probe actions for one frontier: steerable tags in its upstream
    cone that the context does not already hold.

    Bools probe to their non-resting value (one rising edge inside the pinned
    window).  Words probe each declared-domain value other than the current one,
    only when the domain is small — a wide/unknown word offers no sound probe
    values (that tier needs value synthesis; the skiff never guesses).

    Probes are further restricted to tags some rung CONDITION reads: a lever
    the program decides on.  A steerable tag that is only data-read (e.g. a
    never-written constant-table row an indirect copy indexes into) is program
    configuration, not an operator lever — rewriting it would probe a
    different program.
    """
    cone = ctx.pdg.upstream_slice(frontier_tag, follow_calls=True)
    condition_read = {
        tag for node in ctx.pdg.rung_nodes for tag in getattr(node, "condition_reads", ())
    }
    probes: set[ActionPair] = set()
    for tag in sorted(cone & ctx.steerable & condition_read):
        if tag in context:
            continue
        resting = ctx.resting.get(tag)
        if isinstance(resting, bool) or resting is None:
            probes.add((tag, not resting if resting is not None else True))
            continue
        domain = (ctx.nd_domains or {}).get(tag, ())
        if 0 < len(domain) <= _SKIFF_MAX_DOMAIN:
            cur = snap.get(tag)
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
