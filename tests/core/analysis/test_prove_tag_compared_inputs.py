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
    And,
    Block,
    Bool,
    Dint,
    InputBlock,
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
    pack_words,
    run_function,
    search,
    unpack_to_words,
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


# -- a REAL between split points closer than 1 -------------------------------


def _real_window(low: float, high: float, *, via_copy: bool) -> Program:
    value = Real("KLevel", external=True, min=0.0, max=10.0)
    stored = Real("KStored")
    compared = stored if via_copy else value
    with Program(strict=False) as logic:
        if via_copy:
            with Rung():
                copy(value, stored)
        with Rung(compared > low, compared < high):
            latch(Bool("KBad"))
    return logic


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
@pytest.mark.parametrize("via_copy", [False, True], ids=["direct", "copied"])
@pytest.mark.parametrize(
    ("low", "high", "inside"),
    [(5, 6, 5.5), (5.2, 5.6, 5.4), (5, 5, None)],
    ids=["one_apart", "fractional", "empty"],
)
def test_real_input_between_close_split_points(config, via_copy, low, high, inside) -> None:
    logic = _real_window(low, high, via_copy=via_copy)
    reachable = inside is not None
    if reachable:
        assert _witness_hits(logic, [({"KLevel": inside}, 1)], "KBad")
    _agree(logic, Bool("KBad"), reachable, config)


# -- comparisons only the property makes (review B1) -------------------------


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_property_compares_input_with_stepped_int(config) -> None:
    en = Bool("KEn", external=True)
    value = Int("KIn", external=True, min=0, max=199)
    stepped = Int("KD")
    with Program(strict=False) as logic:
        with Rung(en, stepped < 200):
            calc(stepped + 10, stepped)
        with Rung(value > 100, value < 160):
            out(Bool("KWin"))
    prop = And(stepped == value, stepped > 100, stepped < 120)
    result = never(logic, prop, _opt_config=config)
    assert isinstance(result, Counterexample), result
    state = _replay(logic, result.trace).current_state
    assert state.tags["KD"] == state.tags["KIn"] == 110


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_property_compares_two_inputs(config) -> None:
    first = Int("KA", external=True, min=0, max=500)
    second = Int("KB", external=True, min=0, max=500)
    with Program(strict=False) as logic:
        with Rung(first > 100, second > 100):
            out(Bool("KWin"))
    result = never(logic, And(first == second, first > 300, first < 302), _opt_config=config)
    assert isinstance(result, Counterexample), result
    state = _replay(logic, result.trace).current_state
    assert state.tags["KA"] == state.tags["KB"] == 301


# -- the index of an indirect access (review B2) -----------------------------


def _indexed_table(*, via_calc: bool) -> tuple[Program, Bool]:
    en = Bool("KEn", external=True)
    table = Block("KTbl", TagType.INT, 1, 50)
    pointer = Int("KPtr", external=True, min=1, max=50, default=1)
    loaded, stepped, index = Int("KLoaded"), Int("KS"), Int("KP2", default=1)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung():
            for i in range(1, 51):
                copy(i * 10, table[i])
        with Rung(en, stepped < 400):
            calc(stepped + 10, stepped)
        if via_calc:
            with Rung():
                calc(pointer + 0, index)
            with Rung(index >= 1, index <= 50):
                copy(table[index], loaded)
            with Rung(loaded == stepped, stepped > 365, stepped < 375):
                latch(bad)
        else:
            with Rung():
                copy(pointer, index)
            with Rung(index >= 1, index <= 50, table[index] == stepped, stepped > 365):
                latch(bad)
    return logic, bad


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
@pytest.mark.parametrize("via_calc", [False, True], ids=["indirect_compare", "calc_index"])
def test_index_of_element_compared_with_stepped_int(config, via_calc) -> None:
    logic, bad = _indexed_table(via_calc=via_calc)
    assert _witness_hits(logic, [({"KEn": True, "KPtr": 37}, 37)], "KBad")
    _assert_counterexample(logic, bad, config)


# -- free inputs compared with each other (review N5) ------------------------


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_wide_inputs_compared_with_each_other(config) -> None:
    value = Int("KPV", external=True, min=0, max=4095)
    setpoint = Int("KSP", external=True, min=0, max=4095)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(value > setpoint):
            latch(bad)
    _assert_counterexample(logic, bad, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_real_inputs_compared_with_each_other(config) -> None:
    first = Real("KA", external=True, min=0.0, max=100.0)
    second = Real("KB", external=True, min=0.0, max=100.0)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(first > second):
            latch(bad)
    _assert_counterexample(logic, bad, config)


@pytest.mark.parametrize("config", CONFIGS.values(), ids=CONFIGS.keys())
def test_real_and_int_inputs_ordered_between_literals(config) -> None:
    level = Real("KLevel", external=True, min=0.0, max=10.0)
    count = Int("KCount", external=True, min=0, max=10)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        # 4 <= Count < Level < 5: no literal names a value of Level in there.
        with Rung(count > 3, level < 5, count < level):
            latch(bad)
    assert _witness_hits(logic, [({"KLevel": 4.5, "KCount": 4}, 1)], "KBad")
    _assert_counterexample(logic, bad, config)


# -- known gaps, left for a later prover sweep -------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review B3: _all_write_targets drops a run_function out bound to an "
        "indirect element (outs={'y': W[Ptr]}), so In == W[2] is partitioned "
        "against the literal writes only: false Proven"
    ),
)
def test_run_function_indirect_out_is_a_writer() -> None:
    go = Bool("KGo", external=True)
    words = Block("KW", TagType.INT, 1, 3)
    pointer = Int("KPtr", external=True, min=1, max=3)
    value = Int("KIn", external=True, min=0, max=2000)
    bad = Bool("KBad")

    def fifty():
        return {"y": 50}

    with Program(strict=False) as logic:
        with Rung():
            copy(5, words[2])
        with Rung(go):
            run_function(fifty, outs={"y": words[pointer]})
        with Rung(value == words[2], value > 45):
            latch(bad)
    assert _witness_hits(logic, [({"KGo": True, "KPtr": 2, "KIn": 50}, 1)], "KBad")
    _assert_counterexample(logic, bad, _OptConfig())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review B3: _all_write_targets skips the indirect dest of "
        "unpack_to_words(Src, W.select(Ptr, Ptr + 1)) and pack_words(..., "
        "DD[Ptr]), so In == W[2] / In == DD[2] is partitioned against the "
        "literal writes only: false Proven"
    ),
)
@pytest.mark.parametrize("packing", ["unpack_indirect_range", "pack_indirect_dest"])
def test_packing_indirect_dest_is_a_writer(packing) -> None:
    go = Bool("KGo", external=True)
    words = Block("KW", TagType.INT, 1, 4)
    dwords = Block("KDD", TagType.DINT, 1, 3)
    pointer = Int("KPtr", external=True, min=1, max=3)
    value = Int("KIn", external=True, min=0, max=2000)
    source = Dint("KSrc")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        if packing == "unpack_indirect_range":
            with Rung():
                copy(50, source)
                copy(5, words[2])
            with Rung(go):
                unpack_to_words(source, words.select(pointer, pointer + 1))
            with Rung(value == words[2], value > 45):
                latch(bad)
        else:
            with Rung():
                copy(50, words[1])
                copy(0, words[2])
                copy(5, dwords[2])
            with Rung(go):
                pack_words(words.select(1, 2), dwords[pointer])
            with Rung(value == dwords[2], value > 45):
                latch(bad)
    assert _witness_hits(logic, [({"KGo": True, "KPtr": 2, "KIn": 50}, 1)], "KBad")
    _assert_counterexample(logic, bad, _OptConfig())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review N1: a REAL with integer min/max (L = Real(min=0.0, max=10.0), "
        "calc(L * L, P); P > 30, P < 31) is enumerated at integers only, so "
        "L = 5.5 is never tried: false Proven"
    ),
)
def test_real_with_integer_bounds_is_not_enumerated_as_integers() -> None:
    level = Real("KL", external=True, min=0.0, max=10.0)
    square = Real("KP")
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung():
            calc(level * level, square)
        with Rung(square > 30, square < 31):
            latch(bad)
    assert _witness_hits(logic, [({"KL": 5.5}, 1)], "KBad")
    _assert_counterexample(logic, bad, _OptConfig())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review N2: an Int compared with fractional literals (In > 2.5, "
        "In < 3.5) gets the literals +-1, not floor/ceil, so In = 3 is never "
        "tried: false Proven"
    ),
)
def test_int_between_fractional_literals() -> None:
    value = Int("KIn", external=True, min=0, max=2000)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(value > 2.5, value < 3.5):
            latch(bad)
    assert _witness_hits(logic, [({"KIn": 3}, 1)], "KBad")
    _assert_counterexample(logic, bad, _OptConfig())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review N2: an Int compared with a fractional literal (In > 2.5) gets "
        "a witness value it cannot hold (In = 3.5)"
    ),
)
def test_int_witness_value_is_integral() -> None:
    value = Int("KIn", external=True, min=0, max=2000)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(value > 2.5):
            latch(bad)
    result = never(logic, bad)
    assert isinstance(result, Counterexample), result
    witnessed = [step.inputs["KIn"] for step in result.trace if "KIn" in step.inputs]
    assert witnessed
    assert all(float(v).is_integer() for v in witnessed), witnessed


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review N6: InputBlock[Ptr] == 5 (a literal) marks the elements as "
        "compared with a changing value (each other); the refusal hint should "
        "name the missing bounds, not a comparison"
    ),
)
def test_indirect_compare_with_literal_hint() -> None:
    pointer = Int("KPtr", external=True, min=1, max=3, default=1)
    inputs = InputBlock("KIB", TagType.INT, 1, 3)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(inputs[pointer] == 5):
            latch(bad)
    result = never(logic, bad)
    assert isinstance(result, Intractable), result
    assert not any("compared with a changing value" in hint for hint in result.hints)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review N5/D1: Level * 0.1 > 9.5 (Real 0..100) is refused: the "
        "expression marks Level as compared with a computed value and the "
        "reverse edge of * is not inverted; base found the counterexample"
    ),
)
def test_scaled_real_compared_with_literal() -> None:
    level = Real("KLevel", external=True, min=0.0, max=100.0)
    bad = Bool("KBad")
    with Program(strict=False) as logic:
        with Rung(level * 0.1 > 5):
            out(Bool("KHigh"))
        with Rung(level * 0.1 > 9.5):
            latch(bad)
    _assert_counterexample(logic, bad, _OptConfig())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pre-existing (base too): an unscoped callable property "
        "(never(p, lambda s: s['In'] == 301)) reads In where classify cannot "
        "see it, so In keeps its program partition: false Proven"
    ),
)
def test_unscoped_callable_property_reads_input() -> None:
    value = Int("KIn", external=True, min=0, max=500)
    with Program(strict=False) as logic:
        with Rung(value > 100):
            out(Bool("KWin"))
    result = never(logic, lambda state: state["KIn"] == 301)
    assert isinstance(result, Counterexample), result
