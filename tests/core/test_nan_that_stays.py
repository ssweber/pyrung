"""A Real that holds NaN and stays NaN has not changed, whichever NaN object a state holds.

``copy()`` keeps the NaN it copies, ``math()`` computes a new NaN object each scan, and each
patch is a new one too.  Every reader of history must count none of them as a change.
``cause()`` and ``effect()`` follow a NaN that one rung writes and a later rung reads as they
follow a number.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, Real, Rung, copy, math
from pyrung.core import scan_trace
from pyrung.core.analysis.causal.projected import _values_equal
from pyrung.core.coast_run import coast_until
from pyrung.core.state import value_changed
from pyrung.dap.handlers.history_seek import _window_entry

NAN = float("nan")
Source = Real("Source", external=True)
Copied = Real("Copied")
Scaled = Real("Scaled")
Steps = Int("Steps")
TAGS = ("Source", "Copied", "Scaled")


def _plc() -> PLC:
    with Program(strict=False) as program:
        with Rung():
            copy(Source, Copied)
            math(Source * 1.0, Scaled)
            math(Steps + 1, Steps)
    plc = PLC(program, dt=0.01)
    plc.patch({"Source": NAN})
    return plc


@pytest.mark.parametrize(
    ("old", "new", "changed"),
    [
        (NAN, NAN, False),
        (NAN, float("nan"), False),
        (NAN, -NAN, False),
        (0.0, -0.0, False),
        (0, False, False),
        (None, None, False),
        (1.0, NAN, True),
        (NAN, 1.0, True),
        (None, NAN, True),
        (1, 2, True),
    ],
)
def test_value_changed(old: Any, new: Any, changed: bool) -> None:
    assert value_changed(old, new) is changed


@pytest.mark.parametrize(
    ("left", "right", "equal"), [(NAN, float("nan"), True), (1.0, NAN, False), (2, 2.0, True)]
)
def test_a_projected_cause_holds_a_nan_equal_to_a_nan(left: Any, right: Any, equal: bool) -> None:
    assert _values_equal(left, right) is equal


def test_cause_and_previous_transition_find_the_scan_it_became_nan() -> None:
    plc = _plc()
    for _ in range(20):
        plc.step()
    for tag in TAGS:
        transition = plc.history.previous_transition(tag)
        assert transition is not None and transition.scan_id == 1, tag
        chain = plc.cause(tag)
        assert chain is not None and chain.effect.scan_id == 1, tag
        assert plc.cause(tag, 10) is None, tag


def test_diff_monitors_and_the_dap_window_see_no_change() -> None:
    plc = _plc()
    calls: list[tuple[str, int]] = []
    for tag in TAGS:
        plc.monitor(tag, lambda _now, _before, tag=tag: calls.append((tag, plc.state.scan_id)))
    for _ in range(20):
        plc.step()
    # A second patch is a new NaN over the old one.
    plc.patch({"Source": float("nan")})
    plc.step()
    assert calls == [(tag, 1) for tag in TAGS]
    assert set(plc.diff(5, 21)) == {"Steps"}
    adapter: Any = type("Adapter", (), {"_format_value": staticmethod(repr)})()
    assert _window_entry(adapter, plc.history, list(TAGS), 5, 21)["changes"] == {}
    assert plc.history.previous_transition("Source").scan_id == 1


def test_the_loop_runs_a_nan_that_stays() -> None:
    plc = _plc()
    plc.step()
    before = scan_trace.TOTALS["loop scans"]
    coast_until(plc, lambda _state: False, budget=500, predicate_reads=frozenset())
    assert scan_trace.TOTALS["loop scans"] > before
    for tag in TAGS:
        assert plc.history.previous_transition(tag).scan_id == 1, tag


Mid = Real("Mid")
En = Bool("En", external=True)


def _same_scan_plc(value: float) -> PLC:
    # Rung 2 copies Mid into Copied, and rung 3 overwrites it in the same scan.
    with Program(strict=False) as program:
        with Rung(En):
            math(Source * 1.0, Mid)
        with Rung(En):
            copy(Mid, Copied)
        with Rung(Mid != 5.0):
            copy(Mid, Copied)
        with Rung():
            math(Steps + 1, Steps)
    plc = PLC(program, dt=0.01)
    plc.patch({"Source": value})
    plc.step()
    plc.step()
    plc.patch({"En": True})
    for _ in range(5):
        plc.step()
    return plc


def _as_number(chain: Any) -> str:
    return str(chain).replace("nan", "2.0")


@pytest.mark.parametrize("tag", ["Mid", "Copied"])
def test_effect_follows_a_nan_as_it_follows_a_number(tag: str) -> None:
    expected = _same_scan_plc(2.0).effect(tag, 3)
    chain = _same_scan_plc(NAN).effect(tag, 3)
    assert expected is not None and chain is not None
    assert _as_number(chain) == str(expected)


def test_cause_lists_the_rungs_it_lists_for_a_number() -> None:
    expected = _same_scan_plc(2.0).cause("Copied")
    chain = _same_scan_plc(NAN).cause("Copied")
    assert _as_number(chain) == str(expected)


@pytest.mark.parametrize("tag", ["Mid", "Copied"])
def test_cause_names_the_write_that_stored_the_nan(tag: str) -> None:
    expected = _same_scan_plc(2.0).cause(tag)
    chain = _same_scan_plc(NAN).cause(tag)
    assert expected is not None and chain is not None
    assert expected.effect.occurrence_ordinal is not None
    assert chain.effect.occurrence_ordinal == expected.effect.occurrence_ordinal
