"""System tags in how()'s static facts.

The compiled step function never sets system tags.  The runner sets them
around it, so the facts leave them out of the inputs and hold them constant.
"""

from __future__ import annotations

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
from pyrung.core.analysis.prove import (
    _build_prior_facts,
)
from pyrung.core.analysis.prove.classify import (
    _collect_all_exprs,
    _collect_structural_domain_info,
)
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME

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


class TestHowPrior:
    """how()'s facts hold system tags constant."""

    def test_structural_domains_keep_rtc_constant(self):
        stamp = Int("Stamp")
        with Program(strict=False) as logic:
            with Rung():
                copy(R.month, stamp)
        graph = build_program_graph(logic)
        exprs = _collect_all_exprs(logic, graph)
        domains, _ = _collect_structural_domain_info(logic, graph, exprs)
        assert domains["rtc.month"] == (0,)

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
