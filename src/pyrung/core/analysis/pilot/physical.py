"""Install physical-feedback harnesses and identify non-steerable feedback tags."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def upstream_through_plant(pdg: Any, harness: Any, tags: Iterable[str]) -> frozenset[str]:
    """Every tag that can reach *tags*: through the program, and through the plant.

    The program graph's upstream slice stops at a harness feedback, which no rung
    writes. Its enable drives it, so the cone goes on from the enable.
    """
    enables: dict[str, set[str]] = {}
    if harness is not None:
        for coupling in harness.couplings():
            enables.setdefault(coupling.fb_name, set()).add(coupling.en_name)
    cone: set[str] = set()
    pending = list(tags)
    seen: set[str] = set()
    while pending:
        tag = pending.pop()
        if tag in seen:
            continue
        seen.add(tag)
        upstream = pdg.upstream_slice(tag, follow_calls=True)
        cone |= upstream
        for name in (*upstream, tag):
            for enable in enables.get(name, ()):
                cone.add(enable)
                pending.append(enable)
    return frozenset(cone)


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

    Overrides on a harness *plc* already carries (a debug session's bench
    links, inherited by the fork) carry over, so PILOT plans with the same
    wiring the session runs.  *plc* stands in for the session it forks, so nothing
    settles here: a plan starts from the session's own counts, or from cold if the
    session has no harness.
    """
    from pyrung.core.harness import Harness

    previous = plc._harness
    harness = Harness(plc)
    if previous is not None and previous.overrides:
        harness.set_overrides(previous.overrides)
    harness._install(settle=False)
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
