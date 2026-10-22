"""The canonical conformance program and case definitions (no network)."""

from __future__ import annotations

import pytest

from pyrung.conformance import (
    assert_all_passed,
    build_program,
    case,
    parse_endpoint,
    run,
)
from pyrung.conformance._slot import MAX_SLOTS, slot_register
from pyrung.core import PLC, Rung, copy
from tests.conformance import test_rung_semantics


def _prove_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(7, slot.Result1)
        copy(1, slot.Fired)


def _prove_other_constant(slot):
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(8, slot.Result1)
        copy(1, slot.Fired)


def test_slots_live_at_fixed_ds_registers() -> None:
    build = build_program(test_rung_semantics.cases)
    m = build.tag_map
    assert m.resolve("Conformance_BuildHash") == "DS1"
    assert m.resolve("Conformance_SlotCount") == "DS2"
    assert m.resolve(build.slot[1].Cmd.name) == "DS11"
    assert m.resolve(build.slot[1].Fired.name) == "DS17"
    assert m.resolve(build.slot[3].Cmd.name) == "DS31"
    assert m.resolve(build.slot[3].ErrorCode.name) == "DS38"
    assert slot_register(3, "ErrorCode") == 38
    assert build.last_register == 40


def test_build_hash_is_stable_and_tracks_the_ladder() -> None:
    one = build_program([case("c", ladder=_prove_constant, expect={"Result1": 7})])
    again = build_program([case("c", ladder=_prove_constant, expect={"Result1": 7})])
    other = build_program([case("c", ladder=_prove_other_constant, expect={"Result1": 8})])
    assert 1 <= one.build_hash <= 32767
    assert one.build_hash == again.build_hash
    assert one.build_hash != other.build_hash


def test_first_scan_writes_the_header() -> None:
    build = build_program(test_rung_semantics.cases)
    plc = PLC(build.program)
    plc.step()
    tags = plc.current_state.tags
    assert tags["Conformance_BuildHash"] == build.build_hash
    assert tags["Conformance_SlotCount"] == 3


def test_in_process_only_cases_are_left_out_of_the_program() -> None:
    wire = case("wire", ladder=_prove_constant, expect={"Result1": 7})
    local = case("local", ladder=_prove_constant, expect={"Result1": 7}, in_process_only="why")
    build = build_program([local, wire])
    assert build.cases == (wire,)
    assert build.skipped == (local,)
    with pytest.raises(ValueError, match="No cases can run over Modbus"):
        build_program([local])


def test_export_writes_ladder_and_nicknames(tmp_path) -> None:
    build = build_program(test_rung_semantics.cases)
    out = build.export(tmp_path / "conformance")
    main = (out / "main.csv").read_text(encoding="utf-8")
    assert f"copy({build.build_hash},DS1)" in main
    assert "DS11!=0" in main
    assert (out / "nicknames.csv").exists()


def test_range_expectation_in_process() -> None:
    results = run([case("bracket", ladder=_prove_constant, expect={"Result1": range(5, 10)})])
    assert_all_passed(results)
    results = run([case("bracket", ladder=_prove_constant, expect={"Result1": range(8, 10)})])
    with pytest.raises(AssertionError, match=r"'Result1': 8\.\.9"):
        assert_all_passed(results)


def test_case_rejects_bad_expectations() -> None:
    with pytest.raises(ValueError, match="Unknown slot fields"):
        case("x", ladder=_prove_constant, expect={"Cmd": 1})
    with pytest.raises(TypeError, match="int or a range"):
        case("x", ladder=_prove_constant, expect={"Result1": 1.5})


def test_parse_endpoint() -> None:
    assert str(parse_endpoint("192.168.0.10")) == "192.168.0.10:502"
    assert str(parse_endpoint(" localhost:5020 ")) == "localhost:5020"
    with pytest.raises(ValueError):
        parse_endpoint("host:notaport")
    with pytest.raises(ValueError):
        parse_endpoint("")


def test_max_slots_fit_in_ds() -> None:
    assert slot_register(MAX_SLOTS, "ErrorCode") <= 4500
