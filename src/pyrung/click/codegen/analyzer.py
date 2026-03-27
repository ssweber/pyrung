from __future__ import annotations

import warnings
from collections import defaultdict
from dataclasses import dataclass

from pyrung.click.codegen.constants import _CONDITION_COLS, _PIN_RE
from pyrung.click.codegen.models import (
    Leaf,
    Parallel,
    Series,
    SPNode,
    _AnalyzedRung,
    _InstructionInfo,
    _PinInfo,
    _RawRung,
)

# ---------------------------------------------------------------------------
# Phase 2: Analyze Topology → Logical Structure
# ---------------------------------------------------------------------------


def _strip_wire_prefix(cell: str) -> str:
    """Strip ``T:`` wire-down prefix from a contact cell token."""
    if cell.startswith("T:"):
        return cell[2:]
    return cell


def _extract_conditions(row: list[str], start: int, end: int) -> list[str]:
    """Extract non-wire condition tokens from columns [start, end)."""
    tokens: list[str] = []
    for col in range(start, end):
        cell = row[col + 1]  # +1 because row[0] is marker
        if cell and cell not in {"-", "T", "|"}:
            tokens.append(_strip_wire_prefix(cell))
    return tokens


def _cell_at(rows: list[list[str]], r: int, c: int) -> str:
    """Cell value at condition column *c* on row *r* (empty if out of bounds)."""
    if 0 <= r < len(rows) and 0 <= c < _CONDITION_COLS:
        return rows[r][c + 1]  # +1 to skip marker column
    return ""


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------


class _UF:
    """Lightweight Union-Find for wire port merging."""

    def __init__(self) -> None:
        self._parent: list[int] = []

    def make(self) -> int:
        n = len(self._parent)
        self._parent.append(n)
        return n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self._parent[b] = a


# ---------------------------------------------------------------------------
# Phase 1: Grid → Multigraph
# ---------------------------------------------------------------------------

_WIRE_CELLS = {"-", "T", "|"}


@dataclass
class _Edge:
    """A labeled edge in the multigraph."""

    src: int
    dst: int
    tree: SPNode
    min_row: int
    min_col: int


def _cell_has_down(cell: str) -> bool:
    """Does this cell have a down exit?"""
    if not cell:
        return False
    if cell.startswith("T:"):
        return True
    return cell in ("T", "|")


def _grid_to_graph(
    rows: list[list[str]],
    pin_row_set: set[int],
) -> tuple[int | None, list[tuple[int, str, int]], list[_Edge]]:
    """Convert grid to multigraph.

    Returns ``(source_node, sinks, edges)`` where sinks is a list of
    ``(node_id, af_token, af_row)`` tuples.
    """
    uf = _UF()
    n_rows = len(rows)

    # Port IDs for each occupied cell: (r, c) -> (left, right, down)
    # down is only meaningful for T/|/T:token cells.
    left_port: dict[tuple[int, int], int] = {}
    right_port: dict[tuple[int, int], int] = {}
    down_port: dict[tuple[int, int], int] = {}

    # 1. Assign ports
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if not cell:
                continue
            if cell in _WIRE_CELLS:
                # Wire cell: single conductor (left = right)
                p = uf.make()
                left_port[r, c] = p
                right_port[r, c] = p
                if cell in ("T", "|"):
                    down_port[r, c] = p
            elif cell.startswith("T:"):
                # T:token — left = down (input side), right is separate (output)
                lp = uf.make()
                rp = uf.make()
                left_port[r, c] = lp
                right_port[r, c] = rp
                down_port[r, c] = lp
            else:
                # Content cell — condition separates left from right
                lp = uf.make()
                rp = uf.make()
                left_port[r, c] = lp
                right_port[r, c] = rp

    # 2. Claim down-connections.
    # Straight-down (same col) claims target's left-port (input bus).
    # Diagonal (c-1 fallback) claims target's right-port (output bus).
    # These are independent — a target can have both a left and right claim.
    left_claimed: dict[tuple[int, int], tuple[int, int]] = {}  # target → claimant
    right_claimed: dict[tuple[int, int], tuple[int, int]] = {}  # target → claimant
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if not _cell_has_down(cell):
                continue
            # Try target at (r+1, c), fallback (r+1, c-1)
            for tc in (c, c - 1):
                target = (r + 1, tc)
                if target[0] in pin_row_set:
                    continue
                if target not in left_port:
                    continue
                is_diagonal = tc != c
                if is_diagonal:
                    if target not in right_claimed:
                        right_claimed[target] = (r, c)
                        break
                else:
                    if target not in left_claimed:
                        left_claimed[target] = (r, c)
                        break

    # Union down-port of claimant with target port.
    for target, claimant in left_claimed.items():
        dp = down_port.get(claimant)
        tp = left_port.get(target)
        if dp is not None and tp is not None:
            uf.union(dp, tp)
    for target, claimant in right_claimed.items():
        dp = down_port.get(claimant)
        tp = right_port.get(target)
        if dp is not None and tp is not None:
            uf.union(dp, tp)

    # 2b. Left power rail: all column-0 cells share the same left-port
    rail_port: int | None = None
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        if (r, 0) in left_port:
            if rail_port is None:
                rail_port = left_port[r, 0]
            else:
                uf.union(rail_port, left_port[r, 0])

    # 3. Merge horizontal adjacency
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        for c in range(_CONDITION_COLS - 1):
            if (r, c) in right_port and (r, c + 1) in left_port:
                uf.union(right_port[r, c], left_port[r, c + 1])

    # 4. Build edges from content cells
    edges: list[_Edge] = []
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if not cell or cell in _WIRE_CELLS:
                continue
            label = _strip_wire_prefix(cell)
            src = uf.find(left_port[r, c])
            dst = uf.find(right_port[r, c])
            if src != dst:
                edges.append(_Edge(src, dst, Leaf(label, r, c), r, c))

    # 5. Identify source and sinks
    # Source = left power rail if any column-0 cells exist, else leftmost port
    source: int | None = None
    if rail_port is not None:
        source = uf.find(rail_port)
    else:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            for c in range(_CONDITION_COLS):
                if (r, c) in left_port:
                    source = uf.find(left_port[r, c])
                    break
            if source is not None:
                break

    # Sinks = right-port of the last occupied condition column on each AF row
    sinks: list[tuple[int, str, int]] = []
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        af = rows[r][-1]
        if not af or af.startswith("."):
            continue
        # Find rightmost occupied condition column
        last_c = -1
        for c in range(_CONDITION_COLS - 1, -1, -1):
            if (r, c) in right_port:
                last_c = c
                break
        if last_c >= 0:
            sink_node = uf.find(right_port[r, last_c])
            sinks.append((sink_node, af, r))

    return source, sinks, edges


# ---------------------------------------------------------------------------
# Phase 2: SP Reduction
# ---------------------------------------------------------------------------


def _min_row(tree: SPNode) -> int:
    """Minimum row position in an SP tree (for sort stability)."""
    if isinstance(tree, Leaf):
        return tree.row
    return min(_min_row(c) for c in tree.children) if tree.children else 0


def _min_col(tree: SPNode) -> int:
    """Minimum column position in an SP tree (for sort stability)."""
    if isinstance(tree, Leaf):
        return tree.col
    return min(_min_col(c) for c in tree.children) if tree.children else 0


def _flatten_series(node: SPNode) -> list[SPNode]:
    """Flatten nested Series into a single list."""
    if isinstance(node, Series):
        result: list[SPNode] = []
        for child in node.children:
            result.extend(_flatten_series(child))
        return result
    return [node]


def _flatten_parallel(node: SPNode) -> list[SPNode]:
    """Flatten nested Parallel into a single list."""
    if isinstance(node, Parallel):
        result: list[SPNode] = []
        for child in node.children:
            result.extend(_flatten_parallel(child))
        return result
    return [node]


def _make_series(children: list[SPNode]) -> SPNode:
    """Create a Series, flattening nested Series nodes."""
    flat: list[SPNode] = []
    for c in children:
        flat.extend(_flatten_series(c))
    if len(flat) == 1:
        return flat[0]
    return Series(flat)


def _make_parallel(children: list[SPNode]) -> SPNode:
    """Create a Parallel, flattening nested Parallel nodes."""
    flat: list[SPNode] = []
    for c in children:
        flat.extend(_flatten_parallel(c))
    if len(flat) == 1:
        return flat[0]
    return Parallel(flat)


def _sp_reduce(
    source: int,
    sink: int,
    all_edges: list[_Edge],
) -> SPNode | None:
    """Reduce subgraph between *source* and *sink* to an SP tree."""
    if source == sink:
        return None

    # Extract reachable subgraph: forward from source AND backward from sink
    def _reachable_forward(start: int, edges: list[_Edge]) -> set[int]:
        adj: dict[int, list[int]] = defaultdict(list)
        for e in edges:
            adj[e.src].append(e.dst)
        visited: set[int] = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            stack.extend(adj[n])
        return visited

    def _reachable_backward(start: int, edges: list[_Edge]) -> set[int]:
        adj: dict[int, list[int]] = defaultdict(list)
        for e in edges:
            adj[e.dst].append(e.src)
        visited: set[int] = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            stack.extend(adj[n])
        return visited

    fwd = _reachable_forward(source, all_edges)
    bwd = _reachable_backward(sink, all_edges)
    reachable = fwd & bwd

    edges = [e for e in all_edges if e.src in reachable and e.dst in reachable]
    if not edges:
        return None

    # Reduction loop
    for _ in range(len(edges) * len(edges) + 10):
        changed = False

        # Rule A: Parallel — merge edges between same (src, dst)
        groups: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, e in enumerate(edges):
            groups[e.src, e.dst].append(i)

        new_edges: list[_Edge] = []
        consumed: set[int] = set()
        for key, indices in groups.items():
            if len(indices) >= 2:
                changed = True
                consumed.update(indices)
                children = [edges[i].tree for i in indices]
                children.sort(key=_min_row)
                merged = _make_parallel(children)
                mr = min(edges[i].min_row for i in indices)
                mc = min(edges[i].min_col for i in indices)
                new_edges.append(_Edge(key[0], key[1], merged, mr, mc))
        for i, e in enumerate(edges):
            if i not in consumed:
                new_edges.append(e)
        edges = new_edges

        # Rule B: Series — degree-2 non-terminal node
        in_edges: dict[int, list[int]] = defaultdict(list)
        out_edges: dict[int, list[int]] = defaultdict(list)
        for i, e in enumerate(edges):
            out_edges[e.src].append(i)
            in_edges[e.dst].append(i)

        series_node: int | None = None
        for node in set(in_edges.keys()) | set(out_edges.keys()):
            if node == source or node == sink:
                continue
            if len(in_edges[node]) == 1 and len(out_edges[node]) == 1:
                series_node = node
                break

        if series_node is not None:
            changed = True
            in_idx = in_edges[series_node][0]
            out_idx = out_edges[series_node][0]
            e_in = edges[in_idx]
            e_out = edges[out_idx]
            children = [e_in.tree, e_out.tree]
            merged = _make_series(children)
            mr = min(e_in.min_row, e_out.min_row)
            mc = min(e_in.min_col, e_out.min_col)
            new_edge = _Edge(e_in.src, e_out.dst, merged, mr, mc)
            drop = {in_idx, out_idx}
            edges = [e for i, e in enumerate(edges) if i not in drop] + [new_edge]

        # Check termination
        if len(edges) == 1 and edges[0].src == source and edges[0].dst == sink:
            return edges[0].tree

        if not changed:
            break

    # Check if we're done after the loop
    if len(edges) == 1 and edges[0].src == source and edges[0].dst == sink:
        return edges[0].tree

    # Non-SP fallback: Shannon expansion
    if edges:
        warnings.warn(
            "Rung contains bridge topology; resolved via Shannon expansion",
            stacklevel=2,
        )
        e = edges[0]

        # True branch: short-circuit edge (merge src and dst)
        true_edges: list[_Edge] = []
        for ed in edges[1:]:
            s = e.src if ed.src == e.dst else ed.src
            d = e.src if ed.dst == e.dst else ed.dst
            true_edges.append(_Edge(s, d, ed.tree, ed.min_row, ed.min_col))
        true_sink = e.src if sink == e.dst else sink
        true_tree = _sp_reduce(source, true_sink, true_edges)

        # False branch: delete edge
        false_edges = list(edges[1:])
        false_tree = _sp_reduce(source, sink, false_edges)

        if true_tree is not None and false_tree is not None:
            return _make_parallel(
                [
                    _make_series([e.tree, true_tree]),
                    false_tree,
                ]
            )
        if true_tree is not None:
            return _make_series([e.tree, true_tree])
        if false_tree is not None:
            return false_tree
        return e.tree

    return None


# ---------------------------------------------------------------------------
# Phase 3: Multi-Output Grouping
# ---------------------------------------------------------------------------


def _trees_equal(a: SPNode | None, b: SPNode | None) -> bool:
    """Structural equality of two SP trees (labels only, ignoring row/col)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if type(a) is not type(b):
        return False
    if isinstance(a, Leaf) and isinstance(b, Leaf):
        return a.label == b.label
    if isinstance(a, (Series, Parallel)) and isinstance(b, (Series, Parallel)):
        if len(a.children) != len(b.children):
            return False
        return all(_trees_equal(ac, bc) for ac, bc in zip(a.children, b.children, strict=True))
    return False


def _group_outputs(
    trees: list[tuple[SPNode | None, str, int]],
) -> tuple[SPNode | None, list[_InstructionInfo], list[int]]:
    """Group per-output SP trees into condition_tree + instructions."""
    if not trees:
        return None, [], []

    if len(trees) == 1:
        tree, af, af_row = trees[0]
        return tree, [_InstructionInfo(af, None, [])], [af_row]

    # Check if all outputs have identical trees
    all_same = all(_trees_equal(trees[0][0], t[0]) for t in trees[1:])
    if all_same:
        cond_tree = trees[0][0]
        instructions = [_InstructionInfo(af, None, []) for _, af, _ in trees]
        af_rows = [af_row for _, _, af_row in trees]
        return cond_tree, instructions, af_rows

    # Normalize all trees to Series for lockstep prefix comparison.
    # Leaf → Series([Leaf]), Parallel → Series([Parallel]), Series stays.
    def _as_series_children(t: SPNode | None) -> list[SPNode]:
        if t is None:
            return []
        if isinstance(t, Series):
            return list(t.children)
        return [t]

    child_lists = [_as_series_children(t[0]) for t in trees]
    if all(child_lists):
        min_len = min(len(cl) for cl in child_lists)
        shared_count = 0
        for i in range(min_len):
            ref = child_lists[0][i]
            if all(_trees_equal(ref, cl[i]) for cl in child_lists):
                shared_count += 1
            else:
                break

        if shared_count > 0:
            shared_children = child_lists[0][:shared_count]
            cond_tree = _make_series(shared_children)

            instructions: list[_InstructionInfo] = []
            af_rows: list[int] = []
            for idx, (_tree, af, af_row) in enumerate(trees):
                remaining = child_lists[idx][shared_count:]
                if remaining:
                    branch_tree = _make_series(remaining)
                else:
                    branch_tree = None
                instructions.append(_InstructionInfo(af, branch_tree, []))
                af_rows.append(af_row)
            return cond_tree, instructions, af_rows

    # No shared structure
    instructions = []
    af_rows = []
    for tree, af, af_row in trees:
        instructions.append(_InstructionInfo(af, tree, []))
        af_rows.append(af_row)
    return None, instructions, af_rows


# --- Top-level Phase 2 entry points -----------------------------------------


def _analyze_rungs(raw_rungs: list[_RawRung]) -> list[_AnalyzedRung]:
    """Analyze topology of each rung."""
    analyzed: list[_AnalyzedRung] = []

    # Strip trailing end() rung (auto-appended by to_ladder, not part of user logic).
    if raw_rungs:
        last = raw_rungs[-1]
        last_af = last.rows[0][-1] if last.rows else ""
        if last_af == "end()":
            raw_rungs = raw_rungs[:-1]

    # First pass: detect for/next grouping
    i = 0
    while i < len(raw_rungs):
        rung = raw_rungs[i]
        af0 = rung.rows[0][-1] if rung.rows else ""

        if af0.startswith("for("):
            # for/next block
            analyzed.append(_analyze_single_rung(rung, is_forloop_start=True))
            i += 1
            # Collect body rungs until next()
            while i < len(raw_rungs):
                body_rung = raw_rungs[i]
                body_af = body_rung.rows[0][-1] if body_rung.rows else ""
                if body_af == "next()":
                    analyzed.append(_analyze_single_rung(body_rung, is_forloop_next=True))
                    i += 1
                    break
                analyzed.append(_analyze_single_rung(body_rung, is_forloop_body=True))
                i += 1
        else:
            analyzed.append(_analyze_single_rung(rung))
            i += 1

    return analyzed


def _analyze_single_rung(
    rung: _RawRung,
    *,
    is_forloop_start: bool = False,
    is_forloop_body: bool = False,
    is_forloop_next: bool = False,
) -> _AnalyzedRung:
    """Analyze a single rung's topology via SP graph reduction."""
    comment = "\n".join(rung.comment_lines) if rung.comment_lines else None
    rows = rung.rows

    if not rows:
        return _AnalyzedRung(
            comment=comment,
            condition_tree=None,
            instructions=[],
        )

    # Separate pin rows from content rows
    pin_row_set = {i for i, row in enumerate(rows) if _is_pin_row(row)}

    # Phase 1: Grid → Graph
    source, sinks, edges = _grid_to_graph(rows, pin_row_set)

    if source is None or not sinks:
        # No graph built — handle rows with only an AF and no conditions
        for i, row in enumerate(rows):
            if i not in pin_row_set:
                af = row[-1]
                if af and not af.startswith("."):
                    pins = _collect_pins([rows[j] for j in sorted(pin_row_set)])
                    return _AnalyzedRung(
                        comment=comment,
                        condition_tree=None,
                        instructions=[_InstructionInfo(af_token=af, branch_tree=None, pins=pins)],
                        is_forloop_start=is_forloop_start,
                        is_forloop_body=is_forloop_body,
                        is_forloop_next=is_forloop_next,
                    )
        return _AnalyzedRung(
            comment=comment,
            condition_tree=None,
            instructions=[],
        )

    # Phase 2: SP Reduction (per output)
    output_trees: list[tuple[SPNode | None, str, int]] = []
    for sink_node, af_token, af_row in sinks:
        tree = _sp_reduce(source, sink_node, edges)
        output_trees.append((tree, af_token, af_row))

    # Phase 3: Multi-Output Grouping
    condition_tree, instructions, af_rows = _group_outputs(output_trees)

    # Attach pin rows to their nearest preceding instruction (by row index)
    if pin_row_set and instructions:
        for pin_idx in sorted(pin_row_set):
            best = -1
            for i, ar in enumerate(af_rows):
                if ar <= pin_idx:
                    best = i
            if best >= 0:
                pin_row = rows[pin_idx]
                af = pin_row[-1]
                match = _PIN_RE.match(af)
                if match:
                    pin_conds = _extract_conditions(pin_row, 0, _CONDITION_COLS)
                    instructions[best].pins.append(
                        _PinInfo(
                            name=match.group(1),
                            arg=match.group(2),
                            conditions=pin_conds,
                        )
                    )

    return _AnalyzedRung(
        comment=comment,
        condition_tree=condition_tree,
        instructions=instructions,
        is_forloop_start=is_forloop_start,
        is_forloop_body=is_forloop_body,
        is_forloop_next=is_forloop_next,
    )


def _collect_pins(pin_rows: list[list[str]]) -> list[_PinInfo]:
    """Extract pin info from pin rows."""
    pins: list[_PinInfo] = []
    for row in pin_rows:
        af = row[-1]
        match = _PIN_RE.match(af)
        if match:
            conditions = _extract_conditions(row, 0, _CONDITION_COLS)
            pins.append(
                _PinInfo(
                    name=match.group(1),
                    arg=match.group(2),
                    conditions=conditions,
                )
            )
    return pins
