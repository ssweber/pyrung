from __future__ import annotations

import time
from datetime import datetime

from pyrung.circuitpy.codegen import compile_kernel
from pyrung.core import (
    PLC,
    Bool,
    CompiledPLC,
    Program,
    Rung,
    Timer,
    copy,
    on_delay,
    run_function,
    system,
)


def _assert_states_equivalent(left: PLC | CompiledPLC, right: PLC | CompiledPLC) -> None:
    left_state = left.current_state
    right_state = right.current_state
    assert left_state.scan_id == right_state.scan_id
    assert left_state.timestamp == right_state.timestamp
    assert dict(left_state.tags) == dict(right_state.tags)
    assert dict(left_state.memory) == dict(right_state.memory)


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
    classic_replay = source._replay_to_classic(3)

    assert source._compiled_replay_supported_kernel() is not None
    _assert_states_equivalent(compiled_replay, classic_replay)
    assert dict(compiled_replay._input_overrides.forces_mutable) == dict(
        classic_replay._input_overrides.forces_mutable
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

    expected = source._replay_to_classic(2).current_state
    expected_range = source._replay_range_classic(2, 4)

    source._recent_state_cache.clear()
    source._recent_state_cache_bytes = 0
    source._cache_state(source.current_state)

    def _boom_replay(_scan_id: int) -> PLC:
        raise AssertionError("classic replay path should not be used")

    def _boom_range(_start: int, _end: int) -> list:
        raise AssertionError("classic replay range path should not be used")

    monkeypatch.setattr(source, "_replay_to_classic", _boom_replay)
    monkeypatch.setattr(source, "_replay_range_classic", _boom_range)

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
    classic = source._replay_to_classic(2)

    assert source._compiled_replay_supported_kernel() is None
    _assert_states_equivalent(replay, classic)
