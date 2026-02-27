"""Tests for Program context manager and full integration.

This tests the DSL syntax:
    with Program() as logic:
        with Rung(condition):
            out(target)
"""

import pytest

from pyrung.core import Block, Bool, Dint, Int, PLCRunner, Real, SystemState, TagType
from tests.conftest import evaluate_condition, evaluate_program, evaluate_rung


class TestProgramContextManager:
    """Test Program context manager for capturing rungs."""

    def test_program_captures_rungs(self):
        """Program context manager captures rungs defined within it."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as prog:
            with Rung(Button):
                out(Light)

        assert len(prog.rungs) == 1

    def test_program_multiple_rungs(self):
        """Program captures multiple rungs in order."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        Light = Bool("Light")
        Motor = Bool("Motor")

        with Program() as prog:
            with Rung(Button):
                out(Light)

            with Rung():  # Unconditional
                out(Motor)

        assert len(prog.rungs) == 2


class TestRungDSL:
    """Test Rung DSL with module-level functions."""

    def test_rung_with_out(self):
        """out() adds OUT instruction."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as prog:
            with Rung(Button):
                out(Light)

        # Verify the rung works
        state = SystemState().with_tags({"Button": True, "Light": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Light"] is True

    def test_rung_with_out_block_range(self):
        """out() accepts .select() and drives all tags in the range."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as prog:
            with Rung(Button):
                out(C.select(1, 3))

        state = SystemState().with_tags({"Button": True, "C1": False, "C2": False, "C3": False})
        state = evaluate_rung(prog.rungs[0], state)
        assert state.tags["C1"] is True
        assert state.tags["C2"] is True
        assert state.tags["C3"] is True

        # Next scan with rung not enabled: OUT coils reset automatically.
        state = state.with_tags({"Button": False})
        state = evaluate_rung(prog.rungs[0], state)
        assert state.tags["C1"] is False
        assert state.tags["C2"] is False
        assert state.tags["C3"] is False

    def test_rung_with_latch(self):
        """latch() adds LATCH instruction."""
        from pyrung.core.program import Program, Rung, latch

        Button = Bool("Button")
        Motor = Bool("Motor")

        with Program() as prog:
            with Rung(Button):
                latch(Motor)

        state = SystemState().with_tags({"Button": True, "Motor": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Motor"] is True

    def test_rung_with_latch_block_range(self):
        """latch() accepts .select() and latches all tags in the range."""
        from pyrung.core.program import Program, Rung, latch

        Button = Bool("Button")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as prog:
            with Rung(Button):
                latch(C.select(10, 12))

        state = SystemState().with_tags({"Button": True, "C10": False, "C11": False, "C12": False})
        state = evaluate_rung(prog.rungs[0], state)
        assert state.tags["C10"] is True
        assert state.tags["C11"] is True
        assert state.tags["C12"] is True

        # LATCH remains on after rung false.
        state = state.with_tags({"Button": False})
        state = evaluate_rung(prog.rungs[0], state)
        assert state.tags["C10"] is True
        assert state.tags["C11"] is True
        assert state.tags["C12"] is True

    def test_rung_with_reset(self):
        """reset() adds RESET instruction."""
        from pyrung.core.program import Program, Rung, reset

        StopButton = Bool("StopButton")
        Motor = Bool("Motor")

        with Program() as prog:
            with Rung(StopButton):
                reset(Motor)

        state = SystemState().with_tags({"StopButton": True, "Motor": True})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Motor"] is False

    def test_rung_with_reset_block_range(self):
        """reset() accepts .select() and resets all tags in the range."""
        from pyrung.core.program import Program, Rung, reset

        StopButton = Bool("StopButton")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as prog:
            with Rung(StopButton):
                reset(C.select(20, 22))

        state = SystemState().with_tags({"StopButton": True, "C20": True, "C21": True, "C22": True})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["C20"] is False
        assert new_state.tags["C21"] is False
        assert new_state.tags["C22"] is False

    def test_rung_with_copy(self):
        """copy() adds COPY instruction."""
        from pyrung.core.program import Program, Rung, copy

        Button = Bool("Button")
        Step = Int("Step")

        with Program() as prog:
            with Rung(Button):
                copy(5, Step)

        state = SystemState().with_tags({"Button": True, "Step": 0})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Step"] == 5

    def test_rung_with_pack_bits(self):
        """pack_bits() adds PACK_BITS instruction."""
        from pyrung.core.program import Program, Rung, pack_bits

        Button = Bool("Button")
        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")

        with Program() as prog:
            with Rung(Button):
                pack_bits(C.select(1, 4), Dest)

        state = SystemState().with_tags(
            {"Button": True, "C1": True, "C2": False, "C3": True, "C4": False, "Dest": 0}
        )
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Dest"] == 0b0101

    def test_rung_with_pack_words(self):
        """pack_words() adds PACK_WORDS instruction with low-word-first ordering."""
        from pyrung.core.program import Program, Rung, pack_words

        Button = Bool("Button")
        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Dint("Dest")

        with Program() as prog:
            with Rung(Button):
                pack_words(DS.select(1, 2), Dest)

        state = SystemState().with_tags({"Button": True, "DS1": 0x1234, "DS2": 0x5678})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Dest"] == 0x56781234

    def test_rung_with_pack_text(self):
        """pack_text() parses a CHAR range into a numeric destination."""
        from pyrung.core.program import Program, Rung, pack_text

        Button = Bool("Button")
        CH = Block("CH", TagType.CHAR, 1, 100)
        Dest = Int("Dest")

        with Program() as prog:
            with Rung(Button):
                pack_text(CH.select(1, 3), Dest)

        state = SystemState().with_tags({"Button": True, "CH1": "1", "CH2": "2", "CH3": "3"})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Dest"] == 123

    def test_rung_with_unpack_to_bits(self):
        """unpack_to_bits() adds UNPACK_TO_BITS instruction."""
        from pyrung.core.program import Program, Rung, unpack_to_bits

        Button = Bool("Button")
        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as prog:
            with Rung(Button):
                unpack_to_bits(Source, C.select(1, 4))

        state = SystemState().with_tags({"Button": True, "Source": 0b1010})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["C1"] is False
        assert new_state.tags["C2"] is True
        assert new_state.tags["C3"] is False
        assert new_state.tags["C4"] is True

    def test_rung_with_unpack_to_words(self):
        """unpack_to_words() adds UNPACK_TO_WORDS instruction."""
        from pyrung.core.program import Program, Rung, unpack_to_words

        Button = Bool("Button")
        Source = Real("Source")
        DH = Block("DH", TagType.WORD, 1, 100)

        with Program() as prog:
            with Rung(Button):
                unpack_to_words(Source, DH.select(1, 2))

        state = SystemState().with_tags({"Button": True, "Source": 1.0})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["DH1"] == 0x0000
        assert new_state.tags["DH2"] == 0x3F80

    def test_rung_with_tilde_normally_closed_condition(self):
        """~Bool creates normally closed condition."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as prog:
            with Rung(~Button):  # Light on when Button is OFF
                out(Light)

        # Button OFF -> Light ON
        state = SystemState().with_tags({"Button": False, "Light": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Light"] is True

        # Button ON -> Light OFF
        state = SystemState().with_tags({"Button": True, "Light": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Light"] is False

    def test_rung_rejects_inverted_int_expression_as_condition(self):
        """~Int creates an expression and is invalid as a direct rung condition."""
        from pyrung.core.program import Program, Rung, out

        Step = Int("Step")
        Light = Bool("Light")

        with pytest.raises(TypeError, match="Expected Condition or Tag"):
            with Program():
                with Rung(~Step):
                    out(Light)


class TestSearchDSL:
    """Test SEARCH integration via Program/Rung DSL."""

    def test_rung_with_search_instruction(self):
        from pyrung.core.program import Program, Rung, search

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as prog:
            with Rung(Enable):
                search("==", 20, DS.select(1, 3), Result, Found)

        state = SystemState().with_tags(
            {"Enable": True, "DS1": 10, "DS2": 20, "DS3": 30, "Result": 0, "Found": False}
        )
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Result"] == 2
        assert new_state.tags["Found"] is True

    def test_search_then_copy_by_pointer_pattern(self):
        from pyrung.core.program import Program, Rung, copy, search

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)
        Pointer = Int("Pointer")
        Found = Bool("Found")

        with Program() as prog:
            with Rung(Enable):
                search("==", 30, DS.select(1, 5), Pointer, Found)
            with Rung(Found):
                copy(DS[Pointer], DD[1])

        state = SystemState().with_tags(
            {
                "Enable": True,
                "DS1": 10,
                "DS2": 20,
                "DS3": 30,
                "DS4": 40,
                "DS5": 50,
                "Pointer": 0,
                "Found": False,
                "DD1": 0,
            }
        )
        new_state = evaluate_program(prog, state)

        assert new_state.tags["Pointer"] == 3
        assert new_state.tags["Found"] is True
        assert new_state.tags["DD1"] == 30

    def test_search_continuous_progression_across_runner_steps(self):
        from pyrung.core.program import Program, Rung, search

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search("==", 7, DS.select(1, 4), Result, Found, continuous=True)

        runner = PLCRunner(logic)
        runner.patch(
            {"Enable": True, "DS1": 7, "DS2": 0, "DS3": 7, "DS4": 0, "Result": 0, "Found": False}
        )

        runner.step()
        assert runner.current_state.tags["Result"] == 1
        assert runner.current_state.tags["Found"] is True

        runner.step()
        assert runner.current_state.tags["Result"] == 3
        assert runner.current_state.tags["Found"] is True

        runner.step()
        assert runner.current_state.tags["Result"] == -1
        assert runner.current_state.tags["Found"] is False

    def test_search_reverse_range_order(self):
        from pyrung.core.program import Program, Rung, search

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as prog:
            with Rung(Enable):
                search("==", 5, DS.select(1, 4).reverse(), Result, Found)

        state = SystemState().with_tags(
            {"Enable": True, "DS1": 5, "DS2": 0, "DS3": 5, "DS4": 0, "Result": 0, "Found": False}
        )
        new_state = evaluate_program(prog, state)

        assert new_state.tags["Result"] == 3
        assert new_state.tags["Found"] is True

    def test_search_text_end_to_end(self):
        from pyrung.core.program import Program, Rung, search

        Enable = Bool("Enable")
        CH = Block("CH", TagType.CHAR, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search("==", "ADC", CH.select(1, 6), Result, Found)

        runner = PLCRunner(logic)
        runner.patch(
            {
                "Enable": True,
                "CH1": "A",
                "CH2": "D",
                "CH3": "C",
                "CH4": "X",
                "Result": 0,
                "Found": False,
            }
        )
        runner.step()

        assert runner.current_state.tags["Result"] == 1
        assert runner.current_state.tags["Found"] is True


class TestProgramDecorator:
    """Test @program decorator."""

    def test_program_decorator(self):
        """@program decorator captures rungs from function."""
        from pyrung.core.program import Program, Rung, out, program

        Button = Bool("Button")
        Light = Bool("Light")

        @program
        def my_logic():
            with Rung(Button):
                out(Light)

        # my_logic is now a Program
        assert isinstance(my_logic, Program)
        assert len(my_logic.rungs) == 1


class TestStrictDslControlFlowGuard:
    """Tests for strict AST control-flow guard in Program/@program/@subroutine."""

    def test_context_manager_rejects_if(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program, Rung, out

        Enable = Bool("Enable")
        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="if/elif/else"):
            with Program():
                if True:
                    with Rung(Enable):
                        out(Light)

    def test_context_manager_rejects_and_or(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program, Rung, out

        A = Bool("A")
        B = Bool("B")
        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="all_of\\(\\).*any_of\\(\\)"):
            with Program():
                with Rung(A and B):
                    out(Light)

    def test_context_manager_rejects_not(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program, Rung, out

        A = Bool("A")
        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="Use `~Tag`"):
            with Program():
                with Rung(not A):  # type: ignore[arg-type]
                    out(Light)

    def test_context_manager_rejects_for_loop(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program, Rung, out

        A = Bool("A")
        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="for/while"):
            with Program():
                for _ in range(1):
                    with Rung(A):
                        out(Light)

    def test_context_manager_rejects_try_except(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program

        with pytest.raises(ForbiddenControlFlowError, match="try/except"):
            with Program():
                try:
                    pass
                except RuntimeError:
                    pass

    def test_context_manager_rejects_import(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program

        with pytest.raises(ForbiddenControlFlowError, match="Move imports outside"):
            with Program():
                from pyrung.core import out

                print(out)

    def test_context_manager_rejects_comprehension(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program

        with pytest.raises(ForbiddenControlFlowError, match="Build tag collections outside"):
            with Program():
                print([n for n in (1, 2, 3)])

    def test_context_manager_rejects_nested_function_definition(self):
        from pyrung.core.program import ForbiddenControlFlowError, Program

        with pytest.raises(ForbiddenControlFlowError, match="Define functions and classes outside"):
            with Program():

                def helper():
                    pass

                helper()

    def test_program_decorator_rejects_assignment(self):
        from pyrung.core.program import ForbiddenControlFlowError, Rung, out, program

        Enable = Bool("Enable")
        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="assignment"):

            @program
            def my_logic():
                value = 1
                if value:
                    with Rung(Enable):
                        out(Light)

    def test_allowed_with_expr_call_and_pass_are_accepted(self):
        from pyrung.core.program import Program, Rung, out

        Enable = Bool("Enable")
        Light = Bool("Light")

        calls: list[str] = []

        def marker() -> None:
            calls.append("seen")

        with Program() as logic:
            pass
            marker()
            with Rung(Enable):
                out(Light)

        assert calls == ["seen"]
        assert len(logic.rungs) == 1

    def test_program_strict_false_opt_out(self):
        from pyrung.core.program import Program, Rung, out

        Enable = Bool("Enable")
        Light = Bool("Light")

        with Program(strict=False) as logic:
            value = 1
            if value:
                with Rung(Enable):
                    out(Light)

        assert len(logic.rungs) == 1

    def test_program_decorator_strict_false_opt_out(self):
        from pyrung.core.program import Program, Rung, out, program

        Enable = Bool("Enable")
        Light = Bool("Light")

        @program(strict=False)
        def my_logic():
            value = 1
            if value:
                with Rung(Enable):
                    out(Light)

        assert isinstance(my_logic, Program)
        assert len(my_logic.rungs) == 1

    def test_subroutine_decorator_rejects_assignment(self):
        from pyrung.core.program import ForbiddenControlFlowError, Rung, out, subroutine

        Light = Bool("Light")

        with pytest.raises(ForbiddenControlFlowError, match="assignment"):

            @subroutine("bad_sub")
            def bad_sub():
                value = 1
                if value:
                    with Rung():
                        out(Light)

    def test_subroutine_decorator_strict_false_opt_out(self):
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Enable = Bool("Enable")
        Light = Bool("Light")

        @subroutine("init", strict=False)
        def init_sequence():
            value = 1
            if value:
                with Rung():
                    out(Light)

        with Program() as logic:
            with Rung(Enable):
                call(init_sequence)

        runner = PLCRunner(logic)
        runner.patch({"Enable": True, "Light": False})
        runner.step()
        assert runner.current_state.tags["Light"] is True

    def test_guard_warns_and_skips_when_source_is_unavailable(self, monkeypatch):
        import importlib

        program_module = importlib.import_module("pyrung.core.program")
        validation_module = importlib.import_module("pyrung.core.program.validation")

        Enable = Bool("Enable")
        Light = Bool("Light")

        def _raise_source_error(_obj: object) -> tuple[list[str], int]:
            raise OSError("source unavailable")

        monkeypatch.setattr(validation_module.inspect, "getsourcelines", _raise_source_error)

        with pytest.warns(RuntimeWarning, match="Unable to perform strict DSL control-flow check"):
            with program_module.Program() as logic:
                with program_module.Rung(Enable):
                    program_module.out(Light)

        assert len(logic.rungs) == 1


class TestPublicExports:
    """Test public core exports."""

    def test_pack_unpack_exports(self):
        from pyrung.core import pack_bits, pack_text, pack_words, unpack_to_bits, unpack_to_words

        assert callable(pack_bits)
        assert callable(pack_text)
        assert callable(pack_words)
        assert callable(unpack_to_bits)
        assert callable(unpack_to_words)

    def test_search_export(self):
        from pyrung.core import search

        assert callable(search)

    def test_forbidden_control_flow_error_export(self):
        from pyrung.core import ForbiddenControlFlowError

        assert issubclass(ForbiddenControlFlowError, RuntimeError)

    def test_copy_modifier_exports(self):
        from pyrung.core import as_ascii, as_binary, as_text, as_value

        assert callable(as_value)
        assert callable(as_ascii)
        assert callable(as_text)
        assert callable(as_binary)

    def test_no_copy_text_or_unpack_text_exports(self):
        import pyrung.core as core

        assert not hasattr(core, "copy_text")
        assert not hasattr(core, "unpack_text")


class TestCastingReferenceExamples:
    """Program-level tests based on Click PLC casting behavior."""

    def test_example1_bypass_sign_priority_via_dh(self):
        """DS=-1 -> DD keeps sign, but DS->DH->DD preserves 0x0000FFFF."""
        from pyrung.core.program import Program, Rung, copy

        DS = Block("DS", TagType.INT, 1, 100)
        DH = Block("DH", TagType.WORD, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        with Program() as prog:
            with Rung():
                copy(-1, DS[1])
            with Rung():
                copy(DS[1], DD[1])
            with Rung():
                copy(DS[1], DH[1])
                copy(DH[1], DD[2])

        new_state = evaluate_program(prog, SystemState())

        assert new_state.tags["DS1"] == -1
        assert new_state.tags["DD1"] == -1  # 0xFFFFFFFF sign-preserved
        assert new_state.tags["DH1"] == 0xFFFF
        assert new_state.tags["DD2"] == 65535  # 0x0000FFFF preserved via unsigned hop

    def test_example2_unpack_preserves_32_bit_pattern(self):
        """DD -> DS clamps, while DD -> unpack DH -> pack DD preserves pattern."""
        from pyrung.core.program import Program, Rung, copy, pack_words, unpack_to_words

        DS = Block("DS", TagType.INT, 1, 100)
        DH = Block("DH", TagType.WORD, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        with Program() as prog:
            with Rung():
                copy(305_419_896, DD[1])  # 0x12345678
            with Rung():
                copy(DD[1], DS[1])  # Range-limited for INT destination
            with Rung():
                unpack_to_words(DD[1], DH.select(1, 2))
            with Rung():
                pack_words(DH.select(1, 2), DD[2])

        new_state = evaluate_program(prog, SystemState())

        assert new_state.tags["DD1"] == 305_419_896
        assert new_state.tags["DS1"] == 32767  # clamped INT max
        assert new_state.tags["DH1"] == 0x5678  # low word first
        assert new_state.tags["DH2"] == 0x1234  # high word second
        assert new_state.tags["DD2"] == 305_419_896  # packed back losslessly


class TestCopyAndMathReferenceExamples:
    """Program-level tests based on CLICK reference examples."""

    def test_copy_block_example_with_oneshot_behavior(self):
        """copy_block.md: one-shot block copy on OFF->ON transition behavior."""
        from pyrung.core.program import Program, Rung, blockcopy

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 1000)

        with Program() as logic:
            with Rung(Enable):
                blockcopy(DS.select(1, 3), DS.select(501, 503), oneshot=True)

        runner = PLCRunner(logic)
        runner.patch(
            {
                "Enable": False,
                "DS1": 10,
                "DS2": 20,
                "DS3": 30,
                "DS501": 0,
                "DS502": 0,
                "DS503": 0,
            }
        )
        runner.step()

        # Rising edge copies once
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["DS501"] == 10
        assert runner.current_state.tags["DS502"] == 20
        assert runner.current_state.tags["DS503"] == 30

        # While still true, oneshot blocks additional copies
        runner.patch({"DS1": 100, "DS2": 200, "DS3": 300})
        runner.step()
        assert runner.current_state.tags["DS501"] == 10
        assert runner.current_state.tags["DS502"] == 20
        assert runner.current_state.tags["DS503"] == 30

        # False then true re-arms oneshot and copies again
        runner.patch({"Enable": False})
        runner.step()
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["DS501"] == 100
        assert runner.current_state.tags["DS502"] == 200
        assert runner.current_state.tags["DS503"] == 300

    def test_fill_example_with_oneshot_behavior(self):
        """copy_fill.md: one-shot fill loads constant across destination range."""
        from pyrung.core.program import Program, Rung, fill

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 1000)

        with Program() as logic:
            with Rung(Enable):
                fill(1000, DS.select(501, 503), oneshot=True)

        runner = PLCRunner(logic)
        runner.patch({"Enable": False, "DS501": 1, "DS502": 2, "DS503": 3})
        runner.step()

        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["DS501"] == 1000
        assert runner.current_state.tags["DS502"] == 1000
        assert runner.current_state.tags["DS503"] == 1000

        # Change values while rung remains true; oneshot blocks re-fill
        runner.patch({"DS501": 5, "DS502": 6, "DS503": 7})
        runner.step()
        assert runner.current_state.tags["DS501"] == 5
        assert runner.current_state.tags["DS502"] == 6
        assert runner.current_state.tags["DS503"] == 7

        # Retrigger after OFF->ON
        runner.patch({"Enable": False})
        runner.step()
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["DS501"] == 1000
        assert runner.current_state.tags["DS502"] == 1000
        assert runner.current_state.tags["DS503"] == 1000

    def test_pack_copy_example_c2_to_c7_into_dh(self):
        """copy_pack.md: pack range of C bits into a DH word destination."""
        from pyrung.core.program import Program, Rung, pack_bits

        Enable = Bool("Enable")
        C = Block("C", TagType.BOOL, 1, 100)
        DH = Block("DH", TagType.WORD, 1, 100)

        with Program() as prog:
            with Rung(Enable):
                pack_bits(C.select(2, 7), DH[10])

        # C2 is bit0, C3 bit1 ... C7 bit5
        state = SystemState().with_tags(
            {
                "Enable": True,
                "C2": False,
                "C3": True,
                "C4": True,
                "C5": False,
                "C6": True,
                "C7": False,
            }
        )
        new_state = evaluate_program(prog, state)
        assert new_state.tags["DH10"] == 0b010110

    def test_unpack_copy_example_dh_to_c_range(self):
        """copy_unpack.md: unpack DH source into destination C bit range."""
        from pyrung.core.program import Program, Rung, unpack_to_bits

        Enable = Bool("Enable")
        C = Block("C", TagType.BOOL, 1, 100)
        DH = Block("DH", TagType.WORD, 1, 100)

        with Program() as prog:
            with Rung(Enable):
                unpack_to_bits(DH[2], C.select(15, 19))

        # bit pattern for lower 5 bits: 1,0,1,1,0
        state = SystemState().with_tags({"Enable": True, "DH2": 0b01101})
        new_state = evaluate_program(prog, state)

        assert new_state.tags["C15"] is True
        assert new_state.tags["C16"] is False
        assert new_state.tags["C17"] is True
        assert new_state.tags["C18"] is True
        assert new_state.tags["C19"] is False

    def test_math_hex_shift_rotate_example(self):
        """math_hex.md: RSH/RRO example with DH1=0x45B1."""
        from pyrung.core import rro, rsh
        from pyrung.core.program import Program, Rung, calc

        Enable = Bool("Enable")
        DH = Block("DH", TagType.WORD, 1, 200)

        with Program() as prog:
            with Rung(Enable):
                calc(rsh(DH[1], 1), DH[2], mode="hex")
                calc(rro(DH[1], 1), DH[3], mode="hex")

        state = SystemState().with_tags({"Enable": True, "DH1": 0x45B1})
        new_state = evaluate_program(prog, state)

        assert new_state.tags["DH2"] == 0x22D8
        assert new_state.tags["DH3"] == 0xA2D8


class TestClickPrebuiltProgramIntegration:
    """Program-level integration using prebuilt Click blocks."""

    def test_click_input_to_output_rung_uses_canonical_names(self):
        """x/y prebuilt blocks execute in Program with Click canonical tag names."""
        from pyrung.click import x, y
        from pyrung.core.program import Program, Rung, out

        with Program() as prog:
            with Rung(x[1]):
                out(y[1])

        state = SystemState().with_tags({"X001": True, "Y001": False})
        new_state = evaluate_program(prog, state)
        assert new_state.tags["Y001"] is True

    def test_click_sparse_window_pack_bits_skips_invalid_addresses(self):
        """x.select(1, 21) packs 17 valid bits (1-16 and 21), not raw 21 addresses."""
        from pyrung.click import dd, x
        from pyrung.core.program import Program, Rung, pack_bits

        with Program() as prog:
            with Rung():
                pack_bits(x.select(1, 21), dd[1])

        # Only X021 is ON. If sparse gaps are skipped, this is packed as bit 16.
        state = SystemState().with_tags({"X021": True})
        new_state = evaluate_program(prog, state)
        assert new_state.tags["DD1"] == (1 << 16)


class TestPLCRunnerIntegration:
    """Test full integration with PLCRunner."""

    def test_runner_executes_program(self):
        """PLCRunner evaluates program logic each scan."""
        from pyrung.core.program import Program, Rung, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLCRunner(logic)
        runner.patch({"Button": False, "Light": False})
        runner.step()  # Apply initial state

        # Button off -> Light off
        assert runner.current_state.tags["Light"] is False

        # Press button
        runner.patch({"Button": True})
        runner.step()

        assert runner.current_state.tags["Light"] is True

        # Release button -> Light off (OUT resets)
        runner.patch({"Button": False})
        runner.step()

        assert runner.current_state.tags["Light"] is False

    def test_runner_with_latch_reset_circuit(self):
        """Classic start/stop latch circuit."""
        from pyrung.core.program import Program, Rung, latch, reset

        StartButton = Bool("StartButton")
        StopButton = Bool("StopButton")
        MotorRunning = Bool("MotorRunning")

        with Program() as logic:
            # Start rung: latch motor when start pressed
            with Rung(StartButton):
                latch(MotorRunning)

            # Stop rung: reset motor when stop pressed
            with Rung(StopButton):
                reset(MotorRunning)

        runner = PLCRunner(logic)
        runner.patch({"StartButton": False, "StopButton": False, "MotorRunning": False})
        runner.step()

        assert runner.current_state.tags["MotorRunning"] is False

        # Press start
        runner.patch({"StartButton": True})
        runner.step()
        assert runner.current_state.tags["MotorRunning"] is True

        # Release start - motor stays on (latched)
        runner.patch({"StartButton": False})
        runner.step()
        assert runner.current_state.tags["MotorRunning"] is True

        # Press stop
        runner.patch({"StopButton": True})
        runner.step()
        assert runner.current_state.tags["MotorRunning"] is False

    def test_runner_with_step_sequencer(self):
        """Simple step sequencer using copy."""
        from pyrung.core.program import Program, Rung, copy, out

        Step = Int("Step")
        NextButton = Bool("NextButton")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        with Program() as logic:
            # Light1 on when Step == 0
            with Rung(Step == 0):
                out(Light1)

            # Light2 on when Step == 1
            with Rung(Step == 1):
                out(Light2)

            # Advance step when button pressed (simplified - no edge detection)
            with Rung(NextButton, Step == 0):
                copy(1, Step, oneshot=True)

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "NextButton": False, "Light1": False, "Light2": False})
        runner.step()

        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is False

        # Press next button - Step changes to 1 within this scan
        runner.patch({"NextButton": True})
        runner.step()

        # Step changed to 1
        assert runner.current_state.tags["Step"] == 1
        # Light1 is still True because Rung1 (Step==0) was true at time of evaluation
        # Light2 is still False because Rung2 (Step==1) was false at time of evaluation
        # This is correct PLC behavior - rungs see state at time THEY evaluate
        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is False

        # Next scan: Now Step==0 is false, Step==1 is true
        runner.patch({"NextButton": False})
        runner.step()

        assert runner.current_state.tags["Light1"] is False  # Rung went false, OUT reset
        assert runner.current_state.tags["Light2"] is True  # Rung now true


class TestBranch:
    """Test branch() for parallel paths within a rung."""

    def test_branch_executes_when_parent_and_branch_conditions_true(self):
        """Branch executes when parent rung AND branch conditions are true."""
        from pyrung.core.program import Program, Rung, branch, out

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light1)
                with branch(AutoMode):
                    out(Light2)

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": True, "Light1": False, "Light2": False})
        runner.step()

        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is True

    def test_branch_not_executed_when_parent_false(self):
        """Branch does not execute when parent rung condition is false."""
        from pyrung.core.program import Program, Rung, branch, out

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light1)
                with branch(AutoMode):
                    out(Light2)

        runner = PLCRunner(logic)
        runner.patch({"Step": 1, "AutoMode": True, "Light1": False, "Light2": False})
        runner.step()

        # Parent condition false -> neither light on
        assert runner.current_state.tags["Light1"] is False
        assert runner.current_state.tags["Light2"] is False

    def test_branch_not_executed_when_branch_condition_false(self):
        """Branch does not execute when branch condition is false."""
        from pyrung.core.program import Program, Rung, branch, out

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light1)
                with branch(AutoMode):
                    out(Light2)

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": False, "Light1": False, "Light2": False})
        runner.step()

        # Parent true, branch false -> only Light1
        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is False

    def test_branch_with_copy_oneshot(self):
        """Branch can contain copy with oneshot."""
        from pyrung.core.program import Program, Rung, branch, copy, out

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light)
                with branch(AutoMode):
                    copy(1, Step, oneshot=True)

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": True, "Light": False})
        runner.step()

        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["Step"] == 1

        # Next scan: Step==0 is now false
        runner.step()
        assert runner.current_state.tags["Light"] is False  # Rung false now

    def test_branch_enable_is_snapshotted_before_item_execution(self):
        """Branch enable is computed before item execution and applied for this whole rung scan."""
        from pyrung.core.program import Program, Rung, branch, copy, out

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light1)
                # This write happens before the branch item in source order.
                # Branch should still use its precomputed enable from scan start.
                copy(True, AutoMode)
                with branch(AutoMode):
                    out(Light2)

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": False, "Light1": False, "Light2": False})
        runner.step()

        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["AutoMode"] is True
        assert runner.current_state.tags["Light2"] is False

        # Next scan sees AutoMode already true at scan start, so branch executes.
        runner.step()
        assert runner.current_state.tags["Light2"] is True

    def test_branch_outside_rung_raises_error(self):
        """branch() outside Rung context raises RuntimeError."""
        import pytest

        from pyrung.core.program import Program, branch, out

        Light = Bool("Light")

        with pytest.raises(RuntimeError, match="must be called inside a Rung"):
            with Program():
                with branch():  # No parent rung!
                    out(Light)

    def test_multiple_branches_in_rung(self):
        """Multiple branches can exist in a single rung."""
        from pyrung.core.program import Program, Rung, branch, out

        Step = Int("Step")
        Mode1 = Bool("Mode1")
        Mode2 = Bool("Mode2")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")
        Light3 = Bool("Light3")

        with Program() as logic:
            with Rung(Step == 0):
                out(Light1)
                with branch(Mode1):
                    out(Light2)
                with branch(Mode2):
                    out(Light3)

        runner = PLCRunner(logic)
        runner.patch(
            {
                "Step": 0,
                "Mode1": True,
                "Mode2": False,
                "Light1": False,
                "Light2": False,
                "Light3": False,
            }
        )
        runner.step()

        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is True
        assert runner.current_state.tags["Light3"] is False

    def test_branch_get_combined_condition_includes_parent_and_branch(self):
        """Branch's _get_combined_condition() should include parent AND branch conditions."""
        from pyrsistent import pmap

        from pyrung.core.program import Program, Rung, branch
        from pyrung.core.state import SystemState

        Step = Int("Step")
        Mode = Bool("Mode")

        captured_condition = None

        # Capture the combined condition from inside a branch
        def capture_condition():
            nonlocal captured_condition
            from pyrung.core.program.context import _require_rung_context

            ctx = _require_rung_context("test")
            captured_condition = ctx._rung._get_combined_condition()

        with Program():
            with Rung(Step == 0):
                with branch(Mode):
                    capture_condition()

        # Helper to create state with given tag values
        def state(**tags):
            return SystemState(scan_id=1, timestamp=0.0, tags=pmap(tags), memory=pmap())

        # Verify the condition was captured
        assert captured_condition is not None

        # Should only be true when BOTH conditions are true
        assert evaluate_condition(captured_condition, state(Step=0, Mode=True)) is True
        assert (
            evaluate_condition(captured_condition, state(Step=1, Mode=True)) is False
        )  # Parent false
        assert (
            evaluate_condition(captured_condition, state(Step=0, Mode=False)) is False
        )  # Branch false
        assert (
            evaluate_condition(captured_condition, state(Step=1, Mode=False)) is False
        )  # Both false

    def test_branch_effects_apply_before_later_call_in_same_rung(self):
        """Branch side effects apply before later same-rung instructions in source order."""
        from pyrung.core.program import Program, Rung, branch, call, copy, out, subroutine

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        BranchDone = Bool("BranchDone")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with subroutine("sub"):
                with Rung(Step == 1):
                    out(SubLight)

            with Rung(Step == 0):
                with branch(AutoMode):
                    out(BranchDone)
                    copy(1, Step, oneshot=True)
                call("sub")

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": True, "BranchDone": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Step"] == 1
        assert runner.current_state.tags["BranchDone"] is True
        assert runner.current_state.tags["SubLight"] is True


class TestSubroutineAndCall:
    """Test subroutine() and call() for modular program structure."""

    def test_subroutine_defined_and_called(self):
        """Subroutine is defined and executed when called."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():  # Unconditional
                    out(SubLight)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is True

    def test_subroutine_not_called_when_rung_false(self):
        """Subroutine is not executed when calling rung is false."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    out(SubLight)

        runner = PLCRunner(logic)
        runner.patch({"Button": False, "Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is False
        assert runner.current_state.tags["SubLight"] is False

    def test_subroutine_with_conditional_rung(self):
        """Subroutine rungs have their own conditions."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Step = Int("Step")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("my_sub")

            with subroutine("my_sub"):
                with Rung(Step == 1):  # Only executes when Step==1
                    out(SubLight)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Step": 0, "Light": False, "SubLight": False})
        runner.step()

        # Sub called but its rung condition is false
        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is False

        # Change Step to 1
        runner.patch({"Step": 1})
        runner.step()

        assert runner.current_state.tags["SubLight"] is True

    def test_call_undefined_subroutine_raises_error(self):
        """Calling undefined subroutine raises error at runtime."""
        import pytest

        from pyrung.core.program import Program, Rung, call, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call("nonexistent")

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Light": False})

        with pytest.raises(KeyError, match="nonexistent"):
            runner.step()

    def test_subroutine_not_executed_directly(self):
        """Subroutine rungs are not executed in main scan unless called."""
        from pyrung.core.program import Program, Rung, out, subroutine

        Light = Bool("Light")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung():  # Unconditional
                out(Light)

            with subroutine("my_sub"):
                with Rung():
                    out(SubLight)

        runner = PLCRunner(logic)
        runner.patch({"Light": False, "SubLight": False})
        runner.step()

        # Main rung executed, subroutine NOT called
        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is False

    def test_return_early_exits_subroutine_early(self):
        """return_early() exits subroutine immediately and skips remaining rungs."""
        from pyrung.core.program import Program, Rung, call, out, return_early, subroutine

        Run = Bool("Run")
        First = Bool("First")
        Second = Bool("Second")
        Third = Bool("Third")

        with Program() as logic:
            with Rung(Run):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    out(First)
                    return_early()
                    out(Second)

                with Rung():
                    out(Third)

        runner = PLCRunner(logic)
        runner.patch({"Run": True, "First": False, "Second": False, "Third": False})
        runner.step()

        assert runner.current_state.tags["First"] is True
        assert runner.current_state.tags["Second"] is False
        assert runner.current_state.tags["Third"] is False

    def test_return_early_only_exits_current_subroutine(self):
        """return_early() in a nested call should not abort the caller subroutine."""
        from pyrung.core.program import Program, Rung, call, out, return_early, subroutine

        Run = Bool("Run")
        CallerDone = Bool("CallerDone")
        CalleeBefore = Bool("CalleeBefore")
        CalleeAfter = Bool("CalleeAfter")

        with Program() as logic:
            with Rung(Run):
                call("caller")

            with subroutine("caller"):
                with Rung():
                    call("callee")
                    out(CallerDone)

            with subroutine("callee"):
                with Rung():
                    out(CalleeBefore)
                    return_early()
                    out(CalleeAfter)

        runner = PLCRunner(logic)
        runner.patch(
            {
                "Run": True,
                "CallerDone": False,
                "CalleeBefore": False,
                "CalleeAfter": False,
            }
        )
        runner.step()

        assert runner.current_state.tags["CallerDone"] is True
        assert runner.current_state.tags["CalleeBefore"] is True
        assert runner.current_state.tags["CalleeAfter"] is False

    def test_return_early_outside_subroutine_raises(self):
        """return_early() is only valid while defining a subroutine."""
        import pytest

        from pyrung.core.program import Program, Rung, return_early

        Run = Bool("Run")

        with pytest.raises(RuntimeError, match="inside a subroutine"):
            with Program():
                with Rung(Run):
                    return_early()


class TestSubroutineDecorator:
    """Test @subroutine('name') decorator syntax."""

    def test_decorator_subroutine_defined_and_called(self):
        """Decorated subroutine is auto-registered and executed when called."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        @subroutine("init")
        def init_sequence():
            with Rung():
                out(SubLight)

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call(init_sequence)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is True

    def test_decorator_subroutine_not_executed_directly(self):
        """Decorated subroutine rungs are not in the main scan."""
        from pyrung.core.program import Program, Rung, out, subroutine

        Light = Bool("Light")
        SubLight = Bool("SubLight")

        @subroutine("my_sub")
        def my_sub():
            with Rung():
                out(SubLight)

        with Program() as logic:
            with Rung():
                out(Light)

        # Subroutine was never call()'d, so not registered
        runner = PLCRunner(logic)
        runner.patch({"Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is False

    def test_decorator_subroutine_not_called_when_rung_false(self):
        """Decorated subroutine is not executed when calling rung is false."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        @subroutine("my_sub")
        def my_sub():
            with Rung():
                out(SubLight)

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call(my_sub)

        runner = PLCRunner(logic)
        runner.patch({"Button": False, "Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is False
        assert runner.current_state.tags["SubLight"] is False

    def test_decorator_subroutine_with_conditional_rung(self):
        """Decorated subroutine rungs have their own conditions."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Step = Int("Step")
        Light = Bool("Light")
        SubLight = Bool("SubLight")

        @subroutine("my_sub")
        def my_sub():
            with Rung(Step == 1):
                out(SubLight)

        with Program() as logic:
            with Rung(Button):
                out(Light)
                call(my_sub)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Step": 0, "Light": False, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["Light"] is True
        assert runner.current_state.tags["SubLight"] is False

        runner.patch({"Step": 1})
        runner.step()

        assert runner.current_state.tags["SubLight"] is True

    def test_decorator_outside_program_raises(self):
        """call() with decorated subroutine outside Program raises error."""
        import pytest

        from pyrung.core.program import Rung, call, out, subroutine

        Button = Bool("Button")
        Light = Bool("Light")

        @subroutine("my_sub")
        def my_sub():
            with Rung():
                out(Light)

        with pytest.raises(RuntimeError, match="must be used inside a Program"):
            with Rung(Button):
                call(my_sub)

    def test_decorator_with_program_decorator(self):
        """@subroutine works with @program decorator."""
        from pyrung.core.program import Program, Rung, call, out, program, subroutine

        Button = Bool("Button")
        SubLight = Bool("SubLight")

        @subroutine("init")
        def init_sequence():
            with Rung():
                out(SubLight)

        @program
        def my_logic():
            with Rung(Button):
                call(init_sequence)

        assert isinstance(my_logic, Program)
        assert "init" in my_logic.subroutines

        runner = PLCRunner(my_logic)
        runner.patch({"Button": True, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["SubLight"] is True

    def test_decorator_and_context_manager_coexist(self):
        """Decorator and context-manager subroutines can coexist."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        Light1 = Bool("Light1")
        Light2 = Bool("Light2")

        @subroutine("dec_sub")
        def dec_sub():
            with Rung():
                out(Light1)

        with Program() as logic:
            with Rung(Button):
                call(dec_sub)
                call("ctx_sub")

            with subroutine("ctx_sub"):
                with Rung():
                    out(Light2)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "Light1": False, "Light2": False})
        runner.step()

        assert runner.current_state.tags["Light1"] is True
        assert runner.current_state.tags["Light2"] is True

    def test_call_accepts_string_subroutine_name(self):
        """Existing string-based call() API is unchanged."""
        from pyrung.core.program import Program, Rung, call, out, subroutine

        Button = Bool("Button")
        SubLight = Bool("SubLight")

        with Program() as logic:
            with Rung(Button):
                call("my_sub")

            with subroutine("my_sub"):
                with Rung():
                    out(SubLight)

        runner = PLCRunner(logic)
        runner.patch({"Button": True, "SubLight": False})
        runner.step()

        assert runner.current_state.tags["SubLight"] is True
