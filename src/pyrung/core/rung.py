"""Rung class for the immutable PLC engine.

Rungs contain conditions and instructions.
They evaluate within a ScanContext for batched updates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core._source import _capture_call_end_line
from pyrung.core.condition import (
    Condition,
    ConditionTerm,
    _as_condition,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.sp_tree import SPNode
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.instruction import Instruction


class Rung:
    """A rung of ladder logic.

    Contains conditions (contacts) and instructions (coils).
    Evaluation is done within a ScanContext for batched updates.

    Conditions are ANDed together - all must be true for instructions to execute.
    """

    def __init__(
        self,
        *conditions: ConditionTerm,
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
        self._terminal_instruction: Instruction | None = None
        # Branch rungs may include inherited parent conditions first.
        # This index marks where this rung's own local branch conditions begin.
        self._branch_condition_start = 0
        self._use_prior_snapshot = False
        self.source_file = source_file
        self.source_line = source_line
        self.end_line = end_line
        self.comment: str | None = None

        for cond in conditions:
            self._conditions.append(_as_condition(cond))

    def add_instruction(self, instruction: Instruction) -> None:
        """Add an instruction to execute when conditions are true."""
        if self._terminal_instruction is not None:
            terminal_name = type(self._terminal_instruction).__name__
            current_name = type(instruction).__name__
            raise RuntimeError(
                f"{terminal_name} is terminal in this flow and must be last; "
                f"cannot add instruction {current_name}."
            )
        if getattr(instruction, "end_line", None) is None:
            end_line = _capture_call_end_line(
                getattr(instruction, "source_file", None),
                getattr(instruction, "source_line", None),
            )
            if end_line is not None:
                instruction.end_line = end_line
        self._instructions.append(instruction)
        self._execution_items.append(instruction)
        if instruction.is_terminal():
            self._terminal_instruction = instruction

    def add_branch(self, branch: Rung) -> None:
        """Add a nested branch (parallel path) to this rung."""
        if self._terminal_instruction is not None:
            terminal_name = type(self._terminal_instruction).__name__
            raise RuntimeError(
                f"{terminal_name} is terminal in this flow and must be last; "
                "cannot add branch(...)."
            )
        self._branches.append(branch)
        self._execution_items.append(branch)

    def sp_tree(self) -> SPNode | None:
        """Return this rung's condition structure as an SP tree.

        Returns ``None`` for unconditional rungs (no conditions).
        """
        from pyrung.core.analysis.sp_tree import conditions_to_sp

        return conditions_to_sp(self._conditions)

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

            def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
                return all(cond.evaluate(ctx) for cond in self.conditions)

        return CombinedCondition(self._conditions)

    def evaluate(self, ctx: ScanContext) -> None:
        """Evaluate this rung within a ScanContext.

        Writes are batched in the context and committed at scan end.
        A ConditionView is frozen at rung entry so that all rung conditions,
        branch conditions, and instruction helper conditions evaluate against
        the same snapshot.

        If ``_use_prior_snapshot`` is set (via ``.continued()``), the
        ConditionView from the previous rung is reused instead of creating
        a fresh one — all conditions evaluate against the same pre-instruction
        state as that earlier rung, but only within the same execution scope.

        Args:
            ctx: ScanContext for reading/writing with batched updates.
        """
        condition_view = self._resolve_condition_view(ctx)
        conditions_true = self._evaluate_conditions(condition_view)
        self.execute(ctx, conditions_true, condition_view=condition_view)

    def _resolve_condition_view(self, ctx: ScanContext) -> ConditionView:
        """Resolve the frozen snapshot this rung should use for conditions."""
        from pyrung.core.context import ConditionView

        if self._use_prior_snapshot:
            condition_view = ctx._condition_snapshot
            if (
                condition_view is None
                or condition_view.scope_token is not ctx._condition_scope_token
            ):
                raise RuntimeError(
                    "Rung.continued() used but no prior condition snapshot exists in the "
                    "same execution scope. continued() cannot be used on the first rung in "
                    "a program or subroutine, and cannot cross into or out of a subroutine."
                )
        else:
            condition_view = ConditionView(ctx)

        ctx._condition_snapshot = condition_view
        return condition_view

    def _evaluate_conditions(self, ctx: ScanContext | ConditionView) -> bool:
        """Evaluate all conditions (AND logic).

        Returns True if all conditions are true, or if there are no conditions.
        Accepts either a live ScanContext or a frozen ConditionView.
        """
        if not self._conditions:
            return True

        for cond in self._conditions:
            if not cond.evaluate(ctx):
                return False
        return True

    def _execute_instructions(self, ctx: ScanContext) -> None:
        """Execute instructions/branches in source order."""
        self.execute(ctx, True)

    def _evaluate_local_conditions(self, ctx: ScanContext | ConditionView) -> bool:
        """Evaluate only this branch's local conditions (not inherited parent conditions)."""
        if self._branch_condition_start >= len(self._conditions):
            return True
        for cond in self._conditions[self._branch_condition_start :]:
            if not cond.evaluate(ctx):
                return False
        return True

    def _compute_branch_enable_map(
        self,
        condition_view: ConditionView,
        parent_enabled: bool,
    ) -> dict[int, bool]:
        """Compute direct branch enable states using the rung-entry snapshot."""
        branch_enable_map: dict[int, bool] = {}
        for item in self._execution_items:
            if isinstance(item, Rung):
                branch_enable_map[id(item)] = parent_enabled and item._evaluate_local_conditions(
                    condition_view
                )
        return branch_enable_map

    def execute(
        self,
        ctx: ScanContext,
        enabled: bool,
        *,
        condition_view: ConditionView | None = None,
    ) -> None:
        """Execute this rung with the provided power state.

        Args:
            ctx: Live ScanContext for instruction read-after-write.
            enabled: Whether this rung/branch is powered.
            condition_view: Frozen snapshot from rung entry for condition evaluation.
                If None, a fresh snapshot is created (top-level entry via evaluate()).
        """
        if condition_view is None:
            condition_view = self._resolve_condition_view(ctx)
        else:
            ctx._condition_snapshot = condition_view

        branch_enable_map = self._compute_branch_enable_map(condition_view, parent_enabled=enabled)

        for item in self._execution_items:
            if isinstance(item, Rung):
                branch_power = branch_enable_map.get(id(item), False)
                item.execute(ctx, branch_power, condition_view=condition_view)
            else:
                item.execute(ctx, enabled)
