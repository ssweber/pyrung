"""Automatically generated module split."""

from __future__ import annotations

from pyclickplc.modbus import MODBUS_MAPPINGS, plc_to_modbus

from pyrung.circuitpy.codegen.context import (
    CodegenContext,
    ModbusClientJobSpec,
    ModbusClientSymbolSpec,
)
from pyrung.core.instruction.send_receive import (
    ModbusReceiveInstruction,
    ModbusRtuTarget,
    ModbusSendInstruction,
    RegisterType,
)
from pyrung.core.memory_block import (
    BlockRange,
)
from pyrung.core.tag import Tag


def _modbus_client_symbol_spec(tag: Tag, ctx: CodegenContext) -> ModbusClientSymbolSpec:
    block_info = ctx.tag_block_addresses.get(tag.name)
    if block_info is None:
        return ModbusClientSymbolSpec(
            symbol=ctx.symbol_for_tag(tag),
            owner=ctx.symbol_table[tag.name],
            tag_type=tag.type.name,
        )
    block_id, _ = block_info
    return ModbusClientSymbolSpec(
        symbol=ctx.symbol_for_tag(tag),
        owner=ctx.block_symbols[block_id],
        tag_type=tag.type.name,
    )


def _modbus_client_operand_tags(operand: Tag | BlockRange) -> tuple[Tag, ...]:
    if isinstance(operand, Tag):
        return (operand,)
    return tuple(operand.tags())


def _modbus_client_spec_for_instruction(
    instr: ModbusSendInstruction | ModbusReceiveInstruction,
    ctx: CodegenContext,
) -> ModbusClientJobSpec:
    existing = ctx.modbus_client_specs_by_instruction.get(id(instr))
    if existing is not None:
        return existing
    if ctx.modbus_client is None:
        raise ValueError(
            f"{type(instr).__name__} requires generate_circuitpy(..., modbus_client=ModbusClientConfig(...))"
        )

    targets = {target.name: target for target in ctx.modbus_client.targets}
    if instr.target_name not in targets:
        raise ValueError(f"Unknown Modbus client target: {instr.target_name!r}")

    if instr.bank is None:
        # Raw ModbusAddress path (TCP only).
        if isinstance(instr.raw_target, ModbusRtuTarget):
            raise ValueError(
                "ModbusRtuTarget is not yet supported for CircuitPython code generation"
            )
        assert instr.remote_address is not None
        ra = instr.remote_address
        is_coil = ra.register_type in (RegisterType.COIL, RegisterType.DISCRETE_INPUT)
        modbus_start = ra.address
        modbus_quantity = instr.register_count
        item_tags = _modbus_client_operand_tags(
            instr.source if isinstance(instr, ModbusSendInstruction) else instr.dest
        )
        item_specs = tuple(_modbus_client_symbol_spec(tag, ctx) for tag in item_tags)
        if isinstance(instr, ModbusSendInstruction):
            function_code = (
                5
                if is_coil and modbus_quantity == 1
                else 15
                if is_coil
                else 6
                if modbus_quantity == 1
                else 16
            )
            busy_tag = instr.sending
        else:
            function_code = (
                2
                if ra.register_type == RegisterType.DISCRETE_INPUT
                else 1
                if is_coil
                else 4
                if ra.register_type == RegisterType.INPUT
                else 3
            )
            busy_tag = instr.receiving
            ctx.mark_helper("_store_copy_value_to_type")

        state_key = ctx.state_key_for(instr)
        spec = ModbusClientJobSpec(
            var_name=f"_mb_client_{state_key}",
            kind="send" if isinstance(instr, ModbusSendInstruction) else "receive",
            target_name=instr.target_name,
            bank=None,
            plc_start=modbus_start,
            modbus_start=modbus_start,
            modbus_quantity=modbus_quantity,
            function_code=function_code,
            item_count=len(item_specs),
            items=item_specs,
            is_coil=is_coil,
            word_order="low_high" if instr.word_swap else "high_low",
            busy=_modbus_client_symbol_spec(busy_tag, ctx),
            success=_modbus_client_symbol_spec(instr.success, ctx),
            error=_modbus_client_symbol_spec(instr.error, ctx),
            exception_response=_modbus_client_symbol_spec(instr.exception_response, ctx),
        )
        ctx.modbus_client_specs_by_instruction[id(instr)] = spec
        ctx.modbus_client_specs.append(spec)
        return spec

    # Click bank-addressed path.
    mapping = MODBUS_MAPPINGS[instr.bank]
    modbus_start, _ = plc_to_modbus(instr.bank, instr.addresses[0])
    modbus_last, modbus_last_width = plc_to_modbus(instr.bank, instr.addresses[-1])
    modbus_quantity = (modbus_last + modbus_last_width) - modbus_start
    item_tags = _modbus_client_operand_tags(
        instr.source if isinstance(instr, ModbusSendInstruction) else instr.dest
    )
    item_specs = tuple(_modbus_client_symbol_spec(tag, ctx) for tag in item_tags)
    if isinstance(instr, ModbusSendInstruction):
        function_code = (
            5
            if mapping.is_coil and len(instr.addresses) == 1
            else 15
            if mapping.is_coil
            else 6
            if modbus_quantity == 1
            else 16
        )
        busy_tag = instr.sending
    else:
        function_code = (
            2
            if mapping.is_coil and 2 in mapping.function_codes
            else 1
            if mapping.is_coil
            else 4
            if 4 in mapping.function_codes
            else 3
        )
        busy_tag = instr.receiving
        ctx.mark_helper("_store_copy_value_to_type")

    state_key = ctx.state_key_for(instr)
    spec = ModbusClientJobSpec(
        var_name=f"_mb_client_{state_key}",
        kind="send" if isinstance(instr, ModbusSendInstruction) else "receive",
        target_name=instr.target_name,
        bank=instr.bank,
        plc_start=instr.start,
        modbus_start=modbus_start,
        modbus_quantity=modbus_quantity,
        function_code=function_code,
        item_count=len(item_specs),
        items=item_specs,
        is_coil=mapping.is_coil,
        busy=_modbus_client_symbol_spec(busy_tag, ctx),
        success=_modbus_client_symbol_spec(instr.success, ctx),
        error=_modbus_client_symbol_spec(instr.error, ctx),
        exception_response=_modbus_client_symbol_spec(instr.exception_response, ctx),
    )
    ctx.modbus_client_specs_by_instruction[id(instr)] = spec
    ctx.modbus_client_specs.append(spec)
    return spec


def _compile_modbus_send_instruction(
    instr: ModbusSendInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    spec = _modbus_client_spec_for_instruction(instr, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, spec.var_name)
    return [f'{" " * indent}{spec.var_name}["enabled"] = bool({enabled_expr})']


def _compile_modbus_receive_instruction(
    instr: ModbusReceiveInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    spec = _modbus_client_spec_for_instruction(instr, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, spec.var_name)
    return [f'{" " * indent}{spec.var_name}["enabled"] = bool({enabled_expr})']
