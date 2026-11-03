"""Shared avoid and hold-admission checks for PILOT execution boundaries."""

from __future__ import annotations

from collections.abc import Callable
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
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given — the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate. This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).

    The resting baseline has the steerable inputs and nothing else, so a check may raise
    on it for a tag it reads and a real state has.  That says nothing against the check,
    and nothing clear about the hold: the hold is refused, as it is for a condition that
    reads a tag the baseline lacks.  A check raising on a real snapshot raises from here.
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    base = dict(snapshot) if snapshot is not None else dict(getattr(ctx, "resting", {}) or {})
    for tag, value in pairs:
        base[tag] = value
    try:
        return _avoid_snap_names(avoid, base)
    except _AvoidCheckRaised as raised:
        if snapshot is not None:
            raise
        return (raised.check,)


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
