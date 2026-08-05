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
    needs_edge = any(t in edge_tags for t in patch)

    if needs_edge:
        release = {t: resting.get(t, False) for t in patch if t in edge_tags}
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
