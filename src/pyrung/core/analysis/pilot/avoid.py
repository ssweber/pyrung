"""Shared avoid and hold-admission checks for PILOT execution boundaries."""

from __future__ import annotations

from typing import Any


def _avoid_snap_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions *snap* trips (``()`` when avoid is None).

    A ``_AvoidPredicate`` reports its violated member names; a bare callable
    (someone passed ``avoid_pred=`` a raw predicate) reports a generic name.
    """
    if avoid is None:
        return ()
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        try:
            return tuple(violated(snap))
        except Exception:
            return ()
    try:
        return ("avoided condition",) if bool(avoid(snap)) else ()
    except Exception:
        return ()


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given — the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate. This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    base = dict(snapshot) if snapshot is not None else dict(getattr(ctx, "resting", {}) or {})
    for tag, value in pairs:
        base[tag] = value
    return _avoid_snap_names(avoid, base)


def _avoid_forces(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    return bool(_avoid_violations(ctx, pairs, snapshot))


def _hold_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    blocked_actions = getattr(ctx, "blocked_actions", frozenset())
    if tag in action_tags or pair in blocked_actions:
        return False
    return not _avoid_forces(ctx, [pair])
