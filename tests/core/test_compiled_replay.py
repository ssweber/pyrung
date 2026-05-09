from __future__ import annotations

import time
from datetime import datetime

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    blockcopy,
    calc,
    call,
    copy,
    fill,
    on_delay,
    out,
    rise,
    run_function,
    search,
    shift,
    subroutine,
    system,
    to_binary,
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
            copy(True, light)

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
            calc(index + 1, index)
        with Rung(enable):
            copy(0, ds[ds[1]])

    runner = CompiledPLC(program)

    assert runner.current_state.tags["DS1"] == 0


def test_compiled_plc_does_not_seed_static_block_ranges_from_compiler_cache() -> None:
    enable = Bool("Enable")
    ds = Block("DS", TagType.INT, 1, 12)

    with Program(strict=False) as program:
        with Rung(enable):
            blockcopy(ds.select(1, 3), ds.select(10, 12))

    compiled = compile_kernel(program)
    runner = CompiledPLC(program, compiled=compiled)

    assert not set(runner.current_state.tags).intersection(
        {"DS1", "DS2", "DS3", "DS10", "DS11", "DS12"}
    )


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


def test_compiled_plc_matches_plc_for_initial_and_first_scan_system_runtime_defaults() -> None:
    program = Program(strict=False)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    _assert_states_equivalent(plc, compiled)

    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)


def test_compiled_plc_matches_plc_for_patch_force_and_prev_capture() -> None:
    enable = Bool("Enable")
    reset_tag = Bool("Reset")

    with Program(strict=False) as program:
        with Rung(enable):
            copy(True, reset_tag)
            on_delay(Timer[1], preset=50).reset(reset_tag)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "Reset": False})
    compiled.patch({"Enable": True, "Reset": False})
    plc.step()
    compiled.step()

    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.memory["_prev:Reset"] is True


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

    plc.patch({"Enable": True, "Pointer": 999})
    compiled.patch({"Enable": True, "Pointer": 999})
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


def test_intra_rung_write_not_visible_to_timer_reset_in_compiled_kernel() -> None:
    """Regression: compiled kernel must snapshot helper conditions at rung entry."""
    Enable = Bool("Enable")
    ResetBtn = Bool("ResetBtn")

    with Program(strict=False) as program:
        with Rung(Enable):
            copy(True, ResetBtn)
            on_delay(Timer[1], preset=100).reset(ResetBtn)

    plc = PLC(program, dt=0.010)
    compiled = CompiledPLC(program, dt=0.010)

    plc.patch({"Enable": True, "ResetBtn": False})
    compiled.patch({"Enable": True, "ResetBtn": False})

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["ResetBtn"] is True
    assert compiled.current_state.tags["Timer_Acc"] == 10

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Timer_Acc"] == 0


def test_compiled_plc_matches_plc_for_continued_snapshot_chain() -> None:
    """Regression: compiled kernel must reuse the anchor snapshot for continued()."""
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
    assert compiled.current_state.tags["Output"] is False

    plc.patch({"Enable": False})
    compiled.patch({"Enable": False})

    plc.step()
    compiled.step()
    _assert_states_equivalent(plc, compiled)
    assert compiled.current_state.tags["Latched"] is False
    assert compiled.current_state.tags["Output"] is True


def test_replay_to_prefers_compiled_path_when_supported() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            copy(True, light)

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


def test_history_at_and_replay_range_use_compiled_path_when_supported(monkeypatch) -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as program:
        with Rung(enable):
            copy(True, light)

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
