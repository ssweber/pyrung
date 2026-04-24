"""Spec formulas — parse mined invariants and generate pytest code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pyrung.core.program import Program

SpecKind = Literal["edge_correlation", "steady_implication", "value_temporal"]

# ---------------------------------------------------------------------------
# Formula regexes
# ---------------------------------------------------------------------------

_EDGE_RE = re.compile(r"^(.+?)(↑|↓)\s*->\s*(.+?)(↑|↓)\s+within\s+(\d+)\s+scans\s+\[dt=([\d.]+)\]$")

_VALUE_TEMPORAL_RE = re.compile(
    r"^(.+?)=(.+?)\s+=>\s+(.+?)=(.+?)\s+within\s+(\d+)\s+scans\s+\[dt=([\d.]+)\]$"
)

_IMPLICATION_RE = re.compile(r"^(.+?)\s+=>\s+(~?)(.+?)\s+\[dt=([\d.]+)\]$")


@dataclass(frozen=True)
class SpecEntry:
    """A checking-ready invariant parsed from a ``# spec:`` line."""

    kind: SpecKind
    formula: str
    antecedent_tag: str
    consequent_tag: str
    antecedent_direction: str | None = None
    consequent_direction: str | None = None
    antecedent_value: Any = None
    consequent_value: Any = None
    negated: bool = False
    delay_scans: int = 0
    dt_seconds: float = 0.010


_ARROW_TO_DIR = {"↑": "up", "↓": "down"}


def parse_formula(formula: str) -> SpecEntry:
    """Parse a canonical formula string into a ``SpecEntry``.

    Raises ``ValueError`` on unrecognised formulas.
    """
    text = formula.strip()

    m = _EDGE_RE.match(text)
    if m:
        return SpecEntry(
            kind="edge_correlation",
            formula=text,
            antecedent_tag=m.group(1).strip(),
            consequent_tag=m.group(3).strip(),
            antecedent_direction=_ARROW_TO_DIR[m.group(2)],
            consequent_direction=_ARROW_TO_DIR[m.group(4)],
            delay_scans=int(m.group(5)),
            dt_seconds=float(m.group(6)),
        )

    m = _VALUE_TEMPORAL_RE.match(text)
    if m:
        return SpecEntry(
            kind="value_temporal",
            formula=text,
            antecedent_tag=m.group(1).strip(),
            consequent_tag=m.group(3).strip(),
            antecedent_value=_parse_value(m.group(2).strip()),
            consequent_value=_parse_value(m.group(4).strip()),
            delay_scans=int(m.group(5)),
            dt_seconds=float(m.group(6)),
        )

    m = _IMPLICATION_RE.match(text)
    if m:
        return SpecEntry(
            kind="steady_implication",
            formula=text,
            antecedent_tag=m.group(1).strip(),
            consequent_tag=m.group(3).strip(),
            negated=m.group(2) == "~",
            dt_seconds=float(m.group(4)),
        )

    raise ValueError(f"Unrecognised spec formula: {text!r}")


def _parse_value(raw: str) -> Any:
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw.strip("\"'")


# ---------------------------------------------------------------------------
# Test generation
# ---------------------------------------------------------------------------


def generate_test_file(
    specs: list[SpecEntry],
    program_source: str,
    *,
    program_var: str = "logic",
    program: Program | None = None,
) -> str:
    """Generate a self-contained pytest file from accepted specs."""
    lines: list[str] = []

    for src_line in program_source.splitlines():
        lines.append(src_line)
    lines.append("")

    has_implication = any(s.kind == "steady_implication" for s in specs)
    if has_implication:
        lines.append("")
        lines.append("import pytest")
        lines.append("")
        lines.append("from pyrung.core.analysis.simplified import expr_requires, reset_dominance")
    lines.append("")

    simplified_forms: dict[str, Any] | None = None
    if program is not None and has_implication:
        simplified_forms = program.simplified()

    used_names: dict[str, int] = {}
    for spec in specs:
        base = _test_function_name(spec)
        count = used_names.get(base, 0)
        used_names[base] = count + 1
        name = base if count == 0 else f"{base}_{count + 1}"

        body = _generate_test_body(
            spec, program_var=program_var, program=program, simplified_forms=simplified_forms
        )

        if body is None:
            lines.append('@pytest.mark.skip(reason="observed in trace, not structurally provable")')
            lines.append(f"def {name}():")
            lines.append(f"    # {spec.formula}")
            lines.append("    pass")
        else:
            lines.append(f"def {name}():")
            lines.append(f"    # {spec.formula}")
            lines.extend(body)
        lines.append("")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _generate_test_body(
    spec: SpecEntry,
    *,
    program_var: str = "logic",
    program: Program | None = None,
    simplified_forms: dict[str, Any] | None = None,
) -> list[str] | None:
    """Generate the body lines of a test function for one spec.

    Returns ``None`` for steady implications that cannot be verified
    structurally (caller emits a ``pytest.mark.skip`` wrapper).
    """
    if spec.kind == "steady_implication":
        return _implication_body(
            spec, program_var=program_var, program=program, simplified_forms=simplified_forms
        )

    lines: list[str] = []
    lines.append(f"    plc = PLC({program_var}, dt={spec.dt_seconds})")
    lines.append("    plc.step()")

    if spec.kind == "edge_correlation":
        lines.extend(_edge_body(spec))
    elif spec.kind == "value_temporal":
        lines.extend(_value_temporal_body(spec))

    return lines


def _edge_body(spec: SpecEntry) -> list[str]:
    lines: list[str] = []
    if spec.antecedent_direction == "down":
        lines.append(f'    plc.patch({{"{spec.antecedent_tag}": True}})')
        lines.append("    plc.step()")
        lines.append(f'    plc.patch({{"{spec.antecedent_tag}": False}})')
    else:
        lines.append(f'    plc.patch({{"{spec.antecedent_tag}": True}})')

    steps = spec.delay_scans + 1
    if steps == 1:
        lines.append("    plc.step()")
    else:
        lines.append(f"    plc.run(cycles={steps})")

    if spec.consequent_direction == "up":
        lines.append(f'    assert plc.current_state.tags["{spec.consequent_tag}"]')
    else:
        lines.append(f'    assert not plc.current_state.tags["{spec.consequent_tag}"]')
    return lines


def _implication_body(
    spec: SpecEntry,
    *,
    program_var: str = "logic",
    program: Program | None = None,
    simplified_forms: dict[str, Any] | None = None,
) -> list[str] | None:
    from pyrung.core.analysis.simplified import expr_requires, reset_dominance

    ant = spec.antecedent_tag
    cons = spec.consequent_tag

    # Tier 1: structural requirement in simplified expression
    if simplified_forms is not None and ant in simplified_forms:
        if expr_requires(simplified_forms[ant].expr, cons, negated=spec.negated):
            neg = ", negated=True" if spec.negated else ""
            lines: list[str] = []
            lines.append(f"    plc = PLC({program_var}, dt={spec.dt_seconds})")
            lines.append("    forms = plc.program.simplified()")
            lines.append(f'    assert "{ant}" in forms')
            lines.append(f'    assert expr_requires(forms["{ant}"].expr, "{cons}"{neg})')
            return lines

    # Tier 2: reset dominance for latched tags
    if program is not None and reset_dominance(program, ant, cons, negated=spec.negated):
        neg = ", negated=True" if spec.negated else ""
        lines = []
        lines.append(f"    plc = PLC({program_var}, dt={spec.dt_seconds})")
        lines.append(f'    assert reset_dominance(plc.program, "{ant}", "{cons}"{neg})')
        return lines

    # Neither tier can verify
    return None


def _value_temporal_body(spec: SpecEntry) -> list[str]:
    lines: list[str] = []
    lines.append(
        f'    plc.patch({{"{spec.antecedent_tag}": {_python_literal(spec.antecedent_value)}}})'
    )

    steps = spec.delay_scans + 1
    if steps == 1:
        lines.append("    plc.step()")
    else:
        lines.append(f"    plc.run(cycles={steps})")

    lines.append(
        f'    assert plc.current_state.tags["{spec.consequent_tag}"]'
        f" == {_python_literal(spec.consequent_value)}"
    )
    return lines


def _test_function_name(spec: SpecEntry) -> str:
    """Derive a test function name from a spec."""
    if spec.kind == "edge_correlation":
        a = _slug(spec.antecedent_tag)
        a_dir = spec.antecedent_direction or "up"
        c = _slug(spec.consequent_tag)
        c_dir = spec.consequent_direction or "up"
        return f"test_{a}_{a_dir}_{c}_{c_dir}"
    if spec.kind == "steady_implication":
        a = _slug(spec.antecedent_tag)
        neg = "not_" if spec.negated else ""
        c = _slug(spec.consequent_tag)
        return f"test_{a}_implies_{neg}{c}"
    a = _slug(spec.antecedent_tag)
    av = _slug(str(spec.antecedent_value))
    c = _slug(spec.consequent_tag)
    cv = _slug(str(spec.consequent_value))
    return f"test_{a}_{av}_{c}_{cv}"


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def _python_literal(value: Any) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return repr(value)
