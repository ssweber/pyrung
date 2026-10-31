"""A finite REAL never leaves CLICK's range, +/-3.4028235E+38.

pyrung keeps 64-bit precision (a documented deviation from CLICK's float32),
but a store past the range is +/-Inf, as a CLICK's float32 Math stores it,
with the out-of-range flag (SC43) for ``calc``.  A finite user-supplied value
past the range is refused; a patch or force may be +/-Inf or NaN.  The
``runner_factory`` cases run on the interpreter and the compiled kernel.
"""

from __future__ import annotations

import math

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    calc,
    copy,
    fill,
    forloop,
    lro,
    lsh,
    out,
    randint,
    rro,
    rsh,
    sin,
    sqrt,
    system,
)
from pyrung.core.fold import _fits_dest
from pyrung.core.storage import REAL_MAX, StoreTransform, store_value

SC43 = system.fault.out_of_range
SC40 = system.fault.division_error
SC46 = system.fault.math_operation_error
INF = math.inf


def test_real_max_is_clicks_documented_limit():
    assert REAL_MAX == 3.4028235e38


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (1.5, 1.5),
        (REAL_MAX, REAL_MAX),
        (3.5e38, INF),
        (-3.5e38, -INF),
        (10**39, INF),
        (-(10**400), -INF),
        (INF, INF),
        (-INF, -INF),
    ],
)
def test_real_store_past_the_range_is_inf(value, stored):
    assert store_value(value, StoreTransform("real")) == stored


def test_real_store_keeps_nan():
    assert math.isnan(store_value(math.nan, StoreTransform("real")))


@pytest.mark.parametrize("sign", [1, -1])
def test_calc_overflow_stores_inf_and_sets_sc43(runner_factory, sign):
    big = Real("Big", default=sign * 3.0e38)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(big * 10, dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == sign * INF
    assert tags[SC43.name] is True


def test_calc_int_past_float_range_is_inf(runner_factory):
    d = Dint("D", default=2147483647)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(d * d * d * d * d * d * d * d * d * d, dest)  # about 2**310
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == INF
    assert tags[SC43.name] is True


def test_calc_in_range_keeps_64_bit_precision(runner_factory):
    big = Real("Big", default=16777216.0)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(big + 1, dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 16777217.0
    assert tags[SC43.name] is False


def test_division_by_zero_is_still_zero_and_sc40(runner_factory):
    one = Real("One", default=1.0)
    zero = Real("Zero")
    dest = Real("Dest", default=5.5)
    with Program() as program:
        with Rung():
            calc(one / zero, dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0.0
    assert tags[SC40.name] is True
    assert tags[SC43.name] is False


def test_copy_into_real_at_the_limit_is_kept(runner_factory):
    src = Real("Src", default=-REAL_MAX)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            copy(src, dest)
    plc = runner_factory(program)
    plc.step()
    assert plc.current_state.tags["Dest"] == -REAL_MAX


@pytest.mark.parametrize("value", [3.5e38, -1e39, float("inf"), float("-inf"), float("nan")])
def test_out_of_range_default_is_refused(value):
    with pytest.raises(ValueError, match="outside the REAL range"):
        Real("X", default=value)


def test_out_of_range_block_default_is_refused():
    block = Block("RB", TagType.REAL, 1, 2)
    block.slot(1, default=1e39)
    with pytest.raises(ValueError, match="outside the REAL range"):
        block[1]  # noqa: B018 - the slot's tag is built on first access


@pytest.mark.parametrize("value", [3.5e38, -1e39, 10**39])
def test_out_of_range_patch_and_force_are_refused(runner_factory, value):
    r = Real("R")
    i = Int("I")
    with Program() as program:
        with Rung():
            copy(r, i)
    plc = runner_factory(program)
    with pytest.raises(ValueError, match="outside the REAL range"):
        plc.patch({r: value})
    with pytest.raises(ValueError, match="outside the REAL range"):
        plc.patch({"R": value})
    with pytest.raises(ValueError, match="outside the REAL range"):
        plc.force(r, value)


def test_patch_by_name_of_a_known_real_is_checked():
    r = Real("R")
    with Program() as program:
        with Rung():
            copy(1.0, r)
    plc = PLC(program)
    with pytest.raises(ValueError, match="patch"):
        plc.patch({"R": 1e39})
    plc.patch({"R": REAL_MAX})


def test_out_of_range_literals_are_refused():
    r = Real("R")
    block = Block("RL", TagType.REAL, 1, 3)
    with Program(), Rung():
        with pytest.raises(ValueError, match="copy"):
            copy(1e39, r)
        with pytest.raises(ValueError, match="fill"):
            fill(float("inf"), block.select(1, 3))
        with pytest.raises(ValueError, match="calc"):
            calc(r * 1e39, r)
        # An Int destination clamps a large literal as before.
        copy(1e39, Int("I"))


def test_fold_does_not_skip_a_real_store_past_the_range():
    dest = Real("Dest")
    assert _fits_dest((-1.0, 1.0), dest)
    assert _fits_dest((-REAL_MAX, REAL_MAX), dest)
    assert not _fits_dest((0.0, 3.5e38), dest)
    assert not _fits_dest((-3.5e38, 0.0), dest)
    assert not _fits_dest((0.0, INF), dest)
    assert not _fits_dest((math.nan, math.nan), dest)


def _same(stored, value):
    return stored == value or (math.isnan(value) and math.isnan(stored))


@pytest.mark.parametrize("value", [INF, -INF, math.nan])
def test_patch_and_force_accept_inf_and_nan(runner_factory, value):
    r = Real("R")
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            copy(r, dest)
    plc = runner_factory(program)
    plc.patch({r: value})
    plc.step()
    assert _same(plc.current_state.tags["Dest"], value)
    plc.force(r, value)
    plc.step()
    assert _same(plc.current_state.tags["R"], value)


def test_an_inf_operand_follows_ieee(runner_factory):
    big = Real("Big", default=3.0e38)
    inf = Real("Inf")
    half = Real("Half")
    nan = Real("NaN")
    sine = Real("Sine")
    with Program() as program:
        with Rung():
            calc(big * 10, inf)
            calc(inf / 2, half)
            calc(inf - inf, nan)
            calc(sin(inf), sine)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Inf"] == INF
    assert tags["Half"] == INF
    assert math.isnan(tags["NaN"])
    assert math.isnan(tags["Sine"])
    assert tags[SC43.name] is True
    assert tags[SC40.name] is False


@pytest.mark.parametrize(
    ("base", "exponent", "stored"),
    [(3.0e38, 2, INF), (-3.0e38, 3, -INF), (-3.0e38, 2, INF), (10.0, 400.0, INF)],
)
def test_a_power_past_the_range_is_inf(runner_factory, base, exponent, stored):
    """``**`` overflows to +/-Inf with SC43, as ``*`` does (Python raises instead)."""
    b = Real("B", default=base)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(b**exponent, dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == stored
    assert tags[SC43.name] is True
    assert tags[SC40.name] is False


@pytest.mark.parametrize(
    "make",
    [
        lambda r: r & 1,
        lambda r: r | 1,
        lambda r: r ^ 1,
        lambda r: r << 1,
        lambda r: 1 >> r,
        lambda r: ~r,
        lambda r: lsh(r, 1),
        lambda r: rsh(1, r),
        lambda r: lro(r, 1),
        lambda r: rro(1, r),
    ],
)
@pytest.mark.parametrize("value", [math.nan, INF, -INF])
def test_a_non_finite_bitwise_operand_is_zero_and_sc43(runner_factory, make, value):
    r = Real("R")
    dest = Int("Dest", default=7)
    with Program() as program:
        with Rung():
            calc(make(r), dest)
    plc = runner_factory(program)
    plc.patch({r: value})
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0
    assert tags[SC43.name] is True
    assert tags[SC40.name] is False


@pytest.mark.parametrize("value", [math.nan, INF, -INF])
@pytest.mark.parametrize("which", ["lo", "hi"])
def test_a_non_finite_randint_bound_is_zero_and_sc43(runner_factory, value, which):
    """pyrung's choice (no CLICK reading): a draw needs integer bounds, so a
    bound holding Inf or NaN makes the calc store 0 with SC43, and the scan
    runs on."""
    r = Real("R", default=3.0)
    dest = Dint("Dest", default=7)
    after = Int("After")
    expr = randint(r, 9) if which == "lo" else randint(0, r)
    with Program() as program:
        with Rung():
            calc(expr + 5, dest)
            copy(1, after)
    plc = runner_factory(program)
    plc.patch({r: value})
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0
    assert tags["After"] == 1
    assert tags[SC43.name] is True
    assert tags[SC40.name] is False
    plc.step()  # the bound is still not finite on the next scan
    assert plc.current_state.tags["Dest"] == 0


def test_a_finite_real_randint_bound_draws_in_range(runner_factory):
    r = Real("R", default=3.0)
    dest = Dint("Dest", default=-1)
    with Program() as program:
        with Rung():
            calc(randint(0, r), dest)
    plc = runner_factory(program)
    for _ in range(5):
        plc.step()
        tags = plc.current_state.tags
        assert 0 <= tags["Dest"] <= 3
        assert tags[SC43.name] is False


@pytest.mark.parametrize("value", [math.nan, INF, -INF])
def test_a_non_finite_forloop_count_runs_no_iteration_and_sc43(runner_factory, value):
    """pyrung's choice (a CLICK For takes an integer count): a REAL count
    holding Inf or NaN runs no iteration, sets SC43, and the scan runs on."""
    r = Real("R", default=3.0)
    hits = Int("Hits")
    after = Int("After")
    with Program() as program:
        with Rung():
            with forloop(r):
                calc(hits + 1, hits)
        with Rung():
            copy(1, after)
    plc = runner_factory(program)
    plc.step()
    assert plc.current_state.tags["Hits"] == 3
    assert plc.current_state.tags[SC43.name] is False
    plc.patch({r: value})
    plc.step()
    tags = plc.current_state.tags
    assert tags["Hits"] == 3
    assert tags["After"] == 1
    assert tags[SC43.name] is True


def test_only_a_real_forloop_count_declares_sc43():
    """The PDG and prover see SC43 written only by a For that can set it."""
    from pyrung.core.instruction.control import ForLoopInstruction, for_loop_iterations

    assert for_loop_iterations(INF) == 0
    assert for_loop_iterations(math.nan) == 0
    assert for_loop_iterations(3.9) == 3
    assert ForLoopInstruction(Real("R"), Int("I"))._fault_writes == ("fault.out_of_range",)
    assert ForLoopInstruction(Int("N"), Int("I"))._fault_writes == ()
    assert ForLoopInstruction(5, Int("I"))._fault_writes == ()


def test_a_nan_result_sets_sc43(runner_factory):
    inf = Real("Inf")
    nan = Real("NaN")
    with Program() as program:
        with Rung():
            calc(inf - inf, nan)
    plc = runner_factory(program)
    plc.patch({inf: INF})
    plc.step()
    tags = plc.current_state.tags
    assert math.isnan(tags["NaN"])
    assert tags[SC43.name] is True


def test_division_by_zero_of_inf_is_still_zero_and_sc40(runner_factory):
    inf = Real("Inf")
    zero = Real("Zero")
    dest = Real("Dest", default=5.5)
    with Program() as program:
        with Rung():
            calc(inf / zero, dest)
    plc = runner_factory(program)
    plc.patch({inf: INF})
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0.0
    assert tags[SC40.name] is True
    assert tags[SC43.name] is False


def test_sqrt_of_minus_inf_is_a_domain_error(runner_factory):
    neg = Real("Neg")
    dest = Real("Dest", default=5.0)
    with Program() as program:
        with Rung():
            calc(sqrt(neg), dest)
    plc = runner_factory(program)
    plc.patch({neg: -INF})
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == 0.0
    assert tags[SC46.name] is True


def test_calc_inf_into_an_integer_is_zero_and_sc43(runner_factory):
    inf = Real("Inf")
    i = Int("I", default=7)
    d = Dint("D", default=7)
    w = Word("W", default=7)
    with Program() as program:
        with Rung():
            calc(inf * 1, i)
            calc(inf * 1, d)
            calc(inf * 1, w)
    plc = runner_factory(program)
    plc.patch({inf: INF})
    plc.step()
    tags = plc.current_state.tags
    assert (tags["I"], tags["D"], tags["W"]) == (0, 0, 0)
    assert tags[SC43.name] is True
    assert tags[SC40.name] is False


def test_copy_inf_into_an_integer_clamps(runner_factory):
    pos = Real("Pos")
    neg = Real("Neg")
    nan = Real("NaN")
    i = Int("I")
    d = Dint("D")
    w = Word("W", default=7)
    n = Int("N", default=7)
    with Program() as program:
        with Rung():
            copy(pos, i)
            copy(neg, d)
            copy(pos, w)
            copy(nan, n)
    plc = runner_factory(program)
    plc.patch({pos: INF, neg: -INF, nan: math.nan})
    plc.step()
    tags = plc.current_state.tags
    assert tags["I"] == 32767
    assert tags["D"] == -2147483648
    assert tags["W"] == 0
    assert tags["N"] == 0


def test_inf_and_nan_compare_as_ieee(runner_factory):
    r = Real("R")
    above = Bool("Above")
    equal = Bool("Equal")
    with Program() as program:
        with Rung(r > 1e38):
            out(above)
        with Rung(r == r):
            out(equal)
    plc = runner_factory(program)
    plc.patch({r: INF})
    plc.step()
    assert plc.current_state.tags["Above"] is True
    assert plc.current_state.tags["Equal"] is True
    plc.patch({r: math.nan})
    plc.step()
    assert plc.current_state.tags["Above"] is False
    assert plc.current_state.tags["Equal"] is False


@pytest.mark.parametrize("start", [1.0, INF])
def test_fold_of_a_real_that_overflows_matches_stepping(start):
    """A REAL doubling each scan under a running timer reaches +Inf (and SC43)
    the same way folded and stepped; from +Inf it stays +Inf."""
    from pyrung.core import Timer, on_delay

    def build():
        r = Real("R", default=1.0)
        tmr = Timer.clone("Tmr")
        with Program() as program:
            with Rung():
                on_delay(tmr, 100_000, "ms")
            with Rung():
                calc(r * 2, r)
        return program, r

    states = []
    for fold in (True, False):
        program, r = build()
        plc = PLC(program, dt=0.01)
        plc.patch({r: start})
        plc.run_for(20.0, fold=fold)
        states.append(plc.current_state)
    folded, stepped = states
    assert folded.scan_id == stepped.scan_id
    assert folded.tags["R"] == stepped.tags["R"] == INF
    assert folded.tags[SC43.name] is stepped.tags[SC43.name] is True


def _prover_programs():
    go = Bool("Go", external=True)
    x = Real("X")
    n = Real("N")
    i = Int("I")
    above = Bool("Above")
    unequal = Bool("Unequal")
    zeroed = Bool("Zeroed")
    with Program() as overflow:
        with Rung(go):
            calc((x + 3.0e38) * 10, x)
        with Rung(x > 3.0e38):
            out(above)
    with Program() as nan:
        with Rung(go):
            calc((x + 3.0e38) * 10, x)
            calc(x - x, n)
        with Rung(n != n):
            out(unequal)
    with Program() as into_int:
        with Rung(go):
            calc((x + 3.0e38) * 10, x)
            calc(x * 1, i)
        with Rung(go, i == 0, x > 1.0):
            out(zeroed)
    return [(overflow, above), (nan, unequal), (into_int, zeroed)]


@pytest.mark.parametrize("case", range(3), ids=["overflow", "nan", "into_int"])
def test_prover_does_not_prove_away_an_inf_result(case):
    """The prover runs on a program whose REAL overflows to Inf (or NaN), and
    never calls Proven a coil the concrete scan turns on."""
    from pyrung.core.analysis.prove import Proven, never

    program, coil = _prover_programs()[case]
    plc = PLC(program)
    plc.patch({"Go": True})
    plc.step()
    plc.step()
    assert plc.current_state.tags[coil.name] is True
    assert not isinstance(never(program, coil), Proven)
