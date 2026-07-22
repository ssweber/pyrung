"""Historical end-to-end gate for the completion-trace wait passage.

The BurnerLoop decision skeleton owns the exact ``x_RotateFB`` completion
frontier.  This test retains the independent cold-boot reachability gate that
originally proved the silent Starting-to-Execute hang was fixed.
"""

from __future__ import annotations

import time

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events

pytestmark = pytest.mark.tumbler

GATE_MAX_SCANS = 20_000
GATE_WALL_BUDGET_S = 240.0


def test_cold_boot_how_y_burnerloop_completes(tumbler_logic) -> None:
    """The silent-hang fixture end-to-end — the wait-edge design's phase-3 gate.

    Born strict-xfail (shipyard rule); flipped when the full passage landed:
    the doors round (investigation), FB permissives via completion-trace holds,
    Starting -> Execute through the wait edge, the guard-aware investigation
    re-earning the door hold for the Execute era, the rotate-sensor liveness
    round, and the heat cascade to the burner loop.  Each era's correction is
    earned through its own incident and survives later reverts (the banked
    checkpoint) — a hold solves one bump, not the passage.
    """
    plc = PLC(tumbler_logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]

    deadline = time.monotonic() + GATE_WALL_BUDGET_S
    finished = None
    for event in pilot_events(plc, target, max_scans=GATE_MAX_SCANS):
        if event.kind == "finished":
            finished = dict(event.data)
            break
        if time.monotonic() > deadline:
            pytest.fail(
                f"cold-boot how(y_BurnerLoop) exceeded the {GATE_WALL_BUDGET_S:.0f}s "
                f"wall budget at scan {event.scan} (kind={event.kind})"
            )

    assert finished is not None and finished["reached"] is True, (
        f"burner loop not reached: {finished.get('reason') if finished else 'no finished event'!r}"
    )
