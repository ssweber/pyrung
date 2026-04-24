"""Compiled parity tests for block operations: blockcopy, fill, search, unpack."""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Word,
    blockcopy,
    copy,
    fill,
    search,
    unpack_to_bits,
    unpack_to_words,
)
from pyrung.core.copy_converters import to_value


class TestBlockCopy:
    def test_copies_range(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                blockcopy(DS.select(1, 3), DS.select(10, 12))

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "DS1": 10, "DS2": 20, "DS3": 30})
        runner.step()
        assert runner.current_state.tags["DS10"] == 10
        assert runner.current_state.tags["DS11"] == 20
        assert runner.current_state.tags["DS12"] == 30

    def test_cross_type_copy(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                blockcopy(DS.select(1, 3), DD.select(1, 3))

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "DS1": 100, "DS2": 200, "DS3": 300})
        runner.step()
        assert runner.current_state.tags["DD1"] == 100
        assert runner.current_state.tags["DD2"] == 200
        assert runner.current_state.tags["DD3"] == 300

    def test_with_converter(self, runner_factory):
        Enable = Bool("Enable")
        CH = Block("CH", TagType.CHAR, 1, 10)
        DS = Block("DS", TagType.INT, 1, 10)

        with Program() as logic:
            with Rung(Enable):
                blockcopy(CH.select(1, 3), DS.select(1, 3), convert=to_value)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "CH1": "1", "CH2": "2", "CH3": "3"})
        runner.step()
        assert runner.current_state.tags["DS1"] == 1
        assert runner.current_state.tags["DS2"] == 2
        assert runner.current_state.tags["DS3"] == 3

    def test_not_executed_when_rung_false(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                blockcopy(DS.select(1, 3), DS.select(10, 12))

        runner = runner_factory(logic)
        runner.patch({"Enable": False, "DS1": 10, "DS2": 20, "DS3": 30, "DS10": 0})
        runner.step()
        assert runner.current_state.tags["DS10"] == 0


class TestFill:
    def test_fills_range_with_constant(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                fill(999, DS.select(1, 5))

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "DS1": 0, "DS2": 0, "DS3": 0, "DS4": 0, "DS5": 0})
        runner.step()
        for i in range(1, 6):
            assert runner.current_state.tags[f"DS{i}"] == 999

    def test_fills_with_tag_value(self, runner_factory):
        Enable = Bool("Enable")
        Source = Int("Source")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                fill(Source, DS.select(1, 3))

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "Source": 42, "DS1": 0, "DS2": 0, "DS3": 0})
        runner.step()
        assert runner.current_state.tags["DS1"] == 42
        assert runner.current_state.tags["DS2"] == 42
        assert runner.current_state.tags["DS3"] == 42

    def test_oneshot(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                fill(100, DS.select(1, 3), oneshot=True)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "DS1": 0, "DS2": 0, "DS3": 0})
        runner.step()
        assert runner.current_state.tags["DS1"] == 100

        runner.patch({"DS1": 0})
        runner.step()
        assert runner.current_state.tags["DS1"] == 0


class TestSearch:
    def test_finds_matching_value(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search(DS.select(1, 5) == 42, result=Result, found=Found)

        runner = runner_factory(logic)
        runner.patch(
            {
                "Enable": True,
                "DS1": 10,
                "DS2": 20,
                "DS3": 42,
                "DS4": 50,
                "DS5": 60,
                "Result": 0,
                "Found": False,
            }
        )
        runner.step()
        assert runner.current_state.tags["Result"] == 3
        assert runner.current_state.tags["Found"] is True

    def test_not_found(self, runner_factory):
        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search(DS.select(1, 3) == 999, result=Result, found=Found)

        runner = runner_factory(logic)
        runner.patch(
            {
                "Enable": True,
                "DS1": 1,
                "DS2": 2,
                "DS3": 3,
                "Result": 0,
                "Found": False,
            }
        )
        runner.step()
        assert runner.current_state.tags["Result"] == -1
        assert runner.current_state.tags["Found"] is False

    def test_text_search(self, runner_factory):
        Enable = Bool("Enable")
        CH = Block("CH", TagType.CHAR, 1, 100)
        Result = Int("Result")
        Found = Bool("Found")

        with Program() as logic:
            with Rung(Enable):
                search(CH.select(1, 6) == "BC", result=Result, found=Found)

        runner = runner_factory(logic)
        runner.patch(
            {
                "Enable": True,
                "CH1": "A",
                "CH2": "B",
                "CH3": "C",
                "CH4": "D",
                "CH5": "E",
                "CH6": "F",
                "Result": 0,
                "Found": False,
            }
        )
        runner.step()
        assert runner.current_state.tags["Result"] == 2
        assert runner.current_state.tags["Found"] is True


class TestUnpackToBits:
    def test_unpacks_int_to_bools(self, runner_factory):
        Enable = Bool("Enable")
        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                unpack_to_bits(Source, C.select(1, 8))

        runner = runner_factory(logic)
        runner.patch(
            {
                "Enable": True,
                "Source": 0b10110001,
                **{f"C{i}": False for i in range(1, 9)},
            }
        )
        runner.step()
        assert runner.current_state.tags["C1"] is True  # bit 0
        assert runner.current_state.tags["C2"] is False  # bit 1
        assert runner.current_state.tags["C3"] is False  # bit 2
        assert runner.current_state.tags["C4"] is False  # bit 3
        assert runner.current_state.tags["C5"] is True  # bit 4
        assert runner.current_state.tags["C6"] is True  # bit 5
        assert runner.current_state.tags["C7"] is False  # bit 6
        assert runner.current_state.tags["C8"] is True  # bit 7

    def test_unpacks_word(self, runner_factory):
        Enable = Bool("Enable")
        Source = Word("Source")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                unpack_to_bits(Source, C.select(1, 16))

        runner = runner_factory(logic)
        runner.patch(
            {
                "Enable": True,
                "Source": 0x8001,
                **{f"C{i}": False for i in range(1, 17)},
            }
        )
        runner.step()
        assert runner.current_state.tags["C1"] is True  # bit 0
        assert runner.current_state.tags["C16"] is True  # bit 15
        assert runner.current_state.tags["C2"] is False


class TestUnpackToWords:
    def test_unpacks_dint_to_two_ints(self, runner_factory):
        Enable = Bool("Enable")
        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                unpack_to_words(Source, DS.select(1, 2))

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "Source": 0x00030001, "DS1": 0, "DS2": 0})
        runner.step()
        assert runner.current_state.tags["DS1"] == 1  # low word
        assert runner.current_state.tags["DS2"] == 3  # high word

    def test_not_executed_when_rung_false(self, runner_factory):
        Enable = Bool("Enable")
        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Enable):
                unpack_to_words(Source, DS.select(1, 2))

        runner = runner_factory(logic)
        runner.patch({"Enable": False, "Source": 0xFFFF0001, "DS1": 0, "DS2": 0})
        runner.step()
        assert runner.current_state.tags["DS1"] == 0
        assert runner.current_state.tags["DS2"] == 0


class TestCopyWithConverter:
    def test_to_value_single_char(self, runner_factory):
        Enable = Bool("Enable")
        CH = Block("CH", TagType.CHAR, 1, 10)
        Result = Int("Result")

        with Program() as logic:
            with Rung(Enable):
                copy(CH[5], Result, convert=to_value)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "CH5": "7", "Result": 0})
        runner.step()
        assert runner.current_state.tags["Result"] == 7
