"""Click ladder CSV export helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.rung import Rung

from .instructions import _InstructionMixin
from .layout import _HEADER, _LayoutMixin
from .translator import _TranslatorMixin
from .types import LadderBundle, LadderExportError, _RenderError
from .validator import _ValidationMixin

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


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

    def __init__(self, *, tag_map: TagMap, program: Program) -> None:
        self._tag_map = tag_map
        self._program = program

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

            return LadderBundle(main_rows=tuple(main_rows), subroutine_rows=tuple(subroutine_rows))
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

    def _end_rung(self) -> list[tuple[str, ...]]:
        """Emit an unconditional ``end()`` rung for the main program tail."""
        return self._single_output_rows(
            self._expand_conditions([], path="main.end"),
            output_token=self._fn("end"),
            first_marker="R",
        )

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


__all__ = ["LadderBundle", "LadderExportError", "build_ladder_bundle"]
