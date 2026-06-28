"""Tests for ``Instruction.accumulating_profile()`` — the uniform structural
profile PILOT uses to reason about *"a held input is driving this accumulator to
completion"*.

Timers and counters return analytic profiles (with a usable ``scans_until``);
drums and non-accumulators return ``None`` and route to PILOT's empirical
fallback.
"""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    count_down,
    count_up,
    event_drum,
    off_delay,
    on_delay,
    out,
)
from pyrung.core.instruction.accumulating import (
    KIND_COUNT_DOWN,
    KIND_COUNT_UP,
    KIND_OFF_DELAY,
    KIND_ON_DELAY,
    AccProfile,
)
from pyrung.core.validation._common import walk_instructions


def _only_profile(prog: Program) -> AccProfile | None:
    profiles = [
        instr.accumulating_profile()
        for instr in walk_instructions(prog)
        if instr.accumulating_profile() is not None
    ]
    return profiles[0] if profiles else None


class TestProfileShape:
    def test_on_delay_profile(self):
        WD = Timer.clone("WD")
        run = Bool("run", external=True)
        with Program() as prog:
            with Rung(run):
                on_delay(WD, 2000, "ms")
        prof = _only_profile(prog)
        assert prof is not None
        assert prof.kind == KIND_ON_DELAY
        assert prof.advance_value is True
        assert prof.direction == 1
        assert prof.done.name == "WD_Done"
        assert prof.accumulator.name == "WD_Acc"
        # 2000 ms / (10 units per 10 ms scan) = 200 scans to done.
        assert prof.rate_per_scan(0.010) == 10.0
        assert prof.scans_until(prof.done_target(2000), acc_now=0, dt=0.010) == 200
        assert prof.scans_until(prof.done_target(2000), acc_now=2000, dt=0.010) == 0

    def test_off_delay_advances_while_unpowered(self):
        TOF = Timer.clone("TOF")
        run = Bool("run", external=True)
        with Program() as prog:
            with Rung(run):
                off_delay(TOF, 1000, "ms")
        prof = _only_profile(prog)
        assert prof is not None
        assert prof.kind == KIND_OFF_DELAY
        # An off-delay accumulates while its rung is NOT powered.
        assert prof.advance_value is False
        assert prof.reset is None

    def test_count_up_profile(self):
        rst = Bool("rst", external=True)
        pulse = Bool("pulse", external=True)
        with Program() as prog:
            with Rung(pulse):
                count_up(Counter[1], preset=7).reset(rst)
        prof = _only_profile(prog)
        assert prof is not None
        assert prof.kind == KIND_COUNT_UP
        assert prof.direction == 1
        # One count per held scan: 7 - 2 = 5 scans from acc=2.
        assert prof.rate_per_scan(0.010) == 1.0
        assert prof.scans_until(prof.done_target(7), acc_now=2, dt=0.010) == 5

    def test_count_down_counts_negative(self):
        rst = Bool("rst", external=True)
        pulse = Bool("pulse", external=True)
        with Program() as prog:
            with Rung(pulse):
                count_down(Counter[1], preset=4).reset(rst)
        prof = _only_profile(prog)
        assert prof is not None
        assert prof.kind == KIND_COUNT_DOWN
        assert prof.direction == -1
        # Done latches at -preset; 4 held scans from 0 reach -4.
        assert prof.done_target(4) == -4
        assert prof.scans_until(-4, acc_now=0, dt=0.010) == 4
        # Three more held scans from -1.
        assert prof.scans_until(-4, acc_now=-1, dt=0.010) == 3
        # Threshold already satisfied (acc already at/below target).
        assert prof.scans_until(-4, acc_now=-4, dt=0.010) == 0


class TestProfileFallback:
    def test_non_accumulator_returns_none(self):
        a = Bool("a", external=True)
        y = Bool("y")
        with Program() as prog:
            with Rung(a):
                out(y)
        assert _only_profile(prog) is None

    def test_drum_returns_none_for_now(self):
        # Drums have no analytic profile yet → None routes them to PILOT's
        # empirical (Tier 2) fallback rather than a wrong analytic estimate.
        enable = Bool("Enable", external=True)
        reset = Bool("Reset", external=True)
        step = Int("Step")
        done = Bool("Done")
        y1, y2 = Bool("Y1"), Bool("Y2")
        with Program() as prog:
            with Rung(enable):
                event_drum(
                    outputs=[y1, y2],
                    events=[enable, enable],
                    pattern=[[1, 0], [0, 1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset)
        assert _only_profile(prog) is None
