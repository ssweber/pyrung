"""Shared avoid and hold-admission checks for PILOT execution boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_BARE_CHECK = "avoided condition"


class _AvoidCheckRaised(BaseException):
    """An avoid check raised on a real state, on its way out of the search to the caller.

    A BaseException, as ``SearchOutOfTime`` is: the search reads its causes under many
    ``except Exception`` that mean "no evidence", and a check that raised has not said a
    state is clear.  ``pilot_how`` and ``pilot_events`` turn it into the public
    ``AvoidCheckError``.
    """

    def __init__(self, check: str, error: Exception) -> None:
        super().__init__(check, error)
        self.check = check
        self.error = error


# Every ask of an avoid is one of two kinds, and a site picks by the state it holds.
#
# ON A REAL STATE: the state how() starts from, or a scan a trial really ran, failed trials
# included.  A raise there is an error, since the check has not said that state is clear:
# ``_member_on_scan``, ``_avoid_scan_names``.
#
# WEIGHED: anything else, such as values laid over a state, the resting baseline, or the
# state behind the start when a plan rewinds to re-run the entry scan.  A raise there says
# nothing about the route, so that member is not violated, and the others keep their
# answers: ``_member_weighed``, ``_avoid_weighed_names`` and what is built on them.


def _member_on_scan(check: str, pred: Callable[[dict[str, Any]], Any], state: Any) -> bool:
    """Whether *pred* holds on a real state.  A *pred* that raises gave no answer to read."""
    try:
        return bool(pred(state))
    except Exception as error:
        raise _AvoidCheckRaised(check, error) from error


def _member_weighed(pred: Callable[[dict[str, Any]], Any], state: Any) -> bool:
    """Whether *pred* holds on a state no scan need have had.  A raise is not violated."""
    try:
        return bool(pred(state))
    except Exception:
        return False


def _avoid_scan_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions a real state trips (``()`` when avoid is None).

    A ``_AvoidPredicate`` reports its violated member names; a bare callable
    (someone passed ``avoid_pred=`` a raw predicate) reports a generic name.
    """
    if avoid is None:
        return ()
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        return tuple(violated(snap))
    return (_BARE_CHECK,) if _member_on_scan(_BARE_CHECK, avoid, snap) else ()


def _avoid_weighed_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Names of the avoid conditions a weighed state trips, each member on its own."""
    if avoid is None:
        return ()
    members = getattr(avoid, "members", None)
    if members is None:
        return (_BARE_CHECK,) if _member_weighed(avoid, snap) else ()
    return tuple(member.name for member in members if _member_weighed(member.pred, snap))


def _avoid_weighed_after_clear(
    avoid: Any,
    start: dict[str, Any],
    observed: dict[str, Any],
) -> tuple[str, ...]:
    """Members clear at *start* that a weighed *observed* trips."""
    exempt = _avoid_weighed_names(avoid, start)
    return tuple(name for name in _avoid_weighed_names(avoid, observed) if name not in exempt)


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Names of the avoid conditions that *pairs* would force.

    Static: overlays each ``(tag, value)`` onto *snapshot* (or the resting
    baseline when no snapshot is given, the neutral world a hold asserts its
    tag against) and evaluates the avoid predicate. This is the action gate's
    primitive (a candidate/hold whose overlay trips avoid depends on it).

    What it asks is weighed.  The baseline has the steerable inputs and nothing else, so a
    callable can raise on it for a tag every real state has, and a hold laid over a state
    is no scan either: both go on, and the gates judge the scans they lead to.  A check
    that answers keeps its answer, so a condition reading a tag the baseline lacks still
    refuses the hold.
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    base = dict(snapshot) if snapshot is not None else dict(getattr(ctx, "resting", {}) or {})
    return _avoid_weighed_names(avoid, {**base, **dict(pairs)})


def _avoid_forces(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> bool:
    return bool(_avoid_violations(ctx, pairs, snapshot))


def _hold_allowed(ctx: Any, pair: tuple[str, Any]) -> bool:
    """Whether a hold of *pair* may be installed.  ``_avoid_violations`` says what is asked."""
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    blocked_actions = getattr(ctx, "blocked_actions", frozenset())
    if tag in action_tags or pair in blocked_actions:
        return False
    return not _avoid_forces(ctx, [pair])
