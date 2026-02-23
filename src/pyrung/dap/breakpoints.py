"""Breakpoint indexing and hit-processing for the DAP adapter."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from pyrung.core import PLCRunner
from pyrung.core.instruction import CallInstruction, Instruction
from pyrung.core.rung import Rung
from pyrung.core.state import SystemState


@dataclass
class SourceBreakpoint:
    line: int
    enabled: bool = True
    condition_source: str | None = None
    condition: Callable[[SystemState], bool] | None = None
    hit_condition: int | None = None
    hit_count: int = 0
    log_message: str | None = None
    snapshot_label: str | None = None
    last_scan_id: int | None = None


class BreakpointManager:
    """Owns source-line breakpoint state, indexing, and hit logic."""

    def __init__(self) -> None:
        self.source_breakpoints_by_file: dict[str, dict[int, SourceBreakpoint]] = {}
        self.breakpoint_rung_map: dict[str, set[int]] = {}
        self.subroutine_source_map: dict[str, tuple[str, int, int | None]] = {}

    def clear(self) -> None:
        self.source_breakpoints_by_file.clear()
        self.breakpoint_rung_map.clear()
        self.subroutine_source_map.clear()

    def clear_source_breakpoints(self) -> None:
        self.source_breakpoints_by_file.clear()

    def canonical_path(self, path: str | None) -> str | None:
        if path is None or path.startswith("<"):
            return None
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def valid_lines(self, source_path: str) -> set[int]:
        return self.breakpoint_rung_map.get(source_path, set())

    def source_breakpoints(self, source_path: str) -> dict[int, SourceBreakpoint]:
        return self.source_breakpoints_by_file.get(source_path, {})

    def subroutine_sources(self) -> dict[str, tuple[str, int, int | None]]:
        """Return a detached view of indexed subroutine source locations."""
        return dict(self.subroutine_source_map)

    def set_source_breakpoints(
        self, source_path: str, breakpoints: dict[int, SourceBreakpoint]
    ) -> None:
        self.source_breakpoints_by_file[source_path] = breakpoints

    def rebuild_index(self, runner: PLCRunner) -> None:
        self.breakpoint_rung_map = {}
        self.subroutine_source_map = {}
        visited_rungs: set[int] = set()
        visited_programs: set[int] = set()
        for rung in runner.iter_top_level_rungs():
            self._index_rung_lines(
                rung=rung, visited_rungs=visited_rungs, visited_programs=visited_programs
            )

    def current_rung_hits_breakpoint(
        self,
        *,
        current_rung: Rung | None,
        current_scan_id: int | None,
        runner: PLCRunner | None,
        on_logpoint_hit: Callable[[SourceBreakpoint, SystemState, int | None], None],
    ) -> bool:
        if current_rung is None:
            return False
        source = self.canonical_path(current_rung.source_file)
        if source is None:
            return False
        file_breakpoints = self.source_breakpoints_by_file.get(source)
        if not file_breakpoints:
            return False
        if current_rung.source_line is None:
            return False
        if runner is None:
            return False

        start_line = int(current_rung.source_line)
        end_line = int(current_rung.end_line or current_rung.source_line)
        if end_line < start_line:
            start_line, end_line = end_line, start_line

        for line, breakpoint in file_breakpoints.items():
            if not breakpoint.enabled:
                continue
            if not (start_line <= line <= end_line):
                continue
            if current_scan_id is not None and breakpoint.last_scan_id == current_scan_id:
                continue
            breakpoint.last_scan_id = current_scan_id

            if breakpoint.condition is not None and not breakpoint.condition(runner.current_state):
                continue

            if not self._source_breakpoint_hit_matches(breakpoint):
                continue

            if breakpoint.log_message is not None:
                on_logpoint_hit(breakpoint, runner.current_state, current_scan_id)
                continue

            return True

        return False

    def process_logpoints_for_current_rung(
        self,
        *,
        current_rung: Rung | None,
        current_scan_id: int | None,
        runner: PLCRunner | None,
        on_logpoint_hit: Callable[[SourceBreakpoint, SystemState, int | None], None],
    ) -> None:
        if current_rung is None:
            return
        source = self.canonical_path(current_rung.source_file)
        if source is None:
            return
        file_breakpoints = self.source_breakpoints_by_file.get(source)
        if not file_breakpoints:
            return
        if current_rung.source_line is None:
            return
        if runner is None:
            return

        start_line = int(current_rung.source_line)
        end_line = int(current_rung.end_line or current_rung.source_line)
        if end_line < start_line:
            start_line, end_line = end_line, start_line

        for line, breakpoint in file_breakpoints.items():
            if breakpoint.log_message is None:
                continue
            if not breakpoint.enabled:
                continue
            if not (start_line <= line <= end_line):
                continue
            if current_scan_id is not None and breakpoint.last_scan_id == current_scan_id:
                continue
            breakpoint.last_scan_id = current_scan_id

            if breakpoint.condition is not None and not breakpoint.condition(runner.current_state):
                continue

            if not self._source_breakpoint_hit_matches(breakpoint):
                continue

            on_logpoint_hit(breakpoint, runner.current_state, current_scan_id)

    def _source_breakpoint_hit_matches(self, breakpoint: SourceBreakpoint) -> bool:
        hit_condition = breakpoint.hit_condition
        if hit_condition is None:
            return True
        breakpoint.hit_count += 1
        if breakpoint.hit_count != hit_condition:
            return False
        breakpoint.hit_count = 0
        return True

    def _index_rung_lines(
        self,
        *,
        rung: Rung,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        rung_id = id(rung)
        if rung_id in visited_rungs:
            return
        visited_rungs.add(rung_id)

        self._index_rung_range(
            source_file=rung.source_file,
            source_line=rung.source_line,
            end_line=rung.end_line,
        )
        for instruction in rung._instructions:
            self._index_instruction_lines(
                instruction=instruction,
                fallback_source_file=rung.source_file,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )
        for branch in rung._branches:
            self._index_rung_lines(
                rung=branch,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )

    def _index_instruction_lines(
        self,
        *,
        instruction: Instruction,
        fallback_source_file: str | None,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        source_file = getattr(instruction, "source_file", None) or fallback_source_file
        source_line = getattr(instruction, "source_line", None)
        self._index_line(source_file, source_line)
        debug_substeps = getattr(instruction, "debug_substeps", None)
        if debug_substeps:
            for substep in debug_substeps:
                self._index_line(
                    getattr(substep, "source_file", None) or source_file,
                    getattr(substep, "source_line", None),
                )

        if isinstance(instruction, CallInstruction):
            self._index_subroutine_lines_for_call(
                instruction=instruction,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )

        nested = getattr(instruction, "instructions", None)
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, Instruction):
                    self._index_instruction_lines(
                        instruction=child,
                        fallback_source_file=source_file,
                        visited_rungs=visited_rungs,
                        visited_programs=visited_programs,
                    )

    def _index_subroutine_lines_for_call(
        self,
        *,
        instruction: CallInstruction,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        program = getattr(instruction, "_program", None)
        if program is None:
            return
        program_id = id(program)
        if program_id in visited_programs:
            return
        visited_programs.add(program_id)
        for subroutine_name, subroutine_rungs in program.subroutines.items():
            self._index_subroutine_source(subroutine_name=subroutine_name, rungs=subroutine_rungs)
            for rung in subroutine_rungs:
                self._index_rung_lines(
                    rung=rung,
                    visited_rungs=visited_rungs,
                    visited_programs=visited_programs,
                )

    def _index_subroutine_source(self, *, subroutine_name: str, rungs: list[Rung]) -> None:
        if subroutine_name in self.subroutine_source_map:
            return
        for rung in rungs:
            source_path = self.canonical_path(rung.source_file)
            if source_path is None or rung.source_line is None:
                continue
            end_line = int(rung.end_line) if rung.end_line is not None else None
            self.subroutine_source_map[subroutine_name] = (
                source_path,
                int(rung.source_line),
                end_line,
            )
            return

    def _index_rung_range(
        self,
        *,
        source_file: str | None,
        source_line: int | None,
        end_line: int | None,
    ) -> None:
        if source_line is None:
            return
        start_line = int(source_line)
        final_line = int(end_line) if end_line is not None else start_line
        if final_line < start_line:
            start_line, final_line = final_line, start_line
        for line in range(start_line, final_line + 1):
            self._index_line(source_file, line)

    def _index_line(self, source_file: str | None, source_line: int | None) -> None:
        canonical = self.canonical_path(source_file)
        if canonical is None or source_line is None:
            return
        lines = self.breakpoint_rung_map.setdefault(canonical, set())
        lines.add(int(source_line))
