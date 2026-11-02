"""The index ranges declared in the tumbler's tags keep its long dwells folding.

``tags.py`` is generated and these ranges are written by hand, so a regen drops
them.  Without them this is the first test to say so; the routes in
``test_pilot_golden_skeleton.py`` only run out of time.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.fold import _slots_read_through_pointers


def test_no_unbounded_read_reaches_the_timer_display(tumbler_logic: Any) -> None:
    pdg = build_program_graph(tumbler_logic)
    assert "HMI_TimeLeft_Tm" not in _slots_read_through_pointers(pdg, tumbler_logic)
