"""Property tests: the CLICK v4 Out family against plain-Python models.

``toggle``, ``debounce``, ``flasher`` and ``timed`` are driven by a random rung
sequence, one level per scan.  Each is checked against a small model written
from the documented behaviour (``docs`` and the hardware-recorded
``tests/conformance/coils/test_out.py`` cases), and every scan the interpreter and
the compiled kernel must hold identical state.

Scan periods are binary fractions of a second (62.5 ms and up), so a unit
count is exact and the models can use ``Fraction`` arithmetic: the timers
accumulate ``floor(n * units_per_scan)`` over ``n`` steady scans.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyrung.core import (
    PLC,
    Bool,
    CompiledPLC,
    Program,
    Rung,
    Timer,
    debounce,
    flasher,
    timed,
    toggle,
)

pytestmark = pytest.mark.hypothesis

SETTINGS = settings(max_examples=120, deadline=None, derandomize=True, database=None)

Run = Bool("OfRun")
Coil = Bool("OfCoil")

DTS = (0.0625, 0.125, 0.25, 0.5)
UNITS = ("ms", "sec")
_ACC_MAX = 32767


def _per_scan(dt: float, unit: str) -> Fraction:
    return Fraction(dt) * (1000 if unit == "ms" else 1)


def _acc(scans: int, per_scan: Fraction) -> int:
    """A timer's accumulator after *scans* uninterrupted scans of timing."""
    return min(math.floor(scans * per_scan), _ACC_MAX)


def _ticks_to_reach(preset: int, per_scan: Fraction) -> int:
    """Scans of timing until the accumulator reaches *preset* (at least one)."""
    scans = 1
    while _acc(scans, per_scan) < preset:
        scans += 1
    return scans


def _build(coil_factory) -> Program:
    with Program(strict=False) as logic:
        with Rung(Run):
            coil_factory()
    return logic


def _drive(logic: Program, dt: float, levels: list[bool]) -> list[dict]:
    """Step both backends over *levels*; return the interpreter's tags per scan.

    Fails if the compiled kernel ever holds different tags or memory.
    """
    plc = PLC(logic, dt=dt)
    kernel = CompiledPLC(logic, dt=dt)
    seen: list[dict] = []
    for level in levels:
        plc.patch({"OfRun": level})
        kernel.patch({"OfRun": level})
        plc.step()
        kernel.step()
        assert dict(plc.current_state.tags) == dict(kernel.current_state.tags)
        assert dict(plc.current_state.memory) == dict(kernel.current_state.memory)
        seen.append(dict(plc.current_state.tags))
    return seen


levels_st = st.lists(st.booleans(), min_size=1, max_size=80)
# Long steady stretches, so a phase has time to finish.
runs_st = st.lists(
    st.tuples(st.booleans(), st.integers(min_value=1, max_value=30)), min_size=1, max_size=8
).map(lambda runs: [level for level, n in runs for _ in range(n)])
any_levels_st = st.one_of(levels_st, runs_st)
dt_st = st.sampled_from(DTS)
unit_st = st.sampled_from(UNITS)


# --- toggle ---------------------------------------------------------------------


def toggle_model(levels: list[bool]) -> list[bool]:
    """Inverts on each OFF->ON edge (the rung counts as OFF before scan 1)."""
    state, prev, out = False, False, []
    for level in levels:
        if level and not prev:
            state = not state
        prev = level
        out.append(state)
    return out


@given(levels=any_levels_st)
@SETTINGS
def test_toggle_flips_once_per_rising_edge(levels):
    logic = _build(lambda: toggle(Coil))
    seen = _drive(logic, 0.125, levels)
    assert [tags["OfCoil"] for tags in seen] == toggle_model(levels)


# --- debounce -------------------------------------------------------------------


def debounce_model(
    levels: list[bool], per_scan: Fraction, on_preset: int, off_preset: int
) -> list[tuple[bool, int, int]]:
    """(coil, on TD, off TD) per scan: the coil follows the input once it has
    held steady for the side's time, and holds in between."""
    coil, run_len, prev, out = False, 0, None, []
    for level in levels:
        run_len = run_len + 1 if level == prev else 1
        prev = level
        acc = _acc(run_len, per_scan)
        if level and acc >= on_preset:
            coil = True
        elif not level and acc >= off_preset:
            coil = False
        out.append((coil, acc if level else 0, 0 if level else acc))
    return out


@given(
    levels=any_levels_st,
    dt=dt_st,
    unit=unit_st,
    on_preset=st.integers(min_value=0, max_value=1200),
    off_preset=st.integers(min_value=0, max_value=1200),
)
@SETTINGS
def test_debounce_follows_a_steady_input(levels, dt, unit, on_preset, off_preset):
    if unit == "sec":
        on_preset, off_preset = on_preset % 8, off_preset % 8
    logic = _build(
        lambda: debounce(
            Coil,
            on=(Timer.clone("OfDbOn"), on_preset),
            off=(Timer.clone("OfDbOff"), off_preset),
            unit=unit,
        )
    )
    seen = _drive(logic, dt, levels)
    expected = debounce_model(levels, _per_scan(dt, unit), on_preset, off_preset)
    got = [(t["OfCoil"], t["OfDbOn_Acc"], t["OfDbOff_Acc"]) for t in seen]
    assert got == expected
    # An Out coil never sets its Timer's done bit (hardware, probe O6).
    assert not any(t["OfDbOn_Done"] or t["OfDbOff_Done"] for t in seen)


# --- flasher --------------------------------------------------------------------


def flasher_model(
    levels: list[bool], per_scan: Fraction, on_preset: int, off_preset: int
) -> list[bool]:
    """The coil per scan.  Each time the rung goes true a run starts at scan 1,
    always in an on phase (hardware, probe O4).  A phase ends on the scan its
    timer reaches the preset, and that scan already shows the next phase, so
    on and off phases run ``ka`` and ``kb`` scans, except that scan 1 is always
    on."""
    ka = _ticks_to_reach(on_preset, per_scan)
    kb = _ticks_to_reach(off_preset, per_scan)
    out, n = [], 0
    for level in levels:
        n = n + 1 if level else 0
        if not level:
            out.append(False)
        elif n == 1 or n < ka:
            out.append(True)
        else:
            out.append((n - ka) % (ka + kb) >= kb)
    return out


@given(
    levels=any_levels_st,
    dt=dt_st,
    unit=unit_st,
    on_preset=st.integers(min_value=1, max_value=600),
    off_preset=st.integers(min_value=1, max_value=600),
)
@SETTINGS
def test_flasher_alternates_and_restarts_with_an_on_phase(levels, dt, unit, on_preset, off_preset):
    if unit == "sec":
        on_preset, off_preset = on_preset % 6 + 1, off_preset % 6 + 1
    logic = _build(
        lambda: flasher(
            Coil,
            on=(Timer.clone("OfFlOn"), on_preset),
            off=(Timer.clone("OfFlOff"), off_preset),
            unit=unit,
        )
    )
    seen = _drive(logic, dt, levels)
    assert [t["OfCoil"] for t in seen] == flasher_model(
        levels, _per_scan(dt, unit), on_preset, off_preset
    )
    assert not any(t["OfFlOn_Done"] or t["OfFlOff_Done"] for t in seen)


# --- timed ----------------------------------------------------------------------


def timed_model(
    levels: list[bool], per_scan: Fraction, preset: int, maintained: bool
) -> list[bool]:
    """A rising edge starts a pulse of exactly the preset (at least one scan)
    that runs out whatever the rung does.  Not maintained, the coil then stays
    off until the rung has dropped; maintained, it follows the rung after the
    pulse.  The timer clears once the rung is false after the pulse."""
    phase, ticks, out = "idle", 0, []
    for level in levels:
        if phase == "spent":
            if level:
                out.append(maintained)
            else:
                phase = "idle"
                out.append(False)
        elif phase == "timing" or level:
            if phase == "idle":
                phase, ticks = "timing", 0
            ticks += 1
            done = _acc(ticks, per_scan) >= preset
            out.append(ticks == 1 or (level and maintained) or not done)
            if done:
                phase = "spent"
        else:
            out.append(False)
    return out


@given(
    levels=any_levels_st,
    dt=dt_st,
    unit=unit_st,
    preset=st.integers(min_value=0, max_value=800),
    maintained=st.booleans(),
)
@SETTINGS
def test_timed_pulses_for_the_preset(levels, dt, unit, preset, maintained):
    if unit == "sec":
        preset %= 8
    logic = _build(lambda: timed(Coil, Timer.clone("OfTm"), preset, unit, maintained=maintained))
    seen = _drive(logic, dt, levels)
    assert [t["OfCoil"] for t in seen] == timed_model(
        levels, _per_scan(dt, unit), preset, maintained
    )
    assert not any(t["OfTm_Done"] for t in seen)
