"""Tests for Program context manager and full integration.

This tests the DSL syntax:
    with Program() as logic:
        with Rung(condition):
            out(target)
"""

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

    def test_rung_with_nc_condition(self):
        """nc() creates normally closed condition."""
        from pyrung.core.program import Program, Rung, nc, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as prog:
            with Rung(nc(Button)):  # Light on when Button is OFF
                out(Light)

        # Button OFF -> Light ON
        state = SystemState().with_tags({"Button": False, "Light": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Light"] is True

        # Button ON -> Light OFF
        state = SystemState().with_tags({"Button": True, "Light": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Light"] is False


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


class TestPublicExports:
    """Test public core exports."""

    def test_pack_unpack_exports(self):
        from pyrung.core import pack_bits, pack_words, unpack_to_bits, unpack_to_words

        assert callable(pack_bits)
        assert callable(pack_words)
        assert callable(unpack_to_bits)
        assert callable(unpack_to_words)


class TestCastingReferenceExamples:
    """Program-level tests based on docs/click_reference/casting.md examples."""

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
        from pyrung.core.program import Program, Rung, math

        Enable = Bool("Enable")
        DH = Block("DH", TagType.WORD, 1, 200)

        with Program() as prog:
            with Rung(Enable):
                math(rsh(DH[1], 1), DH[2], mode="hex")
                math(rro(DH[1], 1), DH[3], mode="hex")

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
            from pyrung.core.program import _require_rung_context

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

    def test_call_still_accepts_string(self):
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
