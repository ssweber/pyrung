"""Low-level static-expression helpers shared by trace and tide readers."""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_values import _expr_tag_names, _written_value_for_tag

_INDEX_CHASE_CAP = 32


def single_calc_source(idx_tag: str, pdg: Any, program: Any) -> tuple[Any, str] | None:
    """Return ``(expression, source_tag)`` for one calc-written index."""

    from pyrung.core.instruction.calc import CalcInstruction

    writers = pdg.writers_of.get(idx_tag, frozenset())
    if len(writers) != 1:
        return None
    ro = resolve_rung(program, pdg.rung_nodes[next(iter(writers))])
    if ro is None:
        return None
    for instr in ro._instructions:
        if isinstance(instr, CalcInstruction) and getattr(instr.dest, "name", None) == idx_tag:
            names = _expr_tag_names(instr.expression)
            if not names:
                return None
            mutable = {name for name in names if pdg.writers_of.get(name)} - {idx_tag}
            if len(mutable) != 1:
                return None
            return instr.expression, next(iter(mutable))
    return None


def index_values(
    idx_tag: str,
    snapshot: dict[str, Any],
    pdg: Any,
    program: Any,
) -> list[int]:
    """Plausible values for an index register, current value first."""

    from pyrung.core.analysis.sp_values import _named_copy_source, _writer_for_tag
    from pyrung.core.crossing import Literal

    rest: set[int] = set()
    current = snapshot.get(idx_tag)
    for ri in sorted(pdg.writers_of.get(idx_tag, frozenset())):
        ro = resolve_rung(program, pdg.rung_nodes[ri])
        if ro is None:
            continue
        written = _written_value_for_tag(ro, idx_tag)
        if isinstance(written, Literal):
            value = written.value
            if isinstance(value, int) and not isinstance(value, bool):
                rest.add(value)
            continue
        instr = _writer_for_tag(ro, idx_tag)
        source = _named_copy_source(instr) if instr is not None else None
        if source is not None and source != idx_tag:
            value = snapshot.get(source)
            if isinstance(value, int) and not isinstance(value, bool):
                rest.add(value)
    out: list[int] = []
    if isinstance(current, int) and not isinstance(current, bool):
        out.append(current)
        rest.discard(current)
    out.extend(sorted(rest))
    return out[:_INDEX_CHASE_CAP]
