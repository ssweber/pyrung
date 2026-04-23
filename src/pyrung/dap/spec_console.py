"""Console verbs for spec test generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.spec import SpecEntry, generate_test_file, parse_formula

_RUNNER_LINE_RE = re.compile(r"^\s*(runner\s*=\s*PLC\(|with\s+PLC\()")


def _accepted_as_specs(adapter: Any) -> list[SpecEntry]:
    accepted = getattr(adapter, "_miner_accepted", [])
    specs: list[SpecEntry] = []
    for candidate in accepted:
        try:
            specs.append(parse_formula(candidate.formula))
        except ValueError:
            continue
    return specs


@register("spec", usage="spec [list] | spec test <filepath>", group="review")
def _cmd_spec(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) >= 2 and parts[1] == "test":
        return _cmd_spec_test(adapter, expression)
    specs = _accepted_as_specs(adapter)
    if not specs:
        return ConsoleResult("No accepted specs.")
    lines = [s.formula for s in specs]
    return ConsoleResult("\n".join(lines))


def _cmd_spec_test(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: spec test <filepath>")
    filepath = Path(parts[2]).expanduser().resolve()
    specs = _accepted_as_specs(adapter)
    if not specs:
        raise adapter.DAPAdapterError("No accepted specs to generate tests for.")
    program_source = _extract_program_source(adapter)
    content = generate_test_file(specs, program_source)
    filepath.write_text(content, encoding="utf-8")
    return ConsoleResult(f"Generated {len(specs)} test(s) to {filepath}")


def _extract_program_source(adapter: Any) -> str:
    program_path = getattr(adapter, "_program_path", None)
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded — cannot extract source.")
    source = Path(program_path).read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if not _RUNNER_LINE_RE.match(line)]
    return "\n".join(lines)
