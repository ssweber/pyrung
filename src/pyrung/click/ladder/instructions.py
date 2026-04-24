"""Instruction token formatting for Click ladder export."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction.calc import infer_calc_mode

from .translator import _quote


# ---- Instruction token mixin ----
class _InstructionMixin:
    """Render instruction objects into Click-compatible output tokens."""

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...
        def _render_operand(
            self,
            value: Any,
            *,
            path: str,
            source: Any,
            allow_immediate: bool = False,
            immediate_context: str = "",
        ) -> str: ...
        def _single_output_rows(
            self,
            condition_rows: list[Any],
            *,
            output_token: str,
            first_marker: str,
        ) -> list[tuple[str, ...]]: ...
        def _expand_conditions(self, conditions: list[Condition], *, path: str) -> list[Any]: ...
        def _render_sequence(self, values: Any, *, path: str, source: Any) -> str: ...
        def _render_condition_sequence(
            self,
            values: tuple[Any, ...],
            *,
            path: str,
            source: Any,
        ) -> str: ...
        def _render_pattern(self, pattern: tuple[tuple[bool, ...], ...]) -> str: ...
        def _render_converter(self, converter: CopyConverter) -> str: ...

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
        # forloop() lowers to: for(...) + child instructions + next().
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

    def _instruction_token(self, instruction: Any, *, path: str) -> str:
        # Dispatch by runtime type name to avoid importing all instruction classes.
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
            convert_kw = {}
            if instruction.convert is not None:
                convert_kw["convert"] = self._render_converter(instruction.convert)
            return self._fn(
                "copy",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **convert_kw,
                **oneshot_kw,
            )
        if instruction_type == "BlockCopyInstruction":
            convert_kw = {}
            if instruction.convert is not None:
                convert_kw["convert"] = self._render_converter(instruction.convert)
            return self._fn(
                "blockcopy",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **convert_kw,
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
                "math",
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
            range_str = self._render_operand(
                instruction.search_range,
                path=f"{path}.search_range",
                source=instruction,
            )
            value_str = self._render_operand(
                instruction.value, path=f"{path}.value", source=instruction
            )
            comparison = f"{range_str} {instruction.condition} {value_str}"
            return self._fn(
                "search",
                comparison,
                result=self._render_operand(
                    instruction.result, path=f"{path}.result", source=instruction
                ),
                found=self._render_operand(
                    instruction.found, path=f"{path}.found", source=instruction
                ),
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
            remote_start_expr = _render_remote_start(instruction)
            target_expr = _render_modbus_target(instruction)
            kwargs: dict[str, str] = dict(
                target=target_expr,
                remote_start=remote_start_expr,
                source=self._render_operand(
                    instruction.source, path=f"{path}.source", source=instruction
                ),
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
            )
            if instruction.word_swap:
                kwargs["word_swap"] = "1"
            return self._fn("send", **kwargs)
        if instruction_type == "ModbusReceiveInstruction":
            remote_start_expr = _render_remote_start(instruction)
            target_expr = _render_modbus_target(instruction)
            kwargs = dict(
                target=target_expr,
                remote_start=remote_start_expr,
                dest=self._render_operand(
                    instruction.dest, path=f"{path}.dest", source=instruction
                ),
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
            )
            if instruction.word_swap:
                kwargs["word_swap"] = "1"
            return self._fn("receive", **kwargs)
        if instruction_type == "CallInstruction":
            return self._fn("call", _quote(str(instruction.subroutine_name)))
        if instruction_type == "ReturnInstruction":
            return self._fn("return")

        if instruction_type == "NopInstruction":
            return "NOP"

        if instruction_type == "RawInstruction":
            return f"raw({instruction.class_name},{instruction.fields})"

        self._raise_issue(
            path=path,
            message=f"Unsupported instruction type: {instruction_type}.",
            source=instruction,
        )

    def _fn(self, name: str, *args: str, **kwargs: str) -> str:
        parts = list(args)
        parts.extend(f"{k}={v}" for k, v in kwargs.items())
        if not parts:
            return f"{name}()"
        return f"{name}({','.join(parts)})"


# ---- External target / remote-start helpers ----
def _render_modbus_target(instruction: object) -> str:
    """Render target constructor for the ladder export."""
    from pyrung.core.instruction.send_receive import ModbusRtuTarget

    raw_target = getattr(instruction, "raw_target", None)
    if isinstance(raw_target, ModbusRtuTarget):
        parts = [
            f"name={_quote(raw_target.name)}",
            f"com_port={_quote(raw_target.com_port)}",
            f"device_id={raw_target.device_id}",
        ]
        return f"ModbusRtuTarget({','.join(parts)})"

    name = getattr(instruction, "target_name", "")
    host = getattr(instruction, "host", None)
    if host is None:
        return _quote(name)
    port = getattr(instruction, "port", 502)
    device_id = getattr(instruction, "device_id", 1)
    parts = [
        f"name={_quote(name)}",
        f"ip={_quote(host)}",
        f"port={port}",
        f"device_id={device_id}",
    ]
    return f"ModbusTcpTarget({','.join(parts)})"


_984_BASES = {
    "holding": 400001,
    "input": 300001,
    "discrete_input": 100001,
    "coil": 1,
}


def _render_remote_start(instruction: object) -> str:
    """Render remote_start as Click address string or ModbusAddress(...)."""
    remote_address = getattr(instruction, "remote_address", None)
    if remote_address is not None:
        base = _984_BASES[remote_address.register_type.value]
        addr_984 = base + remote_address.address
        return f"ModbusAddress(address={addr_984})"

    bank = getattr(instruction, "bank", "")
    start = getattr(instruction, "start", 0)
    return _quote(f"{bank}{start}")


__all__ = ["_InstructionMixin"]
