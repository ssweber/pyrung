"""Steerability inference for *clear-only* (ack-cleared) command interfaces.

A tag the program only ever **resets to its rest/default value** — ``reset()`` on
a Bool, ``copy(0, flag)`` on an Int/Word — is an operator/field command: the
program never asserts the active value, so that value must come from outside.
Such a tag is steerable regardless of ``external`` (the operator "sets" it, the
program only clears it — the acknowledge pattern).

This is the burner's PackML command bank (``C_Clear``, ``C_Reset``,
``C_ProductionMode``, ``C_UnitModeChgRequest`` …): plain ``c`` bits, not declared
``external``, cleared by ``reset()``.  ``pilot_how`` must be able to press them to
drive the state machine; if they drop out of the steerable set the route to
``y_BurnerLoop`` collapses to a bare coast and burns the scan budget.

The positive cases assert the clear-only inference; the negative controls guard
against over-broadening (a program-asserted value, an out coil, a derived clear).
"""

from __future__ import annotations

from pyrung import (
    PLC,
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Word,
    calc,
    copy,
    fill,
    out,
    reset,
    rung,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.trace import (
    _clear_only_command,
    compute_steerable,
)


def _steerable(logic: Program) -> frozenset[str]:
    plc = PLC(logic)
    pdg = build_program_graph(logic)
    return compute_steerable(pdg, plc._known_tags_by_name, logic)


# ── Positive: clear-only interfaces that must be inferred steerable ──────────


def test_bool_reset_conditional_is_steerable():
    """Bool command cleared by a *conditional* ``reset()`` — the C_Clear idiom."""
    Cmd = Bool("Cmd")  # not external — a plain internal command bit
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Cmd):  # Cmd is read (a real reader)
            out(Ack)
        with rung(Ack):  # conditional clear-to-rest
            reset(Cmd)

    assert "Cmd" in _steerable(logic)


def test_bool_reset_unconditional_is_steerable():
    """Momentary Bool command cleared by an *unconditional* ``reset()`` every scan
    — the C_UnitModeChgRequest idiom (mode_change R10).  The unconditional-clobber
    guard must NOT reject a clear-only writer."""
    Cmd = Bool("Momentary")
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Cmd):
            out(Ack)
        with rung():  # unconditional clear
            reset(Cmd)

    assert "Momentary" in _steerable(logic)


def test_int_flag_clear_to_zero_is_steerable():
    """Int flag cleared by ``copy(0, flag)`` — clear-to-default generalized past Bool."""
    Sel = Int("Sel")
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Sel == 1):
            out(Ack)
        with rung(Ack):
            copy(0, Sel)

    assert "Sel" in _steerable(logic)


def test_word_flag_clear_to_zero_is_steerable():
    """Word flag cleared by ``copy(0, flag)`` — same inference, Word type."""
    Mask = Word("Mask")
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Mask != 0):
            out(Ack)
        with rung(Ack):
            copy(0, Mask)

    assert "Mask" in _steerable(logic)


def test_dint_flag_clear_to_zero_is_steerable():
    """Dint flag cleared by ``copy(0, flag)`` — same inference, Dint type."""
    Count = Dint("Count")
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Count != 0):
            out(Ack)
        with rung(Ack):
            copy(0, Count)

    assert "Count" in _steerable(logic)


def test_multiple_clear_writers_all_default_is_steerable():
    """Two writers, both clearing to rest (e.g. cleared from two different acks) —
    still clear-only, so still steerable."""
    Cmd = Bool("Cmd")
    A = Bool("A")
    B = Bool("B")

    with Program(strict=False) as logic:
        with rung(Cmd):
            out(A)
        with rung(A):
            reset(Cmd)
        with rung(B):
            reset(Cmd)

    assert "Cmd" in _steerable(logic)


# ── Bulk-fill housekeeping: NOT a per-register ack (tumbler A_Alm*_Status) ────


def _known(logic: Program) -> dict:
    return PLC(logic)._known_tags_by_name


def test_bulk_fill_band_member_not_clear_only():
    """A status word whose ONLY writer is a *multi-slot bulk fill* is the program's
    own housekeeping, not an ack-cleared operator command.

    The tumbler shape: an alarm band summed into an aggregate that gates a state
    abort, with the band's sole writer a ``fill(0, band.select(...))`` bulk clear.
    Before the fix every band member classified as clear-only (``fill(0, ...)``
    resets it to rest) and so leaked into the steerable set — 88 ``A_Alm*_Status``
    words that then headlined an unreachable decline.  A bulk fill spanning the
    whole band is not the per-register ack idiom, so no member is clear-only."""
    from pyrung.core.tag import TagType

    band = Block("Alm", TagType.INT, 1, 10)
    Extent = Int("Extent")
    ForceClear = Bool("ForceClear", external=True)
    Gate = Bool("Gate")

    with Program(strict=False) as logic:
        with rung():  # aggregate reads every band member
            calc(band.select(1, 10).sum(), Extent)
        with rung(Extent == 0):  # the abort-style gate the aggregate drives
            out(Gate)
        with rung(ForceClear):  # the band's SOLE writer: a bulk housekeeping clear
            fill(0, band.select(1, 10))

    known = _known(logic)
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, known, logic)
    members = [band[i].name for i in range(1, 11)]

    leaked = [m for m in members if m in steerable]
    assert not leaked, f"bulk-fill band members leaked into steerable: {leaked}"
    member = band[5].name
    assert _clear_only_command(member, known.get(member), pdg, logic) is False


def test_single_slot_fill_clear_still_steerable():
    """A *targeted single-slot* ``fill(0, one register)`` is a per-register reset —
    the clear-only ack idiom — so it stays steerable.  Only the multi-slot bulk
    fill is disqualified; the boundary is span, not the instruction."""
    from pyrung.core.tag import TagType

    band = Block("Reg", TagType.INT, 1, 4)
    One = band[2]
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(One == 1):  # a reader
            out(Ack)
        with rung(Ack):  # single-slot targeted reset (span 1)
            fill(0, band.select(2, 2))

    known = _known(logic)
    pdg = build_program_graph(logic)
    assert _clear_only_command(One.name, known.get(One.name), pdg, logic) is True
    assert One.name in compute_steerable(pdg, known, logic)


# ── Negative controls: must stay NON-steerable before and after the fix ──────


def test_program_asserted_latch_not_steerable():
    """A flag the program itself *sets* to a non-default value (``copy(1, flag)``)
    is program-authored, not an operator interface — must stay non-steerable when
    it is not external."""
    Flag = Int("Flag")
    Trig = Bool("Trig", external=True)
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Trig):
            copy(1, Flag)  # program asserts the active value
        with rung(Flag == 1):
            out(Ack)
        with rung(Ack):
            copy(0, Flag)

    assert "Flag" not in _steerable(logic)


def test_out_coil_driven_not_steerable():
    """An out-coil-driven Bool is a computed output, never a command — even though
    ``out`` writes the rest value False when its rung is false."""
    Cond = Bool("Cond", external=True)
    Y = Bool("Y")

    with Program(strict=False) as logic:
        with rung(Cond):
            out(Y)

    assert "Y" not in _steerable(logic)


def test_derived_clear_source_not_steerable():
    """A writer that clears from *another tag* (``copy(Other, flag)``) derives from
    live state — the program authors the value, so not steerable."""
    Flag = Int("Flag2")
    Other = Int("Other", external=True)
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Flag == 0):
            out(Ack)
        with rung(Ack):
            copy(Other, Flag)  # not a literal — derived

    assert "Flag2" not in _steerable(logic)


# ── Anchor: the already-working external path must keep working ──────────────


def test_never_written_external_still_steerable():
    """Regression anchor: a never-written external command stays steerable via the
    pure-input arm (this never depended on the clear-only inference)."""
    Cmd = Int("ExtCmd", external=True, choices={0: "None", 1: "A", 2: "B"})
    Ack = Bool("Ack")

    with Program(strict=False) as logic:
        with rung(Cmd == 1):
            out(Ack)

    assert "ExtCmd" in _steerable(logic)
