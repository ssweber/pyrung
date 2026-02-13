"""Tests for custom_math example callbacks."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Real, Rung, custom
from pyrung.examples.custom_math import weighted_average


def test_weighted_average_callback_end_to_end():
    Enable = Bool("Enable")
    Sensor1 = Int("Sensor1")
    Sensor2 = Int("Sensor2")
    Sensor3 = Int("Sensor3")
    Average = Real("Average")

    with Program() as logic:
        with Rung(Enable):
            custom(
                weighted_average(
                    inputs=[Sensor1, Sensor2, Sensor3],
                    weights=[0.5, 0.3, 0.2],
                    output=Average,
                )
            )

    runner = PLCRunner(logic=logic)
    runner.patch(
        {
            "Enable": True,
            "Sensor1": 10,
            "Sensor2": 20,
            "Sensor3": 30,
            "Average": 0.0,
        }
    )
    runner.step()

    assert runner.current_state.tags["Average"] == pytest.approx(17.0)


def test_weighted_average_zero_weight_sum_returns_zero():
    Enable = Bool("Enable")
    Sensor1 = Int("Sensor1")
    Sensor2 = Int("Sensor2")
    Result = Real("Result")

    with Program() as logic:
        with Rung(Enable):
            custom(
                weighted_average(
                    inputs=[Sensor1, Sensor2],
                    weights=[0.0, 0.0],
                    output=Result,
                )
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Sensor1": 42, "Sensor2": 100, "Result": -1.0})
    runner.step()

    assert runner.current_state.tags["Result"] == 0
