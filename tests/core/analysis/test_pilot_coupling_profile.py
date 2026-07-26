"""Reader half of the coupling/when().do() unification.

An analog harness coupling (``En`` drives a sensor register toward a read
threshold) exposes an :class:`AdvanceProfile` that PILOT's ownership index
consumes exactly like a timer — so ``how(Fb >= threshold)`` can learn "hold En,
coast N scans" by *reading*, no execution.  These are unit tests of the resolver
plumbing; wiring it into the planner is a separate step.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Real, Rung, copy
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.crossing import Cmp
from pyrung.core.harness import Harness
from pyrung.core.physical import Approach, Physical, Ramp
from pyrung.core.runner import PLC

# +1.0 units/s while enabled, -0.5/s decaying.
SENSOR = Physical("TempSensor", profile=Ramp(up=1.0, down=-0.5))


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
    owner = build_advance_index(prog, harness=plc._harness).resolve("Temp")
    assert owner is not None
    assert owner.profile.accumulator is not None
    assert owner.profile.accumulator.name == "Temp"
    assert owner.profile.done is None
    assert owner.profile.linear is not None
    assert owner.profile.linear.direction == 1


def test_advance_condition_reads_enable() -> None:
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    plc, prog = _installed_plc()
    owner = build_advance_index(prog, harness=plc._harness).resolve("Temp")
    assert owner is not None
    step = owner.profile.plan(Cmp("Temp", ">=", 5), plc.state.tags)
    assert step is not None
    # The next operation names the real driver condition PILOT will hold.
    assert _extract_reads_from_condition(step.holds[0].condition, {}) == {"Enable"}


def test_no_harness_excludes_couplings() -> None:
    # default (no harness arg) keeps the program-only behaviour for every
    # existing caller — couplings are opt-in.
    _plc, prog = _installed_plc()
    assert build_advance_index(prog).resolve("Temp") is None


def test_nonlinear_profile_falls_back_to_empirical() -> None:
    # A first-order Approach (slope depends on the current value) cannot be
    # solved analytically: rate_per_scan raises, scans_until -> None.
    fo_sensor = Physical("FoSensor", profile=Approach(toward=10.0, rate=0.4))
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=fo_sensor, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    plc = PLC(prog, dt=0.010)
    Harness(plc).install()

    owner = build_advance_index(prog, harness=plc._harness).resolve("Temp")
    assert owner is not None
    assert owner.profile.linear is not None
    assert (
        owner.profile.linear.estimate_scans(Cmp("Temp", ">=", 5), plc.state.tags, plc._dt) is None
    )


# ── 1b: planner wiring — the driver hold is attached + the coast solves ──────


def test_how_threshold_solves_with_driver_not_in_goal() -> None:
    """``how(Temp >= 5.0)`` with the driver (Enable) NOT named in the goal.

    The blind coast leaf can't solve this — nothing tells it to hold Enable.
    Reading the coupling attaches Enable as a steerable prerequisite, and the
    terminal let-run coasts the ramp to the threshold.
    """
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=SENSOR, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    plc = PLC(prog, dt=0.010)

    path = pilot_how(plc, Temp >= 5.0, max_scans=3000)
    assert path.reachable


def test_how_threshold_path_replays() -> None:
    """The recorded path is self-describing: the steady driver hold (Enable) is
    recorded on the coast step, so a bare replay reproduces the ramp."""
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=SENSOR, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)

    path = pilot_how(PLC(prog, dt=0.010), Temp >= 5.0, max_scans=3000)
    assert path.reachable

    replay = path.replay()
    assert replay.state.tags["Temp"] >= 5.0
