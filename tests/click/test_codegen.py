"""Tests for laddercodec CSV → pyrung codegen (``ladder_to_pyrung``)."""

from __future__ import annotations

import csv
import textwrap
import warnings
from itertools import product
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyrung.click import (
    TagMap,
    c,
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
    Int,
    Program,
    Rung,
    TagType,
    Tms,
    any_of,
)
from pyrung.core.program import (
    branch,
    comment,
    copy,
    fill,
    latch,
    on_delay,
    out,
    reset,
)
from tests.click.helpers import build_program, normalize_pyrung, strip_pyrung_boilerplate

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


def _codegen_body(source: str) -> str:
    logic, mapping = build_program(source)
    bundle = pyrung_to_ladder(logic, mapping)
    return strip_pyrung_boilerplate(ladder_to_pyrung(bundle))


def _strip_codegen_program_body(code: str) -> str:
    """Extract the generated Program body while preserving rung comments."""
    lines = code.splitlines()

    prog_start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("with Program"):
            prog_start = i
            break

    if prog_start is None:
        raise ValueError(f"Expected 'with Program' in generated code, got:\n{code[:200]}")

    base_indent = len(lines[prog_start]) - len(lines[prog_start].lstrip())
    body_lines: list[str] = []
    for line in lines[prog_start + 1 :]:
        stripped = line.strip()
        if not stripped:
            body_lines.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        body_lines.append(line[base_indent + 4 :])

    return normalize_pyrung("\n".join(body_lines))


def _codegen_program_body(source: str) -> str:
    logic, mapping = build_program(source)
    bundle = pyrung_to_ladder(logic, mapping)
    return _strip_codegen_program_body(ladder_to_pyrung(bundle))


def _assert_codegen_body(source: str, expected: str) -> None:
    assert _codegen_body(source) == normalize_pyrung(textwrap.dedent(expected))


def _assert_codegen_program_body(source: str, expected: str) -> None:
    assert _codegen_program_body(source) == normalize_pyrung(textwrap.dedent(expected))


def _assert_generated_code(actual: str, expected: str) -> None:
    assert normalize_pyrung(actual) == normalize_pyrung(textwrap.dedent(expected))


def _assert_codegen_full(
    source: str, expected: str, *, nicknames: dict[str, str] | None = None
) -> None:
    logic, mapping = build_program(source)
    bundle = pyrung_to_ladder(logic, mapping)
    _assert_generated_code(ladder_to_pyrung(bundle, nicknames=nicknames), expected)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_WHEATSTONE_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "wheatstone_bridge.csv"
_WHEATSTONE_CONTACTS = ("X001", "X002", "X003", "X004", "X005")


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
        assert raw_rungs[0].rows == [rows[1]]

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
        assert _leaf_labels(r.condition_tree) == ["X001", "X002"]
        assert [instruction.af_token for instruction in r.instructions] == ["out(Y001)"]

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
        assert sorted(_leaf_labels(child) for child in par.children) == [["X001"], ["X002"]]
        assert set(_leaf_labels(r.condition_tree)) == {"X001", "X002", "C1"}

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
        assert _leaf_labels(r.condition_tree) == ["X001"]
        assert [instruction.af_token for instruction in r.instructions] == [
            "out(Y001)",
            "latch(Y002)",
        ]

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
    from pyrung.click._topology import Parallel, Series

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


def _collect_leaves(node) -> list:
    """Collect all ``Leaf`` nodes from an SP tree in order."""
    from pyrung.click._topology import Leaf, Parallel, Series

    if node is None:
        return []
    if isinstance(node, Leaf):
        return [node]
    if isinstance(node, (Series, Parallel)):
        leaves: list = []
        for child in node.children:
            leaves.extend(_collect_leaves(child))
        return leaves
    return []


def _leaf_labels(node) -> list[str]:
    """Collect all leaf labels from an SP tree in order."""
    return [leaf.label for leaf in _collect_leaves(node)]


def _eval_tree(node, values: dict[str, bool]) -> bool:
    """Evaluate an SP tree against a contact-value mapping."""
    from pyrung.click._topology import Leaf, Parallel, Series

    if node is None:
        return True
    if isinstance(node, Leaf):
        return values[node.label]
    if isinstance(node, Series):
        return all(_eval_tree(child, values) for child in node.children)
    if isinstance(node, Parallel):
        return any(_eval_tree(child, values) for child in node.children)
    raise TypeError(f"Unsupported SP node: {type(node)!r}")


def _is_reachable(start: str, sink: str, edges: list[tuple[str, str]]) -> bool:
    """Return whether *sink* is reachable through closed-contact conductivity."""
    stack = [start]
    seen: set[str] = set()
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, []).append(src)

    while stack:
        node = stack.pop()
        if node == sink:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))

    return False


def _wheatstone_expected(contacts: tuple[str, str, str, str, str], values: dict[str, bool]) -> bool:
    """Brute-force reachability for the canonical 5-edge bridge shape."""
    contact_edges = [
        ("source", "M1", contacts[0]),
        ("source", "M2", contacts[1]),
        ("M1", "sink", contacts[2]),
        ("M2", "sink", contacts[3]),
        ("M1", "M2", contacts[4]),
    ]
    active_edges = [(src, dst) for src, dst, label in contact_edges if values[label]]
    return _is_reachable("source", "sink", active_edges)


def _make_wheatstone_rows(
    contacts: tuple[str, str, str, str, str],
    *,
    offset: int = 0,
    af_token: str = "out(Y001)",
) -> list[list[str]]:
    """Build a Wheatstone bridge rung, optionally shifted right by *offset* columns."""
    if not 0 <= offset <= 27:
        raise ValueError(f"offset must keep the 4-column bridge in bounds, got {offset}")

    x001, x002, x003, x004, x005 = contacts

    top = {offset: x001, offset + 1: "T", offset + 2: x003, offset + 3: "T"}
    _fill_dashes(top, 0, offset)
    _fill_dashes(top, offset + 4, 31)

    middle = {offset + 1: x005, offset + 2: "|", offset + 3: "|"}

    bottom = {offset: x002, offset + 1: "-", offset + 2: x004}
    _fill_dashes(bottom, 0, offset)

    return [
        _make_row("R", top, af=af_token),
        _make_row("", middle),
        _make_row("", bottom),
    ]


@st.composite
def _wheatstone_grid(draw):
    """Generate renamed and shifted Wheatstone bridge grids."""
    contacts = tuple(
        draw(
            st.lists(
                st.integers(min_value=1, max_value=999).map(lambda n: f"X{n:03d}"),
                min_size=5,
                max_size=5,
                unique=True,
            )
        )
    )
    offset = draw(st.integers(min_value=0, max_value=27))
    return _make_wheatstone_rows(contacts, offset=offset), contacts, offset


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
        assert sorted(_leaf_labels(child) for child in par.children) == [["X001"], ["X002"]]
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

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
        assert _leaf_labels(result.condition_tree) == ["X001"]
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

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

        assert _leaf_labels(result.condition_tree) == ["X001"]
        assert [instruction.af_token for instruction in result.instructions] == [
            "out(Y001)",
            "out(Y002)",
        ]

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
        assert sorted(_leaf_labels(child) for child in par.children) == [
            ["X001"],
            ["X002"],
            ["X003"],
        ]

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
        assert _leaf_labels(result.condition_tree) == ["btn"]
        assert [instruction.af_token for instruction in result.instructions] == [
            "out(L1)",
            "out(L2)",
            "out(L3)",
        ]

        # The instruction for L2 has a branch-local condition containing "auto"
        l2_instr = next(i for i in result.instructions if i.af_token == "out(L2)")
        assert l2_instr.branch_tree is not None
        assert _leaf_labels(l2_instr.branch_tree) == ["auto"]

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
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

    def test_af_only_rows_share_an_implicit_source(self):
        """AF-only rows stay on the graph path and preserve all outputs."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        rows = [
            _make_row("R", {}, af="out(Y001)"),
            _make_row("", {}, af="out(Y002)"),
        ]
        rung = _RawRung(comment_lines=[], rows=rows)

        result = _analyze_single_rung(rung)
        assert result.condition_tree is None
        assert [instr.af_token for instr in result.instructions] == ["out(Y001)", "out(Y002)"]
        assert all(instr.branch_tree is None for instr in result.instructions)

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
        assert sorted(_leaf_labels(child) for child in par.children) == [["X001"], ["X002"]]
        assert set(_leaf_labels(result.condition_tree)) == {"X001", "X002", "C1", "C2"}

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
        assert _leaf_labels(result.condition_tree) == ["X001"]
        assert [instruction.af_token for instruction in result.instructions] == [
            "out(Y001)",
            "on_delay(T1)",
        ]
        assert result.instructions[0].pins == []
        assert len(result.instructions[1].pins) == 1
        assert result.instructions[1].pins[0].name == "reset"
        assert result.instructions[1].pins[0].conditions == ["X002"]

    def test_pin_row_requires_immediate_preceding_instruction(self):
        """A separated pin row should fail instead of attaching heuristically."""
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row0 = _make_row("R", _fill_dashes({0: "X001"}, 1, 31), af="on_delay(T1)")
        row1 = _make_row("", {})
        row2 = _make_row("", _fill_dashes({0: "X002"}, 1, 31), af=".reset()")
        rung = _RawRung(comment_lines=[], rows=[row0, row1, row2])

        with pytest.raises(ValueError, match="must immediately follow"):
            _analyze_single_rung(rung)

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
        assert _leaf_labels(result.condition_tree) == ["X001"]
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

    def test_adjacency_table_content_default(self):
        """Content tokens (contacts, comparisons) default to left/right exits.

        Ensures tokens like 'DS1==5' are traversed the same as '-'.
        """
        from pyrung.click.codegen.analyzer import _analyze_single_rung
        from pyrung.click.codegen.models import _RawRung

        row = _make_row("R", _fill_dashes({0: "X001", 1: "DS1==5"}, 2, 31), af="out(Y001)")
        rung = _RawRung(comment_lines=[], rows=[row])

        result = _analyze_single_rung(rung)
        assert _leaf_labels(result.condition_tree) == ["X001", "DS1==5"]
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

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

        assert result.condition_tree is None
        assert [instruction.af_token for instruction in result.instructions] == [
            "out(Y001)",
            "out(Y002)",
        ]
        assert [_leaf_labels(instruction.branch_tree) for instruction in result.instructions] == [
            ["X001"],
            ["X001", "X003", "X002"],
        ]

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

        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]
        assert set(_leaf_labels(result.condition_tree)) == {
            "X001",
            "X002",
            "X003",
            "X004",
            "X005",
            "X006",
            "X007",
        }
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

        assert result.condition_tree is None
        assert [instruction.af_token for instruction in result.instructions] == [
            "out(Y001)",
            "out(Y002)",
            "out(Y002)",
            "out(Y002)",
        ]

    def test_bridge_reduction_is_stable_across_edge_order(self):
        """Bridge-topology fallback should choose the same expansion edge each time."""
        from pyrung.click._topology import Leaf, Parallel
        from pyrung.click.codegen.analyzer import _Edge, _sp_reduce, _trees_equal

        s, u, v, t = 0, 1, 2, 3
        edges = [
            _Edge(s, u, Leaf("A"), 0, 0),
            _Edge(s, v, Leaf("B"), 1, 0),
            _Edge(u, t, Leaf("C"), 0, 1),
            _Edge(v, t, Leaf("D"), 1, 1),
            _Edge(u, v, Leaf("E"), 0, 2),
        ]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree_a = _sp_reduce(s, t, edges)
            tree_b = _sp_reduce(s, t, [edges[4], *edges[:4]])

        assert tree_a is not None
        assert tree_b is not None
        assert _trees_equal(tree_a, tree_b)
        assert isinstance(tree_a, Parallel)
        assert {label for label in _leaf_labels(tree_a)} == {"A", "B", "C", "D", "E"}


class TestShannonBridgeCoverage:
    def test_wheatstone_bridge_csv(self):
        """The real Wheatstone fixture triggers Shannon expansion and preserves all contacts."""
        raw_rungs = _parse_csv(_WHEATSTONE_FIXTURE)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            analyzed = _analyze_rungs(raw_rungs)

        assert len(analyzed) == 1
        assert [str(item.message) for item in caught] == [
            "Rung contains bridge topology; resolved via Shannon expansion"
        ]

        result = analyzed[0]
        assert [instruction.af_token for instruction in result.instructions] == ["out(Y001)"]

        tree = result.condition_tree
        assert tree is not None
        assert {leaf.label for leaf in _collect_leaves(tree)} == set(_WHEATSTONE_CONTACTS)

    def test_wheatstone_bridge_truth_table(self):
        """The Shannon-expanded tree matches brute-force reachability for all 2^5 inputs."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = _analyze_rungs(_parse_csv(_WHEATSTONE_FIXTURE))[0].condition_tree

        assert tree is not None

        for bits in product([False, True], repeat=5):
            values = dict(zip(_WHEATSTONE_CONTACTS, bits, strict=True))
            expected = _wheatstone_expected(_WHEATSTONE_CONTACTS, values)
            actual = _eval_tree(tree, values)
            assert actual == expected, f"Mismatch for {values}"

    @pytest.mark.hypothesis
    @settings(max_examples=40, deadline=None)
    @given(_wheatstone_grid())
    def test_wheatstone_bridge_variants_round_trip(self, grid):
        """Renamed and shifted bridge variants still trigger Shannon and stay stable."""
        from pyrung.click.codegen.analyzer import (
            _analyze_single_rung,
            _grid_to_graph,
            _sp_reduce,
            _trees_equal,
        )
        from pyrung.click.codegen.models import _RawRung

        rows, contacts, offset = grid
        rung = _RawRung(comment_lines=[], rows=rows)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _analyze_single_rung(rung)

        assert [str(item.message) for item in caught] == [
            "Rung contains bridge topology; resolved via Shannon expansion"
        ]

        tree = result.condition_tree
        assert tree is not None
        assert {leaf.label for leaf in _collect_leaves(tree)} == set(contacts)

        for bits in product([False, True], repeat=5):
            values = dict(zip(contacts, bits, strict=True))
            expected = _wheatstone_expected(contacts, values)
            actual = _eval_tree(tree, values)
            assert actual == expected, f"offset={offset}, values={values}"

        source, sinks, edges, _ = _grid_to_graph(rows, set())
        assert source is not None
        assert len(sinks) == 1
        sink, _, _ = sinks[0]

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree_a = _sp_reduce(source, sink, edges)
            tree_b = _sp_reduce(source, sink, list(reversed(edges)))

        assert tree_a is not None
        assert tree_b is not None
        assert _trees_equal(tree_a, tree_b)


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
    def test_simple_contact_coil(self):
        """Simple: X001 → out(Y001)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
            """,
            """
            with Rung(X001):
                out(Y001)
            """,
        )

    def test_and_chain(self):
        """AND chain: X001, X002 → out(Y001)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1, X2):
                    out(Y1)
            """,
            """
            with Rung(X001, X002):
                out(Y001)
            """,
        )

    def test_negated_contact(self):
        """NC contact: ~X001 → out(Y001)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(~X1):
                    out(Y1)
            """,
            """
            with Rung(~X001):
                out(Y001)
            """,
        )

    def test_rise_fall(self):
        """Edge contacts: rise(X001), fall(X002)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(rise(X1)):
                    out(Y1)
                with Rung(fall(X2)):
                    out(Y2)
            """,
            """
            with Rung(rise(X001)):
                out(Y001)

            with Rung(fall(X002)):
                out(Y002)
            """,
        )

    def test_or_expansion(self):
        """OR: any_of(A, B) → out(Y)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(any_of(X1, X2)):
                    out(Y1)
            """,
            """
            with Rung(X001 | X002):
                out(Y001)
            """,
        )

    def test_or_with_trailing_and(self):
        """OR + trailing AND."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(any_of(X1, X2), C1):
                    out(Y1)
            """,
            """
            with Rung(X001 | X002, C1):
                out(Y001)
            """,
        )

    def test_three_way_or(self):
        """3-branch OR exercises | output-bus marker."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(any_of(X1, X2, C1)):
                    out(Y1)
            """,
            """
            with Rung(any_of(X001, X002, C1)):
                out(Y001)
            """,
        )

    def test_two_way_comparison_or_stays_any_of(self):
        """2-way comparison OR stays as any_of(...) for readability and precedence."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(any_of(DS1 == 1, DS1 == 2)):
                    out(Y1)
            """,
            """
            with Rung(any_of(DS1 == 1, DS1 == 2)):
                out(Y001)
            """,
        )

    def test_mid_rung_or(self):
        """OR after AND exercises T: prefix on mid-rung contacts."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1, any_of(X2, C1)):
                    out(Y1)
            """,
            """
            with Rung(X001, X002 | C1):
                out(Y001)
            """,
        )

    def test_two_series_ors(self):
        """Two sequential ORs: first at col 0 (bare), second mid-rung (T: prefix)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(any_of(X1, X2), any_of(C1, C2)):
                    out(Y1)
            """,
            """
            with Rung(X001 | X002, C1 | C2):
                out(Y001)
            """,
        )

    def test_multiple_outputs(self):
        """Multiple outputs from same conditions."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    latch(Y2)
                    reset(Y3)
            """,
            """
            with Rung(X001):
                out(Y001)
                latch(Y002)
                reset(Y003)
            """,
        )

    def test_comment(self):
        """Rung with single-line comment."""
        _assert_codegen_program_body(
            """
            comment("Start motor")
            with Rung(X1):
                out(Y1)
            """,
            """
            comment("Start motor")
            with Rung(X001):
                out(Y001)
            """,
        )

    def test_multiline_comment(self):
        """Rung with multi-line comment."""
        _assert_codegen_program_body(
            """
            comment("Line 1\\nLine 2")
            with Rung(X1):
                out(Y1)
            """,
            '''
            comment("""\\
                Line 1
                Line 2""")
            with Rung(X001):
                out(Y001)
            ''',
        )

    def test_multiline_comment_with_triple_quotes(self):
        """Multi-line comment containing triple-double-quotes must not break syntax."""
        _assert_codegen_program_body(
            '''
            comment('Has """triple""" quotes\\nin comment text')
            with Rung(X1):
                out(Y1)
            ''',
            '''
            comment("""\\
                Has \\\"\\\"\\\"triple\\\"\\\"\\\" quotes
                in comment text""")
            with Rung(X001):
                out(Y001)
            ''',
        )

    def test_comparison_condition(self):
        """Comparison: DS1 == 5 → out(Y001)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(DS1 == 5):
                    out(Y1)
            """,
            """
            with Rung(DS1 == 5):
                out(Y001)
            """,
        )

    def test_timer_with_reset(self):
        """Timer with .reset() pin."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    on_delay(T1, TD1, preset=100, unit=Tms).reset(X2)
            """,
            """
            with Rung(X001):
                on_delay(T1, TD1, preset=100, unit=Tms).reset(X002)
            """,
        )

    def test_counter_with_pins(self):
        """Counter with .down() and .reset() pins."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    count_up(CT1, CTD1, preset=10).down(X2).reset(X3)
            """,
            """
            with Rung(X001):
                count_up(CT1, CTD1, preset=10).down(X002).reset(X003)
            """,
        )

    def test_copy(self):
        """Copy instruction."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    copy(DS1, DS2)
            """,
            """
            with Rung(X001):
                copy(DS1, DS2)
            """,
        )

    def test_branch(self):
        """Branch with conditions."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(X2):
                        out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002):
                    out(Y002)
            """,
        )

    def test_branch_local_or(self):
        """branch(any_of(...)) survives round-trip and stays branch-local."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(any_of(X2, X3)):
                        out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002 | X003):
                    out(Y002)
            """,
        )

    def test_branch_local_or_with_series_suffix_and_sibling_outputs(self):
        """Local branch OR with siblings before/after the branch stays row-identical."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(any_of(X2, X3), X4):
                        out(Y2)
                    out(Y3)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002 | X003, X004):
                    out(Y002)
                out(Y003)
            """,
        )

    def test_branch_series_then_local_or_followed_by_sibling_output(self):
        """A branch block can end on a lower OR leg and still resume the parent rung correctly."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    with branch(X2, any_of(X3, X4)):
                        out(Y1)
                    out(Y2)
            """,
            """
            with Rung(X001):
                with branch(X002, X003 | X004):
                    out(Y001)
                out(Y002)
            """,
        )

    def test_branch_series_then_three_way_local_or_followed_by_sibling_output(self):
        """Three-way branch-local OR keeps the parent continuation visible on intermediate rows."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    with branch(X2, any_of(X3, X4, X5)):
                        out(Y1)
                    out(Y2)
            """,
            """
            with Rung(X001):
                with branch(X002, any_of(X003, X004, X005)):
                    out(Y001)
                out(Y002)
            """,
        )

    def test_nested_branch(self):
        """Nested branch blocks export/import as equivalent row-identical topology."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    with branch(X2):
                        out(Y1)
                        with branch(X3):
                            out(Y2)
            """,
            """
            with Rung(X001, X002):
                out(Y001)
                with branch(X003):
                    out(Y002)
            """,
        )

    def test_branch_2_deep(self):
        """2-deep nesting emits flat branch(B, C)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(X2, C1):
                        out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002, C1):
                    out(Y002)
            """,
        )

    def test_branch_3_deep(self):
        """3-deep nesting emits flat branch(B, C, D)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(X2, C1, C2):
                        out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002, C1, C2):
                    out(Y002)
            """,
        )

    def test_branch_interleaved_across_depths(self):
        """Multiple branches at same level with different depths."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(X2):
                        out(Y2)
                    with branch(C1):
                        out(Y3)
            """,
            """
            with Rung(X001):
                out(Y001)
                with branch(X002):
                    out(Y002)
                with branch(C1):
                    out(Y003)
            """,
        )

    def test_forloop(self):
        """For/next loop."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    with forloop(3, oneshot=True):
                        out(Y1)
            """,
            """
            with Rung(X001):
                with forloop(3, oneshot=True):
                    out(Y001)
            """,
        )

    def test_immediate_contact(self):
        """Immediate contact."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(immediate(X1)):
                    out(Y1)
            """,
            """
            with Rung(immediate(X001)):
                out(Y001)
            """,
        )

    def test_immediate_coil(self):
        """Immediate coil (immediate only in AF, not conditions)."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(immediate(Y1))
            """,
            """
            with Rung(X001):
                out(immediate(Y001))
            """,
        )

    def test_calc(self):
        """Calc instruction."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    calc(DS1 + DS2, DS3)
            """,
            """
            with Rung(X001):
                calc(DS1 + DS2, DS3)
            """,
        )

    def test_calc_decimal_operators(self):
        """Calc with power, modulo, and math functions (decimal-mode) round-trips."""
        _assert_codegen_body(
            """
            from pyrung.core.expression import sqrt
            with Rung(X1):
                calc(DS1**2, DS3)
            with Rung(X1):
                calc(DS1 % DS2, DS3)
            with Rung(X1):
                calc(sqrt(DS1), DS3)
            """,
            """
            with Rung(X001):
                calc(DS1**2, DS3)

            with Rung(X001):
                calc(DS1 % DS2, DS3)

            with Rung(X001):
                calc(sqrt(DS1), DS3)
            """,
        )

    def test_calc_hex_shift_operators(self):
        """Calc with LSH/RSH (hex-mode) round-trips."""
        _assert_codegen_body(
            """
            from pyrung.core.expression import lsh
            with Rung(X1):
                calc(DH1 << 3, DH3)
            with Rung(X1):
                calc(lsh(DH1, 4), DH3)
            """,
            """
            with Rung(X001):
                calc(lsh(DH1, 3), DH3)

            with Rung(X001):
                calc(lsh(DH1, 4), DH3)
            """,
        )

    def test_calc_bitwise_codegen(self):
        """Calc with AND/OR/XOR round-trips through Click-native operators."""
        _assert_codegen_body(
            """
            with Rung(X1):
                calc(DH1 & DH2, DH3)
            with Rung(X1):
                calc(DH1 | DH2, DH3)
            with Rung(X1):
                calc(DH1 ^ DH2, DH3)
            """,
            """
            with Rung(X001):
                calc(DH1 & DH2, DH3)

            with Rung(X001):
                calc(DH1 | DH2, DH3)

            with Rung(X001):
                calc(DH1 ^ DH2, DH3)
            """,
        )

    def test_click_hex_literal_in_comparison(self, tmp_path: Path):
        """Click hex literal ``0000h`` in a condition becomes ``0x0000`` in Python."""
        csv_path = tmp_path / "test.csv"
        header = [
            "marker",
            *[chr(ord("A") + i) for i in range(26)],
            *[f"A{chr(ord('A') + i)}" for i in range(5)],
            "AF",
        ]
        rows = [
            header,
            ["R", "DH001==0000h", *["-"] * 30, "out(C001)"],
            ["R", "DH001==FFFFh", *["-"] * 30, "out(C002)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        code = ladder_to_pyrung(csv_path)
        assert _strip_codegen_program_body(code) == normalize_pyrung(
            textwrap.dedent(
                """
                with Rung(DH001 == 0x0000):
                    out(C001)

                with Rung(DH001 == 0xFFFF):
                    out(C002)
                """
            )
        )

        # Generated code must be valid Python
        ns: dict = {}
        exec(code, ns)

    def test_click_hex_literal_in_calc(self, tmp_path: Path):
        """Click hex literal in a calc expression becomes ``0x`` in Python."""
        csv_path = tmp_path / "test.csv"
        header = [
            "marker",
            *[chr(ord("A") + i) for i in range(26)],
            *[f"A{chr(ord('A') + i)}" for i in range(5)],
            "AF",
        ]
        rows = [
            header,
            ["R", "-", *["-"] * 30, "math(DH001 AND 00FFh,DH002)"],
            ["R", "-", *["-"] * 30, "math(DH001 AND FFFFh,DH003)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        code = ladder_to_pyrung(csv_path)
        assert _strip_codegen_program_body(code) == normalize_pyrung(
            textwrap.dedent(
                """
                with Rung():
                    calc(DH001 & 0x00FF, DH002)

                with Rung():
                    calc(DH001 & 0xFFFF, DH003)
                """
            )
        )

        ns: dict = {}
        exec(code, ns)

    def test_pointer_indirect_addressing(self, tmp_path: Path):
        """Click pointer syntax DH[DS134] renders as dh[tag_var] with correct import."""
        csv_path = tmp_path / "test.csv"
        header = [
            "marker",
            *[chr(ord("A") + i) for i in range(26)],
            *[f"A{chr(ord('A') + i)}" for i in range(5)],
            "AF",
        ]
        rows = [
            header,
            ["R", "-", *["-"] * 30, "copy(DH[DS134],DH051)"],
        ]
        with csv_path.open("w", newline="") as f:
            csv.writer(f).writerows(rows)

        code = ladder_to_pyrung(csv_path)
        _assert_generated_code(
            code,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Int, Word, copy
            from pyrung.click import TagMap, dh, ds

            # --- Tags ---
            DS134 = Int("DS134")
            DH051 = Word("DH051")

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung():
                    copy(dh[DS134], DH051)

            # --- Tag Map ---
            mapping = TagMap({
                DS134: ds[134],
                DH051: dh[51],
            })
            """,
        )
        # Must be valid Python
        ns: dict = {}
        exec(code, ns)

    def test_calc_sum_codegen(self):
        """Calc with SUM(range) round-trips through colon-range syntax."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    calc(dh.select(1, 5).sum(), dh[100])
            """,
            """
            with Rung(X001):
                calc(dh.select(1, 5).sum(), DH100)
            """,
        )

    def test_fill(self):
        """Fill instruction."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    fill(0, ds.select(1, 10))
            """,
            """
            with Rung(X001):
                fill(0, ds.select(1, 10))
            """,
        )

    def test_unpack_to_bits(self):
        """Unpack instruction with range."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    unpack_to_bits(DS1, c.select(1, 16))
            """,
            """
            with Rung(X001):
                unpack_to_bits(DS1, c.select(1, 16))
            """,
        )

    def test_shift(self):
        """Shift register with .clock() and .reset() pins."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    shift(c.select(1, 8)).clock(X2).reset(X3)
            """,
            """
            with Rung(X001):
                shift(c.select(1, 8)).clock(X002).reset(X003)
            """,
        )

    def test_search(self):
        """Search instruction."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    search(ds.select(1, 4) == DS5, result=DS6, found=C1)
            """,
            """
            with Rung(X001):
                search(ds.select(1, 4) == DS5, result=DS6, found=C1)
            """,
        )

    def test_event_drum(self):
        """Event drum with reset pin."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    event_drum(
                        outputs=[Y1, Y2],
                        events=[X3, X4],
                        pattern=[[1, 0], [0, 1]],
                        current_step=DS1,
                        completion_flag=C1,
                    ).reset(X2)
            """,
            """
            with Rung(X001):
                event_drum(outputs=[Y001, Y002], events=[X003, X004], pattern=[[1, 0], [0, 1]], current_step=DS1, completion_flag=C1).reset(X002)
            """,
        )

    def test_time_drum(self):
        """Time drum with reset pin."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    time_drum(
                        outputs=[Y1, Y2],
                        presets=[100, 200],
                        unit=Tms,
                        pattern=[[1, 0], [0, 1]],
                        current_step=DS1,
                        accumulator=TD1,
                        completion_flag=C1,
                    ).reset(X2)
            """,
            """
            with Rung(X001):
                time_drum(outputs=[Y001, Y002], presets=[100, 200], unit=Tms, pattern=[[1, 0], [0, 1]], current_step=DS1, accumulator=TD1, completion_flag=C1).reset(X002)
            """,
        )

    def test_send(self):
        """Send instruction with ModbusTcpTarget."""
        _assert_codegen_body(
            """
            with Rung(X1):
                send(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                send(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start="DS1", source=DS1, sending=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_receive(self):
        """Receive instruction with ModbusTcpTarget."""
        _assert_codegen_body(
            """
            with Rung(X1):
                receive(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                receive(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start="DS1", dest=DS1, receiving=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_send_rtu(self):
        """Send instruction with ModbusRtuTarget."""
        _assert_codegen_body(
            """
            with Rung(X1):
                send(
                    target=ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=5, com_port="cpu2"),
                    remote_start="DS1",
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                send(target=ModbusRtuTarget(name="vfd1", com_port="cpu2", device_id=5), remote_start="DS1", source=DS1, sending=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_receive_rtu(self):
        """Receive instruction with ModbusRtuTarget."""
        _assert_codegen_body(
            """
            with Rung(X1):
                receive(
                    target=ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=5, com_port="slot0_1"),
                    remote_start="DS1",
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                receive(target=ModbusRtuTarget(name="vfd1", com_port="slot0_1", device_id=5), remote_start="DS1", dest=DS1, receiving=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_send_modbus_address(self):
        """Send instruction with ModbusAddress remote_start."""
        _assert_codegen_body(
            """
            with Rung(X1):
                send(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                send(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start=ModbusAddress(address=400001), source=DS1, sending=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_receive_modbus_address(self):
        """Receive instruction with ModbusAddress remote_start."""
        _assert_codegen_body(
            """
            with Rung(X1):
                receive(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                receive(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start=ModbusAddress(address=400001), dest=DS1, receiving=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_send_rtu_modbus_address(self):
        """Send with ModbusRtuTarget and ModbusAddress remote_start."""
        _assert_codegen_body(
            """
            with Rung(X1):
                send(
                    target=ModbusRtuTarget("vfd1", com_port="slot1_2", device_id=2),
                    remote_start=ModbusAddress(100, RegisterType.HOLDING),
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                send(target=ModbusRtuTarget(name="vfd1", com_port="slot1_2", device_id=2), remote_start=ModbusAddress(address=400101), source=DS1, sending=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_receive_rtu_modbus_address(self):
        """Receive with ModbusRtuTarget and ModbusAddress remote_start."""
        _assert_codegen_body(
            """
            with Rung(X1):
                receive(
                    target=ModbusRtuTarget("vfd1", com_port="slot1_2", device_id=2),
                    remote_start=ModbusAddress(100, RegisterType.HOLDING),
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
            """,
            """
            with Rung(X001):
                receive(target=ModbusRtuTarget(name="vfd1", com_port="slot1_2", device_id=2), remote_start=ModbusAddress(address=400101), dest=DS1, receiving=C1, success=C2, error=C3, exception_response=DS2)
            """,
        )

    def test_send_block_range(self):
        """Send with BlockRange source round-trips through DS1..DS3."""
        _assert_codegen_body(
            """
            with Rung(X1):
                send(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    source=ds.select(1, 3),
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS4,
                )
            """,
            """
            with Rung(X001):
                send(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start="DS1", source=ds.select(1, 3), sending=C1, success=C2, error=C3, exception_response=DS4)
            """,
        )

    def test_receive_block_range(self):
        """Receive with BlockRange dest round-trips through DS1..DS3."""
        _assert_codegen_body(
            """
            with Rung(X1):
                receive(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    dest=ds.select(1, 3),
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS4,
                )
            """,
            """
            with Rung(X001):
                receive(target=ModbusTcpTarget(name="plc2", ip="192.168.1.2", port=502, device_id=1), remote_start="DS1", dest=ds.select(1, 3), receiving=C1, success=C2, error=C3, exception_response=DS4)
            """,
        )

    def test_subroutine(self):
        """Subroutine with call() and return."""
        _assert_codegen_body(
            """
            with Rung(X1):
                out(Y1)
                call("init")

            with subroutine("init"):
                with Rung():
                    out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
                call("init")

            with subroutine("init", strict=False):
                with Rung():
                    out(Y002)
            """,
        )

    def test_subroutine_with_conditions(self):
        """Subroutine rungs with conditions."""
        _assert_codegen_body(
            """
            with Rung(X1):
                call("worker")

            with subroutine("worker"):
                with Rung(X2):
                    out(Y1)
                with Rung():
                    out(Y2)
            """,
            """
            with Rung(X001):
                call("worker")

            with subroutine("worker", strict=False):
                with Rung(X002):
                    out(Y001)

                with Rung():
                    out(Y002)
            """,
        )

    def test_rise_fall_or_with_three_outputs(self):
        """Regression: rise/fall OR with 3 outputs in one rung round-trips."""
        _assert_codegen_body(
            """
            with Rung(any_of(rise(C1), fall(C2))):
                copy(1, DS1)
                copy(C2, C4)
                call("SubName")

            with subroutine("SubName"):
                with Rung():
                    out(C4)
            """,
            """
            with Rung(rise(C1) | fall(C2)):
                copy(1, DS1)
                copy(C2, C4)
                call("SubName")

            with subroutine("SubName", strict=False):
                with Rung():
                    out(C4)
            """,
        )


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
            ladder_to_pyrung(42)  # ty: ignore[invalid-argument-type]


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
        _assert_generated_code(
            code,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, out
            from pyrung.click import TagMap, x, y

            # --- Tags ---
            start_button = Bool("start_button")  # X001
            motor_out = Bool("motor_out")  # Y001

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(start_button):
                    out(motor_out)

            # --- Tag Map ---
            mapping = TagMap({
                start_button: x[1],
                motor_out: y[1],
            })
            """,
        )

    def test_dict_nicknames_reserved_python_names_prefixed(self):
        """Reserved Python names become safe variable identifiers."""
        _assert_codegen_full(
            """
            with Rung(X1):
                copy(DS1, DS2)
            """,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, Int, copy
            from pyrung.click import TagMap, ds, x

            # --- Tags ---
            _True = Int("True")  # DS1
            _False = Int("False")  # DS2
            X001 = Bool("X001")

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(X001):
                    copy(_True, _False)

            # --- Tag Map ---
            mapping = TagMap({
                _True: ds[1],
                _False: ds[2],
                X001: x[1],
            })
            """,
            nicknames={"DS1": "True", "DS2": "False"},
        )

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
        _assert_generated_code(
            code,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, Int, copy
            from pyrung.click import TagMap, ds, x

            # --- Tags ---
            _True = Int("True")  # DS1
            _True_2 = Int("_True")  # DS2
            X001 = Bool("X001")

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(X001):
                    copy(_True, _True_2)

            # --- Tag Map ---
            mapping = TagMap({
                _True: ds[1],
                _True_2: ds[2],
                X001: x[1],
            })
            """,
        )

    def test_dict_nicknames_codegen(self):
        """Nicknames round-trip: generated code re-exports same CSV."""
        _assert_codegen_full(
            """
            with Rung(X1):
                out(Y1)
            """,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, out
            from pyrung.click import TagMap, x, y

            # --- Tags ---
            start_button = Bool("start_button")  # X001
            motor_out = Bool("motor_out")  # Y001

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(start_button):
                    out(motor_out)

            # --- Tag Map ---
            mapping = TagMap({
                start_button: x[1],
                motor_out: y[1],
            })
            """,
            nicknames={"X001": "start_button", "Y001": "motor_out"},
        )

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
        _assert_generated_code(
            code,
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, out
            from pyrung.click import TagMap, x, y

            # --- Tags ---
            X001 = Bool("X001")
            Y001 = Bool("Y001")

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(X001):
                    out(Y001)

            # --- Tag Map ---
            mapping = TagMap({
                X001: x[1],
                Y001: y[1],
            })
            """,
        )

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
        import_lines = "\n".join(line for line in code.splitlines() if line.startswith("from "))
        assert normalize_pyrung(import_lines) == normalize_pyrung(
            textwrap.dedent(
                """
                from pyrung import Program, Rung, Bool, out
                from pyrung.click import TagMap, x, y
                """
            )
        )

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
        mapping_block = code.split("# --- Tag Map ---", maxsplit=1)[1]
        assert normalize_pyrung(mapping_block) == normalize_pyrung(
            textwrap.dedent(
                """
                mapping = TagMap({
                    X001: x[1],
                    Y001: y[1],
                })
                """
            )
        )


# ---------------------------------------------------------------------------
# Structured codegen tests
# ---------------------------------------------------------------------------


class TestContinuedRoundTrip:
    """Round-trip tests for .continued() rungs."""

    def test_simple_continuation(self):
        """Two independent wires: primary rung + continued rung."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2).continued():
                    out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
            with Rung(X002).continued():
                out(Y002)
            """,
        )

    def test_continued_chain(self):
        """Three consecutive continued rungs share the same snapshot."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2).continued():
                    out(Y2)
                with Rung(C1).continued():
                    out(Y3)
            """,
            """
            with Rung(X001):
                out(Y001)
            with Rung(X002).continued():
                out(Y002)
            with Rung(C1).continued():
                out(Y003)
            """,
        )

    def test_continuation_with_branch(self):
        """Continued rung with a branch inside it."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2).continued():
                    out(Y2)
                    with branch(X3):
                        out(Y3)
            """,
            """
            with Rung(X001):
                out(Y001)
            with Rung(X002).continued():
                out(Y002)
                with branch(X003):
                    out(Y003)
            """,
        )

    def test_continuation_with_branch_local_or(self):
        """continued() stays stable when the continued rung has branch-local OR."""
        _assert_codegen_body(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2).continued():
                    with branch(any_of(X3, X4)):
                        out(Y2)
            """,
            """
            with Rung(X001):
                out(Y001)
            with Rung(X002, X003 | X004).continued():
                out(Y002)
            """,
        )

    def test_nested_branch_outputs_preserve_inner_shared_prefix(self):
        """Nested shared prefixes should come back as nested branch() blocks."""
        _assert_codegen_body(
            """
            with Program(strict=False) as p:
                with Rung(X1):
                    with branch(X2):
                        with branch(X3):
                            out(Y1)
                        with branch(X4):
                            out(Y2)
                    with branch(X5):
                        out(Y3)
            """,
            """
            with Rung(X001):
                with branch(X002):
                    with branch(X003):
                        out(Y001)
                    with branch(X004):
                        out(Y002)
                with branch(X005):
                    out(Y003)
            """,
        )

    def test_greedy_group_prefix_shrinks_but_stays_nonempty(self):
        """Growing a group shrinks the shared prefix; recursion recovers deeper nesting.

        Outputs: B·C·G, B·C·H, B·E under rung A.
        Pair {1,2} shares [B,C]; triple {1,2,3} shares only [B].
        ``_group_outputs`` absorbs the globally-shared B into the rung
        condition.  The emitter's greedy loop then finds C shared between
        the first two outputs and nests them.
        """
        _assert_codegen_body(
            """
            with Program(strict=False) as p:
                with Rung(X1):
                    with branch(X2, X3, X5):
                        out(Y1)
                    with branch(X2, X3, X6):
                        out(Y2)
                    with branch(X2, X4):
                        out(Y3)
            """,
            """
            with Rung(X001, X002):
                with branch(X003):
                    with branch(X005):
                        out(Y001)
                    with branch(X006):
                        out(Y002)
                with branch(X004):
                    out(Y003)
            """,
        )

    def test_greedy_group_disjoint_consecutive_groups(self):
        """Consecutive outputs with disjoint prefixes form separate groups.

        Outputs: B·C (standalone), D·E, D·F under rung A.
        {1,2} share nothing, so output 1 is emitted alone.  Then {2,3}
        share D and are grouped.
        """
        _assert_codegen_body(
            """
            with Program(strict=False) as p:
                with Rung(X1):
                    with branch(X2, X3):
                        out(Y1)
                    with branch(X4, X5):
                        out(Y2)
                    with branch(X4, X6):
                        out(Y3)
            """,
            """
            with Rung(X001):
                with branch(X002, X003):
                    out(Y001)
                with branch(X004):
                    with branch(X005):
                        out(Y002)
                    with branch(X006):
                        out(Y003)
            """,
        )

    def test_greedy_group_all_share_single_prefix(self):
        """All outputs share the same single-level prefix — one group.

        Outputs: B·C, B·D, B·E under rung A.
        All three share [B]; ``_group_outputs`` absorbs B into the rung
        condition, leaving C, D, E as flat sibling branches.
        """
        _assert_codegen_body(
            """
            with Program(strict=False) as p:
                with Rung(X1):
                    with branch(X2, X3):
                        out(Y1)
                    with branch(X2, X4):
                        out(Y2)
                    with branch(X2, X5):
                        out(Y3)
            """,
            """
            with Rung(X001, X002):
                with branch(X003):
                    out(Y001)
                with branch(X004):
                    out(Y002)
                with branch(X005):
                    out(Y003)
            """,
        )

    def test_greedy_group_four_outputs_two_disjoint_pairs(self):
        """Four outputs forming two independent pairs with different prefixes.

        Outputs: B·C, B·D, E·F, E·G under rung A.
        {1,2} share B; {3,4} share E; {2,3} share nothing.
        Should emit two separate grouped branches.
        """
        _assert_codegen_body(
            """
            with Program(strict=False) as p:
                with Rung(X1):
                    with branch(X2, X3):
                        out(Y1)
                    with branch(X2, X4):
                        out(Y2)
                    with branch(X5, X6):
                        out(Y3)
                    with branch(X5, X7):
                        out(Y4)
            """,
            """
            with Rung(X001):
                with branch(X002):
                    with branch(X003):
                        out(Y001)
                    with branch(X004):
                        out(Y002)
                with branch(X005):
                    with branch(X006):
                        out(Y003)
                    with branch(X007):
                        out(Y004)
            """,
        )


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
        assert "id = Field(retentive=False)" in code
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
        assert "CmdTagBits.slot(4, name='Cmd_Mode_Production')" in code
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
        _assert_generated_code(
            ladder_to_pyrung(pyrung_to_ladder(logic, mapping)),
            """
            \"\"\"Auto-generated pyrung program from laddercodec CSV.\"\"\"

            from pyrung import Program, Rung, Bool, reset
            from pyrung.click import TagMap, c, x

            # --- Tags ---
            X001 = Bool("X001")

            # --- Program ---
            with Program(strict=False) as logic:
                with Rung(X001):
                    reset(c.select(1004, 1006))

            # --- Tag Map ---
            mapping = TagMap({
                X001: x[1],
            })
            """,
        )

    def test_dense_named_array_backing_range_codegen_uses_instance_select(self, tmp_path: Path):
        """Dense named_array backing windows should rewrite to instance_select()."""
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

        assert "@named_array(Int, count=2, stride=2)" in code
        assert "fill(0, Channel.instance_select(1, 2))" in code

    def test_sparse_named_array_backing_range_uses_instance_select(self, tmp_path: Path):
        """Named_array windows with stride gaps should rewrite to instance_select()."""
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

        assert "@named_array(Int, count=2, stride=3)" in code
        assert "fill(0, Sensor.instance_select(1, 2))" in code

    def test_partial_named_array_range_gets_comment(self, tmp_path: Path):
        """Sub-instance range within a named_array emits raw select with comment."""
        from pyclickplc.addresses import AddressRecord, get_addr_key
        from pyclickplc.banks import DataType

        Enable = Bool("Enable")
        Window = Block("Window", TagType.INT, 1, 4)

        with Program() as logic:
            with Rung(Enable):
                fill(0, Window.select(2, 3))

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

        assert "ds.select(502, 503)" in code
        assert "# Channel: val..id" in code

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

    def test_empty_rung_codegen_emits_pass(self):
        """Empty (pass) rung survives program → CSV NOP → codegen → exec → CSV₂."""
        _assert_codegen_program_body(
            """
            comment("Section header")
            with Rung():
                pass
            with Rung(X1):
                out(Y1)
            """,
            """
            comment("Section header")
            with Rung():
                pass

            with Rung(X001):
                out(Y001)
            """,
        )

    def test_explicit_nop_codegen_normalizes_to_pass(self):
        """Explicit nop() rung survives round-trip via NOP in CSV."""
        _assert_codegen_program_body(
            """
            comment("Explicit NOP")
            with Rung():
                nop()
            with Rung(X1):
                out(Y1)
            """,
            """
            comment("Explicit NOP")
            with Rung():
                pass

            with Rung(X001):
                out(Y001)
            """,
        )

    def test_bare_text_rejected(self, tmp_path: Path):
        """Unknown bare text in AF column raises ValueError."""
        from pyrung.click.codegen.emitter import _render_af_token
        from pyrung.click.codegen.models import _OperandCollection

        collection = _OperandCollection()
        with pytest.raises(ValueError, match="Unrecognised AF token"):
            _render_af_token("BOGUS", collection, None)

    def test_blank_raw_rung_preserved_as_pass(self):
        """A raw blank rung should survive import as an explicit pass rung."""
        from pyrung.click.ladder.types import LadderBundle

        header = (
            "marker",
            *tuple(
                [chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]
            ),
            "AF",
        )
        bundle = LadderBundle(
            main_rows=(
                header,
                tuple(_make_row("R", {})),
                tuple(_make_row("R", {0: "X001"}, "out(Y001)")),
            ),
            subroutine_rows=(),
        )

        body = _strip_codegen_program_body(ladder_to_pyrung(bundle))
        assert body == normalize_pyrung(
            textwrap.dedent(
                """
            with Rung():
                pass

            with Rung(X001):
                out(Y001)
            """
            )
        )

    def test_partial_rung_without_output_fails_loudly(self):
        """A rung with conditions but no completed output object should be rejected."""
        from pyrung.click.ladder.types import LadderBundle

        header = (
            "marker",
            *tuple(
                [chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]
            ),
            "AF",
        )
        bundle = LadderBundle(
            main_rows=(
                header,
                tuple(_make_row("R", {0: "X001"})),
            ),
            subroutine_rows=(),
        )

        with pytest.raises(ValueError, match="complete output object"):
            ladder_to_pyrung(bundle)

    def test_render_pin_rejects_lossy_flat_conditions(self):
        """Pins must not truncate multi-token conditions down to the first token."""
        from pyrung.click.codegen.emitter import _render_pin
        from pyrung.click.codegen.models import _OperandCollection, _PinInfo

        pin = _PinInfo(
            name="reset",
            arg="",
            conditions=["X001", "X002"],
            condition_tree=None,
        )

        with pytest.raises(ValueError, match="cannot be rendered losslessly"):
            _render_pin(pin, _OperandCollection(), None)
