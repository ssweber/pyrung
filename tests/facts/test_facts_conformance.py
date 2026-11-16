"""The facts of each conformance case hold on every scan of its runs.

``tests/facts/conformance.py`` says how a case runs, and ``tests/facts/check.py`` what each
fact claims.  :data:`KNOWN` lists the facts the runs refute that are not fixed yet.  Each
has a strict xfail, so the test fails once the bug is fixed, and the entry has to go.
"""

from __future__ import annotations

import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from pyrung.conformance._ladder import case_label
from tests.conformance import loop_corpus
from tests.facts.check import Report, writer_classes
from tests.facts.conformance import cases, check_case

_CASES = {case_label(group.name, case): (group, case) for group, case in cases()}

_PAIR = (
    "TD302 = TD305 = 10.  The rungs compare the two, and no pair of their domain values is"
    " equal, above 0 and below 20."
)

#: Facts the runs refute, by case: ``(fact, tag)`` -> the first scan that refutes it, and why.
KNOWN: dict[str, dict[tuple[str, str], str]] = {
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
}

#: Facts a case's run may leave unjudged.  A case runs its script once, with ``Cmd`` its
#: only input.  So a Done may never turn on or off, and an absorbed value may sit on a
#: threshold.  The idioms reach both with inputs aimed at each counter and timer.
MAY_GO_UNJUDGED = ("done spec", "absorbed value")
# The same script leaves some rungs off for the whole run, so an instruction on one may never
# write.  So a class only has to write in one of the cases that have it.


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
    unjudged = [k for k in report.unjudged if k not in MAY_GO_UNJUDGED]
    assert [k for k in unjudged if not k.endswith((" on", " off"))] == []


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


@cache
def _writer_classes(label: str) -> frozenset[str]:
    return frozenset(writer_classes(loop_corpus.case_alone(*_CASES[label]).program))


def _cases_with(kind: str) -> list[str]:
    return [label for label in sorted(_CASES) if kind in _writer_classes(label)]


@pytest.mark.parametrize("kind", sorted(set().union(*(_writer_classes(label) for label in _CASES))))
def test_each_writer_class_writes_in_some_case(kind: str) -> None:
    # A class the recorder can't see would pass every case's declared-writes check unseen.
    labels = _cases_with(kind)
    assert any(kind not in _report(label).silent_writers for label in labels), labels


def _prior_text_with_hash_seed(label: str, seed: int) -> str:
    code = f"from tests.facts.conformance import prior_text; print(prior_text({label!r}))"
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env={**os.environ, "PYTHONHASHSEED": str(seed)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_facts_do_not_change_with_the_hash_seed() -> None:
    # With hash seeds 0 and 1, this case once got two different sets of facts.
    label = "for_more/clock_edges_in_a_body"
    first, second = (_prior_text_with_hash_seed(label, seed) for seed in (0, 1))
    assert first == second
