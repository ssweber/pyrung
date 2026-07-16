"""The hang-forever survey flags exactly the deliberate rotate bug.

Ground truth (``subroutines/rotate.py``): step 1 advances only when the
external contactor feedback ``i_RotateFB`` arrives (R8), the R9 error escape
guards ``CurStep == 3`` (blower's counterpart guards ``>= 1``), and the R5
timeout is disabled by ``Rotate_EnableLimit = 0``.  So rotate step 1 can hang
forever; blower cannot.
"""

from __future__ import annotations

import pytest

from pyrung.core import PLC
from pyrung.core.analysis.query import wait_edges_without_escape

pytestmark = pytest.mark.tumbler


def test_survey_flags_exactly_rotate_step_1(tumbler_logic) -> None:
    findings = wait_edges_without_escape(tumbler_logic)

    assert len(findings) == 1
    f = findings[0]
    assert f.subroutine == "Rotate"
    assert f.step_register == "Rotate_CurStep"
    assert f.step_value == 1
    assert f.wait_inputs == ("i_RotateFB",)
    assert f.advance_rung == "R8"


def test_finding_message_names_input_range_and_unmet_timeout(tumbler_logic) -> None:
    message = wait_edges_without_escape(tumbler_logic)[0].message

    # Waits on the feedback the program cannot supply, with no escape.
    assert "Rotate step 1 waits on i_RotateFB with no escape" in message
    # The mis-ranged R9 error rung.
    assert "R9 guards Rotate_CurStep == 3" in message
    # The R5 timeout, gated on a register nothing sets.
    assert "R5 needs Rotate_EnableLimit, which nothing sets (rests at 0)" in message


def test_blower_step_is_not_flagged(tumbler_logic) -> None:
    flagged = {f.subroutine for f in wait_edges_without_escape(tumbler_logic)}
    assert "Blower" not in flagged


def test_query_namespace_exposes_the_survey(tumbler_logic) -> None:
    plc = PLC(tumbler_logic)
    findings = plc.query.wait_edges_without_escape()
    assert [f.subroutine for f in findings] == ["Rotate"]
