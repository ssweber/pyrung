"""Write a case's Math formulas into the CLICK export as a person types them.

pyrung's exporter brackets every operation of a ``math()``, so a CLICK that runs the export
never has to choose an order. A case that asks about CLICK's order passes ``formulas=``, one
formula for each ``math()`` of the case, in rung order. The export then writes each formula
in place of the bracketed one, so the PLC reads it in its own order.

The pyrung rungs stay as they are, so every other target runs them as before. The swap is
refused unless the importer reads the typed formula to the same operations as the bracketed
one. So what the case runs in-process is what the drawing imports to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from pyrung.click.codegen.formula import (
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    parse_with_unconfirmed_pairs,
)
from pyrung.click.codegen.utils import _parse_af_args

Row = tuple[str, ...]

_MATH = "math("


def draw_formulas(rows: Sequence[Row], marker: str, formulas: Sequence[str]) -> tuple[Row, ...]:
    """Return *rows* with each ``math()`` of the case marked ``Case <marker>`` typed as *formulas*.

    Raises:
        ValueError: The case does not hold one ``math()`` for each formula, or the importer
            reads a typed formula to other operations than the exported one.
    """
    out = [tuple(r) for r in rows]
    start = next(
        (i for i, r in enumerate(out) if r[0] == "#" and len(r) > 1 and r[1] == f"Case {marker}"),
        None,
    )
    if start is None:
        raise ValueError(f"No case {marker!r} in these rows")
    end = next(
        (
            i
            for i in range(start + 1, len(out))
            if out[i][0] == "#" and len(out[i]) > 1 and out[i][1].startswith("Case ")
        ),
        len(out),
    )
    found = [i for i in range(start, end) if out[i][0] != "#" and out[i][-1].startswith(_MATH)]
    if len(found) != len(formulas):
        raise ValueError(
            f"Case {marker} has {len(found)} math() rows but {len(formulas)} formulas. Give one "
            "formula for each math() of the case, in rung order."
        )
    for index, typed in zip(found, formulas, strict=True):
        row = list(out[index])
        row[-1] = _typed_token(row[-1], typed, marker)
        out[index] = tuple(row)
    return tuple(out)


def _typed_token(token: str, typed: str, marker: str) -> str:
    args, kwargs = _parse_af_args(token[len(_MATH) : -1])
    exported = args[0]
    rest = token[len(_MATH) + len(exported) :]
    if not token.startswith(_MATH + exported) or not rest.startswith(","):
        raise ValueError(f"Case {marker}: cannot find the formula in {token!r}")
    hex_mode = ("mode", "hex") in kwargs
    want, _ = parse_with_unconfirmed_pairs(exported, hex_mode=hex_mode)
    got, _ = parse_with_unconfirmed_pairs(typed, hex_mode=hex_mode)
    if _as_subtraction(got) != _as_subtraction(want):
        raise ValueError(
            f"Case {marker}: the formula {typed!r} does not compute what the case's math() "
            f"exports as {exported!r}."
        )
    return f"{_MATH}{typed}{rest}"


def _as_subtraction(node: Formula) -> Formula:
    """Return *node* with each minus sign written as ``0 - x``, as the exporter writes it."""
    if isinstance(node, Negate):
        return BinaryOp("-", Number(0), _as_subtraction(node.operand))
    if isinstance(node, BinaryOp):
        return replace(node, left=_as_subtraction(node.left), right=_as_subtraction(node.right))
    if isinstance(node, Call):
        return replace(node, args=tuple(_as_subtraction(arg) for arg in node.args))
    return node
