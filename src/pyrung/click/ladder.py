"""Click ladder CSV export helpers."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.expression import (
    AbsExpr,
    AddExpr,
    AndExpr,
    DivExpr,
    ExprCompareEq,
    ExprCompareGe,
    ExprCompareGt,
    ExprCompareLe,
    ExprCompareLt,
    ExprCompareNe,
    Expression,
    FloorDivExpr,
    InvertExpr,
    LiteralExpr,
    LShiftExpr,
    MathFuncExpr,
    ModExpr,
    MulExpr,
    NegExpr,
    OrExpr,
    PosExpr,
    PowExpr,
    RShiftExpr,
    ShiftFuncExpr,
    SubExpr,
    TagExpr,
    XorExpr,
)
from pyrung.core.instruction.calc import infer_calc_mode
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.rung import Rung
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap, _BlockEntry
    from pyrung.core.program import Program

_CONDITION_COLS = 31
_HEADER: tuple[str, ...] = (
    "marker",
    *tuple([chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]),
    "AF",
)

_BINARY_OP_SYMBOL: dict[type[Expression], str] = {
    AddExpr: "+",
    SubExpr: "-",
    MulExpr: "*",
    DivExpr: "/",
    FloorDivExpr: "//",
    ModExpr: "%",
    PowExpr: "**",
    AndExpr: "&",
    OrExpr: "|",
    XorExpr: "^",
    LShiftExpr: "<<",
    RShiftExpr: ">>",
}

_UNARY_PREFIX: dict[type[Expression], str] = {
    NegExpr: "-",
    PosExpr: "+",
    InvertExpr: "~",
}

_BINARY_EXPR_TYPES: tuple[type[Expression], ...] = tuple(_BINARY_OP_SYMBOL)

_COMPARE_OPS: dict[type[Condition], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
    IndirectCompareEq: "==",
    IndirectCompareNe: "!=",
    IndirectCompareLt: "<",
    IndirectCompareLe: "<=",
    IndirectCompareGt: ">",
    IndirectCompareGe: ">=",
    ExprCompareEq: "==",
    ExprCompareNe: "!=",
    ExprCompareLt: "<",
    ExprCompareLe: "<=",
    ExprCompareGt: ">",
    ExprCompareGe: ">=",
}

Issue = dict[str, str | int | None]


class LadderExportError(RuntimeError):
    """Raised when strict ladder export prevalidation or lowering fails."""

    def __init__(self, issues: list[Issue] | tuple[Issue, ...]):
        def _safe_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        normalized: list[Issue] = []
        for issue in issues:
            normalized.append(
                {
                    "path": str(issue.get("path", "")),
                    "message": str(issue.get("message", "")),
                    "source_file": (
                        None if issue.get("source_file") is None else str(issue.get("source_file"))
                    ),
                    "source_line": _safe_int(issue.get("source_line")),
                }
            )
        self.issues: tuple[Issue, ...] = tuple(normalized)

        if self.issues:
            preview = "; ".join(f"{issue['path']}: {issue['message']}" for issue in self.issues[:3])
            if len(self.issues) > 3:
                preview += f" (+{len(self.issues) - 3} more)"
        else:
            preview = "Ladder export failed."
        super().__init__(preview)


@dataclass(frozen=True)
class LadderBundle:
    """Row-matrix CSV payload for Click ladder export."""

    main_rows: tuple[tuple[str, ...], ...]
    subroutine_rows: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]

    def write(self, directory: str | Path) -> None:
        output_dir = Path(directory)
        output_dir.mkdir(parents=True, exist_ok=True)

        with (output_dir / "main.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows(self.main_rows)

        slug_counts: dict[str, int] = {}
        for subroutine_name, rows in self.subroutine_rows:
            base_slug = _slugify(subroutine_name)
            count = slug_counts.get(base_slug, 0)
            slug_counts[base_slug] = count + 1
            slug = base_slug if count == 0 else f"{base_slug}_{count + 1}"

            with (output_dir / f"sub_{slug}.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerows(rows)


class _RenderError(RuntimeError):
    def __init__(self, issue: Issue):
        self.issue = issue
        super().__init__(f"{issue.get('path')}: {issue.get('message')}")


@dataclass
class _ConditionRow:
    cells: list[str]
    cursor: int
    accepts_terms: bool = True

    def clone(self) -> _ConditionRow:
        return _ConditionRow(
            cells=self.cells.copy(),
            cursor=self.cursor,
            accepts_terms=self.accepts_terms,
        )


def build_ladder_bundle(tag_map: TagMap, program: Program) -> LadderBundle:
    """Render a `Program` into deterministic Click ladder CSV row matrices."""
    return _LadderExporter(tag_map=tag_map, program=program).export()


class _LadderExporter:
    def __init__(self, *, tag_map: TagMap, program: Program) -> None:
        self._tag_map = tag_map
        self._program = program

    def export(self) -> LadderBundle:
        try:
            self._run_precheck()

            main_rows: list[tuple[str, ...]] = [tuple(_HEADER)]
            main_rows.extend(
                self._render_scope(self._program.rungs, scope="main", subroutine_name=None)
            )

            subroutine_rows: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
            for subroutine_name in sorted(self._program.subroutines):
                rows: list[tuple[str, ...]] = [tuple(_HEADER)]
                rows.extend(
                    self._render_scope(
                        self._program.subroutines[subroutine_name],
                        scope="subroutine",
                        subroutine_name=subroutine_name,
                    )
                )
                rows = self._ensure_subroutine_return_tail(rows, subroutine_name=subroutine_name)
                subroutine_rows.append((subroutine_name, tuple(rows)))

            return LadderBundle(main_rows=tuple(main_rows), subroutine_rows=tuple(subroutine_rows))
        except _RenderError as exc:
            raise LadderExportError([exc.issue]) from None

    def _run_precheck(self) -> None:
        report = self._tag_map.validate(self._program, mode="strict")
        findings = [*report.errors, *report.warnings, *report.hints]
        if findings:
            issues: list[Issue] = []
            for finding in findings:
                issues.append(
                    {
                        "path": finding.location,
                        "message": f"{finding.code}: {finding.message}",
                        "source_file": None,
                        "source_line": None,
                    }
                )
            raise LadderExportError(issues)

        for subroutine_name in sorted(self._program.subroutines):
            for rung_index, rung in enumerate(self._program.subroutines[subroutine_name]):
                self._assert_no_nested_subroutine_calls(
                    rung,
                    path=f"subroutine[{subroutine_name}].rung[{rung_index}]",
                )

    def _assert_no_nested_subroutine_calls(self, rung: Rung, *, path: str) -> None:
        for instruction_index, instruction in enumerate(rung._instructions):
            instruction_path = (
                f"{path}.instruction[{instruction_index}]({type(instruction).__name__})"
            )
            self._assert_no_nested_subroutine_calls_in_instruction(
                instruction,
                path=instruction_path,
            )

        for branch_index, branch in enumerate(rung._branches):
            self._assert_no_nested_subroutine_calls(
                branch,
                path=f"{path}.branch[{branch_index}]",
            )

    def _assert_no_nested_subroutine_calls_in_instruction(
        self,
        instruction: Any,
        *,
        path: str,
    ) -> None:
        if type(instruction).__name__ == "CallInstruction":
            self._raise_issue(
                path=path,
                message="Nested subroutine calls are not supported for Click export.",
                source=instruction,
            )

        if type(instruction).__name__ != "ForLoopInstruction":
            return

        for child_index, child_instruction in enumerate(getattr(instruction, "instructions", ())):
            child_path = f"{path}.instruction[{child_index}]({type(child_instruction).__name__})"
            self._assert_no_nested_subroutine_calls_in_instruction(
                child_instruction,
                path=child_path,
            )

    def _render_scope(
        self,
        rungs: list[Rung],
        *,
        scope: str,
        subroutine_name: str | None,
    ) -> list[tuple[str, ...]]:
        rows: list[tuple[str, ...]] = []
        for rung_index, rung in enumerate(rungs):
            base_path = (
                f"subroutine[{subroutine_name}].rung[{rung_index}]"
                if scope == "subroutine"
                else f"main.rung[{rung_index}]"
            )
            rows.extend(self._render_rung(rung, path=base_path))
        return rows

    @staticmethod
    def _comment_rows(rung: Rung) -> list[tuple[str, ...]]:
        if rung.comment is None:
            return []
        return [("#", line) for line in rung.comment.splitlines()]

    def _render_rung(self, rung: Rung, *, path: str) -> list[tuple[str, ...]]:
        if not rung._instructions and not rung._branches:
            return []

        comment_rows = self._comment_rows(rung)

        if any(
            type(instruction).__name__ == "ForLoopInstruction" for instruction in rung._instructions
        ):
            if len(rung._instructions) != 1 or rung._branches:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message=(
                        "Rungs that contain forloop() cannot include additional instructions "
                        "or branch(...) blocks "
                        "in Click ladder v1 export."
                    ),
                    source=rung,
                )
            return comment_rows + self._render_forloop_instruction(
                instruction=rung._instructions[0],
                conditions=rung._conditions,
                path=f"{path}.instruction[0](ForLoopInstruction)",
            )

        condition_rows = self._expand_conditions(rung._conditions, path=f"{path}.condition")

        if rung._branches:
            branch_rows = self._render_rung_with_branches(
                rung,
                condition_rows=condition_rows,
                path=path,
            )
            if not branch_rows:
                return []
            return comment_rows + branch_rows

        output_rows: list[tuple[str, ...]]
        pin_rows: list[tuple[str, ...]]
        if rung._instructions:
            output_rows, pin_rows = self._render_instruction_rows(
                instructions=rung._instructions,
                condition_rows=condition_rows,
                path=path,
                first_marker="R",
                source=rung,
            )
        else:
            output_rows = self._single_output_rows(
                condition_rows,
                output_token="",
                first_marker="R",
            )
            pin_rows = []

        rows = comment_rows + output_rows
        rows.extend(pin_rows)
        return rows

    def _render_rung_with_branches(
        self,
        rung: Rung,
        *,
        condition_rows: list[_ConditionRow],
        path: str,
    ) -> list[tuple[str, ...]]:
        parent_cursor = condition_rows[0].cursor
        if parent_cursor >= _CONDITION_COLS:
            self._raise_issue(
                path=f"{path}.branch",
                message="Condition columns exceed AE before branch split.",
                source=rung,
            )

        instruction_index_by_id = {id(item): idx for idx, item in enumerate(rung._instructions)}
        branch_index_by_id = {id(item): idx for idx, item in enumerate(rung._branches)}

        rows: list[tuple[str, ...]] = []
        split_entries: list[int] = []
        first_row_emitted = False

        for item in rung._execution_items:
            if isinstance(item, Rung):
                branch_idx = branch_index_by_id.get(id(item))
                if branch_idx is None:
                    self._raise_issue(
                        path=f"{path}.branch",
                        message="Internal error: branch item missing from branch index map.",
                        source=item,
                    )

                branch_rows, branch_split_entries = self._render_branch_item_rows(
                    branch=item,
                    parent_cursor=parent_cursor,
                    path=f"{path}.branch[{branch_idx}]",
                )
                if branch_rows and not first_row_emitted:
                    branch_rows[0] = self._set_marker(
                        branch_rows[0],
                        marker="R",
                        path=f"{path}.branch[{branch_idx}]",
                    )
                    branch_rows[0] = self._apply_parent_condition_prefix(
                        branch_rows[0],
                        parent_condition_row=condition_rows[0],
                        path=f"{path}.branch[{branch_idx}]",
                    )
                    first_row_emitted = True

                    # Splice OR continuation rows after the first row.
                    if len(condition_rows) > 1:
                        or_cont = self._build_or_continuation_rows(condition_rows[1:])
                        n_or = len(or_cont)
                        base = len(rows)
                        rows.append(branch_rows[0])
                        rows.extend(or_cont)
                        rows.extend(branch_rows[1:])
                        split_entries.extend(
                            base + (e if e == 0 else e + n_or) for e in branch_split_entries
                        )
                        continue

                base = len(rows)
                rows.extend(branch_rows)
                split_entries.extend(base + offset for offset in branch_split_entries)
                continue

            instruction_idx = instruction_index_by_id.get(id(item))
            if instruction_idx is None:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message="Internal error: instruction item missing from instruction index map.",
                    source=item,
                )

            instruction_path = f"{path}.instruction[{instruction_idx}]({type(item).__name__})"
            instruction_token = self._instruction_token(item, path=instruction_path)
            if not first_row_emitted:
                instruction_rows = self._single_output_rows(
                    condition_rows,
                    output_token=instruction_token,
                    first_marker="R",
                )
            else:
                instruction_rows = [
                    self._continuation_output_row(
                        parent_cursor=parent_cursor,
                        output_token=instruction_token,
                        marker="",
                    )
                ]
            first_row_emitted = True

            base = len(rows)
            rows.extend(instruction_rows)
            split_entries.append(base)
            rows.extend(self._pin_rows(item, path=instruction_path))

        if len(split_entries) > 1:
            for marker_index, row_index in enumerate(split_entries):
                rows[row_index] = self._set_split_cell(
                    rows[row_index],
                    split_col=parent_cursor,
                    value=_vertical_marker(index=marker_index, total=len(split_entries)),
                    path=f"{path}.branch",
                )

            # Fill pass-through | markers on non-entry rows (OR continuations,
            # pin rows) that sit between the first and last split entries.
            # | means "vertical wire passes through" (not a junction entry).
            split_set = set(split_entries)
            for r in range(min(split_entries) + 1, max(split_entries)):
                if r not in split_set:
                    rows[r] = self._set_split_cell(
                        rows[r],
                        split_col=parent_cursor,
                        value="|",
                        path=f"{path}.branch",
                    )

        return rows

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
            if len(condition_rows) != 1:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message=(
                        "Rungs with both OR-expanded conditions and multiple output instructions "
                        "are not supported in Click ladder v1 export."
                    ),
                    source=source,
                )
            output_rows = self._multi_output_rows(
                condition_row=condition_rows[0],
                output_tokens=instruction_tokens,
                path=f"{path}.instruction",
                first_marker=first_marker,
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

    def _render_forloop_instruction(
        self,
        *,
        instruction: Any,
        conditions: list[Condition],
        path: str,
    ) -> list[tuple[str, ...]]:
        if type(instruction).__name__ != "ForLoopInstruction":
            self._raise_issue(
                path=path,
                message="Internal error: expected ForLoopInstruction.",
                source=instruction,
            )

        for_kw: dict[str, str] = {}
        if getattr(instruction, "oneshot", False):
            for_kw["oneshot"] = "1"
        for_token = self._fn(
            "for",
            self._render_operand(instruction.count, path=f"{path}.count", source=instruction),
            **for_kw,
        )
        rows = self._single_output_rows(
            self._expand_conditions(conditions, path=f"{path}.condition"),
            output_token=for_token,
            first_marker="R",
        )

        for child_index, child_instruction in enumerate(getattr(instruction, "instructions", ())):
            child_path = f"{path}.instruction[{child_index}]({type(child_instruction).__name__})"
            child_condition_rows = self._expand_conditions([], path=f"{child_path}.condition")
            child_rows = self._single_output_rows(
                child_condition_rows,
                output_token=self._instruction_token(child_instruction, path=child_path),
                first_marker="R",
            )
            child_rows.extend(self._pin_rows(child_instruction, path=child_path))
            rows.extend(child_rows)

        rows.extend(
            self._single_output_rows(
                self._expand_conditions([], path=f"{path}.next"),
                output_token=self._fn("next"),
                first_marker="R",
            )
        )
        return rows

    def _pin_rows(self, instruction: Any, *, path: str) -> list[tuple[str, ...]]:
        pin_specs = self._pin_specs(instruction, path=path)
        rows: list[tuple[str, ...]] = []
        for pin_name, condition, pin_token in pin_specs:
            condition_rows = self._expand_conditions([condition], path=f"{path}.pin[{pin_name}]")
            rows.extend(
                self._single_output_rows(
                    condition_rows,
                    output_token=pin_token,
                    first_marker="",
                )
            )
        return rows

    def _pin_specs(self, instruction: Any, *, path: str) -> list[tuple[str, Condition, str]]:
        specs: list[tuple[str, Condition, str]] = []
        instruction_type = type(instruction).__name__

        if instruction_type == "OnDelayInstruction":
            reset_condition = getattr(instruction, "reset_condition", None)
            if reset_condition is not None:
                specs.append(("reset", reset_condition, ".reset()"))
            return specs

        if instruction_type == "CountUpInstruction":
            down_condition = getattr(instruction, "down_condition", None)
            if down_condition is not None:
                specs.append(("down", down_condition, ".down()"))
            reset_condition = getattr(instruction, "reset_condition", None)
            if reset_condition is not None:
                specs.append(("reset", reset_condition, ".reset()"))
            return specs

        if instruction_type == "CountDownInstruction":
            reset_condition = getattr(instruction, "reset_condition", None)
            if reset_condition is not None:
                specs.append(("reset", reset_condition, ".reset()"))
            return specs

        if instruction_type == "ShiftInstruction":
            clock_condition = getattr(instruction, "clock_condition", None)
            if clock_condition is not None:
                specs.append(("clock", clock_condition, ".clock()"))
            reset_condition = getattr(instruction, "reset_condition", None)
            if reset_condition is not None:
                specs.append(("reset", reset_condition, ".reset()"))
            return specs

        if instruction_type in {"EventDrumInstruction", "TimeDrumInstruction"}:
            reset_condition = getattr(instruction, "reset_condition", None)
            if reset_condition is not None:
                specs.append(("reset", reset_condition, ".reset()"))
            jump_condition = getattr(instruction, "jump_condition", None)
            jump_step = getattr(instruction, "jump_step", None)
            if jump_condition is not None and jump_step is not None:
                jump_value = self._render_operand(
                    jump_step,
                    path=f"{path}.jump_step",
                    source=instruction,
                )
                specs.append(("jump", jump_condition, f".jump({jump_value})"))
            jog_condition = getattr(instruction, "jog_condition", None)
            if jog_condition is not None:
                specs.append(("jog", jog_condition, ".jog()"))
            return specs

        return specs

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

        expanded_rows: list[_ConditionRow] = []
        for row in rows:
            if not row.accepts_terms:
                expanded_rows.append(row)
                continue

            branch_rows: list[_ConditionRow] = []
            for branch_condition in condition.conditions:
                seeded = [row.clone()]
                branch_rows.extend(self._apply_condition_term(seeded, branch_condition, path=path))

            if len(branch_rows) == 1:
                expanded_rows.extend(branch_rows)
                continue

            merge_col = max(branch.cursor for branch in branch_rows)
            if merge_col >= _CONDITION_COLS:
                self._raise_issue(
                    path=path,
                    message="Condition columns exceed AE after OR branch expansion.",
                    source=condition,
                )

            for index, branch_row in enumerate(branch_rows):
                for col in range(branch_row.cursor, merge_col):
                    if branch_row.cells[col] == "":
                        branch_row.cells[col] = "-"
                self._write_cell(
                    branch_row.cells,
                    merge_col,
                    _vertical_marker(index=index, total=len(branch_rows)),
                    path=path,
                    source=condition,
                )
                branch_row.cursor = merge_col + 1
                branch_row.accepts_terms = index == 0
            expanded_rows.extend(branch_rows)
        return expanded_rows

    def _condition_leaf_token(self, condition: Condition, *, path: str) -> str:
        if isinstance(condition, BitCondition):
            return self._render_contact_token(condition.tag, path=f"{path}.tag", source=condition)
        if isinstance(condition, NormallyClosedCondition):
            token = self._render_contact_token(condition.tag, path=f"{path}.tag", source=condition)
            return f"~{token}"
        if isinstance(condition, RisingEdgeCondition):
            tag = self._require_non_immediate_tag(
                condition.tag,
                path=f"{path}.tag",
                source=condition,
                message="Immediate edge contacts are not supported in Click ladder export.",
            )
            return self._fn(
                "rise",
                self._resolve_tag(tag, path=f"{path}.tag", source=condition),
            )
        if isinstance(condition, FallingEdgeCondition):
            tag = self._require_non_immediate_tag(
                condition.tag,
                path=f"{path}.tag",
                source=condition,
                message="Immediate edge contacts are not supported in Click ladder export.",
            )
            return self._fn(
                "fall",
                self._resolve_tag(tag, path=f"{path}.tag", source=condition),
            )
        if isinstance(condition, IntTruthyCondition):
            left = self._resolve_tag(condition.tag, path=f"{path}.tag", source=condition)
            return f"{left}!=0"

        compare_op = _COMPARE_OPS.get(type(condition))
        if compare_op is not None:
            if isinstance(
                condition,
                (
                    CompareEq,
                    CompareNe,
                    CompareLt,
                    CompareLe,
                    CompareGt,
                    CompareGe,
                ),
            ):
                left = self._resolve_tag(condition.tag, path=f"{path}.left", source=condition)
                right = self._render_condition_value(
                    condition.value,
                    path=f"{path}.right",
                    source=condition,
                )
                return f"{left}{compare_op}{right}"

            if isinstance(
                condition,
                (
                    IndirectCompareEq,
                    IndirectCompareNe,
                    IndirectCompareLt,
                    IndirectCompareLe,
                    IndirectCompareGt,
                    IndirectCompareGe,
                ),
            ):
                left = self._render_indirect_ref(
                    condition.indirect_ref,
                    path=f"{path}.left",
                    source=condition,
                )
                right = self._render_condition_value(
                    condition.value,
                    path=f"{path}.right",
                    source=condition,
                )
                return f"{left}{compare_op}{right}"

            if isinstance(
                condition,
                (
                    ExprCompareEq,
                    ExprCompareNe,
                    ExprCompareLt,
                    ExprCompareLe,
                    ExprCompareGt,
                    ExprCompareGe,
                ),
            ):
                left = self._render_expression(
                    condition.left, path=f"{path}.left", source=condition
                )
                right = self._render_expression(
                    condition.right,
                    path=f"{path}.right",
                    source=condition,
                )
                return f"{left}{compare_op}{right}"

        self._raise_issue(
            path=path,
            message=f"Unsupported condition type: {type(condition).__name__}.",
            source=condition,
        )

    def _instruction_token(self, instruction: Any, *, path: str) -> str:
        instruction_type = type(instruction).__name__
        oneshot = getattr(instruction, "oneshot", False)
        oneshot_kw: dict[str, str] = {"oneshot": "1"} if oneshot else {}

        if instruction_type == "OutInstruction":
            return self._fn(
                "out",
                self._render_operand(
                    instruction.target,
                    path=f"{path}.target",
                    source=instruction,
                    allow_immediate=True,
                    immediate_context="coil",
                ),
                **oneshot_kw,
            )
        if instruction_type == "LatchInstruction":
            return self._fn(
                "latch",
                self._render_operand(
                    instruction.target,
                    path=f"{path}.target",
                    source=instruction,
                    allow_immediate=True,
                    immediate_context="coil",
                ),
            )
        if instruction_type == "ResetInstruction":
            return self._fn(
                "reset",
                self._render_operand(
                    instruction.target,
                    path=f"{path}.target",
                    source=instruction,
                    allow_immediate=True,
                    immediate_context="coil",
                ),
            )
        if instruction_type == "CopyInstruction":
            return self._fn(
                "copy",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(instruction.target, path=f"{path}.target", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "BlockCopyInstruction":
            return self._fn(
                "blockcopy",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "FillInstruction":
            return self._fn(
                "fill",
                self._render_operand(instruction.value, path=f"{path}.value", source=instruction),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "CalcInstruction":
            mode = infer_calc_mode(instruction.expression, instruction.dest).mode
            return self._fn(
                "calc",
                self._render_operand(
                    instruction.expression,
                    path=f"{path}.expression",
                    source=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                mode=str(mode),
                **oneshot_kw,
            )
        if instruction_type == "SearchInstruction":
            kw: dict[str, str] = {}
            if instruction.continuous:
                kw["continuous"] = "1"
            kw.update(oneshot_kw)
            return self._fn(
                "search",
                _quote(str(instruction.condition)),
                self._render_operand(instruction.value, path=f"{path}.value", source=instruction),
                self._render_operand(
                    instruction.search_range,
                    path=f"{path}.search_range",
                    source=instruction,
                ),
                self._render_operand(instruction.result, path=f"{path}.result", source=instruction),
                self._render_operand(instruction.found, path=f"{path}.found", source=instruction),
                **kw,
            )
        if instruction_type == "PackBitsInstruction":
            return self._fn(
                "pack_bits",
                self._render_operand(
                    instruction.bit_block,
                    path=f"{path}.bit_block",
                    source=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "PackWordsInstruction":
            return self._fn(
                "pack_words",
                self._render_operand(
                    instruction.word_block,
                    path=f"{path}.word_block",
                    source=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "PackTextInstruction":
            pt_kw: dict[str, str] = {}
            if instruction.allow_whitespace:
                pt_kw["allow_whitespace"] = "1"
            pt_kw.update(oneshot_kw)
            return self._fn(
                "pack_text",
                self._render_operand(
                    instruction.source_range,
                    path=f"{path}.source_range",
                    source=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **pt_kw,
            )
        if instruction_type == "UnpackToBitsInstruction":
            return self._fn(
                "unpack_to_bits",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(
                    instruction.bit_block,
                    path=f"{path}.bit_block",
                    source=instruction,
                ),
                **oneshot_kw,
            )
        if instruction_type == "UnpackToWordsInstruction":
            return self._fn(
                "unpack_to_words",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(
                    instruction.word_block,
                    path=f"{path}.word_block",
                    source=instruction,
                ),
                **oneshot_kw,
            )
        if instruction_type == "OnDelayInstruction":
            return self._fn(
                "on_delay",
                self._render_operand(
                    instruction.done_bit,
                    path=f"{path}.done_bit",
                    source=instruction,
                ),
                self._render_operand(
                    instruction.accumulator,
                    path=f"{path}.accumulator",
                    source=instruction,
                ),
                preset=self._render_operand(
                    instruction.preset, path=f"{path}.preset", source=instruction
                ),
                unit=self._render_operand(
                    instruction.unit, path=f"{path}.unit", source=instruction
                ),
            )
        if instruction_type == "OffDelayInstruction":
            return self._fn(
                "off_delay",
                self._render_operand(
                    instruction.done_bit,
                    path=f"{path}.done_bit",
                    source=instruction,
                ),
                self._render_operand(
                    instruction.accumulator,
                    path=f"{path}.accumulator",
                    source=instruction,
                ),
                preset=self._render_operand(
                    instruction.preset, path=f"{path}.preset", source=instruction
                ),
                unit=self._render_operand(
                    instruction.unit, path=f"{path}.unit", source=instruction
                ),
            )
        if instruction_type == "CountUpInstruction":
            return self._fn(
                "count_up",
                self._render_operand(
                    instruction.done_bit,
                    path=f"{path}.done_bit",
                    source=instruction,
                ),
                self._render_operand(
                    instruction.accumulator,
                    path=f"{path}.accumulator",
                    source=instruction,
                ),
                preset=self._render_operand(
                    instruction.preset, path=f"{path}.preset", source=instruction
                ),
            )
        if instruction_type == "CountDownInstruction":
            return self._fn(
                "count_down",
                self._render_operand(
                    instruction.done_bit,
                    path=f"{path}.done_bit",
                    source=instruction,
                ),
                self._render_operand(
                    instruction.accumulator,
                    path=f"{path}.accumulator",
                    source=instruction,
                ),
                preset=self._render_operand(
                    instruction.preset, path=f"{path}.preset", source=instruction
                ),
            )
        if instruction_type == "ShiftInstruction":
            return self._fn(
                "shift",
                self._render_operand(
                    instruction.bit_range,
                    path=f"{path}.bit_range",
                    source=instruction,
                ),
            )
        if instruction_type == "EventDrumInstruction":
            return self._fn(
                "event_drum",
                outputs=self._render_sequence(
                    instruction.outputs,
                    path=f"{path}.outputs",
                    source=instruction,
                ),
                events=self._render_condition_sequence(
                    instruction.events,
                    path=f"{path}.events",
                    source=instruction,
                ),
                pattern=self._render_pattern(instruction.pattern),
                current_step=self._render_operand(
                    instruction.current_step,
                    path=f"{path}.current_step",
                    source=instruction,
                ),
                completion_flag=self._render_operand(
                    instruction.completion_flag,
                    path=f"{path}.completion_flag",
                    source=instruction,
                ),
            )
        if instruction_type == "TimeDrumInstruction":
            return self._fn(
                "time_drum",
                outputs=self._render_sequence(
                    instruction.outputs,
                    path=f"{path}.outputs",
                    source=instruction,
                ),
                presets=self._render_sequence(
                    instruction.presets,
                    path=f"{path}.presets",
                    source=instruction,
                ),
                unit=self._render_operand(
                    instruction.unit, path=f"{path}.unit", source=instruction
                ),
                pattern=self._render_pattern(instruction.pattern),
                current_step=self._render_operand(
                    instruction.current_step,
                    path=f"{path}.current_step",
                    source=instruction,
                ),
                accumulator=self._render_operand(
                    instruction.accumulator,
                    path=f"{path}.accumulator",
                    source=instruction,
                ),
                completion_flag=self._render_operand(
                    instruction.completion_flag,
                    path=f"{path}.completion_flag",
                    source=instruction,
                ),
            )
        if instruction_type == "ModbusSendInstruction":
            count = len(instruction.addresses)
            remote_start = f"{instruction.bank}{instruction.start}"
            target_expr = _render_modbus_target(instruction)
            return self._fn(
                "send",
                target_expr,
                _quote(remote_start),
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                sending=self._render_operand(
                    instruction.sending,
                    path=f"{path}.sending",
                    source=instruction,
                ),
                success=self._render_operand(
                    instruction.success,
                    path=f"{path}.success",
                    source=instruction,
                ),
                error=self._render_operand(
                    instruction.error, path=f"{path}.error", source=instruction
                ),
                exception_response=self._render_operand(
                    instruction.exception_response,
                    path=f"{path}.exception_response",
                    source=instruction,
                ),
                count=str(count),
            )
        if instruction_type == "ModbusReceiveInstruction":
            count = len(instruction.addresses)
            remote_start = f"{instruction.bank}{instruction.start}"
            target_expr = _render_modbus_target(instruction)
            return self._fn(
                "receive",
                target_expr,
                _quote(remote_start),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                receiving=self._render_operand(
                    instruction.receiving,
                    path=f"{path}.receiving",
                    source=instruction,
                ),
                success=self._render_operand(
                    instruction.success,
                    path=f"{path}.success",
                    source=instruction,
                ),
                error=self._render_operand(
                    instruction.error, path=f"{path}.error", source=instruction
                ),
                exception_response=self._render_operand(
                    instruction.exception_response,
                    path=f"{path}.exception_response",
                    source=instruction,
                ),
                count=str(count),
            )
        if instruction_type == "CallInstruction":
            return self._fn("call", _quote(str(instruction.subroutine_name)))
        if instruction_type == "ReturnInstruction":
            return self._fn("return")

        self._raise_issue(
            path=path,
            message=f"Unsupported instruction type: {instruction_type}.",
            source=instruction,
        )

    def _explicit_count(
        self,
        *,
        operand: Any,
        configured_count: int | None,
        path: str,
        source: Any,
    ) -> int:
        if configured_count is not None:
            return int(configured_count)
        return self._operand_length(operand, path=path, source=source)

    def _operand_length(self, operand: Any, *, path: str, source: Any) -> int:
        if isinstance(operand, Tag):
            return 1
        if isinstance(operand, BlockRange):
            return len(list(operand.tags()))
        self._raise_issue(
            path=path,
            message=(
                "Automatic count inference is only supported for Tag and BlockRange operands."
            ),
            source=source,
        )

    def _render_condition_value(self, value: Any, *, path: str, source: Any) -> str:
        if isinstance(value, Condition):
            self._raise_issue(
                path=path,
                message="Condition values are not supported in comparisons.",
                source=source,
            )
        return self._render_operand(value, path=path, source=source)

    def _render_operand(
        self,
        value: Any,
        *,
        path: str,
        source: Any,
        allow_immediate: bool = False,
        immediate_context: str = "",
    ) -> str:
        if isinstance(value, ImmediateRef):
            if not allow_immediate:
                self._raise_issue(
                    path=path,
                    message="Immediate wrapper is not supported in this Click export context.",
                    source=source,
                )
            return self._render_immediate_operand(
                value,
                path=path,
                source=source,
                context=immediate_context,
            )
        if isinstance(value, Tag):
            return self._resolve_tag(value, path=path, source=source)
        if isinstance(value, BlockRange):
            return self._render_block_range(value, path=path, source=source)
        if isinstance(value, IndirectRef):
            return self._render_indirect_ref(value, path=path, source=source)
        if isinstance(value, IndirectExprRef):
            self._raise_issue(
                path=path,
                message="Indirect expression pointers are not supported in Click ladder export.",
                source=source,
            )
        if isinstance(value, IndirectBlockRange):
            self._raise_issue(
                path=path,
                message="Indirect block ranges are not supported in Click ladder export.",
                source=source,
            )
        if isinstance(value, CopyModifier):
            return self._render_copy_modifier(value, path=path, source=source)
        if isinstance(value, Expression):
            return self._render_expression(value, path=path, source=source)
        if isinstance(value, TimeUnit):
            return value.name
        if isinstance(value, str):
            return _quote(value)
        if isinstance(value, bool):
            return _bool_bit(value)
        if value is None:
            return "none"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, tuple | list):
            return self._render_sequence(value, path=path, source=source)
        self._raise_issue(
            path=path,
            message=f"Unsupported operand type: {type(value).__name__}.",
            source=source,
        )

    def _render_contact_token(self, value: Any, *, path: str, source: Any) -> str:
        return self._render_operand(
            value,
            path=path,
            source=source,
            allow_immediate=True,
            immediate_context="contact",
        )

    def _render_immediate_operand(
        self,
        immediate_ref: ImmediateRef,
        *,
        path: str,
        source: Any,
        context: str,
    ) -> str:
        wrapped = immediate_ref.value

        if context == "contact":
            tag = self._require_tag(
                wrapped,
                path=path,
                source=source,
                message="Immediate contact requires a Tag operand.",
            )
            return self._fn(
                "immediate",
                self._resolve_tag(tag, path=f"{path}.value", source=source),
            )

        if context == "coil":
            if isinstance(wrapped, Tag):
                address = self._resolve_tag(wrapped, path=f"{path}.value", source=source)
                parsed = _parse_display_address(address)
                if parsed is None or parsed[0] != "Y":
                    self._raise_issue(
                        path=path,
                        message="Immediate coil target must resolve to Y bank.",
                        source=source,
                    )
                return self._fn("immediate", address)

            if isinstance(wrapped, BlockRange):
                tags = wrapped.tags()
                addresses = [
                    self._resolve_tag(tag, path=f"{path}.value[{idx}]", source=source)
                    for idx, tag in enumerate(tags)
                ]
                if not addresses:
                    self._raise_issue(
                        path=path,
                        message="Immediate coil range cannot be empty.",
                        source=source,
                    )
                for address in addresses:
                    parsed = _parse_display_address(address)
                    if parsed is None or parsed[0] != "Y":
                        self._raise_issue(
                            path=path,
                            message="Immediate coil targets must resolve to Y bank.",
                            source=source,
                        )
                compact = _compact_contiguous_range(addresses)
                compact = self._require_compact_range(
                    compact,
                    path=path,
                    source=source,
                    message=(
                        "Immediate-wrapped coil ranges must map to contiguous "
                        "addresses for Click export."
                    ),
                )
                return self._fn("immediate", compact)

            self._raise_issue(
                path=path,
                message=(
                    "Immediate coil operand must wrap Tag or BlockRange, "
                    f"got {type(wrapped).__name__}."
                ),
                source=source,
            )

        self._raise_issue(
            path=path,
            message=f"Unknown immediate render context: {context!r}.",
            source=source,
        )

    def _render_copy_modifier(self, modifier: CopyModifier, *, path: str, source: Any) -> str:
        if modifier.mode == "text":
            return self._fn(
                "as_text",
                self._render_operand(modifier.source, path=f"{path}.source", source=source),
                _bool_bit(bool(modifier.suppress_zero)),
                "none" if modifier.pad is None else str(modifier.pad),
                _bool_bit(bool(modifier.exponential)),
                "none" if modifier.termination_code is None else str(modifier.termination_code),
            )
        return self._fn(
            f"as_{modifier.mode}",
            self._render_operand(modifier.source, path=f"{path}.source", source=source),
        )

    def _render_expression(self, expression: Expression, *, path: str, source: Any) -> str:
        if isinstance(expression, TagExpr):
            return self._resolve_tag(expression.tag, path=path, source=source)
        if isinstance(expression, LiteralExpr):
            if isinstance(expression.value, bool):
                return _bool_bit(expression.value)
            return repr(expression.value)
        if isinstance(expression, ShiftFuncExpr):
            return self._fn(
                expression.name,
                self._render_expression(expression.value, path=f"{path}.value", source=source),
                self._render_expression(expression.count, path=f"{path}.count", source=source),
            )
        if isinstance(expression, MathFuncExpr):
            return self._fn(
                expression.name,
                self._render_expression(expression.operand, path=f"{path}.operand", source=source),
            )
        if isinstance(expression, AbsExpr):
            return self._fn(
                "abs",
                self._render_expression(expression.operand, path=f"{path}.operand", source=source),
            )
        if isinstance(expression, (NegExpr, PosExpr, InvertExpr)):
            prefix = _UNARY_PREFIX[type(expression)]
            inner = self._render_expression(
                expression.operand, path=f"{path}.operand", source=source
            )
            if isinstance(expression.operand, _BINARY_EXPR_TYPES):
                return f"{prefix}({inner})"
            return f"{prefix}{inner}"
        if isinstance(
            expression,
            (
                AddExpr,
                SubExpr,
                MulExpr,
                DivExpr,
                FloorDivExpr,
                ModExpr,
                PowExpr,
                AndExpr,
                OrExpr,
                XorExpr,
                LShiftExpr,
                RShiftExpr,
            ),
        ):
            symbol = _BINARY_OP_SYMBOL[type(expression)]
            left = self._render_expression(expression.left, path=f"{path}.left", source=source)
            right = self._render_expression(expression.right, path=f"{path}.right", source=source)
            if isinstance(expression.left, _BINARY_EXPR_TYPES):
                left = f"({left})"
            if isinstance(expression.right, _BINARY_EXPR_TYPES):
                right = f"({right})"
            return f"{left}{symbol}{right}"
        self._raise_issue(
            path=path,
            message=f"Unsupported expression type: {type(expression).__name__}.",
            source=source,
        )

    def _render_condition_sequence(self, values: tuple[Any, ...], *, path: str, source: Any) -> str:
        rendered: list[str] = []
        for index, value in enumerate(values):
            rendered.append(
                self._render_condition_inline(value, path=f"{path}[{index}]", source=source)
            )
        return f"[{','.join(rendered)}]"

    def _render_condition_inline(self, value: Any, *, path: str, source: Any) -> str:
        if isinstance(value, AllCondition):
            return self._fn(
                "all",
                *(
                    self._render_condition_inline(c, path=path, source=source)
                    for c in value.conditions
                ),
            )
        if isinstance(value, AnyCondition):
            return self._fn(
                "any",
                *(
                    self._render_condition_inline(c, path=path, source=source)
                    for c in value.conditions
                ),
            )
        if isinstance(value, Condition):
            return self._condition_leaf_token(value, path=path)
        self._raise_issue(
            path=path,
            message=f"Expected condition, got {type(value).__name__}.",
            source=source,
        )

    def _render_pattern(self, pattern: tuple[tuple[bool, ...], ...]) -> str:
        rows: list[str] = []
        for row in pattern:
            rows.append(f"[{','.join(_bool_bit(cell) for cell in row)}]")
        return f"[{','.join(rows)}]"

    def _render_sequence(self, values: Any, *, path: str, source: Any) -> str:
        rendered: list[str] = []
        for index, value in enumerate(values):
            rendered.append(self._render_operand(value, path=f"{path}[{index}]", source=source))
        return f"[{','.join(rendered)}]"

    def _render_block_range(self, block_range: BlockRange, *, path: str, source: Any) -> str:
        tags = block_range.tags()
        addresses = [
            self._resolve_tag(tag, path=f"{path}[{index}]", source=source)
            for index, tag in enumerate(tags)
        ]
        if not addresses:
            return "[]"
        if len(addresses) == 1:
            return addresses[0]
        compact = _compact_contiguous_range(addresses)
        if compact is not None:
            return compact
        return f"[{','.join(addresses)}]"

    def _render_indirect_ref(self, indirect: IndirectRef, *, path: str, source: Any) -> str:
        entry = self._require_block_entry(indirect.block.name, path=path, source=source)

        try:
            offset = self._tag_map.offset_for(entry.logical)
        except Exception:
            self._raise_issue(
                path=path,
                message=(
                    f"Indirect block {indirect.block.name!r} must have an affine mapping "
                    "for Click ladder export."
                ),
                source=source,
            )

        sample_logical = entry.logical_addresses[0]
        hardware_addr = self._tag_map.resolve(entry.logical, sample_logical)
        parsed_hardware = _parse_display_address(hardware_addr)
        if not isinstance(parsed_hardware, tuple):
            self._raise_issue(
                path=path,
                message=f"Unable to parse hardware bank from {hardware_addr!r}.",
                source=source,
            )
        parsed_address = cast(tuple[str, int], parsed_hardware)
        bank, _ = parsed_address
        pointer = self._resolve_tag(indirect.pointer, path=f"{path}.pointer", source=source)
        if offset == 0:
            return f"{bank}[{pointer}]"
        sign = "+" if offset > 0 else "-"
        return f"{bank}[{pointer}{sign}{abs(offset)}]"

    def _require_block_entry(self, block_name: str, *, path: str, source: Any) -> _BlockEntry:
        entry: _BlockEntry | None = self._tag_map.block_entry_by_name(block_name)
        if entry is None:
            self._raise_issue(
                path=path,
                message=f"Indirect block {block_name!r} is not mapped in TagMap.",
                source=source,
            )
        return cast("_BlockEntry", entry)

    def _require_compact_range(
        self,
        compact: str | None,
        *,
        path: str,
        source: Any,
        message: str,
    ) -> str:
        if not isinstance(compact, str):
            self._raise_issue(path=path, message=message, source=source)
        return cast(str, compact)

    def _require_tag(self, value: Any, *, path: str, source: Any, message: str) -> Tag:
        if not isinstance(value, Tag):
            self._raise_issue(path=path, message=message, source=source)
        return value

    def _require_non_immediate_tag(
        self,
        value: Tag | ImmediateRef,
        *,
        path: str,
        source: Any,
        message: str,
    ) -> Tag:
        if isinstance(value, ImmediateRef):
            self._raise_issue(path=path, message=message, source=source)
        return self._require_tag(value, path=path, source=source, message=message)

    def _resolve_tag(self, tag: Tag, *, path: str, source: Any) -> str:
        try:
            return self._tag_map.resolve(tag)
        except Exception:
            self._raise_issue(
                path=path,
                message=f"Tag {tag.name!r} is not mapped in TagMap.",
                source=source,
            )

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

    def _ensure_subroutine_return_tail(
        self,
        rows: list[tuple[str, ...]],
        *,
        subroutine_name: str,
    ) -> list[tuple[str, ...]]:
        last_token: str | None = None
        for row in reversed(rows[1:]):  # Skip header.
            token = row[-1]
            if token != "":
                last_token = token
                break

        if last_token == "return()":
            return rows

        return_rows = self._single_output_rows(
            self._expand_conditions([], path=f"subroutine[{subroutine_name}].return"),
            output_token=self._fn("return"),
            first_marker="R",
        )
        rows.extend(return_rows)
        return rows

    def _fn(self, name: str, *args: str, **kwargs: str) -> str:
        parts = list(args)
        parts.extend(f"{k}={v}" for k, v in kwargs.items())
        if not parts:
            return f"{name}()"
        return f"{name}({','.join(parts)})"

    def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn:
        source_file = getattr(source, "source_file", None) if source is not None else None
        source_line = getattr(source, "source_line", None) if source is not None else None
        raise _RenderError(
            {
                "path": path,
                "message": message,
                "source_file": source_file,
                "source_line": source_line,
            }
        )


def _render_modbus_target(instruction: object) -> str:
    """Render a ModbusTarget(...) constructor for the ladder export."""
    name = getattr(instruction, "target_name", "")
    host = getattr(instruction, "host", None)
    if host is None:
        return _quote(name)
    port = getattr(instruction, "port", 502)
    device_id = getattr(instruction, "device_id", 1)
    parts = [_quote(name), _quote(host)]
    if port != 502 or device_id != 1:
        parts.append(str(port))
    if device_id != 1:
        parts.append(str(device_id))
    return f"ModbusTarget({','.join(parts)})"


def _quote(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _bool_bit(value: bool) -> str:
    return "1" if bool(value) else "0"


def _vertical_marker(*, index: int, total: int) -> str:
    if total <= 1:
        return "-"
    if index < total - 1:
        return "T"
    return "-"


def _compact_contiguous_range(addresses: list[str]) -> str | None:
    parsed = [_parse_display_address(value) for value in addresses]
    if any(item is None for item in parsed):
        return None

    assert all(item is not None for item in parsed)
    banks = {item[0] for item in parsed if item is not None}
    if len(banks) != 1:
        return None

    nums = [item[1] for item in parsed if item is not None]
    if any(nums[idx] + 1 != nums[idx + 1] for idx in range(len(nums) - 1)):
        return None

    return f"{addresses[0]}..{addresses[-1]}"


def _parse_display_address(value: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug if slug else "subroutine"
