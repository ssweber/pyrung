"""Rung class for the immutable PLC engine.

Rungs contain conditions and instructions.
They evaluate within a ScanContext for batched updates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core.condition import (
    BitCondition,
    Condition,
)
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.instruction import Instruction


class Rung:
    """A rung of ladder logic.

    Contains conditions (contacts) and instructions (coils).
    Evaluation is done within a ScanContext for batched updates.

    Conditions are ANDed together - all must be true for instructions to execute.
    """

    def __init__(self, *conditions: Condition | Tag):
        """Create a rung with conditions.

        Args:
            conditions: Zero or more conditions. If a Bit Tag is passed,
                        it's automatically wrapped in BitCondition.
        """
        self._conditions: list[Condition] = []
        self._instructions: list[Instruction] = []
        self._branches: list[Rung] = []  # Nested branches (parallel paths)
        self._coils: set[Tag] = set()  # Tags that should reset when rung false

        for cond in conditions:
            if isinstance(cond, Tag):
                # Bit tags become BitCondition (normally open contact)
                if cond.type == TagType.BOOL:
                    self._conditions.append(BitCondition(cond))
                else:
                    raise TypeError(
                        f"Non-BOOL tag '{cond.name}' cannot be used directly as condition. "
                        "Use comparison operators: tag == value, tag > 0, etc."
                    )
            elif isinstance(cond, Condition):
                self._conditions.append(cond)
            else:
                raise TypeError(f"Expected Condition or Tag, got {type(cond)}")

    def add_instruction(self, instruction: Instruction) -> None:
        """Add an instruction to execute when conditions are true."""
        self._instructions.append(instruction)

    def register_coil(self, tag: Tag) -> None:
        """Register a tag as a coil output (resets when rung false)."""
        self._coils.add(tag)

    def add_branch(self, branch: Rung) -> None:
        """Add a nested branch (parallel path) to this rung."""
        self._branches.append(branch)

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

        if conditions_true:
            self._execute_instructions(ctx)
        else:
            self._handle_rung_false(ctx)

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
        """Execute all instructions and branches in order."""
        for instruction in self._instructions:
            instruction.execute(ctx)

        # Evaluate nested branches (parallel paths)
        for branch in self._branches:
            branch.evaluate(ctx)

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
            if hasattr(instruction, "reset_oneshot"):
                instruction.reset_oneshot()

        # Propagate false to nested branches
        for branch in self._branches:
            branch._handle_rung_false(ctx)
