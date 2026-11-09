from __future__ import annotations

import time
from datetime import datetime

import pytest

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Block,
    Bool,
    Char,
    CompiledPLC,
    Int,
    Program,
    Rung,
    SystemState,
    TagType,
    Timer,
    blockcopy,
    call,
    copy,
    fill,
    latch,
    math,
    named_array,
    on_delay,
    out,
    rise,
    run_function,
    search,
    shift,
    subroutine,
    system,
    to_binary,
    to_text,
)
from pyrung.core.analysis.prove.kernel import _step_compiled_kernel


def _assert_states_equivalent(left: PLC | CompiledPLC, right: PLC | CompiledPLC) -> None:
    left_state = left.current_state
    right_state = right.current_state
    assert left_state.scan_id == right_state.scan_id
    assert left_state.timestamp == right_state.timestamp
    assert dict(left_state.tags) == dict(right_state.tags)
    assert dict(left_state.memory) == dict(right_state.memory)


def _assert_compiled_kernels_match(
    legacy,
    blockless,
    *,
    steps: list[dict[str, bool | int | float | str]],
    dt: float = 0.010,
) -> None:
    legacy_kernel = legacy.create_kernel()
    blockless_kernel = blockless.create_kernel()

    for patch in steps:
        legacy_kernel.tags.update(patch)
        blockless_kernel.tags.update(patch)
        _step_compiled_kernel(legacy, legacy_kernel, dt=dt)
        _step_compiled_kernel(blockless, blockless_kernel, dt=dt)
        assert legacy_kernel.tags == blockless_kernel.tags
        assert legacy_kernel.memory == blockless_kernel.memory
        assert legacy_kernel.prev == blockless_kernel.prev
        assert legacy_kernel.scan_id == blockless_kernel.scan_id
        assert legacy_kernel.timestamp == blockless_kernel.timestamp


def test_compile_kernel_export_and_replay_kernel_bootstrap() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            latch(light)

    compiled = compile_kernel(program)
    kernel = compiled.create_kernel()

    assert callable(compiled.step_fn)
    assert "def _kernel_step" in compiled.source
    assert kernel.tags["Enable"] is False
    assert kernel.tags["Light"] is False
    assert kernel.memory == {}
    assert kernel.prev == {}


def test_compiled_plc_seeds_explicit_block_pointer_tag() -> None:
    enable = Bool("Enable")
    index = Int("Index")
    ds = Block("DS", TagType.INT, 1, 3)

    with Program(strict=False) as program:
        with Rung(enable):
            math(index + 1, index)
        with Rung(enable):
            copy(0, ds[ds[1]])

    runner = CompiledPLC(program)

    assert runner.current_state.tags["DS1"] == 0


def test_compiled_plc_does_not_seed_static_block_ranges_from_compiler_cache() -> None:
    enable = Bool("Enable")
    ds = Block(
        "DS",
        TagType.INT,
        1,
        12,
        valid_ranges=((1, 3), (10, 12)),
    )

    with Program(strict=False) as program:
        with Rung(enable):
            blockcopy(ds.select(1, 3), ds.select(10, 12))

    compiled = compile_kernel(program)
    runner = CompiledPLC(program, compiled=compiled)

    assert not set(runner.current_state.tags).intersection(
        {"DS1", "DS2", "DS3", "DS10", "DS11", "DS12"}
    )


@pytest.mark.parametrize("retentive", [True, False], ids=["retentive", "not_retentive"])
@pytest.mark.parametrize("transition", ["reboot", "stop_to_run"])
def test_a_dormant_range_keeps_through_a_reset_only_if_retentive(
    transition: str,
    retentive: bool,
) -> None:
    enabled = Bool("ResetRangeEnabled", external=True)
    values = Block("ResetRange", TagType.INT, 1, 2, retentive=retentive)

    with Program(strict=False) as program:
        with Rung(enabled):
            fill(7, values.select(1, 2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    for runner in (plc, compiled):
        runner.patch({enabled: True})
        runner.step()
        runner.patch({enabled: False})
        runner.step()
    _assert_states_equivalent(plc, compiled)
    assert {"ResetRange1", "ResetRange2"} <= set(plc.current_state.tags)

    if transition == "reboot":
        plc.reboot()
        compiled.reboot()
        _assert_states_equivalent(plc, compiled)
    else:
        plc.stop()
        compiled.stop()
        _assert_states_equivalent(plc, compiled)

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    for runner in (plc, compiled):
        tags = runner.current_state.tags
        assert runner.current_state.scan_id == 1
        if retentive:
            # A battery reboot keeps what STOP -> RUN keeps: what a retentive slot the
            # rungs reach holds, named or not.
            assert (tags["ResetRange1"], tags["ResetRange2"]) == (7, 7)
            assert tags["ResetRangeEnabled"] is False
        else:
            assert not {"ResetRange1", "ResetRange2"} & set(tags)


def test_compiled_plc_initial_state_defines_live_block_membership() -> None:
    enabled = Bool("AnchorRangeEnabled", external=True)
    values = Block("AnchorRange", TagType.INT, 1, 2)

    with Program(strict=False) as program:
        with Rung(enabled):
            fill(7, values.select(1, 2))

    initial = SystemState().with_tags(
        {
            enabled.name: False,
            "AnchorRange1": 9,
        }
    )
    plc = PLC(program, initial_state=initial, dt=0.010)
    compiled = CompiledPLC(program, initial_state=initial, dt=0.010)

    # This guards membership installed by the current-state anchor.  Avoid
    # materializing values[1] into Block._tag_cache: durable reset-known status
    # is a separate contract established by an explicit Tag override.
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["AnchorRange1"] == 9
    assert "AnchorRange2" not in compiled.current_state.tags


def test_compiled_plc_forced_block_tag_survives_battery_reboot() -> None:
    enabled = Bool("ForcedRangeEnabled", external=True)
    # Retentive, so a battery keeps it (a non-retentive slot resets, as on STOP -> RUN).
    values = Block("ForcedRange", TagType.INT, 1, 2, retentive=True)

    with Program(strict=False) as program:
        with Rung(enabled):
            fill(7, values.select(1, 2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)
    forced_cell = values[1]

    with plc.forced({forced_cell: 23}), compiled.forced({forced_cell: 23}):
        plc.step()
        compiled.step()
        _assert_states_equivalent(plc, compiled)

    plc.reboot()
    compiled.reboot()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["ForcedRange1"] == 23
    assert "ForcedRange2" not in compiled.current_state.tags


@pytest.mark.parametrize("key_kind", ["tag", "string"])
@pytest.mark.parametrize("override_kind", ["forced", "force_then_unforce"])
def test_unused_block_override_does_not_materialize_current_state(
    key_kind: str,
    override_kind: str,
) -> None:
    enabled = Bool("UnusedOverrideEnabled", external=True)
    values = Block("UnusedOverride", TagType.INT, 1, 2)

    with Program(strict=False) as program:
        with Rung(enabled):
            fill(7, values.select(1, 2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)
    key = values[1] if key_kind == "tag" else "UnusedOverride1"

    if override_kind == "forced":
        with plc.forced({key: 23}), compiled.forced({key: 23}):
            _assert_states_equivalent(plc, compiled)
    else:
        plc.force(key, 23)
        compiled.force(key, 23)
        plc.unforce(key)
        compiled.unforce(key)

    _assert_states_equivalent(plc, compiled)
    assert "UnusedOverride1" not in compiled.current_state.tags

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert "UnusedOverride1" not in compiled.current_state.tags


@pytest.mark.parametrize("key_kind", ["tag", "string"])
def test_applied_block_patch_materializes_at_scan_and_matches_reboot_policy(
    key_kind: str,
) -> None:
    enabled = Bool("PatchedRangeEnabled", external=True)
    values = Block("PatchedRange", TagType.INT, 1, 2, retentive=True)

    with Program(strict=False) as program:
        with Rung(enabled):
            fill(7, values.select(1, 2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)
    key = values[1] if key_kind == "tag" else "PatchedRange1"

    plc.patch({key: 31})
    compiled.patch({key: 31})
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["PatchedRange1"] == 31

    plc.reboot()
    compiled.reboot()

    _assert_states_equivalent(plc, compiled)
    # The rungs reach it and it is retentive, so a battery keeps it, whichever way the
    # patch spelled it.
    assert compiled.current_state.tags["PatchedRange1"] == 31


@pytest.mark.parametrize("compiled_step", ["step", "step_replay"])
@pytest.mark.parametrize("split_after", [None, 1])
def test_applied_block_patch_materializes_on_every_compiled_pre_scan_path(
    compiled_step: str,
    split_after: int | None,
) -> None:
    enabled = Bool("PreScanRangeEnabled", external=True)
    marker = Int("PreScanMarker")
    values = Block("PreScanRange", TagType.INT, 1, 2)

    with Program(strict=False) as program:
        with Rung():
            copy(1, marker)
        with Rung(enabled):
            fill(7, values.select(1, 2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(
        program,
        compiled=compile_kernel(program, split_after=split_after),
        dt=0.010,
    )

    plc.patch({"PreScanRange1": 41})
    compiled.patch({"PreScanRange1": 41})
    plc.step()
    getattr(compiled, compiled_step)()
    if compiled_step == "step_replay":
        compiled._materialize_replay_state()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["PreScanRange1"] == 41
    assert "PreScanRange2" not in compiled.current_state.tags


@pytest.mark.parametrize("blockless", [False, True])
def test_compiled_plc_preserves_named_array_range_identity_and_shape(
    blockless: bool,
) -> None:
    @named_array(Int, count=2, stride=4)
    class ReplayRow:
        first = 7
        second = 0

    enabled = Bool("ReplayRowCopyEnabled")

    with Program(strict=False) as program:
        with Rung(enabled):
            blockcopy(ReplayRow.instance(1).reverse(), ReplayRow.instance(2))

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(
        program,
        compiled=compile_kernel(program, blockless=blockless),
        dt=0.010,
    )

    # An explicit semantic range seeds the fields in both runners, while its
    # sparse backing span and padding never become observable state.
    _assert_states_equivalent(plc, compiled)
    semantic_names = {
        "ReplayRow1_first",
        "ReplayRow1_second",
        "ReplayRow2_first",
        "ReplayRow2_second",
    }
    assert semantic_names <= set(compiled.current_state.tags)
    assert compiled.current_state.tags["ReplayRow1_first"] == 7
    assert compiled.current_state.tags["ReplayRow2_first"] == 7
    assert not {f"ReplayRow{index}" for index in range(1, 9)} & set(compiled.current_state.tags)

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)

    patch = {
        "ReplayRowCopyEnabled": True,
        "ReplayRow1_first": 11,
        "ReplayRow1_second": 22,
    }
    plc.patch(patch)
    compiled.patch(patch)
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["ReplayRow2_first"] == 22
    assert compiled.current_state.tags["ReplayRow2_second"] == 11
    assert not {f"ReplayRow{index}" for index in range(1, 9)} & set(compiled.current_state.tags)


@pytest.mark.parametrize("blockless", [False, True])
def test_compiled_plc_specialized_fill_is_visible_to_same_scan_copy(
    blockless: bool,
) -> None:
    @named_array(Int, stride=3)
    class FillRow:
        first = 0
        second = 0

    observed = Int("FillRowObserved")

    with Program(strict=False) as program:
        with Rung():
            fill(9, FillRow.instance(1))
            copy(FillRow[1].second, observed)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(
        program,
        compiled=compile_kernel(program, blockless=blockless),
        dt=0.010,
    )

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["FillRowObserved"] == 9


@pytest.mark.parametrize("blockless", [False, True])
def test_compiled_plc_sparse_reversed_specialized_search_keeps_address_parity(
    blockless: bool,
) -> None:
    @named_array(Int, count=2, stride=3)
    class SearchRow:
        first = 0
        second = 0

    result = Int("SearchRowResult")
    found = Bool("SearchRowFound")

    with Program(strict=False) as program:
        with Rung():
            search(
                SearchRow.instance_select(1, 2).reverse() == 42,
                result=result,
                found=found,
            )

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(
        program,
        compiled=compile_kernel(program, blockless=blockless),
        dt=0.010,
    )
    patch = {"SearchRow2_first": 42}
    plc.patch(patch)
    compiled.patch(patch)

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["SearchRowFound"] is True
    # Sparse selected ranges currently pair their semantic tags with the
    # physical span in traversal order. Keep compiled replay aligned with that
    # exact interpreter behavior until the public address contract is revisited.
    assert compiled.current_state.tags["SearchRowResult"] == 5


def test_compile_kernel_caches_specialized_static_range_layout(monkeypatch) -> None:
    @named_array(Int, count=50, stride=3)
    class CachedRows:
        first = 0
        second = 0

    selected = CachedRows.instance_select(1, 50)
    range_type = type(selected)
    original_tags = range_type.tags
    calls = 0

    def counted_tags(range_value):
        nonlocal calls
        if range_value is selected:
            calls += 1
        return original_tags(range_value)

    monkeypatch.setattr(range_type, "tags", counted_tags)

    with Program(strict=False) as program:
        with Rung():
            fill(1, selected)

    compile_kernel(program, blockless=True)

    assert calls == 1


def test_blockless_kernel_matches_legacy_for_block_operations() -> None:
    ds = Block("DS", TagType.INT, 1, 6)
    dd = Block("DD", TagType.INT, 1, 6)
    idx = Int("Idx", external=True, min=1, max=6)
    fill_cmd = Bool("FillCmd", external=True)
    copy_cmd = Bool("CopyCmd", external=True)
    out_tag = Int("Out")
    found_addr = Int("FoundAddr")
    found = Bool("Found")

    with Program(strict=False) as program:
        with Rung(fill_cmd):
            fill(3, ds.select(2, 4))
        with Rung(copy_cmd):
            copy(ds[idx], out_tag)
            copy(7, dd[idx])
        with Rung():
            blockcopy(ds.select(1, 3), dd.select(4, 6))
            search(dd.select(1, 6) >= 3, result=found_addr, found=found)

    legacy = compile_kernel(program)
    blockless = compile_kernel(program, blockless=True)

    _assert_compiled_kernels_match(
        legacy,
        blockless,
        steps=[
            {"Idx": 2, "FillCmd": True, "CopyCmd": True},
            {"Idx": 4, "FillCmd": False, "CopyCmd": True},
        ],
    )


@pytest.mark.parametrize("blockless", [False, True])
def test_recompile_keeps_mapped_condition_tag_scalar(blockless: bool) -> None:
    ds = Block("MappedDS", TagType.INT, 1, 5000)
    state = Int("MappedState", default=7)
    index = Int("MappedIndex", default=1)
    observed = Bool("MappedObserved")
    copied = Int("MappedCopied")
    state.map_to(ds[1])

    with Program(strict=False) as program:
        with Rung(state == 7):
            out(observed)
        with Rung():
            copy(ds[index], copied)

    first = compile_kernel(program, blockless=blockless)
    assert len(ds._tag_cache) == 5000
    second = compile_kernel(program, blockless=blockless)

    # Recompilation sees the now-materialized block cache, but a condition on
    # its mapped scalar occupant must not snapshot all 5,000 block entries.
    assert "_cond_block_snap" not in second.source

    first_kernel = first.create_kernel()
    second_kernel = second.create_kernel()
    _step_compiled_kernel(first, first_kernel, dt=0.010)
    _step_compiled_kernel(second, second_kernel, dt=0.010)

    assert first_kernel.tags == second_kernel.tags
    assert second_kernel.tags["MappedObserved"] is True
    assert second_kernel.tags["MappedCopied"] == 7


def test_blockless_kernel_matches_legacy_for_shift_edge_and_oneshot() -> None:
    bits = Block("C", TagType.BOOL, 1, 4)
    clock = Bool("Clock", external=True)
    reset_cmd = Bool("Reset", external=True)
    pulse = Bool("Pulse")
    fired = Bool("Fired")

    with Program(strict=False) as program:
        with Rung():
            shift(bits.select(1, 4)).clock(clock).reset(reset_cmd)
        with Rung(rise(bits[4])):
            out(pulse)
        with Rung(bits[1]):
            out(fired, oneshot=True)

    legacy = compile_kernel(program)
    blockless = compile_kernel(program, blockless=True)

    _assert_compiled_kernels_match(
        legacy,
        blockless,
        steps=[
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": True},
        ],
    )


def test_blockless_kernel_subroutine_matches_legacy_for_block_edge_and_oneshot() -> None:
    bits = Block("C", TagType.BOOL, 1, 4)
    clock = Bool("Clock", external=True)
    reset_cmd = Bool("Reset", external=True)
    pulse = Bool("Pulse")
    fired = Bool("Fired")

    with Program(strict=False) as program:
        with subroutine("worker"):
            with Rung(rise(bits[4])):
                out(pulse)
            with Rung(bits[1]):
                out(fired, oneshot=True)
        with Rung():
            shift(bits.select(1, 4)).clock(clock).reset(reset_cmd)
        with Rung():
            call("worker")

    legacy = compile_kernel(program)
    blockless = compile_kernel(program, blockless=True)

    _assert_compiled_kernels_match(
        legacy,
        blockless,
        steps=[
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": False},
            {"Clock": True, "Reset": False},
            {"Clock": False, "Reset": True},
        ],
    )


def test_kernel_subroutine_copy_converter_scalar_char_fanout_uses_live_tags() -> None:
    enable = Bool("Enable", external=True)
    source = Int("Source", default=123)
    start = Char("Ch0")

    with Program(strict=False) as program:
        with subroutine("worker"):
            with Rung():
                copy(source, start, convert=to_text(termination_code=0))
        with Rung(enable):
            call("worker")

    legacy = compile_kernel(program)
    blockless = compile_kernel(program, blockless=True)

    _assert_compiled_kernels_match(
        legacy,
        blockless,
        steps=[
            {"Enable": False},
            {"Enable": True},
        ],
    )

    runner = CompiledPLC(program, dt=0.010)
    runner.patch({"Enable": True})
    runner.step()

    assert runner.current_state.tags["Ch0"] == "1"
    assert runner.current_state.tags["Ch1"] == "2"
    assert runner.current_state.tags["Ch2"] == "3"
    assert ord(runner.current_state.tags["Ch3"]) == 0


def test_blockless_specialized_char_range_supports_sequential_copy_converter() -> None:
    @named_array(Char, stride=2)
    class NamedChars:
        char1 = ""
        char2 = ""

    source = Int("NamedCharsSource", default=12)

    with Program(strict=False) as program:
        with Rung():
            fill("", NamedChars.instance(1))
            copy(source, NamedChars[1].char1, convert=to_text())

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(
        program,
        compiled=compile_kernel(program, blockless=True),
        dt=0.010,
    )

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["NamedChars_char1"] == "1"
    assert compiled.current_state.tags["NamedChars_char2"] == "2"


def test_compiled_plc_matches_plc_for_initial_and_first_scan_system_runtime_defaults() -> None:
    program = Program(strict=False)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    _assert_states_equivalent(plc, compiled)

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)


def test_compiled_plc_matches_plc_for_patch_force_and_edge_memory() -> None:
    enable = Bool("Enable")
    reset_tag = Bool("Reset")
    output = Bool("Output")

    with Program(strict=False) as program:
        with Rung(enable):
            latch(reset_tag)
        with Rung(enable).continued():
            on_delay(Timer[1], preset=50).reset(reset_tag)
        with Rung(rise(enable)):
            out(output)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "Reset": False})
    compiled.patch({"Enable": True, "Reset": False})
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    # The rise() contact stored the bit it saw in its own site memory.
    assert compiled.current_state.memory["_edge:e1"] is True
    assert plc.current_state.memory["_edge:e1"] is True
    assert compiled.current_state.tags["Output"] is True

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Output"] is False


def test_compiled_plc_matches_plc_for_indirect_copy_converter_address_fault() -> None:
    ds = Block("DS", TagType.INT, 1, 10)
    ch = Block("CH", TagType.CHAR, 1, 10)
    pointer = Int("Pointer")
    enable = Bool("Enable")

    with Program(strict=False) as program:
        with Rung(enable):
            copy(ds[pointer], ch[1], convert=to_binary, oneshot=True)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "Pointer": 0})
    compiled.patch({"Enable": True, "Pointer": 0})
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags[system.fault.address_error.name] is True
    assert compiled.current_state.tags.get(system.fault.out_of_range.name, False) is False


def test_compiled_plc_matches_plc_for_rtc_apply_and_system_points() -> None:
    plc = PLC([], dt=0.1)
    compiled = CompiledPLC(Program(strict=False), dt=0.1)

    base = datetime(2026, 1, 15, 10, 20, 30)
    plc.set_rtc(base)
    compiled.set_rtc(base)

    patch = {
        system.rtc.new_hour.name: 23,
        system.rtc.new_minute.name: 59,
        system.rtc.new_second.name: 58,
        system.rtc.apply_time.name: True,
    }
    plc.patch(patch)
    compiled.patch(patch)
    plc.step()
    compiled.step()
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)


def test_intra_rung_write_visible_to_timer_reset_in_compiled_kernel() -> None:
    """The compiled kernel reads a box's pins at box entry, after the wire above."""
    Enable = Bool("Enable")
    ResetBtn = Bool("ResetBtn")

    with Program(strict=False) as program:
        with Rung(Enable):
            latch(ResetBtn)
        with Rung(Enable).continued():
            on_delay(Timer[1], preset=100).reset(ResetBtn)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "ResetBtn": False})
    compiled.patch({"Enable": True, "ResetBtn": False})

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["ResetBtn"] is True
    assert compiled.current_state.tags["Timer_Acc"] == 0

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Timer_Acc"] == 0


def test_compiled_plc_matches_plc_for_continued_chain() -> None:
    """The compiled kernel reads a continued() rung live, after the rung above."""
    Enable = Bool("Enable")
    Latched = Bool("Latched")
    Output = Bool("Output")

    with Program(strict=False) as program:
        with Rung(Enable):
            out(Latched)
        with Rung(Latched).continued():
            out(Output)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "Latched": False, "Output": False})
    compiled.patch({"Enable": True, "Latched": False, "Output": False})

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Latched"] is True
    assert compiled.current_state.tags["Output"] is True

    plc.patch({"Enable": False})
    compiled.patch({"Enable": False})

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Latched"] is False
    assert compiled.current_state.tags["Output"] is False


def test_replay_to_prefers_compiled_path_when_supported() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            latch(light)

    source = PLC(program, dt=0.01)
    source.patch({"Enable": True})
    for _ in range(5):
        source.step()

    compiled_replay = source.replay_to(3)
    interpreted_replay = source._replay_to_interpreted(3)

    assert source._compiled_replay_supported_kernel() is not None
    _assert_states_equivalent(compiled_replay, interpreted_replay)
    assert dict(compiled_replay._input_overrides.forces_mutable) == dict(
        interpreted_replay._input_overrides.forces_mutable
    )


@pytest.mark.parametrize(
    ("history", "history_budget", "pad", "target", "floor"),
    [
        (None, 1 << 30, 0, 2900, 2900),
        (None, 1 << 30, 0, 2800, 2800),
        # 1300 scans on, the window of 1000 scans starts at the checkpoint after tip - 1000.
        (1000, 1 << 30, 0, 2900, 3200),
        (1000, 1 << 30, 0, 2800, 3000),
        # Six checkpoints of the padded state pass 1 MiB, so the floor moves one interval more.
        (1000, 1_048_576, 1000, 2900, 3400),
    ],
)
def test_both_replay_paths_keep_history_from_the_target(
    history: int | None, history_budget: int, pad: int, target: int, floor: int
) -> None:
    toggle = Bool("FloorToggle")
    count = Int("FloorCount")
    seen = Int("FloorSeen")

    with Program(strict=False) as program:
        with Rung(~toggle):
            out(toggle)
        with Rung(toggle):
            math(count + 1, count)
        with Rung(count == 1450):
            copy(7, seen)

    source = PLC(
        program,
        dt=0.01,
        history=history,
        checkpoint_interval=200,
        history_budget=history_budget,
        initial_state=SystemState().with_tags({f"FloorPad{k}": 0 for k in range(pad)}),
    )
    for _ in range(3000):
        source.step()

    kernel = source._replay_kernel_from(target - 1)
    assert kernel is not None
    forks = [source._replay_to_interpreted(target), source._replay_to_compiled(target, kernel)]
    assert [fork.history.oldest_scan_id for fork in forks] == [target, target]
    truth = source.rung_firings(target)
    assert truth
    assert [fork.rung_firings(target) for fork in forks] == [truth, truth]
    assert [fork._rung_firing_timelines.at(target - 1) for fork in forks] == [{}, {}]
    for scans in (150, 850, 300):
        for fork in forks:
            for _ in range(scans):
                fork.step()
        floors = [fork.history.oldest_scan_id for fork in forks]
        assert floors[0] == floors[1]
        if scans == 150:
            assert forks[0].query.cold_rungs() == forks[1].query.cold_rungs()
    assert floors == [floor, floor]
    _assert_states_equivalent(*forks)
    tip = forks[0].state.scan_id
    assert forks[0].rung_firings(tip - 1) == forks[1].rung_firings(tip - 1)


def _replay_both_ways(source: PLC, target: int) -> list[PLC]:
    kernel = source._replay_kernel_from(target - 1)
    assert kernel is not None
    return [source._replay_to_interpreted(target), source._replay_to_compiled(target, kernel)]


def test_a_replay_of_a_checkpoint_keeps_a_stop_made_after_it() -> None:
    toggle = Bool("StopToggle")
    with Program(strict=False) as program:
        with Rung(~toggle):
            out(toggle)
    source = PLC(program, dt=0.01, checkpoint_interval=100)
    for _ in range(200):
        source.step()
    source.stop()

    for fork in _replay_both_ways(source, 200):
        assert not fork._running
        assert dict(fork.state.memory) == dict(source.state.memory)


def test_a_replay_of_a_checkpoint_steps_on_with_a_battery_change_made_after_it() -> None:
    toggle, seen = Bool("BatteryToggle"), Bool("BatterySeen")
    with Program(strict=False) as program:
        with Rung(~toggle):
            out(toggle)
        with Rung(system.sys.battery_present):
            out(seen)
    source = PLC(program, dt=0.01, checkpoint_interval=100)
    for _ in range(200):
        source.step()
    source.battery_present = False
    live = {}
    for _ in range(50):
        source.step()
        live[source.state.scan_id] = source.state

    for fork in _replay_both_ways(source, 200):
        assert not fork._battery_present
        for scan in range(201, 251):
            fork.step()
            assert dict(fork.state.tags) == dict(live[scan].tags), scan
            assert dict(fork.state.memory) == dict(live[scan].memory), scan


def test_interpreted_replay_preserves_active_pilot_holds() -> None:
    from pyrung.core.analysis.pilot.overlay import PilotRung, _set_pilot_rungs

    held_input = Bool("ReplayHeldInput", default=True, external=True)
    hold_scope = Bool("ReplayHoldScope", default=True)
    alarm = Bool("ReplayHoldAlarm")

    with Program(strict=False) as program:
        with Rung(hold_scope, ~held_input):
            latch(alarm)

    source = PLC(program, dt=0.01, cache=0, checkpoint_interval=10_000)
    _set_pilot_rungs(source, [PilotRung(held_input.name, False, hold_scope)])
    recorded = [source.step(), source.step()]

    kernel = source._compiled_replay_supported_kernel()
    assert kernel is not None
    interpreted = source._replay_range_interpreted(1, 2)
    compiled = source._replay_range_compiled(1, 2, kernel)

    def semantic(states):
        return [
            (
                state.scan_id,
                state.tags[held_input.name],
                state.tags[alarm.name],
            )
            for state in states
        ]

    assert semantic(recorded) == [(1, False, True), (2, False, True)]
    assert semantic(interpreted) == semantic(recorded)
    assert semantic(compiled) == semantic(recorded)


def test_history_at_and_replay_range_use_compiled_path_when_supported(monkeypatch) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            latch(light)

    source = PLC(program, dt=0.01)
    source.patch({"Enable": True})
    for _ in range(6):
        source.step()

    expected = source._replay_to_interpreted(2).current_state
    expected_range = source._replay_range_interpreted(2, 4)

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)

    def _boom_replay(_scan_id: int) -> PLC:
        raise AssertionError("interpreted replay path should not be used")

    def _boom_range(_start: int, _end: int) -> list:
        raise AssertionError("interpreted replay range path should not be used")

    monkeypatch.setattr(source, "_replay_to_interpreted", _boom_replay)
    monkeypatch.setattr(source, "_replay_range_interpreted", _boom_range)

    assert source.history.at(2) == expected
    assert source.history.range(2, 5) == expected_range


def test_replay_to_falls_back_for_unsupported_program() -> None:
    enable = Bool("Enable")

    with Program(strict=False) as program:
        with Rung(enable):
            run_function(time.time)

    source = PLC(program, dt=0.01)
    source.patch({"Enable": True})
    source.step()
    source.step()

    replay = source.replay_to(2)
    interpreted = source._replay_to_interpreted(2)

    assert source._compiled_replay_supported_kernel() is None
    _assert_states_equivalent(replay, interpreted)


def test_a_replay_of_a_world_the_kernel_cannot_run_lands_interpreted() -> None:
    enable = Bool("UnsupportedSeekEnable")

    with Program(strict=False) as program:
        with Rung(enable):
            run_function(time.time)

    source = PLC(program, dt=0.01)
    source.patch({"UnsupportedSeekEnable": True})
    source.run(5)

    replay = source.replay_to(4)

    assert replay.state.scan_id == 4
    assert source._compiled_replay_supported_kernel() is None


def test_compiled_plc_with_prebuilt_kernel_does_not_walk_program(monkeypatch) -> None:
    """When a prebuilt kernel is supplied, construction must not re-walk the
    program graph — the materialized tag set is already on the kernel."""
    from pyrung.circuitpy.codegen import render_kernel

    enable = Bool("Enable")
    light = Bool("Light")

    with Program() as program:
        with Rung(enable):
            out(light)

    kernel = compile_kernel(program)
    assert "Light" in kernel.materialized_tag_names
    assert "Enable" in kernel.materialized_tag_names

    def _boom_collect(_program):
        raise AssertionError("program graph walk should not run when compiled= is supplied")

    monkeypatch.setattr(render_kernel, "_collect_materialized_tag_names", _boom_collect)

    plc = CompiledPLC(program, compiled=kernel)
    assert plc.current_state.scan_id == 0
