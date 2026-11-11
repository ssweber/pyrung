"""Tests for Click portability validation (Stage 2)."""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
from pyrung.click.validation import (
    CLK_EXPR_ONLY_IN_MATH,
    CLK_FORLOOP_COUNT_BANK,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTACT_MUST_BE_X,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_MATH_ARG_NOT_PLAIN,
    CLK_MATH_BIT_RANGE_AGGREGATE,
    CLK_MATH_FLOOR_DIV,
    CLK_MATH_FUNC_MODE_MISMATCH,
    CLK_MATH_MEDIAN_RANGE_TOO_LARGE,
    CLK_MATH_MODE_MIXED,
    CLK_MATH_NESTING_DEPTH,
    CLK_MATH_RANGE_MIXED_BANKS,
    CLK_MATH_RANGE_NOT_CONTIGUOUS,
    CLK_MATH_SUM_BANK_NOT_AVAILABLE,
    CLK_MATH_WIDE_CONSTANT,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_RAND_BOUND_TYPE,
    CLK_RAND_EQUAL_BOUNDS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    CLK_TIMER_PRESET_OVERFLOW,
    ClickValidationReport,
    validate_click_program,
)
from pyrung.core import (
    Block,
    Bool,
    Int,
    Or,
    Tag,
    TagType,
    Timer,
    branch,
    forloop,
    immediate,
    randint,
    to_value,
)
from pyrung.core.expression import lro, lsh, rro, rsh
from pyrung.core.program import (
    Program,
    Rung,
    copy,
    math,
    on_delay,
    out,
    reset,
    rise,
    run_enabled_function,
    run_function,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_program(fn):
    """Build a Program from a function using strict=False for test flexibility."""
    prog = Program(strict=False)
    with prog:
        fn()
    return prog


def _finding_codes(report: ClickValidationReport) -> list[str]:
    """Collect all finding codes across all severity buckets."""
    codes = []
    for f in report.errors:
        codes.append(f.code)
    for f in report.warnings:
        codes.append(f.code)
    for f in report.hints:
        codes.append(f.code)
    return codes


# ---------------------------------------------------------------------------
# Test 1: Allowed case â€” copy(DD[Pointer], Dest) with DS pointer
# ---------------------------------------------------------------------------


class TestAllowedCopyWithDSPointer:
    def test_no_findings(self):
        Pointer = Tag("Pointer", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(ds[100]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        # The IndirectRef in copy source is allowed, and pointer is DS
        relevant = [
            c
            for c in _finding_codes(report)
            if c
            in {
                CLK_PTR_CONTEXT_ONLY_COPY,
                CLK_PTR_POINTER_MUST_BE_DS,
                CLK_PTR_EXPR_NOT_ALLOWED,
                CLK_EXPR_ONLY_IN_MATH,
                CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
                CLK_PTR_DS_UNVERIFIED,
            }
        ]
        assert relevant == []

    def test_named_ds_block_slot_needs_no_explicit_tag_map_entry(self):
        blocks = ClickBlocks()
        blocks.ds.slot(121, name="Pointer")

        def logic():
            with Rung():
                copy(blocks.dd[blocks.ds[121]], blocks.dd[1])

        prog = _build_program(logic)
        report = validate_click_program(prog, TagMap(include_system=False), mode="strict")
        codes = _finding_codes(report)

        assert CLK_PTR_DS_UNVERIFIED not in codes
        assert CLK_PTR_POINTER_MUST_BE_DS not in codes


# ---------------------------------------------------------------------------
# Test 2: Non-DS pointer
# ---------------------------------------------------------------------------


class TestNonDSPointer:
    def test_warn_mode_gives_hint(self):
        Pointer = Tag("Pointer", TagType.DINT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(dd[50]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_PTR_POINTER_MUST_BE_DS for f in report.hints)
        assert not report.errors

    def test_named_dd_block_slot_is_rejected_without_tag_map_entry(self):
        blocks = ClickBlocks()
        blocks.dd.slot(50, name="Pointer")

        def logic():
            with Rung():
                copy(blocks.dd[blocks.dd[50]], blocks.dd[1])

        prog = _build_program(logic)
        report = validate_click_program(prog, TagMap(include_system=False), mode="strict")
        codes = _finding_codes(report)

        assert CLK_PTR_POINTER_MUST_BE_DS in codes
        assert CLK_PTR_DS_UNVERIFIED not in codes

    def test_strict_mode_gives_error(self):
        Pointer = Tag("Pointer", TagType.DINT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(dd[50]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_PTR_POINTER_MUST_BE_DS for f in report.errors)
        assert not report.hints


# ---------------------------------------------------------------------------
# Test 3: Pointer in condition
# ---------------------------------------------------------------------------


class TestPointerInCondition:
    def test_pointer_in_condition_violation(self):
        Pointer = Tag("Pointer", TagType.INT)

        def logic():
            with Rung(dd[Pointer] > 5):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(ds[100])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_PTR_CONTEXT_ONLY_COPY for f in report.hints)


# ---------------------------------------------------------------------------
# Test 4: Pointer expression index (IndirectExprRef)
# ---------------------------------------------------------------------------


class TestPointerExpressionIndex:
    def test_expr_ref_violation(self):
        idx = Tag("idx", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[idx + 1], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [idx.map_to(ds[100]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_PTR_EXPR_NOT_ALLOWED for f in report.hints)


# ---------------------------------------------------------------------------
# Test 5: Inline expression in condition
# ---------------------------------------------------------------------------


class TestExpressionInCondition:
    def test_expr_in_condition_violation(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_EXPR_ONLY_IN_MATH for f in report.hints)


class TestIntTruthinessInCondition:
    def test_warn_mode_gives_hint(self):
        Step = Tag("Step", TagType.INT)

        def logic():
            with Rung(Step):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED for f in report.hints)
        assert not report.errors

    def test_strict_mode_gives_error(self):
        Step = Tag("Step", TagType.INT)

        def logic():
            with Rung(Step):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED for f in report.errors)
        assert not report.hints

    def test_dint_truthiness_gives_hint(self):
        Count = Tag("Count", TagType.DINT)

        def logic():
            with Rung(Count):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED for f in report.hints)
        assert not report.errors

    def test_explicit_compare_has_no_truthiness_finding(self):
        Step = Tag("Step", TagType.INT)

        def logic():
            with Rung(Step != 0):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_grouped_any_of_emits_truthiness_finding(self):
        Step = Tag("Step", TagType.INT)
        Start = Bool("Start")

        def logic():
            from pyrung.core import Or

            with Rung(Or(Step, Start)):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED for f in report.hints)


# ---------------------------------------------------------------------------
# Test 6: Inline expression in copy
# ---------------------------------------------------------------------------


class TestExpressionInCopy:
    def test_expr_in_copy_violation(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                copy(A * 2, Dest)

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_EXPR_ONLY_IN_MATH for f in report.hints)


# ---------------------------------------------------------------------------
# Test 7: Expression in math() â€” no violation
# ---------------------------------------------------------------------------


class TestExpressionInMath:
    def test_no_expr_violation(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(A * 2, Dest)

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        expr_findings = [
            f
            for f in (*report.errors, *report.warnings, *report.hints)
            if f.code == CLK_EXPR_ONLY_IN_MATH
        ]
        assert expr_findings == []


class TestCalcModeMixedValidation:
    def test_warn_mode_gives_hint_for_mixed_family_calc(self):
        a = Tag("A", TagType.INT)
        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a + h, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), h.map_to(dh[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_MODE_MIXED for f in report.hints)

    def test_strict_mode_gives_error_for_mixed_family_calc(self):
        a = Tag("A", TagType.INT)
        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a + h, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), h.map_to(dh[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_MATH_MODE_MIXED for f in report.errors)

    def test_decimal_family_calc_has_no_mixed_mode_finding(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.DINT)
        dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                math(a + b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(dd[1]), dest.map_to(dd[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_MODE_MIXED for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_hex_family_calc_has_no_mixed_mode_finding(self):
        h1 = Tag("H1", TagType.WORD)
        h2 = Tag("H2", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                math(h1 | h2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h1.map_to(dh[1]), h2.map_to(dh[2]), dest.map_to(dh[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_MODE_MIXED for f in (*report.errors, *report.warnings, *report.hints)
        )


class TestFloorDivisionPortability:
    def test_warn_mode_gives_hint_for_floor_div_in_calc(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a // b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_FLOOR_DIV for f in report.hints)

    def test_strict_mode_gives_error_for_floor_div_in_calc(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a // b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_MATH_FLOOR_DIV for f in report.errors)

    def test_regular_division_has_no_floor_div_finding(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a / b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_FLOOR_DIV for f in (*report.errors, *report.warnings, *report.hints)
        )


class TestCalcFuncModeMismatch:
    def test_lsh_in_decimal_mode_gives_finding(self):
        """LSH is hex-only; using it with INT tags (decimal) is a mismatch."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a << 3, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_FUNC_MODE_MISMATCH for f in report.hints)

    def test_sqrt_in_hex_mode_gives_finding(self):
        """SQRT is decimal-only; using it with WORD tags (hex) is a mismatch."""
        from pyrung.core.expression import sqrt

        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                math(sqrt(h), dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h.map_to(dh[1]), dest.map_to(dh[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_FUNC_MODE_MISMATCH for f in report.hints)

    def test_and_in_hex_mode_no_finding(self):
        """AND in hex mode is valid."""
        h1 = Tag("H1", TagType.WORD)
        h2 = Tag("H2", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                math(h1 & h2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h1.map_to(dh[1]), h2.map_to(dh[2]), dest.map_to(dh[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_FUNC_MODE_MISMATCH
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_power_in_decimal_mode_no_finding(self):
        """Power (^) in decimal mode is valid."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a**2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_FUNC_MODE_MISMATCH
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_strict_mode_gives_error(self):
        """Strict mode escalates mismatch to error."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a << 3, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_MATH_FUNC_MODE_MISMATCH for f in report.errors)


class TestCalcNestingDepth:
    def _deeply_nested_expr(self, tag, levels):
        """Build a left-nested binary expression tree with *levels* paren levels.

        Each additional binary-inside-binary adds one paren level in the Click
        formula:  (((...) + tag) + tag) + tag
        """
        expr = tag + tag  # depth 0 binary — no parens yet
        for _ in range(levels):
            # wrapping a binary child inside another binary adds one ( )
            expr = expr + tag
        return expr

    def test_depth_8_is_accepted(self):
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        # A binary inside a binary produces 1 level.  We need exactly 8.
        # depth 0: a + a  (no parens)
        # depth 1: (a + a) + a
        # ...
        # depth 7: 7 levels of nesting from 7 extra wraps
        # We actually want 8 levels.  Let me re-derive:
        # _deeply_nested_expr(tag, 0) → tag + tag → 0 levels (no child is binary)
        # _deeply_nested_expr(tag, 1) → (tag + tag) + tag → 1 level
        # _deeply_nested_expr(tag, N) → N levels
        # So levels=8 → exactly 8.
        expr_8 = self._deeply_nested_expr(a, 8)

        def logic():
            with Rung():
                math(expr_8, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_NESTING_DEPTH
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_depth_9_gives_finding(self):
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        expr = self._deeply_nested_expr(a, 9)

        def logic():
            with Rung():
                math(expr, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_NESTING_DEPTH for f in report.hints)

    def test_strict_mode_gives_error(self):
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        expr = self._deeply_nested_expr(a, 9)

        def logic():
            with Rung():
                math(expr, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_MATH_NESTING_DEPTH for f in report.errors)

    def test_function_calls_count_as_paren_level(self):
        """SQRT(…) adds one paren level; deeply nested args can exceed the limit."""
        from pyrung.core.expression import sqrt

        a = Tag("A", TagType.REAL)
        dest = Tag("Dest", TagType.REAL)

        # 8 levels of binary nesting + sqrt wrapping = 9 total
        inner = self._deeply_nested_expr(a, 8)
        expr = sqrt(inner)

        def logic():
            with Rung():
                math(expr, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(df[1]), dest.map_to(df[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_NESTING_DEPTH for f in report.hints)

    def test_flat_expression_no_finding(self):
        """Simple a + b has depth 0 — well within limits."""
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(a + b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_MATH_NESTING_DEPTH
            for f in (*report.errors, *report.warnings, *report.hints)
        )


def _all_findings(report: ClickValidationReport):
    return (*report.errors, *report.warnings, *report.hints)


class TestAbsMode:
    def test_abs_in_hex_mode_gives_finding(self):
        """ABS is decimal-only in CLICK; abs() is a UnaryExpr, not a MathFuncExpr."""
        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                math(abs(h), dest)

        prog = _build_program(logic)
        tag_map = TagMap([h.map_to(dh[1]), dest.map_to(dh[2])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(
            f.code == CLK_MATH_FUNC_MODE_MISMATCH and "ABS" in f.message for f in report.hints
        )

    def test_abs_in_decimal_mode_no_finding(self):
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(abs(a), dest)

        prog = _build_program(logic)
        tag_map = TagMap([a.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(f.code == CLK_MATH_FUNC_MODE_MISMATCH for f in _all_findings(report))


class TestRangeAggregateModes:
    def test_aggregates_allowed_in_hex_mode(self):
        """MAX/MIN/MDN/AVG are on CLICK's hex formula pad too."""
        H = Block("H", TagType.WORD, 1, 4)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                math(H.select(1, 4).max() + H.select(1, 4).median(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([H.map_to(dh.select(1, 4)), dest.map_to(dh[10])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(f.code.startswith("CLK_MATH") for f in _all_findings(report))

    def test_aggregate_counts_as_paren_level(self):
        """An aggregate's FUNC ( a : b ) adds one paren level, like SQRT(x)."""
        V = Block("V", TagType.INT, 1, 4)
        dest = Tag("Dest", TagType.INT)

        expr = V.select(1, 4).max()
        for _ in range(8):
            expr = (expr + 1) * 2

        def logic():
            with Rung():
                math(expr, dest)

        prog = _build_program(logic)
        tag_map = TagMap([V.map_to(ds.select(1, 4)), dest.map_to(ds[10])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_NESTING_DEPTH for f in report.hints)


class TestRangeAggregateRules:
    def test_contiguous_range_no_finding(self):
        V = Block("V", TagType.INT, 1, 4)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(V.select(1, 4).mean(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([V.map_to(ds.select(11, 14)), dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert report.errors == ()

    def test_gap_in_mapping_gives_finding(self):
        """OBJ-1014: the exported FUNC ( first : last ) would read unmapped registers."""
        V = Block("V", TagType.INT, 1, 3)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(V.select(1, 3).max(), dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            {V[1]: ds[1], V[2]: ds[2], V[3]: ds[5], dest: ds[10]}, include_system=False
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_RANGE_NOT_CONTIGUOUS for f in report.hints)

    def test_reversed_range_gives_finding(self):
        """OBJ-1014: a range must be ordered Low:High."""
        V = Block("V", TagType.INT, 1, 4)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(V.select(1, 4).reverse().sum(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([V.map_to(ds.select(1, 4)), dest.map_to(ds[10])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_MATH_RANGE_NOT_CONTIGUOUS for f in report.errors)

    def test_mixed_banks_gives_finding(self):
        """OBJ-1015: both ends of the range must be in the same address block."""
        V = Block("V", TagType.INT, 1, 2)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(V.select(1, 2).min(), dest)

        prog = _build_program(logic)
        tag_map = TagMap({V[1]: ds[1], V[2]: sd[1], dest: ds[10]}, include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        codes = [f.code for f in report.hints]
        assert CLK_MATH_RANGE_MIXED_BANKS in codes
        assert CLK_MATH_RANGE_NOT_CONTIGUOUS not in codes

    def test_median_range_over_1024_bytes_gives_finding(self):
        """OBJ-1013: MDN accepts at most 1024 bytes (512 DS, 256 DD/DF)."""
        dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                math(dd.select(1, 257).median(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(dd[1000])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_MEDIAN_RANGE_TOO_LARGE for f in report.hints)

    def test_median_range_at_1024_bytes_no_finding(self):
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(ds.select(1, 512).median(), dest)
                math(ds.select(1, 600).mean(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[1000])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(f.code == CLK_MATH_MEDIAN_RANGE_TOO_LARGE for f in _all_findings(report))

    def test_bool_range_sum_no_finding(self):
        """SUM over bits is legal; CLICK spells it plain SUM."""
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(c.select(1, 8).sum(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert report.errors == ()

    def test_bool_range_max_gives_finding(self):
        """Only SUM accepts a bit range in CLICK."""
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(c.select(1, 8).max(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_MATH_BIT_RANGE_AGGREGATE for f in report.hints)

    @staticmethod
    def _sum_findings(block, dest_type, dest_address):
        dest = Tag("Dest", dest_type)

        def logic():
            with Rung():
                math(block.select(1, 4).sum(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(dest_address)], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        return [f for f in _all_findings(report) if f.code == CLK_MATH_SUM_BANK_NOT_AVAILABLE]

    @pytest.mark.parametrize("block", [x, y, c, t, sc, ds, dd, df, td, ctd, sd])
    def test_sum_decimal_banks_no_finding(self, block):
        assert self._sum_findings(block, TagType.INT, ds[1000]) == []

    @pytest.mark.parametrize("block", [dh, xd, yd])
    def test_sum_hex_banks_no_finding(self, block):
        assert self._sum_findings(block, TagType.WORD, dh[100]) == []

    @pytest.mark.parametrize("block", [ct, txt])
    def test_sum_unavailable_banks_give_finding(self, block):
        findings = self._sum_findings(block, TagType.INT, ds[1000])
        assert len(findings) == 1
        assert "not available in CLICK" in findings[0].message

    def test_sum_mixed_families_reports_mode_mixed_only(self):
        """A hex range into a decimal dest is CLK_MATH_MODE_MIXED, not a SUM bank finding."""
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(dh.select(1, 4).sum(), dest)

        prog = _build_program(logic)
        tag_map = TagMap([dest.map_to(ds[1000])], include_system=False)
        report = validate_click_program(prog, tag_map, mode="warn")
        codes = {f.code for f in _all_findings(report)}
        assert CLK_MATH_MODE_MIXED in codes
        assert CLK_MATH_SUM_BANK_NOT_AVAILABLE not in codes


class TestTildeExpressionPortability:
    def test_warn_mode_gives_hint(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(~A, Dest)

        prog = _build_program(logic)
        tag_map = TagMap([Dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_TILDE_BOOL_CONTACT_ONLY for f in report.hints)

    def test_strict_mode_gives_error(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                math(~A, Dest)

        prog = _build_program(logic)
        tag_map = TagMap([Dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_TILDE_BOOL_CONTACT_ONLY for f in report.errors)

    def test_bool_contact_inversion_does_not_emit_tilde_expression_finding(self):
        Start = Bool("Start")

        def logic():
            with Rung(~Start):
                pass

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_TILDE_BOOL_CONTACT_ONLY
            for f in (*report.errors, *report.warnings, *report.hints)
        )


# ---------------------------------------------------------------------------
# Test 8: Unresolved pointer bank
# ---------------------------------------------------------------------------


class TestUnresolvedPointerBank:
    def test_warn_gives_hint(self):
        Pointer = Tag("UnknownPtr", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        # Pointer not in tag_map at all
        tag_map = TagMap(
            [Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_PTR_DS_UNVERIFIED for f in report.hints)

    def test_strict_gives_error(self):
        Pointer = Tag("UnknownPtr", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_PTR_DS_UNVERIFIED for f in report.errors)


# ---------------------------------------------------------------------------
# Test 9: IndirectBlockRange in block copy
# ---------------------------------------------------------------------------


class TestIndirectBlockRange:
    def test_indirect_block_range_violation(self):
        Start = Tag("Start", TagType.INT)
        End = Tag("End", TagType.INT)

        def logic():
            with Rung():
                from pyrung.core.program import blockcopy

                blockcopy(dd.select(Start, End), dd.select(100, 110))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED for f in report.hints)


# ---------------------------------------------------------------------------
# Test 10: ExprCompare in condition (R4 end-to-end)
# ---------------------------------------------------------------------------


class TestExprCompareInCondition:
    def test_both_sides_emit_expr_violation(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 100):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        expr_findings = [f for f in report.hints if f.code == CLK_EXPR_ONLY_IN_MATH]
        # Both condition.left (BinaryExpr) and condition.right (LiteralExpr) are expressions
        assert len(expr_findings) >= 2


# ---------------------------------------------------------------------------
# Test 11: Location formatting deterministic
# ---------------------------------------------------------------------------


class TestLocationFormatting:
    def test_main_rung_instruction_location(self):
        Pointer = Tag("Pointer", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                out(Bool("Filler"))
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        ptr_findings = [f for f in report.hints if f.code == CLK_PTR_DS_UNVERIFIED]
        assert len(ptr_findings) > 0
        loc = ptr_findings[0].location
        # Should contain deterministic location format
        assert loc.startswith("main.rung[0].")
        assert "instruction[1](CopyInstruction)" in loc
        assert "instruction.source" in loc

    def test_location_format_consistency(self):
        """Same program always produces same location strings."""
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        r1 = validate_click_program(prog, tag_map, mode="warn")
        r2 = validate_click_program(prog, tag_map, mode="warn")

        locs1 = [f.location for f in r1.hints]
        locs2 = [f.location for f in r2.hints]
        assert locs1 == locs2


# ---------------------------------------------------------------------------
# Test 12: TagMap.validate() delegation
# ---------------------------------------------------------------------------


class TestTagMapValidate:
    def test_delegates_to_validate_click_program(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report_direct = validate_click_program(prog, tag_map, mode="warn")
        report_method = tag_map.validate(prog, mode="warn")

        assert _finding_codes(report_direct) == _finding_codes(report_method)

    def test_program_validate_click_matches_direct(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report_direct = validate_click_program(prog, tag_map, mode="warn")
        report_facade = prog.validate("click", tag_map=tag_map, mode="warn")

        assert _finding_codes(report_direct) == _finding_codes(report_facade)

    def test_strict_mode_via_tagmap(self):
        A = Tag("A", TagType.INT)

        def logic():
            with Rung():
                copy(A * 2, Tag("Dest", TagType.INT))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = tag_map.validate(prog, mode="strict")
        assert any(f.code == CLK_EXPR_ONLY_IN_MATH for f in report.errors)


# ---------------------------------------------------------------------------
# Test: Summary format
# ---------------------------------------------------------------------------


class TestReportSummary:
    def test_no_findings_summary(self):
        report = ClickValidationReport()
        assert report.summary() == "No findings."

    def test_mixed_summary(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        # Should have hints (no errors/warnings)
        assert "hint(s)" in report.summary()

        report_strict = validate_click_program(prog, tag_map, mode="strict")
        assert "error(s)" in report_strict.summary()


class TestFunctionCallPortability:
    def test_run_function_warn_mode_finding(self):
        Src = Tag("Src", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def fn(src):
            return {"dest": src}

        def logic():
            with Rung():
                run_function(fn, ins={"src": Src}, outs={"dest": Dest})

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_FUNCTION_CALL_NOT_PORTABLE for f in report.hints)

    def test_run_enabled_function_strict_mode_error(self):
        Src = Tag("Src", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def fn(enabled, src):
            _ = enabled
            return {"dest": src}

        def logic():
            with Rung():
                run_enabled_function(fn, ins={"src": Src}, outs={"dest": Dest})

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_FUNCTION_CALL_NOT_PORTABLE for f in report.errors)


# ---------------------------------------------------------------------------
# Context-aware suggestion content tests
# ---------------------------------------------------------------------------


class TestSuggestionContent:
    """Suggestions contain context-specific content (block names, expressions, pointer names)."""

    def test_ptr_context_only_copy_mentions_block_and_pointer(self):
        Pointer = Tag("Pointer", TagType.INT)

        def logic():
            with Rung(dd[Pointer] > 5):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(ds[100])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        r1_findings = [f for f in report.hints if f.code == CLK_PTR_CONTEXT_ONLY_COPY]
        assert r1_findings
        suggestion = r1_findings[0].suggestion
        assert suggestion is not None
        assert "DD" in suggestion
        assert "Pointer" in suggestion

    def test_ptr_pointer_must_be_ds_mentions_pointer_name_and_type(self):
        Pointer = Tag("Pointer", TagType.DINT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Pointer.map_to(dd[50]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        r2_findings = [f for f in report.hints if f.code == CLK_PTR_POINTER_MUST_BE_DS]
        assert r2_findings
        suggestion = r2_findings[0].suggestion
        assert suggestion is not None
        assert "Pointer" in suggestion
        assert "DD" in suggestion

    def test_ptr_ds_unverified_mentions_pointer_name(self):
        Pointer = Tag("UnknownPtr", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        r2b_findings = [f for f in report.hints if f.code == CLK_PTR_DS_UNVERIFIED]
        assert r2b_findings
        suggestion = r2b_findings[0].suggestion
        assert suggestion is not None
        assert "UnknownPtr" in suggestion

    def test_ptr_expr_not_allowed_mentions_block_and_expr_dsl(self):
        idx = Tag("idx", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[idx + 1], Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [idx.map_to(ds[100]), Dest.map_to(dd[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        r3_findings = [f for f in report.hints if f.code == CLK_PTR_EXPR_NOT_ALLOWED]
        assert r3_findings
        suggestion = r3_findings[0].suggestion
        assert suggestion is not None
        assert "DD" in suggestion
        assert "idx" in suggestion
        assert "copy(dd[Ptr], dest)" in suggestion

    def test_expr_only_in_math_mentions_expression_dsl(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        r4_findings = [f for f in report.hints if f.code == CLK_EXPR_ONLY_IN_MATH]
        assert r4_findings
        # At least one finding should mention the expression content
        suggestions = [f.suggestion for f in r4_findings if f.suggestion is not None]
        assert any("A" in s or "B" in s or "+" in s for s in suggestions)

    def test_indirect_block_range_mentions_block_name(self):
        Start = Tag("Start", TagType.INT)
        End = Tag("End", TagType.INT)

        def logic():
            with Rung():
                from pyrung.core.program import blockcopy

                blockcopy(dd.select(Start, End), dd.select(100, 110))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        r5_findings = [f for f in report.hints if f.code == CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED]
        assert r5_findings
        suggestion = r5_findings[0].suggestion
        assert suggestion is not None
        assert "DD" in suggestion


class TestWrappedCopySources:
    def test_wrapped_indirect_ref_stays_in_copy_context(self):
        Pointer = Tag("Pointer", TagType.INT)
        Dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                copy(dd[Pointer], Dest, convert=to_value)

        prog = _build_program(logic)
        tag_map = TagMap([Pointer.map_to(ds[100]), Dest.map_to(dd[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        codes = _finding_codes(report)
        assert CLK_PTR_CONTEXT_ONLY_COPY not in codes


class TestImmediateValidation:
    def test_out_immediate_tag_has_no_immediate_findings(self):
        Start = Bool("Start")
        Coil = Bool("Coil")

        def logic():
            with Rung(Start):
                out(immediate(Coil))

        prog = _build_program(logic)
        tag_map = TagMap([Start.map_to(x[1]), Coil.map_to(y[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        codes = _finding_codes(report)
        assert CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED not in codes
        assert CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y not in codes

    def test_out_immediate_contiguous_range_has_no_immediate_findings(self):
        Start = Bool("Start")
        Coils = Block("Coils", TagType.BOOL, 1, 4)

        def logic():
            with Rung(Start):
                out(immediate(Coils.select(1, 4)))

        prog = _build_program(logic)
        tag_map = TagMap(
            [Start.map_to(x[1]), Coils.map_to(y.select(1, 4))],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        codes = _finding_codes(report)
        assert CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS not in codes
        assert CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y not in codes

    def test_immediate_non_contiguous_range_is_error(self):
        Start = Bool("Start")
        Coils = Block("Coils", TagType.BOOL, 1, 4)

        def logic():
            with Rung(Start):
                out(immediate(Coils.select(1, 4)))

        prog = _build_program(logic)
        tag_map = TagMap(
            [
                Start.map_to(x[1]),
                Coils[1].map_to(y[1]),
                Coils[2].map_to(y[3]),
                Coils[3].map_to(y[4]),
                Coils[4].map_to(y[6]),
            ],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS for f in report.errors)

    def test_immediate_in_copy_is_error(self):
        Start = Bool("Start")
        Source = Bool("Source")
        Dest = Bool("Dest")

        def logic():
            with Rung(Start):
                copy(immediate(Source), Dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Start.map_to(x[1]), Source.map_to(x[2]), Dest.map_to(c[1])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED for f in report.errors)

    def test_immediate_edge_contact_is_error(self):
        Start = Bool("Start")
        Coil = Bool("Coil")

        def logic():
            with Rung(rise(immediate(Start))):
                out(Coil)

        prog = _build_program(logic)
        tag_map = TagMap([Start.map_to(x[1]), Coil.map_to(y[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED for f in report.errors)

    def test_immediate_coil_target_outside_y_is_error(self):
        Start = Bool("Start")
        Coil = Bool("Coil")

        def logic():
            with Rung(Start):
                reset(immediate(Coil))

        prog = _build_program(logic)
        tag_map = TagMap([Start.map_to(x[1]), Coil.map_to(c[2])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y for f in report.errors)

    @pytest.mark.parametrize("bank", ["c", "y"])
    @pytest.mark.parametrize("contact", ["no", "nc"])
    def test_immediate_contact_outside_x_is_error(self, bank, contact):
        """CLICK's Immediate checkbox on a contact is for X bit addresses only."""
        Sensor = Bool("Sensor")
        Coil = Bool("Coil")

        def logic():
            with Rung(immediate(Sensor) if contact == "no" else ~immediate(Sensor)):
                out(Coil)

        prog = _build_program(logic)
        slot = {"c": c[1], "y": y[1]}[bank]
        tag_map = TagMap([Sensor.map_to(slot), Coil.map_to(y[2])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        found = [f for f in report.errors if f.code == CLK_IMMEDIATE_CONTACT_MUST_BE_X]
        assert len(found) == 1
        assert "an immediate contact must read an input mapped to X" in found[0].message

    def test_immediate_contact_in_a_branch_and_a_group_outside_x_is_error(self):
        Start = Bool("Start")
        Sensor = Bool("Sensor")
        Other = Bool("Other")
        Coil = Bool("Coil")
        Lamp = Bool("Lamp")

        def logic():
            with Rung(Start):
                out(Coil)
                with branch(Or(Other, immediate(Sensor))):
                    out(Lamp)

        prog = _build_program(logic)
        tag_map = TagMap(
            [
                Start.map_to(x[1]),
                Sensor.map_to(c[1]),
                Other.map_to(x[2]),
                Coil.map_to(y[1]),
                Lamp.map_to(y[2]),
            ],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        found = [f for f in report.errors if f.code == CLK_IMMEDIATE_CONTACT_MUST_BE_X]
        assert [f.location for f in found] == ["main.rung[0].branch[0].condition"]

    def test_immediate_contact_in_a_nested_loop_body_is_reported_once(self):
        Start = Bool("Start")
        Sensor = Bool("Sensor")
        Coil = Bool("Coil")

        def logic():
            with Rung(Start):
                with forloop(2):
                    with Rung():
                        with forloop(2):
                            with Rung(immediate(Sensor)):
                                out(Coil)

        prog = _build_program(logic)
        tag_map = TagMap(
            [Start.map_to(x[1]), Sensor.map_to(c[1]), Coil.map_to(y[1])], include_system=False
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        found = [f for f in report.errors if f.code == CLK_IMMEDIATE_CONTACT_MUST_BE_X]
        assert len(found) == 1

    def test_immediate_contact_on_x_has_no_contact_finding(self):
        Sensor = Bool("Sensor")
        Coil = Bool("Coil")

        def logic():
            with Rung(immediate(Sensor), ~immediate(Sensor)):
                out(Coil)

        prog = _build_program(logic)
        tag_map = TagMap([Sensor.map_to(x[1]), Coil.map_to(y[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="strict")
        assert CLK_IMMEDIATE_CONTACT_MUST_BE_X not in _finding_codes(report)

    def test_an_immediate_range_is_not_a_contact(self):
        """The DSL refuses an immediate range as a contact, so no bank check is needed."""
        Sensors = Block("Sensors", TagType.BOOL, 1, 2)

        def logic():
            with Rung(immediate(Sensors.select(1, 2))):
                out(Bool("Coil"))

        with pytest.raises(TypeError, match="Immediate contact requires a Tag"):
            _build_program(logic)


# ---------------------------------------------------------------------------
# Timer preset overflow
# ---------------------------------------------------------------------------


class TestTimerPresetOverflow:
    def test_preset_within_range_no_finding(self):
        def logic():
            with Rung():
                on_delay(Timer[1], preset=32767, unit="Tms")

        prog = _build_program(logic)
        tag_map = TagMap(
            [Timer[1].Done.map_to(c[1]), Timer[1].Acc.map_to(ds[1])],
            include_system=False,
        )
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_TIMER_PRESET_OVERFLOW not in _finding_codes(report)

    def test_preset_exceeds_int_range(self):
        def logic():
            with Rung():
                on_delay(Timer[1], preset=60000, unit="Tms")

        prog = _build_program(logic)
        tag_map = TagMap(
            [Timer[1].Done.map_to(c[1]), Timer[1].Acc.map_to(ds[1])],
            include_system=False,
        )
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_TIMER_PRESET_OVERFLOW in _finding_codes(report)

    def test_preset_tag_skipped(self):
        """Dynamic preset via Tag should not trigger overflow check."""
        Preset = Int("Preset")

        def logic():
            with Rung():
                on_delay(Timer[1], preset=Preset, unit="Tms")

        prog = _build_program(logic)
        tag_map = TagMap(
            [Timer[1].Done.map_to(c[1]), Timer[1].Acc.map_to(ds[1]), Preset.map_to(ds[2])],
            include_system=False,
        )
        report = validate_click_program(prog, tag_map, mode="warn")
        assert CLK_TIMER_PRESET_OVERFLOW not in _finding_codes(report)


class TestForLoopCountAndRandBounds:
    """What CLICK's editor refuses: a For count outside the allowed banks, RAND bounds."""

    @staticmethod
    def _for_count(count, mappings, mode="warn"):
        def logic():
            with Rung():
                with forloop(count):
                    out(y[1])

        return validate_click_program(
            _build_program(logic), TagMap(mappings, include_system=False), mode=mode
        )

    def test_real_count_is_refused(self):
        n = Tag("N", TagType.REAL)
        report = self._for_count(n, [n.map_to(df[1])], mode="strict")
        found = [f for f in report.errors if f.code == CLK_FORLOOP_COUNT_BANK]
        assert len(found) == 1
        assert "DF" in found[0].message
        assert found[0].message.isascii()
        assert (found[0].suggestion or "").isascii()

    def test_real_count_is_a_hint_in_warn_mode(self):
        n = Tag("N", TagType.REAL)
        report = self._for_count(n, [n.map_to(df[1])])
        assert [f.code for f in report.hints] == [CLK_FORLOOP_COUNT_BANK]

    def test_allowed_count_banks_pass(self):
        cases = [
            (TagType.INT, ds[1]),
            (TagType.DINT, dd[1]),
            (TagType.INT, td[1]),
            (TagType.DINT, ctd[1]),
            (TagType.INT, sd[1]),
        ]
        for tag_type, address in cases:
            n = Tag("N", tag_type)
            report = self._for_count(n, [n.map_to(address)])
            assert CLK_FORLOOP_COUNT_BANK not in _finding_codes(report), address

    def test_constant_count_passes(self):
        report = self._for_count(5, [])
        assert CLK_FORLOOP_COUNT_BANK not in _finding_codes(report)

    def test_other_bank_count_is_refused(self):
        n = Tag("N", TagType.WORD)
        report = self._for_count(n, [n.map_to(dh[1])])
        assert CLK_FORLOOP_COUNT_BANK in _finding_codes(report)

    @staticmethod
    def _rand(lo, hi, mappings):
        dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                math(randint(lo, hi), dest)

        return validate_click_program(
            _build_program(logic),
            TagMap([dest.map_to(dd[100]), *mappings], include_system=False),
            mode="strict",
        )

    def test_real_bound_is_refused(self):
        lo = Tag("Lo", TagType.REAL)
        report = self._rand(lo, 9, [lo.map_to(df[1])])
        found = [f for f in report.errors if f.code == CLK_RAND_BOUND_TYPE]
        assert found
        assert found[0].message.isascii()

    def test_real_high_bound_is_refused(self):
        hi = Tag("Hi", TagType.REAL)
        report = self._rand(1, hi, [hi.map_to(df[1])])
        assert CLK_RAND_BOUND_TYPE in _finding_codes(report)

    @pytest.mark.parametrize("lo, hi", [(1.5, 3.5), (1, 3.0)])
    def test_float_constant_bound_is_refused(self, lo, hi):
        report = self._rand(lo, hi, [])
        assert CLK_RAND_BOUND_TYPE in _finding_codes(report)

    @pytest.mark.parametrize(
        "kind, block",
        [(TagType.BOOL, c), (TagType.WORD, dh), (TagType.CHAR, txt)],
        ids=["bit", "word", "text"],
    )
    def test_other_tag_types_are_refused(self, kind, block):
        lo = Tag("Lo", kind)
        report = self._rand(lo, 9, [lo.map_to(block[1])])
        assert CLK_RAND_BOUND_TYPE in _finding_codes(report)

    def test_bool_constant_bound_is_refused(self):
        assert CLK_RAND_BOUND_TYPE in _finding_codes(self._rand(True, 9, []))

    def test_equal_constant_bounds_are_refused(self):
        report = self._rand(7, 7, [])
        assert [f.code for f in report.errors] == [CLK_RAND_EQUAL_BOUNDS]

    def test_integer_bounds_pass(self):
        lo = Tag("Lo", TagType.INT)
        wide = Tag("Wide", TagType.DINT)
        for args, mappings in (
            ((7, 9), []),
            ((70000, 70002), []),
            ((6, 1), []),
            ((lo, 9), [lo.map_to(ds[1])]),
            ((lo, lo), [lo.map_to(ds[1])]),
            ((wide, 9), [wide.map_to(dd[1])]),
        ):
            codes = _finding_codes(self._rand(*args, mappings))
            assert CLK_RAND_BOUND_TYPE not in codes
            assert CLK_RAND_EQUAL_BOUNDS not in codes


class TestCalcPlainArguments:
    """CLICK's editor takes only a tag or a constant as an argument of RAND, LSH, RSH,
    LRO and RRO: it refused ``RAND ( DD615 / 2 , 10 )``."""

    @staticmethod
    def _codes(build):
        n = Tag("N", TagType.DINT)
        w = Tag("W", TagType.WORD)
        dest = Tag("Dest", TagType.DINT)
        hdest = Tag("HDest", TagType.WORD)
        expr, target = build(n, w)
        out_tag = dest if target == "dd" else hdest

        def logic():
            with Rung():
                math(expr, out_tag)

        report = validate_click_program(
            _build_program(logic),
            TagMap(
                [n.map_to(dd[1]), w.map_to(dh[1]), dest.map_to(dd[100]), hdest.map_to(dh[100])],
                include_system=False,
            ),
            mode="strict",
        )
        return report, [f for f in report.errors if f.code == CLK_MATH_ARG_NOT_PLAIN]

    @pytest.mark.parametrize(
        "build",
        [
            lambda n, w: (randint(n / 2, 10), "dd"),
            lambda n, w: (randint(1, n + 1), "dd"),
            lambda n, w: (randint(-n, 10), "dd"),
            lambda n, w: (lsh(w + 1, 2), "dh"),
            lambda n, w: (rsh(w, w & 3), "dh"),
            lambda n, w: (lro(w | 1, 1), "dh"),
            lambda n, w: (rro(w, w ^ 1), "dh"),
            lambda n, w: ((w & 0xFF) << 2, "dh"),
            lambda n, w: (w >> (w & 3), "dh"),
        ],
        ids=["rand-div", "rand-add", "rand-neg", "lsh", "rsh", "lro", "rro", "shl-op", "shr-op"],
    )
    def test_expression_argument_is_refused(self, build):
        _, found = self._codes(build)
        assert len(found) == 1
        assert found[0].message.isascii()

    @pytest.mark.parametrize(
        "build",
        [
            lambda n, w: (randint(n, 10), "dd"),
            lambda n, w: (randint(1, 20) + n / 2, "dd"),
            lambda n, w: (lsh(w, 2), "dh"),
            lambda n, w: (rro(w, w), "dh"),
            lambda n, w: ((w << 2) & 0xFF, "dh"),
        ],
        ids=["rand-tag", "rand-in-expr", "lsh-tag", "rro-tags", "shl-in-expr"],
    )
    def test_plain_arguments_pass(self, build):
        _, found = self._codes(build)
        assert found == []

    def test_each_site_is_reported(self):
        _, found = self._codes(lambda n, w: (randint(n / 2, 10) + randint(1, n * 2), "dd"))
        assert len(found) == 2


class TestCalcWideConstant:
    """A CLICK Math cuts each constant to 16 bits whatever the destination: ``DD1 * 40000``
    multiplies by -25536 (hardware, 2026-11-05).  A RAND's constant bounds are cut only
    into an Int or a Word."""

    @staticmethod
    def _found(build, dest_type=TagType.DINT):
        n = Tag("N", TagType.DINT)
        w = Tag("W", TagType.WORD)
        dest = Tag("Dest", dest_type)
        block = {TagType.DINT: dd, TagType.INT: ds, TagType.REAL: df, TagType.WORD: dh}[dest_type]

        def logic():
            with Rung():
                math(build(n, w), dest)

        report = validate_click_program(
            _build_program(logic),
            TagMap(
                [n.map_to(dd[1]), w.map_to(dh[1]), dest.map_to(block[100])],
                include_system=False,
            ),
            mode="strict",
        )
        return [f for f in report.errors if f.code == CLK_MATH_WIDE_CONSTANT]

    @pytest.mark.parametrize(
        "build, dest_type",
        [
            (lambda n, w: n * 40000, TagType.DINT),
            (lambda n, w: n / 65536, TagType.REAL),
            (lambda n, w: n - 32768, TagType.INT),
            (lambda n, w: 70000, TagType.DINT),
            (lambda n, w: randint(70000, 70002), TagType.INT),
            (lambda n, w: w + 70000, TagType.WORD),
            (lambda n, w: randint(1, 70000), TagType.WORD),
            (lambda n, w: w + -1, TagType.WORD),
        ],
        ids=["dd", "df", "ds", "bare", "rand-into-int", "hex", "rand-into-word", "hex-negative"],
    )
    def test_a_constant_past_16_bits_is_reported(self, build, dest_type):
        found = self._found(build, dest_type)
        assert len(found) == 1
        assert found[0].message.isascii()
        assert "past 16 bits" in found[0].message

    @pytest.mark.parametrize(
        "build, dest_type",
        [
            (lambda n, w: n * 32767 + -32768, TagType.DINT),
            (lambda n, w: n * 1.0e6, TagType.REAL),
            (lambda n, w: w + 0xFFFF, TagType.WORD),
            (lambda n, w: randint(70000, 70002), TagType.DINT),
            (lambda n, w: randint(70000, 70002) * 2, TagType.REAL),
        ],
        ids=["edges", "float", "hex-edge", "rand-into-dint", "rand-into-real"],
    )
    def test_a_constant_in_16_bits_passes(self, build, dest_type):
        assert self._found(build, dest_type) == []

    def test_one_finding_names_each_constant(self):
        found = self._found(lambda n, w: n * 40000 + 40000 - 70000)
        assert len(found) == 1
        assert "(40000, 70000)" in found[0].message

    def test_a_hex_math_reads_a_constant_unsigned(self):
        (found,) = self._found(lambda n, w: w + -1 + 70000, TagType.WORD)
        assert "(-1, 70000)" in found.message
        assert "70000 reads 4464 and -1 reads 65535" in found.message
        assert "0..65535" in found.suggestion

    def test_a_rand_site_number_is_not_a_constant(self):
        from pyrung.core.expression import LiteralExpr, RandIntExpr

        draw = RandIntExpr(LiteralExpr(1), LiteralExpr(6), site=40000)
        assert self._found(lambda n, w: draw + n) == []


def test_export_refuses_a_real_rand_bound_unless_allowed():
    from pyrung.click import pyrung_to_ladder
    from pyrung.click.ladder import LadderExportError

    lo = Tag("Lo", TagType.REAL)
    dest = Tag("Dest", TagType.DINT)

    def logic():
        with Rung():
            math(randint(lo, 9), dest)

    prog = _build_program(logic)
    tag_map = TagMap([lo.map_to(df[1]), dest.map_to(dd[100])], include_system=False)
    try:
        pyrung_to_ladder(prog, tag_map)
    except LadderExportError as exc:
        assert CLK_RAND_BOUND_TYPE in str(exc)
    else:
        raise AssertionError("export should refuse a Real RAND bound")
    assert pyrung_to_ladder(prog, tag_map, allow=(CLK_RAND_BOUND_TYPE,)).main_rows


class TestPointerBlockOffset:
    def _setup(self, mode="warn"):
        from pyrung.core import Block

        blk = Block("Blk", TagType.INT, 1, 10)
        idx = Tag("idx", TagType.INT)
        dest = Tag("dest", TagType.INT)

        def logic():
            with Rung():
                copy(blk[idx], dest)

        tag_map = TagMap(
            [idx.map_to(ds[1]), dest.map_to(ds[2]), blk.map_to(ds.select(100, 109))],
            include_system=False,
        )
        return validate_click_program(_build_program(logic), tag_map, mode=mode)

    def test_offset_block_pointer_is_flagged_with_bank_advice(self):
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        report = self._setup(mode="strict")
        found = [f for f in report.errors if f.code == CLK_PTR_BLOCK_OFFSET]
        assert found
        assert found[0].suggestion is not None
        assert "math(idx + 99, Ptr); copy(ds[Ptr], dest)" in found[0].suggestion

    def test_offset_block_pointer_is_a_hint_in_warn_mode(self):
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        report = self._setup()
        assert [f.code for f in report.hints if f.code == CLK_PTR_BLOCK_OFFSET]

    def test_prebuilt_bank_pointer_is_not_flagged(self):
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        idx = Tag("idx", TagType.INT)
        dest = Tag("dest", TagType.INT)

        def logic():
            with Rung():
                copy(ds[idx], dest)

        tag_map = TagMap([idx.map_to(ds[1]), dest.map_to(ds[2])], include_system=False)
        report = validate_click_program(_build_program(logic), tag_map, mode="strict")
        assert CLK_PTR_BLOCK_OFFSET not in _finding_codes(report)

    def test_translator_refuses_offset_pointer(self):
        from pyrung.click import pyrung_to_ladder
        from pyrung.click.ladder import LadderExportError
        from pyrung.core import Block

        blk = Block("Blk", TagType.INT, 1, 10)
        idx = Tag("idx", TagType.INT)
        dest = Tag("dest", TagType.INT)
        with Program(strict=False) as logic:
            with Rung():
                copy(blk[idx], dest)
        tag_map = TagMap(
            [idx.map_to(ds[1]), dest.map_to(ds[2]), blk.map_to(ds.select(100, 109))],
            include_system=False,
        )
        with pytest.raises(LadderExportError, match="CLK_PTR_BLOCK_OFFSET"):
            pyrung_to_ladder(logic, tag_map)


class TestPointerBlockOffsetCases:
    def _mapped(self, start=100):
        from pyrung.core import Block

        blk = Block("Blk", TagType.INT, 1, 10)
        idx = Tag("idx", TagType.INT)
        other = Tag("other", TagType.INT)
        tag_map = TagMap(
            [idx.map_to(ds[1]), other.map_to(ds[2]), blk.map_to(ds.select(start, start + 9))],
            include_system=False,
        )
        return blk, idx, other, tag_map

    def _suggestion(self, prog, tag_map):
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        report = validate_click_program(prog, tag_map, mode="strict")
        found = [f for f in report.errors if f.code == CLK_PTR_BLOCK_OFFSET]
        assert found
        return found[0].suggestion

    def test_negative_offset(self):
        from pyrung.core import Block

        _blk, idx, other, _map = self._mapped()
        blk = Block("Blk", TagType.INT, 25, 34)
        tag_map = TagMap(
            [idx.map_to(ds[1]), other.map_to(ds[2]), blk.map_to(ds.select(21, 30))],
            include_system=False,
        )

        def logic():
            with Rung():
                copy(blk[idx], other)

        assert "math(idx - 4, Ptr); copy(ds[Ptr], dest)" in self._suggestion(
            _build_program(logic), tag_map
        )

    def test_destination_pointer_advice_writes(self):
        blk, idx, other, tag_map = self._mapped()

        def logic():
            with Rung():
                copy(other, blk[idx])

        assert "copy(src, ds[Ptr])" in self._suggestion(_build_program(logic), tag_map)

    def test_translator_raises_with_code_allowed(self):
        from pyrung.click import LadderExportError, pyrung_to_ladder
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        blk, idx, other, tag_map = self._mapped()
        with Program(strict=False) as logic:
            with Rung():
                copy(other, blk[idx])
        with pytest.raises(LadderExportError, match="CLK_PTR_BLOCK_OFFSET") as exc:
            pyrung_to_ladder(logic, tag_map, allow=[CLK_PTR_BLOCK_OFFSET])
        text = str(exc.value)
        assert "math(idx + 99, Ptr)" in text
        assert "copy(src, ds[Ptr])" in text

    def test_following_the_suggestion_exports_a_bare_pointer(self):
        from pyrung.click import pyrung_to_ladder
        from pyrung.click.validation import CLK_PTR_BLOCK_OFFSET

        blk, idx, other, tag_map = self._mapped()
        ptr = Tag("Ptr", TagType.INT)
        tag_map = TagMap(
            [
                idx.map_to(ds[1]),
                other.map_to(ds[2]),
                ptr.map_to(ds[3]),
                blk.map_to(ds.select(100, 109)),
            ],
            include_system=False,
        )
        with Program(strict=False) as logic:
            with Rung():
                math(idx + 99, ptr)
                copy(ds[ptr], other)
        report = validate_click_program(logic, tag_map, mode="strict")
        assert CLK_PTR_BLOCK_OFFSET not in _finding_codes(report)
        tokens = [row[-1] for row in pyrung_to_ladder(logic, tag_map).main_rows[1:]]
        assert "copy(DS[DS3],DS2)" in tokens
