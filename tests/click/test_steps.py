"""estimate_steps: CLICK program-step costs measured on CLICK Programming Software.

Each case is a probe rung of ``devtools/click_step_probe.py`` and the step
count CLICK reported for it (2026-10-29).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pyrung.click import (
    CLICK_STEP_LIMIT,
    ClickBlocks,
    TagMap,
    estimate_steps,
    pyrung_to_ladder,
    validate_click_program,
)
from pyrung.click.validation import CLK_STEP_LIMIT
from pyrung.core import (
    Or,
    Program,
    Rung,
    branch,
    calc,
    call,
    comment,
    copy,
    fill,
    forloop,
    out,
    return_early,
    rise,
    subroutine,
)

b = ClickBlocks()
x, y, c, ds, df = b.x, b.y, b.c, b.ds, b.df

MAIN_BASE = 3  # Main with only END.


def _steps(body: Callable[[], None]) -> int:
    with Program(strict=False) as program:
        body()
    return estimate_steps(pyrung_to_ladder(program, TagMap([]), validate=False)).total


def _contact_out():
    with Rung(x[1]):
        out(y[1])


def _rail_out():
    with Rung():
        out(y[1])


def _four_series():
    with Rung(x[1], x[2], x[3], x[4]):
        out(y[1])


def _three_parallel():
    with Rung(Or(x[1], x[2], x[3])):
        out(y[1])


def _two_coils():
    with Rung(x[1]):
        out(c[1])
        out(c[2])


def _four_coils():
    with Rung(x[1]):
        out(c[1])
        out(c[2])
        out(c[3])
        out(c[4])


def _branch_with_contact():
    with Rung(x[1]):
        out(c[1])
        with branch(x[2]):
            out(c[2])


def _long_comment():
    comment("Step probe, long comment. " * 16)
    with Rung(x[1]):
        out(c[1])


def _compare():
    with Rung(df[1] > 1.5):
        out(c[1])


def _copy():
    with Rung(x[1]):
        copy(5, ds[1], oneshot=True)


def _two_copies():
    with Rung(x[1]):
        copy(5, ds[1])
        copy(6, ds[2])


def _fill():
    with Rung(x[1]):
        fill(0, ds.select(1, 10))


def _long_formula():
    with Rung(x[1]):
        calc((ds[1] * ds[2] + ds[3]) / ds[4], ds[5])


def _rise_real_formula():
    with Rung(rise(x[1])):
        calc(df[1] * 1.8 + 32.0, df[5])


def _for_next():
    with Rung(x[1]):
        with forloop(3):
            copy(1, ds[1])


@pytest.mark.parametrize(
    ("body", "measured"),
    [
        (_contact_out, 2),
        (_rail_out, 2),
        (_four_series, 5),
        (_three_parallel, 4),
        (_two_coils, 6),
        (_four_coils, 10),
        (_branch_with_contact, 6),
        (_long_comment, 2),
        (_compare, 4),
        (_copy, 5),
        (_two_copies, 12),
        (_fill, 5),
        (_long_formula, 10),
        (_for_next, 10),
    ],
    ids=lambda v: getattr(v, "__name__", str(v)).lstrip("_"),
)
def test_rung_costs_match_click(body: Callable[[], None], measured: int):
    assert _steps(body) == MAIN_BASE + measured


def test_real_constant_formula_rounds_up():
    # CLICK reported 7; the operand-and-operator rule gives one more.
    assert _steps(_rise_real_formula) == MAIN_BASE + 8


def test_subroutine_costs_its_base_and_rungs():
    @subroutine("Sub")
    def sub():
        with Rung(x[1]):
            out(c[1])
        with Rung(x[2]):
            return_early()

    def body():
        with Rung(x[1]):
            call(sub)

    with Program(strict=False) as program:
        body()
    estimate = estimate_steps(pyrung_to_ladder(program, TagMap([]), validate=False))
    # Main: END 3 + call rung 2.  Sub: base 4 (with its closing RET) + 2 + 2.
    assert estimate.main == 5
    assert estimate.subroutines == {"Sub": 8}
    assert estimate.unmeasured == ()


def _rungs_of_copies(n: int) -> Program:
    with Program(strict=False) as program:
        for i in range(n):
            with Rung(x[1]):
                copy(i, ds[1 + i % 4000])
    return program


def test_step_limit_finding():
    small = validate_click_program(_rungs_of_copies(10), TagMap([]))
    assert not any(f.code == CLK_STEP_LIMIT for f in (*small.warnings, *small.hints))

    # 5 steps a rung: 1550 rungs is 7753 (near), 1700 is 8503 (over).
    near = validate_click_program(_rungs_of_copies(1550), TagMap([]))
    assert [f.code for f in near.hints if f.code == CLK_STEP_LIMIT] == [CLK_STEP_LIMIT]

    over = validate_click_program(_rungs_of_copies(1700), TagMap([]))
    (finding,) = [f for f in over.warnings if f.code == CLK_STEP_LIMIT]
    assert f"{CLICK_STEP_LIMIT}" in finding.message
    assert "8503" in finding.message


def test_export_over_the_step_limit_warns_and_still_writes():
    with pytest.warns(UserWarning, match=r"CLK_STEP_LIMIT: Estimated 8503 program steps exceeds"):
        bundle = pyrung_to_ladder(_rungs_of_copies(1700), TagMap([]))
    assert estimate_steps(bundle).total == 8503
