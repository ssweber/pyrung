"""Pins drawn teeing off the box's input wire, for cases that ask about that drawing.

pyrung's exporter writes every pin of a box from the rail, with the shared
contacts copied into each pin's row.  CLICK also lets a pin's wire tee off the
box's input wire after some shared contacts and add its own (C000A only asks that
all pins start from the same branch).  A case with a :class:`TeeSpec` is the
tee-drawn twin of a rail-drawn case: it runs the same pyrung rungs everywhere, and
the CLICK export redraws the named pins as tees.  Nothing else in the rung moves,
so what the twin runs in-process is what the drawing imports to (a test imports
the drawing and compares).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Row = tuple[str, ...]


@dataclass(frozen=True)
class TeeSpec:
    """Draw the first pins of a box as tees off its input wire.

    ``box`` is the start of the box row's instruction token (``"count_up("``);
    ``pins`` the pin labels to tee (``(".down()",)``), which must be the first
    pin rows under the box row; ``after`` how many contacts at the start of the
    box row are shared (each teed pin's row begins with exactly those contacts
    in its rail form).  Pins not named stay drawn from the rail, below the teed
    ones.
    """

    box: str
    pins: tuple[str, ...]
    after: int

    def __post_init__(self) -> None:
        if self.after < 1:
            raise ValueError(
                f"TeeSpec(after={self.after}): a tee needs at least one shared contact before "
                "it; with after=0 it would sit in column A, the left power rail, where CLICK "
                "rejects vertical tokens (draw the pins from the rail instead)"
            )


def draw_tees(rows: Sequence[Row], marker: str, spec: TeeSpec) -> tuple[Row, ...]:
    """*rows* with the pins of the case marked ``Case <marker>`` redrawn as tees.

    Raises:
        ValueError: The case's box rung is not the rail drawing the spec expects.
    """
    out = [tuple(r) for r in rows]
    start = next(
        (i for i, r in enumerate(out) if r[0] == "#" and len(r) > 1 and r[1] == f"Case {marker}"),
        None,
    )
    if start is None:
        raise ValueError(f"No case {marker!r} in these rows")
    box_index = next(
        (i for i in range(start, len(out)) if out[i][0] != "#" and out[i][-1].startswith(spec.box)),
        None,
    )
    if box_index is None:
        raise ValueError(f"Case {marker}: no {spec.box} row")
    box = list(out[box_index])
    shared = box[1 : 1 + spec.after]
    if len(shared) < spec.after or any(_is_wire(c) for c in shared):
        raise ValueError(f"Case {marker}: the box row does not start with {spec.after} contacts")
    tee_col = 1 + spec.after
    box[tee_col] = _tee_cell(
        box[tee_col], f"Case {marker}: the box row after {spec.after} contacts"
    )
    for n, label in enumerate(spec.pins):
        index = box_index + 1 + n
        row = list(out[index]) if index < len(out) else []
        if not row or row[-1] != label or row[0] != "":
            raise ValueError(f"Case {marker}: pin {label} is not under the box row, in order")
        if row[1 : 1 + spec.after] != shared:
            raise ValueError(
                f"Case {marker}: pin {label} does not start with the box row's {spec.after} "
                f"shared contacts ({shared} vs {row[1 : 1 + spec.after]})"
            )
        row[1 : 1 + spec.after] = [""] * spec.after
        if row[tee_col] not in ("-",) and _is_wire(row[tee_col]):
            raise ValueError(f"Case {marker}: pin {label} has no wire or contact at the tee")
        if n < len(spec.pins) - 1:
            # The bus continues down past this pin: a tee on its first cell.
            row[tee_col] = _tee_cell(row[tee_col], f"Case {marker}: pin {label}")
        out[index] = tuple(row)
    out[box_index] = tuple(box)
    return tuple(out)


def _tee_cell(cell: str, where: str) -> str:
    """*cell* with the bus running down its left side: a wire becomes ``T``, a contact ``T:``."""
    if cell == "-":
        return "T"
    if _is_wire(cell) or cell.startswith("T:"):
        raise ValueError(f"{where} is {cell!r}: nothing to hang a tee on")
    return f"T:{cell}"


def _is_wire(cell: str) -> bool:
    return cell in ("", "-", "T", "|")
