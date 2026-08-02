"""The bounded-recovery invariant shared by PILOT recovery readers.

Bounded recovery may read evidence, including bounded pinned scans, execute
disposable forks or one-step transition kernels, and accumulate
transaction-local knowledge.  It returns at most one ordinary candidate or
confirmation.  It cannot install a correction in or commit the outer PILOT
world, recursively invoke the drive loop, invoke skiff navigation or commit
probe observations, or leave an unbounded retry.

Callers own how a correction is replayed, scoped, or oriented.  This module
owns the transaction boundary, finite budget, extension identity admission,
rejection rollback, and single terminal result shared by those domains.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

Candidate = TypeVar("Candidate")
Value = TypeVar("Value")


class RecoveryInvariantViolation(RuntimeError):
    """A bounded recovery callback crossed an outer-orchestration boundary."""


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
    protected_states: dict[int, object] = field(default_factory=dict)
    disposable_states: dict[int, object] = field(default_factory=dict)
    finished: bool = False

    def consume_auxiliary(self) -> bool:
        """Charge one caller-owned attempt against the same chain budget."""

        return self.budget.consume()

    def register_disposable_state(self, state: object) -> None:
        """Authorize one transaction-local state for a one-step transition."""

        if _ACTIVE_RECOVERY.get() is not self:
            raise RecoveryInvariantViolation(
                "disposable recovery state may be registered only by its active transaction"
            )
        if id(state) in self.protected_states and self.protected_states[id(state)] is state:
            raise RecoveryInvariantViolation(
                "bounded recovery cannot register the outer PILOT world as disposable"
            )
        self.disposable_states[id(state)] = state

    def finish(self) -> None:
        """Record the transaction's single terminal result."""

        if self.finished:
            raise RecoveryInvariantViolation("bounded recovery produced more than one result")
        self.finished = True


_ACTIVE_RECOVERY: ContextVar[AttemptContext | None] = ContextVar(
    "pyrung_active_recovery",
    default=None,
)


def assert_recovery_inactive(operation: str) -> None:
    """Reject an outer-owner operation from inside bounded recovery."""

    if _ACTIVE_RECOVERY.get() is not None:
        raise RecoveryInvariantViolation(f"bounded recovery cannot {operation}")


def assert_recovery_disposable_state(state: object, operation: str) -> None:
    """Require transaction-local state for an otherwise permitted local commit."""

    recovery = _ACTIVE_RECOVERY.get()
    if recovery is not None and (
        id(state) not in recovery.disposable_states
        or recovery.disposable_states[id(state)] is not state
    ):
        raise RecoveryInvariantViolation(
            f"bounded recovery cannot {operation} on the outer PILOT world"
        )


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
    protected_states: tuple[object, ...] = (),
) -> CompositionResult[Candidate, Value]:
    """Run one bounded, identity-safe correction-composition transaction.

    ``attempt`` performs exactly the domain work for one admitted candidate.
    ``Extend.build`` is deliberately lazy: a repeated cause/act is rejected
    before deriving another candidate or mutating caller observation state.
    ``rollback_to_sibling`` runs only after a proved rejection and may return a
    sibling extension, an outer-loop handoff, or a terminal decision.
    ``protected_states`` names outer owners that callbacks cannot relabel as
    disposable transition state.
    """

    if _ACTIVE_RECOVERY.get() is not None:
        raise RecoveryInvariantViolation("bounded recovery cannot recursively compose")
    context = AttemptContext(
        budget,
        protected_states={id(state): state for state in protected_states},
    )
    if initial_identity is not None:
        context.seen.add(initial_identity)
    token = _ACTIVE_RECOVERY.set(context)
    try:
        return _compose_transaction(
            initial,
            context=context,
            attempt=attempt,
            budget_exhausted=budget_exhausted,
            rollback_to_sibling=rollback_to_sibling,
        )
    finally:
        _ACTIVE_RECOVERY.reset(token)


def _compose_transaction(
    initial: Candidate,
    *,
    context: AttemptContext,
    attempt: Attempt[Candidate, Value],
    budget_exhausted: Callable[[Candidate], Value],
    rollback_to_sibling: Rollback[Candidate, Value] | None,
) -> CompositionResult[Candidate, Value]:
    """Execute inside the active bounded-recovery transaction."""

    current = initial
    budget = context.budget

    def _finish(
        value: Value,
        termination: CompositionTermination,
    ) -> CompositionResult[Candidate, Value]:
        context.finish()
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
