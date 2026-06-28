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

import pytest

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


@pytest.mark.xfail(reason="pilot: NC-reset latch under state-machine churn")
def test_conveyor_motor_reachable():
    """ConveyorMotor should be reachable: hold StopBtn/EstopOK healthy and Auto,
    pulse StartBtn to latch Running, which gates the motor.  PILOT currently
    exhausts its budget wandering the sort state machine instead."""
    logic, ConveyorMotor, _Running = _conveyor()
    # Bounded budget: PILOT makes no progress here, so a small cap fails fast
    # without changing the outcome (it exhausts 3000 scans wandering otherwise).
    path = pilot_how(PLC(logic, dt=0.010), ConveyorMotor, max_scans=300)
    assert path.reachable


@pytest.mark.xfail(reason="pilot: route-ambiguous single-target resolution")
def test_running_route_ambiguous_resolves():
    """Running has multiple writers (one latch, two NC resets).  PILOT reports
    the target as ambiguous instead of resolving to the latch route on its own;
    it should be able to pick a route without an explicit ``choice=``."""
    logic, _ConveyorMotor, Running = _conveyor()
    path = pilot_how(PLC(logic, dt=0.010), Running)
    assert path.reachable
