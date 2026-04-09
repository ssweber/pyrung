"""Tests for the Click conveyor sorting station example."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def click_conveyor(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    module_name = "examples.click_conveyor"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def _set_nc_inputs(mod: ModuleType) -> None:
    """Set NC-wired inputs True to simulate healthy wiring."""
    with mod.runner:
        mod.StopBtn.value = True
        mod.EstopOK.value = True


def test_motor_latches_on_start(click_conveyor: ModuleType) -> None:
    runner = click_conveyor.runner
    _set_nc_inputs(click_conveyor)

    with runner:
        click_conveyor.Auto.value = True
        click_conveyor.StartBtn.value = True
        runner.step()

    # Finger off the button — motor stays on (latched)
    with runner:
        click_conveyor.StartBtn.value = False
        runner.step()

    with runner:
        assert click_conveyor.Running.value is True
        assert click_conveyor.ConveyorMotor.value is True


def test_motor_stops_on_stop(click_conveyor: ModuleType) -> None:
    runner = click_conveyor.runner
    _set_nc_inputs(click_conveyor)

    with runner:
        click_conveyor.Auto.value = True
        click_conveyor.StartBtn.value = True
        runner.step()

    # NC stop button pressed (opens circuit)
    with runner:
        click_conveyor.StopBtn.value = False
        runner.step()

    with runner:
        assert click_conveyor.Running.value is False
        assert click_conveyor.ConveyorMotor.value is False


def test_estop_overrides_start(click_conveyor: ModuleType) -> None:
    runner = click_conveyor.runner
    _set_nc_inputs(click_conveyor)

    with runner:
        click_conveyor.Auto.value = True
        click_conveyor.StartBtn.value = True
        runner.step()

    # Safety relay trips: EstopOK goes False
    with runner:
        click_conveyor.EstopOK.value = False
        runner.step()

    with runner:
        assert click_conveyor.Running.value is False
        assert click_conveyor.ConveyorMotor.value is False


def test_sort_large_box(click_conveyor: ModuleType) -> None:
    """Large box: diverter extends during sorting phase."""
    runner = click_conveyor.runner
    _set_nc_inputs(click_conveyor)

    with runner:
        click_conveyor.Auto.value = True
        click_conveyor.SizeThreshold.value = 100
        click_conveyor.StartBtn.value = True
        runner.step()

    # Box arrives — large
    with runner:
        click_conveyor.EntrySensor.value = True
        click_conveyor.SizeReading.value = 150
        runner.step()

    with runner:
        assert click_conveyor.State.value == 1  # Detecting

    # Run through detection (0.5s = 50 scans)
    runner.run(cycles=50)

    with runner:
        assert click_conveyor.State.value == 2  # Sorting
        assert click_conveyor.DiverterCmd.value is True  # Extended


def test_sort_small_box(click_conveyor: ModuleType) -> None:
    """Small box: diverter stays retracted."""
    runner = click_conveyor.runner
    _set_nc_inputs(click_conveyor)

    with runner:
        click_conveyor.Auto.value = True
        click_conveyor.SizeThreshold.value = 100
        click_conveyor.StartBtn.value = True
        runner.step()

    # Box arrives — small
    with runner:
        click_conveyor.EntrySensor.value = True
        click_conveyor.SizeReading.value = 50
        runner.step()

    # Run through detection
    runner.run(cycles=50)

    with runner:
        assert click_conveyor.State.value == 2
        assert click_conveyor.DiverterCmd.value is False  # Retracted


def test_bin_counter(click_conveyor: ModuleType) -> None:
    runner = click_conveyor.runner

    with runner:
        for _ in range(3):
            click_conveyor.BinASensor.value = True
            runner.step()
            click_conveyor.BinASensor.value = False
            runner.step()

        assert click_conveyor.BinACounter.acc.value == 3
        assert click_conveyor.BinBCounter.acc.value == 0


def test_round_trip_to_csv(click_conveyor: ModuleType) -> None:
    """Verify the example can export to Click CSV and re-import."""
    from pyrung.click import pyrung_to_ladder

    bundle = pyrung_to_ladder(click_conveyor.logic, click_conveyor.mapping)
    assert len(bundle.main_rows) > 0
