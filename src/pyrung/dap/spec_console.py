"""Console verbs for spec test generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.spec import SpecEntry, generate_test_file, parse_formula

_RUNNER_LINE_RE = re.compile(r"^\s*(runner\s*=\s*PLC\(|with\s+PLC\()")
_PROGRAM_DECORATOR_RE = re.compile(r"^@program\b")
_DEF_RE = re.compile(r"^def\s+(\w+)\s*\(")


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
    program_source, prog_var = _extract_program_source(adapter)
    content = generate_test_file(specs, program_source, program_var=prog_var)
    filepath.write_text(content, encoding="utf-8")
    return ConsoleResult(f"Generated {len(specs)} test(s) to {filepath}")


def _extract_program_source(adapter: Any) -> tuple[str, str]:
    """Extract program source and detect the program variable name.

    Returns (source, program_var_name).  Truncates at the ``runner = PLC(...)``
    line so the simulation block is excluded from the generated test file.
    """
    program_path = getattr(adapter, "_program_path", None)
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded — cannot extract source.")
    source = Path(program_path).read_text(encoding="utf-8")

    prog_var = "logic"
    kept: list[str] = []
    in_decorator = False
    for line in source.splitlines():
        if _RUNNER_LINE_RE.match(line):
            break
        if _PROGRAM_DECORATOR_RE.match(line):
            in_decorator = True
        if in_decorator:
            m = _DEF_RE.match(line)
            if m:
                prog_var = m.group(1)
                in_decorator = False
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    return "\n".join(kept), prog_var
