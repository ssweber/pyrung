"""Tests for live tag proxies bound via PLCRunner.active()."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    InputBlock,
    Int,
    OutputBlock,
    PLCRunner,
    SystemState,
    TagType,
    named_array,
    system,
    udt,
)


def test_live_value_requires_active_runner_scope() -> None:
    flag = Bool("Flag")

    with pytest.raises(RuntimeError, match="runner.active"):
        _ = flag.value

    with pytest.raises(RuntimeError, match="runner.active"):
        flag.value = True  # type: ignore[invalid-assignment]


def test_live_value_reads_pending_write_until_next_step() -> None:
    count = Int("Count")
    runner = PLCRunner(logic=[], initial_state=SystemState().with_tags({"Count": 7}))

    with runner.active():
        assert count.value == 7
        count.value = 9  # type: ignore[invalid-assignment]
        assert count.value == 9

    assert runner._pending_patches == {"Count": 9}
    runner.step()
    assert runner.current_state.tags["Count"] == 9
    assert runner._pending_patches == {}


def test_live_value_uses_tag_default_when_absent() -> None:
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
        ds[1].value = 123  # type: ignore[invalid-assignment]
        x[1].value = True  # type: ignore[invalid-assignment]
        y[1].value = True  # type: ignore[invalid-assignment]
        assert ds[1].value == 123
        assert x[1].value is True
        assert y[1].value is True

    runner.step()
    assert runner.current_state.tags["DS1"] == 123
    assert runner.current_state.tags["X1"] is True
    assert runner.current_state.tags["Y1"] is True


def test_live_value_supports_udt_and_named_array_instance_fields() -> None:
    @udt(count=1)
    class Alarm:
        id: Int
        on: Bool

    alarms = Alarm

    @named_array(Int, count=1, stride=1)
    class SubName:
        xCall = 0

    sub_name = SubName

    runner = PLCRunner(logic=[])

    with runner.active():
        alarm_1 = alarms[1]  # type: ignore[not-subscriptable]
        sub_name_1 = sub_name[1]  # type: ignore[not-subscriptable]
        alarm_1.id.value = 42
        alarm_1.on.value = True
        sub_name_1.xCall.value = 1
        assert alarm_1.id.value == 42
        assert alarm_1.on.value is True
        assert sub_name_1.xCall.value == 1

    runner.step()
    assert runner.current_state.tags["Alarm_id"] == 42
    assert runner.current_state.tags["Alarm_on"] is True
    assert runner.current_state.tags["SubName_xCall"] == 1


def test_live_value_supports_system_tags_and_enforces_read_only() -> None:
    runner = PLCRunner(logic=[])

    with runner.active():
        assert system.sys.always_on.value is True
        system.rtc.apply_time.value = True  # type: ignore[invalid-assignment]
        assert system.rtc.apply_time.value is True

    with pytest.raises(ValueError, match="read-only system point"):
        with runner.active():
            system.sys.always_on.value = False  # type: ignore[invalid-assignment]


def test_active_scope_is_context_local_and_supports_nesting() -> None:
    value = Int("Value")
    runner_a = PLCRunner(logic=[])
    runner_b = PLCRunner(logic=[])

    with runner_a.active():
        value.value = 1  # type: ignore[invalid-assignment]
        assert value.value == 1
        with runner_b.active():
            value.value = 2  # type: ignore[invalid-assignment]
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
