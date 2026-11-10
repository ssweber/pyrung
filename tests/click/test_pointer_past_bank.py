"""CLK_PTR_PAST_BANK: a pointer whose known values can run past the end of its bank.

A CLICK writes into the memory past the bank and pyrung drops the write, so the finding
warns.  A pointer with no known bound is never reported.
"""

from __future__ import annotations

import warnings

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, validate_click_program
from pyrung.click.validation import CLK_PTR_PAST_BANK
from pyrung.core import Block, Int, Program, TagType, rung
from pyrung.core.program import copy

_blocks = ClickBlocks()
ds, dd = _blocks.ds, _blocks.dd


def _found(prog, mapping, mode="warn"):
    report = validate_click_program(prog, mapping, mode=mode)
    return [
        f for f in (*report.errors, *report.warnings, *report.hints) if f.code == CLK_PTR_PAST_BANK
    ]


def _copy_through(pointer, bank=dd):
    out = Int("Out")
    with Program(strict=False) as prog:
        with rung():
            copy(bank[pointer], out)
    return prog, TagMap({pointer: ds[1], out: ds[2]}, include_system=False)


_AT = "at main.rung[0].instruction[0](CopyInstruction).instruction.source"


def test_choices_inside_the_bank_are_no_finding():
    ptr = Int("Ptr", choices={1: "v1", 500: "v500", 1000: "v1000"})
    assert _found(*_copy_through(ptr)) == []


def test_choices_past_the_bank_warn():
    ptr = Int("Ptr", choices={0: "v0", 5: "v5", 1001: "v1001", 1531: "v1531"})
    (finding,) = _found(*_copy_through(ptr))
    assert finding.severity == "warning"
    assert finding.message == (
        f"DD[Ptr] {_AT} can reach DD1001. DD ends at DD1000. pyrung reads 0 there."
    )
    assert finding.message.isascii() and finding.suggestion.isascii()


def test_a_copy_write_through_a_dd_pointer_says_what_a_click_does():
    ptr = Int("Ptr", choices={1: "v1", 1531: "v1531"})
    src = Int("Src")
    with Program(strict=False) as prog:
        with rung():
            copy(src, dd[ptr])
    mapping = TagMap({ptr: ds[1], src: ds[2]}, include_system=False)
    (finding,) = _found(prog, mapping)
    assert finding.message == (
        "DD[Ptr] at main.rung[0].instruction[0](CopyInstruction).instruction.dest can "
        "reach DD1531. DD ends at DD1000. A CLICK Copy writes there and raises no flag. "
        "pyrung drops the write."
    )


def test_min_and_max_past_the_bank_warn():
    ptr = Int("Ptr", min=1, max=1010)
    (finding,) = _found(*_copy_through(ptr))
    assert "can reach DD1001" in finding.message


def test_a_pointer_with_no_known_bound_is_no_finding():
    assert _found(*_copy_through(Int("Ptr"))) == []


def test_a_pointer_bounded_by_the_copies_that_write_it_warns():
    ptr = Int("Ptr")
    out = Int("Out")
    go = Int("Go")
    with Program(strict=False) as prog:
        with rung(go == 1):
            copy(5, ptr)
        with rung(go == 2):
            copy(1200, ptr)
        with rung():
            copy(dd[ptr], out)
    mapping = TagMap({ptr: ds[1], out: ds[2], go: ds[3]}, include_system=False)
    (finding,) = _found(prog, mapping)
    assert "can reach DD1200" in finding.message


def test_a_pointer_bounded_inside_the_bank_by_its_writers_is_no_finding():
    ptr = Int("Ptr")
    out = Int("Out")
    with Program(strict=False) as prog:
        with rung():
            copy(900, ptr)
            copy(dd[ptr], out)
    mapping = TagMap({ptr: ds[1], out: ds[2]}, include_system=False)
    assert _found(prog, mapping) == []


def test_a_mapped_block_counts_in_hardware_addresses():
    table = Block("Table", TagType.INT, 1, 100)
    out = Int("Out")

    def report(ptr):
        with Program(strict=False) as prog:
            with rung():
                copy(table[ptr], out)
        mapping = TagMap(
            [table.map_to(ds.select(4401, 4500)), ptr.map_to(ds[1]), out.map_to(ds[2])],
            include_system=False,
        )
        return _found(prog, mapping)

    assert report(Int("Ptr", choices={1: "a", 100: "b"})) == []
    (finding,) = report(Int("Ptr", choices={1: "a", 150: "b"}))
    assert "Table[Ptr]" in finding.message
    assert "can reach DS4550. DS ends at DS4500." in finding.message


def test_a_range_whose_end_passes_the_bank_warns_though_its_start_does_not():
    start = Int("Start", choices={1: "v1", 10: "v10"})
    end = Int("End", choices={20: "v20", 1005: "v1005"})
    dest = Block("Dest", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with rung():
            copy(dd.select(1, 5), dest.select(1, 5))
            copy(dd.select(start, end), dest.select(1, 1000))
    mapping = TagMap({start: ds[1], end: ds[2], dest: ds.select(2001, 4000)}, include_system=False)
    found = _found(prog, mapping)
    assert [f.message for f in found if "indirect range" in f.message] == [
        "The indirect range on DD at "
        "main.rung[0].instruction[1](CopyInstruction).instruction.source "
        "ends at DD1005. DD ends at DD1000. pyrung reads 0 there."
    ]


def test_a_range_that_stays_inside_the_bank_is_no_finding():
    start = Int("Start", choices={1: "v1", 10: "v10"})
    end = Int("End", choices={20: "v20", 1000: "v1000"})
    dest = Block("Dest", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with rung():
            copy(dd.select(start, end), dest.select(1, 1000))
    mapping = TagMap({start: ds[1], end: ds[2], dest: ds.select(2001, 4000)}, include_system=False)
    assert _found(prog, mapping) == []


def test_strict_mode_reports_an_error_but_export_only_warns():
    ptr = Int("Ptr", choices={1: "v1", 1531: "v1531"})
    prog, mapping = _copy_through(ptr)
    assert [f.severity for f in _found(prog, mapping, mode="strict")] == ["error"]
    with pytest.warns(UserWarning, match=CLK_PTR_PAST_BANK):
        pyrung_to_ladder(prog, mapping)


def test_export_of_a_pointer_inside_the_bank_does_not_warn():
    prog, mapping = _copy_through(Int("Ptr", choices={1: "v1", 1000: "v1000"}))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pyrung_to_ladder(prog, mapping)


def test_a_pointer_read_in_a_rung_condition_warns():
    ptr = Int("Ptr", choices={1: "v1", 1500: "v1500"})
    out = Int("Out")
    with Program(strict=False) as prog:
        with rung(dd[ptr] > 0):
            copy(1, out)
    mapping = TagMap({ptr: ds[1], out: ds[2]}, include_system=False)
    (finding,) = _found(prog, mapping)
    assert finding.message == (
        "DD[Ptr] at main.rung[0].condition can reach DD1500. DD ends at DD1000. "
        "pyrung reads 0 there."
    )


def test_a_for_loop_index_with_a_constant_count_is_bounded_by_the_count():
    from pyrung.core import forloop

    out = Int("Out")

    def report(count):
        with Program(strict=False) as prog:
            with rung():
                with forloop(count) as loop:
                    copy(dd[loop.idx], out)
        return _found(prog, TagMap({out: ds[2]}, include_system=False))

    assert report(1000) == []
    (finding,) = report(1100)
    assert "can reach DD1001" in finding.message
    # The index's values stop at 65535, so the largest count builds no 2 ** 31 of them.
    (finding,) = report(2**31 - 1)
    assert "can reach DD1001" in finding.message


def test_a_program_with_no_pointer_builds_no_value_sets(monkeypatch):
    from pyrung.click.validation import pointer_range

    def refuse(self):
        raise AssertionError("value sets built")

    monkeypatch.setattr(pointer_range._PointerBounds, "_build", refuse)
    out = Int("Out")
    with Program(strict=False) as prog:
        with rung():
            copy(ds[1], out)
    assert _found(prog, TagMap({out: ds[2]}, include_system=False)) == []


def test_the_docs_example_with_a_lone_max_warns():
    ptr = Int("Ptr", max=1010)
    (finding,) = _found(*_copy_through(ptr))
    assert "can reach DD1010" in finding.message


def test_a_lone_min_is_no_finding():
    assert _found(*_copy_through(Int("Ptr", min=1))) == []


def test_a_range_wider_than_the_value_cap_still_reports_its_end():
    start = Int("Start", choices={1: "v1"})
    end = Int("End", choices={20: "v20", 4200: "v4200"})
    dest = Block("Dest", TagType.INT, 1, 2000)
    with Program(strict=False) as prog:
        with rung():
            copy(dd.select(start, end), dest.select(1, 1000))
    mapping = TagMap({start: ds[1], end: ds[2], dest: ds.select(2001, 4000)}, include_system=False)
    (finding,) = _found(prog, mapping)
    assert "ends at DD4200" in finding.message
