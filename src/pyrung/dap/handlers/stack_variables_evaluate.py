"""Stack/evaluate ownership for DAP command handling.

Owns stack frame/scopes/variables/evaluate payload construction.
Must preserve evaluate command semantics and error message text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrung.core.state import SystemState
from pyrung.dap.expressions import And, Compare, Expr, ExpressionParseError, Not, Or
from pyrung.dap.expressions import compile as compile_condition
from pyrung.dap.expressions import parse as parse_condition

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _VariablesRequestArgs:
    variablesReference: Any = 0


@dataclass(frozen=True)
class _EvaluateRequestArgs:
    expression: Any = None
    context: Any = None


def on_stack_trace(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        rungs = adapter._top_level_rungs(runner)
        current_step = adapter._current_step
        current_index = adapter._current_rung_index
        program_path = adapter._program_path

    start_frame = int(args.get("startFrame", 0))
    levels = int(args.get("levels", 0))

    if current_step is not None:
        frames = adapter._formatter.build_current_stack_frames(
            current_step=current_step,
            rungs=rungs,
            subroutine_source_map=adapter._breakpoints.subroutine_sources(),
            canonical_path=adapter._canonical_path,
        )
    elif rungs:
        order = list(range(len(rungs)))
        if current_index is not None and 0 <= current_index < len(rungs):
            order = [current_index, *[i for i in order if i != current_index]]

        frames = [
            adapter._formatter.stack_frame_from_rung(
                frame_id=idx,
                name=f"Rung {idx}",
                rung=rungs[idx],
            )
            for idx in order
        ]
    else:
        frame = {"id": 0, "name": "Scan", "line": 1, "column": 1}
        if program_path is not None:
            frame["source"] = {"name": Path(program_path).name, "path": program_path}
        frames = [frame]

    if levels > 0:
        visible = frames[start_frame : start_frame + levels]
    else:
        visible = frames[start_frame:]

    return {"stackFrames": visible, "totalFrames": len(frames)}, []


def on_scopes(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    scopes = [
        {"name": "Tags", "variablesReference": adapter.TAGS_SCOPE_REF, "expensive": False},
        {"name": "Forces", "variablesReference": adapter.FORCES_SCOPE_REF, "expensive": False},
        {"name": "Memory", "variablesReference": adapter.MEMORY_SCOPE_REF, "expensive": False},
    ]
    with adapter._state_lock:
        has_monitors = bool(adapter._monitor_handles)
        monitor_count = len(adapter._monitor_handles)
    if has_monitors:
        scopes.append(
            {
                "name": "PLC Monitors",
                "variablesReference": adapter._monitor_scope_ref,
                "expensive": False,
                "namedVariables": monitor_count,
            }
        )
    return {"scopes": scopes}, []


def on_variables(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_VariablesRequestArgs, args)
    ref = int(parsed.variablesReference)
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        current_ctx = adapter._current_ctx
        state = runner.current_state
        forces = dict(runner.forces)

    if ref == adapter.TAGS_SCOPE_REF:
        values = dict(state.tags)
        if current_ctx is not None:
            values.update(getattr(current_ctx, "_tags_pending", {}))
        return {"variables": adapter._as_dap_variables(values)}, []

    if ref == adapter.FORCES_SCOPE_REF:
        return {"variables": adapter._as_dap_variables(forces)}, []

    if ref == adapter.MEMORY_SCOPE_REF:
        values = dict(state.memory)
        if current_ctx is not None:
            values.update(getattr(current_ctx, "_memory_pending", {}))
        return {"variables": adapter._as_dap_variables(values)}, []

    if ref == adapter._monitor_scope_ref:
        variables = adapter._monitor_variables()
        return {"variables": variables}, []

    raise adapter.DAPAdapterError(f"Unknown variablesReference: {ref}")


def on_evaluate(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_EvaluateRequestArgs, args)
    expression = parsed.expression
    if not isinstance(expression, str) or not expression.strip():
        raise adapter.DAPAdapterError("evaluate.expression is required")
    context = parsed.context
    evaluate_context = context if isinstance(context, str) else "repl"

    with adapter._state_lock:
        adapter._require_runner_locked()
        if evaluate_context == "watch":
            result = adapter._evaluate_watch_expression_locked(expression)
        else:
            result = adapter._evaluate_repl_command_locked(expression)

    return {"result": adapter._format_value(result), "variablesReference": 0}, []


def evaluate_repl_command_locked(adapter: Any, expression: str) -> str:
    parts = expression.strip().split()
    command = parts[0]
    runner = adapter._require_runner_locked()
    if command == "force":
        if len(parts) < 3:
            raise adapter.DAPAdapterError("Usage: force <tag> <value>")
        tag = parts[1]
        raw_value = expression.strip().split(None, 2)[2]
        value = adapter._parse_literal(raw_value)
        runner.add_force(tag, value)
        return f"Forced {tag}={value!r}"
    if command in {"remove_force", "unforce"}:
        if len(parts) != 2:
            raise adapter.DAPAdapterError("Usage: remove_force <tag>")
        tag = parts[1]
        runner.remove_force(tag)
        return f"Removed force {tag}"
    if command == "clear_forces":
        runner.clear_forces()
        return "Cleared all forces"
    raise adapter.DAPAdapterError(
        "Unsupported Debug Console command. Use force/remove_force/unforce/clear_forces. "
        "Use Watch for predicate expressions."
    )


def evaluate_watch_expression_locked(adapter: Any, expression: str) -> Any:
    state = adapter._effective_evaluate_state_locked()
    try:
        parsed = parse_condition(expression)
    except ExpressionParseError as exc:
        raise adapter.DAPAdapterError(str(exc)) from exc

    for name in sorted(adapter._expression_references(parsed)):
        if not adapter._state_has_reference(state, name):
            raise adapter.DAPAdapterError(f"Unknown tag or memory reference: {name}")

    if isinstance(parsed, Compare) and parsed.op is None and parsed.right is None:
        return adapter._state_value_for_reference(state, parsed.tag.name)

    return compile_condition(parsed)(state)


def effective_evaluate_state_locked(adapter: Any) -> SystemState:
    runner = adapter._require_runner_locked()
    state = runner.current_state
    current_ctx = adapter._current_ctx
    if current_ctx is None:
        return state

    pending_tags = getattr(current_ctx, "_tags_pending", {})
    if isinstance(pending_tags, dict) and pending_tags:
        state = state.with_tags(dict(pending_tags))

    pending_memory = getattr(current_ctx, "_memory_pending", {})
    if isinstance(pending_memory, dict) and pending_memory:
        state = state.with_memory(dict(pending_memory))

    return state


def expression_references(adapter: Any, expr: Expr) -> set[str]:
    if isinstance(expr, Compare):
        return {expr.tag.name}
    if isinstance(expr, Not):
        return {expr.child.name}
    if isinstance(expr, And):
        refs: set[str] = set()
        for child in expr.children:
            refs.update(adapter._expression_references(child))
        return refs
    if isinstance(expr, Or):
        refs: set[str] = set()
        for child in expr.children:
            refs.update(adapter._expression_references(child))
        return refs
    return set()


def state_has_reference(adapter: Any, state: SystemState, name: str) -> bool:
    return name in state.tags or name in state.memory


def state_value_for_reference(adapter: Any, state: SystemState, name: str) -> Any:
    if name in state.tags:
        return state.tags.get(name)
    if name in state.memory:
        return state.memory.get(name)
    return None
