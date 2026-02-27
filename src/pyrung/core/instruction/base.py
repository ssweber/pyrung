"""Automatically generated module split."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


@dataclass(frozen=True)
class DebugInstructionSubStep:
    """Per-instruction debug step metadata for chained builder methods."""

    instruction_kind: str
    source_file: str | None
    source_line: int | None
    eval_mode: Literal["enabled", "condition"]
    condition: Any | None = None
    expression: str | None = None


class Instruction(ABC):
    """Base class for all instructions.

    Instructions execute within a ScanContext, writing to batched evolvers.
    All state modifications are collected and committed at scan end.
    """

    source_file: str | None = None
    source_line: int | None = None
    end_line: int | None = None
    debug_substeps: tuple[DebugInstructionSubStep, ...] | None = None
    ALWAYS_EXECUTES: bool = False
    INERT_WHEN_DISABLED: bool = True

    @abstractmethod
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """Execute this instruction within the given context (internal)."""
        pass

    def always_execute(self) -> bool:
        """Whether this instruction should execute even when rung is false.

        Override to return True for terminal instructions like counters
        that need to check their conditions independently.
        """
        return self.ALWAYS_EXECUTES

    def is_inert_when_disabled(self) -> bool:
        """Whether this instruction is a no-op when `enabled` is False."""
        return self.INERT_WHEN_DISABLED

    def is_terminal(self) -> bool:
        """Whether this instruction must be the last execution item in its flow."""
        return False


class SubroutineReturnSignal(Exception):
    """Internal control-flow signal used by return_early() inside subroutines."""


class OneShotMixin:
    """Mixin for instructions that support one-shot mode.

    One-shot instructions execute only once per rung activation.
    They must be reset when the rung goes false.
    """

    def __init__(self, oneshot: bool = False):
        self._oneshot = oneshot
        self._has_executed = False

    @property
    def oneshot(self) -> bool:
        return self._oneshot

    def should_execute(self, enabled: bool) -> bool:
        """Check if instruction should execute (respects oneshot)."""
        if not enabled:
            self._has_executed = False
            return False
        if not self._oneshot:
            return True
        if self._has_executed:
            return False
        self._has_executed = True
        return True

    def reset_oneshot(self) -> None:
        """Reset oneshot state (call when rung goes false)."""
        self._has_executed = False
