"""Strict prevalidation helpers for Click ladder export."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.rung import Rung

from .types import Issue, LadderExportError

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.program import Program


# ---- Strict prevalidation mixin ----
class _ValidationMixin:
    """Run strict checks before lowering ladder logic into Click CSV rows."""

    _tag_map: TagMap
    _program: Program

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...

    def _run_precheck(self) -> None:
        report = self._tag_map.validate(self._program, mode="strict")
        # We intentionally fail on warnings and hints to keep export deterministic.
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

        # Click ladder v1 does not support nested call instructions.
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


__all__ = ["_ValidationMixin"]
