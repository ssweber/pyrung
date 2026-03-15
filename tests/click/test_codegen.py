"""Tests for CSV v2 → pyrung codegen (``csv_to_pyrung``)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pyrung.click import (
    TagMap,
    c,
    csv_to_pyrung,
    ct,
    ctd,
    ds,
    t,
    td,
    x,
    y,
)
from pyrung.click.codegen import (
    _analyze_rungs,
    _parse_af_args,
    _parse_csv,
)
from pyrung.core import (
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    any_of,
    fall,
    immediate,
    rise,
)
from pyrung.core.program import (
    branch,
    calc,
    copy,
    count_up,
    fill,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    unpack_to_bits,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _export_csv(program: Program, tag_map: TagMap, tmp_path: Path) -> Path:
    """Export a program to CSV and return the path."""
    bundle = tag_map.to_ladder(program)
    csv_path = tmp_path / "main.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(bundle.main_rows)
    return csv_path


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
    bundle = tag_map.to_ladder(program)
    original_rows = list(bundle.main_rows)

    csv_path = _export_csv(program, tag_map, tmp_path)
    code = csv_to_pyrung(csv_path, nicknames=nicknames)

    # Execute the generated code
    ns: dict = {}
    exec(code, ns)

    # Re-export
    logic2 = ns["logic"]
    mapping2 = ns["mapping"]
    bundle2 = mapping2.to_ladder(logic2)
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
        assert r.or_groups is None
        assert "X001" in r.shared_conditions
        assert "X002" in r.shared_conditions
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
        assert r.or_groups is not None
        assert len(r.or_groups) == 2

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
        assert r.or_groups is not None
        assert len(r.or_groups) == 2
        # Trailing AND should be in shared_conditions
        assert "C1" in r.shared_conditions

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
                on_delay(Done, Acc, preset=100, unit="Tms").reset(Reset)

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
            with Rung(A) as r:
                r.comment = "Start motor"
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
            with Rung(A) as r:
                r.comment = "Line 1\nLine 2"
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        csv_path = _export_csv(logic, mapping, tmp_path)

        raw_rungs = _parse_csv(csv_path)
        analyzed = _analyze_rungs(raw_rungs)
        assert analyzed[0].comment == "Line 1\nLine 2"


# ---------------------------------------------------------------------------
# Phase 3: Operand inference tests
# ---------------------------------------------------------------------------


class TestOperandInference:
    def test_all_prefix_types(self, tmp_path: Path):
        """Verify correct tag types inferred from all operand prefixes."""
        from pyrung.click.codegen import _parse_operand_prefix

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
        from pyrung.click.codegen import _parse_operand_prefix

        _, tag_type, block_var, _ = _parse_operand_prefix("CTD1")
        assert block_var == "ctd"
        assert tag_type == "Dint"

        _, tag_type, block_var, _ = _parse_operand_prefix("TD1")
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

        assert orig == repro

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
            with Rung(A) as r:
                r.comment = "Start motor"
                out(Y)

        mapping = TagMap({A: x[1], Y: y[1]}, include_system=False)
        code, orig, repro = _round_trip(logic, mapping, tmp_path)

        assert orig == repro

    def test_multiline_comment(self, tmp_path: Path):
        """Rung with multi-line comment."""
        A = Bool("A")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A) as r:
                r.comment = "Line 1\nLine 2"
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
                on_delay(Done, Acc, preset=100, unit="Tms").reset(ResetCond)

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
        code = csv_to_pyrung(csv_path, nicknames=nicks)

        assert 'start_button = Bool("start_button")' in code
        assert 'motor_out = Bool("motor_out")' in code
        assert "# X001" in code
        assert "# Y001" in code
        assert "out(motor_out)" in code

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
        code = csv_to_pyrung(csv_path)

        assert 'X001 = Bool("X001")' in code
        assert 'Y001 = Bool("Y001")' in code
        assert "out(Y001)" in code

    def test_both_raises(self, tmp_path: Path):
        """Providing both nickname_csv and nicknames raises ValueError."""
        csv_path = tmp_path / "dummy.csv"
        csv_path.write_text("marker,A\n")

        with pytest.raises(ValueError, match="not both"):
            csv_to_pyrung(csv_path, nickname_csv="foo.csv", nicknames={"X001": "a"})


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
        code = csv_to_pyrung(csv_path)

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
        code = csv_to_pyrung(csv_path, output_path=out_path)

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
        code = csv_to_pyrung(csv_path)

        assert "mapping = TagMap({" in code
        assert "x[1]" in code
        assert "y[1]" in code
        assert "include_system=False" in code
