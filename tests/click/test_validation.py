"""Tests for Click portability validation (Stage 2)."""

from __future__ import annotations

from pyrung.click import TagMap, c, dd, dh, ds, x, y
from pyrung.click.validation import (
    CLK_CALC_FLOOR_DIV,
    CLK_CALC_FUNC_MODE_MISMATCH,
    CLK_CALC_MODE_MIXED,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    ClickValidationReport,
    validate_click_program,
)
from pyrung.core import Block, Bool, Tag, TagType, immediate, to_value
from pyrung.core.program import (
    Program,
    Rung,
    calc,
    copy,
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
                CLK_EXPR_ONLY_IN_CALC,
                CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
                CLK_PTR_DS_UNVERIFIED,
            }
        ]
        assert relevant == []


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
        assert any(f.code == CLK_EXPR_ONLY_IN_CALC for f in report.hints)


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
            from pyrung.core import any_of

            with Rung(any_of(Step, Start)):
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
        assert any(f.code == CLK_EXPR_ONLY_IN_CALC for f in report.hints)


# ---------------------------------------------------------------------------
# Test 7: Expression in calc() â€” no violation
# ---------------------------------------------------------------------------


class TestExpressionInMath:
    def test_no_expr_violation(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(A * 2, Dest)

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        expr_findings = [
            f
            for f in (*report.errors, *report.warnings, *report.hints)
            if f.code == CLK_EXPR_ONLY_IN_CALC
        ]
        assert expr_findings == []


class TestCalcModeMixedValidation:
    def test_warn_mode_gives_hint_for_mixed_family_calc(self):
        a = Tag("A", TagType.INT)
        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a + h, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), h.map_to(dh[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_CALC_MODE_MIXED for f in report.hints)

    def test_strict_mode_gives_error_for_mixed_family_calc(self):
        a = Tag("A", TagType.INT)
        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a + h, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), h.map_to(dh[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_CALC_MODE_MIXED for f in report.errors)

    def test_decimal_family_calc_has_no_mixed_mode_finding(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.DINT)
        dest = Tag("Dest", TagType.DINT)

        def logic():
            with Rung():
                calc(a + b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(dd[1]), dest.map_to(dd[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_CALC_MODE_MIXED for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_hex_family_calc_has_no_mixed_mode_finding(self):
        h1 = Tag("H1", TagType.WORD)
        h2 = Tag("H2", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                calc(h1 | h2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h1.map_to(dh[1]), h2.map_to(dh[2]), dest.map_to(dh[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_CALC_MODE_MIXED for f in (*report.errors, *report.warnings, *report.hints)
        )


class TestFloorDivisionPortability:
    def test_warn_mode_gives_hint_for_floor_div_in_calc(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a // b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_CALC_FLOOR_DIV for f in report.hints)

    def test_strict_mode_gives_error_for_floor_div_in_calc(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a // b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_CALC_FLOOR_DIV for f in report.errors)

    def test_regular_division_has_no_floor_div_finding(self):
        a = Tag("A", TagType.INT)
        b = Tag("B", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a / b, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), b.map_to(ds[2]), dest.map_to(ds[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_CALC_FLOOR_DIV for f in (*report.errors, *report.warnings, *report.hints)
        )


class TestCalcFuncModeMismatch:
    def test_lsh_in_decimal_mode_gives_finding(self):
        """LSH is hex-only; using it with INT tags (decimal) is a mismatch."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a << 3, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_CALC_FUNC_MODE_MISMATCH for f in report.hints)

    def test_sqrt_in_hex_mode_gives_finding(self):
        """SQRT is decimal-only; using it with WORD tags (hex) is a mismatch."""
        from pyrung.core.expression import sqrt

        h = Tag("H", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                calc(sqrt(h), dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h.map_to(dh[1]), dest.map_to(dh[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_CALC_FUNC_MODE_MISMATCH for f in report.hints)

    def test_and_in_hex_mode_no_finding(self):
        """AND in hex mode is valid."""
        h1 = Tag("H1", TagType.WORD)
        h2 = Tag("H2", TagType.WORD)
        dest = Tag("Dest", TagType.WORD)

        def logic():
            with Rung():
                calc(h1 & h2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [h1.map_to(dh[1]), h2.map_to(dh[2]), dest.map_to(dh[3])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_CALC_FUNC_MODE_MISMATCH
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_power_in_decimal_mode_no_finding(self):
        """Power (^) in decimal mode is valid."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a**2, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="warn")
        assert not any(
            f.code == CLK_CALC_FUNC_MODE_MISMATCH
            for f in (*report.errors, *report.warnings, *report.hints)
        )

    def test_strict_mode_gives_error(self):
        """Strict mode escalates mismatch to error."""
        a = Tag("A", TagType.INT)
        dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(a << 3, dest)

        prog = _build_program(logic)
        tag_map = TagMap(
            [a.map_to(ds[1]), dest.map_to(ds[2])],
            include_system=False,
        )

        report = validate_click_program(prog, tag_map, mode="strict")
        assert any(f.code == CLK_CALC_FUNC_MODE_MISMATCH for f in report.errors)


class TestTildeExpressionPortability:
    def test_warn_mode_gives_hint(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(~A, Dest)

        prog = _build_program(logic)
        tag_map = TagMap([Dest.map_to(ds[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        assert any(f.code == CLK_TILDE_BOOL_CONTACT_ONLY for f in report.hints)

    def test_strict_mode_gives_error(self):
        A = Tag("A", TagType.INT)
        Dest = Tag("Dest", TagType.INT)

        def logic():
            with Rung():
                calc(~A, Dest)

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
        expr_findings = [f for f in report.hints if f.code == CLK_EXPR_ONLY_IN_CALC]
        # Both condition.left (AddExpr) and condition.right (LiteralExpr) are expressions
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

    def test_strict_mode_via_tagmap(self):
        A = Tag("A", TagType.INT)

        def logic():
            with Rung():
                copy(A * 2, Tag("Dest", TagType.INT))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = tag_map.validate(prog, mode="strict")
        assert any(f.code == CLK_EXPR_ONLY_IN_CALC for f in report.errors)


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

    def test_expr_only_in_math_mentions_expression_dsl(self):
        A = Tag("A", TagType.INT)
        B = Tag("B", TagType.INT)

        def logic():
            with Rung((A + B) > 10):
                out(Bool("Light"))

        prog = _build_program(logic)
        tag_map = TagMap(include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        r4_findings = [f for f in report.hints if f.code == CLK_EXPR_ONLY_IN_CALC]
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
