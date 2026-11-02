"""The index ranges declared in the tumbler's tags keep its long dwells folding.

``tags.py`` is generated and these ranges are written by hand, so a regen drops
them.  Without them this is the first test to say so; the routes in
``test_pilot_golden_skeleton.py`` only run out of time.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.bounds import build_constraint_index, check_bounds
from pyrung.core.fold import _slots_read_through_pointers


def test_no_unbounded_read_reaches_the_timer_display(tumbler_logic: Any) -> None:
    pdg = build_program_graph(tumbler_logic)
    assert "HMI_TimeLeft_Tm" in pdg.writers_of
    assert "HMI_TimeLeft_Tm" not in _slots_read_through_pointers(pdg, tumbler_logic)


def test_every_declared_index_may_rest_at_its_default(tumbler_logic: Any) -> None:
    """``never()`` refuses a program whose tag rests outside its declared bounds."""
    pdg = build_program_graph(tumbler_logic)
    constraints = build_constraint_index(pdg.tags)
    declared = {use.pointer for use in pdg.pointer_uses} & constraints.keys()
    assert "AlmHistorian__id_idx" in declared
    rests = {name: pdg.tags[name].default for name in declared}
    assert check_bounds(rests, constraints) == {}
