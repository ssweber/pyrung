"""Contracts for the bounded correction-composition recovery primitive."""

from __future__ import annotations

from pyrung.core.analysis.pilot.recovery import (
    CompositionBudget,
    CompositionTermination,
    Extend,
    Reject,
    Retry,
    Succeed,
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
