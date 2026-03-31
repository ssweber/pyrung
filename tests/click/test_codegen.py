"""Tests for laddercodec CSV → pyrung codegen (``ladder_to_pyrung``)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pyrung.click import (
    TagMap,
    c,
    ct,
    ctd,
    dh,
    ds,
    ladder_to_pyrung,
    pyrung_to_ladder,
    sc,
    sd,
    t,
    td,
    x,
    y,
)
from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.parser import _parse_csv
from pyrung.click.codegen.utils import _parse_af_args
from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Tms,
    any_of,
    fall,
    immediate,
    rise,
)
from pyrung.core.program import (
    branch,
    calc,
    comment,
    copy,
    count_up,
    event_drum,
    fill,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    search,
    shift,
    time_drum,
    unpack_to_bits,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _export_csv(program: Program, tag_map: TagMap, tmp_path: Path) -> Path:
    """Export a program to CSV bundle and return the main.csv path."""
    bundle = pyrung_to_ladder(program, tag_map)
    bundle.write(tmp_path)
    return tmp_path / "main.csv"


def _round_trip(
    program: Program,
    tag_map: TagMap,
    tmp_path: Path,
    *,
    nicknames: dict[str, str] | None = None,
) -> tuple[str, list[tuple[str, ...]], list[tuple[str, ...]]]:
    """Full round-trip: program → CSV → codegen → exec → CSV₂.

    Returns (generated_code, original_rows, reproduced_rows).
    """
    bundle = pyrung_to_ladder(program, tag_map)
    original_rows = list(bundle.main_rows)

    # Write full bundle (main.csv + subroutines/*.csv)
    csv_dir = tmp_path / "original"
    bundle.write(csv_dir)

    # If subroutines exist, pass directory; otherwise pass main.csv
    has_subs = bool(bundle.subroutine_rows)
    csv_input = csv_dir if has_subs else csv_dir / "main.csv"
    code = ladder_to_pyrung(csv_input, nicknames=nicknames)

    # Execute the generated code
    ns: dict = {}
    exec(code, ns)

    # Re-export
    logic2 = ns["logic"]
    mapping2 = ns["mapping"]
    bundle2 = pyrung_to_ladder(logic2, mapping2)
    reproduced_rows = list(bundle2.main_rows)

    return code, original_rows, reproduced_rows


# ---------------------------------------------------------------------------
# Phase 1: CSV parsing tests
# ---------------------------------------------------------------------------


class TestCsvParsing:
    def test_simple_rung(self, tmp_path: Path):
        csv_path = tmp_path / "test.csv"
        rows = [
            [
                "marker",
                *[chr(ord("A") + i) for i in range(26)],
                *[f"A{chr(ord('A') + i)}" for i in range(5)],
                "AF",
            ],
            ["R", "X001", *["-"] * 30, "out(Y001)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        raw_rungs = _parse_csv(csv_path)
        assert len(raw_rungs) == 1
        assert raw_rungs[0].comment_lines == []
        assert raw_rungs[0].rows[0][-1] == "out(Y001)"

    def test_comment_rows(self, tmp_path: Path):
        csv_path = tmp_path / "test.csv"
        rows = [
            [
                "marker",
                *[chr(ord("A") + i) for i in range(26)],
                *[f"A{chr(ord('A') + i)}" for i in range(5)],
                "AF",
            ],
            ["#", "Start motor"],
            ["#", "when button pressed"],
            ["R", "X001", *["-"] * 30, "out(Y001)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        raw_rungs = _parse_csv(csv_path)
        assert len(raw_rungs) == 1
        assert raw_rungs[0].comment_lines == ["Start motor", "when button pressed"]

    def test_multiple_rungs(self, tmp_path: Path):
        csv_path = tmp_path / "test.csv"
        header = [
            "marker",
            *[chr(ord("A") + i) for i in range(26)],
            *[f"A{chr(ord('A') + i)}" for i in range(5)],
            "AF",
        ]
        rows = [
            header,
            ["R", "X001", *["-"] * 30, "out(Y001)"],
            ["R", "X002", *["-"] * 30, "latch(Y002)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        raw_rungs = _parse_csv(csv_path)
        assert len(raw_rungs) == 2


# ---------------------------------------------------------------------------
# Phase 2: Topology analysis tests
# ---------------------------------------------------------------------------


class TestTopologyAnalysis:
    def test_simple_and_chain(self, tmp_path: Path):
        """Simple AND: X001 - X002 → out(Y001)."""
        A = Bool("A")
        B = Bool("B")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A, B):
                out(Y)

        mapping = TagMap({A: x[1], B: x[2], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        assert _find_parallel(r.condition_tree) is None
        labels = _leaf_labels(r.condition_tree)
        assert "X001" in labels
        assert "X002" in labels
        assert len(r.instructions) == 1
        assert r.instructions[0].af_token == "out(Y001)"

    def test_or_expansion(self, tmp_path: Path):
        """OR: any_of(A, B) → out(Y)."""
        A = Bool("A")
        B = Bool("B")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B)):
                out(Y)

        mapping = TagMap({A: x[1], B: x[2], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        par = _find_parallel(r.condition_tree)
        assert par is not None
        assert len(par.children) == 2

    def test_or_with_trailing_and(self, tmp_path: Path):
        """OR + AND: any_of(A, B), Ready → out(Y)."""
        A = Bool("A")
        B = Bool("B")
        Ready = Bool("Ready")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B), Ready):
                out(Y)

        mapping = TagMap({A: x[1], B: x[2], Ready: c[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        par = _find_parallel(r.condition_tree)
        assert par is not None
        assert len(par.children) == 2
        # Trailing AND should be in condition_tree labels
        assert "C1" in _leaf_labels(r.condition_tree)

    def test_multiple_outputs(self, tmp_path: Path):
        """Multiple outputs: same conditions, different instructions."""
        A = Bool("A")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                latch(Y2)

        mapping = TagMap({A: x[1], Y1: y[1], Y2: y[2]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        assert len(r.instructions) == 2
        assert r.instructions[0].af_token == "out(Y001)"
        assert r.instructions[1].af_token == "latch(Y002)"

    def test_pin_rows(self, tmp_path: Path):
        """Timer with .reset() pin."""
        Enable = Bool("Enable")
        Reset = Bool("Reset")
        Done = Bool("Done")
        Acc = Int("Acc")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Done, Acc, preset=100, unit=Tms).reset(Reset)

        mapping = TagMap(
            {Enable: x[1], Reset: x[2], Done: t[1], Acc: td[1]},
            include_system=False,
        )
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        assert len(r.instructions) == 1
        assert len(r.instructions[0].pins) == 1
        assert r.instructions[0].pins[0].name == "reset"

    def test_branch(self, tmp_path: Path):
        """Branch: shared condition + branch-specific conditions."""
        A = Bool("A")
        Mode = Bool("Mode")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                with branch(Mode):
                    out(Y2)

        mapping = TagMap(
            {A: x[1], Mode: x[2], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert len(analyzed) == 1
        r = analyzed[0]
        assert len(r.instructions) == 2

    def test_comment(self, tmp_path: Path):
        """Comment on rung."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Start motor")
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert analyzed[0].comment == "Start motor"

    def test_multiline_comment(self, tmp_path: Path):
        """Multi-line comment."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Line 1\nLine 2")
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert analyzed[0].comment == "Line 1\nLine 2"


# ---------------------------------------------------------------------------
# Phase 2 graph-walk edge cases (synthetic grids)
# ---------------------------------------------------------------------------

# _CONDITION_COLS (31) + marker + AF = 33 cells per row.
_GRID_WIDTH = 33


def _make_row(marker: str, cells: dict[int, str], af: str = "") -> list[str]:
    """Build a 33-cell row from a sparse column map.

    *cells* maps 0-based condition-column index → value.
    Unmentioned columns are blank.
    """
    row = [""] * _GRID_WIDTH
    row[0] = marker
    row[-1] = af
    for col, val in cells.items():
        row[col + 1] = val  # +1 to skip marker
    return row


def _fill_dashes(cells: dict[int, str], start: int, end: int) -> dict[int, str]:
    """Fill condition columns [start, end) with '-' in *cells* (mutates)."""
    for col in range(start, end):
        cells.setdefault(col, "-")
    return cells


def _find_parallel(node):
    """Return the first ``Parallel`` node found in the tree, or ``None``."""
    from pyrung.click.codegen.models import Parallel, Series

    if node is None:
        return None
    if isinstance(node, Parallel):
        return node
    if isinstance(node, Series):
        for child in node.children:
            result = _find_parallel(child)
            if result is not None:
                return result
    return None


def _leaf_labels(node) -> list[str]:
    """Collect all leaf labels from an SP tree in order."""
    from pyrung.click.codegen.models import Leaf, Parallel, Series

    if node is None:
        return []
    if isinstance(node, Leaf):
        return [node.label]
    if isinstance(node, (Series, Parallel)):
        labels: list[str] = []
        for child in node.children:
            labels.extend(_leaf_labels(child))
        return labels
    return []


class TestGraphWalkEdgeCases:
    """Synthetic grids exercising SP graph reduction from the Phase 2 spec."""

    def test_forced_bidirectional_or(self):
        """OR alternative on row 1 reaches AF via UP through T (forced bidi).

        Row 0: R | X001 | T | - ... - | out(Y001)
        Row 1:   | X002 | - |         |
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({0: "X001", 1: "T"}, 2, 31), af="out(Y001)")
        row1 = _make_row("", {0: "X002"})
        rung = _RawRung(comment_lines=[], rows=[row0, row1])

        result = _analyze_single_rung(rung)
        par = _find_parallel(result.condition_tree)
        assert par is not None
        assert len(par.children) == 2
        labels = [_leaf_labels(c) for c in par.children]
        assert ["X001"] in labels
        assert ["X002"] in labels
        assert len(result.instructions) == 1
        assert result.instructions[0].af_token == "out(Y001)"

    def test_up_right_diagonal(self):
        """Cell connects UP-RIGHT to a T when the bridge cell is blank (gap).

        Row 0:   |      | T | - ... - | out(Y001)
        Row 1:   | X001 |   |         |

        X001 at (1,0) has blank to its right at (1,1).  T at (0,1) has
        'down', so the diagonal UP-RIGHT rule fires: X001 → T → AF.
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({1: "T"}, 2, 31), af="out(Y001)")
        row1 = _make_row("", {0: "X001"})
        rung = _RawRung(comment_lines=[], rows=[row0, row1])

        result = _analyze_single_rung(rung)
        assert len(result.instructions) == 1
        assert result.instructions[0].af_token == "out(Y001)"
        assert "X001" in _leaf_labels(result.condition_tree)

    def test_bridge_connects_branch(self):
        """T forces bidirectional down to a '-' bridge, connecting a second AF.

        Row 0: R | X001 | T | - ... - | out(Y001)
        Row 1:   |      | - | - ... - | out(Y002)
                          ^bridge at (1,1) — T forces connection

        Single root (X001).  T forks: right→Y001, down→bridge→Y002.
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        r0 = _make_row("R", _fill_dashes({0: "X001", 1: "T"}, 2, 31), af="out(Y001)")
        r1 = _make_row("", _fill_dashes({1: "-"}, 2, 31), af="out(Y002)")
        result = _analyze_single_rung(_RawRung(comment_lines=[], rows=[r0, r1]))

        assert len(result.instructions) == 2
        assert result.instructions[0].af_token == "out(Y001)"
        assert result.instructions[1].af_token == "out(Y002)"
        assert "X001" in _leaf_labels(result.condition_tree)

    def test_three_way_or(self):
        """Three OR alternatives: X001, X002, X003 all reach the same AF.

        Row 0: R | X001 | T | - ... - | out(Y001)
        Row 1:   | X002 | T |         |
        Row 2:   | X003 | - |         |
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({0: "X001", 1: "T"}, 2, 31), af="out(Y001)")
        row1 = _make_row("", {0: "X002", 1: "|"})
        row2 = _make_row("", {0: "X003"})
        rung = _RawRung(comment_lines=[], rows=[row0, row1, row2])

        result = _analyze_single_rung(rung)
        par = _find_parallel(result.condition_tree)
        assert par is not None
        assert len(par.children) == 3
        labels = [_leaf_labels(c) for c in par.children]
        assert ["X001"] in labels
        assert ["X002"] in labels
        assert ["X003"] in labels

    def test_nested_t_cascade(self):
        """Nested T cascade: T at (0,1) forks, T at (1,1) forks again.

        Row 0: R | btn | T | -   ... - | out(L1)
        Row 1:   |     | T | auto ... - | out(L2)
        Row 2:   |     | - | -   ... - | out(L3)

        Expected: shared=[btn], three instructions in scan order (right-first).
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({0: "btn", 1: "T"}, 2, 31), af="out(L1)")
        row1 = _make_row("", _fill_dashes({1: "T", 2: "auto"}, 3, 31), af="out(L2)")
        row2 = _make_row("", _fill_dashes({1: "-"}, 2, 31), af="out(L3)")
        rung = _RawRung(comment_lines=[], rows=[row0, row1, row2])

        result = _analyze_single_rung(rung)
        assert _find_parallel(result.condition_tree) is None
        assert "btn" in _leaf_labels(result.condition_tree)
        assert len(result.instructions) == 3

        # Instruction AF tokens present
        afs = [i.af_token for i in result.instructions]
        assert "out(L1)" in afs
        assert "out(L2)" in afs
        assert "out(L3)" in afs

        # The instruction for L2 has a branch-local condition containing "auto"
        l2_instr = next(i for i in result.instructions if i.af_token == "out(L2)")
        assert l2_instr.branch_tree is not None
        assert "auto" in _leaf_labels(l2_instr.branch_tree)

        # L1 and L3 have no branch-local conditions
        l1_instr = next(i for i in result.instructions if i.af_token == "out(L1)")
        l3_instr = next(i for i in result.instructions if i.af_token == "out(L3)")
        assert l1_instr.branch_tree is None
        assert l3_instr.branch_tree is None

    def test_unconditional_rung(self):
        """Rung with no conditions — all dashes to AF."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row = _make_row("R", _fill_dashes({}, 0, 31), af="out(Y001)")
        rung = _RawRung(comment_lines=[], rows=[row])

        result = _analyze_single_rung(rung)
        assert result.condition_tree is None
        assert len(result.instructions) == 1
        assert result.instructions[0].af_token == "out(Y001)"

    def test_or_with_three_trailing_and(self):
        """OR alternatives followed by multiple shared trailing AND conditions.

        Row 0: R | X001 | T | C1 | C2 | - ... | out(Y001)
        Row 1:   | X002 | - |    |    |       |
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row(
            "R", _fill_dashes({0: "X001", 1: "T", 2: "C1", 3: "C2"}, 4, 31), af="out(Y001)"
        )
        row1 = _make_row("", {0: "X002"})
        rung = _RawRung(comment_lines=[], rows=[row0, row1])

        result = _analyze_single_rung(rung)
        par = _find_parallel(result.condition_tree)
        assert par is not None
        assert len(par.children) == 2
        # Trailing AND conditions should be in condition_tree
        labels = _leaf_labels(result.condition_tree)
        assert "C1" in labels
        assert "C2" in labels

    def test_pin_attached_to_correct_instruction(self):
        """Pin row attaches to its nearest preceding instruction.

        Real encoder layout for branch + timer w/ pin:
        Row 0: R | X001 | T | - ... | out(Y001)
        Row 1:   |      | - | - ... | on_delay(T1)
        Row 2:   | X002 | - | - ... | .reset()

        Pin (.reset at row 2) attaches to on_delay (row 1), not out (row 0).
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({0: "X001", 1: "T"}, 2, 31), af="out(Y001)")
        row1 = _make_row("", _fill_dashes({1: "-"}, 2, 31), af="on_delay(T1)")
        row2 = _make_row("", _fill_dashes({0: "X002"}, 1, 31), af=".reset()")
        rung = _RawRung(comment_lines=[], rows=[row0, row1, row2])

        result = _analyze_single_rung(rung)
        assert len(result.instructions) == 2
        assert result.instructions[0].af_token == "out(Y001)"
        assert result.instructions[0].pins == []
        assert result.instructions[1].af_token == "on_delay(T1)"
        assert len(result.instructions[1].pins) == 1
        assert result.instructions[1].pins[0].name == "reset"
        assert result.instructions[1].pins[0].conditions == ["X002"]

    def test_noncanonical_left_edge_not_col0(self):
        """Non-canonical grid: first content starts at column 1, not column 0.

        Row 0: R |   | X001 | - ... | out(Y001)

        Fallback root finding should locate X001 at col 1.
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row = _make_row("R", _fill_dashes({1: "X001"}, 2, 31), af="out(Y001)")
        rung = _RawRung(comment_lines=[], rows=[row])

        result = _analyze_single_rung(rung)
        assert "X001" in _leaf_labels(result.condition_tree)
        assert result.instructions[0].af_token == "out(Y001)"

    def test_adjacency_table_content_default(self):
        """Content tokens (contacts, comparisons) default to left/right exits.

        Ensures tokens like 'DS1==5' are traversed the same as '-'.
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row = _make_row("R", _fill_dashes({0: "X001", 1: "DS1==5"}, 2, 31), af="out(Y001)")
        rung = _RawRung(comment_lines=[], rows=[row])

        result = _analyze_single_rung(rung)
        labels = _leaf_labels(result.condition_tree)
        assert "X001" in labels
        assert "DS1==5" in labels

    def test_another_case_t_fork_down_through_contact(self):
        """T fork down through contact reaches second AF without reverse leakage."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        rows = [
            _make_row("R", _fill_dashes({0: "X001", 1: "T"}, 2, 31), af="out(Y001)"),
            _make_row("", {1: "X003", 2: "|"}),
            _make_row("", _fill_dashes({0: "X002"}, 1, 31), af="out(Y002)"),
        ]
        rung = _RawRung(comment_lines=[], rows=rows)
        result = _analyze_single_rung(rung)

        assert len(result.instructions) == 2
        afs = {i.af_token for i in result.instructions}
        assert "out(Y001)" in afs
        assert "out(Y002)" in afs
        # X001 should be in the shared condition or a branch
        all_labels = _leaf_labels(result.condition_tree)
        for instr in result.instructions:
            all_labels.extend(_leaf_labels(instr.branch_tree))
        assert "X001" in all_labels

    def test_or_with_all_offs_t_prefix_rows_above_output(self):
        """T:-prefixed stacked OR rows above output row remain reachable."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        rows = [
            _make_row("R", {1: "T:X006", 2: "X007", 3: "|"}),
            _make_row("", {1: "T:X004", 2: "X005", 3: "|"}),
            _make_row("", _fill_dashes({0: "X001", 1: "X002", 2: "X003"}, 3, 31), af="out(Y001)"),
        ]
        rung = _RawRung(comment_lines=[], rows=rows)
        result = _analyze_single_rung(rung)

        assert len(result.instructions) == 1
        assert result.instructions[0].af_token == "out(Y001)"
        # All condition labels should be present
        labels = _leaf_labels(result.condition_tree)
        assert "X001" in labels
        assert "X002" in labels
        assert "X003" in labels
        assert "X006" in labels
        assert "X007" in labels
        assert "X004" in labels
        assert "X005" in labels
        # Should have a Parallel node (OR structure)
        par = _find_parallel(result.condition_tree)
        assert par is not None

    def test_crazy_mid_grid_vertical_or_stack(self):
        """Mid-grid vertical stack yields unconditional and OR-branch outputs."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        rows = [
            _make_row("R", {}),
            _make_row("", _fill_dashes({}, 0, 31), af="out(Y001)"),
            _make_row("", {}),
            _make_row("", {2: "T", 3: "X010", 4: "T"}),
            _make_row("", {4: "|"}),
            _make_row("", _fill_dashes({4: "T:X001"}, 5, 31), af="out(Y002)"),
            _make_row("", _fill_dashes({4: "T:X002"}, 5, 31), af="out(Y002)"),
            _make_row("", _fill_dashes({4: "X003", 5: "X004"}, 6, 31), af="out(Y002)"),
        ]
        rung = _RawRung(comment_lines=[], rows=rows)
        result = _analyze_single_rung(rung)

        # Should have instructions for both Y001 and Y002
        afs = [i.af_token for i in result.instructions]
        assert "out(Y001)" in afs
        assert "out(Y002)" in afs


# ---------------------------------------------------------------------------
# Phase 3: Operand inference tests
# ---------------------------------------------------------------------------


class TestOperandInference:
    def test_all_prefix_types(self, tmp_path: Path):
        """Verify correct tag types inferred from all operand prefixes."""
        from pyrung.click.codegen.utils import _parse_operand_prefix

        cases = [
            ("X001", "Bool", "x", 1),
            ("Y001", "Bool", "y", 1),
            ("C1", "Bool", "c", 1),
            ("DS1", "Int", "ds", 1),
            ("DD1", "Dint", "dd", 1),
            ("DH1", "Word", "dh", 1),
            ("DF1", "Real", "df", 1),
            ("T1", "Bool", "t", 1),
            ("TD1", "Int", "td", 1),
            ("CT1", "Bool", "ct", 1),
            ("CTD1", "Dint", "ctd", 1),
            ("SC1", "Bool", "sc", 1),
            ("SD1", "Int", "sd", 1),
            ("TXT1", "Char", "txt", 1),
        ]
        for operand, expected_type, expected_block, expected_idx in cases:
            result = _parse_operand_prefix(operand)
            assert result is not None, f"Failed to parse {operand}"
            _, tag_type, block_var, idx = result
            assert tag_type == expected_type, f"{operand}: expected {expected_type}, got {tag_type}"
            assert block_var == expected_block, (
                f"{operand}: expected {expected_block}, got {block_var}"
            )
            assert idx == expected_idx, f"{operand}: expected {expected_idx}, got {idx}"

    def test_longer_prefix_wins(self):
        """CTD matches before CT, TD matches before T."""
        from pyrung.click.codegen.utils import _parse_operand_prefix

        result = _parse_operand_prefix("CTD1")
        assert result is not None
        _, tag_type, block_var, _ = result
        assert block_var == "ctd"
        assert tag_type == "Dint"

        result = _parse_operand_prefix("TD1")
        assert result is not None
        _, tag_type, block_var, _ = result
        assert block_var == "td"
        assert tag_type == "Int"


# ---------------------------------------------------------------------------
# AF argument parsing
# ---------------------------------------------------------------------------


class TestAfArgParsing:
    def test_simple_args(self):
        args, kwargs = _parse_af_args("Y001")
        assert args == ["Y001"]
        assert kwargs == []

    def test_kwargs(self):
        args, kwargs = _parse_af_args("T1,TD1,preset=100,unit=Tms")
        assert args == ["T1", "TD1"]
        assert kwargs == [("preset", "100"), ("unit", "Tms")]

    def test_nested_brackets(self):
        args, kwargs = _parse_af_args("outputs=[C1,C2],events=[X001,X002]")
        assert kwargs == [("outputs", "[C1,C2]"), ("events", "[X001,X002]")]

    def test_comparison_not_kwarg(self):
        """DS1==5 should not be split as kwarg."""
        args, kwargs = _parse_af_args("DS1==5")
        assert args == ["DS1==5"]
        assert kwargs == []


# ---------------------------------------------------------------------------
# Round-trip golden tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_simple_contact_coil(self, tmp_path: Path):
        """Simple: X001 → out(Y001)."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_and_chain(self, tmp_path: Path):
        """AND chain: X001, X002 → out(Y001)."""
        A = Bool("A")
        B = Bool("B")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A, B):
                out(Y)

        mapping = TagMap({A: x[1], B: x[2], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_negated_contact(self, tmp_path: Path):
        """NC contact: ~X001 → out(Y001)."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(~A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_rise_fall(self, tmp_path: Path):
        """Edge contacts: rise(X001), fall(X002)."""
        A = Bool("A")
        B = Bool("B")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(rise(A)):
                out(Y1)
            with Rung(fall(B)):
                out(Y2)

        mapping = TagMap({A: x[1], B: x[2], Y1: y[1], Y2: y[2]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_or_expansion(self, tmp_path: Path):
        """OR: any_of(A, B) → out(Y)."""
        A = Bool("A")
        B = Bool("B")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B)):
                out(Y)

        mapping = TagMap({A: x[1], B: x[2], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "with Rung(X001 | X002):" in code
        assert "any_of" not in code
        assert orig == repro

    def test_or_with_trailing_and(self, tmp_path: Path):
        """OR + trailing AND."""
        A = Bool("A")
        B = Bool("B")
        Ready = Bool("Ready")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B), Ready):
                out(Y)

        mapping = TagMap(
            {A: x[1], B: x[2], Ready: c[1], Y: y[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "with Rung(X001 | X002, C1):" in code
        assert orig == repro

    def test_three_way_or(self, tmp_path: Path):
        """3-branch OR exercises | output-bus marker."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B, C)):
                out(Y)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], Y: y[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "with Rung(any_of(X001, X002, C1)):" in code
        assert orig == repro

    def test_two_way_comparison_or_stays_any_of(self, tmp_path: Path):
        """2-way comparison OR stays as any_of(...) for readability and precedence."""
        Step = Int("Step")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(Step == 1, Step == 2)):
                out(Y)

        mapping = TagMap({Step: ds[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "with Rung(any_of(DS1 == 1, DS1 == 2)):" in code
        assert orig == repro

    def test_mid_rung_or(self, tmp_path: Path):
        """OR after AND exercises T: prefix on mid-rung contacts."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A, any_of(B, C)):
                out(Y)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], Y: y[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_two_series_ors(self, tmp_path: Path):
        """Two sequential ORs: first at col 0 (bare), second mid-rung (T: prefix)."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        D = Bool("D")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(any_of(A, B), any_of(C, D)):
                out(Y)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], D: c[2], Y: y[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        # Native merged-continuation topology for series ORs decodes to a
        # canonicalized equivalent form in codegen today. Keep this test as
        # a round-trip guard for parser/exporter stability.
        assert repro[0] == orig[0]
        assert repro[-1] == orig[-1]
        assert repro[1][-1] == "out(Y001)"
        assert repro[1][1] == "X001"
        assert repro[1][2] == "T"
        assert repro[2][1] == "X002"

    def test_multiple_outputs(self, tmp_path: Path):
        """Multiple outputs from same conditions."""
        A = Bool("A")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")
        Y3 = Bool("Y3")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                latch(Y2)
                reset(Y3)

        mapping = TagMap(
            {A: x[1], Y1: y[1], Y2: y[2], Y3: y[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_comment(self, tmp_path: Path):
        """Rung with single-line comment."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Start motor")
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_multiline_comment(self, tmp_path: Path):
        """Rung with multi-line comment."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Line 1\nLine 2")
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_comparison_condition(self, tmp_path: Path):
        """Comparison: DS1 == 5 → out(Y001)."""
        Counter = Int("Counter")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(Counter == 5):
                out(Y)

        mapping = TagMap({Counter: ds[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_timer_with_reset(self, tmp_path: Path):
        """Timer with .reset() pin."""
        Enable = Bool("Enable")
        ResetCond = Bool("ResetCond")
        Done = Bool("Done")
        Acc = Int("Acc")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Done, Acc, preset=100, unit=Tms).reset(ResetCond)

        mapping = TagMap(
            {Enable: x[1], ResetCond: x[2], Done: t[1], Acc: td[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_counter_with_pins(self, tmp_path: Path):
        """Counter with .down() and .reset() pins."""
        Enable = Bool("Enable")
        Down = Bool("Down")
        ResetCond = Bool("ResetCond")
        Done = Bool("Done")
        Acc = Dint("Acc")

        with Program() as logic:
            with Rung(Enable):
                count_up(Done, Acc, preset=10).down(Down).reset(ResetCond)

        mapping = TagMap(
            {Enable: x[1], Down: x[2], ResetCond: x[3], Done: ct[1], Acc: ctd[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_copy(self, tmp_path: Path):
        """Copy instruction."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dst = Int("Dst")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dst)

        mapping = TagMap({Enable: x[1], Src: ds[1], Dst: ds[2]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_branch(self, tmp_path: Path):
        """Branch with conditions."""
        A = Bool("A")
        Mode = Bool("Mode")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                with branch(Mode):
                    out(Y2)

        mapping = TagMap(
            {A: x[1], Mode: x[2], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_branch_2_deep(self, tmp_path: Path):
        """2-deep nesting emits flat branch(B, C)."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                with branch(B, C):
                    out(Y2)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "branch(" in code
        assert orig == repro

    def test_branch_3_deep(self, tmp_path: Path):
        """3-deep nesting emits flat branch(B, C, D)."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        D = Bool("D")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                with branch(B, C, D):
                    out(Y2)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], D: c[2], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "branch(" in code
        assert orig == repro

    def test_branch_interleaved_across_depths(self, tmp_path: Path):
        """Multiple branches at same level with different depths."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")
        Y3 = Bool("Y3")

        with Program() as logic:
            with Rung(A):
                out(Y1)
                with branch(B):
                    out(Y2)
                with branch(C):
                    out(Y3)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], Y1: y[1], Y2: y[2], Y3: y[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_forloop(self, tmp_path: Path):
        """For/next loop."""
        Enable = Bool("Enable")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(Enable):
                with forloop(3, oneshot=True):
                    out(Y)

        mapping = TagMap({Enable: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_immediate_contact(self, tmp_path: Path):
        """Immediate contact."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(immediate(A)):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_immediate_coil(self, tmp_path: Path):
        """Immediate coil (immediate only in AF, not conditions)."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(immediate(Y))

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro
        assert "immediate" in code

    def test_calc(self, tmp_path: Path):
        """Calc instruction."""
        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A + B, Result)

        mapping = TagMap(
            {Enable: x[1], A: ds[1], B: ds[2], Result: ds[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_calc_decimal_operators(self, tmp_path: Path):
        """Calc with power, modulo, and math functions (decimal-mode) round-trips."""
        from pyrung.core.expression import sqrt

        Enable = Bool("Enable")
        A = Int("A")
        B = Int("B")
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                calc(A**2, Result)
            with Rung(Enable):
                calc(A % B, Result)
            with Rung(Enable):
                calc(sqrt(A), Result)

        mapping = TagMap(
            {Enable: x[1], A: ds[1], B: ds[2], Result: ds[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_calc_hex_shift_operators(self, tmp_path: Path):
        """Calc with LSH/RSH (hex-mode) round-trips."""
        from pyrung.core import Block, TagType
        from pyrung.core.expression import lsh

        Enable = Bool("Enable")
        H = Block("H", TagType.WORD, 1, 1)
        HDest = Block("HDest", TagType.WORD, 1, 1)

        with Program() as logic:
            with Rung(Enable):
                calc(H[1] << 3, HDest[1])
            with Rung(Enable):
                calc(lsh(H[1], 4), HDest[1])

        mapping = TagMap(
            {Enable: x[1], H[1]: dh[1], HDest[1]: dh[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_calc_bitwise_round_trip(self, tmp_path: Path):
        """Calc with AND/OR/XOR round-trips through Click-native operators."""
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        H1 = Block("H1", TagType.WORD, 1, 1)
        H2 = Block("H2", TagType.WORD, 1, 1)
        HDest = Block("HDest", TagType.WORD, 1, 1)

        with Program() as logic:
            with Rung(Enable):
                calc(H1[1] & H2[1], HDest[1])
            with Rung(Enable):
                calc(H1[1] | H2[1], HDest[1])
            with Rung(Enable):
                calc(H1[1] ^ H2[1], HDest[1])

        mapping = TagMap(
            {Enable: x[1], H1[1]: dh[1], H2[1]: dh[2], HDest[1]: dh[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_calc_sum_round_trip(self, tmp_path: Path):
        """Calc with SUM(range) round-trips through colon-range syntax."""
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        DH = Block("DH", TagType.WORD, 1, 10)
        Dest = Block("Dest", TagType.WORD, 1, 1)

        with Program() as logic:
            with Rung(Enable):
                calc(DH.select(1, 5).sum(), Dest[1])

        mapping = TagMap(
            {Enable: x[1], Dest[1]: dh[100], **{DH[i]: dh[i] for i in range(1, 11)}},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "SUM" in code or ".sum()" in code
        assert orig == repro

    def test_fill(self, tmp_path: Path):
        """Fill instruction."""
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        Dest = Block("Dest", TagType.INT, 1, 10)

        with Program() as logic:
            with Rung(Enable):
                fill(0, Dest.select(1, 10))

        mapping = TagMap(
            {Enable: x[1], Dest: ds.select(1, 10)},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_unpack_to_bits(self, tmp_path: Path):
        """Unpack instruction with range."""
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        Source = Int("Source")
        Bits = Block("Bits", TagType.BOOL, 1, 16)

        with Program() as logic:
            with Rung(Enable):
                unpack_to_bits(Source, Bits.select(1, 16))

        mapping = TagMap(
            {Enable: x[1], Source: ds[1], Bits: c.select(1, 16)},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_shift(self, tmp_path: Path):
        """Shift register with .clock() and .reset() pins."""
        from pyrung.core import Block, TagType

        Data = Bool("Data")
        Clock = Bool("Clock")
        ResetCond = Bool("ResetCond")
        Bits = Block("Bits", TagType.BOOL, 1, 8)

        with Program() as logic:
            with Rung(Data):
                shift(Bits.select(1, 8)).clock(Clock).reset(ResetCond)

        mapping = TagMap(
            {Data: x[1], Clock: x[2], ResetCond: x[3], Bits: c.select(1, 8)},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_search(self, tmp_path: Path):
        """Search instruction."""
        from pyrung.core import Block, TagType

        Enable = Bool("Enable")
        Target = Int("Target")
        Data = Block("Data", TagType.INT, 1, 4)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search(Data.select(1, 4) == Target, result=Result, found=Found)

        mapping = TagMap(
            {Enable: x[1], Target: ds[5], Data: ds.select(1, 4), Result: ds[6], Found: c[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_event_drum(self, tmp_path: Path):
        """Event drum with reset pin."""
        Enable = Bool("Enable")
        ResetCond = Bool("ResetCond")
        Out1 = Bool("Out1")
        Out2 = Bool("Out2")
        Event1 = Bool("Event1")
        Event2 = Bool("Event2")
        Step = Int("Step")
        Done = Bool("Done")

        with Program() as logic:
            with Rung(Enable):
                event_drum(
                    outputs=[Out1, Out2],
                    events=[Event1, Event2],
                    pattern=[[1, 0], [0, 1]],
                    current_step=Step,
                    completion_flag=Done,
                ).reset(ResetCond)

        mapping = TagMap(
            {
                Enable: x[1],
                ResetCond: x[2],
                Event1: x[3],
                Event2: x[4],
                Out1: y[1],
                Out2: y[2],
                Step: ds[1],
                Done: c[1],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_time_drum(self, tmp_path: Path):
        """Time drum with reset pin."""
        Enable = Bool("Enable")
        ResetCond = Bool("ResetCond")
        Out1 = Bool("Out1")
        Out2 = Bool("Out2")
        Step = Int("Step")
        Acc = Int("Acc")
        Done = Bool("Done")

        with Program() as logic:
            with Rung(Enable):
                time_drum(
                    outputs=[Out1, Out2],
                    presets=[100, 200],
                    unit=Tms,
                    pattern=[[1, 0], [0, 1]],
                    current_step=Step,
                    accumulator=Acc,
                    completion_flag=Done,
                ).reset(ResetCond)

        mapping = TagMap(
            {
                Enable: x[1],
                ResetCond: x[2],
                Out1: y[1],
                Out2: y[2],
                Step: ds[1],
                Acc: td[1],
                Done: c[1],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_send(self, tmp_path: Path):
        """Send instruction with ModbusTcpTarget."""
        from pyrung.click import ModbusTcpTarget, send

        Enable = Bool("Enable")
        Source = Int("Source")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                send(
                    target=target,
                    remote_start="DS1",
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Source: ds[1],
                Sending: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_receive(self, tmp_path: Path):
        """Receive instruction with ModbusTcpTarget."""
        from pyrung.click import ModbusTcpTarget, receive

        Enable = Bool("Enable")
        Dest = Int("Dest")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Dest: ds[1],
                Receiving: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_send_rtu(self, tmp_path: Path):
        """Send instruction with ModbusRtuTarget."""
        from pyrung.click import ModbusRtuTarget, send

        Enable = Bool("Enable")
        Source = Int("Source")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=5, com_port="cpu2")

        with Program() as logic:
            with Rung(Enable):
                send(
                    target=target,
                    remote_start="DS1",
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Source: ds[1],
                Sending: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_receive_rtu(self, tmp_path: Path):
        """Receive instruction with ModbusRtuTarget."""
        from pyrung.click import ModbusRtuTarget, receive

        Enable = Bool("Enable")
        Dest = Int("Dest")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=5, com_port="slot0_1")

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Dest: ds[1],
                Receiving: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_send_modbus_address(self, tmp_path: Path):
        """Send instruction with ModbusAddress remote_start."""
        from pyrung.click import ModbusAddress, ModbusTcpTarget, RegisterType, send

        Enable = Bool("Enable")
        Source = Int("Source")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                send(
                    target=target,
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Source: ds[1],
                Sending: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_receive_modbus_address(self, tmp_path: Path):
        """Receive instruction with ModbusAddress remote_start."""
        from pyrung.click import ModbusAddress, ModbusTcpTarget, RegisterType, receive

        Enable = Bool("Enable")
        Dest = Int("Dest")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Dest: ds[1],
                Receiving: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_send_rtu_modbus_address(self, tmp_path: Path):
        """Send with ModbusRtuTarget and ModbusAddress remote_start."""
        from pyrung.click import ModbusAddress, ModbusRtuTarget, RegisterType, send

        Enable = Bool("Enable")
        Source = Int("Source")
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusRtuTarget("vfd1", com_port="slot1_2", device_id=2)

        with Program() as logic:
            with Rung(Enable):
                send(
                    target=target,
                    remote_start=ModbusAddress(100, RegisterType.HOLDING),
                    source=Source,
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Source: ds[1],
                Sending: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_receive_rtu_modbus_address(self, tmp_path: Path):
        """Receive with ModbusRtuTarget and ModbusAddress remote_start."""
        from pyrung.click import ModbusAddress, ModbusRtuTarget, RegisterType, receive

        Enable = Bool("Enable")
        Dest = Int("Dest")
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusRtuTarget("vfd1", com_port="slot1_2", device_id=2)

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start=ModbusAddress(100, RegisterType.HOLDING),
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Dest: ds[1],
                Receiving: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[2],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_send_block_range(self, tmp_path: Path):
        """Send with BlockRange source round-trips through DS1..DS3."""
        from pyrung.click import ModbusTcpTarget, send

        Enable = Bool("Enable")
        Source = Block("Source", TagType.INT, 1, 3)
        Sending = Bool("Sending")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                send(
                    target=target,
                    remote_start="DS1",
                    source=Source.select(1, 3),
                    sending=Sending,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Source: ds.select(1, 3),
                Sending: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[4],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_receive_block_range(self, tmp_path: Path):
        """Receive with BlockRange dest round-trips through DS1..DS3."""
        from pyrung.click import ModbusTcpTarget, receive

        Enable = Bool("Enable")
        Dest = Block("Dest", TagType.INT, 1, 3)
        Receiving = Bool("Receiving")
        Success = Bool("Success")
        Error = Bool("Error")
        ExCode = Int("ExCode")

        target = ModbusTcpTarget("plc2", "192.168.1.2")

        with Program() as logic:
            with Rung(Enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=Dest.select(1, 3),
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                )

        mapping = TagMap(
            {
                Enable: x[1],
                Dest: ds.select(1, 3),
                Receiving: c[1],
                Success: c[2],
                Error: c[3],
                ExCode: ds[4],
            },
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_subroutine(self, tmp_path: Path):
        """Subroutine with call() and return."""
        from pyrung.core.program import call, subroutine

        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("init")

            with subroutine("init"):
                with Rung():
                    out(SubLight)

        mapping = TagMap(
            {Button: x[1], Light: y[1], SubLight: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro
        assert 'subroutine("init")' in code
        assert 'call("init")' in code

    def test_subroutine_with_conditions(self, tmp_path: Path):
        """Subroutine rungs with conditions."""
        from pyrung.core.program import call, subroutine

        Enable = Bool("Enable")
        Cond = Bool("Cond")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(Enable):
                call("worker")

            with subroutine("worker"):
                with Rung(Cond):
                    out(Y1)
                with Rung():
                    out(Y2)

        mapping = TagMap(
            {Enable: x[1], Cond: x[2], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_rise_fall_or_with_three_outputs(self, tmp_path: Path):
        """Regression: rise/fall OR with 3 outputs in one rung round-trips."""
        from pyrung.core.program import call, subroutine

        C1 = Bool("C1")
        C2 = Bool("C2")
        C4 = Bool("C4")
        DS1 = Int("DS1")

        with Program() as logic:
            with Rung(any_of(rise(C1), fall(C2))):
                copy(1, DS1)
                copy(C2, C4)
                call("SubName")

            with subroutine("SubName"):
                with Rung():
                    out(C4)

        mapping = TagMap(
            {C1: c[1], C2: c[2], C4: c[4], DS1: ds[1]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "with Rung(rise(C1) | fall(C2)):" in code
        assert orig == repro


# ---------------------------------------------------------------------------
# In-memory round-trip tests (LadderBundle → ladder_to_pyrung, no disk I/O)
# ---------------------------------------------------------------------------


class TestInMemoryRoundTrip:
    def test_bundle_round_trip_no_disk(self):
        """ladder_to_pyrung(bundle) produces valid code without writing CSV to disk."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        code = ladder_to_pyrung(bundle)

        ns: dict = {}
        exec(code, ns)

        logic2 = ns["logic"]
        mapping2 = ns["mapping"]
        bundle2 = pyrung_to_ladder(logic2, mapping2)

        assert list(bundle.main_rows) == list(bundle2.main_rows)

    def test_bundle_round_trip_with_subroutines(self, tmp_path: Path):
        """ladder_to_pyrung(bundle) handles subroutine rows in-memory."""
        from pyrung.core.program import call, subroutine

        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung(A):
                    out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)

        # First via disk (reference)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "ref"
        bundle.write(csv_dir)
        code_disk = ladder_to_pyrung(csv_dir)

        # Then via in-memory
        code_mem = ladder_to_pyrung(bundle)

        assert code_disk == code_mem

    def test_disk_round_trip_with_subroutines_from_main_csv_path(self, tmp_path: Path):
        """Passing main.csv still loads sibling subroutine CSV files."""
        from pyrung.core.program import call, subroutine

        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung(A):
                    out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "ref"
        bundle.write(csv_dir)

        assert (csv_dir / "subroutines" / "my_sub.csv").exists()
        assert ladder_to_pyrung(csv_dir / "main.csv") == ladder_to_pyrung(csv_dir)

    def test_disk_import_requires_subroutines_directory(self, tmp_path: Path):
        """Subroutine imports require the sibling subroutines directory."""
        from pyrung.core.program import call, subroutine

        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung(A):
                    out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "missing_subs"
        bundle.write(csv_dir)

        (csv_dir / "subroutines" / "my_sub.csv").unlink()
        (csv_dir / "subroutines").rmdir()

        with pytest.raises(ValueError, match="subroutines directory not found"):
            ladder_to_pyrung(csv_dir)

        with pytest.raises(ValueError, match="subroutines directory not found"):
            ladder_to_pyrung(csv_dir / "main.csv")

    def test_bundle_round_trip_type_error(self):
        """ladder_to_pyrung rejects unsupported source types."""
        with pytest.raises(TypeError, match="source must be"):
            ladder_to_pyrung(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Nickname merge tests
# ---------------------------------------------------------------------------


class TestNicknameMerge:
    def test_dict_nicknames(self, tmp_path: Path):
        """Dict-based: verify generated code uses nickname variable names."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        nicks = {"X001": "start_button", "Y001": "motor_out"}
        code = ladder_to_pyrung(csv_path, nicknames=nicks)

        assert 'start_button = Bool("start_button")' in code
        assert 'motor_out = Bool("motor_out")' in code
        assert "# X001" in code
        assert "# Y001" in code
        assert "out(motor_out)" in code

    def test_dict_nicknames_reserved_python_names_prefixed(self, tmp_path: Path):
        """Reserved Python names become safe variable identifiers."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dst = Int("Dst")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dst)

        mapping = TagMap({Enable: x[1], Src: ds[1], Dst: ds[2]}, include_system=False)
        nicks = {"DS1": "True", "DS2": "False"}
        code, orig, repro = _round_trip(logic, mapping, tmp_path, nicknames=nicks)

        assert '_True = Int("True")' in code
        assert '_False = Int("False")' in code
        assert "copy(_True, _False)" in code
        assert orig == repro

    def test_dict_nicknames_prefixed_names_remain_unique(self, tmp_path: Path):
        """Sanitized identifiers stay unique if a nickname already has the prefix."""
        Enable = Bool("Enable")
        Src = Int("Src")
        Dst = Int("Dst")

        with Program() as logic:
            with Rung(Enable):
                copy(Src, Dst)

        mapping = TagMap({Enable: x[1], Src: ds[1], Dst: ds[2]}, include_system=False)
        nicks = {"DS1": "True", "DS2": "_True"}
        code = ladder_to_pyrung(_export_csv(logic, mapping, tmp_path), nicknames=nicks)

        assert '_True = Int("True")' in code
        assert '_True_2 = Int("_True")' in code
        assert "copy(_True, _True_2)" in code

    def test_dict_nicknames_round_trip(self, tmp_path: Path):
        """Nicknames round-trip: generated code re-exports same CSV."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        nicks = {"X001": "start_button", "Y001": "motor_out"}
        code, orig, repro = _round_trip(logic, mapping, tmp_path, nicknames=nicks)

        assert orig == repro

    def test_no_nicknames(self, tmp_path: Path):
        """Without nicknames, raw operand names are used."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)
        code = ladder_to_pyrung(csv_path)

        assert 'X001 = Bool("X001")' in code
        assert 'Y001 = Bool("Y001")' in code
        assert "out(Y001)" in code

    def test_both_raises(self, tmp_path: Path):
        """Providing both nickname_csv and nicknames raises ValueError."""
        csv_path = tmp_path / "dummy.csv"
        csv_path.write_text("marker,A\n")

        with pytest.raises(ValueError, match="not both"):
            ladder_to_pyrung(csv_path, nickname_csv="foo.csv", nicknames={"X001": "a"})


# ---------------------------------------------------------------------------
# Code generation structure tests
# ---------------------------------------------------------------------------


class TestCodeGeneration:
    def test_imports_minimal(self, tmp_path: Path):
        """Minimal program generates correct imports."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)
        code = ladder_to_pyrung(csv_path)

        assert "from pyrung import" in code
        assert "Program" in code
        assert "Rung" in code
        assert "Bool" in code
        assert "out" in code
        assert "from pyrung.click import" in code
        assert "TagMap" in code

    def test_output_file(self, tmp_path: Path):
        """output_path writes the generated file."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        out_path = tmp_path / "generated.py"
        code = ladder_to_pyrung(csv_path, output_path=out_path)

        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == code

    def test_tag_map_in_output(self, tmp_path: Path):
        """Generated code includes a TagMap constructor."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)
        code = ladder_to_pyrung(csv_path)

        assert "mapping = TagMap({" in code
        assert "x[1]" in code
        assert "y[1]" in code


# ---------------------------------------------------------------------------
# Structured codegen tests
# ---------------------------------------------------------------------------


class TestContinuedRoundTrip:
    """Round-trip tests for .continued() rungs."""

    def test_simple_continuation(self, tmp_path: Path):
        """Two independent wires: primary rung + continued rung."""
        A = Bool("A")
        B = Bool("B")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")

        with Program() as logic:
            with Rung(A):
                out(Y1)
            with Rung(B).continued():
                out(Y2)

        mapping = TagMap(
            {A: x[1], B: x[2], Y1: y[1], Y2: y[2]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert ".continued()" in code
        assert orig == repro

    def test_continued_chain(self, tmp_path: Path):
        """Three consecutive continued rungs share the same snapshot."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")
        Y3 = Bool("Y3")

        with Program() as logic:
            with Rung(A):
                out(Y1)
            with Rung(B).continued():
                out(Y2)
            with Rung(C).continued():
                out(Y3)

        mapping = TagMap(
            {A: x[1], B: x[2], C: c[1], Y1: y[1], Y2: y[2], Y3: y[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert code.count(".continued()") == 2
        assert orig == repro

    def test_continuation_with_branch(self, tmp_path: Path):
        """Continued rung with a branch inside it."""
        A = Bool("A")
        B = Bool("B")
        Mode = Bool("Mode")
        Y1 = Bool("Y1")
        Y2 = Bool("Y2")
        Y3 = Bool("Y3")

        with Program() as logic:
            with Rung(A):
                out(Y1)
            with Rung(B).continued():
                out(Y2)
                with branch(Mode):
                    out(Y3)

        mapping = TagMap(
            {A: x[1], B: x[2], Mode: x[3], Y1: y[1], Y2: y[2], Y3: y[3]},
            include_system=False,
        )
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert ".continued()" in code
        assert orig == repro


class TestStructuredCodegen:
    def _make_nickname_csv(self, tmp_path, records):
        """Helper to write a nickname CSV and return its path."""
        import pyclickplc

        path = tmp_path / "nicknames.csv"
        pyclickplc.write_csv(path, records)
        return path

    def test_named_array_codegen(self, tmp_path: Path):
        """Named array: codegen emits @named_array decorator and .map_to()."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Channel_id_1 = Int("Channel1_id")
        Channel_id_2 = Int("Channel2_id")

        with Program() as logic:
            with Rung(Enable):
                copy(Channel_id_1, Channel_id_2)

        mapping = TagMap(
            {Enable: x[1], Channel_id_1: ds[101], Channel_id_2: ds[103]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        # Build nickname CSV with named_array metadata
        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 101): AddressRecord(
                    memory_type="DS",
                    address=101,
                    nickname="Channel1_id",
                    comment="<Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 102): AddressRecord(
                    memory_type="DS",
                    address=102,
                    nickname="Channel1_val",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 103): AddressRecord(
                    memory_type="DS",
                    address=103,
                    nickname="Channel2_id",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 104): AddressRecord(
                    memory_type="DS",
                    address=104,
                    nickname="Channel2_val",
                    comment="</Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "@named_array(" in code
        assert "class Channel:" in code
        assert "Channel[1].id" in code or "Channel[2].id" in code
        assert "ds.select(" in code
        assert "mapping = TagMap([" in code
        assert "*Channel.map_to(" in code

    def test_named_array_codegen_accepts_sparse_nicknames(self, tmp_path: Path):
        """Sparse named_array nicknames still round-trip through codegen."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Channel_id_1 = Int("Channel1_id")
        Channel_id_2 = Int("Channel2_id")

        with Program() as logic:
            with Rung(Enable):
                copy(Channel_id_1, Channel_id_2)

        mapping = TagMap(
            {Enable: x[1], Channel_id_1: ds[101], Channel_id_2: ds[103]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 101): AddressRecord(
                    memory_type="DS",
                    address=101,
                    nickname="Channel1_id",
                    comment="<Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 102): AddressRecord(
                    memory_type="DS",
                    address=102,
                    nickname="",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 103): AddressRecord(
                    memory_type="DS",
                    address=103,
                    nickname="Channel2_id",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 104): AddressRecord(
                    memory_type="DS",
                    address=104,
                    nickname="",
                    comment="</Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "@named_array(Int, count=2, stride=2)" in code
        assert "class Channel:" in code
        assert "id = 0" in code
        assert "Channel[1].id" in code or "Channel[2].id" in code
        assert "val =" not in code

    def test_udt_codegen(self, tmp_path: Path):
        """UDT: codegen emits @udt decorator and .map_to()."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Motor_running = Bool("Motor1_running")
        Motor_speed = Int("Motor1_speed")

        with Program() as logic:
            with Rung(Enable):
                out(Motor_running)

        mapping = TagMap(
            {Enable: x[1], Motor_running: c[101], Motor_speed: ds[1001]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 1001): AddressRecord(
                    memory_type="DS",
                    address=1001,
                    nickname="Motor1_speed",
                    comment="<Motor.speed:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 1002): AddressRecord(
                    memory_type="DS",
                    address=1002,
                    nickname="Motor2_speed",
                    comment="</Motor.speed:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("C", 101): AddressRecord(
                    memory_type="C",
                    address=101,
                    nickname="Motor1_running",
                    comment="<Motor.running:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 102): AddressRecord(
                    memory_type="C",
                    address=102,
                    nickname="Motor2_running",
                    comment="</Motor.running:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "@udt(" in code
        assert "class Motor:" in code
        assert "Motor[1].running" in code
        assert "mapping = TagMap([" in code
        assert "Motor.running.map_to(" in code
        assert "Motor.speed.map_to(" in code

    def test_udt_field_range_codegen_uses_structure_not_plain_block(self, tmp_path: Path):
        """A range that exactly matches one UDT field should stay structured."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Bits = Block("Bits", TagType.BOOL, 1, 10)

        with Program() as logic:
            with Rung(Enable):
                reset(Bits.select(1, 10))

        mapping = TagMap({Enable: x[1], Bits: c.select(1031, 1040)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("C", 1031): AddressRecord(
                    memory_type="C",
                    address=1031,
                    nickname="Sts1_TagBitsA",
                    comment="<Sts.TagBitsA:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1040): AddressRecord(
                    memory_type="C",
                    address=1040,
                    nickname="Sts10_TagBitsA",
                    comment="</Sts.TagBitsA:udt>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "@udt(" in code
        assert "class Sts:" in code
        assert "TagBitsA: Bool = False" in code
        assert "Sts.TagBitsA.map_to(c.select(1031, 1040))" in code
        assert "reset(Sts.TagBitsA.select(1, 10))" in code
        assert 'C1031_to_C1040 = Block("C1031_to_C1040"' not in code

    def test_plain_block_range_codegen_uses_full_block_and_aliases(self, tmp_path: Path):
        """Plain named blocks become first-class Blocks with used slot aliases."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Bits = Block("Bits", TagType.BOOL, 1, 3)

        with Program() as logic:
            with Rung(Enable):
                out(Bits[1])
                reset(Bits.select(1, 3))

        mapping = TagMap({Enable: x[1], Bits: c.select(1004, 1006)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("C", 1001): AddressRecord(
                    memory_type="C",
                    address=1001,
                    nickname="",
                    comment="<CmdTagBits:block>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1004): AddressRecord(
                    memory_type="C",
                    address=1004,
                    nickname="Cmd_Mode_Production",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1019): AddressRecord(
                    memory_type="C",
                    address=1019,
                    nickname="",
                    comment="</CmdTagBits:block>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert 'CmdTagBits = Block("CmdTagBits", TagType.BOOL, 1, 19)' in code
        assert "CmdTagBits.configure_slot(4, name='Cmd_Mode_Production')" in code
        assert "Cmd_Mode_Production = CmdTagBits[4]" in code
        assert "out(Cmd_Mode_Production)" in code
        assert "reset(CmdTagBits.select(4, 6))" in code
        assert "CmdTagBits: c.select(1001, 1019)" in code
        assert 'Bool("Cmd_Mode_Production")' not in code
        assert 'C1004_to_C1006 = Block("C1004_to_C1006"' not in code

    def test_plain_block_explicit_start_uses_logical_indices(self, tmp_path: Path):
        """Explicit plain-block starts should preserve logical slot numbering."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Bits = Block("Bits", TagType.BOOL, 1, 3)

        with Program() as logic:
            with Rung(Enable):
                reset(Bits.select(1, 3))

        mapping = TagMap({Enable: x[1], Bits: c.select(1004, 1006)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("C", 1001): AddressRecord(
                    memory_type="C",
                    address=1001,
                    nickname="",
                    comment="<CmdTagBits:block(10)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1019): AddressRecord(
                    memory_type="C",
                    address=1019,
                    nickname="",
                    comment="</CmdTagBits:block(10)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert 'CmdTagBits = Block("CmdTagBits", TagType.BOOL, 10, 28)' in code
        assert "reset(CmdTagBits.select(13, 15))" in code

    def test_raw_range_codegen_inlines_hardware_bank_without_helper(self, tmp_path: Path):
        """Unnamed raw windows should use bank .select() inline, not helper Blocks."""
        Enable = Bool("Enable")
        Bits = Block("Bits", TagType.BOOL, 1, 3)

        with Program() as logic:
            with Rung(Enable):
                reset(Bits.select(1, 3))

        mapping = TagMap({Enable: x[1], Bits: c.select(1004, 1006)}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert "reset(c.select(1004, 1006))" in code
        assert 'C1004_to_C1006 = Block("C1004_to_C1006"' not in code
        assert "C1004_to_C1006:" not in code
        assert orig == repro

    def test_dense_named_array_backing_range_codegen_uses_select_instances(self, tmp_path: Path):
        """Dense named_array backing windows should rewrite to select_instances()."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Window = Block("Window", TagType.INT, 1, 4)

        with Program() as logic:
            with Rung(Enable):
                fill(0, Window.select(1, 4))

        mapping = TagMap({Enable: x[1], Window: ds.select(501, 504)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 501): AddressRecord(
                    memory_type="DS",
                    address=501,
                    nickname="Channel1_id",
                    comment="<Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 502): AddressRecord(
                    memory_type="DS",
                    address=502,
                    nickname="Channel1_val",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 503): AddressRecord(
                    memory_type="DS",
                    address=503,
                    nickname="Channel2_id",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 504): AddressRecord(
                    memory_type="DS",
                    address=504,
                    nickname="Channel2_val",
                    comment="</Channel:named_array(2,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "@named_array(Int, count=2)" in code
        assert "fill(0, Channel.select_instances(1, 2))" in code

    def test_sparse_named_array_backing_range_stays_raw_bank(self, tmp_path: Path):
        """Named_array windows with stride gaps should remain raw bank ranges."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Window = Block("Window", TagType.INT, 1, 6)

        with Program() as logic:
            with Rung(Enable):
                fill(0, Window.select(1, 6))

        mapping = TagMap({Enable: x[1], Window: ds.select(501, 506)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 501): AddressRecord(
                    memory_type="DS",
                    address=501,
                    nickname="Sensor1_raw",
                    comment="<Sensor:named_array(2,3)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 502): AddressRecord(
                    memory_type="DS",
                    address=502,
                    nickname="Sensor1_scaled",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 503): AddressRecord(
                    memory_type="DS",
                    address=503,
                    nickname="",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 504): AddressRecord(
                    memory_type="DS",
                    address=504,
                    nickname="Sensor2_raw",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 505): AddressRecord(
                    memory_type="DS",
                    address=505,
                    nickname="Sensor2_scaled",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 506): AddressRecord(
                    memory_type="DS",
                    address=506,
                    nickname="",
                    comment="</Sensor:named_array(2,3)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert "fill(0, ds.select(501, 506))" in code
        assert "@named_array(" not in code
        assert "Sensor.select_instances(" not in code

    def test_bare_block_marker_stays_raw_and_imports_tags(self, tmp_path: Path):
        """Bare block markers should be grouping-only and not reconstruct semantics."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Bits = Block("Bits", TagType.BOOL, 1, 3)

        with Program() as logic:
            with Rung(Enable):
                out(Bits[1])
                reset(Bits.select(1, 3))

        mapping = TagMap({Enable: x[1], Bits: c.select(1004, 1006)}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("C", 1001): AddressRecord(
                    memory_type="C",
                    address=1001,
                    nickname="",
                    comment="<CmdTagBits>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1004): AddressRecord(
                    memory_type="C",
                    address=1004,
                    nickname="Cmd_Mode_Production",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("C", 1019): AddressRecord(
                    memory_type="C",
                    address=1019,
                    nickname="",
                    comment="</CmdTagBits>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert 'Cmd_Mode_Production = Bool("Cmd_Mode_Production")' in code
        assert "out(Cmd_Mode_Production)" in code
        assert "reset(c.select(1004, 1006))" in code
        assert 'CmdTagBits = Block("CmdTagBits"' not in code
        assert "Cmd_Mode_Production = CmdTagBits[4]" not in code

    def test_bare_dotted_marker_stays_raw_and_imports_tags(self, tmp_path: Path):
        """Bare dotted markers should stay grouping-only and not reconstruct UDTs."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Config_timeout = Int("Config1_timeout")

        with Program() as logic:
            with Rung(Enable):
                copy(Config_timeout, Config_timeout)

        mapping = TagMap({Enable: x[1], Config_timeout: ds[301]}, include_system=False)
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 301): AddressRecord(
                    memory_type="DS",
                    address=301,
                    nickname="Config1_timeout",
                    comment="<Config.timeout />",
                    initial_value="100",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("C", 201): AddressRecord(
                    memory_type="C",
                    address=201,
                    nickname="Config1_enabled",
                    comment="<Config.enabled />",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert 'Config1_timeout = Int("Config1_timeout")' in code
        assert "@udt(" not in code
        assert "Config.timeout" not in code
        assert "copy(Config1_timeout, Config1_timeout)" in code

    def test_mixed_structured_and_flat(self, tmp_path: Path):
        """Mixed: some tags in structures, some flat → both coexist."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Ch_id = Int("Channel1_id")
        Flat = Int("FlatTag")

        with Program() as logic:
            with Rung(Enable):
                copy(Ch_id, Flat)

        mapping = TagMap(
            {Enable: x[1], Ch_id: ds[101], Flat: ds[200]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 101): AddressRecord(
                    memory_type="DS",
                    address=101,
                    nickname="Channel1_id",
                    comment="<Channel:named_array(1,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 102): AddressRecord(
                    memory_type="DS",
                    address=102,
                    nickname="Channel1_val",
                    comment="</Channel:named_array(1,2)>",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 200): AddressRecord(
                    memory_type="DS",
                    address=200,
                    nickname="FlatTag",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        # Should have both structure and flat tags
        assert "@named_array(" in code
        assert "class Channel:" in code
        assert 'FlatTag = Int("FlatTag")' in code
        # Flat tag should use regular variable name in TagMap
        assert "FlatTag.map_to(ds[200])" in code

    def test_singleton_structure_no_index(self, tmp_path: Path):
        """Singleton structure (count=1) → no instance index in references."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Config_timeout = Int("Config1_timeout")

        with Program() as logic:
            with Rung(Enable):
                copy(Config_timeout, Config_timeout)

        mapping = TagMap(
            {Enable: x[1], Config_timeout: ds[301]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("DS", 301): AddressRecord(
                    memory_type="DS",
                    address=301,
                    nickname="Config1_timeout",
                    comment="<Config.timeout:udt />",
                    initial_value="100",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("C", 201): AddressRecord(
                    memory_type="C",
                    address=201,
                    nickname="Config1_enabled",
                    comment="<Config.enabled:udt />",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("X", 1): AddressRecord(
                    memory_type="X",
                    address=1,
                    nickname="Enable",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        # Singleton → should use Config.timeout not Config[1].timeout
        assert "Config.timeout" in code
        assert "Config[1]" not in code

    def test_nickname_csv_preserves_custom_non_reserved_sc_sd_aliases(self, tmp_path: Path):
        """Custom SC/SD nicknames should import for non-reserved system addresses."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        ModeReady = Bool("ModeReady")
        RecipeShadow = Int("RecipeShadow")
        Mirror = Int("Mirror")

        with Program() as logic:
            with Rung(ModeReady):
                copy(RecipeShadow, Mirror)

        mapping = TagMap(
            {ModeReady: sc[20], RecipeShadow: sd[91], Mirror: ds[1]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        csv_dir = tmp_path / "csv_out"
        bundle.write(csv_dir)

        nick_path = self._make_nickname_csv(
            tmp_path,
            {
                get_addr_key("SC", 20): AddressRecord(
                    memory_type="SC",
                    address=20,
                    nickname="ModeReady",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.BIT,
                ),
                get_addr_key("SD", 91): AddressRecord(
                    memory_type="SD",
                    address=91,
                    nickname="RecipeShadow",
                    comment="",
                    initial_value="123",
                    retentive=False,
                    data_type=DataType.INT,
                ),
                get_addr_key("DS", 1): AddressRecord(
                    memory_type="DS",
                    address=1,
                    nickname="Mirror",
                    comment="",
                    initial_value="0",
                    retentive=False,
                    data_type=DataType.INT,
                ),
            },
        )

        code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_path)

        assert 'ModeReady = Bool("ModeReady")' in code
        assert 'RecipeShadow = Int("RecipeShadow")' in code
        assert 'Mirror = Int("Mirror")' in code
        assert "with Rung(ModeReady):" in code
        assert "copy(RecipeShadow, Mirror)" in code
        assert "ModeReady: sc[20]" in code
        assert "RecipeShadow: sd[91]" in code
        assert "Mirror: ds[1]" in code
        assert "system.rtc.year2" not in code

class TestNop:
    """Test NOP / empty rung codegen and round-trip."""

    def test_empty_rung_round_trip(self, tmp_path: Path):
        """Empty (pass) rung survives program → CSV NOP → codegen → exec → CSV₂."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Section header")
            with Rung():
                pass
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        # Codegen should emit pass, not nop()
        assert "pass" in code
        assert "nop()" not in code
        assert orig == repro

    def test_explicit_nop_round_trip(self, tmp_path: Path):
        """Explicit nop() rung survives round-trip via NOP in CSV."""
        from pyrung.click import nop

        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            comment("Explicit NOP")
            with Rung():
                nop()
            with Rung(A):
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        # Round-trip normalises nop() → pass (both map to CSV NOP)
        assert "pass" in code
        assert orig == repro

    def test_bare_text_rejected(self, tmp_path: Path):
        """Unknown bare text in AF column raises ValueError."""
        from pyrung.click.codegen.emitter import _render_af_token
        from pyrung.click.codegen.models import _OperandCollection

        collection = _OperandCollection()
        with pytest.raises(ValueError, match="Unrecognised AF token"):
            _render_af_token("BOGUS", collection, None)
