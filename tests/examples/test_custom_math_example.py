"""Tests for custom_math run_function example."""

from __future__ import annotations

import pytest

from examples.custom_math import weighted_average
from pyrung.core import Bool, Int, PLCRunner, Program, Real, Rung, run_function


def test_weighted_average_end_to_end():
    Enable = Bool("Enable")
    Sensor1 = Int("Sensor1")
    Sensor2 = Int("Sensor2")
    Sensor3 = Int("Sensor3")
    Average = Real("Average")

    with Program() as logic:
        with Rung(Enable):
            run_function(
                weighted_average,
                ins={"sensor1": Sensor1, "sensor2": Sensor2, "sensor3": Sensor3},
                outs={"result": Average},
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
    Sensor3 = Int("Sensor3")
    Result = Real("Result")

    with Program() as logic:
        with Rung(Enable):
            run_function(
                weighted_average,
                ins={
                    "sensor1": Sensor1,
                    "sensor2": Sensor2,
                    "sensor3": Sensor3,
                    "weight1": 0.0,
                    "weight2": 0.0,
                    "weight3": 0.0,
                },
                outs={"result": Result},
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Sensor1": 42, "Sensor2": 100, "Sensor3": 7, "Result": -1.0})
    runner.step()

    assert runner.current_state.tags["Result"] == 0
