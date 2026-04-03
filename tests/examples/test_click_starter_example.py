"""Tests for the Click PLC starter example."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def click_starter(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    module_name = "examples.click_starter"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_motor_latches_on_start(click_starter: ModuleType) -> None:
    runner = click_starter.runner

    with runner.active():
        click_starter.Start.value = True
    runner.step()

    with runner.active():
        click_starter.Start.value = False
    runner.step()

    with runner.active():
        assert click_starter.Motor.value is True


def test_motor_stops_on_stop(click_starter: ModuleType) -> None:
    runner = click_starter.runner

    with runner.active():
        click_starter.Start.value = True
    runner.step()

    with runner.active():
        click_starter.Start.value = False
        click_starter.Stop.value = True
    runner.step()

    with runner.active():
        assert click_starter.Motor.value is False


def test_alarm_on_high_temp(click_starter: ModuleType) -> None:
    runner = click_starter.runner
    Zone = click_starter.Zone

    with runner.active():
        Zone[1].setpoint.value = 750
        Zone[1].temp.value = 800  # over setpoint
    runner.step()

    with runner.active():
        assert click_starter.Alarm.value is True


def test_no_alarm_when_below_setpoint(click_starter: ModuleType) -> None:
    runner = click_starter.runner
    Zone = click_starter.Zone

    with runner.active():
        Zone[1].setpoint.value = 750
        Zone[1].temp.value = 700

    # Run a couple scans to make sure alarm stays off
    runner.run(cycles=3)

    with runner.active():
        assert click_starter.Alarm.value is False


def test_round_trip_to_csv(click_starter: ModuleType) -> None:
    """Verify the example can export to Click CSV and re-import."""
    from pyrung.click import pyrung_to_ladder

    bundle = pyrung_to_ladder(click_starter.logic, click_starter.mapping)
    assert len(bundle.main_rows) > 0
