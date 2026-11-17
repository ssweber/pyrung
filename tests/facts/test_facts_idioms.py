"""Check that the facts of each idiom in the corpus hold on every scan of its runs.

``tests/facts/check.py`` says what each fact claims and how a run checks it. A fact that a
run refutes is a soundness bug. :data:`KNOWN` lists the ones that are not fixed yet. Each
has a strict xfail, so the test fails once the bug is fixed, and then the entry has to go.

A fact that the runs never reach passes unchecked. So each kind of fact that an idiom has
must be judged in some scan, and each Done must turn both on and off. :data:`UNJUDGED`
lists the facts that the runs cannot reach, and why.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any

import pytest

from pyrung.core import PLC
from tests.facts.check import RandomInputs, Report, Step, check_program
from tests.facts.corpus import CORPUS, OPENING, PREPARE

# recipe_mode_compared_with_a_table_entry stores values past RecipeMode's choices on purpose.
pytestmark = pytest.mark.filterwarnings("ignore:BoundsViolation. RecipeMode=:UserWarning")

#: The seeds of the random runs, one run for each.
SEEDS = (1, 2)

#: Scans of each random walk, as ``check_program`` runs by default.
SCANS = 600

Driver = Callable[[PLC, Step, RandomInputs], None]

#: The tally of a domain swap that crossed a comparison.
_CROSSED = "domain swap crossed a comparison, not judged"

#: The facts that the runs refute, by idiom: ``(fact, tag)`` -> the first scan that refutes it.
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
    "real_reading_compared_with_a_table_entry": {
        ("input domain", "HmiTemp"): (
            "scan 2.  HmiTemp = 1.5 is copied into the table entry TempIn is compared with."
            "  HmiTemp's domain lists whole numbers only, since no rung compares it."
        ),
    },
    "shown_count_compared_with_an_hmi_count": {
        ("stateful domain", "Boxes_Acc"): (
            "scan 10.  BoxesShown copies the count, and a rung compares it with HmiBoxes."
            "  The count's domain ends at its preset, 3, so 5 is no value of it."
        ),
    },
    "text_copy_skipped_on_a_bad_digit": {
        ("scan-local", "KeyLevel"): (
            "scan 10.  The copy writes nothing when KeyChar is not a digit, so KeyLevel keeps"
            " the value of the scan before.  The facts take the copy for a write on every"
            " scan."
        ),
    },
}

#: The facts that the runs cannot reach, by idiom. Each maps a kind of fact, or ``"<Done> on"``,
#: to why.
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
}


#: Instruction classes that write nothing in an idiom's runs, by idiom, with why. Any other
#: class that writes nothing is one that the check cannot see write.
SILENT: dict[str, dict[str, str]] = {
    "received_value_the_ladder_clears": {
        "ModbusReceiveInstruction": "no peer answers in a run, so the run sets the payload",
    },
    "received_count_the_ladder_adds_to": {
        "ModbusReceiveInstruction": "no peer answers in a run, so the run sets the payload",
    },
}


@cache
def _report(name: str) -> Report:
    opening = OPENING.get(name)
    return check_program(
        name,
        CORPUS[name],
        seeds=SEEDS,
        prepare=PREPARE.get(name),
        driver=None if opening is None else _opening_then_walk(opening),
    )


def _opening_then_walk(opening: tuple[dict[str, Any], ...]) -> Driver:
    def drive(_plc: PLC, one_scan: Step, walk: RandomInputs) -> None:
        for patch in opening:
            one_scan(patch)
        for _ in range(SCANS):
            one_scan(walk.next())

    return drive


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
    # A swap that crosses a comparison judges nothing. A domain that grew too coarse would
    # excuse more and more swaps that way.
    judged = report.tally.judged
    swapped = sum(count for kind, count in judged.items() if kind.endswith(", swapped"))
    assert judged.get(_CROSSED, 0) * 10 <= swapped
    # The runs move every stepping tag, so they reach the values that its writes produce.
    assert not report.unmoved
    assert sorted(report.unjudged) == sorted(UNJUDGED.get(name, {}))
    assert report.silent_writers == sorted(SILENT.get(name, {}))


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
