"""Crossings — counter / timer done-bit inversion."""

from __future__ import annotations

from pyrung import Bool, Dint, Int
from pyrung.core.analysis.crossings.accumulating import (
    CountDownDoneCrossing,
    CountUpDoneCrossing,
    TimerDoneCrossing,
)
from pyrung.core.crossing import AffineCmp, Cmp, CrossingContext, eq_target
from pyrung.core.instruction.counters import CountDownInstruction, CountUpInstruction
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction


def _ctx() -> CrossingContext:
    return CrossingContext()


def _only(result):
    (branch,) = result.branches
    return branch


def _count_up(preset):
    return CountUpInstruction(Bool("Done"), Dint("Acc"), preset, Bool("En"), Bool("Rst"))


# --- count_up predecessor frontier --------------------------------------------


def test_count_up_done_true_includes_one_scan_frontier() -> None:
    r = CountUpDoneCrossing().reverse(_count_up(10), None, eq_target("Done", True), _ctx())
    assert _only(r) == (Cmp("Acc", ">=", 9, bound_is_tag=False),)
    assert r.exact is False


def test_count_up_done_true_tag_preset_preserves_frontier_offset() -> None:
    r = CountUpDoneCrossing().reverse(
        _count_up(Dint("Preset")), None, eq_target("Done", True), _ctx()
    )
    assert _only(r) == (AffineCmp("Acc", ">=", "Preset", scale=1, offset=-1),)


def test_count_up_done_false_falls_through() -> None:
    assert (
        CountUpDoneCrossing()
        .reverse(_count_up(10), None, eq_target("Done", False), _ctx())
        .fallthrough
    )


def test_count_up_accumulator_target_falls_through() -> None:
    # The accumulator chase is the walker's value-stepping domain.
    assert (
        CountUpDoneCrossing().reverse(_count_up(10), None, eq_target("Acc", 5), _ctx()).fallthrough
    )


def test_on_delay_done_true_falls_through_without_dt_constraint() -> None:
    instr = OnDelayInstruction(Bool("Done"), Int("Acc"), 100, Bool("En"))
    assert TimerDoneCrossing().reverse(instr, None, eq_target("Done", True), _ctx()).fallthrough


# --- count_down predecessor frontier ------------------------------------------


def test_count_down_done_true_includes_one_scan_frontier() -> None:
    instr = CountDownInstruction(Bool("Done"), Dint("Acc"), 5, Bool("Dn"), Bool("Rst"))
    r = CountDownDoneCrossing().reverse(instr, None, eq_target("Done", True), _ctx())
    assert _only(r) == (Cmp("Acc", "<=", -4, bound_is_tag=False),)


def test_count_down_done_true_tag_preset_preserves_negation_and_frontier() -> None:
    instr = CountDownInstruction(Bool("Done"), Dint("Acc"), Dint("Preset"), Bool("Dn"), Bool("Rst"))
    result = CountDownDoneCrossing().reverse(
        instr,
        None,
        eq_target("Done", True),
        _ctx(),
    )
    assert _only(result) == (AffineCmp("Acc", "<=", "Preset", scale=-1, offset=1),)


# --- off_delay (no clean inversion) -------------------------------------------


def test_off_delay_falls_through() -> None:
    instr = OffDelayInstruction(Bool("Done"), Int("Acc"), 100, Bool("En"))
    assert TimerDoneCrossing().reverse(instr, None, eq_target("Done", True), _ctx()).fallthrough
