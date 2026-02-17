"""Rung class for the immutable PLC engine.

Rungs contain conditions and instructions.
They evaluate within a ScanContext for batched updates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core.condition import (
    Condition,
    _as_condition,
)
from pyrung.core.tag import Tag

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.instruction import Instruction


class Rung:
    """A rung of ladder logic.

    Contains conditions (contacts) and instructions (coils).
    Evaluation is done within a ScanContext for batched updates.

    Conditions are ANDed together - all must be true for instructions to execute.
    """

    def __init__(
        self,
        *conditions: Condition | Tag,
        source_file: str | None = None,
        source_line: int | None = None,
        end_line: int | None = None,
    ):
        """Create a rung with conditions.

        Args:
            conditions: Zero or more conditions. If a BOOL Tag is passed,
                        it's automatically wrapped in BitCondition.
        """
        self._conditions: list[Condition] = []
        self._instructions: list[Instruction] = []
        self._branches: list[Rung] = []  # Nested branches (parallel paths)
        self._execution_items: list[Instruction | Rung] = []  # Source-order execution sequence
        # Branch rungs may include inherited parent conditions first.
        # This index marks where this rung's own local branch conditions begin.
        self._branch_condition_start = 0
        self._coils: set[Tag] = set()  # Tags that should reset when rung false
        self.source_file = source_file
        self.source_line = source_line
        self.end_line = end_line

        for cond in conditions:
            self._conditions.append(_as_condition(cond))

    def add_instruction(self, instruction: Instruction) -> None:
        """Add an instruction to execute when conditions are true."""
        self._instructions.append(instruction)
        self._execution_items.append(instruction)

    def register_coil(self, tag: Tag) -> None:
        """Register a tag as a coil output (resets when rung false)."""
        self._coils.add(tag)

    def add_branch(self, branch: Rung) -> None:
        """Add a nested branch (parallel path) to this rung."""
        self._branches.append(branch)
        self._execution_items.append(branch)

    def _get_combined_condition(self) -> Condition | None:
        """Get a single condition representing all rung conditions ANDed together.

        Returns None if there are no conditions (unconditional rung).
        Used by counter instructions to capture the rung's enable condition.
        """
        if not self._conditions:
            return None
        if len(self._conditions) == 1:
            return self._conditions[0]
        # For multiple conditions, we need to create a combined condition
        # Since there's no AndCondition class, we'll create a lambda-based condition
        from pyrung.core.condition import Condition as ConditionBase

        class CombinedCondition(ConditionBase):
            def __init__(self, conditions: list[Condition]):
                self.conditions = conditions

            def evaluate(self, ctx: ScanContext) -> bool:
                return all(cond.evaluate(ctx) for cond in self.conditions)

        return CombinedCondition(self._conditions)

    def evaluate(self, ctx: ScanContext) -> None:
        """Evaluate this rung within a ScanContext.

        Writes are batched in the context and committed at scan end.

        Args:
            ctx: ScanContext for reading/writing with batched updates.
        """
        conditions_true = self._evaluate_conditions(ctx)
        self._execute_with_enable(ctx, conditions_true)

    def _evaluate_conditions(self, ctx: ScanContext) -> bool:
        """Evaluate all conditions (AND logic).

        Returns True if all conditions are true, or if there are no conditions.
        """
        if not self._conditions:
            return True

        for cond in self._conditions:
            if not cond.evaluate(ctx):
                return False
        return True

    def _execute_instructions(self, ctx: ScanContext) -> None:
        """Execute instructions/branches in source order."""
        self._execute_with_enable(ctx, True)

    def _evaluate_local_conditions(self, ctx: ScanContext) -> bool:
        """Evaluate only this branch's local conditions (not inherited parent conditions)."""
        if self._branch_condition_start >= len(self._conditions):
            return True
        for cond in self._conditions[self._branch_condition_start :]:
            if not cond.evaluate(ctx):
                return False
        return True

    def _compute_branch_enable_map(self, ctx: ScanContext, parent_enabled: bool) -> dict[int, bool]:
        """Compute direct branch enable states before executing any items."""
        branch_enable_map: dict[int, bool] = {}
        for item in self._execution_items:
            if isinstance(item, Rung):
                branch_enable_map[id(item)] = parent_enabled and item._evaluate_local_conditions(ctx)
        return branch_enable_map

    def _execute_with_enable(self, ctx: ScanContext, enabled: bool) -> None:
        """Execute or false-handle this rung using a precomputed enable state."""
        if not enabled:
            self._handle_rung_false(ctx)
            return

        branch_enable_map = self._compute_branch_enable_map(ctx, parent_enabled=True)

        for item in self._execution_items:
            if isinstance(item, Rung):
                item._execute_with_enable(ctx, branch_enable_map.get(id(item), False))
            else:
                item.execute(ctx)

    def _execute_always_instructions(self, ctx: ScanContext) -> None:
        """Execute instructions that always run (like counters) even when rung is false."""
        for instruction in self._instructions:
            if instruction.always_execute():
                instruction.execute(ctx)

    def _handle_rung_false(self, ctx: ScanContext) -> None:
        """Handle outputs when rung goes false.

        - Execute always-execute instructions (like counters)
        - Reset registered coil outputs to False
        - Reset oneshot triggers on instructions
        - Propagate false to nested branches
        """
        # Execute instructions that always run (counters check their own conditions)
        self._execute_always_instructions(ctx)

        # Reset coil outputs
        for tag in self._coils:
            ctx.set_tag(tag.name, tag.default)

        # Reset oneshot triggers
        for instruction in self._instructions:
            reset_oneshot = getattr(instruction, "reset_oneshot", None)
            if callable(reset_oneshot):
                reset_oneshot()

        # Propagate false to nested branches
        for item in self._execution_items:
            if isinstance(item, Rung):
                item._handle_rung_false(ctx)
