"""Targeted tests for the PLCDebugger refactor architecture."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Real,
    Rung,
    call,
    count_up,
    forloop,
    out,
    rise,
    subroutine,
    system,
)
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

    runner = PLC(logic)
    steps = list(runner.debug.scan_steps_debug())

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

    runner = PLC(logic)
    steps = list(runner.debug.scan_steps_debug())

    instruction_steps = [step for step in steps if step.kind == "instruction"]
    assert len(instruction_steps) == 2
    assert all(step.instruction_kind == "OutInstruction" for step in instruction_steps)
    assert runner.current_state.tags["Pulse"] is True


def test_forloop_handler_runs_no_iteration_for_a_non_finite_count() -> None:
    """A REAL count holding Inf runs no iteration and sets SC43 when stepped
    through the debugger, as in a plain scan."""
    pulse = Bool("Pulse")
    count = Real("Count")

    with Program(strict=False) as logic:
        with Rung():
            with forloop(count):
                out(pulse)

    runner = PLC(logic)
    runner.patch({count: float("inf")})
    steps = list(runner.debug.scan_steps_debug())

    assert not [step for step in steps if step.instruction_kind == "OutInstruction"]
    assert runner.current_state.tags["Pulse"] is False
    assert runner.current_state.tags[system.fault.out_of_range.name] is True


def _bare_body_edge_reset_program() -> Program:
    enable = Bool("Enable")
    x = Bool("X")
    cnt = Counter.clone("Cnt")

    with Program(strict=False) as logic:
        with Rung(enable):
            with forloop(3):
                count_up(cnt, 100).reset(rise(x))
    return logic


def _rise_x_shown(events: Any) -> list[tuple[str, Any]]:
    shown = []
    for event in events:
        if event.instruction_kind != "Reset" or event.trace is None:
            continue
        for region in event.trace.regions:
            for condition in region.conditions:
                if condition.expression == "rise(X)":
                    details = {d["name"]: d["value"] for d in condition.details}
                    shown.append((condition.status, details["previous"]))
    return shown


def test_forloop_handler_shows_a_bare_body_edge_leg_as_the_engine_scans_it() -> None:
    """A bare body instruction with an edge leg is a row of its own: the debugger
    traces its helper leg on that row's view, so rise() shows true on the first
    iteration only, as the engine fires it."""
    logic = _bare_body_edge_reset_program()

    def staged() -> PLC:
        plc = PLC(logic)
        plc.patch({"Enable": True})
        plc.step()
        plc.patch({"Enable": True, "X": True})
        return plc

    plain = staged()
    plain.step()
    debugged = staged()
    steps = list(debugged.debug.scan_steps_debug())

    assert _rise_x_shown(steps) == [("true", False), ("false", True), ("false", True)]
    assert debugged.current_state.tags == plain.current_state.tags
    assert debugged.current_state.memory == plain.current_state.memory


def test_replay_trace_shows_a_bare_body_edge_leg_as_the_engine_scans_it() -> None:
    plc = PLC(_bare_body_edge_reset_program())
    plc.patch({"Enable": True})
    plc.step()
    plc.patch({"Enable": True, "X": True})
    plc.step()
    plc.step()

    rise_scan = plc.current_state.scan_id - 1
    events = [e for trace in plc.replay_trace_at(rise_scan).values() for e in trace.events]
    assert _rise_x_shown(events) == [("true", False), ("false", True), ("false", True)]
    held = [e for trace in plc.replay_trace_at(rise_scan + 1).values() for e in trace.events]
    assert _rise_x_shown(held) == [("false", True)] * 3


def test_debugger_accepts_protocol_runner_wrapper_without_private_api() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as logic:
        with Rung(enable):
            out(light)

    runner = PLC(logic)
    runner.patch({"Enable": True})

    class RunnerFacade:
        def __init__(self, inner: PLC) -> None:
            self._debug = inner.debug

        def prepare_scan(self) -> tuple[Any, float]:
            return self._debug.prepare_scan()

        def commit_scan(self, ctx: Any, dt: float) -> None:
            self._debug.commit_scan(ctx, dt)

        def iter_top_level_rungs(self) -> Any:
            return self._debug.iter_top_level_rungs()

        def evaluate_condition_value(
            self, condition: Any, ctx: Any
        ) -> tuple[bool, list[dict[str, Any]]]:
            return self._debug.evaluate_condition_value(condition, ctx)

        def condition_term_text(self, condition: Any, details: list[dict[str, Any]]) -> str:
            return self._debug.condition_term_text(condition, details)

        def condition_annotation(self, *, status: str, expression: str, summary: str) -> str:
            return self._debug.condition_annotation(
                status=status,
                expression=expression,
                summary=summary,
            )

        def condition_expression(self, condition: Any) -> str:
            return self._debug.condition_expression(condition)

    debugger = PLCDebugger(step_factory=ScanStep)
    steps = list(debugger.scan_steps_debug(RunnerFacade(runner)))

    assert [step.kind for step in steps] == ["rung", "instruction"]
    assert runner.current_state.tags["Light"] is True
