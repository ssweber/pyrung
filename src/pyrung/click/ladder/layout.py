"""Ladder row-matrix layout and branching helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.condition import AllCondition, AnyCondition, Condition
from pyrung.core.rung import Rung

from .types import _ConditionRow, _OutputSlot

# ---- Matrix shape/constants ----
_CONDITION_COLS = 31

_HEADER: tuple[str, ...] = (
    "marker",
    *tuple([chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]),
    "AF",
)


# ---- Row-layout mixin ----
class _LayoutMixin:
    """Build 31-column condition matrices and branch wiring rows."""

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...
        def _instruction_token(self, instruction: Any, *, path: str) -> str: ...
        def _pin_rows(self, instruction: Any, *, path: str) -> list[tuple[str, ...]]: ...
        def _condition_leaf_token(self, condition: Condition, *, path: str) -> str: ...

    def _render_rung_with_branches(
        self,
        rung: Rung,
        *,
        condition_rows: list[_ConditionRow],
        path: str,
        first_marker: str = "R",
    ) -> list[tuple[str, ...]]:
        parent_cursor = condition_rows[0].cursor
        if parent_cursor >= _CONDITION_COLS:
            self._raise_issue(
                path=f"{path}.branch",
                message="Condition columns exceed AE before branch split.",
                source=rung,
            )

        slots, pin_rows = self._collect_execution_slots(rung, path=path)
        if not slots:
            return []

        rows = self._render_slots_on_condition_rows(
            condition_rows=condition_rows,
            slots=slots,
            split_col=parent_cursor,
            first_marker=first_marker,
            path=f"{path}.branch",
        )
        rows.extend(pin_rows)
        return rows

    def _collect_execution_slots(
        self,
        rung: Rung,
        *,
        path: str,
    ) -> tuple[list[_OutputSlot], list[tuple[str, ...]]]:
        instruction_index_by_id = {id(item): idx for idx, item in enumerate(rung._instructions)}
        branch_index_by_id = {id(item): idx for idx, item in enumerate(rung._branches)}

        slots: list[_OutputSlot] = []
        pin_rows: list[tuple[str, ...]] = []

        for item in rung._execution_items:
            if isinstance(item, Rung):
                branch_idx = branch_index_by_id.get(id(item))
                if branch_idx is None:
                    self._raise_issue(
                        path=f"{path}.branch",
                        message="Internal error: branch item missing from branch index map.",
                        source=item,
                    )

                branch_path = f"{path}.branch[{branch_idx}]"
                if item._branches:
                    self._raise_issue(
                        path=f"{branch_path}.branch",
                        message="Nested branch(...) export is not supported in Click ladder v1.",
                        source=item,
                    )
                if any(
                    type(instruction).__name__ == "ForLoopInstruction"
                    for instruction in item._instructions
                ):
                    self._raise_issue(
                        path=f"{branch_path}.instruction",
                        message="forloop() is not supported inside branch(...) in Click ladder v1 export.",
                        source=item,
                    )
                if not item._instructions:
                    continue

                local_conditions = tuple(item._conditions[item._branch_condition_start :])
                for instruction_index, instruction in enumerate(item._instructions):
                    instruction_path = (
                        f"{branch_path}.instruction[{instruction_index}]"
                        f"({type(instruction).__name__})"
                    )
                    slots.append(
                        _OutputSlot(
                            output_token=self._instruction_token(
                                instruction, path=instruction_path
                            ),
                            local_conditions=local_conditions,
                        )
                    )

                first_path = f"{branch_path}.instruction[0]({type(item._instructions[0]).__name__})"
                pin_rows.extend(self._pin_rows(item._instructions[0], path=first_path))
                continue

            instruction_idx = instruction_index_by_id.get(id(item))
            if instruction_idx is None:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message="Internal error: instruction item missing from instruction index map.",
                    source=item,
                )
            instruction_path = f"{path}.instruction[{instruction_idx}]({type(item).__name__})"
            slots.append(
                _OutputSlot(
                    output_token=self._instruction_token(item, path=instruction_path),
                    local_conditions=(),
                )
            )
            pin_rows.extend(self._pin_rows(item, path=instruction_path))

        return slots, pin_rows

    def _render_slots_on_condition_rows(
        self,
        *,
        condition_rows: list[_ConditionRow],
        slots: list[_OutputSlot],
        split_col: int,
        first_marker: str,
        path: str,
    ) -> list[tuple[str, ...]]:
        if not slots:
            return []

        if split_col < 0 or split_col >= _CONDITION_COLS:
            self._raise_issue(
                path=path,
                message="Condition columns exceed AE before output branch split.",
                source=None,
            )

        row_count = max(len(condition_rows), len(slots))
        rows: list[list[str]] = []
        for row_index in range(row_count):
            cells = (
                condition_rows[row_index].cells.copy()
                if row_index < len(condition_rows)
                else [""] * _CONDITION_COLS
            )
            rows.append([first_marker if row_index == 0 else "", *cells, ""])

        if len(slots) > 1:
            for slot_index in range(len(slots)):
                # Split node markers on the parent branch column (T / -).
                marker = _vertical_marker(index=slot_index, total=len(slots))
                cell_index = split_col + 1
                existing = rows[slot_index][cell_index]
                if existing in {"", "-", "T", "|"}:
                    if marker == "T" or existing == "T":
                        rows[slot_index][cell_index] = "T"
                    elif existing == "|":
                        rows[slot_index][cell_index] = "|"
                    else:
                        rows[slot_index][cell_index] = marker
                else:
                    self._raise_issue(
                        path=path,
                        message=(
                            f"Conflicting branch split content at column {split_col}: "
                            f"{existing!r} vs {marker!r}."
                        ),
                        source=None,
                    )

        for slot_index, slot in enumerate(slots):
            row = rows[slot_index]
            row[-1] = slot.output_token

            local_tokens = self._local_condition_tokens(
                slot.local_conditions,
                path=f"{path}.slot[{slot_index}]",
            )
            for token_index, token in enumerate(local_tokens):
                col = split_col + token_index
                if col >= _CONDITION_COLS:
                    self._raise_issue(
                        path=f"{path}.slot[{slot_index}]",
                        message="Condition columns exceed AE after branch offset.",
                        source=None,
                    )
                value = token
                if (
                    token_index == 0
                    and slot_index < len(slots) - 1
                    and col > 0
                    and value not in {"-", "T", "|"}
                    and not value.startswith("T:")
                ):
                    # Lift first local contact for upper branches into a T: token.
                    value = f"T:{value}"
                row[col + 1] = value

            # Every output path must stay wired to AF.
            for col in range(split_col + 1, _CONDITION_COLS):
                if row[col + 1] == "":
                    row[col + 1] = "-"

        return [tuple(row) for row in rows]

    def _local_condition_tokens(
        self,
        conditions: tuple[Condition, ...],
        *,
        path: str,
    ) -> list[str]:
        if not conditions:
            return []

        local_rows = self._expand_conditions(list(conditions), path=f"{path}.condition")
        if len(local_rows) != 1:
            self._raise_issue(
                path=f"{path}.condition",
                message="branch(...) local any_of() export is not supported in Click ladder v1.",
                source=None,
            )

        local = local_rows[0]
        tokens: list[str] = []
        for col in range(local.cursor):
            value = local.cells[col]
            if value != "":
                tokens.append(value)
        return tokens

    def _continuation_output_row(
        self,
        *,
        parent_cursor: int,
        output_token: str,
        marker: str,
    ) -> tuple[str, ...]:
        cells = [""] * _CONDITION_COLS
        for col in range(parent_cursor, _CONDITION_COLS):
            cells[col] = "-"
        return tuple([marker, *cells, output_token])

    @staticmethod
    def _build_or_continuation_rows(
        condition_rows: list[_ConditionRow],
    ) -> list[tuple[str, ...]]:
        """Build blank continuation rows for OR-expanded condition rows (rows 1..N)."""
        rows: list[tuple[str, ...]] = []
        for condition_row in condition_rows:
            cells = condition_row.cells.copy()
            rows.append(tuple(["", *cells, ""]))
        return rows

    def _render_instruction_rows(
        self,
        *,
        instructions: list[Any],
        condition_rows: list[_ConditionRow],
        path: str,
        first_marker: str,
        source: Any,
    ) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        if not instructions:
            return [], []

        instruction_tokens: list[str] = []
        for instruction_index, instruction in enumerate(instructions):
            instruction_path = (
                f"{path}.instruction[{instruction_index}]({type(instruction).__name__})"
            )
            instruction_tokens.append(self._instruction_token(instruction, path=instruction_path))

        output_rows: list[tuple[str, ...]]
        if len(instruction_tokens) == 1:
            output_rows = self._single_output_rows(
                condition_rows,
                output_token=instruction_tokens[0],
                first_marker=first_marker,
            )
        else:
            output_rows = self._render_slots_on_condition_rows(
                condition_rows=condition_rows,
                slots=[_OutputSlot(output_token=token) for token in instruction_tokens],
                split_col=condition_rows[0].cursor,
                first_marker=first_marker,
                path=f"{path}.instruction",
            )

        first_instruction_path = f"{path}.instruction[0]({type(instructions[0]).__name__})"
        pin_rows = self._pin_rows(instructions[0], path=first_instruction_path)
        return output_rows, pin_rows

    def _render_branch_rows(
        self,
        *,
        branches: list[Rung],
        parent_cursor: int,
        path: str,
    ) -> tuple[list[tuple[str, ...]], list[int]]:
        branch_rows: list[tuple[str, ...]] = []
        branch_entry_rows: list[int] = []
        condition_offset = parent_cursor + 1

        for branch_index, branch in enumerate(branches):
            branch_path = f"{path}.branch[{branch_index}]"
            if branch._branches:
                self._raise_issue(
                    path=f"{branch_path}.branch",
                    message="Nested branch(...) export is not supported in Click ladder v1.",
                    source=branch,
                )
            if any(
                type(instruction).__name__ == "ForLoopInstruction"
                for instruction in branch._instructions
            ):
                self._raise_issue(
                    path=f"{branch_path}.instruction",
                    message="forloop() is not supported inside branch(...) in Click ladder v1 export.",
                    source=branch,
                )

            if not branch._instructions:
                continue

            local_conditions = branch._conditions[branch._branch_condition_start :]
            local_condition_rows = self._expand_conditions(
                local_conditions,
                path=f"{branch_path}.condition",
            )
            shifted_condition_rows = self._offset_condition_rows(
                local_condition_rows,
                offset=condition_offset,
                path=f"{branch_path}.condition",
            )
            output_rows, pin_rows = self._render_instruction_rows(
                instructions=branch._instructions,
                condition_rows=shifted_condition_rows,
                path=branch_path,
                first_marker="",
                source=branch,
            )

            output_rows = self._ensure_split_wires(
                output_rows,
                split_col=parent_cursor,
                path=branch_path,
            )
            if output_rows:
                branch_entry_rows.append(len(branch_rows))

            branch_rows.extend(output_rows)
            branch_rows.extend(pin_rows)

        return branch_rows, branch_entry_rows

    def _render_branch_item_rows(
        self,
        *,
        branch: Rung,
        parent_cursor: int,
        path: str,
    ) -> tuple[list[tuple[str, ...]], list[int]]:
        if branch._branches:
            self._raise_issue(
                path=f"{path}.branch",
                message="Nested branch(...) export is not supported in Click ladder v1.",
                source=branch,
            )
        if any(
            type(instruction).__name__ == "ForLoopInstruction"
            for instruction in branch._instructions
        ):
            self._raise_issue(
                path=f"{path}.instruction",
                message="forloop() is not supported inside branch(...) in Click ladder v1 export.",
                source=branch,
            )

        if not branch._instructions:
            return [], []

        local_conditions = branch._conditions[branch._branch_condition_start :]
        local_condition_rows = self._expand_conditions(
            local_conditions,
            path=f"{path}.condition",
        )
        shifted_condition_rows = self._offset_condition_rows(
            local_condition_rows,
            offset=parent_cursor + 1,
            path=f"{path}.condition",
        )
        output_rows, pin_rows = self._render_instruction_rows(
            instructions=branch._instructions,
            condition_rows=shifted_condition_rows,
            path=path,
            first_marker="",
            source=branch,
        )
        output_rows = self._ensure_split_wires(
            output_rows,
            split_col=parent_cursor,
            path=path,
        )

        rows = output_rows.copy()
        rows.extend(pin_rows)
        split_entries = [index for index, row in enumerate(output_rows) if row[-1] != ""]
        return rows, split_entries

    def _offset_condition_rows(
        self,
        condition_rows: list[_ConditionRow],
        *,
        offset: int,
        path: str,
    ) -> list[_ConditionRow]:
        shifted_rows: list[_ConditionRow] = []
        for row in condition_rows:
            shifted_cells = [""] * _CONDITION_COLS
            for col, value in enumerate(row.cells):
                if value == "":
                    continue
                self._write_cell(
                    shifted_cells,
                    col + offset,
                    value,
                    path=path,
                    source=None,
                )

            shifted_cursor = row.cursor + offset
            if shifted_cursor > _CONDITION_COLS:
                self._raise_issue(
                    path=path,
                    message="Condition columns exceed AE after branch offset.",
                    source=None,
                )
            shifted_rows.append(
                _ConditionRow(
                    cells=shifted_cells,
                    cursor=shifted_cursor,
                    accepts_terms=row.accepts_terms,
                )
            )
        return shifted_rows

    def _ensure_split_wires(
        self,
        rows: list[tuple[str, ...]],
        *,
        split_col: int,
        path: str,
    ) -> list[tuple[str, ...]]:
        wired_rows: list[tuple[str, ...]] = []
        for row in rows:
            if row[split_col + 1] == "":
                wired_rows.append(
                    self._set_split_cell(row, split_col=split_col, value="-", path=path)
                )
                continue
            wired_rows.append(row)
        return wired_rows

    def _apply_branch_markers(
        self,
        *,
        anchor_rows: list[tuple[str, ...]],
        branch_rows: list[tuple[str, ...]],
        branch_entry_rows: list[int],
        split_col: int,
        path: str,
    ) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        if not branch_entry_rows:
            return anchor_rows, branch_rows

        marked_anchor_rows = anchor_rows.copy()
        marked_branch_rows = branch_rows.copy()

        total = len(branch_entry_rows) + 1
        marked_anchor_rows[0] = self._set_split_cell(
            marked_anchor_rows[0],
            split_col=split_col,
            value=_vertical_marker(index=0, total=total),
            path=path,
        )
        for marker_index, row_index in enumerate(branch_entry_rows, start=1):
            marked_branch_rows[row_index] = self._set_split_cell(
                marked_branch_rows[row_index],
                split_col=split_col,
                value=_vertical_marker(index=marker_index, total=total),
                path=path,
            )

        return marked_anchor_rows, marked_branch_rows

    def _set_split_cell(
        self,
        row: tuple[str, ...],
        *,
        split_col: int,
        value: str,
        path: str,
    ) -> tuple[str, ...]:
        if split_col < 0 or split_col >= _CONDITION_COLS:
            self._raise_issue(
                path=path,
                message="Condition columns exceed AE.",
                source=None,
            )

        row_cells = list(row)
        cell_index = split_col + 1
        existing = row_cells[cell_index]
        if existing not in {"", "-", "T", "|"}:
            self._raise_issue(
                path=path,
                message=(
                    f"Conflicting branch split content at column {split_col}: "
                    f"{existing!r} vs {value!r}."
                ),
                source=None,
            )
        row_cells[cell_index] = value
        return tuple(row_cells)

    def _set_marker(
        self,
        row: tuple[str, ...],
        *,
        marker: str,
        path: str,
    ) -> tuple[str, ...]:
        existing = row[0]
        if existing not in {"", marker}:
            self._raise_issue(
                path=path,
                message=f"Conflicting row marker: {existing!r} vs {marker!r}.",
                source=None,
            )
        row_cells = list(row)
        row_cells[0] = marker
        return tuple(row_cells)

    def _apply_parent_condition_prefix(
        self,
        row: tuple[str, ...],
        *,
        parent_condition_row: _ConditionRow,
        path: str,
    ) -> tuple[str, ...]:
        row_cells = list(row)
        for col in range(parent_condition_row.cursor):
            value = parent_condition_row.cells[col]
            if value == "":
                continue
            cell_index = col + 1
            existing = row_cells[cell_index]
            if existing not in {"", value}:
                self._raise_issue(
                    path=path,
                    message=(
                        f"Conflicting parent condition content at column {col}: "
                        f"{existing!r} vs {value!r}."
                    ),
                    source=None,
                )
            row_cells[cell_index] = value
        return tuple(row_cells)

    def _single_output_rows(
        self,
        condition_rows: list[_ConditionRow],
        *,
        output_token: str,
        first_marker: str,
    ) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for row_index, condition_row in enumerate(condition_rows):
            cells = condition_row.cells.copy()
            if condition_row.accepts_terms:
                # Main path: fill wires from cursor to AF column.
                for col in range(condition_row.cursor, _CONDITION_COLS):
                    if cells[col] == "":
                        cells[col] = "-"
            rows.append(
                tuple(
                    [
                        first_marker if row_index == 0 else "",
                        *cells,
                        output_token if row_index == 0 else "",
                    ]
                )
            )
        return rows

    def _multi_output_rows(
        self,
        *,
        condition_row: _ConditionRow,
        output_tokens: list[str],
        path: str,
        first_marker: str = "R",
    ) -> list[tuple[str, ...]]:
        split_col = condition_row.cursor
        if split_col >= _CONDITION_COLS:
            self._raise_issue(
                path=path,
                message="Condition columns exceed AE before output branch split.",
                source=None,
            )

        rows: list[tuple[str, ...]] = []
        for index, output_token in enumerate(output_tokens):
            cells = condition_row.cells.copy() if index == 0 else [""] * _CONDITION_COLS
            self._write_cell(
                cells,
                split_col,
                _vertical_marker(index=index, total=len(output_tokens)),
                path=path,
                source=None,
            )
            for col in range(split_col + 1, _CONDITION_COLS):
                if cells[col] == "":
                    cells[col] = "-"
            rows.append(tuple([first_marker if index == 0 else "", *cells, output_token]))
        return rows

    def _expand_conditions(self, conditions: list[Condition], *, path: str) -> list[_ConditionRow]:
        rows: list[_ConditionRow] = [_ConditionRow(cells=[""] * _CONDITION_COLS, cursor=0)]
        for condition in conditions:
            rows = self._apply_condition_term(rows, condition, path=path)
        return rows

    def _apply_condition_term(
        self,
        rows: list[_ConditionRow],
        condition: Condition,
        *,
        path: str,
    ) -> list[_ConditionRow]:
        if isinstance(condition, AllCondition):
            expanded = rows
            for child in condition.conditions:
                expanded = self._apply_condition_term(expanded, child, path=path)
            return expanded

        if isinstance(condition, AnyCondition):
            return self._apply_any_condition(rows, condition, path=path)

        token = self._condition_leaf_token(condition, path=path)
        for row in rows:
            if not row.accepts_terms:
                continue
            if row.cursor >= _CONDITION_COLS:
                self._raise_issue(
                    path=path,
                    message="Condition columns exceed AE.",
                    source=condition,
                )
            self._write_cell(row.cells, row.cursor, token, path=path, source=condition)
            row.cursor += 1
        return rows

    def _apply_any_condition(
        self,
        rows: list[_ConditionRow],
        condition: AnyCondition,
        *,
        path: str,
    ) -> list[_ConditionRow]:
        if not condition.conditions:
            self._raise_issue(
                path=path,
                message="any_of() cannot be empty.",
                source=condition,
            )

        active_rows: list[_ConditionRow] = []
        frozen_rows: list[_ConditionRow] = []

        for row in rows:
            if not row.accepts_terms:
                frozen_rows.append(row.clone())
                continue

            start_cursor = row.cursor
            branch_rows: list[_ConditionRow] = []
            for branch_condition in condition.conditions:
                seeded = [row.clone()]
                branch_rows.extend(self._apply_condition_term(seeded, branch_condition, path=path))

            if len(branch_rows) == 1:
                active_rows.extend(branch_rows)
                continue

            merge_col = max(branch.cursor for branch in branch_rows)
            if merge_col >= _CONDITION_COLS:
                self._raise_issue(
                    path=path,
                    message="Condition columns exceed AE after OR branch expansion.",
                    source=condition,
                )

            total = len(branch_rows)
            for index, branch_row in enumerate(branch_rows):
                if index > 0:
                    for col in range(start_cursor):
                        branch_row.cells[col] = ""

                if index < total - 1 and start_cursor > 0:
                    contact_cols = [
                        col
                        for col in range(start_cursor, branch_row.cursor)
                        if branch_row.cells[col] not in {"", "-", "T", "|"}
                    ]
                    if contact_cols:
                        contact_col = contact_cols[0]
                        tok = branch_row.cells[contact_col]
                        if tok and not tok.startswith("T:"):
                            # Preserve Click's upward branch marker form.
                            branch_row.cells[contact_col] = f"T:{tok}"

                for col in range(branch_row.cursor, merge_col):
                    if branch_row.cells[col] == "":
                        branch_row.cells[col] = "-"

                marker = _output_bus_marker(index=index, total=total)
                if marker:
                    self._write_cell(
                        branch_row.cells,
                        merge_col,
                        marker,
                        path=path,
                        source=condition,
                    )

                branch_row.cursor = merge_col + 1
                branch_row.accepts_terms = index == 0

            branch_rows = self._compact_any_triplet(
                branch_rows,
                start_cursor=start_cursor,
                merge_col=merge_col,
            )
            active_rows.extend(branch_rows)

        if not active_rows:
            return frozen_rows

        if frozen_rows:
            return self._merge_any_frozen_rows(active_rows, frozen_rows)
        return active_rows

    def _merge_any_frozen_rows(
        self,
        active_rows: list[_ConditionRow],
        frozen_rows: list[_ConditionRow],
    ) -> list[_ConditionRow]:
        merged = [row.clone() for row in active_rows]
        if not merged:
            return [row.clone() for row in frozen_rows]

        split_col = max(0, merged[0].cursor - 1)
        for index, frozen in enumerate(frozen_rows):
            target_index = index + 1
            if target_index >= len(merged):
                merged.append(frozen.clone())
                continue

            target = merged[target_index]
            for col in range(split_col):
                if target.cells[col] == merged[0].cells[col]:
                    target.cells[col] = ""

            for col, value in enumerate(frozen.cells):
                if value == "":
                    continue
                existing = target.cells[col]
                if existing in {"", value}:
                    target.cells[col] = value
                    continue
                if col < split_col and value not in {"-", "T", "|"}:
                    target.cells[col] = value
            target.accepts_terms = False

        return merged

    @staticmethod
    def _contact_count(row: _ConditionRow, *, start: int, end: int) -> int:
        return sum(1 for col in range(start, end) if row.cells[col] not in {"", "-", "T", "|"})

    def _compact_any_triplet(
        self,
        branch_rows: list[_ConditionRow],
        *,
        start_cursor: int,
        merge_col: int,
    ) -> list[_ConditionRow]:
        if len(branch_rows) != 3:
            return branch_rows

        first, middle, last = branch_rows
        if self._contact_count(first, start=start_cursor, end=merge_col) != 1:
            return branch_rows
        if self._contact_count(middle, start=start_cursor, end=merge_col) < 2:
            return branch_rows
        if self._contact_count(last, start=start_cursor, end=merge_col) != 1:
            return branch_rows
        if middle.cells[merge_col] != "|":
            return branch_rows

        last_token_col = next(
            (
                col
                for col in range(start_cursor, merge_col)
                if last.cells[col] not in {"", "-", "T", "|"}
            ),
            None,
        )
        if last_token_col is None:
            return branch_rows

        last_token = last.cells[last_token_col]
        if merge_col > 0 and not last_token.startswith("T:"):
            first.cells[merge_col] = f"T:{last_token}"
        else:
            first.cells[merge_col] = last_token
        middle.cells[merge_col] = ""
        middle.cursor = merge_col
        middle.accepts_terms = False
        first.accepts_terms = True
        # After compaction the middle row is the last visible branch —
        # strip any T: prefix that was applied before compaction.
        for col in range(start_cursor, merge_col):
            tok = middle.cells[col]
            if tok.startswith("T:"):
                middle.cells[col] = tok[2:]
                break
        return [first, middle]

    def _write_cell(
        self,
        cells: list[str],
        col: int,
        value: str,
        *,
        path: str,
        source: Any,
    ) -> None:
        if col < 0 or col >= _CONDITION_COLS:
            self._raise_issue(
                path=path,
                message="Condition columns exceed AE.",
                source=source,
            )
        existing = cells[col]
        if existing not in {"", value}:
            self._raise_issue(
                path=path,
                message=f"Conflicting cell content at column {col}: {existing!r} vs {value!r}.",
                source=source,
            )
        cells[col] = value


# ---- Marker helpers ----
def _vertical_marker(*, index: int, total: int) -> str:
    if total <= 1:
        return "-"
    if index < total - 1:
        return "T"
    return "-"


def _output_bus_marker(*, index: int, total: int) -> str:
    """Marker for the output-side vertical bus of an OR merge.

    Native Click topology: T (right+down) on the first row, ``|``
    (vertical pass-through) on middle rows, nothing on the last row.
    """
    if total <= 1:
        return "-"
    if index == 0:
        return "T"
    if index < total - 1:
        return "|"
    return ""


__all__ = [
    "_CONDITION_COLS",
    "_HEADER",
    "_LayoutMixin",
    "_output_bus_marker",
    "_vertical_marker",
]
