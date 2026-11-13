"""Checks that exported ladder rows follow the drawing rule of CLICK's syntax check.

The checks read the grid alone, with no program and no exporter code.  Each cell of a row says
whether a vertical runs down its left edge to the row below, and what lies across it (nothing, a
wire or a contact).  The left edges of the cells on a row are the points a rung is wired through.
Wires and verticals join points into nets.  Column A's left edge is the rail.

The rule, checked on every rung:

- Each net other than the rail has at most one vertical, in one column, with no gap.
- That vertical runs from the net's topmost to its bottommost touching row (a row where a contact
  or a wire meets the net).  A net without a vertical touches one row.
- What leaves a net stacks down from the net's top row.  A row between two of them is not empty to
  the right of the vertical.
- A net's column is its longest contact path from the rail.  Each contact sits in the column of
  the net it starts from, so no wire comes between a net and its contact.
- Every output row is wired from its net's column to AE.
- No vertical sits in column A, and no row is empty.  A rung has at most 32 rows.
- A rung is one wire.  A vertical joins each row to the row above, except a box's pin rows (their
  output starts with ``.``), which CLICK draws from the rail.

The module also checks that an export keeps each output's logic.  :func:`assert_same_logic` reads
the export back with ``ladder_to_pyrung`` and compares truth tables.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence
from typing import Any

from pyrung.click import ladder_to_pyrung
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    Condition,
    NormallyClosedCondition,
)
from pyrung.core.rung import Rung
from tests.click.helpers import exec_with_source

COLS = 31  # columns A to AE
MAX_ROWS = 32


def _parts(cell: str) -> tuple[bool, str | None]:
    """(vertical down the left edge, what lies across).  Across is None, "-" or a contact."""
    if cell == "":
        return False, None
    if cell == "-":
        return False, "-"
    if cell == "T":
        return True, "-"
    if cell == "|":
        return True, None
    if cell.startswith("T:"):
        return True, cell[2:]
    return False, cell


def split_rungs(rows: Sequence[Sequence[str]]) -> list[list[tuple[str, ...]]]:
    """The rungs of an exported scope.  A rung runs from one ``R`` marker to the next.

    The header and ``#`` comment rows are dropped.
    """
    rungs: list[list[tuple[str, ...]]] = []
    for row in rows:
        row = tuple(row)
        if not row or row[0] == "marker" or row[0].startswith("#"):
            continue
        if row[0].startswith("R") or not rungs:
            rungs.append([row])
        else:
            rungs[-1].append(row)
    return rungs


def check_scope(rows: Sequence[Sequence[str]]) -> None:
    """:func:`check_rung` for every rung of an exported scope."""
    for index, rung in enumerate(split_rungs(rows)):
        try:
            check_rung(rung)
        except AssertionError as exc:
            drawing = "\n".join(",".join(row).rstrip(",") for row in rung)
            raise AssertionError(f"rung {index + 1}: {exc}\n{drawing}") from None


def check_rung(rows: Sequence[Sequence[str]]) -> None:
    """Assert that one rung's rows ``(marker, A..AE, AF)`` follow the drawing rule."""
    height = len(rows)
    assert height <= MAX_ROWS, f"{height} rows, over CLICK's {MAX_ROWS}"
    for r, row in enumerate(rows):
        assert len(row) == COLS + 2, f"row {r} has {len(row)} cells"
        assert any(row[1:]), f"row {r} is empty"
    grid = [[_parts(row[c + 1]) for c in range(COLS)] for row in rows]

    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(point: tuple[int, int]) -> tuple[int, int]:
        parent.setdefault(point, point)
        while parent[point] != point:
            parent[point] = parent[parent[point]]
            point = parent[point]
        return point

    def join(a: tuple[int, int], b: tuple[int, int]) -> None:
        parent[find(a)] = find(b)

    rail = (0, 0)
    for r in range(height):
        join((r, 0), rail)

    contacts: list[tuple[int, int]] = []
    wires: list[tuple[int, int]] = []
    verticals: list[tuple[int, int]] = []
    for r in range(height):
        for c in range(COLS):
            vertical, across = grid[r][c]
            if vertical:
                assert c > 0, f"vertical in column A on row {r}"
                assert r + 1 < height, f"vertical at column {c} runs off the bottom row"
                verticals.append((r, c))
                join((r, c), (r + 1, c))
            if across is None:
                continue
            if across == "-":
                wires.append((r, c))
                join((r, c), (r, c + 1))
            else:
                contacts.append((r, c))

    for r in range(1, height):
        if not rows[r][-1].startswith("."):
            assert any(grid[r - 1][c][0] for c in range(COLS)), (
                f"no vertical joins row {r} to the row above, so the rung holds two wires"
            )

    edges = [(find((r, c)), find((r, c + 1))) for r, c in contacts]
    for (r, c), (src, dst) in zip(contacts, edges, strict=True):
        assert src != dst, f"contact at row {r}, column {c} is bypassed"

    # Longest contact path from the rail to each net.
    longest: dict[tuple[int, int], int] = {find(rail): 0}
    for _ in range(len(edges) + 1):
        changed = False
        for src, dst in edges:
            if src in longest and longest.get(dst, -1) < longest[src] + 1:
                longest[dst] = longest[src] + 1
                changed = True
        if not changed:
            break
    else:
        raise AssertionError("the contacts form a loop")

    for (r, c), (src, _dst) in zip(contacts, edges, strict=True):
        assert src in longest, f"contact at row {r}, column {c} is not fed from the rail"
        assert c == longest[src], (
            f"contact at row {r}, column {c} should sit in column {longest[src]}, "
            "right after the net it starts from"
        )

    touching: dict[tuple[int, int], set[int]] = {}
    for r, c in contacts + wires:
        touching.setdefault(find((r, c)), set()).add(r)
        touching.setdefault(find((r, c + 1)), set()).add(r)
    columns: dict[tuple[int, int], set[int]] = {}
    vertical_rows: dict[tuple[int, int], set[int]] = {}
    for r, c in verticals:
        net = find((r, c))
        columns.setdefault(net, set()).add(c)
        vertical_rows.setdefault(net, set()).add(r)

    for net, rows_touched in touching.items():
        if net == find(rail):
            assert net not in columns, "the rail carries a vertical"
            continue
        top, bottom = min(rows_touched), max(rows_touched)
        if net not in columns:
            assert top == bottom, f"a net touches rows {top} and {bottom} with no vertical"
            continue
        cols = columns[net]
        assert len(cols) == 1, f"one net has verticals in columns {sorted(cols)}"
        (col,) = cols
        assert col == longest.get(net), (
            f"the vertical in column {col} should sit in column {longest.get(net)}, "
            "its longest contact path from the rail"
        )
        assert vertical_rows[net] == set(range(top, bottom)), (
            f"the vertical in column {col} runs on rows {sorted(vertical_rows[net])}, "
            f"not from row {top} to row {bottom}"
        )
        # What leaves the net (contacts, outputs, branches) stacks down from its top row.  A row
        # between two of them holds the rest of the upper one, so it is not empty to the right.
        leaving = [r for r in range(top, bottom + 1) if grid[r][col][1] is not None]
        assert leaving and leaving[0] == top, (
            f"nothing leaves the vertical in column {col} on its top row {top}"
        )
        for r in range(top + 1, leaving[-1]):
            if r not in leaving:
                assert any(rows[r][c + 1] for c in range(col + 1, COLS)), (
                    f"row {r} leaves a gap under the vertical in column {col}"
                )
    for net in columns:
        assert net in touching, "a vertical touches nothing"

    for r, row in enumerate(rows):
        if not row[-1]:
            continue
        net = find((r, COLS))
        start = longest.get(net)
        assert start is not None, f"the output on row {r} is not fed from the rail"
        for c in range(start, COLS):
            assert row[c + 1] in {"-", "T"}, (
                f"the output on row {r} is not wired from column {start} to AE (column {c} "
                f"holds {row[c + 1]!r})"
            )


def contact_size(conditions: Sequence[Condition]) -> tuple[int, int]:
    """(longest contact path, rows) of plain contacts in series, as the drawing rule lays them out.

    A path of 31 contacts ends past AE when a vertical must follow it.
    """
    depth = rows = 0
    for condition in conditions:
        if isinstance(condition, AllCondition):
            size = contact_size(condition.conditions)
        elif isinstance(condition, AnyCondition):
            sizes = [contact_size([child]) for child in condition.conditions]
            size = (max(d for d, _ in sizes), sum(h for _, h in sizes))
        else:
            size = (1, 1)
        depth += size[0]
        rows = max(rows, size[1])
    return depth, rows


# ---- Logic ----


def _value(condition: Condition, values: dict[str, bool], address: Any) -> bool:
    if isinstance(condition, AllCondition):
        return all(_value(child, values, address) for child in condition.conditions)
    if isinstance(condition, AnyCondition):
        return any(_value(child, values, address) for child in condition.conditions)
    if isinstance(condition, BitCondition):
        return values[address(condition.tag)]
    if isinstance(condition, NormallyClosedCondition):
        return not values[address(condition.tag)]
    raise TypeError(f"no truth table for {type(condition).__name__}")


def _leaves(condition: Condition, address: Any) -> set[str]:
    if isinstance(condition, (AllCondition, AnyCondition)):
        return set().union(*(_leaves(child, address) for child in condition.conditions))
    return {address(condition.tag)}


def _outputs(rung: Rung, conditions: list[Condition], found: list) -> None:
    for item in rung._execution_items:
        if isinstance(item, Rung):
            local = item._conditions[item._branch_condition_start :]
            _outputs(item, [*conditions, *local], found)
        else:
            found.append((item, conditions))


def output_conditions(program: Any, mapping: Any) -> dict[str, tuple[list[Condition], Any]]:
    """The conditions that feed each output and pin, and a tag resolver.

    An ``out()`` is keyed by its target's address.  Any other instruction is keyed by its type and
    its place among the program's instructions, and each pin adds ``.reset`` or ``.down`` to it.
    """

    def address(tag: Any) -> str:
        # Code that ladder_to_pyrung writes names each tag by its address and maps none of them.
        try:
            return mapping.resolve(tag)
        except KeyError:
            return tag.name

    result: dict[str, tuple[list[Condition], Any]] = {}
    count = 0
    for rung in program.rungs:
        found: list = []
        _outputs(rung, list(rung._conditions), found)
        for instruction, conditions in found:
            if type(instruction).__name__ == "OutInstruction":
                result[address(instruction.target)] = (conditions, address)
                continue
            count += 1
            key = f"{type(instruction).__name__}#{count}"
            result[key] = (conditions, address)
            for pin in ("down", "reset"):
                condition = getattr(instruction, f"{pin}_condition", None)
                if condition is not None:
                    result[f"{key}.{pin}"] = ([condition], address)
    return result


def assert_same_logic(program: Any, mapping: Any, bundle: Any, *, samples: int = 256) -> None:
    """The export read back with ``ladder_to_pyrung`` gives every output and pin the same truth table.

    Rungs of plain contacts only.  Up to 10 inputs are checked in every combination, more in
    *samples* random ones.
    """
    namespace: dict[str, object] = {}
    exec_with_source(ladder_to_pyrung(bundle), namespace)
    before = output_conditions(program, mapping)
    after = output_conditions(namespace["logic"], namespace["mapping"])
    assert set(before) == set(after), f"outputs {sorted(before)} read back as {sorted(after)}"
    for target, (conditions, address) in before.items():
        conditions_after, address_after = after[target]
        names = sorted(
            set().union(
                *(_leaves(c, address) for c in conditions),
                *(_leaves(c, address_after) for c in conditions_after),
            )
        )
        if len(names) <= 10:
            assignments = itertools.product((False, True), repeat=len(names))
        else:
            rng = random.Random(target)
            assignments = (tuple(rng.random() < 0.5 for _ in names) for _ in range(samples))
        for bits in assignments:
            values = dict(zip(names, bits, strict=True))
            want = all(_value(c, values, address) for c in conditions)
            got = all(_value(c, values, address_after) for c in conditions_after)
            assert want == got, f"{target} differs at {values}"
