"""Shared avoid and hold-admission checks for PILOT execution boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_BARE_CHECK = "avoided condition"


class _AvoidCheckRaised(BaseException):
    """An avoid check raised, on its way out of the search to the caller.

    A BaseException, as ``SearchOutOfTime`` is: the search reads its causes under many
    ``except Exception`` that mean "no evidence", and a check that raised has not said a
    state is clear.  ``pilot_how`` and ``pilot_events`` turn it into the public
    ``AvoidCheckError``.
    """

    def __init__(self, check: str, error: Exception) -> None:
        super().__init__(check, error)
        self.check = check
        self.error = error


def _avoid_member_true(check: str, pred: Callable[[dict[str, Any]], Any], state: Any) -> bool:
    """Whether *pred* holds on *state*.  A *pred* that raises gave no answer to read."""
    try:
        return bool(pred(state))
    except Exception as error:
        raise _AvoidCheckRaised(check, error) from error


def _avoid_snap_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions *snap* trips (``()`` when avoid is None).

    A ``_AvoidPredicate`` reports its violated member names; a bare callable
    (someone passed ``avoid_pred=`` a raw predicate) reports a generic name.
    """
    if avoid is None:
        return ()
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        return tuple(violated(snap))
    return (_BARE_CHECK,) if _avoid_member_true(_BARE_CHECK, avoid, snap) else ()


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
    real: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given — the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate. This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).  *real* is
    the state a judgment on the baseline falls back on (``_resting_violations``).
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    if snapshot is None:
        return _resting_violations(ctx, avoid, pairs, real or {})
    return _avoid_snap_names(avoid, {**snapshot, **dict(pairs)})


def _resting_violations(
    ctx: Any,
    avoid: Any,
    pairs: Any,
    real: Mapping[str, Any],
) -> tuple[str, ...]:
    """Names of the avoid conditions *pairs* would force from the resting baseline.

    The baseline has the steerable inputs and nothing else, so a check can raise on it
    for a tag every real state has.  That is no answer about the hold, so that check is
    asked again with real values: *real*, the state the search is judging the hold from,
    with resting filling only what it lacks.  The hold forces it if it goes true with the
    hold on and was clear without.  A raise there is the check's own and reaches the
    caller.  A check the baseline can answer keeps that answer, so a condition reading a
    tag the baseline lacks still refuses the hold.
    """
    resting = dict(getattr(ctx, "resting", {}) or {})
    held = dict(pairs)
    members = getattr(avoid, "members", None)
    checks = [(_BARE_CHECK, avoid)] if members is None else [(m.name, m.pred) for m in members]

    def forces(check: str, pred: Any) -> bool:
        try:
            return _avoid_member_true(check, pred, {**resting, **held})
        except _AvoidCheckRaised:
            now = {**resting, **real}
            return _avoid_member_true(check, pred, {**now, **held}) and not _avoid_member_true(
                check, pred, now
            )

    return tuple(check for check, pred in checks if forces(check, pred))


def _avoid_forces(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
    real: Mapping[str, Any] | None = None,
) -> bool:
    return bool(_avoid_violations(ctx, pairs, snapshot, real))


def _hold_allowed(
    ctx: Any,
    pair: tuple[str, Any],
    real: Mapping[str, Any] | None = None,
) -> bool:
    """Whether a hold of *pair* may be installed, judged from the real state *real*."""
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    blocked_actions = getattr(ctx, "blocked_actions", frozenset())
    if tag in action_tags or pair in blocked_actions:
        return False
    return not _avoid_forces(ctx, [pair], real=real)
