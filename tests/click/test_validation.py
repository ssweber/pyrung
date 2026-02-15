"""Tests for Click portability validation (Stage 2)."""

from __future__ import annotations

from pyrung.click import TagMap, dd, ds
from pyrung.click.validation import (
    CLK_EXPR_ONLY_IN_MATH,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    ClickValidationReport,
    validate_click_program,
)
from pyrung.core import Bool, Tag, TagType, as_value
from pyrung.core.program import Program, Rung, copy, math, out, run_enabled_function, run_function

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
                copy(as_value(dd[Pointer]), Dest)

        prog = _build_program(logic)
        tag_map = TagMap([Pointer.map_to(ds[100]), Dest.map_to(dd[1])], include_system=False)

        report = validate_click_program(prog, tag_map, mode="warn")
        codes = _finding_codes(report)
        assert CLK_PTR_CONTEXT_ONLY_COPY not in codes
