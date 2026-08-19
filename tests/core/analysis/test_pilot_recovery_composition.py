"""Contracts for the bounded correction-composition recovery primitive."""

from __future__ import annotations

import pytest

from pyrung.core.analysis.pilot.recovery import (
    CompositionBudget,
    CompositionTermination,
    Extend,
    RecoveryInvariantViolation,
    Reject,
    Retry,
    Stop,
    Succeed,
    assert_recovery_disposable_state,
    compose_corrections,
)


def test_composition_budget_counts_admitted_candidates_exactly() -> None:
    attempted: list[str] = []
    built: list[str] = []

    def attempt(candidate, _ctx):
        attempted.append(candidate)
        extended = candidate + "x"
        return Extend(
            ("cause", extended),
            lambda: built.append(extended) or extended,
            Reject("cycle"),
            Reject("unresolved"),
        )

    result = compose_corrections(
        "A",
        budget=CompositionBudget(3),
        attempt=attempt,
        budget_exhausted=lambda candidate: f"budget:{candidate}",
    )

    assert result.termination is CompositionTermination.BUDGET
    assert result.attempts == 3
    assert result.value == "budget:Axxx"
    assert attempted == ["A", "Ax", "Axx"]
    assert built == ["Ax", "Axx", "Axxx"]


def test_repeated_extension_identity_stops_before_deriving_a_cycle() -> None:
    builds = 0

    def attempt(candidate, _ctx):
        nonlocal builds

        def build():
            nonlocal builds
            builds += 1
            return candidate + 1

        return Extend(
            "same-cause",
            build,
            Reject(f"cycle:{candidate}"),
            Reject("unresolved"),
        )

    result = compose_corrections(
        0,
        budget=CompositionBudget(5),
        attempt=attempt,
        budget_exhausted=lambda candidate: f"budget:{candidate}",
    )

    assert result.termination is CompositionTermination.CYCLE
    assert result.value == "cycle:1"
    assert result.attempts == 2
    assert builds == 1


def test_rejection_rolls_back_to_a_sibling_and_preserves_success_handoff() -> None:
    trace: list[tuple[str, str]] = []

    def attempt(candidate, _ctx):
        trace.append(("attempt", candidate))
        if candidate == "root":
            return Reject("root-rejected")
        return Succeed("ordinary-outer-bearing")

    def rollback(candidate, rejection, ctx):
        trace.append(("rollback", f"{candidate}:{rejection}"))
        assert ctx.consume_auxiliary()
        return Extend(
            "sibling",
            lambda: "sibling",
            Reject("cycle"),
            Reject("unresolved"),
        )

    result = compose_corrections(
        "root",
        budget=CompositionBudget(3),
        attempt=attempt,
        budget_exhausted=lambda candidate: f"budget:{candidate}",
        rollback_to_sibling=rollback,
    )

    assert result.termination is CompositionTermination.SUCCESS
    assert result.value == "ordinary-outer-bearing"
    assert result.attempts == 3
    assert trace == [
        ("attempt", "root"),
        ("rollback", "root:root-rejected"),
        ("attempt", "sibling"),
    ]


def test_cycle_can_retry_an_exact_fallback_without_spending_another_attempt() -> None:
    attempted: list[str] = []

    def attempt(candidate, _ctx):
        attempted.append(candidate)
        if candidate == "exact":
            return Succeed("confirmed-exact")
        return Extend(
            "repeated-cause",
            lambda: "wide-again",
            Retry("exact"),
            Reject("unresolved"),
        )

    result = compose_corrections(
        "wide",
        budget=CompositionBudget(2),
        attempt=attempt,
        budget_exhausted=lambda candidate: f"budget:{candidate}",
        initial_identity="repeated-cause",
    )

    assert result.termination is CompositionTermination.SUCCESS
    assert result.value == "confirmed-exact"
    assert result.attempts == 1
    assert attempted == ["wide", "exact"]


def test_repeated_retries_remain_bounded_after_one_free_fallback() -> None:
    attempted: list[int] = []

    def attempt(candidate, _ctx):
        attempted.append(candidate)
        return Retry(candidate + 1)

    result = compose_corrections(
        0,
        budget=CompositionBudget(2),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
    )

    assert result.termination is CompositionTermination.BUDGET
    assert result.attempts == 2
    assert result.value == 4
    assert attempted == [0, 1, 2, 3]


def test_recovery_transaction_cannot_compose_recursively() -> None:
    def attempt(candidate, _ctx):
        with pytest.raises(RecoveryInvariantViolation, match="recursively compose"):
            compose_corrections(
                candidate,
                budget=CompositionBudget(1),
                attempt=lambda value, _nested_ctx: Succeed(value),
                budget_exhausted=lambda value: value,
            )
        return Stop(candidate)

    result = compose_corrections(
        "candidate",
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
    )

    assert result.termination is CompositionTermination.STOPPED


def test_recovery_rejects_outer_transition_before_state_mutation() -> None:
    from pyrung.core.analysis.pilot.attempt_transition import transition_once

    outer_state = object()

    def attempt(candidate, _ctx):
        with pytest.raises(
            RecoveryInvariantViolation,
            match="execute a transition on the outer PILOT world",
        ):
            transition_once(outer_state, None, None, None)
        return Stop(candidate)

    compose_corrections(
        "candidate",
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
    )


def test_protected_outer_state_cannot_self_grant_disposable_capability() -> None:
    outer_state = object()
    disposable_clone = object()

    def attempt(candidate, ctx):
        with pytest.raises(
            RecoveryInvariantViolation,
            match="register the outer PILOT world",
        ):
            ctx.register_disposable_state(outer_state)
        ctx.register_disposable_state(disposable_clone)
        assert_recovery_disposable_state(disposable_clone, "commit")
        return Stop(candidate)

    compose_corrections(
        "candidate",
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
        protected_states=(outer_state,),
    )


def test_recovery_rejects_outer_orchestration_owners() -> None:
    from pyrung.core.analysis.pilot.correction_lifecycle import (
        _install_confirmed_correction,
    )
    from pyrung.core.analysis.pilot.pilot import (
        _monitor_committed_trial,
        _pilot_loop_events,
    )
    from pyrung.core.analysis.pilot.skiff import run_skiff_scan

    def attempt(candidate, _ctx):
        forbidden = (
            lambda: next(_pilot_loop_events(None, None)),
            lambda: next(_monitor_committed_trial(None, None, None, None)),
            lambda: _install_confirmed_correction(
                None,
                None,
                origin_key=(),
                scan=0,
                source="test",
            ),
            lambda: run_skiff_scan(None, None, None, pilot_rungs=()),
        )
        for invoke in forbidden:
            with pytest.raises(RecoveryInvariantViolation):
                invoke()
        return Stop(candidate)

    compose_corrections(
        "candidate",
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
    )


def test_recovery_allows_bounded_roleless_pinned_evidence() -> None:
    from pyrung import PLC, Bool, Program, out, rung
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.skiff import run_pinned_scan

    source = Bool("RecoveryPinnedSource", external=True)
    result = Bool("RecoveryPinnedResult")
    with Program() as program:
        with rung(source):
            out(result)
    plc = PLC(program)
    pdg = build_program_graph(program)

    def attempt(candidate, _ctx):
        evidence = run_pinned_scan(
            plc,
            frozenset({source.name, result.name}),
            pdg,
            pilot_rungs=(),
            actions=((source.name, True),),
        )
        return Succeed(evidence)

    composition = compose_corrections(
        "candidate",
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
    )

    assert composition.termination is CompositionTermination.SUCCESS
    assert composition.value.after[result.name] is True
