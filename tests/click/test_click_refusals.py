"""Export findings for forms CLICK v4's editor dialogs refuse (checked by hand, 2026-11-06).

Each refused form is a finding that export refuses unless allowed; each accepted form
next to it (checked the same day) gets none.
"""

from __future__ import annotations

import contextlib

import pytest

from pyrung import debounce, flasher, timed
from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import (
    CLK_COPY_CONSTANT_INTO_BIT,
    CLK_MATH_BIT_OPERAND,
    CLK_MATH_DEST_BANK,
    CLK_MATH_MODE_MIXED,
    CLK_SEARCH_RANGE_BANK,
    CLK_SEARCH_RESULT_NOT_DS,
    CLK_TIMER_PRESET_TOO_LOW,
    validate_click_program,
)
from pyrung.core import Bool, Int, Timer, forloop, randint
from pyrung.core.program import (
    Program,
    Rung,
    copy,
    fill,
    math,
    search,
    time_drum,
    time_event_drum,
)

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _program(body) -> Program:
    prog = Program(strict=False)
    with prog:
        body()
    return prog


def _found(body, code: str, tag_map: TagMap | None = None) -> list:
    """The findings of *code*, strict mode, each with ASCII text."""
    report = validate_click_program(_program(body), tag_map or TagMap(), mode="strict")
    found = [f for f in report.errors if f.code == code]
    for finding in found:
        assert finding.message.isascii(), finding.message
        assert finding.suggestion and finding.suggestion.isascii(), finding.suggestion
    return found


def _export_refuses(
    body, code: str, tag_map: TagMap | None = None, allowed: tuple[str, ...] = ()
) -> None:
    """Export refuses *code*, and passes once it is allowed (with *allowed*, the
    other findings of the form)."""
    prog, tag_map = _program(body), tag_map or TagMap()
    with pytest.raises(LadderExportError, match=code):
        pyrung_to_ladder(prog, tag_map, allow=allowed)
    assert pyrung_to_ladder(prog, tag_map, allow=(code, *allowed)).main_rows


# --- A constant into bit memory -----------------------------------------------------


@pytest.mark.parametrize("value", [2, 1, 0, False])
@pytest.mark.parametrize("bit", [c[1], y[1]], ids=["C", "Y"])
def test_a_constant_copied_into_a_bit_is_refused(value, bit):
    def body():
        with Rung(x[1]):
            copy(value, bit)

    # copy(False, bit) also warns when the program is built: use reset().
    warned = (
        pytest.warns(UserWarning, match="use reset") if value is False else contextlib.nullcontext()
    )
    with warned:
        (finding,) = _found(body, CLK_COPY_CONSTANT_INTO_BIT)
        _export_refuses(body, CLK_COPY_CONSTANT_INTO_BIT)
    assert f"copy({value!r}, ...) into {bit.name.rstrip('0123456789')}" in finding.message
    assert "takes no constant into bit memory" in finding.message


@pytest.mark.parametrize("value", [2, True, 0, False])
def test_a_constant_filled_into_bits_is_refused(value):
    def body():
        with Rung(x[1]):
            fill(value, c.select(1, 4))

    (finding,) = _found(body, CLK_COPY_CONSTANT_INTO_BIT)
    assert "CLICK's Fill" in finding.message
    _export_refuses(body, CLK_COPY_CONSTANT_INTO_BIT)


def test_a_constant_into_a_register_and_a_bit_into_a_bit_pass():
    def body():
        with Rung(x[1]):
            copy(2, ds[1])
            copy(2.7, ds[2])
            copy(c[1], c[2])
            fill(0, ds.select(10, 14))
            fill(7, dd.select(1, 2))

    assert _found(body, CLK_COPY_CONSTANT_INTO_BIT) == []


# --- A bit in a Math formula --------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: c[1] + 5,
        lambda: c[1],
        lambda: (ds[1] + c[5]) * 2,
        lambda: ds[1] + c.select(1, 4).sum() + c[9],
    ],
    ids=["sum", "bare", "nested", "beside-a-bit-sum"],
)
def test_a_bit_in_a_math_formula_is_refused(build):
    def body():
        with Rung(x[1]):
            math(build(), ds[100])

    (finding,) = _found(body, CLK_MATH_BIT_OPERAND)
    assert "reads the bit" in finding.message
    assert "except in a SUM over a bit range" in finding.message
    _export_refuses(body, CLK_MATH_BIT_OPERAND)


def test_each_bit_is_named_once():
    def body():
        with Rung(x[1]):
            math(c[1] + c[2] * c[1], ds[100])

    (finding,) = _found(body, CLK_MATH_BIT_OPERAND)
    assert "reads the bits C1, C2;" in finding.message


@pytest.mark.parametrize(
    "build, dest",
    [
        (lambda: c.select(1, 8).sum(), ds[100]),
        (lambda: c.select(1, 8).sum() + ds[1], ds[100]),
        (lambda: ds[1] * 2 + dd[1], dd[100]),
        (lambda: dd[1] * 40000, dd[100]),
        (lambda: randint(1, 2) + dh[1], dh[100]),
    ],
    ids=["bit-sum", "bit-sum-in-formula", "registers", "wide-constant", "rand-in-hex"],
)
def test_a_formula_without_a_bit_operand_passes(build, dest):
    def body():
        with Rung(x[1]):
            math(build(), dest)

    assert _found(body, CLK_MATH_BIT_OPERAND) == []


def test_a_bit_rand_bound_is_the_rand_finding_only():
    def body():
        with Rung(x[1]):
            math(randint(c[1], 9), ds[100])

    assert _found(body, CLK_MATH_BIT_OPERAND) == []


# --- The Math Result ----------------------------------------------------------------


@pytest.mark.parametrize(
    "dest",
    [x[1], y[1], c[1], t[1], ct[1], sc[50], xd[1], yd[1], sd[29], txt[1]],
    ids=lambda dest: dest.name,
)
def test_a_math_result_outside_the_dialogs_banks_is_refused(dest):
    def body():
        with Rung(x[2]):
            math(ds[1] + 1, dest)

    (finding,) = _found(body, CLK_MATH_DEST_BANK)
    bank = dest.name.rstrip("0123456789")
    assert f"math() into {bank} at" in finding.message
    assert "Result in DS, DD, DF, TD or CTD, or DH in a hex Math" in finding.message
    # X, T, CT, XD and YD are also not writable, and XD or YD makes a mixed-mode Math.
    report = validate_click_program(_program(body), TagMap(), mode="strict")
    others = tuple({f.code for f in report.errors} - {CLK_MATH_DEST_BANK})
    _export_refuses(body, CLK_MATH_DEST_BANK, allowed=others)


def test_a_math_result_in_each_bank_the_dialog_takes_passes():
    def body():
        with Rung(x[1]):
            math(ds[1] + 1, ds[2])
            math(ds[1] + 1, dd[2])
            math(ds[1] + 1, df[2])
            math(ds[1] + 1, td[2])
            math(ds[1] + 1, ctd[2])
            math(dh[1] + 1, dh[2])

    assert _found(body, CLK_MATH_DEST_BANK) == []


def test_a_decimal_math_into_dh_is_refused_as_a_mixed_math():
    # The dialog refuses a decimal Math into DH (2026-11-06).  A Word destination makes the
    # Math decimal only through a non-Word operand, which CLK_MATH_MODE_MIXED flags, so the
    # destination check has nothing of its own to add.
    def body():
        with Rung(x[1]):
            math(ds[1] + 1, dh[3])

    assert _found(body, CLK_MATH_DEST_BANK) == []
    (finding,) = _found(body, CLK_MATH_MODE_MIXED)
    assert "mixes WORD" in finding.message
    _export_refuses(body, CLK_MATH_MODE_MIXED)


# --- Search banks -------------------------------------------------------------------


@pytest.mark.parametrize("result", [dd[1], td[1]], ids=["DD", "TD"])
def test_a_search_result_must_be_a_ds(result):
    def body():
        with Rung(x[1]):
            search(ds.select(1, 10) == 5, result=result, found=c[1])

    (finding,) = _found(body, CLK_SEARCH_RESULT_NOT_DS)
    assert "takes a DS Result only" in finding.message
    _export_refuses(body, CLK_SEARCH_RESULT_NOT_DS)


def test_a_search_range_outside_the_dialogs_banks_is_refused():
    def body():
        with Rung(x[1]):
            search(sd.select(1, 10) == 5, result=ds[100], found=c[1])

    (finding,) = _found(body, CLK_SEARCH_RANGE_BANK)
    assert "range in SD" in finding.message
    assert "DS, DD, DH, DF, TD, CTD, TXT" in finding.message
    _export_refuses(body, CLK_SEARCH_RANGE_BANK)


def test_a_search_over_each_bank_the_dialog_takes_passes():
    def body():
        with Rung(x[1]):
            search(ds.select(1, 10) == 5, result=ds[100], found=c[1])
            search(dd.select(1, 10) > 5, result=ds[101], found=c[2])
            search(dh.select(1, 10) == 5, result=ds[102], found=c[3])
            search(df.select(1, 10) >= 2.5, result=ds[103], found=c[4])
            search(td.select(1, 10) < 5, result=ds[104], found=c[5])
            search(ctd.select(1, 10) != 5, result=ds[105], found=c[6])
            search(txt.select(1, 10) == "A", result=ds[106], found=c[7])

    assert _found(body, CLK_SEARCH_RANGE_BANK) == []
    assert _found(body, CLK_SEARCH_RESULT_NOT_DS) == []


# --- Constant presets ----------------------------------------------------------------

Timers = Timer.clone("RefusalTimers", count=4)


def _timer_map(*extra) -> TagMap:
    return TagMap(
        [
            Timers._blocks["Done"].map_to(t.select(1, 4)),
            Timers._blocks["Acc"].map_to(td.select(1, 4)),
            *extra,
        ],
        include_system=False,
    )


@pytest.mark.parametrize(
    "build, count",
    [
        (lambda: flasher(y[1], on=(Timers[1], 0), off=(Timers[2], 10)), 1),
        (lambda: flasher(y[1], on=(Timers[1], 10), off=(Timers[2], -1)), 1),
        (lambda: flasher(y[1], on=(Timers[1], 0), off=(Timers[2], 0)), 2),
        (lambda: timed(y[1], Timers[1], 0), 1),
        (lambda: timed(y[1], Timers[1], -5), 1),
        (lambda: debounce(y[1], on=(Timers[1], -1), off=(Timers[2], 10)), 1),
        (lambda: debounce(y[1], on=(Timers[1], 10), off=(Timers[2], -1)), 1),
    ],
    ids=["flasher-on-0", "flasher-off-minus", "flasher-both-0", "timed-0", "timed-minus",
         "debounce-on-minus", "debounce-off-minus"],
)  # fmt: skip
def test_a_constant_out_coil_preset_below_the_dialogs_floor_is_refused(build, count):
    def body():
        with Rung(x[1]):
            build()

    found = _found(body, CLK_TIMER_PRESET_TOO_LOW, _timer_map())
    assert len(found) == count
    _export_refuses(body, CLK_TIMER_PRESET_TOO_LOW, _timer_map())


def test_out_coil_presets_the_dialog_takes_pass():
    preset = Int("OnTime")

    def body():
        with Rung(x[1]):
            flasher(y[1], on=(Timers[1], 1), off=(Timers[2], 1))
            timed(y[2], Timers[3], 1)
            debounce(y[3], on=(Timers[4], 0), off=(Timers[1], 0))
            # A register preset takes any value, 0 and below included.
            flasher(y[4], on=(Timers[2], preset), off=(Timers[3], preset))
            timed(y[5], Timers[4], preset)

    assert _found(body, CLK_TIMER_PRESET_TOO_LOW, _timer_map(preset.map_to(ds[1]))) == []


def test_a_negative_constant_drum_step_is_refused_and_zero_passes():
    step, done = Int("Step"), Bool("Done")
    tag_map = TagMap([step.map_to(ds[1]), done.map_to(c[1])], include_system=False)

    def body():
        with Rung(x[1]):
            time_drum(
                outputs=[y[1]],
                presets=[0, -1, 100],
                pattern=[[1], [0], [1]],
                current_step=step,
                accumulator=td[1],
                completion_flag=done,
            ).reset(x[2])
        with Rung(x[1]):
            time_event_drum(
                outputs=[y[2]],
                presets=[-2, None],
                events=[None, x[3]],
                pattern=[[1], [0]],
                current_step=ds[2],
                accumulator=td[2],
                completion_flag=c[2],
            ).reset(x[2])

    found = _found(body, CLK_TIMER_PRESET_TOO_LOW, tag_map)
    assert [f.location.rsplit(".", 1)[-1] for f in found] == ["presets[1]", "presets[0]"]
    assert "time_drum() preset -1" in found[0].message
    assert "time_event_drum() preset -2" in found[1].message


# --- For count ------------------------------------------------------------------------
# The editor refuses a negative constant count; pyrung's forloop() already refuses any
# constant below 1 (tests/core/test_forloop.py), so there is no finding to add.


def test_a_constant_for_count_of_70000_passes():
    def body():
        with Rung(x[1]):
            with forloop(70000):
                copy(1, ds[1])

    report = validate_click_program(_program(body), TagMap(), mode="strict")
    assert report.errors == ()
