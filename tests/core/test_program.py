"""Tests for Program context manager and full integration.

This tests the DSL syntax:
    with Program() as logic:
        with Rung(condition):
            out(target)
"""

from pyrung.core import Bit, Int, PLCRunner, SystemState
from tests.conftest import evaluate_condition, evaluate_rung


class TestProgramContextManager:
    """Test Program context manager for capturing rungs."""

    def test_program_captures_rungs(self):
        """Program context manager captures rungs defined within it."""
        from pyrung.core.program import Program, Rung, out

        Button = Bit("Button")
        Light = Bit("Light")

        with Program() as prog:
            with Rung(Button):
                out(Light)

        assert len(prog.rungs) == 1

    def test_program_multiple_rungs(self):
        """Program captures multiple rungs in order."""
        from pyrung.core.program import Program, Rung, out

        Button = Bit("Button")
        Light = Bit("Light")
        Motor = Bit("Motor")

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

        Button = Bit("Button")
        Light = Bit("Light")

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

        Button = Bit("Button")
        Motor = Bit("Motor")

        with Program() as prog:
            with Rung(Button):
                latch(Motor)

        state = SystemState().with_tags({"Button": True, "Motor": False})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Motor"] is True

    def test_rung_with_reset(self):
        """reset() adds RESET instruction."""
        from pyrung.core.program import Program, Rung, reset

        StopButton = Bit("StopButton")
        Motor = Bit("Motor")

        with Program() as prog:
            with Rung(StopButton):
                reset(Motor)

        state = SystemState().with_tags({"StopButton": True, "Motor": True})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Motor"] is False

    def test_rung_with_copy(self):
        """copy() adds COPY instruction."""
        from pyrung.core.program import Program, Rung, copy

        Button = Bit("Button")
        Step = Int("Step")

        with Program() as prog:
            with Rung(Button):
                copy(5, Step)

        state = SystemState().with_tags({"Button": True, "Step": 0})
        new_state = evaluate_rung(prog.rungs[0], state)
        assert new_state.tags["Step"] == 5

    def test_rung_with_nc_condition(self):
        """nc() creates normally closed condition."""
        from pyrung.core.program import Program, Rung, nc, out

        Button = Bit("Button")
        Light = Bit("Light")

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

        Button = Bit("Button")
        Light = Bit("Light")

        @program
        def my_logic():
            with Rung(Button):
                out(Light)

        # my_logic is now a Program
        assert isinstance(my_logic, Program)
        assert len(my_logic.rungs) == 1


class TestPLCRunnerIntegration:
    """Test full integration with PLCRunner."""

    def test_runner_executes_program(self):
        """PLCRunner evaluates program logic each scan."""
        from pyrung.core.program import Program, Rung, out

        Button = Bit("Button")
        Light = Bit("Light")

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

        StartButton = Bit("StartButton")
        StopButton = Bit("StopButton")
        MotorRunning = Bit("MotorRunning")

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
        NextButton = Bit("NextButton")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")

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
        AutoMode = Bit("AutoMode")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")

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
        AutoMode = Bit("AutoMode")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")

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
        AutoMode = Bit("AutoMode")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")

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
        AutoMode = Bit("AutoMode")
        Light = Bit("Light")

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

        Light = Bit("Light")

        with pytest.raises(RuntimeError, match="must be called inside a Rung"):
            with Program():
                with branch():  # No parent rung!
                    out(Light)

    def test_multiple_branches_in_rung(self):
        """Multiple branches can exist in a single rung."""
        from pyrung.core.program import Program, Rung, branch, out

        Step = Int("Step")
        Mode1 = Bit("Mode1")
        Mode2 = Bit("Mode2")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")
        Light3 = Bit("Light3")

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
        Mode = Bit("Mode")

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

        Button = Bit("Button")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Step = Int("Step")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light = Bit("Light")

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

        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Step = Int("Step")
        Light = Bit("Light")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light = Bit("Light")

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

        Button = Bit("Button")
        SubLight = Bit("SubLight")

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

        Button = Bit("Button")
        Light1 = Bit("Light1")
        Light2 = Bit("Light2")

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

        Button = Bit("Button")
        SubLight = Bit("SubLight")

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
