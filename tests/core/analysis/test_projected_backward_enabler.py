"""Projected-oracle substrate + backward-enabler projection (in ``sp_values``).

The fixture is a minimal SFC step sequencer with the burner's shape:

* ``CurStep`` advances ``1 -> 2`` only via the transition rung, whose gate
  ``Trans == 1`` is in turn set ONLY from ``CurStep == 1``;
* ``2 -> 3`` happens via the even-step neutral-zone bump (gated on the derived
  ``valstepisodd``);
* there is no admissible successor past step 3.

So the true reachable domain is ``{1, 2, 3}`` even though the gate-agnostic
forward writers ``calc(CurStep + 1, CurStep)`` would otherwise enumerate it
without bound.
"""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, calc, copy
from pyrung.core.analysis.pdg import build_program_graph, resolve_rung
from pyrung.core.analysis.sp_values import (
    _enabler_reachable,
    _writer_projection,
    projected_writer_overlay,
)


def _build_sfc() -> Program:
    CurStep = Int("CurStep")
    Trans = Int("Trans")
    valstepisodd = Int("valstepisodd")
    fb = Bool("fb", external=True)
    with Program(strict=False) as prog:
        with Rung(CurStep == 1, fb):  # rung 0: Trans set ONLY from CurStep==1
            copy(1, Trans)
        with Rung(Trans == 1):  # rung 1: transition increment
            calc(CurStep + 1, CurStep)
            copy(0, Trans)
        with Rung():  # rung 2: parity derive
            calc(CurStep % 2, valstepisodd)
        with Rung(valstepisodd != 1):  # rung 3: even-step neutral bump
            calc(CurStep + 1, CurStep)
    return prog


def _rung(prog: Program, pdg, rung_index: int):
    for node in pdg.rung_nodes:
        if node.rung_index == rung_index and node.subroutine is None:
            return resolve_rung(prog, node)
    raise AssertionError(f"no main rung {rung_index}")


def test_projected_writer_overlay_pins_affine_source_and_derives_parity() -> None:
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    transition = _rung(prog, pdg, 1)

    built = projected_writer_overlay(transition, "CurStep", 2, {}, pdg, prog, {})
    assert built is not None
    overlay, pinned = built
    # To produce CurStep==2 the affine source CurStep must have been 1, and the
    # one-hop derive recomputes parity from that pin (1 % 2 == 1).
    assert overlay["CurStep"] == 1
    assert overlay["valstepisodd"] == 1
    assert {"CurStep", "valstepisodd"} <= pinned


def test_writer_projection_rejects_even_step_admits_transition_for_step2() -> None:
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    even_step = _rung(prog, pdg, 3)
    transition = _rung(prog, pdg, 1)

    # The even-step rung is parity-counterfactual for producing CurStep==2
    # (it fires only from an even step; CurStep==1 is odd).
    even_proj = _writer_projection(even_step, "CurStep", 2, {}, pdg, prog, {}, frozenset())
    assert even_proj is not None
    assert even_proj[0] is True

    # The transition rung is live, carrying Trans as its frontier prerequisite.
    trans_proj = _writer_projection(transition, "CurStep", 2, {}, pdg, prog, {}, frozenset())
    assert trans_proj is not None
    assert trans_proj == (False, ["Trans"])


def test_enabler_reachable_bounds_the_sequencer() -> None:
    prog = _build_sfc()
    pdg = build_program_graph(prog)

    # Trans==1 is reachable when CurStep is pinned to 1 — the gate CurStep==1 holds.
    assert (
        _enabler_reachable("Trans", 1, {}, pdg, prog, {"CurStep": 1}, {"CurStep"}, depth=4) is True
    )

    # Trans==1 is counterfactual when CurStep is pinned to 3 (the phantom step-4
    # predecessor): Trans's only producer is gated CurStep==1, which contradicts
    # the pin — so the forward writer can never advance CurStep past the gate.
    assert (
        _enabler_reachable("Trans", 1, {}, pdg, prog, {"CurStep": 3}, {"CurStep"}, depth=4) is False
    )


def test_enabler_reachable_treats_unprovable_producer_as_reachable() -> None:
    # Soundness: a free input (no classifiable producer) must resolve toward
    # reachable, never toward unreachable — else a consumer using this to bound a
    # domain could drop a reachable value.
    prog = _build_sfc()
    pdg = build_program_graph(prog)
    assert _enabler_reachable("fb", True, {}, pdg, prog, {}, set(), depth=4) is True
