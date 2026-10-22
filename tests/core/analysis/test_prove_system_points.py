"""System points in the prover kernel (see prove/system_points.py).

The compiled step function never sets system tags; the runner does around
it.  Each test pins one runner behaviour the prover must reproduce, checked
against the concrete PLC.
"""

from __future__ import annotations

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    copy,
    fall,
    latch,
    out,
    rise,
    system,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Proven,
    TraceStep,
    _build_explore_context,
    always,
)
from pyrung.core.analysis.prove.passes import _OptConfig
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

S = system.sys


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_trace_replays(program: Program, result: Counterexample, tag_name: str) -> None:
    plc = _replay_trace(program, result.trace)
    assert plc.current_state.tags[tag_name] is True


def _first_scan_gate() -> tuple[Program, Bool]:
    enable = Bool("Enable", external=True)
    hit = Bool("Hit")
    with Program(strict=False) as logic:
        with Rung(S.first_scan, enable):
            out(hit)
    return logic, hit


class TestFirstScan:
    def test_first_scan_gate_is_reachable(self):
        logic, hit = _first_scan_gate()
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_rise_of_first_scan_fires_on_scan_zero(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(rise(S.first_scan), enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_fall_of_first_scan_fires_on_scan_one(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(fall(S.first_scan), enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_first_scan_initializes_values(self):
        enable = Bool("Enable", external=True)
        speed = Int("Speed")
        limit = Int("Limit")
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.first_scan):
                copy(5, speed)
                copy(7, limit)
            with Rung(speed == 5, limit == 7, enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_first_scan_is_false_after_scan_zero(self):
        enable = Bool("Enable", external=True)
        armed = Bool("Armed")
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.first_scan):
                latch(armed)
            with Rung(~S.first_scan, armed, enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_first_scan_never_fires_twice(self):
        seen = Bool("Seen")
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.first_scan, seen):
                out(hit)
            with Rung(S.first_scan):
                latch(seen)
        assert isinstance(always(logic, ~hit), Proven)


class TestConstantSystemPoints:
    def test_always_on_reads_true(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.always_on, enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")

    def test_always_on_never_reads_false(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(~S.always_on, enable):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    def test_mode_run_is_constant_without_a_stop_command(self):
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(~S.mode_run):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    def test_mode_run_is_free_when_the_program_can_stop(self):
        stop = Bool("Stop", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(~S.mode_run):
                out(hit)
            with Rung(stop):
                out(S.cmd_mode_stop)
        # One scan runs with mode_run False after cmd_mode_stop is written.
        plc = PLC(logic, dt=0.010)
        plc.patch({"Stop": True})
        plc.step()
        plc.step()
        assert plc.current_state.tags["Hit"] is True
        assert isinstance(always(logic, ~hit), Counterexample)


class TestFreeSystemPoints:
    def test_clock_gate_is_reachable(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.clock_100ms, enable):
                out(hit)
        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True})
        plc.run(cycles=6)
        assert plc.current_state.tags["Hit"] is True
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        assert any("sys.clock_100ms" in caveat for caveat in result.caveats)

    def test_scan_counter_gate_is_reachable(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.scan_counter == 3, enable):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        assert any("sys.scan_counter" in caveat for caveat in result.caveats)

    def test_scan_clock_toggle_edge_is_reachable(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(rise(S.scan_clock_toggle), enable):
                out(hit)
        assert isinstance(always(logic, ~hit), Counterexample)

    def test_unbounded_scan_counter_copy_is_not_proven(self):
        level = Int("Level")
        with Program(strict=False) as logic:
            with Rung():
                copy(S.scan_counter, level)
        assert not isinstance(always(logic, level == 0), Proven)

    def test_how_domain_prior_keeps_system_tags_out_of_inputs(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.clock_1s, S.scan_counter == 3, enable):
                out(hit)
        from dataclasses import replace

        ctx = _build_explore_context(logic, _opt_config=replace(_OptConfig(), domains_only=True))
        assert not set(ctx.nondeterministic_dims) & set(SYSTEM_TAGS_BY_NAME)


class TestScanStartResets:
    def test_rtc_apply_request_is_cleared_before_the_next_scan(self):
        request = Bool("Request", external=True)
        phase = Bool("Phase")
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(~system.rtc.apply_date, phase):
                out(hit)
            with Rung(request):
                latch(system.rtc.apply_date)
            with Rung(system.rtc.apply_date):
                latch(phase)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        _assert_trace_replays(logic, result, "Hit")
