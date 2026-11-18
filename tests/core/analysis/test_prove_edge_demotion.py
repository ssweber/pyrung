"""Check that edge-source tags can leave PILOT's world key.

A tag in rise()/fall() whose exit value is scan-local need not be tracked.
Retained state and inputs decide that value, not the tag's own entry value.
Each edge contact keeps the bit it last saw in its own site memory
(``_edge:e{N}``).  The world key carries that memory.
"""

from __future__ import annotations

# copy(True/False, Bool) is intentional here — tests verify copy-vs-latch demotion semantics
from pyrung.core import (
    Bool,
    Program,
    fall,
    latch,
    out,
    rung,
)

# ===================================================================
# Group: Replay with demoted edge prev values
# ===================================================================


class TestDemotedEdgeReplay:
    """Checks that a how() plan through an edge on a tag left out of the key replays."""

    def test_how_replay_with_demoted_fall(self):
        """how() path through fall() on a demoted tag must replay correctly."""
        from pyrung.core.runner import PLC

        button = Bool("Button", external=True)
        sensor = Bool("Sensor")
        target = Bool("Target")
        with Program(strict=False) as logic:
            with rung(button):
                out(sensor)
            with rung(fall(sensor)):
                latch(target)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Button": True})
        plc.step()
        assert plc.state.tags["Sensor"] is True

        path = plc.how(target)
        assert path.reachable

        assert path.replay().state.tags["Target"] is True
