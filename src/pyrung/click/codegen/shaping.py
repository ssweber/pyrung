"""Rung shaping: analyzed rung -> ``with rung`` / ``with branch`` blocks.

The emitter renders exactly the blocks built here, so the logic check below
can compare what the generated code will say against what the source rows
said without rendering or executing anything.

The check compares logic, not shape.  Each output is reduced to its AF
token, its full condition (rung contacts and enclosing branch contacts, with
nested AND/OR flattened) and its pins, and outputs are grouped by the CLICK
rung (with its continued rungs) they sit in.  Continued rows coming back as
branches, or a branch coming back as ``.continued()``, is a shape change and
passes.  A contact that goes missing does not.

Grouping outputs under a shared ``with rung(...)`` or ``with branch(...)``
reads the shared contacts once, where the group starts; the read-order guard
(:mod:`~pyrung.click.codegen.read_order`) keeps a contact out of a group where
that would read it across a write.
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
from pyrung.click.codegen.read_order import ReadOrder, shared_limit

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
    """
    real = [instr for instr in rung.instructions if instr.af_token != NOP]
    condition = rung.condition_tree
    if len(real) == 1 and real[0].branch_tree is not None:
        return series_tree(condition, real[0].branch_tree), [replace(real[0], branch_tree=None)]
    return condition, _group_blocks([(instr.branch_tree, instr) for instr in real], rung.read_order)


def _shared_count(
    group: list[tuple[SPNode | None, _InstructionInfo]], order: ReadOrder | None
) -> int:
    """How many leading contacts *group* shares and may read as one branch node."""
    trees = [tree for tree, _instr in group]
    shared = len(factor_outputs(trees).shared)
    return shared_limit(order, trees, [instr.position for _tree, instr in group], shared)


def _group_blocks(
    outputs: list[tuple[SPNode | None, _InstructionInfo]], order: ReadOrder | None = None
) -> list[_BlockItem]:
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
            if not _shared_count(outputs[index : stop + 1], order):
                break
            stop += 1

        group = outputs[index:stop]
        count = _shared_count(group, order) if len(group) > 1 else len(as_series_children(tree))
        remaining: list[tuple[SPNode | None, _InstructionInfo]] = []
        for group_tree, group_instr in group:
            rest = as_series_children(group_tree)[count:]
            remaining.append((make_compound(rest, Series) if rest else None, group_instr))
        shared = as_series_children(tree)[:count]
        items.append(_BranchBlock(make_compound(shared, Series), _group_blocks(remaining, order)))
        index = stop
    return items


# ---------------------------------------------------------------------------
# Logic check
# ---------------------------------------------------------------------------

# A tree key is a label string or ("AND"|"OR", (child keys...)).
_TreeKey = Any
_OutputKey = tuple[str, Any, tuple[tuple[str, str, Any], ...]]


def _tree_key(tree: SPNode | None) -> _TreeKey:
    """Canonical form of *tree*: labels only, nested AND/OR flattened."""
    if tree is None:
        return None
    if isinstance(tree, Leaf):
        return str(tree.label)
    kind = "AND" if isinstance(tree, Series) else "OR"
    children: list[_TreeKey] = []
    for child in tree.children:
        key = _tree_key(child)
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


def _source_snapshots(rungs: list[_AnalyzedRung]) -> list[list[_OutputKey]]:
    """Outputs per raw source rung."""
    snapshots: list[list[_OutputKey]] = []
    for rung in rungs:
        if rung.source_outputs is None:
            continue
        snapshots.append(
            [
                _output_key(out.af_token, out.tree, out.pins)
                for out in rung.source_outputs
                if out.af_token != NOP
            ]
        )
    return [snapshot for snapshot in snapshots if snapshot]


def _walk_blocks(items: list[_BlockItem], context: SPNode | None, out: list[_OutputKey]) -> None:
    for item in items:
        if isinstance(item, _BranchBlock):
            _walk_blocks(item.items, series_tree(context, item.tree), out)
        else:
            # The emitter renders an instruction under its enclosing blocks
            # only; an instruction's own branch_tree is not rendered.
            out.append(_output_key(item.af_token, context, item.pins))


def _emitted_snapshots(rungs: list[_AnalyzedRung]) -> list[list[_OutputKey]]:
    """Outputs per CLICK rung (a rung and its continued rungs) as the emitter renders *rungs*."""
    snapshots: list[list[_OutputKey]] = []
    for rung in rungs:
        if not rung.is_continued or not snapshots:
            snapshots.append([])
        current = snapshots[-1]
        if rung.role is RungRole.FORLOOP_START:
            # The emitter renders the rung condition and the first for() token.
            if rung.instructions:
                first = rung.instructions[0]
                current.append(_output_key(first.af_token, rung.condition_tree, ()))
            continue
        if rung.role is RungRole.FORLOOP_NEXT:
            # next() is implied by the end of the forloop block.
            if rung.instructions:
                current.append(_output_key(rung.instructions[0].af_token, None, ()))
            continue
        condition, items = rung_blocks(rung)
        _walk_blocks(items, condition, current)
    return [snapshot for snapshot in snapshots if snapshot]


def emitted_logic_mismatch(rungs: list[_AnalyzedRung]) -> str | None:
    """Describe the first logic difference between source rows and emitted code.

    Returns ``None`` when every non-NOP output keeps its AF token, its full
    condition, its pins and its CLICK rung.
    """
    expected = _source_snapshots(rungs)
    actual = _emitted_snapshots(rungs)
    if expected == actual:
        return None

    flat_expected = [(group, key) for group, outs in enumerate(expected) for key in outs]
    flat_actual = [(group, key) for group, outs in enumerate(actual) for key in outs]
    for index, (exp, act) in enumerate(zip(flat_expected, flat_actual, strict=False)):
        if exp[1] != act[1]:
            return (
                f"output {index + 1} should be {_describe(exp[1])}, "
                f"but generated code has {_describe(act[1])}"
            )
    if len(flat_expected) > len(flat_actual):
        missing = flat_expected[len(flat_actual)][1]
        return (
            f"output {len(flat_actual) + 1} ({_describe(missing)}) is missing from generated code"
        )
    if len(flat_actual) > len(flat_expected):
        extra = flat_actual[len(flat_expected)][1]
        return f"generated code adds output {_describe(extra)}"
    # Same outputs, different rung grouping.
    for index, (exp, act) in enumerate(zip(flat_expected, flat_actual, strict=True)):
        if exp[0] != act[0]:
            return (
                f"output {index + 1} ({_describe(exp[1])}) lands in a different CLICK rung "
                "than in the source (rung vs .continued() grouping changed)"
            )
    return "outputs differ from the source"  # pragma: no cover - defensive


__all__ = [
    "NOP",
    "emitted_logic_mismatch",
    "rung_blocks",
    "series_tree",
]
