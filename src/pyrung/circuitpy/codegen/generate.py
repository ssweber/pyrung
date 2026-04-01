"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import math as _math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyrung.circuitpy.codegen._constants import _TYPE_DEFAULTS
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.circuitpy.codegen.render import _render_code
from pyrung.circuitpy.codegen.render_runtime import _render_runtime
from pyrung.circuitpy.hardware import P1AM
from pyrung.circuitpy.modbus import ModbusClientConfig, ModbusServerConfig
from pyrung.circuitpy.p1am import RunStopConfig, board
from pyrung.circuitpy.validation import validate_circuitpy_program
from pyrung.click.tag_map import TagMap
from pyrung.core.memory_block import Block
from pyrung.core.program import Program
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME, system
from pyrung.core.tag import Tag

MappedTagScope = Literal["referenced_only", "all_mapped"]


@dataclass(frozen=True)
class CircuitPyOutput:
    """Result of :func:`generate_circuitpy`.

    *code* is the ``code.py`` content (program-specific, stays as ``.py``).
    *runtime* is the ``pyrung_rt.py`` content (generic runtime library,
    intended to be compiled to ``.mpy`` via ``mpy-cross``).  An empty
    string means no runtime module is needed.
    """

    code: str
    runtime: str


def _needs_modbus_backing(tag: Tag, mode: MappedTagScope) -> bool:
    if mode == "all_mapped":
        return True
    return tag.default != _TYPE_DEFAULTS[tag.type]


def generate_circuitpy(
    program: Program,
    hw: P1AM,
    *,
    target_scan_ms: float,
    watchdog_ms: int | None = None,
    runstop: RunStopConfig | None = None,
    modbus_server: ModbusServerConfig | None = None,
    modbus_client: ModbusClientConfig | None = None,
    tag_map: TagMap | None = None,
    mapped_tag_scope: MappedTagScope = "referenced_only",
) -> CircuitPyOutput:
    if not isinstance(program, Program):
        raise TypeError(f"program must be Program, got {type(program).__name__}")
    if not isinstance(hw, P1AM):
        raise TypeError(f"hw must be P1AM, got {type(hw).__name__}")
    if not isinstance(target_scan_ms, (int, float)):
        raise TypeError(
            f"target_scan_ms must be a finite number > 0, got {type(target_scan_ms).__name__}"
        )
    if not _math.isfinite(float(target_scan_ms)) or float(target_scan_ms) <= 0:
        raise ValueError("target_scan_ms must be finite and > 0")
    if watchdog_ms is not None:
        if not isinstance(watchdog_ms, int):
            raise TypeError(f"watchdog_ms must be int or None, got {type(watchdog_ms).__name__}")
        if watchdog_ms < 0:
            raise ValueError("watchdog_ms must be >= 0")
    if runstop is not None and not isinstance(runstop, RunStopConfig):
        raise TypeError(f"runstop must be RunStopConfig or None, got {type(runstop).__name__}")
    if modbus_server is not None and not isinstance(modbus_server, ModbusServerConfig):
        raise TypeError(
            f"modbus_server must be ModbusServerConfig or None, got {type(modbus_server).__name__}"
        )
    if modbus_client is not None and not isinstance(modbus_client, ModbusClientConfig):
        raise TypeError(
            f"modbus_client must be ModbusClientConfig or None, got {type(modbus_client).__name__}"
        )
    if tag_map is not None and not isinstance(tag_map, TagMap):
        raise TypeError(f"tag_map must be TagMap or None, got {type(tag_map).__name__}")
    if mapped_tag_scope not in {"referenced_only", "all_mapped"}:
        raise ValueError("mapped_tag_scope must be 'referenced_only' or 'all_mapped'")
    if (modbus_server is not None or modbus_client is not None) and tag_map is None:
        raise ValueError("tag_map is required when modbus_server or modbus_client is enabled")

    ctx = CodegenContext(
        program=program,
        hw=hw,
        target_scan_ms=float(target_scan_ms),
        watchdog_ms=watchdog_ms,
        modbus_server=modbus_server,
        modbus_client=modbus_client,
        tag_map=tag_map,
    )
    ctx.collect_hw_bindings()
    ctx.collect_program_references()
    ctx.runstop = runstop
    if runstop is not None:
        ctx.ensure_tag_referenced(board.switch)
        if runstop.expose_mode_tags:
            ctx.ensure_tag_referenced(system.sys.mode_run)
            ctx.ensure_tag_referenced(system.sys.cmd_mode_stop)
    if tag_map is not None:
        for entry in tag_map.entries:
            logical = getattr(entry, "logical", None)
            if isinstance(logical, Tag):
                if _needs_modbus_backing(logical, mapped_tag_scope):
                    ctx.ensure_tag_referenced(logical)
                continue
            if isinstance(logical, Block):
                ctx._ensure_block_binding(logical)
                for addr in getattr(entry, "logical_addresses", ()):
                    tag = logical[addr]
                    if _needs_modbus_backing(tag, mapped_tag_scope):
                        ctx.ensure_tag_referenced(tag)
        if mapped_tag_scope == "all_mapped":
            for slot in tag_map.mapped_slots():
                if slot.source != "system":
                    continue
                system_tag = SYSTEM_TAGS_BY_NAME.get(slot.logical_name)
                if system_tag is not None:
                    ctx.ensure_tag_referenced(system_tag)

    if not hw._slots and not ctx.board_tag_names:
        raise ValueError(
            "P1AM hardware config must include at least one configured slot or referenced board tags"
        )

    if hw._slots:
        slot_numbers = sorted(hw._slots)
        expected = list(range(1, slot_numbers[-1] + 1))
        if slot_numbers != expected:
            raise ValueError(
                "Configured slots must be contiguous from 1..N for v1 roll-call generation"
            )

    report = validate_circuitpy_program(program, hw=hw, mode="strict")
    if report.errors:
        lines = [f"{len(report.errors)} error(s)."]
        for err in report.errors:
            lines.append(f"{err.code} @ {err.location}: {err.message}")
        raise ValueError("\n".join(lines))

    ctx.collect_retentive_tags()
    ctx.assign_symbols()

    # Predict has_runtime from config — modbus requires a runtime module.
    # Render code first so compile_rung populates ctx.used_helpers and
    # ctx.modbus_client_specs; _render_runtime needs those.
    has_runtime = ctx.modbus_server is not None or ctx.modbus_client is not None
    source = _render_code(ctx, has_runtime=has_runtime)
    runtime_source = _render_runtime(ctx) if has_runtime else ""

    if runtime_source:
        try:
            compile(runtime_source, "pyrung_rt.py", "exec")
        except SyntaxError as exc:
            raise RuntimeError(f"Generated runtime source is invalid: {exc}") from exc
    try:
        compile(source, "code.py", "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"Generated source is invalid: {exc}") from exc
    return CircuitPyOutput(code=source, runtime=runtime_source)


def write_circuitpy(
    program: Program,
    hw: P1AM,
    *,
    output_dir: str | Path,
    target_scan_ms: float,
    watchdog_ms: int | None = None,
    runstop: RunStopConfig | None = None,
    modbus_server: ModbusServerConfig | None = None,
    modbus_client: ModbusClientConfig | None = None,
    tag_map: TagMap | None = None,
    mapped_tag_scope: MappedTagScope = "referenced_only",
) -> Path:
    """Generate and write ``code.py`` to *output_dir*.

    Accepts the same parameters as :func:`generate_circuitpy` plus
    ``output_dir``.  Returns the path to the written file.
    """
    result = generate_circuitpy(
        program,
        hw,
        target_scan_ms=target_scan_ms,
        watchdog_ms=watchdog_ms,
        runstop=runstop,
        modbus_server=modbus_server,
        modbus_client=modbus_client,
        tag_map=tag_map,
        mapped_tag_scope=mapped_tag_scope,
    )
    out = Path(output_dir)
    code_path = out / "code.py"
    code_path.write_text(result.code, encoding="utf-8")
    return code_path
