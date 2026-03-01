"""Tests for the P1AM hardware model."""

import pytest

from pyrung.circuitpy import MAX_SLOTS, P1AM, RunStopConfig, board
from pyrung.circuitpy.catalog import MODULE_CATALOG, ModuleDirection
from pyrung.core import (
    InputBlock,
    OutputBlock,
    PLCRunner,
    Program,
    Rung,
    TagType,
    out,
)
from pyrung.core.tag import LiveInputTag, LiveOutputTag

# ---------------------------------------------------------------------------
# Discrete input modules
# ---------------------------------------------------------------------------


class TestDiscreteInput:
    def test_slot_returns_input_block(self):
        hw = P1AM()
        result = hw.slot(1, "P1-08SIM")
        assert isinstance(result, InputBlock)

    def test_input_block_type_is_bool(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08SIM")
        assert block.type is TagType.BOOL

    def test_input_block_has_correct_channel_count(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08SIM")
        assert block.start == 1
        assert block.end == 8

    def test_16ch_input_block(self):
        hw = P1AM()
        block = hw.slot(1, "P1-16ND3")
        assert block.end == 16

    def test_input_tag_is_live_input_tag(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08SIM")
        tag = block[1]
        assert isinstance(tag, LiveInputTag)


# ---------------------------------------------------------------------------
# Discrete output modules
# ---------------------------------------------------------------------------


class TestDiscreteOutput:
    def test_slot_returns_output_block(self):
        hw = P1AM()
        result = hw.slot(1, "P1-08TRS")
        assert isinstance(result, OutputBlock)

    def test_output_block_type_is_bool(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08TRS")
        assert block.type is TagType.BOOL

    def test_output_block_has_correct_channel_count(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08TRS")
        assert block.start == 1
        assert block.end == 8

    def test_4ch_relay_output(self):
        hw = P1AM()
        block = hw.slot(1, "P1-04TRS")
        assert block.end == 4

    def test_15ch_output(self):
        hw = P1AM()
        block = hw.slot(1, "P1-15TD1")
        assert block.end == 15

    def test_output_tag_is_live_output_tag(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08TRS")
        tag = block[1]
        assert isinstance(tag, LiveOutputTag)


# ---------------------------------------------------------------------------
# Analog modules
# ---------------------------------------------------------------------------


class TestAnalog:
    def test_analog_input_returns_input_block(self):
        hw = P1AM()
        result = hw.slot(1, "P1-04ADL-1")
        assert isinstance(result, InputBlock)

    def test_analog_input_type_is_int(self):
        hw = P1AM()
        block = hw.slot(1, "P1-04ADL-1")
        assert block.type is TagType.INT

    def test_analog_input_4ch(self):
        hw = P1AM()
        block = hw.slot(1, "P1-04AD")
        assert block.end == 4

    def test_analog_input_8ch(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08ADL-1")
        assert block.end == 8

    def test_analog_output_returns_output_block(self):
        hw = P1AM()
        result = hw.slot(1, "P1-04DAL-1")
        assert isinstance(result, OutputBlock)

    def test_analog_output_type_is_int(self):
        hw = P1AM()
        block = hw.slot(1, "P1-04DAL-1")
        assert block.type is TagType.INT


# ---------------------------------------------------------------------------
# Combo modules
# ---------------------------------------------------------------------------


class TestCombo:
    def test_combo_discrete_returns_tuple(self):
        hw = P1AM()
        result = hw.slot(1, "P1-16CDR")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_combo_discrete_tuple_types(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-16CDR")
        assert isinstance(inp, InputBlock)
        assert isinstance(out_block, OutputBlock)

    def test_combo_discrete_channel_counts(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-16CDR")
        assert inp.end == 8
        assert out_block.end == 8

    def test_combo_15cdd_channel_counts(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-15CDD1")
        assert inp.end == 8
        assert out_block.end == 7

    def test_combo_analog_returns_tuple(self):
        hw = P1AM()
        result = hw.slot(1, "P1-4ADL2DAL-1")
        assert isinstance(result, tuple)
        inp, out_block = result
        assert isinstance(inp, InputBlock)
        assert isinstance(out_block, OutputBlock)

    def test_combo_analog_channel_counts(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-4ADL2DAL-1")
        assert inp.end == 4
        assert out_block.end == 2

    def test_combo_analog_type_is_int(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-4ADL2DAL-1")
        assert inp.type is TagType.INT
        assert out_block.type is TagType.INT

    def test_combo_discrete_type_is_bool(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-16CDR")
        assert inp.type is TagType.BOOL
        assert out_block.type is TagType.BOOL


# ---------------------------------------------------------------------------
# Tag naming
# ---------------------------------------------------------------------------


class TestTagNaming:
    def test_default_input_tag_name(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08SIM")
        assert block[1].name == "Slot1.1"
        assert block[3].name == "Slot1.3"

    def test_default_output_tag_name(self):
        hw = P1AM()
        block = hw.slot(2, "P1-08TRS")
        assert block[1].name == "Slot2.1"

    def test_custom_name(self):
        hw = P1AM()
        block = hw.slot(1, "P1-08SIM", name="Inputs")
        assert block[1].name == "Inputs.1"
        assert block[8].name == "Inputs.8"

    def test_combo_default_naming(self):
        hw = P1AM()
        inp, out_block = hw.slot(3, "P1-16CDR")
        assert inp[1].name == "Slot3_In.1"
        assert out_block[1].name == "Slot3_Out.1"

    def test_combo_custom_naming(self):
        hw = P1AM()
        inp, out_block = hw.slot(3, "P1-16CDR", name="Panel")
        assert inp[1].name == "Panel_In.1"
        assert out_block[1].name == "Panel_Out.1"

    def test_slot_number_in_default_name(self):
        hw = P1AM()
        block = hw.slot(15, "P1-08SIM")
        assert block[1].name == "Slot15.1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_slot_zero_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="1.15"):
            hw.slot(0, "P1-08SIM")

    def test_slot_negative_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="1.15"):
            hw.slot(-1, "P1-08SIM")

    def test_slot_over_max_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="1.15"):
            hw.slot(16, "P1-08SIM")

    def test_slot_non_int_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="1.15"):
            hw.slot("1", "P1-08SIM")  # type: ignore[arg-type]

    def test_unknown_module_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="Unknown module"):
            hw.slot(1, "P1-FAKE")

    def test_duplicate_slot_raises(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with pytest.raises(ValueError, match="already configured"):
            hw.slot(1, "P1-08TRS")

    def test_duplicate_slot_error_names_existing_module(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        with pytest.raises(ValueError, match="P1-08SIM"):
            hw.slot(1, "P1-08TRS")


# ---------------------------------------------------------------------------
# P1AM properties
# ---------------------------------------------------------------------------


class TestP1AMProperties:
    def test_configured_slots_empty(self):
        hw = P1AM()
        assert hw.configured_slots == {}

    def test_configured_slots_populated(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        hw.slot(3, "P1-08TRS")
        slots = hw.configured_slots
        assert set(slots.keys()) == {1, 3}
        assert slots[1].part_number == "P1-08SIM"
        assert slots[3].part_number == "P1-08TRS"

    def test_get_slot_returns_same_block(self):
        hw = P1AM()
        original = hw.slot(1, "P1-08SIM")
        retrieved = hw.get_slot(1)
        assert retrieved is original

    def test_get_slot_unconfigured_raises(self):
        hw = P1AM()
        with pytest.raises(ValueError, match="not configured"):
            hw.get_slot(1)

    def test_repr_empty(self):
        hw = P1AM()
        assert repr(hw) == "P1AM()"

    def test_repr_configured(self):
        hw = P1AM()
        hw.slot(1, "P1-08SIM")
        hw.slot(3, "P1-08TRS")
        assert repr(hw) == "P1AM(1=P1-08SIM, 3=P1-08TRS)"

    def test_max_slots_is_15(self):
        assert MAX_SLOTS == 15


# ---------------------------------------------------------------------------
# Exhaustive: every catalog module can be instantiated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("part", sorted(MODULE_CATALOG.keys()))
def test_every_catalog_module_can_be_slotted(part: str):
    hw = P1AM()
    result = hw.slot(1, part)
    spec = MODULE_CATALOG[part]
    if spec.is_combo:
        assert isinstance(result, tuple)
        inp, out_block = result
        assert isinstance(inp, InputBlock)
        assert isinstance(out_block, OutputBlock)
    elif spec.direction is ModuleDirection.INPUT:
        assert isinstance(result, InputBlock)
    else:
        assert isinstance(result, OutputBlock)


# ---------------------------------------------------------------------------
# Integration: PLCRunner with P1AM blocks
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_basic_program_with_p1am_blocks(self):
        hw = P1AM()
        inputs = hw.slot(1, "P1-08SIM")
        outputs = hw.slot(2, "P1-08TRS")

        Button = inputs[1]
        Light = outputs[1]

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLCRunner(logic)
        runner.patch({Button.name: True})
        runner.step()
        assert runner.current_state.tags[Light.name] is True

    def test_analog_program(self):
        hw = P1AM()
        analog_in = hw.slot(1, "P1-04ADL-1")
        analog_out = hw.slot(2, "P1-04DAL-1")

        sensor = analog_in[1]
        drive = analog_out[1]

        assert sensor.type is TagType.INT
        assert drive.type is TagType.INT

    def test_combo_module_in_program(self):
        hw = P1AM()
        inp, out_block = hw.slot(1, "P1-16CDR")

        Switch = inp[1]
        Relay = out_block[1]

        with Program() as logic:
            with Rung(Switch):
                out(Relay)

        runner = PLCRunner(logic)
        runner.patch({Switch.name: True})
        runner.step()
        assert runner.current_state.tags[Relay.name] is True

    def test_multiple_slots(self):
        hw = P1AM()
        di = hw.slot(1, "P1-08SIM")
        do = hw.slot(2, "P1-08TRS")
        ai = hw.slot(3, "P1-04ADL-1")
        ao = hw.slot(4, "P1-04DAL-1")

        assert di[1].name == "Slot1.1"
        assert do[1].name == "Slot2.1"
        assert ai[1].name == "Slot3.1"
        assert ao[1].name == "Slot4.1"

        # No tag name collisions
        names = {di[1].name, do[1].name, ai[1].name, ao[1].name}
        assert len(names) == 4


class TestP1AMBoardModel:
    def test_board_namespace_has_expected_tag_names(self):
        assert board.switch.name == "board.switch"
        assert board.led.name == "board.led"
        assert board.neopixel.r.name == "board.neopixel.r"
        assert board.neopixel.g.name == "board.neopixel.g"
        assert board.neopixel.b.name == "board.neopixel.b"
        assert board.save_memory_cmd.name == "board.save_memory_cmd"

    def test_board_tag_types_are_correct(self):
        assert board.switch.type is TagType.BOOL
        assert board.led.type is TagType.BOOL
        assert board.neopixel.r.type is TagType.INT
        assert board.neopixel.g.type is TagType.INT
        assert board.neopixel.b.type is TagType.INT
        assert board.save_memory_cmd.type is TagType.BOOL

    def test_runstop_config_defaults_and_validation(self):
        cfg = RunStopConfig()
        assert cfg.source == "board.switch"
        assert cfg.run_when_high is True
        assert cfg.debounce_ms == 30
        assert cfg.expose_mode_tags is True

        with pytest.raises(ValueError, match="source"):
            RunStopConfig(source="board.led")  # type: ignore[arg-type]
