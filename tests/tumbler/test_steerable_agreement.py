"""Agreement pin on a real program: ``core.analysis.steerable`` vs ``pilot.trace``.

The synthetic pins in ``tests/core/analysis/test_steerable.py`` cover each arm;
this one covers the shapes a hand-written fixture never thinks of (subroutines,
bulk fills, mode tables, 1697 tags).

DELETE when ``pilot/trace.py`` is collapsed onto ``core.analysis.steerable``.
"""

from __future__ import annotations

import pytest

from pyrung.core import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable

pytestmark = pytest.mark.tumbler


def test_steerable_agrees_on_the_real_tumbler(tumbler_logic) -> None:
    from pyrung.core.analysis.pilot.trace import compute_steerable as trace_steerable

    plc = PLC(tumbler_logic)
    pdg = build_program_graph(tumbler_logic)
    known = plc._known_tags_by_name

    mine = compute_steerable(pdg, known, tumbler_logic)
    theirs = trace_steerable(pdg, known, tumbler_logic)

    assert mine == theirs
    assert mine, "a real program must yield some steerable tags — a vacuous pass proves nothing"


def test_clear_only_agrees_on_the_real_tumbler(tumbler_logic) -> None:
    from pyrung.core.analysis.pilot.trace import compute_clear_only as trace_clear_only

    plc = PLC(tumbler_logic)
    pdg = build_program_graph(tumbler_logic)
    known = plc._known_tags_by_name

    assert compute_clear_only(pdg, known, tumbler_logic) == trace_clear_only(
        pdg, known, tumbler_logic
    )
