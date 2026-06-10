"""Physical layer glue: Harness installation on walk forks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Physical feedback: Harness on walk forks
# ---------------------------------------------------------------------------
# ``_do_jump`` bumps scan_id by *skip* (not 1), so the Harness's
# scan-indexed heap drains at the right time during folds.  Profile
# feedback tags are excluded from the plateau guard so their per-scan
# drift doesn't break fold detection.  ``PLC.fork()`` propagates the
# installed Harness, so every trial fork in ``_explore`` inherits it.


def _install_walk_harness(plc: PLC) -> frozenset[str]:
    """Install a :class:`~pyrung.core.harness.Harness` on *plc* if it has
    physical couplings, and return profile-feedback tag names.

    Profile-feedback names are excluded from the plateau guard so their
    per-scan drift doesn't break fold detection.  The installed Harness
    propagates to forks via ``PLC.fork()``, so every trial runner in
    ``_explore`` inherits it automatically.
    """
    from pyrung.core.harness import Harness

    harness = Harness(plc)
    harness.install()
    profile_fb_names: set[str] = set()
    has_couplings = False
    for coupling in harness.couplings():
        has_couplings = True
        if coupling.physical.feedback_type == "analog":
            profile_fb_names.add(coupling.fb_name)
    if not has_couplings:
        harness.uninstall()
    return frozenset(profile_fb_names)


def _install_replay_harness(plc: PLC, unlink: list[str] | None) -> None:
    """Mirror the work fork's physical model on a replay fork.

    The verify/annotate forks replay the plan step-by-step (no folding), so
    they need the same couplings as the work fork — including any ``unlink=``
    drops.  Without the unlink, a fault-scenario plan (walker forcing a
    feedback tag directly) would be validated against an intact physical
    chain whose synthesized feedback can conflict with the forced values.
    """
    from pyrung.core.harness import Harness

    harness = Harness(plc)
    harness.install()
    if unlink:
        harness.unlink(unlink)


def _harness_nearest_scan(plc: PLC) -> int | None:
    """Peek the installed Harness's heap for the nearest scheduled scan."""
    h = plc._harness
    if h is not None and h._heap:
        return h._heap[0].target_scan
    return None
