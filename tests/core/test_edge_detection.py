"""Tests for rise() and fall() edge detection DSL functions.

These tests verify:
1. rise()/fall() create proper conditions
2. Each edge contact keeps its own memory (_edge:e{N})
3. Edge detection works correctly across scan cycles
"""

from pyrung.core import Bool, Program, Rung, fall, latch, out, reset, rise


class TestRiseDSL:
    """Test rise() DSL function for rising edge detection."""

    def test_rise_creates_rising_edge_condition(self):
        """rise() should create a RisingEdgeCondition."""
        from pyrung.core import rise
        from pyrung.core.condition import RisingEdgeCondition

        Button = Bool("Button")
        cond = rise(Button)

        assert isinstance(cond, RisingEdgeCondition)
        assert cond.tag is Button

    def test_rise_in_rung_fires_on_transition(self, runner_factory):
        """Rung with rise() should fire only on 0->1 transition."""
        from pyrung.core import rise

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(rise(Button)):
                out(Light)  # out() resets when rung false - shows one-shot behavior

        runner = runner_factory(logic)

        # Initial state: Button=False, Light=False
        runner.patch({"Button": False, "Light": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button still False - no edge
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button goes True - rising edge! Light turns on for ONE scan
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Button stays True - no edge, so out() resets Light to False
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Back to False!

    def test_rise_does_not_fire_when_already_on(self, runner_factory):
        """rise() should not fire if signal was already True."""
        from pyrung.core import rise

        Button = Bool("Button")
        Counter = Bool("Counter")  # Use bit as simple flag

        with Program() as logic:
            with Rung(rise(Button)):
                latch(Counter)

        runner = runner_factory(logic)

        # Start with Button already True
        runner.patch({"Button": True, "Counter": False})
        runner.step()

        # No rising edge because prev was also True (initialized to False, then first scan)
        # Actually first scan: prev=False (default), current=True -> rising edge!
        assert runner.current_state.tags.get("Counter") is True

        # Now reset and continue - no more edges while Button stays True
        runner.patch({"Counter": False})
        runner.step()
        runner.step()  # Button still True, prev=True -> no edge
        assert runner.current_state.tags.get("Counter") is False

    def test_rise_fires_again_after_off_on_cycle(self, runner_factory):
        """rise() should fire again after signal goes off then on."""
        from pyrung.core import rise

        Button = Bool("Button")
        PulseCount = Bool("PulseCount")

        with Program() as logic:
            with Rung(rise(Button)):
                latch(PulseCount)

        runner = runner_factory(logic)

        # First rising edge
        runner.patch({"Button": False, "PulseCount": False})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True

        # Reset the counter
        runner.patch({"PulseCount": False})
        runner.step()

        # Button goes off
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is False

        # Button goes on again - should fire!
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True


class TestFallDSL:
    """Test fall() DSL function for falling edge detection."""

    def test_fall_creates_falling_edge_condition(self):
        """fall() should create a FallingEdgeCondition."""
        from pyrung.core import fall
        from pyrung.core.condition import FallingEdgeCondition

        Button = Bool("Button")
        cond = fall(Button)

        assert isinstance(cond, FallingEdgeCondition)
        assert cond.tag is Button

    def test_fall_in_rung_fires_on_transition(self, runner_factory):
        """Rung with fall() should fire only on 1->0 transition."""
        from pyrung.core import fall

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(fall(Button)):
                out(Light)  # out() resets when rung false - shows one-shot behavior

        runner = runner_factory(logic)

        # Initial state: Button=True, Light=False
        runner.patch({"Button": True, "Light": False})
        runner.step()
        # First scan: prev=False (default), current=True -> not falling edge
        assert runner.current_state.tags.get("Light") is False

        # Button stays True - no edge
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button goes False - falling edge! Light turns on for ONE scan
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Button stays False - no edge (already off), so out() resets Light
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Back to False!

    def test_fall_does_not_fire_when_already_off(self, runner_factory):
        """fall() should not fire if signal was already False."""
        from pyrung.core import fall

        Button = Bool("Button")
        Counter = Bool("Counter")

        with Program() as logic:
            with Rung(fall(Button)):
                latch(Counter)

        runner = runner_factory(logic)

        # Start with Button already False
        runner.patch({"Button": False, "Counter": False})
        runner.step()
        # No falling edge: prev=False, current=False
        assert runner.current_state.tags.get("Counter") is False

        # Still False
        runner.step()
        assert runner.current_state.tags.get("Counter") is False

    def test_fall_fires_again_after_on_off_cycle(self, runner_factory):
        """fall() should fire again after signal goes on then off."""
        from pyrung.core import fall

        Button = Bool("Button")
        PulseCount = Bool("PulseCount")

        with Program() as logic:
            with Rung(fall(Button)):
                latch(PulseCount)

        runner = runner_factory(logic)

        # Setup: Button on
        runner.patch({"Button": True, "PulseCount": False})
        runner.step()  # prev=False, current=True -> no fall
        runner.step()  # prev=True, current=True -> no fall

        # First falling edge
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True

        # Reset the counter
        runner.patch({"PulseCount": False})
        runner.step()

        # Button goes on
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is False

        # Button goes off again - should fire!
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True


class TestEdgeSiteMemory:
    """Each edge contact keeps its own memory (``_edge:e{N}``), stored when scanned."""

    def test_site_memory_follows_the_bit_its_contact_saw(self, runner_factory):
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(rise(Button)):
                out(Light)

        runner = runner_factory(logic)

        runner.patch({"Button": True})
        runner.step()

        assert runner.current_state.memory.get("_edge:e1") is True
        assert "_prev:Button" not in runner.current_state.memory

        runner.patch({"Button": False})
        runner.step()

        assert runner.current_state.memory.get("_edge:e1") is False

    def test_each_contact_has_its_own_site(self, runner_factory):
        A = Bool("A")
        B = Bool("B")
        Out = Bool("Out")

        with Program() as logic:
            with Rung(rise(A), fall(B)):
                out(Out)

        runner = runner_factory(logic)

        runner.patch({"A": True, "B": False})
        runner.step()

        assert runner.current_state.memory.get("_edge:e1") is True
        assert runner.current_state.memory.get("_edge:e2", False) is False

        runner.patch({"A": False, "B": True})
        runner.step()

        assert runner.current_state.memory.get("_edge:e1") is False
        assert runner.current_state.memory.get("_edge:e2") is True

    def test_contact_behind_an_open_contact_still_stores(self, runner_factory):
        """Memory updates when the contact is scanned, powered or not."""
        Gate = Bool("Gate")
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Gate, rise(Button)):
                out(Light)

        runner = runner_factory(logic)
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.memory.get("_edge:e1") is True
        # The rise happened while the rung was open: closing Gate later sees none.
        runner.patch({"Gate": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is False

    def test_two_contacts_on_one_tag_keep_separate_memory(self, runner_factory):
        """A contact above the bit's writer and one below it see different edges."""
        Start = Bool("Start")
        Bit = Bool("Bit")
        Above = Bool("Above")
        Below = Bool("Below")

        with Program() as logic:
            with Rung(rise(Bit)):
                out(Above)
            with Rung(Start):
                out(Bit)
            with Rung(rise(Bit)):
                out(Below)

        runner = runner_factory(logic)
        runner.patch({"Start": True})
        runner.step()
        assert runner.current_state.tags.get("Above") is False
        assert runner.current_state.tags.get("Below") is True
        runner.step()
        assert runner.current_state.tags.get("Above") is True
        assert runner.current_state.tags.get("Below") is False
        runner.step()
        assert runner.current_state.tags.get("Above") is False


class TestEdgeCombinations:
    """Test edge conditions combined with other conditions."""

    def test_rise_with_other_conditions(self, runner_factory):
        """rise() can be combined with other conditions in a rung."""
        from pyrung.core import rise

        Button = Bool("Button")
        Enable = Bool("Enable")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(rise(Button), Enable):  # Both must be true
                latch(Light)

        runner = runner_factory(logic)

        # Rising edge but Enable is False
        runner.patch({"Button": False, "Enable": False, "Light": False})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Enable was False

        # Reset Button
        runner.patch({"Button": False})
        runner.step()

        # Now Enable is True and Button rises
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

    def test_rise_and_fall_in_different_rungs(self, runner_factory):
        """rise() and fall() can control set/reset of same output."""
        from pyrung.core import fall, rise

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(rise(Button)):
                latch(Light)
            with Rung(fall(Button)):
                reset(Light)

        runner = runner_factory(logic)

        runner.patch({"Button": False, "Light": False})
        runner.step()

        # Rising edge - Light on
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Stays on while Button held
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Falling edge - Light off
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is False


class TestEdgeOutsideProgram:
    """An edge in ``run_until()``/``when()`` is no contact of the program.

    It keeps its own memory, judged after each scan against the bit as the
    predicate last saw it (from when it was armed).
    """

    @staticmethod
    def _plc():
        from pyrung.core import PLC

        button = Bool("Button", external=True)
        light = Bool("Light")
        with Program(strict=False) as logic:
            with Rung(button):
                out(light)
        return PLC(logic, dt=0.010), light

    def test_run_until_rise_stops_on_the_scan_it_turns_on(self):
        plc, light = self._plc()
        plc.step()
        plc.patch({"Button": True})
        state = plc.run_until(rise(light), max_cycles=5)
        assert state.scan_id == 2
        assert state.tags["Light"] is True

    def test_run_until_rise_waits_for_a_fresh_edge(self):
        """Already on when armed: no edge until it goes off and on again."""
        plc, light = self._plc()
        plc.patch({"Button": True})
        plc.step()
        state = plc.run_until(rise(light), max_cycles=5)
        assert state.scan_id == 6

    def test_run_until_fall_without_fold(self):
        plc, light = self._plc()
        plc.patch({"Button": True})
        plc.step()
        plc.patch({"Button": False})
        state = plc.run_until(fall(light), max_cycles=5, fold=False)
        assert state.scan_id == 2
        assert state.tags["Light"] is False

    def test_when_rise_pauses_once(self):
        plc, light = self._plc()
        hits: list[int] = []
        plc.when(rise(light)).do(lambda state: hits.append(state.scan_id))
        plc.step()
        plc.patch({"Button": True})
        plc.step()
        plc.step()
        plc.patch({"Button": False})
        plc.step()
        plc.patch({"Button": True})
        plc.step()
        assert hits == [2, 5]

    def test_program_edge_reused_in_run_until_keeps_its_own_memory(self):
        """The predicate copies the contact: the program's site is untouched."""
        from pyrung.core import PLC

        button = Bool("Button", external=True)
        light = Bool("Light")
        edge = rise(button)
        with Program(strict=False) as logic:
            with Rung(edge):
                latch(light)
        plc = PLC(logic, dt=0.010)
        key = edge._memory_key
        assert key is not None
        plc.step()
        plc.patch({"Button": True})
        state = plc.run_until(edge, max_cycles=5)
        assert state.scan_id == 2
        assert edge._memory_key == key
        assert state.memory[key] is True
        assert state.tags["Light"] is True

    def test_run_until_edge_folded_matches_stepped(self):
        """A fold past a timer's run stops on the same scan as stepping."""
        from pyrung.core import PLC, Timer, on_delay

        tmr = Timer.clone("Tmr")
        go = Bool("Go", external=True)
        lamp = Bool("Lamp")
        with Program(strict=False) as logic:
            with Rung(go):
                on_delay(tmr, 5000)
            with Rung(tmr.Done):
                out(lamp)

        def run(edge, fold):
            plc = PLC(logic, dt=0.010)
            plc.patch({"Go": True})
            plc.step()
            if edge is fall:
                plc.run_until(tmr.Done, max_cycles=1000, fold=fold)
                plc.patch({"Go": False})
            state = plc.run_until(edge(lamp), max_cycles=1000, fold=fold)
            return state.scan_id, state.tags["Lamp"]

        assert run(rise, True) == run(rise, False) == (500, True)
        assert run(fall, True) == run(fall, False) == (501, False)
