"""The facts check catches a wrong fact.

Each test plants one wrong fact in a small program's facts, runs the check and expects it
to report that fact.  The same program with its real facts passes, so the planted fact is
what fails.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, copy, math, on_delay, out
from pyrung.core.analysis.pilot import static_proofs
from tests.facts import check
from tests.facts.check import Facts, Report, check_program

SEEDS = (1,)
SCANS = 300
DONE = Timer.clone("Delay").Done.name


def stepper() -> Any:
    """Step runs 0 -> 1 -> 2 while Go is on and goes back to 0 when it drops."""
    go = Bool("Go", external=True)
    step = Int("Step")
    shown = Int("StepShown")
    with Program() as prog:
        with Rung(go, step == 1):
            copy(2, step)
        with Rung(go, step == 0):
            copy(1, step)
        with Rung(~go):
            copy(0, step)
        with Rung():
            math(step + 10, shown)
    return prog


def delayed() -> Any:
    """A 50 ms on-delay behind an input, and a bit that shows the input."""
    go = Bool("Go", external=True)
    running = Bool("Running")
    timer = Timer.clone("Delay")
    with Program() as prog:
        with Rung(go):
            out(running)
            on_delay(timer, 50)
    return prog


def _run(monkeypatch: pytest.MonkeyPatch, build: Callable[[], Any], plant: Any) -> Report:
    real = check.compute_facts

    def planted(program: Any, plc: Any) -> Any:
        facts = real(program, plc)
        assert isinstance(facts, Facts)
        plant(facts)
        return facts

    monkeypatch.setattr(check, "compute_facts", planted)
    return check_program(build.__name__, build, seeds=SEEDS, scans=SCANS)


def _prior(facts: Facts, **changes: Any) -> None:
    facts.prior = dataclasses.replace(facts.prior, **changes)


def _failed(report: Report) -> set[tuple[str, str]]:
    return {(v.fact, v.key) for v in report.violations}


@pytest.mark.parametrize("build", [stepper, delayed])
def test_the_real_facts_pass(build: Callable[[], Any]) -> None:
    report = check_program(build.__name__, build, seeds=SEEDS, scans=SCANS)
    assert report.violations == []


def test_a_produced_domain_without_a_reached_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        assert facts.produced["Step"] == (0, 1, 2)
        facts.produced["Step"] = (0, 1)

    assert ("produced domain", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_stateful_domain_without_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        dims = dict(facts.prior.stateful_dims)
        assert 2 in dims["Step"]
        # Every value but 0 and 1 answers both comparisons the way 2 does.
        dims["Step"] = (0, 1)
        _prior(facts, stateful_dims=dims)

    assert ("stateful domain", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_stateful_tag_called_scan_local(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        names = facts.prior.stateful_names
        assert "Step" in names
        _prior(facts, stateful_names=tuple(n for n in names if n != "Step"))

    assert ("scan-local", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_wrong_functional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        _prior(
            facts,
            elided_tags={**facts.prior.elided_tags, "StepShown": "functional_dep"},
            functional_dep_projections={
                **facts.prior.functional_dep_projections,
                "StepShown": ("Step", 1, 11),
            },
        )

    assert ("functional dep", "StepShown") in _failed(_run(monkeypatch, stepper, plant))


def test_a_changing_tag_called_a_reference_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        facts.reference_constants = facts.reference_constants | {"Step"}

    assert ("reference constant", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_wrong_rest_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        facts.transient_rests = {**facts.transient_rests, "Step": 0}

    assert ("transient rest", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_wrong_init_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        facts.init_constant_reps = {**facts.init_constant_reps, "Step": "Go"}

    assert ("init constant", "Step") in _failed(_run(monkeypatch, stepper, plant))


def test_a_value_no_rung_produces(monkeypatch: pytest.MonkeyPatch) -> None:
    real = static_proofs.ProofFacts.producers

    def producers(self: Any, tag: str, value: Any) -> Any:
        return () if (tag, value) == ("Step", 2) else real(self, tag, value)

    monkeypatch.setattr(static_proofs.ProofFacts, "producers", producers)
    report = check_program("stepper", stepper, seeds=SEEDS, scans=SCANS)
    assert ("producers", repr(("Step", 2))) in _failed(report)


def test_two_bits_kept_apart_that_hold_together(monkeypatch: pytest.MonkeyPatch) -> None:
    real = static_proofs.exclusion_evidence

    def evidence(proofs: Any, a: Any, b: Any) -> Any:
        if {a, b} == {("Running", True), (DONE, True)}:
            return "a planted exclusion"
        return real(proofs, a, b)

    monkeypatch.setattr(static_proofs, "exclusion_evidence", evidence)
    report = check_program("delayed", delayed, seeds=SEEDS, scans=SCANS)
    assert any(fact == "exclusion" for fact, _key in _failed(report))


def test_a_wrong_timer_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        assert facts.done_presets == {DONE: 50}
        facts.done_presets = {DONE: 80}

    assert any(fact == "done spec" for fact, _key in _failed(_run(monkeypatch, delayed, plant)))
