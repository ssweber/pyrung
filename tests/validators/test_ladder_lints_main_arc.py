"""Focused coverage for the main ladder-lint expansion arc."""

from __future__ import annotations

from pyrung import (
    And,
    Bool,
    Int,
    Or,
    Real,
    Rung,
    branch,
    calc,
    call,
    copy,
    out,
    return_early,
    subroutine,
)
from pyrung.core import Block, Program, TagType
from pyrung.core.validation import (
    CALL_NEVER_CALLED,
    CALL_RECURSION,
    CMP_ALWAYS_FALSE,
    CMP_ALWAYS_TRUE,
    MATH_DIV_ZERO,
    PTR_MAY_ESCAPE_BLOCK,
    RUNG_REDUNDANT_TERM,
    TAG_DEAD_WRITE,
)


def _codes(program: Program, *codes: str) -> list[str]:
    return [finding.code for finding in program.check(select=set(codes))]


def test_comparison_constant_over_declared_bounds() -> None:
    speed = Int("Speed", min=0, max=10, external=True)
    result = Bool("Result")
    with Program() as program:
        with Rung(speed > 10):
            out(result)
        with Rung(speed >= 0):
            out(Bool("Other"))

    assert _codes(program, CMP_ALWAYS_FALSE, CMP_ALWAYS_TRUE) == [
        CMP_ALWAYS_FALSE,
        CMP_ALWAYS_TRUE,
    ]


def test_comparison_punts_on_open_external_domain() -> None:
    speed = Int("Speed", external=True)
    with Program() as program:
        with Rung(speed > 10):
            out(Bool("Result"))

    assert not _codes(program, CMP_ALWAYS_FALSE, CMP_ALWAYS_TRUE)


def test_self_comparison_preserves_operand_correlation() -> None:
    value = Int("Value", min=0, max=1, external=True)
    with Program() as program:
        with Rung(value == value):
            out(Bool("Same"))
        with Rung(value < value):
            out(Bool("Less"))

    assert _codes(program, CMP_ALWAYS_FALSE, CMP_ALWAYS_TRUE) == [
        CMP_ALWAYS_TRUE,
        CMP_ALWAYS_FALSE,
    ]


def test_self_comparison_punts_on_open_domain() -> None:
    value = Int("Value", external=True)
    with Program() as program:
        with Rung(value == value):
            out(Bool("Same"))

    assert not _codes(program, CMP_ALWAYS_FALSE, CMP_ALWAYS_TRUE)


def test_closed_nan_self_comparison_preserves_ieee_behavior() -> None:
    nan = float("nan")
    value = Real("Value", default=nan, choices={nan: "not-a-number"}, external=True)
    with Program() as program:
        with Rung(value == value):
            out(Bool("Same"))

    assert _codes(program, CMP_ALWAYS_FALSE, CMP_ALWAYS_TRUE) == [CMP_ALWAYS_FALSE]


def test_comparison_uses_only_complete_producer_domains() -> None:
    source = Int("Source", external=True)
    closed = Int("Closed")
    open_value = Int("OpenValue")
    with Program() as program:
        with Rung():
            copy(1, closed)
        with Rung(closed > 1):
            out(Bool("ClosedResult"))
        with Rung():
            copy(source, open_value)
        with Rung(open_value > 10):
            out(Bool("OpenResult"))

    assert _codes(program, CMP_ALWAYS_FALSE) == [CMP_ALWAYS_FALSE]


def test_pointer_closed_domain_can_escape_block() -> None:
    data = Block("Data", TagType.INT, 1, 10)
    pointer = Int(
        "Pointer",
        default=1,
        choices={0: "low", 1: "first", 11: "high", 12: "higher", 13: "highest"},
    )
    with Program() as program:
        with Rung():
            copy(data[pointer], Int("Value"))

    report = program.check(select={PTR_MAY_ESCAPE_BLOCK})
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.bad_values == (0, 11, 12, 13)  # type: ignore[attr-defined]
    assert "outside values: 0, 11..13" in finding.message


def test_pointer_open_domain_punts() -> None:
    data = Block("Data", TagType.INT, 1, 10)
    pointer = Int("Pointer", default=1, external=True)
    with Program() as program:
        with Rung():
            copy(data[pointer], Int("Value"))

    assert not _codes(program, PTR_MAY_ESCAPE_BLOCK)


def test_pointer_guard_excludes_out_of_block_domain_value() -> None:
    data = Block("Data", TagType.INT, 1, 10)
    pointer = Int("Pointer", default=1, choices={0: "bad", 1: "good"}, external=True)
    with Program() as program:
        with Rung(pointer == 1):
            copy(data[pointer], Int("Value"))

    assert not _codes(program, PTR_MAY_ESCAPE_BLOCK)


def test_uncalled_subroutine_and_indirect_recursion() -> None:
    with Program(strict=False) as program:
        with Rung():
            call("first")
        with subroutine("first"):
            with Rung():
                call("second")
        with subroutine("second"):
            with Rung():
                call("first")
        with subroutine("unused"):
            with Rung():
                out(Bool("UnusedOutput"))

    report = program.check(select={CALL_NEVER_CALLED, CALL_RECURSION})
    assert [finding.code for finding in report] == [CALL_NEVER_CALLED, CALL_RECURSION]
    recursion = report.findings[1]
    assert recursion.cycle == ("first", "second", "first")  # type: ignore[attr-defined]


def test_direct_recursion_is_reported() -> None:
    with Program(strict=False) as program:
        with Rung():
            call("again")
        with subroutine("again"):
            with Rung():
                call("again")

    assert _codes(program, CALL_RECURSION) == [CALL_RECURSION]


def test_divisor_proved_zero_under_rung_guard() -> None:
    divisor = Int("Divisor", choices={0: "zero", 1: "one"}, external=True)
    numerator = Int("Numerator", external=True)
    result = Int("Result")
    with Program() as program:
        with Rung(divisor == 0):
            calc(numerator / divisor, result)

    assert _codes(program, MATH_DIV_ZERO) == [MATH_DIV_ZERO]


def test_divisor_merely_may_be_zero_punts() -> None:
    divisor = Int("Divisor", choices={0: "zero", 1: "one"}, external=True)
    numerator = Int("Numerator", external=True)
    with Program() as program:
        with Rung():
            calc(numerator / divisor, Int("Result"))

    assert not _codes(program, MATH_DIV_ZERO)


def test_caller_guards_participate_in_zero_proof() -> None:
    divisor = Int("Divisor", choices={0: "zero", 1: "one"}, external=True)
    numerator = Int("Numerator", external=True)
    with Program(strict=False) as program:
        with Rung(divisor == 0):
            call("divide")
        with subroutine("divide"):
            with Rung():
                calc(numerator / divisor, Int("Result"))

    assert _codes(program, MATH_DIV_ZERO) == [MATH_DIV_ZERO]


def test_intrascan_divisor_write_invalidates_caller_guard_proof() -> None:
    divisor = Int("Divisor", choices={0: "zero", 1: "one"})
    numerator = Int("Numerator", external=True)
    with Program(strict=False) as program:
        with Rung(divisor == 0):
            call("divide")
        with subroutine("divide"):
            with Rung():
                copy(1, divisor)
            with Rung():
                calc(numerator / divisor, Int("Result"))

    assert not _codes(program, MATH_DIV_ZERO)


def test_literal_zero_divisor_is_reported() -> None:
    numerator = Int("Numerator", external=True)
    with Program() as program:
        with Rung():
            calc(numerator / 0, Int("Result"))

    assert _codes(program, MATH_DIV_ZERO) == [MATH_DIV_ZERO]


def test_prior_early_return_prevents_false_div_zero_finding() -> None:
    divisor = Int("Divisor", choices={0: "zero"}, external=True)
    numerator = Int("Numerator", external=True)
    with Program(strict=False) as program:
        with Rung():
            call("divide")
        with subroutine("divide"):
            with Rung(divisor == 0):
                return_early()
            with Rung():
                calc(numerator / divisor, Int("Result"))

    assert not _codes(program, MATH_DIV_ZERO)


def test_later_early_return_does_not_hide_div_zero_finding() -> None:
    numerator = Int("Numerator", external=True)
    with Program(strict=False) as program:
        with Rung():
            call("divide")
        with subroutine("divide"):
            with Rung():
                calc(numerator / 0, Int("Result"))
            with Rung():
                return_early()

    assert _codes(program, MATH_DIV_ZERO) == [MATH_DIV_ZERO]


def test_same_rung_branch_return_conservatively_hides_div_zero_finding() -> None:
    stop = Bool("Stop", external=True)
    numerator = Int("Numerator", external=True)
    with Program(strict=False) as program:
        with Rung():
            call("divide")
        with subroutine("divide"):
            with Rung():
                with branch(stop):
                    return_early()
                calc(numerator / 0, Int("Result"))

    assert not _codes(program, MATH_DIV_ZERO)


def test_same_rung_branch_return_conservatively_hides_pointer_escape() -> None:
    data = Block("ReturnData", TagType.INT, 1, 10)
    pointer = Int("ReturnPointer", default=1, choices={1: "safe", 11: "outside"})
    stop = Bool("PointerStop", external=True)
    with Program(strict=False) as program:
        with Rung():
            call("read")
        with subroutine("read"):
            with Rung():
                with branch(stop):
                    return_early()
                copy(data[pointer], Int("ReadValue"))

    assert not _codes(program, PTR_MAY_ESCAPE_BLOCK)


def test_return_sensitive_nonzero_call_path_blocks_guard_only_div_zero_proof() -> None:
    divisor = Int("PathDivisor", choices={0: "zero", 1: "one"}, external=True)
    numerator = Int("PathNumerator", external=True)
    stop = Bool("PathStop", external=True)
    with Program(strict=False) as program:
        with Rung(divisor == 0):
            call("divide")
        with Rung(divisor == 1):
            call("outer")
        with subroutine("outer"):
            with Rung(stop):
                return_early()
            with Rung():
                call("divide")
        with subroutine("divide"):
            with Rung():
                calc(numerator / divisor, Int("PathResult"))

    assert not _codes(program, MATH_DIV_ZERO)


def test_duplicate_and_subsumed_terms_are_redundant() -> None:
    enabled = Bool("Enabled", external=True)
    speed = Int("Speed", external=True)
    with Program() as program:
        with Rung(enabled, enabled):
            out(Bool("Duplicate"))
        with Rung(speed > 10, speed > 5):
            out(Bool("AndResult"))
        with Rung(Or(speed > 5, speed > 10)):
            out(Bool("OrResult"))
        with Rung(And(speed < 20, speed < 30)):
            out(Bool("Nested"))

    assert _codes(program, RUNG_REDUNDANT_TERM) == [RUNG_REDUNDANT_TERM] * 4


def test_contradiction_owns_diagnostic_over_redundancy() -> None:
    value = Int("Value", external=True)
    with Program() as program:
        with Rung(value > 10, value < 5, value > 0):
            out(Bool("Result"))

    assert not _codes(program, RUNG_REDUNDANT_TERM)


def test_simple_write_overwritten_before_read_is_dead() -> None:
    value = Int("Value")
    with Program() as program:
        with Rung():
            copy(1, value)
        with Rung():
            copy(2, value)

    assert _codes(program, TAG_DEAD_WRITE) == [TAG_DEAD_WRITE]


def test_intervening_read_keeps_write_live() -> None:
    value = Int("Value")
    observed = Bool("Observed")
    with Program() as program:
        with Rung():
            copy(1, value)
        with Rung(value == 1):
            out(observed)
        with Rung():
            copy(2, value)

    assert not _codes(program, TAG_DEAD_WRITE)


def test_conditional_later_copy_is_not_a_guaranteed_overwrite() -> None:
    value = Int("Value")
    enabled = Bool("Enabled", external=True)
    with Program() as program:
        with Rung():
            copy(1, value)
        with Rung(enabled):
            copy(2, value)

    assert not _codes(program, TAG_DEAD_WRITE)


def test_later_copy_from_indirect_source_is_not_a_guaranteed_overwrite() -> None:
    data = Block("Data", TagType.INT, 1, 2)
    pointer = Int("Pointer", default=1, external=True)
    value = Int("Value")
    with Program() as program:
        with Rung():
            copy(1, value)
        with Rung():
            copy(data[pointer], value)

    assert not _codes(program, TAG_DEAD_WRITE)
