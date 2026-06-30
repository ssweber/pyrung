"""Reader half of the coupling/when().do() unification.

An analog harness coupling (``En`` drives a sensor register toward a read
threshold) exposes an :class:`AccProfile` that PILOT's accumulator resolver
consumes exactly like a timer — so ``how(Fb >= threshold)`` can learn "hold En,
coast N scans" by *reading*, no execution.  These are unit tests of the resolver
plumbing; wiring it into the planner is a separate step.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Real, Rung, copy
from pyrung.core.analysis.pilot.accumulators import resolve_profile, scans_to_eject
from pyrung.core.harness import Harness, _profile_registry
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

SENSOR = Physical("TempSensor", profile="coupling_reader_thermal")

if "coupling_reader_thermal" not in _profile_registry:

    def _thermal(cur: float, en: bool, dt: float) -> float:
        return cur + (1.0 if en else -0.5) * dt  # +1.0 units/s while enabled

    _profile_registry["coupling_reader_thermal"] = _thermal


def _installed_plc() -> tuple[PLC, object]:
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=SENSOR, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    plc = PLC(prog, dt=0.010)
    Harness(plc).install()
    return plc, prog


def test_analog_coupling_resolves_via_accumulator() -> None:
    plc, prog = _installed_plc()
    match = resolve_profile("Temp", prog, harness=plc._harness)
    assert match is not None
    # analog: matched via the Fb register (Temp >= 5.0), not a done bit
    assert match.via_done is False
    assert match.profile.accumulator.name == "Temp"
    assert match.profile.direction == 1


def test_analog_coupling_scans_to_eject_is_analytic() -> None:
    plc, prog = _installed_plc()
    match = resolve_profile("Temp", prog, harness=plc._harness)
    assert match is not None
    # +1.0 units/s * 0.01 dt = 0.01/scan; 5.0 / 0.01 = 500 scans from cold
    assert scans_to_eject(match, plc, threshold=5) == 500


def test_advance_condition_reads_enable() -> None:
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    plc, prog = _installed_plc()
    match = resolve_profile("Temp", prog, harness=plc._harness)
    assert match is not None
    # advance must be a real Condition naming the driver PILOT will hold
    assert _extract_reads_from_condition(match.profile.advance, {}) == {"Enable"}


def test_no_harness_excludes_couplings() -> None:
    # default (no harness arg) keeps the program-only behaviour for every
    # existing caller — couplings are opt-in.
    _plc, prog = _installed_plc()
    assert resolve_profile("Temp", prog) is None


def test_nonlinear_profile_falls_back_to_empirical() -> None:
    # A first-order profile (slope depends on the current value) cannot be
    # solved analytically: rate_per_scan raises, scans_until -> None.  Without a
    # fork, scans_to_eject returns None (the caller would then measure).
    if "coupling_reader_firstorder" not in _profile_registry:

        def _first_order(cur: float, en: bool, dt: float) -> float:
            return cur + (10.0 - cur) * 0.4 * dt if en else cur

        _profile_registry["coupling_reader_firstorder"] = _first_order

    fo_sensor = Physical("FoSensor", profile="coupling_reader_firstorder")
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=fo_sensor, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    plc = PLC(prog, dt=0.010)
    Harness(plc).install()

    match = resolve_profile("Temp", prog, harness=plc._harness)
    assert match is not None
    assert scans_to_eject(match, plc, threshold=5) is None  # no fork → empirical declines
