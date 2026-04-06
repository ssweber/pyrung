"""Click ladder CSV export helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.condition import AllCondition, AnyCondition, Condition
from pyrung.core.rung import Rung

from .instructions import _InstructionMixin
from .layout import _HEADER, _LayoutMixin
from .translator import _TranslatorMixin
from .types import ExportSummary, LadderBundle, LadderExportError, _RenderError
from .validator import _ValidationMixin

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


@dataclass(frozen=True)
class _InstructionPath:
    """One emitted instruction paired with its full condition path."""

    conditions: tuple[Condition, ...]
    instruction: Any

    def strip_prefix(self, count: int) -> _InstructionPath:
        return _InstructionPath(self.conditions[count:], self.instruction)


# ---- Public entrypoint ----
def build_ladder_bundle(tag_map: TagMap, program: Program) -> LadderBundle:
    """Render a `Program` into deterministic Click ladder CSV row matrices."""
    return _LadderExporter(tag_map=tag_map, program=program).export()


# ---- Orchestrator ----
class _LadderExporter(
    _ValidationMixin,
    _LayoutMixin,
    _InstructionMixin,
    _TranslatorMixin,
):
    """Facade that orchestrates validation, layout, and token rendering."""

    # DSL name → CSV token name (only entries where names differ)
    _RENAME_TABLE: tuple[tuple[str, str, str], ...] = (
        # (instruction_class_name, dsl_name, csv_name)
        ("CalcInstruction", "calc", "math"),
        ("ReturnInstruction", "return_early", "return"),
        ("ForLoopInstruction", "forloop", "for"),
    )

    def __init__(self, *, tag_map: TagMap, program: Program) -> None:
        self._tag_map = tag_map
        self._program = program
        self._forloop_count = 0
        self._added_return_count = 0

    def export(self) -> LadderBundle:
        try:
            self._run_precheck()

            # Main scope always ends with an explicit end() rung.
            main_rows: list[tuple[str, ...]] = [tuple(_HEADER)]
            main_rows.extend(
                self._render_scope(self._program.rungs, scope="main", subroutine_name=None)
            )
            main_rows.extend(self._end_rung())

            # Each subroutine matrix gets a deterministic return() tail.
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

            summary = self._build_summary()
            return LadderBundle(
                main_rows=tuple(main_rows),
                subroutine_rows=tuple(subroutine_rows),
                export_summary=summary,
            )
        except _RenderError as exc:
            raise LadderExportError([exc.issue]) from None

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
        """Expand rung comments into '#' passthrough rows."""
        if rung.comment is None:
            return []
        return [("#", line) for line in rung.comment.splitlines()]

    def _normalize_branching_rung(self, rung: Rung) -> Rung:
        """Normalize a branching rung into an ordered shared-prefix tree."""
        paths = self._collect_instruction_paths(rung)
        if not paths:
            return rung
        normalized = self._build_normalized_rung(paths)
        normalized._use_prior_snapshot = rung._use_prior_snapshot
        normalized.comment = rung.comment
        return normalized

    def _collect_instruction_paths(
        self,
        rung: Rung,
        *,
        prefix: tuple[Condition, ...] = (),
    ) -> list[_InstructionPath]:
        local_conditions = tuple(rung._conditions[rung._branch_condition_start :])
        current_prefix = prefix + local_conditions

        paths: list[_InstructionPath] = []
        for item in rung._execution_items:
            if isinstance(item, Rung):
                paths.extend(self._collect_instruction_paths(item, prefix=current_prefix))
            else:
                paths.append(_InstructionPath(current_prefix, item))
        return paths

    def _build_normalized_rung(self, paths: list[_InstructionPath]) -> Rung:
        common_prefix_len = self._common_prefix_len(paths)
        normalized = Rung(*paths[0].conditions[:common_prefix_len])
        remaining = [path.strip_prefix(common_prefix_len) for path in paths]

        index = 0
        while index < len(remaining):
            current = remaining[index]
            if not current.conditions:
                normalized.add_instruction(current.instruction)
                index += 1
                continue

            segment_key = self._condition_signature(current.conditions[0])
            stop = index + 1
            while stop < len(remaining):
                candidate = remaining[stop]
                if not candidate.conditions:
                    break
                if self._condition_signature(candidate.conditions[0]) != segment_key:
                    break
                stop += 1

            normalized.add_branch(self._build_normalized_rung(remaining[index:stop]))
            index = stop

        return normalized

    def _common_prefix_len(self, paths: list[_InstructionPath]) -> int:
        if not paths:
            return 0

        max_len = min(len(path.conditions) for path in paths)
        prefix_len = 0
        for index in range(max_len):
            first = self._condition_signature(paths[0].conditions[index])
            if any(
                self._condition_signature(path.conditions[index]) != first for path in paths[1:]
            ):
                break
            prefix_len += 1
        return prefix_len

    def _condition_signature(self, condition: Condition) -> str:
        if isinstance(condition, AllCondition):
            parts = ",".join(self._condition_signature(child) for child in condition.conditions)
            return f"AND({parts})"
        if isinstance(condition, AnyCondition):
            parts = ",".join(self._condition_signature(child) for child in condition.conditions)
            return f"OR({parts})"
        return self._condition_leaf_token(condition, path="normalize.condition")

    def _render_rung(self, rung: Rung, *, path: str) -> list[tuple[str, ...]]:
        comment_rows = self._comment_rows(rung)
        first_marker = "" if rung._use_prior_snapshot else "R"

        if not rung._instructions and not rung._branches:
            # Empty rung (comment-only or bare pass) → emit NOP in AF column.
            condition_rows = self._expand_conditions(rung._conditions, path=f"{path}.condition")
            output_rows = self._single_output_rows(
                condition_rows,
                output_token="NOP",
                first_marker=first_marker,
            )
            self._assert_rung_height(rows=output_rows, path=path, source=rung)
            return comment_rows + output_rows

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
            self._forloop_count += 1
            output_rows = self._render_forloop_instruction(
                instruction=rung._instructions[0],
                conditions=rung._conditions,
                path=f"{path}.instruction[0](ForLoopInstruction)",
            )
            self._assert_rung_height(rows=output_rows, path=path, source=rung)
            return comment_rows + output_rows

        condition_rows = self._expand_conditions(rung._conditions, path=f"{path}.condition")

        if rung._branches:
            normalized_rung = self._normalize_branching_rung(rung)
            branch_rows = self._render_rung_with_branches(
                normalized_rung,
                path=path,
                first_marker=first_marker,
            )
            pin_rows = self._branch_pin_rows(normalized_rung, path=path)
            if not branch_rows:
                return []
            rows = branch_rows + pin_rows
            self._assert_rung_height(rows=rows, path=path, source=rung)
            return comment_rows + rows

        output_rows: list[tuple[str, ...]]
        pin_rows: list[tuple[str, ...]]
        if rung._instructions:
            output_rows, pin_rows = self._render_instruction_rows(
                instructions=rung._instructions,
                condition_rows=condition_rows,
                path=path,
                first_marker=first_marker,
                source=rung,
            )
        else:
            output_rows = self._single_output_rows(
                condition_rows,
                output_token="",
                first_marker=first_marker,
            )
            pin_rows = []

        rows = comment_rows + output_rows
        rows.extend(pin_rows)
        self._assert_rung_height(rows=output_rows + pin_rows, path=path, source=rung)
        return rows

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

        self._added_return_count += 1
        return_rows = self._single_output_rows(
            self._expand_conditions([], path=f"subroutine[{subroutine_name}].return"),
            output_token=self._fn("return"),
            first_marker="R",
        )
        rows.extend(return_rows)
        return rows

    def _end_rung(self) -> list[tuple[str, ...]]:
        """Emit an unconditional ``end()`` rung for the main program tail."""
        return self._single_output_rows(
            self._expand_conditions([], path="main.end"),
            output_token=self._fn("end"),
            first_marker="R",
        )

    def _build_summary(self) -> ExportSummary:
        used_types = self._collect_instruction_types(self._program)
        renames: list[tuple[str, str]] = []
        for class_name, dsl_name, csv_name in self._RENAME_TABLE:
            if class_name in used_types:
                renames.append((dsl_name, csv_name))
        return ExportSummary(
            renames=tuple(renames),
            added_next=self._forloop_count,
            added_return=self._added_return_count,
            added_end=True,
        )

    @staticmethod
    def _collect_instruction_types(program: Program) -> set[str]:
        """Collect all instruction class names used across the program."""
        types: set[str] = set()
        all_rungs = list(program.rungs)
        for sub_rungs in program.subroutines.values():
            all_rungs.extend(sub_rungs)
        for rung in all_rungs:
            for instr in rung._instructions:
                types.add(type(instr).__name__)
                # ForLoopInstruction has nested instructions
                for child in getattr(instr, "instructions", ()):
                    types.add(type(child).__name__)
            for branch_block in rung._branches:
                for instr in branch_block._instructions:
                    types.add(type(instr).__name__)
        return types

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

    def _assert_rung_height(
        self,
        *,
        rows: list[tuple[str, ...]],
        path: str,
        source: Any,
    ) -> None:
        if len(rows) <= 32:
            return
        self._raise_issue(
            path=path,
            message="Rendered rung exceeds Click's 32-row limit.",
            source=source,
        )


__all__ = ["LadderBundle", "LadderExportError", "build_ladder_bundle"]
