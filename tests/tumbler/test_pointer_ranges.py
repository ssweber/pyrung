"""The index ranges declared in the tumbler's tags.

``tags.py`` is generated and these ranges are written by hand, so a regen drops them.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.bounds import build_constraint_index, check_bounds


def test_every_declared_index_may_rest_at_its_default(tumbler_logic: Any) -> None:
    """``never()`` refuses a program whose tag rests outside its declared bounds."""
    pdg = build_program_graph(tumbler_logic)
    constraints = build_constraint_index(pdg.tags)
    declared = {use.pointer for use in pdg.pointer_uses} & constraints.keys()
    assert "AlmHistorian__id_idx" in declared
    rests = {name: pdg.tags[name].default for name in declared}
    assert check_bounds(rests, constraints) == {}
