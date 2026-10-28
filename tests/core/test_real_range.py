"""A REAL never leaves CLICK's range, +/-3.4028235E+38.

pyrung keeps 64-bit precision (a documented deviation from CLICK's float32),
but a store past the range saturates at it, with the out-of-range flag (SC43)
for ``calc``, and a user-supplied value past it is refused.  The
``runner_factory`` cases run on the interpreter and the compiled kernel.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Block,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    calc,
    copy,
    fill,
    system,
)
from pyrung.core.fold import _fits_dest
from pyrung.core.storage import REAL_MAX, StoreTransform, store_value

SC43 = system.fault.out_of_range
SC40 = system.fault.division_error


def test_real_max_is_clicks_documented_limit():
    assert REAL_MAX == 3.4028235e38


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (1.5, 1.5),
        (REAL_MAX, REAL_MAX),
        (3.5e38, REAL_MAX),
        (-3.5e38, -REAL_MAX),
        (10**39, REAL_MAX),
        (-(10**400), -REAL_MAX),
        (float("inf"), 0),
        (float("nan"), 0),
    ],
)
def test_real_store_saturates(value, stored):
    assert store_value(value, StoreTransform("real")) == stored


@pytest.mark.parametrize("sign", [1, -1])
def test_calc_overflow_saturates_and_sets_sc43(runner_factory, sign):
    big = Real("Big", default=sign * 3.0e38)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(big * 10, dest)
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == sign * REAL_MAX
    assert tags[SC43.name] is True


def test_calc_int_past_float_range_saturates(runner_factory):
    d = Dint("D", default=2147483647)
    dest = Real("Dest")
    with Program() as program:
        with Rung():
            calc(d * d * d * d * d * d * d * d * d * d, dest)  # about 2**310
    plc = runner_factory(program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Dest"] == REAL_MAX
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


@pytest.mark.parametrize("value", [3.5e38, float("inf"), float("nan")])
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


def test_fold_does_not_skip_a_saturating_real_store():
    dest = Real("Dest")
    assert _fits_dest((-1.0, 1.0), dest)
    assert _fits_dest((-REAL_MAX, REAL_MAX), dest)
    assert not _fits_dest((0.0, 3.5e38), dest)
    assert not _fits_dest((-3.5e38, 0.0), dest)
