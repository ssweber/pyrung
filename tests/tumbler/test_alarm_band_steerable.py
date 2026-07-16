"""Regression: silent alarm-band housekeeping words are not steerable levers.

The tumbler clears its whole ``A_Alm*_Status`` / ``A_Warn*_Status`` band with one
bulk ``fill(0, ds.select(201, 350))`` (main R74).  The unused slots' *only* writer
is that bulk clear, so they used to classify as ack-cleared operator commands and
leak into the steerable set — 88 ``A_Alm*_Status`` words that then headlined the
unreachable decline ``frontier Sts_StateCurrent=6 is gated by free word
'A_Alm10_Status'``.  Slot 10 was an arbitrary lexicographic artifact.

A multi-slot bulk fill is the program's own housekeeping, not a per-register ack,
so no band member is clear-only and none is steerable or a free-word decline
culprit.
"""

from __future__ import annotations

import pytest

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.skiff import _frontier_free_words
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


def test_free_word_decline_does_not_headline_alarm_slot(tumbler_logic) -> None:
    """``_frontier_free_words(Sts_StateCurrent)`` must not caption an alarm-band word.

    The ``A_AlmExtent == 0`` gate holds when the band rests at zero; a housekeeping
    band member is never the blocking free lever.
    """
    from types import SimpleNamespace

    plc = PLC(tumbler_logic, dt=0.010)
    pdg = build_program_graph(tumbler_logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, tumbler_logic)
    known = plc._known_tags_by_name
    ctx = SimpleNamespace(
        pdg=pdg,
        steerable=steerable,
        resting={t: getattr(known.get(t), "default", None) for t in steerable},
    )

    free = _frontier_free_words("Sts_StateCurrent", ctx)
    offenders = [w for w in free if w.startswith("A_Alm") or w.startswith("A_Warn")]
    assert not offenders, f"alarm/warn band words headline the decline: {offenders[:5]}"
