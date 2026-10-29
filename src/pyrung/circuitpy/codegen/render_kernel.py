"""Kernel function renderer — compiles a Program into a fast step function.

Reuses the same compile_rung() pipeline as the CircuitPy renderer but
wraps the output in a plain-Python function that reads/writes kernel
dicts instead of module-level globals backed by hardware I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _FAULT_ADDRESS_ERROR_TAG,
    _STORE_TYPE_HELPERS,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._helpers import _render_helper_defs, _with_helper_deps
from pyrung.circuitpy.codegen._util import (
    _first_defined_name,
    _global_line,
    _subroutine_symbol,
)
from pyrung.circuitpy.codegen.compile import compile_rungs
from pyrung.circuitpy.codegen.context import CodegenContext, describe_static_range
from pyrung.core.kernel import BlockSpec, CompiledKernel
from pyrung.core.memory_block import Block, BlockRange
from pyrung.core.program import Program
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import Tag


def _collect_materialized_tag_names(
    program: Program,
    ctx: CodegenContext | None = None,
) -> frozenset[str]:
    """Return the set of non-system tag names materialized in the program graph.

    ``BlockRange`` objects intentionally retain their owning ``Block``.  Walking
    into that block would make the result depend on ``Block._tag_cache`` and on
    whether compilation has already expanded static ranges.  For state seeding,
    only concrete ``Tag`` objects that appear in the program graph should count.
    """

    found: set[str] = set()
    visited: set[int] = set()
    queue: list[Any] = []
    queue.extend(program.rungs)
    for subroutine_rungs in program.subroutines.values():
        queue.extend(subroutine_rungs)

    while queue:
        current = queue.pop()
        if current is None:
            continue
        if isinstance(current, Tag):
            if current.name not in SYSTEM_TAGS_BY_NAME:
                found.add(current.name)
            continue
        if isinstance(current, (str, bytes, bytearray, int, float, bool)):
            continue
        if isinstance(current, Block):
            continue
        if type(current) is BlockRange:
            continue
        if isinstance(current, BlockRange):
            layout = (
                ctx.static_range_layout(current)
                if ctx is not None
                else describe_static_range(current)
            )
            if not layout.uses_backing_storage:
                queue.extend(layout.tags)
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        if isinstance(current, Mapping):
            queue.extend(current.keys())
            queue.extend(current.values())
            continue
        if isinstance(current, tuple | list | set | frozenset):
            queue.extend(current)
            continue

        if hasattr(current, "__dict__"):
            queue.extend(vars(current).values())
            continue
        if hasattr(current, "__slots__"):
            for slot in current.__slots__:
                if slot in {"__weakref__", "__dict__"}:
                    continue
                if hasattr(current, slot):
                    queue.append(getattr(current, slot))

    return frozenset(found)


def compile_kernel(
    program: Program,
    *,
    force_rung_enable: bool = False,
    blockless: bool = False,
    proof_metadata: bool = False,
    split_after: int | None = None,
) -> CompiledKernel:
    """Compile a Program into a fast in-process replay kernel.

    ``split_after`` (a rung count) emits a second ``pre_step_fn`` covering the
    leading rungs — the synthesis ``plant`` pass — so the caller can drain inputs
    *between* the two passes (the plant reads the previous commit, the input-read
    phase).  ``None`` ⇒ a single ``step_fn`` for the whole program.
    """
    ctx = CodegenContext.for_kernel(
        program,
        force_rung_enable=force_rung_enable,
        blockless=blockless,
        proof_metadata=proof_metadata,
    )
    source = _render_kernel_source(ctx, split_after=split_after)

    namespace: dict[str, Any] = {}
    exec(compile(source, "<kernel>", "exec"), namespace)  # noqa: S102
    step_fn = namespace["_kernel_step"]
    pre_step_fn = namespace.get("_kernel_step_pre")

    indirect_block_info: dict[str, tuple[str, int, int, frozenset[int]]] = {}
    for block_id in ctx.used_indirect_blocks:
        symbol = ctx.block_symbols[block_id]
        binding = ctx.block_bindings[block_id]
        static_addrs = frozenset(
            addr for _name, (bid, addr) in ctx.tag_block_addresses.items() if bid == block_id
        )
        indirect_block_info[symbol] = (
            binding.block.name,
            binding.start,
            binding.end,
            static_addrs,
        )

    block_specs = _build_block_specs(ctx)

    return CompiledKernel(
        step_fn=step_fn,
        pre_step_fn=pre_step_fn,
        referenced_tags=dict(ctx.referenced_tags),
        block_specs=block_specs,
        edge_tags=set(ctx.edge_prev_tags),
        source=source,
        blockless=blockless,
        has_io_gaps=ctx.has_io_gaps,
        indirect_block_info=indirect_block_info,
        materialized_tag_names=_collect_materialized_tag_names(program, ctx),
    )


def _build_block_specs(ctx: CodegenContext) -> dict[str, BlockSpec]:
    specs: dict[str, BlockSpec] = {}
    for binding in sorted(
        ctx.emitted_bindings(),
        key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
    ):
        symbol = ctx.block_symbols[binding.block_id]
        compact = ctx.compact_block_map.get(binding.block_id)
        addresses = (
            sorted(compact) if compact is not None else list(range(binding.start, binding.end + 1))
        )
        tags = [binding.block._get_tag(addr) for addr in addresses]
        tag_names = [t.name for t in tags]
        if not tag_names:
            continue
        for tag in tags:
            ctx.referenced_tags.setdefault(tag.name, tag)
        specs[symbol] = BlockSpec(
            symbol=symbol,
            size=len(tag_names),
            default=_TYPE_DEFAULTS[binding.tag_type],
            tag_type=binding.tag_type,
            tag_names=tuple(tag_names),
        )
    return specs


def _render_kernel_source(ctx: CodegenContext, *, split_after: int | None = None) -> str:
    sub_fn_lines = _compile_subroutines(ctx)
    rungs = ctx.program.rungs
    if split_after is None:
        pre_body: list[str] | None = None
        main_body = _compile_rung_body(ctx, rungs, "_kernel_step")
    else:
        # Split the scan into a leading ``plant`` pre-pass and the main pass.  Both
        # compile against the *same* ctx (symbols already assigned over the whole
        # program in ``for_kernel``), so referenced tags / blocks / edge tags are
        # the correct union; only the rung bodies differ.  Each renders its own
        # load→run→flush, so a drain between the two passes is visible to the main
        # pass (it re-reads the tags dict).
        pre_body = _compile_rung_body(ctx, rungs[:split_after], "_kernel_step_pre")
        main_body = _compile_rung_body(ctx, rungs[split_after:], "_kernel_step")

    lines: list[str] = []
    lines.extend(_render_imports(ctx))
    lines.extend(_render_helper_defs(_with_helper_deps(ctx.used_helpers)))
    lines.extend(_render_indirect_helpers(ctx))
    lines.extend(_render_embedded_functions(ctx))
    lines.extend(_render_declarations(ctx))
    lines.extend(sub_fn_lines)
    if pre_body is not None:
        lines.extend(_render_step_function(ctx, pre_body, fn_name="_kernel_step_pre"))
    lines.extend(_render_step_function(ctx, main_body))
    return "\n".join(lines).rstrip() + "\n"


# -- Compilation (populates ctx side-effects) --------------------------------


def _compile_subroutines(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for sub_name in ctx.subroutine_names:
        fn_name = _subroutine_symbol(sub_name)
        ctx.function_globals[fn_name] = set()
        ctx.set_current_function(fn_name)
        body = compile_rungs(ctx.program.subroutines[sub_name], fn_name, ctx, indent=4)
        ctx.set_current_function(None)
        globals_needed = ctx.globals_for_function(fn_name)
        if ctx.blockless:
            globals_needed = [name for name in globals_needed if name not in {"_mem", "_prev"}]
        globals_line = _global_line(globals_needed, indent=4)
        if ctx.blockless:
            lines.append(f"def {fn_name}(tags, _mem, _prev, dt):")
        elif ctx.kernel_runtime:
            lines.append(f"def {fn_name}(tags):")
        else:
            lines.append(f"def {fn_name}():")
        if globals_line is not None:
            lines.append(globals_line)
        if body:
            lines.extend(body)
        else:
            lines.append("    pass")
        lines.append("")
    return lines


def _compile_rung_body(ctx: CodegenContext, rungs: list[Any], fn_name: str) -> list[str]:
    ctx.function_globals[fn_name] = set()
    ctx.set_current_function(fn_name)
    body = compile_rungs(rungs, fn_name, ctx, indent=4)
    ctx.set_current_function(None)
    return body


# -- Source rendering ---------------------------------------------------------


def _render_imports(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    needs_math = (
        "_render_text_from_numeric" in ctx.used_helpers
        or "_parse_pack_text_value" in ctx.used_helpers
        or "_store_copy_value_to_type" in ctx.used_helpers
        or "_calc_math_isfinite" in ctx.used_helpers
        or "_math_func" in ctx.used_helpers
        or bool(ctx.used_helpers & set(_STORE_TYPE_HELPERS.values()))
    )
    needs_struct = (
        "_int_to_float_bits" in ctx.used_helpers
        or "_float_to_int_bits" in ctx.used_helpers
        or "_parse_pack_text_value" in ctx.used_helpers
    )
    needs_re = "_parse_pack_text_value" in ctx.used_helpers
    if needs_math:
        lines.append("import math")
    if needs_re:
        lines.append("import re")
    if needs_struct:
        lines.append("import struct")
    if lines:
        lines.append("")
    return lines


def _render_indirect_helpers(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    fault_sym = ctx.symbol_if_referenced(_FAULT_ADDRESS_ERROR_TAG)
    for binding in sorted(
        (ctx.block_bindings[bid] for bid in ctx.used_indirect_blocks),
        key=lambda b: ctx.index_helper_name(b.block_id),
    ):
        helper_name = ctx.index_helper_name(binding.block_id)
        lines.append(f"def {helper_name}(addr):")
        if fault_sym is not None:
            lines.append(f"    global {fault_sym}")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        if fault_sym is not None:
            lines.append(f"        {fault_sym} = True")
        lines.append(
            f'        raise IndexError(f"Address {{addr}} out of range'
            f" for {binding.logical_name} ({binding.start}-{binding.end})"
            '")'
        )
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            if fault_sym is not None:
                lines.append(f"        {fault_sym} = True")
            lines.append(
                f'        raise IndexError(f"Address {{addr}} out of range'
                f" for {binding.logical_name} ({binding.start}-{binding.end})"
                '")'
            )
        lines.append(f"    return int(addr) - {binding.start}")
        lines.append("")
    return lines


def _render_embedded_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for symbol in sorted(ctx.function_sources):
        src = ctx.function_sources[symbol].rstrip()
        lines.append(src)
        fn_name = _first_defined_name(src)
        if fn_name is not None and fn_name != symbol:
            lines.append(f"{symbol} = {fn_name}")
        lines.append("")
    return lines


def _render_declarations(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for tag_name in sorted(ctx.scalar_tags):
        tag = ctx.scalar_tags[tag_name]
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"{symbol} = {tag.default!r}")

    if ctx.blockless:
        for binding in sorted(
            ctx.emitted_bindings(),
            key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
        ):
            tag_names = ctx.block_layout_tag_names(binding.block_id)
            if not tag_names:
                continue
            lines.append(f"{ctx.block_name_tuple_symbol(binding.block_id)} = {tag_names!r}")
    else:
        for binding in sorted(
            ctx.emitted_bindings(),
            key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
        ):
            compact = ctx.compact_block_map.get(binding.block_id)
            if compact is not None:
                size = len(compact)
            else:
                size = binding.end - binding.start + 1
            if size == 0:
                continue
            symbol = ctx.block_symbols[binding.block_id]
            default = _TYPE_DEFAULTS[binding.tag_type]
            lines.append(f"{symbol} = [{default!r}] * {size}")

    lines.extend(["_mem = {}", "_prev = {}", ""])
    return lines


def _render_step_function(
    ctx: CodegenContext, main_body: list[str], *, fn_name: str = "_kernel_step"
) -> list[str]:
    all_symbols: set[str] = set()
    scalar_symbols = sorted(ctx.symbol_table[n] for n in ctx.scalar_tags)
    all_symbols.update(scalar_symbols)
    all_symbols.update({"_mem", "_prev"})

    active_block_bindings = []
    for binding in sorted(
        ctx.emitted_bindings(),
        key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
    ):
        if ctx.blockless:
            tag_names = ctx.block_layout_tag_names(binding.block_id)
            if tag_names:
                all_symbols.add(ctx.block_name_tuple_symbol(binding.block_id))
            continue
        compact = ctx.compact_block_map.get(binding.block_id)
        if compact is not None and len(compact) == 0:
            continue
        active_block_bindings.append(binding)
        all_symbols.add(ctx.block_symbols[binding.block_id])

    lines: list[str] = [f"def {fn_name}(tags, blocks, memory, prev, dt):"]
    globals_line = _global_line(sorted(all_symbols), indent=4)
    if globals_line is not None:
        lines.append(globals_line)

    for tag_name in sorted(ctx.scalar_tags):
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"    {symbol} = tags[{tag_name!r}]")

    if not ctx.blockless:
        for binding in active_block_bindings:
            symbol = ctx.block_symbols[binding.block_id]
            lines.append(f"    {symbol} = blocks[{symbol!r}]")

    lines.extend(
        [
            "    _mem = memory",
            "    _prev = prev",
            '    memory["_dt"] = dt',
        ]
    )

    if main_body:
        lines.extend(main_body)
    else:
        lines.append("    pass")

    for tag_name in sorted(ctx.scalar_tags):
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"    tags[{tag_name!r}] = {symbol}")

    lines.append("")
    return lines
