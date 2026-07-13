"""Skiff scans for opaque transition pipelines.

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

from pyrung.core.analysis.pilot.causal import empirical_program_writes, pilot_touched_tags
from pyrung.core.analysis.pilot.compass import CompassObservation, _action_sort_key
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
    return run_pinned_scan(plc, allowed, pdg, actions=actions, scans=scans)


def run_pinned_scan(
    plc: PLC,
    allowed_tags: frozenset[str],
    pdg: ProgramGraph,
    *,
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

    fork = plc.fork()
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
) -> tuple[CompassObservation, ...]:
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
    register moved.  An observed move is an ``"edge"`` observation (a pair
    carries a *composite* cause — a tuple of action pairs); a still stand is
    ``"no_change"`` so the same probe is never re-sent.

    Returns the NEW observations — the skiff never writes the compass itself;
    the caller applies them at its RECORD point (an empty return means
    genuinely stuck).  Honesty invariant: a learned edge is a *bearing* only —
    it surfaces as a prescribed candidate (or prescribed batch, for a
    composite) on the next iteration and must be confirmed live through the
    verify pipeline.  Nothing here commits a plan step.
    """
    from pyrung.core.analysis.pilot.trace import _all_nodes

    frontiers = []
    seen_frontier: set[tuple[str, str]] = set()
    for n in _all_nodes(frame.tree):
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
    # sound probe lever and must not headline a free-word decline.  Restricted to
    # the frontier cones' steerable words (cheap) over the whole recorded run.
    cone_steerable: set[str] = set()
    for node in frontiers:
        cone_steerable.update(ctx.pdg.upstream_slice(node.tag, follow_calls=True) & ctx.steerable)
    empirical_writes = empirical_program_writes(
        state.work,
        frozenset(cone_steerable),
        start_scan=0,
        end_scan=getattr(getattr(state.work, "state", None), "scan_id", 0) or 0,
        pilot_touched=pilot_touched_tags(
            getattr(state, "hold_log", ()),
            getattr(state, "journey", ()),
            tuple(r.dest for r in getattr(state, "rungs", ())),
        ),
    )

    # Honest decline: an unreadable frontier whose upstream cone holds a free
    # word (steerable, no declared complete domain) has no sound probe values.
    # Name the tag and nudge a ``choices=`` declaration so the miss is specific,
    # not a generic ``stuck: <reason>``.  The terminal stuck exit prefers this.
    for node in frontiers:
        free_words = _frontier_free_words(node.tag, ctx, empirical_writes)
        if free_words:
            word = free_words[0]
            state.skiff_declines.setdefault(
                frame.key,
                f"pilot: unreachable — frontier {node.tag}={node.value!r} is gated by "
                f"free word {word!r} (external, no declared domain); the skiff has no "
                f"sound probe values for it. Declare choices= (or min=/max=) on {word} "
                f"so the prover, bounds, and skiff can resolve it.",
            )
            break

    # Context: the readable half of the bearing.  A joint requirement
    # (command + config select) is only observable when the known-steerable
    # trace actions ride along with the probe.
    context: dict[str, Any] = {}
    for tag, value in frame.tree.ordered_actions():
        if tag in ctx.steerable and ctx.route_allowed((tag, value)):
            context.setdefault(tag, value)

    observations: list[CompassObservation] = []
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
            state.work, allowed, ctx.pdg, actions=tuple(context.items()), scans=scans
        )
        if not _values_match(control.after.get(node.tag), cur_val):
            continue

        # Pass 1: single actions.
        edge_found = False
        budget = max_probes
        for probe in ctx.compass.unprobed_actions(node.tag, cur_val, set(singles))[:budget]:
            budget -= 1
            obs = _send_probe(
                node.tag, cur_val, (probe,), probe, context, allowed, state, ctx, scans
            )
            edge_found |= obs.kind == "edge"
            observations.append(obs)

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
                observations.append(
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
                )
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
    scans: int,
) -> CompassObservation:
    """Run one isolated probe and return the observation — applied at RECORD."""
    actions = dict(context)
    actions.update(probe_actions)
    result = run_pinned_scan(
        state.work, allowed, ctx.pdg, actions=tuple(actions.items()), scans=scans
    )
    new_val = result.after.get(frontier_tag)
    if not _values_match(new_val, cur_val):
        return CompassObservation("edge", frontier_tag, cause, cur_val, new_val)
    return CompassObservation("no_change", frontier_tag, cause, cur_val)


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
    data word offers no sound probe values and is left for the honest decline
    (that tier needs a ``choices=`` declaration, not a guess).
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


def _frontier_free_words(
    frontier_tag: str, ctx: Any, empirical_writes: frozenset[str] = frozenset()
) -> list[str]:
    """Steerable **word** tags in the frontier's upstream cone that carry no
    declared complete domain — the free words the skiff cannot probe soundly.

    These are the honest-decline culprits: an unreadable frontier gated by such a
    word has no sound probe values, so the fix is a domain *declaration*
    (``choices=`` / ``min=``/``max=``) — the single source of truth the prover,
    bounds checks, validators, and the skiff all read — not a ``how()``-only
    guess.  Dispatches purely on domain completeness, never on tag names.

    *empirical_writes* (the empirical steerable veto) drops words the recorded run
    shows the program wrote: a program-authored status word is not a free operator
    lever, so it must not headline the decline (positive evidence only — empty is
    the prior behavior).
    """
    cone = ctx.pdg.upstream_slice(frontier_tag, follow_calls=True)
    words: list[str] = []
    for tag in sorted(cone & ctx.steerable):
        if tag in empirical_writes:
            continue  # recorded run shows the program wrote it — not a free lever
        resting = ctx.resting.get(tag)
        if isinstance(resting, bool) or resting is None:
            continue  # a Bool, not a word
        if _declared_domain(ctx.pdg.tags.get(tag)) is not None:
            continue  # declared complete domain — probeable, not a free word
        words.append(tag)
    return words


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
