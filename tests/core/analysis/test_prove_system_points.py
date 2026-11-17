"""System tags in how()'s static facts.

The compiled step function never sets system tags.  The runner sets them
around it, so the facts leave them out of the inputs.  A system tag the runtime
writes holds every value the runtime gives it (``RUNTIME_VALUES``).
"""

from __future__ import annotations

from datetime import datetime

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    copy,
    latch,
    math,
    out,
    system,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.drive_setup import build_prover_context
from pyrung.core.analysis.pilot.program_facts import compute_reference_constants
from pyrung.core.analysis.prove import (
    _build_prior_facts,
)
from pyrung.core.analysis.prove.absorb import _is_stable_threshold, _stable_int_tag_value
from pyrung.core.analysis.prove.classify import (
    _collect_all_exprs,
    _collect_structural_domain_info,
)
from pyrung.core.system_points import (
    _DERIVED_TAG_NAMES,
    HOOK_BOUND_TAG_NAMES,
    READ_ONLY_SYSTEM_TAG_NAMES,
    RUNTIME_VALUES,
    SYSTEM_TAGS_BY_NAME,
)
from pyrung.core.tag import tag_written_from_outside

S = system.sys
F = system.fault
R = system.rtc


class TestFreeSystemPoints:
    def test_how_domain_prior_keeps_system_tags_out_of_inputs(self):
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as logic:
            with Rung(S.clock_1s, S.scan_counter == 3, enable):
                out(hit)
        ctx = _build_prior_facts(logic)
        assert not set(ctx.nondeterministic_dims) & set(SYSTEM_TAGS_BY_NAME)


def _divisor(name: str, *, zero: bool = True) -> Int:
    choices = {1: "one", 0: "zero"} if zero else {1: "one", 2: "two"}
    return Int(name, external=True, choices=choices, default=1)


def _structural_domains(logic: Program) -> dict:
    graph = build_program_graph(logic)
    domains, _ = _collect_structural_domain_info(logic, graph, _collect_all_exprs(logic, graph))
    return domains


class TestRuntimeValues:
    """``RUNTIME_VALUES`` covers every value the runtime gives a system tag."""

    def test_lists_every_tag_the_runtime_moves(self):
        moved = (_DERIVED_TAG_NAMES | HOOK_BOUND_TAG_NAMES) & READ_ONLY_SYSTEM_TAG_NAMES
        # These four read 0 always, which is their default.
        constant = {name for name in moved if name.startswith("firmware.")}
        constant.add(S.interrupt_scan_time_ms.name)
        assert moved - constant <= RUNTIME_VALUES.keys()
        assert RUNTIME_VALUES.keys() <= READ_ONLY_SYSTEM_TAG_NAMES

    def test_holds_what_the_runtime_writes(self):
        quotient = Int("Quotient")
        divisor = _divisor("Divisor")
        with Program(strict=False) as logic:
            with Rung():
                math(100 / divisor, quotient)
        starts = (
            datetime(2026, 12, 31, 23, 59, 59),
            datetime(2024, 2, 29, 0, 0, 0),
            datetime(1999, 7, 4, 12, 30, 1),
        )
        for start in starts:
            plc = PLC(logic, dt=0.010)
            plc.set_rtc(start)
            for scan in range(150):
                plc.patch({divisor.name: scan % 2})
                plc.step()
                for name in SYSTEM_TAGS_BY_NAME:
                    found, value = plc.debug.system_runtime.resolve(name, plc.current_state)
                    assert found
                    values = RUNTIME_VALUES.get(name, (SYSTEM_TAGS_BY_NAME[name].default,))
                    if values is not None:
                        assert value in values, (start, scan, name, value)


class TestHowPrior:
    """how()'s facts hold every value the runtime gives a system tag."""

    def test_structural_domains_give_rtc_its_range(self):
        stamp = Int("Stamp")
        with Program(strict=False) as logic:
            with Rung():
                copy(R.month, stamp)
        domains = _structural_domains(logic)
        assert domains["rtc.month"] == tuple(range(1, 13))
        assert set(range(1, 13)) <= set(domains["Stamp"])

    def test_copy_of_scan_counter_has_no_finite_domain(self):
        stamp = Int("Stamp")
        with Program(strict=False) as logic:
            with Rung():
                copy(S.scan_counter, stamp)
        domains = _structural_domains(logic)
        assert "sys.scan_counter" not in domains
        assert "Stamp" not in domains

    def test_always_on_is_true(self):
        held = Bool("Held")
        with Program(strict=False) as logic:
            with Rung(S.always_on):
                out(held)
        assert _structural_domains(logic)["sys.always_on"] == (True,)

    def test_fault_flag_stays_out_of_the_prior(self):
        divisor = _divisor("Divisor")
        quotient = Int("Quotient")
        alarm = Bool("Alarm")
        with Program(strict=False) as logic:
            with Rung():
                math(100 / divisor, quotient)
            with Rung(F.division_error):
                latch(alarm)
        ctx = _build_prior_facts(logic)
        assert "fault.division_error" not in ctx.stateful_dims
        assert "fault.division_error" not in ctx.nondeterministic_dims
        plc = PLC(logic, dt=0.010)
        plc.step()
        prior = build_prover_context(logic)
        key = prior.key_config.stateful_names if prior.key_config is not None else ()
        assert "fault.division_error" not in key


class TestNotAConstant:
    """A tag with no ladder writer is a constant only when nothing outside the ladder writes it."""

    def test_runtime_tags_are_written_from_outside(self):
        assert tag_written_from_outside(R.hour)
        assert tag_written_from_outside(S.scan_counter)
        assert tag_written_from_outside(S.always_on)
        # The runtime gives these only their default.
        assert not tag_written_from_outside(system.firmware.main_ver_low)
        assert not tag_written_from_outside(R.new_hour)
        assert not tag_written_from_outside(Int("Plain"))

    @staticmethod
    def _threshold_graph():
        fed = Int("Fed", link="Done")
        plain = Int("Plain", default=5)
        done = Bool("Done")
        with Program(strict=False) as logic:
            with Rung(R.second >= 30, S.scan_counter > 5, fed > plain):
                out(done)
        return build_program_graph(logic)

    def test_is_stable_threshold(self):
        graph = self._threshold_graph()
        assert _is_stable_threshold("Plain", graph)
        assert not _is_stable_threshold("rtc.second", graph)
        assert not _is_stable_threshold("sys.scan_counter", graph)
        assert not _is_stable_threshold("Fed", graph)

    def test_stable_int_tag_value(self):
        graph = self._threshold_graph()
        assert _stable_int_tag_value("Plain", graph, {}) == 5
        assert _stable_int_tag_value("rtc.second", graph, {}) is None
        assert _stable_int_tag_value("sys.scan_counter", graph, {}) is None
        assert _stable_int_tag_value("Fed", graph, {}) is None

    def test_reference_constants(self):
        # The named-state idiom of a reference constant: copy(Ref, State), then State == Ref.
        state = Int("State")
        ref = Int("Ref", default=3)
        lamp, clock_lamp = Bool("Lamp"), Bool("ClockLamp")
        with Program(strict=False) as logic:
            with Rung():
                copy(ref, state)
                copy(R.hour, state)
            with Rung(state == ref):
                out(lamp)
            with Rung(state == R.hour):
                out(clock_lamp)
        graph = build_program_graph(logic)
        assert compute_reference_constants(graph, logic, graph.tags) == {"Ref"}
