"""Rung shaping: analyzed rung -> ``with rung`` / ``with branch`` blocks.

The emitter renders exactly the blocks built here, so the logic check below
can compare what the generated code will say against what the source rows
said without rendering or executing anything.

The check compares the drawing, row for row.  Each output is reduced to its
AF token, its full condition (rung contacts and enclosing branch contacts,
with nested AND/OR flattened) and its pins, every contact named by its grid
cell, and outputs are grouped by the wire they sit on: each wire of a CLICK
rung is one pyrung rung, ``.continued()`` after the first.  A contact that
goes missing fails it, and so does a contact read from another cell (two
cells with one label folded into one ``with branch(...)``) or an output on
another wire.

Outputs share a ``with rung(...)`` or ``with branch(...)`` only where the
drawing has one cell, teed off to each of them: the cell is read once, where
the group starts, in pyrung as on a CLICK.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from pyrung.click._topology import (
    Leaf,
    Series,
    SPNode,
    as_series_children,
    factor_outputs,
    make_compound,
)
from pyrung.click.codegen.models import RungRole, _AnalyzedRung, _InstructionInfo, _PinInfo

NOP = "NOP"


@dataclass
class _BranchBlock:
    """``with branch(tree):`` holding instructions and nested branches."""

    tree: SPNode
    items: list[_BlockItem]


_BlockItem = _InstructionInfo | _BranchBlock


def series_tree(*parts: SPNode | None) -> SPNode | None:
    """AND the non-empty *parts* together (``None`` when all are empty)."""
    present = [part for part in parts if part is not None]
    if not present:
        return None
    return make_compound(present, Series)


def rung_blocks(rung: _AnalyzedRung) -> tuple[SPNode | None, list[_BlockItem]]:
    """Shape *rung* for emission: the ``with rung(...)`` condition and its body.

    NOP outputs are dropped here (a pyrung NOP is a whole empty rung, never an
    instruction beside others); the analyzer already gave every NOP that shared
    a rung with other outputs a rung of its own.  A lone remaining output keeps
    its branch contacts by moving them onto the rung condition.

    That is the one drawing that does not round-trip as written: CLICK draws
    ``with rung(A): with branch(B): out(Y)`` as A then B in series on one row,
    the same cells as ``with rung(A, B): out(Y)``.  The rows hold no mark of
    where the rung's contacts end and the branch's begin, so the plain rung is
    the one read back.
    """
    real = [instr for instr in rung.instructions if instr.af_token != NOP]
    condition = rung.condition_tree
    if len(real) == 1 and real[0].branch_tree is not None:
        return series_tree(condition, real[0].branch_tree), [replace(real[0], branch_tree=None)]
    return condition, _group_blocks([(instr.branch_tree, instr) for instr in real])


def _shared_count(group: list[tuple[SPNode | None, _InstructionInfo]]) -> int:
    """How many leading contact cells *group* shares, teed off to each."""
    return len(factor_outputs([tree for tree, _instr in group], cells=True).shared)


def _group_blocks(outputs: list[tuple[SPNode | None, _InstructionInfo]]) -> list[_BlockItem]:
    """Recursive shared-prefix factoring of consecutive outputs into branches."""
    items: list[_BlockItem] = []
    index = 0
    while index < len(outputs):
        tree, instr = outputs[index]
        if tree is None:
            items.append(replace(instr, branch_tree=None) if instr.branch_tree else instr)
            index += 1
            continue

        stop = index + 1
        while stop < len(outputs):
            candidate_tree, _candidate = outputs[stop]
            if candidate_tree is None:
                break
            if not _shared_count(outputs[index : stop + 1]):
                break
            stop += 1

        group = outputs[index:stop]
        count = _shared_count(group) if len(group) > 1 else len(as_series_children(tree))
        remaining: list[tuple[SPNode | None, _InstructionInfo]] = []
        for group_tree, group_instr in group:
            rest = as_series_children(group_tree)[count:]
            remaining.append((make_compound(rest, Series) if rest else None, group_instr))
        shared = as_series_children(tree)[:count]
        items.append(_BranchBlock(make_compound(shared, Series), _group_blocks(remaining)))
        index = stop
    return items


# ---------------------------------------------------------------------------
# Logic check
# ---------------------------------------------------------------------------

# A tree key is a cell ("cell", label, row, col) or ("AND"|"OR", (child keys...)).
_TreeKey = Any
_OutputKey = tuple[str, Any, tuple[tuple[str, str, Any], ...]]
# An output's place: (CLICK rung, wire in it) and its key.
_Placed = tuple[tuple[int, int], _OutputKey]


def _tree_key(tree: SPNode | None, *, cells: bool = True) -> _TreeKey:
    """Canonical form of *tree*, nested AND/OR flattened.

    Each contact is its cell, or with *cells* False its label alone (logic,
    not drawing: two drawings that read the same contacts give one key).
    """
    if tree is None:
        return None
    if isinstance(tree, Leaf):
        if not cells:
            return str(tree.label)
        return ("cell", str(tree.label), tree.row, tree.col)
    kind = "AND" if isinstance(tree, Series) else "OR"
    children: list[_TreeKey] = []
    for child in tree.children:
        key = _tree_key(child, cells=cells)
        if isinstance(key, tuple) and key[0] == kind:
            children.extend(key[1])
        else:
            children.append(key)
    if len(children) == 1:
        return children[0]
    return (kind, tuple(children))


def _render_key(key: _TreeKey) -> str:
    if key is None:
        return "(always)"
    if isinstance(key, str):
        return key
    if key[0] == "cell":
        return key[1]
    return f"{key[0]}({', '.join(_render_key(child) for child in key[1])})"


def _output_key(
    af_token: str, tree: SPNode | None, pins: list[_PinInfo] | tuple[_PinInfo, ...]
) -> _OutputKey:
    return (
        af_token,
        _tree_key(tree),
        tuple((pin.name, pin.arg, _tree_key(pin.condition_tree)) for pin in pins),
    )


def _describe(key: _OutputKey) -> str:
    af_token, tree, pins = key
    text = f"{af_token} when {_render_key(tree)}"
    for name, arg, pin_tree in pins:
        text += f" .{name}({arg}) when {_render_key(pin_tree)}"
    return text


def _source_outputs(rungs: list[_AnalyzedRung]) -> list[_Placed]:
    """Every output of the source rows, placed on its raw rung and wire."""
    placed: list[_Placed] = []
    for number, rung in enumerate(rungs):
        if rung.source_outputs is None:
            continue
        # A For body rung has no .continued(): its wires stay one rung.
        one_rung = rung.role is not RungRole.NORMAL
        placed.extend(
            ((number, 0 if one_rung else out.wire), _output_key(out.af_token, out.tree, out.pins))
            for out in rung.source_outputs
            if out.af_token != NOP
        )
    return _renumber(placed)


def _walk_blocks(items: list[_BlockItem], context: SPNode | None, out: list[_OutputKey]) -> None:
    for item in items:
        if isinstance(item, _BranchBlock):
            _walk_blocks(item.items, series_tree(context, item.tree), out)
        else:
            # The emitter renders an instruction under its enclosing blocks
            # only; an instruction's own branch_tree is not rendered.
            out.append(_output_key(item.af_token, context, item.pins))


def _rung_outputs(rung: _AnalyzedRung) -> list[_OutputKey]:
    """One pyrung rung's outputs as the emitter renders it."""
    if rung.role is RungRole.FORLOOP_START:
        # The emitter renders the rung condition and the first for() token.
        if not rung.instructions:
            return []
        return [_output_key(rung.instructions[0].af_token, rung.condition_tree, ())]
    if rung.role is RungRole.FORLOOP_NEXT:
        # next() is implied by the end of the forloop block.
        if not rung.instructions:
            return []
        return [_output_key(rung.instructions[0].af_token, None, ())]
    out: list[_OutputKey] = []
    condition, items = rung_blocks(rung)
    _walk_blocks(items, condition, out)
    return out


def _emitted_outputs(rungs: list[_AnalyzedRung]) -> list[_Placed]:
    """Every output as the emitter renders *rungs*, placed on its CLICK rung and wire.

    A rung starts a CLICK rung; each ``.continued()`` rung is its next wire.
    """
    placed: list[_Placed] = []
    number, wire = -1, 0
    for rung in rungs:
        if rung.is_continued and number >= 0:
            wire += 1
        else:
            number, wire = number + 1, 0
        placed.extend(((number, wire), key) for key in _rung_outputs(rung))
    return _renumber(placed)


def _renumber(placed: list[_Placed]) -> list[_Placed]:
    """Number the CLICK rungs that hold outputs 0, 1, ..., and each one's wires the same way."""
    rungs: dict[int, int] = {}
    wires: dict[tuple[int, int], int] = {}
    out: list[_Placed] = []
    for (rung, wire), key in placed:
        number = rungs.setdefault(rung, len(rungs))
        if (rung, wire) not in wires:
            wires[(rung, wire)] = sum(1 for r, _w in wires if r == rung)
        out.append(((number, wires[(rung, wire)]), key))
    return out


def emitted_logic_mismatch(rungs: list[_AnalyzedRung]) -> str | None:
    """Describe the first difference between the source rows and the emitted code.

    Returns ``None`` when every non-NOP output keeps its AF token, its full
    condition (each contact from its own cell), its pins, its CLICK rung and
    its wire in it.
    """
    expected = _source_outputs(rungs)
    actual = _emitted_outputs(rungs)
    if expected == actual:
        return None

    for index, (exp, act) in enumerate(zip(expected, actual, strict=False)):
        if exp[1] != act[1]:
            want, have = _describe(exp[1]), _describe(act[1])
            if want == have:
                return (
                    f"output {index + 1} ({want}) reads a contact from another cell with "
                    "the same label than the source rows draw"
                )
            return f"output {index + 1} should be {want}, but generated code has {have}"
    if len(expected) > len(actual):
        missing = expected[len(actual)][1]
        return f"output {len(actual) + 1} ({_describe(missing)}) is missing from generated code"
    if len(actual) > len(expected):
        extra = actual[len(expected)][1]
        return f"generated code adds output {_describe(extra)}"
    # Same outputs, placed on other rungs or wires.
    for index, (exp, act) in enumerate(zip(expected, actual, strict=True)):
        if exp[0][0] != act[0][0]:
            return (
                f"output {index + 1} ({_describe(exp[1])}) lands in a different CLICK rung "
                "than in the source (rung vs .continued() grouping changed)"
            )
        if exp[0] != act[0]:
            return (
                f"output {index + 1} ({_describe(exp[1])}) lands on a different wire of its "
                "CLICK rung than in the source (each wire is one rung or .continued() rung)"
            )
    return "outputs differ from the source"  # pragma: no cover - defensive


__all__ = [
    "NOP",
    "emitted_logic_mismatch",
    "rung_blocks",
    "series_tree",
]
