"""ORIENT re-reads the wait — the completion-trace gate (design Part 2).

The tumbler's cold-boot ``how(y_BurnerLoop)`` is the silent-hang fixture: a
prescribed Starting(3)->Execute(6) WAIT edge whose completion condition
(``Sts_StateCompleteFlag``) descends five transparent hops behind the pipeline
cut to the dead contactor feedback ``x_RotateFB``
(``production_states`` R3 -> ``Rotate__init`` <- ``rotate`` R22 <- R20 <- R8 <-
``i_RotateFB`` <- ``x_RotateFB``).

Before Part 2 the wait was a terminal: the trace stopped at the pipeline
boundary and the drive stalled blind, its frontier clause naming the Heat
subtree instead of the true blocker.  Part 2 re-reads the completion every
ORIENT: its steerable producers enter the ordinary trace pool (the
self-advancing FB permissives are held for the coast) and its unmet frontier
names ``x_RotateFB``.

Honesty note on the "terminal" form: on this fixture the completion re-read is
strong enough to *solve* the wait — holding ``x_RotateFB`` / ``x_BlowerFB`` for
the coast, Starting completes and the drive reaches Execute — so the eventual
stall is a downstream Heat blocker where rotate is genuinely satisfied (naming
rotate there would be dishonest).  The pointable-at-``x_RotateFB`` guarantee is
therefore asserted where the wait actually lives: the completion re-read's
frontier, surfaced on every ``candidates_built`` event while the wait is open.
"""

from __future__ import annotations

import time

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events

pytestmark = pytest.mark.tumbler

GATE_MAX_SCANS = 20_000
GATE_WALL_BUDGET_S = 240.0


def _completion_frontier_tags(event) -> set[str]:
    return {t for t, _v in dict(event.data).get("completion_frontier") or ()}


def test_completion_reread_names_rotate_fb(tumbler_logic) -> None:
    """The completion re-read descends the pipeline cut and names x_RotateFB.

    Every ORIENT that prescribes the Starting->Execute wait re-reads its charted
    completion condition as ordinary transparent ladder; the sibling trace's
    unmet frontier (``candidates_built.completion_frontier``) names the true
    producer-chain leaf, ``x_RotateFB`` — the stall behind the wait is pointable,
    exactly as the design's "KEEP" table demands.
    """
    plc = PLC(tumbler_logic, dt=0.010)
    plc.step()
    target = plc._known_tags_by_name["y_BurnerLoop"]

    named_rotate = False
    for event in pilot_events(plc, target, max_scans=1200):
        if event.kind == "candidates_built" and "x_RotateFB" in _completion_frontier_tags(event):
            named_rotate = True
            break
        if event.kind == "finished":
            break

    assert named_rotate, (
        "the Starting->Execute wait's completion re-read never named x_RotateFB "
        "in its frontier — the pipeline-cut blocker is not pointable"
    )


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
