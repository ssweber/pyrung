"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import math as _math

from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.circuitpy.codegen.render import _render_code
from pyrung.circuitpy.hardware import P1AM
from pyrung.circuitpy.validation import validate_circuitpy_program
from pyrung.core.program import Program


def generate_circuitpy(
    program: Program,
    hw: P1AM,
    *,
    target_scan_ms: float,
    watchdog_ms: int | None = None,
) -> str:
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

    if not hw._slots:
        raise ValueError("P1AM hardware config must include at least one configured slot")

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

    ctx = CodegenContext(
        program=program,
        hw=hw,
        target_scan_ms=float(target_scan_ms),
        watchdog_ms=watchdog_ms,
    )
    ctx.collect_hw_bindings()
    ctx.collect_program_references()
    ctx.collect_retentive_tags()
    ctx.assign_symbols()

    source = _render_code(ctx)
    try:
        compile(source, "code.py", "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"Generated source is invalid: {exc}") from exc
    return source
