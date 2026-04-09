"""Ladder row-matrix layout and branching helpers."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _RenderedExecutionRow:
    cells: list[str]
    af: str = ""


@dataclass
class _ExecutionBand:
    """One contiguous occupied row band for a rendered execution item."""

    start_row: int
    rows: list[_RenderedExecutionRow]
    needs_parent_fill: bool

    @property
    def stop_row(self) -> int:
        return self.start_row + len(self.rows) - 1


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
        path: str,
        first_marker: str = "R",
    ) -> list[tuple[str, ...]]:
        block_rows = self._render_execution_block(
            conditions=rung._conditions,
            execution_items=rung._execution_items,
            path=path,
            base_col=0,
            source=rung,
        )
        return [
            tuple([first_marker if row_index == 0 else "", *row.cells, row.af])
            for row_index, row in enumerate(block_rows)
        ]

    def _branch_pin_rows(
        self,
        rung: Rung,
        *,
        path: str,
    ) -> list[tuple[str, ...]]:
        """Collect pin rows for branched rungs in source order."""
        instruction_index_by_id = {id(item): idx for idx, item in enumerate(rung._instructions)}
        branch_index_by_id = {id(item): idx for idx, item in enumerate(rung._branches)}

        rows: list[tuple[str, ...]] = []
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
                rows.extend(self._branch_pin_rows(item, path=branch_path))
                continue

            instruction_idx = instruction_index_by_id.get(id(item))
            if instruction_idx is None:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message="Internal error: instruction item missing from instruction index map.",
                    source=item,
                )
            instruction_path = f"{path}.instruction[{instruction_idx}]({type(item).__name__})"
            rows.extend(self._pin_rows(item, path=instruction_path))

        return rows

    def _render_execution_block(
        self,
        *,
        conditions: list[Condition],
        execution_items: list[Any],
        path: str,
        base_col: int,
        source: Any,
    ) -> list[_RenderedExecutionRow]:
        if not execution_items:
            return []

        condition_rows = self._offset_condition_rows(
            self._expand_conditions(list(conditions), path=f"{path}.condition"),
            base_col=base_col,
            path=f"{path}.condition",
            source=source,
        )
        split_col = condition_rows[0].cursor
        if split_col >= _CONDITION_COLS:
            self._raise_issue(
                path=f"{path}.branch",
                message="Condition columns exceed AE before branch split.",
                source=source,
            )

        instruction_index_by_id: dict[int, int] = {}
        branch_index_by_id: dict[int, int] = {}
        if isinstance(source, Rung):
            instruction_index_by_id = {
                id(item): idx for idx, item in enumerate(source._instructions)
            }
            branch_index_by_id = {id(item): idx for idx, item in enumerate(source._branches)}

        rendered_items: list[tuple[list[_RenderedExecutionRow], bool]] = []

        for item in execution_items:
            if isinstance(item, Rung):
                branch_idx = branch_index_by_id.get(id(item))
                if branch_idx is None:
                    self._raise_issue(
                        path=f"{path}.branch",
                        message="Internal error: branch item missing from branch index map.",
                        source=item,
                    )
                branch_path = f"{path}.branch[{branch_idx}]"
                block = self._render_branch_block(
                    branch=item,
                    path=branch_path,
                    base_col=split_col,
                )
                needs_parent_fill = False
            else:
                instruction_idx = instruction_index_by_id.get(id(item))
                if instruction_idx is None:
                    self._raise_issue(
                        path=f"{path}.instruction",
                        message="Internal error: instruction item missing from instruction index map.",
                        source=item,
                    )
                instruction_path = f"{path}.instruction[{instruction_idx}]({type(item).__name__})"
                block = [
                    _RenderedExecutionRow(
                        cells=[""] * _CONDITION_COLS,
                        af=self._instruction_token(item, path=instruction_path),
                    )
                ]
                needs_parent_fill = True

            if not block:
                continue
            rendered_items.append((block, needs_parent_fill))

        if not rendered_items:
            return []

        bands: list[_ExecutionBand] = []
        next_row = 0
        for block, needs_parent_fill in rendered_items:
            bands.append(
                _ExecutionBand(
                    start_row=next_row,
                    rows=block,
                    needs_parent_fill=needs_parent_fill,
                )
            )
            next_row += len(block)

        total_rows = max(len(condition_rows), next_row)
        rows: list[_RenderedExecutionRow] = []
        for row_index in range(total_rows):
            cells = (
                condition_rows[row_index].cells.copy()
                if row_index < len(condition_rows)
                else [""] * _CONDITION_COLS
            )
            rows.append(_RenderedExecutionRow(cells=cells))

        for band in bands:
            self._overlay_block_rows(
                target_rows=rows,
                start_row=band.start_row,
                block_rows=band.rows,
                path=path,
                source=source,
            )

        for band in bands[:-1]:
            for row_index in range(band.start_row, band.stop_row + 1):
                self._mark_downward_continuation(
                    rows[row_index].cells,
                    split_col,
                    path=path,
                    source=source,
                )

        for band in bands:
            if not band.needs_parent_fill:
                continue
            row = rows[band.start_row]
            if row.cells[split_col] == "":
                row.cells[split_col] = "-"
            for col in range(split_col + 1, _CONDITION_COLS):
                if row.cells[col] == "":
                    row.cells[col] = "-"

        return rows

    def _render_branch_block(
        self,
        *,
        branch: Rung,
        path: str,
        base_col: int,
    ) -> list[_RenderedExecutionRow]:
        if any(
            type(instruction).__name__ == "ForLoopInstruction"
            for instruction in branch._instructions
        ):
            self._raise_issue(
                path=f"{path}.instruction",
                message="forloop() is not supported inside branch(...) in Click ladder v1 export.",
                source=branch,
            )

        local_conditions = list(branch._conditions[branch._branch_condition_start :])
        return self._render_execution_block(
            conditions=local_conditions,
            execution_items=branch._execution_items,
            path=path,
            base_col=base_col,
            source=branch,
        )

    def _offset_condition_rows(
        self,
        condition_rows: list[_ConditionRow],
        *,
        base_col: int,
        path: str,
        source: Any,
    ) -> list[_ConditionRow]:
        """Shift relative condition rows into absolute column positions."""
        shifted: list[_ConditionRow] = []
        total_rows = len(condition_rows)
        for row_index, row in enumerate(condition_rows):
            cells = [""] * _CONDITION_COLS
            for col, value in enumerate(row.cells):
                if value == "":
                    continue
                target_col = base_col + col
                if target_col >= _CONDITION_COLS:
                    self._raise_issue(
                        path=path,
                        message="Condition columns exceed AE.",
                        source=source,
                    )
                if (
                    base_col > 0
                    and row_index < total_rows - 1
                    and col == 0
                    and value not in {"-", "T", "|"}
                    and not value.startswith("T:")
                ):
                    value = f"T:{value}"
                self._write_cell(cells, target_col, value, path=path, source=source)
            shifted.append(
                _ConditionRow(
                    cells=cells,
                    cursor=base_col + row.cursor,
                    accepts_terms=row.accepts_terms,
                )
            )
        return shifted

    def _overlay_block_rows(
        self,
        *,
        target_rows: list[_RenderedExecutionRow],
        start_row: int,
        block_rows: list[_RenderedExecutionRow],
        path: str,
        source: Any,
    ) -> None:
        for row_offset, block_row in enumerate(block_rows):
            row = target_rows[start_row + row_offset]
            for col, value in enumerate(block_row.cells):
                if value == "":
                    continue
                self._write_cell(row.cells, col, value, path=path, source=source)
            if block_row.af:
                if row.af not in {"", block_row.af}:
                    self._raise_issue(
                        path=path,
                        message=f"Conflicting AF content: {row.af!r} vs {block_row.af!r}.",
                        source=source,
                    )
                row.af = block_row.af

    def _mark_downward_continuation(
        self,
        cells: list[str],
        col: int,
        *,
        path: str,
        source: Any,
    ) -> None:
        if col < 0 or col >= _CONDITION_COLS:
            self._raise_issue(
                path=path,
                message="Condition columns exceed AE before output branch split.",
                source=source,
            )

        existing = cells[col]
        if existing in {"", "-"}:
            marker = "|" if any(cell != "" for cell in cells[col + 1 :]) else "T"
            cells[col] = marker
            return
        if existing in {"T", "|"} or existing.startswith("T:"):
            return
        cells[col] = f"T:{existing}"

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
        local = local_rows[0]
        tokens: list[str] = []
        for col in range(local.cursor):
            value = local.cells[col]
            if value != "":
                tokens.append(value)
        return tokens

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
                message="Or() cannot be empty.",
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

            active_rows.extend(branch_rows)

        if not active_rows:
            return frozen_rows

        if frozen_rows:
            return self._merge_any_frozen_rows(active_rows, frozen_rows, start_cursor)
        return active_rows

    def _merge_any_frozen_rows(
        self,
        active_rows: list[_ConditionRow],
        frozen_rows: list[_ConditionRow],
        start_cursor: int,
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
            # Only dedup inherited prefix cells (before this OR's branches).
            # Cells at start_cursor and beyond belong to the current OR's
            # branch content (e.g. wire fills) and must not be cleared.
            for col in range(min(split_col, start_cursor)):
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
