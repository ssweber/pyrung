"""Physical layer glue: Harness installation on PILOT forks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def install_harness(plc: PLC) -> frozenset[str]:
    """Install a :class:`~pyrung.core.harness.Harness` on *plc* and return
    all feedback tag names (bool + analog).

    Feedback tags are excluded from the steerable set — the Harness
    synthesizes them, PILOT must not steer them.  The installed Harness
    propagates to forks via ``PLC.fork()``.
    """
    from pyrung.core.harness import Harness

    harness = Harness(plc)
    harness.install()
    fb_names: set[str] = set()
    has_couplings = False
    for coupling in harness.couplings():
        has_couplings = True
        fb_names.add(coupling.fb_name)
    if not has_couplings:
        harness.uninstall()
    return frozenset(fb_names)
