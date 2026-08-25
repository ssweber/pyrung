"""Install physical-feedback harnesses and identify non-steerable feedback tags."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def install_harness(plc: PLC, unlink: list[str] | None = None) -> frozenset[str]:
    """Install a :class:`~pyrung.core.harness.Harness` on *plc* and return
    the **synthesized** feedback tag names (bool + analog).

    Feedback tags are excluded from the steerable set — the Harness
    synthesizes them, PILOT must not steer them.  The installed Harness
    propagates to forks via ``PLC.fork()``.

    ``unlink`` models a broken sensor for fault injection: the named feedback
    tags have their coupling removed (``Harness.unlink``) so the Harness no
    longer drives them, and they are dropped from the returned set so they
    become free, steerable inputs.  This is how ``how(..., unlink=[...])``
    lets PILOT reach a fault that is otherwise unreachable while the link
    holds the sensor lockstep with its driver.
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
    elif unlink:
        # Defeat the named couplings: the Harness stops synthesizing them, and
        # dropping them from ``fb_names`` keeps them in the steerable set.
        harness.unlink(unlink)
        fb_names -= set(unlink)
    return frozenset(fb_names)
