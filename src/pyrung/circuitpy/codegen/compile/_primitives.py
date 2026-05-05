"""Automatically generated module split."""

from __future__ import annotations

from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _FAULT_ADDRESS_ERROR_TAG,
    _FAULT_OUT_OF_RANGE_TAG,
    _INT_MAX,
    _INT_MIN,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _indent_body,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
from pyrung.core.expression import (
    BinaryExpr,
    Expression,
    LiteralExpr,
    MathFuncExpr,
    ShiftFuncExpr,
    SumExpr,
    TagExpr,
    UnaryExpr,
)
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.time_mode import TimeUnit


def _snapshot_tag_symbol(
    tag: Tag | ImmediateRef,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    if isinstance(tag, ImmediateRef):
        tag = tag.tag

    block_info = ctx.tag_block_addresses.get(tag.name)
    if block_info is not None and block_snapshots is not None:
        block_id, addr = block_info
        block_symbol = block_snapshots.get(block_id)
        if block_symbol is not None:
            binding = ctx.block_bindings.get(block_id)
            if binding is None:
                raise RuntimeError(f"Missing block binding for condition tag {tag.name!r}")
            return f"{block_symbol}[{ctx.block_index(block_id, addr)}]"

    if scalar_snapshots is not None:
        snapshot_symbol = scalar_snapshots.get(tag.name)
        if snapshot_symbol is not None:
            return snapshot_symbol

    if block_info is not None and ctx.blockless:
        return f"tags.get({tag.name!r}, {tag.default!r})"

    return ctx.symbol_for_tag(tag)


def _block_name_expr(
    block_id: int,
    index_expr: str,
    ctx: CodegenContext,
) -> str:
    return f"{ctx.block_name_tuple_symbol(block_id)}[{index_expr}]"


def _range_item_read_expr(
    range_value: BlockRange | IndirectBlockRange,
    symbol: str,
    key_expr: str,
    ctx: CodegenContext,
) -> str:
    if not ctx.blockless:
        return f"{symbol}[{key_expr}]"
    binding = ctx.block_bindings[id(range_value.block)]
    default = _TYPE_DEFAULTS[binding.tag_type]
    return f"{symbol}.get({key_expr}, {default!r})"


def _range_item_write_expr(symbol: str, key_expr: str, value_expr: str) -> str:
    return f"{symbol}[{key_expr}] = {value_expr}"


def _compile_expression_impl(
    expr: Expression,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    """Return a Python expression string with explicit parentheses."""
    if isinstance(expr, TagExpr):
        return _snapshot_tag_symbol(
            expr.tag,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(expr, LiteralExpr):
        return repr(expr.value)

    if isinstance(expr, BinaryExpr):
        left = _compile_expression_impl(
            expr.left,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        right = _compile_expression_impl(
            expr.right,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        if expr.symbol in _BITWISE_SYMBOLS:
            return f"(int({left}) {expr.symbol} int({right}))"
        return f"({left} {expr.symbol} {right})"

    if isinstance(expr, UnaryExpr):
        inner = _compile_expression_impl(
            expr.operand,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        if expr.symbol == "abs":
            return f"abs({inner})"
        if expr.symbol == "~":
            return f"(~int({inner}))"
        return f"({expr.symbol}({inner}))"

    if isinstance(expr, MathFuncExpr):
        if expr.name not in _ALLOWED_MATH_FUNCS:
            raise TypeError(f"Unsupported expression type: {type(expr).__name__}")
        return (
            f"math.{expr.name}("
            f"{_compile_expression_impl(expr.operand, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            ")"
        )

    if isinstance(expr, ShiftFuncExpr):
        value = _compile_expression_impl(
            expr.value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        count = _compile_expression_impl(
            expr.count,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        if expr.name == "lsh":
            return f"(int({value}) << int({count}))"
        if expr.name == "rsh":
            return f"(int({value}) >> int({count}))"
        if expr.name == "lro":
            return (
                f"((((int({value}) & 0xFFFF) << (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) >> (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        if expr.name == "rro":
            return (
                f"((((int({value}) & 0xFFFF) >> (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) << (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        raise TypeError(f"Unsupported expression type: {type(expr).__name__}")

    if isinstance(expr, SumExpr):
        terms = [
            _snapshot_tag_symbol(
                tag,
                ctx,
                scalar_snapshots=scalar_snapshots,
                block_snapshots=block_snapshots,
            )
            for tag in expr.block_range
        ]
        return f"({' + '.join(terms)})"

    raise TypeError(f"Unsupported expression type: {type(expr).__name__}")


_BITWISE_SYMBOLS = frozenset({"&", "|", "^", "<<", ">>"})

_ALLOWED_MATH_FUNCS = frozenset(
    {
        "sqrt",
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "radians",
        "degrees",
        "log10",
        "log",
    }
)


def _copy_converter_target_info(
    target: Tag | IndirectRef | IndirectExprRef,
    ctx: CodegenContext,
    stem: str,
) -> tuple[list[str], str, str, str | None, str]:
    if isinstance(target, Tag):
        block_info = ctx.tag_block_addresses.get(target.name)
        if block_info is None or ctx.blockless:
            return [], "scalar", _compile_lvalue(target, ctx), None, target.type.name
        block_id, addr = block_info
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for tag-backed target {target.name!r}")
        symbol = ctx.symbol_for_block(binding.block)
        start_var = f"_{stem}_start_idx"
        return (
            [f"{start_var} = {ctx.block_index(block_id, addr)}"],
            "block",
            symbol,
            start_var,
            target.type.name,
        )

    if isinstance(target, IndirectRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        ptr = _compile_value(target.pointer, ctx)
        if ctx.blockless:
            return (
                [f"{start_var} = {helper}(int({ptr}))"],
                "blockless_dynamic",
                ctx.block_name_tuple_symbol(binding.block_id),
                start_var,
                binding.tag_type.name,
            )
        return (
            [f"{start_var} = {helper}(int({ptr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    if isinstance(target, IndirectExprRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        expr = _compile_expression_impl(target.expr, ctx)
        if ctx.blockless:
            return (
                [f"{start_var} = {helper}(int({expr}))"],
                "blockless_dynamic",
                ctx.block_name_tuple_symbol(binding.block_id),
                start_var,
                binding.tag_type.name,
            )
        return (
            [f"{start_var} = {helper}(int({expr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    raise TypeError(f"Unsupported copy modifier target type: {type(target).__name__}")


def _copy_converter_write_lines(
    *,
    values_var: str,
    target_kind: str,
    target_symbol: str,
    target_start_var: str | None,
    fault_body: list[str],
) -> list[str]:
    if target_kind == "scalar":
        return [
            f"if len({values_var}) > 1:",
            *_indent_body(fault_body, 4),
            f"elif len({values_var}) == 1:",
            f"    {target_symbol} = {values_var}[0]",
        ]

    if target_kind == "blockless_dynamic":
        if target_start_var is None:
            raise RuntimeError("blockless dynamic target is missing start index")
        return [
            f"_copy_count = len({values_var})",
            "if _copy_count == 0:",
            "    pass",
            f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
            *_indent_body(fault_body, 4),
            "else:",
            f"    for _copy_offset, _copy_value in enumerate({values_var}):",
            f"        tags[{target_symbol}[{target_start_var} + _copy_offset]] = _copy_value",
        ]

    if target_start_var is None:
        raise RuntimeError("copy modifier block target is missing start index")
    return [
        f"_copy_count = len({values_var})",
        "if _copy_count == 0:",
        "    pass",
        f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
        *_indent_body(fault_body, 4),
        "else:",
        f"    for _copy_offset, _copy_value in enumerate({values_var}):",
        f"        {target_symbol}[{target_start_var} + _copy_offset] = _copy_value",
    ]


def _compile_guarded_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    enabled_body: list[str],
    *,
    disabled_body: list[str] | None = None,
) -> list[str]:
    sp = " " * indent
    lines: list[str] = []
    if ctx.used_indirect_blocks:
        enabled_body = [
            "try:",
            *[f"    {line}" for line in enabled_body],
            "except IndexError:",
            "    pass",
        ]
    enabled_literal = _bool_literal(enabled_expr)
    if getattr(instr, "oneshot", False):
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            if disabled_body:
                lines.extend(f"{sp}{line}" for line in disabled_body)
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        if disabled_body:
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        lines.extend(f"{sp}{line}" for line in enabled_body)
        return lines
    if enabled_literal is False:
        if disabled_body is not None:
            lines.extend(f"{sp}{line}" for line in disabled_body)
        return lines

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
    if disabled_body is not None:
        lines.append(f"{sp}else:")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
    return lines


def _compile_assignment_lines(
    target: Tag | IndirectRef | IndirectExprRef,
    value_expr: str,
    ctx: CodegenContext,
    *,
    indent: int,
) -> list[str]:
    lvalue = _compile_lvalue(target, ctx)
    return [f"{' ' * indent}{lvalue} = {value_expr}"]


def _compile_lvalue(target: Tag | IndirectRef | IndirectExprRef, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        if ctx.blockless and target.name in ctx.tag_block_addresses:
            return f"tags[{target.name!r}]"
        return ctx.symbol_for_tag(target)
    if isinstance(target, IndirectRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        helper = ctx.use_indirect_block(binding.block_id)
        ptr = _compile_value(target.pointer, ctx)
        index_expr = f"{helper}(int({ptr}))"
        if ctx.blockless:
            return f"tags[{_block_name_expr(binding.block_id, index_expr, ctx)}]"
        block_symbol = ctx.symbol_for_block(target.block)
        return f"{block_symbol}[{index_expr}]"
    if isinstance(target, IndirectExprRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        helper = ctx.use_indirect_block(binding.block_id)
        expr = _compile_expression_impl(target.expr, ctx)
        index_expr = f"{helper}(int({expr}))"
        if ctx.blockless:
            return f"tags[{_block_name_expr(binding.block_id, index_expr, ctx)}]"
        block_symbol = ctx.symbol_for_block(target.block)
        return f"{block_symbol}[{index_expr}]"
    raise TypeError(f"Unsupported assignment target: {type(target).__name__}")


def _compile_range_setup(
    range_value: Any,
    ctx: CodegenContext,
    *,
    stem: str,
    include_addresses: bool,
) -> tuple[list[str], str, str, str]:
    if not isinstance(range_value, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}"
        )
    binding = ctx.block_bindings[id(range_value.block)]
    symbol = "tags" if ctx.blockless else ctx.symbol_for_block(range_value.block)
    if isinstance(range_value, BlockRange):
        name = ctx.next_name(stem)
        indices_var = f"_{name}_{'names' if ctx.blockless else 'indices'}"
        addrs_var = f"_{name}_addrs"
        addresses = [int(addr) for addr in range_value.addresses]
        if ctx.blockless:
            indices_expr = repr(tuple(binding.block._get_tag(addr).name for addr in addresses))
        else:
            indices = [ctx.block_index(binding.block_id, addr) for addr in addresses]
            indices_expr = _sequence_expr(indices)
        lines = [f"{indices_var} = {indices_expr}"]
        if include_addresses:
            lines.append(f"{addrs_var} = {_sequence_expr(addresses)}")
        else:
            addrs_var = "[]"
        return lines, symbol, indices_var, addrs_var

    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name(stem)
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_{'name' if ctx.blockless else 'idx'}"
    indices_var = f"_{name}_{'names' if ctx.blockless else 'indices'}"
    addrs_var = f"_{name}_addrs"
    start_expr = _compile_address_expr(range_value.start_expr, ctx)
    end_expr = _compile_address_expr(range_value.end_expr, ctx)
    lines = [
        f"{start_var} = int({start_expr})",
        f"{end_var} = int({end_expr})",
        f"if {start_var} > {end_var}:",
        '    raise ValueError("Indirect range start must be <= end")',
        f"{indices_var} = []",
        f"for {addr_var} in range({start_var}, {end_var} + 1):",
    ]
    if include_addresses:
        lines.append(f"{addrs_var} = []")
    else:
        addrs_var = "[]"
    resolved_expr = f"{helper}(int({addr_var}))"
    if ctx.blockless:
        name_expr = _block_name_expr(binding.block_id, resolved_expr, ctx)
        lines.append(f"    {idx_var} = {name_expr}")
    else:
        lines.append(f"    {idx_var} = {resolved_expr}")
    lines.append(f"    {indices_var}.append({idx_var})")
    if include_addresses:
        lines.append(f"    {addrs_var}.append(int({addr_var}))")
    if range_value.reverse_order:
        lines.append(f"{indices_var}.reverse()")
        if include_addresses:
            lines.append(f"{addrs_var}.reverse()")
    return lines, symbol, indices_var, addrs_var


def _sequence_expr(values: list[int]) -> str:
    if not values:
        return "[]"
    if len(values) == 1:
        return repr(range(values[0], values[0] + 1))

    step = values[1] - values[0]
    if step != 0 and all(values[i + 1] - values[i] == step for i in range(len(values) - 1)):
        return repr(range(values[0], values[-1] + step, step))
    return repr(values)


def _search_compare_expr(condition: str, left_expr: str, right_expr: str) -> str:
    if condition == "==":
        return f"({left_expr} == {right_expr})"
    if condition == "!=":
        return f"({left_expr} != {right_expr})"
    if condition == "<":
        return f"({left_expr} < {right_expr})"
    if condition == "<=":
        return f"({left_expr} <= {right_expr})"
    if condition == ">":
        return f"({left_expr} > {right_expr})"
    if condition == ">=":
        return f"({left_expr} >= {right_expr})"
    raise ValueError(f"Unsupported search comparison: {condition!r}")


def _pack_store_expr(value_expr: str, dest_type: str, ctx: CodegenContext) -> str:
    if dest_type == "REAL":
        ctx.mark_helper("_int_to_float_bits")
        return f"_int_to_float_bits({value_expr})"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    raise TypeError(f"Unsupported pack destination type: {dest_type}")


def _calc_store_expr(value_expr: str, dest_type: str, mode: str, ctx: CodegenContext) -> str:
    if mode == "hex":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "BOOL":
        return f"bool({value_expr})"
    if dest_type == "REAL":
        return f"float({value_expr})"
    if dest_type == "CHAR":
        return value_expr
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    return value_expr


def _timer_dt_to_units_expr(unit: TimeUnit, dt_expr: str, frac_expr: str) -> str:
    if unit == TimeUnit.Tms:
        return f"(({dt_expr} * 1000.0) + {frac_expr})"
    if unit == TimeUnit.Ts:
        return f"(({dt_expr}) + {frac_expr})"
    if unit == TimeUnit.Tm:
        return f"(({dt_expr} / 60.0) + {frac_expr})"
    if unit == TimeUnit.Th:
        return f"(({dt_expr} / 3600.0) + {frac_expr})"
    if unit == TimeUnit.Td:
        return f"(({dt_expr} / 86400.0) + {frac_expr})"
    raise ValueError(f"Unsupported timer unit: {unit}")


def _load_cast_expr(value_expr: str, tag_type: str) -> str:
    if tag_type == "BOOL":
        return f"bool({value_expr})"
    if tag_type == "INT":
        return f"max({_INT_MIN}, min({_INT_MAX}, int({value_expr})))"
    if tag_type == "DINT":
        return f"max({_DINT_MIN}, min({_DINT_MAX}, int({value_expr})))"
    if tag_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if tag_type == "REAL":
        return f"float({value_expr})"
    if tag_type == "CHAR":
        return f"({value_expr} if isinstance({value_expr}, str) else '')"
    return value_expr


def _compile_set_out_of_range_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_OUT_OF_RANGE_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _compile_set_address_error_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_ADDRESS_ERROR_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _compile_target_write_lines(
    target: Tag | BlockRange | IndirectBlockRange | ImmediateRef,
    value_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    if isinstance(target, ImmediateRef):
        return _compile_target_write_lines(target.value, value_expr, ctx, indent)
    if isinstance(target, Tag):
        return [f"{sp}{_compile_lvalue(target, ctx)} = {value_expr}"]

    if ctx.blockless:
        setup, symbol, indices_var, _ = _compile_range_setup(
            target,
            ctx,
            stem="targetwrite",
            include_addresses=False,
        )
        lines = [f"{sp}{line}" for line in setup]
        lines.extend(
            [
                f"{sp}for _target_name in {indices_var}:",
                f"{sp}    {_range_item_write_expr(symbol, '_target_name', value_expr)}",
            ]
        )
        return lines

    if isinstance(target, BlockRange):
        binding = ctx.block_bindings[id(target.block)]
        symbol = ctx.symbol_for_block(target.block)
        lines: list[str] = []
        for addr in target.addresses:
            index = ctx.block_index(binding.block_id, addr)
            lines.append(f"{sp}{symbol}[{index}] = {value_expr}")
        return lines if lines else [f"{sp}pass"]

    binding = ctx.block_bindings[id(target.block)]
    symbol = ctx.symbol_for_block(target.block)
    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name("irange")
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_list = f"_{name}_indices"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    start_expr = _compile_address_expr(target.start_expr, ctx)
    end_expr = _compile_address_expr(target.end_expr, ctx)
    lines = [
        f"{sp}{start_var} = int({start_expr})",
        f"{sp}{end_var} = int({end_expr})",
        f"{sp}if {start_var} > {end_var}:",
        f'{sp}    raise ValueError("Indirect range start must be <= end")',
        f"{sp}{addr_list} = []",
        f"{sp}for {addr_var} in range({start_var}, {end_var} + 1):",
        f"{sp}    {idx_var} = {helper}(int({addr_var}))",
        f"{sp}    {addr_list}.append({idx_var})",
    ]
    if target.reverse_order:
        lines.append(f"{sp}{addr_list}.reverse()")
    lines.extend(
        [
            f"{sp}for {idx_var} in {addr_list}:",
            f"{sp}    {symbol}[{idx_var}] = {value_expr}",
        ]
    )
    return lines


def _compile_address_expr(
    addr: int | Tag | Any,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    if isinstance(addr, int):
        return repr(addr)
    if isinstance(addr, Tag):
        return _snapshot_tag_symbol(
            addr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(addr, Expression):
        return _compile_expression_impl(
            addr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    raise TypeError(f"Unsupported indirect address expression type: {type(addr).__name__}")


def _compile_indirect_value(
    indirect_ref: IndirectRef,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    block_id = id(indirect_ref.block)
    binding = ctx.block_bindings.get(block_id)
    if binding is None:
        raise RuntimeError(f"Missing block binding for indirect ref {indirect_ref.block.name!r}")
    helper = ctx.use_indirect_block(binding.block_id)
    ptr = _compile_value(
        indirect_ref.pointer,
        ctx,
        scalar_snapshots=scalar_snapshots,
        block_snapshots=block_snapshots,
    )
    index_expr = f"{helper}(int({ptr}))"
    if block_snapshots is not None and block_id in block_snapshots:
        block_symbol = block_snapshots[block_id]
        return f"{block_symbol}[{index_expr}]"
    if ctx.blockless:
        name_expr = _block_name_expr(binding.block_id, index_expr, ctx)
        default = _TYPE_DEFAULTS[binding.tag_type]
        return f"tags.get({name_expr}, {default!r})"
    block_symbol = ctx.symbol_for_block(indirect_ref.block)
    return f"{block_symbol}[{index_expr}]"


def _compile_value(
    value: Any,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    if isinstance(value, Tag):
        return _snapshot_tag_symbol(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(value, IndirectRef):
        return _compile_indirect_value(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(value, IndirectExprRef):
        block_id = id(value.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression ref {value.block.name!r}"
            )
        helper = ctx.use_indirect_block(binding.block_id)
        expr = _compile_expression_impl(
            value.expr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        index_expr = f"{helper}(int({expr}))"
        if block_snapshots is not None and block_id in block_snapshots:
            block_symbol = block_snapshots[block_id]
            return f"{block_symbol}[{index_expr}]"
        if ctx.blockless:
            name_expr = _block_name_expr(binding.block_id, index_expr, ctx)
            default = _TYPE_DEFAULTS[binding.tag_type]
            return f"tags.get({name_expr}, {default!r})"
        block_symbol = ctx.symbol_for_block(value.block)
        return f"{block_symbol}[{index_expr}]"
    if isinstance(value, Expression):
        return _compile_expression_impl(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    return repr(value)
