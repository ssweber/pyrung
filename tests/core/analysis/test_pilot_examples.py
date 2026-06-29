"""PILOT integration against the shipped ``examples/`` programs.

These drive ``pilot_how`` end-to-end on realistic programs.  Cases PILOT cannot
yet solve are marked ``xfail`` with a ``pilot:`` reason (matching the convention
in ``test_packml_diagnosis`` / ``test_graph_semantic_path``).  They assert the
behaviour PILOT *should* have, so the day it gains the capability the test
xpasses and flags the gap as closed.
"""

from __future__ import annotations

# The example modules run an import-time simulation unless this is set; PILOT
# only needs the program object, so skip the demo run.
import os

os.environ.setdefault("PYRUNG_DAP_ACTIVE", "1")

from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC


def _conveyor():
    """The click_conveyor logic + a couple of its target tags.

    Imported lazily inside each test (after the ``_clean_block_state`` fixture
    has reset ClickBlocks) so the program is built against fresh block state.
    """
    from examples.click_conveyor import ConveyorMotor, Running, logic

    return logic, ConveyorMotor, Running


# ---------------------------------------------------------------------------
# click_conveyor — start/stop motor latch and its NC-reset interlocks
# ---------------------------------------------------------------------------


def test_conveyor_motor_reachable():
    """ConveyorMotor should be reachable: hold StopBtn/EstopOK healthy and Auto,
    pulse StartBtn to latch Running, which gates the motor.  Preserve-tracing
    surfaces the NC-reset interlocks (``StopBtn``/``EstopOK`` held healthy) as
    prerequisites of the latch persisting, so the establish + preserve actions
    widen into one pulse instead of wandering the sort state machine."""
    logic, ConveyorMotor, _Running = _conveyor()
    path = pilot_how(PLC(logic, dt=0.010), ConveyorMotor, max_scans=300)
    assert path.reachable


def test_running_route_ambiguous_resolves():
    """Running's latch is gated ``StartBtn ∧ Or(Auto, Manual)``.  The OR is over
    directly-steerable inputs, so PILOT collapses it (no ``choice=`` needed)
    rather than reporting ambiguous, and preserve-tracing holds the NC resets
    (``StopBtn``/``EstopOK``) healthy so the latch sticks."""
    logic, _ConveyorMotor, Running = _conveyor()
    path = pilot_how(PLC(logic, dt=0.010), Running)
    assert path.reachable
