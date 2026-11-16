"""The facts check catches a wrong fact.

Each test plants one wrong fact in a small program's facts, runs the check and expects it
to report that fact.  The same program with its real facts passes, so the planted fact is
what fails.  One more test checks that the report lists a stepping tag no run moves.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from pyrung import Bool, Int, Program, Rung, Timer, copy, forloop, math, on_delay, out, rise
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


def gauge() -> Any:
    """A level input from 0 to 100 and an alarm above 50."""
    level = Int("Level", external=True, min=0, max=100)
    with Program() as prog:
        with Rung(level > 50):
            out(Bool("LevelHigh"))
    return prog


def kept_count() -> Any:
    """A count of Go's edges from 0 to 5, and a copy of it taken when Store is on.

    A rung reads the copy, so the copy is part of the state the facts describe.
    """
    go = Bool("Go", external=True)
    count = Int("Count")
    kept = Int("Kept")
    with Program() as prog:
        with Rung(rise(go), count < 5):
            math(count + 1, count)
        with Rung(count >= 5):
            copy(0, count)
        with Rung(Bool("Store", external=True)):
            copy(count, kept)
        with Rung(kept == 3):
            out(Bool("KeptThree"))
    return prog


def never_counts() -> Any:
    """A count whose rung reads a bit nothing writes, so the count never moves."""
    count = Int("NeverCount")
    with Program() as prog:
        with Rung(Bool("NeverOn")):
            math(count + 1, count)
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


@pytest.mark.parametrize("build", [stepper, delayed, gauge, kept_count])
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


def test_an_input_domain_without_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def plant(facts: Facts) -> None:
        dims = dict(facts.prior.nondeterministic_dims)
        assert 51 in dims["Level"]
        # Every level above 50 turns the alarm on, and 50 does not.
        dims["Level"] = tuple(v for v in dims["Level"] if v <= 50)
        _prior(facts, nondeterministic_dims=dims)

    assert ("input domain", "Level") in _failed(_run(monkeypatch, gauge, plant))


def test_a_count_called_absorbed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyrung.core.analysis.prove.absorb import _ThresholdAtomSpec, _ThresholdVectorSpec

    def plant(facts: Facts) -> None:
        # Kept copies Count itself, so a move of Count that keeps its sides shows.
        atom = _ThresholdAtomSpec(acc_name="Count", threshold=0, form="ge", mode="exact")
        spec = _ThresholdVectorSpec(acc_name="Count", kind="int_up", atoms=(atom,))
        _prior(facts, threshold_vector_specs=(*facts.prior.threshold_vector_specs, spec))

    assert ("absorbed value", "Count") in _failed(_run(monkeypatch, kept_count, plant))


def test_a_value_the_harness_reaches_called_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.facts.corpus import CORPUS, PREPARE

    name = "valve_with_limit_switch_watchdog"
    real = static_proofs.linked_feedback_block

    def block(tag: str, value: Any, proofs: Any) -> Any:
        if (tag, value) == ("Valve", True):
            return "a planted block"
        return real(tag, value, proofs)

    monkeypatch.setattr(static_proofs, "linked_feedback_block", block)
    report = check_program(name, CORPUS[name], seeds=SEEDS, scans=SCANS, prepare=PREPARE[name])
    assert ("linked block", repr(("Valve", True))) in _failed(report)


def _drop_writes(monkeypatch: pytest.MonkeyPatch, kind: type, keep: Callable[[Any], bool]) -> None:
    """Drop the targets of each *kind* instruction that *keep* rejects, in the facts' list too."""
    from pyrung.core.analysis import write_sites

    real = write_sites.instruction_write_targets

    def targets(instr: Any) -> Any:
        found = real(instr)
        return tuple(t for t in found if keep(t)) if isinstance(instr, kind) else found

    monkeypatch.setattr(write_sites, "instruction_write_targets", targets)
    monkeypatch.setattr(check, "instruction_write_targets", targets)


def test_a_write_the_instruction_does_not_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyrung.core.instruction.timers import OnDelayInstruction

    acc = Timer.clone("Delay").Acc.name
    _drop_writes(monkeypatch, OnDelayInstruction, lambda t: getattr(t, "name", None) != acc)
    report = check_program("delayed", delayed, seeds=SEEDS, scans=SCANS)
    assert ("declared writes", f"OnDelayInstruction {acc}") in _failed(report)


def counted_loop() -> Any:
    """A forloop that adds 1 to a total three times on each scan Go is on."""
    total = Int("LoopTotal")
    with Program() as prog:
        with Rung(Bool("Go", external=True)):
            with forloop(3):
                math(total + 1, total)
        with Rung(total > 5):
            copy(0, total)
    return prog


def test_a_loop_index_the_loop_does_not_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    # The executor runs the loop without calling its execute(), so this shows the recorder
    # still sees the index the loop writes.
    from pyrung.core.instruction.control import ForLoopInstruction

    real = check_program("counted_loop", counted_loop, seeds=SEEDS, scans=SCANS)
    assert real.violations == [] and real.silent_writers == []
    _drop_writes(monkeypatch, ForLoopInstruction, lambda _target: False)
    report = check_program("counted_loop", counted_loop, seeds=SEEDS, scans=SCANS)
    assert any(
        f == "declared writes" and k.startswith("ForLoopInstruction ") for f, k in _failed(report)
    )


def test_a_writer_the_recorder_never_sees_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without the loop runners wrapped, the recorder sees none of the loop's own writes.
    monkeypatch.setattr(check.WriteRecorder, "_LOOP_RUNNERS", ())
    report = check_program("counted_loop", counted_loop, seeds=SEEDS, scans=SCANS)
    assert report.silent_writers == ["ForLoopInstruction"]


def test_absorbed_values_whose_moves_all_cross_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    real = check_program("delayed", delayed, seeds=SEEDS, scans=SCANS)
    assert "absorbed value" not in real.unjudged
    # Each move now crosses a threshold by the scan's end, so no move judges anything.
    monkeypatch.setattr(check.Prober, "_same_sides", lambda self, a, b: False)
    report = check_program("delayed", delayed, seeds=SEEDS, scans=SCANS)
    assert report.tally.judged.get("absorbed value crossed a threshold, not judged")
    assert "absorbed value" in report.unjudged


def test_a_stepping_tag_no_run_moves_is_listed() -> None:
    report = check_program("never_counts", never_counts, seeds=SEEDS, scans=SCANS)
    assert report.unmoved == ["NeverCount"]


def test_a_wrong_counter_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.facts.corpus import CORPUS

    real = check.compute_facts

    def planted(program: Any, plc: Any) -> Any:
        facts = real(program, plc)
        assert facts.done_presets == {"Batch_Done": 5}
        facts.done_presets = {"Batch_Done": 9}
        return facts

    monkeypatch.setattr(check, "compute_facts", planted)
    report = check_program("gated_counter", CORPUS["gated_counter"], seeds=SEEDS, scans=SCANS)
    assert ("done spec", "Batch_Done") in _failed(report)
