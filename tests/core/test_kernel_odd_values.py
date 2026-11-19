"""The compiled kernel and stepping agree on odd values in math() and copy().

CLICK has no form for most of these values, but pyrung runs them when you step.

The compiled kernel runs long runs, and it replays a scan for ``history.at`` once the scan
has left the recent-state cache.  The kernel compiles only the forms it runs the way
stepping does.  A program with any other value form gets no kernel, so pyrung steps its
scans and its replays.
"""

from __future__ import annotations

import math as pymath
import warnings
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import PLC, Program, Timer, copy, math, rung
from pyrung.click import ClickBlocks
from pyrung.core import Bool, Int, jit_stats, named_array, to_binary, to_text, udt

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


@udt()
class Motor:
    speed: Int
    run: Bool


@named_array(Int, count=2, stride=2)
class Pair:
    a = 0
    b = 0


# DS100 points at TXT1, and TXT1 and TXT2 hold digits.  DS102 points below every bank,
# and DS103 points past the end of DS and TXT.
DATA = {
    x[3]: True,
    ds[1]: 4,
    ds[100]: 1,
    ds[101]: 2,
    ds[102]: 0,
    ds[103]: 5000,
    txt[1]: "7",
    txt[2]: "2",
}

# Each form, and what some tags hold after a scan.  A flag the form does not name is off.
KERNEL_FORMS: dict[str, tuple[Callable[[], Any], dict[str, Any]]] = {
    "None": (lambda: math(None, ds[1]), {"DS1": 4, "fault.address_error": True}),
    "text '1.5'": (lambda: math("1.5", df[3]), {"DF3": 1.5}),
    "text '1e3'": (lambda: math("1e3", df[3]), {"DF3": 1000.0}),
    "text '1e40'": (lambda: math("1e40", df[3]), {"DF3": pymath.inf}),
    "text '-1e40'": (lambda: math("-1e40", df[3]), {"DF3": -pymath.inf}),
    "text 'nan'": (lambda: math("nan", df[3]), {"DF3": pymath.nan}),
    "text 'inf'": (lambda: math("inf", df[3]), {"DF3": pymath.inf}),
    "Char tag": (lambda: math(txt[1], df[3]), {"DF3": 7.0}),
    "Char pointer": (lambda: math(txt[ds[100]], df[3]), {"DF3": 7.0}),
    "Char plus Char": (lambda: math(txt[1] + txt[2], df[3]), {"DF3": 72.0}),
    "Char times 2": (lambda: math(txt[1] * 2, df[3]), {"DF3": 77.0}),
    "Char sum of a sum": (lambda: math((txt[1] + txt[2]) + txt[1], df[3]), {"DF3": 727.0}),
    "to_text() into a Bool": (
        lambda: copy(ds[1], c[3], convert=to_text()),
        {"C3": False, "fault.out_of_range": True},
    ),
    "to_text() into a Real": (
        lambda: copy(ds[1], df[3], convert=to_text()),
        {"DF3": 0.0, "fault.out_of_range": True},
    ),
    "to_binary of '7' into an Int": (
        lambda: copy(55, ds[3], convert=to_binary),
        {"DS3": 0, "fault.out_of_range": True},
    ),
    "to_binary into a Bool pointer": (
        lambda: copy(ds[1], c[ds[101]], convert=to_binary),
        {"C2": False, "fault.out_of_range": True},
    ),
    "to_text() from below DS into an Int": (
        lambda: copy(ds[ds[102]], ds[3], convert=to_text()),
        {"DS3": 0, "fault.out_of_range": True},
    ),
    "to_text() from past DS into an Int": (
        lambda: copy(ds[ds[103]], ds[3], convert=to_text()),
        {"DS3": 0},
    ),
    "to_text() from below DS to past TXT": (
        lambda: copy(ds[ds[102]], txt[ds[103]], convert=to_text()),
        {},
    ),
    "copy from past DS to below DS": (
        lambda: copy(ds[ds[103]], ds[ds[102]]),
        {"fault.address_error": True},
    ),
}

# Each form stores a value when you step it, so its scans run.
DECLINED_FORMS: dict[str, Callable[[], Any]] = {
    "Timer[1] into a Char": lambda: copy(Timer[1], txt[3]),
    "Timer into a Bool": lambda: copy(Timer, c[3]),
    "udt into a Bool": lambda: copy(Motor, c[3]),
    "udt instance into a Bool": lambda: copy(Motor[1], c[3]),
    "named_array into a Bool": lambda: copy(Pair, c[3]),
    "Block into a Bool": lambda: copy(ds, c[3]),
    "range into a Bool": lambda: math(ds.select(1, 2), c[3]),
    "slot into a Bool": lambda: copy(ds.slot(1), c[3]),
    "inf into an Int": lambda: copy(float("inf"), ds[3]),
    "nan into a Bool": lambda: copy(float("nan"), c[3]),
    "bytes into an Int": lambda: copy(b"7", ds[3]),
}


def _program(build: Callable[[], Any]) -> Program:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Program(strict=False) as program:
            with rung(x[3]):
                build()
    return program


def _stepped(program: Program, scans: int) -> tuple[PLC, dict[int, Any]]:
    """Step *scans* scans with a one-second cache, and return the PLC and each state."""
    plc = PLC(program, dt=0.01, cache="1s")
    plc.patch(DATA)
    states = {}
    for _ in range(scans):
        plc.step()
        states[plc.current_state.scan_id] = plc.current_state
    return plc, states


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, float) and pymath.isnan(left):
        return isinstance(right, float) and pymath.isnan(right)
    return left == right


@pytest.mark.parametrize("name", list(KERNEL_FORMS))
def test_history_replays_the_form_through_the_kernel_as_stepping_ran_it(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, expected = KERNEL_FORMS[name]
    plc, states = _stepped(_program(build), 150)
    assert plc._compiled_replay_supported_kernel() is not None

    def _interpreted(_start: int, _end: int) -> list[Any]:
        raise AssertionError("the replay should go through the kernel")

    monkeypatch.setattr(plc, "_replay_range_interpreted", _interpreted)
    for scan in (1, 5, 20):
        replayed = plc.history.at(scan)
        assert replayed.tags.keys() == states[scan].tags.keys()
        for key, value in states[scan].tags.items():
            assert _same(replayed.tags[key], value), key
    tags = states[5].tags
    flags = {"fault.address_error": False, "fault.out_of_range": False}
    for key, value in {**flags, **expected}.items():
        assert _same(tags.get(key, 0), value), key


@pytest.mark.parametrize("name", list(KERNEL_FORMS))
def test_a_long_run_of_the_form_runs_in_the_kernel_and_lands_where_stepping_does(
    name: str, loop_from_first_scan: None
) -> None:
    build, _ = KERNEL_FORMS[name]
    program = _program(build)
    _, states = _stepped(program, 30)
    plc = PLC(program, dt=0.01)
    plc.patch(DATA)
    with jit_stats.collect() as stats:
        plc.run_for(0.3)
    # Only the first scan steps.  A kernel scan that raised would be undone and stepped.
    assert stats.scans["interpreted"] == 1
    assert stats.scans["undone"] == 0
    assert plc.current_state.scan_id == 30
    for key, value in states[30].tags.items():
        assert _same(plc.current_state.tags.get(key, value), value), key


@pytest.mark.parametrize("name", list(DECLINED_FORMS))
def test_a_form_with_no_kernel_runs_and_replays_stepped(
    name: str, loop_from_first_scan: None
) -> None:
    program = _program(DECLINED_FORMS[name])
    plc, states = _stepped(program, 150)
    with warnings.catch_warnings():
        # The kernel build declines without the warning about a bug in pyrung.
        warnings.simplefilter("error", RuntimeWarning)
        assert plc._compiled_replay_supported_kernel() is None
    for scan in (1, 5, 20):
        assert plc.history.at(scan) == states[scan]
    plc.run_for(0.3)
    assert plc.current_state.scan_id == 180
