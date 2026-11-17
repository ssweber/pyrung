"""Shared avoid and hold-admission checks for PILOT execution boundaries."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_BARE_CHECK = "avoided condition"


class _AvoidCheckRaised(BaseException):
    """Carries an error from an avoid check on a real state out of the search to the caller.

    This is a BaseException, as ``SearchOutOfTime`` is.  The search reads its causes under
    many ``except Exception`` blocks that mean "no evidence", and a check that raised has not
    said that a state is clear.  ``pilot_how`` and ``pilot_events`` turn it into the public
    ``AvoidCheckError``.
    """

    def __init__(self, check: str, error: Exception) -> None:
        super().__init__(check, error)
        self.check = check
        self.error = error


# Every ask of an avoid is one of two kinds.  A call site picks the kind by the state it holds.
#
# ON A REAL STATE: the state how() starts from, or a scan that a trial really ran, including
# failed trials.  If the check raises there, that is an error, because the check has not said
# that the state is clear.  The functions are ``_member_on_scan`` and ``_avoid_scan_names``.
#
# WEIGHED: any other state, such as values laid over a state, the resting baseline, or the
# state that a restart lands on before its first scan.  If the check raises there, it says
# nothing about the route.  That member counts as not violated, and the other members keep
# their answers.  The functions are ``_member_weighed``, ``_avoid_weighed_names`` and what
# is built on them.


def _member_on_scan(check: str, pred: Callable[[dict[str, Any]], Any], state: Any) -> bool:
    """Return whether *pred* holds on a real state.  A *pred* that raises gave no answer."""
    try:
        return bool(pred(state))
    except Exception as error:
        raise _AvoidCheckRaised(check, error) from error


def _member_weighed(pred: Callable[[dict[str, Any]], Any], state: Any) -> bool:
    """Return whether *pred* holds on a state that no scan need have had.

    A *pred* that raises counts as not violated.
    """
    try:
        return bool(pred(state))
    except Exception:
        return False


def _avoid_scan_names(avoid: Any, snap: dict[str, Any]) -> tuple[str, ...]:
    """Return the names of the avoid conditions that a real state trips.

    Return ``()`` when avoid is None.

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
    """Return the names of the avoid conditions that a weighed state trips, each member alone."""
    if avoid is None:
        return ()
    members = getattr(avoid, "members", None)
    if members is None:
        return (_BARE_CHECK,) if _member_weighed(avoid, snap) else ()
    return tuple(
        member.name
        for member in members
        # A member that reads a tag it cannot name never trips a weighed state.  A condition
        # would read a tag that the state lacks as violated, and a callable would raise.  So a
        # member trips only when the state has every tag that it reads and its check says yes.
        if member.tags is not None
        and all(tag in snap for tag in member.tags)
        and _member_weighed(member.pred, snap)
    )


def _avoid_weighed_after_clear(
    avoid: Any,
    start: dict[str, Any],
    observed: dict[str, Any],
) -> tuple[str, ...]:
    """Return the members that are clear at *start* and that a weighed *observed* trips."""
    exempt = _avoid_weighed_names(avoid, start)
    return tuple(name for name in _avoid_weighed_names(avoid, observed) if name not in exempt)


def _avoid_violations(
    ctx: Any,
    pairs: list[tuple[str, Any]] | tuple[tuple[str, Any], ...],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return the names of the avoid conditions that *pairs* would force.

    This is a static check.  It overlays each ``(tag, value)`` onto *snapshot* and evaluates
    the avoid predicate.  When no snapshot is given, it uses the resting baseline, the
    neutral world that a hold asserts its tag against.  This is the action gate's primitive,
    because a candidate or hold whose overlay trips avoid depends on it.

    The state it asks about is weighed.  The baseline has the steerable inputs and nothing
    else, so a callable can raise on it for a tag that every real state has.  A hold laid over
    a state is not a scan either.  In both cases, a check that raises counts as not violated,
    and the candidate or hold goes on.  The gates judge the scans that it leads to.  A check
    that answers keeps its answer, so a check that reads only what the baseline has can still
    refuse the hold.
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
    """Return whether a hold of *pair* may be installed.

    ``_avoid_violations`` describes what is asked.
    """
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    blocked_actions = getattr(ctx, "blocked_actions", frozenset())
    if tag in action_tags or pair in blocked_actions:
        return False
    return not _avoid_forces(ctx, [pair])
