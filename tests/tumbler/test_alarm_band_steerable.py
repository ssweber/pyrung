"""Regression: silent alarm-band housekeeping words are not steerable levers.

The tumbler clears its whole ``A_Alm*_Status`` / ``A_Warn*_Status`` band with one
bulk ``fill(0, ds.select(201, 350))`` (main R74).  The unused slots' *only* writer
is that bulk clear, so they used to classify as ack-cleared operator commands and
leak into the steerable set as 88 bogus ``A_Alm*_Status`` operator inputs.

A multi-slot bulk fill is the program's own housekeeping, not a per-register ack,
so no band member is clear-only or steerable.
"""

from __future__ import annotations

import pytest

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.steerable import compute_steerable

pytestmark = pytest.mark.tumbler


def test_alarm_status_band_not_steerable(tumbler_logic) -> None:
    plc = PLC(tumbler_logic, dt=0.010)
    pdg = build_program_graph(tumbler_logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, tumbler_logic)

    alarm_words = sorted(t for t in steerable if t.startswith("A_Alm") and t.endswith("_Status"))
    assert alarm_words == [], f"alarm-band housekeeping words leaked as levers: {alarm_words}"
    warn_words = sorted(t for t in steerable if t.startswith("A_Warn") and t.endswith("_Status"))
    assert warn_words == [], f"warn-band housekeeping words leaked as levers: {warn_words}"
