"""Dark-run current-world work-in-progress selection against PILOT routes.

This diagnostic wraps Orientation while a normal ``PLC.how`` drive runs.  The
real result is returned untouched.  Beside it, the tool retraces every current
root alternative, materializes its real immediate acts, and selects one using a
deterministic technician-shaped rule:

1. preserve an operation the program/PILOT already owns;
2. prefer an act on work visibly underway over fresh work;
3. use PILOT's existing option order to break the remaining tie.

There are no weights and no retained shadow route.  Work evidence is recomputed
from the current snapshot, installed rungs, pending departure, and the last
still-committed operation.  Existing route enumeration is scaffolding for the
experiment, not part of the proposed end state.

The tool does not execute the shadow act or claim an outcome advantage.  It
records disagreements for later paired replay.  ``shadow_scaffold_only`` is a
deliberate deletion blocker: it says the selected act was visible only because
the diagnostic retained the baseline route tree, not because an unlocked
current-world read reconstructed it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pyrung import PLC  # noqa: E402
from pyrung.core.analysis.pilot.navigation import (  # noqa: E402
    Bearing,
    Coast,
    Dwell,
    NavigationConstraints,
    OrientationResult,
    OrientationWorld,
    Pulse,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.trace import (  # noqa: E402
    TraceChoice,
    _all_nodes,
    rank_trace_choices,
)
from pyrung.core.analysis.sp_values import _values_match  # noqa: E402


@dataclass(frozen=True)
class WorkEvidence:
    """Discrete reasons that a current alternative is already underway."""

    reasons: tuple[str, ...] = ()

    @property
    def underway(self) -> bool:
        return bool(self.reasons)


@dataclass(frozen=True)
class ShadowOption:
    """One exact immediate act and the current-world facts used to order it."""

    identity: tuple[Any, ...]
    act: Any
    source: str
    route_label: str
    evidence: WorkEvidence
    hard_class: str
    ordinary_key: tuple[Any, ...]


_HARD_CLASS_ORDER = {
    # An exact current producer or already-owned coast is not interrupted by a
    # merely attractive action on another alternative.
    "owned": 0,
    "action": 1,
    "fallback": 2,
}


def shadow_order(option: ShadowOption) -> tuple[Any, ...]:
    """Deterministic order: ownership, underway/fresh, then existing evidence."""

    return (
        _HARD_CLASS_ORDER[option.hard_class],
        0 if option.evidence.underway else 1,
        option.ordinary_key,
        repr(option.identity),
    )


def select_shadow_option(options: list[ShadowOption]) -> ShadowOption | None:
    """Return one immediate act; no queue or alternative state is retained."""

    return min(options, key=shadow_order) if options else None


def _choice_label(choice: TraceChoice | None, source: str) -> str:
    if choice is None:
        return source
    return f"{choice.id}: {choice.label}"


def _tree_anchors(tree: Any, choice: TraceChoice | None) -> tuple[tuple[str, Any], ...]:
    """Concrete facts identifying work on one current traced alternative."""

    anchors: list[tuple[str, Any]] = []
    if choice is not None and choice.via_hint is not None:
        anchors.append(choice.via_hint)
    for node in _all_nodes(tree):
        if node.relational or node.value is None:
            continue
        pair = (node.tag, node.value)
        if pair not in anchors:
            anchors.append(pair)
    return tuple(anchors)


def _anchor_matches(
    anchors: tuple[tuple[str, Any], ...],
    tag: str,
    value: Any,
) -> bool:
    return any(
        anchor_tag == tag and _values_match(anchor_value, value)
        for anchor_tag, anchor_value in anchors
    )


def current_work_evidence(
    tree: Any,
    choice: TraceChoice | None,
    state: Any,
    snapshot: dict[str, Any],
) -> WorkEvidence:
    """Recognize work a technician can point to in the current world.

    History in ``journey`` is deliberately ignored.  A reverted act is not work
    underway.  Every reason below requires both a current alternative anchor and
    evidence still owned by the revertible world.
    """

    anchors = _tree_anchors(tree, choice)
    anchor_tags = {tag for tag, _value in anchors}
    reasons: list[str] = []

    for rung in getattr(state, "rungs", ()):
        tag = getattr(rung, "dest", None)
        value = getattr(rung, "value", None)
        if (
            tag is not None
            and _anchor_matches(anchors, tag, value)
            and _values_match(snapshot.get(tag), value)
        ):
            reasons.append(f"held:{tag}={value!r}")

    pending = getattr(state, "pending_departure", None)
    if pending is not None and pending.channel_tag in anchor_tags:
        current = snapshot.get(pending.channel_tag)
        if not _values_match(current, pending.from_value):
            reasons.append(f"pending:{pending.channel_tag}={current!r}")

    committed = tuple(getattr(state, "committed_acts", ()))
    if committed:
        context = committed[-1].context
        before = context.before_snap
        after = context.after_snap
        for tag, desired in anchors:
            if (
                tag in after
                and not _values_match(before.get(tag), after.get(tag))
                and _values_match(after.get(tag), desired)
                and _values_match(snapshot.get(tag), desired)
            ):
                reasons.append(f"established:{tag}={desired!r}")

        gauge = getattr(state, "gauge", None)
        if (
            gauge is not None
            and gauge.components
            and any(component.tag in anchor_tags for component in gauge.components)
            and gauge.ordinal_advanced(before, after)
        ):
            reasons.append("gauge:advanced")

    return WorkEvidence(tuple(dict.fromkeys(reasons)))


def _candidate_ordinary_key(
    candidate: Any, route_index: int, candidate_index: int
) -> tuple[Any, ...]:
    established = (
        candidate.route_prescribed or candidate.influence_prescribed or candidate.current_prescribed
    )
    prescription = 0 if established else 1 if candidate.program_prescribed else 2
    compass = candidate.compass_score or (0, 0)
    return (
        prescription,
        candidate.avail_tier or 0,
        int(bool(candidate.over_wake)),
        compass[0],
        compass[1],
        route_index,
        candidate_index,
    )


def _candidate_hard_class(candidate: Any) -> str:
    if candidate.current_prescribed or candidate.program_prescribed:
        return "owned"
    return "action"


def _options_from_result(
    result: Any,
    *,
    source: str,
    choice: TraceChoice | None,
    route_index: int,
) -> list[ShadowOption]:
    """Materialize every immediate Pulse plus the alternative's special act."""

    from pyrung.core.analysis.pilot.options import _candidate_applied

    trace = getattr(result, "trace", None)
    if trace is None:
        return []
    world = trace.world
    candidates = trace.candidates
    evidence = current_work_evidence(world.frame.tree, choice, world.state, world.snapshot)
    label = _choice_label(choice, source)
    result_options: list[ShadowOption] = []

    for candidate_index, candidate in enumerate(candidates.candidates):
        act = Pulse(
            candidate.pair,
            _candidate_applied(candidate, candidates, world.context),
            candidate,
        )
        identity = act_identity(act)
        if world.context.compass.knowledge.act_is_nogood(world.world_key, identity):
            continue
        result_options.append(
            ShadowOption(
                identity=identity,
                act=act,
                source=source,
                route_label=label,
                evidence=evidence,
                hard_class=_candidate_hard_class(candidate),
                ordinary_key=_candidate_ordinary_key(
                    candidate,
                    route_index,
                    candidate_index,
                ),
            )
        )

    # Waits, learned batches, widening, and terminal coast/dwell are minted by
    # Orientation rather than appearing as ordinary candidate objects.  Include
    # the exact selected act so the dark comparison uses executable identities.
    if isinstance(result, Bearing) and not isinstance(result.act, Pulse):
        act = result.act
        identity = act_identity(act)
        if not world.context.compass.knowledge.act_is_nogood(world.world_key, identity):
            hard_class = (
                "owned"
                if isinstance(act, Coast) and act.mode == "bearing"
                else "fallback"
                if isinstance(act, (Coast, Dwell))
                else "action"
            )
            result_options.append(
                ShadowOption(
                    identity=identity,
                    act=act,
                    source=source,
                    route_label=label,
                    evidence=evidence,
                    hard_class=hard_class,
                    ordinary_key=(route_index, -1),
                )
            )
    return result_options


def _fresh_constraints(constraints: NavigationConstraints) -> NavigationConstraints:
    return NavigationConstraints(
        blocked_actions=constraints.blocked_actions,
        avoid_predicate=constraints.avoid_predicate,
    )


def _read_alternative(
    original_orient: Callable[..., Any],
    compass: Any,
    raw_world: Any,
    baseline_world: Any,
    target: TargetSpec,
    constraints: NavigationConstraints,
    route: TraceChoice | None,
) -> Any:
    """Read one alternative without commitment, normalized to the baseline key."""

    from pyrung.core.analysis.pilot import orientation

    context = replace(
        raw_world.context,
        target_tag=target.tag,
        target_value=target.value,
        target_predicate=target.predicate,
        blocked_route_actions=constraints.blocked_actions,
        avoid_pred=constraints.avoid_predicate,
        route=route,
    )
    seed = replace(
        raw_world,
        context=context,
        frame=None,
        root_route=None,
    )
    fresh = _fresh_constraints(constraints)
    read = orientation._read_world(seed, target, fresh)
    read = replace(
        read,
        world_key=baseline_world.world_key,
        frame=replace(read.frame, key=baseline_world.world_key),
        key_config=baseline_world.key_config,
    )
    return original_orient(compass, read, target, fresh)


def _ranked_choices(
    baseline_world: Any,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> tuple[tuple[TraceChoice, Any], ...]:
    if target.predicate is not None:
        return ()
    ctx = baseline_world.context
    state = baseline_world.state
    rejected = frozenset()
    if state.key_config is not None:
        from pyrung.core.analysis.pilot.orientation import _exact_rejected_actions

        rejected = _exact_rejected_actions(
            ctx.compass.knowledge.nogood_identities(baseline_world.world_key)
        )
    _choices, ranked = rank_trace_choices(
        target.tag,
        target.value,
        baseline_world.snapshot,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        clear_only=ctx.clear_only,
        opaque_loop=ctx.opaque_loop,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        prior=ctx.domain_prior,
        avoid_pred=constraints.avoid_predicate,
        via_pred=ctx.via_pred,
        rejected_actions=rejected,
        harness=getattr(state.work, "_harness", None),
    )
    return ranked


class DarkRunObserver:
    """Installable read-only Orientation observer and structured report."""

    def __init__(self, *, strict: bool = False) -> None:
        self.strict = strict
        self.rows: list[dict[str, Any]] = []

    def compare(
        self,
        original_orient: Callable[..., Any],
        compass: Any,
        raw_world: Any,
        target: TargetSpec,
        constraints: NavigationConstraints,
        baseline: Any,
    ) -> dict[str, Any]:
        baseline_trace = getattr(baseline, "trace", None)
        if baseline_trace is None:
            raise RuntimeError("baseline Orientation result has no trace")
        baseline_world = baseline_trace.world
        baseline_choice = baseline_world.root_route

        alternatives: list[tuple[str, TraceChoice | None, Any]] = [
            ("baseline-scaffold", baseline_choice, baseline)
        ]
        alternatives.append(
            (
                "unlocked",
                None,
                _read_alternative(
                    original_orient,
                    compass,
                    raw_world,
                    baseline_world,
                    target,
                    constraints,
                    None,
                ),
            )
        )
        seen_routes: set[tuple[Any, ...]] = set()
        for route_index, (choice, _tree) in enumerate(
            _ranked_choices(baseline_world, target, constraints),
            start=1,
        ):
            identity = (choice.writer_locks, choice.or_locks)
            if identity in seen_routes:
                continue
            seen_routes.add(identity)
            alternatives.append(
                (
                    f"current-route-{route_index}",
                    choice,
                    _read_alternative(
                        original_orient,
                        compass,
                        raw_world,
                        baseline_world,
                        target,
                        constraints,
                        choice,
                    ),
                )
            )

        options: list[ShadowOption] = []
        for route_index, (source, choice, result) in enumerate(alternatives):
            options.extend(
                _options_from_result(
                    result,
                    source=source,
                    choice=choice,
                    route_index=route_index,
                )
            )
        selected = select_shadow_option(options)
        baseline_identity = act_identity(baseline.act) if isinstance(baseline, Bearing) else None
        shadow_identity = selected.identity if selected is not None else None
        non_scaffold_identities = {
            option.identity for option in options if option.source != "baseline-scaffold"
        }
        row = {
            "iteration": len(self.rows),
            "world_key": repr(baseline_world.world_key),
            "target": (target.tag, target.value),
            "baseline_result": type(baseline).__name__,
            "baseline_route": _choice_label(baseline_choice, "unlocked"),
            "baseline_identity": baseline_identity,
            "shadow_identity": shadow_identity,
            "agree": baseline_identity == shadow_identity,
            "shadow_source": selected.source if selected is not None else None,
            "shadow_route": selected.route_label if selected is not None else None,
            "shadow_evidence": selected.evidence.reasons if selected is not None else (),
            "shadow_scaffold_only": (
                selected is not None
                and selected.source == "baseline-scaffold"
                and selected.identity not in non_scaffold_identities
            ),
            "candidates": tuple(
                {
                    "identity": option.identity,
                    "source": option.source,
                    "route": option.route_label,
                    "hard_class": option.hard_class,
                    "work": option.evidence.reasons,
                    "ordinary_key": option.ordinary_key,
                }
                for option in sorted(options, key=shadow_order)
            ),
            "baseline_followup": [],
        }
        self.rows.append(row)
        return row

    def on_event(self, event: Any) -> None:
        if not self.rows:
            return
        if event.kind in {
            "candidate_accepted",
            "candidate_rejected",
            "zoom_accepted",
            "zoom_rejected",
            "batch_accepted",
            "batch_rejected",
            "widening_accepted",
            "widening_rejected",
            "trend_checkpoint",
            "route_exhausted",
            "route_unproductive",
            "stuck",
            "finished",
        }:
            self.rows[-1]["baseline_followup"].append(event.kind)

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Patch only the diagnostic process and always restore Orientation."""

        from pyrung.core.analysis.pilot import orientation

        original = orientation.orient

        def observed(
            compass: Any,
            world: OrientationWorld,
            target: TargetSpec,
            constraints: NavigationConstraints,
        ) -> OrientationResult:
            baseline = original(compass, world, target, constraints)
            try:
                self.compare(
                    original,
                    compass,
                    world,
                    target,
                    constraints,
                    baseline,
                )
            except Exception as exc:
                if self.strict:
                    raise
                self.rows.append(
                    {
                        "iteration": len(self.rows),
                        "baseline_result": type(baseline).__name__,
                        "shadow_error": f"{type(exc).__name__}: {exc}",
                        "baseline_followup": [],
                    }
                )
            return baseline

        orientation.orient = observed  # ty: ignore[invalid-assignment]
        try:
            yield
        finally:
            orientation.orient = original


def _parse_condition(plc: PLC, text: str) -> Any:
    from devtools.pilot_divergence import parse_target

    spec = parse_target(text)
    try:
        tag = plc._known_tags_by_name[spec.tag_name]
    except KeyError:
        raise ValueError(f"fixture has no tag named {spec.tag_name!r}") from None
    return tag == spec.value if spec.explicit_value else tag


def run_dark_drive(
    plc: PLC,
    target: Any,
    *,
    max_scans: int,
    avoid: Any = None,
    via: Any = None,
    strict: bool = False,
) -> tuple[Any, DarkRunObserver]:
    """Run unchanged PILOT behavior while collecting shadow decisions."""

    observer = DarkRunObserver(strict=strict)
    with observer.installed():
        plan = plc.how(
            target,
            max_scans=max_scans,
            avoid=avoid,
            via=via,
            on_event=observer.on_event,
        )
    return plan, observer


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare PILOT's route decision with current-world work-in-progress selection."
    )
    parser.add_argument("--fixture", default="tests.fixtures.tumbler")
    parser.add_argument("--target", required=True, help="Tag or Tag=JSON")
    parser.add_argument("--avoid", action="append", default=[], help="Tag or Tag=JSON")
    parser.add_argument("--via", action="append", default=[], help="Tag or Tag=JSON")
    parser.add_argument("--max-scans", type=int, default=400_000)
    parser.add_argument("--dt", type=float, default=0.010)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--show-agreements", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--fail-on-disagreement", action="store_true")
    return parser.parse_args(argv)


def _combine(conditions: list[Any]) -> Any:
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else tuple(conditions)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        fixture = importlib.import_module(args.fixture)
        plc = PLC(fixture.logic, dt=args.dt)
        plc.step()
        target = _parse_condition(plc, args.target)
        avoid = _combine([_parse_condition(plc, text) for text in args.avoid])
        via = _combine([_parse_condition(plc, text) for text in args.via])
        plan, observer = run_dark_drive(
            plc,
            target,
            max_scans=args.max_scans,
            avoid=avoid,
            via=via,
            strict=args.strict,
        )
    except (AttributeError, ImportError, TimeoutError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows = [
        row
        for row in observer.rows
        if args.show_agreements or not row.get("agree", False) or "shadow_error" in row
    ]
    rendered = "\n".join(
        json.dumps(row, sort_keys=True, default=str, ensure_ascii=False) for row in rows
    )
    if args.output is not None:
        args.output.write_text(rendered + ("\n" if rendered else ""), encoding="utf-8")
    elif rendered:
        print(rendered)

    errors = sum("shadow_error" in row for row in observer.rows)
    disagreements = sum(row.get("agree") is False for row in observer.rows)
    scaffold_only = sum(row.get("shadow_scaffold_only") is True for row in observer.rows)
    print(
        f"baseline={'reached' if plan.reachable else 'stopped'}; "
        f"orientations={len(observer.rows)}; disagreements={disagreements}; "
        f"scaffold-only={scaffold_only}; errors={errors}",
        file=sys.stderr,
    )
    if errors:
        return 2
    if args.fail_on_disagreement and disagreements:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
