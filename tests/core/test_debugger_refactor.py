"""Targeted tests for the PLCDebugger refactor architecture."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, call, forloop, out, subroutine
from pyrung.core.debug_handlers import (
    CallInstructionDebugHandler,
    ForLoopInstructionDebugHandler,
    GenericInstructionDebugHandler,
)
from pyrung.core.debug_trace import ConditionTrace, SourceSpan, TraceEvent, TraceRegion
from pyrung.core.debugger import PLCDebugger
from pyrung.core.instruction import CallInstruction, ForLoopInstruction, OutInstruction
from pyrung.core.runner import ScanStep


def test_trace_event_to_dict_preserves_legacy_shape() -> None:
    event = TraceEvent(
        regions=[
            TraceRegion(
                kind="instruction",
                source=SourceSpan(source_file="main.py", source_line=10, end_line=10),
                enabled_state="enabled",
                conditions=[
                    ConditionTrace(
                        source_file="main.py",
                        source_line=10,
                        expression="Enable",
                        status="true",
                        value=True,
                        details=[{"name": "enabled", "value": True}],
                        summary="Enable(True)",
                        annotation="[T] Enable(True)",
                    )
                ],
            )
        ]
    )

    payload = event.to_dict()
    assert payload["regions"][0]["kind"] == "instruction"
    assert payload["regions"][0]["source_line"] == 10
    assert payload["regions"][0]["end_line"] == 10
    assert payload["regions"][0]["conditions"][0]["expression"] == "Enable"


def test_debugger_control_flow_handlers_are_registered_with_generic_fallback() -> None:
    debugger = PLCDebugger(step_factory=dict)
    fake_program = SimpleNamespace(subroutines={}, call_subroutine_ctx=lambda *_args: None)

    call_handler = debugger._resolve_instruction_handler(CallInstruction("sub", fake_program))
    forloop_handler = debugger._resolve_instruction_handler(
        ForLoopInstruction(count=1, idx_tag=Int("Idx"), instructions=[])
    )
    fallback_handler = debugger._resolve_instruction_handler(OutInstruction(Bool("Out")))

    assert isinstance(call_handler, CallInstructionDebugHandler)
    assert isinstance(forloop_handler, ForLoopInstructionDebugHandler)
    assert isinstance(fallback_handler, GenericInstructionDebugHandler)


def test_call_handler_preserves_subroutine_context_and_call_stack() -> None:
    done = Bool("Done")
    sub = Bool("Sub")

    with Program(strict=False) as logic:
        with subroutine("work"):
            with Rung():
                out(sub)

        with Rung():
            call("work")
            out(done)

    runner = PLCRunner(logic)
    steps = list(runner.scan_steps_debug())

    sub_instruction_steps = [
        step for step in steps if step.kind == "instruction" and step.subroutine_name == "work"
    ]
    assert sub_instruction_steps
    assert sub_instruction_steps[0].call_stack == ("work",)
    assert runner.current_state.tags["Sub"] is True
    assert runner.current_state.tags["Done"] is True


def test_forloop_handler_emits_nested_child_instruction_steps() -> None:
    pulse = Bool("Pulse")

    with Program(strict=False) as logic:
        with Rung():
            with forloop(2):
                out(pulse)

    runner = PLCRunner(logic)
    steps = list(runner.scan_steps_debug())

    instruction_steps = [step for step in steps if step.kind == "instruction"]
    assert len(instruction_steps) == 2
    assert all(step.instruction_kind == "OutInstruction" for step in instruction_steps)
    assert runner.current_state.tags["Pulse"] is True


def test_debugger_accepts_protocol_runner_wrapper_without_private_api() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as logic:
        with Rung(enable):
            out(light)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True})

    class RunnerFacade:
        def __init__(self, inner: PLCRunner) -> None:
            self._inner = inner

        def prepare_scan(self) -> tuple[Any, float]:
            return self._inner.prepare_scan()

        def commit_scan(self, ctx: Any, dt: float) -> None:
            self._inner.commit_scan(ctx, dt)

        def iter_top_level_rungs(self) -> Any:
            return self._inner.iter_top_level_rungs()

        def evaluate_condition_value(self, condition: Any, ctx: Any) -> tuple[bool, list[dict[str, Any]]]:
            return self._inner.evaluate_condition_value(condition, ctx)

        def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
            return self._inner.condition_term_text(condition, details)

        def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
            return self._inner.condition_annotation(
                status=status,
                expression=expression,
                summary=summary,
            )

        def condition_expression(self, condition: Any) -> str:
            return self._inner.condition_expression(condition)

    debugger = PLCDebugger(step_factory=ScanStep)
    steps = list(debugger.scan_steps_debug(RunnerFacade(runner)))

    assert [step.kind for step in steps] == ["instruction", "rung"]
    assert runner.current_state.tags["Light"] is True
