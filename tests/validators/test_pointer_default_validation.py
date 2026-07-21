"""Tests for indirect pointer default validation."""

from pyrung import Bool, Rung, call, copy, return_early, subroutine
from pyrung.core import Block, Dint, Int, Program, TagType, calc
from pyrung.core.validation.pointer_default import (
    PTR_DEFAULT_BEFORE_BLOCK_START,
    validate_pointer_defaults,
)


class TestPointerDefaultValidator:
    def test_copy_source_pointer_below_start_flagged(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.code == PTR_DEFAULT_BEFORE_BLOCK_START
        assert finding.target_name == "DS[Ptr]"
        assert finding.pointer_default == 0
        assert finding.block_start == 1
        assert finding.block_end == 100
        assert len(finding.sites) == 1
        assert finding.sites[0].arg_path == "instruction.source"
        assert "set Ptr in 1..100 before indexing DS" in finding.message
        # Location is the compact traceback form, 1-indexed (rung_index 0 → "R1").
        assert finding.sites[0].rung_index == 0
        assert "Main:R1" in finding.message
        assert "Main:R0" not in finding.message

    def test_source_and_dest_pointers_flagged_independently(self):
        ds = Block("DS", TagType.INT, 1, 100)
        dd = Block("DD", TagType.DINT, 1, 100)
        src_ptr = Int("SrcPtr")
        dst_ptr = Int("DstPtr")

        with Program() as prog:
            with Rung():
                copy(ds[src_ptr], dd[dst_ptr])

        report = validate_pointer_defaults(prog)

        assert {finding.target_name for finding in report.findings} == {
            "DD[DstPtr]",
            "DS[SrcPtr]",
        }

    def test_repeated_same_dereference_collapses_to_one_finding(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        dest_a = Dint("DestA")
        dest_b = Dint("DestB")

        with Program() as prog:
            with Rung():
                copy(ds[ptr], dest_a)
            with Rung():
                copy(ds[ptr], dest_b)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 1
        assert report.findings[0].target_name == "DS[Ptr]"
        assert len(report.findings[0].sites) == 2

    def test_zero_based_block_is_clean(self):
        ds = Block("DS", TagType.INT, 0, 100)
        ptr = Int("Ptr")
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_in_range_pointer_default_is_clean(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr", default=1)
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_actual_pointer_with_in_range_default_stays_clean(self):
        ds = Block("DS", TagType.INT, 1, 100)
        base = Int("Base")
        actual_ptr = Int("ActualPtr", default=1)
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                calc(base + 1, actual_ptr)
                copy(ds[actual_ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_unconditional_copy_before_use_suppresses_finding(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        src = Int("Src", default=5)
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(src, ptr)
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_unconditional_calc_before_use_suppresses_finding(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        base = Int("Base")
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                calc(base + 1, ptr)
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_same_rung_unconditional_copy_before_use_suppresses(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        src = Int("Src", default=5)
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(src, ptr)
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_conditional_copy_before_use_still_flags(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        src = Int("Src", default=5)
        dest = Dint("Dest")
        enable = Bool("Enable")

        with Program() as prog:
            with Rung(enable):
                copy(src, ptr)
            with Rung():
                copy(ds[ptr], dest)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 1

    def test_return_early_guarded_write_before_use_suppresses(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        guard = Int("Guard")
        dest = Dint("Dest")

        @subroutine("sub")
        def sub():
            with Rung(guard < 1):
                return_early()
            with Rung():
                copy(5, ptr)
            with Rung():
                copy(ds[ptr], dest)

        with Program() as prog:
            with Rung():
                call(sub)

        report = validate_pointer_defaults(prog)

        assert len(report.findings) == 0

    def test_program_validate_select_and_ignore_support_new_rule(self):
        ds = Block("DS", TagType.INT, 1, 100)
        ptr = Int("Ptr")
        dest = Dint("Dest")

        with Program() as prog:
            with Rung():
                copy(ds[ptr], dest)

        selected = prog.validate(select={PTR_DEFAULT_BEFORE_BLOCK_START})
        ignored = prog.validate(ignore={PTR_DEFAULT_BEFORE_BLOCK_START})

        assert len(selected.findings) == 1
        assert all(f.code == PTR_DEFAULT_BEFORE_BLOCK_START for f in selected.findings)
        assert not any(f.code == PTR_DEFAULT_BEFORE_BLOCK_START for f in ignored.findings)
