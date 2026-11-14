"""The index ranges declared in the tumbler's tags.

``tags.py`` is generated and these ranges are written by hand, so a regen drops them.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.bounds import build_constraint_index, check_bounds

# The ranges written by hand in tests/fixtures/tumbler/tags.py.
HAND_WRITTEN = {
    "z_AckAndClearAllAlm_idx",
    "sm_JMP__target_ds_idx",
    "AlmHistorian__id_idx",
    "AlmHistorian__subid_idx",
    "AlmHistorian__stepid_idx",
    "AlmHistorian__val_idx",
    "AlmHistorian__cat_idx",
    "AlmHistorian__MMDD_idx",
    "AlmHistorian__hhmm_idx",
    "AlmHistorian__ss_idx",
    "AlmHistorian__ack_idx",
    "AlmHistorian__status_idx",
}


def test_every_declared_index_may_rest_at_its_default(tumbler_logic: Any) -> None:
    """``never()`` refuses a program whose tag rests outside its declared bounds."""
    pdg = build_program_graph(tumbler_logic)
    constraints = build_constraint_index(pdg.tags)
    pointers = {use.pointer for use in pdg.pointer_uses}
    assert HAND_WRITTEN <= pointers
    assert HAND_WRITTEN <= constraints.keys()
    declared = pointers & constraints.keys()
    rests = {name: pdg.tags[name].default for name in declared}
    assert check_bounds(rests, constraints) == {}
