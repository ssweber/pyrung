"""Formatting helpers for DAP stack frames and trace payloads."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pyrung.core.debug_trace import TraceEvent
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep


class DAPFormatter:
    """Build DAP-facing payloads from runner/trace models."""

    def build_current_stack_frames(
        self,
        *,
        current_step: ScanStep,
        rungs: list[Rung],
        subroutine_source_map: dict[str, tuple[str, int, int | None]],
        canonical_path: Callable[[str | None], str | None],
    ) -> list[dict[str, Any]]:
        step_name = f"Rung {current_step.rung_index}"
        if current_step.kind == "branch":
            step_name = f"Branch (rung {current_step.rung_index})"
        elif current_step.kind == "subroutine":
            sub_name = current_step.subroutine_name or "subroutine"
            step_name = f"{sub_name} (rung {current_step.rung_index})"
        elif current_step.kind == "instruction":
            kind_name = current_step.instruction_kind or "Instruction"
            if current_step.subroutine_name:
                step_name = (
                    f"{kind_name} ({current_step.subroutine_name}, rung {current_step.rung_index})"
                )
            else:
                step_name = f"{kind_name} (rung {current_step.rung_index})"

        frames: list[dict[str, Any]] = [
            self._stack_frame_from_step(
                frame_id=0,
                name=step_name,
                step=current_step,
            )
        ]

        next_frame_id = 1
        for depth, subroutine_name in enumerate(reversed(current_step.call_stack)):
            innermost = depth == 0 and current_step.subroutine_name == subroutine_name
            frames.append(
                self._stack_frame_from_subroutine(
                    frame_id=next_frame_id,
                    subroutine_name=subroutine_name,
                    current_step=current_step,
                    innermost=innermost,
                    subroutine_source_map=subroutine_source_map,
                    canonical_path=canonical_path,
                )
            )
            next_frame_id += 1

        if current_step.kind != "rung" and 0 <= current_step.rung_index < len(rungs):
            frames.append(
                self.stack_frame_from_rung(
                    frame_id=next_frame_id,
                    name=f"Rung {current_step.rung_index}",
                    rung=rungs[current_step.rung_index],
                )
            )

        return frames

    def stack_frame_from_rung(
        self,
        *,
        frame_id: int,
        name: str,
        rung: Rung,
    ) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "id": frame_id,
            "name": name,
            "line": int(rung.source_line or 1),
            "column": 1,
        }
        if rung.end_line is not None:
            frame["endLine"] = int(rung.end_line)
        if rung.source_file:
            source_path = str(Path(rung.source_file))
            frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def current_trace_body(
        self,
        *,
        event_result: tuple[int, int, Any] | None,
        current_scan_id: int,
        trace_version: int,
        canonical_path: Callable[[str | None], str | None],
        format_value: Callable[[Any], str],
    ) -> dict[str, Any] | None:
        if event_result is None:
            return None

        scan_id, rung_index, event = event_result
        trace_source = "live" if scan_id > current_scan_id else "inspect"
        trace = event.trace if isinstance(event.trace, TraceEvent) else None
        step_kind: str | None = event.kind
        instruction_kind: str | None = event.instruction_kind
        enabled_state: str | None = event.enabled_state
        subroutine_name: str | None = event.subroutine_name
        call_stack: list[str] = list(event.call_stack)
        source_line: int | None = event.source_line
        end_line: int | None = event.end_line if event.end_line is not None else event.source_line
        step_source_file = event.source_file

        regions = self._regions_from_trace_event(
            trace,
            canonical_path=canonical_path,
            format_value=format_value,
        )
        step_source = None
        step_source_path = canonical_path(step_source_file)
        if step_source_path:
            step_source = {"name": Path(step_source_path).name, "path": step_source_path}

        display_status = self._step_display_status_from_fields(enabled_state=enabled_state)
        display_text = self._step_display_text_from_fields(
            kind=step_kind,
            instruction_kind=instruction_kind,
            display_status=display_status,
        )

        return {
            "traceVersion": trace_version,
            "traceSource": trace_source,
            "scanId": scan_id,
            "rungId": rung_index,
            "step": {
                "kind": step_kind,
                "instructionKind": instruction_kind,
                "enabledState": enabled_state,
                "displayStatus": display_status,
                "displayText": display_text,
                "source": step_source,
                "line": source_line,
                "endLine": end_line if end_line is not None else source_line,
                "subroutineName": subroutine_name,
                "callStack": call_stack,
                "rungIndex": rung_index,
            },
            "regions": regions,
        }

    def _stack_frame_from_step(
        self,
        *,
        frame_id: int,
        name: str,
        step: ScanStep,
    ) -> dict[str, Any]:
        source_line = int(step.source_line or step.rung.source_line or 1)
        frame: dict[str, Any] = {
            "id": frame_id,
            "name": name,
            "line": source_line,
            "column": 1,
        }
        end_line = step.end_line or step.source_line
        if end_line is not None:
            frame["endLine"] = int(end_line)
        source_file = step.source_file or step.rung.source_file
        if source_file:
            source_path = str(Path(source_file))
            frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def _stack_frame_from_subroutine(
        self,
        *,
        frame_id: int,
        subroutine_name: str,
        current_step: ScanStep,
        innermost: bool,
        subroutine_source_map: dict[str, tuple[str, int, int | None]],
        canonical_path: Callable[[str | None], str | None],
    ) -> dict[str, Any]:
        source_location: tuple[str, int, int | None] | None = None
        if innermost:
            source_location = self._subroutine_source_from_step_rung(
                current_step,
                canonical_path=canonical_path,
            )
        if source_location is None:
            source_location = subroutine_source_map.get(subroutine_name)

        frame: dict[str, Any] = {
            "id": frame_id,
            "name": f"Subroutine {subroutine_name}",
            "line": 1,
            "column": 1,
        }
        if source_location is None:
            return frame

        source_path, source_line, end_line = source_location
        frame["line"] = int(source_line)
        if end_line is not None:
            frame["endLine"] = int(end_line)
        frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def _subroutine_source_from_step_rung(
        self,
        step: ScanStep,
        *,
        canonical_path: Callable[[str | None], str | None],
    ) -> tuple[str, int, int | None] | None:
        source_path = canonical_path(step.rung.source_file)
        if source_path is None or step.rung.source_line is None:
            return None
        end_line = int(step.rung.end_line) if step.rung.end_line is not None else None
        return source_path, int(step.rung.source_line), end_line

    def _regions_from_trace_event(
        self,
        trace: TraceEvent | None,
        *,
        canonical_path: Callable[[str | None], str | None],
        format_value: Callable[[Any], str],
    ) -> list[dict[str, Any]]:
        regions: list[dict[str, Any]] = []
        if not isinstance(trace, TraceEvent):
            return regions

        for region in trace.regions:
            source_body = None
            source_path = (
                canonical_path(region.source.source_file)
                if isinstance(region.source.source_file, str)
                else None
            )
            if source_path:
                source_body = {"name": Path(source_path).name, "path": source_path}

            conditions: list[dict[str, Any]] = []
            for cond in region.conditions:
                cond_source = None
                cond_path = (
                    canonical_path(cond.source_file)
                    if isinstance(cond.source_file, str)
                    else None
                )
                if cond_path:
                    cond_source = {"name": Path(cond_path).name, "path": cond_path}
                details = [
                    {
                        "name": str(detail.get("name", "")),
                        "value": format_value(detail.get("value")),
                    }
                    for detail in cond.details
                    if isinstance(detail, dict)
                ]
                conditions.append(
                    {
                        "source": cond_source,
                        "line": cond.source_line,
                        "expression": cond.expression,
                        "status": cond.status,
                        "value": cond.value,
                        "details": details,
                        "summary": cond.summary,
                        "annotation": cond.annotation,
                    }
                )

            regions.append(
                {
                    "kind": region.kind,
                    "enabledState": region.enabled_state,
                    "source": source_body,
                    "line": region.source.source_line,
                    "endLine": region.source.end_line,
                    "conditions": conditions,
                }
            )

        return regions

    def _step_display_status_from_fields(self, *, enabled_state: str | None) -> str:
        if enabled_state == "enabled":
            return "enabled"
        if enabled_state == "disabled_parent":
            return "skipped"
        return "disabled"

    def _step_display_text_from_fields(
        self,
        *,
        kind: str | None,
        instruction_kind: str | None,
        display_status: str,
    ) -> str:
        if display_status == "enabled":
            prefix = "[RUN]" if kind == "instruction" else "[ON]"
        elif display_status == "skipped":
            prefix = "[SKIP]"
        else:
            prefix = "[OFF]"

        if kind == "instruction":
            label = instruction_kind or "Instruction"
        elif kind == "branch":
            label = "Branch"
        elif kind == "subroutine":
            label = "Subroutine"
        else:
            label = "Rung"
        return f"{prefix} {label}"
