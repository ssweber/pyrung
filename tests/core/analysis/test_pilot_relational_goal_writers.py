"""A comparison goal on a tag the program writes: how() looks at what its writers write."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, copy, latch


def test_a_goal_on_a_tag_nothing_reads_finds_the_writer() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.fork.state.tags["Go"] is True
    assert plan.replay().state.tags["Level"] == 5


def test_it_takes_the_writer_whose_value_meets_the_goal() -> None:
    low, high, level = Bool("Low", external=True), Bool("High", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(low):
            copy(2, level)
        with Rung(high):
            copy(9, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.fork.state.tags["High"] is True
    assert plan.replay().state.tags["Level"] == 9


def test_no_writer_meets_the_goal_and_how_does_not_claim_it() -> None:
    go, level = Bool("Go", external=True), Int("Level")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(2, level)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert not plan.reachable


def test_a_goal_on_a_tag_the_program_also_reads_still_reaches() -> None:
    go, level, lamp = Bool("Go", external=True), Int("Level"), Bool("Lamp")
    with Program(strict=False) as prog:
        with Rung(go):
            copy(5, level)
        with Rung(level >= 5):
            latch(lamp)
    plc = PLC(prog, dt=0.01)
    plc.step()

    plan = plc.how(level >= 5)

    assert plan.reachable, plan.reason
    assert plan.replay().state.tags["Level"] == 5
