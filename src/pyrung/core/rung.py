"""Rung class for the immutable PLC engine.

Rungs contain conditions and instructions.
They evaluate within a ScanContext for batched updates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pyrung.core._source import _capture_call_end_line
from pyrung.core.condition import (
    Condition,
    ConditionTerm,
    _as_condition,
)
from pyrung.core.instruction.base import _EXECUTOR_BRANCH
from pyrung.core.site_memory import build_edge_frames, scan_edge_frame

if TYPE_CHECKING:
    from pyrung.core.analysis.sp_tree import SPNode
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.instruction import Instruction

_ANALYSIS_CACHE_MISS = object()


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
        # Analysis asks these two static questions repeatedly while walking
        # alternate producer routes.  Keep the caches rung-local so their
        # lifetime follows the program and invalidate them while the rung is
        # still being assembled.
        self._writer_by_tag_cache: dict[str, Instruction | None] | None = None
        self._written_value_cache: tuple[int, dict[str, object]] | None = None
        self._sp_tree_cache: SPNode | None | object = _ANALYSIS_CACHE_MISS
        self._branches: list[Rung] = []  # Nested branches (parallel paths)
        self._execution_items: list[Instruction | Rung] = []  # Source-order execution sequence
        self._execution_plan: list[tuple[int, Instruction | Rung]] = []
        self._terminal_instruction: Instruction | None = None
        # Outputs in this rung's wire tree (its own and its branches'), and the
        # box with pins among them: a box with pins ends its rung.
        self._tree_outputs = 0
        self._pin_box: Instruction | None = None
        # A forloop body's bare instructions are each a body rung of their own.
        self._loop_capture = False
        # A forloop body rung: no .continued() there, so a box's neighbours
        # move to body rungs of their own.
        self._in_loop_body = False
        # Branch rungs may include inherited parent conditions first.
        # This index marks where this rung's own local branch conditions begin.
        self._branch_condition_start = 0
        # Set by ``.continued()``, the import form of a CLICK rung saved with
        # several wires.  The rung is solved and exported as a rung of its own.
        self._use_prior_snapshot = False
        # ``(memory key, tag)`` of the edge sites this rung's own contacts hold
        # (its row), scanned when the solve reaches the row; built on first
        # scan (pyrung.core.site_memory.build_edge_frames).
        self._edge_frame: tuple[tuple[str, str], ...] | None = None
        self.source_file = source_file
        self.source_line = source_line
        self.end_line = end_line
        self.comment: str | None = None

        for cond in conditions:
            self._conditions.append(_as_condition(cond))

    def add_instruction(self, instruction: Instruction) -> None:
        """Add an instruction to execute when conditions are true.

        A box with pins (:meth:`Instruction.has_pins`) ends its rung: it is
        refused beside any other output of the rung's wire tree (the trunk and
        every branch), as CLICK draws its pins from the box's own wire.
        """
        terminal = self._terminal_instruction
        if terminal is not None:
            if terminal.has_pins() and not self._loop_capture:
                raise RuntimeError(_box_shares_rung(terminal, instruction, self._in_loop_body))
            terminal_name = type(terminal).__name__
            current_name = type(instruction).__name__
            raise RuntimeError(
                f"{terminal_name} is terminal in this flow and must be last; "
                f"cannot add instruction {current_name}."
            )
        if not self._loop_capture:
            if self._pin_box is not None:
                raise RuntimeError(_box_shares_rung(self._pin_box, instruction, self._in_loop_body))
            if instruction.has_pins() and self._tree_outputs:
                raise RuntimeError(
                    _box_shares_rung(instruction, self._first_output(), self._in_loop_body)
                )
        if getattr(instruction, "end_line", None) is None:
            end_line = _capture_call_end_line(
                getattr(instruction, "source_file", None),
                getattr(instruction, "source_line", None),
            )
            if end_line is not None:
                instruction.end_line = end_line
        self._instructions.append(instruction)
        self._invalidate_analysis_caches()
        self._execution_items.append(instruction)
        self._execution_plan.append((instruction._executor_kind, instruction))
        # A forloop body's bare box is a body rung of its own, so the next bare
        # instruction is another body rung: the box ends only its own (a NOP
        # still ends the flow).
        if instruction.is_terminal() and not (self._loop_capture and instruction.has_pins()):
            self._terminal_instruction = instruction
        self._tree_outputs += 1
        if instruction.has_pins():
            self._pin_box = instruction

    def add_branch(self, branch: Rung) -> None:
        """Add a nested branch (parallel path) to this rung.

        Refused where it would put a box with pins beside another output (see
        :meth:`add_instruction`).
        """
        terminal = self._terminal_instruction
        if terminal is not None:
            other = branch._first_output()
            if terminal.has_pins() and other is not None:
                raise RuntimeError(_box_shares_rung(terminal, other, self._in_loop_body))
            terminal_name = type(terminal).__name__
            raise RuntimeError(
                f"{terminal_name} is terminal in this flow and must be last; "
                "cannot add branch(...)."
            )
        if self._tree_outputs and branch._tree_outputs:
            if self._pin_box is not None:
                raise RuntimeError(
                    _box_shares_rung(self._pin_box, branch._first_output(), self._in_loop_body)
                )
            if branch._pin_box is not None:
                raise RuntimeError(
                    _box_shares_rung(branch._pin_box, self._first_output(), self._in_loop_body)
                )
        self._tree_outputs += branch._tree_outputs
        if branch._pin_box is not None:
            self._pin_box = branch._pin_box
        self._branches.append(branch)
        self._execution_items.append(branch)
        self._execution_plan.append((_EXECUTOR_BRANCH, branch))

    def _first_output(self) -> Instruction | None:
        """The first instruction of this rung's wire tree, in solve order."""
        for item in self._execution_items:
            if isinstance(item, Rung):
                found = item._first_output()
                if found is not None:
                    return found
            else:
                return item
        return None

    def sp_tree(self) -> SPNode | None:
        """Return this rung's condition structure as an SP tree.

        Returns ``None`` for unconditional rungs (no conditions).
        """
        if self._sp_tree_cache is not _ANALYSIS_CACHE_MISS:
            return cast("SPNode | None", self._sp_tree_cache)

        from pyrung.core.analysis.sp_tree import conditions_to_sp

        self._sp_tree_cache = conditions_to_sp(self._conditions)
        return self._sp_tree_cache

    def _invalidate_analysis_caches(self) -> None:
        """Clear facts derived from this rung while it is being assembled."""

        self._writer_by_tag_cache = None
        self._written_value_cache = None
        self._sp_tree_cache = _ANALYSIS_CACHE_MISS
        self._edge_frame = None

    def _get_combined_condition(self) -> Condition | None:
        """Get a single condition representing all rung conditions ANDed together.

        Returns None if there are no conditions (unconditional rung).
        Used by counter instructions to capture the rung's enable condition.
        """
        if not self._conditions:
            return None
        if len(self._conditions) == 1:
            return self._conditions[0]
        from pyrung.core.condition import AllCondition

        return AllCondition(*self._conditions)

    def evaluate(self, ctx: ScanContext) -> None:
        """Evaluate this rung within a ScanContext.

        Writes are batched in the context and committed at scan end.  Each
        instruction solves only the path from the rail to it: the trunk's
        contacts are read once and that power is held for everything under
        it; each branch's own contacts are read when execution reaches the
        branch, so they see the writes of the instructions above it.  A ``.continued()`` rung is a rung of
        its own, its contacts read live like any other's.

        Args:
            ctx: ScanContext for reading/writing with batched updates.
        """
        condition_view = self._new_row_view(ctx, live=True)
        conditions_true = self._evaluate_conditions(condition_view)
        self.execute(ctx, conditions_true)

    def _new_row_view(self, ctx: ScanContext, *, live: bool = False) -> ConditionView:
        """A condition view for this rung's own row (its trunk, or a branch's
        local contacts), taken as the solve reaches it, with the row's edge
        sites scanned.

        *live* views read the pending writes directly and are only good for
        evaluating the row at once; a frozen one (the default) may be kept and
        re-read later (observers, the debugger).
        """
        frame = self._edge_frame
        if frame is None:
            frame = build_edge_frames(self)
        condition_view = ctx._new_condition_view(live=live)
        if frame:
            scan_edge_frame(ctx, condition_view, frame)
        return condition_view

    def _evaluate_conditions(self, ctx: ScanContext | ConditionView) -> bool:
        """Evaluate all conditions (AND logic).

        Returns True if all conditions are true, or if there are no conditions.
        Accepts either a live ScanContext or a ConditionView.
        """
        if not self._conditions:
            return True

        for cond in self._conditions:
            if not cond.evaluate(ctx):
                return False
        return True

    def _evaluate_local_conditions(self, ctx: ScanContext | ConditionView) -> bool:
        """Evaluate only this branch's local conditions (not inherited parent conditions)."""
        conditions = self._conditions
        start = self._branch_condition_start
        if start >= len(conditions):
            return True
        for i in range(start, len(conditions)):
            if not conditions[i].evaluate(ctx):
                return False
        return True

    def _solve_row(self, ctx: ScanContext, parent_enabled: bool) -> bool:
        """Solve this branch (or For-body rung) as the solve reaches it: its
        own contacts, read live, under the power held from the row above."""
        condition_view = self._new_row_view(ctx, live=True)
        return parent_enabled and self._evaluate_local_conditions(condition_view)

    def _execute_in_loop(self, ctx: ScanContext) -> None:
        """Execute this rung as a forloop body rung, on one iteration.

        A body rung is a rung of its own: on each iteration it reads its local
        contacts live (the loop rung's contacts held at loop entry).  A loop
        whose rung is false does not run its body at all.
        """
        self.execute(ctx, self._solve_row(ctx, True))

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        """Execute this rung's rows in solve order with the provided power state.

        Each branch is solved when the loop reaches it (``_solve_row``): its
        own contacts read live, under *enabled*, the power held for the rows
        under this one.

        Args:
            ctx: Live ScanContext for instruction read-after-write.
            enabled: Whether this rung/branch is powered.
        """
        for item in self._execution_items:
            if isinstance(item, Rung):
                item.execute(ctx, item._solve_row(ctx, enabled))
            else:
                item.execute(ctx, enabled)


# DSL names that do not follow from the instruction class name.
_DSL_NAMES = {
    "CalcInstruction": "math",
    "FunctionCallInstruction": "run_function",
    "EnabledFunctionCallInstruction": "run_enabled_function",
    "BlockCopyInstruction": "blockcopy",
    "ForLoopInstruction": "forloop",
    "ReturnInstruction": "return_early",
    "BreakInstruction": "break_loop",
    "ContinueInstruction": "continue_loop",
    "ModbusSendInstruction": "send",
    "ModbusReceiveInstruction": "receive",
}


def _dsl_label(instruction: Instruction | None) -> str:
    """``count_up() (line 12)``: the instruction as its author wrote it."""
    if instruction is None:  # pragma: no cover - defensive
        return "the other output"
    class_name = type(instruction).__name__
    name = _DSL_NAMES.get(class_name)
    if name is None:
        stem = class_name.removesuffix("Instruction")
        name = "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in stem).lstrip("_")
    line = getattr(instruction, "source_line", None)
    return f"{name}() (line {line})" if line is not None else f"{name}()"


def _box_shares_rung(box: Instruction, other: Instruction | None, in_loop_body: bool) -> str:
    """Why *box*, a box with pins, cannot share its rung with *other*."""
    where = (
        "what comes before the box into a body rung above, what follows it into a body "
        "rung of its own."
        if in_loop_body
        else "what comes before the box into a rung above, what follows it into a rung of its own."
    )
    return (
        f"{_dsl_label(box)} has pins, so it ends its rung: CLICK draws its pins from the "
        f"box's own wire, and no other output or branch shares that rung. Move "
        f"{_dsl_label(other)} off this rung: {where}"
    )
