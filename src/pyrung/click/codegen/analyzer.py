from __future__ import annotations

from pyrung.click.codegen.constants import _ADJACENCY, _CONDITION_COLS, _PIN_RE
from pyrung.click.codegen.models import (
    _AnalyzedRung,
    _InstructionInfo,
    _OrGroup,
    _OrLevel,
    _PathResult,
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


def _cell_exits(cell: str) -> tuple[str, ...]:
    """Exit directions for a cell type (content tokens default to left/right)."""
    if not cell:
        return ()
    if cell.startswith("T:"):
        return ("left", "right", "down")
    return _ADJACENCY.get(cell, ("left", "right"))


def _cell_at(rows: list[list[str]], r: int, c: int) -> str:
    """Cell value at condition column *c* on row *r* (empty if out of bounds)."""
    if 0 <= r < len(rows) and 0 <= c < _CONDITION_COLS:
        return rows[r][c + 1]  # +1 to skip marker column
    return ""


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


def _walk_grid(
    rows: list[list[str]],
    pin_row_set: set[int],
) -> list[_PathResult]:
    """Find root cells and DFS-walk from each."""
    n_rows = len(rows)
    paths: list[_PathResult] = []

    # Primary roots: first non-blank cell in column 0 per non-pin row.
    roots: list[tuple[int, int]] = []
    primary_col0_root_rows: set[int] = set()
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        if _cell_at(rows, r, 0):
            roots.append((r, 0))
            primary_col0_root_rows.add(r)

    # Supplemental roots: AF-blank rows with blank column 0 whose first
    # non-blank cell is a T/T: vertical-chain head.
    for r in range(n_rows):
        if r in pin_row_set or r in primary_col0_root_rows:
            continue
        if _cell_at(rows, r, 0):
            continue
        if rows[r][-1]:
            continue

        first_col = -1
        first_cell = ""
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if cell:
                first_col = c
                first_cell = cell
                break
        if first_col < 0:
            continue
        if not (first_cell == "T" or first_cell.startswith("T:")):
            continue

        # Only root at the head of a vertical chain.
        if r > 0 and (r - 1) not in pin_row_set:
            above = _cell_at(rows, r - 1, first_col)
            if above and "down" in _cell_exits(above):
                continue

        roots.append((r, first_col))

    # Fallback: if column 0 is entirely blank, scan for the leftmost occupied
    # column (handles non-canonical decoded grids).
    if not roots:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            for c in range(_CONDITION_COLS):
                if _cell_at(rows, r, c):
                    roots.append((r, c))
                    break

    for root_r, root_c in roots:
        _dfs(
            rows,
            root_r,
            root_c,
            set(),
            pin_row_set,
            paths,
            (),
            primary_col0_root_rows,
        )

    # Preserve walk order, but drop exact duplicates.
    deduped: list[_PathResult] = []
    seen: set[tuple[tuple[str, ...], str, int]] = set()
    for p in paths:
        key = (tuple(p.conditions), p.af_token, p.af_row)
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    return deduped


def _dfs(
    rows: list[list[str]],
    r: int,
    c: int,
    visited: set[tuple[int, int]],
    pin_row_set: set[int],
    paths: list[_PathResult],
    conditions: tuple[str, ...],
    primary_col0_root_rows: set[int],
) -> None:
    """Recursive DFS walk. Priority: right → down → up → up-right."""
    cell = _cell_at(rows, r, c)
    if not cell or (r, c) in visited:
        return
    visited.add((r, c))

    # Record meaningful tokens (skip wire markers)
    is_content = cell not in {"-", "|", "T"}
    token = _strip_wire_prefix(cell) if is_content else cell
    conds = conditions + (token,) if is_content else conditions

    exits = _cell_exits(cell)
    has_right = "right" in exits
    has_down = "down" in exits

    # --- RIGHT ---
    if has_right:
        nc = c + 1
        if nc < _CONDITION_COLS:
            next_cell = _cell_at(rows, r, nc)
            if next_cell and (r, nc) not in visited:
                _dfs(
                    rows,
                    r,
                    nc,
                    visited,
                    pin_row_set,
                    paths,
                    conds,
                    primary_col0_root_rows,
                )
            elif not next_cell:
                # End of horizontal run — check AF on this row
                af = rows[r][-1]
                if af and not af.startswith("."):
                    paths.append(_PathResult(list(conds), af, r))
        else:
            # Past last condition column → AF exit
            af = rows[r][-1]
            if af and not af.startswith("."):
                paths.append(_PathResult(list(conds), af, r))

    # --- DOWN (forced bidirectional from T) ---
    if has_down:
        nr = r + 1
        if 0 <= nr < len(rows) and nr not in pin_row_set:
            below = _cell_at(rows, nr, c)
            if below and (nr, c) not in visited:
                # T:-prefixed contacts represent OR fork inputs. Stepping down
                # from them starts a parallel branch and must not carry the
                # current contact token into that branch.
                down_conds = conditions if cell.startswith("T:") else conds

                if cell.startswith("T:"):
                    below_is_content = below not in {"-", "|", "T"}
                    below_is_plain_content = below_is_content and not below.startswith("T:")
                    if not (below_is_plain_content and nr in primary_col0_root_rows):
                        _dfs(
                            rows,
                            nr,
                            c,
                            visited,
                            pin_row_set,
                            paths,
                            down_conds,
                            primary_col0_root_rows,
                        )
                else:
                    _dfs(
                        rows,
                        nr,
                        c,
                        visited,
                        pin_row_set,
                        paths,
                        down_conds,
                        primary_col0_root_rows,
                    )

    # --- UP (forced bidirectional — a T above pulls us up) ---
    if r > 0 and (r - 1) not in pin_row_set:
        above = _cell_at(rows, r - 1, c)
        if (
            above
            and "down" in _cell_exits(above)
            and not above.startswith("T:")
            and (r - 1, c) not in visited
        ):
            _dfs(
                rows,
                r - 1,
                c,
                visited,
                pin_row_set,
                paths,
                conds,
                primary_col0_root_rows,
            )

    # --- UP-RIGHT diagonal (T's down-wire drawn at left edge of its cell) ---
    # A cell connects diagonally up-right to a T when the cell directly to the
    # right is blank (gap), meaning there is no bridge occupying that position.
    if r > 0 and c + 1 < _CONDITION_COLS and (r - 1) not in pin_row_set:
        diag = _cell_at(rows, r - 1, c + 1)
        right_cell = _cell_at(rows, r, c + 1)
        if diag and "down" in _cell_exits(diag) and not right_cell:
            if (r - 1, c + 1) not in visited:
                _dfs(
                    rows,
                    r - 1,
                    c + 1,
                    visited,
                    pin_row_set,
                    paths,
                    conds,
                    primary_col0_root_rows,
                )

    visited.discard((r, c))


# --- Path grouping helpers ---------------------------------------------------


def _longest_common_prefix(lists: list[list[str]]) -> list[str]:
    """Return the longest list that is a prefix of every element in *lists*."""
    if not lists:
        return []
    prefix = lists[0]
    for lst in lists[1:]:
        i = 0
        while i < len(prefix) and i < len(lst) and prefix[i] == lst[i]:
            i += 1
        prefix = prefix[:i]
    return list(prefix)


def _longest_common_suffix(lists: list[list[str]]) -> list[str]:
    """Return the longest list that is a suffix of every element in *lists*."""
    if not lists:
        return []
    reversed_lists = [lst[::-1] for lst in lists]
    return _longest_common_prefix(reversed_lists)[::-1]


def _build_condition_tree(cond_lists: list[list[str]]) -> list[str | _OrLevel]:
    """Build an ordered sequence of AND conditions and OR levels from path lists.

    Recursively groups paths by common prefix tokens.  When all paths share the
    same first token it becomes a plain AND condition; when they diverge the
    distinct first tokens form an ``_OrLevel``.
    """
    # Drop fully-consumed (empty) paths
    active = [cl for cl in cond_lists if cl]
    if not active:
        return []

    # Group by first token (preserving insertion order)
    groups: dict[str, list[list[str]]] = {}
    for cl in active:
        groups.setdefault(cl[0], []).append(cl[1:])

    if len(groups) == 1:
        # All paths share the same first token → shared AND condition
        token = next(iter(groups))
        return [token] + _build_condition_tree(groups[token])

    # Multiple distinct first tokens → OR level
    or_groups: list[_OrGroup] = [_OrGroup(conditions=[tok]) for tok in groups]
    result: list[str | _OrLevel] = [_OrLevel(groups=or_groups)]

    # Collect remaining tails from all groups and recurse
    all_remainders: list[list[str]] = []
    for remainders in groups.values():
        all_remainders.extend(remainders)
    result.extend(_build_condition_tree(all_remainders))
    return result


def _group_paths(
    paths: list[_PathResult],
) -> tuple[list[str | _OrLevel], list[_InstructionInfo], list[int]]:
    """Determine OR vs branch structure from walk paths.

    Returns ``(condition_seq, instructions, af_rows)``.
    """
    if not paths:
        return [], [], []

    unique_afs = list(dict.fromkeys(p.af_token for p in paths))

    if len(unique_afs) == 1:
        # All paths reach the same AF → possible OR
        if len(paths) == 1:
            p = paths[0]
            return (
                list(p.conditions),
                [_InstructionInfo(p.af_token, [], [])],
                [p.af_row],
            )

        # Multiple paths → build condition tree (handles prefix, suffix, nested ORs)
        cond_lists = [p.conditions for p in paths]

        # Extract common trailing AND (suffix) first
        suffix = _longest_common_suffix(cond_lists)
        n_suffix = len(suffix)
        trimmed = [cl[: len(cl) - n_suffix] if n_suffix else list(cl) for cl in cond_lists]

        # Build tree from remaining conditions
        seq = _build_condition_tree(trimmed)
        # Append suffix as trailing AND conditions
        seq.extend(suffix)

        af = unique_afs[0]
        af_row = paths[0].af_row
        return (
            seq,
            [_InstructionInfo(af, [], [])],
            [af_row],
        )

    # Different AFs → branch.  Shared conditions = common prefix.
    cond_lists = [p.conditions for p in paths]
    prefix = _longest_common_prefix(cond_lists)
    n_prefix = len(prefix)

    instructions: list[_InstructionInfo] = []
    af_rows: list[int] = []
    for p in paths:
        branch_conds = p.conditions[n_prefix:]
        instructions.append(_InstructionInfo(p.af_token, branch_conds, []))
        af_rows.append(p.af_row)

    return list(prefix), instructions, af_rows


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
    """Analyze a single rung's topology via graph walk."""
    comment = "\n".join(rung.comment_lines) if rung.comment_lines else None
    rows = rung.rows

    if not rows:
        return _AnalyzedRung(
            comment=comment,
            condition_seq=[],
            instructions=[],
        )

    # Separate pin rows from content rows
    pin_row_set = {i for i, row in enumerate(rows) if _is_pin_row(row)}

    # Walk the grid
    paths = _walk_grid(rows, pin_row_set)

    if not paths:
        # No paths discovered — handle rows with only an AF and no conditions,
        # or rows that are entirely blank in the condition columns.
        for i, row in enumerate(rows):
            if i not in pin_row_set:
                af = row[-1]
                if af and not af.startswith("."):
                    pins = _collect_pins([rows[j] for j in sorted(pin_row_set)])
                    return _AnalyzedRung(
                        comment=comment,
                        condition_seq=[],
                        instructions=[
                            _InstructionInfo(af_token=af, branch_conditions=[], pins=pins)
                        ],
                        is_forloop_start=is_forloop_start,
                        is_forloop_body=is_forloop_body,
                        is_forloop_next=is_forloop_next,
                    )
        return _AnalyzedRung(
            comment=comment,
            condition_seq=[],
            instructions=[],
        )

    # Group walk results into OR / branch structure
    condition_seq, instructions, af_rows = _group_paths(paths)

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
        condition_seq=condition_seq,
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
