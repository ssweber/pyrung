"""Apply one PILOT pulse with edge-aware scan semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.coast import LIMITS, CoastSession

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def _apply_pulse(
    plc: PLC,
    actions: list[tuple[str, Any]],
    resting: dict[str, Any],
    edge_tags: set[str],
    session: Any = None,
    *,
    rearm_tags: set[str] | frozenset[str] = frozenset(),
) -> int:
    """Apply *actions* with rising-edge semantics where needed.

    Returns the number of scans consumed. *session*, when given, records the
    pulse onto that session's timeline (pens ticked after every raw scan, the
    settle dwell run on the session) so a Done that fires inside the pulse
    window is a recorded pen mark, not history-only.
    """
    if session is None:
        session = CoastSession(plc, kind="pulse")
    assert session.plc is plc

    patch = {t: v for t, v in actions}
    source = plc.state.tags

    def _needs_release(tag: str, value: Any) -> bool:
        rest = resting.get(tag, False)
        return bool(
            (tag in edge_tags or tag in rearm_tags)
            and not _values_match(value, rest)
            and (tag not in rearm_tags or not _values_match(source.get(tag), rest))
        )

    from pyrung.core.analysis.sp_values import _values_match

    needs_edge = any(_needs_release(tag, value) for tag, value in patch.items())

    if needs_edge:
        release = {
            tag: resting.get(tag, False)
            for tag, value in patch.items()
            if _needs_release(tag, value)
        }
        if release:
            plc.patch(release)
            session.step_kernel()
            session.note_pens()

    plc.patch(patch)
    session.step_kernel()
    session.note_pens()

    # Fixed settle window — the one waiting shape with no predicate (an
    # explicit dwell, never disguised as a trigger).
    session.dwell(LIMITS.pulse_settle_scans)
    return 6 if needs_edge else 5
