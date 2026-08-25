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
from collections.abc import Iterator
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


def _warn_dropped_contact(label: str, row: int) -> None:
    """Warn when a source contact reaches no output and is omitted.

    Unlike :func:`_warn_bypassed_contact` (a ``src == dst`` self-short), this
    fires for a contact that is present in the source grid but connects to no
    output — a dead-end edge pruned by the reachable-subgraph intersection in
    :func:`_sp_reduce`. The detector only observes the drop; the cause is
    either a malformed source ladder or a codec/wiring bug, so the message
    names both and prescribes no fix.
    """
    warnings.warn(
        f"Imported ladder rung drops contact {label!r} (row {row}): it is present in the "
        "source grid but is not connected into any output, so it was omitted from generated "
        "logic. The source ladder is malformed or the codec produced invalid topology.",
        stacklevel=2,
    )


def _extract_conditions(row: list[str], start: int, end: int) -> list[str]:
    """Extract non-wire condition tokens from columns [start, end)."""
    tokens: list[str] = []
    for col in range(start, end):
        cell = row[col + 1]  # +1 because row[0] is marker
        if cell and cell not in {"-", "T", "|"}:
            tokens.append(_strip_wire_prefix(cell))
    return tokens


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


def _annotate_rung_rows(rows: list[list[str]], highlight_row: int) -> str:
    """Render rung rows as CSV lines with carets under *highlight_row*'s AF cell."""
    lines: list[str] = []
    for r, row in enumerate(rows):
        prefix = f"  row {r:>2} | "
        lines.append(prefix + ",".join(row))
        if r == highlight_row:
            af_offset = len(",".join(row[:-1])) + (1 if len(row) > 1 else 0)
            carets = " " * af_offset + "^" * max(1, len(row[-1]))
            lines.append(" " * (len(prefix) - 2) + "| " + carets)
    return "\n".join(lines)


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


def _grid_to_graph(
    rows: list[list[str]],
    pin_row_set: set[int],
) -> tuple[int | None, list[tuple[int, str, int]], list[_Edge], dict[int, int]]:
    """Convert grid to multigraph.

    Returns ``(source_node, sinks, edges, pin_sinks)`` where sinks is a list
    of ``(node_id, af_token, af_row)`` tuples and pin_sinks maps pin row
    index to its rightmost sink node.
    """
    # Local aliases for hot methods — avoids per-call attribute lookup in
    # CPython's LOAD_ATTR; local vars use the cheaper LOAD_FAST opcode.
    uf = _UF()
    uf_make = uf.make
    uf_union = uf.union
    uf_find = uf.find
    n_rows = len(rows)
    ncols = _CONDITION_COLS

    # --- Flat grid extraction ---
    # The original grid lives in rows[r][c+1] (col 0 is the marker).  We
    # copy it into a flat list so every later pass can index with
    # grid[r * ncols + c] — one array lookup instead of a function call
    # with bounds checks.  All out-of-bounds cells become "".
    grid: list[str] = []
    for row in rows:
        row_len = len(row)
        for c in range(ncols):
            idx = c + 1  # skip marker column
            grid.append(row[idx] if idx < row_len else "")

    # --- Connectivity flags ---
    # Precompute which sides each cell exposes (left/right/down) plus its
    # cell-type classification, packed into a single int per cell.  This
    # replaces repeated _cell_sides() / startswith("T:") calls across the
    # four grid passes below.
    _F_LEFT = 1  # cell has a left connection
    _F_RIGHT = 2  # cell has a right connection
    _F_DOWN = 4  # cell has a downward connection
    _F_WIRE = 8  # wire cell: left and right share the same UF port
    _F_TPFX = 16  # T:token cell: left=down (input), right is separate

    adjacency = _ADJACENCY
    wire_cells = _WIRE_CELLS
    flags: list[int] = []
    for cell in grid:
        if not cell:
            flags.append(0)
            continue
        f = 0
        if cell in wire_cells:
            # "-" connects left↔right; "T" adds down; "|" is left+down only
            f = _F_WIRE | _F_LEFT | _F_RIGHT
            if cell in ("T", "|"):
                f |= _F_DOWN
            if cell == "|":
                f &= ~_F_RIGHT
        elif cell.startswith("T:"):
            # T:token — tee junction carrying a contact label
            f = _F_TPFX | _F_LEFT | _F_RIGHT | _F_DOWN
        else:
            # Content cell (contact/comparison) — defaults to left+right
            sides = adjacency.get(cell, ("left", "right"))
            if "left" in sides:
                f |= _F_LEFT
            if "right" in sides:
                f |= _F_RIGHT
            if "down" in sides:
                f |= _F_DOWN
        flags.append(f)

    # --- Port arrays ---
    # Each occupied cell gets up to three UF port IDs (left, right, down).
    # Stored in flat arrays parallel to `grid`; _NO_PORT means unoccupied.
    _NO_PORT = -1
    total = n_rows * ncols
    left_port = [_NO_PORT] * total
    right_port = [_NO_PORT] * total
    down_port = [_NO_PORT] * total

    # 1. Assign ports — pin rows participate in wiring (their T junctions
    #    may connect down to non-pin rows) but never produce sinks.
    #
    #    Wire cells: single conductor (left == right port).
    #    T:token:    left == down (input side), right is separate (output).
    #    Content:    left and right are separate (condition separates them).
    for pos in range(total):
        f = flags[pos]
        if not f:
            continue
        if f & _F_WIRE:
            p = uf_make()
            left_port[pos] = p
            right_port[pos] = p
            if f & _F_DOWN:
                down_port[pos] = p
        elif f & _F_TPFX:
            lp = uf_make()
            rp = uf_make()
            left_port[pos] = lp
            right_port[pos] = rp
            down_port[pos] = lp
        else:
            lp = uf_make()
            rp = uf_make()
            left_port[pos] = lp
            right_port[pos] = rp

    # 2. Claim down-connections.
    #    A cell with a down exit unions its down-port with the target cell
    #    one row below.  Straight-down (same col) claims the target's
    #    left-port; diagonal (col-1 fallback) claims the target's
    #    right-port.  Each target side can only be claimed once — the first
    #    claimant wins — but a single target may be claimed on both sides
    #    independently (left by one source, right by another).
    left_claimed: set[int] = set()
    right_claimed: set[int] = set()
    for r in range(n_rows):
        if r + 1 >= n_rows:
            continue
        base = r * ncols
        next_base = base + ncols
        for c in range(ncols):
            pos = base + c
            if not (flags[pos] & _F_DOWN):
                continue
            dp = down_port[pos]
            if dp == _NO_PORT:
                continue
            # Try straight-down first, then diagonal
            tpos = next_base + c
            tp = left_port[tpos]
            if tp != _NO_PORT and tpos not in left_claimed:
                left_claimed.add(tpos)
                uf_union(dp, tp)
                continue
            if c > 0:
                tpos_diag = next_base + c - 1
                tp = right_port[tpos_diag]
                if tp != _NO_PORT and tpos_diag not in right_claimed:
                    right_claimed.add(tpos_diag)
                    uf_union(dp, tp)

    # 2b. Left power rail: all column-0 cells share the same left-port
    rail_port: int | None = None
    for r in range(n_rows):
        lp = left_port[r * ncols]
        if lp != _NO_PORT:
            if rail_port is None:
                rail_port = lp
            else:
                uf_union(rail_port, lp)

    # 3. Merge horizontal adjacency — if adjacent cells both expose
    #    a connecting side (right→left), union them into one conductor.
    for r in range(n_rows):
        base = r * ncols
        for c in range(ncols - 1):
            pos = base + c
            pos1 = pos + 1
            if (
                (flags[pos] & _F_RIGHT)
                and (flags[pos1] & _F_LEFT)
                and right_port[pos] != _NO_PORT
                and left_port[pos1] != _NO_PORT
            ):
                uf_union(right_port[pos], left_port[pos1])

    # 4. Build edges from content cells (contacts/comparisons — not wires).
    #    Each content cell becomes a labeled edge from its left-port
    #    equivalence class to its right-port equivalence class.
    edges: list[_Edge] = []
    edges_append = edges.append
    for r in range(n_rows):
        base = r * ncols
        for c in range(ncols):
            pos = base + c
            f = flags[pos]
            if not f or (f & _F_WIRE):
                continue
            cell = grid[pos]
            label = cell[2:] if (f & _F_TPFX) else cell  # strip "T:" prefix
            src = uf_find(left_port[pos])
            dst = uf_find(right_port[pos])
            if src != dst:
                edges_append(_Edge(src, dst, Leaf(label, r, c), r, c))
            else:
                _warn_bypassed_contact(label)

    # 5. Identify source and sinks
    #    Source = left power rail node; sinks = rightmost port on each AF row.
    source: int | None = None
    if rail_port is not None:
        source = uf_find(rail_port)
    else:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            base = r * ncols
            for c in range(ncols):
                if left_port[base + c] != _NO_PORT:
                    source = uf_find(left_port[base + c])
                    break
            if source is not None:
                break

    # AF-only rows (no condition cells) are unconditional: they sink
    # directly to the source/rail node.
    sinks: list[tuple[int, str, int]] = []
    if source is None:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            af = rows[r][-1]
            if af and not af.startswith("."):
                source = uf_make()
                break

    for r in range(n_rows):
        if r in pin_row_set:
            continue
        af = rows[r][-1]
        if not af or af.startswith("."):
            continue
        base = r * ncols
        last_c = -1
        for c in range(ncols - 1, -1, -1):
            if right_port[base + c] != _NO_PORT:
                last_c = c
                break
        if last_c >= 0:
            sink_node = uf_find(right_port[base + last_c])
        elif source is not None:
            sink_node = source
        else:
            continue
        sinks.append((sink_node, af, r))

    # 6. Pin sinks — rightmost occupied right_port per pin row.
    pin_sinks: dict[int, int] = {}
    for r in pin_row_set:
        base = r * ncols
        last_c = -1
        for c in range(ncols - 1, -1, -1):
            if right_port[base + c] != _NO_PORT:
                last_c = c
                break
        if last_c >= 0:
            pin_sinks[r] = uf_find(right_port[base + last_c])

    return source, sinks, edges, pin_sinks


# ---------------------------------------------------------------------------
# SP Reduction
# ---------------------------------------------------------------------------


def _walk_tree_leaves(node: SPNode | None) -> Iterator[Leaf]:
    """Yield every Leaf in an SP tree (carrying its row/col position)."""
    if node is None:
        return
    if isinstance(node, Leaf):
        yield node
    else:
        for child in node.children:
            yield from _walk_tree_leaves(child)


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


def _analyze_rungs(
    raw_rungs: list[_RawRung],
    *,
    validate: bool = False,
    source_name: str | None = None,
) -> list[_AnalyzedRung]:
    """Analyze topology of each rung.

    When *validate* is True, a source contact that reaches no output raises
    ``ValueError`` instead of only warning (see ``_analyze_single_rung``).
    When *source_name* is provided, analysis errors identify that source and
    the 1-indexed raw rung number.
    """
    analyzed: list[_AnalyzedRung] = []

    def analyze_one(rung_index: int, *, role: RungRole = RungRole.NORMAL) -> _AnalyzedRung:
        try:
            return _analyze_single_rung(
                raw_rungs[rung_index],
                role=role,
                validate=validate,
            )
        except ValueError as exc:
            if source_name is None:
                raise
            raise ValueError(f"{source_name}, rung {rung_index + 1}: {exc}") from None

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
            analyzed.append(analyze_one(i, role=RungRole.FORLOOP_START))
            i += 1
            # Collect body rungs until next()
            while i < len(raw_rungs):
                body_rung = raw_rungs[i]
                body_af = body_rung.rows[0][-1] if body_rung.rows else ""
                if body_af == "next()":
                    analyzed.append(analyze_one(i, role=RungRole.FORLOOP_NEXT))
                    i += 1
                    break
                analyzed.append(analyze_one(i, role=RungRole.FORLOOP_BODY))
                i += 1
        else:
            analyzed.extend(_split_continued(analyze_one(i)))
            i += 1

    return analyzed


def _analyze_single_rung(
    rung: _RawRung,
    *,
    role: RungRole = RungRole.NORMAL,
    validate: bool = False,
) -> _AnalyzedRung:
    """Analyze a single rung's topology via SP graph reduction.

    Every content contact becomes an ``_Edge`` in :func:`_grid_to_graph`; a
    contact whose right side wires to nothing (e.g. a malformed OR-branch
    lacking tee/down wiring) becomes a dead-end edge that :func:`_sp_reduce`
    prunes via its reachable-subgraph intersection, so it silently vanishes
    from the generated logic. After building the trees we compare every
    content edge's Leaf position against the positions actually present in the
    produced trees; any missing one is a dropped contact — always warned, and
    raised as ``ValueError`` when *validate* is True.
    """
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

    if validate:
        retained_instruction_rows = set(af_rows)
        missing_instructions = [
            (af_token, af_row)
            for _tree, af_token, af_row in output_trees
            if af_row not in retained_instruction_rows
        ]
        if missing_instructions:
            detail = ", ".join(
                f"{af_token} (row {af_row})" for af_token, af_row in missing_instructions
            )
            raise ValueError(
                f"Rung drops output instruction(s) present in the source during grouping: {detail}."
            )

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
                        f"Pin row {row_index} must immediately follow its owning instruction "
                        f"row.\n{_annotate_rung_rows(rows, row_index)}"
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

    # Dropped-condition detection: every content edge's Leaf should reach at
    # least one output or pin tree before output grouping. Grouping may safely
    # merge equal conditions from different source positions, so checking its
    # factored trees would mistake that merge for a dropped source occurrence.
    # Positions absent from every pre-group tree were pruned as dead-end edges
    # (unwired conditions) and silently omitted from the logic.
    present: set[tuple[int, int]] = set()
    for tree, _af_token, _af_row in output_trees:
        for leaf in _walk_tree_leaves(tree):
            present.add((leaf.row, leaf.col))
    for tree in pin_trees.values():
        for leaf in _walk_tree_leaves(tree):
            present.add((leaf.row, leaf.col))

    dropped = [
        (str(e.tree.label), e.tree.row)
        for e in edges
        if isinstance(e.tree, Leaf) and (e.tree.row, e.tree.col) not in present
    ]
    if dropped:
        for label, drow in dropped:
            _warn_dropped_contact(label, drow)
        if validate:
            detail = ", ".join(f"{label} (row {drow})" for label, drow in dropped)
            raise ValueError(
                "Rung drops condition(s) present in the source but not connected into any "
                f"output: {detail}. The source ladder is malformed or the codec produced "
                "invalid topology."
            )

    return _AnalyzedRung(
        comment=comment,
        condition_tree=condition_tree,
        instructions=instructions,
        role=role,
    )
