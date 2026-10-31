"""CLICK export and import of forloop body rungs, Break/Continue and nested loops."""

from __future__ import annotations

import textwrap

from pyrung.click import ladder_to_pyrung, pyrung_to_ladder
from tests.click.helpers import (
    build_program,
    normalize_csv,
    normalize_pyrung,
    strip_pyrung_boilerplate,
)

_SOURCE = """
with Program(strict=False) as p:
    with rung(X1):
        with forloop(3):
            out(Y1)
            comment("skip odd")
            with rung(X2):
                continue_loop()
            with rung(C1, X3):
                out(Y2)
                copy(1, DS1)
            with rung(X4):
                with forloop(2):
                    calc(DS2 + 1, DS2)
                    with rung(C2):
                        break_loop()
            with forloop(DS3):
                out(Y3)
"""


def test_body_rungs_export_between_for_and_next():
    logic, mapping = build_program(_SOURCE)
    bundle = pyrung_to_ladder(logic, mapping)
    rows = normalize_csv(bundle.main_rows)
    tokens = [(row[0], row[-1]) for row in rows]
    assert tokens == [
        ("R", "for(3)"),
        ("R", "out(Y001)"),
        ("#", "skip odd"),
        ("R", "continue()"),
        ("R", "out(Y002)"),
        ("", "copy(1,DS1)"),
        ("R", "for(2)"),
        ("R", "math(DS2 + 1,DS2,mode=decimal)"),
        ("R", "break()"),
        ("R", "next()"),
        ("R", "for(DS3)"),
        ("R", "out(Y003)"),
        ("R", "next()"),
        ("R", "next()"),
    ]
    # Body-rung contacts sit on the rail of their own rung.
    assert rows[3][1] == "X002"
    assert rows[4][1:3] == ("C1", "X003")


def test_conditioned_body_rung_rows():
    logic, mapping = build_program(
        """
        with rung(X1):
            with forloop(2):
                with rung(X2):
                    break_loop()
        """
    )
    bundle = pyrung_to_ladder(logic, mapping)
    rows = normalize_csv(bundle.main_rows)[:3]
    assert [(row[1], row[-1]) for row in rows] == [
        ("X001", "for(2)"),
        ("X002", "break()"),
        ("-", "next()"),
    ]
    assert all(set(row[2:-1]) == {"-"} for row in rows)


def test_import_restores_body_rungs_and_nesting():
    logic, mapping = build_program(_SOURCE)
    code = ladder_to_pyrung(pyrung_to_ladder(logic, mapping))
    assert "comment('skip odd')" in code
    assert strip_pyrung_boilerplate(code) == normalize_pyrung(
        textwrap.dedent(
            """
            with rung(X001):
                with forloop(3):
                    out(Y001)
                    with rung(X002):
                        continue_loop()
                    with rung(C1, X003):
                        out(Y002)
                        copy(1, DS1)
                    with rung(X004):
                        with forloop(2):
                            calc(DS2 + 1, DS2)
                            with rung(C2):
                                break_loop()
                    with forloop(DS3):
                        out(Y003)
            """
        )
    )


def test_imported_program_exports_the_same_rows():
    logic, mapping = build_program(_SOURCE)
    bundle = pyrung_to_ladder(logic, mapping)
    namespace: dict[str, object] = {}
    exec(ladder_to_pyrung(bundle), namespace)  # noqa: S102
    again = pyrung_to_ladder(namespace["logic"], mapping)
    assert normalize_csv(again.main_rows) == normalize_csv(bundle.main_rows)


def test_summary_renames_loop_exits_and_counts_nested_next():
    logic, mapping = build_program(_SOURCE)
    summary = pyrung_to_ladder(logic, mapping).export_summary
    assert ("break_loop", "break") in summary.renames
    assert ("continue_loop", "continue") in summary.renames
    assert summary.added_next == 3


def _loop_codes(source: str) -> list[str]:
    from pyrung.click.validation import validate_click_program

    logic, mapping = build_program(source)
    report = validate_click_program(logic, mapping, mode="strict")
    return sorted(f.code for f in report.errors if f.code.startswith("CLK_FORLOOP"))


def test_validation_counts_loop_depth_through_calls():
    assert _loop_codes(_SOURCE) == []
    deep = """
with Program(strict=False) as p:
    with subroutine("inner"):
        with rung():
            with forloop(2):
                with forloop(2):
                    out(Y2)
    with rung(X1):
        with forloop(2):
            with forloop(2):
                call("inner")
"""
    assert _loop_codes(deep) == ["CLK_FORLOOP_NESTING_DEPTH"]


def test_validation_flags_inner_oneshot_and_return_inside_a_loop():
    source = """
with Program(strict=False) as p:
    with subroutine("work"):
        with rung(X1):
            with forloop(3, oneshot=True):
                with forloop(2, oneshot=True):
                    out(Y1)
                with rung(X2):
                    return_early()
    with rung():
        call("work")
"""
    assert _loop_codes(source) == ["CLK_FORLOOP_INNER_ONESHOT", "CLK_FORLOOP_RETURN_INSIDE"]


def test_validation_sees_body_rung_contacts():
    from pyrung.click import ClickBlocks, TagMap
    from pyrung.click.validation import validate_click_program
    from pyrung.core import Program, Rung, forloop, out

    blocks = ClickBlocks()
    with Program(strict=False) as logic:
        with Rung(blocks.x[1]):
            with forloop(2):
                with Rung(blocks.ds[blocks.ds[1]] == 0):
                    out(blocks.y[1])
                with Rung(blocks.ds[blocks.ds[2]] == 0):
                    out(blocks.y[2])
    report = validate_click_program(logic, TagMap(include_system=False), mode="strict")
    locations = sorted(f.location for f in report.errors if f.code == "CLK_PTR_CONTEXT_ONLY_COPY")
    assert locations == [
        "main.rung[0].body_rung[0].condition[0].indirect_ref",
        "main.rung[0].body_rung[1].condition[0].indirect_ref",
    ]


def _nested_call_findings(source: str, mode: str = "strict"):
    from pyrung.click.validation import validate_click_program

    logic, mapping = build_program(source)
    report = validate_click_program(logic, mapping, mode=mode)
    return [
        f for f in (*report.errors, *report.warnings, *report.hints) if f.code == "CLK_NESTED_CALL"
    ]


_NESTED = """
with Program(strict=False) as p:
    with subroutine("inner"):
        with rung():
            out(Y2)
    with subroutine("outer"):
        with rung(X2):
            call("inner")
    with rung():
        call("outer")
"""


def test_validation_flags_a_call_inside_a_subroutine():
    (finding,) = _nested_call_findings(_NESTED)
    assert finding.severity == "error"
    assert finding.location == "subroutine 'outer' rung 1"
    assert "nested subroutine calls" in finding.message
    assert finding.suggestion
    assert finding.message.isascii() and finding.suggestion.isascii()
    (hint,) = _nested_call_findings(_NESTED, mode="warn")
    assert hint.severity == "hint"


def test_validation_flags_a_call_in_a_loop_or_branch_of_a_subroutine():
    source = """
with Program(strict=False) as p:
    with subroutine("inner"):
        with rung():
            out(Y2)
    with subroutine("outer"):
        with rung(X2):
            with forloop(2):
                call("inner")
        with rung(X3):
            out(Y3)
            with branch(X4):
                call("inner")
    with rung():
        call("outer")
"""
    findings = _nested_call_findings(source)
    assert [f.location for f in findings] == [
        "subroutine 'outer' rung 1",
        "subroutine 'outer' rung 2",
    ]


def test_validation_accepts_calls_from_main_only():
    assert _nested_call_findings(_SOURCE) == []
    source = """
with Program(strict=False) as p:
    with subroutine("work"):
        with rung():
            out(Y2)
    with rung(X1):
        with forloop(2):
            call("work")
"""
    assert _nested_call_findings(source) == []


def test_validation_flags_a_call_in_a_loop_body_rung_of_a_subroutine():
    source = """
with Program(strict=False) as p:
    with subroutine("inner"):
        with rung():
            out(Y2)
    with subroutine("outer"):
        with rung():
            with forloop(2):
                with rung(X2):
                    call("inner")
    with rung():
        call("outer")
"""
    (finding,) = _nested_call_findings(source)
    assert finding.location == "subroutine 'outer' rung 1"
