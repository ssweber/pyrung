"""Inputs compared with a changing value get every value, not a literal partition.

The expression partition gives an input one value per class its literal
comparisons split.  Compared with another changing value (a timer or
counter accumulator, a calc-stepped Int, another input, an expression), the
input's classes are split at points no literal names: ``T.Acc == In`` needs
``In`` equal to whatever the accumulator holds.  Each case below checks the
prover against concrete stepping under the default, ``all_off`` and
``sound_baseline`` configurations: exhaustively over the inputs where that is
small, else on a witness trace.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    calc,
    copy,
    latch,
    on_delay,
    out,
    search,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    _build_explore_context,
    never,
)
from pyrung.core.analysis.prove.passes import _OptConfig

CONFIGS = {
    "default": _OptConfig(),
    "all_off": _OptConfig.all_off(),
    "sound_baseline": _OptConfig.sound_baseline(),
}


def _replay(program: Program, trace: list[TraceStep]) -> PLC:
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _witness_hits(program: Program, seq: list[tuple[dict[str, Any], int]], bad: str) -> bool:
    plc = PLC(program, dt=0.010)
    for inputs, scans in seq:
        plc.patch(inputs)
        for _ in range(scans):
            plc.step()
    return plc.current_state.tags[bad] is True


def _one_scan_reachable(program: Program, domains: dict[str, range], bad: str) -> bool:
    """Every joint input value, one scan from power-on each: does *bad* latch?"""
    names = sorted(domains)
    for combo in itertools.product(*(domains[n] for n in names)):
        plc = PLC(program, dt=0.010)
        plc.patch(dict(zip(names, combo, strict=True)))
        plc.step()
        if plc.current_state.tags[bad]:
            return True
    return False


def _assert_counterexample(program: Program, bad: Bool, config: _OptConfig) -> None:
    result = never(program, bad, _opt_config=config)
    assert isinstance(result, Counterexample), result
    assert _replay(program, result.trace).current_state.tags[bad.name] is True


def _agree(program: Program, bad: Bool, reachable: bool, config: _OptConfig) -> None:
    if reachable:
        _assert_counterexample(program, bad, config)
    else:
        result = never(program, bad, _opt_config=config)
        assert isinstance(result, Proven), result
        assert not result.caveats


# -- the reviewer's shape, scaled down: an accumulator equal to an input ----


def _timer_equals_input() -> tuple[Program, Bool, list[tuple[dict[str, Any], int]]]:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=199)
    timer = Timer.clone("KT")
    window, first, second, bad = Bool("KY"), Bool("KA"), Bool("KB"), Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en):
            on_delay(timer, 200)
        with Rung(value > 100, value < 160):
            out(window)
        with Rung(timer.Acc == value, timer.Acc < 120, window):
            latch(first)
        with Rung(first, timer.Acc == value, timer.Acc > 120, timer.Acc < 140, window):
            latch(second)
        with Rung(second, timer.Acc == value, timer.Acc > 140, window):
            latch(bad)
    # The accumulator moves 10 ms a scan: 110, then 130, then 150.
    seq = [({"KEn": True, "KIn": 110}, 11), ({"KIn": 130}, 2), ({"KIn": 150}, 2)]
    return logic, bad, seq


def _stepped_int_equals_input() -> tuple[Program, Bool, list[tuple[dict[str, Any], int]]]:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=199)
    stepped = Int("KD")
    window, first, second, bad = Bool("KY"), Bool("KA"), Bool("KB"), Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en, stepped < 200):
            calc(stepped + 10, stepped)
        with Rung(value > 100, value < 160):
            out(window)
        with Rung(stepped == value, stepped < 120, window):
            latch(first)
        with Rung(first, stepped == value, stepped > 120, stepped < 140, window):
            latch(second)
        with Rung(second, stepped == value, stepped > 140, window):
            latch(bad)
    seq = [({"KEn": True, "KIn": 110}, 11), ({"KIn": 130}, 2), ({"KIn": 150}, 2)]
    return logic, bad, seq


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
@pytest.mark.parametrize("build", [_timer_equals_input, _stepped_int_equals_input])
def test_input_equal_to_stepping_value_is_found(build, config) -> None:
    logic, bad, seq = build()
    assert _witness_hits(logic, seq, bad.name)
    _assert_counterexample(logic, bad, config)


def test_input_equal_to_accumulator_explores_every_value() -> None:
    logic, bad, _seq = _timer_equals_input()
    result = never(logic, bad, _debug=True)
    assert isinstance(result, Counterexample)
    assert result._debug_context.nondeterministic_dims["KIn"] == tuple(range(200))


# -- expressions -------------------------------------------------------------


def _offset_window() -> Program:
    value = Int("KIn", external=True, min=0, max=200)
    with Program(strict=False) as logic:
        with Rung(value > 3, value + 5 > 100, value + 5 < 104):
            latch(Bool("KBad"))
    return logic


def _constant_expression_window() -> Program:
    value = Int("KIn", external=True, min=0, max=200)
    limit = Int("KLim", readonly=True, default=50)
    with Program(strict=False) as logic:
        # 101 and 102 only; no literal splits there.
        with Rung(value > 3, value > limit * 2, value < limit * 2 + 3):
            latch(Bool("KBad"))
    return logic


def _same_input_both_sides() -> Program:
    value = Int("KIn", external=True, min=0, max=200)
    with Program(strict=False) as logic:
        # value > 100 and value < 102.5: 101 and 102 only.
        with Rung(value > 3, value * 2 > value + 100, value * 3 < value + 205):
            latch(Bool("KBad"))
    return logic


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
@pytest.mark.parametrize(
    "build",
    [_offset_window, _constant_expression_window, _same_input_both_sides],
    ids=["offset", "constant_expression", "same_input"],
)
def test_input_inside_an_expression_comparison(build, config) -> None:
    logic = build()
    reachable = _one_scan_reachable(logic, {"KIn": range(201)}, "KBad")
    assert reachable
    _agree(logic, Bool("KBad"), reachable, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_stepped_tag_equal_to_input_plus_offset(config) -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=100)
    stepped = Int("KD")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en, stepped < 60):
            calc(stepped + 7, stepped)
        with Rung(value > 3, stepped == value + 5, stepped > 20):
            latch(bad)
    assert _witness_hits(logic, [({"KEn": True, "KIn": 23}, 4)], "KBad")
    _assert_counterexample(logic, bad, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_input_equal_to_expression_of_another_input(config) -> None:
    first = Int("KA", external=True, min=0, max=40)
    second = Int("KB", external=True, min=0, max=40)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(first > 25, second < 10, first == second + 23):
            latch(bad)
    reachable = _one_scan_reachable(logic, {"KA": range(41), "KB": range(41)}, "KBad")
    assert reachable
    _agree(logic, bad, reachable, config)


# -- data flow, other inputs, indirect and search reads -----------------------


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_input_copied_then_compared_with_stepped_tag(config) -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=100)
    stepped, stored = Int("KD"), Int("KZ")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en, stepped < 60):
            calc(stepped + 7, stepped)
        with Rung():
            copy(value, stored)
        with Rung(value > 3, stored == stepped, stepped > 20):
            latch(bad)
    assert _witness_hits(logic, [({"KEn": True, "KIn": 28}, 5)], "KBad")
    _assert_counterexample(logic, bad, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
@pytest.mark.parametrize("upper", [20, 21], ids=["unreachable", "reachable"])
def test_two_inputs_compared_with_each_other(config, upper) -> None:
    first = Int("KA", external=True, min=0, max=30)
    second = Int("KB", external=True, min=0, max=30)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        # Equal inside both windows: 20 when upper=21, nothing when upper=20.
        with Rung(first > 10, first < upper, second > 19, second < 25, first == second):
            latch(bad)
    reachable = _one_scan_reachable(logic, {"KA": range(31), "KB": range(31)}, "KBad")
    assert reachable is (upper == 21)
    _agree(logic, bad, reachable, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_three_inputs_ordered_inside_one_window(config) -> None:
    first = Int("KA", external=True, min=0, max=12)
    second = Int("KB", external=True, min=0, max=12)
    third = Int("KC", external=True, min=0, max=12)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        # A partition splits 5..9 at its ends only: two values, three tags.
        with Rung(first > 4, third < 10, first < second, second < third):
            latch(bad)
    domains = {"KA": range(13), "KB": range(13), "KC": range(13)}
    reachable = _one_scan_reachable(logic, domains, "KBad")
    assert reachable
    _agree(logic, bad, reachable, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_indirect_element_equal_to_input(config) -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=60)
    pointer = Int("KPtr", external=True, choices={1: "first", 2: "second"})
    regs = Block("KDS", TagType.INT, 1, 2)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en, regs[1] < 50):
            calc(regs[1] + 7, regs[1])
        with Rung(value > 3, value < 40, regs[pointer] == value, regs[1] > 20):
            latch(bad)
    assert _witness_hits(logic, [({"KEn": True, "KIn": 28, "KPtr": 1}, 4)], "KBad")
    _assert_counterexample(logic, bad, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_search_for_input(config) -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=60)
    regs = Block("KDS", TagType.INT, 1, 2)
    result, found, bad = Int("KR"), Bool("KF"), Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en, regs[1] < 50):
            calc(regs[1] + 7, regs[1])
        with Rung():
            search(regs.select(1, 2) == value, result=result, found=found)
        with Rung(value > 3, value < 40, found, result == 1):
            latch(bad)
    assert _witness_hits(logic, [({"KEn": True, "KIn": 28}, 4)], "KBad")
    _assert_counterexample(logic, bad, config)


# -- a partner with a known value set keeps the partition --------------------


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_partner_written_only_with_literals_keeps_partition(config) -> None:
    mode_a, mode_b = Bool("KModeA", external=True), Bool("KModeB", external=True)
    value = Int("KIn", external=True, min=0, max=999)
    mode = Int("KMode")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(mode_a):
            copy(5, mode)
        with Rung(mode_b):
            copy(7, mode)
        with Rung(value == mode, value > 6):
            latch(bad)
    assert _witness_hits(logic, [({"KModeB": True, "KIn": 7}, 2)], "KBad")
    _assert_counterexample(logic, bad, config)
    result = never(logic, bad, _opt_config=config, _debug=True)
    domain = set(result._debug_context.nondeterministic_dims["KIn"])
    assert {4, 5, 6, 7, 8} <= domain
    assert len(domain) < 20


def test_partner_copied_from_bounded_input_keeps_partition() -> None:
    source = Int("KSource", external=True, min=0, max=5)
    stored = Int("KStored")
    other = Int("KOther", external=True, default=5)
    flag = Bool("KFlag")
    with Program(strict=False) as logic:
        with Rung():
            copy(source, stored)
        with Rung(stored > other):
            out(flag)
    ctx = _build_explore_context(logic)
    assert not isinstance(ctx, Intractable)
    assert set(range(-1, 7)) <= set(ctx.nondeterministic_dims["KOther"])


# -- refusals ----------------------------------------------------------------


def test_unbounded_input_compared_with_accumulator_is_refused() -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True)
    timer = Timer.clone("KT")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en):
            on_delay(timer, 200)
        with Rung(value > 100, timer.Acc == value):
            latch(bad)
    result = never(logic, bad)
    assert isinstance(result, Intractable)
    assert "KIn" in result.tags
    hint = next(h for h in result.hints if "KIn" in h)
    assert "compared with a changing value (KT_Acc)" in hint
    assert hint.isascii()


def test_wide_input_compared_with_accumulator_is_refused() -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=1000)
    timer = Timer.clone("KT")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(en):
            on_delay(timer, 1000)
        with Rung(value > 500, timer.Acc == value):
            latch(bad)
    result = never(logic, bad)
    assert isinstance(result, Intractable)
    hint = next(h for h in result.hints if "KIn" in h)
    assert "too wide" in hint and "1001 values" in hint


def test_real_input_compared_with_tag_is_refused() -> None:
    level = Real("KLevel", external=True, min=0.0, max=10.0)
    setpoint = Real("KSetpoint")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung():
            calc(setpoint + 0.5, setpoint)
        with Rung(level > 2.0, level < setpoint):
            latch(bad)
    result = never(logic, bad)
    assert isinstance(result, Intractable)
    assert "KLevel" in result.tags


# -- how()'s prior keeps the partition ---------------------------------------


def test_domains_only_prior_keeps_the_partition() -> None:
    logic, _bad, _seq = _timer_equals_input()
    ctx = _build_explore_context(logic, _opt_config=replace(_OptConfig(), domains_only=True))
    assert not isinstance(ctx, Intractable)
    assert len(ctx.nondeterministic_dims["KIn"]) < 20
