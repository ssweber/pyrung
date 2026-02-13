"""Tests for live tag proxies bound via PLCRunner.active()."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    Field,
    InputBlock,
    Int,
    OutputBlock,
    PackedStruct,
    PLCRunner,
    Struct,
    SystemState,
    TagType,
    system,
)


def test_live_value_requires_active_runner_scope() -> None:
    flag = Bool("Flag")

    with pytest.raises(RuntimeError, match="runner.active"):
        _ = flag.value

    with pytest.raises(RuntimeError, match="runner.active"):
        flag.value = True


def test_live_value_stages_write_and_reads_pending_before_step() -> None:
    count = Int("Count")
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"Count": 7}))

    with runner.active():
        assert count.value == 7
        count.value = 9
        assert count.value == 9

    assert runner._pending_patches == {"Count": 9}
    runner.step()
    assert runner.current_state.tags["Count"] == 9
    assert runner._pending_patches == {}


def test_live_value_reads_default_when_tag_absent() -> None:
    count = Int("Count")
    runner = PLCRunner(logic=[])

    with runner.active():
        assert count.value == 0


def test_live_value_supports_block_input_and_output_tags() -> None:
    ds = Block("DS", TagType.INT, 1, 10)
    x = InputBlock("X", TagType.BOOL, 1, 10)
    y = OutputBlock("Y", TagType.BOOL, 1, 10)
    runner = PLCRunner(logic=[])

    with runner.active():
        ds[1].value = 123
        x[1].value = True
        y[1].value = True
        assert ds[1].value == 123
        assert x[1].value is True
        assert y[1].value is True

    runner.step()
    assert runner.current_state.tags["DS1"] == 123
    assert runner.current_state.tags["X1"] is True
    assert runner.current_state.tags["Y1"] is True


def test_live_value_supports_struct_and_packed_struct_instance_fields() -> None:
    alarms = Struct(
        "Alarm",
        count=1,
        id=Field(TagType.INT),
        on=Field(TagType.BOOL),
    )
    sub_name = PackedStruct(
        "SubName",
        TagType.INT,
        count=1,
        xCall=Field(default=0),
    )

    runner = PLCRunner(logic=[])

    with runner.active():
        alarms[1].id.value = 42
        alarms[1].on.value = True
        sub_name[1].xCall.value = 1
        assert alarms[1].id.value == 42
        assert alarms[1].on.value is True
        assert sub_name[1].xCall.value == 1

    runner.step()
    assert runner.current_state.tags["Alarm1_id"] == 42
    assert runner.current_state.tags["Alarm1_on"] is True
    assert runner.current_state.tags["SubName1_xCall"] == 1


def test_live_value_supports_system_tags_and_enforces_read_only() -> None:
    runner = PLCRunner(logic=[])

    with runner.active():
        assert system.sys.always_on.value is True
        system.rtc.apply_time.value = True
        assert system.rtc.apply_time.value is True

    with pytest.raises(ValueError, match="read-only system point"):
        with runner.active():
            system.sys.always_on.value = False


def test_active_scope_is_context_local_and_supports_nesting() -> None:
    value = Int("Value")
    runner_a = PLCRunner(logic=[])
    runner_b = PLCRunner(logic=[])

    with runner_a.active():
        value.value = 1
        assert value.value == 1
        with runner_b.active():
            value.value = 2
            assert value.value == 2
        assert value.value == 1

    assert runner_a._pending_patches["Value"] == 1
    assert runner_b._pending_patches["Value"] == 2


def test_patch_accepts_tag_keys_string_keys_and_mixed_keys() -> None:
    flag = Bool("Flag")
    count = Int("Count")
    runner = PLCRunner(logic=[])

    runner.patch({flag: True})
    runner.patch({"Count": 2})
    runner.patch({count: 3, "Other": 4})
    runner.step()

    assert runner.current_state.tags["Flag"] is True
    assert runner.current_state.tags["Count"] == 3
    assert runner.current_state.tags["Other"] == 4


def test_patch_rejects_invalid_key_types() -> None:
    runner = PLCRunner(logic=[])

    with pytest.raises(TypeError, match="str or Tag"):
        runner.patch({1: True})  # type: ignore[arg-type]
