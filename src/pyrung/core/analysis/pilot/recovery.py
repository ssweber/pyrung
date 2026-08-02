"""Bounded correction-chain composition shared by PILOT recovery readers.

The callers own how a correction is replayed, scoped, or oriented.  This
module owns the invariant common to those domains: a candidate is attempted
only while budget remains, an extension identity is admitted once, rejection
may roll back to a sibling, and success or a named terminal stops the chain.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

Candidate = TypeVar("Candidate")
Value = TypeVar("Value")


@dataclass
class CompositionBudget:
    """One explicit attempt budget, including caller-declared auxiliary work."""

    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> bool:
        """Claim one attempt, returning false without exceeding the limit."""

        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass
class AttemptContext:
    """Composition-owned identity and budget state exposed to callbacks."""

    budget: CompositionBudget
    seen: set[Hashable] = field(default_factory=set)

    def consume_auxiliary(self) -> bool:
        """Charge one caller-owned attempt against the same chain budget."""

        return self.budget.consume()


@dataclass(frozen=True)
class Extend(Generic[Candidate, Value]):
    """Lazily add one identity-bearing correction to the current candidate."""

    identity: Hashable
    build: Callable[[], Candidate | None]
    cycle: Succeed[Value] | Reject[Value] | Stop[Value] | Retry[Candidate]
    unresolved: Succeed[Value] | Reject[Value] | Stop[Value] | Retry[Candidate]


@dataclass(frozen=True)
class Succeed(Generic[Value]):
    """Return a confirmed candidate or an honest handoff to the outer loop."""

    value: Value


@dataclass(frozen=True)
class Reject(Generic[Value]):
    """Reject the current branch and permit rollback to a sibling."""

    value: Value


@dataclass(frozen=True)
class Stop(Generic[Value]):
    """Stop without claiming either success or a proved rejection."""

    value: Value


@dataclass(frozen=True)
class Retry(Generic[Candidate]):
    """Retry a caller-declared form of the same candidate without a budget charge."""

    candidate: Candidate


CompositionDecision = (
    Extend[Candidate, Value] | Succeed[Value] | Reject[Value] | Stop[Value] | Retry[Candidate]
)


class CompositionTermination(Enum):
    """Why a bounded correction-composition transaction ended."""

    SUCCESS = "success"
    REJECTED = "rejected"
    STOPPED = "stopped"
    CYCLE = "cycle"
    BUDGET = "budget"


@dataclass(frozen=True)
class CompositionResult(Generic[Candidate, Value]):
    candidate: Candidate
    value: Value
    termination: CompositionTermination
    attempts: int


Attempt = Callable[[Candidate, AttemptContext], CompositionDecision[Candidate, Value]]
Rollback = Callable[
    [Candidate, Value, AttemptContext],
    CompositionDecision[Candidate, Value] | None,
]


def compose_corrections(
    initial: Candidate,
    *,
    budget: CompositionBudget,
    attempt: Attempt[Candidate, Value],
    budget_exhausted: Callable[[Candidate], Value],
    initial_identity: Hashable | None = None,
    rollback_to_sibling: Rollback[Candidate, Value] | None = None,
) -> CompositionResult[Candidate, Value]:
    """Run one bounded, identity-safe correction-composition transaction.

    ``attempt`` performs exactly the domain work for one admitted candidate.
    ``Extend.build`` is deliberately lazy: a repeated cause/act is rejected
    before deriving another candidate or mutating caller observation state.
    ``rollback_to_sibling`` runs only after a proved rejection and may return a
    sibling extension, an outer-loop handoff, or a terminal decision.
    """

    current = initial
    context = AttemptContext(budget)
    if initial_identity is not None:
        context.seen.add(initial_identity)

    def _finish(
        value: Value,
        termination: CompositionTermination,
    ) -> CompositionResult[Candidate, Value]:
        return CompositionResult(current, value, termination, budget.used)

    charge_attempt = True
    free_retry_available = False
    while True:
        if charge_attempt:
            if not budget.consume():
                return _finish(budget_exhausted(current), CompositionTermination.BUDGET)
            free_retry_available = True
        decision = attempt(current, context)
        charge_attempt = True

        while True:
            if isinstance(decision, Succeed):
                return _finish(decision.value, CompositionTermination.SUCCESS)
            if isinstance(decision, Stop):
                return _finish(decision.value, CompositionTermination.STOPPED)
            if isinstance(decision, Retry):
                current = decision.candidate
                if free_retry_available:
                    free_retry_available = False
                    charge_attempt = False
                break
            if isinstance(decision, Extend):
                if decision.identity in context.seen:
                    decision = decision.cycle
                    if isinstance(decision, Retry):
                        continue
                    if isinstance(decision, Reject) and rollback_to_sibling is not None:
                        continue
                    return _finish(decision.value, CompositionTermination.CYCLE)
                context.seen.add(decision.identity)
                extended = decision.build()
                if extended is not None:
                    current = extended
                    break
                decision = decision.unresolved
                continue

            if rollback_to_sibling is None:
                return _finish(decision.value, CompositionTermination.REJECTED)
            sibling = rollback_to_sibling(current, decision.value, context)
            if sibling is None:
                return _finish(decision.value, CompositionTermination.REJECTED)
            decision = sibling
        # An admitted extension becomes the next budgeted candidate attempt.
