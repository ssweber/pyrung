"""The facts of each conformance case hold on every scan of its runs.

``tests/facts/conformance.py`` says how a case runs, and ``tests/facts/check.py`` what each
fact claims.  :data:`KNOWN` lists the facts the runs refute that are not fixed yet.  Each
has a strict xfail, so the test fails once the bug is fixed, and the entry has to go.
"""

from __future__ import annotations

from functools import cache

import pytest

from pyrung.conformance._ladder import case_label
from tests.facts.check import Report
from tests.facts.conformance import cases, check_case

_CASES = {case_label(group.name, case): (group, case) for group, case in cases()}

_SYSTEM = "The how() prior holds system tags at their defaults, so a copy of {} gets 0 only."
_PAIR = (
    "TD302 = TD305 = 10.  The rungs compare the two, and no pair of their domain values is"
    " equal, above 0 and below 20."
)
_NOT_FINITE = "A Real domain has no value that stands for {}."

#: Facts the runs refute, by case: ``(fact, tag)`` -> the first scan that refutes it, and why.
KNOWN: dict[str, dict[tuple[str, str], str]] = {
    "system/scan_counter_steps_by_one": {
        ("stateful domain", "System_Result2"): "scan 3.  " + _SYSTEM.format("sys.scan_counter"),
    },
    "rtc_set/date_and_time_apply": {
        ("stateful domain", "DD74"): "scan 3.  " + _SYSTEM.format("rtc.year4"),
        ("stateful domain", "DD75"): "scan 3.  " + _SYSTEM.format("rtc.hour"),
        ("stateful domain", "DD84"): "scan 3.  " + _SYSTEM.format("rtc.month"),
        ("stateful domain", "DD85"): "scan 3.  " + _SYSTEM.format("rtc.day"),
    },
    "counters/count_down_done_at_preset": {
        ("stateful domain", "CTD101"): (
            "scan 3.  The domain of the down counter's Acc is (0,), but a copy stages it at 5"
            " and the counter counts it down from there."
        ),
    },
    "reset_pins/rton_reset_held": {
        ("stateful domain", "TD302"): "scan 43.  " + _PAIR,
        ("stateful domain", "TD305"): "scan 43.  " + _PAIR,
    },
    "drum_ds/ds_preset_ends": {
        ("scan-local", "DS4016"): (
            "scan 20.  Slice elision drops DS4016, but the time drum reads it as a step"
            " preset on scans that do not write it."
        ),
    },
    "store_probe/non_finite_into_word": {
        ("stateful domain", "DF427"): "scan 4.  " + _NOT_FINITE.format("+Inf"),
    },
    "word_floor/below_int_and_nan": {
        ("stateful domain", "DF32"): "scan 3.  " + _NOT_FINITE.format("NaN"),
    },
    "modbus_raw/raw_values": {
        ("stateful domain", "DF114"): "scan 3.  " + _NOT_FINITE.format("NaN"),
        ("stateful domain", "DF115"): "scan 3.  " + _NOT_FINITE.format("NaN"),
    },
}


@cache
def _report(label: str) -> Report:
    return check_case(*_CASES[label])


@pytest.mark.parametrize("label", sorted(_CASES))
def test_facts_hold(label: str) -> None:
    report = _report(label)
    assert report.intractable is None
    assert report.tally.scans > 0 and report.tally.judged
    known = KNOWN.get(label, {})
    new = [str(v) for v in report.violations if (v.fact, v.key) not in known]
    assert not new, "\n".join(new)


@pytest.mark.parametrize(
    ("label", "fact", "key"),
    [
        pytest.param(
            label,
            fact,
            key,
            marks=pytest.mark.xfail(strict=True, reason=f"{label}: {fact} of {key}, {why}"),
        )
        for label, failures in sorted(KNOWN.items())
        for (fact, key), why in sorted(failures.items())
    ],
)
def test_known_failure_is_fixed(label: str, fact: str, key: str) -> None:
    assert (fact, key) not in {(v.fact, v.key) for v in _report(label).violations}
