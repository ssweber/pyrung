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

    if scans < 1:
        raise ValueError("scans must be >= 1")

    fork = plc.fork()
    before = dict(fork.state.tags)
    allowed = participating_tags_for_sandbox(
        role,
        routes=routes,
        actions=actions,
        extra_tags=extra_tags,
    )
    force_map = _sandbox_force_map(fork, before, allowed, pdg)
    scan_before = fork.state.scan_id

    with fork.forced(force_map):
        if actions:
            fork.patch(dict(actions))
        for _ in range(scans):
            fork.step()

    after = dict(fork.state.tags)
    return SandboxResult(
        allowed_tags=allowed,
        forced_tags=frozenset(force_map),
        actions=actions,
        scan_before=scan_before,
        scan_after=fork.state.scan_id,
        before=before,
        after=after,
        participating_changes=_diff(before, after, tags=allowed),
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
