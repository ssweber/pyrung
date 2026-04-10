"""Tests for Click ladder CSV export (`pyrung_to_ladder`)."""

from __future__ import annotations

import csv
import functools
import io
import re
import textwrap
from pathlib import Path

import pytest

from pyrung.click import (
    LadderExportError,
    ModbusTcpTarget,
    TagMap,
    c,
    ct,
    ctd,
    dd,
    dh,
    ds,
    pyrung_to_ladder,
    receive,
    sc,
    send,
    t,
    td,
    txt,
    x,
    y,
)
from pyrung.core import (
    And,
    Block,
    BlockRange,
    Bool,
    Counter,
    Dint,
    Int,
    Or,
    Program,
    Rung,
    Tag,
    TagType,
    Timer,
    immediate,
)
from pyrung.core.program import (
    blockcopy,
    branch,
    calc,
    call,
    comment,
    copy,
    count_down,
    event_drum,
    fill,
    forloop,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    return_early,
    search,
    shift,
    subroutine,
    time_drum,
    unpack_to_bits,
    unpack_to_words,
)
from tests.click.helpers import build_program, normalize_csv

Counter3 = Counter.clone("Counter3")
Timer2 = Timer.clone("Timer2")


def _header() -> tuple[str, ...]:
    return (
        "marker",
        *tuple(
            [chr(ord("A") + i) for i in range(26)] + [f"A{chr(ord('A') + i)}" for i in range(5)]
        ),
        "AF",
    )


def _row(marker: str, prefix: list[str], af: str) -> tuple[str, ...]:
    cells = list(prefix)
    cells.extend(["-"] * (31 - len(cells)))
    return tuple([marker, *cells, af])


def _blank_row(marker: str, prefix: list[str], af: str = "") -> tuple[str, ...]:
    """Like _row but pads with blanks (OR continuation rows have no wires)."""
    cells = list(prefix)
    cells.extend([""] * (31 - len(cells)))
    return tuple([marker, *cells, af])


_END_ROW = _row("R", [], "end()")


def _literal_csv_rows(csv_text: str) -> list[tuple[str, ...]]:
    parsed_rows: list[tuple[str, ...]] = []
    for row in csv.reader(io.StringIO(textwrap.dedent(csv_text).strip()), strict=True):
        if row and row[0] != "#" and len(row) > 33:
            row = [*row[:32], ",".join(row[32:])]
        parsed_rows.append(tuple(row))
    rows = tuple(parsed_rows)
    return normalize_csv(rows)


def _assert_export_main_rows(source: str, expected_csv: str) -> None:
    logic, mapping = build_program(source)
    bundle = pyrung_to_ladder(logic, mapping)
    assert normalize_csv(bundle.main_rows) == _literal_csv_rows(expected_csv)


def test_header_and_width_invariants():
    A = Bool("A")
    B = Bool("B")

    with Program() as logic:
        with Rung(A):
            out(B)

    mapping = TagMap({A: x[1], B: y[1]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)

    assert bundle.main_rows[0] == _header()
    assert all(len(row) == 33 for row in bundle.main_rows)


def test_export_roundtrip_guard_rejects_missing_pin_row():
    from pyrung.click.ladder._exporter import _LadderExporter
    from pyrung.click.ladder.types import _RenderError

    logic, mapping = build_program(
        """
        with Rung(X1):
            on_delay(Timer[1], preset=100, unit="Tms").reset(X2)
        """
    )

    exporter = _LadderExporter(tag_map=mapping, program=logic)
    rendered_rows = exporter._render_scope(logic.rungs, scope="main", subroutine_name=None)
    mutated_rows = [row for row in rendered_rows if row[-1] != ".reset()"]

    with pytest.raises(_RenderError, match="pin count mismatch"):
        exporter._validate_scope_roundtrip(
            source_rungs=logic.rungs,
            rendered_rows=mutated_rows,
            scope="main",
            subroutine_name=None,
        )


def test_and_example_golden():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1, X2):
                out(Y1)
        """,
        """
        R,X001,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        """,
    )


def test_or_expansion_with_trailing_and_golden():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(Or(X1, X2), C1):
                out(Y1)
        """,
        """
        R,X001,T,C1,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,X002,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        """,
    )


def test_branch_row_is_continuation_after_parent_conditions():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(X2):
                    out(Y2)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        """,
    )


def test_multiple_branches_stack_vertical_markers():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(X2):
                    out(Y2)
                with branch(X3):
                    out(Y3)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,T:X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,X003,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y003)
        """,
    )


def test_parent_instruction_after_branch_stays_on_parent_path():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(X2):
                    out(Y2)
                out(Y3)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,T:X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y003)
        """,
    )


def test_branch_local_or_expands_with_click_topology():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(Or(X2, X3)):
                    out(Y2)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,T:X002,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,X003,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        """,
    )


def test_branch_local_or_with_series_suffix_stays_mechanical():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(Or(X2, X3), X4):
                    out(Y2)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,T:X002,T,X004,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,X003,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        """,
    )


def test_branch_local_or_with_series_suffix_pushes_post_branch_siblings_down():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
                with branch(Or(X2, X3), X4):
                    out(Y2)
                out(Y3)
        """,
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,T:X002,T,X004,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,T:X003,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y003)
        """,
    )


def test_branch_with_series_then_local_or_keeps_click_merge_topology():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                with branch(X2, Or(X3, X4)):
                    out(Y1)
                out(Y2)
        """,
        """
        R,X001,T:X002,T:X003,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,|,X004,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        """,
    )


def test_branch_with_series_then_three_way_local_or_keeps_parent_continuation_visible():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                with branch(X2, Or(X3, X4, X5)):
                    out(Y1)
                out(Y2)
        """,
        """
        R,X001,T:X002,T:X003,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,|,T:X004,|,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        ,,|,X005,,,,,,,,,,,,,,,,,,,,,,,,,,,,,
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        """,
    )


def test_multiple_instruction_rows_share_powered_path():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1, X2):
                out(Y1)
                latch(Y2)
                reset(Y3)
        """,
        """
        R,X001,X002,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,latch(Y002)
        ,,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,reset(Y003)
        """,
    )


def test_immediate_contact_and_coils_render_canonical_tokens():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(immediate(X1)):
                out(immediate(Y1))
                latch(immediate(Y2))
                reset(immediate(Y3))
        """,
        """
        R,immediate(X001),T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(immediate(Y001))
        ,,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,latch(immediate(Y002))
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,reset(immediate(Y003))
        """,
    )


def test_immediate_contiguous_range_renders_compact_token():
    Start = Bool("Start")
    Outputs = Block("Outputs", TagType.BOOL, 1, 4)

    with Program() as logic:
        with Rung(Start):
            out(immediate(Outputs.select(1, 4)))

    mapping = TagMap(
        {
            Start: x[1],
            Outputs: y.select(1, 4),
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)

    assert normalize_csv(tuple(bundle.main_rows)) == _literal_csv_rows(
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(immediate(Y001..Y004))
        """
    )


def test_vertical_wire_stack_for_three_or_branches():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(Or(X1, X2, X3), C1):
                out(Y1)
        """,
        """
        R,X001,T,C1,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,X002,|
        ,X003
        """,
    )


def test_builder_pin_rows_are_independent_continuations():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                count_up(Counter[1], preset=5).down(X2).reset(X3)
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,count_up(CT1,CTD1,preset=5)
        ,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,.down()
        ,X003,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,.reset()
        """,
    )


def test_forloop_lowers_to_for_body_and_next_rows():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                with forloop(3, oneshot=True):
                    out(Y1)
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,for(3,oneshot=1)
        R,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        R,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,next()
        """,
    )


def test_subroutine_files_sorted_slugged_and_return_tailed(tmp_path: Path):
    Start = Bool("Start")
    SubOut = Bool("SubOut")

    with Program() as logic:
        with Rung(Start):
            call("beta-two")
            call("alpha")

        with subroutine("alpha"):
            with Rung():
                out(SubOut)

        with subroutine("beta-two"):
            with Rung():
                return_early()

    mapping = TagMap({Start: x[1], SubOut: y[1]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)
    assert [name for name, _ in bundle.subroutine_rows] == ["alpha", "beta-two"]
    assert normalize_csv(tuple(bundle.main_rows)) == _literal_csv_rows(
        """
        R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,call("beta-two")
        ,,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,call("alpha")
        """
    )
    alpha_rows = dict(bundle.subroutine_rows)["alpha"]
    beta_rows = dict(bundle.subroutine_rows)["beta-two"]
    assert normalize_csv(alpha_rows) == _literal_csv_rows(
        """
        R,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        R,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,return()
        """
    )
    assert normalize_csv(beta_rows) == _literal_csv_rows(
        """
        R,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,return()
        """
    )

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)
    assert (out_dir / "main.csv").exists()
    assert (out_dir / "subroutines" / "alpha.csv").exists()
    assert (out_dir / "subroutines" / "beta_two.csv").exists()


def test_string_token_rendering_uses_doubled_quotes_without_backslash_escapes():
    Enable = Bool("Enable")
    Chars = Block("Chars", TagType.CHAR, 1, 4)
    Result = Int("Result")
    Found = Bool("Found")

    with Program() as logic:
        with Rung(Enable):
            search(Chars.select(1, 4) == 'sub"name', result=Result, found=Found)
        with Rung(Enable):
            search(Chars.select(1, 4) == "normal", result=Result, found=Found)

    mapping = TagMap(
        {
            Enable: x[1],
            Chars: txt.select(1, 4),
            Result: ds[1],
            Found: c[1],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert tokens == [
        'search(TXT1..TXT4 == "sub""name",result=DS1,found=C1)',
        'search(TXT1..TXT4 == "normal",result=DS1,found=C1)',
        "end()",
    ]


def test_string_token_csv_roundtrip_requires_only_doubled_quote_unescape(tmp_path: Path):
    Enable = Bool("Enable")
    Chars = Block("Chars", TagType.CHAR, 1, 4)
    Result = Int("Result")
    Found = Bool("Found")

    with Program() as logic:
        with Rung(Enable):
            search(Chars.select(1, 4) == 'sub"name', result=Result, found=Found)

    mapping = TagMap(
        {
            Enable: x[1],
            Chars: txt.select(1, 4),
            Result: ds[1],
            Found: c[1],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = 'search(TXT1..TXT4 == "sub""name",result=DS1,found=C1)'
    expected_rows = _literal_csv_rows(
        f"""
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,{expected}
        """
    )
    assert normalize_csv(tuple(bundle.main_rows)) == expected_rows

    out_dir = tmp_path / "ladder"
    bundle.write(out_dir)

    raw_csv = (out_dir / "main.csv").read_text(encoding="utf-8")
    assert '\\"' not in raw_csv

    with (out_dir / "main.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    assert normalize_csv(tuple(tuple(row) for row in rows)) == expected_rows

    af_token = rows[1][-1]
    assert af_token == expected
    assert '\\"' not in af_token

    # Extract the value literal from the comparison expression
    value_literal = af_token.split(" == ", maxsplit=1)[1].split(",result=", maxsplit=1)[0]
    assert value_literal == '"sub""name"'
    assert value_literal[1:-1].replace('""', '"') == 'sub"name'


def test_tokens_include_explicit_defaults_and_oneshot():
    Enable = Bool("Enable")
    Light = Bool("Light")
    Dest1 = Int("Dest1")
    Dest2 = Int("Dest2")
    Result = Int("Result")
    Found = Bool("Found")
    Data = Block("Data", TagType.INT, 1, 2)
    Chars = Block("Chars", TagType.CHAR, 1, 2)
    PackDest = Int("PackDest")

    with Program() as logic:
        with Rung(Enable):
            out(Light)
        with Rung(Enable):
            copy(5, Dest1)
        with Rung(Enable):
            calc(Dest1 + 1, Dest2)
        with Rung(Enable):
            search(Data.select(1, 2) == 1, result=Result, found=Found)
        with Rung(Enable):
            pack_text(Chars.select(1, 2), PackDest)

    mapping = TagMap(
        {
            Enable: x[1],
            Light: y[1],
            Dest1: ds[1],
            Dest2: ds[2],
            Result: ds[3],
            Found: c[1],
            Data: ds.select(10, 11),
            Chars: txt.select(1, 2),
            PackDest: ds[4],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert tokens == [
        "out(Y001)",
        "copy(5,DS1)",
        "math(DS1 + 1,DS2,mode=decimal)",
        "search(DS10..DS11 == 1,result=DS3,found=C1)",
        "pack_text(TXT1..TXT2,DS4)",
        "end()",
    ]


def test_calc_hex_token_uses_inferred_hex_mode():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                calc(DH1 | DH2, DH3)
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,math(DH1 OR DH2,DH3,mode=hex)
        """,
    )


def test_calc_token_uses_click_native_operators():
    """Calc expressions use Click-native operator syntax in CSV tokens."""
    from pyrung.core.expression import lsh, rsh, sqrt

    Enable = Bool("Enable")
    A = Int("A")
    B = Int("B")
    Result = Int("Result")
    H1 = Block("H1", TagType.WORD, 1, 1)
    H2 = Block("H2", TagType.WORD, 1, 1)
    HDest = Block("HDest", TagType.WORD, 1, 1)

    with Program() as logic:
        # Decimal-mode operators
        with Rung(Enable):
            calc(A**2, Result)  # Power → ^
        with Rung(Enable):
            calc(A % B, Result)  # Modulo → MOD
        with Rung(Enable):
            calc(A + B, Result)  # Addition with spaces
        with Rung(Enable):
            calc(sqrt(A), Result)  # MathFuncExpr → uppercase Click name
        # Hex-mode operators (WORD tags)
        with Rung(Enable):
            calc(H1[1] << 3, HDest[1])  # Left shift operator → LSH
        with Rung(Enable):
            calc(H1[1] >> 1, HDest[1])  # Right shift operator → RSH
        with Rung(Enable):
            calc(lsh(H1[1], 4), HDest[1])  # ShiftFuncExpr
        with Rung(Enable):
            calc(rsh(H1[1], 2), HDest[1])  # ShiftFuncExpr
        with Rung(Enable):
            calc(H1[1] & H2[1], HDest[1])  # AND
        with Rung(Enable):
            calc(H1[1] | H2[1], HDest[1])  # OR
        with Rung(Enable):
            calc(H1[1] ^ H2[1], HDest[1])  # XOR

    mapping = TagMap(
        {
            Enable: x[1],
            A: ds[1],
            B: ds[2],
            Result: ds[3],
            H1[1]: dh[1],
            H2[1]: dh[2],
            HDest[1]: dh[3],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert tokens == [
        "math(DS1 ^ 2,DS3,mode=decimal)",
        "math(DS1 MOD DS2,DS3,mode=decimal)",
        "math(DS1 + DS2,DS3,mode=decimal)",
        "math(SQRT(DS1),DS3,mode=decimal)",
        "math(LSH(DH1,3),DH3,mode=hex)",
        "math(RSH(DH1,1),DH3,mode=hex)",
        "math(LSH(DH1,4),DH3,mode=hex)",
        "math(RSH(DH1,2),DH3,mode=hex)",
        "math(DH1 AND DH2,DH3,mode=hex)",
        "math(DH1 OR DH2,DH3,mode=hex)",
        "math(DH1 XOR DH2,DH3,mode=hex)",
        "end()",
    ]


def test_calc_math_func_names_use_click_convention():
    """All math function names map to Click formula-pad names."""
    from pyrung.core.expression import (
        acos,
        asin,
        atan,
        cos,
        degrees,
        log,
        log10,
        radians,
        sin,
        sqrt,
        tan,
    )
    from pyrung.core.tag import Real

    Enable = Bool("Enable")
    A = Real("A")
    Result = Real("Result")

    func_and_click_name = [
        (sqrt, "SQRT"),
        (sin, "SIN"),
        (cos, "COS"),
        (tan, "TAN"),
        (asin, "ASIN"),
        (acos, "ACOS"),
        (atan, "ATAN"),
        (radians, "RAD"),
        (degrees, "DEG"),
        (log10, "LOG"),
        (log, "LN"),
    ]

    from pyrung.click import df

    for func, click_name in func_and_click_name:
        with Program() as logic:
            with Rung(Enable):
                calc(func(A), Result)

        mapping = TagMap(
            {Enable: x[1], A: df[1], Result: df[2]},
            include_system=False,
        )
        bundle = pyrung_to_ladder(logic, mapping)
        tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]
        assert tokens == [f"math({click_name}(DF1),DF2,mode=decimal)", "end()"], (
            f"{func.__name__} should export as {click_name}, tokens={tokens}"
        )


def test_mixed_family_calc_fails_precheck_with_calc_mode_mixed_code():
    Enable = Bool("Enable")
    A = Int("A")
    H = Block("H", TagType.WORD, 1, 1)
    Dest = Int("Dest")

    with Program() as logic:
        with Rung(Enable):
            calc(A + H[1], Dest)

    mapping = TagMap(
        {
            Enable: x[1],
            A: ds[1],
            H[1]: dh[1],
            Dest: ds[2],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    assert list(exc_info.value.issues) == [
        {
            "path": "main.rung[0].instruction[0](CalcInstruction).instruction.expression",
            "message": (
                "CLK_CALC_MODE_MIXED: calc() mixes WORD (hex-family) and non-WORD "
                "(decimal-family) operands at "
                "main.rung[0].instruction[0](CalcInstruction).instruction.expression."
            ),
            "source_file": None,
            "source_line": None,
        }
    ]


def test_tokens_cover_remaining_instruction_families_and_pin_rows():
    Enable = Bool("Enable")

    SrcBlock = Block("SrcBlock", TagType.INT, 1, 3)
    DstBlock = Block("DstBlock", TagType.INT, 1, 3)
    Bits = Block("Bits", TagType.BOOL, 1, 32)
    Words = Block("Words", TagType.INT, 1, 2)
    DWord = Dint("DWord")

    TonReset = Bool("TonReset")

    CdReset = Bool("CdReset")

    ShiftClock = Bool("ShiftClock")
    ShiftReset = Bool("ShiftReset")

    DrumReset = Bool("DrumReset")
    DrumJump = Bool("DrumJump")
    DrumJog = Bool("DrumJog")
    DrumStep = Int("DrumStep")
    DrumAcc = Int("DrumAcc")
    DrumDone = Bool("DrumDone")
    DrumOut1 = Bool("DrumOut1")
    DrumOut2 = Bool("DrumOut2")
    Event1 = Bool("Event1")
    Event2 = Bool("Event2")

    SendSource = Int("SendSource")
    SendBusy = Bool("SendBusy")
    SendSuccess = Bool("SendSuccess")
    SendError = Bool("SendError")
    SendEx = Int("SendEx")
    RecvDest = Int("RecvDest")
    RecvBusy = Bool("RecvBusy")
    RecvSuccess = Bool("RecvSuccess")
    RecvError = Bool("RecvError")
    RecvEx = Int("RecvEx")

    with Program() as logic:
        with Rung(Enable):
            blockcopy(SrcBlock.select(1, 3), DstBlock.select(1, 3), oneshot=True)
        with Rung(Enable):
            fill(7, DstBlock.select(1, 3), oneshot=True)
        with Rung(Enable):
            pack_bits(Bits.select(1, 16), DWord, oneshot=True)
        with Rung(Enable):
            pack_words(Words.select(1, 2), DWord, oneshot=True)
        with Rung(Enable):
            unpack_to_bits(DWord, Bits.select(1, 32), oneshot=True)
        with Rung(Enable):
            unpack_to_words(DWord, Words.select(1, 2), oneshot=True)
        with Rung(Enable):
            on_delay(Timer[1], preset=100).reset(TonReset)
        with Rung(Enable):
            off_delay(Timer2, preset=50)
        with Rung(Enable):
            count_down(Counter3, preset=9).reset(CdReset)
        with Rung(Enable):
            shift(Bits.select(1, 8)).clock(ShiftClock).reset(ShiftReset)
        with Rung(Enable):
            event_drum(
                outputs=[DrumOut1, DrumOut2],
                events=[Event1, Event2],
                pattern=[[1, 0], [0, 1]],
                current_step=DrumStep,
                completion_flag=DrumDone,
            ).reset(DrumReset).jump(DrumJump, step=DrumStep).jog(DrumJog)
        with Rung(Enable):
            time_drum(
                outputs=[DrumOut1, DrumOut2],
                presets=[100, 200],
                pattern=[[1, 0], [0, 1]],
                current_step=DrumStep,
                accumulator=DrumAcc,
                completion_flag=DrumDone,
            ).reset(DrumReset).jump(DrumJump, step=DrumStep).jog(DrumJog)
        with Rung(Enable):
            send(
                target=ModbusTcpTarget("plc1", "127.0.0.1", port=502, device_id=3),
                remote_start="DS1",
                source=SendSource,
                sending=SendBusy,
                success=SendSuccess,
                error=SendError,
                exception_response=SendEx,
            )
        with Rung(Enable):
            receive(
                target=ModbusTcpTarget("plc2", "127.0.0.1", port=502, device_id=4),
                remote_start="DS2",
                dest=RecvDest,
                receiving=RecvBusy,
                success=RecvSuccess,
                error=RecvError,
                exception_response=RecvEx,
            )

    mapping = TagMap(
        {
            Enable: x[1],
            SrcBlock: ds.select(100, 102),
            DstBlock: ds.select(200, 202),
            Bits: c.select(10, 41),
            Words: ds.select(300, 301),
            DWord: dd[1],
            Timer[1].Done: t[1],
            Timer[1].Acc: td[1],
            TonReset: x[2],
            Timer2.Done: t[2],
            Timer2.Acc: td[2],
            Counter3.Done: ct[3],
            Counter3.Acc: ctd[3],
            CdReset: x[3],
            ShiftClock: x[4],
            ShiftReset: x[5],
            DrumReset: x[6],
            DrumJump: x[7],
            DrumJog: x[8],
            DrumStep: ds[10],
            DrumAcc: td[10],
            DrumDone: c[2],
            DrumOut1: y[1],
            DrumOut2: c[1],
            Event1: x[9],
            Event2: sc[50],
            SendSource: ds[20],
            SendBusy: c[3],
            SendSuccess: c[4],
            SendError: c[5],
            SendEx: ds[21],
            RecvDest: ds[22],
            RecvBusy: c[6],
            RecvSuccess: c[7],
            RecvError: c[8],
            RecvEx: ds[23],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]

    assert tokens == [
        "blockcopy(DS100..DS102,DS200..DS202,oneshot=1)",
        "fill(7,DS200..DS202,oneshot=1)",
        "pack_bits(C10..C25,DD1,oneshot=1)",
        "pack_words(DS300..DS301,DD1,oneshot=1)",
        "unpack_to_bits(DD1,C10..C41,oneshot=1)",
        "unpack_to_words(DD1,DS300..DS301,oneshot=1)",
        "on_delay(T1,TD1,preset=100,unit=Tms)",
        ".reset()",
        "off_delay(T2,TD2,preset=50,unit=Tms)",
        "count_down(CT3,CTD3,preset=9)",
        ".reset()",
        "shift(C10..C17)",
        ".clock()",
        ".reset()",
        "event_drum(outputs=[Y001,C1],events=[X009,SC50],pattern=[[1,0],[0,1]],current_step=DS10,completion_flag=C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        "time_drum(outputs=[Y001,C1],presets=[100,200],unit=Tms,pattern=[[1,0],[0,1]],current_step=DS10,accumulator=TD10,completion_flag=C2)",
        ".reset()",
        ".jump(DS10)",
        ".jog()",
        'send(target=ModbusTcpTarget(name="plc1",ip="127.0.0.1",port=502,device_id=3),remote_start="DS1",source=DS20,sending=C3,success=C4,error=C5,exception_response=DS21)',
        'receive(target=ModbusTcpTarget(name="plc2",ip="127.0.0.1",port=502,device_id=4),remote_start="DS2",dest=DS22,receiving=C6,success=C7,error=C8,exception_response=DS23)',
        "end()",
    ]


def test_precheck_and_issue_payload():
    A = Int("A")
    Dest = Int("Dest")

    with Program() as logic:
        with Rung():
            copy(A * 2, Dest)

    mapping = TagMap({A: ds[1], Dest: ds[2]}, include_system=False)

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    assert list(exc_info.value.issues) == [
        {
            "path": "main.rung[0].instruction[0](CopyInstruction).instruction.source",
            "message": (
                "CLK_EXPR_ONLY_IN_CALC: Expression used outside calc instruction at "
                "main.rung[0].instruction[0](CopyInstruction).instruction.source."
            ),
            "source_file": None,
            "source_line": None,
        }
    ]


def test_immediate_non_contiguous_range_fails_with_explicit_diagnostic():
    Start = Bool("Start")
    Outputs = Block("Outputs", TagType.BOOL, 1, 4)

    with Program() as logic:
        with Rung(Start):
            out(immediate(Outputs.select(1, 4)))

    mapping = TagMap(
        {
            Start: x[1],
            Outputs[1]: y[1],
            Outputs[2]: y[3],
            Outputs[3]: y[4],
            Outputs[4]: y[6],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    assert list(exc_info.value.issues) == [
        {
            "path": "main.rung[0].instruction[0](OutInstruction).instruction.target",
            "message": (
                "CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS: Immediate coil range must map to "
                "contiguous addresses at "
                "main.rung[0].instruction[0](OutInstruction).instruction.target."
            ),
            "source_file": None,
            "source_line": None,
        }
    ]


def test_immediate_in_copy_fails_with_context_diagnostic():
    Enable = Bool("Enable")
    Source = Bool("Source")
    Dest = Int("Dest")

    with Program() as logic:
        with Rung(Enable):
            copy(immediate(Source), Dest)

    mapping = TagMap(
        {
            Enable: x[1],
            Source: x[2],
            Dest: ds[1],
        },
        include_system=False,
    )

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    assert exc_info.value.issues[0] == {
        "path": "main.rung[0].instruction[0](CopyInstruction).instruction.source",
        "message": (
            "CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED: Immediate wrapper is not allowed at "
            "main.rung[0].instruction[0](CopyInstruction).instruction.source."
        ),
        "source_file": None,
        "source_line": None,
    }


def test_nested_subroutine_call_issue_includes_source_location():
    A = Bool("A")
    SubOut = Bool("SubOut")

    with Program() as logic:
        with Rung(A):
            call("outer")

        with subroutine("outer"):
            with Rung():
                call("inner")

        with subroutine("inner"):
            with Rung():
                out(SubOut)

    mapping = TagMap({A: x[1], SubOut: y[1]}, include_system=False)

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    issue = exc_info.value.issues[0]
    assert issue["path"] == "subroutine[outer].rung[0].instruction[0](CallInstruction)"
    assert issue["message"] == "Nested subroutine calls are not supported for Click export."
    assert isinstance(issue["source_file"], str)
    assert Path(issue["source_file"]).samefile(__file__)
    assert isinstance(issue["source_line"], int)


# --- Rung comment rows ---


def test_comment_single_line():
    _assert_export_main_rows(
        """
        with Program() as p:
            comment("Turn on B when A is true.")
            with Rung(X1):
                out(Y1)
        """,
        """
        #,Turn on B when A is true.
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        """,
    )


def test_comment_multi_line():
    _assert_export_main_rows(
        """
        with Program() as p:
            comment("Line one.\\nLine two.")
            with Rung(X1):
                out(Y1)
        """,
        """
        #,Line one.
        #,Line two.
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        """,
    )


def test_no_comment_no_extra_rows():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                out(Y1)
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        """,
    )


def test_comment_with_branches():
    A = Bool("A")
    Mode = Bool("Mode")
    Y1 = Bool("Y1")
    Y2 = Bool("Y2")

    with Program() as logic:
        comment("Branching rung.")
        with Rung(A):
            with branch(Mode):
                out(Y1)
            with branch(~Mode):
                out(Y2)

    mapping = TagMap({A: x[1], Mode: c[1], Y1: y[1], Y2: y[2]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)

    # Comment row should be first, before the R row
    assert bundle.main_rows[1] == ("#", "Branching rung.")
    assert bundle.main_rows[2][0] == "R"


def test_empty_branch_comment_fails_round_trip_validation():
    logic, mapping = build_program("""
        with Program() as p:
            comment("No rows should be emitted.")
            with Rung(X1):
                with branch(C1):
                    pass
    """)

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, mapping)

    issue = exc_info.value.issues[0]
    assert issue["path"] == "main"
    assert issue["message"] == (
        "CSV round-trip validation failed: comment sequence mismatch: "
        "expected ['No rows should be emitted.'], got []"
    )


# --- Native topology golden suite (source: tests/fixtures/click_or_topology.csv) ---


_NATIVE_OR_TOPOLOGY_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "click_or_topology.csv"
)


@functools.lru_cache(maxsize=1)
def _native_or_topology_rows() -> dict[int, tuple[tuple[str, ...], ...]]:
    patterns: dict[int, list[tuple[str, ...]]] = {}
    current_pattern: int | None = None

    with _NATIVE_OR_TOPOLOGY_FIXTURE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row:
                continue

            marker = row[0]
            if marker == "#":
                text = row[1].strip() if len(row) > 1 else ""
                match = re.match(r"^(\d+)\.", text)
                if match is not None:
                    current_pattern = int(match.group(1))
                    patterns[current_pattern] = []
                continue

            if marker in {"R", ""} and current_pattern is not None:
                patterns[current_pattern].append(tuple(row))

    return {key: tuple(rows) for key, rows in patterns.items()}


def _assert_native_pattern(
    *,
    pattern_id: int,
    bundle_rows: tuple[tuple[str, ...], ...],
    expected_rows: tuple[tuple[str, ...], ...],
) -> None:
    assert _native_or_topology_rows()[pattern_id] == expected_rows
    assert bundle_rows == (_header(), *expected_rows, _END_ROW)


def test_native_or_topology_fixture_shape():
    patterns = _native_or_topology_rows()
    assert set(patterns) == set(range(1, 9))
    assert all(len(row) == 33 for rows in patterns.values() for row in rows)


def test_native_pattern_1_mid_rung_or():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(X001, Or(X002, C1)):
            out(Y001)

    mapping = TagMap({X001: x[1], X002: x[2], C1: c[1], Y001: y[1]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T:X002", "T"], "out(Y001)"),
        _blank_row("", ["", "C1"]),
    )
    _assert_native_pattern(pattern_id=1, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_2_series_ors():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(Or(X001, X002), Or(C1, C2)):
            out(Y001)

    mapping = TagMap({X001: x[1], X002: x[2], C1: c[1], C2: c[2], Y001: y[1]}, include_system=False)
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T", "T:C1", "T"], "out(Y001)"),
        _blank_row("", ["X002", "", "C2"]),
    )
    _assert_native_pattern(pattern_id=2, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_3_or_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(Or(X001, X002)):
            out(Y001)
            with branch(C1):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], C1: c[1], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "C1"], "out(Y002)"),
    )
    _assert_native_pattern(pattern_id=3, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_4_three_way_or_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(Or(X001, X002, X003)):
            out(Y001)
            with branch(C1):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "|", "C1"], "out(Y002)"),
        _blank_row("", ["X003"]),
    )
    _assert_native_pattern(pattern_id=4, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_5_combined_or_multi_output_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    X004 = Bool("X004")
    C1 = Bool("C1")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")
    Y003 = Bool("Y003")

    with Program() as logic:
        with Rung(X001, Or(X002, X003, X004)):
            out(Y001)
            with branch(C1):
                out(Y002)
            out(Y003)

    mapping = TagMap(
        {
            X001: x[1],
            X002: x[2],
            X003: x[3],
            X004: x[4],
            C1: c[1],
            Y001: y[1],
            Y002: y[2],
            Y003: y[3],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T:X002", "T", "T"], "out(Y001)"),
        _row("", ["", "T:X003", "|", "T:C1"], "out(Y002)"),
        _row("", ["", "X004", "", "-"], "out(Y003)"),
    )
    _assert_native_pattern(pattern_id=5, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_6_mid_rung_or_with_nested_all_of():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    C2 = Bool("C2")
    C3 = Bool("C3")
    Y001 = Bool("Y001")

    with Program() as logic:
        with Rung(X001, Or(X002, And(C1, C2, C3), X003)):
            out(Y001)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], C2: c[2], C3: c[3], Y001: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T:X002", "-", "-", "T"], "out(Y001)"),
        _blank_row("", ["", "T:C1", "C2", "C3", "|"]),
        _blank_row("", ["", "X003", "-", "-"]),
    )
    _assert_native_pattern(pattern_id=6, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_7_or_with_two_branches():
    X001 = Bool("X001")
    X002 = Bool("X002")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")
    Y003 = Bool("Y003")

    with Program() as logic:
        with Rung(Or(X001, X002)):
            out(Y001)
            with branch(C1):
                out(Y002)
            with branch(C2):
                out(Y003)

    mapping = TagMap(
        {X001: x[1], X002: x[2], C1: c[1], C2: c[2], Y001: y[1], Y002: y[2], Y003: y[3]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "T:C1"], "out(Y002)"),
        _row("", ["", "", "C2"], "out(Y003)"),
    )
    _assert_native_pattern(pattern_id=7, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_native_pattern_8_series_ors_plus_branch():
    X001 = Bool("X001")
    X002 = Bool("X002")
    X003 = Bool("X003")
    C1 = Bool("C1")
    C2 = Bool("C2")
    Y001 = Bool("Y001")
    Y002 = Bool("Y002")

    with Program() as logic:
        with Rung(Or(X001, X002), Or(C1, C2)):
            out(Y001)
            with branch(X003):
                out(Y002)

    mapping = TagMap(
        {X001: x[1], X002: x[2], X003: x[3], C1: c[1], C2: c[2], Y001: y[1], Y002: y[2]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    expected = (
        _row("R", ["X001", "T", "T:C1", "T", "T"], "out(Y001)"),
        _row("", ["X002", "", "C2", "", "X003"], "out(Y002)"),
    )
    _assert_native_pattern(pattern_id=8, bundle_rows=bundle.main_rows, expected_rows=expected)


def test_calc_sum_expr_renders_colon_range():
    """SumExpr renders as SUM ( first : last ) with spaced colon syntax."""
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
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]
    assert tokens == ["math(SUM ( DH1 : DH5 ),DH100,mode=hex)", "end()"]


def test_calc_sum_expr_hex_mode():
    """SumExpr on WORD block infers hex mode."""
    Enable = Bool("Enable")
    DH = Block("DH", TagType.WORD, 1, 10)
    Dest = Block("Dest", TagType.WORD, 1, 1)

    with Program() as logic:
        with Rung(Enable):
            calc(DH.select(1, 3).sum(), Dest[1])

    mapping = TagMap(
        {Enable: x[1], Dest[1]: dh[100], **{DH[i]: dh[i] for i in range(1, 11)}},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]
    assert tokens == ["math(SUM ( DH1 : DH3 ),DH100,mode=hex)", "end()"]


def test_calc_sum_expr_decimal_mode():
    """SumExpr on INT block infers decimal mode."""
    Enable = Bool("Enable")
    DS = Block("DS", TagType.INT, 1, 10)
    Result = Int("Result")

    with Program() as logic:
        with Rung(Enable):
            calc(DS.select(1, 5).sum(), Result)

    mapping = TagMap(
        {Enable: x[1], Result: ds[100], **{DS[i]: ds[i] for i in range(1, 11)}},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    tokens = [row[-1] for row in bundle.main_rows[1:] if row[-1] != ""]
    assert tokens == ["math(SUM ( DS1 : DS5 ),DS100,mode=decimal)", "end()"]


# ---- ExportSummary tests ----


def test_summary_includes_calc_rename():
    """Summary reports calc → math when program uses calc."""
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
    bundle = pyrung_to_ladder(logic, mapping)
    s = bundle.export_summary
    assert s.renames == (("calc", "math"),)
    assert s.added_end is True
    assert s.summary() == "Renamed: calc \u2192 math\nAdded:   end() on main"


def test_summary_omits_calc_rename_when_unused():
    """Summary does not include calc → math when program has no calc."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            out(Light)

    mapping = TagMap(
        {Enable: x[1], Light: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    s = bundle.export_summary
    assert s.renames == ()
    assert s.summary() == "Added:   end() on main"


def test_summary_counts_forloop_next():
    """Summary reports correct count of next() additions."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                out(Light)
        with Rung(Enable):
            with forloop(2):
                out(Light)

    mapping = TagMap(
        {Enable: x[1], Light: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    s = bundle.export_summary
    assert s.added_next == 2
    assert s.renames == (("forloop", "for"),)
    assert (
        s.summary()
        == "Renamed: forloop \u2192 for\nAdded:   next() closing 2 for-loops, end() on main"
    )


def test_summary_counts_subroutine_return():
    """Summary reports return() additions on subroutines."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            call("MySub")
        with subroutine("MySub"):
            with Rung(Enable):
                out(Light)

    mapping = TagMap(
        {Enable: x[1], Light: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    s = bundle.export_summary
    assert s.added_return == 1
    assert s.summary() == "Added:   return() on 1 subroutine, end() on main"


def test_summary_return_not_counted_when_explicit():
    """Summary does not count return() when subroutine already ends with one."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            call("MySub")
        with subroutine("MySub"):
            with Rung(Enable):
                out(Light)
            with Rung():
                return_early()

    mapping = TagMap(
        {Enable: x[1], Light: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    s = bundle.export_summary
    assert s.added_return == 0


def test_summary_end_always_present():
    """Summary always reports end() on main."""
    Enable = Bool("Enable")
    Light = Bool("Light")

    with Program() as logic:
        with Rung(Enable):
            out(Light)

    mapping = TagMap(
        {Enable: x[1], Light: y[1]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    assert bundle.export_summary.added_end is True
    assert bundle.export_summary.summary() == "Added:   end() on main"


def test_summary_str_format():
    """Summary string format matches expected two-line layout."""
    Enable = Bool("Enable")
    A = Int("A")
    B = Int("B")
    Result = Int("Result")

    with Program() as logic:
        with Rung(Enable):
            calc(A + B, Result)
        with Rung(Enable):
            with forloop(3):
                out(A)
        with Rung(Enable):
            call("MySub")
        with subroutine("MySub"):
            with Rung(Enable):
                out(A)

    mapping = TagMap(
        {Enable: x[1], A: ds[1], B: ds[2], Result: ds[3]},
        include_system=False,
    )
    bundle = pyrung_to_ladder(logic, mapping)
    assert str(bundle.export_summary) == (
        "Renamed: calc \u2192 math, forloop \u2192 for\n"
        "Added:   next() closing 1 for-loop, return() on 1 subroutine, end() on main"
    )


def test_send_rtu_target_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                send(
                    target=ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=5, com_port="slot0_1"),
                    remote_start="DS1",
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,send(target=ModbusRtuTarget(name="vfd1",com_port="slot0_1",device_id=5),remote_start="DS1",source=DS1,sending=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_receive_rtu_target_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                receive(
                    target=ModbusRtuTarget("vfd1", "/dev/ttyUSB0", device_id=3, com_port="cpu2"),
                    remote_start="DS1",
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,receive(target=ModbusRtuTarget(name="vfd1",com_port="cpu2",device_id=3),remote_start="DS1",dest=DS1,receiving=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_send_modbus_address_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                send(
                    target=ModbusTcpTarget("plc2", "192.168.1.2", device_id=1),
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    source=DS1,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,send(target=ModbusTcpTarget(name="plc2",ip="192.168.1.2",port=502,device_id=1),remote_start=ModbusAddress(address=400001),source=DS1,sending=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_receive_modbus_address_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                receive(
                    target=ModbusTcpTarget("plc2", "192.168.1.2", device_id=1),
                    remote_start=ModbusAddress(0, RegisterType.HOLDING),
                    dest=DS1,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS2,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,receive(target=ModbusTcpTarget(name="plc2",ip="192.168.1.2",port=502,device_id=1),remote_start=ModbusAddress(address=400001),dest=DS1,receiving=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_send_rtu_modbus_address_token():
    _assert_export_main_rows(
        """
        with Program() as p:
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
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,send(target=ModbusRtuTarget(name="vfd1",com_port="slot1_2",device_id=2),remote_start=ModbusAddress(address=400101),source=DS1,sending=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_receive_rtu_modbus_address_token():
    _assert_export_main_rows(
        """
        with Program() as p:
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
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,receive(target=ModbusRtuTarget(name="vfd1",com_port="slot1_2",device_id=2),remote_start=ModbusAddress(address=400101),dest=DS1,receiving=C1,success=C2,error=C3,exception_response=DS2)
        """,
    )


def test_send_block_range_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                send(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    source=DS1..DS3,
                    sending=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS4,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,send(target=ModbusTcpTarget(name="plc2",ip="192.168.1.2",port=502,device_id=1),remote_start="DS1",source=DS1..DS3,sending=C1,success=C2,error=C3,exception_response=DS4)
        """,
    )


def test_receive_block_range_token():
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                receive(
                    target=ModbusTcpTarget("plc2", "192.168.1.2"),
                    remote_start="DS1",
                    dest=DS1..DS3,
                    receiving=C1,
                    success=C2,
                    error=C3,
                    exception_response=DS4,
                )
        """,
        """
        R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,receive(target=ModbusTcpTarget(name="plc2",ip="192.168.1.2",port=502,device_id=1),remote_start="DS1",dest=DS1..DS3,receiving=C1,success=C2,error=C3,exception_response=DS4)
        """,
    )


# --- Nested branch export ---


def test_nested_branch_export_pushes_later_siblings_down():
    """Nested branches keep source order and push later siblings below the nested block."""
    _assert_export_main_rows(
        """
        with Program() as p:
            with Rung(X1):
                with branch(X2):
                    out(Y1)
                    with branch(X3):
                        out(Y2)
                with branch(X4):
                    out(Y3)
        """,
        """
        R,X001,T:X002,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
        ,,|,X003,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
        ,,X004,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y003)
        """,
    )


def test_branch_rendered_height_over_32_rows_raises_clear_error():
    A = Bool("A")
    conditions = [Bool(f"C{i}") for i in range(1, 33)]
    outputs = [Bool(f"O{i}") for i in range(1, 34)]

    with Program(strict=False) as logic:
        with Rung(A):
            out(outputs[0])
            for condition, out_tag in zip(conditions, outputs[1:], strict=True):
                with branch(condition):
                    out(out_tag)

    mapping: dict[Tag | Block, Tag | BlockRange] = {A: x[1]}
    for idx, condition in enumerate(conditions, start=1):
        mapping[condition] = c[idx]
    for idx, out_tag in enumerate(outputs, start=100):
        mapping[out_tag] = c[idx]

    with pytest.raises(LadderExportError) as exc_info:
        pyrung_to_ladder(logic, TagMap(mapping, include_system=False))

    issue = exc_info.value.issues[0]
    assert issue["path"] == "main.rung[0]"
    assert issue["message"] == "Rendered rung exceeds Click's 32-row limit."
    assert isinstance(issue["source_file"], str)
    assert Path(issue["source_file"]).samefile(__file__)
    assert isinstance(issue["source_line"], int)


# ---------------------------------------------------------------------------
# continued() export tests
# ---------------------------------------------------------------------------


class TestContinuedExport:
    """Ladder export for .continued() rungs."""

    def test_continued_rung_blank_marker(self):
        """Continued rung rows use blank marker instead of R."""
        _assert_export_main_rows(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2).continued():
                    out(Y2)
            """,
            """
            R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
            ,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
            """,
        )

    def test_continued_after_branch(self):
        """Continuation after a rung with branches."""
        _assert_export_main_rows(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                    with branch(X3):
                        out(Y2)
                with Rung(X2).continued():
                    out(Y3)
            """,
            """
            R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
            ,,X003,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
            ,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y003)
            """,
        )

    def test_continued_with_own_conditions(self):
        """Continued rung with its own conditions exports correctly."""
        _assert_export_main_rows(
            """
            with Program() as p:
                with Rung(X1):
                    out(Y1)
                with Rung(X2, C1).continued():
                    out(Y2)
            """,
            """
            R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
            ,X002,C1,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y002)
            """,
        )
