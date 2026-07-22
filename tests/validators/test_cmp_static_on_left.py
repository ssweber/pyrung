"""Specification tests for CMP_STATIC_ON_LEFT.

Convention: the moving/computed value on the left, the expectation on the right,
the operator pointing the way the value moves (``Timer.Acc >= Setpoint``, not
``Setpoint <= Timer.Acc``).  "Dynamic" (belongs on the left) is any
program-written tag, self-advancing register, or inline computed expression;
"static" is a literal, an ``S.`` constant, or a never-written setpoint/external
tag.  The verdict is driven by the writer-membership index; calc-derived
provenance only sharpens the message.

Three tiers, plus the escalation into CMP_TRUE_AT_RESET when the right operand is
a monotone-from-zero register:

1. ``==`` / ``!=`` static-left → info (same predicate either way).
2. ordered operator, dynamic right → warning (flip, which reverses the operator).
3. ordered operator, monotone register right → CMP_TRUE_AT_RESET when true at
   reset, else the tier-2 flip warning.

Note: Python reflects comparison operators (``5 < tag`` becomes ``tag > 5``), so
the meaningful case is a static *tag* on the left, which ``Tag.__lt__`` preserves.
"""

from __future__ import annotations

from pyrung.core import Bool, Int, Program, Rung, Timer, calc, copy, on_delay
from pyrung.core.validation.report import validate


def _codes(report, code):
    return [f for f in report if f.code == code]


# --- Tier 1: == / != static-left -------------------------------------------


def _eq_info_program() -> Program:
    lo_limit = Int("LoLimit")  # never written, not external → a static constant
    running = Int("Running")  # program-written below → dynamic
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung(out):
            copy(1, running)
        with Rung(lo_limit == running):
            copy(1, out)
    return prog


class TestTier1Info:
    def test_static_eq_dynamic_is_info(self):
        report = validate(_eq_info_program())
        sol = _codes(report, "CMP_STATIC_ON_LEFT")
        assert len(sol) == 1
        assert sol[0].severity == "info"
        assert "Running == LoLimit" in sol[0].message


# --- Tier 2: ordered, dynamic right ----------------------------------------


def _calc_right_program() -> Program:
    lo_limit = Int("LoLimit")  # static constant
    a = Int("A", external=True)
    b = Int("B", external=True)
    calc_out = Int("CalcOut")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung(out):
            calc(a + b, calc_out)
        with Rung(lo_limit < calc_out):
            copy(1, out)
    return prog


def _computed_right_program() -> Program:
    lo_limit = Int("LoLimit")  # static constant
    a = Int("A", external=True)
    b = Int("B", external=True)
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung(lo_limit < (a + b)):
            copy(1, out)
    return prog


class TestTier2Maybe:
    """Two ordinary tags — the analyzer can't prove which is measurement vs
    threshold, so these are advisory 'maybe' findings, not warnings."""

    def test_calc_tag_right_is_advisory_and_calls_it_calculated(self):
        report = validate(_calc_right_program())
        sol = _codes(report, "CMP_STATIC_ON_LEFT")
        assert len(sol) == 1
        assert sol[0].severity == "advisory"
        assert "calculated" in sol[0].message
        assert "CalcOut > LoLimit" in sol[0].message

    def test_inline_computed_right_is_advisory(self):
        report = validate(_computed_right_program())
        sol = _codes(report, "CMP_STATIC_ON_LEFT")
        assert len(sol) == 1
        assert sol[0].severity == "advisory"
        assert "(A + B) > LoLimit" in sol[0].message


# --- Tier 3: monotone register right ---------------------------------------


def _escalation_program() -> Program:
    """``Setpoint > Acc`` with the preset = Setpoint → escalates to TRUE_AT_RESET."""
    setpoint = Int("Setpoint", external=True)
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, setpoint, "sec")
        with Rung(setpoint > tmr.Acc):
            copy(1, out)
    return prog


def _fallback_program() -> Program:
    """``LoLimit < Acc`` — false at reset, so it falls back to the flip warning."""
    lo_limit = Int("LoLimit")  # static constant, also the preset
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung():
            on_delay(tmr, lo_limit, "sec")
        with Rung(lo_limit < tmr.Acc):
            copy(1, out)
    return prog


class TestTier3Escalation:
    def test_true_at_reset_claims_the_comparison(self):
        report = validate(_escalation_program())
        assert _codes(report, "CMP_TRUE_AT_RESET")
        # The behavioral finding subsumes the operand-order nit — no double report.
        assert not _codes(report, "CMP_STATIC_ON_LEFT")

    def test_not_true_at_reset_is_known_warning_on_accumulator(self):
        # Right side is provably the accumulator — a KNOWN order issue, warning.
        report = validate(_fallback_program())
        assert not _codes(report, "CMP_TRUE_AT_RESET")
        sol = _codes(report, "CMP_STATIC_ON_LEFT")
        assert len(sol) == 1
        assert sol[0].severity == "warning"
        assert "Tmr.Acc > LoLimit" in sol[0].message


# --- Negative controls ------------------------------------------------------


def _correct_forms_program() -> Program:
    limit = Int("Limit", external=True)
    a = Int("A", external=True)
    b = Int("B", external=True)
    calc_out = Int("CalcOut")
    calc_a = Int("CalcA")
    calc_b = Int("CalcB")
    tmr = Timer.clone("Tmr")
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung(out):
            calc(a + b, calc_out)
        with Rung(out):
            calc(a + 1, calc_a)
        with Rung(out):
            calc(b + 1, calc_b)
        with Rung():
            on_delay(tmr, 5, "sec")
        # dynamic (calc) on left, literal right — correct
        with Rung(calc_out > 3):
            copy(1, out)
        # dynamic (accumulator) on left, static right — correct (bench form)
        with Rung(tmr.Acc > limit):
            copy(1, out)
        # dynamic vs dynamic — no canonical side, exempt
        with Rung(calc_a > calc_b):
            copy(1, out)
    return prog


def _external_sensor_program() -> Program:
    """An external sensor on the left, a calc-derived band on the right.  The analyzer
    cannot prove the sensor is the mover (no accumulator), so this is a 'maybe'."""
    dryer_temp = Int("DryerTemp", external=True)  # sensor input
    setpoint = Int("Setpoint", external=True)
    band = Int("LowBandTemp")  # calc-derived threshold — written
    out = Bool("Out", external=True)
    with Program(strict=False) as prog:
        with Rung(out):
            calc(setpoint - 5, band)
        with Rung(dryer_temp < band):
            copy(1, out)
    return prog


class TestNoFalsePositive:
    def test_correct_and_exempt_forms_produce_no_finding(self):
        report = validate(_correct_forms_program())
        assert not _codes(report, "CMP_STATIC_ON_LEFT")


class TestMaybeGrading:
    def test_external_sensor_is_advisory_not_a_gate_failure(self):
        # No accumulator to anchor the verdict, so `sensor < band` surfaces as a
        # hedged advisory — visible, but never in the error/warning CI gate.
        report = validate(_external_sensor_program())
        sol = _codes(report, "CMP_STATIC_ON_LEFT")
        assert len(sol) == 1
        assert sol[0].severity == "advisory"
        assert not report.errors()
        assert not report.warnings()
