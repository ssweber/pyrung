"""Rung analysis: grid -> logical structure.

Converts a ladder rung's cell grid into a Series/Parallel condition tree
via three stages.

Example rung grid (two parallel contacts A/B, then C in series):

    R, A, T, C, AF
     , B,  ,  ,

    1. Wiring:    Assign ports to each cell, merge wire cells with
                  union-find, and emit a labeled edge for each contact.
                  The result is a multigraph from source (power rail)
                  to sink (AF output).

    2. Reduction: Repeatedly apply two rules until one edge remains:
                  - Parallel: edges sharing the same endpoints merge
                    into "A or B".
                  - Series: a node with exactly one in-edge and one
                    out-edge collapses into "A then B".
                  Result: Series(Parallel(A, B), C)

    3. Grouping:  When a rung has multiple AF outputs, factor out
                  shared condition prefixes into a single tree with
                  per-output branches.
"""

from __future__ import annotations

import warnings
from collections import defaultdict
from dataclasses import dataclass

from pyrung.click._topology import (
    Leaf,
    Parallel,
    Series,
    SPNode,
    factor_outputs,
    make_compound,
    trees_equal,
)
from pyrung.click.codegen.constants import _ADJACENCY, _CONDITION_COLS, _PIN_RE
from pyrung.click.codegen.models import (
    RungRole,
    _AnalyzedRung,
    _InstructionInfo,
    _PinInfo,
    _RawRung,
)

# ---------------------------------------------------------------------------
# Rung Analysis
# ---------------------------------------------------------------------------


def _strip_wire_prefix(cell: str) -> str:
    """Strip ``T:`` wire-down prefix from a contact cell token."""
    if cell.startswith("T:"):
        return cell[2:]
    return cell


def _warn_bypassed_contact(label: str) -> None:
    """Warn when imported topology shorts around a contact cell."""
    warnings.warn(
        f"Imported ladder topology bypasses contact {label!r}; "
        "this condition was omitted from generated logic.",
        stacklevel=3,
    )


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


def _cell_sides(cell: str) -> tuple[str, ...]:
    """Return the exposed sides for a Click ladder cell token."""
    if not cell:
        return ()
    if cell.startswith("T:"):
        return ("left", "right", "down")
    if cell in _ADJACENCY:
        return _ADJACENCY[cell]
    return ("left", "right")


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


def _rows_are_blank(rows: list[list[str]]) -> bool:
    """Return True when every condition and AF cell in the rung is blank."""
    for row in rows:
        if any(cell for cell in row[1:]):
            return False
    return True


def _rows_have_content(rows: list[list[str]]) -> bool:
    """Return True when the rung contains any nonblank condition/AF content."""
    return not _rows_are_blank(rows)


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
# Wiring: Grid -> Multigraph
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
    return "down" in _cell_sides(cell)


def _grid_to_graph(
    rows: list[list[str]],
    pin_row_set: set[int],
) -> tuple[int | None, list[tuple[int, str, int]], list[_Edge], dict[int, int]]:
    """Convert grid to multigraph.

    Returns ``(source_node, sinks, edges, pin_sinks)`` where sinks is a list
    of ``(node_id, af_token, af_row)`` tuples and pin_sinks maps pin row
    index to its rightmost sink node.
    """
    uf = _UF()
    n_rows = len(rows)

    # Port IDs for each occupied cell: (r, c) -> (left, right, down)
    # down is only meaningful for T/|/T:token cells.
    left_port: dict[tuple[int, int], int] = {}
    right_port: dict[tuple[int, int], int] = {}
    down_port: dict[tuple[int, int], int] = {}

    # 1. Assign ports
    # Pin rows participate in wiring (their T junctions may connect down to
    # non-pin rows) but never produce sinks.
    for r in range(n_rows):
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
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if not _cell_has_down(cell):
                continue
            # Try target at (r+1, c), fallback (r+1, c-1)
            for tc in (c, c - 1):
                target = (r + 1, tc)
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
        if (r, 0) in left_port:
            if rail_port is None:
                rail_port = left_port[r, 0]
            else:
                uf.union(rail_port, left_port[r, 0])

    # 3. Merge horizontal adjacency
    for r in range(n_rows):
        for c in range(_CONDITION_COLS - 1):
            left_cell = _cell_at(rows, r, c)
            right_cell = _cell_at(rows, r, c + 1)
            if (
                "right" in _cell_sides(left_cell)
                and "left" in _cell_sides(right_cell)
                and (r, c) in right_port
                and (r, c + 1) in left_port
            ):
                uf.union(right_port[r, c], left_port[r, c + 1])

    # 4. Build edges from content cells
    edges: list[_Edge] = []
    for r in range(n_rows):
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if not cell or cell in _WIRE_CELLS:
                continue
            label = _strip_wire_prefix(cell)
            src = uf.find(left_port[r, c])
            dst = uf.find(right_port[r, c])
            if src != dst:
                edges.append(_Edge(src, dst, Leaf(label, r, c), r, c))
            else:
                _warn_bypassed_contact(label)

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

    # AF-only rows are unconditional: they sink directly to the source/rail node.
    sinks: list[tuple[int, str, int]] = []
    if source is None:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            af = rows[r][-1]
            if af and not af.startswith("."):
                source = uf.make()
                break

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
        elif source is not None:
            sink_node = source
        else:
            continue
        sinks.append((sink_node, af, r))

    # 6. Pin sinks — rightmost occupied right_port per pin row.
    pin_sinks: dict[int, int] = {}
    for r in pin_row_set:
        last_c = -1
        for c in range(_CONDITION_COLS - 1, -1, -1):
            if (r, c) in right_port:
                last_c = c
                break
        if last_c >= 0:
            pin_sinks[r] = uf.find(right_port[r, last_c])

    return source, sinks, edges, pin_sinks


# ---------------------------------------------------------------------------
# SP Reduction
# ---------------------------------------------------------------------------


def _min_attr(tree: SPNode, attr: str) -> int:
    """Minimum leaf attribute in an SP tree (for sort stability)."""
    if isinstance(tree, Leaf):
        return int(getattr(tree, attr))
    return min((_min_attr(c, attr) for c in tree.children), default=0)


def _parallel_sort_key(tree: SPNode) -> int:
    """Sort key for Parallel children: minimum row index."""
    return _min_attr(tree, "row")


def _reachable(start: int, edges: list[_Edge], *, reverse: bool = False) -> set[int]:
    """Return nodes reachable from *start* in the requested edge direction."""
    adj: dict[int, list[int]] = defaultdict(list)
    for edge in edges:
        src, dst = (edge.dst, edge.src) if reverse else (edge.src, edge.dst)
        adj[src].append(dst)
    visited: set[int] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(adj[node])
    return visited


def _pick_shannon_edge(source: int, sink: int, edges: list[_Edge]) -> _Edge:
    """Pick a stable expansion edge for a non-SP subgraph.

    Prefer an internal edge from a split node into a join node: that is the
    characteristic "bridge" edge in the minimal non-SP shape, and expanding it
    tends to keep both recursive branches simple and order-independent.
    """
    in_degree: dict[int, int] = defaultdict(int)
    out_degree: dict[int, int] = defaultdict(int)
    for edge in edges:
        out_degree[edge.src] += 1
        in_degree[edge.dst] += 1

    def _priority(edge: _Edge) -> tuple[int, int, int, int, int, int, int, int, int]:
        src_is_split = out_degree[edge.src] > 1
        dst_is_join = in_degree[edge.dst] > 1
        is_internal = edge.src != source and edge.dst != sink
        return (
            0 if is_internal and src_is_split and dst_is_join else 1,
            0 if is_internal else 1,
            0 if src_is_split else 1,
            0 if dst_is_join else 1,
            -(out_degree[edge.src] + in_degree[edge.dst]),
            edge.min_col,
            edge.min_row,
            edge.src,
            edge.dst,
        )

    return min(edges, key=_priority)


def _sp_reduce(
    source: int,
    sink: int,
    all_edges: list[_Edge],
) -> SPNode | None:
    """Reduce subgraph between *source* and *sink* to an SP tree.

    Non-SP bridge topologies fall back to Shannon expansion. That preserves
    rung semantics but not the original bridge drawing, so the first import
    may re-export as an equivalent pure SP tree. Once that normalized form is
    written back out, later CSV -> SP -> CSV passes stay stable.
    """
    if source == sink:
        return None

    # Extract reachable subgraph: forward from source and backward from sink.
    fwd = _reachable(source, all_edges)
    bwd = _reachable(sink, all_edges, reverse=True)
    reachable = fwd & bwd

    edges = [e for e in all_edges if e.src in reachable and e.dst in reachable]
    if not edges:
        return None

    # Reduction loop. Genuine SP reductions always shrink the edge set, so a
    # progress check is more reliable than a guessed iteration budget.
    while True:
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
                merged = make_compound(children, Parallel, sort_key=_parallel_sort_key)
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
            merged = make_compound(children, Series)
            mr = min(e_in.min_row, e_out.min_row)
            mc = min(e_in.min_col, e_out.min_col)
            new_edge = _Edge(e_in.src, e_out.dst, merged, mr, mc)
            drop = {in_idx, out_idx}
            edges = [e for i, e in enumerate(edges) if i not in drop] + [new_edge]

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
        e = _pick_shannon_edge(source, sink, edges)
        remaining_edges = [ed for ed in edges if ed is not e]

        # True branch: short-circuit edge (merge src and dst)
        true_edges: list[_Edge] = []
        for ed in remaining_edges:
            s = e.src if ed.src == e.dst else ed.src
            d = e.src if ed.dst == e.dst else ed.dst
            true_edges.append(_Edge(s, d, ed.tree, ed.min_row, ed.min_col))
        true_sink = e.src if sink == e.dst else sink
        true_tree = _sp_reduce(source, true_sink, true_edges)

        # False branch: delete edge
        false_edges = list(remaining_edges)
        false_tree = _sp_reduce(source, sink, false_edges)

        if true_tree is not None and false_tree is not None:
            return make_compound(
                [
                    make_compound([e.tree, true_tree], Series),
                    false_tree,
                ],
                Parallel,
                sort_key=_parallel_sort_key,
            )
        if true_tree is not None:
            return make_compound([e.tree, true_tree], Series)
        if false_tree is not None:
            return false_tree
        return e.tree

    return None


# ---------------------------------------------------------------------------
# Output Grouping
# ---------------------------------------------------------------------------


# Re-export for backward compatibility (tests import from here).
_trees_equal = trees_equal


def _group_outputs(
    trees: list[tuple[SPNode | None, str, int]],
) -> tuple[SPNode | None, list[_InstructionInfo], list[int]]:
    """Group per-output SP trees into top-level condition_tree + instructions."""
    if not trees:
        return None, [], []

    if len(trees) == 1:
        tree, af, af_row = trees[0]
        return tree, [_InstructionInfo(af, None, [])], [af_row]

    result = factor_outputs([t[0] for t in trees])

    if result.shared:
        cond_tree = make_compound(result.shared, Series)
        instructions: list[_InstructionInfo] = []
        af_rows: list[int] = []
        for idx, (_tree, af, af_row) in enumerate(trees):
            remaining = result.branches[idx]
            branch_tree = make_compound(remaining, Series) if remaining else None
            instructions.append(_InstructionInfo(af, branch_tree, []))
            af_rows.append(af_row)
        return cond_tree, instructions, af_rows

    # No shared prefix — each output gets its full tree as branch_tree.
    instructions = []
    af_rows = []
    for tree, af, af_row in trees:
        instructions.append(_InstructionInfo(af, tree, []))
        af_rows.append(af_row)
    return None, instructions, af_rows


# --- Analyzer entry points --------------------------------------------------


def _split_continued(rung: _AnalyzedRung) -> list[_AnalyzedRung]:
    """Split a rung with continued-style wires into primary + continued rungs.

    The motivating Click-only shape is a shared wire that feeds a terminal
    instruction pin and also drives a sibling output. In pyrung, counters and
    RTON-style ``on_delay(...).reset(...)`` are terminal in-flow, and their
    reset conditions render inside the call rather than as peer rows, so that
    layout cannot live in one DSL rung. Splitting off the sibling as
    ``.continued()`` preserves the shared snapshot and stays expressible.

    The current trigger is still a structural proxy: no shared
    ``condition_tree`` and every instruction carries its own ``branch_tree``.
    That matches exporter-produced continued rows and the terminal-pin case,
    but hand-authored CSV can still over- or under-trigger it.
    """
    if rung.condition_tree is not None:
        return [rung]
    if len(rung.instructions) < 2:
        return [rung]
    if not all(instr.branch_tree is not None for instr in rung.instructions):
        return [rung]

    # First instruction → primary rung
    first = rung.instructions[0]
    result: list[_AnalyzedRung] = [
        _AnalyzedRung(
            comment=rung.comment,
            condition_tree=first.branch_tree,
            instructions=[_InstructionInfo(first.af_token, None, first.pins)],
        )
    ]

    remaining = rung.instructions[1:]
    if len(remaining) == 1:
        instr = remaining[0]
        result.append(
            _AnalyzedRung(
                comment=None,
                condition_tree=instr.branch_tree,
                instructions=[_InstructionInfo(instr.af_token, None, instr.pins)],
                is_continued=True,
            )
        )
    else:
        # Re-group remaining outputs — they may share a prefix (→ branches)
        trees = [(instr.branch_tree, instr.af_token, i) for i, instr in enumerate(remaining)]
        cond_tree, grouped, _ = _group_outputs(trees)

        if (
            cond_tree is None
            and len(grouped) > 1
            and all(g.branch_tree is not None for g in grouped)
        ):
            # Still no shared prefix → each becomes its own continued rung
            for instr in remaining:
                result.append(
                    _AnalyzedRung(
                        comment=None,
                        condition_tree=instr.branch_tree,
                        instructions=[_InstructionInfo(instr.af_token, None, instr.pins)],
                        is_continued=True,
                    )
                )
        else:
            # Shared prefix → single continued rung (may have branches)
            for gi, orig in zip(grouped, remaining, strict=True):
                gi.pins = orig.pins
            result.append(
                _AnalyzedRung(
                    comment=None,
                    condition_tree=cond_tree,
                    instructions=grouped,
                    is_continued=True,
                )
            )

    return result


def _analyze_rungs(raw_rungs: list[_RawRung]) -> list[_AnalyzedRung]:
    """Analyze topology of each rung."""
    analyzed: list[_AnalyzedRung] = []

    # Strip trailing end() rung (auto-appended by pyrung_to_ladder, not part of user logic).
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
            analyzed.append(_analyze_single_rung(rung, role=RungRole.FORLOOP_START))
            i += 1
            # Collect body rungs until next()
            while i < len(raw_rungs):
                body_rung = raw_rungs[i]
                body_af = body_rung.rows[0][-1] if body_rung.rows else ""
                if body_af == "next()":
                    analyzed.append(_analyze_single_rung(body_rung, role=RungRole.FORLOOP_NEXT))
                    i += 1
                    break
                analyzed.append(_analyze_single_rung(body_rung, role=RungRole.FORLOOP_BODY))
                i += 1
        else:
            analyzed.extend(_split_continued(_analyze_single_rung(rung)))
            i += 1

    return analyzed


def _analyze_single_rung(
    rung: _RawRung,
    *,
    role: RungRole = RungRole.NORMAL,
) -> _AnalyzedRung:
    """Analyze a single rung's topology via SP graph reduction."""
    # Strip trailing empty comment lines (Click IDE visual padding).
    cleaned = list(rung.comment_lines) if rung.comment_lines else []
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    comment = "\n".join(cleaned) if cleaned else None
    rows = rung.rows

    if not rows:
        return _AnalyzedRung(
            comment=comment,
            condition_tree=None,
            instructions=[],
            role=role,
        )

    # Separate pin rows from content rows
    pin_row_set = {i for i, row in enumerate(rows) if _is_pin_row(row)}

    # Wiring: Grid -> Multigraph
    source, sinks, edges, pin_sinks = _grid_to_graph(rows, pin_row_set)

    if source is None or not sinks:
        if _rows_are_blank(rows):
            return _AnalyzedRung(
                comment=comment,
                condition_tree=None,
                instructions=[_InstructionInfo("NOP", None, [])],
                role=role,
            )
        if _rows_have_content(rows):
            raise ValueError(
                "Rung contains condition/output content that did not resolve to a complete output object."
            )
        return _AnalyzedRung(
            comment=comment,
            condition_tree=None,
            instructions=[],
            role=role,
        )

    # SP Reduction (per output)
    output_trees: list[tuple[SPNode | None, str, int]] = []
    for sink_node, af_token, af_row in sinks:
        tree = _sp_reduce(source, sink_node, edges)
        output_trees.append((tree, af_token, af_row))

    # Output Grouping
    condition_tree, instructions, af_rows = _group_outputs(output_trees)

    # Exporter pins immediately follow their owning AF row. Walk the raw rows
    # in order so malformed layouts fail loudly instead of silently attaching
    # to the wrong instruction.
    # Compute SP trees for pin row conditions.
    pin_trees: dict[int, SPNode | None] = {}
    if pin_sinks and source is not None:
        for pr, ps in pin_sinks.items():
            pin_trees[pr] = _sp_reduce(source, ps, edges)

    if pin_row_set and instructions:
        instruction_by_row = {af_row: index for index, af_row in enumerate(af_rows)}
        current_instruction: int | None = None

        for row_index, row in enumerate(rows):
            af = row[-1]

            if row_index in pin_row_set:
                if current_instruction is None:
                    raise ValueError(
                        f"Pin row {row_index} must immediately follow its owning instruction row."
                    )

                match = _PIN_RE.match(af)
                if match:
                    pin_conds = _extract_conditions(row, 0, _CONDITION_COLS)
                    pin_tree = pin_trees.get(row_index)
                    instructions[current_instruction].pins.append(
                        _PinInfo(
                            name=match.group(1),
                            arg=match.group(2),
                            conditions=pin_conds,
                            condition_tree=pin_tree,
                        )
                    )
                continue

            if af and not af.startswith("."):
                current_instruction = instruction_by_row.get(row_index)
                continue

            current_instruction = None

    return _AnalyzedRung(
        comment=comment,
        condition_tree=condition_tree,
        instructions=instructions,
        role=role,
    )
