"""The facts of each idiom in the corpus hold on every scan of its runs.

``tests/facts/check.py`` says what each fact claims and how a run checks it.  A fact a run
refutes is a soundness bug.  :data:`KNOWN` lists the ones not fixed yet.  Each has a strict
xfail, so the test fails once the bug is fixed, and the entry has to go.

A fact the runs never reach passes unchecked.  So each kind of fact an idiom has must be
judged in some scan, and each Done must turn both on and off.  :data:`UNJUDGED` lists the
ones the runs cannot reach, and why.
"""

from __future__ import annotations

from functools import cache

import pytest

from tests.facts.check import Report, check_program
from tests.facts.corpus import CORPUS, PREPARE

#: Seeds of the random runs, one run each.
SEEDS = (1, 2)

#: Facts the runs refute, by idiom: ``(fact, tag)`` -> the first scan that refutes it.
KNOWN: dict[str, dict[tuple[str, str], str]] = {
    "funcdep_follows_its_source": {
        ("functional dep", "PosOffset"): (
            "scan 1 ends with PosOffset = 0 and 5 + 1 * Pos = 5.  PosOffset is elided, but"
            " the relation only holds once the rung has written it."
        ),
    },
    "funcdep_written_before_its_source_moves": {
        ("functional dep", "Lag"): (
            "scan 1 ends with Lag = 5 and 5 + 1 * Lead = 6.  The rung writes Lag before"
            " Lead moves, and the dependency check ignores that order."
        ),
    },
    "hmi_value_copied_after_ladder_default": {
        ("stateful domain", "WorkingSetpoint"): (
            "scan 12.  WorkingSetpoint copies HmiSetpoint, which the HMI can set to any"
            " value.  Its domain takes HmiSetpoint's written values only, 0 and 5."
        ),
    },
    "compared_input_copied_and_compared": {
        ("stateful domain", "HeldLevel"): (
            "scan 15.  HeldLevel copies RawLevel, and its domain takes RawLevel's split"
            " points at 10 only.  No value of it is above 50."
        ),
    },
}

#: Facts the runs cannot reach, by idiom: a kind of fact, or ``"<Done> on"`` -> why.
UNJUDGED: dict[str, dict[str, str]] = {
    "retentive_timer_with_reset": {
        "absorbed value": (
            "the Acc counts whole seconds up to 2, so every move from 1 crosses 0 or 2"
        ),
    },
    "scratch_register": {
        "absorbed value": "Tmp is 0 at the start of every scan, which is a threshold",
    },
    "stepper_with_math_advance": {
        "absorbed value": "State only holds 0..3, and each of them is a threshold",
    },
    "valve_with_limit_switch_watchdog": {
        "done spec": "the limit switch follows the valve, so the watchdog never finishes",
        "Stall_Done on": "the limit switch follows the valve, so the watchdog never finishes",
        "Stall_Done off": "Stall_Done never turns on",
    },
}


@cache
def _report(name: str) -> Report:
    return check_program(name, CORPUS[name], seeds=SEEDS, prepare=PREPARE.get(name))


def _found(report: Report) -> set[tuple[str, str]]:
    return {(v.fact, v.key) for v in report.violations}


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_facts_hold(name: str) -> None:
    report = _report(name)
    assert report.intractable is None
    assert report.tally.scans > 0 and report.tally.judged
    known = KNOWN.get(name, {})
    new = [str(v) for v in report.violations if (v.fact, v.key) not in known]
    assert not new, "\n".join(new)
    # The runs move every stepping tag, so they reach the values its writes produce.
    assert not report.unmoved
    assert sorted(report.unjudged) == sorted(UNJUDGED.get(name, {}))


@pytest.mark.parametrize(
    ("name", "fact", "key"),
    [
        pytest.param(
            name,
            fact,
            key,
            marks=pytest.mark.xfail(strict=True, reason=f"{name}: {fact} of {key}, {why}"),
        )
        for name, failures in sorted(KNOWN.items())
        for (fact, key), why in sorted(failures.items())
    ],
)
def test_known_failure_is_fixed(name: str, fact: str, key: str) -> None:
    assert (fact, key) not in _found(_report(name))
