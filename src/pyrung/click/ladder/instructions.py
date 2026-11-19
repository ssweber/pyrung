"""Instruction token formatting for Click ladder export."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from pyrung.click.capabilities import single_register_fill_dest
from pyrung.click.validation.findings import (
    CLK_COPY_RANGE_NOT_CONSECUTIVE,
    RangeFault,
    RangeProblem,
    _display_address,
    range_not_consecutive_advice,
    range_not_consecutive_text,
)
from pyrung.click.validation.hardware import constant_text_past_txt
from pyrung.core.condition import Condition
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction.calc import infer_calc_mode
from pyrung.core.instruction.conversions import copy_source_kind
from pyrung.core.instruction.data_transfer import _plain_text_converter
from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation.render import operand_name

from ._loop_items import is_own_rung, loop_item_rung
from .translator import _compact_contiguous_range, _quote


def _exported_converter(instruction: Any) -> CopyConverter | None:
    """Return the converter that the export writes with a Copy.

    CLICK saves a conversion with every Copy between TXT and a number register or
    constant, so the export writes the converter that pyrung runs. A Copy of a TXT into
    a DF exports plain, because CLICK's dialog offers no conversion there.
    """
    authored = getattr(instruction, "authored_convert", instruction.convert)
    if authored is not None:
        return authored
    converter = instruction.convert
    if converter is not None and converter.mode == "value":
        if copy_source_kind(instruction.dest) is TagType.REAL:
            return None
    return converter


# ---- Instruction token mixin ----


class _InstructionMixin:
    """Render instruction objects into Click-compatible output tokens."""

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...
        def _next_marker(self) -> str: ...
        def _render_operand(
            self,
            value: Any,
            *,
            path: str,
            source: Any,
            allow_immediate: bool = False,
            immediate_context: str = "",
        ) -> str: ...
        def _render_network(
            self,
            conditions: list[Condition],
            items: list[Any],
            *,
            path: str,
            source: Any,
            first_marker: str,
        ) -> list[tuple[str, ...]]: ...
        def _render_sequence(self, values: Any, *, path: str, source: Any) -> str: ...
        def _render_shift_range(self, bit_range: Any, *, path: str, source: Any) -> str: ...
        def _hex_constants(
            self, mode: Literal["word", "copy", "math", "exact"] | None
        ) -> AbstractContextManager[None]: ...
        @staticmethod
        def _is_hex_bank_operand(rendered: str) -> bool: ...
        def _render_condition_sequence(
            self,
            values: tuple[Any, ...],
            *,
            path: str,
            source: Any,
        ) -> str: ...
        def _render_pattern(self, pattern: tuple[tuple[bool, ...], ...]) -> str: ...
        def _render_converter(
            self, converter: CopyConverter, instruction: Any, source_text: str, *, path: str
        ) -> str: ...
        def _render_source_rung(self, rung: Any, *, path: str) -> list[tuple[str, ...]]: ...

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
        rows = self._render_network(
            conditions, [for_token], path=path, source=instruction, first_marker=self._next_marker()
        )

        for child_index, child_instruction in enumerate(instruction.body):
            child_path = f"{path}.body[{child_index}]({type(child_instruction).__name__})"
            if is_own_rung(child_instruction):
                # A conditioned body rung, or a nested loop: a rung of its own.
                rows.extend(
                    self._render_source_rung(loop_item_rung(child_instruction), path=child_path)
                )
                continue
            child_rows = self._render_network(
                [],
                [self._instruction_token(child_instruction, path=child_path)],
                path=child_path,
                source=child_instruction,
                first_marker=self._next_marker(),
            )
            child_rows.extend(self._pin_rows(child_instruction, path=child_path))
            rows.extend(child_rows)

        rows.extend(
            self._render_network(
                [],
                [self._fn("next")],
                path=f"{path}.next",
                source=instruction,
                first_marker=self._next_marker(),
            )
        )
        return rows

    def _pin_rows(self, instruction: Any, *, path: str) -> list[tuple[str, ...]]:
        pin_specs = self._pin_specs(instruction, path=path)
        rows: list[tuple[str, ...]] = []
        for pin_name, condition, pin_token in pin_specs:
            # A pin is a wire of its own, drawn from the rail below the box's rows.
            rows.extend(
                self._render_network(
                    [condition],
                    [pin_token],
                    path=f"{path}.pin[{pin_name}]",
                    source=instruction,
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

        if instruction_type in {
            "EventDrumInstruction",
            "TimeDrumInstruction",
            "TimeEventDrumInstruction",
        }:
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

    def _out_variant_token(self, instruction: Any, instruction_type: str, *, path: str) -> str:
        """Render a debounce, flasher or timed coil, with each phase as its Timer's TD and preset.

        CLICK's coil names only the TD of the phase. The T of the same number comes with
        it.
        """
        if instruction_type == "TimedInstruction":
            phases = [("on", instruction.timer_acc, instruction.on_preset)]
        else:
            phases = [
                ("on", instruction.on_acc, instruction.on_preset),
                ("off", instruction.off_acc, instruction.off_preset),
            ]
        kwargs: dict[str, str] = {}
        for side, acc, preset in phases:
            kwargs[f"{side}_acc"] = self._render_operand(
                acc, path=f"{path}.{side}_acc", source=instruction
            )
            kwargs[f"{side}_preset"] = self._render_operand(
                preset, path=f"{path}.{side}_preset", source=instruction
            )
        kwargs["unit"] = self._render_operand(
            instruction.unit, path=f"{path}.unit", source=instruction
        )
        if instruction_type == "TimedInstruction" and instruction.maintained:
            kwargs["maintained"] = "1"
        verb = {
            "DebounceInstruction": "debounce",
            "FlasherInstruction": "flasher",
            "TimedInstruction": "timed",
        }[instruction_type]
        return self._fn(
            verb,
            self._render_operand(instruction.target, path=f"{path}.target", source=instruction),
            **kwargs,
        )

    def _single_register_fill_token(
        self, instruction: Any, tag: Any, *, path: str, oneshot_kw: dict[str, str]
    ) -> str:
        """Render a Fill of one register as the Copy that CLICK accepts in its place.

        The Copy must store what the Fill stores. The export refuses a text value of
        several characters, because it would fill several TXT. It also refuses a value
        that would need a Copy between text and a number, because that Copy converts.
        """
        value = instruction.value
        if isinstance(value, str) and len(value) > 1 and tag.type == TagType.CHAR:
            self._raise_issue(
                path=f"{path}.value",
                message=(
                    f"fill({value!r}, ...) writes one register, which CLICK takes as a Copy. "
                    "A Copy of several characters fills several TXT. "
                    "Fill one character, or use copy() into the TXT you mean."
                ),
                source=instruction,
            )
        if _plain_text_converter(value, tag) is not None:
            self._raise_issue(
                path=f"{path}.value",
                message=(
                    "fill() of one register exports as a CLICK Copy. A Copy between text "
                    "and a number converts the value. Use copy() with convert= to choose "
                    "the conversion."
                ),
                source=instruction,
            )
        dest = self._render_operand(tag, path=f"{path}.dest", source=instruction)
        with self._hex_constants("copy" if self._is_hex_bank_operand(dest) else None):
            source = self._render_operand(value, path=f"{path}.value", source=instruction)
        return self._fn("copy", source, dest, **oneshot_kw)

    def _range_text(
        self, value: Any, *, path: str, instruction: Any
    ) -> tuple[str, RangeProblem | None]:
        """Return a Copy family range as the export writes it, and its problem, if any.

        The render writes a range that is not consecutive ascending addresses as a list,
        such as ``[DS1,DS5]``. CLICK has no list form.
        """
        text = self._render_operand(value, path=path, source=instruction)
        if not text.startswith("["):
            return text, None
        addresses = [address for address in text[1:-1].split(",") if address]
        block_range = value.value if isinstance(value, ImmediateRef) else value
        fault: RangeFault
        if not addresses:
            fault = "empty"
        elif (
            getattr(block_range, "reverse_order", False)
            and len(addresses) > 1
            and _compact_contiguous_range(addresses[::-1]) is not None
        ):
            # Only a range that the author reversed gets the advice to remove .reverse().
            # A block mapped onto descending addresses gets the advice to map it again.
            fault = "descending"
        else:
            fault = "scattered"
        own = [_display_address(tag.name) for tag in block_range.tags()] == addresses
        return text, (operand_name(value), fault, addresses, own)

    def _render_run(self, value: Any, *, path: str, instruction: Any) -> str:
        """Render a Copy family range, and refuse one that CLICK cannot hold as a range.

        The export refuses it even when CLK_COPY_RANGE_NOT_CONSECUTIVE is allowed, so it
        never writes a list.
        """
        text, problem = self._range_text(value, path=path, instruction=instruction)
        if problem is not None:
            self._raise_issue(
                path=path,
                message=(
                    f"{CLK_COPY_RANGE_NOT_CONSECUTIVE}: "
                    f"{range_not_consecutive_text([problem])} "
                    f"{range_not_consecutive_advice([problem])}"
                ),
                source=instruction,
            )
        return text

    def _refuse_constant_text_past_txt(self, instruction: Any, dest: str, *, path: str) -> None:
        # CLICK's Copy refuses a constant past TXT1000 (OBJ-003). So the export refuses it,
        # even when CLK_TEXT_PAST_BANK is allowed.
        match = re.fullmatch(r"TXT(\d+)", dest)
        if match is None:
            return
        count = constant_text_past_txt(instruction, int(match.group(1)))
        if count is not None:
            self._raise_issue(
                path=path,
                message=(
                    f"copy() into {dest} has a {count}-character constant, which runs past "
                    "TXT1000. CLICK's Copy refuses it (OBJ-003), so this cannot be written "
                    "in CLICK."
                ),
                source=instruction,
            )

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
        if instruction_type == "ToggleInstruction":
            return self._fn(
                "toggle",
                self._render_operand(
                    instruction.target,
                    path=f"{path}.target",
                    source=instruction,
                    allow_immediate=True,
                    immediate_context="coil",
                ),
            )
        if instruction_type in {"DebounceInstruction", "FlasherInstruction", "TimedInstruction"}:
            return self._out_variant_token(instruction, instruction_type, path=path)
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
            converter = _exported_converter(instruction)
            dest = self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction)
            self._refuse_constant_text_past_txt(instruction, dest, path=f"{path}.dest")
            with self._hex_constants("copy" if self._is_hex_bank_operand(dest) else None):
                copy_source = self._render_operand(
                    instruction.source, path=f"{path}.source", source=instruction
                )
            if converter is not None:
                convert_kw["convert"] = self._render_converter(
                    converter, instruction, copy_source, path=f"{path}.convert"
                )
            return self._fn("copy", copy_source, dest, **convert_kw, **oneshot_kw)
        if instruction_type == "BlockCopyInstruction":
            convert_kw = {}
            block_source = self._render_run(
                instruction.source, path=f"{path}.source", instruction=instruction
            )
            if instruction.convert is not None:
                convert_kw["convert"] = self._render_converter(
                    instruction.convert, instruction, block_source, path=f"{path}.convert"
                )
            return self._fn(
                "blockcopy",
                block_source,
                self._render_run(instruction.dest, path=f"{path}.dest", instruction=instruction),
                **convert_kw,
                **oneshot_kw,
            )
        if instruction_type == "FillInstruction":
            single = single_register_fill_dest(instruction.dest)
            if single is not None:
                return self._single_register_fill_token(
                    instruction, single, path=path, oneshot_kw=oneshot_kw
                )
            dest = self._render_run(instruction.dest, path=f"{path}.dest", instruction=instruction)
            with self._hex_constants("word" if self._is_hex_bank_operand(dest) else None):
                fill_value = self._render_operand(
                    instruction.value, path=f"{path}.value", source=instruction
                )
            return self._fn("fill", fill_value, dest, **oneshot_kw)
        if instruction_type == "CalcInstruction":
            expression = instruction.source_expression
            mode = infer_calc_mode(expression, instruction.dest).mode
            # Hex-mode Math reads every constant as hex (shift counts too).
            with self._hex_constants("math" if str(mode) == "hex" else None):
                formula = self._render_operand(
                    expression,
                    path=f"{path}.expression",
                    source=instruction,
                )
            return self._fn(
                "math",
                formula,
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
            with self._hex_constants("exact" if self._is_hex_bank_operand(range_str) else None):
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
                self._render_run(
                    instruction.bit_block,
                    path=f"{path}.bit_block",
                    instruction=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **oneshot_kw,
            )
        if instruction_type == "PackWordsInstruction":
            return self._fn(
                "pack_words",
                self._render_run(
                    instruction.word_block,
                    path=f"{path}.word_block",
                    instruction=instruction,
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
                self._render_run(
                    instruction.source_range,
                    path=f"{path}.source_range",
                    instruction=instruction,
                ),
                self._render_operand(instruction.dest, path=f"{path}.dest", source=instruction),
                **pt_kw,
            )
        if instruction_type == "UnpackToBitsInstruction":
            return self._fn(
                "unpack_to_bits",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_run(
                    instruction.bit_block,
                    path=f"{path}.bit_block",
                    instruction=instruction,
                ),
                **oneshot_kw,
            )
        if instruction_type == "UnpackToWordsInstruction":
            return self._fn(
                "unpack_to_words",
                self._render_operand(instruction.source, path=f"{path}.source", source=instruction),
                self._render_run(
                    instruction.word_block,
                    path=f"{path}.word_block",
                    instruction=instruction,
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
                self._render_shift_range(
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
        if instruction_type == "TimeEventDrumInstruction":
            # A step's blank preset or event renders as None.
            return self._fn(
                "time_event_drum",
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
                events=self._render_condition_sequence(
                    instruction.events,
                    path=f"{path}.events",
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
            target_expr = self._render_modbus_target(instruction, path)
            kwargs: dict[str, str] = dict(
                target=target_expr,
                remote_start=remote_start_expr,
                source=self._render_operand(
                    instruction.source, path=f"{path}.source", source=instruction
                ),
            )
            kwargs.update(self._render_status_bits(instruction, "sending", path))
            if instruction.word_swap:
                kwargs["word_swap"] = "1"
            return self._fn("send", **kwargs)
        if instruction_type == "ModbusReceiveInstruction":
            remote_start_expr = _render_remote_start(instruction)
            target_expr = self._render_modbus_target(instruction, path)
            kwargs = dict(
                target=target_expr,
                remote_start=remote_start_expr,
                dest=self._render_operand(
                    instruction.dest, path=f"{path}.dest", source=instruction
                ),
            )
            kwargs.update(self._render_status_bits(instruction, "receiving", path))
            if instruction.word_swap:
                kwargs["word_swap"] = "1"
            return self._fn("receive", **kwargs)
        if instruction_type == "CallInstruction":
            return self._fn("call", _quote(str(instruction.subroutine_name)))
        if instruction_type == "ReturnInstruction":
            return self._fn("return")
        if instruction_type == "BreakInstruction":
            return self._fn("break")
        if instruction_type == "ContinueInstruction":
            return self._fn("continue")

        if instruction_type == "NopInstruction":
            return "NOP"

        if instruction_type == "RawInstruction":
            return f"raw({instruction.class_name},{instruction.fields})"

        self._raise_issue(
            path=path,
            message=f"Unsupported instruction type: {instruction_type}.",
            source=instruction,
        )

    def _render_status_bits(self, instruction: Any, busy_name: str, path: str) -> dict[str, str]:
        """Return the status keyword arguments of a Send or Receive.

        If the caller did not pass a status bit, pyrung gave it a hidden tag.  The export
        leaves that bit out.
        """
        rendered: dict[str, str] = {}
        for name in (busy_name, "success", "error", "exception_response"):
            tag = getattr(instruction, name)
            if not is_modbus_status_tag(tag.name):
                rendered[name] = self._render_operand(
                    tag, path=f"{path}.{name}", source=instruction
                )
        return rendered

    def _render_modbus_target(self, instruction: Any, path: str) -> str:
        """Return the target constructor, with a DS device ID written as its address."""
        from pyrung.core.instruction.send_receive import ModbusRtuTarget, ModbusTcpTarget

        device_id = instruction.device_id
        if isinstance(device_id, Tag):
            device_id_text = self._render_operand(
                device_id, path=f"{path}.device_id", source=instruction
            )
        else:
            device_id_text = str(device_id)
        raw_target = instruction.raw_target
        if isinstance(raw_target, ModbusRtuTarget):
            parts = [
                f"name={_quote(raw_target.name)}",
                f"com_port={_quote(raw_target.com_port)}",
                f"device_id={device_id_text}",
            ]
            return f"ModbusRtuTarget({','.join(parts)})"
        if instruction.host is None:
            return _quote(instruction.target_name)
        parts = [
            f"name={_quote(instruction.target_name)}",
            f"ip={_quote(instruction.host)}",
            f"port={instruction.port}",
            f"device_id={device_id_text}",
        ]
        # Ethernet is CLICK's default port, so only WLAN is written.
        if isinstance(raw_target, ModbusTcpTarget) and raw_target.com_port != "ethernet":
            parts.append(f"com_port={_quote(raw_target.com_port)}")
        return f"ModbusTcpTarget({','.join(parts)})"

    def _fn(self, name: str, *args: str, **kwargs: str) -> str:
        parts = list(args)
        parts.extend(f"{k}={v}" for k, v in kwargs.items())
        if not parts:
            return f"{name}()"
        return f"{name}({','.join(parts)})"


# ---- External target / remote-start helpers ----


_984_BASES = {
    "holding": 400001,
    "input": 300001,
    "discrete_input": 100001,
    "coil": 1,
}


def _render_remote_start(instruction: object) -> str:
    """Render remote_start as a Click address string or ``ModbusAddress(...)``.

    A hex-written address stays hex. Any other address is MODBUS 984, written as CLICK
    writes it (a coil is ``17``, not ``000017``).
    """
    remote_address = getattr(instruction, "remote_address", None)
    if remote_address is not None:
        register_type = remote_address.register_type.value
        if remote_address.notation == "hex":
            return (
                f"ModbusAddress(address={remote_address.address:X}h,register_type={register_type})"
            )
        addr_984 = _984_BASES[register_type] + remote_address.address
        return f"ModbusAddress(address={addr_984})"

    bank = getattr(instruction, "bank", "")
    start = getattr(instruction, "start", 0)
    return _quote(f"{bank}{start}")


__all__ = ["_InstructionMixin"]
