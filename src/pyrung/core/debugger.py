"""Debugger stepping engine for PLCRunner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Generator

    from pyrung.core.context import ScanContext
    from pyrung.core.instruction import Instruction
    from pyrung.core.rung import Rung


class PLCDebugger:
    """Produces debug scan steps and trace payloads for a runner."""

    def __init__(self, *, step_factory: type[Any]) -> None:
        self._step_factory = step_factory

    def scan_steps_debug(self, runner: Any) -> Generator[Any, None, None]:
        """Execute one scan cycle and yield debug steps."""
        ctx, dt = runner._prepare_scan()

        for i, rung in enumerate(runner._logic):
            enabled, rung_condition_traces = self._evaluate_conditions_with_trace(
                runner, rung._conditions, ctx
            )
            yield from self._iter_rung_steps(
                runner=runner,
                rung_index=i,
                rung=rung,
                ctx=ctx,
                kind="rung",
                depth=0,
                subroutine_name=None,
                call_stack=(),
                enabled=enabled,
                parent_enabled=True,
                rung_condition_traces=rung_condition_traces,
            )

        runner._commit_scan(ctx, dt)

    def _iter_rung_steps(
        self,
        *,
        runner: Any,
        rung_index: int,
        rung: Rung,
        ctx: ScanContext,
        kind: Literal["rung", "branch", "subroutine"],
        depth: int,
        subroutine_name: str | None,
        call_stack: tuple[str, ...],
        enabled: bool,
        parent_enabled: bool,
        rung_condition_traces: list[dict[str, Any]],
    ) -> Generator[Any, None, None]:
        from pyrung.core.instruction import SubroutineReturnSignal
        from pyrung.core.rung import Rung as RungClass

        enabled_state = self._enabled_state_for(
            kind=kind, enabled=enabled, parent_enabled=parent_enabled
        )
        branch_enable_map: dict[int, bool] = {}
        branch_trace_map: dict[int, dict[str, Any]] = {}
        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            local_conditions = item._conditions[item._branch_condition_start :]
            if enabled:
                local_enabled, local_traces = self._evaluate_conditions_with_trace(
                    runner, local_conditions, ctx
                )
                branch_state: Literal["enabled", "disabled_local", "disabled_parent"]
                branch_state = "enabled" if local_enabled else "disabled_local"
            else:
                local_enabled = False
                local_traces = [
                    self._skipped_condition_trace(runner, cond) for cond in local_conditions
                ]
                branch_state = "disabled_parent"
            branch_enable_map[id(item)] = local_enabled
            branch_trace_map[id(item)] = {"enabled_state": branch_state, "conditions": local_traces}

        step_trace = self._build_step_trace(
            kind=kind,
            rung=rung,
            enabled_state=enabled_state,
            rung_condition_traces=rung_condition_traces,
            branch_trace_map=branch_trace_map,
        )
        try:
            for item in rung._execution_items:
                if isinstance(item, RungClass):
                    branch_enabled = branch_enable_map.get(id(item), False)
                    branch_trace = branch_trace_map.get(id(item), {})
                    yield from self._iter_rung_steps(
                        runner=runner,
                        rung_index=rung_index,
                        rung=item,
                        ctx=ctx,
                        kind="branch",
                        depth=depth + 1,
                        subroutine_name=subroutine_name,
                        call_stack=call_stack,
                        enabled=branch_enabled,
                        parent_enabled=enabled,
                        rung_condition_traces=list(branch_trace.get("conditions", [])),
                    )
                else:
                    yield from self._iter_instruction_steps(
                        runner=runner,
                        rung_index=rung_index,
                        rung=rung,
                        kind=kind,
                        subroutine_name=subroutine_name,
                        instruction=item,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                        enabled=enabled,
                        enabled_state=enabled_state,
                        step_trace=step_trace,
                    )
        except SubroutineReturnSignal:
            if kind != "branch":
                yield self._step_factory(
                    rung_index=rung_index,
                    rung=rung,
                    ctx=ctx,
                    kind=kind,
                    subroutine_name=subroutine_name,
                    depth=depth,
                    call_stack=call_stack,
                    source_file=rung.source_file,
                    source_line=rung.source_line,
                    end_line=rung.end_line,
                    enabled_state=enabled_state,
                    trace=step_trace,
                    instruction_kind=None,
                )
            raise

        if kind != "branch" or enabled:
            yield self._step_factory(
                rung_index=rung_index,
                rung=rung,
                ctx=ctx,
                kind=kind,
                subroutine_name=subroutine_name,
                depth=depth,
                call_stack=call_stack,
                source_file=rung.source_file,
                source_line=rung.source_line,
                end_line=rung.end_line,
                enabled_state=enabled_state,
                trace=step_trace,
                instruction_kind=None,
            )

    def _iter_instruction_steps(
        self,
        *,
        runner: Any,
        rung_index: int,
        rung: Rung,
        kind: Literal["rung", "branch", "subroutine"],
        subroutine_name: str | None,
        instruction: Instruction,
        ctx: ScanContext,
        depth: int,
        call_stack: tuple[str, ...],
        enabled: bool,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        step_trace: dict[str, Any],
    ) -> Generator[Any, None, None]:
        from pyrung.core.instruction import (
            CallInstruction,
            ForLoopInstruction,
            SubroutineReturnSignal,
            resolve_tag_or_value_ctx,
        )

        if isinstance(instruction, CallInstruction):
            if not enabled:
                instruction.execute(ctx, enabled)
                return
            if instruction.subroutine_name not in instruction._program.subroutines:
                raise KeyError(f"Subroutine '{instruction.subroutine_name}' not defined")
            yield self._step_factory(
                rung_index=rung_index,
                rung=rung,
                ctx=ctx,
                kind="instruction",
                subroutine_name=subroutine_name,
                depth=depth,
                call_stack=call_stack,
                source_file=getattr(instruction, "source_file", None) or rung.source_file,
                source_line=getattr(instruction, "source_line", None) or rung.source_line,
                end_line=(
                    getattr(instruction, "end_line", None)
                    or getattr(instruction, "source_line", None)
                    or rung.source_line
                ),
                enabled_state=enabled_state,
                trace=step_trace,
                instruction_kind=instruction.__class__.__name__,
            )
            next_stack = (*call_stack, instruction.subroutine_name)
            try:
                for sub_rung in instruction._program.subroutines[instruction.subroutine_name]:
                    sub_enabled, sub_condition_traces = self._evaluate_conditions_with_trace(
                        runner, sub_rung._conditions, ctx
                    )
                    yield from self._iter_rung_steps(
                        runner=runner,
                        rung_index=rung_index,
                        rung=sub_rung,
                        ctx=ctx,
                        kind="subroutine",
                        depth=depth + 1,
                        subroutine_name=instruction.subroutine_name,
                        call_stack=next_stack,
                        enabled=sub_enabled,
                        parent_enabled=True,
                        rung_condition_traces=sub_condition_traces,
                    )
            except SubroutineReturnSignal:
                return
            return

        if isinstance(instruction, ForLoopInstruction):
            if not enabled:
                instruction.execute(ctx, enabled)
                return

            if not instruction.should_execute(enabled):
                return

            count_value = resolve_tag_or_value_ctx(instruction.count, ctx)
            iterations = max(0, int(count_value))

            for i in range(iterations):
                ctx.set_tag(instruction.idx_tag.name, i)
                for child in instruction.instructions:
                    yield from self._iter_instruction_steps(
                        runner=runner,
                        rung_index=rung_index,
                        rung=rung,
                        kind=kind,
                        subroutine_name=subroutine_name,
                        instruction=child,
                        ctx=ctx,
                        depth=depth,
                        call_stack=call_stack,
                        enabled=True,
                        enabled_state="enabled",
                        step_trace=step_trace,
                    )
            return

        if not enabled and instruction.is_inert_when_disabled():
            instruction.execute(ctx, enabled)
            return

        instruction_source_file = getattr(instruction, "source_file", None) or rung.source_file
        instruction_source_line = getattr(instruction, "source_line", None) or rung.source_line
        instruction_end_line = (
            getattr(instruction, "end_line", None)
            or getattr(instruction, "source_line", None)
            or rung.source_line
        )
        debug_substeps = getattr(instruction, "debug_substeps", None)
        if debug_substeps:
            for substep in debug_substeps:
                substep_source_file = substep.source_file or instruction_source_file
                substep_source_line = substep.source_line or instruction_source_line
                substep_condition_trace = self._instruction_substep_condition_trace(
                    runner=runner,
                    substep=substep,
                    ctx=ctx,
                    enabled=enabled,
                    enabled_state=enabled_state,
                    source_file=substep_source_file,
                    source_line=substep_source_line,
                )
                substep_trace = self._instruction_substep_trace(
                    source_file=substep_source_file,
                    source_line=substep_source_line,
                    enabled_state=enabled_state,
                    condition_trace=substep_condition_trace,
                )
                yield self._step_factory(
                    rung_index=rung_index,
                    rung=rung,
                    ctx=ctx,
                    kind="instruction",
                    subroutine_name=subroutine_name,
                    depth=depth,
                    call_stack=call_stack,
                    source_file=substep_source_file,
                    source_line=substep_source_line,
                    end_line=(
                        substep_source_line
                        if substep_source_line is not None
                        else instruction_end_line
                    ),
                    enabled_state=enabled_state,
                    trace=substep_trace,
                    instruction_kind=substep.instruction_kind,
                )
            instruction.execute(ctx, enabled)
            return

        yield self._step_factory(
            rung_index=rung_index,
            rung=rung,
            ctx=ctx,
            kind="instruction",
            subroutine_name=subroutine_name,
            depth=depth,
            call_stack=call_stack,
            source_file=instruction_source_file,
            source_line=instruction_source_line,
            end_line=instruction_end_line,
            enabled_state=enabled_state,
            trace=step_trace,
            instruction_kind=instruction.__class__.__name__,
        )
        instruction.execute(ctx, enabled)

    def _instruction_substep_trace(
        self,
        *,
        source_file: str | None,
        source_line: int | None,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        condition_trace: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "regions": [
                {
                    "kind": "instruction",
                    "source_file": source_file,
                    "source_line": source_line,
                    "end_line": source_line,
                    "enabled_state": enabled_state,
                    "conditions": [condition_trace],
                }
            ]
        }

    def _instruction_substep_condition_trace(
        self,
        *,
        runner: Any,
        substep: Any,
        ctx: ScanContext,
        enabled: bool,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        source_file: str | None,
        source_line: int | None,
    ) -> dict[str, Any]:
        condition = getattr(substep, "condition", None)
        expression = getattr(substep, "expression", None)
        eval_mode = getattr(substep, "eval_mode", "condition")

        if eval_mode == "enabled":
            text = str(expression or substep.instruction_kind or "Enable")
            if enabled_state == "disabled_parent":
                return {
                    "source_file": source_file,
                    "source_line": source_line,
                    "expression": text,
                    "status": "skipped",
                    "value": None,
                    "details": [],
                    "summary": text,
                    "annotation": runner._condition_annotation(
                        status="skipped",
                        expression=text,
                        summary=text,
                    ),
                }
            value = bool(enabled)
            status = "true" if value else "false"
            summary = f"{text}({value})"
            return {
                "source_file": source_file,
                "source_line": source_line,
                "expression": text,
                "status": status,
                "value": value,
                "details": [{"name": "enabled", "value": enabled}],
                "summary": summary,
                "annotation": runner._condition_annotation(
                    status=status,
                    expression=text,
                    summary=summary,
                ),
            }

        if condition is None:
            text = str(expression or substep.instruction_kind or "Condition")
            return {
                "source_file": source_file,
                "source_line": source_line,
                "expression": text,
                "status": "skipped",
                "value": None,
                "details": [],
                "summary": text,
                "annotation": runner._condition_annotation(
                    status="skipped",
                    expression=text,
                    summary=text,
                ),
            }

        text = str(expression or runner._condition_expression(condition))
        if enabled_state == "disabled_parent":
            return {
                "source_file": source_file,
                "source_line": source_line,
                "expression": text,
                "status": "skipped",
                "value": None,
                "details": [],
                "summary": text,
                "annotation": runner._condition_annotation(
                    status="skipped",
                    expression=text,
                    summary=text,
                ),
            }

        value, details = runner._evaluate_condition_value(condition, ctx)
        status = "true" if value else "false"
        summary = runner._condition_term_text(condition, details)
        return {
            "source_file": source_file,
            "source_line": source_line,
            "expression": text,
            "status": status,
            "value": value,
            "details": details,
            "summary": summary,
            "annotation": runner._condition_annotation(
                status=status,
                expression=text,
                summary=summary,
            ),
        }

    def _enabled_state_for(
        self,
        *,
        kind: Literal["rung", "branch", "subroutine"],
        enabled: bool,
        parent_enabled: bool,
    ) -> Literal["enabled", "disabled_local", "disabled_parent"]:
        if enabled:
            return "enabled"
        if kind == "branch" and not parent_enabled:
            return "disabled_parent"
        return "disabled_local"

    def _build_step_trace(
        self,
        *,
        kind: Literal["rung", "branch", "subroutine"],
        rung: Rung,
        enabled_state: Literal["enabled", "disabled_local", "disabled_parent"],
        rung_condition_traces: list[dict[str, Any]],
        branch_trace_map: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        from pyrung.core.rung import Rung as RungClass

        regions: list[dict[str, Any]] = [
            {
                "kind": "branch" if kind == "branch" else "rung",
                "source_file": rung.source_file,
                "source_line": rung.source_line,
                "end_line": self._effective_region_end_line(rung),
                "enabled_state": enabled_state,
                "conditions": rung_condition_traces,
            }
        ]

        for item in rung._execution_items:
            if not isinstance(item, RungClass):
                continue
            branch_trace = branch_trace_map.get(id(item), {})
            regions.append(
                {
                    "kind": "branch",
                    "source_file": item.source_file,
                    "source_line": item.source_line,
                    "end_line": self._effective_region_end_line(item),
                    "enabled_state": branch_trace.get("enabled_state", "disabled_local"),
                    "conditions": list(branch_trace.get("conditions", [])),
                }
            )

        return {"regions": regions}

    def _effective_region_end_line(self, rung: Rung) -> int | None:
        if rung.end_line is not None:
            return int(rung.end_line)

        lines: list[int] = []
        if rung.source_line is not None:
            lines.append(int(rung.source_line))
        self._collect_rung_instruction_lines(rung, lines)
        if not lines:
            return None
        return max(lines)

    def _collect_rung_instruction_lines(self, rung: Rung, lines: list[int]) -> None:
        from pyrung.core.instruction import Instruction
        from pyrung.core.rung import Rung as RungClass

        for item in rung._execution_items:
            if isinstance(item, RungClass):
                self._collect_rung_instruction_lines(item, lines)
                continue
            if isinstance(item, Instruction):
                line = getattr(item, "source_line", None)
                if line is not None:
                    lines.append(int(line))
                end_line = getattr(item, "end_line", None)
                if end_line is not None:
                    lines.append(int(end_line))
                nested = getattr(item, "instructions", None)
                if isinstance(nested, list):
                    for child in nested:
                        if not isinstance(child, Instruction):
                            continue
                        child_line = getattr(child, "source_line", None)
                        if child_line is not None:
                            lines.append(int(child_line))
                        child_end_line = getattr(child, "end_line", None)
                        if child_end_line is not None:
                            lines.append(int(child_end_line))

    def _evaluate_conditions_with_trace(
        self,
        runner: Any,
        conditions: list[Any],
        ctx: ScanContext,
    ) -> tuple[bool, list[dict[str, Any]]]:
        if not conditions:
            return True, []

        traces: list[dict[str, Any]] = []
        enabled = True
        for condition in conditions:
            if not enabled:
                traces.append(self._skipped_condition_trace(runner, condition))
                continue
            value, details = runner._evaluate_condition_value(condition, ctx)
            if not value:
                enabled = False
            expression = runner._condition_expression(condition)
            status = "true" if value else "false"
            summary = runner._condition_term_text(condition, details)
            traces.append(
                {
                    "source_file": getattr(condition, "source_file", None),
                    "source_line": getattr(condition, "source_line", None),
                    "expression": expression,
                    "status": status,
                    "value": value,
                    "details": details,
                    "summary": summary,
                    "annotation": runner._condition_annotation(
                        status=status,
                        expression=expression,
                        summary=summary,
                    ),
                }
            )
        return enabled, traces

    def _skipped_condition_trace(self, runner: Any, condition: Any) -> dict[str, Any]:
        expression = runner._condition_expression(condition)
        return {
            "source_file": getattr(condition, "source_file", None),
            "source_line": getattr(condition, "source_line", None),
            "expression": expression,
            "status": "skipped",
            "value": None,
            "details": [],
            "summary": expression,
            "annotation": runner._condition_annotation(
                status="skipped",
                expression=expression,
                summary=expression,
            ),
        }
