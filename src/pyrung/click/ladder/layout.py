"""Ladder row layout.  Each rung is drawn from a graph of its electrical nodes.

CLICK's syntax check redraws a pasted rung into one standard form, so the export draws that form
and a pasted rung survives the check unchanged.  The rung is a series-parallel network from the
left rail to the outputs.  Each electrical node (net) between contacts is drawn as one vertical on
one column edge.

- A net sits in the column of its longest contact path from the rail.  A contact sits in the cell
  right after its start net, and a shorter path gets wire up to its end net.
- The alternatives of an Or, and the outputs and branches under a net, stack downward in source
  order.  Elements in series all start on the same top row.
- A net's vertical runs from the topmost to the bottommost row that touches it.  The rail draws no
  vertical.

Each cell (r, c) then says two things.  One is whether a vertical runs down the left edge of
column c from row r to row r+1.  The other is what lies across the cell:

=====  ========  =======
Cell   Vertical  Across
=====  ========  =======
""     no        nothing
``-``  no        wire
``X``  no        contact
``T``  yes       wire
``|``  yes       nothing
``T:X`` yes      contact
=====  ========  =======

This is the importer's reading of a grid (``codegen/analyzer.py``, ``_grid_to_graph``), run
backward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn

from pyrung.core.condition import AllCondition, AnyCondition, Condition, text_window_of
from pyrung.core.rung import Rung

# ---- Matrix shape/constants ----
_CONDITION_COLS = 31

_HEADER: tuple[str, ...] = (
    "marker",
    *tuple([chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]),
    "AF",
)

_RAIL = 0
_WIRE = "-"


# ---- Net graph ----
@dataclass
class _Net:
    """One electrical node.  ``col`` is the column whose left edge carries its vertical.

    *path* and *source* name what made the node, so an error about its vertical points there.
    """

    path: str
    source: Any
    col: int = 0
    top: int | None = None
    bottom: int | None = None

    def touch(self, row: int) -> None:
        self.top = row if self.top is None else min(self.top, row)
        self.bottom = row if self.bottom is None else max(self.bottom, row)


@dataclass(frozen=True)
class _Contact:
    """A contact from net *src* to net *dst* on *row*.  The ``-`` token is a plain wire."""

    src: int
    dst: int
    row: int
    token: str
    path: str
    source: Any


@dataclass(frozen=True)
class _Sink:
    """An output (or pin) on *row*, wired from *net* to AF."""

    net: int
    row: int
    af: str
    path: str
    source: Any


@dataclass
class _Branch:
    """Conditions and the items under them in source order.  An item is an AF token or a branch."""

    conditions: list[Condition]
    items: list[str | _Branch]
    path: str
    source: Any
    # Beside each item, the instructions whose pin rows it draws, with their paths.
    pins: list[list[tuple[Any, str]]] = field(default_factory=list)


@dataclass
class _Network:
    nets: list[_Net]
    contacts: list[_Contact] = field(default_factory=list)
    sinks: list[_Sink] = field(default_factory=list)

    def new_net(self, path: str, source: Any) -> int:
        self.nets.append(_Net(path, source))
        return len(self.nets) - 1


# ---- Row-layout mixin ----
class _LayoutMixin:
    """Build 31-column condition matrices for rungs, branches, pins and loop rows."""

    if TYPE_CHECKING:

        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...
        def _next_marker(self) -> str: ...
        def _assert_rung_height(
            self, *, rows: list[tuple[str, ...]], path: str, source: Any
        ) -> None: ...
        def _instruction_token(self, instruction: Any, *, path: str) -> str: ...
        def _pin_rows(self, instruction: Any, *, path: str) -> list[tuple[str, ...]]: ...
        def _condition_leaf_token(self, condition: Condition, *, path: str) -> str: ...

    def _render_network(
        self,
        conditions: list[Condition],
        items: list[str | _Branch],
        *,
        path: str,
        source: Any,
        first_marker: str,
    ) -> list[tuple[str, ...]]:
        """Rows ``(marker, A..AE, AF)`` for *conditions* feeding *items*."""
        network = _Network([_Net(path, source)])
        root = _Branch(list(conditions), items, path, source)
        self._place_branch(network, root, _RAIL, 0)
        return [
            (first_marker if index == 0 else "", *cells, af)
            for index, (cells, af) in enumerate(self._paint(network))
        ]

    def _render_rung_items(self, rung: Rung, *, path: str) -> list[tuple[str, ...]]:
        """The CLICK rungs of a rung's conditions and items, each followed by its pin rows."""
        rows: list[tuple[str, ...]] = []
        tree = self._branch_tree(rung, path=path, conditions=list(rung._conditions))
        if not tree.items:
            return rows
        for piece in _click_rungs(tree):
            piece_rows = self._render_network(
                piece.conditions,
                piece.items,
                path=piece.path,
                source=piece.source,
                first_marker=self._next_marker(),
            )
            for pins in piece.pins:
                for instruction, instruction_path in pins:
                    piece_rows.extend(self._pin_rows(instruction, path=instruction_path))
            self._assert_rung_height(rows=piece_rows, path=piece.path, source=piece.source)
            rows.extend(piece_rows)
        return rows

    def _branch_tree(self, rung: Rung, *, path: str, conditions: list[Condition]) -> _Branch:
        """*rung* as a :class:`_Branch`.  A branch that holds no output draws no rows."""
        instruction_index_by_id = {id(item): idx for idx, item in enumerate(rung._instructions)}
        branch_index_by_id = {id(item): idx for idx, item in enumerate(rung._branches)}
        items: list[str | _Branch] = []
        pins: list[list[tuple[Any, str]]] = []
        for item in rung._execution_items:
            if isinstance(item, Rung):
                branch_idx = branch_index_by_id.get(id(item))
                if branch_idx is None:
                    self._raise_issue(
                        path=f"{path}.branch",
                        message="Internal error: branch item missing from branch index map.",
                        source=item,
                    )
                branch_path = f"{path}.branch[{branch_idx}]"
                if any(
                    type(instruction).__name__ == "ForLoopInstruction"
                    for instruction in item._instructions
                ):
                    self._raise_issue(
                        path=f"{branch_path}.instruction",
                        message=(
                            "forloop() is not supported inside branch(...) in Click ladder v1 "
                            "export."
                        ),
                        source=item,
                    )
                branch = self._branch_tree(
                    item,
                    path=branch_path,
                    conditions=list(item._conditions[item._branch_condition_start :]),
                )
                if branch.items:
                    items.append(branch)
                    pins.append([pin for item_pins in branch.pins for pin in item_pins])
                continue
            instruction_idx = instruction_index_by_id.get(id(item))
            if instruction_idx is None:
                self._raise_issue(
                    path=f"{path}.instruction",
                    message="Internal error: instruction item missing from instruction index map.",
                    source=item,
                )
            instruction_path = f"{path}.instruction[{instruction_idx}]({type(item).__name__})"
            items.append(self._instruction_token(item, path=instruction_path))
            pins.append([(item, instruction_path)])
        return _Branch(conditions, items, path, rung, pins)

    # ---- Placement ----
    def _place_branch(self, network: _Network, branch: _Branch, start: int, top: int) -> int:
        """Place *branch* from net *start* with its top on row *top*.  Returns its height."""
        net, condition_height = self._place_series(
            network,
            branch.conditions,
            start,
            None,
            top,
            path=f"{branch.path}.condition",
            source=branch.source,
        )
        if net == _RAIL and len(branch.items) > 1:
            # Column A is the rail and takes no vertical.  _click_rungs gives each item on the
            # rail a CLICK rung of its own.
            self._raise_issue(
                path=branch.path,
                message="Internal error: two items on the rail in one CLICK rung.",
                source=branch.source,
            )
        row = top
        for item in branch.items:
            if isinstance(item, str):
                network.sinks.append(_Sink(net, row, item, branch.path, branch.source))
                network.nets[net].touch(row)
                row += 1
            else:
                row += self._place_branch(network, item, net, row)
        return max(condition_height, row - top)

    def _place_series(
        self,
        network: _Network,
        terms: list[Condition],
        start: int,
        end: int | None,
        top: int,
        *,
        path: str,
        source: Any,
    ) -> tuple[int, int]:
        """Place *terms* in series from *start*, all from row *top*.  Returns (end net, height).

        The last term ends on *end* when one is given, so an Or that ends an alternative of another
        Or shares that Or's merge net.
        """
        flat = _series_terms(terms)
        if not flat:
            if end is None:
                return start, 0
            self._add_contact(network, start, end, top, _WIRE, path=path, source=source)
            return end, 1
        net = start
        height = 0
        for index, term in enumerate(flat):
            target = end if index == len(flat) - 1 else None
            net, term_height = self._place_term(
                network, term, net, target, top, path=path, source=source
            )
            height = max(height, term_height)
        return net, height

    def _place_term(
        self,
        network: _Network,
        condition: Condition,
        start: int,
        end: int | None,
        top: int,
        *,
        path: str,
        source: Any,
    ) -> tuple[int, int]:
        if isinstance(condition, AnyCondition):
            if not condition.conditions:
                self._raise_issue(path=path, message="Or() cannot be empty.", source=condition)
            # Every alternative ends on one merge net, so an Or nested in an Or draws as one Or.
            merge = network.new_net(path, condition) if end is None else end
            row = top
            for alternative in condition.conditions:
                _net, height = self._place_series(
                    network, [alternative], start, merge, row, path=path, source=condition
                )
                row += height
            return merge, row - top
        token = self._condition_leaf_token(condition, path=path)
        dst = network.new_net(path, condition) if end is None else end
        self._add_contact(network, start, dst, top, token, path=path, source=condition)
        return dst, 1

    @staticmethod
    def _add_contact(
        network: _Network, src: int, dst: int, row: int, token: str, *, path: str, source: Any
    ) -> None:
        network.contacts.append(_Contact(src, dst, row, token, path, source))
        network.nets[src].touch(row)
        network.nets[dst].touch(row)
        # Contacts are made in source order.  So every contact into a net comes before any contact
        # out of it, and one pass finds each net's longest path.
        network.nets[dst].col = max(network.nets[dst].col, network.nets[src].col + 1)

    # ---- Painting ----
    def _paint(self, network: _Network) -> list[tuple[list[str], str]]:
        """Write each cell from the token table, so the importer reads back the same graph."""
        nets = network.nets
        height = max(
            [contact.row + 1 for contact in network.contacts]
            + [sink.row + 1 for sink in network.sinks]
        )
        across: list[list[str | None]] = [[None] * _CONDITION_COLS for _ in range(height)]
        vertical: list[list[bool]] = [[False] * _CONDITION_COLS for _ in range(height)]
        af: list[str] = [""] * height
        # The net at each point (row, left edge of column).  Two nets on one point would be a
        # short, which a series-parallel rung cannot produce.
        points: dict[tuple[int, int], int] = {}

        def claim(row: int, col: int, net: int, *, where: str, src: Any) -> None:
            if points.setdefault((row, col), net) != net:
                self._raise_issue(
                    path=where,
                    message=f"Conflicting cell content at row {row}, column {col}.",
                    source=src,
                )

        def put(row: int, col: int, value: str, *, where: str, src: Any) -> None:
            if col >= _CONDITION_COLS:
                self._raise_issue(path=where, message="Condition columns exceed AE.", source=src)
            if across[row][col] is not None:
                self._raise_issue(
                    path=where,
                    message=(
                        f"Conflicting cell content at column {col}: "
                        f"{across[row][col]!r} vs {value!r}."
                    ),
                    source=src,
                )
            across[row][col] = value

        for contact in network.contacts:
            col = nets[contact.src].col
            dst_col = nets[contact.dst].col
            put(contact.row, col, contact.token, where=contact.path, src=contact.source)
            for pad in range(col + 1, dst_col):
                put(contact.row, pad, _WIRE, where=contact.path, src=contact.source)
            claim(contact.row, col, contact.src, where=contact.path, src=contact.source)
            for point in range(col + 1, dst_col + 1):
                claim(contact.row, point, contact.dst, where=contact.path, src=contact.source)

        for sink in network.sinks:
            if af[sink.row]:
                self._raise_issue(
                    path=sink.path,
                    message=f"Conflicting AF content: {af[sink.row]!r} vs {sink.af!r}.",
                    source=sink.source,
                )
            af[sink.row] = sink.af
            for col in range(nets[sink.net].col, _CONDITION_COLS):
                put(sink.row, col, _WIRE, where=sink.path, src=sink.source)
                claim(sink.row, col, sink.net, where=sink.path, src=sink.source)

        for index, net in enumerate(nets):
            if index == _RAIL or net.top is None or net.bottom is None:
                continue
            for row in range(net.top, net.bottom):
                if net.col >= _CONDITION_COLS:
                    self._raise_issue(
                        path=net.path, message="Condition columns exceed AE.", source=net.source
                    )
                vertical[row][net.col] = True
                claim(row, net.col, index, where=net.path, src=net.source)
                claim(row + 1, net.col, index, where=net.path, src=net.source)

        rows: list[tuple[list[str], str]] = []
        for row in range(height):
            cells: list[str] = []
            for col in range(_CONDITION_COLS):
                value = across[row][col]
                if not vertical[row][col]:
                    cells.append(value or "")
                elif value is None:
                    cells.append("|")
                elif value == _WIRE:
                    cells.append("T")
                else:
                    cells.append(f"T:{value}")
            rows.append((cells, af[row]))
        return rows


def _click_rungs(tree: _Branch) -> list[_Branch]:
    """*tree* split into the rungs CLICK draws it as.

    Items that hang straight from the rail are separate wires.  CLICK's syntax check makes each
    of them a rung of its own, so the export draws them that way.
    """
    if _series_terms(tree.conditions) or len(tree.items) < 2:
        return [tree]
    pieces: list[_Branch] = []
    for item, pins in zip(tree.items, tree.pins, strict=True):
        if isinstance(item, str):
            item = _Branch([], [item], tree.path, tree.source, [pins])
        pieces.extend(_click_rungs(item))
    return pieces


def _series_terms(terms: list[Condition]) -> list[Condition]:
    """*terms* with each And opened up into its own terms."""
    flat: list[Condition] = []
    for term in terms:
        # A TXT compare against a longer literal is an Or underneath but one compare as written.
        term = text_window_of(term)
        if isinstance(term, AllCondition):
            flat.extend(_series_terms(list(term.conditions)))
        else:
            flat.append(term)
    return flat


# ---- Column A guard ----
def _is_vertical_token(cell: str) -> bool:
    return cell in {"T", "|"} or cell.startswith("T:")


def assert_no_column_a_vertical(rows: list[tuple[str, ...]] | tuple[tuple[str, ...], ...]) -> None:
    """Raise ``ValueError`` if any ladder row has a vertical token in column A.

    Column A is CLICK's left power rail; CLICK rejects ``T``, ``|`` and ``T:``
    there ("Vertical-down tokens are not allowed in Column A").  Rows are
    ``(marker, A, B, ..., AF)``; ``#`` comment rows are skipped.
    """
    for index, row in enumerate(rows):
        if len(row) < 2 or row[0].startswith("#"):
            continue
        if _is_vertical_token(row[1]):
            raise ValueError(
                f"Vertical token {row[1]!r} in column A (row {index}); column A is the "
                "left power rail and cannot carry a T, | or T: marker."
            )


__all__ = [
    "_CONDITION_COLS",
    "_HEADER",
    "_LayoutMixin",
    "assert_no_column_a_vertical",
]
